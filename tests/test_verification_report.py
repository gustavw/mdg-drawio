"""Unit tests for the verification report's coverage→Component mapping.

Uses a fabricated coverage.json shape so it never runs a nested test suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "scripts" / "verification_report.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verification_report", REPORT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["verification_report"] = module
    spec.loader.exec_module(module)
    return module


def _coverage_data() -> dict:
    return {
        "files": {
            # two modules realizing types_co are aggregated into one Component
            "mdg_drawio/layout/_types.py": {
                "summary": {"covered_lines": 8, "num_statements": 10}
            },
            "mdg_drawio/layout/layout.py": {
                "summary": {"covered_lines": 2, "num_statements": 10}
            },
            "mdg_drawio/layout/layered.py": {
                "summary": {"covered_lines": 5, "num_statements": 5}
            },
            # unmapped file (no realizing Component) is ignored
            "mdg_drawio/__init__.py": {
                "summary": {"covered_lines": 0, "num_statements": 3}
            },
        }
    }


def test_coverage_aggregates_by_component() -> None:
    module = _load()
    coverage = module.component_coverage(_coverage_data())

    # types_co aggregates _types.py (8/10) + layout.py (2/10) = 10/20 = 50%
    assert coverage["types_co"].covered == 10
    assert coverage["types_co"].statements == 20
    assert coverage["types_co"].pct == 50.0
    # fully covered single-module Component
    assert coverage["layered_co"].pct == 100.0


def test_unmapped_files_are_ignored() -> None:
    module = _load()
    coverage = module.component_coverage(_coverage_data())

    # __init__.py maps to no Component and must not appear
    assert all(not c.startswith("__init__") for c in coverage)
    assert set(coverage) == {"types_co", "layered_co"}


def test_failing_test_run_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing pytest run must not be reported as passing verification."""
    module = _load()

    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        # The first subprocess (the coverage-instrumented pytest run) fails;
        # the JSON export must never be reached.
        assert "pytest" in cmd, "coverage json export should not run after failure"
        return subprocess.CompletedProcess(cmd, returncode=1)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as excinfo:
        module._run_coverage()

    assert excinfo.value.code == 1
