#!/usr/bin/env python3
"""Unified architecture-model consistency check.

Runs every model checker and prints one consolidated report. This is the single
"is the model sound?" entry point for the MBSE structure (see skill://architect).

    python scripts/check_model.py     # or: make model-check

Aggregates:
* import integrity      — C4 relationships <-> real imports
* traceability          — Component <-> module parity, no orphan modules
* code-view lockfile    — code_architecture.mdg equals the real import graph

Exit 0 when every check passes, 1 otherwise. On failure the offending check's
output is echoed so the fix is obvious.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


class Check(NamedTuple):
    label: str
    argv: list[str]


CHECKS: tuple[Check, ...] = (
    Check(
        "import integrity   (C4 relationships <-> imports)",
        [str(SCRIPTS / "check_architecture_components.py")],
    ),
    Check(
        "traceability       (Component <-> module parity)",
        [str(SCRIPTS / "check_traceability.py")],
    ),
    Check(
        "code-view lockfile (code view == import graph)",
        [str(SCRIPTS / "generate_code_arch.py"), "--check"],
    ),
    Check(
        "traceability matrix (matrix == model)",
        [str(SCRIPTS / "generate_traceability_matrix.py"), "--check"],
    ),
)


def _run(check: Check) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, *check.argv],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0, (result.stdout + result.stderr)


def main() -> int:
    print("=== Model consistency ===")
    results = [(check, *_run(check)) for check in CHECKS]

    for check, ok, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {check.label}")

    failures = [(check, output) for check, ok, output in results if not ok]
    if failures:
        for check, output in failures:
            print(f"\n----- {check.label} -----")
            print(output.rstrip())
        print(f"\n{len(failures)} of {len(results)} model checks failed.")
        return 1

    print("\nAll model checks passed: the architecture model is consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
