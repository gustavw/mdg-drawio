"""Merge newly-derived cells into an existing ``.mdg`` file, correctly nested
and indented, without disturbing anything already there.

This is a TEXT-level merge, not a model-level one: an existing ``.mdg`` file
is hand-authored (comments, exact formatting, manual arrangement), so we
insert new lines at the right place rather than re-serializing the whole
document from a freshly-built model (which would risk reformatting or
dropping content the forward generator doesn't round-trip).

The identity a "new" cell is judged against is the SAME one the forward
generator and its geometry overlay already rely on
(:mod:`mdg_drawio.generator.overlay`): a previously-generated node's draw.io
cell id equals its ``.mdg`` node_id. So a resolved cell is "already
represented" iff its raw draw.io ``cell_id`` matches a node_id already
declared in the existing file; anything else is genuinely new.

Safety: this module only ever computes a new text (:func:`render_merge`) or
validates one (:func:`validate`, which re-parses the result through the same
parser the real pipeline uses) -- it never writes a file itself. The CLI
(``mdg_drawio/reverse/merge_cli.py``) is responsible for the dry-run-by-default,
validate-before-write contract.

Edges are emitted too, as flat top-level statements (``lib.Function(source,
target[, label])``, per GRAMMAR.md -- edges take no block and aren't nested),
appended after any new top-level vertex subtree. An edge's endpoints must
themselves resolve to a known node -- either newly assigned in this run, or
already declared in the existing file -- otherwise it is skipped and reported,
same as an unresolved vertex.

Vertices and edges both use their draw.io cell id as persistent identity.
Edge declarations store it in the common ``edge_id=`` keyword. Legacy edge
declarations without that keyword are matched semantically during their first
sync and migrated in place, preserving compatible formatting and extra kwargs.

:func:`plan_sync`/:func:`render_sync` (the `mdg sync` verb) extend this with
removal: draw.io as the sole source of truth, so a vertex or edge no longer
present there is deleted from the ``.mdg`` too, not just left stale. Uses the
SAME identity convention and the SAME registry-``kind`` classification
:func:`_is_edge_shape` already relies on -- just applied to the *existing*
text instead of a freshly-derived cell.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from mdg_drawio.contracts import Document
from mdg_drawio.markup import html_to_markdown
from mdg_drawio.notation import parse as parse_mdg
from mdg_drawio.notation import shapes_by_function

from .containment import Containment
from .derive import Cell, CellResult, DocumentResult, RawCell
from .style_index import registry_entry

INDENT_STEP = "    "

# Mirrors CALL_RE's shape (dsl_engine.py) independently -- mdg_drawio/reverse may
# not import a leading-underscore module member across the package boundary
# (enforced by tests/test_architecture.py), so this is a small, purpose-built
# re-implementation of just the structural facts a merge needs: is this line a
# call, what's its indent, and does it open a block. It does not need to
# understand args, kwargs, or foreign-namespace passthrough the way the real
# engine does.
_CALL_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?P<ns>[A-Za-z_]\w*)\.)?(?P<func>[A-Za-z_]\w*)"
    r"\((?P<args>.*)\)\s*(?P<colon>:?)\s*$"
)


def _is_edge_call(ns: str | None, func: str) -> bool:
    """Whether ``ns.func`` names a registered EDGE-kind shape.

    ``False`` (lenient) if *ns* is absent or unresolvable -- matches this
    codebase's existing precedent that an unregistered/ambiguous function
    keeps generic (vertex-like) behaviour rather than erroring. Used by
    :func:`index_existing` so an edge's own line is never mistaken for a
    vertex declaration naming its first argument: an edge's args are
    references to OTHER nodes' ids, not the edge's persistent ``edge_id``.
    Without this, a vertex whose real declaration line is lost
    (e.g. to an earlier bug) but whose edges survive looks "already
    represented" forever, by the edge line's own source token -- sync can
    then never re-derive and restore the missing vertex.
    """
    if not ns:
        return False
    try:
        entries = shapes_by_function(ns).get(func)
    except (KeyError, FileNotFoundError):
        return False
    if not entries:
        return False
    return entries[0].get("kind") == "edge"


def _comment_start(line: str) -> int | None:
    """Index of an unquoted ``#`` comment marker, or ``None`` if there isn't
    one. An independent re-implementation of dsl_engine.strip_inline_comment's
    exact algorithm (not an import -- scripts/ only uses mdg_drawio.notation's
    public surface, never ``_core`` submodules)."""
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
                return i
        i += 1
    return None


def _strip_inline_comment(line: str) -> str:
    """Strip a trailing ``#`` comment, respecting quoted strings.

    Without this, a declaration with a trailing comment (used throughout this
    project's own ``.mdg`` fixtures, e.g. ``c4.Container(c1, "...")  # note``)
    is invisible to :func:`index_existing` -- the merge tool would believe an
    already-drawn shape is new and duplicate it under a fresh id.
    """
    idx = _comment_start(line)
    return line[:idx].rstrip() if idx is not None else line


def _add_colon(line: str) -> str:
    """Insert a ``:`` right after the real content, before any trailing ``#``
    comment -- appending it after the comment would put it somewhere the real
    parser's own comment-stripping throws away, so the colon would never take
    effect."""
    idx = _comment_start(line)
    if idx is None:
        return line.rstrip() + ":"
    code, comment = line[:idx].rstrip(), line[idx:]
    return f"{code}: {comment}"


def _first_arg_token(args: str) -> str | None:
    """The first top-level comma-separated argument, quotes stripped.

    Respects quoting so a comma inside a quoted label doesn't split early.
    """
    depth = 0
    quote: str | None = None
    for i, ch in enumerate(args):
        if quote:
            if ch == "\\":
                continue
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return args[:i].strip().strip("\"'").strip() or None
    token = args.strip()
    return token.strip("\"'").strip() or None


@dataclass(frozen=True)
class ExistingIndex:
    """The existing ``.mdg`` text, indexed by declared node_id.

    ``clean_lines`` mirrors ``lines`` with any trailing ``#`` comment
    stripped from each -- used wherever a line's real (non-comment) content
    matters, e.g. checking for a trailing ``:``.
    """

    lines: list[str]
    clean_lines: list[str]
    node_line: dict[str, int]
    node_indent: dict[str, str]

    def node_ids(self) -> set[str]:
        return set(self.node_line)


def index_existing(text: str) -> ExistingIndex:
    """Scan an existing ``.mdg`` for every declared node_id, keeping the
    FIRST declaration if an id oddly repeats (defensive, mirrors load_cells'
    keep-first precedent for malformed input). An edge call's own line is
    skipped entirely (see :func:`_is_edge_call`) -- its first argument is a
    reference to another node, not a declaration of its own."""
    lines = text.splitlines()
    clean_lines = [_strip_inline_comment(line) for line in lines]
    node_line: dict[str, int] = {}
    node_indent: dict[str, str] = {}
    for i, line in enumerate(clean_lines):
        match = _CALL_LINE_RE.match(line)
        if not match:
            continue
        if _is_edge_call(match.group("ns"), match.group("func")):
            continue
        node_id = _first_arg_token(match.group("args"))
        if node_id and node_id not in node_line:
            node_line[node_id] = i
            node_indent[node_id] = match.group("indent")
    return ExistingIndex(lines, clean_lines, node_line, node_indent)


def _opens_block(existing: ExistingIndex, container_node_id: str) -> bool:
    """Whether the container's own line ends in ``:`` (comments aside) --
    the ONLY thing that makes anything below it a real child in the actual
    grammar. Indentation alone means nothing without it: a typo that dropped
    the colon, or a stray indented comment, must not be mistaken for real
    children (mirrors dsl_engine.py's ``opens_block``/container-stack logic).
    """
    line = existing.clean_lines[existing.node_line[container_node_id]]
    return line.rstrip().endswith(":")


def _child_insertion(
    existing: ExistingIndex, container_node_id: str
) -> tuple[int, str]:
    """(line index to insert before, indent string for the new child)."""
    start = existing.node_line[container_node_id]
    container_indent = len(existing.node_indent[container_node_id])
    if not _opens_block(existing, container_node_id):
        # No real block open yet -- the new child goes immediately after the
        # declaration, one step deeper, regardless of any misleadingly
        # indented content below it (which isn't really nested under it).
        return start + 1, existing.node_indent[container_node_id] + INDENT_STEP
    first_child_indent: str | None = None
    end = start + 1
    for i in range(start + 1, len(existing.lines)):
        line = existing.lines[i]
        if not line.strip():
            end = i + 1
            continue
        indent_width = len(line) - len(line.lstrip(" \t"))
        if indent_width <= container_indent:
            break
        if first_child_indent is None:
            first_child_indent = line[:indent_width]
        end = i + 1
    child_indent = first_child_indent or (
        existing.node_indent[container_node_id] + INDENT_STEP
    )
    return end, child_indent


def _needs_colon(existing: ExistingIndex, container_node_id: str) -> bool:
    """Whether the container's own line needs a ``:`` appended before a new
    child can be spliced under it -- purely a property of that one line; see
    :func:`_opens_block`."""
    return not _opens_block(existing, container_node_id)


def _label_for(cell: Cell) -> str | None:
    """The best label a hand-drawn cell offers, or ``None`` if there isn't a
    trustworthy one -- callers fall back to the ``label or node_id``
    convention the forward engine already applies (GRAMMAR.md/dsl_engine.py).

    Three sources, in priority order:

    1. A C4 object cell's own ``c4Name`` attribute -- takes priority over its
       plain ``value``, which for an untouched palette cell is only an
       unsubstituted ``%c4Name%`` template (see :class:`Cell`).
    2. An object-wrapped cell's generic draw.io ``label`` attribute -- how
       every OTHER notation's object cells carry their user-typed text (a
       bpmn2 Pool/Lane/task is ``<object label="...">``, no C4-style
       template substitution involved).
    3. The cell's own ``value`` -- a bare (non-object-wrapped) vertex or edge.

    Anything that still looks like a template placeholder is skipped rather
    than guessed at. Raw HTML (draw.io renders a label as HTML whenever its
    style sets ``html=1`` -- the common case, including a plain multi-line
    label typed in the UI, each line its own ``<div>``) is converted to
    markdown via :func:`~mdg_drawio.markup.html_to_markdown` rather than
    discarded -- losing an entire label just because it has formatting (or
    simply spans more than one line) would otherwise silently drop real
    content the user typed.
    """
    name = cell.object_attrs.get("c4Name", "").strip()
    if name and "%" not in name:
        return name
    label = cell.object_attrs.get("label", "").strip()
    if label and "%" not in label:
        return html_to_markdown(label) if "<" in label else label
    value = cell.value.strip()
    if value and "%" not in value:
        return html_to_markdown(value) if "<" in value else value
    return None


def _edge_label_for(cell: Cell) -> str | None:
    """Extract connector text, including C4's edge-specific object field."""
    description = cell.object_attrs.get("c4Description", "").strip()
    if description and "%" not in description:
        return html_to_markdown(description) if "<" in description else description
    return _label_for(cell)


def _edge_visibility_for(cell: Cell) -> bool | None:
    """Recover authored visibility, separating it from generated hiding."""
    marker = cell.mdg_visibility
    expected = cell.mdg_expected_visible
    if marker in ("auto", "visible", "hidden") and expected is not None:
        if cell.actual_visible != expected:
            return cell.actual_visible
        if marker == "visible":
            return True
        if marker == "hidden":
            return False
        return None
    # Markerless legacy XML has no provenance. A visible edge used the default;
    # a hidden edge is conservatively treated as an authored choice.
    return None if cell.actual_visible else False


def _edge_shape_from_provenance(
    cell: Cell, resolved: CellResult | None
) -> str | None:
    """Use generator provenance only to disambiguate visually equal shapes."""
    if not cell.mdg_type or cell.mdg_variant is None:
        return None
    try:
        library, function = cell.mdg_type.split(".", 1)
        entries = shapes_by_function(library).get(function, [])
    except (KeyError, FileNotFoundError, ValueError):
        return None
    shape_id = next(
        (
            str(entry["id"])
            for entry in entries
            if int(entry.get("variant", 1)) == cell.mdg_variant
            and entry.get("kind") == "edge"
        ),
        None,
    )
    if shape_id is None:
        return None
    # If the visible style uniquely identifies another shape, it was edited in
    # draw.io and must win. Provenance only resolves fingerprint collisions.
    if resolved is not None and resolved.resolved_by == "unique":
        return None
    candidate_ids = (
        {candidate.shape_id for candidate in resolved.candidates} if resolved else set()
    )
    return shape_id if not candidate_ids or shape_id in candidate_ids else None


def _escape_dsl_string(text: str) -> str:
    """Escape ``text`` for embedding in a ``.mdg`` double-quoted string
    literal (parsed via Python's ``ast``, per dsl_engine.py) -- NOT XML
    escaping. XML-escaping only handles ``&``/``<``/``>`` and leaves a
    literal ``"`` or ``\\`` untouched, which silently changes the DSL
    argument list's meaning instead of raising: a label like ``Bad", "Extra``
    would render as ``"Bad", "Extra"`` -- syntactically valid, but silently
    truncating the label and fabricating an extra positional argument.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _shape_meta(shape_id: str) -> tuple[str, str, int] | None:
    """(library, function name, variant) from the registry, or ``None`` if
    the shape id doesn't resolve (defensive; should not happen for a shape
    that came from the loaded StyleIndex)."""
    entry = registry_entry(shape_id)
    if entry is None:
        return None
    library = shape_id.split(".")[0]
    return library, str(entry["function"]), int(entry.get("variant", 1))


def _render_declaration(
    cell: Cell, shape_id: str, node_id: str, indent: str, opens_block: bool
) -> str | None:
    meta = _shape_meta(shape_id)
    if meta is None:
        return None
    library, function, variant = meta
    args = [node_id]
    label = _label_for(cell)
    if label is not None:
        args.append(f'"{_escape_dsl_string(label)}"')
    variant_suffix = f", variant={variant}" if variant != 1 else ""
    colon = ":" if opens_block else ""
    return f"{indent}{library}.{function}({', '.join(args)}{variant_suffix}){colon}"


def _render_edge_declaration(
    cell: Cell,
    shape_id: str,
    source_node_id: str,
    target_node_id: str,
    edge_id: str | None = None,
) -> str | None:
    """An edge declaration, flat and top-level (edges take no block, no
    indent, per GRAMMAR.md). Only ``source``, ``target``, and ``label`` are
    ever emitted -- an edge shape declaring further optional args (e.g.
    C4 Rel's ``technology``) is valid DSL without them, same as
    :func:`_render_declaration` never emits every possible vertex kwarg."""
    meta = _shape_meta(shape_id)
    if meta is None:
        return None
    library, function, variant = meta
    args = [source_node_id, target_node_id]
    label = _edge_label_for(cell)
    if label is not None:
        args.append(f'"{_escape_dsl_string(label)}"')
    variant_suffix = f", variant={variant}" if variant != 1 else ""
    visible = _edge_visibility_for(cell)
    visible_suffix = f", visible={visible}" if visible is not None else ""
    edge_id_suffix = (
        f', edge_id="{_escape_dsl_string(edge_id)}"' if edge_id else ""
    )
    return (
        f"{library}.{function}({', '.join(args)}"
        f"{variant_suffix}{visible_suffix}{edge_id_suffix})"
    )


@dataclass
class NewNode:
    """One new cell, plus its own new (not-yet-existing) children, recursively.

    Not frozen: this is a builder, assembled incrementally by
    :func:`_build_forest` (which appends each node's children as it discovers
    them), the same way :class:`~mdg_drawio.reverse.derive.CellResult` or
    ``mdg_drawio``'s own ``Node`` are mutated post-construction rather than
    replaced wholesale.
    """

    cell_id: str
    node_id: str
    shape_id: str
    children: list[NewNode] = field(default_factory=list)


@dataclass(frozen=True)
class Insertion:
    """``top_level`` is explicit rather than inferred from
    ``anchor_line == len(existing.lines)``: an existing container's last
    child can legitimately land there too when it happens to be the file's
    final line, which is a splice (no blank-line separator), not an append.

    ``anchor_depth`` is the anchor container's own nesting depth (``-1`` for
    a top-level insertion) -- see :func:`render_merge` for why it matters
    when two anchors tie on ``anchor_line``.
    """

    anchor_line: int  # insert before this existing line index
    text: str  # the fully rendered, indented block (no trailing newline)
    colon_fix_line: int | None = None  # existing line needing ':' appended
    top_level: bool = False  # append at EOF with a blank-line separator
    anchor_depth: int = 0


@dataclass(frozen=True)
class MergePlan:
    """``new_node_count`` is every new element actually added, not the number
    of insertion operations: a brand-new container with new children inside
    it is ONE insertion (its whole subtree renders as one text block) but
    several new nodes -- reporting insertion count would understate what
    changed.

    ``renamed_ids`` (draw.io cell_id -> the fresh semantic node_id just
    minted for it) covers every newly-added VERTEX. Edge cell ids are instead
    persisted verbatim as ``edge_id=`` and are never rename targets.
    ``mdg sync --write`` applies
    this to the ``.drawio`` file itself (:func:`~mdg_drawio.reverse.derive.
    rewrite_cell_ids`) so a later plain regenerate's geometry overlay can
    still find the cell by id."""

    insertions: list[Insertion]
    skipped: list[str]
    new_edge_count: int
    new_node_count: int
    renamed_ids: dict[str, str]


def _build_forest(
    new_cells: list[CellResult],
    containments: dict[str, Containment],
    existing_ids: set[str],
    id_of_node_id: dict[str, str],
) -> dict[str | None, list[NewNode]]:
    """Group new cells by their nearest EXISTING ancestor (or ``None`` for
    top-level), nesting any new cells whose container is ALSO new underneath
    it. Returns ``{anchor: [roots...]}``. Precondition: every cell in
    ``new_cells`` is resolved (its ``chosen`` is not ``None``) -- callers
    (:func:`_build_insertions`) already filter unresolved cells out before
    reaching here.
    """
    new_by_cell_id = {c.cell_id: c for c in new_cells}
    node_of: dict[str, NewNode] = {}
    for c in new_cells:
        assert c.chosen is not None  # precondition, see docstring
        node_of[c.cell_id] = NewNode(c.cell_id, c.cell_id, c.chosen.shape_id)
    roots_by_anchor: dict[str | None, list[NewNode]] = {}

    for cell in new_cells:
        containment = containments.get(cell.cell_id)
        container_node_id = containment.container_node_id if containment else None
        container_cell_id = (
            id_of_node_id.get(container_node_id, container_node_id)
            if container_node_id is not None
            else None
        )
        if container_cell_id is not None and container_cell_id in new_by_cell_id:
            node_of[container_cell_id].children.append(node_of[cell.cell_id])
        else:
            anchor = container_cell_id if container_cell_id in existing_ids else None
            roots_by_anchor.setdefault(anchor, []).append(node_of[cell.cell_id])

    return roots_by_anchor


def _render_subtree(
    node: NewNode, cells_by_id: dict[str, Cell], node_ids: dict[str, str], indent: str
) -> list[str]:
    line = _render_declaration(
        cells_by_id[node.cell_id],
        node.shape_id,
        node_ids[node.cell_id],
        indent,
        opens_block=bool(node.children),
    )
    if line is None:
        return []
    out = [line]
    for child in node.children:
        out.extend(_render_subtree(child, cells_by_id, node_ids, indent + INDENT_STEP))
    return out


@dataclass(frozen=True)
class NewEdge:
    """One new edge cell, resolved to a shape and both endpoints' node ids."""

    cell_id: str
    shape_id: str
    source_node_id: str
    target_node_id: str


