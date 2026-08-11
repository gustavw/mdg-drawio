"""Unit tests for the three under-tested layout modes.

`sequence`, `process`, and `palette` had the lowest per-Component coverage and
carried several of the review's correctness findings. These tests exercise their
real placement/routing math directly (no full pipeline) so each Component clears
the coverage gate and its behaviour is pinned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mdg_drawio.contracts import (
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
    PALETTE_DEFAULT_PAGE_HEIGHT,
    PALETTE_DEFAULT_PAGE_WIDTH,
    Edge,
    GeometryPoint,
    Node,
)
from mdg_drawio.layout.config import Config
from mdg_drawio.layout.palette import PaletteLayout
from mdg_drawio.layout.process import ProcessLayout
from mdg_drawio.layout.sequence import SequenceLayout


def _size_of(node_type: str) -> tuple[float, float]:
    return (100.0, 50.0)


def _node(
    node_id: str, *, width: float = 0.0, height: float = 0.0, **extra: object
) -> Node:
    return Node(
        id=node_id,
        type="c4.System",
        label=node_id,
        width=width,
        height=height,
        extra=dict(extra),
    )


# ---------------------------------------------------------------------------
# Sequence layout
# ---------------------------------------------------------------------------

def test_sequence_empty_returns_default_page() -> None:
    result = SequenceLayout().apply([], [], _size_of)
    assert result.nodes == []
    assert result.edges == []
    assert (result.page_width, result.page_height) == (
        DEFAULT_PAGE_WIDTH,
        DEFAULT_PAGE_HEIGHT,
    )


def test_sequence_places_columns_left_to_right() -> None:
    cfg = Config()
    nodes = [_node("a"), _node("b", width=80.0, height=40.0)]
    result = SequenceLayout().apply(nodes, [], _size_of, cfg)

    assert [n.id for n in result.nodes] == ["a", "b"]
    a, b = result.nodes
    # First column uses the resolved size; second keeps its explicit size.
    assert (a.width, a.height) == (100.0, 50.0)
    assert (b.width, b.height) == (80.0, 40.0)
    # Columns march rightward with a gap between them.
    assert b.x == pytest.approx(a.x + a.width + cfg.column_gap)
    assert a.y == b.y == cfg.margin_y


def test_sequence_edge_routes_horizontally_between_columns() -> None:
    nodes = [_node("a"), _node("b")]
    edge = Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b")
    result = SequenceLayout().apply(nodes, [edge], _size_of)

    assert len(result.edges) == 1
    wps = result.edges[0].waypoints
    # Geometry is fully determined by default Config: column centres at
    # margin_x+w/2 (90) and margin_x+w+gap+w/2 (230); y = margin_y+header+row_gap.
    assert [(w.x, w.y) for w in wps] == [(90.0, 140.0), (230.0, 140.0)]


@pytest.mark.parametrize(
    "source_id,target_id,missing",
    [("a", "ghost", "ghost"), ("ghost", "a", "ghost")],
)
def test_sequence_skips_edge_with_unknown_participant(
    source_id: str, target_id: str, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    nodes = [_node("a"), _node("b")]
    edge = Edge(id="e", type="c4.Rel", source_id=source_id, target_id=target_id)
    result = SequenceLayout().apply(nodes, [edge], _size_of)

    assert result.edges == []  # fabricated positions are not invented
    warning = capsys.readouterr().err
    assert "unknown participant" in warning
    assert missing in warning


# ---------------------------------------------------------------------------
# Process layout
# ---------------------------------------------------------------------------

def test_process_lays_out_all_nodes_left_to_right() -> None:
    nodes = [_node("a"), _node("b")]
    edges = [Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b")]
    result = ProcessLayout().apply(nodes, edges, _size_of)

    by_id = {n.id: n for n in result.nodes}
    assert set(by_id) == {"a", "b"}
    assert len(result.edges) == 1
    # The edge source is ranked strictly left of its target (direction=LR),
    # on the same row — this is the property the mode's name promises.
    assert by_id["a"].x < by_id["b"].x
    assert by_id["a"].y == by_id["b"].y


def test_process_rank_excluded_node_floats_above_its_connected_task() -> None:
    nodes = [_node("a"), _node("b"), _node("data")]
    edges = [
        Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b"),
        Edge(id="a->data", type="c4.Rel", source_id="a", target_id="data"),
    ]
    result = ProcessLayout().apply(
        nodes, edges, _size_of, Config(rank_exclude_ids=frozenset({"data"}))
    )

    by_id = {n.id: n for n in result.nodes}
    routed = {e.id: e for e in result.edges}
    assert set(by_id) == {"a", "b", "data"}
    assert set(routed) == {"a->b", "a->data"}
    # The excluded node is never ranked/placed into the sequence -- instead it
    # floats directly above the task it's connected to, centered horizontally.
    data, anchor = by_id["data"], by_id["a"]
    assert (data.width, data.height) == _size_of(data.type)
    assert data.x == pytest.approx(anchor.x + anchor.width / 2 - data.width / 2)
    assert data.y == pytest.approx(anchor.y - data.height - Config().column_gap)
    assert by_id["a"].x > 0.0 and by_id["b"].x > 0.0
    # The edge touching the excluded node passes through unrouted (no waypoints),
    # while the ranked edge gets routed.
    assert routed["a->data"].waypoints == []
    assert routed["a->b"].waypoints != []


def test_process_rank_excluded_node_shared_by_two_tasks_floats_above_first() -> None:
    """A data artifact read by more than one task (e.g. a DataObject two
    tasks both consume) renders above the EARLIEST task in the sequence, not
    whichever one happens to be listed first in its own edges."""
    nodes = [_node("a"), _node("b"), _node("data")]
    edges = [
        Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b"),
        # Declared toward "b" (the later task) first, deliberately, to prove
        # anchor choice depends on rank, not edge/declaration order.
        Edge(id="b->data", type="c4.Rel", source_id="b", target_id="data"),
        Edge(id="a->data", type="c4.Rel", source_id="a", target_id="data"),
    ]
    result = ProcessLayout().apply(
        nodes, edges, _size_of, Config(rank_exclude_ids=frozenset({"data"}))
    )

    by_id = {n.id: n for n in result.nodes}
    data, anchor = by_id["data"], by_id["a"]
    assert anchor.x < by_id["b"].x  # sanity: "a" really is the earlier task
    assert data.x == pytest.approx(anchor.x + anchor.width / 2 - data.width / 2)


def test_process_rank_excluded_node_with_no_connection_keeps_origin() -> None:
    """An excluded node with nothing to anchor to (no edge to a ranked node)
    is left at its origin geometry -- there's nowhere meaningful to float it."""
    nodes = [_node("a"), _node("orphan_data")]
    edges: list[Edge] = []
    result = ProcessLayout().apply(
        nodes,
        edges,
        _size_of,
        Config(rank_exclude_ids=frozenset({"orphan_data"})),
    )
    by_id = {n.id: n for n in result.nodes}
    assert (by_id["orphan_data"].x, by_id["orphan_data"].y) == (0.0, 0.0)
    assert (by_id["orphan_data"].width, by_id["orphan_data"].height) == _size_of(
        by_id["orphan_data"].type
    )


