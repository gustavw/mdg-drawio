"""Tests for the reverse derivation POC (:mod:`mdg_drawio.reverse`).

Groups, roughly in dependency order:

* scoring/parsing unit tests (no data);
* synthetic ranking-policy tests -- a tiny hand-built :class:`StyleIndex` pins
  every branch of the vote/prior/band/confidence policy exactly, independent of
  the real palette (no ``make build-data`` needed, so these run everywhere and
  never rot if the vendored palette changes);
* ``load_cells`` XML-parsing edge cases (no data), including a regression test
  for a real bug this suite caught: object-wrapped cells losing their id;
* ``fixtures`` helper unit tests (no data);
* ``naming`` semantic-id assignment tests (no data);
* CLI (``mdg_drawio.reverse.derive_cli``) tests, via a monkeypatched index (no data);
* data-gated end-to-end tests against the real palette -- the two version-
  priority scenarios from the design discussion, plus corpus-wide sanity
  checks. These skip without ``make build-data`` (palette styles are
  git-ignored, draw.io-copyright).
"""
from __future__ import annotations

import base64
import json
import re
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mdg_drawio.reverse import fixtures as fx
from mdg_drawio.reverse.derive import (
    DEFAULT_BAND,
    DEFAULT_SIM_FLOOR,
    Cell,
    derive,
    load_cells,
    parent_map,
    rewrite_cell_ids,
)
from mdg_drawio.reverse.derive_cli import main
from mdg_drawio.reverse.naming import assign_semantic_ids, semantic_base
from mdg_drawio.reverse.scoring import (
    BARE,
    COSMETIC_KEYS,
    SHAPE_KEYS,
    Weights,
    parse_style,
    similarity,
)
from mdg_drawio.reverse.style_index import ShapeEntry, StyleIndex

INDEX = StyleIndex.load()
needs_data = pytest.mark.skipif(
    not INDEX.entries, reason="needs palette styles (run `make build-data`)"
)


# ── scoring / parsing (no data) ──────────────────────────────────────────────
def test_parse_style_bare_and_kv() -> None:
    tokens = parse_style("ellipse;fillColor=#fff;rounded=1;")
    assert tokens["ellipse"] is BARE
    assert tokens["fillColor"] == "#fff"
    assert tokens["rounded"] == "1"


def test_parse_style_empty_string_yields_no_tokens() -> None:
    assert parse_style("") == {}
    assert parse_style(";;;") == {}


def test_identical_styles_are_perfectly_similar() -> None:
    tokens = parse_style("shape=widget;html=1;fillColor=#083F75")
    assert similarity(tokens, tokens) == 1.0


def test_disjoint_styles_have_zero_similarity() -> None:
    a = parse_style("shape=a;html=1")
    b = parse_style("rounded=1;dashed=1")
    assert similarity(a, b) == 0.0


def test_similarity_of_two_empty_styles_is_zero_not_a_division_error() -> None:
    assert similarity({}, {}) == 0.0


def test_cosmetic_difference_barely_changes_similarity() -> None:
    base = parse_style("shape=mxgraph.x;html=1;fillColor=#083F75;fontColor=#fff")
    recoloured = parse_style("shape=mxgraph.x;html=1;fillColor=#FF0000;fontColor=#000")
    # Two cosmetic tokens differ but the shape survives with a high score.
    assert similarity(base, recoloured) > 0.85


def test_shape_difference_tanks_similarity() -> None:
    a = parse_style("shape=mxgraph.a;html=1;fillColor=#083F75")
    b = parse_style("shape=mxgraph.b;html=1;fillColor=#083F75")
    assert similarity(a, b) < 0.5


def test_colour_breaks_tie_between_identical_shapes() -> None:
    # The C4 Person vs Person_Ext case: identical but for fill/stroke colour.
    person = parse_style("shape=mxgraph.c4.person2;html=1;fillColor=#083F75")
    person_ext = parse_style("shape=mxgraph.c4.person2;html=1;fillColor=#6C6477")
    query = parse_style("shape=mxgraph.c4.person2;html=1;fillColor=#083F75")
    assert similarity(query, person) > similarity(query, person_ext)


def test_raising_cosmetic_weight_increases_colour_sensitivity() -> None:
    # Weights are meant to be tunable: giving cosmetic tokens the same weight
    # as shape/structural ones makes a colour mismatch cost proportionally
    # more of the total, widening (not narrowing) the person/person_ext gap.
    person = parse_style("shape=mxgraph.c4.person2;html=1;fillColor=#083F75")
    person_ext = parse_style("shape=mxgraph.c4.person2;html=1;fillColor=#6C6477")
    query = parse_style("shape=mxgraph.c4.person2;html=1;fillColor=#083F75")
    flat = Weights(shape=1.0, structural=1.0, cosmetic=1.0)

    default_gap = similarity(query, person) - similarity(query, person_ext)
    flat_gap = similarity(query, person, flat) - similarity(query, person_ext, flat)
    assert flat_gap > default_gap > 0


