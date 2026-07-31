"""Tests for the reverse derivation POC (:mod:`scripts.reverse`).

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
* CLI (``scripts.reverse.__main__``) tests, via a monkeypatched index (no data);
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

from scripts.reverse import fixtures as fx
from scripts.reverse.__main__ import main
from scripts.reverse.derive import (
    DEFAULT_BAND,
    DEFAULT_SIM_FLOOR,
    Cell,
    derive,
    load_cells,
)
from scripts.reverse.naming import assign_semantic_ids, semantic_base
from scripts.reverse.scoring import (
    BARE,
    COSMETIC_KEYS,
    SHAPE_KEYS,
    Weights,
    parse_style,
    similarity,
)
from scripts.reverse.style_index import ShapeEntry, StyleIndex

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


# ── synthetic ranking policy (no data) ───────────────────────────────────────
def _entry(shape_id: str, library: str, style: str) -> ShapeEntry:
    return ShapeEntry(shape_id, library, style, f"sha1:{shape_id}", parse_style(style))


def _cell(style: str, cell_id: str = "1") -> Cell:
    return Cell(cell_id, style, "", parse_style(style))


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
    # Guards against silently drifting defaults between the module and derive().
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
    assert {c.cell_id for c in cells} == {"20", "21"}


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
