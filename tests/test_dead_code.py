"""Action-permutation reachability: the static dead-code guardrail.

Traces the classes/functions the CLI touches across its action permutation
space (:mod:`scripts.trace_actions`) and diffs it against the static universe
of ``mdg_drawio`` definitions (:mod:`scripts.analyze_dead_code`).

Three responsibilities:

* **Regression gate** — every permutation must run cleanly (no uncaught
  exception), and the convertible fixtures must actually convert. A pipeline
  break flips one of these.
* **Allowlist honesty gate** — every entry in
  :mod:`scripts.dead_code_allowlist` must be a real definition that the sweep
  genuinely never reaches. Stale entries fail here so they get removed.
* **Dead-code gate** — nothing may be both unreached by the sweep AND
  unreferenced anywhere (``truly_dead``). This fails on genuine dead code but
  stays quiet on *coverage gaps* (``uncovered`` — referenced but not exercised),
  so ordinary refactors are not blocked.

For speed this uses only the CLI-sweep signal (no regression-suite trace); the
fuller three-way picture is ``make dead-code``.
"""

from __future__ import annotations

import pytest

from scripts.analyze_dead_code import (
    build_reference_graph,
    build_report,
    collect_definitions,
    collect_exported,
)
from scripts.trace_actions import TraceResult, run_all, touched_union

# Synthetic code objects that appear in a trace but are never `def`/`class`
# definitions in source, so they legitimately have no universe entry.
_SYNTHETIC = (
    "<module>",
    "<genexpr>",
    "<listcomp>",
    "<dictcomp>",
    "<setcomp>",
    "<lambda>",
)


@pytest.fixture(scope="module")
def results() -> list[TraceResult]:
    """Trace the covering permutation set once for the whole module."""
    return run_all(full=False, quiet=True)


def test_every_permutation_runs_without_crashing(results: list[TraceResult]) -> None:
    """No permutation may raise — a crash surfaces as an ``error:`` outcome."""
    crashed = [
        f"{r.label} -> {r.outcome}"
        for r in results
        if r.outcome.startswith("error:")
    ]
    assert not crashed, "permutations raised:\n" + "\n".join(crashed)


def test_convertible_fixtures_convert(results: list[TraceResult]) -> None:
    """Every traced fixture (all 7 notation coverage sheets, docs/architecture,
    tests/action_fixtures) converts. There is no longer a known-gap notation to
    special-case here -- see todo/notation-coverage-parser.md Phase 1."""
    failed = [f"{r.label}: {r.outcome}" for r in results if r.outcome != "ok"]
    assert not failed, "expected every traced fixture to convert:\n" + "\n".join(failed)


def test_touched_union_covers_the_core_pipeline(results: list[TraceResult]) -> None:
    """The union must include the obvious live entry points."""
    touched = touched_union(results)
    for expected in (
        "mdg_drawio.cli:main",
        "mdg_drawio.engine.convert:convert",
        "mdg_drawio.engine.preload:preload_core",
        "mdg_drawio.generator.generator:generate",
    ):
        assert expected in touched, f"expected live symbol not traced: {expected}"


def test_static_qualnames_mirror_traced_qualnames(results: list[TraceResult]) -> None:
    """Every traced in-package symbol resolves to a real static definition.

    Guards the AST qualname builder (including its ``<locals>`` handling)
    against drift from CPython's ``co_qualname``. Synthetic comprehension /
    lambda frames are the only allowed exceptions.
    """
    universe = collect_definitions()
    touched = touched_union(results)
    orphans = [
        key
        for key in touched
        if key not in universe and not any(s in key for s in _SYNTHETIC)
    ]
    assert not orphans, (
        "traced symbols with no static definition:\n" + "\n".join(sorted(orphans))
    )


def test_allowlist_is_not_stale(results: list[TraceResult]) -> None:
    """Every allowlist entry must be a real, genuinely-unreached definition.

    Fails if an entry names something that no longer exists or that the sweep
    actually reaches — either way it should be deleted from the allowlist.
    """
    universe = collect_definitions()
    report = build_report(
        universe, touched_union(results), build_reference_graph()
    )
    assert not report.stale_allowlist, (
        "stale allowlist entries (remove them from scripts/dead_code_allowlist.py):\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(report.stale_allowlist.items()))
    )


def test_no_truly_dead_code(results: list[TraceResult]) -> None:
    """The real dead-code gate: nothing is both unreached AND unreferenced.

    Uses the fast CLI-sweep signal (no regression-suite trace), so an item is
    ``truly_dead`` only when no permutation runs it AND its name is referenced
    nowhere in the repo. Delete such code, use it, or — if it is reached only by
    dynamic dispatch (e.g. a notation resolved via ``__import__``) — allowlist it
    with that reason. Coverage gaps ('uncovered') stay advisory and never fail.
    """
    universe = collect_definitions()
    report = build_report(
        universe, touched_union(results),
        build_reference_graph(), collect_exported(),
    )
    assert not report.truly_dead, (
        "truly-dead definitions (delete, use, or allowlist with a reason):\n"
        + "\n".join(
            f"  {k}" + ("  (exported public API)" if k in report.exported else "")
            for k in sorted(report.truly_dead)
        )
    )
