"""Tests for the reverse derivation POC (:mod:`scripts.reverse`).

Two groups:

* pure scoring/parsing unit tests (no generated data needed);
* data-gated end-to-end tests that build ground-truth ``.drawio`` fixtures from
  the palette styles and assert the ranking behaviour the design specifies --
  notably the two version-priority scenarios. These skip without
  ``make build-data`` (the palette styles are git-ignored, draw.io-copyright).
"""
from __future__ import annotations

import zlib

import pytest

from scripts.reverse import fixtures as fx
from scripts.reverse.derive import derive, load_cells
from scripts.reverse.scoring import BARE, parse_style, similarity
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


def test_load_cells_handles_compressed_diagram() -> None:
    inner = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="2" style="ellipse;html=1;" vertex="1" parent="1"/>'
        "</root></mxGraphModel>"
    )
    payload = zlib.compressobj(-1, zlib.DEFLATED, -15)
    packed = payload.compress(inner.encode()) + payload.flush()
    import base64

    b64 = base64.b64encode(packed).decode()
    doc = f'<mxfile><diagram name="P">{b64}</diagram></mxfile>'
    cells = load_cells(doc)
    assert [c.cell_id for c in cells] == ["2"]
    assert cells[0].tokens["ellipse"] is BARE


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
    from scripts.reverse.scoring import COSMETIC_KEYS, SHAPE_KEYS

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