def test_process_rank_exclusion_preserves_larger_authored_headroom() -> None:
    lane = _node("lane", padding_extra_top=200.0)
    task = _node("task")
    task.parent_id = lane.id
    data = _node("data")
    edges = [
        Edge(id="association", type="c4.Rel", source_id="data", target_id="task")
    ]

    ProcessLayout().apply(
        [lane, task, data],
        edges,
        _size_of,
        Config(rank_exclude_ids=frozenset({"data"})),
    )

    assert lane.extra["padding_extra_top"] == 200.0


def test_process_bypassed_branch_does_not_overlap_same_rank_sibling() -> None:
    container = _node("container")
    predecessor = _node("predecessor")
    detour = _node("detour")
    sibling = _node("sibling")
    successor = _node("successor")
    for node in (predecessor, detour, sibling, successor):
        node.parent_id = container.id
    edges = [
        Edge(id="p-d", type="c4.Rel", source_id="predecessor", target_id="detour"),
        Edge(id="d-s", type="c4.Rel", source_id="detour", target_id="successor"),
        Edge(id="p-s", type="c4.Rel", source_id="predecessor", target_id="successor"),
        Edge(id="p-x", type="c4.Rel", source_id="predecessor", target_id="sibling"),
    ]

    result = ProcessLayout().apply(
        [container, predecessor, detour, sibling, successor], edges, _size_of
    )
    by_id = {node.id: node for node in result.nodes}
    detour_box = by_id["detour"]
    sibling_box = by_id["sibling"]

    assert detour_box.y >= sibling_box.y + sibling_box.height + Config().column_gap


def test_bypassed_branch_edge_to_successor_uses_a_single_bend() -> None:
    """Regression: a bypassed node's edge back to its successor used to
    always fall through to the plain default route -- forward-exit,
    backward-entry, split down the cross-axis middle -- because that route's
    single-bend alternative (_minimal_bend_route) was gated on the SOURCE
    branching (out_degree >= 2), which a detour node with one successor
    never does. The detour is, by construction, offset on the cross axis
    from its successor (that's the whole point of parking it on a secondary
    row), so the plain default route always cost an avoidable second bend.
    Forcing only the entry side (mirroring _minimal_bend_route, but for the
    target instead of the source) gets the same one-corner elbow without
    forcing an exit side nothing here needs differentiated."""
    container = _node("container")
    predecessor = _node("predecessor")
    detour = _node("detour")
    sibling = _node("sibling")
    successor = _node("successor")
    for node in (predecessor, detour, sibling, successor):
        node.parent_id = container.id
    edges = [
        Edge(id="p-d", type="c4.Rel", source_id="predecessor", target_id="detour"),
        Edge(id="d-s", type="c4.Rel", source_id="detour", target_id="successor"),
        Edge(id="p-s", type="c4.Rel", source_id="predecessor", target_id="successor"),
        Edge(id="p-x", type="c4.Rel", source_id="predecessor", target_id="sibling"),
    ]

    result = ProcessLayout().apply(
        [container, predecessor, detour, sibling, successor], edges, _size_of
    )
    detour_to_successor = next(e for e in result.edges if e.id == "d-s")

    assert len(detour_to_successor.waypoints) == 1
    assert detour_to_successor.source_anchor != ""
    assert detour_to_successor.target_anchor != ""
    assert detour_to_successor.source_anchor != detour_to_successor.target_anchor


