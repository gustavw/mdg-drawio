#!/usr/bin/env python3
"""Aggregate the Makefile quality signals into one static D3 dashboard.

Runs the suite under coverage (test results + per-Component coverage in one
pass), the CLI action sweep (dead-code reachability), the model-consistency
gates, and lint, then renders a self-contained ``dashboard.html`` — vendored D3
and all data inlined, so it opens offline in any browser.

    python scripts/build_dashboard.py                 # -> dashboard.html
    python scripts/build_dashboard.py -o out.html       # custom path
    python scripts/build_dashboard.py --data-only        # print collected JSON

Advisory tool: it never fails the build. It reports what the gates found.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "assets"
DEFAULT_OUTPUT = ROOT / "dashboard.html"

# Allow direct invocation (`python scripts/build_dashboard.py`) to import the
# sibling `scripts.*` modules, where only scripts/ lands on sys.path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Test results + coverage (one instrumented pytest run)
# ---------------------------------------------------------------------------


def _module_of(classname: str) -> str:
    """Test module from a JUnit ``classname`` (drop any test-class suffix)."""
    parts = classname.split(".")
    for i, part in enumerate(parts):
        if part.startswith("test_"):
            return ".".join(parts[: i + 1])
    return classname


def _parse_junit(path: Path) -> dict:
    """Aggregate a JUnit XML report into totals + per-module breakdown."""
    root = ET.parse(path).getroot()
    modules: dict[str, dict[str, float]] = {}
    slowest: list[dict] = []
    totals = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0}

    for case in root.iter("testcase"):
        module = _module_of(case.get("classname", ""))
        time = float(case.get("time", 0.0))
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"

        totals["total"] += 1
        totals[status] += 1
        totals["time"] += time

        m = modules.setdefault(
            module, {"passed": 0, "failed": 0, "skipped": 0, "total": 0, "time": 0.0}
        )
        m[status] += 1
        m["total"] += 1
        m["time"] += time
        slowest.append({
            "name": f"{module}::{case.get('name', '')}",
            "time": round(time, 3),
            "status": status,
        })

    totals["time"] = round(totals["time"], 2)
    module_list = [
        {
            "module": name,
            **{k: (round(v, 2) if k == "time" else v) for k, v in vals.items()},
        }
        for name, vals in sorted(modules.items())
    ]
    slowest.sort(key=lambda t: t["time"], reverse=True)
    return {"totals": totals, "by_module": module_list, "slowest": slowest[:10]}


def _coverage_min() -> int:
    """Read COVERAGE_MIN from the Makefile (the CI ratchet); default 60."""
    text = (ROOT / "Makefile").read_text()
    match = re.search(r"^COVERAGE_MIN\s*:?=\s*(\d+)", text, re.M)
    return int(match.group(1)) if match else 60


def _component_coverage(coverage_data: dict) -> dict:
    """Per-Component line coverage via the verification-report mapping."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "verification_report", ROOT / "scripts" / "verification_report.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    by_component = module.component_coverage(coverage_data)
    components = [
        {
            "component": name,
            "covered": cov.covered,
            "statements": cov.statements,
            "pct": round(cov.pct, 1),
        }
        for name, cov in sorted(by_component.items(), key=lambda kv: kv[1].pct)
    ]
    total_covered = sum(c["covered"] for c in components)
    total_stmts = sum(c["statements"] for c in components)
    overall = round(100.0 * total_covered / total_stmts, 1) if total_stmts else 0.0
    return {"components": components, "overall": overall, "min": _coverage_min()}


def _tests_and_coverage() -> tuple[dict, dict, dict]:
    """One `coverage run -m pytest` pass -> (test results, coverage, raw json)."""
    with tempfile.TemporaryDirectory(prefix="mdg-dash-") as tmp:
        junit = Path(tmp) / "junit.xml"
        cov_json = Path(tmp) / "coverage.json"
        subprocess.run(
            [sys.executable, "-m", "coverage", "run", "-m", "pytest", "tests",
             f"--junitxml={junit}", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(cov_json)],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        tests = _parse_junit(junit)
        coverage_data = json.loads(cov_json.read_text())
        coverage = _component_coverage(coverage_data)
    return tests, coverage, coverage_data


# ---------------------------------------------------------------------------
# Dead code (CLI action sweep + test reachability derived from coverage)
# ---------------------------------------------------------------------------


def _dead_code(coverage_data: dict) -> dict:
    from scripts.analyze_dead_code import (
        build_reference_graph,
        build_report,
        collect_definitions,
        collect_exported,
        covered_definitions,
    )
    from scripts.trace_actions import run_all, touched_union

    universe = collect_definitions()
    touched = touched_union(run_all(quiet=True))
    report = build_report(
        universe, touched, build_reference_graph(),
        collect_exported(), covered_definitions(coverage_data),
    )
    return {
        "universe": report.universe_size,
        "reachable_pct": round(report.reachable_pct, 1),
        "truly_dead": sorted(report.truly_dead),
        "uncovered": len(report.uncovered),
        "test_reached": len(report.test_reached),
        "allowlisted": len(report.allowlisted_dead),
    }


# ---------------------------------------------------------------------------
# Model-consistency gates + lint
# ---------------------------------------------------------------------------


def _model_checks() -> list[dict]:
    from scripts import check_model

    gates = []
    for check in check_model.CHECKS:
        ok, _ = check_model._run(check)
        label = re.sub(r"\s*\(.*\)$", "", check.label).strip()
        gates.append({"label": label, "ok": ok})
    return gates


def _lint() -> list[dict]:
    results = []
    mypy = subprocess.run(
        ["mypy"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    m = re.search(r"Found (\d+) error", mypy.stdout)
    results.append({
        "tool": "mypy",
        "ok": mypy.returncode == 0,
        "issues": int(m.group(1)) if m else 0,
    })
    ruff = subprocess.run(
        ["ruff", "check", ".", "--output-format", "concise"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    r = re.search(r"Found (\d+) error", ruff.stdout)
    results.append({
        "tool": "ruff",
        "ok": ruff.returncode == 0,
        "issues": int(r.group(1)) if r else 0,
    })
    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect(*, stamp: str) -> dict:
    """Gather every quality signal into one data dict."""
    tests, coverage, coverage_data = _tests_and_coverage()
    return {
        "generated_at": stamp,
        "tests": tests,
        "coverage": coverage,
        "dead_code": _dead_code(coverage_data),
        "model": _model_checks(),
        "lint": _lint(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_dashboard",
        description="Aggregate Makefile quality signals into a static D3 dashboard.",
    )
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--data-only", action="store_true", help="print JSON, no HTML")
    args = parser.parse_args(argv)

    print("build_dashboard: collecting signals (runs the suite)...", file=sys.stderr)
    data = collect(stamp=datetime.now().strftime("%Y-%m-%d %H:%M"))

    if args.data_only:
        print(json.dumps(data, indent=2))
        return 0

    from scripts.dashboard_render import render  # local import: renderer is heavy

    args.output.write_text(render(data), encoding="utf-8")
    try:
        shown = args.output.resolve().relative_to(ROOT)
    except ValueError:
        shown = args.output
    print(f"build_dashboard: wrote {shown}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