def test_arrow_ends_outweigh_an_unrelated_shared_token() -> None:
    """A real bug: an ERD crow's-foot edge (``endArrow=ERmandOne``, no
    ``shape=`` to anchor on) was matching a plain unmarked line over its own
    dedicated cardinality shape, because the plain line happened to also
    share an unrelated token (``rounded=0``) the dedicated shape lacks.
    Treating the arrow ends as shape-defining (weight ``shape``, not
    ``structural``) fixes this: the correct semantic match now wins even
    though it disagrees on more *other* tokens."""
    query = parse_style("fontSize=12;html=1;endArrow=ERmandOne;rounded=0;")
    plain_line = parse_style("endArrow=none;html=1;rounded=0;")
    mandatory_one = parse_style(
        "edgeStyle=entityRelationEdgeStyle;fontSize=12;html=1;endArrow=ERmandOne;"
    )
    assert similarity(query, mandatory_one) > similarity(query, plain_line)


# ── synthetic ranking policy (no data) ───────────────────────────────────────
def _entry(
    shape_id: str, library: str, style: str, kind: str = "vertex"
) -> ShapeEntry:
    return ShapeEntry(
        shape_id, library, style, f"sha1:{shape_id}", parse_style(style), kind=kind
    )


def _cell(style: str, cell_id: str = "1", is_edge: bool = False) -> Cell:
    return Cell(cell_id, style, "", parse_style(style), is_edge=is_edge)


WIDGET_STYLE = "shape=widget;html=1;"
ANCHOR_STYLE = "shape=anchorshape;html=1;"


def _synthetic_index() -> StyleIndex:
    """uml/uml25 share ``widget`` (mirrors the real lifeline collision); ``uml``
    alone also has an unambiguous anchor and a same-library near-tied twin."""
    return StyleIndex(
        [
            _entry("uml.widget.v1", "uml", WIDGET_STYLE),
            _entry("uml25.widget.v1", "uml25", WIDGET_STYLE),
            _entry("uml.anchor.v1", "uml", ANCHOR_STYLE),
            _entry("uml.twin_a.v1", "uml", "shape=twin;html=1;"),
            _entry("uml.twin_b.v1", "uml", "shape=twin;html=1;"),
            _entry("other.thing.v1", "other", "shape=something;html=1;"),
        ]
    )


def test_unique_candidate_resolves_confidently() -> None:
    idx = _synthetic_index()
    result = derive([_cell(ANCHOR_STYLE)], idx)
    cell = result.cells[0]
    assert cell.chosen is not None
    assert cell.chosen.shape_id == "uml.anchor.v1"
    assert cell.resolved_by == "unique"
    assert cell.confidence == pytest.approx(1.0)


def test_same_library_tie_resolves_as_single_library() -> None:
    idx = _synthetic_index()
    result = derive([_cell("shape=twin;html=1;")], idx)
    cell = result.cells[0]
    assert cell.chosen is not None
    assert cell.chosen.library == "uml"
    assert cell.resolved_by == "single-library"
    assert cell.confidence == pytest.approx(0.85)


def test_lone_ambiguous_shape_falls_to_recency_prior() -> None:
    """No anchors present: the ambiguous shape defaults to the newest version,
    and a second identical ambiguous cell resolves the same way (the prior is
    per-library, not per-cell -- it must not stack)."""
    idx = _synthetic_index()
    result = derive([_cell(WIDGET_STYLE, "10"), _cell(WIDGET_STYLE, "11")], idx)
    for cell in result.cells:
        assert cell.chosen is not None
        assert cell.chosen.library == "uml25"
        assert cell.resolved_by == "recency-prior"
        assert cell.confidence == pytest.approx(0.45)
    assert result.library_scores["uml25"] == pytest.approx(0.2)
    assert result.library_scores["uml"] == pytest.approx(0.1)


def test_anchor_vote_outranks_the_recency_prior() -> None:
    """The core design invariant: one anchor vote for the OLDER library beats
    the newer library's recency prior, pulling the ambiguous shape with it."""
    idx = _synthetic_index()
    result = derive([_cell(WIDGET_STYLE, "10"), _cell(ANCHOR_STYLE, "11")], idx)
    widget_cell = next(c for c in result.cells if c.cell_id == "10")
    anchor_cell = next(c for c in result.cells if c.cell_id == "11")

    assert widget_cell.chosen is not None
    assert widget_cell.chosen.library == "uml"
    assert widget_cell.resolved_by == "library-vote"
    assert anchor_cell.resolved_by == "unique"
    assert result.library_scores["uml"] > result.library_scores["uml25"]