# ---------------------------------------------------------------------------
# Palette layout
# ---------------------------------------------------------------------------

def test_palette_without_file_keeps_node_geometry() -> None:
    n = _node("a", width=40.0, height=30.0)
    n.x, n.y = 11.0, 12.0
    result = PaletteLayout().apply([n], [], _size_of)
    placed = result.nodes[0]
    assert (placed.x, placed.y, placed.width, placed.height) == (11.0, 12.0, 40.0, 30.0)


def test_palette_reposition_preserves_non_geometry_fields() -> None:
    """Regression: repositioning used to reconstruct Node/Edge field-by-field,
    silently dropping anything not in that hand-picked list (variant,
    object_attributes, ...) back to its dataclass default -- a variant=2
    shape would render with variant 1's style/label. replace() carries every
    field forward except the geometry actually being overridden."""
    n = Node(
        id="a",
        type="bpmn2.HorizontalLane",
        label="",
        variant=2,
        object_attributes={"c4Name": "Alice"},
    )
    n.x, n.y = 5.0, 6.0
    result = PaletteLayout().apply([n], [], _size_of)
    placed = result.nodes[0]
    assert placed.variant == 2
    assert placed.object_attributes == {"c4Name": "Alice"}

    edge = Edge(
        id="a->b", type="c4.Rel", source_id="a", target_id="b",
        object_attributes={"c4Description": "calls"},
    )
    routed = PaletteLayout().apply([n], [edge], _size_of)
    assert routed.edges[0].object_attributes == {"c4Description": "calls"}


def test_palette_empty_nodes_uses_default_page() -> None:
    result = PaletteLayout().apply([], [], _size_of)
    assert result.nodes == []
    assert (result.page_width, result.page_height) == (
        PALETTE_DEFAULT_PAGE_WIDTH,
        PALETTE_DEFAULT_PAGE_HEIGHT,
    )


def test_palette_applies_positions_by_id_and_element_name(tmp_path: Path) -> None:
    # Node "a" is placed past the default page floor so page growth is actually
    # exercised on the width axis, while height stays below the floor.
    positions = [
        {"id": "a", "x": 2000, "y": 20, "width": 100, "height": 60},
        {"elementName": "SystemShape", "x": 200, "y": 40, "width": 80, "height": 50},
        {"id": "a->b", "geometryPoints": [{"x": 5, "y": 6}, {"x": 7, "y": 8}]},
    ]
    palette_file = tmp_path / "palette.json"
    palette_file.write_text(json.dumps(positions), encoding="utf-8")

    nodes = [_node("a"), _node("b", elementName="SystemShape")]
    edges = [Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b")]

    layout = PaletteLayout()
    layout.palette_path = str(palette_file)
    result = layout.apply(nodes, edges, _size_of)

    by_id = {n.id: n for n in result.nodes}
    # Matched by id.
    assert (by_id["a"].x, by_id["a"].y, by_id["a"].width) == (2000.0, 20.0, 100.0)
    # Matched by the elementName fallback.
    assert (by_id["b"].x, by_id["b"].y, by_id["b"].width) == (200.0, 40.0, 80.0)
    # Edge waypoints applied from geometryPoints.
    assert result.edges[0].waypoints == [
        GeometryPoint(x=5, y=6),
        GeometryPoint(x=7, y=8),
    ]
    # Width grows to fit content past the floor (2000 + 100); height stays at the
    # floor since content (90) is below PALETTE_DEFAULT_PAGE_HEIGHT.
    assert result.page_width == 2100
    assert result.page_height == PALETTE_DEFAULT_PAGE_HEIGHT


def test_palette_missing_file_raises_valueerror(tmp_path: Path) -> None:
    layout = PaletteLayout()
    layout.palette_path = str(tmp_path / "does_not_exist.json")
    with pytest.raises(ValueError, match="could not read palette file"):
        layout.apply([_node("a")], [], _size_of)


def test_palette_malformed_json_raises_valueerror(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    layout = PaletteLayout()
    layout.palette_path = str(bad)
    with pytest.raises(ValueError, match="could not read palette file"):
        layout.apply([_node("a")], [], _size_of)
