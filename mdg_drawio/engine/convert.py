"""Conversion pipeline orchestration — the ``convert()`` entry point.

Detects notation, resolves layout config, injects overlays, runs layout, and
generates multipage XML. Pre-load and pre-write validation are delegated to the
sibling ``preload`` and ``validate`` modules.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from mdg_drawio.contracts import (
    C4_SCALER_EXTRA_LINE_COUNT,
    C4_SCALER_FRAGMENT_GAP,
    C4_SCALER_HORIZONTAL_PADDING,
    C4_SCALER_LINE_HEIGHT,
    C4_SCALER_MAX_WIDTH,
    C4_SCALER_PERSON_ASPECT_RATIO,
    C4_SCALER_RECTANGULAR_HEIGHT_SCALE,
    C4_SCALER_SUBTITLE_KEY,
    C4_SCALER_TITLE_LINE_HEIGHT,
    C4_SCALER_VERTICAL_PADDING,
    C4_SCALER_WIDTH_CUSHION,
    PALETTE_MODE,
    ROTATED_LABEL_PADDING,
    SMALL_BOX_SCALER_HORIZONTAL_PADDING,
    SMALL_BOX_SCALER_TITLE_FONT_SIZE,
    SMALL_BOX_SCALER_TITLE_LINE_HEIGHT,
    SMALL_BOX_SCALER_VERTICAL_PADDING,
)
from mdg_drawio.generator import (
    Document,
    Edge,
    GeometryOverlay,
    GeometryPoint,
    MultiPageDocument,
    Node,
    StyleProvider,
    create_style_provider,
    generate,
    read_overlay,
    to_string,
)
from mdg_drawio.layout import (
    BaseLayout,
    Config,
    ShapeScalingConfig,
    SizeResolver,
    absolute_node_boxes,
    build_parent_map,
    create_size_resolver,
    create_style_resolver,
    dispatch_layout,
    estimate_text_width,
    padding_dict,
    regrow_containers_to_fit_children,
    resolve_page_size,
    scale_node_sizes,
)
from mdg_drawio.notation import LIBRARIES, DslError, parse, split_pages

from .preload import preload_core
from .validate import validate_generated_xml

# ---------------------------------------------------------------------------
# Notation detection
# ---------------------------------------------------------------------------

_USE_RE = re.compile(r"^\s*use\s+(\w+)", re.MULTILINE)
_DEFAULT_NOTATION = "c4"


def _detect_notation(source: str) -> str:
    """Detect the primary notation from a ``use <name>`` statement.

    A page with no ``use`` line falls back to the default notation. A ``use``
    naming a library that does not exist is a loud error, not a silent
    fallback: it decides the page's layout config, shape scaling and rank
    exclusions, so quietly treating ``use bpnm2`` (a typo) as C4 renders the
    whole page with the wrong policy and no diagnostic. Same principle as
    :func:`_normalize_direction`.
    """
    m = _USE_RE.search(source)
    if m is None:
        return _DEFAULT_NOTATION
    notation = m.group(1)
    if notation not in LIBRARIES:
        raise ValueError(
            f"unknown notation `use {notation}`; "
            f"expected one of {sorted(LIBRARIES)}"
        )
    return notation


# ---------------------------------------------------------------------------
# Layout mode detection from frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*$(.*?)^---\s*$", re.MULTILINE | re.DOTALL
)
# Every real .mdg uses "mode:" for the layout algorithm (layered/process/
# sequence/palette) -- see GRAMMAR.md and every committed fixture.
_LAYOUT_MODE_RE = re.compile(r"^mode:\s*(\S+)", re.MULTILINE)
_DEFAULT_LAYOUT_MODE = "layered"
_VALID_DIRECTIONS = ("TB", "LR")


def _c4_shape_scaling() -> ShapeScalingConfig:
    """Text-driven sizing policy for the C4 notation.

    Node types are grouped so every shape in a category scales symmetrically:
    people keep a fixed aspect ratio, while boxed shapes (system/container/
    component) share width and a reduced height relative to their text.
    """
    shape_scale_groups: dict[str, str] = {
        "c4.Person": "c4.person",
        "c4.Person_Ext": "c4.person",
        "c4.System": "c4.system",
        "c4.System_Ext": "c4.system",
        "c4.Container": "c4.container",
        "c4.ContainerDb": "c4.container",
        "c4.ContainerMicroservice": "c4.container",
        "c4.ContainerQueue": "c4.container",
        "c4.ContainerWebBrowser": "c4.container",
        "c4.Component": "c4.component",
    }
    rectangular_groups = ("c4.system", "c4.container", "c4.component")
    return ShapeScalingConfig(
        enabled=True,
        type_groups=shape_scale_groups,
        aspect_ratio_groups={"c4.person": C4_SCALER_PERSON_ASPECT_RATIO},
        height_scale_groups={
            group_id: C4_SCALER_RECTANGULAR_HEIGHT_SCALE
            for group_id in rectangular_groups
        },
        leading_extra_text_keys=(C4_SCALER_SUBTITLE_KEY,),
        extra_text_keys=(),
        horizontal_padding=C4_SCALER_HORIZONTAL_PADDING,
        vertical_padding=C4_SCALER_VERTICAL_PADDING,
        line_height=C4_SCALER_LINE_HEIGHT,
        title_line_height=C4_SCALER_TITLE_LINE_HEIGHT,
        fragment_gap=C4_SCALER_FRAGMENT_GAP,
        extra_line_count=C4_SCALER_EXTRA_LINE_COUNT,
        width_cushion=C4_SCALER_WIDTH_CUSHION,
        max_width=C4_SCALER_MAX_WIDTH,
    )


# ERD vertex functions with no rows of their own (rows.allowed == []) --
# Table/RowKey/Row/EntityTable are deliberately excluded: those already size
# correctly from their row content via the childLayout=tableLayout/
# stackLayout container path (_stack_children), so scaling them again here
# would be redundant rather than wrong, but adds nothing.
_ERD_LEAF_SHAPE_TYPES = frozenset(
    {
        "erd.EntityRect",
        "erd.EntityRounded",
        "erd.WeakEntity",
        "erd.Attribute",
        "erd.KeyAttribute",
        "erd.WeakKeyAttribute",
        "erd.DerivedAttribute",
        "erd.MultivalueAttribute",
        "erd.AssociativeEntity",
        "erd.Relationship",
        "erd.IdentifyingRelationship",
        "erd.Cloud",
        "erd.Note",
    }
)


def _erd_shape_scaling() -> ShapeScalingConfig:
    """Text-driven sizing for ERD's row-less shapes.

    Every one of these has a small, fixed palette default (e.g. 100x40 for
    EntityRect/EntityRounded) sized for a short single word, and no
    mechanism grows it for a longer or multi-line label (e.g. "Employee\\n
    (source system: SuccessFactors)") -- it silently overflows the box.
    Padding/line-height are tuned down from C4's defaults: ERD entities are
    small, tight boxes with plain (non-bold, ~12px) labels, not C4's bold
    title-card style.
    """
    return ShapeScalingConfig(
        enabled=True,
        node_types=set(_ERD_LEAF_SHAPE_TYPES),
        horizontal_padding=SMALL_BOX_SCALER_HORIZONTAL_PADDING,
        vertical_padding=SMALL_BOX_SCALER_VERTICAL_PADDING,
        title_font_size=SMALL_BOX_SCALER_TITLE_FONT_SIZE,
        title_line_height=SMALL_BOX_SCALER_TITLE_LINE_HEIGHT,
    )


# Plain (non-bold, ~12px label) small-box leaf shapes in the general-purpose
# shape library. List/ListItem are deliberately excluded: List already has
# childLayout=stackLayout, so ListItem already sizes correctly via the same
# _stack_children path Table/RowKey use in ERD.
_GENERAL_LEAF_SHAPE_TYPES = frozenset(
    {
        "general.Rectangle",
        "general.RoundedRectangle",
        "general.Process",
        "general.Parallelogram",
        "general.Trapezoid",
        "general.Text",
    }
)


def _general_shape_scaling() -> ShapeScalingConfig:
    """Text-driven sizing for the general-purpose shape library's leaf boxes.

    These carry arbitrary user text more often than any other notation --
    Rectangle/RoundedRectangle/etc. are the generic building blocks reached
    for when no specialized notation fits -- yet had the smallest, most
    generic palette defaults (120x60) and no growth mechanism at all.
    """
    return ShapeScalingConfig(
        enabled=True,
        node_types=set(_GENERAL_LEAF_SHAPE_TYPES),
        horizontal_padding=SMALL_BOX_SCALER_HORIZONTAL_PADDING,
        vertical_padding=SMALL_BOX_SCALER_VERTICAL_PADDING,
        title_font_size=SMALL_BOX_SCALER_TITLE_FONT_SIZE,
        title_line_height=SMALL_BOX_SCALER_TITLE_LINE_HEIGHT,
    )


# UML 2.5's leaf shapes most likely to carry a real (not structural-marker)
# label. Ports, pseudostate nodes (InitialPseudoState, FinalState,
# ShallowHistory, Junction, ...) are deliberately excluded: those are small
# by design in the UML spec itself (unlabeled or a single glyph), not an
# oversight -- growing them to fit a hypothetical long label would misrender
# standard notation.
_UML25_LEAF_SHAPE_TYPES = frozenset(
    {
        "uml25.Comment",
        "uml25.Instance",
        "uml25.Property",
    }
)


def _uml25_shape_scaling() -> ShapeScalingConfig:
    """Text-driven sizing for UML 2.5's free-text-bearing leaf shapes."""
    return ShapeScalingConfig(
        enabled=True,
        node_types=set(_UML25_LEAF_SHAPE_TYPES),
        horizontal_padding=SMALL_BOX_SCALER_HORIZONTAL_PADDING,
        vertical_padding=SMALL_BOX_SCALER_VERTICAL_PADDING,
        title_font_size=SMALL_BOX_SCALER_TITLE_FONT_SIZE,
        title_line_height=SMALL_BOX_SCALER_TITLE_LINE_HEIGHT,
    )


# The legacy "uml" library's leaf shapes with rows.allowed == [] and a real
# label. Item/Title/Divider/Spacer/SelfCall/LollipopNotation are row-like or
# structural helpers for other composite shapes, not directly-placed leaf
# vertices, so they're excluded here.
_UML_LEAF_SHAPE_TYPES = frozenset(
    {
        "uml.Object",
        "uml.Interface",
        "uml.Module",
        "uml.Package",
    }
)


def _uml_shape_scaling() -> ShapeScalingConfig:
    """Text-driven sizing for the legacy UML library's leaf shapes."""
    return ShapeScalingConfig(
        enabled=True,
        node_types=set(_UML_LEAF_SHAPE_TYPES),
        horizontal_padding=SMALL_BOX_SCALER_HORIZONTAL_PADDING,
        vertical_padding=SMALL_BOX_SCALER_VERTICAL_PADDING,
        title_font_size=SMALL_BOX_SCALER_TITLE_FONT_SIZE,
        title_line_height=SMALL_BOX_SCALER_TITLE_LINE_HEIGHT,
    )


# BPMN2's free-text annotation. Every other small-boxed bpmn2 shape is either
# a choreography-specific band meant to nest inside a composite activity
# (Participant*, part=1) or a niche Conversation-family hexagon -- both
# conventionally carry short labels and have no confirmed overflow bug, so
# they're left alone rather than speculatively widened.
_BPMN2_LEAF_SHAPE_TYPES = frozenset({"bpmn2.TextAnnotation"})


def _bpmn2_shape_scaling() -> ShapeScalingConfig:
    """Text-driven sizing for BPMN2's free-text annotation shape."""
    return ShapeScalingConfig(
        enabled=True,
        node_types=set(_BPMN2_LEAF_SHAPE_TYPES),
        horizontal_padding=SMALL_BOX_SCALER_HORIZONTAL_PADDING,
        vertical_padding=SMALL_BOX_SCALER_VERTICAL_PADDING,
        title_font_size=SMALL_BOX_SCALER_TITLE_FONT_SIZE,
        title_line_height=SMALL_BOX_SCALER_TITLE_LINE_HEIGHT,
    )


# Notation → default shape-scaling factory. The engine is the only layer
# allowed to bridge notation and layout (see the cross-package import rule in
# the architecture tests), so notation-specific layout policy is wired here.
# Adding scaling support for a new notation is a single declarative entry —
# no new control flow.
#
# archimate3 is deliberately absent: every element's default (.v1) variant
# already has a comfortable 150x75 palette size. Only the opt-in compact
# icon variant (variant=2, ~60x35) is small, and that's the point of
# choosing it -- growing it back up would defeat the denser look the author
# asked for.
_SHAPE_SCALING_BY_NOTATION: dict[str, Callable[[], ShapeScalingConfig]] = {
    "c4": _c4_shape_scaling,
    "erd": _erd_shape_scaling,
    "general": _general_shape_scaling,
    "uml25": _uml25_shape_scaling,
    "uml": _uml_shape_scaling,
    "bpmn2": _bpmn2_shape_scaling,
}


def _apply_default_shape_scaling(notation: str, config: Config) -> Config:
    """Apply a notation's default scaler unless one is already configured."""
    if config.shape_scaling.enabled:
        return config
    factory = _SHAPE_SCALING_BY_NOTATION.get(notation)
    if factory is None:
        return config
    return replace(config, shape_scaling=factory())


# BPMN data artifacts: annotations linked by association, not sequence flow --
# excluded from ranking so ProcessLayout floats them above whichever task
# they're associated with (see mdg_drawio.layout.process) instead of slotting
# them into the ranked sequence. Matched by DSL function name (the part of
# Node.type after the library prefix), not registry lookup: this is a fixed,
# well-known BPMN vocabulary, not something a registry edit should silently
# change the meaning of here.
_BPMN2_DATA_ARTIFACT_FUNCTIONS = frozenset(
    {
        "DataObject",
        "DataObjectCollection",
        "DataInput",
        "DataInputCollection",
        "DataOutput",
        "DataOutputCollection",
        "DataStore",
    }
)


def _bpmn2_rank_exclude_ids(nodes: list[Node]) -> frozenset[str]:
    return frozenset(
        node.id
        for node in nodes
        if node.type.rsplit(".", 1)[-1] in _BPMN2_DATA_ARTIFACT_FUNCTIONS
    )


# Notation → rank-exclusion detector, same bridging pattern as shape scaling.
_RANK_EXCLUDE_BY_NOTATION: dict[str, Callable[[list[Node]], frozenset[str]]] = {
    "bpmn2": _bpmn2_rank_exclude_ids,
}


def _resolve_rank_exclude_ids(notation: str, nodes: list[Node]) -> frozenset[str]:
    factory = _RANK_EXCLUDE_BY_NOTATION.get(notation)
    if factory is None:
        return frozenset()
    return factory(nodes)


def _resolve_layout_config(notation: str, mode: str) -> Config:
    """Import the notation's ``layout.py`` and call ``get_layout_config(mode)``.

    Falls back to ``Config()`` defaults when the notation has no
    ``layout.py`` module, then layers on the notation's default shape scaling.
    """
    try:
        mod = __import__(
            f"mdg_drawio.notation.{notation}.layout", fromlist=["get_layout_config"]
        )
        config = mod.get_layout_config(mode)
    except (ImportError, AttributeError):
        config = Config()
    return _apply_default_shape_scaling(notation, config)


def _detect_layout_mode(source: str) -> str:
    """Extract the layout mode from YAML frontmatter."""
    m = _FRONTMATTER_RE.search(source)
    if not m:
        return _DEFAULT_LAYOUT_MODE
    lm = _LAYOUT_MODE_RE.search(m.group(1))
    if lm:
        return lm.group(1)
    return _DEFAULT_LAYOUT_MODE


def _normalize_direction(raw: str) -> str | None:
    """Validate a ``direction`` frontmatter value from the parsed Diagram.

    Returns the flow direction (``TB`` top-to-bottom, ``LR`` left-to-right) or
    ``None`` when unset (the notation's Config default applies). An unknown value
    is a loud error, not a silent fallback.
    """
    if not raw:
        return None
    direction = raw.strip().upper()
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(
            f"invalid `direction: {raw}` in frontmatter; "
            f"expected one of {list(_VALID_DIRECTIONS)}"
        )
    return direction


# ---------------------------------------------------------------------------
# Model conversion
# ---------------------------------------------------------------------------


def _deduplicate_nodes(page: Document) -> None:
    """Remove duplicate nodes by ID, keeping first occurrence."""
    seen: set[str] = set()
    deduped: list[Node] = []
    for n in page.nodes:
        if n.id in seen:
            continue
        seen.add(n.id)
        deduped.append(n)
    page.nodes = deduped


def _hide_implied_containment_edges(nodes: list[Node], edges: list[Edge]) -> None:
    """Hide any edge whose endpoints are a direct parent-child pair.

    Containment nesting already communicates the relationship visually; an
    explicitly authored edge between a container and its own direct child
    (any relation type, any notation) becomes a redundant line drawn through
    the nesting. Only adjacent pairs are hidden -- an edge between a
    grandparent and grandchild is a real cross-level relationship and stays
    visible. The edge itself stays in the document (and so in the source
    ``.mdg``); only its rendering is suppressed.
    """
    parent_by_id = build_parent_map(nodes)
    direct_pairs = {
        frozenset((child_id, parent_id))
        for child_id, parent_id in parent_by_id.items()
    }
    for edge in edges:
        edge.hidden_by_containment = (
            frozenset((edge.source_id, edge.target_id)) in direct_pairs
        )


# draw.io colour style keys read into the overlay (see generator/overlay.py's
# _PRESERVED_NODE_STYLE_KEYS) but excluded from a node whose library is in
# _COLOR_SEMANTIC_LIBRARIES below.
_COLOR_STYLE_KEYS = frozenset({"fillColor", "strokeColor", "fontColor"})

# Notations where colour routinely encodes a real, .mdg-driven distinction
# (a C4 Person vs Person_Ext, an ArchiMate layer convention, a UML
# stereotype) rather than a purely decorative choice -- a manual colour
# tweak there is deliberately NOT preserved across a plain regenerate, so it
# can never mask an intentional type/variant change made in the .mdg.
# Every other notation's colour (erd, general, bpmn2, ...) is treated as
# decorative and preserved the same way text alignment already is. Extend
# this set, never the exclusion logic itself, if another notation turns out
# to encode meaning in colour too.
_COLOR_SEMANTIC_LIBRARIES = frozenset({"archimate3", "c4", "uml", "uml25"})


def _node_library(node_type: str) -> str:
    return node_type.split(".", 1)[0]


def _inject_node_overlay(
    nodes: list[Node], overlay: GeometryOverlay | None
) -> None:
    """Apply existing node positions and preserved style tokens from overlay.

    Style tokens (e.g. text alignment, fill/stroke/font colour) are applied
    last, so a manual edit made directly in draw.io always wins over the
    palette/config default on the next plain regenerate -- the same "what's
    already there survives" contract geometry already gets. Colour keys are
    dropped first for a node in a _COLOR_SEMANTIC_LIBRARIES notation.
    """
    if not overlay:
        return
    for node in nodes:
        if node.id in overlay.nodes:
            geo = overlay.nodes[node.id]
            node.x = geo.get("x", node.x)
            node.y = geo.get("y", node.y)
            node.width = geo.get("width", node.width)
            node.height = geo.get("height", node.height)
        if node.id in overlay.node_styles:
            style = overlay.node_styles[node.id]
            if _node_library(node.type) in _COLOR_SEMANTIC_LIBRARIES:
                style = {
                    k: v for k, v in style.items() if k not in _COLOR_STYLE_KEYS
                }
            node.style_overrides.update(style)


def _resolve_node_sizes(
    nodes: list[Node], size_of: SizeResolver
) -> None:
    """Resolve sizes for nodes without existing geometry."""
    for node in nodes:
        if node.width > 0 and node.height > 0:
            continue
        variant_resolver = getattr(size_of, "resolve_variant", None)
        if callable(variant_resolver):
            w, h = variant_resolver(node.type, node.variant)
        else:
            w, h = size_of(node.type)
        node.width = w
        node.height = h


def _inject_edge_overlay(
    edges: list[Edge], overlay: GeometryOverlay | None
) -> None:
    """Apply existing edge anchors and waypoints from overlay.

    Parallel edges share endpoint identity, so their overlays are consumed in
    document order rather than overwriting one another in a dictionary.
    """
    if not overlay:
        return
    edge_occurrences: dict[str, int] = {}
    for edge in edges:
        edge_key = f"{edge.source_id}->{edge.target_id}"
        occurrence = edge_occurrences.get(edge_key, 0)
        edge_occurrences[edge_key] = occurrence + 1
        overlays = overlay.edges.get(edge_key, [])
        if occurrence < len(overlays):
            ea = overlays[occurrence]
            if ea.exit_x:
                edge.style_overrides["exitX"] = ea.exit_x
            if ea.exit_y:
                edge.style_overrides["exitY"] = ea.exit_y
            if ea.entry_x:
                edge.style_overrides["entryX"] = ea.entry_x
            if ea.entry_y:
                edge.style_overrides["entryY"] = ea.entry_y
            if ea.waypoints:
                edge.waypoints = [
                    GeometryPoint(x=wp[0], y=wp[1]) for wp in ea.waypoints
                ]


def _content_size_for_page(
    nodes: list[Node],
    edges: list[Edge],
) -> tuple[float, float]:
    """Return final absolute content width/height after layout mutations."""
    xs: list[float] = []
    ys: list[float] = []
    rights: list[float] = []
    bottoms: list[float] = []

    for x, y, width, height in absolute_node_boxes(nodes).values():
        xs.append(x)
        ys.append(y)
        rights.append(x + width)
        bottoms.append(y + height)

    for edge in edges:
        for waypoint in edge.waypoints:
            xs.append(waypoint.x)
            ys.append(waypoint.y)
            rights.append(waypoint.x)
            bottoms.append(waypoint.y)

    if not xs:
        return 0.0, 0.0

    return max(rights) - min(xs), max(bottoms) - min(ys)


def _resolve_page_size_for_final_content(
    nodes: list[Node],
    edges: list[Edge],
    *,
    config: Config | None = None,
) -> tuple[int, int]:
    """Resolve page size from the final (post-layout) content bounds."""
    cfg = config or Config()
    content_width, content_height = _content_size_for_page(nodes, edges)
    return resolve_page_size(
        content_width=content_width,
        content_height=content_height,
        margin_x=cfg.margin_x,
        margin_y=cfg.margin_y,
        aspect_ratio=cfg.aspect_ratio,
    )


def _apply_layout_to_document(
    page: Document,
    size_of: SizeResolver,
    layout_mode: str,
    config: Config | None = None,
) -> Document:
    """Run layout on a page, mutating nodes and edges in-place."""
    overlay = page.geometry_overlay

    _deduplicate_nodes(page)
    if page.diagram.mode != PALETTE_MODE:
        _hide_implied_containment_edges(page.nodes, page.edges)
    _inject_node_overlay(page.nodes, overlay)
    _resolve_node_sizes(page.nodes, size_of)
    scale_node_sizes(page.nodes, config)

    layout_cls = dispatch_layout(layout_mode)
    if layout_cls is None:
        layout_cls = dispatch_layout(_DEFAULT_LAYOUT_MODE)
    if layout_cls is None:
        raise RuntimeError(f"layout mode {_DEFAULT_LAYOUT_MODE} not found")

    layout: BaseLayout = layout_cls()
    result = layout.apply(page.nodes, page.edges, size_of, config=config)
    # Nodes are mutated in place by every layout (so page.nodes already
    # reflects final positions), but routing (_route_edges and friends)
    # builds brand-new Edge objects rather than mutating existing ones --
    # without this, routed waypoints/anchors are silently discarded and
    # every edge falls back to draw.io's default floating connection.
    page.nodes = result.nodes
    page.edges = result.edges

    _inject_node_overlay(page.nodes, overlay)
    # The overlay above can move a container's children away from the
    # positions layout grew the container to fit -- re-fit every container
    # to its children's now-final geometry so a manually-repositioned child
    # doesn't overflow (or leave dead space in) a stale-sized parent.
    regrow_containers_to_fit_children(page.nodes, padding_dict(config or Config()))

    page_width, page_height = _resolve_page_size_for_final_content(
        result.nodes, result.edges,
        config=config,
    )
    page.diagram = replace(page.diagram, page_width=page_width, page_height=page_height)
    _inject_edge_overlay(page.edges, overlay)

    return page


# ---------------------------------------------------------------------------
# Multi-page generation
# ---------------------------------------------------------------------------

def _generate_multipage(
    pages: list[tuple[Document, str]],
    size_of: SizeResolver,
    styles: StyleProvider,
    overlays: list[GeometryOverlay] | None = None,
) -> str:
    """Generate a multi-diagram mxfile from parsed pages.

    Each page gets its own layout run and ``<diagram>`` inside a single
    ``<mxfile>``.
    """
    root = ET.Element("mxfile", {
        "host": "app.diagrams.net",
        "modified": "2024-01-01T00:00:00.000Z",
        "agent": "mdg-drawio",
        "type": "device",
    })

    ovs = overlays or []
    if overlays and len(overlays) != len(pages):
        if len(overlays) > len(pages):
            detail = "extra overlays ignored"
        else:
            detail = "new pages will be laid out fresh"
        print(
            f"mdg: warning: {len(overlays)} overlay(s) for {len(pages)} page(s) "
            f"— {detail}",
            file=sys.stderr,
        )
    for _i, (page_doc, page_source) in enumerate(pages):
        if _i < len(ovs):
            page_doc.geometry_overlay = ovs[_i]
        # Prefer the mode the parser already recorded on the page itself --
        # only fall back to re-deriving it from raw text when a notation's
        # parse_page() left it unset.
        layout_mode = page_doc.diagram.mode or _detect_layout_mode(page_source)
        notation = _detect_notation(page_source)
        layout_config = _resolve_layout_config(notation, layout_mode)
        direction = _normalize_direction(page_doc.diagram.direction)
        if direction is not None:
            layout_config = replace(layout_config, direction=direction)
        rank_exclude_ids = _resolve_rank_exclude_ids(notation, page_doc.nodes)
        layout_config = replace(
            layout_config, rank_exclude_ids=rank_exclude_ids
        )
        if page_doc.diagram.grid:
            layout_config = replace(layout_config, grid=True)
        page_doc = _apply_layout_to_document(
            page_doc, size_of, layout_mode, layout_config
        )
        page_xml = generate(page_doc, styles)
        page_elem = ET.fromstring(page_xml)

        # The generator wraps everything in <mxfile>; extract its children
        # and assign per-page IDs so draw.io doesn't reject duplicates.
        for child in list(page_elem):
            if child.tag == "diagram":
                child.set("id", f"diagram-{_i}")
                if not child.get("name"):
                    child.set("name", page_doc.diagram.name or f"Page-{_i + 1}")
            root.append(child)

    return to_string(root)


# ---------------------------------------------------------------------------
# Page normalisation
# ---------------------------------------------------------------------------

def _normalize_pages(
    doc: Document | MultiPageDocument,
    source: str,
) -> list[tuple[Document, str]]:
    """Flatten parsed document into ``(Document, page_source)`` pairs.

    Delegates page splitting to ``notation.split_pages`` -- the same
    function ``doc.pages`` was actually built from -- rather than
    re-deriving page boundaries with a separate implementation. A prior,
    parallel regex-based splitter here only recognized the legacy
    ``page "Name"`` (no colon) statement, not the ``---``/``page:`` YAML
    frontmatter block every real ``.mdg`` file uses, so for a genuine
    multi-page file it silently fell back to handing every page the
    *entire* file as its own source. Harmless when every page uses the
    same library (notation/layout-mode detection landed on the right
    answer by coincidence), but a page after the first using a different
    library got whichever notation/mode the first page's frontmatter
    declared.
    """
    if isinstance(doc, Document):
        return [(doc, source)]
    page_sources = [page_source for _name, page_source in split_pages(source)]
    result: list[tuple[Document, str]] = []
    for i, page_doc in enumerate(doc.pages):
        page_src = page_sources[i] if i < len(page_sources) else source
        result.append((page_doc, page_src))
    return result


# Public API
# ---------------------------------------------------------------------------


_START_SIZE_RE = re.compile(r"startSize=(\d+)")


_STACKING_CHILD_LAYOUTS = ("childLayout=stackLayout", "childLayout=tableLayout")


def _annotate_stack_containers(
    nodes: list[Node], style_of: Callable[[str], str]
) -> None:
    """Flag nodes whose palette shape stacks its children tightly.

    draw.io has two child-layout primitives that both mean "children are
    fixed-size rows/bands stacked with no gap, starting right after the
    title band": ``childLayout=stackLayout`` (BPMN pools, UML classifier
    members) and ``childLayout=tableLayout`` (ERD tables and their RowKey/Row
    entries, the BPMN cross-functional flowchart, the C4 legend, the UML
    activity partition). Without this, such a container's rows fall through
    to the generic ranked-graph layout, which inserts a full
    ``column_gap``/``row_gap`` between "siblings" that have no edges between
    them -- rows that should be flush end up floating apart with dead space,
    and their own border-line decorations (drawn flush against the row's own
    edges) visibly stop short of the next row instead of meeting it.

    Records ``extra['child_layout']='stack'`` and the title-band ``start_size``
    so the container layout stacks their children tightly (matching draw.io)
    instead of laying them out as free graph nodes. ``childLayout``/``startSize``
    are generic draw.io style primitives, so this stays notation-agnostic.
    """
    for node in nodes:
        style = style_of(node.type)
        if not any(marker in style for marker in _STACKING_CHILD_LAYOUTS):
            continue
        node.extra.setdefault("child_layout", "stack")
        if "start_size" not in node.extra:
            match = _START_SIZE_RE.search(style)
            node.extra["start_size"] = float(match.group(1)) if match else 0.0


def _annotate_rotated_label_sizing(
    nodes: list[Node], style_of: Callable[[str], str]
) -> None:
    """Ensure a swimlane/pool container is long enough to fit its OWN title
    on one line.

    draw.io's ``swimlane`` shape draws its title in a narrow ``startSize``
    band; when the container is too short along the title's reading axis,
    ``whiteSpace=wrap`` breaks it into sub-lines that don't fit the band's
    thickness and render as overlapping text. Setting a ``min_width``/
    ``min_height`` hint (consumed by ``_grow_parent_to_fit_children``) keeps
    the container at least as long as its own label needs, independent of
    how much room its children require. ``horizontal=0`` rotates the title
    onto the left edge (reading along the container's height); otherwise it
    reads normally along the width. Notation-agnostic: any library's
    swimlane-shaped container benefits identically.
    """
    for node in nodes:
        if not node.label:
            continue
        style = style_of(node.type)
        if "swimlane" not in style:
            continue
        required = estimate_text_width(node.label) + ROTATED_LABEL_PADDING
        axis = "min_height" if "horizontal=0" in style else "min_width"
        node.extra.setdefault(axis, required)


def _annotate_type_padding(nodes: list[Node], styles: StyleProvider) -> None:
    """Inject per-type extra inner padding from the override config.

    A shape whose type declares a ``padding`` block (see generator
    ``style_overrides.yaml``) gets that clearance added to its children's inset,
    e.g. so inner shapes clear a top header. Notation-agnostic.
    """
    for node in nodes:
        for side, value in styles.type_padding(node.type).items():
            if value is None:
                continue
            node.extra.setdefault(f"padding_extra_{side}", float(value))


def _read_overlays(output_path: Path, force: bool) -> list[GeometryOverlay]:
    """Read geometry overlays from an existing output, tolerating a bad file.

    A corrupt or non-drawio file at the output path degrades to "no overlay"
    with a warning rather than aborting the whole conversion.
    """
    if force or not output_path.exists():
        return []
    try:
        return read_overlay(str(output_path))
    except (ValueError, OSError) as exc:
        print(
            f"mdg: warning: could not read existing overlay from "
            f"{output_path}: {exc} — regenerating without it",
            file=sys.stderr,
        )
        return []


def convert(input_path: Path, output_path: Path, force: bool) -> int:
    """Convert an MDG file to a draw.io diagram.

    Without --force: preserves existing node positions and edge anchors.
    With --force: full regeneration, ignoring any existing output.
    """
    registries, styles = preload_core()

    if not input_path.exists():
        print(f"mdg: error: input file not found: {input_path}", file=sys.stderr)
        return 1

    overlays = _read_overlays(output_path, force)

    # utf-8-sig transparently strips a leading BOM so the anchored frontmatter
    # regex still matches for files saved by BOM-emitting editors.
    source = input_path.read_text(encoding="utf-8-sig")

    try:
        doc = parse(source)
    except (DslError, ValueError) as exc:
        print(f"mdg: error: parse failed: {exc}", file=sys.stderr)
        return 1

    pages = _normalize_pages(doc, source)
    if not pages:
        print("mdg: error: no pages found in document", file=sys.stderr)
        return 1

    size_of = create_size_resolver(registries=registries, styles=styles)
    style_of = create_style_resolver(styles=styles, registries=registries)
    style_provider = create_style_provider(registries, styles)
    for page_doc, _page_src in pages:
        _annotate_stack_containers(page_doc.nodes, style_of)
        _annotate_rotated_label_sizing(page_doc.nodes, style_of)
        # Palette/golden pages render verbatim — no type-level padding overrides.
        if page_doc.diagram.mode != PALETTE_MODE:
            _annotate_type_padding(page_doc.nodes, style_provider)
    xml_output = _generate_multipage(pages, size_of, style_provider, overlays)

    # Pre-write validation guardrail
    validation_errors = validate_generated_xml(xml_output)
    if validation_errors:
        for err in validation_errors:
            print(f"mdg: validation error: {err}", file=sys.stderr)
        return 1

    output_path.write_text(xml_output, encoding="utf-8")
    print(f"mdg: wrote {output_path} ({len(pages)} page(s))", file=sys.stderr)
    return 0