def _is_edge_shape(shape_id: str) -> bool:
    entry = registry_entry(shape_id)
    return entry is not None and entry.get("kind") == "edge"


def _resolve_endpoint(
    raw_id: str | None, node_ids: dict[str, str], existing_ids: set[str]
) -> str | None:
    """The semantic node_id for an edge endpoint's raw draw.io cell id, or
    ``None`` if it names a cell this run neither resolved nor found already
    declared -- a dangling/unresolvable endpoint, so the edge can't be
    emitted (its declaration would reference an id that doesn't exist).

    ``existing_ids`` is checked FIRST: :func:`~mdg_drawio.reverse.naming.
    assign_semantic_ids` mints a semantic id for every resolved cell
    regardless of whether it's already represented (see its own docstring),
    so an already-represented endpoint would otherwise resolve to a
    newly-minted id instead of the established one its cell_id already is
    (the identity convention this whole module relies on, see the module
    docstring).
    """
    if raw_id is None:
        return None
    if raw_id in existing_ids:
        return raw_id
    if raw_id in node_ids:
        return node_ids[raw_id]
    return None


def _classify_new_cells(
    result: DocumentResult,
    existing_ids: set[str],
    raw_cells: dict[str, RawCell],
    node_ids: dict[str, str],
) -> tuple[list[CellResult], list[NewEdge], list[str]]:
    """Split ``result.cells`` into (genuinely new vertex cells, genuinely new
    resolved edges, human-readable skip reasons for anything unresolved)."""
    new_cells: list[CellResult] = []
    new_edges: list[NewEdge] = []
    skipped: list[str] = []
    for cell in result.cells:
        if cell.cell_id in existing_ids:
            continue
        raw = raw_cells.get(cell.cell_id)
        if raw is not None and raw.is_edge:
            if cell.chosen is None:
                skipped.append(
                    f"{cell.cell_id}: could not derive an edge shape "
                    f"({cell.resolved_by})"
                )
                continue
            if not _is_edge_shape(cell.chosen.shape_id):
                skipped.append(
                    f"{cell.cell_id}: resolved edge to non-edge shape "
                    f"{cell.chosen.shape_id!r}"
                )
                continue
            source_node_id = _resolve_endpoint(raw.source_id, node_ids, existing_ids)
            target_node_id = _resolve_endpoint(raw.target_id, node_ids, existing_ids)
            if source_node_id is None or target_node_id is None:
                skipped.append(
                    f"{cell.cell_id}: endpoint not resolved to a known node "
                    f"(source={raw.source_id!r}, target={raw.target_id!r})"
                )
                continue
            new_edges.append(
                NewEdge(
                    cell.cell_id, cell.chosen.shape_id, source_node_id, target_node_id
                )
            )
            continue
        if cell.chosen is None:
            skipped.append(
                f"{cell.cell_id}: could not derive a shape ({cell.resolved_by})"
            )
            continue
        new_cells.append(cell)
    return new_cells, new_edges, skipped


