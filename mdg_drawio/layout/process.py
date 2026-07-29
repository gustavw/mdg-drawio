"""Process-mode layout — left-to-right flow with optional swimlanes.

Wraps layered layout with ``direction="LR"``. Supports rank-excluded nodes (e.g.
data artifacts positioned by the caller).
"""

from __future__ import annotations

from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
)
from .config import Config
from .layered import LayeredLayout


class ProcessLayout(BaseLayout):
    """Left-to-right process flow layout.

    ``rank_exclude_ids`` names nodes excluded from the rank graph. Excluded
    nodes are returned untouched alongside the ranked ones; edges touching
    excluded nodes pass through unrouted.
    """

    def apply(
        self,
        nodes: list[Node],
        edges: list[Edge],
        size_of: SizeResolver,
        config: Config | None = None,
        rank_exclude_ids: frozenset[str] | set[str] = frozenset(),
    ) -> Result:
        cfg = config or Config()

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

        layered = LayeredLayout()
        result = layered.apply(ranked_nodes, rank_edges, size_of, config=cfg)

        laid_out = result.nodes + excluded_nodes
        routed_edges = result.edges + passthrough_edges

        return Result(
            nodes=laid_out,
            edges=routed_edges,
            page_width=result.page_width,
            page_height=result.page_height,
        )


LAYOUT_MODE = "process"
LAYOUT_CLASS: type[BaseLayout] = ProcessLayout
