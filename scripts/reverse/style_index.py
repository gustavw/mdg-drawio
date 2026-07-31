"""The reverse index: every registry shape's canonical style, loaded from the
git-ignored ``generated_data`` sidecars, ready to be scored against a query.

Also holds the notation-family / version-recency metadata used to default a
lone ambiguous shape to the newest version of its family.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from scripts.reverse.scoring import DEFAULT_WEIGHTS, Weights, parse_style, similarity

# Notation families that ship in more than one version. The value is the
# version rank (higher = newer). Used only as a small tie-breaking prior, so a
# solitary ambiguous shape defaults to the latest version -- e.g. a lone UML
# lifeline resolves to uml25 rather than uml. Extend as versioned pairs appear.
VERSION_RANK: dict[str, int] = {"uml": 1, "uml25": 2}


def recency_prior(library: str, scale: float = 0.1) -> float:
    """A tiny per-library prior favouring the newest version of a family.

    Scaled well below one anchor vote (weight ``1.0``) so it only ever breaks
    a tie the document evidence left open -- it can never override an anchor.
    """
    return VERSION_RANK.get(library, 0) * scale


@dataclass(frozen=True)
class ShapeEntry:
    """One registry shape's canonical style, parsed and ready to score."""

    shape_id: str
    library: str
    style: str
    fingerprint: str
    tokens: dict[str, object] = field(compare=False)


@dataclass(frozen=True)
class Scored:
    """A shape entry paired with its similarity to a query cell."""

    entry: ShapeEntry
    sim: float


class StyleIndex:
    """All registry shapes, scorable against a query style."""

    def __init__(self, entries: list[ShapeEntry]) -> None:
        self.entries = entries

    @classmethod
    def load(cls, data_dir: Path | None = None) -> StyleIndex:
        """Load every ``<lib>_styles.json`` under ``generated_data/notation``."""
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
                    )
                )
        return cls(entries)

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
