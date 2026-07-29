"""Architectural import-boundary tests.

Enforces the module contract:

Level 1 — External consumers MUST import from package level only:
    ``from mdg_drawio.layout import Node``          ✅
    ``from mdg_drawio.layout.layout import Node``   ❌

Level 2 — Internal submodules MUST use relative imports for siblings:
    ``from ._types import Node``                    ✅
    ``from mdg_drawio.layout._types import Node``   ❌
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent.parent / "mdg_drawio"
PACKAGES = [
    "contracts",
    "generator",
    "layout",
    "notation",
]

# Regex to match absolute imports of submodules: from mdg_drawio.X.Y import ...
_ABSOLUTE_SUBMODULE_RE = re.compile(
    r"from\s+mdg_drawio\.(\w+)\.(\w+)\s+import"
)


def _find_py_files(pkg: str) -> list[Path]:
    """Return all .py files in or under a package directory."""
    pkg_dir = PACKAGE_DIR / pkg
    if not pkg_dir.is_dir():
        return []
    return sorted(pkg_dir.rglob("*.py"))


def _is_internal(file: Path, pkg: str) -> bool:
    """True if *file* lives inside the *pkg* directory."""
    try:
        file.resolve().relative_to((PACKAGE_DIR / pkg).resolve())
        return True
    except ValueError:
        return False


def _submodule_imports(source: str) -> list[tuple[str, str, str]]:
    """Return ``(package, submodule, line)`` for each absolute submodule import."""
    results: list[tuple[str, str, str]] = []
    for m in _ABSOLUTE_SUBMODULE_RE.finditer(source):
        pkg_name = m.group(1)
        sub = m.group(2)
        line_no = source[: m.start()].count("\n") + 1
        results.append((pkg_name, sub, f"line {line_no}"))
    return results


def _all_py_files() -> list[Path]:
    """Every .py file in the project, excluding tests and generated data."""
    all_files: list[Path] = []
    for py_file in PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        all_files.append(py_file)
    return sorted(all_files)


def _numeric_constant_value(node: ast.expr) -> int | float | None:
    if not isinstance(node, ast.Constant):
        return None
    if isinstance(node.value, bool):
        return None
    if isinstance(node.value, (int, float)):
        return node.value
    return None


def _module_level_numeric_assignments(file: Path) -> list[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    violations: list[str] = []
    rel = file.resolve().relative_to(PACKAGE_DIR.resolve())

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            value = _numeric_constant_value(node.value)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    violations.append(f"{rel}:{node.lineno}: {target.id}={value!r}")
        elif isinstance(node, ast.AnnAssign):
            value = _numeric_constant_value(node.value) if node.value else None
            if value is None or not isinstance(node.target, ast.Name):
                continue
            violations.append(f"{rel}:{node.lineno}: {node.target.id}={value!r}")

    return violations


# ---------------------------------------------------------------------------
# Level 1: no external consumer may import a submodule directly
# ---------------------------------------------------------------------------


def _level1_violations() -> list[str]:
    violations: list[str] = []
    all_files = _all_py_files()

    for file in all_files:
        source = file.read_text(encoding="utf-8")
        rel = file.resolve().relative_to(PACKAGE_DIR.resolve())
        for pkg_name, sub, loc in _submodule_imports(source):
            if pkg_name not in PACKAGES:
                continue
            if _is_internal(file, pkg_name):
                continue  # handled by level 2
            # Exception: notation._core is a sub-package, importing from its
            # parent package's sub-packages is fine.
            violations.append(
                f"{rel}: external import of mdg_drawio.{pkg_name}.{sub} ({loc})"
            )
    return violations


def test_no_external_submodule_imports() -> None:
    """External consumers must import from package level, never submodules."""
    violations = _level1_violations()
    assert not violations, (
        f"{len(violations)} external submodule import(s) found:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Engine orchestration constants
# ---------------------------------------------------------------------------


def test_engine_does_not_define_numeric_defaults() -> None:
    """Numeric defaults and tuning values belong in contracts, not engine."""
    engine_files = sorted((PACKAGE_DIR / "engine").glob("*.py"))
    assert engine_files, "engine package has no modules"
    violations: list[str] = []
    for engine_file in engine_files:
        violations.extend(_module_level_numeric_assignments(engine_file))
    assert not violations, (
        "engine is an orchestrator; numeric defaults must be imported from "
        "mdg_drawio.contracts instead of defined locally:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Level 2: internal submodules must use relative imports, not absolute
# ---------------------------------------------------------------------------

# Modules that use TYPE_CHECKING-only absolute imports — allowed.
_ALLOWED_ABSOLUTE_INTERNAL: set[tuple[str, str, str]] = {
    # layout/config.py has TYPE_CHECKING import of _types.Node
    ("layout", "config.py", "layout._types"),
}


def _level2_violations() -> list[str]:
    violations: list[str] = []
    for pkg in PACKAGES:
        for file in _find_py_files(pkg):
            source = file.read_text(encoding="utf-8")
            rel = str(file.resolve().relative_to(PACKAGE_DIR.resolve()))
            for pkg_name, sub, loc in _submodule_imports(source):
                if pkg_name != pkg:
                    continue  # not importing from own package
                key = (pkg, file.name, f"{pkg}.{sub}")
                if key in _ALLOWED_ABSOLUTE_INTERNAL:
                    continue
                violations.append(
                    f"{rel}: internal absolute import of mdg_drawio.{pkg_name}.{sub}"
                    f" ({loc}) — use relative import instead"
                )
    return violations


def test_no_internal_absolute_sibling_imports() -> None:
    """Internal submodules must use relative imports for siblings."""
    violations = _level2_violations()
    assert not violations, (
        f"{len(violations)} internal absolute sibling import(s) found:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Level 3: a leading-underscore *member* is module-private — never imported
# across a module boundary. (A leading-underscore *module* like `_types` is
# package-internal and MAY be imported by siblings; only its public — non-
# underscore — members should be pulled in.) If a `_name` function/class is
# imported elsewhere, the underscore is a lie: make it public or keep it home.
# ---------------------------------------------------------------------------


def _rel_parts(file: Path) -> list[str]:
    """Dotted-name parts of a file relative to the repo root (drops ``.py``)."""
    rel = file.resolve().relative_to(PACKAGE_DIR.parent.resolve())
    return list(rel.with_suffix("").parts)


def _module_names() -> set[str]:
    """Every dotted module and package name under mdg_drawio."""
    names: set[str] = set()
    for py in PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        parts = _rel_parts(py)
        if parts[-1] == "__init__":
            parts.pop()
        names.add(".".join(parts))            # the module/package itself
        names.add(".".join(parts[:-1]))       # its parent package
    return names


def _private_member_violations(
    source: str, package_parts: list[str], modules: set[str]
) -> list[tuple[str, str, int]]:
    """`(src, name, line)` for each `from X import _member` where `_member` is a
    private member (not a submodule of X). *package_parts* is the containing
    package that a relative import resolves against."""
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:  # relative import
            base = package_parts[: len(package_parts) - (node.level - 1)]
            src = ".".join([*base, node.module] if node.module else base)
        else:
            src = node.module or ""
        for alias in node.names:
            name = alias.name
            if (
                name.startswith("_")
                and not name.startswith("__")
                and f"{src}.{name}" not in modules  # a private *module* is fine
            ):
                out.append((src, name, node.lineno))
    return out


def _private_member_imports(file: Path) -> list[str]:
    raw = _rel_parts(file)
    is_init = raw[-1] == "__init__"
    dotted_parts = raw[:-1] if is_init else raw
    package_parts = dotted_parts if is_init else dotted_parts[:-1]
    rel = "/".join(raw[1:]) + ".py"
    return [
        f"{rel}:{ln}: from {src} import {name}"
        for src, name, ln in _private_member_violations(
            file.read_text(encoding="utf-8"), package_parts, _module_names()
        )
    ]


def test_no_cross_module_private_member_imports() -> None:
    """A `_`-prefixed function/class must not be imported from another module."""
    violations = [v for file in _all_py_files() for v in _private_member_imports(file)]
    assert not violations, (
        f"{len(violations)} cross-module import(s) of a private (_-prefixed) "
        "member — drop the underscore (it is shared API) or stop importing it:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Detector self-tests — prove the guards fire on known-bad input, so a broken
# detector (matching nothing) cannot make the tests above pass forever.
# ---------------------------------------------------------------------------


def test_submodule_import_detector_flags_a_known_bad_import() -> None:
    bad = "import os\nfrom mdg_drawio.layout._types import Node\n"
    assert _submodule_imports(bad) == [("layout", "_types", "line 2")]


def test_submodule_import_detector_ignores_package_level_import() -> None:
    # Package-level import (one segment after mdg_drawio) is allowed → no match.
    assert _submodule_imports("from mdg_drawio.layout import Node\n") == []


def test_private_member_import_detector_flags_and_ignores() -> None:
    pkg = ["mdg_drawio", "engine"]
    modules = {"mdg_drawio.engine.preload", "mdg_drawio.layout._types"}
    # BAD — a private member pulled from a sibling module.
    assert _private_member_violations(
        "from .preload import _preload_core\n", pkg, modules
    ) == [("mdg_drawio.engine.preload", "_preload_core", 1)]
    # OK — a public member from a private module.
    assert _private_member_violations(
        "from mdg_drawio.layout._types import Node\n", pkg, modules
    ) == []
    # OK — importing a private *module* itself (package-internal).
    assert _private_member_violations(
        "from mdg_drawio.layout import _types\n", pkg, modules
    ) == []
    # OK — dunders are not private members.
    assert _private_member_violations(
        "from __future__ import annotations\n", pkg, modules
    ) == []


def test_numeric_constant_value_detects_numbers_and_excludes_bool() -> None:
    def val(expr: str) -> int | float | None:
        return _numeric_constant_value(ast.parse(expr, mode="eval").body)

    assert val("42") == 42
    assert val("3.5") == 3.5
    assert val("True") is None  # bool is not a numeric default
    assert val("'x'") is None
