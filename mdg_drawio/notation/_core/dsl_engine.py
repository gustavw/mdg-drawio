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
from typing import Protocol

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

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

CALL_RE: re.Pattern[str] = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?P<call>[A-Za-z_][A-Za-z0-9_]*)\.)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)\s*(?P<colon>:?)\s*$"
)

# Lines the block parser skips silently.
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
    for line in body_lines:
        if line.strip() == "---":
            return True
    return False


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


def literal_value(node: ast.AST, field_name: str = "") -> object:
    """Extract a Python literal or identifier from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return node.value
    raise ValueError(f"{field_name} must be a literal or identifier")


def literal_string(node: ast.AST, field_name: str = "") -> str:
    """Extract a string literal from an AST node."""
    value = literal_value(node, field_name)
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
) -> tuple[list[Node], list[Edge], str]:
    """Parse a block-syntax DSL source into nodes, edges, and a diagram name.

    Handles the container stack (indent-based ``parent_id`` assignment) and
    ``opens_block`` detection (trailing colon). Notation-specific logic lives
    in the callbacks.

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
) -> None:
    """Process one parsed block call, mutating nodes/edges/container_stack.

    ``state["diagram_name"]`` is updated in place when a diagram-title call is
    seen. Kept separate so ``parse_block_source`` can wrap each call uniformly.
    """
    if _process_registry_root(call, line_number, container_stack, state):
        return

    if call.namespace != namespace:
        # Foreign namespace — look up the registry to determine if this
        # is an edge or a node, then build appropriately.
        foreign_args = parse_call_arguments(call.args_source, line_number)
        _pop_container_stack(container_stack, call.indent)
        foreign_variant = _passthrough_variant(foreign_args, line_number)
        parent_frame = container_stack[-1] if container_stack else None

        registry_entry = _registry_entry(
            call.namespace, call.name, foreign_variant, line_number
        )
        if registry_entry and registry_entry.get("kind") == "edge":
            _build_passthrough_edge(
                call.namespace,
                call.name,
                foreign_args,
                foreign_variant,
                line_number,
                edges,
            )
        else:
            if parent_frame is not None:
                _validate_child_allowed(
                    parent_frame, call.namespace, call.name, line_number
                )
            node = _build_passthrough_node(
                call.namespace,
                call.name,
                foreign_args,
                foreign_variant,
                line_number,
                nodes,
                container_stack,
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
            _ContainerFrame(indent=call.indent, node_id=node.id, kind_label="child")
        )

def _registry_entry(
    ns: str, function: str, variant: int, line_number: int
) -> dict[str, object] | None:
    """Resolve exactly ``(ns, function, variant)`` for passthrough dispatch.

    Unknown functions retain the parser's existing generic-node behavior, as
    do undeclared variants in a single-kind family (full variant validation is
    Phase 3). In a mixed-kind family, however, falling back can change a vertex
    into an edge or vice versa, so an unsupported variant is an actionable,
    line-numbered authoring error.
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
    if len({entry.get("kind") for entry in entries}) == 1:
        return entries[0]
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
        indent=indent, node_id=node.id, kind_label=kind_label, allowed=allowed
    )


def _validate_child_allowed(
    parent_frame: _ContainerFrame, ns: str, name: str, line_number: int
) -> None:
    """Reject a child function not permitted by the parent's rows/contains.allowed."""
    if parent_frame.allowed is None or name in parent_frame.allowed:
        return
    allowed = ", ".join(sorted(parent_frame.allowed)) or "(none)"
    raise DslError(
        f"{ns}.{name}(): not a valid {parent_frame.kind_label} of "
        f"{parent_frame.node_id!r}; expected one of {allowed}",
        line_number,
    )


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
    for arg in args:
        if not isinstance(arg, ast.keyword) or arg.arg != "variant":
            continue
        value = literal_value(arg.value, "variant")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise DslError(
                f"variant= must be an integer, got {value!r}", line_number
            )
        values["variant"] = value
    return parse_keyword_int(values, "variant", 1, line_number)


def _build_passthrough_node(
    ns: str,
    name: str,
    args: list[ast.AST | ast.keyword],
    variant: int,
    line_number: int,
    nodes: list[Node],
    container_stack: list[_ContainerFrame],
) -> Node:
    """Build a passthrough Node."""
    pos_args = [a for a in args if not isinstance(a, ast.keyword)]
    if not pos_args:
        raise DslError(
            f"{ns}.{name}(): requires at least a node id argument",
            line_number,
        )
    node_id = _extract_arg_string(pos_args[0])
    if not node_id:
        raise DslError(
            f"{ns}.{name}(): first argument must be a node id (string or "
            f"identifier)",
            line_number,
        )
    label = ""
    if len(pos_args) >= 2:
        label = _extract_arg_string(pos_args[1])
    node = Node(
        id=node_id,
        type=f"{ns}.{name}",
        label=label,
        variant=variant,
        element_name=name,
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
) -> None:
    """Build a passthrough Edge for a foreign-namespace edge call."""
    pos_args = [a for a in args if not isinstance(a, ast.keyword)]
    source_id = ""
    target_id = ""
    label = ""
    if len(pos_args) >= 1:
        source_id = _extract_arg_string(pos_args[0])
    if len(pos_args) >= 2:
        target_id = _extract_arg_string(pos_args[1])
    if len(pos_args) >= 3:
        label = _extract_arg_string(pos_args[2])
    source_is_none = len(pos_args) >= 1 and _is_none_literal(pos_args[0])
    target_is_none = len(pos_args) >= 2 and _is_none_literal(pos_args[1])
    unconnected = source_is_none and target_is_none
    if not unconnected and (not source_id or not target_id):
        raise DslError(
            f"{ns}.{name}(): edge requires source and target ids",
            line_number,
        )
    edge = Edge(
        id=f"palette-edge-{len(edges) + 1}" if unconnected else "",
        type=f"{ns}.{name}",
        source_id=source_id,
        target_id=target_id,
        label=label,
        extra={"variant": variant},
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


def _extract_arg_string(node: ast.AST) -> str:
    """Best-effort extraction of a string or identifier from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _is_none_literal(node: ast.AST) -> bool:
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
