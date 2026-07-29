#!/usr/bin/env python3
"""
Full pipeline: extract drawio shapes → generate runtime notation style data.

Steps:
  1. tools/palette/generate_palette.py  →  tools/palette/output/
  2. tools/styles/generate_outputs.py   →  tools/styles/output/
  3. scripts/build_notation_styles.py   →  mdg_drawio/generated_data/notation/

Run from anywhere:
    python scripts/build_data.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PALETTE_SCRIPT = ROOT / "tools" / "palette" / "generate_palette.py"
STYLES_SCRIPT = ROOT / "tools" / "styles" / "generate_outputs.py"
DATA_DIR = ROOT / "mdg_drawio" / "generated_data"


def run(script: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(script.parent),
    )
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    print("=== Step 1: Generate palette fixtures ===")
    run(PALETTE_SCRIPT)

    print("\n=== Step 2: Generate JSON outputs ===")
    run(STYLES_SCRIPT)

    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)

    target = DATA_DIR.relative_to(ROOT)
    print(f"\n=== Step 3: Build notation style sidecars in {target}/ ===")
    run(ROOT / "scripts" / "build_notation_styles.py")


if __name__ == "__main__":
    main()
