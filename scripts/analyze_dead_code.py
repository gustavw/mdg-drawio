#!/usr/bin/env python3
"""Dead-code report: two signals over every ``mdg_drawio/**/*.py`` definition.

Statically parses every ``class`` / ``def`` (the "universe") and classifies each
against two independent signals:

1. **Runtime reachability** — did any CLI action permutation, or the regression
   suite, *execute* it (:mod:`scripts.trace_actions`)?
2. **Static call-graph reachability** — starting from the executed ("live")
   definitions, can it be reached by following reference edges (one definition
   mentioning or nesting another)?

Crossing them separates genuinely-dead code from mere coverage gaps::

    untouched + NOT reachable from live -> TRULY DEAD (dead island; delete)
    untouched + reachable from live     -> UNCOVERED  (gap; add a fixture)
    executed                            -> reachable  (not reported)

Definition keys mirror ``code.co_qualname`` exactly — including the ``<locals>``
segment CPython inserts for nested functions — so a statically parsed key
compares directly against a traced one. A class counts as reached when any of
its methods (or ``__init__``) is touched.

**Blind spots (why "truly dead" is a strong hint, not a proof).** Both signals
are heuristics over a dynamic language:

* Call-graph edges match on the *leaf* name, so ``foo()`` links to every
  definition named ``foo`` — an island sharing a name with live code looks
  reachable. This *under*-reports dead islands (safe direction for deletions).
* Neither signal follows dynamic dispatch: ``getattr``, string-keyed registries,
  or ``__import__(f"...{notation}.layout")``. Code reached only that way, for an
  input the fixtures lack, can look truly-dead while being live. The notation
  layout modules are the prime example.
* Dunders (``__eq__``, ``__post_init__``), ``@property``, and dataclass-generated
  methods are called implicitly; public API may have only external callers.
  ``__all__`` re-exports are treated as surface, not use, so unused public API
  is reported and annotated ``(exported public API)`` for human judgement.

So confirm a "truly dead" hit by eye before deleting; treat "uncovered" as a
prompt to add a fixture, not to delete.

Advisory by design: always exits 0. Known-legitimate unreached definitions live
in :mod:`scripts.dead_code_allowlist`.

Usage::

    python scripts/analyze_dead_code.py                  # trace in-process, report
    python scripts/analyze_dead_code.py --full            # exhaustive permutations
    python scripts/analyze_dead_code.py --trace-json t.json  # reuse an artifact
    python scripts/analyze_dead_code.py --json            # machine-readable output
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "mdg_drawio"

# Allow direct invocation (``python scripts/analyze_dead_code.py``) where only
# ``scripts/`` — not the repo root — lands on sys.path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dead_code_allowlist import ALLOWLIST
from scripts.trace_actions import (
    run_all,
    touched_union,
    trace_regression_suite,
)


# ---------------------------------------------------------------------------
# Static universe of definitions
# ---------------------------------------------------------------------------


def _module_name(path: Path) -> str:
    """Dotted import name for a file, matching ``frame.f_globals['__name__']``."""
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _iter_scope_defs(
    node: ast.AST, prefix: str, in_function: bool, module: str
) -> Iterator[tuple[str, str, ast.AST]]:
    """Yield ``(module:qualname, kind, node)`` for every ``def``/``class``.

    Recurses through all child nodes but only ``def``/``class`` introduce a new
    scope — so nested definitions inside ``if``/``for``/``try`` blocks keep the
    qualname of their enclosing function or class. A ``<locals>`` segment is
    inserted whenever descending into a function body, mirroring CPython.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not prefix:
                qual = child.name
            elif in_function:
                qual = f"{prefix}.<locals>.{child.name}"
            else:
                qual = f"{prefix}.{child.name}"
            kind = "class" if isinstance(child, ast.ClassDef) else "function"
            yield f"{module}:{qual}", kind, child
            is_func = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            yield from _iter_scope_defs(child, qual, is_func, module)
        else:
            yield from _iter_scope_defs(child, prefix, in_function, module)


