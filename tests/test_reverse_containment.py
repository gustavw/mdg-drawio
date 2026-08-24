"""Tests for containment resolution (:mod:`mdg_drawio.reverse.containment`).

Two groups:

* algorithm-mechanics unit tests -- hand-built :class:`CellResult`/:class:`RawCell`
  fixtures with a monkeypatched ``_is_container_capable``, so the climbing
  algorithm (passthrough skipping, cycle/dangling-reference safety, warnings,
  depth counting) is pinned independently of any real registry data or the
  scoring/derive pipeline. These need no generated data and never rot;
* data-gated end-to-end tests against the real palette and registry -- nested
  C4 boundaries, the group/layer/anomaly scenarios built from real shapes, and
  a multi-page isolation check. These skip without ``make build-data``.
"""
from __future__ import annotations

import pytest

from mdg_drawio.reverse import containment
from mdg_drawio.reverse import fixtures as fx
from mdg_drawio.reverse.containment import Containment, resolve_containment
from mdg_drawio.reverse.derive import (
    Candidate,
    CellResult,
    DocumentResult,
    RawCell,
    derive,
    load_cells,
    parent_map,
)
from mdg_drawio.reverse.naming import assign_semantic_ids
from mdg_drawio.reverse.style_index import StyleIndex

INDEX = StyleIndex.load()
needs_data = pytest.mark.skipif(
    not INDEX.entries, reason="needs palette styles (run `make build-data`)"
)


# ── algorithm mechanics (no data; monkeypatched container-capability) ───────
def _result(cell_id: str, shape_id: str | None) -> CellResult:
    """A minimal resolved cell, with or without a chosen shape."""
    chosen = Candidate(shape_id, "lib", 1.0) if shape_id else None
    return CellResult(
        cell_id=cell_id,
        style="irrelevant",
        candidates=[],
        chosen=chosen,
        resolved_by="unique" if chosen else "none",
        confidence=1.0 if chosen else 0.0,
    )


def _doc(*results: CellResult) -> DocumentResult:
    return DocumentResult(list(results), {}, {})


def _patch_containers(
    monkeypatch: pytest.MonkeyPatch, capable: set[str]
) -> None:
    """Fake registry lookup: only shape ids in ``capable`` may have children."""
    monkeypatch.setattr(
        containment, "_is_container_capable", lambda shape_id: shape_id in capable
    )


def test_top_level_cell_has_no_container(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_containers(monkeypatch, set())
    raw = {"1": RawCell(None, "", False), "10": RawCell("1", "shape=leaf;", False)}
    result = _doc(_result("10", "lib.leaf.v1"))
    out = resolve_containment(result, raw, {"10": "leaf1"})
    assert out == [Containment("10", None, 0, ())]


def test_single_level_containment(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "20": RawCell("1", "shape=box;", False),
        "21": RawCell("20", "shape=leaf;", False),
    }
    result = _doc(_result("20", "lib.box.v1"), _result("21", "lib.leaf.v1"))
    node_ids = {"20": "box1", "21": "leaf1"}
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}
    assert out["20"] == Containment("20", None, 0, ())
    assert out["21"] == Containment("21", "box1", 1, ())


def test_nested_containment_depth_and_immediate_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Depth increments per level, and each cell's container is its DIRECT
    parent -- not always the outermost ancestor."""
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "30": RawCell("1", "shape=box;", False),
        "31": RawCell("30", "shape=box;", False),
        "32": RawCell("31", "shape=leaf;", False),
    }
    result = _doc(
        _result("30", "lib.box.v1"),
        _result("31", "lib.box.v1"),
        _result("32", "lib.leaf.v1"),
    )
    node_ids = {"30": "box1", "31": "box2", "32": "leaf1"}
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}
    assert out["30"] == Containment("30", None, 0, ())
    assert out["31"] == Containment("31", "box1", 1, ())
    assert out["32"] == Containment("32", "box2", 2, ())  # direct parent, not box1


def test_sibling_containers_do_not_cross_contaminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "40": RawCell("1", "shape=box;", False),
        "41": RawCell("1", "shape=box;", False),
        "42": RawCell("40", "shape=leaf;", False),
        "43": RawCell("41", "shape=leaf;", False),
    }
    result = _doc(
        _result("40", "lib.box.v1"),
        _result("41", "lib.box.v1"),
        _result("42", "lib.leaf.v1"),
        _result("43", "lib.leaf.v1"),
    )
    node_ids = {"40": "boxA", "41": "boxB", "42": "leaf1", "43": "leaf2"}
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}
    assert out["42"].container_node_id == "boxA"
    assert out["43"].container_node_id == "boxB"


def test_layer_cell_is_skipped_without_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A styleless "layer" is normal draw.io structure, not an anomaly."""
    _patch_containers(monkeypatch, set())
    raw = {
        "1": RawCell(None, "", False),
        "layer1": RawCell("1", "", False),  # empty style -- a layer
        "50": RawCell("layer1", "shape=leaf;", False),
    }
    result = _doc(_result("50", "lib.leaf.v1"))
    out = resolve_containment(result, raw, {"50": "leaf1"})
    assert out == [Containment("50", None, 0, ())]


