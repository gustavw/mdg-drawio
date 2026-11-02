"""The reverse index: every registry shape's canonical style, loaded from the
git-ignored ``generated_data`` sidecars, ready to be scored against a query.

Also holds the notation-family / version-recency metadata used to default a
lone ambiguous shape to the newest version of its family.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mdg_drawio.notation import load_registry, shapes_by_id

from .scoring import DEFAULT_WEIGHTS, Weights, parse_style, similarity

# Notation families that ship in more than one version. The value is the
# version rank (higher = newer). Used only as a small tie-breaking prior, so a
# solitary ambiguous shape defaults to the latest version -- e.g. a lone UML
# lifeline resolves to uml25 rather than uml. Extend as versioned pairs appear.
VERSION_RANK: dict[str, int] = {"uml": 1, "uml25": 2}


# Scaled well below one anchor vote (weight 1.0) so the recency prior only
# ever breaks a tie the document evidence left open -- it can never override
# an anchor.
_DEFAULT_RECENCY_SCALE = 0.1


def recency_prior(library: str, scale: float = _DEFAULT_RECENCY_SCALE) -> float:
    """A tiny per-library prior favouring the newest version of a family."""
    return VERSION_RANK.get(library, 0) * scale


def registry_entry(shape_id: str) -> dict[str, Any] | None:
    """The registry entry for ``shape_id`` (``<library>.<function>.v<N>``), or
    ``None`` if the id doesn't conform or isn't found. Shared by every
    consumer that needs to look a derived shape id back up in the registry
    (``containment.py``'s ``contains.allowed`` check, ``merge.py``'s
    function/variant lookup), so the id-parsing and not-found handling live
    in exactly one place."""
    parts = shape_id.split(".")
    if len(parts) < 2:
        return None
    try:
        return shapes_by_id(parts[0]).get(shape_id)
    except KeyError:
        return None


# Marks a compartment row type's synthetic shape id (``erd.rowkey.row``). Row
# types (``erd.Row``/``erd.RowKey``, ``uml25.Item``, ...) have no registry
# shape entry of their own -- they only exist nested inside a parent whose
# ``rows.allowed`` names them -- so they need an id that can never collide
# with a real ``<library>.<function>.v<N>`` shape id, while still giving
# :func:`~mdg_drawio.reverse.naming.semantic_base` its usual middle segment.
_ROW_SUFFIX = "row"


def row_shape_id(library: str, name: str) -> str:
    """The synthetic shape id for row type *name* in *library*."""
    return f"{library}.{name.lower()}.{_ROW_SUFFIX}"


def row_type_spec(shape_id: str) -> dict[str, Any] | None:
    """The registry ``row_types`` entry a :func:`row_shape_id` names (its
    ``name`` in the registry's own spelling, plus its ``args``), or ``None``
    if *shape_id* is not a row id or names no registered row type."""
    parts = shape_id.split(".")
    if len(parts) != 3 or parts[2] != _ROW_SUFFIX:
        return None
    try:
        registry = load_registry(parts[0])
    except (KeyError, FileNotFoundError):
        return None
    for spec in registry.get("row_types", []):
        if isinstance(spec, dict) and str(spec.get("name", "")).lower() == parts[1]:
            return spec
    return None


def allowed_rows(shape_id: str) -> list[str]:
    """The row type names *shape_id*'s registry entry accepts as compartment
    rows (``rows.allowed``) -- empty for a shape that takes none."""
    entry = registry_entry(shape_id)
    if entry is None:
        return []
    rows = entry.get("rows") or {}
    return [str(name) for name in rows.get("allowed") or []]


@dataclass(frozen=True)
class RowTypeEntry:
    """One compartment row type's canonical tableRow style, parsed and ready
    to score, plus what reverse needs to read its sub-cells back.

    ``cell_count`` is how many leaf sub-cells the forward generator emits for
    the row (erd's Row/RowKey: two -- a key tag and a text label; see
    ``generator._compound_row_override``); ``0`` for a plain row whose text
    lives on the row cell itself. ``arg_names`` are the registry's declared
    argument names for the row type.
    """

    library: str
    name: str
    tokens: dict[str, object] = field(compare=False)
    cell_count: int = 0
    arg_names: frozenset[str] = frozenset()

    @property
    def shape_id(self) -> str:
        return row_shape_id(self.library, self.name)


def _row_arg_names(library: str, name: str) -> frozenset[str]:
    spec = row_type_spec(row_shape_id(library, name))
    args = spec.get("args", []) if spec else []
    return frozenset(
        str(arg["name"]) for arg in args if isinstance(arg, dict) and "name" in arg
    )


@dataclass(frozen=True)
class ShapeEntry:
    """One registry shape's canonical style, parsed and ready to score."""

    shape_id: str
    library: str
    style: str
    fingerprint: str
    tokens: dict[str, object] = field(compare=False)
    # The registry's ``kind`` ("vertex"/"edge"/"diagram"), defaulting to
    # "vertex" for a sidecar built before this field existed -- keeps a stale
    # (unrebuilt) ``generated_data`` cache from crashing rather than silently
    # matching edge cells, the safer of the two failure directions.
    kind: str = "vertex"


@dataclass(frozen=True)
class Scored:
    """A shape entry paired with its similarity to a query cell."""

    entry: ShapeEntry
    sim: float


class StyleIndex:
    """All registry shapes, scorable against a query style."""

    def __init__(
        self,
        entries: list[ShapeEntry],
        row_types: list[RowTypeEntry] | None = None,
    ) -> None:
        self.entries = entries
        self.row_types = row_types or []

    @classmethod
    def load(cls, data_dir: Path | None = None) -> StyleIndex:
        """Load every ``<lib>_styles.json`` (shapes) and ``<lib>_row_types.json``
        (compartment row types) under ``generated_data/notation``."""
        if data_dir is None:
            from mdg_drawio.notation import DATA_DIR

            data_dir = DATA_DIR
        notation = Path(data_dir) / "notation"
        entries: list[ShapeEntry] = []
        for path in sorted(notation.glob("*_styles.json")):
            library = path.stem.removesuffix("_styles")
            shapes: dict[str, dict[str, object]] = json.loads(
                path.read_text(encoding="utf-8")
            )
            for shape_id, rec in shapes.items():
                style = str(rec.get("style") or "")
                entries.append(
                    ShapeEntry(
                        shape_id=shape_id,
                        library=library,
                        style=style,
                        fingerprint=str(rec.get("fingerprint") or ""),
                        tokens=parse_style(style),
                        kind=str(rec.get("kind") or "vertex"),
                    )
                )
        row_types: list[RowTypeEntry] = []
        for path in sorted(notation.glob("*_row_types.json")):
            library = path.stem.removesuffix("_row_types")
            records: dict[str, dict[str, object]] = json.loads(
                path.read_text(encoding="utf-8")
            )
            for name, rec in records.items():
                cells = rec.get("cells")
                row_types.append(
                    RowTypeEntry(
                        library=library,
                        name=name,
                        tokens=parse_style(str(rec.get("style") or "")),
                        cell_count=len(cells) if isinstance(cells, list) else 0,
                        arg_names=_row_arg_names(library, name),
                    )
                )
        return cls(entries, row_types)

    def libraries(self) -> set[str]:
        return {e.library for e in self.entries}

    def score_all(
        self,
        query_tokens: dict[str, object],
        weights: Weights = DEFAULT_WEIGHTS,
    ) -> list[Scored]:
        """Every entry scored against the query, best first."""
        scored = [
            Scored(e, similarity(query_tokens, e.tokens, weights))
            for e in self.entries
        ]
        scored.sort(key=lambda s: s.sim, reverse=True)
        return scored
