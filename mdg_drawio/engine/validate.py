"""Pre-write validation of generated draw.io XML."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def _validate_diagram_ids(diagrams: list[ET.Element]) -> list[str]:
    """Validate that every diagram has a unique draw.io id."""
    errors: list[str] = []
    diagram_ids: set[str] = set()

    if not diagrams:
        errors.append("no <diagram> elements found")

    for diagram in diagrams:
        diagram_id = diagram.get("id", "")
        if not diagram_id:
            errors.append("diagram missing id attribute")
        elif diagram_id in diagram_ids:
            errors.append(f"duplicate diagram id: {diagram_id!r}")
        else:
            diagram_ids.add(diagram_id)
    return errors


def _iter_cell_ids(root_el: ET.Element) -> list[str]:
    """Yield every cell id in a draw.io root element, in document order.

    Scans direct <mxCell> children AND <mxCell> inside <object>/<UserObject>
    wrappers. The wrapper holds the id; the inner <mxCell> may carry its own.
    Returned as a list (not a set) so callers can detect duplicates.
    """
    ids: list[str] = []
    for cell in root_el.findall("mxCell"):
        cell_id = cell.get("id")
        if cell_id:
            ids.append(cell_id)
    for tag in ("object", "UserObject"):
        for wrapper in root_el.findall(tag):
            cell_id = wrapper.get("id")
            if cell_id:
                ids.append(cell_id)
            inner = wrapper.find("mxCell")
            if inner is not None:
                inner_id = inner.get("id")
                if inner_id:
                    ids.append(inner_id)
    return ids


def _root_cell_ids(root_el: ET.Element) -> set[str]:
    """Return the set of ids for all cells in a draw.io root element."""
    return set(_iter_cell_ids(root_el))


def _iter_edge_cells(root_el: ET.Element) -> list[ET.Element]:
    """Return every edge <mxCell>, including those wrapped in <object>/<UserObject>.

    C4 edges are emitted inside a <UserObject> wrapper, so a plain
    ``findall("mxCell")`` (direct children only) would never see them.
    """
    edges: list[ET.Element] = []
    for cell in root_el.findall("mxCell"):
        if cell.get("edge") == "1":
            edges.append(cell)
    for tag in ("object", "UserObject"):
        for wrapper in root_el.findall(tag):
            inner = wrapper.find("mxCell")
            if inner is not None and inner.get("edge") == "1":
                edges.append(inner)
    return edges


def _validate_unique_cell_ids(
    diagram_id: str | None,
    root_el: ET.Element,
) -> list[str]:
    """Validate that every cell id in a diagram is unique.

    Duplicate ids make draw.io resolve references unpredictably, so catch them
    at generation time rather than at open time.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for cell_id in _iter_cell_ids(root_el):
        if cell_id in seen:
            duplicates.add(cell_id)
        seen.add(cell_id)
    return [
        f"diagram {diagram_id!r}: duplicate cell id: {dup!r}"
        for dup in sorted(duplicates)
    ]


def _validate_root_cells(
    diagram_id: str | None,
    cell_ids: set[str],
) -> list[str]:
    """Validate the mandatory draw.io root cells."""
    errors: list[str] = []
    if "0" not in cell_ids:
        errors.append(f"diagram {diagram_id!r}: missing mxCell id='0'")
    if "1" not in cell_ids:
        errors.append(f"diagram {diagram_id!r}: missing mxCell id='1'")
    return errors


def _validate_edge_references(
    diagram_id: str | None,
    root_el: ET.Element,
    cell_ids: set[str],
) -> list[str]:
    """Validate edge source/target references inside one diagram."""
    errors: list[str] = []
    for cell in _iter_edge_cells(root_el):
        for attr in ("source", "target"):
            ref = cell.get(attr, "")
            if ref and ref not in cell_ids:
                errors.append(
                    f"diagram {diagram_id!r}: "
                    f"edge {cell.get('id')!r} {attr}={ref!r} "
                    f"references unknown cell"
                )
    return errors


def _validate_diagram_model(diagram: ET.Element) -> list[str]:
    """Validate graph model structure for one diagram element."""
    diagram_id = diagram.get("id")
    model = diagram.find("mxGraphModel")
    if model is None:
        return [f"diagram {diagram_id!r}: missing mxGraphModel"]

    root_el = model.find("root")
    if root_el is None:
        return [f"diagram {diagram_id!r}: missing root element"]

    cell_ids = _root_cell_ids(root_el)
    return [
        *_validate_root_cells(diagram_id, cell_ids),
        *_validate_unique_cell_ids(diagram_id, root_el),
        *_validate_edge_references(diagram_id, root_el, cell_ids),
    ]


def validate_generated_xml(xml_string: str) -> list[str]:
    """Validate generated drawio XML before writing to disk.

    Returns a list of error messages (empty = valid). This is a guardrail
    that catches failures at generation time instead of at draw.io open time.
    """
    errors: list[str] = []

    # 1. Must be valid XML
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as exc:
        return [f"invalid XML: {exc}"]

    # 2. Must be a <mxfile>
    if root.tag != "mxfile":
        errors.append("root element is not <mxfile>")

    # 3. Every <diagram> must have a unique id
    diagrams = root.findall("diagram")
    errors.extend(_validate_diagram_ids(diagrams))

    # 4. Every diagram must have an mxGraphModel with root cells 0 and 1
    for diagram in diagrams:
        errors.extend(_validate_diagram_model(diagram))

    return errors
