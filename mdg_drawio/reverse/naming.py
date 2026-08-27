"""Semantic ``.mdg`` node ids for derived cells.

``.mdg`` ``node_id``\\ s are author-chosen and meant to be meaningful, stable
anchors (see ``GRAMMAR.md`` -> "Ids"). For a derived cell we cannot recover the
author's original name, but every registry shape id already follows
``<library>.<function-slug>.v<N>`` (verified: all 756 shapes in the current
palette conform, zero exceptions) -- the middle segment is already a clean,
human-chosen semantic name, so no extra registry lookup is needed.

This assigns ``<base><n>`` per resolved cell (``person1``, ``person2``,
``system1``, ...), counted per base name in document order. The counter is
global across notation libraries because ``.mdg`` node ids themselves are
global: unrelated concepts that happen to share a base still need distinct ids
or the forward pipeline would treat one as a duplicate and drop it.

The mapping is a small, explicit, pure function of the resolved shapes and
their document order -- re-deriving it after a manual rename costs nothing, so
renaming an assigned id later is exactly that: relabelling one entry, not
re-running the derivation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .derive import DocumentResult

_NAME_RE = re.compile(r"^(\D+)(\d+)$")


def semantic_base(shape_id: str) -> str:
    """The semantic slug of a registry shape id, e.g. ``c4.person_ext.v2`` ->
    ``person_ext``. Falls back to the whole id if it doesn't conform."""
    parts = shape_id.split(".")
    return parts[1] if len(parts) >= 2 else shape_id


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
    default) when naming a document from scratch. Node ids are global within a
    document, so every notation sharing a semantic base also shares its counter:
    C4's ``container1`` and General's ``container2`` must remain distinct even
    though the underlying concepts come from different libraries.
    """
    reserved = reserved or {}
    counters: dict[str, int] = {}
    assigned: list[SemanticId] = []
    for cell in result.cells:
        if cell.chosen is None:
            continue
        shape_id = cell.chosen.shape_id
        base = semantic_base(shape_id)
        if base not in counters:
            counters[base] = reserved.get(base, 0)
        counters[base] += 1
        assigned.append(SemanticId(cell.cell_id, f"{base}{counters[base]}", base))
    return assigned