def _build_insertions(
    existing: ExistingIndex,
    new_cells: list[CellResult],
    cells_by_id: dict[str, Cell],
    node_ids: dict[str, str],
    containments: dict[str, Containment],
    existing_ids: set[str],
    id_of_node_id: dict[str, str],
) -> list[Insertion]:
    """One :class:`Insertion` per anchor (existing container or top-level)
    that actually has new content to add."""
    roots_by_anchor = _build_forest(
        new_cells, containments, existing_ids, id_of_node_id
    )

    insertions: list[Insertion] = []
    for anchor, roots in roots_by_anchor.items():
        if anchor is None:
            text = "\n".join(
                line
                for root in roots
                for line in _render_subtree(root, cells_by_id, node_ids, "")
            )
            if text:
                insertions.append(
                    Insertion(
                        len(existing.lines), text, top_level=True, anchor_depth=-1
                    )
                )
            continue
        line_index, indent = _child_insertion(existing, anchor)
        text = "\n".join(
            line
            for root in roots
            for line in _render_subtree(root, cells_by_id, node_ids, indent)
        )
        if not text:
            continue
        colon_fix = (
            existing.node_line[anchor] if _needs_colon(existing, anchor) else None
        )
        anchor_containment = containments.get(anchor)
        anchor_depth = anchor_containment.depth if anchor_containment else 0
        insertions.append(
            Insertion(line_index, text, colon_fix, anchor_depth=anchor_depth)
        )
    return insertions