def test_below_floor_similarity_resolves_to_no_match() -> None:
    idx = _synthetic_index()
    result = derive([_cell("totallyunrelatedtoken=xyz;other=abc;")], idx)
    cell = result.cells[0]
    assert cell.chosen is None
    assert cell.candidates == []
    assert cell.resolved_by == "none"
    assert cell.confidence == 0.0


def test_edge_cell_never_resolves_to_a_vertex_only_entry() -> None:
    """Regression: an edge cell used to be scored against the WHOLE index,
    vertex entries included -- a bare edge style with only boilerplate
    tokens (html=1;rounded=0;) could out-score every real edge candidate
    against a vertex whose canonical style happens to be equally bare (a
    composite shape's near-empty anchor cell), silently "resolving" the
    edge to a shape it structurally cannot be."""
    idx = StyleIndex(
        [_entry("lib.bareanchor.v1", "lib", "html=1;rounded=0;", kind="vertex")]
    )
    result = derive(
        [_cell("edgeStyle=orthogonalEdgeStyle;html=1;rounded=0;", is_edge=True)], idx
    )
    cell = result.cells[0]
    assert cell.chosen is None
    assert cell.candidates == []


def test_vertex_cell_never_resolves_to_an_edge_only_entry() -> None:
    idx = StyleIndex([_entry("lib.rel.v1", "lib", "html=1;rounded=0;", kind="edge")])
    result = derive([_cell("html=1;rounded=0;", is_edge=False)], idx)
    cell = result.cells[0]
    assert cell.chosen is None
    assert cell.candidates == []


def test_band_widens_or_narrows_the_candidate_set() -> None:
    # x vs the query: shared shape+html (weight 13) / +1 extra cosmetic token
    # on the query alone -> similarity 13/14 ~= 0.9286, a hand-computable gap.
    idx = StyleIndex(
        [
            _entry("libA.x.v1", "libA", "shape=multi;html=1;"),
            _entry("libB.y.v1", "libB", "shape=multi;html=1;fillColor=#111111;"),
        ]
    )
    query = _cell("shape=multi;html=1;fillColor=#111111;")

    narrow = derive([query], idx, band=0.02).cells[0]
    assert {c.shape_id for c in narrow.candidates} == {"libB.y.v1"}

    wide = derive([query], idx, band=0.08).cells[0]
    assert {c.shape_id for c in wide.candidates} == {"libA.x.v1", "libB.y.v1"}


def test_reported_candidates_are_capped_at_eight() -> None:
    idx = StyleIndex(
        [_entry(f"uml.dup{i}.v1", "uml", "shape=dup;html=1;") for i in range(10)]
    )
    cell = derive([_cell("shape=dup;html=1;")], idx).cells[0]
    assert cell.chosen is not None
    assert len(cell.candidates) == 8


def test_defaults_are_the_module_constants() -> None:
    # Pins these tuning constants so changing one is a deliberate, reviewable
    # diff here, not an incidental edit buried in derive.py.
    assert DEFAULT_SIM_FLOOR == 0.4
    assert DEFAULT_BAND == 0.02


# ── load_cells XML parsing (no data) ─────────────────────────────────────────
def test_load_cells_handles_compressed_diagram() -> None:
    inner = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="ellipse;html=1;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    payload = zlib.compressobj(-1, zlib.DEFLATED, -15)
    packed = payload.compress(inner.encode()) + payload.flush()
    b64 = base64.b64encode(packed).decode()
    doc = f'<mxfile><diagram name="P">{b64}</diagram></mxfile>'
    cells = load_cells(doc)
    assert [c.cell_id for c in cells] == ["2"]
    assert cells[0].tokens["ellipse"] is BARE


def test_object_wrapped_cells_keep_their_own_id() -> None:
    """Regression: drawio puts an object cell's id on ``<object>``, not on its
    inner ``<mxCell>`` (see extract_shapes.js's cellsToXml). Before the fix,
    every object-wrapped cell's inner element read as id "", so a document with
    more than one -- e.g. two C4 Person nodes -- collapsed to a single cell."""
    doc = (
        '<mxfile><diagram name="P"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<object id="5" label="A"><mxCell style="shape=a;" vertex="1" '
        'parent="1"/></object>'
        '<object id="6" label="B"><mxCell style="shape=b;" vertex="1" '
        'parent="1"/></object>'
        '<mxCell id="7" style="shape=c;" vertex="1" parent="1"/>'
        "</root></mxGraphModel></diagram></mxfile>"
    )
    cells = load_cells(doc)
    assert [(c.cell_id, c.style) for c in cells] == [
        ("5", "shape=a;"),
        ("6", "shape=b;"),
        ("7", "shape=c;"),
    ]