@lru_cache(maxsize=1)
def collect_definitions() -> dict[str, str]:
    """Map every ``module:qualname`` in ``mdg_drawio/`` to ``class``/``function``.

    Cached (parsed once per process); callers treat the result as read-only.
    """
    universe: dict[str, str] = {}
    for path in sorted(PKG_DIR.rglob("*.py")):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for key, kind, _node in _iter_scope_defs(tree, "", False, module):
            universe[key] = kind
    return universe


def covered_definitions(coverage_data: dict) -> set[str]:
    """Function definitions whose body executed, per a ``coverage json`` report.

    A ``module:qualname`` is "reached" if any line in the function body appears
    in that file's executed lines. Class bodies run at import, so only functions
    are considered — a class is picked up through its executed methods by the
    caller's class-reachability rule. Reuses the same qualname walk as the
    universe, so the keys line up exactly.
    """
    executed: dict[str, set[int]] = {}
    for fpath, entry in coverage_data.get("files", {}).items():
        executed[fpath.replace("\\", "/")] = set(entry.get("executed_lines", []))

    reached: set[str] = set()
    for path in sorted(PKG_DIR.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        lines = executed.get(rel) or next(
            (v for k, v in executed.items() if k.endswith(rel)), None
        )
        if not lines:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for key, kind, node in _iter_scope_defs(tree, "", False, _module_name(path)):
            if kind != "function" or not isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            body_start = node.body[0].lineno if node.body else node.lineno
            end = node.end_lineno or body_start
            if not lines.isdisjoint(range(body_start, end + 1)):
                reached.add(key)
    return reached


# ---------------------------------------------------------------------------
# Static call-graph reachability (the second signal)
# ---------------------------------------------------------------------------
#
# The runtime trace answers "was this executed by the sweep?". On its own that
# conflates genuinely-dead code with code the fixtures simply do not exercise.
# So we ask a sharper static question: starting from the definitions that ARE
# executed at runtime (the live seeds), can we reach an unexecuted definition by
# following reference edges — one definition mentioning another's name, or
# containing it? If yes it is live-but-uncovered (a coverage gap); if no it is a
# dead island — nothing that runs can ever call it.
#
#   untouched + reachable-from-live   -> UNCOVERED  (gap; add a fixture)
#   untouched + NOT reachable-from-live -> TRULY DEAD (island; delete)
#
# Edges are matched on the *leaf* name, so ``foo()`` links to every definition
# named ``foo``. That OVER-connects (a dead island sharing a name with live code
# looks reachable), so the check UNDER-reports dead islands — the safe direction
# for a signal that suggests deletions. It also cannot follow dynamic dispatch
# (getattr, string keys, ``__import__(f"...{notation}...")``); code reached only
# that way needs an allowlist entry. See the module docstring / README.


def _leaf(key: str) -> str:
    """The bare symbol name from a ``module:Qual.name`` key."""
    return key.split(":", 1)[1].rsplit(".", 1)[-1]


def _scope_reference_names(node: ast.AST) -> set[str]:
    """Names referenced in *node*'s own body, not descending into nested defs."""
    names: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue  # a nested definition is its own scope
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        names |= _scope_reference_names(child)
    return names


def _add_edges(
    node: ast.AST,
    prefix: str,
    in_function: bool,
    module: str,
    name_index: dict[str, set[str]],
    edges: dict[str, set[str]],
) -> None:
    """Populate *edges*: caller -> callees it references, and parent -> nested."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not prefix:
                qual = child.name
            elif in_function:
                qual = f"{prefix}.<locals>.{child.name}"
            else:
                qual = f"{prefix}.{child.name}"
            key = f"{module}:{qual}"
            targets = edges.setdefault(key, set())
            if prefix:  # containment: enclosing scope reaches this definition
                edges.setdefault(f"{module}:{prefix}", set()).add(key)
            for name in _scope_reference_names(child):
                targets |= name_index.get(name, set())
            _add_edges(
                child, qual,
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef),
                module, name_index, edges,
            )
        else:
            _add_edges(child, prefix, in_function, module, name_index, edges)


@lru_cache(maxsize=1)
def build_reference_graph() -> dict[str, set[str]]:
    """Reference edges between ``mdg_drawio`` definitions (leaf-name resolved).

    Cached (parsed once per process); callers treat the result as read-only.
    """
    name_index: dict[str, set[str]] = {}
    for key in collect_definitions():
        name_index.setdefault(_leaf(key), set()).add(key)
    edges: dict[str, set[str]] = {}
    for path in sorted(PKG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _add_edges(tree, "", False, _module_name(path), name_index, edges)
    return edges


def _seed_keys(runtime_reached: set[str], universe: dict[str, str]) -> set[str]:
    """Live definitions to seed reachability from — executed defs and, when a
    method executed, its enclosing class."""
    seeds = {key for key in runtime_reached if key in universe}
    for key in runtime_reached:
        module, _, qual = key.partition(":")
        if "." in qual:
            class_key = f"{module}:{qual.rsplit('.', 1)[0]}"
            if class_key in universe:
                seeds.add(class_key)
    return seeds


def reachable_from(seeds: set[str], edges: dict[str, set[str]]) -> set[str]:
    """Transitive closure of *seeds* over the reference graph."""
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        for nxt in edges.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


@lru_cache(maxsize=1)
def collect_exported() -> set[str]:
    """Leaf names listed in any ``__all__`` (for annotating dead public API)."""
    exported: set[str] = set()
    for path in (ROOT / "mdg_drawio").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ) and isinstance(node.value, ast.List | ast.Tuple):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        exported.add(elt.value)
    return exported


# ---------------------------------------------------------------------------
# Runtime reachability
# ---------------------------------------------------------------------------


def _class_reached(key: str, reached: set[str]) -> bool:
    """A class is reached if it or any of its members (methods) is reached."""
    if key in reached:
        return True
    member_prefix = key + "."
    return any(r.startswith(member_prefix) for r in reached)


def _reached(key: str, kind: str, reached: set[str]) -> bool:
    """Whether *key* is reached by a runtime signal (class = any member)."""
    return _class_reached(key, reached) if kind == "class" else key in reached


def unreached_definitions(
    universe: dict[str, str], touched: set[str]
) -> dict[str, str]:
    """Definitions in the universe that no permutation touched."""
    return {
        key: kind
        for key, kind in universe.items()
        if not _reached(key, kind, touched)
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class Report:
    universe_size: int
    touched_size: int
    truly_dead: dict[str, str]  # unreached + unreachable from live code (dead island)
    uncovered: dict[str, str]  # unreached but reachable from live code (coverage gap)
    test_reached: dict[str, str]  # CLI-unreached but the regression suite runs it
    exported: set[str]  # subset of truly_dead that is in some __all__ (public surface)
    allowlisted_dead: dict[str, str]  # CLI-untouched and on the allowlist
    stale_allowlist: dict[str, str]  # allowlisted but actually touched (or unknown)

    @property
    def reachable_pct(self) -> float:
        unreached = (
            len(self.truly_dead) + len(self.uncovered) + len(self.allowlisted_dead)
        )
        reached = self.universe_size - unreached
        return 100.0 * reached / self.universe_size if self.universe_size else 0.0


def build_report(
    universe: dict[str, str],
    touched: set[str],
    edges: dict[str, set[str]],
    exported: set[str] | None = None,
    also_reached: set[str] | None = None,
) -> Report:
    """Diff the universe against the runtime + call-graph signals.

    The allowlist partition and staleness check use the CLI ``touched`` set
    only, so they stay consistent with the fast test gate. ``also_reached`` (the
    regression-suite trace, optional) is folded into the live seeds. Each
    remaining CLI-unreached definition is then classified:

        executed by the suite         -> test_reached (CLI-uncovered but live)
        else reachable from live code  -> uncovered    (coverage gap; add a test)
        else                           -> truly_dead   (dead island; delete)
    """
    exported = exported or set()
    also_reached = also_reached or set()
    live = touched | also_reached
    reachable = reachable_from(_seed_keys(live, universe), edges)
    dead = unreached_definitions(universe, touched)

    allowlisted_dead: dict[str, str] = {}
    truly_dead: dict[str, str] = {}
    uncovered: dict[str, str] = {}
    test_reached: dict[str, str] = {}
    exported_dead: set[str] = set()
    for key, kind in dead.items():
        if key in ALLOWLIST:
            allowlisted_dead[key] = kind
        elif _reached(key, kind, also_reached):
            test_reached[key] = kind
        elif _reached(key, kind, reachable):
            uncovered[key] = kind
        else:
            truly_dead[key] = kind
            if _leaf(key) in exported:
                exported_dead.add(key)

    # An allowlist entry is stale if it names something that is not a real
    # definition, or one the CLI sweep actually reaches.
    stale: dict[str, str] = {}
    for key in ALLOWLIST:
        if key not in universe:
            stale[key] = "not a definition"
        elif key not in dead:
            stale[key] = "actually touched"

    return Report(
        universe_size=len(universe),
        touched_size=len(touched),
        truly_dead=truly_dead,
        uncovered=uncovered,
        test_reached=test_reached,
        exported=exported_dead,
        allowlisted_dead=allowlisted_dead,
        stale_allowlist=stale,
    )


def _print_section(header: str, items: dict[str, str], exported: set[str]) -> None:
    """Print one ``[kind] key`` section, or nothing when *items* is empty."""
    if not items:
        return
    print(header.format(n=len(items)))
    for key in sorted(items):
        tag = "  (exported public API)" if key in exported else ""
        print(f"  [{items[key]:8}] {key}{tag}")
    print()


def _print_report(report: Report) -> None:
    print("Dead-code report (action-permutation reachability)")
    print("=" * 55)
    print(f"  definitions (mdg_drawio/): {report.universe_size}")
    print(f"  symbols touched by sweep : {report.touched_size}")
    print(f"  reachable                : {report.reachable_pct:.1f}%")
    print()

    if not report.truly_dead:
        print("No truly-dead definitions outside the allowlist.\n")
    _print_section(
        "TRULY DEAD ({n}) -- untouched AND unreferenced:",
        report.truly_dead, report.exported,
    )
    _print_section(
        "UNCOVERED ({n}) -- referenced but NO runtime path (CLI or tests) runs it; "
        "add a test or delete:",
        report.uncovered, set(),
    )
    _print_section(
        "Reached by tests ({n}) -- no CLI permutation runs it, but the regression "
        "suite does (alive):",
        report.test_reached, set(),
    )

    if report.allowlisted_dead:
        print(f"Allowlisted ({len(report.allowlisted_dead)}):")
        for key in sorted(report.allowlisted_dead):
            print(f"  {key}  -> {ALLOWLIST[key]}")

    if report.stale_allowlist:
        print()
        print(f"STALE allowlist entries ({len(report.stale_allowlist)}) -- remove:")
        for key in sorted(report.stale_allowlist):
            print(f"  {key}  ({report.stale_allowlist[key]})")


def _load_touched(args: argparse.Namespace) -> set[str]:
    """Get the touched union, from an artifact or a fresh in-process sweep."""
    if args.trace_json:
        data = json.loads(Path(args.trace_json).read_text(encoding="utf-8"))
        return set(data["touched_union"])
    return touched_union(run_all(full=args.full, quiet=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze_dead_code",
        description="Report mdg_drawio definitions no action permutation touches.",
    )
    parser.add_argument(
        "--trace-json", type=str, default=None,
        help="Reuse a trace artifact instead of tracing in-process",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Exhaustive permutation set (ignored with --trace-json)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--no-tests", action="store_true",
        help="Skip the regression-suite trace (faster, less truthful)",
    )
    args = parser.parse_args(argv)

    universe = collect_definitions()
    touched = _load_touched(args)
    also_reached: set[str] = set()
    if not args.no_tests:
        print("analyze_dead_code: tracing regression suite for reachability...",
              file=sys.stderr)
        also_reached = trace_regression_suite(quiet=True)
    report = build_report(
        universe, touched, build_reference_graph(),
        collect_exported(), also_reached,
    )

    if args.json:
        print(json.dumps({
            "universe_size": report.universe_size,
            "touched_size": report.touched_size,
            "reachable_pct": round(report.reachable_pct, 2),
            "truly_dead": report.truly_dead,
            "uncovered": report.uncovered,
            "test_reached": report.test_reached,
            "exported_dead": sorted(report.exported),
            "allowlisted_dead": report.allowlisted_dead,
            "stale_allowlist": report.stale_allowlist,
        }, indent=2))
    else:
        _print_report(report)

    # Advisory: always exit 0. The gate on allowlist staleness lives in the test.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
