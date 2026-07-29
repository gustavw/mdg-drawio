"""Architectural test: no undocumented magic numbers in code logic."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent.parent / "mdg_drawio"
_ALLOWED = frozenset({0, 1, 2, 3, -1, 0.5, 4.0, 120, 60})


def _is_dataclass_decorator(dec: ast.expr) -> bool:
    if isinstance(dec, ast.Name) and dec.id == "dataclass":
        return True
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
        return dec.func.id == "dataclass"
    return False


def _is_named_constant(name: str) -> bool:
    """True for UPPER_CASE or _private module-level constants."""
    return name.isupper() or name.startswith("_")


def _has_constant_name(tree: ast.AST, val: int | float) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_named_constant(target.id):
                    if isinstance(node.value, ast.Constant) and node.value.value == val:
                        return True
    return False


def _find_magic_numbers(filepath: Path) -> list[str]:
    rel = str(filepath.relative_to(PACKAGE_DIR.parent))
    if "constants.py" in rel:
        return []
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        return _scan_source(source, rel)
    except SyntaxError:
        return []


def _scan_source(source: str, rel: str) -> list[str]:
    """Report undocumented magic-number literals in *source* (rel = label)."""
    tree = ast.parse(source)

    dataclass_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                if _is_dataclass_decorator(dec):
                    for body_node in ast.walk(node):
                        if hasattr(body_node, "lineno"):
                            dataclass_lines.add(body_node.lineno)
                    break

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, (int, float)):
            continue
        val = node.value
        if val in _ALLOWED:
            continue
        if node.lineno in dataclass_lines:
            continue
        if _has_constant_name(tree, val):
            continue
        violations.append(f"{rel}:{node.lineno}: {val}")
    return violations


def test_no_undocumented_magic_numbers() -> None:
    """Meaningful numeric literals must be named or exempt."""
    all_violations: list[str] = []
    for py_file in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        all_violations.extend(_find_magic_numbers(py_file))
    assert not all_violations, (
        f"{len(all_violations)} undocumented magic number(s) found:\n"
        + "\n".join(f"  {v}" for v in sorted(all_violations))
    )


def test_detector_flags_a_real_magic_number() -> None:
    """Self-test: an inline literal with no name and no exemption is reported.

    Guards against a detector that silently matches nothing (which would make
    the suite above pass forever).
    """
    src = "def f(x):\n    return x * 999\n"
    assert _scan_source(src, "fake.py") == ["fake.py:2: 999"]


def test_detector_respects_named_constants_and_allowlist() -> None:
    """A named constant and small allowed numbers are not flagged."""
    src = "GAP = 999\n\n\ndef f(x):\n    return x * 2\n"
    assert _scan_source(src, "fake.py") == []
