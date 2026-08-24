#!/usr/bin/env python3
"""Round-trip check: architecture MDG relationships ↔ mdg_drawio/ codebase.

Checks:

1. Forward (notation components → edges)
   Every component inside a notation boundary must be referenced by at least
   one c4.Rel / uml.Association — no orphaned nodes.

2. Reverse — exhaustive walk (codebase → MDG)
   Every file/dir under mdg_drawio/ is categorised. Each significant module
   must have a matching Component label. Package boundaries must exist.
   Notation-specific files (registries, stubs) are verified via collective
   components. Unknown files are flagged as unaccounted.

3. Edge integrity
   Every c4.Rel / uml.Association source and target must reference a declared
   node on its page — no dangling endpoints.

4. Relationship snapshot and page policies
   Page relationship counts must match the expected architecture snapshot.
   Context, Container, and Code pages also enforce page-specific endpoint
   policies.

5. Import integrity (bidirectional)
   Forward: every cross-package c4.Rel must have a matching Python import.
   Reverse: every cross-package ``import mdg_drawio.<pkg>`` must have a
   matching c4.Rel on the Component page.

Exit 0: all checks pass. Exit 1: one or more mismatches.

    python scripts/check_architecture_components.py [mdg_file ...]
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from mdg_drawio.contracts import Document, MultiPageDocument, Node
from mdg_drawio.notation import DslError, parse

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "mdg_drawio"
DEFAULT_MDG_PATHS = (
    REPO_ROOT / "docs" / "architecture" / "c4_architecture.mdg",
    REPO_ROOT / "docs" / "architecture" / "code_architecture.mdg",
)

# Every significant module must have a matching Component label.
# Key: path relative to mdg_drawio/.  Value: substring in a Component label.
MODULE_TO_LABEL: dict[str, str] = {
    "cli.py": "CLI main()",
    "engine/convert.py": "convert()",
    "engine/derive.py": "derive()",
    "engine/merge.py": "merge()",
    "engine/preload.py": "Pre-load",
    "engine/validate.py": "XML Validation",
    "contracts/models.py": "Models",
    "contracts/constants.py": "Constants",
    "generator/generator.py": "generate()",
    "generator/overlay.py": "Overlay",
    "generator/xml_utils.py": "XML Utils",
    "layout/_types.py": "Types & BaseLayout",
    "layout/config.py": "Config",
    "layout/size_resolver.py": "Size Resolver",
    "layout/_container_layout.py": "Container Layout",
    "layout/layered.py": "LayeredLayout",
    "layout/process.py": "ProcessLayout",
    "layout/sequence.py": "SequenceLayout",
    "layout/palette.py": "PaletteLayout",
    "notation/_core/dsl_engine.py": "DSL Engine",
    "notation/_core/registry.py": "Registry",
    "notation/_core/styles.py": "Styles",
    "notation/_core/normalize.py": "Normalize",
    "notation/_core/palette.py": "Palette",
    "notation/GRAMMAR.md": "GRAMMAR.md",
    "reverse/containment.py": "Containment Resolution",
    "reverse/derive.py": "Document-Level Ranking",
    "reverse/derive_cli.py": "Derive CLI",
    "reverse/fixtures.py": "Fixture Synthesis",
    "reverse/merge.py": "Merge Planning",
    "reverse/merge_cli.py": "Merge CLI",
    "reverse/naming.py": "Semantic Naming",
    "reverse/scoring.py": "Weighted Style Match",
    "reverse/style_index.py": "Style Index",
}

# Boundaries that must exist on the Component page.
EXPECTED_BOUNDARIES: set[str] = {
    "contracts_co_boundary",
    "engine_co_boundary",
    "generator_co_boundary",
    "layout_co_boundary",
    "notation_co_boundary",
    "reverse_co_boundary",
}

# Files relative to mdg_drawio/ that are skipped.
SKIP_FILES: set[str] = {
    "__init__.py",
    "__main__.py",
    "layout/layout.py",  # re-export shim for _types
    "generator/style_overrides.yaml",  # generation-time style config (not a module)
    "reverse/__main__.py",  # POC standalone `derive` CLI entry point, not modeled
}

# Directories that are package containers (covered by boundary checks).
SKIP_CONTAINER_DIRS: set[str] = {
    "contracts", "engine", "generator", "layout", "notation", "reverse",
}

# Notation-specific.
EXPECTED_LIBRARIES: tuple[str, ...] = (
    "c4", "archimate3", "bpmn2", "erd", "general", "uml", "uml25",
)
COLLECTIVE_COMPONENT_SUBSTRS: tuple[str, ...] = (
    "YAML Registries",
    "Notation Parsers",
)

EXPECTED_RELATION_COUNTS: dict[str, int] = {
    "Context": 4,
    # +2: engine -> reverse, reverse -> notation (the new Reverse Derivation &
    # Merge container, added for the `mdg merge`/`mdg derive` subcommands).
    "Container": 13,
    # +25: the Reverse Derivation & Merge container's own Component/Code-page
    # edges -- engine.merge()/derive() -> reverse boundary, 9 new components'
    # intra-package and cross-package (-> notation) relations.
    "Component": 63,
    "Code": 63,
}

CONTEXT_RELATION_TYPES: set[str] = {
    "c4.Person",
    "c4.System",
    "c4.System_Ext",
}

CONTAINER_PACKAGE_IDS: dict[str, str] = {
    "cli": "cli.py",
    "engine": "engine",
    "contracts": "contracts",
    "layout": "layout",
    "generator": "generator",
    "notation": "notation",
    "reverse": "reverse",
}
CONTAINER_DATA_IDS: set[str] = {"palette_data"}
CONTAINER_EXTERNAL_IDS: set[str] = {"author_c", "drawio_c"}
CONTAINER_ALLOWED_IDS = (
    set(CONTAINER_PACKAGE_IDS) | CONTAINER_DATA_IDS | CONTAINER_EXTERNAL_IDS
)

CODE_RELATION_ELEMENT_NAMES: set[str] = {"Package", "Class", "Component"}


# Exact Component-page relationships that are intentionally not checked via
# Python imports. Keeping this edge-level allowlist makes new relationships
# fail until they are either import-verifiable or deliberately classified here.
NON_IMPORT_RELATIONS: dict[tuple[str, str], str] = {
    ("author_co", "cli_co"): "external actor invokes CLI",
    ("engine_validate_co", "generator_co_boundary"): (
        "validates the generator's output XML (data flow, not an import — "
        "validate.py imports no generator code)"
    ),
    ("registry_co", "registries_co"): "registry loader reads YAML artifacts",
    ("styles_co", "palette_data_co"): "style loader reads generated JSON",
    ("normalize_co", "styles_co"): "runtime pre-loaded style data",
    ("palette_co", "styles_co"): "runtime pre-loaded style data",
    ("dsl_engine_co", "registries_co"): "runtime pre-loaded registry data",
    ("registries_co", "grammar_co"): "YAML artifacts conform to spec",
    ("stubs_co", "registries_co"): "stub libraries are registry artifacts",
}

# ── Import integrity: MDG id → mdg_drawio subpackage ────────────────────────

# Endpoints with no code module: external actors, generated data, docs, and
# not-yet-implemented stubs. Everything else is derived (see ID_TO_PACKAGE).
_NON_CODE_IDS: dict[str, str | None] = {
    "author_co": None,
    "palette_data_co": None,
    "registries_co": None,
    "grammar_co": None,
    "stubs_co": None,
}

# ── Intra-package import verification: component id → modules that realize it ─


def _load_traceability() -> ModuleType:
    """Load the traceability meta-model module (sibling script)."""
    import importlib.util

    path = REPO_ROOT / "scripts" / "check_traceability.py"
    spec = importlib.util.spec_from_file_location("check_traceability", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_traceability", module)
    spec.loader.exec_module(module)
    return module


# Component → modules that realize it, derived from the declared ``trace``
# relationships in the C4 MDG (see scripts/check_traceability.py). This replaces
# the former hardcoded literal: the model is now the single source of truth.
# Multi-valued because one Component may be realized by several modules.
COMPONENT_TO_MODULE: dict[str, set[str]] = _load_traceability().component_modules()


def _build_id_to_package() -> dict[str, str | None]:
    """Component/boundary id → subpackage, derived from the model (not hardcoded).

    - Container_Boundary ids map to their package structurally (``<pkg>_co_boundary``).
    - Code-backed Components map to the package of the module(s) that realize
      them (from the ``realized-by`` traces via COMPONENT_TO_MODULE).
    - Non-code endpoints fall back to ``_NON_CODE_IDS``.
    """
    mapping: dict[str, str | None] = {}
    for boundary in EXPECTED_BOUNDARIES:
        mapping[boundary] = boundary.removesuffix("_co_boundary")
    for component, modules in COMPONENT_TO_MODULE.items():
        containers = {
            m.split("/", 1)[0] if "/" in m else m.removesuffix(".py")
            for m in modules
        }
        assert len(containers) == 1, f"{component} spans containers {containers}"
        mapping[component] = containers.pop()
    mapping.update(_NON_CODE_IDS)
    return mapping


ID_TO_PACKAGE: dict[str, str | None] = _build_id_to_package()

# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class Component:
    id: str
    label: str
    line: int


@dataclass
class Relation:
    source: str
    target: str
    line: int


@dataclass
class Page:
    name: str
    source: str
    nodes: list[Node] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)


@dataclass
class RelationCheckResult:
    errors: int
    coverage: str | None = None


# ── MDG parsing ──────────────────────────────────────────────────────────────


def split_pages(source: str) -> list[str]:
    lines = source.splitlines()
    starts = [
        i
        for i, line in enumerate(lines)
        if line.strip() == "---" and _frontmatter_has_page(lines, i + 1)
    ]
    if not starts:
        return [source]

    pages: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        pages.append("\n".join(lines[start:end]))
    return pages


def _frontmatter_has_page(lines: list[str], start: int) -> bool:
    for line in lines[start:]:
        stripped = line.strip()
        if stripped == "---":
            return False
        if stripped.startswith("page:"):
            return True
    return False


def extract_pages(source: str) -> list[Page]:
    parsed = parse(source)
    documents = parsed.pages if isinstance(parsed, MultiPageDocument) else [parsed]
    page_sources = split_pages(source)
    if len(page_sources) != len(documents):
        raise ValueError(
            f"parsed {len(documents)} page(s), but found {len(page_sources)} "
            "source page section(s)"
        )

    return [
        Page(
            name=doc.diagram.name or f"Page {index}",
            source=page_source,
            nodes=doc.nodes,
            relations=_relations_from_document(doc, page_source),
        )
        for index, (doc, page_source) in enumerate(
            zip(documents, page_sources, strict=True),
            start=1,
        )
    ]


def _relations_from_document(doc: Document, page_source: str) -> list[Relation]:
    occurrences: dict[tuple[str, str], int] = {}
    relations: list[Relation] = []
    for edge in doc.edges:
        key = (edge.source_id, edge.target_id)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        relations.append(
            Relation(
                source=edge.source_id,
                target=edge.target_id,
                line=_line_for_relation(page_source, key, occurrence),
            )
        )
    return relations


def extract_components(page: Page) -> list[Component]:
    return [
        Component(
            id=node.id,
            label=node.label,
            line=_line_for_node(page.source, node.id),
        )
        for node in page.nodes
        if node.element_name == "Component"
    ]


def extract_relations(page: Page) -> list[Relation]:
    return page.relations


def extract_all_declared_ids(page: Page) -> set[str]:
    return {node.id for node in page.nodes}


def components_in_boundary(page: Page, boundary_ids: Iterable[str]) -> list[Component]:
    boundary_set = set(boundary_ids)
    return [
        Component(
            id=node.id,
            label=node.label,
            line=_line_for_node(page.source, node.id),
        )
        for node in page.nodes
        if node.element_name == "Component" and node.parent_id in boundary_set
    ]


def notation_boundary_ids(page: Page) -> list[str]:
    return [
        node.id
        for node in page.nodes
        if node.element_name in ("System_Boundary", "Container_Boundary")
        and "notation" in node.id.lower()
    ]


@dataclass(frozen=True)
class SourceCall:
    name: str
    args_source: str
    line: int


def _strip_inline_comment(line: str) -> str:
    in_quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote:
            if ch == "\\" and in_quote != "'":
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
        else:
            if ch in ('"', "'"):
                in_quote = ch
            elif ch == "#":
                return line[:i].rstrip()
        i += 1
    return line


def _iter_source_calls(source: str) -> Iterable[SourceCall]:
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        stripped = _strip_inline_comment(raw_line.rstrip()).strip()
        if (
            not stripped
            or stripped in {"---"}
            or stripped.startswith(("page:", "mode:"))
        ):
            continue
        if stripped.endswith(":"):
            stripped = stripped[:-1].rstrip()
        open_paren = stripped.find("(")
        close_paren = stripped.rfind(")")
        if open_paren <= 0 or close_paren <= open_paren:
            continue
        prefix = stripped[:open_paren].strip()
        if "." not in prefix:
            continue
        _, name = prefix.rsplit(".", 1)
        yield SourceCall(
            name=name,
            args_source=stripped[open_paren + 1 : close_paren],
            line=line_number,
        )


def _parse_args(args_source: str) -> list[ast.AST | ast.keyword]:
    expr = ast.parse(f"f({args_source})", mode="eval").body
    if not isinstance(expr, ast.Call):
        return []
    return [*expr.args, *expr.keywords]


def _identifier_like_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            number = node.value
            if isinstance(number, float) and number.is_integer():
                number = int(number)
            return str(number)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        left = _identifier_like_value(node.left)
        right = _identifier_like_value(node.right)
        if left is not None and right is not None:
            return f"{left}-{right}"
    return None


def _literal_or_name_or_none(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and node.value is None:
        return None
    return _identifier_like_value(node)


def _positional_arg_ids(args_source: str) -> list[str | None]:
    try:
        parsed = _parse_args(args_source)
    except SyntaxError:
        return []
    return [
        _literal_or_name_or_none(arg)
        for arg in parsed
        if not isinstance(arg, ast.keyword)
    ]


def _line_for_node(source: str, node_id: str) -> int:
    for call in _iter_source_calls(source):
        args = _positional_arg_ids(call.args_source)
        if args and args[0] == node_id:
            return call.line
    return 0


def _line_for_relation(
    source: str,
    endpoints: tuple[str, str],
    occurrence: int,
) -> int:
    seen = 0
    for call in _iter_source_calls(source):
        if call.name not in {"Rel", "Association"}:
            continue
        args = _positional_arg_ids(call.args_source)
        if len(args) < 2 or (args[0], args[1]) != endpoints:
            continue
        if seen == occurrence:
            return call.line
        seen += 1
    return 0


# ── Import helpers ───────────────────────────────────────────────────────────


def _imports_of(py_files: Iterable[Path]) -> set[str]:
    """All ``mdg_drawio.*`` import sources found in the given .py files.

    Captures both ``from mdg_drawio.x import ...`` and plain
    ``import mdg_drawio.x`` — the latter is a real dependency too (see
    ``engine/preload.py``), so it must not be invisible to the checker.
    """
    imports: set[str] = set()
    for pf in py_files:
        try:
            tree = ast.parse(pf.read_text())
        except (SyntaxError, FileNotFoundError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
    return imports


def _collect_package_imports(pkg_name: str) -> set[str]:
    """All ``mdg_drawio.*`` import sources found in a package's .py files."""
    pkg_dir = REPO_ROOT / "mdg_drawio" / pkg_name
    if pkg_dir.is_dir():
        py_files = list(pkg_dir.rglob("*.py"))
    else:
        pyfile = REPO_ROOT / "mdg_drawio" / f"{pkg_name}.py"
        py_files = [pyfile] if pyfile.exists() else []
    return _imports_of(py_files)


