"""
DrawIO ↔ JSON codec for round-trip parity.

Public API:
    parse(filepath)         → dict  (structured representation of .drawio file)
    write(data, filepath)   → None  (reconstruct .drawio from parsed dict)
    validate(data)          → list[str]  (empty list means valid)
    extract_palette(data_list) → dict   (style catalog across multiple files)
    make_id()               → str   (generate a unique cell ID)
"""

import json
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional


def make_id() -> str:
    """Generate a unique cell ID compatible with draw.io's format."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Number helpers
# ---------------------------------------------------------------------------

def _num(val: Optional[str]) -> Optional[Any]:
    """Parse a numeric string to int or float, preserving integer-ness."""
    if val is None:
        return None
    try:
        f = float(val)
        return int(f) if f == int(f) else f
    except (ValueError, TypeError):
        return None


def _fmt(v: Any) -> str:
    """Format a number back to a string, outputting ints without decimals."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_point(el: ET.Element) -> dict:
    result = {}
    for attr in ("x", "y"):
        v = _num(el.get(attr))
        if v is not None:
            result[attr] = v
    return result


def _parse_rect(el: ET.Element) -> dict:
    result = {}
    for attr in ("x", "y", "width", "height"):
        v = _num(el.get(attr))
        if v is not None:
            result[attr] = v
    return result


def _parse_geometry(el: Optional[ET.Element]) -> Optional[dict]:
    if el is None:
        return None

    geom: dict = {}

    # Numeric position/size attrs — omit if absent (zeros are omitted in source)
    for attr in ("x", "y", "width", "height"):
        v = _num(el.get(attr))
        if v is not None:
            geom[attr] = v

    # relative="1" flag (only on edge geometries)
    if el.get("relative") is not None:
        geom["relative"] = el.get("relative")

    # Sub-elements
    for child in el:
        as_val = child.get("as")
        if child.tag == "mxPoint":
            if as_val == "sourcePoint":
                geom["source_point"] = _parse_point(child)
            elif as_val == "targetPoint":
                geom["target_point"] = _parse_point(child)
            elif as_val == "offset":
                geom["offset"] = _parse_point(child)
        elif child.tag == "Array" and as_val == "points":
            geom["waypoints"] = [_parse_point(p) for p in child if p.tag == "mxPoint"]
        elif child.tag == "mxRectangle" and as_val == "alternateBounds":
            ab = _parse_rect(child)
            # Only keep when the bounds are meaningfully different from the regular geometry.
            # Identical alternate_bounds are written by draw.io as a side-effect of resizing
            # and carry no semantic information.
            if (ab.get("width") != geom.get("width") or
                    ab.get("height") != geom.get("height") or
                    ab.get("x") != geom.get("x") or
                    ab.get("y") != geom.get("y")):
                geom["alternate_bounds"] = ab

    return geom if geom else None


def _parse_cell(cell_el: ET.Element, object_attrs: Optional[dict] = None) -> dict:
    """
    Parse an mxCell element (possibly wrapped in an <object> element).

    object_attrs: attributes from the parent <object> element, with 'label'
                  promoted to 'value' and 'id' promoted to top-level.
    """
    cell: dict[str, Any] = {}

    if object_attrs is not None:
        # Object-wrapped cell (e.g. C4 shapes): id and label live on <object>
        cell["id"] = object_attrs.pop("id")
        cell["value"] = object_attrs.pop("label", "")
        if object_attrs:
            cell["object_attrs"] = object_attrs
        # Remaining mxCell attributes (no id/value here)
        for k, v in cell_el.attrib.items():
            cell[k] = v
    else:
        # Regular mxCell: preserve ALL XML attributes in original order
        for k, v in cell_el.attrib.items():
            cell[k] = v

    geom = _parse_geometry(cell_el.find("mxGeometry"))
    if geom is not None:
        cell["geometry"] = geom

    # y on tableRow cells is managed by childLayout=tableLayout — strip it so the
    # schema representation stays layout-agnostic and avoids stale coordinates.
    style = cell.get("style", "")
    if "shape=tableRow" in style and "geometry" in cell:
        cell["geometry"].pop("y", None)
        if not cell["geometry"]:
            del cell["geometry"]

    return cell


