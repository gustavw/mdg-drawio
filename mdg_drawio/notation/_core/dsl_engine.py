"""Shared DSL engine primitives — notation-agnostic.

These are the building blocks every notation parser reuses: call matching,
argument parsing, page splitting, frontmatter stripping, and the indent-based
container-stack block parser.

Improvements over the DrawIoGen reference:
- All public functions have full type annotations.
- Error messages always include line numbers.
- No mutable module-level global state.
- Parser functions are pure: input → output, no side effects.
"""


from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from mdg_drawio.contracts import (
    PAGE_PREFIX_LENGTH,
    QUOTE_OFFSET,
    Diagram,
    Document,
    Edge,
    MultiPageDocument,
    Node,
)

# The DSL engine holds no registry cache of its own: it reads through
# ``registry`` (the single pre-load cache) via ``shapes_by_function``.

# draw.io's own structural/annotation palette pages (as opposed to a real
# notation library like erd/uml25/c4/bpmn2/archimate3): a wildcard
# ``contains: {allowed: ['*']}`` container in one of these namespaces is a
# freeform visual grouping with no metamodel semantics, so it may legitimately
# hold shapes from any other library. Only "general" is registered today
# (see general_registry.yaml); the rest are listed so they inherit the same
# exception the moment they are registered.
_STRUCTURAL_NAMESPACES: frozenset[str] = frozenset(
    {"general", "misc", "advanced", "basic", "arrows", "flowchart"}
)

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

CALL_RE: re.Pattern[str] = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?P<call>[A-Za-z_][A-Za-z0-9_]*)\.)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)\s*(?P<colon>:?)\s*$"
)

