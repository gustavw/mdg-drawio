"""Shared data models for the draw.io generation pipeline.

These are the contract types that the DSL parser produces and the drawio
generator consumes. All types are plain dataclasses — no external dependencies.

Improvements over the DrawIoGen reference (TypedDicts):
- @dataclass gives __init__, __repr__, __eq__ for free
- Explicit Optional defaults; no total=False masking
- __post_init__ validates required fields at construction time
- frozen=True on immutable leaf types
- to_dict() / from_dict() for JSON round-trip
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from .constants import (
    A4_LANDSCAPE_HEIGHT,
    A4_LANDSCAPE_WIDTH,
    DEFAULT_BOUNDARY_PADDING,
)

# draw.io style tokens are semicolon-delimited key=value pairs.
StyleDict: TypeAlias = dict[str, str | int | float | None]


def index_shapes_by_function(
    shapes: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group notation registry shape entries by their ``function`` name.

    The single source for the ``{function: [entries]}`` transformation shared by
    the registry, the DSL engine, the layout size resolver, and the generator's
    StyleProvider — each of which would otherwise re-implement it.
    """
    by_function: dict[str, list[dict[str, Any]]] = {}
    for shape in shapes:
        by_function.setdefault(shape["function"], []).append(shape)
    return by_function


# ---------------------------------------------------------------------------
# Leaf types (frozen — hashable, cachable, immutable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeometryPoint:
    """A single point in an edge's geometry path."""
    x: float = 0.0
    y: float = 0.0
    as_: str = ""  # "sourcePoint", "targetPoint", or empty


@dataclass(frozen=True)
class Anchor:
    """Explicit anchor coordinates for edge endpoints."""
    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    perimeter: float = 0.0


@dataclass(frozen=True)
class Diagram:
    """Page-level metadata."""
    name: str = ""
    description: str = ""
    page_width: float = A4_LANDSCAPE_WIDTH     # A4 landscape default
    page_height: float = A4_LANDSCAPE_HEIGHT   # A4 landscape default
    aspect_ratio: str = ""
    mode: str = ""                # layout mode: layered, sequence, process, palette
    direction: str = ""           # flow override: "TB"|"LR" (empty = config default)


# ---------------------------------------------------------------------------
# Compound types
# ---------------------------------------------------------------------------

@dataclass
class BoundaryPadding:
    """Padding inside a container boundary."""
    top: float = DEFAULT_BOUNDARY_PADDING
    right: float = DEFAULT_BOUNDARY_PADDING
    bottom: float = DEFAULT_BOUNDARY_PADDING
    left: float = DEFAULT_BOUNDARY_PADDING


@dataclass
class GeometryChild:
    """A child element within an mxGeometry (e.g. <Array>, <mxPoint>)."""
    tag: str = ""
    attributes: StyleDict = field(default_factory=dict)


@dataclass
class ChildCell:
    """A child mxCell attached to a node (e.g. label cell, divider)."""
    label: str = ""
    style_overrides: StyleDict = field(default_factory=dict)
    geometry_attributes: StyleDict = field(default_factory=dict)
    geometry_points: list[StyleDict] = field(default_factory=list)
    cell_attributes: StyleDict = field(default_factory=dict)


@dataclass
class NodeChildCell:
    """A recursively nestable child cell (e.g. UML class member rows)."""
    label: str = ""
    object_attributes: StyleDict = field(default_factory=dict)
    style_overrides: StyleDict = field(default_factory=dict)
    geometry_attributes: StyleDict = field(default_factory=dict)
    geometry_children: list[GeometryChild] = field(default_factory=list)
    cell_attributes: StyleDict = field(default_factory=dict)
    child_cells: list[NodeChildCell] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core model types
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """A diagram node — shape, container, or swimlane.

    ``id``, ``type``, and ``label`` are required (validated in __post_init__).
    All other fields are optional and default to their zero/empty values.
    """

    id: str = ""
    type: str = ""
    label: str = ""

    # Geometry (assigned by the layout engine)
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    # Absolute (page-frame) coordinates, retained while x/y are rewritten to be
    # parent-relative during containment layout so page-size and reverse-sync
    # math can recover the original position.
    abs_x: float | None = None
    abs_y: float | None = None

    # Hierarchy
    parent_id: str | None = None
    contains: list[str] = field(default_factory=list)

    # Text content
    text_parts: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    # Layout hints
    padding: BoundaryPadding | None = None

    # Style and geometry overrides
    style_overrides: StyleDict = field(default_factory=dict)
    object_attributes: StyleDict = field(default_factory=dict)
    geometry_attributes: StyleDict = field(default_factory=dict)
    geometry_children: list[GeometryChild] = field(default_factory=list)
    child_cells: list[NodeChildCell] = field(default_factory=list)

    # Identity
    palette_label: str = ""
    palette_decoration: bool = False
    variant: int = 1
    element_name: str = ""  # DSL call name this node was authored as
    key_label: str = ""     # Extra label for the key cell in ERD table rows

    # Extra passthrough for layout/generator round-trips
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Node.id is required")
        if not self.type:
            raise ValueError("Node.type is required")
        if not self.label:
            raise ValueError("Node.label is required")


@dataclass
class Edge:
    """A diagram edge — relationship, association, or flow.

    ``type`` is required (validated in __post_init__).
    ``id`` is optional — auto-generated if absent.
    """

    id: str = ""
    type: str = ""
    source_id: str = ""
    target_id: str = ""

    label: str = ""
    description: str = ""
    source_anchor: str | Anchor = ""
    target_anchor: str | Anchor = ""

    # Waypoints (populated by the layout engine)
    waypoints: list[GeometryPoint] = field(default_factory=list)

    # Style
    style_overrides: StyleDict = field(default_factory=dict)
    object_attributes: StyleDict = field(default_factory=dict)
    geometry_attributes: StyleDict = field(default_factory=dict)
    geometry_points: list[StyleDict] = field(default_factory=list)
    child_cells: list[ChildCell] = field(default_factory=list)

    # Flags
    palette_decoration: bool = False
    hidden: bool = False

    # Extra passthrough
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("Edge.type is required")
        if self.source_id and not self.target_id:
            raise ValueError(
                f"Edge {self.id!r}: source_id set but target_id is empty"
            )
        if self.target_id and not self.source_id:
            raise ValueError(
                f"Edge {self.id!r}: target_id set but source_id is empty"
            )


@dataclass
class EdgeAnchorOverlay:
    """Existing edge geometry from a previous .drawio output.

    Captures anchors (exitX/exitY/entryX/entryY from the style string)
    and elbow waypoints (from <Array as="points"> in mxGeometry).
    """
    exit_x: str | None = None
    exit_y: str | None = None
    entry_x: str | None = None
    entry_y: str | None = None
    waypoints: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class GeometryOverlay:
    """Existing geometry from a previous .drawio output.

    Injected into ``Document`` before layout/generation. Layout respects
    node positions that already exist; generation reads edge anchors and
    elbow waypoints, preserving manual adjustments.
    """
    nodes: dict[str, dict[str, float]] = field(default_factory=dict)
    edges: dict[str, EdgeAnchorOverlay] = field(default_factory=dict)


@dataclass
class Document:
    """A single diagram page — the unit of work for layout and generation."""

    diagram: Diagram = field(default_factory=Diagram)
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    geometry_overlay: GeometryOverlay | None = None

@dataclass
class MultiPageDocument:
    """Multiple pages, parsed from a single .mdg file."""

    pages: list[Document] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("MultiPageDocument.pages must not be empty")


