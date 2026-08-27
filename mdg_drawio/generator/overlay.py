"""Read an existing .drawio file and extract a ``GeometryOverlay``.

This is a pure parse operation — it reads data, it does not apply it.
The returned ``GeometryOverlay`` is set on ``Document.geometry_overlay``
and consumed by the layout engine and generator.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from mdg_drawio.contracts import (
    PAGE_CELL_ID,
    ROOT_CELL_ID,
    EdgeAnchorOverlay,
    GeometryOverlay,
)

_ANCHOR_STYLE_KEYS = frozenset({"exitX", "exitY", "entryX", "entryY"})

# Per-instance style tokens preserved verbatim across a plain regenerate
# (`mdg in.mdg out.drawio`) -- purely cosmetic tweaks a user commonly
# hand-adjusts in the draw.io UI, which a fresh generation would otherwise
# reset to the palette/config default. This module has no notion of which
# notation a cell belongs to (it reads the .drawio in isolation, before any
# .mdg is even parsed), so it reads every one of these unconditionally --
# engine/convert.py's `_inject_node_overlay` is what actually applies them,
# and it excludes the colour keys for a node whose library encodes real
# meaning in colour (e.g. C4 Person vs Person_Ext) once it knows the node's
# type, so a manual colour tweak there can never mask an intentional .mdg
# type/variant change. See convert.py's `_COLOR_SEMANTIC_LIBRARIES`.
_PRESERVED_NODE_STYLE_KEYS = frozenset(
    {"align", "verticalAlign", "fillColor", "strokeColor", "fontColor"}
)


def _read_cell_geometry(
    cell_id: str,
    cell: ET.Element,
) -> tuple[str, dict[str, float]] | None:
    """Read x, y, width, height from a vertex cell.

    *cell_id* is the effective id: for object/UserObject-wrapped cells it comes
    from the wrapper, since the inner ``<mxCell>`` carries no id of its own.
    """
    if cell.get("vertex") != "1":
        return None
    if not cell_id or cell_id in (ROOT_CELL_ID, PAGE_CELL_ID):
        return None
    geom = cell.find("mxGeometry")
    if geom is None:
        return None
    geo: dict[str, float] = {}
    for field in ("x", "y", "width", "height"):
        val = geom.get(field)
        if val is not None:
            try:
                geo[field] = float(val)
            except ValueError:
                continue
    return (cell_id, geo) if geo else None


def _read_cell_style_overrides(cell: ET.Element) -> dict[str, str]:
    """Read the preserved-key subset of a vertex cell's style string."""
    style = cell.get("style", "")
    overrides: dict[str, str] = {}
    for token in style.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key in _PRESERVED_NODE_STYLE_KEYS:
            overrides[key] = value
    return overrides


def _read_edge_waypoints(cell: ET.Element) -> list[tuple[float, float]]:
    """Read elbow waypoints from an edge cell's <Array as="points">."""
    waypoints: list[tuple[float, float]] = []
    geom = cell.find("mxGeometry")
    if geom is None:
        return waypoints
    array = geom.find('Array[@as="points"]')
    if array is None:
        return waypoints
    for pt in array.findall("mxPoint"):
        try:
            x = float(pt.get("x", ""))
            y = float(pt.get("y", ""))
            waypoints.append((x, y))
        except (ValueError, TypeError):
            pass
    return waypoints


def _read_edge_anchors(
    cell_id: str,
    cell: ET.Element,
) -> tuple[str, EdgeAnchorOverlay] | None:
    """Read exitX/exitY/entryX/entryY and elbow waypoints from an edge cell.

    *cell_id* is the effective id (from the object/UserObject wrapper when the
    edge is wrapped), used only as the fallback key for unconnected edges.
    """
    if cell.get("edge") != "1":
        return None
    style = cell.get("style", "")
    anchors: dict[str, str] = {}
    for token in style.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key in _ANCHOR_STYLE_KEYS:
            anchors[key] = value

    waypoints = _read_edge_waypoints(cell)
    source = cell.get("source", "")
    target = cell.get("target", "")
    if not source or not target:
        if not anchors and not waypoints:
            return None
    key = f"{source}->{target}" if source and target else cell_id
    return (key, EdgeAnchorOverlay(
        exit_x=anchors.get("exitX"),
        exit_y=anchors.get("exitY"),
        entry_x=anchors.get("entryX"),
        entry_y=anchors.get("entryY"),
        waypoints=waypoints,
    ))


def _iter_cells(root_el: ET.Element) -> list[tuple[str, ET.Element]]:
    """Yield ``(effective_id, mxCell)`` for every cell under root.

    Unwraps ``<object>``/``<UserObject>`` wrappers (used for cells carrying
    metadata — every C4 node and edge) so their geometry/anchors are read too.
    The wrapper holds the id; the inner ``<mxCell>`` holds vertex/edge/geometry.
    """
    cells: list[tuple[str, ET.Element]] = []
    for child in root_el:
        if child.tag == "mxCell":
            cells.append((child.get("id", ""), child))
        elif child.tag in ("object", "UserObject"):
            inner = child.find("mxCell")
            if inner is not None:
                cells.append((child.get("id", ""), inner))
    return cells


def _read_diagram_overlay(diagram: ET.Element) -> GeometryOverlay:
    """Read node and edge overlays from one diagram element."""
    result = GeometryOverlay()
    model = diagram.find("mxGraphModel")
    if model is None:
        return result
    root_el = model.find("root")
    if root_el is None:
        return result
    for cell_id, cell in _iter_cells(root_el):
        node_geo = _read_cell_geometry(cell_id, cell)
        if node_geo:
            result.nodes[node_geo[0]] = node_geo[1]
            style_overrides = _read_cell_style_overrides(cell)
            if style_overrides:
                result.node_styles[node_geo[0]] = style_overrides
            continue
        edge_anchor = _read_edge_anchors(cell_id, cell)
        if edge_anchor:
            result.edges.setdefault(edge_anchor[0], []).append(edge_anchor[1])
    return result


def read_overlay(path: str) -> list[GeometryOverlay]:
    """Read per-page geometry overlays from a .drawio file path."""
    with open(path, encoding="utf-8") as f:
        try:
            return read_overlay_xml(f.read())
        except ValueError as exc:
            raise ValueError(f"malformed overlay file {path!r}: {exc}") from exc


def read_overlay_xml(xml_string: str) -> list[GeometryOverlay]:
    """Read per-page geometry overlays from a .drawio XML string."""
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as exc:
        raise ValueError(f"malformed overlay XML: {exc}") from exc
    return [_read_diagram_overlay(d) for d in root.findall("diagram")]
