"""
Generate JSON exports and palette.json for all .drawio files in this directory.

Usage:
    python generate_outputs.py

Outputs:
    output/<name>.json  — one file per .drawio, schema-validated
    output/palette.json — style catalog across all files
"""

import json
import sys
from pathlib import Path

from drawio_codec import parse, validate, extract_palette


def main() -> int:
    here = Path(__file__).parent
    palettes_dir = here.parent / "palette" / "output"
    out_dir = here / "output"
    out_dir.mkdir(exist_ok=True)

    drawio_files = sorted(palettes_dir.rglob("*.drawio"))
    if not drawio_files:
        print("No .drawio files found.")
        return 1

    all_data = []
    errors_found = False

    for filepath in drawio_files:
        rel = filepath.relative_to(palettes_dir)
        print(f"Parsing {rel} ...", end=" ")
        data = parse(filepath)
        errors = validate(data)

        if errors:
            print("SCHEMA ERRORS:")
            for e in errors:
                print(f"  {e}")
            errors_found = True
        else:
            print("OK")
            out_path = out_dir / rel.with_suffix(".json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        all_data.append(data)

    palette = extract_palette(all_data)
    palette_path = out_dir / "palette.json"
    with open(palette_path, "w", encoding="utf-8") as f:
        json.dump(palette, f, indent=2, ensure_ascii=False)

    total_shapes = len(palette["shapes"])
    print(f"\nWrote {len(drawio_files)} JSON files → {out_dir}/")
    print(f"Wrote palette.json ({total_shapes} unique shape styles) → {palette_path}")

    return 1 if errors_found else 0


if __name__ == "__main__":
    sys.exit(main())
