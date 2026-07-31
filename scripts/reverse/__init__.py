"""Reverse derivation: draw.io cell -> registry shape (POC).

The forward pipeline binds a registry shape to its palette cell with an EXACT
style fingerprint (:mod:`mdg_drawio.notation._core.normalize`). Going the other
way -- taking a diagram a user drew directly in the draw.io UI and deriving
which registry shape each cell came from -- an exact hash is too brittle: a
recoloured or re-fonted cell would match nothing.

This package scores instead of hashing, in two layers:

* :mod:`scripts.reverse.scoring` -- weighted token similarity. Shape-defining
  tokens dominate; cosmetic tokens (colour, font, alignment) carry a small
  weight, so a cosmetic edit barely moves the score yet still breaks ties
  between otherwise-identical shapes (e.g. C4 Person vs Person_Ext).
* :mod:`scripts.reverse.derive` -- document-level ranking. Unambiguous cells
  vote for their library; a small version-recency prior defaults a lone
  ambiguous shape to the newest version (a solitary UML lifeline -> uml25),
  while any anchor from an older version still wins (lifeline + a uml-only
  shape -> uml).
* :mod:`scripts.reverse.naming` -- assigns each resolved cell a semantic
  ``.mdg`` node id (``person1``, ``system1``, ...) from its shape's registry
  slug, so a derived diagram's ids read like a hand-authored one.

The raw palette styles live in the git-ignored ``generated_data`` (draw.io is
copyright), so this package -- and its tests -- require ``make build-data``.
"""
from __future__ import annotations

from scripts.reverse.derive import (
    Candidate,
    CellResult,
    DocumentResult,
    derive,
    load_cells,
)
from scripts.reverse.naming import SemanticId, assign_semantic_ids, semantic_base
from scripts.reverse.scoring import DEFAULT_WEIGHTS, Weights, parse_style, similarity
from scripts.reverse.style_index import ShapeEntry, StyleIndex

__all__ = [
    "Candidate",
    "CellResult",
    "DocumentResult",
    "derive",
    "load_cells",
    "SemanticId",
    "assign_semantic_ids",
    "semantic_base",
    "DEFAULT_WEIGHTS",
    "Weights",
    "parse_style",
    "similarity",
    "ShapeEntry",
    "StyleIndex",
]
