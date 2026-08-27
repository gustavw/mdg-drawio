"""Sequence-mode layout — participant columns with horizontal edges.

Each node becomes a column header (participant). Edges are drawn as horizontal
connections between columns, stacked top-to-bottom in declaration order.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from mdg_drawio.contracts import DEFAULT_PAGE_HEIGHT, DEFAULT_PAGE_WIDTH

from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
    Waypoint,
    resolve_node_size,
)
from .config import Config, resolve_page_size


class SequenceLayout(BaseLayout):
    """Lay out nodes and edges in sequence-diagram style."""

    def apply(
        self,
        nodes: list[Node],
        edges: list[Edge],
        size_of: SizeResolver,
        config: Config | None = None,
    ) -> Result:
        cfg = config or Config()

        if not nodes:
            pw, ph = DEFAULT_PAGE_WIDTH, DEFAULT_PAGE_HEIGHT
            return Result(nodes=[], edges=[], page_width=pw, page_height=ph)

        placed: list[Node] = []
        column_centers: dict[str, float] = {}
        column_rights: dict[str, float] = {}

        x = float(cfg.margin_x)
        max_header_height = 0.0
        total_nodes_width = 0.0

        for node in nodes:
            w, h = resolve_node_size(size_of, node)
            width = node.width if node.width else w
            height = node.height if node.height else h

            # Only geometry is decided here; every other field carries over
            # via replace(). A field-by-field reconstruction silently drops
            # anything not in the hand-picked list (``variant``,
            # ``object_attributes``, ``child_cells``, ...) back to its
            # dataclass default -- see the same fix in palette.py.
            placed_node = replace(
                node,
                x=x,
                y=cfg.margin_y,
                width=float(width),
                height=float(height),
                style_overrides=dict(node.style_overrides),
                extra=dict(node.extra),
            )
            placed.append(placed_node)
            column_centers[node.id] = x + float(width) / 2.0
            column_rights[node.id] = x + float(width)
            x += float(width) + cfg.column_gap
            max_header_height = max(max_header_height, float(height))
            total_nodes_width = x - cfg.column_gap

        y = cfg.margin_y + max_header_height + cfg.row_gap
        routed: list[Edge] = []

        for edge in edges:
            src_x = column_centers.get(edge.source_id)
            tgt_x = column_centers.get(edge.target_id)
            if src_x is None or tgt_x is None:
                # An endpoint names a participant that was never declared —
                # skip it rather than draw a meaningless line to a fabricated
                # column position.
                missing = edge.source_id if src_x is None else edge.target_id
                print(
                    f"mdg: warning: sequence edge {edge.id!r} references "
                    f"unknown participant {missing!r} — skipped",
                    file=sys.stderr,
                )
                continue

            routed.append(
                replace(
                    edge,
                    waypoints=[
                        Waypoint(x=src_x, y=y),
                        Waypoint(x=tgt_x, y=y),
                    ],
                    style_overrides=dict(edge.style_overrides),
                    extra=dict(edge.extra),
                )
            )
            y += cfg.row_gap

        content_w = total_nodes_width
        content_h = (y - cfg.row_gap)
        page_w, page_h = resolve_page_size(
            content_width=content_w,
            content_height=content_h,
            margin_x=cfg.margin_x,
            margin_y=cfg.margin_y,
            aspect_ratio=cfg.aspect_ratio,
        )

        return Result(
            nodes=placed,
            edges=routed,
            page_width=page_w,
            page_height=page_h,
        )


LAYOUT_MODE = "sequence"
LAYOUT_CLASS: type[BaseLayout] = SequenceLayout