def _parse_root(root_el: ET.Element) -> list:
    cells = []
    for child in root_el:
        if child.tag == "mxCell":
            cells.append(_parse_cell(child))
        elif child.tag in ("object", "UserObject"):
            # <object>/<UserObject> wrap an <mxCell> and carry extra metadata
            # (C4, etc.). draw.io treats the two tags interchangeably.
            attrs = dict(child.attrib)   # preserves insertion order
            cell_el = child.find("mxCell")
            if cell_el is not None:
                cells.append(_parse_cell(cell_el, object_attrs=attrs))
    return cells


def _parse_graph_model(el: ET.Element) -> dict:
    attrs = {}
    for k, v in el.attrib.items():
        n = _num(v)
        attrs[k] = n if n is not None else v
    return attrs


def _parse_diagram(el: ET.Element) -> dict:
    diagram: dict[str, Any] = {
        "id": el.get("id", ""),
        "name": el.get("name", ""),
    }
    gm_el = el.find("mxGraphModel")
    if gm_el is not None:
        diagram["graph_model"] = _parse_graph_model(gm_el)
        root_el = gm_el.find("root")
        diagram["cells"] = _parse_root(root_el) if root_el is not None else []
    else:
        diagram["graph_model"] = {}
        diagram["cells"] = []
    return diagram


