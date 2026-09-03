"""Container-view architectural invariants.

These tests enforce the contracts shown in the C4 Container diagram:

- Engine is the sole I/O boundary for registries and styles.
- CLI is a thin shell — imports only from engine.
- Core, Generator, and DSL engine have pre-load caches.
- Cross-service imports are restricted to allowed symbols only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent.parent / "mdg_drawio"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_module(rel_path: str) -> str:
    return (PACKAGE_DIR / rel_path).read_text(encoding="utf-8")


def _has_module_level_attr(path: str, name: str) -> bool:
    """Check if *path* has a module-level assignment to *name*."""
    tree = ast.parse(_read_module(path))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name)
                    and node.target.id == name):
                return True
    return False


def _has_function(path: str, name: str) -> bool:
    """Check if *path* defines a function called *name*."""
    tree = ast.parse(_read_module(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return True
    return False


def _cross_imports(path: str) -> dict[str, set[str]]:
    """Return ``{imported_module: {imported_names}}`` for cross-package imports.

    Cross-package = importing from a different mdg_drawio sub-package.
    """
    own_pkg = path.split("/")[0]
    tree = ast.parse(_read_module(path))
    result: dict[str, set[str]] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("mdg_drawio"):
            continue
        parts = node.module.split(".")
        if len(parts) < 2:
            continue
        imported_pkg = parts[1]
        if imported_pkg == own_pkg:
            continue  # same-package import — allowed
        names = {a.name for a in node.names}
        result.setdefault(node.module, set()).update(names)
    return result


def _top_level_imports_from(path: str) -> set[str]:
    """Return top-level packages imported from path (e.g. 'mdg_drawio')."""
    tree = ast.parse(_read_module(path))
    pkgs: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                pkgs.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                pkgs.add(node.module.split(".")[0])
    return pkgs


def _mdg_subpackages(source: str) -> set[str]:
    """Return the ``mdg_drawio.<pkg>`` sub-packages imported anywhere in *source*.

    Resolves the SECOND path segment, so it can tell ``engine`` from ``layout``
    — unlike ``_top_level_imports_from`` which collapses everything to
    ``mdg_drawio``. Walks the whole tree (function-body and TYPE_CHECKING imports
    count too).
    """
    tree = ast.parse(source)
    subpkgs: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for mod in modules:
            parts = mod.split(".")
            if parts[0] == "mdg_drawio" and len(parts) >= 2:
                subpkgs.add(parts[1])
    return subpkgs


# ---------------------------------------------------------------------------
# Invariant 1: CLI is a thin shell
# ---------------------------------------------------------------------------

CLI_ALLOWED_IMPORTS = {
    "mdg_drawio",   # from mdg_drawio.engine import convert
    "argparse",
    "os",           # MDG_DEBUG env var gates the traceback
    "sys",
    "collections",
    "pathlib",
    "__future__",
}


def test_cli_is_thin_shell() -> None:
    """CLI must only import from engine, not from other mdg_drawio packages."""
    source = _read_module("cli.py")

    # The load-bearing check: of the mdg_drawio sub-packages, CLI may touch only
    # `engine`. (`_top_level_imports_from` cannot see this — it collapses every
    # mdg_drawio.* import to "mdg_drawio".)
    subpkgs = _mdg_subpackages(source)
    assert subpkgs <= {"engine"}, (
        f"CLI must delegate through engine only, but imports mdg_drawio "
        f"sub-package(s): {subpkgs - {'engine'}}"
    )

    # Top-level allowlist: no non-stdlib third-party or unexpected packages.
    unexpected = _top_level_imports_from("cli.py") - CLI_ALLOWED_IMPORTS
    assert not unexpected, (
        f"CLI imports from unexpected packages: {unexpected}. "
        f"CLI must only import from engine + stdlib."
    )


def test_thin_shell_guard_rejects_a_sibling_import() -> None:
    """The guard itself must reject a non-engine sub-package import.

    Without this, a bug in `_mdg_subpackages` (e.g. collapsing to the top-level
    name) would let `test_cli_is_thin_shell` pass forever.
    """
    bad = (
        "from mdg_drawio.engine import convert\n"
        "from mdg_drawio.layout import Config\n"
    )
    assert _mdg_subpackages(bad) == {"engine", "layout"}
    assert not (_mdg_subpackages(bad) <= {"engine"})


# ---------------------------------------------------------------------------
# Invariant 2: Engine has required entry points
# ---------------------------------------------------------------------------

# Engine is a package; each required function lives in its own module.
ENGINE_REQUIRED_FUNCTIONS = {
    "engine/preload.py": "preload_core",
    "engine/convert.py": "convert",
    "engine/validate.py": "validate_generated_xml",
}


def test_engine_has_entry_points() -> None:
    """Engine modules must define their required functions."""
    for path, func in ENGINE_REQUIRED_FUNCTIONS.items():
        assert _has_function(path, func), (
            f"{path} missing required function: {func}"
        )


# ---------------------------------------------------------------------------
# Invariant 3: Pre-load caches exist
# ---------------------------------------------------------------------------

# Notation core still uses the pre-load cache+setter pattern (it feeds parsing).
PRELOAD_MODULES: dict[str, dict[str, str]] = {
    "notation/_core/registry.py":
        {"cache": "_registries", "setter": "set_registries"},
}


def test_preload_caches_exist() -> None:
    """Modules that receive pre-loaded data must have a cache and setter."""
    for path, attrs in PRELOAD_MODULES.items():
        assert _has_module_level_attr(path, attrs["cache"]), (
            f"{path}: missing pre-load cache '{attrs['cache']}'"
        )
        assert _has_function(path, attrs["setter"]), (
            f"{path}: missing pre-load setter '{attrs['setter']}'"
        )


def test_generator_uses_injected_style_provider() -> None:
    """Generator is globals-free: it exposes a StyleProvider port + factory and
    holds no module-global data cache (data is injected, like layout)."""
    assert _has_function("generator/generator.py", "create_style_provider"), (
        "generator/generator.py: missing create_style_provider factory"
    )
    for cache in ("_styles", "_registries", "_overrides"):
        assert not _has_module_level_attr("generator/generator.py", cache), (
            f"generator/generator.py: unexpected global cache '{cache}' "
            "(data should be injected via StyleProvider)"
        )


# Only the notation parse path may hold mutable module-global state (it pre-loads
# registries/styles into caches). Every other module must receive data via an
# injected port (StyleProvider / SizeResolver) — see generator (globals-free) and
# layout. Adding a module here is a deliberate exception, not a default.
_GLOBAL_STATE_ALLOWED = {
    "notation/_core/registry.py",  # the single registry pre-load cache (parse path)
}


def _mutates_module_global(rel_path: str) -> bool:
    """True if any function in *rel_path* rebinds a module global (``global x``).

    A ``global`` statement is the tell-tale of the mutable-singleton + setter
    pattern; injected data never needs it.
    """
    tree = ast.parse(_read_module(rel_path))
    return any(isinstance(node, ast.Global) for node in ast.walk(tree))


def test_data_flows_by_injection_not_module_globals() -> None:
    """Drift guard: consumers must receive data by injection, not global caches.

    Only the whitelisted notation parse path may mutate module-global state; any
    other module doing so has reintroduced the fragile setter pattern this
    architecture avoids (the class of bug where a consumer is left un-wired).
    """
    offenders = [
        rel
        for py in sorted(PACKAGE_DIR.rglob("*.py"))
        if "__pycache__" not in (rel := str(py.relative_to(PACKAGE_DIR)))
        and rel not in _GLOBAL_STATE_ALLOWED
        and _mutates_module_global(rel)
    ]
    assert not offenders, (
        "these modules mutate module-global state instead of using dependency "
        f"injection (StyleProvider/SizeResolver): {offenders}. Inject the data, "
        "or add the module to _GLOBAL_STATE_ALLOWED with justification."
    )


# ---------------------------------------------------------------------------
# Invariant 4: Cross-service imports are restricted to allowed symbols
# ---------------------------------------------------------------------------

# For each (file, imported_module), the set of allowed imported names.
SYMBOL_ALLOWLIST: dict[str, dict[str, set[str]]] = {
    # --- Generator ---
    "generator/generator.py": {
        "mdg_drawio.contracts": {
            "CANVAS_DX", "CANVAS_DY",
            "DEFAULT_NODE_HEIGHT", "DEFAULT_NODE_WIDTH",
            "PAGE_CELL_ID", "PALETTE_MODE", "ROOT_CELL_ID",
            "Anchor", "ChildCell", "Document", "Edge", "GeometryChild", "Node",
            "NodeChildCell",
            "derived_edge_id",
            "index_shapes_by_function",
        },
    },
    "generator/overlay.py": {
        "mdg_drawio.contracts": {
            "PAGE_CELL_ID", "ROOT_CELL_ID",
            "EdgeAnchorOverlay", "GeometryOverlay",
        },
    },

    # --- Layout ---
    "layout/_types.py": {
        "mdg_drawio.contracts": {"Edge", "GeometryPoint", "Node"},
    },
    "layout/config.py": {
        "mdg_drawio.contracts": {
            "DEFAULT_MARGIN_X", "DEFAULT_MARGIN_Y",
            "DEFAULT_TOP_PADDING", "DEFAULT_BOTTOM_PADDING",
            "BoundaryPadding",
        },
    },
    "layout/layered.py": {
        "mdg_drawio.contracts": {"Anchor", "DEFAULT_PAGE_HEIGHT", "DEFAULT_PAGE_WIDTH"},
    },
    "layout/sequence.py": {
        "mdg_drawio.contracts": {"DEFAULT_PAGE_HEIGHT", "DEFAULT_PAGE_WIDTH"},
    },
    "layout/palette.py": {
        "mdg_drawio.contracts": {"PALETTE_DEFAULT_PAGE_HEIGHT",
                                 "PALETTE_DEFAULT_PAGE_WIDTH",
                                 "PALETTE_MODE"},
    },
    "layout/size_resolver.py": {
        "mdg_drawio.contracts": {
            "DEFAULT_NODE_HEIGHT", "DEFAULT_NODE_WIDTH",
            "index_shapes_by_function",
        },
    },

    # --- Notation ---
    "notation/_core/dsl_engine.py": {
        "mdg_drawio.contracts": {
            "PAGE_PREFIX_LENGTH", "QUOTE_OFFSET",
            "Diagram", "Document", "Edge", "MultiPageDocument", "Node",
            "derived_edge_id",
        },
    },
    "notation/_core/registry.py": {
        "mdg_drawio.contracts": {"index_shapes_by_function"},
    },
    "notation/c4/__init__.py": {
        "mdg_drawio.contracts": {
            "C4_SCALER_SUBTITLE_KEY",
            "Diagram", "Document", "Edge", "MultiPageDocument", "Node",
            "derived_edge_id",
        },
    },
}

def test_cross_service_imports_are_valid_symbols() -> None:
    """Every cross-package import must only use allowed symbols."""
    violations: list[str] = []
    for path, allowed_by_module in SYMBOL_ALLOWLIST.items():
        actual_imports = _cross_imports(path)
        for module, names in actual_imports.items():
            if module not in allowed_by_module:
                violations.append(
                    f"{path}: imports from unexpected module {module}"
                )
                continue
            unexpected = names - allowed_by_module[module]
            if unexpected:
                violations.append(
                    f"{path}: from {module} imports unexpected symbols: "
                    f"{sorted(unexpected)}"
                )
    # Also check: any file NOT in the allowlist must have zero cross-package imports
    for rel_path in sorted(_all_non_engine_source_files()):
        if rel_path in SYMBOL_ALLOWLIST:
            continue
        cross = _cross_imports(rel_path)
        if cross:
            violations.append(
                f"{rel_path}: has cross-package imports {dict(cross)} "
                f"but is not in SYMBOL_ALLOWLIST"
            )
    assert not violations, (
        f"{len(violations)} cross-service import violation(s):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_symbol_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted symbol must correspond to a real import.

    The allowlist only *permits*, so an entry for a symbol a module stopped
    importing (or never imported) sits there passing forever — which is how
    ``layout/_container_layout.py`` came to declare ``MIN_CONTAINER_WIDTH``,
    ``MIN_CONTAINER_HEIGHT`` and ``DEFAULT_PAGE_MARGIN`` it never used, and
    how those three constants stayed alive in ``contracts`` with no consumer
    at all. Checking the reverse direction keeps the allowlist an accurate
    description of the import graph instead of a wish list.
    """
    stale: list[str] = []
    for path, allowed_by_module in SYMBOL_ALLOWLIST.items():
        actual_imports = _cross_imports(path)
        if not actual_imports:
            stale.append(f"{path}: allowlisted but has no cross-package imports")
            continue
        for module, allowed_names in allowed_by_module.items():
            unused = allowed_names - actual_imports.get(module, set())
            if unused:
                stale.append(
                    f"{path}: allows symbols from {module} it does not "
                    f"import: {sorted(unused)}"
                )
    assert not stale, (
        f"{len(stale)} stale allowlist entr(ies) — delete them (and any "
        f"constant left with no consumer):\n"
        + "\n".join(f"  {s}" for s in stale)
    )


