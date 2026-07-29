"""Test the unified model-consistency entry point (scripts/check_model.py)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECK_MODEL = ROOT / "scripts" / "check_model.py"


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_MODEL)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_check_model() -> Any:
    """Import check_model.py as a module (typed Any for dynamic attr access)."""
    spec = importlib.util.spec_from_file_location("check_model", CHECK_MODEL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_is_consistent() -> None:
    """The committed architecture model must pass every checker."""
    result = _run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All model checks passed" in result.stdout


def test_model_check_aggregates_every_checker() -> None:
    """The report must cover import integrity, traceability, and the lockfile."""
    stdout = _run().stdout

    assert "import integrity" in stdout
    assert "traceability" in stdout
    assert "code-view lockfile" in stdout
    assert "traceability matrix" in stdout


def test_model_check_propagates_a_checker_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing sub-checker must make the aggregator exit 1 and print [FAIL].

    Without this, a `check_model` that always returned 0 (or never propagated a
    sub-check's non-zero exit) would still satisfy the two tests above, since the
    labels are printed for PASS and FAIL alike.
    """
    cm = _load_check_model()
    passing = cm.Check("synthetic passing check", ["-c", "pass"])
    failing = cm.Check("synthetic failing check", ["-c", "import sys; sys.exit(1)"])
    cm.CHECKS = (passing, failing)

    returncode = cm.main()
    out = capsys.readouterr().out

    assert returncode == 1
    assert "[PASS] synthetic passing check" in out
    assert "[FAIL] synthetic failing check" in out
    assert "model checks failed" in out