def test_duplicate_cell_id_keeps_only_the_first() -> None:
    doc = (
        '<mxfile><diagram name="P"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="9" style="shape=first;" vertex="1" parent="1"/>'
        '<mxCell id="9" style="shape=second;" vertex="1" parent="1"/>'
        "</root></mxGraphModel></diagram></mxfile>"
    )
    cells = load_cells(doc)
    assert len(cells) == 1
    assert cells[0].style == "shape=first;"


def test_empty_style_cells_are_skipped() -> None:
    doc = (
        '<mxfile><diagram name="P"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="9" style="" vertex="1" parent="1"/>'
        '<mxCell id="10" style="shape=a;" vertex="1" parent="1"/>'
        "</root></mxGraphModel></diagram></mxfile>"
    )
    cells = load_cells(doc)
    assert [c.cell_id for c in cells] == ["10"]


def test_load_cells_skips_a_styled_element_with_no_id() -> None:
    """Regression: a styled vertex with no id attribute at all used to be
    KEPT (as cell_id="") by load_cells but DROPPED by parent_map -- a cell
    that can't be identified can't be tracked by anything downstream
    (containment, naming, merge dedup), and the desync made it vanish from
    containment resolution with no warning at all. Both functions must agree:
    an id-less cell is skipped entirely."""
    doc = (
        '<mxfile><diagram name="P"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell style="shape=a;" vertex="1" parent="1"/>'
        '<mxCell id="10" style="shape=b;" vertex="1" parent="1"/>'
        "</root></mxGraphModel></diagram></mxfile>"
    )
    cells = load_cells(doc)
    assert [c.cell_id for c in cells] == ["10"]


def test_load_cells_and_parent_map_agree_when_an_id_is_missing() -> None:
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell style="shape=a;" vertex="1" parent="1"/>'
        '<mxCell id="10" style="shape=b;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    cell_ids = {c.cell_id for c in load_cells(doc)}
    assert cell_ids <= set(parent_map(doc))


def test_multiple_diagram_pages_are_all_collected() -> None:
    plain = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="20" style="shape=page1;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    compressed_inner = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="21" style="shape=page2;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    payload = zlib.compressobj(-1, zlib.DEFLATED, -15)
    packed = payload.compress(compressed_inner.encode()) + payload.flush()
    b64 = base64.b64encode(packed).decode()
    doc = (
        "<mxfile>"
        f'<diagram name="Page-1">{plain}</diagram>'
        f'<diagram name="Page-2">{b64}</diagram>'
        "</mxfile>"
    )
    cells = load_cells(doc)
    # Multi-page documents are page-prefixed (see the collision regression
    # below) -- ids "20"/"21" don't collide here, but still get the "N:"
    # prefix, since whether a document is multi-page is a property of the
    # whole document, not of any one page's particular ids.
    assert {c.cell_id for c in cells} == {"0:20", "1:21"}


def test_multi_page_documents_do_not_silently_merge_colliding_ids() -> None:
    """Regression: draw.io numbers ids independently per page, so the SAME raw
    id recurring across pages is common, not a data error. Before page-
    prefixing, load_cells' cell_id-only dedup silently dropped every page-2+
    cell whose id happened to repeat a page-1 id."""
    page1 = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="shape=page1thing;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    page2 = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="shape=page2thing;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    doc = (
        "<mxfile>"
        f'<diagram name="Page-1">{page1}</diagram>'
        f'<diagram name="Page-2">{page2}</diagram>'
        "</mxfile>"
    )
    cells = load_cells(doc)
    assert [(c.cell_id, c.style) for c in cells] == [
        ("0:2", "shape=page1thing;"),
        ("1:2", "shape=page2thing;"),
    ]


def test_single_page_document_ids_stay_unprefixed() -> None:
    """The overwhelmingly common case (one page) keeps bare ids -- no needless
    "0:" prefix when there's nothing to disambiguate against."""
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="5" style="shape=solo;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    cells = load_cells(doc)
    assert [c.cell_id for c in cells] == ["5"]


def test_parent_map_covers_styled_and_styleless_cells_alike() -> None:
    """Unlike load_cells, parent_map must include layers/groups too -- a
    resolved cell's containment ancestry commonly passes through them."""
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="layer1" parent="1"/>'
        '<mxCell id="9" style="shape=a;" vertex="1" parent="layer1"/>'
        "</root></mxGraphModel>"
    )
    mapping = parent_map(doc)
    assert mapping["9"].parent_id == "layer1"
    assert mapping["layer1"].parent_id == "1"
    assert mapping["layer1"].style == ""  # a layer: present, but styleless


