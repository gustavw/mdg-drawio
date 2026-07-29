"""
Reconstruct .drawio files from JSON exports in the output/ directory.

Usage:
    python json_to_drawio.py

Reads every *.json file in output/ (skipping palette.json) and writes a
matching .drawio file next to it, e.g.:
    output/test_general_shapes.json  →  output/test_general_shapes.drawio
"""

import json
import sys
from pathlib import Path

from drawio_codec import write


def main() -> int:
    out_dir = Path(__file__).parent / "output"
    json_files = sorted(
        p for p in out_dir.rglob("*.json") if p.name != "palette.json"
    )

    if not json_files:
        print("No JSON files found in output/")
        return 1

    for json_path in json_files:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        drawio_path = json_path.with_suffix(".drawio")
        write(data, drawio_path)
        rel = drawio_path.relative_to(out_dir)
        print(f"{rel.with_suffix('.json')}  →  {rel}")

    print(f"\nWrote {len(json_files)} .drawio files → {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
