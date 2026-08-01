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

Scope: this only emits new VERTEX declarations. A brand-new connector drawn
between two shapes (a ``c4.Rel(...)``) is not yet emitted -- edges are
reported as a count so nothing is silently lost, but rendering them is a
separate, later extension.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from mdg_drawio.notation import parse as parse_mdg
from mdg_drawio.notation._core.registry import shapes_by_id
from scripts.reverse.containment import Containment
from scripts.reverse.derive import Cell, CellResult, DocumentResult, RawCell

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
    """The existing ``.mdg`` text, indexed by declared node_id."""

    lines: list[str]
    node_line: dict[str, int]
    node_indent: dict[str, str]

    def node_ids(self) -> set[str]:
        return set(self.node_line)


def index_existing(text: str) -> ExistingIndex:
    """Scan an existing ``.mdg`` for every declared node_id, keeping the
    FIRST declaration if an id oddly repeats (defensive, mirrors load_cells'
    keep-first precedent for malformed input)."""
    lines = text.splitlines()
    node_line: dict[str, int] = {}
    node_indent: dict[str, str] = {}
    for i, line in enumerate(lines):
        match = _CALL_LINE_RE.match(line)
        if not match:
            continue
        node_id = _first_arg_token(match.group("args"))
        if node_id and node_id not in node_line:
            node_line[node_id] = i
            node_indent[node_id] = match.group("indent")
    return ExistingIndex(lines, node_line, node_indent)


def _child_insertion(
    existing: ExistingIndex, container_node_id: str
) -> tuple[int, str]:
    """(line index to insert before, indent string for the new child)."""
    start = existing.node_line[container_node_id]
    container_indent = len(existing.node_indent[container_node_id])
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
    line_index = existing.node_line[container_node_id]
    line = existing.lines[line_index]
    if line.rstrip().endswith(":"):
        return False
    end, _ = _child_insertion(existing, container_node_id)
    return end == line_index + 1  # no descendant lines found -- first child


def _label_for(cell: Cell) -> str | None:
    """The best label a hand-drawn cell offers, or ``None`` if there isn't a
    trustworthy one -- callers fall back to the ``label or node_id``
    convention the forward engine already applies (GRAMMAR.md/dsl_engine.py).

    Prefers a C4 object cell's own ``c4Name`` attribute over its plain
    ``value`` (which, for an untouched palette cell, is only an
    unsubstituted ``%c4Name%`` template -- see :class:`Cell`). Anything that
    still looks like a template placeholder or carries raw HTML is skipped
    rather than guessed at.
    """
    name = cell.object_attrs.get("c4Name", "").strip()
    if name and "%" not in name:
        return name
    value = cell.value.strip()
    if value and "%" not in value and "<" not in value:
        return value
    return None


def _shape_meta(shape_id: str) -> tuple[str, str, int] | None:
    """(library, function name, variant) from the registry, or ``None`` if
    the shape id doesn't resolve (defensive; should not happen for a shape
    that came from the loaded StyleIndex)."""
    parts = shape_id.split(".")
    if len(parts) < 2:
        return None
    library = parts[0]
    try:
        entry = shapes_by_id(library).get(shape_id)
    except KeyError:
        return None
    if entry is None:
        return None
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
        args.append(f'"{escape(label)}"')
    kwargs = f", variant={variant}" if variant != 1 else ""
    colon = ":" if opens_block else ""
    return f"{indent}{library}.{function}({', '.join(args)}{kwargs}){colon}"


@dataclass(frozen=True)
class NewNode:
    """One new cell, plus its own new (not-yet-existing) children, recursively."""

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
    """

    anchor_line: int  # insert before this existing line index
    text: str  # the fully rendered, indented block (no trailing newline)
    colon_fix_line: int | None = None  # existing line needing ':' appended
    top_level: bool = False  # append at EOF with a blank-line separator


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
) -> tuple[dict[str | None, list[NewNode]], dict[str, str]]:
    """Group new cells by their nearest EXISTING ancestor (or ``None`` for
    top-level), nesting any new cells whose container is ALSO new underneath
    it. Returns ``{anchor: [roots...]}`` plus ``{cell_id: anchor}`` for
    reporting. Precondition: every cell in ``new_cells`` is resolved (its
    ``chosen`` is not ``None``) -- callers (:func:`plan_merge`) already
    filter unresolved cells out before reaching here.
    """
    new_by_cell_id = {c.cell_id: c for c in new_cells}
    node_of: dict[str, NewNode] = {}
    for c in new_cells:
        assert c.chosen is not None  # precondition, see docstring
        node_of[c.cell_id] = NewNode(c.cell_id, c.cell_id, c.chosen.shape_id)
    roots_by_anchor: dict[str | None, list[NewNode]] = {}
    anchor_of: dict[str, str] = {}

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
            anchor_of[cell.cell_id] = anchor or "(top level)"

    return roots_by_anchor, anchor_of


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

    new_cells: list[CellResult] = []
    skipped: list[str] = []
    new_edge_count = 0
    for cell in result.cells:
        if cell.cell_id in existing_ids:
            continue
        raw = raw_cells.get(cell.cell_id)
        if raw is not None and raw.is_edge:
            if cell.chosen is not None:
                new_edge_count += 1
            continue
        if cell.chosen is None:
            skipped.append(
                f"{cell.cell_id}: could not derive a shape ({cell.resolved_by})"
            )
            continue
        new_cells.append(cell)

    roots_by_anchor, _ = _build_forest(
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
                    Insertion(len(existing.lines), text, top_level=True)
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
        insertions.append(Insertion(line_index, text, colon_fix))

    return MergePlan(insertions, skipped, new_edge_count, len(new_cells))


def render_merge(existing: ExistingIndex, plan: MergePlan) -> str:
    """The full merged text -- applies every insertion, highest line index
    first, so earlier (lower-index) insertion points stay valid."""
    lines = list(existing.lines)
    for insertion in plan.insertions:
        if insertion.colon_fix_line is not None:
            lines[insertion.colon_fix_line] = (
                lines[insertion.colon_fix_line].rstrip() + ":"
            )
    for insertion in sorted(plan.insertions, key=lambda i: i.anchor_line, reverse=True):
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