def test_parent_map_reads_parent_off_object_wrapped_cells() -> None:
    """An object cell's `parent` lives on the inner mxCell already (only its
    `id` needed the object->inner copy-down fix) -- verify it comes through."""
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<object id="5" label="A"><mxCell style="shape=a;" vertex="1" '
        'parent="1"/></object>'
        "</root></mxGraphModel>"
    )
    mapping = parent_map(doc)
    assert mapping["5"].parent_id == "1"
    assert mapping["5"].style == "shape=a;"


def test_parent_map_flags_edges() -> None:
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="8" style="shape=a;" vertex="1" parent="1"/>'
        '<mxCell id="9" style="x;" edge="1" parent="1" source="8" target="8"/>'
        "</root></mxGraphModel>"
    )
    mapping = parent_map(doc)
    assert mapping["8"].is_edge is False
    assert mapping["9"].is_edge is True


def test_parent_map_and_load_cells_agree_on_multi_page_ids() -> None:
    """The two functions must use identical page-prefixing so a resolved
    cell's id can be looked up directly in the raw parent map."""
    page1 = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="shape=x;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    page2 = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="shape=y;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    doc = (
        "<mxfile>"
        f'<diagram name="Page-1">{page1}</diagram>'
        f'<diagram name="Page-2">{page2}</diagram>'
        "</mxfile>"
    )
    cell_ids = {c.cell_id for c in load_cells(doc)}
    assert cell_ids <= set(parent_map(doc))


# ── rewrite_cell_ids (no data) ────────────────────────────────────────────────
def test_rewrite_cell_ids_returns_none_for_empty_renames() -> None:
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        "</root></mxGraphModel>"
    )
    assert rewrite_cell_ids(doc, {}) is None


def test_rewrite_cell_ids_returns_none_when_nothing_matches() -> None:
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="5" style="shape=a;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    assert rewrite_cell_ids(doc, {"nonexistent": "e1"}) is None


def test_rewrite_cell_ids_renames_id_and_every_reference() -> None:
    """A renamed vertex's id must update everywhere it's referenced: a
    child's `parent`, and an edge's `source`/`target`."""
    doc = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="5" style="shape=a;" vertex="1" parent="1"/>'
        '<mxCell id="6" style="shape=b;" vertex="1" parent="5"/>'
        '<mxCell id="7" style="edgeStyle=x;" edge="1" parent="1" '
        'source="5" target="6"/>'
        "</root></mxGraphModel>"
    )
    out = rewrite_cell_ids(doc, {"5": "entityrect1"})
    assert out is not None
    cells = {c.get("id"): c for c in ET.fromstring(out).iter("mxCell")}
    assert "5" not in cells
    assert cells["entityrect1"].get("style") == "shape=a;"
    assert cells["6"].get("parent") == "entityrect1"
    assert cells["7"].get("source") == "entityrect1"
    assert cells["7"].get("target") == "6"  # untouched -- not renamed


def test_rewrite_cell_ids_renames_the_wrapper_of_an_object_cell() -> None:
    doc = (
        '<mxfile><diagram name="P"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<object id="5" label="A"><mxCell style="shape=a;" vertex="1" '
        'parent="1"/></object>'
        "</root></mxGraphModel></diagram></mxfile>"
    )
    out = rewrite_cell_ids(doc, {"5": "entityrect1"})
    assert out is not None
    root = ET.fromstring(out)
    obj = next(root.iter("object"))
    assert obj.get("id") == "entityrect1"
    cells = load_cells(out)
    assert [c.cell_id for c in cells] == ["entityrect1"]


def test_rewrite_cell_ids_respects_multi_page_prefixes() -> None:
    """Only the targeted page's cell is renamed -- the SAME raw id on
    another page, not named in `renames`, is left untouched."""
    page = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="shape=x;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    doc = (
        "<mxfile>"
        f'<diagram name="Page-1">{page}</diagram>'
        f'<diagram name="Page-2">{page}</diagram>'
        "</mxfile>"
    )
    out = rewrite_cell_ids(doc, {"1:2": "entityrect1"})
    assert out is not None
    models = list(ET.fromstring(out).iter("mxGraphModel"))
    page1_ids = {c.get("id") for c in models[0].iter("mxCell")}
    page2_ids = {c.get("id") for c in models[1].iter("mxCell")}
    assert "2" in page1_ids and "entityrect1" not in page1_ids
    assert "entityrect1" in page2_ids and "2" not in page2_ids


def test_rewrite_cell_ids_converts_a_compressed_page_to_inline_xml() -> None:
    """A renamed compressed page is rewritten as inline XML rather than
    re-compressed -- draw.io reads both forms interchangeably."""
    inner = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="ellipse;html=1;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    payload = zlib.compressobj(-1, zlib.DEFLATED, -15)
    packed = payload.compress(inner.encode()) + payload.flush()
    b64 = base64.b64encode(packed).decode()
    doc = f'<mxfile><diagram name="P">{b64}</diagram></mxfile>'

    out = rewrite_cell_ids(doc, {"2": "entityrect1"})
    assert out is not None
    diagram = next(ET.fromstring(out).iter("diagram"))
    assert diagram.text is None or not diagram.text.strip()
    assert diagram.find("mxGraphModel") is not None
    cells = load_cells(out)
    assert [c.cell_id for c in cells] == ["entityrect1"]


