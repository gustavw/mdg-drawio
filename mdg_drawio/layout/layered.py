"""Layered (Sugiyama-style) layout — longest-path ranking, barycenter sweep,
orthogonal edge routing.

The algorithm produces a directed acyclic graph layout:
1. Cycle removal via back-edge reversal (bad edges marked hidden).
2. Longest-path ranking: every node gets a rank (layer) number.
3. Barycenter sweep: 4 passes (alternating up/down) to reduce crossings.
4. Position assignment with column packing.
5. Edge routing as 3-segment orthogonal polylines.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from mdg_drawio.contracts import DEFAULT_PAGE_HEIGHT, DEFAULT_PAGE_WIDTH, Anchor

from ._container_layout import absolute_node_boxes, apply_container_layout
from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
    Waypoint,
)
from .config import Config, resolve_page_size


class LayeredLayout(BaseLayout):
    """Sugiyama-style layered graph layout."""

    def apply(
        self,
        nodes: list[Node],
        edges: list[Edge],
        size_of: SizeResolver,
        config: Config | None = None,
    ) -> Result:
        cfg = config or Config()

        if not nodes:
            pw = DEFAULT_PAGE_WIDTH
            ph = DEFAULT_PAGE_HEIGHT
            return Result(nodes=[], edges=[], page_width=pw, page_height=ph)

        _default_node_sizes(nodes, size_of)
        default_padding = _padding_dict(cfg)
        container_state = apply_container_layout(
            nodes,
            edges,
            size_of,
            direction=cfg.direction,
            rank_gap=cfg.rank_gap,
            column_gap=cfg.column_gap,
            default_padding=default_padding,
        )

        node_by_id = {n.id: n for n in nodes}
        layout_nodes = container_state.top_level_nodes
        layout_node_by_id = {n.id: n for n in layout_nodes}

        edges, reversed_ids = _remove_cycles(edges, node_by_id)
        layout_edges = _collapse_edges_to_layout_units(
            edges,
            container_state.top_level_by_id,
        )
        layers = _assign_layers(layout_nodes, layout_edges)
        ordered_layers = _order_layers(layers, layout_edges, layout_node_by_id)
        _assign_positions(
            ordered_layers,
            layout_node_by_id,
            direction=cfg.direction,
            margin_x=cfg.margin_x,
            margin_y=cfg.margin_y,
            rank_gap=cfg.rank_gap,
            column_gap=cfg.column_gap,
        )
        node_boxes = absolute_node_boxes(nodes)
        routed_edges = _route_edges(
            edges,
            node_by_id,
            direction=cfg.direction,
            node_boxes=node_boxes,
        )
        # Ranking reversed back edges in place; restore their declared
        # orientation now that layout is done (these objects are what the
        # generator emits).
        _restore_reversed_edges(edges, reversed_ids)

        content_w, content_h = _content_extents(nodes, node_boxes)
        page_w, page_h = resolve_page_size(
            content_width=content_w,
            content_height=content_h,
            margin_x=cfg.margin_x,
            margin_y=cfg.margin_y,
            aspect_ratio=cfg.aspect_ratio,
        )

        return Result(
            nodes=list(nodes),
            edges=routed_edges,
            page_width=page_w,
            page_height=page_h,
        )


def _default_node_sizes(nodes: list[Node], size_of: SizeResolver) -> None:
    """Fill in missing width/height from the size resolver, in place."""
    for node in nodes:
        w, h = size_of(node.type)
        if not node.width:
            node.width = float(w)
        if not node.height:
            node.height = float(h)


def _padding_dict(cfg: Config) -> dict[str, float]:
    """Flatten the config's boundary padding into a side→value dict."""
    return {
        "top": cfg.boundary_padding.top,
        "right": cfg.boundary_padding.right,
        "bottom": cfg.boundary_padding.bottom,
        "left": cfg.boundary_padding.left,
    }