def test_group_cell_is_skipped_without_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl+G "group" cell is a UI bounding box, not an anomaly, and does
    not block finding a real container above it."""
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "60": RawCell("1", "shape=box;", False),
        "group1": RawCell("60", "group;", False),
        "61": RawCell("group1", "shape=leaf;", False),
    }
    result = _doc(_result("60", "lib.box.v1"), _result("61", "lib.leaf.v1"))
    node_ids = {"60": "box1", "61": "leaf1"}
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}
    assert out["61"] == Containment("61", "box1", 1, ())


def test_non_container_ancestor_warns_and_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_containers(monkeypatch, {"lib.box.v1"})  # "lib.other.v1" is NOT capable
    raw = {
        "1": RawCell(None, "", False),
        "70": RawCell("1", "shape=other;", False),
        "71": RawCell("70", "shape=leaf;", False),
    }
    result = _doc(_result("70", "lib.other.v1"), _result("71", "lib.leaf.v1"))
    out = {c.cell_id: c for c in resolve_containment(result, raw, {"70": "other1"})}
    cell = out["71"]
    assert cell.container_node_id is None
    assert cell.depth == 0
    assert len(cell.warnings) == 1
    assert "not container-capable" in cell.warnings[0]


def test_unresolved_ancestor_warns_and_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor with real style that failed to resolve to any shape."""
    _patch_containers(monkeypatch, set())
    raw = {
        "1": RawCell(None, "", False),
        "80": RawCell("1", "shape=unknownthing;", False),
        "81": RawCell("80", "shape=leaf;", False),
    }
    # cell 80 is never given a CellResult with a chosen shape at all.
    result = _doc(_result("81", "lib.leaf.v1"))
    out = {c.cell_id: c for c in resolve_containment(result, raw, {})}
    cell = out["81"]
    assert cell.container_node_id is None
    assert "did not resolve to a known shape" in cell.warnings[0]


def test_continues_past_an_anomaly_to_find_a_real_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-container ancestor is skipped, not a dead end: climbing continues
    upward and can still find a legitimate container further out."""
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "90": RawCell("1", "shape=box;", False),
        "91": RawCell("90", "shape=other;", False),  # not container-capable
        "92": RawCell("91", "shape=leaf;", False),
    }
    result = _doc(
        _result("90", "lib.box.v1"),
        _result("91", "lib.other.v1"),
        _result("92", "lib.leaf.v1"),
    )
    node_ids = {"90": "box1", "91": "other1", "92": "leaf1"}
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}
    cell = out["92"]
    assert cell.container_node_id == "box1"
    assert cell.depth == 1
    assert len(cell.warnings) == 1


def test_cycle_in_parent_chain_is_detected_and_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_containers(monkeypatch, set())
    raw = {
        "a": RawCell("b", "shape=x;", False),
        "b": RawCell("a", "shape=y;", False),  # a <-> b cycle
    }
    result = _doc(_result("a", "lib.x.v1"), _result("b", "lib.y.v1"))
    out = {c.cell_id: c for c in resolve_containment(result, raw, {})}
    assert out["a"].container_node_id is None
    assert any("cycle detected" in w for w in out["a"].warnings)


def test_dangling_parent_reference_terminates_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_containers(monkeypatch, set())
    raw = {"100": RawCell("nonexistent-id", "shape=leaf;", False)}
    result = _doc(_result("100", "lib.leaf.v1"))
    out = resolve_containment(result, raw, {"100": "leaf1"})
    assert out == [Containment("100", None, 0, ())]


def test_edges_are_excluded_from_containment_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "110": RawCell("1", "shape=box;", False),
        "111": RawCell("110", "edgeStyle=x;", True),  # is_edge=True
    }
    result = _doc(_result("110", "lib.box.v1"), _result("111", "lib.edge.v1"))
    out = resolve_containment(result, raw, {"110": "box1", "111": "rel1"})
    assert [c.cell_id for c in out] == ["110"]


def test_missing_node_id_falls_back_to_the_raw_container_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive fallback: every legitimate container should have a semantic
    id assigned (naming.py covers every resolved cell), but if the caller
    passes an incomplete mapping, resolution still returns something usable."""
    _patch_containers(monkeypatch, {"lib.box.v1"})
    raw = {
        "1": RawCell(None, "", False),
        "120": RawCell("1", "shape=box;", False),
        "121": RawCell("120", "shape=leaf;", False),
    }
    result = _doc(_result("120", "lib.box.v1"), _result("121", "lib.leaf.v1"))
    out = {c.cell_id: c for c in resolve_containment(result, raw, {})}
    assert out["121"].container_node_id == "120"