def test_malformed_xml_raises() -> None:
    with pytest.raises(ET.ParseError):
        load_cells("<mxfile><diagram>not closed")


# ── fixtures helper (no data) ─────────────────────────────────────────────────
def test_perturb_replaces_an_existing_token_without_duplicating() -> None:
    style = "shape=widget;fillColor=#111111;html=1;"
    perturbed = fx.perturb(style, fillColor="#FF0000")
    tokens = parse_style(perturbed)
    assert tokens["fillColor"] == "#FF0000"
    assert perturbed.count("fillColor=") == 1


def test_perturb_appends_a_token_that_was_absent() -> None:
    style = "shape=widget;html=1;"
    perturbed = fx.perturb(style, fontFamily="Comic Sans MS")
    assert parse_style(perturbed)["fontFamily"] == "Comic Sans MS"


def test_special_characters_in_style_round_trip_through_load_cells() -> None:
    entry = _entry("x.y.v1", "x", 'shape=widget;label="quoted";html=1;')
    doc = fx.document(fx.entry_cell(entry, cell_id="5"))
    cells = load_cells(doc)
    assert cells[0].style == entry.style


# ── naming: semantic .mdg node ids (no data) ─────────────────────────────────
def test_semantic_base_extracts_the_middle_segment() -> None:
    assert semantic_base("c4.person_ext.v2") == "person_ext"
    assert semantic_base("uml25.lifelinestateinvariant.v1") == "lifelinestateinvariant"


def test_semantic_base_falls_back_to_the_whole_id_if_non_conforming() -> None:
    assert semantic_base("solo") == "solo"


def test_assign_semantic_ids_counts_per_base_in_document_order() -> None:
    """Two ambiguous cells resolving to the same shape share one counter."""
    idx = _synthetic_index()
    result = derive([_cell(WIDGET_STYLE, "10"), _cell(WIDGET_STYLE, "11")], idx)
    assigned = assign_semantic_ids(result)
    assert [(a.cell_id, a.node_id) for a in assigned] == [
        ("10", "widget1"),
        ("11", "widget2"),
    ]
    assert {a.base for a in assigned} == {"widget"}


def test_assign_semantic_ids_gives_each_distinct_base_its_own_counter() -> None:
    idx = _synthetic_index()
    cells = [
        _cell(ANCHOR_STYLE, "10"),
        _cell(WIDGET_STYLE, "11"),
        _cell(WIDGET_STYLE, "12"),
    ]
    result = derive(cells, idx)
    assigned = {a.cell_id: a.node_id for a in assign_semantic_ids(result)}
    assert assigned["10"] == "anchor1"
    # Both widget cells are ambiguous (no anchor for either version present
    # here) and fall to the recency prior -> the same shape -> one counter.
    assert assigned["11"] == "widget1"
    assert assigned["12"] == "widget2"


def test_assign_semantic_ids_are_globally_unique_across_libraries() -> None:
    """Every notation shares the document-wide ``node_id`` namespace."""
    idx = StyleIndex(
        [
            _entry("uml.widget.v1", "uml", "shape=uw;"),
            _entry("uml25.widget.v1", "uml25", "shape=u25w;"),
            _entry("other.widget.v1", "other", "shape=ow;"),
        ]
    )
    cells = [
        _cell("shape=uw;", "10"),
        _cell("shape=u25w;", "11"),
        _cell("shape=ow;", "12"),
    ]
    result = derive(cells, idx)
    assigned = {a.cell_id: a.node_id for a in assign_semantic_ids(result)}
    assert assigned["10"] == "widget1"  # uml
    assert assigned["11"] == "widget2"  # uml25 -- shares uml's counter (by design)
    assert assigned["12"] == "widget3"  # unrelated library, same global id namespace


@needs_data
def test_naming_c4_container_and_general_container_get_unique_ids() -> None:
    """Real registry shapes with the same base still need unique node ids."""
    c4_container = fx.get(INDEX, "c4.container.v1")
    general_container = fx.get(INDEX, "general.container.v1")
    doc = fx.document(
        fx.entry_cell(c4_container, cell_id="10", x=0, parent="1"),
        fx.entry_cell(general_container, cell_id="11", x=200, parent="1"),
    )
    result = derive(load_cells(doc), INDEX)
    assigned = {a.cell_id: a.node_id for a in assign_semantic_ids(result)}
    assert assigned["10"] == "container1"
    assert assigned["11"] == "container2"


