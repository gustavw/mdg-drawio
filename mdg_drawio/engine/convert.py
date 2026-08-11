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
    create_size_resolver,
    estimate_text_width,
    create_style_resolver,
    dispatch_layout,
    resolve_page_size,
    scale_node_sizes,
)
from mdg_drawio.notation import LIBRARIES, DslError, parse

from .preload import preload_core
from .validate import validate_generated_xml

# ---------------------------------------------------------------------------
# Notation detection
# ---------------------------------------------------------------------------

_USE_RE = re.compile(r"^\s*use\s+(\w+)", re.MULTILINE)


def _detect_notation(source: str) -> str:
    """Detect the primary notation from a ``use <name>`` statement."""
    m = _USE_RE.search(source)
    if m and m.group(1) in LIBRARIES:
        return m.group(1)
    return "c4"


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


# Notation → default shape-scaling factory. The engine is the only layer
# allowed to bridge notation and layout (see the cross-package import rule in
# the architecture tests), so notation-specific layout policy is wired here.
# Adding scaling support for a new notation is a single declarative entry —
# no new control flow.
_SHAPE_SCALING_BY_NOTATION: dict[str, Callable[[], ShapeScalingConfig]] = {
    "c4": _c4_shape_scaling,
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


def _inject_node_overlay(
    nodes: list[Node], overlay: GeometryOverlay | None
) -> None:
    """Apply existing node positions from overlay."""
    if not overlay:
        return
    for node in nodes:
        if node.id in overlay.nodes:
            geo = overlay.nodes[node.id]
            node.x = geo.get("x", node.x)
            node.y = geo.get("y", node.y)
            node.width = geo.get("width", node.width)
            node.height = geo.get("height", node.height)


def _resolve_node_sizes(
    nodes: list[Node], size_of: SizeResolver
) -> None:
    """Resolve sizes for nodes without existing geometry."""
    for node in nodes:
        if node.width > 0 and node.height > 0:
            continue
        w, h = size_of(node.type)
        node.width = w
        node.height = h


def _inject_edge_overlay(
    edges: list[Edge], overlay: GeometryOverlay | None
) -> None:
    """Apply existing edge anchors and waypoints from overlay.

    The overlay key is ``"{source}->{target}"`` — the same identity the C4
    parser assigns as the edge id. Parallel edges between the same pair of nodes
    therefore share a key and cannot be told apart on a round-trip; the pre-write
    duplicate-id validation (``engine/validate.py``) rejects such documents up
    front, so this path never sees genuinely-ambiguous parallel edges.
    """
    if not overlay:
        return
    for edge in edges:
        edge_key = f"{edge.source_id}->{edge.target_id}"
        if edge_key in overlay.edges:
            ea = overlay.edges[edge_key]
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
        layout_mode = _detect_layout_mode(page_source)
        notation = _detect_notation(page_source)
        layout_config = _resolve_layout_config(notation, layout_mode)
        direction = _normalize_direction(page_doc.diagram.direction)
        if direction is not None:
            layout_config = replace(layout_config, direction=direction)
        rank_exclude_ids = _resolve_rank_exclude_ids(notation, page_doc.nodes)
        layout_config = replace(
            layout_config, rank_exclude_ids=rank_exclude_ids
        )
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
    """Flatten parsed document into ``(Document, page_source)`` pairs."""
    if isinstance(doc, Document):
        return [(doc, source)]
    page_sources = _split_page_sources(source)
    result: list[tuple[Document, str]] = []
    for i, page_doc in enumerate(doc.pages):
        page_src = page_sources[i] if i < len(page_sources) else source
        result.append((page_doc, page_src))
    return result


def _split_page_sources(source: str) -> list[str]:
    """Split raw source on ``page "Name"`` boundaries."""
    raw_lines = source.lstrip("\n").split("\n")
    global_header_lines: list[str] = []
    page_chunks: list[list[str]] = []
    current_chunk: list[str] | None = None

    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith('page "') or stripped.startswith("page '"):
            current_chunk = [line]
            page_chunks.append(current_chunk)
        elif current_chunk is not None:
            current_chunk.append(line)
        else:
            global_header_lines.append(line)

    global_header = "\n".join(global_header_lines)
    if global_header:
        global_header += "\n"

    if not page_chunks:
        return [source]

    return [global_header + "\n".join(chunk) for chunk in page_chunks]


# Public API
# ---------------------------------------------------------------------------


_START_SIZE_RE = re.compile(r"startSize=(\d+)")


def _annotate_stack_containers(
    nodes: list[Node], style_of: Callable[[str], str]
) -> None:
    """Flag nodes whose palette shape is a ``childLayout=stackLayout`` container.

    Records ``extra['child_layout']='stack'`` and the title-band ``start_size``
    so the container layout stacks their children tightly (matching draw.io)
    instead of laying them out as free graph nodes. ``childLayout``/``startSize``
    are generic draw.io style primitives, so this stays notation-agnostic.
    """
    for node in nodes:
        style = style_of(node.type)
        if "childLayout=stackLayout" not in style:
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