def _content_extents(
    nodes: list[Node],
    node_boxes: dict[str, tuple[float, float, float, float]],
) -> tuple[float, float]:
    if not nodes:
        return 0.0, 0.0
    min_x = min(box[0] for box in node_boxes.values())
    min_y = min(box[1] for box in node_boxes.values())
    max_x = max(box[0] + box[2] for box in node_boxes.values())
    max_y = max(box[1] + box[3] for box in node_boxes.values())
    return max_x - min_x, max_y - min_y


# ---------------------------------------------------------------------------
# Algorithm steps (unchanged logic, parameterized by caller)
# ---------------------------------------------------------------------------


def _edge_adjacency(edges: list[Edge]) -> dict[str, list[Edge]]:
    """Build outgoing edge adjacency keyed by source id."""
    adj: dict[str, list[Edge]] = defaultdict(list)
    for edge in edges:
        if edge.source_id and edge.target_id:
            adj[edge.source_id].append(edge)
    return adj


def _find_back_edges(
    adj: dict[str, list[Edge]],
    node_by_id: dict[str, Node],
) -> list[Edge]:
    """Find DFS back edges that would create cycles.

    Iterative (explicit stack) so a deep dependency chain cannot blow Python's
    recursion limit. ``gray`` = on the current DFS path, so an edge into a gray
    node is a back edge.
    """
    white, gray, black = 0, 1, 2
    color: dict[str, int] = {node_id: white for node_id in node_by_id}
    back_edges: list[Edge] = []

    for root in node_by_id:
        if color[root] != white:
            continue
        color[root] = gray
        stack: list[tuple[str, Iterator[Edge]]] = [
            (root, iter(adj.get(root, [])))
        ]
        while stack:
            node_id, edge_iter = stack[-1]
            descended = False
            for edge in edge_iter:
                target_id = edge.target_id
                if target_id not in color:
                    continue
                target_color = color[target_id]
                if target_color == gray:
                    back_edges.append(edge)
                elif target_color == white:
                    color[target_id] = gray
                    stack.append((target_id, iter(adj.get(target_id, []))))
                    descended = True
                    break
            if not descended:
                color[node_id] = black
                stack.pop()

    return back_edges


def _reverse_back_edges(
    edges: list[Edge],
    back_edges: list[Edge],
) -> tuple[list[Edge], set[int]]:
    """Hide and reverse back edges before ranking.

    Reversed edges are tracked by object identity (``id(edge)``), not
    ``edge.id`` — passthrough edges share an empty id, so an id-based set would
    match every one of them.
    """
    back_edge_ids = {id(e) for e in back_edges}
    reversed_ids: set[int] = set()
    result: list[Edge] = []
    for edge in edges:
        if id(edge) in back_edge_ids:
            edge.hidden = True
            edge.source_id, edge.target_id = edge.target_id, edge.source_id
            reversed_ids.add(id(edge))
        result.append(edge)
    return result, reversed_ids


def _remove_cycles(
    edges: list[Edge],
    node_by_id: dict[str, Node],
) -> tuple[list[Edge], set[int]]:
    back_edges = _find_back_edges(_edge_adjacency(edges), node_by_id)
    return _reverse_back_edges(edges, back_edges)


def _layer_graph(
    nodes: list[Node],
    edges: list[Edge],
) -> tuple[dict[str, Node], dict[str, int], dict[str, list[str]]]:
    """Build graph state used by longest-path layer assignment."""
    node_by_id = {n.id: n for n in nodes}
    in_degree: dict[str, int] = defaultdict(int)
    out_adj: dict[str, list[str]] = defaultdict(list)

    for edge in edges:
        source_id = edge.source_id
        target_id = edge.target_id
        if (
            source_id
            and target_id
            and source_id in node_by_id
            and target_id in node_by_id
        ):
            in_degree[target_id] += 1
            out_adj[source_id].append(target_id)

    return node_by_id, in_degree, out_adj