def test_assign_semantic_ids_skips_unresolved_cells() -> None:
    idx = _synthetic_index()
    result = derive(
        [_cell(ANCHOR_STYLE, "10"), _cell("totallyunrelated=xyz;", "99")], idx
    )
    assigned = {a.cell_id: a.node_id for a in assign_semantic_ids(result)}
    assert assigned == {"10": "anchor1"}
    assert "99" not in assigned


# ── CLI (no data; monkeypatched index) ────────────────────────────────────────
def test_cli_returns_error_when_no_style_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        StyleIndex, "load", classmethod(lambda cls, data_dir=None: StyleIndex([]))
    )
    rc = main(["unused.drawio"])
    assert rc == 2
    assert "make build-data" in capsys.readouterr().err


def test_cli_json_output_reports_the_chosen_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    idx = _synthetic_index()
    monkeypatch.setattr(
        StyleIndex, "load", classmethod(lambda cls, data_dir=None: idx)
    )
    anchor = fx.get(idx, "uml.anchor.v1")
    path = tmp_path / "diagram.drawio"
    path.write_text(fx.document(fx.entry_cell(anchor, cell_id="10")), encoding="utf-8")

    rc = main([str(path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cells"][0]["shape_id"] == "uml.anchor.v1"
    assert payload["cells"][0]["node_id"] == "anchor1"
    assert payload["cells"][0]["library"] == "uml"
    assert payload["cells"][0]["resolved_by"] == "unique"
    assert "library_scores" in payload


def test_cli_table_output_lists_matches_and_no_match_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    idx = _synthetic_index()
    monkeypatch.setattr(
        StyleIndex, "load", classmethod(lambda cls, data_dir=None: idx)
    )
    anchor = fx.get(idx, "uml.anchor.v1")
    unmatched = fx.cell_xml(
        "99", "totallyunrelatedtoken=xyz;", x=300, width=10, height=10
    )
    path = tmp_path / "diagram.drawio"
    path.write_text(
        fx.document(fx.entry_cell(anchor, cell_id="10"), unmatched), encoding="utf-8"
    )

    rc = main([str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "library scores:" in out
    assert "uml.anchor.v1" in out
    assert "anchor1" in out
    assert "(no match)" in out


@needs_data
def test_cli_reports_containment_for_a_nested_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end through main(): a Person nested in a System_Boundary reports
    its container's semantic node id and depth in both output modes."""
    monkeypatch.setattr(
        StyleIndex, "load", classmethod(lambda cls, data_dir=None: INDEX)
    )
    boundary = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    path = tmp_path / "diagram.drawio"
    path.write_text(
        fx.document(
            fx.entry_cell(boundary, cell_id="10", parent="1"),
            fx.entry_cell(person, cell_id="11", parent="10"),
        ),
        encoding="utf-8",
    )

    rc = main([str(path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    boundary_cell = next(c for c in payload["cells"] if c["cell_id"] == "10")
    person_cell = next(c for c in payload["cells"] if c["cell_id"] == "11")
    assert person_cell["depth"] == 1
    assert person_cell["container_node_id"] == boundary_cell["node_id"]
    assert person_cell["containment_warnings"] == []

    rc = main([str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert person_cell["container_node_id"] in out


# ── ranking scenarios (need generated data) ──────────────────────────────────
@needs_data
def test_scenario_lone_ambiguous_shape_prefers_newest_version() -> None:
    """A solitary lifeline (uml/uml25 collision) defaults to uml25."""
    lifeline = fx.find(INDEX, "uml25", "lifeline")
    doc = fx.document(fx.entry_cell(lifeline, cell_id="10"))
    result = derive(load_cells(doc), INDEX)
    chosen = result.cells[0].chosen
    assert chosen is not None
    assert chosen.library == "uml25"
    assert "lifeline" in chosen.shape_id
    assert result.cells[0].resolved_by == "recency-prior"


@needs_data
def test_scenario_anchor_pulls_ambiguous_shape_to_older_library() -> None:
    """Lifeline + a uml-only shape: the anchor pulls the lifeline to uml."""
    lifeline = fx.find(INDEX, "uml25", "lifeline")
    anchor = fx.library_only_anchor(INDEX, "uml")
    doc = fx.document(
        fx.entry_cell(lifeline, cell_id="10", x=0),
        fx.entry_cell(anchor, cell_id="11", x=240),
    )
    result = derive(load_cells(doc), INDEX)
    lifeline_cell = next(c for c in result.cells if c.cell_id == "10")
    assert lifeline_cell.chosen is not None
    assert lifeline_cell.chosen.library == "uml"
    assert "lifeline" in lifeline_cell.chosen.shape_id
    assert lifeline_cell.resolved_by == "library-vote"


@needs_data
def test_default_edge_style_never_resolves_to_a_vertex_shape() -> None:
    """A real bug: a bare default connector (drawn with no distinguishing
    ``shape=`` token) resolved to uml25.portwithprovidedinterface.v1 -- a
    VERTEX (a UML port icon), because that composite shape's canonical style
    is its near-empty anchor cell (html=1;rounded=0;), which a plain default
    edge also carries."""
    doc = fx.document(fx.edge_cell_xml("e1", "src", "tgt"))
    result = derive(load_cells(doc), INDEX)
    chosen = result.cells[0].chosen
    if chosen is not None:
        assert fx.get(INDEX, chosen.shape_id).kind == "edge"
        assert chosen.shape_id != "uml25.portwithprovidedinterface.v1"


def _structurally_unique_entry(index: StyleIndex) -> ShapeEntry:
    """First entry whose non-cosmetic tokens are unique across the index."""

    def structural(tokens: dict[str, object]) -> frozenset[tuple[str, object]]:
        return frozenset(
            (k, v)
            for k, v in tokens.items()
            if k not in COSMETIC_KEYS or k in SHAPE_KEYS
        )

    signatures: dict[frozenset[tuple[str, object]], int] = {}
    for entry in index.entries:
        signatures[structural(entry.tokens)] = (
            signatures.get(structural(entry.tokens), 0) + 1
        )
    for entry in sorted(index.entries, key=lambda e: e.shape_id):
        if signatures[structural(entry.tokens)] == 1:
            return entry
    raise AssertionError("expected at least one structurally-unique shape")


@needs_data
def test_recoloured_shape_still_resolves_to_the_same_shape() -> None:
    entry = _structurally_unique_entry(INDEX)
    messy = fx.perturb(
        entry.style,
        fillColor="#123456",
        strokeColor="#654321",
        fontColor="#abcdef",
        fontFamily="Comic Sans MS",
        align="left",
    )
    doc = fx.document(fx.entry_cell(entry, style=messy))
    chosen = derive(load_cells(doc), INDEX).cells[0].chosen
    assert chosen is not None
    assert chosen.shape_id == entry.shape_id


@needs_data
def test_uniquely_styled_cells_resolve_to_their_own_library() -> None:
    """Every shape with a globally-unique style derives to its own library.

    A styleless cell (a plain text label) carries no shape identity and is
    excluded. Shapes whose fingerprint collides across libraries are excluded
    here too: a lone ambiguous cell legitimately falls to the recency prior
    (covered by the scenario tests) -- so this asserts the unambiguous majority
    is perfectly derivable, and that any cross-library resolution is a genuine
    same-style collision rather than a wrong match.
    """
    fingerprint_libs: dict[str, set[str]] = {}
    for entry in INDEX.entries:
        fingerprint_libs.setdefault(entry.fingerprint, set()).add(entry.library)
    cross_library = {fp for fp, libs in fingerprint_libs.items() if len(libs) > 1}

    unique, collision_misses = [], []
    for entry in INDEX.entries:
        if not entry.style.strip():
            continue
        doc = fx.document(fx.entry_cell(entry))
        cells = derive(load_cells(doc), INDEX).cells
        chosen = cells[0].chosen if cells else None
        if entry.fingerprint in cross_library:
            continue
        unique.append(entry)
        if chosen is None or chosen.library != entry.library:
            got = chosen.library if chosen else None
            collision_misses.append((entry.shape_id, got))

    assert not collision_misses, f"unexpected misses: {collision_misses[:10]}"
    # Sanity: the unambiguous set really is the large majority (~78%).
    assert len(unique) / len(INDEX.entries) > 0.7


@needs_data
def test_every_object_wrapped_palette_cell_is_parsed() -> None:
    """Corpus-wide guard for the object-cell id bug: every real palette entry
    (many are object cells, e.g. C4 Person) round-trips to exactly one cell."""
    misses = []
    for entry in INDEX.entries:
        if not entry.style.strip():
            continue
        doc = fx.document(fx.entry_cell(entry))
        if len(load_cells(doc)) != 1:
            misses.append(entry.shape_id)
    assert not misses, f"expected exactly one cell for: {misses[:10]}"


@needs_data
def test_every_real_shape_id_yields_an_identifier_safe_semantic_base() -> None:
    """Every registry shape id's semantic base must be directly usable as a
    bare .mdg node_id (lowercase letters/digits/underscore, not digit-first),
    with no registry-specific exceptions -- verified against the full palette."""
    bad = [
        e.shape_id
        for e in INDEX.entries
        if not re.fullmatch(r"[a-z][a-z0-9_]*", semantic_base(e.shape_id))
    ]
    assert not bad, f"non-identifier-safe semantic base for: {bad[:10]}"
