"""Shared data models for the draw.io generation pipeline.

These are the contract types that the DSL parser produces and the drawio
generator consumes. All types are plain dataclasses — no external dependencies.

Improvements over the DrawIoGen reference (TypedDicts):
- @dataclass gives __init__, __repr__, __eq__ for free
- Explicit Optional defaults; no total=False masking
- __post_init__ validates required fields at construction time
- frozen=True on immutable leaf types

Note for anyone rebuilding one of these: use ``dataclasses.replace`` rather
than reconstructing field-by-field. A hand-picked field list silently resets
everything it omits to the dataclass default, which has caused real rendering
bugs in the layout modes more than once (see ``layout/palette.py`` and
``layout/layered.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import escape
from typing import Any

from .constants import (
    A4_LANDSCAPE_HEIGHT,
    A4_LANDSCAPE_WIDTH,
    DEFAULT_BOUNDARY_PADDING,
    DIAGRAM_ID_PREFIX,
    DRAWIO_ACTION_LINK_PREFIX,
    DRAWIO_PAGE_LINK_PREFIX,
    PAGE_LINKS_COUNT_STYLE,
    PAGE_LINKS_COUNT_SYMBOL,
    PAGE_LINKS_MARKER_ATTRIBUTE,
)

# draw.io style tokens are semicolon-delimited key=value pairs.
type StyleDict = dict[str, str | int | float | None]


def derived_edge_id(source_id: str, target_id: str) -> str:
    """Return a deterministic, collision-free id for one directed edge.

    The readable ``source-target`` form covers normal identifier-safe node
    ids. MDG also accepts hyphenated node ids, where plain concatenation is
    ambiguous; a source-length prefix makes those pairs unambiguous while
    keeping the common form compact.
    """
    if "-" in source_id or "-" in target_id:
        return f"{len(source_id)}-{source_id}-{target_id}"
    return f"{source_id}-{target_id}"


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


def page_diagram_id(page_index: int) -> str:
    """Return the ``<diagram>`` id of the page at *page_index*.

    The single source for page ids: the multi-page generator assigns them and
    a cross-page reference link points at them, so the two cannot drift.
    """
    return f"{DIAGRAM_ID_PREFIX}{page_index}"


def page_cell_link(page_id: str, cell_id: str) -> str:
    """Return a draw.io link that opens page *page_id* and shows *cell_id*.

    draw.io runs the actions of a ``data:action/json`` link in order: ``open``
    switches to the page, then ``select`` and ``scroll`` bring the target
    cell into view on it.
    """
    actions = [
        {"open": f"{DRAWIO_PAGE_LINK_PREFIX}{page_id}"},
        {"select": {"cells": [cell_id]}},
        {"scroll": {"cells": [cell_id]}},
    ]
    return DRAWIO_ACTION_LINK_PREFIX + json.dumps(
        {"actions": actions}, separators=(",", ":")
    )


def page_links_html(links: Iterable[PageLink]) -> str:
    """Return the label fragment that carries *links* in a draw.io cell.

    draw.io lists every ``<a>`` in a selected cell's label under the cell,
    titled with the anchor's text, and follows the one clicked. The anchors
    are hidden in the drawing itself; a small count shows that they are there.
    The whole fragment is one ``<span>`` marked with
    ``PAGE_LINKS_MARKER_ATTRIBUTE`` so ``strip_page_links_html`` can remove it
    again: it is generated output, never part of the authored label.
    """
    ordered = sorted(links, key=lambda link: link.title)
    if not ordered:
        return ""
    anchors = "".join(
        f'<a href="{escape(page_cell_link(link.page_id, link.cell_id))}" '
        f'style="display:none">{escape(link.title)}</a>'
        for link in ordered
    )
    return (
        f'<span {PAGE_LINKS_MARKER_ATTRIBUTE}="{len(ordered)}" '
        f'style="{PAGE_LINKS_COUNT_STYLE}">'
        f"{escape(page_links_count_text(len(ordered)))}{anchors}</span>"
    )


def page_links_count_text(count: int) -> str:
    """The visible text ``page_links_html`` adds after a label for *count*
    links; size resolution measures it so the label still fits its shape."""
    return f" {PAGE_LINKS_COUNT_SYMBOL}{count}" if count else ""


_PAGE_LINKS_SPAN_RE = re.compile(
    rf"<span\b[^>]*\b{re.escape(PAGE_LINKS_MARKER_ATTRIBUTE)}\b[^>]*>.*?</span>",
    re.DOTALL | re.IGNORECASE,
)


def strip_page_links_html(label: str) -> str:
    """Remove the fragment ``page_links_html`` added to a label.

    The fragment holds no ``<span>`` of its own, so the first ``</span>``
    after the marker closes it. A label without the marker is returned as is.
    """
    return _PAGE_LINKS_SPAN_RE.sub("", label)


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
class PageLink:
    """A link from a node to a node on another page.

    An edge whose endpoints are declared on different pages is not drawn:
    each endpoint gets a ``PageLink`` to the other instead. *title* is what
    draw.io shows for the link; *page_id* and *cell_id* say where it goes.
    """
    title: str
    page_id: str
    cell_id: str


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
    grid: bool = False            # force square-grid sibling arrangement (layered only)


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

    ``id`` and ``type`` are required (validated in __post_init__). ``label``
    may legitimately be empty -- an icon-only shape (a BPMN start/end event,
    a gateway) is deliberately unlabeled in draw.io itself (``value=""``);
    every consumer (size estimation, generator XML output) already treats a
    falsy label as "no text" rather than assuming one. All other fields are
    optional and default to their zero/empty values.
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

    # Hierarchy. ``parent_id`` (child points at parent) is what every notation
    # parser populates today; ``contains`` (parent lists its children) is the
    # inverse form, read by both the container layout and the generator's
    # container detection. Set one or the other, not a conflicting mix.
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

    # Links to nodes on other pages, one per cross-page edge at this node;
    # set by the engine, rendered into the label by the generator.
    page_links: list[PageLink] = field(default_factory=list)

    # Extra passthrough for layout/generator round-trips
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Node.id is required")
        if not self.type:
            raise ValueError("Node.type is required")


@dataclass
class Edge:
    """A diagram edge — relationship, association, or flow.

    ``type`` is required (validated in __post_init__).
    ``id`` is optional for programmatic callers. Parsed connected edges use
    the deterministic ``<source>-<target>`` convention.
    """

    id: str = ""
    type: str = ""
    source_id: str = ""
    target_id: str = ""
    # Source line of the declaration, for errors from whole-document checks
    # (0 when the edge was not parsed from a .mdg file).
    line: int = 0

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
    # ``visible`` is the authored tri-state: ``None`` delegates visibility to
    # derived rules, while True/False explicitly overrides them. ``hidden``
    # remains the legacy programmatic hide flag; containment has its own
    # recomputed field so presentation state never becomes sticky.
    visible: bool | None = None
    hidden: bool = False
    hidden_by_containment: bool = False

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

    @property
    def effective_hidden(self) -> bool:
        if self.visible is not None:
            return not self.visible
        return self.hidden or self.hidden_by_containment


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

    ``node_styles`` carries a small, deliberately narrow allowlist of
    per-instance cosmetic style tokens (see ``generator.overlay``'s
    ``_PRESERVED_NODE_STYLE_KEYS``) read back the same way -- e.g. a user
    switching one node's text to left-aligned in the draw.io UI must survive
    a plain regenerate, not just its position.
    """
    nodes: dict[str, dict[str, float]] = field(default_factory=dict)
    node_styles: dict[str, dict[str, str]] = field(default_factory=dict)
    edges_by_id: dict[str, EdgeAnchorOverlay] = field(default_factory=dict)
    # Endpoint identity is not unique: parallel relationships are legal.
    # Preserve overlays in draw.io document order so each authored edge gets
    # its own anchors and route during regeneration.
    edges: dict[str, list[EdgeAnchorOverlay]] = field(default_factory=dict)


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
