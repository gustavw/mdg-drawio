"""Containment resolution: where a resolved cell nests, and how deep.

C4 is the only notation with a real forward ``.mdg`` parser today, and none of
its shapes declare ``rows.allowed`` (verified against the registry: zero C4
shapes have non-empty ``rows.allowed``) -- every nested child is genuine
containment, never a compartment row. This module targets pure parent/child
containment only. The rows-vs-containment branch ``GRAMMAR.md`` describes is a
pre-existing gap in the forward parser (``dsl_engine.py`` never reads a
shape's ``rows.allowed``/``contains.allowed`` either -- every indented child
becomes a contained node regardless of what the parent declares) -- out of
scope here; revisit if another notation gains a real parser with shapes that
declare rows.

A cell's *legitimate* container is the nearest ancestor, via draw.io's own
``parent=`` chain, whose resolved shape has a non-empty registry
``contains.allowed`` (only ``System_Boundary``/``Container_Boundary`` today,
read from the registry so this tracks future registry changes automatically --
never hardcoded). Anything else encountered while climbing is transparently
skipped, with a warning recorded so a human can review the source file:

* a draw.io "layer" cell (styleless -- organisational, not a shape);
* a Ctrl+G "group" cell (a bare ``group`` style token -- a UI bounding box,
  not a shape);
* an ancestor that did not resolve to any known shape;
* an ancestor that resolved to a shape without ``contains.allowed`` (e.g. a
  cell accidentally nested inside a Person in the source file).

A cycle in the raw ``parent=`` chain (malformed/adversarial input) is detected
and stops the climb with a warning rather than looping.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .derive import Candidate, DocumentResult, RawCell
from .scoring import BARE, parse_style
from .style_index import registry_entry

# Hard ceiling on ancestry-chain length. The per-call `visited` set already
# guarantees termination (a repeat MUST occur within len(raw_cells) steps), so
# this is defense-in-depth against a pathological/adversarial file, not the
# primary cycle guard.
_MAX_CHAIN_STEPS = 200


def _is_container_capable(shape_id: str) -> bool:
    """Whether shape_id's registry entry declares a non-empty contains.allowed.

    Reads the registry directly (never a hardcoded shape list) so this stays
    correct if the registry changes or another notation gains real
    containment. Deliberately uncached: this project's own convention is that
    consumers never hold a module-global cache over registry-derived data
    (the one sanctioned exception is ``registry.py``'s own ``load_registry``,
    which a cache here would silently shadow -- e.g. across a test session's
    ``set_registries`` swap). ``registry_entry`` is already cheap: it is a
    single dict comprehension over the (already-cached) loaded registry.
    """
    entry = registry_entry(shape_id)
    if entry is None:
        return False
    return bool(entry.get("contains", {}).get("allowed"))


def _is_structural_passthrough(style: str) -> bool:
    """A draw.io "layer" (no style) or Ctrl+G "group" cell: structural, not a
    real shape -- skip it silently while climbing the containment chain."""
    if not style.strip():
        return True
    return parse_style(style).get("group") is BARE


@dataclass(frozen=True)
class Containment:
    """One resolved cell's nesting: its nearest legitimate container (by
    semantic node id, or ``None`` if top-level), its depth (count of
    legitimate containers between it and the page root), and any anomalies
    encountered while climbing."""

    cell_id: str
    container_node_id: str | None
    depth: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _climb(
    cell_id: str,
    raw_cells: dict[str, RawCell],
    chosen_by_id: dict[str, Candidate | None],
) -> tuple[list[str], list[str]]:
    """Every legitimate container ancestor's cell_id, nearest first, plus any
    warnings recorded for anomalies skipped along the way."""
    warnings: list[str] = []
    containers: list[str] = []
    visited = {cell_id}
    current = raw_cells.get(cell_id)
    parent_id = current.parent_id if current else None
    steps = 0
    while parent_id is not None and steps < _MAX_CHAIN_STEPS:
        steps += 1
        if parent_id in visited:
            warnings.append(f"cycle detected in parent chain at {parent_id!r}")
            break
        visited.add(parent_id)
        raw_parent = raw_cells.get(parent_id)
        if raw_parent is None:
            break  # reached the page/absolute root, or a dangling reference
        if _is_structural_passthrough(raw_parent.style):
            parent_id = raw_parent.parent_id
            continue
        chosen = chosen_by_id.get(parent_id)
        if chosen is None:
            warnings.append(
                f"ancestor {parent_id!r} did not resolve to a known shape; "
                "skipped for containment"
            )
            parent_id = raw_parent.parent_id
            continue
        if not _is_container_capable(chosen.shape_id):
            warnings.append(
                f"ancestor {parent_id!r} resolved to {chosen.shape_id!r}, "
                "which is not container-capable; skipped for containment"
            )
            parent_id = raw_parent.parent_id
            continue
        containers.append(parent_id)
        parent_id = raw_parent.parent_id
    return containers, warnings


def resolve_containment(
    result: DocumentResult,
    raw_cells: dict[str, RawCell],
    node_ids: dict[str, str],
) -> list[Containment]:
    """Resolve every non-edge resolved cell's nearest legitimate container.

    ``node_ids`` is the ``cell_id -> semantic node_id`` mapping from
    :func:`scripts.reverse.naming.assign_semantic_ids` -- a container's
    reported identity is its semantic id, not its raw draw.io id. Edges are
    skipped entirely: ``.mdg`` declares relationships flat
    (``c4.Rel(a, b, ...)``), so containment is not meaningful for them.
    """
    chosen_by_id = {c.cell_id: c.chosen for c in result.cells}
    out = []
    for cell in result.cells:
        raw = raw_cells.get(cell.cell_id)
        if raw is None or raw.is_edge:
            continue
        containers, warnings = _climb(cell.cell_id, raw_cells, chosen_by_id)
        container_node_id = (
            node_ids.get(containers[0], containers[0]) if containers else None
        )
        out.append(
            Containment(
                cell.cell_id, container_node_id, len(containers), tuple(warnings)
            )
        )
    return out
