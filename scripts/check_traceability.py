#!/usr/bin/env python3
"""Traceability meta-model checker.

Reads the model-only ``use`` / ``trace`` declarations from an architecture MDG
file and enforces a ruleset that links C4 Components to the modules that
realize them in another MDG view (the Code view). Trace declarations never
render; they are the machine-checkable bridge between views.

Declared syntax (skipped by the DSL parser, see ``_SKIP_PREFIXES``)::

    use "code_architecture.mdg" as code
    trace <component_id> -> <alias>.<node_id> : <relation-type>

Each relation type is defined once in ``RELATION_RULES`` (the trace
vocabulary). Every trace is validated against its rule:

* **resolvable-source**  — the trace source is a Component on the C4 page.
* **resolvable-target**  — the alias is declared via ``use`` and the target
  node exists in the referenced view.
* **known-relation**     — the relation type is registered in RELATION_RULES.
* **package-parity** (rule flag) — the target's innermost package equals the
  trace source id (Container ▸ Component ▸ module).
* **target-coverage** (rule flag) — every class in the referenced view is the
  target of at least one trace of this type (no untraced code).

    python scripts/check_traceability.py            # report + exit code
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
C4_MDG = ROOT / "docs" / "architecture" / "c4_architecture.mdg"
ARCH_DIR = ROOT / "docs" / "architecture"

_REALIZED_BY = "realized-by"

_USE_RE = re.compile(r'^use\s+"([^"]+)"\s+as\s+(\w+)\s*$')
_TRACE_RE = re.compile(r"^trace\s+(\w+)\s*->\s*(\w+)\.(\w+)\s*:\s*(\S+)\s*$")
_PACKAGE_RE = re.compile(r"uml\.Package\((\w+),")
_CLASS_RE = re.compile(r"uml\.Class\((\w+)")
_COMPONENT_RE = re.compile(r"c4\.Component\((\w+)")


class Trace(NamedTuple):
    source: str
    alias: str
    target: str
    relation: str
    line: int


class RefView(NamedTuple):
    classes: set[str]
    packages: dict[str, str]


class RelationRule(NamedTuple):
    """Meta-model rule for one trace relation type.

    ``target_alias`` — the ``use ... as <alias>`` view this relation must point
    into (so coverage is scoped to the right view).
    ``require_package_parity`` — the target's innermost package must equal the
    trace source id (Container ▸ Component ▸ module parity).
    ``require_target_coverage`` — every class in the target view must be the
    target of at least one trace of this type (no untraced target).
    """

    description: str
    target_alias: str
    require_package_parity: bool
    require_target_coverage: bool


# The trace vocabulary. Adding a relationship type is a single entry here
# (see skill://architect). Superseded types are intentionally
# absent from RELATION_RULES so they error if used:
#   verifies (test -> Component) — superseded by coverage-derived verification
#     (`make verification` maps line coverage → module → Component), not a
#     manual trace.
RELATION_RULES: dict[str, RelationRule] = {
    "realized-by": RelationRule(
        description="a C4 Component is realized by code module(s)",
        target_alias="code",
        require_package_parity=True,
        require_target_coverage=True,
    ),
    "satisfies": RelationRule(
        description="a C4 Component satisfies an architecture decision",
        target_alias="decisions",
        require_package_parity=False,
        require_target_coverage=True,
    ),
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_uses(text: str) -> dict[str, str]:
    """Return ``{alias: referenced_file}`` from ``use`` declarations."""
    uses: dict[str, str] = {}
    for line in text.splitlines():
        match = _USE_RE.match(line.strip())
        if match:
            uses[match.group(2)] = match.group(1)
    return uses


def parse_traces(text: str) -> list[Trace]:
    """Return every ``trace`` declaration."""
    traces: list[Trace] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _TRACE_RE.match(line.strip())
        if match:
            source, alias, target, relation = match.groups()
            traces.append(Trace(source, alias, target, relation, number))
    return traces


def malformed_directives(text: str) -> list[str]:
    """Report lines that look like ``trace``/``use`` directives but don't parse.

    The DSL parser strips every ``trace``/``use`` line before rendering, so a
    typo'd directive would otherwise vanish silently from both the diagram and
    this model check. Catch anything starting with the keyword that fails its
    regex so malformed traceability can never be ignored.
    """
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("trace ") and not _TRACE_RE.match(stripped):
            errors.append(
                f"line {number}: malformed trace directive (expected "
                f"`trace <source> -> <alias>.<target> : <relation>`): {stripped!r}"
            )
        elif stripped.startswith("use ") and not _USE_RE.match(stripped):
            errors.append(
                f"line {number}: malformed use directive (expected "
                f'`use "<file>" as <alias>`): {stripped!r}'
            )
    return errors


def _component_ids(text: str) -> set[str]:
    return set(_COMPONENT_RE.findall(text))


def _class_packages(text: str) -> dict[str, str]:
    """Map each ``uml.Class`` node id to its innermost enclosing package.

    In the Container ▸ Component ▸ module code view the innermost package is the
    Component, so this yields ``{module_node: component_id}``.
    """
    packages: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        indent = _indent(line)
        pkg = _PACKAGE_RE.match(stripped)
        if pkg:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, pkg.group(1)))
            continue
        cls = _CLASS_RE.match(stripped)
        if cls:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            packages[cls.group(1)] = stack[-1][1] if stack else cls.group(1)
    return packages


def _load_code_generator() -> ModuleType:
    path = ROOT / "scripts" / "generate_code_arch.py"
    spec = importlib.util.spec_from_file_location("generate_code_arch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("generate_code_arch", module)
    spec.loader.exec_module(module)
    return module


def _node_to_module() -> dict[str, str]:
    """Reverse map: Code-view node id -> module rel path (authoritative)."""
    gen = _load_code_generator()
    return {gen._node_id(gen._rel(p)): gen._rel(p) for p in gen._significant_modules()}


def component_modules() -> dict[str, set[str]]:
    """Derive ``{component_id: {module_rel_path}}`` from realized-by traces.

    This supersedes the hardcoded COMPONENT_TO_MODULE mapping: the trace
    declarations in the MDG are the single source of truth.
    """
    text = C4_MDG.read_text(encoding="utf-8")
    node_to_module = _node_to_module()
    mapping: dict[str, set[str]] = {}
    for trace in parse_traces(text):
        if trace.relation != _REALIZED_BY:
            continue
        module = node_to_module.get(trace.target)
        if module is not None:
            mapping.setdefault(trace.source, set()).add(module)
    return mapping


def _load_referenced_views(uses: dict[str, str]) -> dict[str, RefView]:
    """Load every ``use``-referenced view (classes + package of each class)."""
    referenced: dict[str, RefView] = {}
    for alias, rel in uses.items():
        ref_text = (ARCH_DIR / rel).read_text(encoding="utf-8")
        referenced[alias] = RefView(
            classes=set(_CLASS_RE.findall(ref_text)),
            packages=_class_packages(ref_text),
        )
    return referenced


def _verify_trace(
    trace: Trace,
    rule: RelationRule,
    components: set[str],
    referenced: dict[str, RefView],
) -> tuple[list[str], tuple[str, str] | None]:
    """Verify one trace against its rule. Return (errors, resolved target)."""
    if trace.source not in components:
        return [
            f"line {trace.line}: trace source {trace.source!r} "
            "is not a Component on the C4 page"
        ], None
    if trace.alias != rule.target_alias:
        return [
            f"line {trace.line}: {trace.relation} must target the "
            f"{rule.target_alias!r} view, not {trace.alias!r}"
        ], None
    ref = referenced.get(trace.alias)
    if ref is None:
        return [
            f"line {trace.line}: unknown view alias {trace.alias!r} "
            "(missing a `use` declaration)"
        ], None
    if trace.target not in ref.classes:
        return [
            f"line {trace.line}: trace target {trace.alias}.{trace.target} "
            "does not exist in the referenced view"
        ], None

    errors: list[str] = []
    if rule.require_package_parity:
        # The target must be nested under the package named after its source
        # (Container ▸ Component ▸ module).
        node_pkg = ref.packages.get(trace.target)
        if node_pkg != trace.source:
            errors.append(
                f"line {trace.line}: {trace.target} is nested under "
                f"{node_pkg!r}, but is traced ({trace.relation}) from "
                f"{trace.source!r} — the module must live in its "
                "component's package"
            )
    return errors, (trace.alias, trace.target)


def _coverage_errors(
    rules: dict[str, RelationRule],
    referenced: dict[str, RefView],
    covered: dict[str, set[tuple[str, str]]],
) -> list[str]:
    """Every class in a relation's target view must be covered by that relation."""
    errors: list[str] = []
    for relation, rule in rules.items():
        if not rule.require_target_coverage:
            continue
        ref = referenced.get(rule.target_alias)
        if ref is None:
            continue
        seen = covered.get(relation, set())
        for node in sorted(ref.classes):
            if (rule.target_alias, node) not in seen:
                errors.append(
                    f"orphan {rule.target_alias}.{node}: no {relation} "
                    "trace targets it"
                )
    return errors


