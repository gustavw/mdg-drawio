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
    Anchor,
    Edge,
    GeometryPoint,
    Node,
)
from mdg_drawio.layout.config import Config
from mdg_drawio.layout.layered import LayeredLayout
from mdg_drawio.layout.palette import PaletteLayout
from mdg_drawio.layout.process import ProcessLayout
from mdg_drawio.layout.sequence import SequenceLayout
from mdg_drawio.layout.size_resolver import create_size_resolver


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


def test_sequence_placement_preserves_non_geometry_fields() -> None:
    """Regression: sequence placement used to reconstruct Node/Edge
    field-by-field, so anything outside that hand-picked list (variant,
    object_attributes, text_parts, child_cells, ...) silently reset to its
    dataclass default. Same defect palette.py already fixed with replace()."""
    node = Node(
        id="a",
        type="uml25.Lifeline",
        label="Alice",
        variant=2,
        element_name="Lifeline",
        text_parts=["desc"],
        object_attributes={"c4Name": "Alice"},
    )
    edge = Edge(
        id="a->a",
        type="c4.Rel",
        source_id="a",
        target_id="a",
        object_attributes={"c4Description": "calls"},
    )
    result = SequenceLayout().apply([node], [edge], _size_of)

    placed = result.nodes[0]
    assert placed.variant == 2
    assert placed.element_name == "Lifeline"
    assert placed.text_parts == ["desc"]
    assert placed.object_attributes == {"c4Name": "Alice"}
    assert result.edges[0].object_attributes == {"c4Description": "calls"}


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


def test_top_level_excluded_node_stays_inside_final_page_bounds() -> None:
    nodes = [_node("task"), _node("data")]
    edges = [
        Edge(id="association", type="c4.Rel", source_id="data", target_id="task")
    ]
    result = ProcessLayout().apply(
        nodes,
        edges,
        _size_of,
        Config(rank_exclude_ids=frozenset({"data"})),
    )

    assert all(node.x >= 0 and node.y >= 0 for node in result.nodes)
    assert all(node.x + node.width <= result.page_width for node in result.nodes)
    assert all(node.y + node.height <= result.page_height for node in result.nodes)


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
# Layered layout
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("form", ["field", "extra"])
def test_layered_lays_out_children_declared_by_contains(form: str) -> None:
    """``Node.contains`` and ``extra["contains"]`` are the two ways a parent
    can name its children (the inverse of ``parent_id``). Both must lay the
    children out inside the parent.

    The container layout used to read only the ``extra`` form while the
    generator keyed its container detection off the ``Node.contains`` field,
    so a node using the declared field had its edges routed as a container's
    while its children were never placed inside it.
    """
    parent = _node("p")
    child = _node("c", width=100.0, height=50.0)
    if form == "field":
        parent.contains = ["c"]
    else:
        parent.extra["contains"] = ["c"]

    result = LayeredLayout().apply([parent, child], [], _size_of)

    by_id = {n.id: n for n in result.nodes}
    assert by_id["c"].parent_id == "p"
    # The parent grew to enclose its child rather than leaving it outside.
    assert by_id["p"].width >= by_id["c"].width
    assert by_id["p"].height >= by_id["c"].height


def test_layered_grid_config_forces_square_arrangement() -> None:
    """``Config(grid=True)`` packs a container's children into a square-ish
    grid instead of the default single-column/rank arrangement."""
    parent = _node("p")
    children = [_node(f"c{i}", width=100.0, height=50.0) for i in range(4)]
    for child in children:
        child.parent_id = "p"

    result = LayeredLayout().apply(
        [parent, *children], [], _size_of, config=Config(grid=True)
    )

    by_id = {n.id: n for n in result.nodes}
    xs = {by_id[f"c{i}"].x for i in range(4)}
    ys = {by_id[f"c{i}"].y for i in range(4)}
    assert len(xs) == 2
    assert len(ys) == 2


def test_layered_grid_config_overrides_stack_child_layout() -> None:
    """``grid: true`` is a document-wide override -- it applies even to a
    container marked ``child_layout: stack`` (e.g. a BPMN pool's lanes)."""
    parent = _node("p", child_layout="stack")
    children = [_node(f"c{i}", width=100.0, height=50.0) for i in range(4)]
    for child in children:
        child.parent_id = "p"

    result = LayeredLayout().apply(
        [parent, *children], [], _size_of, config=Config(grid=True)
    )

    by_id = {n.id: n for n in result.nodes}
    ys = {by_id[f"c{i}"].y for i in range(4)}
    assert len(ys) == 2, "expected a grid, not a single stacked column"


def _rel(
    *,
    object_attributes: dict[str, str | int | float | None] | None = None,
    extra: dict[str, object] | None = None,
    source_anchor: str | Anchor = "",
) -> Edge:
    """A minimal a→b relationship."""
    return Edge(
        id="a->b",
        type="c4.Rel",
        source_id="a",
        target_id="b",
        object_attributes=object_attributes or {},
        extra=extra or {},
        source_anchor=source_anchor,
    )


