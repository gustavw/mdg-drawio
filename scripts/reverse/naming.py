"""Semantic ``.mdg`` node ids for derived cells.

``.mdg`` ``node_id``\\ s are author-chosen and meant to be meaningful, stable
anchors (see ``GRAMMAR.md`` -> "Ids"). For a derived cell we cannot recover the
author's original name, but every registry shape id already follows
``<library>.<function-slug>.v<N>`` (verified: all 756 shapes in the current
palette conform, zero exceptions) -- the middle segment is already a clean,
human-chosen semantic name, so no extra registry lookup is needed.

This assigns ``<base><n>`` per resolved cell (``person1``, ``person2``,
``system1``, ...), counted per base name in document order. Genuinely
versioned variants of ONE concept (an actor, matched against ``uml`` or
``uml25`` -- see ``style_index.VERSION_RANK``) intentionally still share one
counter, matching how a human would name the same real-world thing regardless
of which palette version drew it. But the base alone is not enough to scope
a counter: unrelated libraries can share a base string for unrelated concepts
(e.g. C4's ``Container`` -- an application/service -- and a generic draw.io
``swimlane`` grouping box both slug to ``"container"``), so counters are
actually scoped per (version-family, base) pair -- see :func:`_counter_scope`.

The mapping is a small, explicit, pure function of the resolved shapes and
their document order -- re-deriving it after a manual rename costs nothing, so
renaming an assigned id later is exactly that: relabelling one entry, not
re-running the derivation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.reverse.derive import DocumentResult
from scripts.reverse.style_index import VERSION_RANK

_NAME_RE = re.compile(r"^(\D+)(\d+)$")


def semantic_base(shape_id: str) -> str:
    """The semantic slug of a registry shape id, e.g. ``c4.person_ext.v2`` ->
    ``person_ext``. Falls back to the whole id if it doesn't conform."""
    parts = shape_id.split(".")
    return parts[1] if len(parts) >= 2 else shape_id


def _family_of(library: str) -> str:
    """Collapse versioned-variant libraries into one shared scope key; every
    other library is its own scope. ``VERSION_RANK`` today names exactly one
    family (uml/uml25) -- if it ever grows to cover more than one *distinct*
    family, this must group by family, not flatten every entry into one
    bucket."""
    return "|".join(sorted(VERSION_RANK)) if library in VERSION_RANK else library


def _counter_scope(shape_id: str) -> str:
    """The counter-collision scope for a shape id: unrelated concepts that
    happen to share a base string across different libraries (e.g. C4's
    Container vs. a generic draw.io swimlane, both based "container") get
    independent counters, while genuinely versioned variants of one concept
    (uml/uml25) still share one -- see :func:`_family_of`."""
    library = shape_id.split(".")[0]
    return f"{_family_of(library)}.{semantic_base(shape_id)}"


def reserved_counters(existing_node_ids: set[str]) -> dict[str, int]:
    """The highest ``<n>`` already used per ``<base>`` among existing ids.

    For merging new cells into an existing ``.mdg``: seeds
    :func:`assign_semantic_ids`'s counters so a freshly-derived cell never
    collides with a name already in the file (e.g. ``person1`` already
    present -> the next new Person becomes ``person2``, not ``person1``
    again). An existing id that doesn't follow the ``<base><n>`` convention
    (hand-written, e.g. ``sys1`` typed by a human, or a GUID) simply doesn't
    match and is ignored -- it occupies a different namespace, so there is no
    collision to guard against.
    """
    reserved: dict[str, int] = {}
    for node_id in existing_node_ids:
        match = _NAME_RE.match(node_id)
        if match:
            base, n = match.group(1), int(match.group(2))
            reserved[base] = max(reserved.get(base, 0), n)
    return reserved


@dataclass(frozen=True)
class SemanticId:
    """One resolved cell's generated node id, traceable back to its source."""

    cell_id: str
    node_id: str
    base: str


def assign_semantic_ids(
    result: DocumentResult, reserved: dict[str, int] | None = None
) -> list[SemanticId]:
    """Assign ``<base><n>`` ids to every resolved cell, in document order.

    Unresolved cells (:attr:`CellResult.chosen` is ``None``) are skipped -- an
    unidentified shape has no semantic name to derive. ``reserved`` (see
    :func:`reserved_counters`) seeds each base's counter above whatever is
    already used in an existing document being merged into; omit it (the
    default) when naming a document from scratch. ``reserved`` is keyed by
    base alone (an existing ``.mdg`` id carries no library information), so
    it seeds every :func:`_counter_scope` sharing that base -- conservative,
    since we can't know which scope originally produced an existing name.
    """
    reserved = reserved or {}
    counters: dict[str, int] = {}
    assigned: list[SemanticId] = []
    for cell in result.cells:
        if cell.chosen is None:
            continue
        shape_id = cell.chosen.shape_id
        base = semantic_base(shape_id)
        scope = _counter_scope(shape_id)
        if scope not in counters:
            counters[scope] = reserved.get(base, 0)
        counters[scope] += 1
        assigned.append(SemanticId(cell.cell_id, f"{base}{counters[scope]}", base))
    return assigned