def _build_edge_insertion(
    existing: ExistingIndex, new_edges: list[NewEdge], cells_by_id: dict[str, Cell]
) -> tuple[Insertion | None, int]:
    """One top-level :class:`Insertion` rendering every new edge (``None`` if
    there's nothing to add), plus how many were actually emitted.

    An edge whose exact rendered line already appears verbatim in the
    existing file is skipped -- see the module docstring for what this dedup
    does and does not catch.
    """
    lines: list[str] = []
    for edge in new_edges:
        legacy_line = _render_edge_declaration(
            cells_by_id[edge.cell_id],
            edge.shape_id,
            edge.source_node_id,
            edge.target_node_id,
        )
        line = _render_edge_declaration(
            cells_by_id[edge.cell_id],
            edge.shape_id,
            edge.source_node_id,
            edge.target_node_id,
            edge.cell_id,
        )
        if (
            line is None
            or line in existing.clean_lines
            or legacy_line in existing.clean_lines
        ):
            continue
        lines.append(line)
    if not lines:
        return None, 0
    insertion = Insertion(
        len(existing.lines), "\n".join(lines), top_level=True, anchor_depth=-1
    )
    return insertion, len(lines)


def plan_merge(
    existing: ExistingIndex,
    cells: list[Cell],
    result: DocumentResult,
    node_ids: dict[str, str],
    containments: dict[str, Containment],
    raw_cells: dict[str, RawCell],
) -> MergePlan:
    """Compute what to insert, and where, to add every new cell to
    ``existing`` -- does not touch the text itself (see :func:`render_merge`).
    """
    existing_ids = existing.node_ids()
    cells_by_id = {c.cell_id: c for c in cells}
    id_of_node_id = {v: k for k, v in node_ids.items()}

    new_cells, new_edges, skipped = _classify_new_cells(
        result, existing_ids, raw_cells, node_ids
    )
    insertions = _build_insertions(
        existing,
        new_cells,
        cells_by_id,
        node_ids,
        containments,
        existing_ids,
        id_of_node_id,
    )
    edge_insertion, new_edge_count = _build_edge_insertion(
        existing, new_edges, cells_by_id
    )
    if edge_insertion is not None:
        insertions.append(edge_insertion)
    renamed_ids = {c.cell_id: node_ids[c.cell_id] for c in new_cells}
    return MergePlan(
        insertions, skipped, new_edge_count, len(new_cells), renamed_ids
    )