def parse(filepath: str | Path) -> dict:
    """Parse a .drawio file and return a structured dict."""
    tree = ET.parse(str(filepath))
    root = tree.getroot()
    return {
        "source_file": Path(filepath).name,
        "host": root.get("host", ""),
        "diagrams": [_parse_diagram(el) for el in root if el.tag == "diagram"],
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _set_point(el: ET.Element, point: dict) -> None:
    for attr in ("x", "y"):
        if attr in point:
            el.set(attr, _fmt(point[attr]))


def _write_geometry(geom: dict, cell_el: ET.Element) -> None:
    geom_el = ET.SubElement(cell_el, "mxGeometry")

    for attr in ("x", "y", "width", "height"):
        if attr in geom:
            geom_el.set(attr, _fmt(geom[attr]))
    if "relative" in geom:
        geom_el.set("relative", geom["relative"])
    geom_el.set("as", "geometry")

    if "source_point" in geom:
        pt = ET.SubElement(geom_el, "mxPoint")
        _set_point(pt, geom["source_point"])
        pt.set("as", "sourcePoint")

    if "target_point" in geom:
        pt = ET.SubElement(geom_el, "mxPoint")
        _set_point(pt, geom["target_point"])
        pt.set("as", "targetPoint")

    if "waypoints" in geom:
        arr = ET.SubElement(geom_el, "Array")
        arr.set("as", "points")
        for wp in geom["waypoints"]:
            pt = ET.SubElement(arr, "mxPoint")
            _set_point(pt, wp)

    if "alternate_bounds" in geom:
        rect = ET.SubElement(geom_el, "mxRectangle")
        ab = geom["alternate_bounds"]
        for attr in ("x", "y", "width", "height"):
            if attr in ab:
                rect.set(attr, _fmt(ab[attr]))
        rect.set("as", "alternateBounds")

    if "offset" in geom:
        pt = ET.SubElement(geom_el, "mxPoint")
        _set_point(pt, geom["offset"])
        pt.set("as", "offset")


def _write_cell(root_el: ET.Element, cell: dict) -> None:
    # Only these two keys are structural — never written as XML attributes
    skip_always = {"object_attrs", "geometry"}

    if "object_attrs" in cell:
        # Reconstruct <object id="…" …attrs… label="…"> <mxCell …/> </object>
        obj_el = ET.SubElement(root_el, "object")
        obj_el.set("id", cell["id"])
        for k, v in cell.get("object_attrs", {}).items():
            obj_el.set(k, str(v))
        obj_el.set("label", cell.get("value", ""))

        # mxCell inside <object> carries no id or value/label
        cell_el = ET.SubElement(obj_el, "mxCell")
        for k, v in cell.items():
            if k not in skip_always and k not in ("id", "value"):
                cell_el.set(k, str(v))
    else:
        cell_el = ET.SubElement(root_el, "mxCell")
        # Write ALL cell dict keys as XML attributes, preserving original order
        for k, v in cell.items():
            if k not in skip_always:
                cell_el.set(k, str(v))

    if "geometry" in cell:
        _write_geometry(cell["geometry"], cell_el)


def write(data: dict, filepath: str | Path) -> None:
    """Reconstruct a .drawio file from a parsed dict."""
    mxfile_el = ET.Element("mxfile")
    if data.get("host"):
        mxfile_el.set("host", data["host"])

    for diagram in data.get("diagrams", []):
        diag_el = ET.SubElement(mxfile_el, "diagram")
        diag_el.set("id", diagram["id"])
        diag_el.set("name", diagram["name"])

        gm_el = ET.SubElement(diag_el, "mxGraphModel")
        for k, v in diagram.get("graph_model", {}).items():
            gm_el.set(k, _fmt(v) if isinstance(v, (int, float)) else str(v))

        root_el = ET.SubElement(gm_el, "root")
        for cell in diagram.get("cells", []):
            _write_cell(root_el, cell)

    tree = ET.ElementTree(mxfile_el)
    ET.indent(tree, space="  ")
    tree.write(str(filepath), encoding="unicode", xml_declaration=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(data: dict) -> list:
    """
    Validate a parsed dict against the JSON schema.
    Returns a list of error strings (empty list = valid).
    Requires the 'jsonschema' package.
    """
    schema_path = Path(__file__).parent / "schema.json"
    if not schema_path.exists():
        return ["schema.json not found next to drawio_codec.py"]

    try:
        import jsonschema
    except ImportError:
        return ["jsonschema package not installed (pip install jsonschema)"]

    with open(schema_path) as f:
        schema = json.load(f)

    errors = []
    validator = jsonschema.Draft7Validator(schema)
    for err in validator.iter_errors(data):
        errors.append(f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}")
    return errors


# ---------------------------------------------------------------------------
# Palette extraction
# ---------------------------------------------------------------------------

def _shape_key_from_style(style: str) -> str:
    """Derive a short shape identifier from a drawio style string."""
    if not style:
        return "default"

    tokens = [t.strip() for t in style.split(";") if t.strip()]
    if not tokens:
        return "default"

    # Explicit shape=xxx takes precedence
    for token in tokens:
        if token.startswith("shape="):
            return token[6:]

    # First token with no '=' is a named style class (e.g. "ellipse", "rhombus")
    if "=" not in tokens[0]:
        return tokens[0]

    return "default"


def _notation_from_style(style: str) -> str:
    """
    Derive the diagram notation from a style string.

    Priority:
      1. Explicit notation= token in the style string.
      2. shape=mxgraph.XXX — use mxgraph.XXX as the notation directly.
         The namespace list in notation_map.json (generated by
         build_notation_map.py from the draw.io source) validates known
         prefixes, but unknown ones are handled identically — the second
         segment is always the authoritative notation label with no
         hardcoded renaming.
      3. "general" fallback.

    Note: UML 2.5 shapes that share names with the core UML palette
    (umlActor, umlLifeline, endState, …) have neither a notation= token
    nor a mxgraph.uml25 prefix, so they resolve to "general" by style
    alone. Their source_files entry in the palette records which diagrams
    actually use them, giving the context the style string cannot.
    """
    shape_val = None
    for token in style.split(";"):
        token = token.strip()
        if token.startswith("notation="):
            return token[9:]
        if token.startswith("shape="):
            shape_val = token[6:]

    if shape_val and shape_val.startswith("mxgraph."):
        parts = shape_val.split(".")
        if len(parts) >= 2 and parts[1]:
            return f"mxgraph.{parts[1]}"

    return "general"


def extract_palette(data_list: list) -> dict:
    """
    Build a style palette catalog from a list of parsed drawio dicts.
    Groups unique styles by notation and shape key.
    """
    # Map (notation, shape_key, style) → {geometry_template, sources}
    seen: dict[tuple, dict] = {}

    for data in data_list:
        src = data.get("source_file", "")
        for diagram in data.get("diagrams", []):
            for cell in diagram.get("cells", []):
                style = cell.get("style", "")
                if not style:
                    continue
                if cell.get("edge") == "1":
                    continue  # skip edges from the shape palette

                notation = _notation_from_style(style)
                shape_key = _shape_key_from_style(style)
                key = (notation, shape_key, style)

                if key not in seen:
                    geom = cell.get("geometry", {})
                    template = {}
                    if "width" in geom:
                        template["width"] = geom["width"]
                    if "height" in geom:
                        template["height"] = geom["height"]
                    seen[key] = {
                        "palette_key": f"{notation}__{shape_key}",
                        "notation": notation,
                        "shape_key": shape_key,
                        "style": style,
                        "default_geometry": template,
                        "source_files": [src],
                    }
                else:
                    if src not in seen[key]["source_files"]:
                        seen[key]["source_files"].append(src)

    shapes = sorted(seen.values(), key=lambda s: (s["notation"], s["shape_key"]))
    return {"shapes": shapes}