def check(
    c4_path: Path = C4_MDG,
    rules: dict[str, RelationRule] = RELATION_RULES,
) -> list[str]:
    """Return traceability violations (empty when the model is sound)."""
    text = c4_path.read_text(encoding="utf-8")
    components = _component_ids(text)
    referenced = _load_referenced_views(parse_uses(text))

    errors: list[str] = list(malformed_directives(text))
    covered: dict[str, set[tuple[str, str]]] = {}
    for trace in parse_traces(text):
        rule = rules.get(trace.relation)
        if rule is None:
            errors.append(
                f"line {trace.line}: unknown relation type {trace.relation!r} "
                "— add it to RELATION_RULES"
            )
            continue
        trace_errors, resolved = _verify_trace(trace, rule, components, referenced)
        errors.extend(trace_errors)
        if resolved is not None:
            covered.setdefault(trace.relation, set()).add(resolved)

    errors.extend(_coverage_errors(rules, referenced, covered))
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("Traceability violations:")
        for error in errors:
            print(f"  {error}")
        print(f"\n{len(errors)} violation(s).")
        return 1
    traces = parse_traces(C4_MDG.read_text(encoding="utf-8"))
    realized = [t for t in traces if t.relation == _REALIZED_BY]
    print(f"  realized-by traces: {len(realized)}")
    print(f"  components traced:  {len({t.source for t in realized})}")
    print("  [OK] traceability meta-model is consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