# ---------------------------------------------------------------------------
# Invariant 5: No cross-package imports between generator, layout, notation
# ---------------------------------------------------------------------------

_SERVICE_PACKAGES = {"generator", "layout", "notation"}
_ORCHESTRATOR_FILES = {"__main__.py", "cli.py"}


def _is_orchestrator(rel: str) -> bool:
    """Orchestrators wire the service packages together and are import-exempt.

    The engine is a package (``engine/``); every module in it is an
    orchestrator. ``reverse/`` is exempt for the same reason: deriving a
    shape and merging into a ``.mdg`` inherently means reaching into
    ``notation`` (parsing, registry lookup) -- it is engine's counterpart
    for the reverse direction, not a peer of generator/layout/notation that
    should stay siloed.
    """
    return (
        rel in _ORCHESTRATOR_FILES
        or rel.startswith("engine/")
        or rel.startswith("reverse/")
    )


def _cross_package_imports(path: str) -> list[tuple[str, int, str]]:
    """Return ``(package, line, line_text)`` for each cross-package import."""
    source = (PACKAGE_DIR / path).read_text(encoding="utf-8")
    results: list[tuple[str, int, str]] = []
    for i, line in enumerate(source.splitlines(), 1):
        m = re.match(r"from\s+mdg_drawio\.(\w+)\s+import", line)
        if m:
            results.append((m.group(1), i, line.strip()))
    return results


def test_no_cross_package_imports() -> None:
    """Non-orchestrator files must not import from other service packages.

    Only ``contracts`` and own-package imports are allowed. Orchestrators
    (engine, cli, __main__) are exempt — they wire everything together.
    """
    violations: list[str] = []
    for py_file in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = str(py_file.relative_to(PACKAGE_DIR))
        if _is_orchestrator(rel) or rel.endswith("__init__.py"):
            continue

        own_pkg = rel.split("/")[0] if "/" in rel else ""

        for pkg, line, text in _cross_package_imports(rel):
            if pkg == "contracts":
                continue
            if pkg == own_pkg:
                continue
            violations.append(f"{rel}:{line}: {text}")

    assert not violations, (
        f"{len(violations)} cross-package import violation(s) — "
        f"only contracts and own-package imports are allowed:\n"
        + "\n".join(f"  {v}" for v in violations)
    )

def _all_non_engine_source_files() -> list[str]:
    """Return all .py files in mdg_drawio except orchestrator/entry points."""
    files: list[str] = []
    for py_file in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = str(py_file.relative_to(PACKAGE_DIR))
        if _is_orchestrator(rel):
            continue
        if rel.endswith("__init__.py"):
            continue
        files.append(rel)
    return files
