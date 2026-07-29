"""
Scan the draw.io source tree and produce notation_map.json.

Discovers every mxgraph.XXX namespace by reading:
  - shapes/*.js   (mxCellRenderer.registerShape calls and string literals)
  - stencils/**/*.xml  (name= attributes)

Run whenever the draw.io source is updated:
    python build_notation_map.py [path/to/drawio]

Writes: notation_map.json (next to this script)
"""

import json
import re
import sys
from pathlib import Path

DRAWIO_DEFAULT = Path.home() / "dev" / "DrawIoGen" / "drawio"
MXGRAPH_RE = re.compile(r"mxgraph\.[a-zA-Z0-9_]+")


def collect_namespaces(drawio_root: Path) -> set:
    namespaces = set()
    webapp = drawio_root / "src" / "main" / "webapp"

    # JS shape files
    for js_file in (webapp / "shapes").rglob("*.js"):
        for match in MXGRAPH_RE.findall(js_file.read_text(errors="ignore")):
            namespaces.add(match)

    # Stencil XML files
    for xml_file in (webapp / "stencils").rglob("*.xml"):
        for match in MXGRAPH_RE.findall(xml_file.read_text(errors="ignore")):
            namespaces.add(match)

    # Keep only the second-level prefix (mxgraph.foo), drop deeper segments
    return {".".join(ns.split(".")[:2]) for ns in namespaces}


def main():
    drawio_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DRAWIO_DEFAULT

    if not drawio_root.exists():
        print(f"draw.io source not found at {drawio_root}")
        print("Usage: python build_notation_map.py [path/to/drawio]")
        sys.exit(1)

    namespaces = sorted(collect_namespaces(drawio_root))

    notation_map = {
        "source": str(drawio_root),
        "namespaces": namespaces,
    }

    out = Path(__file__).parent / "notation_map.json"
    out.write_text(json.dumps(notation_map, indent=2))
    print(f"Found {len(namespaces)} namespaces → {out}")
    for ns in namespaces:
        print(f"  {ns}")


if __name__ == "__main__":
    main()