def _initial_rank_queue(
    nodes: list[Node],
    in_degree: dict[str, int],
) -> list[str]:
    """Return starting nodes for rank assignment."""
    queue = [n.id for n in nodes if in_degree[n.id] == 0]
    if not queue:
        return [nodes[0].id]
    return queue


def _longest_path_ranks(
    nodes: list[Node],
    in_degree: dict[str, int],
    out_adj: dict[str, list[str]],
    queue: list[str],
) -> dict[str, int]:
    """Assign rank by longest path from a source node."""
    ranks: dict[str, int] = {}
    for node_id in queue:
        ranks[node_id] = 0

    while queue:
        node_id = queue.pop(0)
        for target_id in out_adj.get(node_id, []):
            new_rank = ranks[node_id] + 1
            if target_id not in ranks or new_rank > ranks[target_id]:
                ranks[target_id] = new_rank
            in_degree[target_id] -= 1
            if in_degree[target_id] == 0:
                queue.append(target_id)

    for node in nodes:
        if node.id not in ranks:
            ranks[node.id] = 0

    return ranks


def _layers_from_ranks(
    nodes: list[Node],
    node_by_id: dict[str, Node],
    ranks: dict[str, int],
) -> list[list[Node]]:
    """Group nodes into layer lists by rank."""
    max_rank = max(ranks.values(), default=0)
    layers: list[list[Node]] = [[] for _ in range(max_rank + 1)]

    for node in nodes:
        layers[ranks[node.id]].append(node_by_id[node.id])

    return layers


def _assign_layers(
    nodes: list[Node],
    edges: list[Edge],
) -> list[list[Node]]:
    if not nodes:
        return []

    node_by_id, in_degree, out_adj = _layer_graph(nodes, edges)
    queue = _initial_rank_queue(nodes, in_degree)
    ranks = _longest_path_ranks(nodes, in_degree, out_adj, queue)
    return _layers_from_ranks(nodes, node_by_id, ranks)


def _collapse_edges_to_layout_units(
    edges: list[Edge],
    top_level_by_id: dict[str, str],
) -> list[Edge]:
    if not top_level_by_id:
        return edges

    collapsed: list[Edge] = []
    for edge in edges:
        source_id = top_level_by_id.get(edge.source_id, edge.source_id)
        target_id = top_level_by_id.get(edge.target_id, edge.target_id)
        if not source_id or not target_id or source_id == target_id:
            continue
        collapsed.append(
            Edge(
                id=edge.id,
                type=edge.type,
                source_id=source_id,
                target_id=target_id,
                hidden=edge.hidden,
                description=edge.description,
            )
        )
    return collapsed