def render_merge(existing: ExistingIndex, plan: MergePlan) -> str:
    """The full merged text -- applies every insertion, highest line index
    first, so earlier (lower-index) insertion points stay valid.

    Two DIFFERENT anchors can legitimately compute the same insertion line
    (e.g. an existing container's last child happens to be another existing,
    currently-childless container -- both "append at the end" to the same
    spot). Splicing ties in an arbitrary order can scramble nesting: whichever
    is spliced LAST at a given index ends up appearing FIRST in the output
    (each `lines[i:i] = [...]` pushes the previous insertion at that index
    down), so among same-line ties, the SHALLOWER anchor must be spliced
    first -- letting the deeper one's block land immediately after its own
    declaration, before the shallower anchor's own appended content.
    """
    lines = list(existing.lines)
    for insertion in plan.insertions:
        if insertion.colon_fix_line is not None:
            lines[insertion.colon_fix_line] = _add_colon(
                lines[insertion.colon_fix_line]
            )
    ordered = sorted(
        plan.insertions, key=lambda i: (-i.anchor_line, i.anchor_depth)
    )
    for insertion in ordered:
        if insertion.top_level:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(insertion.text)
        else:
            lines[insertion.anchor_line : insertion.anchor_line] = [insertion.text]
    return "\n".join(lines) + "\n"


_NS_FUNCTION_RE = re.compile(
    r"^[ \t]*(?P<ns>[A-Za-z_]\w*)\.(?P<function>[A-Za-z_]\w*)"
    r"\((?P<args>.*)\)\s*:?\s*$"
)
_VARIANT_KWARG_RE = re.compile(r"^variant\s*=\s*(\d+)$")
_LABEL_KWARG_RE = re.compile(r"^label\s*=")
_KWARG_RE = re.compile(r"^[A-Za-z_]\w*\s*=")


def _split_top_level_args(args: str) -> list[str]:
    """Every top-level comma-separated argument in a call's raw argument
    string, respecting quoting/nesting (same scan as :func:`_first_arg_token`,
    generalized to return every segment instead of just the first)."""
    if not args.strip():
        return []
    segments: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, ch in enumerate(args):
        if quote:
            if ch == "\\":
                continue
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            segments.append(args[start:i].strip())
            start = i + 1
    segments.append(args[start:].strip())
    return segments


@dataclass(frozen=True)
class ExistingEdge:
    """One existing top-level edge declaration, as parsed from the ``.mdg``
    text (not from the ``.drawio`` -- these are the file's OWN edge lines,
    whatever wrote them)."""

    line_index: int
    shape_id: str
    source_token: str
    target_token: str
    label: str
    variant: int
    visible: bool | None
    edge_id: str | None


def _argument_token(args: list[str], name: str, position: int) -> str | None:
    for argument in args:
        if not _KWARG_RE.match(argument):
            continue
        key, value = argument.split("=", 1)
        if key.strip() == name:
            return value.strip()
    positional = [argument for argument in args if not _KWARG_RE.match(argument)]
    return positional[position] if position < len(positional) else None


def _string_token(token: str | None) -> str:
    if token is None:
        return ""
    try:
        value = ast.literal_eval(token)
    except (SyntaxError, ValueError):
        return token.strip().strip("\"'")
    return value if isinstance(value, str) else str(value)


def _existing_edges(existing: ExistingIndex) -> list[ExistingEdge]:
    """Every existing DSL line that resolves, via the registry, to an edge
    shape -- with its raw source/target argument tokens.

    ``index_existing``'s ``node_line`` cannot answer this: it records a call
    line under its FIRST argument token regardless of kind, so an edge line
    is silently invisible there once its source's own vertex declaration has
    already claimed that key (see that function's docstring). Classifying by
    the registry's ``kind`` (mirrors :func:`_is_edge_shape`) is the only
    reliable signal available from text alone.
    """
    edges: list[ExistingEdge] = []
    for i, line in enumerate(existing.clean_lines):
        match = _NS_FUNCTION_RE.match(line)
        if not match:
            continue
        args = _split_top_level_args(match.group("args"))
        variant = 1
        for extra in args[2:]:
            kwarg = _VARIANT_KWARG_RE.match(extra)
            if kwarg:
                variant = int(kwarg.group(1))
                break
        ns, function = match.group("ns"), match.group("function")
        entry = registry_entry(f"{ns}.{function.lower()}.v{variant}")
        if entry is None or entry.get("kind") != "edge":
            continue
        source = _argument_token(args, "source", 0)
        target = _argument_token(args, "target", 1)
        if source is None or target is None:
            continue
        label = _string_token(_argument_token(args, "label", 2))
        visible_token = _argument_token(args, "visible", len(args) + 1)
        visible = (
            visible_token.strip() == "True"
            if visible_token is not None
            and visible_token.strip() in ("True", "False")
            else None
        )
        edge_id_token = _argument_token(args, "edge_id", len(args) + 1)
        edges.append(
            ExistingEdge(
                i,
                str(entry["id"]),
                source,
                target,
                label,
                variant,
                visible,
                _string_token(edge_id_token) or None,
            )
        )
    return edges


def _rewrite_edge_line(
    raw_line: str,
    clean_line: str,
    old_source: str,
    new_source: str,
    old_target: str,
    new_target: str,
) -> str | None:
    """Rebuild an existing edge line with its source/target tokens updated
    to *new_source*/*new_target*, preserving everything else about the line
    (label, variant, any trailing comment) verbatim. ``None`` if the line
    doesn't parse as expected (defensive; should not happen for a line
    :func:`_existing_edges` already classified as an edge).

    Used for a SURVIVING edge (kept, not re-derived) whose endpoint was
    itself renamed this run (a reparented or self-healed survivor, see
    plan_sync) -- otherwise its kept-verbatim text goes on referencing an id
    nothing declares any more, and the next forward-generate's XML
    validation rejects it as a dangling reference.
    """
    match = _NS_FUNCTION_RE.match(clean_line)
    if match is None:
        return None
    args = _split_top_level_args(match.group("args"))
    if len(args) < 2:
        return None
    if args[0] == old_source:
        args[0] = new_source
    if args[1] == old_target:
        args[1] = new_target
    comment_start = _comment_start(raw_line)
    comment = f" {raw_line[comment_start:]}" if comment_start is not None else ""
    ns, function = match.group("ns"), match.group("function")
    return f"{ns}.{function}({', '.join(args)}){comment}"


