"""Process-mode layout — left-to-right flow with optional swimlanes.

Wraps layered layout with ``direction="LR"``. Supports rank-excluded nodes (e.g.
data artifacts positioned by the caller).
"""

from __future__ import annotations

from collections import defaultdict
from math import ceil

from ._container_layout import absolute_node_boxes
from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
    resolve_node_size,
)
from .config import Config
from .layered import LayeredLayout


def _resolve_missing_sizes(nodes: list[Node], size_of: SizeResolver) -> None:
    """Fill missing dimensions for nodes that bypass layered layout."""
    for node in nodes:
        width, height = resolve_node_size(size_of, node)
        if not node.width:
            node.width = float(width)
        if not node.height:
            node.height = float(height)


def _reserve_headroom_for_excluded(
    ranked_nodes: list[Node],
    excluded_nodes: list[Node],
    passthrough_edges: list[Edge],
    *,
    gap: float,
) -> None:
    """Give each container that will anchor a floating excluded node (e.g. a
    Lane with a task a data object sits above) extra top padding BEFORE
    layout runs, sized to fit it.

    Without this, the container's height is finalized by the time
    :func:`_position_excluded_nodes` places the floating node above its
    anchor, so the float has nowhere to go but above the container's own
    boundary -- reserving the space up front instead pushes the whole
    container's content down to make room, the same way a real swimlane
    would need to grow to fit an annotation drawn above its content.
    """
    ranked_ids = {n.id for n in ranked_nodes}
    excluded_by_id = {n.id: n for n in excluded_nodes}
    node_by_id = {n.id: n for n in ranked_nodes}
    needed_top: dict[str, float] = defaultdict(float)

    for edge in passthrough_edges:
        source_id, target_id = edge.source_id, edge.target_id
        if source_id in excluded_by_id and target_id in ranked_ids:
            anchor_id, excluded = target_id, excluded_by_id[source_id]
        elif target_id in excluded_by_id and source_id in ranked_ids:
            anchor_id, excluded = source_id, excluded_by_id[target_id]
        else:
            continue
        anchor = node_by_id.get(anchor_id)
        if anchor is None or not anchor.parent_id:
            continue
        needed_top[anchor.parent_id] = max(
            needed_top[anchor.parent_id], excluded.height + gap
        )

    for parent_id, extra in needed_top.items():
        parent = node_by_id.get(parent_id)
        if parent is not None:
            raw_existing = parent.extra.get("padding_extra_top", 0.0)
            try:
                existing = float(raw_existing)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"node {parent.id!r}: padding 'padding_extra_top' "
                    f"must be numeric, got {raw_existing!r}"
                ) from exc
            parent.extra["padding_extra_top"] = max(existing, extra)


def _position_excluded_nodes(
    excluded_nodes: list[Node],
    ranked_nodes: list[Node],
    passthrough_edges: list[Edge],
    *,
    gap: float,
) -> None:
    """Position each rank-excluded node directly above the EARLIEST-ranked
    node it connects to (smallest ``x``, i.e. first in the LR sequence).

    A data artifact shared across several tasks (e.g. a BPMN DataObject read
    by more than one) renders above the first task in the sequence, not
    whichever one its edge happens to be declared toward -- matching how a
    hand-drawn diagram places it once at the point the data first enters the
    flow, not once per consumer.

    Excluded nodes with no connection to anything ranked are left at their
    origin geometry (there's nothing to anchor them to).
    """
    ranked_by_id = {n.id: n for n in ranked_nodes}
    excluded_ids = {n.id for n in excluded_nodes}
    connections: dict[str, list[str]] = defaultdict(list)
    for edge in passthrough_edges:
        source_id, target_id = edge.source_id, edge.target_id
        if source_id in excluded_ids and target_id in ranked_by_id:
            connections[source_id].append(target_id)
        elif target_id in excluded_ids and source_id in ranked_by_id:
            connections[target_id].append(source_id)

    for node in excluded_nodes:
        connected_ids = connections.get(node.id, [])
        if not connected_ids:
            continue
        anchor = min((ranked_by_id[cid] for cid in connected_ids), key=lambda n: n.x)
        node.x = anchor.x + anchor.width / 2 - node.width / 2
        node.y = anchor.y - node.height - gap


