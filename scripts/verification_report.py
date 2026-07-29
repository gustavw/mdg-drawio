#!/usr/bin/env python3
"""Per-Component test-coverage report (verification traceability, Phase 3 · C).

Maps real line-coverage data (``coverage``) onto C4 Components via the
``realized-by`` traces: covered file → module → Component. Answers "which
Component is under-tested?" from actual coverage, not manufactured traces.

    make verification                         # run the suite under coverage + report
    python scripts/verification_report.py --json coverage.json   # report only

Advisory by default (exit 0). ``--min <pct>`` gates: exit 1 if any code-backed
Component's line coverage is below the threshold.

Because coverage is dynamic (it requires running the suite), this is a separate
`make verification` target — it is intentionally NOT part of the static
`make model-check` gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent


class Coverage(NamedTuple):
    covered: int
    statements: int

    @property
    def pct(self) -> float:
        return 100.0 * self.covered / self.statements if self.statements else 0.0


def _module_to_component() -> dict[str, str]:
    """Invert the realized-by traces: module rel path → Component id."""
    path = ROOT / "scripts" / "check_traceability.py"
    spec = importlib.util.spec_from_file_location("check_traceability", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_traceability", module)
    spec.loader.exec_module(module)
    return {
        rel: component
        for component, rels in module.component_modules().items()
        for rel in rels
    }


def component_coverage(data: dict) -> dict[str, Coverage]:
    """Aggregate coverage.json file data into per-Component line coverage."""
    module_to_component = _module_to_component()
    totals: dict[str, tuple[int, int]] = {}
    for file_path, entry in data.get("files", {}).items():
        norm = file_path.replace("\\", "/")
        if "mdg_drawio/" not in norm:
            continue
        rel = norm.split("mdg_drawio/", 1)[1]
        component = module_to_component.get(rel)
        if component is None:
            continue
        summary = entry["summary"]
        covered, statements = totals.get(component, (0, 0))
        totals[component] = (
            covered + summary["covered_lines"],
            statements + summary["num_statements"],
        )
    return {comp: Coverage(c, s) for comp, (c, s) in totals.items()}


def _run_coverage() -> dict:
    """Run the test suite under coverage and return the JSON report.

    Measures in-process coverage. The pipeline tests drive conversions
    in-process (via ``main()``), so the real layout/generator/engine paths are
    captured; the few tests that shell out to ``python scripts/...`` exercise
    those tools rather than new ``mdg_drawio`` code, so they are not counted.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        env = {**os.environ, "COVERAGE_FILE": str(Path(tmp) / ".coverage")}
        result = subprocess.run(
            [sys.executable, "-m", "coverage", "run", "-m", "pytest", "tests", "-q"],
            cwd=ROOT,
            env=env,
            check=False,
        )
        # Coverage can still emit JSON when tests fail; a passing coverage report
        # over a failing suite is not verification. The suite itself is not
        # advisory, so surface the failure and stop.
        if result.returncode != 0:
            print(
                f"Test suite failed (pytest exit {result.returncode}); "
                "coverage report is not trustworthy.",
                file=sys.stderr,
            )
            raise SystemExit(result.returncode)
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(report)],
            cwd=ROOT,
            env=env,
            check=True,
        )
        return json.loads(report.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="read an existing coverage.json")
    parser.add_argument("--min", type=float, default=None, help="fail below this pct")
    args = parser.parse_args()

    if args.json:
        data = json.loads(args.json.read_text(encoding="utf-8"))
    else:
        data = _run_coverage()
    coverage = component_coverage(data)

    print("=== Per-Component test coverage ===")
    for component in sorted(coverage, key=lambda c: coverage[c].pct):
        cov = coverage[component]
        print(f"  {cov.pct:5.1f}%  {component}  ({cov.covered}/{cov.statements})")

    gaps = sorted(c for c, cov in coverage.items() if cov.covered == 0)
    if gaps:
        print("\nUNTESTED components (0% coverage):")
        for component in gaps:
            print(f"  {component}")

    if args.min is not None:
        below = sorted(c for c, cov in coverage.items() if cov.pct < args.min)
        if below:
            print(f"\n{len(below)} component(s) below {args.min:.0f}%: {below}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
