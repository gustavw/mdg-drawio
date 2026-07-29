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

from mdg_drawio.contracts import DEFAULT_PAGE_HEIGHT, DEFAULT_PAGE_WIDTH

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


def _route_edges(
    edges: list[Edge],
    node_by_id: dict[str, Node],
    *,
    direction: str,
    node_boxes: dict[str, tuple[float, float, float, float]] | None = None,
) -> list[Edge]:
    horizontal = direction == "LR"
    routed: list[Edge] = []

    for edge in edges:
        src = node_by_id.get(edge.source_id or "")
        tgt = node_by_id.get(edge.target_id or "")
        waypoints: list[Waypoint] = []

        if src is not None and tgt is not None:
            src_x, src_y, src_w, src_h = (
                node_boxes.get(src.id, (src.x, src.y, src.width, src.height))
                if node_boxes is not None
                else (src.x, src.y, src.width, src.height)
            )
            tgt_x, tgt_y, tgt_w, tgt_h = (
                node_boxes.get(tgt.id, (tgt.x, tgt.y, tgt.width, tgt.height))
                if node_boxes is not None
                else (tgt.x, tgt.y, tgt.width, tgt.height)
            )
            if horizontal:
                sx = src_x + src_w
                sy = src_y + src_h / 2
                tx = tgt_x
                ty = tgt_y + tgt_h / 2

                mid_x = (sx + tx) / 2
                if abs(sy - ty) < 1:
                    waypoints = [Waypoint(x=mid_x, y=sy)]
                else:
                    waypoints = [
                        Waypoint(x=mid_x, y=sy),
                        Waypoint(x=mid_x, y=ty),
                    ]
            else:
                sx = src_x + src_w / 2
                sy = src_y + src_h
                tx = tgt_x + tgt_w / 2
                ty = tgt_y

                mid_y = (sy + ty) / 2
                if abs(sx - tx) < 1:
                    waypoints = [Waypoint(x=sx, y=mid_y)]
                else:
                    waypoints = [
                        Waypoint(x=sx, y=mid_y),
                        Waypoint(x=tx, y=mid_y),
                    ]

        routed.append(
            Edge(
                id=edge.id,
                type=edge.type,
                source_id=edge.source_id,
                target_id=edge.target_id,
                waypoints=waypoints,
                source_anchor=edge.source_anchor,
                target_anchor=edge.target_anchor,
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
