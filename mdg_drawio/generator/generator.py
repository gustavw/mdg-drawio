"""Draw.io XML generation from the pipeline data models.

Produces valid ``.drawio`` (mxfile) XML from a ``Document`` instance.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import yaml

from mdg_drawio.contracts import (
    CANVAS_DX,
    CANVAS_DY,
    DEFAULT_NODE_HEIGHT,
    DEFAULT_NODE_WIDTH,
    PAGE_CELL_ID,
    PALETTE_MODE,
    ROOT_CELL_ID,
    Anchor,
    ChildCell,
    Document,
    Edge,
    GeometryChild,
    Node,
    NodeChildCell,
    index_shapes_by_function,
)

from .xml_utils import to_string

DEFAULT_VERTEX_STYLE = "whiteSpace=wrap;html=1;"


DEFAULT_EDGE_STYLE = "rounded=0;orthogonalLoop=1;jettySize=auto;html=1;"

_StyleValue = str | int | float | None


# Per-type rendering overrides, keyed by node type. Each entry may carry a
# ``style`` block (draw.io tokens) and a ``padding`` block (extra inner insets
# consumed by the layout). Loaded once from the committed config file and passed
# into a ``StyleProvider`` — the generator holds no module-global state.
_STYLE_OVERRIDES_PATH = Path(__file__).parent / "style_overrides.yaml"
_TypeOverrides = dict[str, dict[str, dict[str, _StyleValue]]]

_OVERRIDE_SECTIONS = ("style", "padding")
_PADDING_SIDES = ("top", "right", "bottom", "left")


def _validate_overrides(raw: Any) -> None:
    """Validate the parsed override config, raising ValueError on any problem.

    The config is developer-authored and committed, so a typo (an unknown
    section, a misspelled padding side, a non-numeric inset) should fail loudly
    at load time rather than silently no-op during generation.
    """
    name = _STYLE_OVERRIDES_PATH.name
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: top level must be a mapping")
    unknown_top = set(raw) - {"overrides"}
    if unknown_top:
        raise ValueError(f"{name}: unknown top-level key(s): {sorted(unknown_top)}")
    overrides = raw.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError(f"{name}: 'overrides' must be a mapping")

    for node_type, entry in overrides.items():
        where = f"{name}: overrides.{node_type}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping of sections")
        unknown = set(entry) - set(_OVERRIDE_SECTIONS)
        if unknown:
            raise ValueError(
                f"{where}: unknown section(s) {sorted(unknown)}; "
                f"allowed: {list(_OVERRIDE_SECTIONS)}"
            )
        style = entry.get("style")
        if style is not None and not isinstance(style, dict):
            raise ValueError(f"{where}.style must be a mapping")
        _validate_padding(entry.get("padding"), f"{where}.padding")


def _validate_padding(padding: Any, where: str) -> None:
    if padding is None:
        return
    if not isinstance(padding, dict):
        raise ValueError(f"{where} must be a mapping")
    bad_sides = set(padding) - set(_PADDING_SIDES)
    if bad_sides:
        raise ValueError(
            f"{where}: unknown side(s) {sorted(bad_sides)}; "
            f"allowed: {list(_PADDING_SIDES)}"
        )
    for side, value in padding.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}.{side} must be a number, got {value!r}")


@cache
def load_style_overrides() -> _TypeOverrides:
    """Read + validate the committed override config (used by the factory; cached)."""
    if not _STYLE_OVERRIDES_PATH.exists():
        return {}
    raw = yaml.safe_load(_STYLE_OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    _validate_overrides(raw)
    overrides = raw.get("overrides") or {}
    return {
        str(node_type): {
            section: dict(attrs) for section, attrs in entry.items()
        }
        for node_type, entry in overrides.items()
    }


def _strip_style_tokens(style: str, tokens: set[str]) -> str:
    """Remove style tokens matching *tokens* from the style string."""
    parts = style.split(";")
    kept = [p for p in parts if p.split("=", 1)[0] not in tokens]
    return ";".join(kept)


def _apply_corrections(style: str, corrections: dict[str, _StyleValue]) -> str:
    """Merge type-level style overrides onto *style*.

    Any attribute in *corrections* replaces its palette value (a ``null`` value
    deletes it). Overridden tokens are stripped first so the result stays clean
    rather than relying on draw.io's last-wins parsing.
    """
    if not corrections:
        return style
    stripped = _strip_style_tokens(style, set(corrections)).rstrip(";")
    tokens = [f"{k}={v}" for k, v in corrections.items() if v is not None]
    parts = [p for p in (stripped, *tokens) if p]
    return ";".join(parts) + ";" if parts else ""


def _has_alignment_tokens(style: str) -> bool:
    """Whether *style* already sets its own ``align``/``verticalAlign``.

    Checked against the fully-resolved style so a shape whose own palette
    style already bakes in label positioning (e.g. ``c4.System_Boundary``'s
    ``align=left;verticalAlign=bottom;``) is never double-styled.
    """
    tokens = {part.split("=", 1)[0] for part in style.split(";") if part}
    return "align" in tokens or "verticalAlign" in tokens


def _apply_container_label_position(style: str) -> str:
    """Pin a genuine container's label top-left, matching ArchiMate/draw.io's
    own nesting convention (a container's title sits top-left of its box)."""
    if _has_alignment_tokens(style):
        return style
    return (
        style.rstrip(";")
        + ";align=left;verticalAlign=top;spacingLeft=4;spacingTop=4;"
    )


def _split_type(node_type: str) -> tuple[str, str]:
    """Split a namespaced type like ``c4.person`` into ``(library, function)``."""
    parts = node_type.split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


class StyleProvider(Protocol):
    """Port: resolves draw.io styles, label templates, and per-type overrides.

    Injected into ``generate`` (the composition root builds it), mirroring how
    ``SizeResolver`` is injected into layout — the generator holds no global
    state and never touches the filesystem itself.
    """

    def resolve_style(self, node_type: str, variant: int = 1) -> str: ...
    def resolve_edge_style(self, edge_type: str, variant: int = 1) -> str: ...
    def label_template(self, node_type: str, variant: int = 1) -> tuple[str, bool]: ...
    def style_corrections(self, node_type: str) -> dict[str, _StyleValue]: ...
    def type_padding(self, node_type: str) -> dict[str, _StyleValue]: ...
    def row_type_entry(self, type_str: str) -> dict | None: ...
    def edge_label_templates(self, edge_type: str, variant: int = 1) -> list[dict]: ...


@dataclass(frozen=True)
class PaletteStyleProvider:
    """Palette-backed ``StyleProvider`` built from preloaded registry/style data."""

    registries: dict[str, dict]  # {library: {function: [shape_entry, ...]}}
    styles: dict[str, dict]      # {library: {shape_id: entry}}
    overrides: _TypeOverrides    # {node_type: {"style"/"padding": {...}}}

    def resolve_style(self, node_type: str, variant: int = 1) -> str:
        return self._registry_style(node_type, variant) or DEFAULT_VERTEX_STYLE

    def resolve_edge_style(self, edge_type: str, variant: int = 1) -> str:
        return self._registry_style(edge_type, variant) or DEFAULT_EDGE_STYLE

    def _registry_style(self, type_str: str, variant: int = 1) -> str | None:
        """Style string from the palette, gated by a known registry function.

        A function family's variants can have genuinely different geometry
        (e.g. bpmn2 Pool v1 vs v2 orientation) -- resolving without the
        requested variant would silently render every variant as v1.

        Row types (e.g. uml25's Item/Header/Divider) have no independent
        registry function of their own -- they only exist nested inside a
        composite shape -- so they fall back to the row-type sidecar instead.
        """
        library, function = _split_type(type_str)
        if not library:
            return None
        if self.registries.get(library, {}).get(function):
            entry = self._palette_entry(library, function, variant)
            return (str(entry.get("style", "")) if entry else "") or None
        row_entry = self.row_type_entry(type_str)
        return (str(row_entry.get("style", "")) if row_entry else "") or None

    def row_type_entry(self, type_str: str) -> dict | None:
        """Palette-derived style/geometry for a row type (e.g. uml25.Item).

        Distinct from ``_palette_entry``: row types have no shape id and no
        variant, only a single canonical entry per (library, function).
        """
        library, function = _split_type(type_str)
        if not library:
            return None
        row_types = self.styles.get(library, {}).get("_row_types")
        if not isinstance(row_types, dict):
            return None
        entry = row_types.get(function)
        return entry if isinstance(entry, dict) else None

    def _palette_entry(
        self, library: str, function: str, variant: int = 1
    ) -> dict | None:
        """Palette sidecar entry for a shape, preferring the exact variant."""
        styles_for_lib = self.styles.get(library, {})
        exact = styles_for_lib.get(f"{library}.{function.lower()}.v{variant}")
        if exact is not None:
            return exact
        for shape_id, entry in styles_for_lib.items():
            if shape_id.startswith(f"{library}.{function.lower()}."):
                return entry
        return None

    def label_template(self, node_type: str, variant: int = 1) -> tuple[str, bool]:
        """Inherit a shape's label from its palette entry.

        Returns ``(template, uses_placeholders)``. Placeholder-based anchor cells
        (draw.io ``placeholders=1``) carry the exact ``%attr%`` template, emitted
        verbatim so rendering matches the shape library 1:1. Generic — no
        library-specific knowledge here.
        """
        library, function = _split_type(node_type)
        if not library:
            return "", False
        entry = self._palette_entry(library, function, variant)
        if not entry:
            return "", False
        cells = entry.get("cells") or []
        if not cells:
            return "", False
        attrs = cells[0].get("object_attrs") or {}
        if attrs.get("placeholders") != "1":
            return "", False
        return str(cells[0].get("value", "")), True

    def edge_label_templates(self, edge_type: str, variant: int = 1) -> list[dict]:
        """Endpoint label-cell templates (source/target cardinality) from the palette.

        The palette's first captured cell is the edge line itself; any cells
        after it are label markers anchored near one endpoint via geometry
        ``x`` (negative = source side, positive = target side). Only their
        position/style is reused here -- the label text comes from the
        authored call's ``source_label``/``target_label``.
        """
        library, function = _split_type(edge_type)
        if not library:
            return []
        entry = self._palette_entry(library, function, variant)
        cells = (entry or {}).get("cells") or []
        return [cell for cell in cells[1:] if isinstance(cell, dict)]

    def style_corrections(self, node_type: str) -> dict[str, _StyleValue]:
        return self.overrides.get(node_type, {}).get("style", {})

    def type_padding(self, node_type: str) -> dict[str, _StyleValue]:
        return self.overrides.get(node_type, {}).get("padding", {})


def create_style_provider(
    registries: dict[str, dict],
    styles: dict[str, dict],
    overrides: _TypeOverrides | None = None,
) -> PaletteStyleProvider:
    """Build a ``StyleProvider`` from preloaded data (composition-root factory).

    *registries* is the raw ``{library: parsed_yaml_doc}`` (indexed by function
    here). *overrides* defaults to the committed config.
    """
    return PaletteStyleProvider(
        registries={
            lib: index_shapes_by_function(reg.get("shapes", []))
            for lib, reg in registries.items()
        },
        styles=dict(styles),
        overrides=dict(overrides) if overrides is not None else load_style_overrides(),
    )


@dataclass
class _GenCtx:
    """Mutable generation context — the injected StyleProvider + per-run state."""

    styles: StyleProvider
    child_seq: dict[str, int] = field(default_factory=dict)
    container_ids: set[str] = field(default_factory=set)
    # Type-level style overrides are skipped for palette/golden output so it
    # stays true to the raw palette (see ``PALETTE_MODE``).
    apply_overrides: bool = True

    def next_child_id(self, parent_id: str) -> str:
        """Generate a stable child cell ID: ``parent__cN``."""
        seq = self.child_seq.get(parent_id, 0)
        self.child_seq[parent_id] = seq + 1
        return f"{parent_id}__c{seq}"


def _node_geometry(node: Node) -> tuple[float, float, float, float]:
    """Resolve geometry for a node.

    Nodes must have positions set by the layout engine or geometry overlay
    before generation. The 0,0 default is a safety net for foreign-namespace
    passthrough nodes (a known gap; see skill://reviewer "Project gotchas").
    """
    w = node.width or DEFAULT_NODE_WIDTH
    h = node.height or DEFAULT_NODE_HEIGHT
    return node.x or 0.0, node.y or 0.0, w, h

def _style_tokens(overrides: dict[str, _StyleValue]) -> list[str]:
    """Render a style-override mapping as draw.io style tokens.

    A ``None`` value means a bare flag token (``rounded``) rather than a
    ``key=value`` pair. The single source for this rendering — node styles,
    edge styles and child-cell styles all went through their own copy of this
    loop before, which is how they drifted apart on the empty-input case.
    """
    return [
        key if value is None else f"{key}={value}"
        for key, value in overrides.items()
    ]


def _tokens_to_style(tokens: list[str]) -> str:
    """Join style tokens into a trailing-semicolon style string ("" if empty)."""
    return ";".join(tokens) + ";" if tokens else ""


def _build_node_cell_attrs(
    node: Node,
    node_id: str,
    parent_id: str,
    base_style: str,
    *,
    label: str | None = None,
) -> dict[str, str]:
    """Build mxCell attributes for a node.

    ``label`` overrides ``node.label`` when given -- used by compound rows
    (e.g. erd RowKey), whose text lives in a child cell, not the row itself.
    """
    full_style = base_style
    overrides = _tokens_to_style(_style_tokens(node.style_overrides))
    if overrides:
        full_style = full_style.rstrip(";") + ";" + overrides
    if isinstance(node.extra.get("dashed"), bool):
        full_style = _apply_corrections(
            full_style, {"dashed": 1 if node.extra["dashed"] else None}
        )

    attrs: dict[str, str] = {
        "id": node_id,
        "value": node.label if label is None else label,
        "style": full_style,
        # "vertex" is a boolean mxCell flag ("1" = this cell is a vertex); it is
        # unrelated to PAGE_CELL_ID, which only happens to share the value "1".
        "vertex": "1",
        "parent": parent_id,
    }

    for key, value in node.object_attributes.items():
        if value is not None:
            attrs[key] = str(value)

    return attrs


def _build_node_geometry(
    cell: ET.Element, x: float, y: float, w: float, h: float, node: Node
) -> None:
    """Append mxGeometry to a node's mxCell."""
    geo_attrs: dict[str, str] = {
        "x": str(x),
        "y": str(y),
        "width": str(w),
        "height": str(h),
        "as": "geometry",
    }
    for key, value in node.geometry_attributes.items():
        if value is not None:
            geo_attrs[key] = str(value)
    geometry = ET.SubElement(cell, "mxGeometry", geo_attrs)

    for gc in node.geometry_children:
        tag = gc.tag or "mxPoint"
        child_attrs = {
            k: str(v) for k, v in gc.attributes.items() if v is not None
        }
        ET.SubElement(geometry, tag, child_attrs)


def _style_string_to_overrides(style: str) -> dict[str, str | int | float | None]:
    """Parse a raw draw.io style string into a style_overrides-shaped dict."""
    overrides: dict[str, str | int | float | None] = {}
    for token in style.split(";"):
        token = token.strip()
        if not token:
            continue
        key, sep, value = token.partition("=")
        overrides[key] = value if sep else None
    return overrides


def _child_geometry(
    raw: object,
) -> tuple[dict[str, Any], list[GeometryChild]]:
    """Split palette geometry attrs from nested geometry elements.

    Palette extraction represents ``<mxRectangle as="alternateBounds">`` as
    an ``alternate_bounds`` mapping. It must be reconstructed as a child XML
    element, not stringified into an invalid mxGeometry attribute.
    """
    geometry = dict(raw) if isinstance(raw, dict) else {}
    alternate = geometry.pop("alternate_bounds", None)
    children: list[GeometryChild] = []
    if isinstance(alternate, dict):
        children.append(
            GeometryChild(
                tag="mxRectangle",
                attributes={**alternate, "as": "alternateBounds"},
            )
        )
    return geometry, children


def _compound_row_override(
    node: Node, styles: StyleProvider
) -> tuple[str, list[NodeChildCell]] | None:
    """Style + compound sub-cells for a nested compound row (e.g. erd RowKey).

    erd's Row and RowKey are real draw.io table rows made of two sub-cells (a
    key tag, a text label). RowKey also has its own independent top-level
    palette entry -- a standalone "Table Row" shape wrapped in its own mini
    ``shape=table`` container -- which is the *wrong* style once nested
    inside a real Table (a table-within-a-row). Row has no such top-level
    entry at all. Both resolve correctly here from the row-type sidecar,
    which was extracted directly from the nested (nothing-else-wrapped) form.

    A standalone ``erd.RowKey`` keeps its registered outer ``shape=table``
    style, then receives the keyed tableRow template as a recursive child.
    Nested Row/RowKey nodes already *are* tableRows, so their two leaf cells
    attach directly to the authored node.
    """
    entry = styles.row_type_entry(node.type)
    cells = (entry or {}).get("cells") if entry else None
    if not cells or len(cells) < 2:
        return None
    key_cell, text_cell = cells[0], cells[1]
    key_geometry, key_geometry_children = _child_geometry(key_cell.get("geometry"))
    text_geometry, text_geometry_children = _child_geometry(
        text_cell.get("geometry")
    )
    leaf_cells = [
        NodeChildCell(
            label=str(node.extra.get("key", "")),
            geometry_attributes=key_geometry,
            geometry_children=key_geometry_children,
            style_overrides=_style_string_to_overrides(key_cell.get("style", "")),
        ),
        NodeChildCell(
            label=node.label,
            geometry_attributes=text_geometry,
            geometry_children=text_geometry_children,
            style_overrides=_style_string_to_overrides(text_cell.get("style", "")),
        ),
    ]
    row_style = str((entry or {}).get("style", ""))
    standalone_style = styles.resolve_style(node.type, node.variant)
    if node.parent_id or standalone_style == row_style:
        return row_style, leaf_cells

    # A row type may have a separately registered standalone wrapper (RowKey
    # is shape=table at top level, but shape=tableRow when nested). Preserve
    # that outer style while rebuilding the inner hierarchy with authored
    # values instead of the palette's example labels.
    row_geometry = {
        key: value
        for key in ("width", "height")
        if (value := (entry or {}).get(key)) is not None
    }
    row_cell = NodeChildCell(
        style_overrides=_style_string_to_overrides(row_style),
        geometry_attributes=row_geometry,
        child_cells=leaf_cells,
    )
    return standalone_style, [row_cell]


def _append_node(mx_root: ET.Element, node: Node, ctx: _GenCtx) -> None:
    """Append a single node to the mxGraphModel root."""
    node_id = node.id
    parent_id = node.parent_id or PAGE_CELL_ID

    compound = _compound_row_override(node, ctx.styles)
    if compound is not None:
        base_style, child_cells = compound
        outer_label: str | None = ""
    else:
        base_style = ctx.styles.resolve_style(node.type, node.variant)
        child_cells = node.child_cells
        outer_label = None
    if ctx.apply_overrides:
        base_style = _apply_corrections(
            base_style, ctx.styles.style_corrections(node.type)
        )
    cell_attrs = _build_node_cell_attrs(
        node, node_id, parent_id, base_style, label=outer_label
    )
    if ctx.apply_overrides and node_id in ctx.container_ids:
        cell_attrs["style"] = _apply_container_label_position(cell_attrs["style"])

    wrapper = _maybe_wrap_object(mx_root, node, cell_attrs, ctx.styles)
    cell_parent: ET.Element = wrapper if wrapper is not None else mx_root

    if wrapper is not None:
        cell_attrs.pop("id", None)
        cell_attrs.pop("value", None)
        cell_attrs["parent"] = parent_id

    cell = ET.SubElement(cell_parent, "mxCell", cell_attrs)

    x, y, w, h = _node_geometry(node)
    _build_node_geometry(cell, x, y, w, h, node)

    for child_cell in child_cells:
        _append_node_child(mx_root, node_id, child_cell, ctx)


def _wrap_object(
    mx_root: ET.Element,
    type_str: str,
    object_attributes: dict,
    cell_id: str,
    fallback_label: str,
    styles: StyleProvider,
    variant: int = 1,
) -> ET.Element | None:
    """Wrap a cell (node or edge) in a <UserObject> if it has identity attrs.

    Inherits the palette's label 1:1: for placeholder-based shapes the palette
    template and ``placeholders=1`` flag are emitted verbatim and draw.io
    substitutes the attribute values the notation supplied — identical
    fonts/layout to the shape library. Otherwise the cell's own label is used.
    Generic — no library-specific knowledge lives here.
    """
    obj_attrs: dict[str, str] = {}
    for key, value in object_attributes.items():
        if value is not None:
            obj_attrs[key] = str(value)
    if not obj_attrs:
        return None
    obj_attrs["id"] = cell_id

    template, uses_placeholders = styles.label_template(type_str, variant)
    if uses_placeholders:
        obj_attrs["placeholders"] = "1"
        obj_attrs["label"] = template
        # Guarantee every %token% the template references resolves: default to
        # empty so neither a literal "%token%" nor a palette example value leaks.
        for token in re.findall(r"%(\w+)%", template):
            obj_attrs.setdefault(token, "")
    else:
        obj_attrs["label"] = fallback_label
    return ET.SubElement(mx_root, "UserObject", obj_attrs)


def _maybe_wrap_object(
    mx_root: ET.Element,
    node: Node,
    cell_attrs: dict[str, str],
    styles: StyleProvider,
) -> ET.Element | None:
    """Wrap a node in a <UserObject> element if it has identity attributes."""
    return _wrap_object(
        mx_root,
        node.type,
        node.object_attributes,
        cell_attrs.get("id", ""),
        cell_attrs.get("value", ""),
        styles,
        node.variant,
    )


def _non_null_attrs(attrs: dict[str, _StyleValue]) -> dict[str, str]:
    """Convert non-null style-like attributes to XML string attrs."""
    return {key: str(value) for key, value in attrs.items() if value is not None}


def _wrap_child_object(
    mx_root: ET.Element,
    child_id: str,
    label: str,
    object_attributes: dict[str, _StyleValue],
) -> ET.Element | None:
    """Wrap a child cell in a <UserObject> element when object attrs exist."""
    obj_attrs = _non_null_attrs(object_attributes)
    if not obj_attrs:
        return None
    obj_attrs["id"] = child_id
    if label and "label" not in obj_attrs:
        obj_attrs["label"] = label
    return ET.SubElement(mx_root, "UserObject", obj_attrs)


def _build_node_child_cell_attrs(
    child: NodeChildCell,
    child_id: str,
    parent_id: str,
    *,
    wrapped: bool,
) -> dict[str, str]:
    """Build mxCell attrs for a recursive node child."""
    cell_attrs: dict[str, str] = {
        "id": child_id,
        "value": child.label,
        "parent": parent_id,
        "vertex": "1",
    }
    if wrapped:
        cell_attrs.pop("id", None)
        cell_attrs.pop("value", None)
    cell_attrs.update(_non_null_attrs(child.cell_attributes))

    style = _overrides_to_style(child.style_overrides)
    if style:
        cell_attrs["style"] = style
    return cell_attrs


def _append_node_child_cells(
    children: list[NodeChildCell],
    mx_root: ET.Element,
    parent_id: str,
    ctx: _GenCtx,
) -> None:
    """Append recursive node child cells below a geometry-owning cell."""
    for nested_child in children:
        _append_node_child(mx_root, parent_id, nested_child, ctx)


def _append_node_child_geometry(
    child_cell: ET.Element,
    child: NodeChildCell,
) -> None:
    """Append mxGeometry and geometry children for a node child cell."""
    geo_attrs = _non_null_attrs(child.geometry_attributes)
    if "as" not in geo_attrs:
        geo_attrs["as"] = "geometry"
    geometry = ET.SubElement(child_cell, "mxGeometry", geo_attrs)

    for gc in child.geometry_children:
        tag = gc.tag or "mxPoint"
        ET.SubElement(geometry, tag, _non_null_attrs(gc.attributes))


def _append_node_child(
    mx_root: ET.Element,
    parent_id: str,
    child: NodeChildCell,
    ctx: _GenCtx,
) -> None:
    """Append a recursive child cell (e.g. UML class member rows)."""
    child_id = ctx.next_child_id(parent_id)
    wrapper = _wrap_child_object(
        mx_root, child_id, child.label, child.object_attributes
    )
    wrapper_parent = wrapper if wrapper is not None else mx_root
    cell_attrs = _build_node_child_cell_attrs(
        child, child_id, parent_id, wrapped=wrapper is not None
    )
    child_cell = ET.SubElement(wrapper_parent, "mxCell", cell_attrs)
    _append_node_child_geometry(child_cell, child)
    _append_node_child_cells(child.child_cells, mx_root, child_id, ctx)


def _build_edge_style(edge: Edge, ctx: _GenCtx, variant: int) -> str:
    """Assemble the full draw.io style string for an edge.

    Starts from the palette edge style, then applies container-routing rules
    and any per-edge style overrides. *variant* is resolved once by the caller
    so the style and the label template it pairs with cannot disagree.
    """
    base_style = ctx.styles.resolve_edge_style(edge.type, variant)
    # Strip orthogonal routing tokens — only allowed for container edges
    full_style = _strip_style_tokens(base_style, {"edgeStyle", "elbow"})
    if edge.source_id in ctx.container_ids or edge.target_id in ctx.container_ids:
        full_style = full_style.rstrip(";") + ";edgeStyle=orthogonalEdgeStyle;"

    overrides = _style_overrides_for_edge(edge)
    if overrides:
        full_style = full_style.rstrip(";") + ";" + overrides
    return full_style


def _coerce_variant(edge: Edge) -> int:
    """Read the edge ``variant`` as an int, falling back to 1 on bad input.

    ``edge.extra`` is untyped notation-supplied data, so a non-numeric variant
    must not abort the whole generation with a bare ``ValueError``.
    """
    raw = edge.extra.get("variant", 1)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _append_edge(mx_root: ET.Element, edge: Edge, ctx: _GenCtx) -> None:
    """Append a single edge to the mxGraphModel root."""
    edge_id = edge.id or f"e_{edge.source_id}->{edge.target_id}"
    source_id = edge.source_id
    target_id = edge.target_id

    variant = _coerce_variant(edge)
    full_style = _build_edge_style(edge, ctx, variant)

    edge_attrs: dict[str, str] = {
        "id": edge_id,
        "style": full_style,
        "edge": "1",
        "parent": PAGE_CELL_ID,
        "source": source_id,
        "target": target_id,
    }
    if edge.hidden:
        # draw.io hides a cell via the mxCell `visible` attribute, not a style
        # token -- a style-only "hidden=1" is inert and still renders.
        edge_attrs["visible"] = "0"

    if edge.label:
        edge_attrs["value"] = edge.label

    # Wrap in a <UserObject> and inherit the palette edge label template 1:1
    # (same mechanism as nodes), so a C4 Rel renders like its shape-library
    # entry instead of a bare label.
    wrapper = _wrap_object(
        mx_root, edge.type, edge.object_attributes, edge_id, edge.label,
        ctx.styles, variant,
    )
    edge_parent: ET.Element = wrapper if wrapper is not None else mx_root
    if wrapper is not None:
        edge_attrs.pop("id", None)
        edge_attrs.pop("value", None)
        edge_attrs["parent"] = PAGE_CELL_ID

    edge_cell = ET.SubElement(edge_parent, "mxCell", edge_attrs)

    _append_edge_geometry(edge_cell, edge)

    for child in edge.child_cells:
        _append_edge_child(mx_root, edge_id, child, ctx)
    for child in _edge_endpoint_label_cells(edge, ctx, variant):
        _append_edge_child(mx_root, edge_id, child, ctx)


def _is_terminal_point(as_value: str) -> bool:
    """Return True for draw.io source/target point markers."""
    return as_value in ("sourcePoint", "targetPoint")


def _append_waypoint_or_terminal(
    geometry: ET.Element,
    point_attrs: dict[str, str],
    waypoints: list[dict[str, str]],
) -> None:
    """Append terminal points directly, collecting regular waypoints."""
    if _is_terminal_point(point_attrs.get("as", "")):
        ET.SubElement(geometry, "mxPoint", point_attrs)
    else:
        waypoints.append(point_attrs)


def _collect_edge_waypoints(
    geometry: ET.Element,
    edge: Edge,
) -> list[dict[str, str]]:
    """Collect edge waypoints while appending source/target points."""
    waypoints: list[dict[str, str]] = []
    for point in edge.waypoints:
        point_attrs = {"x": str(point.x), "y": str(point.y)}
        if point.as_:
            point_attrs["as"] = point.as_
        _append_waypoint_or_terminal(geometry, point_attrs, waypoints)

    for gp in edge.geometry_points:
        gp_attrs = _non_null_attrs(gp)
        as_value = gp_attrs.pop("as", "")
        if _is_terminal_point(as_value):
            gp_attrs["as"] = as_value
            ET.SubElement(geometry, "mxPoint", gp_attrs)
        else:
            waypoints.append(gp_attrs)
    return waypoints


def _append_points_array(
    geometry: ET.Element,
    waypoints: list[dict[str, str]],
) -> None:
    """Append the draw.io points array when non-terminal waypoints exist."""
    if not waypoints:
        return

    array = ET.SubElement(geometry, "Array", {"as": "points"})
    for wp in waypoints:
        ET.SubElement(array, "mxPoint", wp)


def _append_edge_geometry(edge_cell: ET.Element, edge: Edge) -> None:
    """Append mxGeometry with waypoints to an edge cell."""
    geo_attrs: dict[str, str] = {"relative": "1", "as": "geometry"}
    geo_attrs.update(_non_null_attrs(edge.geometry_attributes))
    geometry = ET.SubElement(edge_cell, "mxGeometry", geo_attrs)
    _append_points_array(geometry, _collect_edge_waypoints(geometry, edge))


def _edge_endpoint_label_cells(
    edge: Edge, ctx: _GenCtx, variant: int
) -> list[ChildCell]:
    """Build source/target cardinality label cells for an edge, if authored.

    ``source_label``/``target_label`` are registry-declared passthrough
    keywords (e.g. ``erd.Rel``, ``uml.Relation``/``Association``) carried in
    ``edge.extra``; on their own they never reach the generated XML. The
    palette template supplies position and base style per endpoint (via
    ``edge_label_templates``); a slot with no authored value is skipped
    rather than falling back to the palette's own example text.
    """
    templates = ctx.styles.edge_label_templates(edge.type, variant)
    if not templates:
        return []
    vertical_align = edge.extra.get("label_vertical_align")
    cells: list[ChildCell] = []
    for template in templates:
        geometry = template.get("geometry") or {}
        try:
            x = float(geometry.get("x", 0))
        except (TypeError, ValueError):
            x = 0.0
        if x > 0:
            slot = "target_label"
        elif x < 0:
            slot = "source_label"
        else:
            continue
        value = edge.extra.get(slot)
        if not value:
            continue
        overrides = _style_string_to_overrides(str(template.get("style", "")))
        if vertical_align:
            overrides["verticalAlign"] = vertical_align
        cells.append(
            ChildCell(
                label=str(value),
                style_overrides=overrides,
                geometry_attributes={
                    key: geometry[key]
                    for key in ("x", "y", "width", "height", "relative")
                    if key in geometry
                },
            )
        )
    return cells


def _append_edge_child(
    mx_root: ET.Element,
    edge_id: str,
    child: ChildCell,
    ctx: _GenCtx,
) -> None:
    """Append a child cell attached to an edge."""
    child_id = ctx.next_child_id(edge_id)
    cell_attrs: dict[str, str] = {
        "id": child_id,
        "parent": edge_id,
        "vertex": "1",
    }
    if child.label:
        cell_attrs["value"] = child.label

    for key, value in child.cell_attributes.items():
        if value is not None:
            cell_attrs[key] = str(value)

    style = _overrides_to_style(child.style_overrides)
    if style:
        cell_attrs["style"] = style

    cell = ET.SubElement(mx_root, "mxCell", cell_attrs)

    geo_attrs: dict[str, str] = {}
    for key, value in child.geometry_attributes.items():
        if value is not None:
            geo_attrs[key] = str(value)
    if "as" not in geo_attrs:
        geo_attrs["as"] = "geometry"
    geometry = ET.SubElement(cell, "mxGeometry", geo_attrs)

    for point in child.geometry_points:
        pt_attrs = {k: str(v) for k, v in point.items() if v is not None}
        ET.SubElement(geometry, "mxPoint", pt_attrs)


def _style_overrides_for_edge(edge: Edge) -> str:
    """Build style override tokens for an edge."""
    tokens = _style_tokens(edge.style_overrides)
    anchor_tokens = _anchor_tokens("exit", edge.source_anchor)
    if anchor_tokens:
        tokens.append(anchor_tokens)
    anchor_tokens = _anchor_tokens("entry", edge.target_anchor)
    if anchor_tokens:
        tokens.append(anchor_tokens)
    return _tokens_to_style(tokens)


def _anchor_tokens(prefix: str, anchor: str | Anchor) -> str:
    """Build anchor position tokens (exitX, exitY, etc.) from an anchor.

    ``x``/``y`` are always emitted together once ``anchor`` is a real
    ``Anchor`` -- draw.io's anchor is a POINT on the perimeter, defined by
    both fractions jointly (0 is a legitimate coordinate, e.g. the left or
    top edge, not "unset"; only a bare ``Anchor()``/``""`` means no anchor at
    all). ``dx``/``dy``/``perimeter`` stay individually optional: they are
    true add-on overrides where 0 and "absent" really are equivalent.
    """
    if not anchor:
        return ""
    if isinstance(anchor, str):
        return ""
    if anchor == Anchor():
        return ""
    tokens: list[str] = [f"{prefix}X={anchor.x}", f"{prefix}Y={anchor.y}"]
    if anchor.dx:
        tokens.append(f"{prefix}Dx={anchor.dx}")
    if anchor.dy:
        tokens.append(f"{prefix}Dy={anchor.dy}")
    if anchor.perimeter:
        tokens.append(f"{prefix}Perimeter={anchor.perimeter}")
    return ";".join(tokens)


def _overrides_to_style(overrides: dict[str, _StyleValue]) -> str:
    """Convert a style-override dict to a semicolon-delimited style string."""
    return _tokens_to_style(_style_tokens(overrides))


def generate(
    document: Document,
    styles: StyleProvider,
    diagram_id: str = "generated-diagram",
) -> str:
    """Produce a valid draw.io XML string from a Document instance.

    *styles* is the injected ``StyleProvider`` (built by the composition root),
    so the generator holds no global state. Returns a complete ``<mxfile>``
    document as a pretty-printed string.
    """
    root = ET.Element("mxfile", {"host": "app.diagrams.net"})

    diagram_attrs: dict[str, str] = {
        "id": diagram_id,
        "name": document.diagram.name or "Page 1",
    }
    diagram = ET.SubElement(root, "diagram", diagram_attrs)

    graph_attrs: dict[str, str] = {
        "dx": str(CANVAS_DX),
        "dy": str(CANVAS_DY),
        "grid": "1",
        "gridSize": "10",
        "guides": "1",
        "tooltips": "1",
        "connect": "1",
        "arrows": "1",
        "fold": "1",
        "page": "1",
        "pageScale": "1",
        "pageWidth": str(int(document.diagram.page_width)),
        "pageHeight": str(int(document.diagram.page_height)),
        # "math" is a boolean mxGraphModel flag ("0" = off); it is unrelated to
        # ROOT_CELL_ID, which only happens to share the value "0".
        "math": "0",
        "shadow": "0",
    }
    graph = ET.SubElement(diagram, "mxGraphModel", graph_attrs)
    mx_root = ET.SubElement(graph, "root")

    ET.SubElement(mx_root, "mxCell", {"id": ROOT_CELL_ID})
    ET.SubElement(mx_root, "mxCell", {"id": PAGE_CELL_ID, "parent": ROOT_CELL_ID})

    ctx = _GenCtx(styles=styles, apply_overrides=document.diagram.mode != PALETTE_MODE)

    # Identify container nodes — needed for conditional orthogonal edge routing.
    # Collect the referenced parent ids in one pass first; the nested `any(...)`
    # this replaced rescanned every node for every node.
    parent_ids = {n.parent_id for n in document.nodes if n.parent_id}
    ctx.container_ids = {
        n.id for n in document.nodes if n.contains or n.id in parent_ids
    }

    for node in document.nodes:
        _append_node(mx_root, node, ctx)

    for edge in document.edges:
        _append_edge(mx_root, edge, ctx)

    return to_string(root)