def test_layered_routing_preserves_non_geometry_fields() -> None:
    """Regression: ``_route_edges`` rebuilt every Edge field-by-field, so
    ``object_attributes`` (and geometry_points/child_cells/palette_decoration)
    were dropped on the way out of layout. That is what makes the generator
    wrap a C4 Rel in its <UserObject> and inherit the palette label template,
    so every Rel silently degraded to a bare ``value=`` label instead. Only
    the routing result is computed here; the rest must carry over."""
    nodes = [_node("a"), _node("b")]
    edge = _rel(
        object_attributes={"c4Type": "Relationship", "c4Description": "calls"},
        extra={"variant": 2},
    )
    result = LayeredLayout(route_edges=True).apply(nodes, [edge], _size_of)

    routed = result.edges[0]
    assert routed.waypoints, "routing still runs"
    assert routed.object_attributes == {
        "c4Type": "Relationship",
        "c4Description": "calls",
    }
    assert routed.extra == {"variant": 2}


def test_layered_emits_no_edge_geometry_by_default() -> None:
    """An ordinary ranked diagram leaves routing to draw.io.

    Pre-baked elbows also freeze the picture: draw.io honours an explicit
    ``<Array as="points">`` and will not re-route around a shape the author
    later moves. Only process mode opts into computed geometry.
    """
    nodes = [_node("a"), _node("b")]
    result = LayeredLayout().apply(nodes, [_rel()], _size_of)

    (edge,) = result.edges
    assert edge.waypoints == []
    # Anchors go with the waypoints they were chosen for, so they are not
    # emitted either -- an edge pinned to a port picked for a path that is no
    # longer drawn is worse than no anchor at all.
    assert edge.source_anchor == ""
    assert edge.target_anchor == ""


def test_layered_keeps_an_explicitly_authored_anchor() -> None:
    """Turning routing off must not discard anchors layout never computed.

    These arrive from an author override or from an existing ``.drawio`` via
    the geometry overlay, and are the author's manual adjustment to preserve.
    """
    nodes = [_node("a"), _node("b")]
    edge = _rel(source_anchor=Anchor(x=1.0, y=0.5))
    result = LayeredLayout().apply(nodes, [edge], _size_of)

    assert result.edges[0].source_anchor == Anchor(x=1.0, y=0.5)


def test_process_still_routes_edges() -> None:
    """Process mode is the one layout that keeps its computed elbows."""
    nodes = [_node("a"), _node("b")]
    result = ProcessLayout().apply(nodes, [_rel()], _size_of)

    assert result.edges[0].waypoints, "process mode must still route"


def test_layered_keeps_cycle_edges_visible_and_in_declared_orientation() -> None:
    """Cycle removal must only mutate the temporary ranking graph.

    A back edge is reversed internally so ranking sees a DAG, but every
    authored relationship must still be emitted visibly in its declared
    orientation.
    """
    nodes = [_node("a"), _node("b")]
    edges = [
        Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b"),
        Edge(id="b->a", type="c4.Rel", source_id="b", target_id="a"),
    ]
    result = LayeredLayout(route_edges=True).apply(nodes, edges, _size_of)

    back_edge = next(e for e in result.edges if e.id == "b->a")
    assert (back_edge.source_id, back_edge.target_id) == ("b", "a")
    assert not back_edge.hidden
    assert all(not edge.hidden for edge in result.edges)


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


def test_palette_clears_positions_when_path_is_unset(tmp_path: Path) -> None:
    palette_file = tmp_path / "palette.json"
    palette_file.write_text(
        json.dumps([{"id": "a", "x": 200, "y": 300}]), encoding="utf-8"
    )
    layout = PaletteLayout()
    layout.palette_path = palette_file
    first = layout.apply([_node("a")], [], _size_of)
    assert (first.nodes[0].x, first.nodes[0].y) == (200.0, 300.0)

    layout.palette_path = None
    second = layout.apply([_node("a")], [], _size_of)
    assert (second.nodes[0].x, second.nodes[0].y) == (0.0, 0.0)


def test_registry_size_resolver_uses_node_variant_in_layout() -> None:
    registries = {
        "bpmn2": {
            "shapes": [
                {
                    "id": "bpmn2.horizontalpool.v1",
                    "function": "HorizontalPool",
                    "variant": 1,
                },
                {
                    "id": "bpmn2.horizontalpool.v2",
                    "function": "HorizontalPool",
                    "variant": 2,
                },
            ]
        }
    }
    styles = {
        "bpmn2": {
            "bpmn2.horizontalpool.v1": {"width": 480, "height": 380},
            "bpmn2.horizontalpool.v2": {"width": 480, "height": 360},
        }
    }
    node = Node(id="pool", type="bpmn2.HorizontalPool", variant=2)

    result = SequenceLayout().apply(
        [node], [], create_size_resolver(registries, styles)
    )

    assert (result.nodes[0].width, result.nodes[0].height) == (480.0, 360.0)