def _replace_keyword(
    args: list[str], name: str, rendered_value: str | None
) -> None:
    for index, argument in enumerate(args):
        if not _KWARG_RE.match(argument):
            continue
        key, _value = argument.split("=", 1)
        if key.strip() != name:
            continue
        if rendered_value is None:
            args.pop(index)
        else:
            args[index] = f"{name}={rendered_value}"
        return
    if rendered_value is not None:
        args.append(f"{name}={rendered_value}")


def _replace_structural_argument(
    args: list[str], name: str, position: int, rendered_value: str
) -> None:
    for index, argument in enumerate(args):
        if not _KWARG_RE.match(argument):
            continue
        key, _value = argument.split("=", 1)
        if key.strip() == name:
            args[index] = f"{name}={rendered_value}"
            return
    positional_indexes = [
        index for index, argument in enumerate(args) if not _KWARG_RE.match(argument)
    ]
    if position < len(positional_indexes):
        args[positional_indexes[position]] = rendered_value
    else:
        args.append(f"{name}={rendered_value}")


def _rewrite_matched_edge(
    existing_edge: ExistingEdge,
    current: CurrentEdge,
    cell: Cell,
    raw_line: str,
    clean_line: str,
) -> str | None:
    """Update one identity-matched edge while preserving compatible kwargs."""
    match = _NS_FUNCTION_RE.match(clean_line)
    if match is None:
        return None
    current_meta = _shape_meta(current.shape_id) if current.shape_id else None
    existing_meta = _shape_meta(existing_edge.shape_id)
    comment_start = _comment_start(raw_line)
    comment = f" {raw_line[comment_start:]}" if comment_start is not None else ""

    if current_meta is not None and (
        existing_meta is None or current_meta[:2] != existing_meta[:2]
    ):
        rendered = _render_edge_declaration(
            cell,
            current.shape_id or existing_edge.shape_id,
            current.source_node_id,
            current.target_node_id,
            current.cell_id,
        )
        return f"{rendered}{comment}" if rendered is not None else None

    args = _split_top_level_args(match.group("args"))
    _replace_structural_argument(args, "source", 0, current.source_node_id)
    _replace_structural_argument(args, "target", 1, current.target_node_id)
    if current.label is not None:
        rendered_label = (
            f'"{_escape_dsl_string(current.label)}"' if current.label else None
        )
        label_token = _argument_token(args, "label", 2)
        if label_token is not None:
            if any(
                _KWARG_RE.match(argument)
                and argument.split("=", 1)[0].strip() == "label"
                for argument in args
            ):
                _replace_keyword(args, "label", rendered_label)
            else:
                positional_indexes = [
                    index
                    for index, argument in enumerate(args)
                    if not _KWARG_RE.match(argument)
                ]
                if len(positional_indexes) >= 3:
                    label_index = positional_indexes[2]
                    if rendered_label is None:
                        args.pop(label_index)
                    else:
                        args[label_index] = rendered_label
        elif rendered_label is not None:
            _replace_keyword(args, "label", rendered_label)

    variant = current_meta[2] if current_meta is not None else existing_edge.variant
    _replace_keyword(args, "variant", str(variant) if variant != 1 else None)
    _replace_keyword(
        args,
        "visible",
        str(current.visible) if current.visible is not None else None,
    )
    _replace_keyword(args, "edge_id", f'"{_escape_dsl_string(current.cell_id)}"')
    ns, function = match.group("ns"), match.group("function")
    return f"{ns}.{function}({', '.join(args)}){comment}"


def _rewrite_node_label_line(
    raw_line: str,
    clean_line: str,
    new_label: str,
) -> str | None:
    """Replace one surviving node declaration's label and preserve its shape.

    The node id, function, description/other arguments, block colon, indentation,
    and trailing comment remain intact. Both the normal positional label and the
    registry-supported ``label=`` spelling are handled. ``None`` is defensive:
    callers leave an unparseable line untouched rather than risking corruption.
    """
    match = _NS_FUNCTION_RE.match(clean_line)
    if match is None:
        return None
    args = _split_top_level_args(match.group("args"))
    if not args:
        return None

    rendered_label = f'"{_escape_dsl_string(new_label)}"'
    label_kwarg = next(
        (i for i, argument in enumerate(args) if _LABEL_KWARG_RE.match(argument)),
        None,
    )
    if label_kwarg is not None:
        args[label_kwarg] = f"label={rendered_label}"
    elif len(args) >= 2 and _KWARG_RE.match(args[1]) is None:
        args[1] = rendered_label
    else:
        args.insert(1, rendered_label)

    indent_width = len(raw_line) - len(raw_line.lstrip(" \t"))
    indent = raw_line[:indent_width]
    colon = ":" if clean_line.rstrip().endswith(":") else ""
    comment_start = _comment_start(raw_line)
    comment = f" {raw_line[comment_start:]}" if comment_start is not None else ""
    ns, function = match.group("ns"), match.group("function")
    return f"{indent}{ns}.{function}({', '.join(args)}){colon}{comment}"


def _block_range(existing: ExistingIndex, node_id: str) -> tuple[int, int]:
    """``[start, end)`` line range spanning ``node_id``'s own declaration and
    everything nested under it -- the whole subtree that must disappear
    together if ``node_id`` itself is gone. Mirrors the block-boundary scan
    :func:`_child_insertion` already does to find where a container's
    existing children end.
    """
    start = existing.node_line[node_id]
    if not _opens_block(existing, node_id):
        return start, start + 1
    container_indent = len(existing.node_indent[node_id])
    end = start + 1
    for i in range(start + 1, len(existing.lines)):
        line = existing.lines[i]
        if not line.strip():
            end = i + 1
            continue
        indent_width = len(line) - len(line.lstrip(" \t"))
        if indent_width <= container_indent:
            break
        end = i + 1
    return start, end


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted, non-overlapping union of ``[start, end)`` ranges -- a removed
    node nested inside another removed node's own range collapses into one
    (its block is already covered by the outer one)."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True)
