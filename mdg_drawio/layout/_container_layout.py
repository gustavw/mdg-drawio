"""Container layout utilities — parent/child layout and boundary geometry.

Works with ``Node`` and ``Edge`` types from the layout package contract.
Pure geometry helpers; does not depend on any notation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil

from mdg_drawio.contracts import (
    DEFAULT_BOTTOM_PADDING,
    DEFAULT_TOP_PADDING,
)

from ._types import Edge, Node, SizeResolver

_NARROW = frozenset("iIl1!|.,;:tf ")
_WIDE = frozenset("mwMWOQD@%")
_AVG_CHAR_WIDTH = 6.5
_WIDE_CHAR_WIDTH = 9.5
_BOLD_WIDTH_MULTIPLIER = 1.1
_DEFAULT_FONT_SIZE = 11
_RELATIVE_CHILDREN_KEY = "_layout_children_relative"
_DEFAULT_BOUNDARY_PADDING: dict[str, float] = {
    "top": DEFAULT_TOP_PADDING,
    "right": DEFAULT_TOP_PADDING,
    "bottom": DEFAULT_BOTTOM_PADDING,
    "left": DEFAULT_TOP_PADDING,
}

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class ContainerState:
    """Hierarchy facts computed while laying out container interiors."""

    parent_by_id: dict[str, str]
    top_level_nodes: list[Node]
    top_level_by_id: dict[str, str]


@dataclass(frozen=True)
class _ContainerLayoutOptions:
    """Shared settings for parent-relative container layout."""

    direction: str
    rank_gap: float
    column_gap: float
    default_padding: dict[str, float]


def estimate_text_width(
    text: str, font_size: int = _DEFAULT_FONT_SIZE, bold: bool = False
) -> float:
    """Estimate pixel width of *text* without a font engine.

    Uses per-character width tables derived from Helvetica metrics at 11 px.
    Bold adds ~10 %.  Adequate for minimum-width calculations; not pixel-perfect.
    """
    width = 0.0
    for ch in text:
        if ch in _NARROW:
            width += 4.0
        elif ch in _WIDE:
            width += _WIDE_CHAR_WIDTH
        else:
            width += _AVG_CHAR_WIDTH
    if bold:
        width *= _BOLD_WIDTH_MULTIPLIER
    return width * font_size / _DEFAULT_FONT_SIZE


def _node_size(node: Node, size_of: SizeResolver) -> tuple[float, float]:
    default_w, default_h = size_of(node.type)
    w = node.width if node.width else float(default_w)
    h = node.height if node.height else float(default_h)
    return w, h


def _contains_ids(node: Node) -> list[str]:
    raw = node.extra.get("contains", [])
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, Iterable):
        return []
    return [str(value) for value in raw if value]


def _would_create_parent_cycle(
    child_id: str,
    parent_id: str,
    parent_by_id: dict[str, str],
) -> bool:
    current = parent_id
    seen: set[str] = set()
    while current:
        if current == child_id:
            return True
        if current in seen:
            return True
        seen.add(current)
        current = parent_by_id.get(current, "")
    return False


def _build_parent_map(nodes: list[Node]) -> dict[str, str]:
    node_by_id = {node.id: node for node in nodes}
    parent_by_id: dict[str, str] = {}

    for node in nodes:
        parent_id = node.parent_id
        if (
            parent_id is not None
            and parent_id in node_by_id
            and parent_id != node.id
            and not _would_create_parent_cycle(node.id, parent_id, parent_by_id)
        ):
            parent_by_id[node.id] = parent_id

    for parent in nodes:
        for child_id in _contains_ids(parent):
            child = node_by_id.get(child_id)
            if child is None or child.id == parent.id:
                continue
            existing_parent = parent_by_id.get(child.id)
            if existing_parent is None:
                if _would_create_parent_cycle(child.id, parent.id, parent_by_id):
                    continue
                parent_by_id[child.id] = parent.id
                child.parent_id = parent.id
            elif existing_parent != parent.id:
                continue

    return parent_by_id


def _children_by_parent(
    nodes: list[Node],
    parent_by_id: dict[str, str],
) -> dict[str, list[Node]]:
    by_parent: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        parent_id = parent_by_id.get(node.id)
        if parent_id is not None:
            by_parent[parent_id].append(node)
    return by_parent


def _top_level_id(node_id: str, parent_by_id: dict[str, str]) -> str:
    current = node_id
    seen: set[str] = set()
    while current in parent_by_id and current not in seen:
        seen.add(current)
        current = parent_by_id[current]
    return current


def _coerce_padding(value: object, node: Node, key: str) -> float:
    """Coerce a padding override to float, naming the node/key on failure."""
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            pass
    raise ValueError(
        f"node {node.id!r}: padding {key!r} must be numeric, got {value!r}"
    )


def _padding_values(
    node: Node,
    default_padding: dict[str, float],
) -> tuple[float, float, float, float]:
    padding = dict(default_padding)
    raw_padding = node.extra.get("padding", {})
    if isinstance(raw_padding, dict):
        padding.update(raw_padding)

    for key in ("top", "right", "bottom", "left"):
        extra_key = f"padding_{key}"
        if extra_key in node.extra:
            padding[key] = node.extra[extra_key]

    # Additive per-side clearance on top of the resolved inset (e.g. to keep
    # inner shapes clear of a top header). See generator ``style_overrides.yaml``.
    for key in ("top", "right", "bottom", "left"):
        add = node.extra.get(f"padding_extra_{key}")
        if add is not None:
            base = _coerce_padding(padding.get(key, 0), node, key)
            padding[key] = base + _coerce_padding(add, node, f"padding_extra_{key}")

    return (
        _coerce_padding(padding.get("top", 0), node, "top"),
        _coerce_padding(padding.get("right", 0), node, "right"),
        _coerce_padding(padding.get("bottom", 0), node, "bottom"),
        _coerce_padding(padding.get("left", 0), node, "left"),
    )


def _position_children_lr(
    ranked: list[list[Node]],
    *,
    top: float,
    left: float,
    rank_gap: float,
    child_gap: float,
) -> None:
    x = left
    for rank in ranked:
        y = top
        for child in rank:
            child.x = x
            child.y = y
            y += child.height + child_gap
        x += max((child.width for child in rank), default=0.0) + rank_gap


def _position_children_tb(
    ranked: list[list[Node]],
    *,
    top: float,
    left: float,
    rank_gap: float,
    child_gap: float,
) -> None:
    y = top
    for rank in ranked:
        x = left
        for child in rank:
            child.x = x
            child.y = y
            x += child.width + child_gap
        y += max((child.height for child in rank), default=0.0) + rank_gap


# Containers with at least this many children are packed into a grid rather than
# one rank-per-column row — a long dependency chain of components would otherwise
# spread across the whole canvas (see the generated Code view).
_GRID_MIN_CHILDREN = 5
# Target grid shape: slightly wider than tall (reads well for component clusters).
_GRID_ASPECT = 1.6


def _position_children_grid(
    children: list[Node],
    *,
    top: float,
    left: float,
    col_gap: float,
    row_gap: float,
) -> None:
    """Pack *children* into a left-to-right, top-to-bottom grid.

    Column count targets ``_GRID_ASPECT`` so a coupled cluster stays compact
    instead of stretching into one long row/column. Columns are sized to their
    own widest member (and rows to their tallest), so a single wide child does
    not inflate every column. Input order (rank order) is preserved as reading
    order, so dependency flow is still roughly visible.
    """
    if not children:
        return
    cols = max(1, ceil((len(children) * _GRID_ASPECT) ** 0.5))
    rows = ceil(len(children) / cols)

    col_width = [0.0] * cols
    row_height = [0.0] * rows
    for index, child in enumerate(children):
        row, col = divmod(index, cols)
        col_width[col] = max(col_width[col], child.width)
        row_height[row] = max(row_height[row], child.height)

    col_x = [left] * cols
    for col in range(1, cols):
        col_x[col] = col_x[col - 1] + col_width[col - 1] + col_gap
    row_y = [top] * rows
    for row in range(1, rows):
        row_y[row] = row_y[row - 1] + row_height[row - 1] + row_gap

    for index, child in enumerate(children):
        row, col = divmod(index, cols)
        child.x = col_x[col]
        child.y = row_y[row]


def _position_ranked_children(
    ranked: list[list[Node]],
    options: _ContainerLayoutOptions,
    *,
    top: float,
    left: float,
    child_gap: float,
) -> None:
    if options.direction == "TB":
        _position_children_tb(
            ranked,
            top=top,
            left=left,
            rank_gap=options.rank_gap,
            child_gap=child_gap,
        )
        return

    _position_children_lr(
        ranked,
        top=top,
        left=left,
        rank_gap=options.rank_gap,
        child_gap=child_gap,
    )


def _grow_parent_to_fit_children(
    parent: Node,
    children: list[Node],
    *,
    right_pad: float,
    bottom_pad: float,
) -> None:
    max_right = max((child.x + child.width for child in children), default=0.0)
    max_bottom = max((child.y + child.height for child in children), default=0.0)
    parent.width = max(parent.width, max_right + right_pad)
    parent.height = max(parent.height, max_bottom + bottom_pad)


# Row text padding (item spacingLeft 4 + spacingRight 4) and a cushion so text
# rendered at draw.io's default font never wraps against our 11px width estimate.
_STACK_TEXT_PADDING = 8.0
_STACK_WIDTH_CUSHION = 1.15


def _stack_children(parent: Node, children: list[Node]) -> None:
    """Lay children out as a tight vertical stack, matching draw.io's
    ``childLayout=stackLayout`` (e.g. UML class member rows).

    Children fill the parent width and stack directly below the title band
    (``start_size``) with no gaps; the parent is widened to fit the longest row
    (and the title) so nothing wraps, and sized to the exact stacked height.
    This makes our geometry identical to what draw.io computes on load, so its
    stack re-layout is a no-op instead of shrinking/reflowing the shape.
    """
    start = float(parent.extra.get("start_size", 0))

    needed_width = parent.width
    if parent.label:
        title = estimate_text_width(parent.label, bold=True) + _STACK_TEXT_PADDING
        needed_width = max(needed_width, title)
    for child in children:
        row = estimate_text_width(child.label) * _STACK_WIDTH_CUSHION
        needed_width = max(needed_width, row + _STACK_TEXT_PADDING)
    parent.width = needed_width

    y = start
    for child in children:
        child.x = 0.0
        child.y = y
        child.width = parent.width
        y += child.height
    parent.height = y


def _layout_container_children(
    parent: Node,
    children: list[Node],
    edges: list[Edge],
    parent_by_id: dict[str, str],
    size_of: SizeResolver,
    options: _ContainerLayoutOptions,
) -> None:
    parent.extra[_RELATIVE_CHILDREN_KEY] = True
    for child in children:
        child.width, child.height = _node_size(child, size_of)

    if parent.extra.get("child_layout") == "stack":
        _stack_children(parent, children)
        return

    top_pad, right_pad, bottom_pad, left_pad = _padding_values(
        parent,
        options.default_padding,
    )
    child_gap = float(parent.extra.get("child_gap", options.column_gap))
    ranked = _rank_sibling_nodes(children, edges, parent_by_id)
    top = float(parent.extra.get("start_size", 0)) + top_pad
    # A "degenerate" ranking has no rank wider than one node — a dependency chain
    # (or a cycle that defeats ranking), so the primary axis alone would produce a
    # long thin strip (N columns in LR, N rows in TB). Grid-pack those to use both
    # axes. A ranking *with* parallelism (some rank has ≥2 nodes) keeps the primary
    # flow: siblings spread on the secondary axis, ranks advance on the primary —
    # i.e. primary TB ⇒ secondary LR, and vice versa.
    degenerate = max((len(rank) for rank in ranked), default=0) <= 1
    if degenerate and len(children) >= _GRID_MIN_CHILDREN:
        ordered = [child for rank in ranked for child in rank]
        _position_children_grid(
            ordered,
            top=top,
            left=left_pad,
            col_gap=child_gap,
            row_gap=options.rank_gap,
        )
    else:
        _position_ranked_children(
            ranked,
            options,
            top=top,
            left=left_pad,
            child_gap=child_gap,
        )
    _grow_parent_to_fit_children(
        parent,
        children,
        right_pad=right_pad,
        bottom_pad=bottom_pad,
    )


def _layout_container_tree(
    parent_id: str,
    by_parent: dict[str, list[Node]],
    node_by_id: dict[str, Node],
    edges: list[Edge],
    parent_by_id: dict[str, str],
    size_of: SizeResolver,
    options: _ContainerLayoutOptions,
) -> None:
    children = by_parent.get(parent_id, [])
    for child in children:
        if child.id in by_parent:
            _layout_container_tree(
                child.id,
                by_parent,
                node_by_id,
                edges,
                parent_by_id,
                size_of,
                options,
            )

    if children:
        _layout_container_children(
            node_by_id[parent_id],
            children,
            edges,
            parent_by_id,
            size_of,
            options,
        )


def apply_container_layout(
    nodes: list[Node],
    edges: list[Edge],
    size_of: SizeResolver,
    *,
    direction: str,
    rank_gap: float,
    column_gap: float,
    default_padding: dict[str, float],
) -> ContainerState:
    """Lay out generic parent/child containers and return hierarchy state.

    Any node with children from ``parent_id`` or ``extra["contains"]`` behaves
    as a container. Children are positioned relative to their parent so draw.io
    containment works naturally, while the parent expands to enclose its direct
    children plus padding. Non-container nodes are left unchanged.
    """
    if not nodes:
        return ContainerState({}, [], {})

    parent_by_id = _build_parent_map(nodes)
    if not parent_by_id:
        return ContainerState(
            {},
            list(nodes),
            {node.id: node.id for node in nodes},
        )

    node_by_id = {node.id: node for node in nodes}
    by_parent = _children_by_parent(nodes, parent_by_id)
    options = _ContainerLayoutOptions(
        direction=direction,
        rank_gap=rank_gap,
        column_gap=column_gap,
        default_padding=default_padding,
    )

    top_level_nodes = [
        node for node in nodes if node.id not in parent_by_id
    ]
    for node in top_level_nodes:
        if node.id in by_parent:
            _layout_container_tree(
                node.id,
                by_parent,
                node_by_id,
                edges,
                parent_by_id,
                size_of,
                options,
            )

    top_level_by_id = {
        node.id: _top_level_id(node.id, parent_by_id) for node in nodes
    }
    return ContainerState(parent_by_id, top_level_nodes, top_level_by_id)


def absolute_node_boxes(
    nodes: list[Node],
) -> dict[str, Box]:
    """Return absolute page-frame boxes for nodes with parent-relative children."""
    node_by_id = {node.id: node for node in nodes}
    parent_by_id = _build_parent_map(nodes)
    boxes: dict[str, Box] = {}

    def _box(node_id: str) -> Box:
        if node_id in boxes:
            return boxes[node_id]
        node = node_by_id[node_id]
        x = node.x
        y = node.y
        parent_id = parent_by_id.get(node_id)
        if parent_id in node_by_id:
            px, py, _pw, _ph = _box(parent_id)
            x += px
            y += py
        boxes[node_id] = (x, y, node.width, node.height)
        return boxes[node_id]

    for node in nodes:
        _box(node.id)
    return boxes


def _rank_sibling_nodes(
    nodes: list[Node],
    edges: list[Edge],
    parent_by_id: dict[str, str],
) -> list[list[Node]]:
    """Group sibling nodes by relationship-derived topological rank."""
    if not nodes:
        return []

    node_by_id = {n.id: n for n in nodes}
    order = {n.id: idx for idx, n in enumerate(nodes)}
    sibling_ids = set(node_by_id)

    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = defaultdict(int)

    for edge in edges:
        source = _sibling_owner(edge.source_id, sibling_ids, parent_by_id)
        target = _sibling_owner(edge.target_id, sibling_ids, parent_by_id)
        if source is None or target is None or source == target:
            continue
        if target not in outgoing[source]:
            outgoing[source].append(target)
            indegree[target] += 1

    queue = sorted(
        (nid for nid in sibling_ids if indegree[nid] == 0),
        key=order.__getitem__,
    )
    ranks: dict[str, int] = {nid: 0 for nid in sibling_ids}
    visited: list[str] = []

    while queue:
        nid = queue.pop(0)
        visited.append(nid)
        for target in sorted(outgoing[nid], key=order.__getitem__):
            ranks[target] = max(ranks[target], ranks[nid] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    if len(visited) != len(nodes):
        return [[node] for node in nodes]

    rank_numbers = sorted(set(ranks.values()))
    return [
        [
            node_by_id[nid]
            for nid in sorted(
                (nid for nid, rank in ranks.items() if rank == rn),
                key=order.__getitem__,
            )
        ]
        for rn in rank_numbers
    ]


def _sibling_owner(
    node_id: str,
    sibling_ids: set[str],
    parent_by_id: dict[str, str],
) -> str | None:
    """Walk up the parent chain to find the nearest sibling ancestor."""
    current = node_id
    seen: set[str] = set()
    while current and current not in seen:
        if current in sibling_ids:
            return current
        seen.add(current)
        current = parent_by_id.get(current, "")
    return None
