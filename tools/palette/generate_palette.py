#!/usr/bin/env python3
"""
Generate drawio files for the full "More Shapes" menu, 1:1 with drawio itself.

Runs extract_shapes.js, which replays drawio's own initPalettes() and emits:
    { menu, config, palettes }
where `menu` is the section -> entry structure, `config` maps each entry id to
its ordered palette ids, and `palettes` holds the captured shapes per palette id.

Output layout mirrors the menu exactly:
    fixtures/<Section>/<Entry>.drawio         (one file per menu entry)
        └─ one page per palette, in menu order
"""

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
EXTRACT_JS = SCRIPT_DIR / "extract_shapes.js"
OUTPUT_DIR = SCRIPT_DIR / "output"

# Layout parameters
COLS = 5
CELL_PAD_X = 20
CELL_PAD_Y = 40
DEFAULT_W = 120
DEFAULT_H = 60
PAGE_MARGIN = 40

# Nicer display names for menu entries whose titles are raw resource keys.
TITLE_OVERRIDES = {
    "general": "General", "basic": "Basic", "arrows": "Arrows 2",
    "clipart": "Clipart", "flowchart": "Flowchart", "android": "Android",
    "ios": "iOS", "mockups": "Mockups", "uml": "UML", "uml 2.5": "UML 2.5",
    "bpmn 2.0": "BPMN 2.0", "azure": "Azure", "cisco": "Cisco", "rack": "Rack",
    "sysml": "SysML", "eip": "Enterprise Integration", "electrical": "Electrical",
    "signs": "Signs", "gmdl": "Material Design", "procEng": "P&ID",
    "cabinets": "Cabinets", "floorplans": "Floorplan", "entityRelation": "Entity Relation",
    "archiMate21": "ArchiMate 2.1",
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def xa(v: str) -> str:
    return escape(str(v), quote=True)


def clean_title(title: str) -> str:
    """Turn a (possibly resource-key) menu title into a display title."""
    if title in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[title]
    # Title-case all-lowercase keys; leave mixed-case titles as-is.
    if title and title == title.lower():
        return " ".join(w.capitalize() for w in title.split())
    return title


def safe_name(name: str) -> str:
    """Filesystem-safe stem."""
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("_") or "untitled"


def page_name(palette_title: str) -> str:
    """Short page-tab name: keep the part after the last ' / ' if present."""
    if " / " in palette_title:
        return palette_title.rsplit(" / ", 1)[1]
    return palette_title


# ──────────────────────────────────────────────────────────────────────────────
# Layout
# ──────────────────────────────────────────────────────────────────────────────

def layout_shapes(shapes: list[dict], start_id: int = 2):
    if not shapes:
        return [], PAGE_MARGIN * 2, PAGE_MARGIN * 2, start_id

    rows = [shapes[i:i + COLS] for i in range(0, len(shapes), COLS)]
    row_heights = [max(max(s.get("h", DEFAULT_H), 10) for s in row) for row in rows]

    col_widths = []
    for ci in range(COLS):
        cw = DEFAULT_W
        for row in rows:
            if ci < len(row):
                cw = max(cw, max(row[ci].get("w", DEFAULT_W), 10))
        col_widths.append(cw)

    cells = []
    cell_id = start_id
    y = PAGE_MARGIN
    for ri, row in enumerate(rows):
        x = PAGE_MARGIN
        for ci, s in enumerate(row):
            cw = col_widths[ci]
            rh = row_heights[ri]
            sw = max(s.get("w", DEFAULT_W), 10)
            sh = max(s.get("h", DEFAULT_H), 10)
            sx = x + (cw - sw) // 2
            sy = y + (rh - sh) // 2
            cells.append(dict(id=cell_id, x=sx, y=sy, w=sw, h=sh, xml=s.get("xml")))
            cell_id += 1
            x += cw + CELL_PAD_X
        y += row_heights[ri] + CELL_PAD_Y

    total_w = PAGE_MARGIN + sum(col_widths) + CELL_PAD_X * (COLS - 1) + PAGE_MARGIN
    total_h = y + PAGE_MARGIN
    return cells, total_w, total_h, cell_id


# ──────────────────────────────────────────────────────────────────────────────
# Shape rendering — translate-only.
#
# Every shape arrives as one canonical mxGraphModel XML (drawio's own encoding).
# Rendering it means exactly: re-id its cells into this page, remap references,
# and offset the root cells into their grid slot. Nothing is reconstructed from
# decomposed fields, so no shape property can be silently dropped.
# ──────────────────────────────────────────────────────────────────────────────

# draw.io treats <object> and <UserObject> as interchangeable wrappers around an
# <mxCell> (see mxfile.xsd UserObjectType). Handle both so metadata-carrying
# wrappers are never silently dropped.
CELL_TAGS = ("mxCell", "object", "UserObject")


def _translate_point(pt, dx: int, dy: int) -> None:
    """Shift an mxPoint (edge terminal/waypoint) by the grid-cell offset."""
    try:
        pt.set("x", str(int(dx + float(pt.get("x", "0")))))
        pt.set("y", str(int(dy + float(pt.get("y", "0")))))
    except Exception:
        pass


def _cell_node(el):
    """The node carrying style/parent/geometry: an <object> wraps an <mxCell>."""
    if el.tag == "mxCell":
        return el
    inner = el.find("mxCell")
    return inner if inner is not None else el


def render_shape(cell: dict) -> str:
    xml_str = cell.get("xml")
    dx, dy = cell["x"], cell["y"]
    cid = cell["id"]
    if not xml_str:
        return _fallback_cell(cell)

    try:
        root = ET.fromstring(xml_str)
        model_root = root.find("root") if root.tag == "mxGraphModel" else root
        container = model_root if model_root is not None else root

        cells_el = [el for el in container
                    if el.tag in CELL_TAGS and el.get("id", "") not in ("0", "1")]

        # Pass 1: id remap (source/target/parent may be forward references).
        id_map: dict[str, str] = {}
        for el in cells_el:
            eid = el.get("id", "")
            if eid and eid not in id_map:
                id_map[eid] = f"{cid}_{eid}"

        # Pass 2: rewrite ids/refs and offset root-level cells into the grid slot.
        parts = []
        seen: set[str] = set()
        for el in cells_el:
            eid = el.get("id", "")
            new_id = id_map.get(eid, f"{cid}_{eid}")
            if new_id in seen:
                continue
            seen.add(new_id)
            el.set("id", new_id)

            node = _cell_node(el)   # geometry/parent/edge live here (object→inner mxCell)

            parent = node.get("parent", "1")
            if parent in ("0", "1"):
                node.set("parent", "1")
                parent_is_root = True
            else:
                node.set("parent", id_map.get(parent, "1"))
                parent_is_root = False

            for ref in ("source", "target"):
                rv = node.get(ref)
                if rv is not None and rv in id_map:
                    node.set(ref, id_map[rv])

            is_edge = node.get("edge") == "1"
            geo = node.find("mxGeometry")
            if geo is not None and parent_is_root:
                if is_edge:
                    # Edges are relative="1"; their absolute terminal points and
                    # waypoints must move into the slot (else they stack at origin).
                    for pt in geo.findall("mxPoint"):
                        if pt.get("as") in ("sourcePoint", "targetPoint"):
                            _translate_point(pt, dx, dy)
                    arr = geo.find("Array")
                    if arr is not None and arr.get("as") == "points":
                        for pt in arr.findall("mxPoint"):
                            _translate_point(pt, dx, dy)
                elif geo.get("relative") != "1":
                    # Absolute vertex. Children/relative cells keep their own coords.
                    try:
                        geo.set("x", str(int(dx + float(geo.get("x", "0")))))
                        geo.set("y", str(int(dy + float(geo.get("y", "0")))))
                    except Exception:
                        pass

            parts.append(ET.tostring(el, encoding="unicode"))

        return "\n".join(parts) if parts else _fallback_cell(cell)
    except Exception:
        return _fallback_cell(cell)


def _fallback_cell(cell: dict) -> str:
    cid = cell["id"]
    return (
        f'<mxCell id="{cid}" value="" style="rounded=0;whiteSpace=wrap;html=1;" '
        f'vertex="1" parent="1">'
        f'<mxGeometry x="{cell["x"]}" y="{cell["y"]}" '
        f'width="{cell["w"]}" height="{cell["h"]}" as="geometry"/>'
        f'</mxCell>'
    )


# ──────────────────────────────────────────────────────────────────────────────
# Diagram page / file generation
# ──────────────────────────────────────────────────────────────────────────────

def diagram_xml(name: str, shapes: list[dict], idx: int) -> str:
    cells, total_w, total_h, _ = layout_shapes(shapes, start_id=2)

    cell_parts = []
    for c in cells:
        try:
            cell_parts.append(render_shape(c))
        except Exception:
            pass

    cells_body = "\n".join(cell_parts)
    w = max(total_w, 1200)
    h = max(total_h, 900)

    return (
        f'<diagram id="pg-{idx}" name="{xa(name)}">'
        f'<mxGraphModel dx="1200" dy="900" grid="1" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{w}" pageHeight="{h}" math="0" shadow="0">'
        f'<root>'
        f'<mxCell id="0"/><mxCell id="1" parent="0"/>'
        f'{cells_body}'
        f'</root></mxGraphModel></diagram>'
    )


def mxfile_xml(diagrams: list[str]) -> str:
    body = "\n".join(diagrams)
    return f'<mxfile host="app.diagrams.net">\n{body}\n</mxfile>\n'


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("Running shape extractor…")
    try:
        result = subprocess.run(
            ["node", str(EXTRACT_JS)],
            capture_output=True, text=True, timeout=180,
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode != 0:
            print("extractor stderr:", result.stderr[:800])
            sys.exit(1)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("ERROR: extractor timed out")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: JSON parse failed: {e}")
        sys.exit(1)

    menu = data["menu"]
    config = data["config"]
    palettes = data["palettes"]

    print(f"Extracted {len(palettes)} palettes, "
          f"{sum(len(p.get('shapes', [])) for p in palettes.values())} shapes total")

    # Clean output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old_dir in OUTPUT_DIR.iterdir():
        if old_dir.is_dir():
            for f in old_dir.glob("*.drawio"):
                f.unlink()
            try:
                old_dir.rmdir()
            except OSError:
                pass
    for old in OUTPUT_DIR.glob("*.drawio"):
        old.unlink()

    total_files = 0
    total_pages = 0
    missing_palettes: list[str] = []

    for section in menu:
        section_dir = OUTPUT_DIR / clean_title(section["title"])
        section_dir.mkdir(exist_ok=True)

        section_files = 0
        for entry in section["entries"]:
            eid = entry["id"]
            pal_ids = config.get(eid, [eid])

            diagrams = []
            for pid in pal_ids:
                pal = palettes.get(pid)
                if pal is None:
                    missing_palettes.append(pid)
                    continue
                shapes = pal.get("shapes", [])
                if not shapes:
                    continue
                diagrams.append(diagram_xml(page_name(pal["title"]), shapes, len(diagrams) + 1))

            if not diagrams:
                continue

            fname = safe_name(clean_title(entry["title"])) + ".drawio"
            (section_dir / fname).write_text(mxfile_xml(diagrams), encoding="utf-8")
            total_files += 1
            section_files += 1
            total_pages += len(diagrams)

        print(f"  {clean_title(section['title']):12}  {section_files} files")

    print(f"\nGenerated {total_files} files ({total_pages} pages) in {OUTPUT_DIR}/")
    if missing_palettes:
        print(f"Note: {len(missing_palettes)} palette id(s) in config had no "
              f"captured shapes (drawio config/registration mismatches): "
              f"{', '.join(missing_palettes)}")


if __name__ == "__main__":
    main()