# ── data-gated end-to-end (real registry + palette) ──────────────────────────
@needs_data
def test_is_container_capable_matches_only_the_real_boundary_shapes() -> None:
    """Corpus guard: exactly System_Boundary/Container_Boundary are
    container-capable among all real C4 shapes today -- catches accidental
    drift if the registry changes."""
    c4_entries = [e for e in INDEX.entries if e.library == "c4"]
    capable = {
        e.shape_id
        for e in c4_entries
        if containment._is_container_capable(e.shape_id)
    }
    assert capable == {"c4.system_boundary.v1", "c4.container_boundary.v1"}


@needs_data
def test_real_nested_boundaries_group_and_anomaly_resolve_correctly() -> None:
    """End-to-end against real palette data: two levels of nested
    System_Boundary, a Ctrl+G-grouped Person, an anomalous non-container
    parent, and an edge -- all in one document."""
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    component = fx.get(INDEX, "c4.component.v1")

    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="10", parent="1"),
        fx.entry_cell(sys_b, cell_id="11", parent="10"),
        fx.entry_cell(component, cell_id="12", parent="11"),
        fx.group_cell_xml("14", parent="1"),
        fx.entry_cell(person, cell_id="13", parent="14"),
        fx.entry_cell(component, cell_id="15", parent="13"),
        fx.edge_cell_xml("16", source="12", target="13", parent="1"),
    )
    result = derive(load_cells(doc), INDEX)
    node_ids = {s.cell_id: s.node_id for s in assign_semantic_ids(result)}
    raw = parent_map(doc)
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}

    assert out["10"].container_node_id is None
    assert out["10"].depth == 0
    assert out["11"].container_node_id == node_ids["10"]
    assert out["11"].depth == 1
    assert out["12"].container_node_id == node_ids["11"]
    assert out["12"].depth == 2
    assert out["13"].container_node_id is None  # group transparently skipped
    assert out["13"].depth == 0
    assert out["15"].container_node_id is None  # anomaly: parent is a Person
    assert "not container-capable" in out["15"].warnings[0]
    assert "16" not in out  # the edge is excluded entirely


@needs_data
def test_multi_page_document_does_not_leak_containment_across_pages() -> None:
    """A cell parented to "1" on page 2 must resolve against page 2's own
    root, never page 1's -- the page-prefixing fix in derive.py must keep
    parent_map and load_cells ids aligned per page."""
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")

    page1 = (
        "<mxGraphModel><root><mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/>"
        + fx.entry_cell(sys_b, cell_id="10", parent="1")
        + "</root></mxGraphModel>"
    )
    page2 = (
        "<mxGraphModel><root><mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/>"
        + fx.entry_cell(person, cell_id="10", parent="1")  # SAME raw id, page 2
        + "</root></mxGraphModel>"
    )
    doc = (
        "<mxfile>"
        f'<diagram name="Page-1">{page1}</diagram>'
        f'<diagram name="Page-2">{page2}</diagram>'
        "</mxfile>"
    )
    result = derive(load_cells(doc), INDEX)
    node_ids = {s.cell_id: s.node_id for s in assign_semantic_ids(result)}
    raw = parent_map(doc)
    out = {c.cell_id: c for c in resolve_containment(result, raw, node_ids)}

    assert set(out) == {"0:10", "1:10"}
    # Page 2's Person must not be mistaken for a child of page 1's boundary.
    assert out["0:10"].container_node_id is None
    assert out["1:10"].container_node_id is None