def _collect_component_imports(component: str) -> set[str]:
    """All ``mdg_drawio.*`` imports made by the modules that realize *component*.

    This scopes cross-package verification to the Component level: only the
    module(s) actually realizing the source Component count, so an unrelated
    sibling module importing the target can no longer satisfy the edge.
    """
    modules = COMPONENT_TO_MODULE.get(component) or set()
    return _imports_of(PKG_DIR / rel for rel in modules)


def _collect_all_cross_package_imports() -> set[tuple[str, str]]:
    """Return {(src_pkg, tgt_pkg)} for all cross-package imports."""
    pairs: set[tuple[str, str]] = set()
    pkg_dir = REPO_ROOT / "mdg_drawio"
    for pyfile in pkg_dir.rglob("*.py"):
        rel = pyfile.relative_to(pkg_dir)
        parts = rel.parts
        src_pkg = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
        if src_pkg in ("__init__", "__main__"):
            continue
        try:
            tree = ast.parse(pyfile.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                if module.startswith("mdg_drawio."):
                    tgt_pkg = module.split(".")[1]
                    if tgt_pkg != src_pkg:
                        pairs.add((src_pkg, tgt_pkg))
    return pairs


# ── Checks ───────────────────────────────────────────────────────────────────


def check_forward(component_page: Page) -> int:
    """Every notation component must have at least one Rel edge."""
    boundary_ids = notation_boundary_ids(component_page)
    if not boundary_ids:
        print("FAIL [forward]: no notation boundary found on Component page")
        return 1

    comps = components_in_boundary(component_page, boundary_ids)
    if not comps:
        print("FAIL [forward]: no components inside notation boundary")
        return 1

    notation_ids = {c.id for c in comps}
    relations = extract_relations(component_page)
    referenced = {r.source for r in relations} | {r.target for r in relations}
    orphans = notation_ids - referenced

    print(f"  Notation components: {len(notation_ids)}")
    print(f"  Relations:           {len(relations)}")
    print(f"  Referenced ids:      {len(referenced)}")

    if orphans:
        for oid in sorted(orphans):
            print(f"  ORPHAN: {oid}")
        return 1

    print("  [OK] all Notation components have at least one edge")
    return 0






def _classify_notation(name: str, is_dir: bool) -> str | None:
    """Sub-classifier for files under notation/.  Returns None to fall through."""
    if name == "shape-registry.schema.json":
        return "skip"
    if name.endswith("_shapes_coverage.mdg"):
        return "skip"
    if is_dir and name == "_core":
        return "skip"
    if is_dir and name in EXPECTED_LIBRARIES:
        return "library"
    if name.endswith("_registry.yaml"):
        return "registry_yaml"
    return None  # fall through to MODULE_TO_LABEL


def _classify_entry(rel_str: str, is_dir: bool) -> str | None:
    """Categorise a mdg_drawio/ entry.  Returns category or None (unaccounted)."""
    name = rel_str.rsplit("/", 1)[-1]

    # Noise.
    if name == ".DS_Store":
        return "skip"
    if "__pycache__" in rel_str.split("/"):
        return "skip"
    if rel_str.startswith("generated_data"):
        return "skip"

    # Package glue.
    if name == "__init__.py":
        return "skip"
    if rel_str in SKIP_FILES:
        return "skip"

    # Container directories (covered by boundary checks).
    if is_dir and rel_str in SKIP_CONTAINER_DIRS:
        return "skip"

    # Notation-specific.
    if rel_str.startswith("notation/"):
        result = _classify_notation(name, is_dir)
        if result is not None:
            return result

    # Modules with expected labels.
    if rel_str in MODULE_TO_LABEL:
        return "module"

    return None



def _check_boundaries(component_page: Page) -> int:
    """Verify all expected boundaries exist in declared ids."""
    declared = extract_all_declared_ids(component_page)
    errors = 0
    for bid in EXPECTED_BOUNDARIES:
        if bid not in declared:
            print(f"  MISSING BOUNDARY: {bid}")
            errors += 1
    return errors


def _check_collectives(component_page: Page) -> int:
    """Verify notation collective components exist."""
    notation_ids = notation_boundary_ids(component_page)
    notation_labels = {
        c.label for c in components_in_boundary(component_page, notation_ids)
    }
    errors = 0
    for substr in COLLECTIVE_COMPONENT_SUBSTRS:
        if not any(substr.lower() in lbl.lower() for lbl in notation_labels):
            print(f'  MISSING COMPONENT: no label contains "{substr}"')
            errors += 1
    return errors


def check_reverse(component_page: Page) -> int:
    """Exhaustive walk: every file under mdg_drawio/ must be accounted for."""
    all_labels = {c.label for c in extract_components(component_page)}
    errors = 0
    accounted = 0
    skipped = 0

    for entry in sorted(PKG_DIR.rglob("*")):
        rel_str = str(entry.relative_to(PKG_DIR))
        category = _classify_entry(rel_str, entry.is_dir())

        if category is None:
            print(f"  UNACCOUNTED: {rel_str} - no category matches this file")
            errors += 1
        elif category == "skip":
            skipped += 1
        elif category in ("library", "registry_yaml"):
            accounted += 1
        elif category == "module":
            expected = MODULE_TO_LABEL[rel_str]
            if not any(expected.lower() in lbl.lower() for lbl in all_labels):
                print(
                    f"  MISSING COMPONENT: {rel_str} "
                    f'- no label contains "{expected}"'
                )
                errors += 1
            else:
                accounted += 1

    errors += _check_boundaries(component_page)
    errors += _check_collectives(component_page)

    if errors:
        return 1

    print(f"  Files accounted: {accounted}")
    print(f"  Files skipped:   {skipped}")
    print("  [OK] all mdg_drawio files are covered by MDG components")
    return 0


# ── Remaining checks ─────────────────────────────────────────────────────────


def check_edge_integrity(pages: Iterable[Page]) -> int:
    """Every relationship endpoint on every page must reference a declared id."""
    errors = 0
    total_declared = 0
    total_relations = 0

    for page in pages:
        declared = extract_all_declared_ids(page)
        relations = extract_relations(page)
        total_declared += len(declared)
        total_relations += len(relations)
        if relations:
            print(f"  {page.name}: {len(relations)} relationships")
        for r in relations:
            if r.source not in declared:
                print(
                    f"  DANGLING SOURCE: {page.name} line {r.line}: "
                    f"{r.source} -> {r.target}"
                )
                errors += 1
            if r.target not in declared:
                print(
                    f"  DANGLING TARGET: {page.name} line {r.line}: "
                    f"{r.source} -> {r.target}"
                )
                errors += 1

    if errors:
        return 1

    if total_relations == 0:
        print("FAIL [edge integrity]: no relationships found")
        return 1

    print(f"  Declared ids:   {total_declared}")
    print(f"  Relationships:  {total_relations}")
    print("  [OK] all relationship endpoints reference declared ids")
    return 0


def check_relationship_snapshot(pages: Iterable[Page]) -> int:
    """Tripwire for accidental relationship additions/removals per page."""
    counts = {page.name: len(page.relations) for page in pages}
    errors = 0
    for page_name, expected in EXPECTED_RELATION_COUNTS.items():
        actual = counts.get(page_name)
        if actual != expected:
            print(
                f"  RELATION COUNT MISMATCH: {page_name}: "
                f"expected {expected}, got {actual if actual is not None else 0}"
            )
            errors += 1

    extra_pages = sorted(set(counts) - set(EXPECTED_RELATION_COUNTS))
    for page_name in extra_pages:
        print(f"  UNEXPECTED PAGE IN SNAPSHOT: {page_name}")
        errors += 1

    if errors:
        return 1

    for page_name in EXPECTED_RELATION_COUNTS:
        print(f"  {page_name}: {counts[page_name]} relationships")
    print("  [OK] relationship counts match the architecture snapshot")
    return 0


def _nodes_by_id(page: Page) -> dict[str, Node]:
    return {node.id: node for node in page.nodes}


def _check_context_semantics(page: Page) -> int:
    nodes = _nodes_by_id(page)
    errors = 0
    for relation in page.relations:
        for endpoint in (relation.source, relation.target):
            node = nodes.get(endpoint)
            if node is None:
                continue
            if node.type not in CONTEXT_RELATION_TYPES:
                print(
                    f"  CONTEXT POLICY: line {relation.line}: {endpoint} "
                    f"is {node.type}, expected actor/system endpoint"
                )
                errors += 1
    return errors


def _check_container_semantics(page: Page) -> int:
    nodes = _nodes_by_id(page)
    errors = 0
    for relation in page.relations:
        for endpoint in (relation.source, relation.target):
            if endpoint not in CONTAINER_ALLOWED_IDS:
                node = nodes.get(endpoint)
                node_type = node.type if node else "<missing>"
                print(
                    f"  CONTAINER POLICY: line {relation.line}: {endpoint} "
                    f"is {node_type}, not a known package/external/data endpoint"
                )
                errors += 1

    for endpoint, rel_path in CONTAINER_PACKAGE_IDS.items():
        if not (PKG_DIR / rel_path).exists():
            print(f"  CONTAINER POLICY: {endpoint} maps to missing {rel_path}")
            errors += 1
    return errors


def _check_code_semantics(page: Page) -> int:
    nodes = _nodes_by_id(page)
    errors = 0
    for relation in page.relations:
        for endpoint in (relation.source, relation.target):
            node = nodes.get(endpoint)
            if node is None:
                continue
            if node.element_name not in CODE_RELATION_ELEMENT_NAMES:
                print(
                    f"  CODE POLICY: line {relation.line}: {endpoint} "
                    f"is uml.{node.element_name}, expected package/class/component"
                )
                errors += 1
    return errors


def check_page_relationship_semantics(pages: Iterable[Page]) -> int:
    """Architecture-specific relationship endpoint policies by page."""
    errors = 0
    for page in pages:
        if page.name == "Context":
            errors += _check_context_semantics(page)
        elif page.name == "Container":
            errors += _check_container_semantics(page)
        elif page.name == "Code":
            errors += _check_code_semantics(page)

    if errors:
        return 1

    print("  Context: actor/system relationships only")
    print("  Container: package/external/data endpoints only")
    print("  Code: package/class/component associations only")
    print("  [OK] page relationship semantics match architecture policy")
    return 0


def _module_imports_target(src_rel: str, tgt_rel: str) -> bool:
    """Check if src file imports tgt module via relative import."""
    src_path = PKG_DIR / src_rel
    tgt_module = tgt_rel.rsplit("/", 1)[-1].removesuffix(".py")
    try:
        tree = ast.parse(src_path.read_text())
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
            if node.module and node.module.endswith(tgt_module):
                return True
            if node.module is None:
                # from . import tgt_module
                if any(a.name == tgt_module for a in node.names):
                    return True
    return False


def _verify_cross_package_import(
    r: Relation,
    mdg_pairs: set[tuple[str, str]],
    src_pkg: str,
    tgt_pkg: str,
) -> RelationCheckResult:
    """Verify one cross-package MDG edge has a matching Component-level import.

    The source imports are scoped to the module(s) that realize the source
    Component (via COMPONENT_TO_MODULE). Only endpoints with no realizing
    module — boundaries and non-code ids — fall back to whole-package imports.
    """
    mdg_pairs.add((src_pkg, tgt_pkg))
    if r.source in COMPONENT_TO_MODULE:
        src_imports = _collect_component_imports(r.source)
        scope = f"{r.source} modules"
    else:
        src_imports = _collect_package_imports(src_pkg)
        scope = f"{src_pkg}/"
    expected = f"mdg_drawio.{tgt_pkg}"
    if expected not in src_imports:
        print(
            f"  MISSING IMPORT: line {r.line}: "
            f"{r.source}({src_pkg}) -> {r.target}({tgt_pkg}) "
            f'- no import of "{expected}" in {scope}'
        )
        return RelationCheckResult(errors=1)
    return RelationCheckResult(errors=0, coverage="import")


def _verify_intra_package_import(r: Relation) -> RelationCheckResult:
    """Verify one intra-package MDG edge has a matching relative import.

    Components may be realized by several modules (see COMPONENT_TO_MODULE), so
    the edge holds when *any* source module imports *any* target module.
    """
    src_mods = COMPONENT_TO_MODULE.get(r.source) or set()
    tgt_mods = COMPONENT_TO_MODULE.get(r.target) or set()
    if not src_mods or not tgt_mods:
        print(
            f"  MISSING MODULE MAPPING: line {r.line}: "
            f"{r.source} -> {r.target} - declare a realized-by trace "
            "or classify the edge in NON_IMPORT_RELATIONS"
        )
        return RelationCheckResult(errors=1)

    pairs = [(s, t) for s in sorted(src_mods) for t in sorted(tgt_mods) if s != t]
    if not pairs:
        print(
            f"  UNCLASSIFIED SAME-MODULE RELATION: line {r.line}: "
            f"{r.source} -> {r.target} ({sorted(src_mods)}) "
            "- classify the edge in NON_IMPORT_RELATIONS"
        )
        return RelationCheckResult(errors=1)

    if not any(_module_imports_target(s, t) for s, t in pairs):
        print(
            f"  MISSING IMPORT: line {r.line}: "
            f"{r.source}({sorted(src_mods)}) -> {r.target}({sorted(tgt_mods)}) "
            "- no relative import between the mapped modules"
        )
        return RelationCheckResult(errors=1)
    return RelationCheckResult(errors=0, coverage="import")


def _verify_edge_import(
    r: Relation,
    mdg_pairs: set[tuple[str, str]],
) -> RelationCheckResult:
    """Verify one Component-page relationship is covered by the checker."""
    if (r.source, r.target) in NON_IMPORT_RELATIONS:
        return RelationCheckResult(errors=0, coverage="non_import")

    missing = [
        endpoint
        for endpoint in (r.source, r.target)
        if endpoint not in ID_TO_PACKAGE
    ]
    if missing:
        print(
            f"  UNMAPPED RELATION ENDPOINT: line {r.line}: "
            f"{r.source} -> {r.target} - missing {', '.join(missing)} "
            "from ID_TO_PACKAGE"
        )
        return RelationCheckResult(errors=1)

    src_pkg = ID_TO_PACKAGE.get(r.source)
    tgt_pkg = ID_TO_PACKAGE.get(r.target)
    if src_pkg is None or tgt_pkg is None:
        print(
            f"  UNCLASSIFIED NON-IMPORT RELATION: line {r.line}: "
            f"{r.source} -> {r.target} - classify the edge in "
            "NON_IMPORT_RELATIONS"
        )
        return RelationCheckResult(errors=1)

    if src_pkg != tgt_pkg:
        return _verify_cross_package_import(r, mdg_pairs, src_pkg, tgt_pkg)

    return _verify_intra_package_import(r)


def check_import_integrity(component_page: Page) -> int:
    """Bidirectional: MDG edges <-> imports (cross-package + intra-package)."""
    relations = extract_relations(component_page)
    errors = 0
    mdg_pairs: set[tuple[str, str]] = set()
    import_verified = 0
    non_import = 0

    for r in relations:
        result = _verify_edge_import(r, mdg_pairs)
        errors += result.errors
        if result.coverage == "import":
            import_verified += 1
        elif result.coverage == "non_import":
            non_import += 1

    # Reverse: cross-package import -> MDG edge.
    code_pairs = _collect_all_cross_package_imports()
    for src_pkg, tgt_pkg in sorted(code_pairs):
        if (src_pkg, tgt_pkg) not in mdg_pairs:
            print(
                f"  MISSING EDGE: import mdg_drawio.{tgt_pkg} in {src_pkg}/ "
                f"- no matching c4.Rel on Component page"
            )
            errors += 1

    if errors:
        return 1

    covered = import_verified + non_import
    if covered != len(relations):
        print(
            f"  UNCOVERED RELATIONSHIPS: covered {covered} of {len(relations)}"
        )
        return 1

    print(f"  Component relationships: {len(relations)}")
    print(f"  Import-verified edges:   {import_verified}")
    print(f"  Explicit non-import:     {non_import}")
    print(f"  Cross-package edges:       {len(mdg_pairs)}")
    print(f"  Codebase cross imports:    {len(code_pairs)}")
    print("  [OK] all import-verifiable edges match")
    return 0


def check_view_containment(pages: Iterable[Page]) -> int:
    """Cross-view: Container page ↔ Component page agree on containers.

    - Every ``Container_Boundary`` on the Component page maps to a Container
      declared on the Container page.
    - Every code Container on the Container page is decomposed on the Component
      page (as a boundary, or ≥1 Component that resolves to that package).
    """
    by_name = {p.name: p for p in pages}
    container_page = by_name.get("Container")
    component_page = by_name.get("Component")
    if container_page is None or component_page is None:
        print("  MISSING PAGE: both Container and Component pages are required")
        return 1

    containers = {n.id for n in container_page.nodes if n.element_name == "Container"}
    boundaries = {
        n.id for n in component_page.nodes if n.element_name == "Container_Boundary"
    }
    component_pkgs = {
        ID_TO_PACKAGE.get(n.id)
        for n in component_page.nodes
        if n.element_name == "Component"
    }

    errors = 0
    for boundary in sorted(boundaries):
        pkg = boundary.removesuffix("_co_boundary")
        if pkg not in containers:
            print(
                f"  BOUNDARY WITHOUT CONTAINER: {boundary} -> {pkg!r} "
                "is not a Container on the Container page"
            )
            errors += 1

    for container in sorted(containers & set(CONTAINER_PACKAGE_IDS)):
        decomposed = (
            f"{container}_co_boundary" in boundaries or container in component_pkgs
        )
        if not decomposed:
            print(
                f"  CONTAINER NOT DECOMPOSED: Container {container!r} has no "
                "boundary or Component on the Component page"
            )
            errors += 1

    if errors:
        return 1
    print(f"  Containers: {len(containers & set(CONTAINER_PACKAGE_IDS))} code, "
          f"{len(boundaries)} boundaries — views agree")
    print("  [OK] Container and Component views are consistent")
    return 0


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    mdg_paths = [Path(arg) for arg in sys.argv[1:]] if len(sys.argv) > 1 else [
        *DEFAULT_MDG_PATHS
    ]

    pages: list[Page] = []
    for mdg_path in mdg_paths:
        source = mdg_path.read_text()
        try:
            pages.extend(extract_pages(source))
        except (DslError, ValueError) as exc:
            print(f"Error in {mdg_path}: {exc}")
            return 1
    component_page: Page | None = None
    for p in pages:
        if p.name == "Component":
            component_page = p
            break

    if component_page is None:
        print("Error: no Component page found in MDG file")
        return 1

    print("=== Forward check (notation components -> edges) ===")
    fwd = check_forward(component_page) == 0

    print("\n=== Reverse check (codebase -> MDG components) ===")
    rev = check_reverse(component_page) == 0

    print("\n=== Edge integrity (all relationship endpoints exist) ===")
    edge = check_edge_integrity(pages) == 0

    print("\n=== Relationship snapshot (expected page counts) ===")
    snapshot = check_relationship_snapshot(pages) == 0

    print("\n=== Page relationship semantics ===")
    semantics = check_page_relationship_semantics(pages) == 0

    print("\n=== Import integrity (Component relationships <-> imports) ===")
    imp = check_import_integrity(component_page) == 0

    print("\n=== View containment (Container page <-> Component page) ===")
    containment = check_view_containment(pages) == 0

    if fwd and rev and edge and snapshot and semantics and imp and containment:
        print("\nAll checks pass: MDG and codebase are in sync.")
        return 0

    print("\nOne or more checks failed. See details above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
