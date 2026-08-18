"""Container layout utilities — parent/child layout and boundary geometry.

Works with ``Node`` and ``Edge`` types from the layout package contract.
Pure geometry helpers; does not depend on any notation.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil

from ._types import Edge, Node, SizeResolver

# Per-character width table for estimate_text_width, from Helvetica metrics at
# _DEFAULT_FONT_SIZE. Kept together here (the only consumer) rather than in
# contracts: they are a property of this estimator, not a cross-package
# contract, and a second copy elsewhere is free to drift out of step with it.
_NARROW = frozenset("iIl1!|.,;:tf ")
_WIDE = frozenset("mwMWOQD@%")
_NARROW_CHAR_WIDTH = 4.0
_AVG_CHAR_WIDTH = 6.5
_WIDE_CHAR_WIDTH = 9.5
_BOLD_WIDTH_MULTIPLIER = 1.1
_DEFAULT_FONT_SIZE = 11
_RELATIVE_CHILDREN_KEY = "_layout_children_relative"

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


@dataclass(frozen=True)
class _SharedRankPlan:
    """A rank->primary-axis-offset scale shared across sibling containers.

    ``ranks`` maps a node id to its rank computed over the COMBINED children
    of every container-sibling (e.g. every Lane's tasks under one Pool), not
    just its own container's children. ``offsets`` maps each distinct rank
    value to a primary-axis offset (x for LR, y for TB) sized from the
    widest/tallest node at that rank ACROSS ALL siblings -- so two lanes
    place same-rank content in the same column even though each one only
    positions its own subset of it.
    """

    ranks: dict[str, int]
    offsets: dict[int, float]


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
            width += _NARROW_CHAR_WIDTH
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
    """Ids this node declares it contains, from either representation.

    ``Node.contains`` is the first-class contract field; ``extra["contains"]``
    is the loose passthrough form. Both are read here because the generator
    keys its container detection off ``Node.contains`` — reading only the
    ``extra`` form meant a node populating the declared field was treated as a
    container when routing its edges but never had its children laid out
    inside it.
    """
    declared = [str(value) for value in node.contains if value]
    raw = node.extra.get("contains", [])
    if isinstance(raw, str):
        return [*declared, raw]
    if not isinstance(raw, Iterable):
        return declared
    return [*declared, *(str(value) for value in raw if value)]


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


def _sibling_adjacency(
    ranked: list[list[Node]],
    edges: list[Edge],
    parent_by_id: dict[str, str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Outgoing/incoming adjacency among this rank set's siblings, collapsing
    any edge endpoint outside the set to its nearest sibling ancestor (see
    :func:`_sibling_owner`)."""
    sibling_ids = {n.id for rank in ranked for n in rank}
    out_adj: dict[str, list[str]] = defaultdict(list)
    in_adj: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source = _sibling_owner(edge.source_id, sibling_ids, parent_by_id)
        target = _sibling_owner(edge.target_id, sibling_ids, parent_by_id)
        if source is None or target is None or source == target:
            continue
        out_adj[source].append(target)
        in_adj[target].append(source)
    return out_adj, in_adj


def _singleton_chain_links(
    ranked: list[list[Node]],
    edges: list[Edge],
    parent_by_id: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """1:1 adjacency among single-occupant ranks: ``next_of``/``prev_of`` map
    a node to its sole chain neighbor, ``singleton_ids`` is every node that is
    alone in its own rank (a precondition for being part of a chain)."""
    out_adj, in_adj = _sibling_adjacency(ranked, edges, parent_by_id)

    singleton_ids = {rank[0].id for rank in ranked if len(rank) == 1}
    next_of: dict[str, str] = {}
    prev_of: dict[str, str] = {}
    for node_id in singleton_ids:
        outs = out_adj.get(node_id, [])
        if len(outs) == 1 and outs[0] in singleton_ids:
            target = outs[0]
            if len(in_adj.get(target, [])) == 1:
                next_of[node_id] = target
                prev_of[target] = node_id
    return next_of, prev_of, singleton_ids


def _bypassed_branch_ids(
    ranked: list[list[Node]],
    edges: list[Edge],
    parent_by_id: dict[str, str],
) -> set[str]:
    """Node ids on the LONGER of two parallel paths between the same
    predecessor and successor -- e.g. a gateway's "no" branch that visits one
    extra step before rejoining where its "yes" branch goes directly.

    Detected structurally: a node N with exactly one predecessor P and one
    successor S, where P ALSO has a direct edge straight to S -- a strictly
    shorter alternate route that bypasses N entirely. These are secondary
    detours, not the main sequence, so :func:`_drop_bypassed_branches` pushes
    them off the primary flow line.
    """
    out_adj, in_adj = _sibling_adjacency(ranked, edges, parent_by_id)
    direct_edges = {(s, t) for s, targets in out_adj.items() for t in targets}

    branch_ids: set[str] = set()
    for rank in ranked:
        for node in rank:
            preds = in_adj.get(node.id, [])
            succs = out_adj.get(node.id, [])
            if len(preds) != 1 or len(succs) != 1:
                continue
            if (preds[0], succs[0]) in direct_edges:
                branch_ids.add(node.id)
    return branch_ids


def _drop_bypassed_branches(
    ranked: list[list[Node]],
    branch_ids: set[str],
    *,
    gap: float,
    horizontal: bool,
) -> None:
    """Push each bypassed branch beyond its rank's occupied cross-axis extent.

    A rank can already contain several siblings. Moving a detour by exactly one
    child slot would put it on top of the next sibling, so each detour is placed
    after every currently occupied slot instead.
    """
    for rank in ranked:
        for node in rank:
            if node.id not in branch_ids:
                continue
            if horizontal:
                occupied_bottom = max(
                    (
                        sibling.y + sibling.height
                        for sibling in rank
                        if sibling is not node
                    ),
                    default=node.y,
                )
                node.y = max(node.y + node.height + gap, occupied_bottom + gap)
            else:
                occupied_right = max(
                    (
                        sibling.x + sibling.width
                        for sibling in rank
                        if sibling is not node
                    ),
                    default=node.x,
                )
                node.x = max(node.x + node.width + gap, occupied_right + gap)


def _align_singleton_rank_chains(
    ranked: list[list[Node]],
    edges: list[Edge],
    parent_by_id: dict[str, str],
    *,
    top: float,
    left: float,
    horizontal: bool,
) -> None:
    """Straighten a simple 1:1 chain onto a shared centerline.

    ``_position_children_lr``/``_position_children_tb`` pack each rank from a
    shared top/left edge, so single-occupant ranks of DIFFERENT sizes in the
    same chain -- e.g. an 80px task next to a 50px gateway, or a 50px start
    event ahead of both -- end up centered on different lines even though the
    edges between them should read as one straight line.

    Every node in a maximal chain is centered on ``top/left + (the chain's
    own tallest/widest member) / 2`` -- computed from the WHOLE chain, not
    pairwise from each node's immediate predecessor, so a short node at the
    very front (e.g. a start event) doesn't clamp a taller node behind it
    into a merely "as close as allowed" position instead of true center. This
    also guarantees no member ever moves above/left of ``top``/``left``: the
    tallest member sits exactly there, and no other member is taller than it.
    Only single-occupant ranks are touched: repositioning a rank with
    siblings risks overlapping one of them, which needs real reflow, not a
    nudge (out of scope here).
    """
    next_of, prev_of, singleton_ids = _singleton_chain_links(
        ranked, edges, parent_by_id
    )
    node_by_id = {n.id: n for rank in ranked for n in rank}

    visited: set[str] = set()
    for start_id in singleton_ids:
        if start_id in visited or start_id in prev_of:
            continue  # not a chain head (or already handled via another head)
        chain = [start_id]
        visited.add(start_id)
        current = start_id
        while current in next_of and next_of[current] not in visited:
            current = next_of[current]
            chain.append(current)
            visited.add(current)
        if len(chain) < 2:
            continue
        extent = max(
            (node_by_id[nid].height if horizontal else node_by_id[nid].width)
            for nid in chain
        )
        center = (top if horizontal else left) + extent / 2
        for nid in chain:
            node = node_by_id[nid]
            if horizontal:
                node.y = center - node.height / 2
            else:
                node.x = center - node.width / 2


def _grow_parent_to_fit_children(
    parent: Node,
    children: list[Node],
    *,
    right_pad: float,
    bottom_pad: float,
) -> None:
    max_right = max((child.x + child.width for child in children), default=0.0)
    max_bottom = max((child.y + child.height for child in children), default=0.0)
    width = max(parent.width, max_right + right_pad)
    height = max(parent.height, max_bottom + bottom_pad)

    # A container whose OWN title needs a minimum span (e.g. a swimlane with a
    # rotated header, sized in convert.py's ``_annotate_rotated_label_sizing``
    # from the label's rendered length) must not shrink back below it just
    # because its children happen to need less room -- otherwise draw.io wraps
    # the title into overlapping sub-lines within the header band's thin
    # ``startSize``. Generic ``extra`` keys keep this notation-agnostic.
    min_width = parent.extra.get("min_width")
    if min_width is not None:
        width = max(width, float(min_width))
    min_height = parent.extra.get("min_height")
    if min_height is not None:
        height = max(height, float(min_height))

    parent.width = width
    parent.height = height


# Row text padding (item spacingLeft 4 + spacingRight 4) and a cushion so text
# rendered at draw.io's default font never wraps against our 11px width estimate.
_STACK_TEXT_PADDING = 8.0
_STACK_WIDTH_CUSHION = 1.15


def _stack_children(parent: Node, children: list[Node]) -> None:
    """Lay children out as a tight vertical stack, matching draw.io's
    ``childLayout=stackLayout`` (e.g. UML class member rows, or a BPMN Pool
    stacking Lanes).

    Children fill the parent width and stack directly below the title band
    (``start_size``) with no gaps; the parent is widened to fit the longest row
    (and the title) so nothing wraps, and sized to the exact stacked height.
    This makes our geometry identical to what draw.io computes on load, so its
    stack re-layout is a no-op instead of shrinking/reflowing the shape.

    A row's own text sets the floor for a plain leaf row (a UML member), but a
    child that is ITSELF a container (a Lane full of tasks) already had its
    width grown to fit its own content by the recursive descent that ran
    before this parent's turn (:func:`_layout_container_tree` lays out
    children before their parent) -- ``child.width`` already reflects that,
    and is almost always far wider than the child's own short label. Sizing
    purely from label text here would silently shrink it back down, clipping
    everything the child actually contains.
    """
    start = float(parent.extra.get("start_size", 0))

    needed_width = parent.width
    if parent.label:
        title = estimate_text_width(parent.label, bold=True) + _STACK_TEXT_PADDING
        needed_width = max(needed_width, title)
    for child in children:
        row = estimate_text_width(child.label) * _STACK_WIDTH_CUSHION
        needed_width = max(needed_width, row + _STACK_TEXT_PADDING, child.width)
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
    shared_rank_plan: _SharedRankPlan | None = None,
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
    top = float(parent.extra.get("start_size", 0)) + top_pad
    horizontal = options.direction != "TB"

    if shared_rank_plan is not None:
        # This container is one of ≥2 container-siblings (e.g. a Lane among
        # a Pool's Lanes) whose combined children were already ranked
        # together one level up, so a flow crossing between siblings still
        # lines up on the primary axis -- see _shared_rank_plan.
        ranked = _position_children_by_shared_plan(
            children,
            shared_rank_plan,
            top=top,
            left=left_pad,
            child_gap=child_gap,
            horizontal=horizontal,
        )
        _align_singleton_rank_chains(
            ranked, edges, parent_by_id, top=top, left=left_pad, horizontal=horizontal
        )
        branch_ids = _bypassed_branch_ids(ranked, edges, parent_by_id)
        _drop_bypassed_branches(
            ranked, branch_ids, gap=child_gap, horizontal=horizontal
        )
    else:
        ranked = _rank_sibling_nodes(children, edges, parent_by_id)
        # A "degenerate" ranking has no rank wider than one node — a dependency
        # chain (or a cycle that defeats ranking), so the primary axis alone
        # would produce a long thin strip (N columns in LR, N rows in TB).
        # Grid-pack those to use both axes. A ranking *with* parallelism (some
        # rank has ≥2 nodes) keeps the primary flow: siblings spread on the
        # secondary axis, ranks advance on the primary — i.e. primary TB ⇒
        # secondary LR, and vice versa.
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
            _align_singleton_rank_chains(
                ranked,
                edges,
                parent_by_id,
                top=top,
                left=left_pad,
                horizontal=horizontal,
            )
            branch_ids = _bypassed_branch_ids(ranked, edges, parent_by_id)
            _drop_bypassed_branches(
                ranked, branch_ids, gap=child_gap, horizontal=horizontal
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
    shared_rank_plan: _SharedRankPlan | None = None,
) -> None:
    children = by_parent.get(parent_id, [])

    # If ≥2 of this parent's own children are THEMSELVES containers (e.g. two
    # Lanes under a Pool), their grandchildren need ONE shared rank/offset
    # scale -- otherwise each child container ranks and positions its content
    # in total isolation, and a flow crossing from one to the other lands on
    # an arbitrary, independently-chosen column (see _shared_rank_plan).
    container_children = [c for c in children if c.id in by_parent]
    grandchildren_plan = None
    if len(container_children) >= 2:
        combined_grandchildren = [
            grandchild
            for container_child in container_children
            for grandchild in by_parent.get(container_child.id, [])
        ]
        grandchildren_plan = _shared_rank_plan(
            combined_grandchildren, edges, parent_by_id, options
        )

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
                shared_rank_plan=grandchildren_plan,
            )

    if children:
        _layout_container_children(
            node_by_id[parent_id],
            children,
            edges,
            parent_by_id,
            size_of,
            options,
            shared_rank_plan=shared_rank_plan,
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

    Any node with children from ``parent_id``, ``Node.contains``, or
    ``extra["contains"]`` behaves
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


def _topological_ranks(
    sibling_ids: set[str],
    edges: list[Edge],
    parent_by_id: dict[str, str],
    order: dict[str, int],
) -> dict[str, int] | None:
    """Longest-path rank for each id in *sibling_ids*.

    An edge endpoint outside the set collapses to its nearest ancestor that
    IS in the set (:func:`_sibling_owner`), so a deeply-nested descendant's
    edge still counts at its owning sibling's level. ``None`` if a cycle
    among the siblings prevents every id from being ranked (caller decides
    the fallback).
    """
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

    # deque: a BFS frontier, where list.pop(0) is O(n) per step.
    queue = deque(
        sorted(
            (nid for nid in sibling_ids if indegree[nid] == 0),
            key=order.__getitem__,
        )
    )
    ranks: dict[str, int] = {nid: 0 for nid in sibling_ids}
    visited: set[str] = set()

    while queue:
        nid = queue.popleft()
        visited.add(nid)
        for target in sorted(outgoing[nid], key=order.__getitem__):
            ranks[target] = max(ranks[target], ranks[nid] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    if len(visited) != len(sibling_ids):
        return None
    return ranks


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

    ranks = _topological_ranks(sibling_ids, edges, parent_by_id, order)
    if ranks is None:
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


def _shared_rank_plan(
    nodes: list[Node],
    edges: list[Edge],
    parent_by_id: dict[str, str],
    options: _ContainerLayoutOptions,
) -> _SharedRankPlan | None:
    """A cross-container rank/offset scale for *nodes* -- the COMBINED
    children of every container-sibling under one parent (e.g. every Lane's
    tasks under a Pool). ``None`` if there's nothing to rank, or a cycle
    spans the combined set (falls back to each container ranking its own
    content in isolation, same as before this existed)."""
    if not nodes:
        return None
    order = {n.id: idx for idx, n in enumerate(nodes)}
    sibling_ids = set(order)
    ranks = _topological_ranks(sibling_ids, edges, parent_by_id, order)
    if ranks is None:
        return None

    horizontal = options.direction != "TB"
    by_rank: dict[int, list[Node]] = defaultdict(list)
    for node in nodes:
        by_rank[ranks[node.id]].append(node)

    offsets: dict[int, float] = {}
    cursor = 0.0
    for rank in sorted(by_rank):
        offsets[rank] = cursor
        widest = max(
            (node.width if horizontal else node.height) for node in by_rank[rank]
        )
        cursor += widest + options.rank_gap
    return _SharedRankPlan(ranks=ranks, offsets=offsets)


def _position_children_by_shared_plan(
    children: list[Node],
    plan: _SharedRankPlan,
    *,
    top: float,
    left: float,
    child_gap: float,
    horizontal: bool,
) -> list[list[Node]]:
    """Position *children* (one container-sibling's share of a combined,
    cross-container ranking) using the plan's SHARED primary-axis offsets, so
    same-rank content lines up across siblings. The cross axis still stacks
    locally: only what THIS container actually has at a given rank affects
    its own cross-axis packing.

    Returns the rank groups (ascending), in the same shape
    :func:`_rank_sibling_nodes` returns, so downstream callers (the
    singleton-chain aligner, the degenerate/grid check) don't need to know
    which ranking strategy produced them.
    """
    by_rank: dict[int, list[Node]] = defaultdict(list)
    for child in children:
        by_rank[plan.ranks.get(child.id, 0)].append(child)

    ranked: list[list[Node]] = []
    for rank in sorted(by_rank):
        group = by_rank[rank]
        primary = (left if horizontal else top) + plan.offsets.get(rank, 0.0)
        cursor = top if horizontal else left
        for child in group:
            if horizontal:
                child.x = primary
                child.y = cursor
                cursor += child.height + child_gap
            else:
                child.y = primary
                child.x = cursor
                cursor += child.width + child_gap
        ranked.append(group)
    return ranked


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