# Lines the block parser skips silently. ``use ``/``trace `` are model-only
# statements consumed by the traceability meta-model (scripts/check_traceability.py),
# never rendered as draw.io nodes or edges.
_SKIP_PREFIXES = (
    "title:", "mode:", "notation:", "use ", "trace ", "layout:", "direction:",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DslError(Exception):
    """A DSL syntax or semantic error with a line number."""

    def __init__(self, message: str, line_number: int | None = None) -> None:
        loc = f"line {line_number}: " if line_number is not None else ""
        super().__init__(f"{loc}{message}")
        self.line_number = line_number


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def strip_inline_comment(line: str) -> str:
    """Strip a trailing ``#`` comment, respecting quoted strings."""
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


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def parse_frontmatter(source: str) -> tuple[dict[str, str], str]:
    """Strip YAML frontmatter delimited by ``---`` from *source*.

    Returns ``(metadata_dict, body)``. If no frontmatter is present,
    metadata is empty and body is the full source unchanged.
    """
    lines = source.lstrip("\n").split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, source
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, source
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, raw = line.partition(":")
            value = raw.strip().strip('"').strip("'")
            metadata[key.strip()] = value
    return metadata, "\n".join(lines[end + 1:])


_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALSE_VALUES = frozenset({"false", "0", "no"})


def parse_bool_metadata(metadata: dict[str, str], key: str) -> bool:
    """Parse a boolean frontmatter value (e.g. ``grid: true``).

    Absent -> False. An unrecognized value is a loud error, not a silent
    falsy default.
    """
    raw = metadata.get(key, "")
    if not raw:
        return False
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise DslError(f"invalid `{key}: {raw}` in frontmatter; expected true or false")


# ---------------------------------------------------------------------------
# Block (text) variables
# ---------------------------------------------------------------------------

_BLOCK_VAR_RE = re.compile(
    r'^[ \t]*block[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*"""'
    r'(?P<content>.*?)"""[ \t]*$',
    re.MULTILINE | re.DOTALL,
)


def _dedent_block_content(content: str) -> str:
    """Drop exactly one leading and one trailing newline, if present -- the
    fence lines (the ``\"\"\"`` and the line it's on) are punctuation, not
    content, the same convention a Python docstring or a YAML ``|`` block
    scalar uses."""
    if content.startswith("\r\n"):
        content = content[2:]
    elif content.startswith("\n"):
        content = content[1:]
    if content.endswith("\r\n"):
        content = content[:-2]
    elif content.endswith("\n"):
        content = content[:-1]
    return content


def extract_block_variables(source: str) -> tuple[dict[str, str], str]:
    """Extract every ``block NAME = \"\"\"...\"\"\"`` declaration from *source*.

    A block variable holds free-form multi-line text (typically markdown --
    see :mod:`mdg_drawio.markup`) that stands in for a quoted string literal
    anywhere a call argument expects one (see ``literal_value``):
    ``general.Text(n1, my_text)`` reads exactly as if ``my_text`` had been
    written inline as a quoted string, once ``my_text`` is a declared block.

    Declarations are recognized file-wide and order-independent -- a block
    is commonly placed BELOW the call that references it, so this runs as
    one whole-source pre-pass before any per-line/per-page parsing, not a
    top-to-bottom "declare before use" scan.

    Returns ``(variables, source_with_declarations_removed)``. A duplicate
    name keeps its FIRST declaration (defensive; mirrors the reverse
    module's ``index_existing`` precedent for malformed input). An
    unclosed ``block NAME = \"\"\"`` (no matching closing fence anywhere)
    is not touched here -- it falls through to the ordinary per-line
    parser, which reports it as invalid syntax with a line number, same as
    any other malformed call.
    """
    variables: dict[str, str] = {}

    def _consume(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in variables:
            variables[name] = _dedent_block_content(match.group("content"))
        return ""

    cleaned = _BLOCK_VAR_RE.sub(_consume, source)
    return variables, cleaned


# ---------------------------------------------------------------------------
# Page splitting
# ---------------------------------------------------------------------------

def _frontmatter_prefix(raw_lines: list[str]) -> tuple[list[str], int]:
    """Return frontmatter lines and index where the body starts.

    If the frontmatter contains a ``page:`` field, it is per-page frontmatter
    and should be included in the body — return empty frontmatter.
    """
    if not raw_lines or raw_lines[0].rstrip() != "---":
        return [], 0
    try:
        end = raw_lines.index("---", 1)
    except ValueError:
        return [], 0
    if end <= 0:
        return [], 0
    fm_lines = ["---", *raw_lines[1:end], "---"]
    if "page:" in "\n".join(fm_lines):
        return [], 0
    return fm_lines, end + 1


def _page_name_from_statement(stripped: str) -> str:
    """Extract the page name from a stripped page statement."""
    quote = stripped[PAGE_PREFIX_LENGTH]
    return stripped[QUOTE_OFFSET:].rstrip(quote + " \t")


def _has_per_page_frontmatter(body_lines: list[str]) -> bool:
    """True if the body uses ``---`` + ``page:`` per-page frontmatter."""
    return any(line.strip() == "---" for line in body_lines)


def _collect_page_sections(
    body_lines: list[str],
    default_title: str,
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Collect global header lines and page-specific body lines."""
    if _has_per_page_frontmatter(body_lines):
        return [], _collect_per_page_sections(body_lines)
    return _collect_legacy_page_sections(body_lines, default_title)


def _collect_per_page_sections(
    body_lines: list[str],
) -> list[tuple[str, list[str]]]:
    """Parse per-page ``---`` + ``page:`` YAML frontmatter blocks.

    The frontmatter block is retained at the head of each page section so the
    per-page parser can recover *all* its metadata (``mode``, ``direction``, …)
    via ``parse_frontmatter`` — not just the ``page:`` name used to delimit
    sections here.
    """
    sections: list[tuple[str, list[str]]] = []
    current_name = ""
    current_lines: list[str] = []
    fm_lines: list[str] = []
    in_frontmatter = False

    def flush() -> None:
        if fm_lines or current_lines:
            sections.append((current_name, [*fm_lines, *current_lines]))

    for line in body_lines:
        stripped = line.strip()

        if stripped == "---" and not in_frontmatter:
            in_frontmatter = True
            flush()
            current_name = ""
            current_lines = []
            fm_lines = [line]
            continue

        if in_frontmatter:
            fm_lines.append(line)
            if stripped.startswith("page:"):
                current_name = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            elif stripped == "---":
                in_frontmatter = False
            continue

        current_lines.append(line)

    flush()
    return sections


def _collect_legacy_page_sections(
    body_lines: list[str],
    default_title: str,
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Parse legacy ``page "Name"`` statement sections."""
    global_header: list[str] = []
    page_sections: list[tuple[str, list[str]]] = []
    current_name = default_title
    current_lines: list[str] | None = None

    for line in body_lines:
        stripped = line.strip()
        if stripped.startswith('page "') or stripped.startswith("page '"):
            if current_lines is not None:
                page_sections.append((current_name, current_lines))
            current_name = _page_name_from_statement(stripped)
            current_lines = []
        elif current_lines is None:
            global_header.append(line)
        else:
            current_lines.append(line)

    if current_lines is not None:
        page_sections.append((current_name, current_lines))

    return global_header, page_sections


def _prefix_page_sources(
    frontmatter_prefix: list[str],
    global_header: list[str],
    page_sections: list[tuple[str, list[str]]],
) -> list[tuple[str, str]]:
    """Prepend global page context to every page body."""
    header_lines = [*frontmatter_prefix, *global_header]
    header_prefix = "\n".join(header_lines)
    if header_prefix:
        header_prefix += "\n"
    return [
        (name, header_prefix + "\n".join(lines))
        for name, lines in page_sections
    ]


def split_pages(source: str) -> list[tuple[str, str]]:
    """Split a DSL source on ``page "Name"`` statements.

    The global header (frontmatter + ``use``/``notation`` lines before the
    first page) is prepended to every page section so each can be parsed
    independently.

    Returns ``[(page_name, page_source), ...]``. If no page statement is
    found, returns a single entry with the frontmatter title or empty name.
    """
    raw_lines = source.lstrip("\n").split("\n")
    frontmatter_prefix, body_start = _frontmatter_prefix(raw_lines)
    body_lines = raw_lines[body_start:]

    metadata, _ = parse_frontmatter(source)
    default_title = metadata.get("title", "")

    global_header, page_sections = _collect_page_sections(
        body_lines, default_title
    )

    if not page_sections:
        return [(default_title, "\n".join(body_lines))]

    return _prefix_page_sources(frontmatter_prefix, global_header, page_sections)



# ---------------------------------------------------------------------------
# Multi-page document construction
# ---------------------------------------------------------------------------

def parse_call_arguments(
    source: str, line_number: int = 0
) -> list[ast.AST | ast.keyword]:
    """Parse a comma-separated argument list using Python's AST.

    Returns a list of ``ast.AST`` positional args and ``ast.keyword`` kwargs.
    """
    if not source.strip():
        return []
    try:
        expr = ast.parse(f"f({source})", mode="eval").body
    except SyntaxError as exc:
        raise DslError(f"invalid argument list: {source!r}", line_number) from exc
    if not isinstance(expr, ast.Call):
        raise DslError(f"invalid argument list: {source!r}", line_number)
    return [*expr.args, *expr.keywords]


def literal_value(
    node: ast.AST, field_name: str = "", blocks: dict[str, str] | None = None
) -> object:
    """Extract a Python literal or identifier from an AST node.

    A bare identifier that names a declared ``block`` variable (see
    ``extract_block_variables``) resolves to that block's text instead of
    its own spelling. This is the ONLY place that substitution happens --
    id-typed extraction (``literal_or_name``/``_identifier_like_value``)
    never takes *blocks*, so a block variable can never satisfy a
    node_id/source/target argument by accident.
    """
    if isinstance(node, ast.Name):
        if blocks and node.id in blocks:
            return blocks[node.id]
        return node.id
    if isinstance(node, ast.Constant):
        return node.value
    raise ValueError(f"{field_name} must be a literal or identifier")


def literal_string(
    node: ast.AST, field_name: str = "", blocks: dict[str, str] | None = None
) -> str:
    """Extract a string literal from an AST node."""
    value = literal_value(node, field_name, blocks)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")
    return value


def literal_or_name(node: ast.AST, field_name: str = "") -> str:
    """Extract a string or identifier from an AST node.

    Handles hyphenated ids like ``some-id-1`` which parse as subtraction
    expressions in Python.
    """
    value = _identifier_like_value(node)
    if value is None:
        raise ValueError(f"{field_name} must be a string or identifier")
    return value


def _identifier_like_value(node: ast.AST) -> str | None:
    """Recover a hyphenated identifier from a chain of subtraction AST nodes."""
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


def parse_keyword_int(
    kwargs: dict[str, str | int | float],
    key: str,
    default: int = 1,
    line_number: int = 0,
) -> int:
    """Extract an integer kwarg value."""
    raw = kwargs.get(key)
    if raw is None:
        return default
    # bool is a subclass of int; reject it so ``variant=True`` is not silently 1.
    if isinstance(raw, bool):
        raise DslError(f"{key}= must be an integer, got {raw!r}", line_number)
    if isinstance(raw, (int, float)):
        return int(raw)
    raise DslError(f"{key}= must be an integer, got {raw!r}", line_number)


# ---------------------------------------------------------------------------
# Block parser (indent-based container-stack)
# ---------------------------------------------------------------------------

class NodeBuilder(Protocol):
    """Protocol for constructing a node from a DSL call."""

    def __call__(
        self, function: str, args: list[ast.AST | ast.keyword], line_number: int
    ) -> Node: ...


class EdgeBuilder(Protocol):
    """Protocol for constructing an edge from a DSL call."""

    def __call__(
        self, function: str, args: list[ast.AST | ast.keyword], line_number: int
    ) -> Edge: ...


class IsEdge(Protocol):
    """Protocol for testing whether a DSL call represents an edge."""

    def __call__(self, function: str, args: list[ast.AST | ast.keyword]) -> bool: ...


@dataclass(frozen=True)
class _ParsedBlockCall:
    """Parsed state for one block-syntax call line."""

    namespace: str
    name: str
    args_source: str
    indent: int
    opens_block: bool


@dataclass(frozen=True)
class _ContainerFrame:
    """One open block on the container stack.

    Both a ``rows.allowed`` shape (UML class members, ERD table rows) and a
    ``contains`` shape (BPMN pools, C4 boundaries) nest their children as
    real ``Node``s with ``parent_id`` set to this frame's ``node_id`` -- the
    palette style backing a rows-shape is a genuine draw.io swimlane
    (``childLayout=stackLayout``/``tableLayout``), not a static compartment,
    so real contained nodes are the correct representation for both, and the
    container layout engine already positions them that way (see
    ``tests/test_pipeline.py::test_stacklayout_container_children_stack_tightly``).
    The only difference between the two is which function names are legal
    directly beneath this frame -- that's what ``allowed`` validates.

    ``allowed`` of ``None`` means unrestricted: either a genuine
    ``contains: {allowed: ['*']}`` entry, or an unregistered function kept
    lenient for backward compatibility. ``kind_label`` ("row"/"child") is
    only used to phrase a rejection's error message.
    """

    indent: int
    node_id: str
    namespace: str
    kind_label: str  # "row" | "child"
    allowed: frozenset[str] | None = None


def _process_registry_root(
    call: _ParsedBlockCall,
    line_number: int,
    container_stack: list[_ContainerFrame],
    state: dict[str, str],
) -> bool:
    """Consume a non-rendering registry root and apply its title as a fallback."""
    if call.name != _registry_root(call.namespace):
        return False
    root_args = parse_call_arguments(call.args_source, line_number)
    _pop_container_stack(container_stack, call.indent)
    positional = [arg for arg in root_args if not isinstance(arg, ast.keyword)]
    if positional and not state["diagram_name"]:
        title = _extract_arg_string(positional[0])
        if title:
            state["diagram_name"] = title
    return True


def _should_skip_block_line(line: str, stripped: str) -> bool:
    """Return True when a block parser line carries no DSL call."""
    return (
        not line
        or stripped.startswith("#")
        or stripped.startswith("---")
        or any(stripped.startswith(prefix) for prefix in _SKIP_PREFIXES)
    )


def _parse_block_call_line(
    raw_line: str,
    *,
    namespace: str,
    line_number: int,
) -> _ParsedBlockCall | None:
    """Parse one source line into call metadata, or None when skipped."""
    line = strip_inline_comment(raw_line.rstrip())
    stripped = line.lstrip()
    if _should_skip_block_line(line, stripped):
        return None

    match = CALL_RE.match(line)
    if match is None:
        raise DslError(
            f"invalid {namespace} DSL syntax: {line!r}", line_number
        )

    return _ParsedBlockCall(
        namespace=match.group("call") or namespace,
        name=match.group("name"),
        args_source=match.group("args"),
        indent=len(line) - len(stripped),
        opens_block=bool(match.group("colon")),
    )


def _pop_container_stack(
    container_stack: list[_ContainerFrame],
    indent: int,
) -> None:
    """Pop containers no longer in scope for the current indent level."""
    while container_stack and indent <= container_stack[-1].indent:
        container_stack.pop()


def parse_block_source(
    source: str,
    *,
    namespace: str,
    diagram_title_call: str,
    diagram_name_default: str,
    parse_diagram_title: Callable[
        [list[ast.AST | ast.keyword], int, str], str
    ],
    is_edge: IsEdge,
    build_node: NodeBuilder,
    build_edge: EdgeBuilder,
    blocks: dict[str, str] | None = None,
) -> tuple[list[Node], list[Edge], str]:
    """Parse a block-syntax DSL source into nodes, edges, and a diagram name.

    Handles the container stack (indent-based ``parent_id`` assignment) and
    ``opens_block`` detection (trailing colon). Notation-specific logic lives
    in the callbacks.

    *blocks* is the table of declared ``block`` variables (see
    ``extract_block_variables``), consulted for foreign-namespace passthrough
    calls only -- *build_node*/*build_edge* are responsible for consulting it
    themselves for own-namespace calls (typically by closing over it).

    Returns ``(nodes, edges, diagram_name)``.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    # Boxed so the per-call helper can update it in place.
    state: dict[str, str] = {"diagram_name": diagram_name_default}
    container_stack: list[_ContainerFrame] = []

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        call = _parse_block_call_line(
            raw_line, namespace=namespace, line_number=line_number
        )
        if call is None:
            continue

        # Builders and node/edge model validation raise plain ValueError/
        # TypeError with no line context; funnel them through DslError so the
        # documented "errors always include line numbers" contract holds.
        try:
            _process_block_call(
                call,
                line_number=line_number,
                namespace=namespace,
                diagram_title_call=diagram_title_call,
                parse_diagram_title=parse_diagram_title,
                is_edge=is_edge,
                build_node=build_node,
                build_edge=build_edge,
                nodes=nodes,
                edges=edges,
                container_stack=container_stack,
                state=state,
                blocks=blocks,
            )
        except (ValueError, TypeError) as exc:
            raise DslError(str(exc), line_number) from exc

    _ensure_endpoint_free_edge_ids_are_unique(nodes, edges)
    return nodes, edges, state["diagram_name"]


def _process_block_call(
    call: _ParsedBlockCall,
    *,
    line_number: int,
    namespace: str,
    diagram_title_call: str,
    parse_diagram_title: Callable[[list[ast.AST | ast.keyword], int, str], str],
    is_edge: IsEdge,
    build_node: NodeBuilder,
    build_edge: EdgeBuilder,
    nodes: list[Node],
    edges: list[Edge],
    container_stack: list[_ContainerFrame],
    state: dict[str, str],
    blocks: dict[str, str] | None = None,
) -> None:
    """Process one parsed block call, mutating nodes/edges/container_stack.

    ``state["diagram_name"]`` is updated in place when a diagram-title call is
    seen. Kept separate so ``parse_block_source`` can wrap each call uniformly.

    *blocks* (declared ``block`` variables) is only consulted for the
    foreign-namespace (passthrough) path below -- an own-namespace call goes
    through the injected ``build_node``/``build_edge`` callback instead,
    which the caller is responsible for having already bound to *blocks*
    itself (see ``parse_block_source``).
    """
    if _process_registry_root(call, line_number, container_stack, state):
        return

    if call.namespace != namespace:
        # Foreign namespace — look up the registry to determine if this
        # is an edge or a node, then build appropriately.
        foreign_args = parse_call_arguments(call.args_source, line_number)
        _pop_container_stack(container_stack, call.indent)
        foreign_variant = _passthrough_variant(foreign_args, line_number)

        registry_entry = _registry_entry(
            call.namespace, call.name, foreign_variant, line_number
        )
        declared_specs = _declared_args(call.namespace, call.name, registry_entry)
        _validate_keyword_args(
            call.namespace, call.name, foreign_args, declared_specs, line_number
        )
        _validate_nested_call(
            container_stack,
            call.namespace,
            call.name,
            registry_entry,
            line_number,
        )
        if registry_entry and registry_entry.get("kind") == "edge":
            # An unregistered function is never treated as an edge (the
            # `else` branch below always builds it as a node instead), so a
            # matched edge entry always has real declared_specs.
            assert declared_specs is not None
            _build_passthrough_edge(
                call.namespace,
                call.name,
                foreign_args,
                foreign_variant,
                line_number,
                edges,
                declared_specs,
                blocks,
            )
        else:
            node = _build_passthrough_node(
                call.namespace,
                call.name,
                foreign_args,
                foreign_variant,
                line_number,
                nodes,
                container_stack,
                declared_specs,
                blocks,
            )
            if call.opens_block:
                container_stack.append(
                    _open_container_frame(
                        node, registry_entry, call.namespace, call.name,
                        call.indent, line_number,
                    )
                )
        return

    args = parse_call_arguments(call.args_source, line_number)
    _pop_container_stack(container_stack, call.indent)
    variant = _passthrough_variant(args, line_number)
    registry_entry = _registry_entry(
        call.namespace, call.name, variant, line_number
    )
    declared_specs = _declared_args(
        call.namespace, call.name, registry_entry
    )
    _validate_keyword_args(
        call.namespace,
        call.name,
        args,
        declared_specs,
        line_number,
    )
    if declared_specs is not None:
        args = _normalize_registry_args(
            call.namespace, call.name, args, declared_specs, line_number
        )
    _validate_nested_call(
        container_stack,
        call.namespace,
        call.name,
        registry_entry,
        line_number,
    )

    if call.name == diagram_title_call:
        state["diagram_name"] = parse_diagram_title(
            args, line_number, state["diagram_name"]
        )
        return

    if is_edge(call.name, args):
        edges.append(build_edge(call.name, args, line_number))
        return

    node = build_node(call.name, args, line_number)
    if container_stack:
        node.parent_id = container_stack[-1].node_id
    nodes.append(node)

    if call.opens_block:
        container_stack.append(
            _open_container_frame(
                node,
                registry_entry,
                call.namespace,
                call.name,
                call.indent,
                line_number,
            )
        )

def _registry_entry(
    ns: str, function: str, variant: int, line_number: int
) -> dict[str, object] | None:
    """Resolve exactly ``(ns, function, variant)`` for passthrough dispatch.

    Unknown functions retain the parser's existing generic-node behavior.
    For a registered family, an undeclared variant is always an actionable,
    line-numbered error: otherwise generation silently falls back to another
    variant's style, even when node/edge classification happens to agree.
    """
    from .registry import shapes_by_function

    try:
        by_func = shapes_by_function(ns)
    except (KeyError, FileNotFoundError):
        return None
    entries = by_func.get(function, [])
    for entry in entries:
        if int(entry.get("variant", 1)) == variant:
            return entry
    if not entries:
        return None
    valid_variants = ", ".join(
        str(entry.get("variant", 1)) for entry in entries
    )
    raise DslError(
        f"{ns}.{function}(): unsupported variant {variant}; expected one of "
        f"{valid_variants}",
        line_number,
    )


def _classify_container_kind(
    entry: dict[str, object] | None,
) -> tuple[str, frozenset[str] | None]:
    """Decide what kind of block a shape's registry entry may open.

    Returns ``(kind_label, allowed)``. ``kind_label`` is one of:

    - ``"row"``: children are compartment rows (``rows.allowed`` is
      non-empty). ``allowed`` is that set.
    - ``"child"``: children are real contained nodes (a ``contains`` entry is
      present). ``allowed`` of ``None`` means unrestricted.
    - ``"none"``: the shape declares neither -- a block on it is invalid.

    An unresolved entry (unregistered function) is treated as unrestricted
    ``"child"``, preserving the parser's pre-Phase-2 lenient behavior for
    notations without full registry coverage. So is a ``kind: "diagram"``
    entry (a pre-composed, non-primitive reference fragment, e.g.
    ``uml25.Expansion`` -- ``buildable: false``, no ``rows``/``contains`` of
    its own): coverage sheets still demonstrate it with nested content, and
    it carries no registry contract to validate that content against.
    """
    if entry is None or entry.get("kind") == "diagram":
        return "child", None
    rows = entry.get("rows")
    rows_dict = rows if isinstance(rows, dict) else {}
    rows_allowed = frozenset(rows_dict.get("allowed") or [])
    if rows_allowed:
        return "row", rows_allowed
    contains = entry.get("contains")
    if isinstance(contains, dict):
        allowed = contains.get("allowed") or []
        return "child", None if allowed == ["*"] else frozenset(allowed)
    return "none", None


def _open_container_frame(
    node: Node,
    entry: dict[str, object] | None,
    ns: str,
    name: str,
    indent: int,
    line_number: int,
) -> _ContainerFrame:
    """Build the container-stack frame opened by *node*'s trailing colon."""
    kind_label, allowed = _classify_container_kind(entry)
    if kind_label == "none":
        raise DslError(
            f"{ns}.{name}(): shape has neither rows nor containment; it "
            f"cannot open a block",
            line_number,
        )
    return _ContainerFrame(
        indent=indent,
        node_id=node.id,
        namespace=ns,
        kind_label=kind_label,
        allowed=allowed,
    )


def _validate_child_allowed(
    parent_frame: _ContainerFrame,
    ns: str,
    name: str,
    entry: dict[str, object] | None,
    line_number: int,
) -> None:
    """Reject a child function not permitted by the parent's rows/contains.allowed."""
    if entry is not None and entry.get("kind") == "edge":
        raise DslError(
            f"{ns}.{name}(): edges cannot be nested inside "
            f"{parent_frame.node_id!r}",
            line_number,
        )
    same_namespace = ns == parent_frame.namespace
    if same_namespace:
        if parent_frame.allowed is None or name in parent_frame.allowed:
            return
    elif parent_frame.allowed is None and parent_frame.namespace in _STRUCTURAL_NAMESPACES:
        # A wildcard container on a structural palette page (general, misc,
        # advanced, basic, arrows, flowchart) is a freeform grouping, not a
        # notation-specific compartment -- it may hold any library's shapes.
        return
    if parent_frame.allowed is None:
        expected = f"{parent_frame.namespace}.*"
    else:
        allowed = ", ".join(sorted(parent_frame.allowed)) or "(none)"
        expected = f"{parent_frame.namespace}.{{{allowed}}}"
    raise DslError(
        f"{ns}.{name}(): not a valid {parent_frame.kind_label} of "
        f"{parent_frame.node_id!r}; expected {expected}",
        line_number,
    )


def _validate_nested_call(
    container_stack: list[_ContainerFrame],
    ns: str,
    name: str,
    entry: dict[str, object] | None,
    line_number: int,
) -> None:
    """Validate a call against its active parent, if it has one."""
    if container_stack:
        _validate_child_allowed(
            container_stack[-1], ns, name, entry, line_number
        )


def _declared_args(
    ns: str, function: str, entry: dict[str, object] | None
) -> list[dict[str, object]] | None:
    """Declared arg specs for ``(ns, function)``: from the resolved shape
    entry if one was found, else from a matching row type in the library's
    registry (row types have no shape entry of their own). ``None`` when
    neither exists -- an unregistered function, kept lenient (Phase 1
    precedent: unknown functions retain generic-node behavior). A
    ``kind: "diagram"`` entry (a pre-composed reference fragment, e.g.
    ``uml25.Expansion`` -- ``buildable: false``, no ``args`` of its own) gets
    the same lenient treatment as Phase 2's containment validation: it
    carries no real argument contract to bind against.
    """
    if entry is not None and entry.get("kind") != "diagram":
        entry_args = entry.get("args")
        if isinstance(entry_args, list):
            return [a for a in entry_args if isinstance(a, dict)]
        return []
    from .registry import load_registry

    try:
        registry = load_registry(ns)
    except (KeyError, FileNotFoundError):
        return None
    for row_type in registry.get("row_types", []):
        if not isinstance(row_type, dict) or row_type.get("name") != function:
            continue
        row_args = row_type.get("args")
        if isinstance(row_args, list):
            return [a for a in row_args if isinstance(a, dict)]
        return []
    return None


def _validate_keyword_args(
    ns: str,
    function: str,
    args: list[ast.AST | ast.keyword],
    declared_specs: list[dict[str, object]] | None,
    line_number: int,
) -> None:
    """Reject undeclared keywords for registered shapes and row types."""
    if declared_specs is None:
        return
    declared = {"variant"} | {str(spec["name"]) for spec in declared_specs}
    keywords = [kw for kw in args if isinstance(kw, ast.keyword)]
    unknown = sorted(kw.arg or "**kwargs" for kw in keywords if kw.arg not in declared)
    if unknown:
        raise DslError(
            f"{ns}.{function}(): unknown keyword argument(s): "
            f"{', '.join(unknown)}",
            line_number,
        )
    seen: set[str] = set()
    duplicates: list[str] = []
    for kw in keywords:
        if kw.arg is None:
            continue
        if kw.arg in seen:
            duplicates.append(kw.arg)
        seen.add(kw.arg)
    if duplicates:
        raise DslError(
            f"{ns}.{function}(): keyword argument supplied twice: "
            f"{', '.join(sorted(set(duplicates)))}",
            line_number,
        )


def _bind_registry_args(
    ns: str,
    function: str,
    args: list[ast.AST | ast.keyword],
    declared_specs: list[dict[str, object]],
    line_number: int,
) -> dict[str, ast.AST]:
    """Bind call arguments to declared registry arg names, Python-signature
    style: the registry entry is the signature, the call is bound against it.

    Positional call values fill ``passing: positional`` declared args
    left-to-right; keyword call values bind by name to any declared arg.
    Rejects excess positional arguments, an arg supplied both positionally
    and by keyword, and a missing required argument. ``variant=`` is handled
    separately (``Node.variant``) and is never a declared arg.
    """
    pos_args = [a for a in args if not isinstance(a, ast.keyword)]
    kw_args = [
        a for a in args if isinstance(a, ast.keyword) and a.arg not in (None, "variant")
    ]

    positional_specs = [s for s in declared_specs if s.get("passing") == "positional"]
    if len(pos_args) > len(positional_specs):
        raise DslError(
            f"{ns}.{function}(): too many positional arguments (expected at "
            f"most {len(positional_specs)})",
            line_number,
        )

    bound: dict[str, ast.AST] = {}
    positionally_bound: set[str] = set()
    for spec, value in zip(positional_specs, pos_args, strict=False):
        name = str(spec["name"])
        bound[name] = value
        positionally_bound.add(name)

    for kw in kw_args:
        keyword_name = kw.arg
        assert keyword_name is not None
        if keyword_name in bound:
            source = (
                "positionally"
                if keyword_name in positionally_bound
                else "by keyword"
            )
            raise DslError(
                f"{ns}.{function}(): {keyword_name}= supplied twice "
                f"(already given {source})",
                line_number,
            )
        bound[keyword_name] = kw.value

    for spec in declared_specs:
        name = str(spec["name"])
        if spec.get("required") and name not in bound:
            raise DslError(
                f"{ns}.{function}(): missing required argument {name!r}",
                line_number,
            )
    return bound


def _normalize_registry_args(
    ns: str,
    function: str,
    args: list[ast.AST | ast.keyword],
    declared_specs: list[dict[str, object]],
    line_number: int,
) -> list[ast.AST | ast.keyword]:
    """Normalize a registry-bound call for a notation-native builder.

    Native builders predate registry signatures and consume structural values
    positionally. Bind first, then emit those values in declared order; an
    omitted optional slot before a later supplied value becomes an empty-string
    placeholder. Keyword-only values and the special ``variant=`` control stay
    as keywords. This gives native and passthrough calls the same public calling
    convention without coupling notation-specific builders to registry data.
    """
    bound = _bind_registry_args(ns, function, args, declared_specs, line_number)
    positional_specs = [
        spec for spec in declared_specs if spec.get("passing") == "positional"
    ]
    last_bound = max(
        (
            index
            for index, spec in enumerate(positional_specs)
            if str(spec["name"]) in bound
        ),
        default=-1,
    )
    normalized: list[ast.AST | ast.keyword] = [
        bound.get(str(spec["name"]), ast.Constant(value=""))
        for spec in positional_specs[: last_bound + 1]
    ]
    normalized.extend(
        ast.keyword(
            arg=str(spec["name"]),
            value=cast(ast.expr, bound[str(spec["name"])]),
        )
        for spec in declared_specs
        if spec.get("passing") == "keyword_only"
        and str(spec["name"]) in bound
    )
    normalized.extend(
        kw
        for kw in args
        if isinstance(kw, ast.keyword) and kw.arg == "variant"
    )
    return normalized


def _registry_root(ns: str) -> str:
    """Return a notation's non-rendering document-root function, if known."""
    from .registry import load_registry

    try:
        registry = load_registry(ns)
    except (KeyError, FileNotFoundError):
        return ""
    grammar = registry.get("grammar", {})
    if not isinstance(grammar, dict):
        return ""
    root = grammar.get("root", "")
    return str(root) if root else ""


def _passthrough_variant(
    args: list[ast.AST | ast.keyword], line_number: int
) -> int:
    """Read and validate the common ``variant=N`` passthrough keyword."""
    values: dict[str, str | int | float] = {}
    seen = False
    for arg in args:
        if not isinstance(arg, ast.keyword) or arg.arg != "variant":
            continue
        if seen:
            raise DslError(
                "keyword argument supplied twice: variant", line_number
            )
        seen = True
        value = literal_value(arg.value, "variant")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise DslError(
                f"variant= must be an integer, got {value!r}", line_number
            )
        values["variant"] = value
    return parse_keyword_int(values, "variant", 1, line_number)


def _passthrough_keyword_extra(
    args: list[ast.AST | ast.keyword],
    blocks: dict[str, str] | None = None,
) -> dict[str, object]:
    """Preserve declared row/shape keywords (``key=``, ``dashed=``, ...) onto
    the node for later consumption (e.g. compound row rendering).

    Keyword names are already validated by ``_validate_keyword_args`` before
    a node is built, so every keyword seen here is known-legal; ``variant``
    is handled separately (``Node.variant``), not duplicated here.
    """
    extra: dict[str, object] = {}
    for arg in args:
        if not isinstance(arg, ast.keyword) or arg.arg is None:
            continue
        arg_name = arg.arg
        if arg_name == "variant":
            continue
        extra[arg_name] = literal_value(arg.value, arg_name, blocks)
    return extra


def _build_passthrough_node(
    ns: str,
    name: str,
    args: list[ast.AST | ast.keyword],
    variant: int,
    line_number: int,
    nodes: list[Node],
    container_stack: list[_ContainerFrame],
    declared_specs: list[dict[str, object]] | None,
    blocks: dict[str, str] | None = None,
) -> Node:
    """Build a passthrough Node.

    When *declared_specs* is available, arguments are bound against it (see
    ``_bind_registry_args``) using the registry entry as the call's
    signature: ``node_id`` and ``label``/``text`` (whichever the entry
    declares) are structural, every other declared value lands in
    ``Node.extra``. An unregistered function (``declared_specs`` is
    ``None``) keeps the lenient legacy behavior -- no arg-count or
    keyword-binding validation, just the first two positional arguments.

    *blocks* (declared ``block NAME = \"\"\"...\"\"\"`` variables, see
    ``extract_block_variables``) resolves a bare identifier used where a
    string value is expected -- ``label``/``text`` and every other
    non-id declared value -- to that block's text. ``node_id`` extraction
    (:func:`_extract_arg_id`) never sees *blocks*.
    """
    if declared_specs is not None:
        bound = _bind_registry_args(ns, name, args, declared_specs, line_number)
        node_id_value = bound.get("node_id")
        node_id = (
            _extract_arg_id(node_id_value) if node_id_value is not None else ""
        )
        if not node_id:
            raise DslError(
                f"{ns}.{name}(): first argument must be a node id (string or "
                f"identifier)",
                line_number,
            )
        label_value = bound.get("label", bound.get("text"))
        label = (
            _extract_arg_string(label_value, blocks)
            if label_value is not None
            else ""
        )
        extra = {
            str(spec["name"]): literal_value(
                bound[str(spec["name"])], str(spec["name"]), blocks
            )
            for spec in declared_specs
            if str(spec["name"]) not in ("node_id", "label", "text")
            and str(spec["name"]) in bound
        }
    else:
        pos_args = [a for a in args if not isinstance(a, ast.keyword)]
        if not pos_args:
            raise DslError(
                f"{ns}.{name}(): requires at least a node id argument",
                line_number,
            )
        node_id = _extract_arg_id(pos_args[0])
        if not node_id:
            raise DslError(
                f"{ns}.{name}(): first argument must be a node id (string or "
                f"identifier)",
                line_number,
            )
        label = (
            _extract_arg_string(pos_args[1], blocks) if len(pos_args) >= 2 else ""
        )
        extra = _passthrough_keyword_extra(args, blocks)

    node = Node(
        id=node_id,
        type=f"{ns}.{name}",
        label=label,
        variant=variant,
        element_name=name,
        extra=extra,
    )
    if container_stack:
        node.parent_id = container_stack[-1].node_id
    nodes.append(node)
    return node


def _build_passthrough_edge(
    ns: str,
    name: str,
    args: list[ast.AST | ast.keyword],
    variant: int,
    line_number: int,
    edges: list[Edge],
    declared_specs: list[dict[str, object]],
    blocks: dict[str, str] | None = None,
) -> None:
    """Build a passthrough Edge for a foreign-namespace edge call.

    Always registry-driven: this is only called once a matched registry
    entry classified the function as an edge (see ``_process_block_call``),
    so *declared_specs* is always a real signature -- unlike
    ``_build_passthrough_node``, there is no unregistered-function fallback
    here, since an unregistered function is never treated as an edge.

    *blocks* resolves a declared ``block`` variable used as ``label`` or any
    other non-id value arg -- see ``_build_passthrough_node``.
    """
    bound = _bind_registry_args(ns, name, args, declared_specs, line_number)
    source_value = bound.get("source")
    target_value = bound.get("target")
    label_value = bound.get("label")
    source_id = _extract_arg_id(source_value) if source_value is not None else ""
    target_id = _extract_arg_id(target_value) if target_value is not None else ""
    label = (
        _extract_arg_string(label_value, blocks) if label_value is not None else ""
    )
    source_is_none = source_value is not None and is_none_literal(source_value)
    target_is_none = target_value is not None and is_none_literal(target_value)
    unconnected = source_is_none and target_is_none
    if not unconnected and (not source_id or not target_id):
        raise DslError(
            f"{ns}.{name}(): edge requires source and target ids",
            line_number,
        )
    extra: dict[str, object] = {"variant": variant}
    for spec in declared_specs:
        spec_name = str(spec["name"])
        if spec_name in ("source", "target", "label") or spec_name not in bound:
            continue
        extra[spec_name] = literal_value(bound[spec_name], spec_name, blocks)
    edge = Edge(
        id=f"palette-edge-{len(edges) + 1}" if unconnected else "",
        type=f"{ns}.{name}",
        source_id=source_id,
        target_id=target_id,
        label=label,
        extra=extra,
    )
    edges.append(edge)


def _ensure_endpoint_free_edge_ids_are_unique(
    nodes: list[Node], edges: list[Edge]
) -> None:
    """Avoid collisions between generated palette-edge IDs and authored IDs."""
    endpoint_free = [
        edge for edge in edges if not edge.source_id and not edge.target_id
    ]
    used_ids = {node.id for node in nodes}
    used_ids.update(
        edge.id
        for edge in edges
        if (edge.source_id or edge.target_id) and edge.id
    )

    for edge in endpoint_free:
        base_id = edge.id or "palette-edge"
        candidate = base_id
        suffix = 2
        while candidate in used_ids:
            candidate = f"{base_id}-{suffix}"
            suffix += 1
        edge.id = candidate
        used_ids.add(candidate)


def _extract_arg_string(node: ast.AST, blocks: dict[str, str] | None = None) -> str:
    """Best-effort extraction of a string or identifier from an AST node.

    A bare identifier naming a declared ``block`` variable resolves to that
    block's text -- see ``literal_value`` for the same substitution rule.
    """
    if isinstance(node, ast.Name):
        if blocks and node.id in blocks:
            return blocks[node.id]
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _extract_arg_id(node: ast.AST) -> str:
    """Extract an *identity* argument (node_id, edge source/target).

    Wider than :func:`_extract_arg_string`: an unquoted hyphenated id like
    ``some-id-1`` parses as a chain of subtraction nodes, which
    :func:`_identifier_like_value` recovers. The C4 parser accepted those all
    along (via :func:`literal_or_name`), so without this the same document
    was legal in one notation and a confusing "first argument must be a node
    id" error in every other — and draw.io's own cell ids (what the reverse
    derivation seeds node ids from) routinely contain hyphens.
    """
    return _identifier_like_value(node) or ""


def is_none_literal(node: ast.AST) -> bool:
    """True for the literal ``None`` — the grammar's unconnected-endpoint form."""
    return isinstance(node, ast.Constant) and node.value is None

def build_pages_document(
    pages: list[tuple[str, str]],
    build_page: Callable[[str, str, int], Document],
) -> Document | MultiPageDocument:
    """Build a single-page or multi-page document from parsed pages.

    Each page is built independently via *build_page(source, name, index)*.
    """
    if len(pages) > 1:
        return MultiPageDocument(
            pages=[
                build_page(page_source, page_name, index)
                for index, (page_name, page_source) in enumerate(pages)
            ]
        )
    if pages:
        page_name, page_source = pages[0]
        return build_page(page_source, page_name, 0)
    return Document(diagram=Diagram(name=""))
