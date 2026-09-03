#!/usr/bin/env python3
"""Drive the conversion pipeline over its action permutation space and record,
per permutation, the ordered sequence of ``mdg_drawio`` classes/functions that
execute.

The ``mdg`` CLI exposes conversion and reverse-editing actions. The sweep
branches on their real input dimensions:

* **action** — ``convert`` plus representative ``derive`` / ``merge`` / ``sync``
* **fixture** — every ``*_shapes_coverage.mdg`` and every architecture ``.mdg``
* **notation** — derived from the fixture; notation coverage sheets exercise
  the shared parser, including clean rejection of still-unsupported constructs
* **layout mode** — ``layered`` / ``palette`` / ``process`` / ``sequence``
  (injected into frontmatter for c4 fixtures)
* **direction** — ``TB`` / ``LR`` (injected into frontmatter)
* **--force** — full regeneration vs. overlay round-trip
* **overlay** — a non-force pass re-reads geometry written by a prior pass

Each permutation runs under ``sys.settrace``: every function *entry* whose code
lives under ``mdg_drawio/`` is recorded in call order via ``co_qualname`` (so a
method reads ``module:Class.method``). The union of these keys across all
permutations is the "reachable" set that :mod:`scripts.analyze_dead_code`
diffs against the static universe of definitions.

Two enumeration modes:

* **covering** (default) — every dimension *value* is exercised at least once
  without the full cartesian blow-up; this is what the test drives.
* **full** (``--full``) — the cartesian product of every c4 dimension.

Usage::

    python scripts/trace_actions.py                 # covering set -> action_trace.json
    python scripts/trace_actions.py --full           # exhaustive cartesian product
    python scripts/trace_actions.py -o /tmp/t.json    # custom artifact path
    python scripts/trace_actions.py --quiet           # suppress the pipeline's stderr
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, FrameType
from typing import Any, Literal

import mdg_drawio.layout as layout_pkg
from mdg_drawio.cli import main as cli_main
from mdg_drawio.engine.convert import _detect_notation

# The convert *module* (not the re-exported ``convert`` function): reached via
# importlib because ``mdg_drawio.engine`` shadows the submodule name with the
# function. Typed ``Any`` so ``preload_core`` can be monkeypatched (see
# run_all) without tripping the module-attribute type check.
_CONVERT_MODULE: Any = importlib.import_module("mdg_drawio.engine.convert")

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "mdg_drawio"
PKG_PREFIX = str(PKG_DIR) + os.sep

DIRECTIONS = ("TB", "LR")
DEFAULT_ARTIFACT = ROOT / "action_trace.json"
Action = Literal["convert", "derive", "merge", "sync"]
REVERSE_ACTIONS: tuple[Action, ...] = ("derive", "merge", "sync")


# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------


def discover_fixtures() -> list[Path]:
    """Return every ``.mdg`` document that drives the pipeline.

    The per-notation shape-coverage documents plus the hand-authored
    architecture views — the full spread of real inputs the CLI ever sees.
    """
    fixtures: list[Path] = sorted(
        (PKG_DIR / "notation").glob("*/*_shapes_coverage.mdg")
    )
    fixtures += sorted((ROOT / "docs" / "architecture").glob("*.mdg"))
    # Hand-authored fixtures that exercise paths the above do not (e.g. the
    # legacy ``page "Name"`` multi-page syntax).
    fixtures += sorted((ROOT / "tests" / "action_fixtures").glob("*.mdg"))
    return fixtures


def _is_c4(fixture: Path) -> bool:
    """Whether a fixture receives the exhaustive layout permutations."""
    source = fixture.read_text(encoding="utf-8-sig")
    return _detect_notation(source) == "c4"


# ---------------------------------------------------------------------------
# Frontmatter injection
# ---------------------------------------------------------------------------


def _inject_frontmatter(
    source: str, *, layout: str | None, direction: str | None
) -> str:
    """Return *source* with ``layout:``/``direction:`` set in its frontmatter.

    A permutation only cares about which code path runs, not whether the
    resulting diagram is meaningful, so forcing an unusual mode onto any c4
    document is intentional. If the document has no frontmatter block one is
    prepended; existing keys are replaced in place.
    """
    if layout is None and direction is None:
        return source

    keys: dict[str, str] = {}
    if layout is not None:
        keys["layout"] = layout
    if direction is not None:
        keys["direction"] = direction

    lines = source.splitlines()
    if lines and lines[0].strip() == "---":
        # Existing frontmatter: rewrite matching keys, keep everything else.
        end = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
        )
        if end is not None:
            body = lines[1:end]
            remaining = dict(keys)
            for i, line in enumerate(body):
                head = line.split(":", 1)[0].strip()
                if head in remaining:
                    body[i] = f"{head}: {remaining.pop(head)}"
            body += [f"{k}: {v}" for k, v in remaining.items()]
            return "\n".join(["---", *body, "---", *lines[end + 1 :]])

    block = ["---", *[f"{k}: {v}" for k, v in keys.items()], "---"]
    return "\n".join([*block, source])


# ---------------------------------------------------------------------------
# Permutation model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Permutation:
    """One concrete run of the pipeline."""

    fixture: Path
    force: bool
    action: Action = "convert"
    layout: str | None = None
    direction: str | None = None
    overlay: bool = False  # non-force pass that re-reads a prior output

    @property
    def label(self) -> str:
        rel = self.fixture.relative_to(ROOT)
        parts = [self.action, rel.as_posix()]
        parts.append(self.layout or "auto")
        parts.append(self.direction or "auto")
        parts.append("force" if self.force else "overlay" if self.overlay else "fresh")
        return " | ".join(parts)


def build_permutations(*, full: bool) -> list[Permutation]:
    """Enumerate the permutation set.

    Covering mode touches every dimension value at least once; full mode is the
    cartesian product of ``mode x direction x force`` for each C4 fixture.
    Other notation fixtures contribute one representative convert run each.
    One C4 fixture additionally exercises every reverse CLI action against a
    real generated draw.io document.
    """
    fixtures = discover_fixtures()
    modes = sorted(layout_pkg.modes())
    perms: list[Permutation] = []

    for fixture in fixtures:
        if not _is_c4(fixture):
            # One representative parse/conversion run for each other notation.
            perms.append(Permutation(fixture, force=True))
            continue

        if full:
            for mode in modes:
                for direction in DIRECTIONS:
                    for force in (True, False):
                        perms.append(
                            Permutation(
                                fixture,
                                force=force,
                                layout=mode,
                                direction=direction,
                                overlay=not force,
                            )
                        )
            continue

        # Covering set: each mode once (TB, force), each direction once
        # (default mode), plus one overlay round-trip.
        for mode in modes:
            perms.append(Permutation(fixture, force=True, layout=mode, direction="TB"))
        for direction in DIRECTIONS:
            perms.append(Permutation(fixture, force=True, direction=direction))
        perms.append(Permutation(fixture, force=False, overlay=True))

    reverse_fixture = next((fixture for fixture in fixtures if _is_c4(fixture)), None)
    if reverse_fixture is not None:
        perms.extend(
            Permutation(reverse_fixture, force=False, action=action)
            for action in REVERSE_ACTIONS
        )

    return perms


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


def _class_key(frame: FrameType) -> str | None:
    """For an ``__init__``/``__new__`` frame, the ``module:Class`` being built.

    Captures instantiation of pure ``@dataclass`` containers: their generated
    ``__init__`` has a synthetic code object (``co_qualname`` is
    ``__create_fn__.<locals>.__init__``, filename ``<string>``) so the call
    itself is invisible, but the bound ``self`` names the real class. Returns
    ``None`` for classes outside ``mdg_drawio``.
    """
    instance = frame.f_locals.get("self") or frame.f_locals.get("cls")
    if instance is None:
        return None
    cls = instance if isinstance(instance, type) else type(instance)
    module = getattr(cls, "__module__", "")
    if not module.startswith("mdg_drawio"):
        return None
    return f"{module}:{cls.__qualname__}"


def _make_tracer(record: list[str]) -> Callable[[FrameType, str, object], None]:
    """Build a ``sys.settrace`` callback recording in-package execution.

    On every ``call`` event: records the entered function as ``module:qualname``
    when its code lives under ``mdg_drawio/``, and — for ``__init__``/``__new__``
    frames — additionally records the class being instantiated (so pure
    dataclasses, whose generated ``__init__`` is a synthetic code object, are
    still seen as reached). Returning ``None`` skips line/return events while
    still firing on every new frame.

    Each symbol is recorded once, in first-occurrence (call) order: a hot
    function called thousands of times adds nothing to reachability, and keeping
    only distinct entries makes ``sequence`` a compact, analysable call trace
    instead of a multi-megabyte log.
    """
    seen: set[str] = set()

    def _add(key: str) -> None:
        if key not in seen:
            seen.add(key)
            record.append(key)

    def tracer(frame: FrameType, event: str, _arg: object) -> None:
        if event != "call":
            return None
        code = frame.f_code
        module = frame.f_globals.get("__name__", "")
        if code.co_filename.startswith(PKG_PREFIX):
            _add(f"{module}:{code.co_qualname}")
        # Only inspect ``self`` for our own classes. Reading ``f_locals`` on
        # stdlib frames (e.g. ``re._parser`` while argparse compiles a pattern
        # under the tracer) can corrupt their partially-built state, so the
        # cheap ``f_globals`` module check must gate the ``f_locals`` access.
        if code.co_name in ("__init__", "__new__") and module.startswith("mdg_drawio"):
            key = _class_key(frame)
            if key is not None:
                _add(key)
        return None

    return tracer


@dataclass
class TraceResult:
    label: str
    fixture: str
    action: Action
    force: bool
    layout: str | None
    direction: str | None
    overlay: bool
    outcome: str
    sequence: list[str] = field(default_factory=list)

    @property
    def touched(self) -> set[str]:
        return set(self.sequence)


@contextlib.contextmanager
def _temp_input(perm: Permutation) -> Iterator[tuple[Path, Path]]:
    """Materialise the (possibly frontmatter-injected) input + output paths."""
    source = perm.fixture.read_text(encoding="utf-8-sig")
    source = _inject_frontmatter(source, layout=perm.layout, direction=perm.direction)
    with tempfile.TemporaryDirectory(prefix="mdg-trace-") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / perm.fixture.name
        input_path.write_text(source, encoding="utf-8")
        output_path = tmp_dir / (perm.fixture.stem + ".drawio")
        yield input_path, output_path


def _convert_argv(input_path: Path, output_path: Path, force: bool) -> list[str]:
    """CLI arguments for one convert invocation."""
    argv = [str(input_path), str(output_path)]
    if force:
        argv.append("--force")
    return argv


def _argv(perm: Permutation, input_path: Path, output_path: Path) -> list[str]:
    """CLI arguments for the action represented by *perm*."""
    if perm.action == "convert":
        return _convert_argv(input_path, output_path, perm.force)
    if perm.action == "derive":
        return ["derive", str(output_path), "--json"]
    return [perm.action, str(input_path), str(output_path)]


def trace_permutation(perm: Permutation, *, quiet: bool = True) -> TraceResult:
    """Run one permutation through the CLI under the tracer.

    The pipeline is driven the way a user drives it — ``cli.main(argv)`` — so
    the CLI shell is part of what gets traced. Overlay and reverse-action
    permutations first do an untraced ``--force`` pass to seed the output
    geometry. The previous tracer is saved and restored so this never clobbers
    an enclosing coverage run.
    """
    record: list[str] = []
    stderr_sink = io.StringIO() if quiet else sys.stderr
    stdout_sink = io.StringIO() if quiet else sys.stdout

    with _temp_input(perm) as (input_path, output_path):
        with contextlib.redirect_stderr(stderr_sink), contextlib.redirect_stdout(
            stdout_sink
        ):
            if perm.overlay or perm.action != "convert":
                # Seed geometry (untraced) so the traced pass reads an overlay.
                cli_main(_convert_argv(input_path, output_path, True))

            argv = _argv(perm, input_path, output_path)
            previous = sys.gettrace()
            sys.settrace(_make_tracer(record))
            try:
                code = cli_main(argv)
                outcome = "ok" if code == 0 else f"exit={code}"
            # Deliberately catches BaseException: one permutation blowing up
            # (including on SystemExit) is a result to record, not a reason to
            # abort the whole sweep.
            except BaseException as exc:
                outcome = f"error:{type(exc).__name__}: {exc}"
            finally:
                sys.settrace(previous)

    return TraceResult(
        label=perm.label,
        fixture=perm.fixture.relative_to(ROOT).as_posix(),
        action=perm.action,
        force=perm.force,
        layout=perm.layout,
        direction=perm.direction,
        overlay=perm.overlay,
        outcome=outcome,
        sequence=record,
    )


@contextlib.contextmanager
def _cached_preload() -> Iterator[None]:
    """Memoise ``preload_core`` for the duration of a sweep.

    Loading the registries + styles takes ~0.6 s and dominates every convert;
    it is input-independent and convert treats the result as read-only, so
    caching it collapses the sweep from ~85 s to a few seconds.

    The cache starts cold and ``convert`` calls ``preload_core`` before any
    other work, so the first (non-overlay) permutation runs the real load
    *under the tracer* — its symbols land in the union before the cache fills.
    """
    original = _CONVERT_MODULE.preload_core
    _CONVERT_MODULE.preload_core = functools.lru_cache(maxsize=1)(original)
    try:
        yield
    finally:
        _CONVERT_MODULE.preload_core = original


def run_all(*, full: bool = False, quiet: bool = True) -> list[TraceResult]:
    """Trace every permutation and return the per-permutation results.

    Overlay permutations are enumerated last (per fixture), so a non-overlay
    permutation always fills the preload cache under the tracer first.
    """
    with _cached_preload():
        return [
            trace_permutation(p, quiet=quiet)
            for p in build_permutations(full=full)
        ]


def touched_union(results: list[TraceResult]) -> set[str]:
    """Union of every ``module:qualname`` touched across all permutations."""
    union: set[str] = set()
    for result in results:
        union |= result.touched
    return union


# ---------------------------------------------------------------------------
# Regression-suite reachability (second runtime signal)
# ---------------------------------------------------------------------------

_FILE_MODULE_CACHE: dict[str, str] = {}


def _module_of_file(filename: str) -> str:
    """Dotted import name for an in-package file path (matches trace keys)."""
    cached = _FILE_MODULE_CACHE.get(filename)
    if cached is not None:
        return cached
    rel = Path(filename).resolve().relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    module = ".".join(parts)
    _FILE_MODULE_CACHE[filename] = module
    return module


def trace_regression_suite(*, quiet: bool = True) -> set[str]:
    """Return the ``mdg_drawio`` symbols the regression suite executes.

    Complements the CLI action sweep: the tests exercise code paths no fixture
    reaches (child-cell rendering, multi-boundary layout, generator internals),
    so folding them in makes "truly dead" far more trustworthy.

    Uses ``sys.monitoring`` (PEP 669) with ``DISABLE`` so each code location
    fires at most once — orders of magnitude cheaper than ``settrace`` over a
    full suite. Its own sweep test is skipped (it only re-runs the CLI sweep).
    ``self`` inspection is not available here, so pure dataclass instantiation
    is not captured — the CLI sweep and static references cover classes.
    """
    import pytest

    mon = sys.monitoring
    tool_id = mon.PROFILER_ID
    hits: set[str] = set()

    def on_start(code: CodeType, _offset: int) -> object:
        filename = code.co_filename
        if filename.startswith(PKG_PREFIX):
            hits.add(f"{_module_of_file(filename)}:{code.co_qualname}")
        return mon.DISABLE

    out_sink = io.StringIO() if quiet else sys.stdout
    err_sink = io.StringIO() if quiet else sys.stderr
    mon.use_tool_id(tool_id, "mdg-deadcode")
    try:
        mon.register_callback(tool_id, mon.events.PY_START, on_start)
        mon.set_events(tool_id, mon.events.PY_START)
        with contextlib.redirect_stdout(out_sink), contextlib.redirect_stderr(err_sink):
            pytest.main(
                ["tests", "-p", "no:cacheprovider", "-q",
                 "--ignore=tests/test_dead_code.py"]
            )
    finally:
        mon.set_events(tool_id, 0)
        mon.register_callback(tool_id, mon.events.PY_START, None)
        mon.free_tool_id(tool_id)
    return hits


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def to_artifact(results: list[TraceResult], *, full: bool) -> dict:
    """Serialise results into the JSON artifact structure."""
    return {
        "generated_by": "scripts/trace_actions.py",
        "mode": "full" if full else "covering",
        "permutation_count": len(results),
        "permutations": [
            {
                "label": r.label,
                "fixture": r.fixture,
                "action": r.action,
                "force": r.force,
                "layout": r.layout,
                "direction": r.direction,
                "overlay": r.overlay,
                "outcome": r.outcome,
                # First-occurrence-ordered distinct symbols (see _make_tracer).
                "sequence": r.sequence,
            }
            for r in results
        ],
        "touched_union": sorted(touched_union(results)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trace_actions",
        description="Trace classes/functions touched by each CLI action permutation.",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_ARTIFACT,
        help=f"Artifact path (default: {DEFAULT_ARTIFACT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Exhaustive cartesian product instead of the covering set",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the pipeline's own stderr output",
    )
    args = parser.parse_args(argv)

    results = run_all(full=args.full, quiet=args.quiet)
    artifact = to_artifact(results, full=args.full)
    args.output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    try:
        shown = args.output.resolve().relative_to(ROOT)
    except ValueError:
        shown = args.output
    errored = [r for r in results if r.outcome.startswith("error:")]
    print(
        f"trace_actions: {len(results)} permutation(s) "
        f"({artifact['mode']} mode), "
        f"{len(artifact['touched_union'])} unique symbol(s) touched "
        f"-> {shown}",
        file=sys.stderr,
    )
    if errored:
        print(f"trace_actions: {len(errored)} permutation(s) raised:", file=sys.stderr)
        for r in errored:
            print(f"  {r.label}: {r.outcome}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
