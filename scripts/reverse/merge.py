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
(``scripts/reverse/merge_cli.py``) is responsible for the dry-run-by-default,
validate-before-write contract.

Edges are emitted too, as flat top-level statements (``lib.Function(source,
target[, label])``, per GRAMMAR.md -- edges take no block and aren't nested),
appended after any new top-level vertex subtree. An edge's endpoints must
themselves resolve to a known node -- either newly assigned in this run, or
already declared in the existing file -- otherwise it is skipped and reported,
same as an unresolved vertex.

Dedup scope: a vertex's identity is its draw.io cell_id (see above), but an
edge has no id of its own in the ``.mdg`` grammar to key on. Re-running a
merge against an unchanged source is still safe -- an edge is skipped if its
exact rendered line already appears verbatim in the existing file -- but a
logically-identical edge re-labeled or re-routed through a different cell_id
is NOT detected as a duplicate. Closing that gap needs cross-referencing
existing edge declarations against the registry (which function names are
edges) rather than a text-level check; out of scope here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mdg_drawio.notation import parse as parse_mdg

from .containment import Containment
from .derive import Cell, CellResult, DocumentResult, RawCell
from .style_index import registry_entry

INDENT_STEP = "    "

# Mirrors CALL_RE's shape (dsl_engine.py) independently -- scripts/reverse may
# not import a leading-underscore module member across the package boundary
# (enforced by tests/test_architecture.py), so this is a small, purpose-built
# re-implementation of just the structural facts a merge needs: is this line a
# call, what's its indent, and does it open a block. It does not need to
# understand args, kwargs, or foreign-namespace passthrough the way the real
# engine does.
_CALL_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*"
    r"\((?P<args>.*)\)\s*(?P<colon>:?)\s*$"
)


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
    keep-first precedent for malformed input)."""
    lines = text.splitlines()
    clean_lines = [_strip_inline_comment(line) for line in lines]
    node_line: dict[str, int] = {}
    node_indent: dict[str, str] = {}
    for i, line in enumerate(clean_lines):
        match = _CALL_LINE_RE.match(line)
        if not match:
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

    Anything that still looks like a template placeholder or carries raw
    HTML is skipped rather than guessed at.
    """
    name = cell.object_attrs.get("c4Name", "").strip()
    if name and "%" not in name:
        return name
    label = cell.object_attrs.get("label", "").strip()
    if label and "%" not in label and "<" not in label:
        return label
    value = cell.value.strip()
    if value and "%" not in value and "<" not in value:
        return value
    return None


def _edge_label_for(cell: Cell) -> str | None:
    """Extract connector text, including C4's edge-specific object field."""
    description = cell.object_attrs.get("c4Description", "").strip()
    if description and "%" not in description and "<" not in description:
        return description
    return _label_for(cell)


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
    cell: Cell, shape_id: str, source_node_id: str, target_node_id: str
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
    return f"{library}.{function}({', '.join(args)}{variant_suffix})"


@dataclass
class NewNode:
    """One new cell, plus its own new (not-yet-existing) children, recursively.

    Not frozen: this is a builder, assembled incrementally by
    :func:`_build_forest` (which appends each node's children as it discovers
    them), the same way :class:`~scripts.reverse.derive.CellResult` or
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
    changed."""

    insertions: list[Insertion]
    skipped: list[str]
    new_edge_count: int
    new_node_count: int


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

    ``existing_ids`` is checked FIRST: :func:`~scripts.reverse.naming.
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
        line = _render_edge_declaration(
            cells_by_id[edge.cell_id],
            edge.shape_id,
            edge.source_node_id,
            edge.target_node_id,
        )
        if line is None or line in existing.clean_lines:
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
    return MergePlan(insertions, skipped, new_edge_count, len(new_cells))


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


def validate(text: str) -> str | None:
    """``None`` if ``text`` parses cleanly; the error message otherwise."""
    try:
        parse_mdg(text)
    except Exception as exc:  # noqa: BLE001 -- report any parse failure, don't crash
        return str(exc)
    return None
