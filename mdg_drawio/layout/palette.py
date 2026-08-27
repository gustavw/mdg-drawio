"""Palette-mode layout — read exact positions from a pre-baked file.

Unlike the algorithmic layouts, palette mode does not compute anything. A
palette file (JSON) already contains ``x, y, width, height`` for every node and
``geometryPoints`` for every edge. This layout simply applies those positions.

Follows the same `Result` contract so the generator treats palette like
any other mode.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from mdg_drawio.contracts import (
    PALETTE_DEFAULT_PAGE_HEIGHT,
    PALETTE_DEFAULT_PAGE_WIDTH,
    PALETTE_MODE,
)

from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
    Waypoint,
)
from .config import Config


class PaletteLayout(BaseLayout):
    """Apply pre-baked positions from a palette file."""

    margin_x: float = 0
    margin_y: float = 0

    palette_path: Path | str | None = None

    def __init__(self) -> None:
        # Instance-level so palette data never leaks between layout runs.
        self._positions: list[dict[str, Any]] = []

    def _load_positions(self) -> None:
        """Load pre-baked positions from ``palette_path`` if one is set."""
        self._positions = []
        if self.palette_path is None:
            return
        try:
            with open(self.palette_path, encoding="utf-8") as fh:
                self._positions = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"could not read palette file {self.palette_path!r}: {exc}"
            ) from exc

    def apply(
        self,
        nodes: list[Node],
        edges: list[Edge],
        size_of: SizeResolver,
        config: Config | None = None,
    ) -> Result:
        self._load_positions()

        node_map: dict[str, dict[str, Any]] = {}
        for entry in self._positions:
            nid = entry.get("id", entry.get("elementName", ""))
            if nid:
                node_map[nid] = entry

        placed: list[Node] = []
        for node in nodes:
            pos = node_map.get(node.id) if node.id else None
            if pos is None:
                pos = node_map.get(node.extra.get("elementName", ""))
            # Only position (x/y/width/height) comes from the palette file;
            # every other field carries over from the parsed node via
            # replace() -- a field-by-field reconstruction here previously
            # silently dropped variant (and would drop any future field too),
            # so a variant=2 shape rendered with variant 1's style/label.
            placed.append(
                replace(
                    node,
                    x=float(pos.get("x", node.x)) if pos else node.x,
                    y=float(pos.get("y", node.y)) if pos else node.y,
                    width=float(pos.get("width", node.width))
                    if pos
                    else node.width,
                    height=float(pos.get("height", node.height))
                    if pos
                    else node.height,
                    style_overrides=dict(node.style_overrides),
                    extra=dict(node.extra),
                )
            )

        edge_map: dict[str, list[Waypoint]] = {}
        for entry in self._positions:
            eid = entry.get("id", "")
            pts = entry.get("geometryPoints") or entry.get("waypoints") or []
            if eid and pts:
                edge_map[eid] = [
                    Waypoint(x=p.get("x", 0), y=p.get("y", 0)) for p in pts
                ]

        routed: list[Edge] = []
        for edge in edges:
            # Same reasoning as the node loop above: only waypoints come from
            # the palette file, everything else (object_attributes, variant
            # in extra, etc.) carries over via replace().
            routed.append(
                replace(
                    edge,
                    waypoints=edge_map.get(edge.id, edge.waypoints),
                    style_overrides=dict(edge.style_overrides),
                    extra=dict(edge.extra),
                )
            )

        page_w = PALETTE_DEFAULT_PAGE_WIDTH
        page_h = PALETTE_DEFAULT_PAGE_HEIGHT
        if placed:
            max_x = max(n.x + n.width for n in placed)
            max_y = max(n.y + n.height for n in placed)
            page_w = int(max(max_x + self.margin_x, PALETTE_DEFAULT_PAGE_WIDTH))
            page_h = int(max(max_y + self.margin_y, PALETTE_DEFAULT_PAGE_HEIGHT))

        return Result(
            nodes=placed,
            edges=routed,
            page_width=page_w,
            page_height=page_h,
        )


LAYOUT_MODE = PALETTE_MODE
LAYOUT_CLASS: type[BaseLayout] = PaletteLayout