class SyncPlan:
    """``mdg sync``'s plan: everything :class:`MergePlan` already computes
    (additions), plus the existing line ranges to delete -- vertices/edges
    no longer present in the current ``.drawio``. draw.io is the source of
    truth: anything genuinely gone there is removed here, never left behind.

    ``node_label_rewrites`` applies labels edited in draw.io to SURVIVING node
    declarations without re-deriving/reordering them. ``edge_token_rewrites``
    covers all semantic updates to an identity-matched edge: migration to an
    ``edge_id``, endpoints, label, type/variant, and authored visibility.
    """

    merge_plan: MergePlan
    removed_ranges: list[tuple[int, int]]
    removed_vertex_count: int
    removed_edge_count: int
    node_label_rewrites: dict[int, str]
    edge_token_rewrites: dict[int, str]


@dataclass(frozen=True)
class CurrentEdge:
    """One draw.io edge with both stable identity and derived semantics."""

    cell_id: str
    shape_id: str | None
    raw_source_id: str
    raw_target_id: str
    source_node_id: str
    target_node_id: str
    label: str | None
    visible: bool | None


def _current_edges(
    cells: list[Cell],
    result: DocumentResult,
    raw_cells: dict[str, RawCell],
    node_ids: dict[str, str],
    existing_ids: set[str],
) -> list[CurrentEdge]:
    results_by_id = {cell.cell_id: cell for cell in result.cells}
    cells_by_id = {cell.cell_id: cell for cell in cells}
    current: list[CurrentEdge] = []
    for cell in cells:
        raw = raw_cells.get(cell.cell_id)
        if raw is None or not raw.is_edge or not raw.source_id or not raw.target_id:
            continue
        source = _resolve_endpoint(raw.source_id, node_ids, existing_ids)
        target = _resolve_endpoint(raw.target_id, node_ids, existing_ids)
        if source is None or target is None:
            continue
        resolved = results_by_id.get(cell.cell_id)
        derived_shape_id = (
            resolved.chosen.shape_id
            if resolved is not None and resolved.chosen is not None
            else None
        )
        source_cell = cells_by_id[cell.cell_id]
        shape_id = (
            _edge_shape_from_provenance(source_cell, resolved) or derived_shape_id
        )
        current.append(
            CurrentEdge(
                cell.cell_id,
                shape_id,
                raw.source_id,
                raw.target_id,
                source,
                target,
                _edge_label_for(source_cell),
                _edge_visibility_for(source_cell),
            )
        )
    return current


def _match_edges(
    existing_edges: list[ExistingEdge], current_edges: list[CurrentEdge]
) -> dict[int, CurrentEdge]:
    """Match persistent IDs first, then migrate legacy edges semantically."""
    unmatched = {edge.cell_id: edge for edge in current_edges}
    matches: dict[int, CurrentEdge] = {}

    for existing_edge in existing_edges:
        if existing_edge.edge_id is None:
            continue
        current = unmatched.pop(existing_edge.edge_id, None)
        if current is not None:
            matches[existing_edge.line_index] = current

    for existing_edge in existing_edges:
        if existing_edge.line_index in matches or existing_edge.edge_id is not None:
            continue
        exact = next(
            (
                current
                for current in unmatched.values()
                if current.shape_id == existing_edge.shape_id
                and current.raw_source_id == existing_edge.source_token
                and current.raw_target_id == existing_edge.target_token
                and (
                    current.label is None
                    or current.label == existing_edge.label
                )
                and current.visible == existing_edge.visible
            ),
            None,
        )
        current = exact or next(
            (
                candidate
                for candidate in unmatched.values()
                if candidate.raw_source_id == existing_edge.source_token
                and candidate.raw_target_id == existing_edge.target_token
            ),
            None,
        )
        if current is not None:
            matches[existing_edge.line_index] = current
            unmatched.pop(current.cell_id)

    return matches


def _existing_parent_by_id(existing: ExistingIndex) -> dict[str, str | None] | None:
    """``{node_id: parent_node_id}`` for every vertex the existing ``.mdg``
    text currently declares, parsed the same way the real forward pipeline
    would see it -- ``None`` if the text doesn't parse (defensive: sync must
    never itself be the reason an otherwise-workable file becomes
    unparseable to reason about; a caller that can't determine parentage
    simply skips reparent detection for this run rather than crashing)."""
    try:
        document = parse_mdg("\n".join(existing.lines))
    # Deliberately broad, mirroring validate()'s own contract: any parse
    # failure here just disables reparent detection, never sync itself.
    except Exception:
        return None
    if not isinstance(document, Document):
        return None
    return {node.id: node.parent_id for node in document.nodes}


def _surviving_node_label_rewrites(
    existing: ExistingIndex,
    cells: list[Cell],
    surviving_ids: set[str],
) -> dict[int, str]:
    """Line rewrites for surviving nodes whose draw.io label was edited."""
    try:
        document = parse_mdg("\n".join(existing.lines))
    except Exception:
        return {}
    if not isinstance(document, Document):
        return {}

    old_labels = {node.id: node.label for node in document.nodes}
    cells_by_id = {cell.cell_id: cell for cell in cells if not cell.is_edge}
    rewrites: dict[int, str] = {}
    for node_id in surviving_ids:
        cell = cells_by_id.get(node_id)
        if cell is None:
            continue
        new_label = _label_for(cell)
        if new_label is None or new_label == old_labels.get(node_id):
            continue
        line_index = existing.node_line[node_id]
        rewritten = _rewrite_node_label_line(
            existing.lines[line_index], existing.clean_lines[line_index], new_label
        )
        if rewritten is not None:
            rewrites[line_index] = rewritten
    return rewrites


def _reparented_survivor_ids(
    existing: ExistingIndex,
    current_cell_ids: set[str],
    containments: dict[str, Containment],
) -> set[str]:
    """Existing node ids whose container in the CURRENT ``.drawio`` (per
    *containments*) no longer matches what the existing ``.mdg`` text
    currently declares -- e.g. a cell the user dragged into a different or
    newly-created container without otherwise changing it. Its own cell_id
    still matches, so ``plan_sync``'s "don't disturb what's already there"
    contract would otherwise leave its declaration exactly where it is,
    silently stale relative to where it now actually sits.
    """
    existing_parent_by_id = _existing_parent_by_id(existing)
    if existing_parent_by_id is None:
        return set()
    reparented = set()
    for node_id, old_parent in existing_parent_by_id.items():
        if node_id not in current_cell_ids:
            continue  # genuinely gone -- the other removal path covers it
        containment = containments.get(node_id)
        new_parent = containment.container_node_id if containment else None
        if new_parent != old_parent:
            reparented.add(node_id)
    return reparented


