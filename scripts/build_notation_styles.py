#!/usr/bin/env python3
"""Build the notation styles sidecars in generated_data/notation/<lib>_styles.json.

Joins each registry entry to its palette entry (provenance.pages order +
menu_index) and FAILS if any committed render.fingerprint no longer matches
the palette — palette drift must be a loud error, not silent misrendering.

The sidecar is generated data (derived from the draw.io shape library) and is
gitignored along with the rest of mdg_drawio/generated_data/. Runs as the final
step of `make build-data`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
PALETTE_OUTPUT_DIR = ROOT / "tools" / "styles" / "output"
LIBRARY_PALETTE_JSON: dict[str, str] = {
    "archimate3": "Business/ArchiMate_3.2.json",
    "bpmn2": "Business/BPMN_2.0.json",
    "c4": "Software/C4.json",
    "erd": "Software/Entity_Relation.json",
    "general": "Standard/General.json",
    "uml": "Software/UML.json",
    "uml25": "Software/UML_2.5.json",
}


def _main() -> None:
    sys.path.insert(0, str(ROOT))

    from mdg_drawio.notation import LIBRARIES, load_registry
    from mdg_drawio.notation._core.normalize import style_fingerprint
    from mdg_drawio.notation._core.palette import anchor_cell, flatten_entries
    from mdg_drawio.notation._core.styles import DATA_DIR

    def build_library(library: str) -> tuple[dict[str, Any], list[str]]:
        registry = load_registry(library)
        data = json.loads(
            (PALETTE_OUTPUT_DIR / LIBRARY_PALETTE_JSON[library]).read_text(
                encoding="utf-8"
            )
        )
        flat = flatten_entries(data, registry["provenance"]["pages"])

        sidecar: dict[str, Any] = {}
        errors: list[str] = []
        for shape in registry["shapes"]:
            cells = flat[shape["menu_index"] - 1]
            # Guard the object/value layer, which the style fingerprint does NOT
            # cover: a value of "[object Object]" is the unambiguous signature of
            # the extractor stringifying an <object> wrapper instead of emitting
            # it (see tools/palette/extract_shapes.js setValue/object handling).
            # No legitimate shape has that value, so treat it as a loud failure
            # rather than let corrupted metadata ship silently.
            if any(c.get("value") == "[object Object]" for c in cells):
                errors.append(
                    f"{shape['id']}: object-layer corruption — a cell value is "
                    f"'[object Object]' (menu_index {shape['menu_index']}); the "
                    "palette extractor dropped an <object> wrapper"
                )
                continue
            anchor = anchor_cell(cells, shape["kind"])
            style = anchor.get("style") or ""
            fingerprint = style_fingerprint(style)
            expected = shape["render"]["fingerprint"]
            if fingerprint != expected:
                errors.append(
                    f"{shape['id']}: fingerprint mismatch — registry {expected}, "
                    f"palette {fingerprint} (menu_index {shape['menu_index']}); "
                    "the palette drifted or menu_index is wrong"
                )
                continue
            geometry = anchor.get("geometry") or {}
            sidecar[shape["id"]] = {
                "fingerprint": fingerprint,
                "style": style,
                "width": geometry.get("width"),
                "height": geometry.get("height"),
                "cells": cells,
            }
        return sidecar, errors

    if not PALETTE_OUTPUT_DIR.exists():
        sys.exit("tools/styles/output/ missing — run `make build-data` first")
    out_dir = DATA_DIR / "notation"
    out_dir.mkdir(parents=True, exist_ok=True)
    all_errors: list[str] = []
    for library in LIBRARIES:
        sidecar, errors = build_library(library)
        all_errors.extend(errors)
        path = out_dir / f"{library}_styles.json"
        path.write_text(json.dumps(sidecar, indent=1), encoding="utf-8")
        print(f"{library}: {len(sidecar)} shapes -> {path.relative_to(ROOT)}")
    if all_errors:
        print(f"\n{len(all_errors)} PALETTE VALIDATION ERRORS:", file=sys.stderr)
        for e in all_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
