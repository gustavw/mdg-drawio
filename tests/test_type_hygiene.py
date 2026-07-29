"""Guardrail: the type checker is never silenced with inline bypass comments.

mypy runs strict (``disallow_untyped_defs``, ``warn_unused_ignores``, …). A
``# type: ignore`` (or ``# mypy: ignore``) comment hides a real type error rather
than fixing it, so the project bans them outright: fix the type, narrow with an
``isinstance``/``assert``, or adjust the annotation instead.

``warn_unused_ignores`` only catches ignores that have become redundant; it does
not stop a new, load-bearing one from being added. This test does.

If a third-party stub gap ever makes an ignore genuinely unavoidable, that is a
deliberate decision — remove this test (or scope it) in the same change, with the
reason in the commit, rather than slipping a silent bypass past the gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ROOTS = ("mdg_drawio", "scripts", "tests")
# Assembled from fragments so this guard file does not match itself.
_BYPASS = re.compile(r"#\s*(?:type|mypy)\s*:\s*ignore")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for top in _ROOTS:
        files += sorted((_ROOT / top).rglob("*.py"))
    return files


def test_no_mypy_bypass_comments() -> None:
    offenders: list[str] = []
    for path in _python_files():
        if path.resolve() == Path(__file__).resolve():
            continue  # this guard names the pattern in its own docstring
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BYPASS.search(line):
                offenders.append(f"{path.relative_to(_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "mypy bypass comments are banned — fix the type instead of silencing it:\n"
        + "\n".join(offenders)
    )


def test_guard_pattern_actually_matches() -> None:
    """The guard is only meaningful if its pattern catches the real forms."""
    assert _BYPASS.search("x = y  # type: ignore[arg-type]")
    assert _BYPASS.search("z = w  # type:ignore")
    assert not _BYPASS.search("x = 1  # a normal comment")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