def plan_sync(
    existing: ExistingIndex,
    cells: list[Cell],
    result: DocumentResult,
    node_ids: dict[str, str],
    containments: dict[str, Containment],
    raw_cells: dict[str, RawCell],
) -> SyncPlan:
    """Compute :func:`plan_merge`'s usual additions PLUS removal of any
    existing vertex or edge no longer present in the current ``.drawio``.

    Vertices/edges that persist keep their exact existing text untouched --
    same "don't disturb what's already there" contract as ``plan_merge``;
    only genuinely gone content is deleted and genuinely new content is
    added. A vertex whose OWN cell_id is gone takes its whole nested subtree
    with it (:func:`_block_range`); an edge is removed if either endpoint is
    gone, or if its (source, target) pair has fewer current edge cells than
    existing declared lines -- per-pair COUNT, not membership, since more
    than one edge can share a pair (see
    :func:`_split_surviving_and_extra_cells`): the earliest-declared lines
    for a pair survive first, any excess beyond the current cell count is
    removed.

    A survivor whose CONTAINER changed (:func:`_reparented_survivor_ids`) --
    still present, own id unchanged, just moved to sit under a different
    parent in the current ``.drawio`` -- has its own declaration (and any
    subtree nested under it) removed and re-derived too, same as a genuinely
    new cell, so it lands back under its current container instead of
    silently keeping a stale parent. Its OWN existing edges are deliberately
    NOT swept up in that removal (only a truly-gone vertex forces its edges
    out): they reference it by id, unaffected by where its declaration now
    lives in the file.
    """
    current_cell_ids = {c.cell_id for c in cells}
    truly_removed_roots = existing.node_ids() - current_cell_ids
    reparented_roots = _reparented_survivor_ids(
        existing, current_cell_ids, containments
    )
    removed_roots = truly_removed_roots | reparented_roots
    vertex_ranges = [_block_range(existing, node_id) for node_id in removed_roots]

    existing_edges = _existing_edges(existing)
    current_edges = _current_edges(
        cells, result, raw_cells, node_ids, existing.node_ids()
    )
    edge_matches = _match_edges(existing_edges, current_edges)
    removed_edge_lines = [
        edge.line_index
        for edge in existing_edges
        if edge.line_index not in edge_matches
    ]
    edge_ranges = [(i, i + 1) for i in removed_edge_lines]

    removed_ranges = _merge_ranges(vertex_ranges + edge_ranges)

    # A node_id whose declaration falls inside a removed range must not
    # block the add-phase from re-deriving a cell that individually still
    # exists in the .drawio (e.g. a shape whose old parent container was
    # deleted, now re-parented or top-level): its old declaration is gone,
    # so it must look "new" again, not "already represented".
    removed_line_set = {i for start, end in removed_ranges for i in range(start, end)}
    surviving_ids = {
        node_id
        for node_id, line in existing.node_line.items()
        if line not in removed_line_set
    }
    reduced_existing = ExistingIndex(
        existing.lines,
        existing.clean_lines,
        {k: v for k, v in existing.node_line.items() if k in surviving_ids},
        {k: v for k, v in existing.node_indent.items() if k in surviving_ids},
    )

    removed_edge_line_set = set(removed_edge_lines)
    matched_cell_ids = {edge.cell_id for edge in edge_matches.values()}
    reduced_cells = [
        cell for cell in result.cells if cell.cell_id not in matched_cell_ids
    ]
    reduced_result = DocumentResult(
        reduced_cells, result.library_scores, result.anchor_votes
    )

    merge_plan = plan_merge(
        reduced_existing, cells, reduced_result, node_ids, containments, raw_cells
    )

    node_label_rewrites = _surviving_node_label_rewrites(
        existing, cells, surviving_ids
    )

    edge_token_rewrites: dict[int, str] = {}
    cells_by_id = {cell.cell_id: cell for cell in cells}
    for edge in existing_edges:
        if edge.line_index in removed_edge_line_set:
            continue
        current = edge_matches.get(edge.line_index)
        if current is None:
            continue
        cell = cells_by_id.get(current.cell_id)
        if cell is None:
            continue
        rewritten = _rewrite_matched_edge(
            edge,
            current,
            cell,
            existing.lines[edge.line_index],
            existing.clean_lines[edge.line_index],
        )
        if rewritten is not None and rewritten != existing.lines[edge.line_index]:
            edge_token_rewrites[edge.line_index] = rewritten

    return SyncPlan(
        merge_plan,
        removed_ranges,
        len(removed_roots),
        len(removed_edge_lines),
        node_label_rewrites,
        edge_token_rewrites,
    )


def render_sync(existing: ExistingIndex, plan: SyncPlan) -> str:
    """The full synced text: existing lines minus every removed range, plus
    :func:`plan_merge`'s usual insertions for genuinely new content.

    Applies removal first -- computing a remap from old to new line indices
    -- then reuses :func:`render_merge`'s own splice/colon-fix/tie-break
    logic unchanged against the filtered lines and remapped insertions.
    """
    removed = {i for start, end in plan.removed_ranges for i in range(start, end)}
    remap: dict[int, int] = {}
    kept = 0
    for i in range(len(existing.lines) + 1):
        remap[i] = kept
        if i < len(existing.lines) and i not in removed:
            kept += 1

    line_rewrites = {**plan.edge_token_rewrites, **plan.node_label_rewrites}
    filtered_lines = [
        line_rewrites.get(i, line)
        for i, line in enumerate(existing.lines)
        if i not in removed
    ]
    filtered_clean_lines = [
        _strip_inline_comment(line_rewrites[i])
        if i in line_rewrites
        else line
        for i, line in enumerate(existing.clean_lines)
        if i not in removed
    ]
    remapped_insertions = [
        Insertion(
            anchor_line=remap[insertion.anchor_line],
            text=insertion.text,
            colon_fix_line=(
                None
                if insertion.colon_fix_line is None
                else remap[insertion.colon_fix_line]
            ),
            top_level=insertion.top_level,
            anchor_depth=insertion.anchor_depth,
        )
        for insertion in plan.merge_plan.insertions
    ]
    filtered_existing = ExistingIndex(filtered_lines, filtered_clean_lines, {}, {})
    remapped_plan = MergePlan(
        remapped_insertions,
        plan.merge_plan.skipped,
        plan.merge_plan.new_edge_count,
        plan.merge_plan.new_node_count,
        plan.merge_plan.renamed_ids,
    )
    return render_merge(filtered_existing, remapped_plan)


def validate(text: str) -> str | None:
    """``None`` if ``text`` parses cleanly; the error message otherwise."""
    try:
        parse_mdg(text)
    # Deliberately broad: the point is to REPORT any parse failure so the
    # caller can leave the file untouched, never to crash the merge.
    except Exception as exc:
        return str(exc)
    return None