def _order_layers(
    layers: list[list[Node]],
    edges: list[Edge],
    node_by_id: dict[str, Node],
) -> list[list[Node]]:
    in_adj: dict[str, list[str]] = defaultdict(list)
    out_adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        sid = e.source_id
        tid = e.target_id
        if sid and tid:
            out_adj[sid].append(tid)
            in_adj[tid].append(sid)

    def barycenter(layer: list[Node], neighbors: dict[str, list[str]],
                   ref_pos: dict[str, float]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for node in layer:
            nbrs = [
                ref_pos[nid]
                for nid in neighbors.get(node.id, [])
                if nid in ref_pos
            ]
            scores[node.id] = sum(nbrs) / len(nbrs) if nbrs else float("inf")
        return scores

    for _ in range(2):
        for up_pass in (False, True):
            for i in (
                reversed(range(1, len(layers)))
                if up_pass
                else range(1, len(layers))
            ):
                upper = layers[i - 1] if up_pass else layers[i]
                lower = layers[i] if up_pass else layers[i - 1]
                ref_layer = lower if up_pass else upper
                neighbors = in_adj if up_pass else out_adj
                ref_pos = {n.id: float(idx) for idx, n in enumerate(ref_layer)}
                scores = barycenter(upper if up_pass else lower, neighbors, ref_pos)
                target = upper if up_pass else lower
                target.sort(key=lambda n: scores.get(n.id, float("inf")))

    return layers


def _assign_positions(
    layers: list[list[Node]],
    node_by_id: dict[str, Node],
    *,
    direction: str,
    margin_x: float,
    margin_y: float,
    rank_gap: float,
    column_gap: float,
) -> None:
    horizontal = direction == "LR"
    primary = float(margin_x if horizontal else margin_y)

    for layer in layers:
        if horizontal:
            cursor = margin_y
            for node in layer:
                node.x = primary
                node.y = cursor
                cursor += node.height + column_gap
            primary += max((node.width for node in layer), default=0.0) + rank_gap
        else:
            cursor = float(margin_x)
            for node in layer:
                node.x = cursor
                node.y = primary
                cursor += node.width + column_gap
            primary += max((node.height for node in layer), default=0.0) + rank_gap


# Extra clearance between an arced route and the obstruction(s) it bypasses.
_ARC_CLEARANCE = 20.0

# Canonical perimeter anchors: middle (the default, floating/unset), and the
# four cardinal sides a route can be pinned to instead when the default port
# would run straight through another shape.
_ANCHOR_TOP = Anchor(x=0.5, y=0.0)
_ANCHOR_BOTTOM = Anchor(x=0.5, y=1.0)
_ANCHOR_LEFT = Anchor(x=0.0, y=0.5)
_ANCHOR_RIGHT = Anchor(x=1.0, y=0.5)


def _blocking_boxes(
    lo: float,
    hi: float,
    level: float,
    exclude_ids: set[str],
    node_boxes: dict[str, tuple[float, float, float, float]],
    container_ids: set[str],
    *,
    horizontal: bool,
) -> list[tuple[float, float, float, float]]:
    """Boxes (other than the edge's own endpoints, or any container -- an
    edge routing through a Lane/boundary it's nested in is normal) that a
    straight route along *level* between *lo* and *hi* would pass through.
    """
    blockers = []
    for node_id, (box_x, box_y, box_w, box_h) in node_boxes.items():
        if node_id in exclude_ids or node_id in container_ids:
            continue
        if horizontal:
            span_lo, span_hi = box_x, box_x + box_w
            cross_lo, cross_hi = box_y, box_y + box_h
        else:
            span_lo, span_hi = box_y, box_y + box_h
            cross_lo, cross_hi = box_x, box_x + box_w
        if span_lo < hi and span_hi > lo and cross_lo < level < cross_hi:
            blockers.append((box_x, box_y, box_w, box_h))
    return blockers


def _fan_out_anchor(
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    *,
    horizontal: bool,
) -> Anchor | None:
    """When a node branches (a gateway's multiple outgoing edges), each
    branch should leave from a side that matches where its OWN target sits,
    not all pile onto the same default port -- otherwise two options read as
    one tangled line at the point they leave the shape, even though they
    diverge further along. ``None`` means the target doesn't clear the
    source's own footprint on the cross axis, where the plain default port
    is the right choice -- forcing a cross-axis exit toward a target whose
    box still overlaps the source's span there would have to double back
    across the source's own body to reach it (a branch that reads as
    "crossing through the gateway"), which is worse than the plain route.
    """
    src_x, src_y, src_w, src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    if horizontal:
        src_lo, src_hi = src_y, src_y + src_h
        tgt_lo, tgt_hi = tgt_y, tgt_y + tgt_h
    else:
        src_lo, src_hi = src_x, src_x + src_w
        tgt_lo, tgt_hi = tgt_x, tgt_x + tgt_w
    if tgt_lo >= src_hi:
        return _ANCHOR_BOTTOM if horizontal else _ANCHOR_RIGHT
    if tgt_hi <= src_lo:
        return _ANCHOR_TOP if horizontal else _ANCHOR_LEFT
    return None


def _obstruction_route(
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    blockers: list[tuple[float, float, float, float]],
    exclude_ids: set[str],
    boxes: dict[str, tuple[float, float, float, float]],
    container_ids: set[str],
    *,
    horizontal: bool,
) -> tuple[list[Waypoint], Anchor, Anchor]:
    """Arc around ``blockers`` on the cross axis (above/below for a
    horizontal flow, left/right for vertical) -- tries the near side first,
    falls back to the far side if something ALSO occupies that level."""
    src_x, src_y, src_w, src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    if horizontal:
        lo, hi = min(src_x + src_w, tgt_x), max(src_x + src_w, tgt_x)
        near = min(src_y, tgt_y, *(by for _, by, _, _ in blockers)) - _ARC_CLEARANCE
        near_clear = not _blocking_boxes(
            lo, hi, near, exclude_ids, boxes, container_ids, horizontal=True
        )
        if near_clear:
            arc, anchor = near, _ANCHOR_TOP
        else:
            arc = (
                max(
                    src_y + src_h, tgt_y + tgt_h,
                    *(by + bh for _, by, _, bh in blockers),
                )
                + _ARC_CLEARANCE
            )
            anchor = _ANCHOR_BOTTOM
        sx, tx = src_x + src_w / 2, tgt_x + tgt_w / 2
        return [Waypoint(x=sx, y=arc), Waypoint(x=tx, y=arc)], anchor, anchor

    lo, hi = min(src_y + src_h, tgt_y), max(src_y + src_h, tgt_y)
    near = min(src_x, tgt_x, *(bx for bx, _, _, _ in blockers)) - _ARC_CLEARANCE
    near_clear = not _blocking_boxes(
        lo, hi, near, exclude_ids, boxes, container_ids, horizontal=False
    )
    if near_clear:
        arc, anchor = near, _ANCHOR_LEFT
    else:
        arc = (
            max(
                src_x + src_w, tgt_x + tgt_w,
                *(bx + bw for bx, _, bw, _ in blockers),
            )
            + _ARC_CLEARANCE
        )
        anchor = _ANCHOR_RIGHT
    sy, ty = src_y + src_h / 2, tgt_y + tgt_h / 2
    return [Waypoint(x=arc, y=sy), Waypoint(x=arc, y=ty)], anchor, anchor


def _fan_out_route(
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    fan_anchor: Anchor,
    *,
    horizontal: bool,
) -> tuple[list[Waypoint], Anchor, Anchor]:
    """Route a fan-out branch vertical/horizontal-first (cross axis first)
    into the side of the target matching ``fan_anchor``, instead of the
    default port every other branch of the same source would use."""
    src_x, src_y, src_w, src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    if horizontal:
        near_side = fan_anchor is _ANCHOR_TOP
        sx, tx = src_x + src_w / 2, tgt_x + tgt_w / 2
        sy = src_y if near_side else src_y + src_h
        ty = tgt_y + tgt_h if near_side else tgt_y
        source_anchor = _ANCHOR_TOP if near_side else _ANCHOR_BOTTOM
        target_anchor = _ANCHOR_BOTTOM if near_side else _ANCHOR_TOP
        mid = (sy + ty) / 2
        waypoints = (
            [Waypoint(x=sx, y=mid)]
            if abs(sx - tx) < 1
            else [Waypoint(x=sx, y=mid), Waypoint(x=tx, y=mid)]
        )
        return waypoints, source_anchor, target_anchor

    near_side = fan_anchor is _ANCHOR_LEFT
    sy, ty = src_y + src_h / 2, tgt_y + tgt_h / 2
    sx = src_x if near_side else src_x + src_w
    tx = tgt_x + tgt_w if near_side else tgt_x
    source_anchor = _ANCHOR_LEFT if near_side else _ANCHOR_RIGHT
    target_anchor = _ANCHOR_RIGHT if near_side else _ANCHOR_LEFT
    mid = (sx + tx) / 2
    waypoints = (
        [Waypoint(x=mid, y=sy)]
        if abs(sy - ty) < 1
        else [Waypoint(x=mid, y=sy), Waypoint(x=mid, y=ty)]
    )
    return waypoints, source_anchor, target_anchor


def _minimal_bend_route(
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    exit_anchor: Anchor,
    *,
    horizontal: bool,
) -> tuple[list[Waypoint], Anchor, Anchor]:
    """Connect a fan-out branch's forced cross-axis exit (top/bottom for a
    horizontal flow) into the target's ORDINARY forward-entry side, with a
    single-corner elbow -- the default whenever nothing needs the source and
    target paired on the SAME axis (:func:`_fan_out_route`'s pairing), which
    costs an extra bend unless they already happen to line up on the primary
    axis: cross-exit + primary-entry is an L (one corner) regardless of
    alignment, while cross-exit + matching cross-entry is a Z (two corners)
    whenever they don't.

    The one case that overrides this preference is a target fed by several
    sources (fan-in): the EARLIEST source (by rank) still gets this route,
    but later ones deliberately pay the extra bend to land on a different
    entry point -- see :func:`_route_edges`'s ``primary_incoming`` -- so two
    branches converging on the same step don't read as one merged line.
    """
    src_x, _src_y, src_w, _src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    if horizontal:
        sx = src_x + src_w / 2
        ty = tgt_y + tgt_h / 2
        return [Waypoint(x=sx, y=ty)], exit_anchor, _ANCHOR_LEFT
    sy = _src_y + _src_h / 2
    tx = tgt_x + tgt_w / 2
    return [Waypoint(x=tx, y=sy)], exit_anchor, _ANCHOR_TOP


def _minimal_bend_entry_route(
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    entry_anchor: Anchor,
    *,
    horizontal: bool,
) -> tuple[list[Waypoint], Anchor, Anchor]:
    """Mirror of :func:`_minimal_bend_route`: the SOURCE keeps its ordinary
    forward exit -- there is no sibling branch here needing it forced off the
    default port -- and the TARGET's entry side is forced instead, with a
    single-corner elbow.

    For a single-successor source whose target sits off its cross axis (e.g.
    a node relocated to a secondary row by the bypass-branch layout), the
    plain default route (:func:`_default_route`) pays an avoidable second
    bend splitting the cross-axis gap down the middle. Forcing only the
    entry side gets the same one-corner elbow :func:`_minimal_bend_route`
    gives fan-out branches, without forcing an exit side that would serve no
    purpose here (nothing else leaves this source that needs to visually
    diverge from it).
    """
    src_x, src_y, src_w, src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    if horizontal:
        sy = src_y + src_h / 2
        tx = tgt_x + tgt_w / 2
        return [Waypoint(x=tx, y=sy)], _ANCHOR_RIGHT, entry_anchor
    sx = src_x + src_w / 2
    ty = tgt_y + tgt_h / 2
    return [Waypoint(x=sx, y=ty)], _ANCHOR_BOTTOM, entry_anchor


def _default_route(
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    *,
    horizontal: bool,
) -> list[Waypoint]:
    """The plain default port-to-port elbow: source's forward side to
    target's back side, no anchor override (draw.io's own floating
    connection is fine here -- nothing to avoid, nothing fanning out)."""
    src_x, src_y, src_w, src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    if horizontal:
        sx, sy = src_x + src_w, src_y + src_h / 2
        tx, ty = tgt_x, tgt_y + tgt_h / 2
        mid = (sx + tx) / 2
        return (
            [Waypoint(x=mid, y=sy)]
            if abs(sy - ty) < 1
            else [Waypoint(x=mid, y=sy), Waypoint(x=mid, y=ty)]
        )
    sx, sy = src_x + src_w / 2, src_y + src_h
    tx, ty = tgt_x + tgt_w / 2, tgt_y
    mid = (sy + ty) / 2
    return (
        [Waypoint(x=sx, y=mid)]
        if abs(sx - tx) < 1
        else [Waypoint(x=sx, y=mid), Waypoint(x=tx, y=mid)]
    )


def _compute_primary_incoming(
    edges: list[Edge],
    node_by_id: dict[str, Node],
    in_degree: dict[str, int],
    node_boxes: dict[str, tuple[float, float, float, float]] | None,
    *,
    horizontal: bool,
) -> dict[str, int]:
    """For each target fed by ≥2 sources, ``id()`` of the edge from the
    EARLIEST source (by rank -- its position on the primary axis).

    That edge keeps the target's ordinary entry side; only later source(s)
    get pushed to the cross-axis entry a fan-out branch would otherwise pair
    with (see :func:`_minimal_bend_route`) -- without this, two branches
    from different gateways both landing on the same next step would both
    enter it from the identical point, reading as one merged line.
    """
    primary_incoming: dict[str, int] = {}
    best_rank: dict[str, float] = {}
    for edge in edges:
        if not (edge.source_id and edge.target_id) or in_degree[edge.target_id] < 2:
            continue
        src_node = node_by_id.get(edge.source_id)
        if src_node is None:
            continue
        src_box = (node_boxes or {}).get(src_node.id) or (
            src_node.x, src_node.y, src_node.width, src_node.height
        )
        rank = src_box[0] if horizontal else src_box[1]
        if edge.target_id not in best_rank or rank < best_rank[edge.target_id]:
            best_rank[edge.target_id] = rank
            primary_incoming[edge.target_id] = id(edge)
    return primary_incoming


def _route_one_edge(
    edge: Edge,
    src_box: tuple[float, float, float, float],
    tgt_box: tuple[float, float, float, float],
    exclude_ids: set[str],
    boxes: dict[str, tuple[float, float, float, float]],
    container_ids: set[str],
    out_degree: dict[str, int],
    in_degree: dict[str, int],
    primary_incoming: dict[str, int],
    *,
    horizontal: bool,
) -> tuple[list[Waypoint], Anchor | str, Anchor | str]:
    """Pick a route for one edge: around an obstruction, as a fan-out/fan-in
    branch, or the plain default port-to-port elbow -- see
    :func:`_obstruction_route`/:func:`_fan_out_route`/
    :func:`_minimal_bend_route`/:func:`_default_route`.
    """
    source_anchor: Anchor | str = edge.source_anchor
    target_anchor: Anchor | str = edge.target_anchor
    src_x, src_y, src_w, src_h = src_box
    tgt_x, tgt_y, tgt_w, tgt_h = tgt_box
    # An already-explicit anchor (an overlay round-trip, or a future author
    # override) is never second-guessed here.
    has_explicit_anchor = bool(edge.source_anchor) or bool(edge.target_anchor)

    level = src_y + src_h / 2 if horizontal else src_x + src_w / 2
    far_level = tgt_y + tgt_h / 2 if horizontal else tgt_x + tgt_w / 2
    blockers = (
        _blocking_boxes(
            min(src_x + src_w, tgt_x) if horizontal else min(src_y + src_h, tgt_y),
            max(src_x + src_w, tgt_x) if horizontal else max(src_y + src_h, tgt_y),
            level, exclude_ids, boxes, container_ids, horizontal=horizontal,
        )
        if not has_explicit_anchor and abs(level - far_level) < 1
        else []
    )
    if blockers:
        return _obstruction_route(
            src_box, tgt_box, blockers, exclude_ids, boxes, container_ids,
            horizontal=horizontal,
        )

    is_branching_source = out_degree.get(edge.source_id or "", 0) >= 2
    fan_anchor = (
        _fan_out_anchor(src_box, tgt_box, horizontal=horizontal)
        if not has_explicit_anchor and is_branching_source
        else None
    )
    if fan_anchor is None:
        if not has_explicit_anchor and not is_branching_source:
            # A single-successor source has no sibling branch that needs its
            # exit forced off the default port -- but its target can still
            # sit off the cross axis (e.g. relocated to a secondary row by
            # the bypass-branch layout), where the plain default route below
            # would pay an avoidable second bend. Force only the entry side.
            entry_anchor = _fan_out_anchor(tgt_box, src_box, horizontal=horizontal)
            if entry_anchor is not None:
                return _minimal_bend_entry_route(
                    src_box, tgt_box, entry_anchor, horizontal=horizontal
                )
        return _default_route(src_box, tgt_box, horizontal=horizontal), \
            source_anchor, target_anchor

    # The paired cross-axis entry costs an extra bend (see _minimal_bend_route)
    # -- worth it ONLY to keep a later-arriving branch off a target's already-
    # claimed entry point. Every other case (no fan-in at all, or this IS the
    # earliest source) takes the cheaper, fewer-bend route by default.
    needs_paired_entry = (
        in_degree.get(edge.target_id or "", 0) >= 2
        and primary_incoming.get(edge.target_id or "") != id(edge)
    )
    if needs_paired_entry:
        return _fan_out_route(src_box, tgt_box, fan_anchor, horizontal=horizontal)
    return _minimal_bend_route(src_box, tgt_box, fan_anchor, horizontal=horizontal)


def _route_edges(
    edges: list[Edge],
    node_by_id: dict[str, Node],
    *,
    direction: str,
    node_boxes: dict[str, tuple[float, float, float, float]] | None = None,
) -> list[Edge]:
    horizontal = direction == "LR"
    boxes = node_boxes or {}
    # A node referenced as some other node's parent is a container -- routing
    # through it (e.g. a Lane) is expected, so it never counts as a blocker.
    container_ids = {n.parent_id for n in node_by_id.values() if n.parent_id}
    out_degree: dict[str, int] = defaultdict(int)
    in_degree: dict[str, int] = defaultdict(int)
    for edge in edges:
        if edge.source_id and edge.target_id:
            out_degree[edge.source_id] += 1
            in_degree[edge.target_id] += 1
    primary_incoming = _compute_primary_incoming(
        edges, node_by_id, in_degree, node_boxes, horizontal=horizontal
    )

    routed: list[Edge] = []

    for edge in edges:
        src = node_by_id.get(edge.source_id or "")
        tgt = node_by_id.get(edge.target_id or "")
        waypoints: list[Waypoint] = []
        source_anchor = edge.source_anchor
        target_anchor = edge.target_anchor

        if src is not None and tgt is not None:
            src_box = boxes.get(src.id, (src.x, src.y, src.width, src.height))
            tgt_box = boxes.get(tgt.id, (tgt.x, tgt.y, tgt.width, tgt.height))
            waypoints, source_anchor, target_anchor = _route_one_edge(
                edge, src_box, tgt_box, {src.id, tgt.id}, boxes, container_ids,
                out_degree, in_degree, primary_incoming, horizontal=horizontal,
            )

        routed.append(
            Edge(
                id=edge.id,
                type=edge.type,
                source_id=edge.source_id,
                target_id=edge.target_id,
                waypoints=waypoints,
                source_anchor=source_anchor,
                target_anchor=target_anchor,
                hidden=edge.hidden,
                label=edge.label,
                description=edge.description,
                style_overrides=dict(edge.style_overrides),
                extra=dict(edge.extra),
            )
        )

    return routed


def _restore_reversed_edges(edges: list[Edge], reversed_ids: set[int]) -> None:
    """Un-swap the endpoints of back edges after ranking, in place.

    Cycle removal reverses back edges so ranking sees a DAG. These same edge
    objects are what the generator emits, so their original orientation must be
    restored — otherwise a back edge is emitted with swapped source/target (and,
    for passthrough edges whose id is derived from the endpoints, a duplicate id
    that collides with its counterpart). ``hidden`` is left untouched.
    """
    for edge in edges:
        if id(edge) in reversed_ids:
            edge.source_id, edge.target_id = edge.target_id, edge.source_id


LAYOUT_MODE = "layered"
LAYOUT_CLASS: type[BaseLayout] = LayeredLayout