def _fit_final_bounds(
    nodes: list[Node], result: Result, cfg: Config
) -> tuple[int, int]:
    """Keep floating top-level nodes on-canvas and include them in page size."""
    boxes = absolute_node_boxes(nodes)
    if not boxes:
        return result.page_width, result.page_height

    min_x = min(x for x, _, _, _ in boxes.values())
    min_y = min(y for _, y, _, _ in boxes.values())
    shift_x = max(0.0, -min_x)
    shift_y = max(0.0, -min_y)
    if shift_x or shift_y:
        for node in nodes:
            if not node.parent_id:
                node.x += shift_x
                node.y += shift_y
        boxes = absolute_node_boxes(nodes)

    max_x = max(x + width for x, _, width, _ in boxes.values())
    max_y = max(y + height for _, y, _, height in boxes.values())
    return (
        max(result.page_width, ceil(max_x + cfg.margin_x)),
        max(result.page_height, ceil(max_y + cfg.margin_y)),
    )


class ProcessLayout(BaseLayout):
    """Left-to-right process flow layout.

    ``rank_exclude_ids`` names nodes excluded from the rank graph -- e.g. a
    BPMN data artifact linked by association rather than sequence flow.
    Excluded nodes are positioned above the earliest-ranked node they connect
    to (see :func:`_position_excluded_nodes`) rather than joining the ranked
    flow; edges touching them pass through unrouted.

    This is the only mode that asks the layered layout to route edges
    (``route_edges=True``); everywhere else draw.io does its own routing.
    """

    def apply(
        self,
        nodes: list[Node],
        edges: list[Edge],
        size_of: SizeResolver,
        config: Config | None = None,
    ) -> Result:
        cfg = config or Config()
        rank_exclude_ids = cfg.rank_exclude_ids

        if not rank_exclude_ids:
            ranked_nodes, excluded_nodes = list(nodes), []
            rank_edges, passthrough_edges = list(edges), []
        else:
            ranked_nodes = [n for n in nodes if n.id not in rank_exclude_ids]
            excluded_nodes = [n for n in nodes if n.id in rank_exclude_ids]
            rank_edges = [
                e
                for e in edges
                if e.source_id not in rank_exclude_ids
                and e.target_id not in rank_exclude_ids
            ]
            # Partition by object identity — two distinct edges can be equal by
            # value (same endpoints/type), so ``in`` on a list would misclassify
            # them.
            rank_edge_ids = {id(e) for e in rank_edges}
            passthrough_edges = [e for e in edges if id(e) not in rank_edge_ids]

        _resolve_missing_sizes(excluded_nodes, size_of)
        _reserve_headroom_for_excluded(
            ranked_nodes, excluded_nodes, passthrough_edges, gap=cfg.column_gap
        )

        # Process mode is the one layout that wants computed edge geometry:
        # a left-to-right flow with swimlanes, gateway fan-outs and detour
        # branches reads far better with the elbows (and matching exit/entry
        # sides) worked out here than with draw.io's own routing. Every other
        # mode leaves routing to draw.io -- see LayeredLayout.route_edges.
        layered = LayeredLayout(route_edges=True)
        result = layered.apply(ranked_nodes, rank_edges, size_of, config=cfg)

        _position_excluded_nodes(
            excluded_nodes, result.nodes, passthrough_edges, gap=cfg.column_gap
        )

        laid_out = result.nodes + excluded_nodes
        routed_edges = result.edges + passthrough_edges
        page_width, page_height = _fit_final_bounds(laid_out, result, cfg)

        return Result(
            nodes=laid_out,
            edges=routed_edges,
            page_width=page_width,
            page_height=page_height,
        )


LAYOUT_MODE = "process"
LAYOUT_CLASS: type[BaseLayout] = ProcessLayout
