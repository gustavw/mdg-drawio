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
* :mod:`scripts.reverse.containment` -- resolves where each cell nests (its
  nearest legitimate container, by the registry's ``contains.allowed``) and
  how deep, climbing draw.io's own parent chain past layers/groups/anomalies.
* :mod:`scripts.reverse.merge` -- splices new cells into an EXISTING ``.mdg``
  file's text, correctly indented and nested, without disturbing anything
  already there. A cell already represented in the file (its draw.io id
  matches a declared node_id) is left alone; genuinely new cells are inserted
  at the right place. Never writes a file itself -- see
  ``scripts/reverse/merge_cli.py`` for the dry-run-by-default,
  validate-before-write CLI.

The raw palette styles live in the git-ignored ``generated_data`` (draw.io is
copyright), so this package -- and its tests -- require ``make build-data``.
"""
from __future__ import annotations

from scripts.reverse.containment import Containment, resolve_containment
from scripts.reverse.derive import (
    Candidate,
    CellResult,
    DocumentResult,
    RawCell,
    derive,
    load_cells,
    parent_map,
)
from scripts.reverse.merge import (
    ExistingIndex,
    Insertion,
    MergePlan,
    NewNode,
    index_existing,
    plan_merge,
    render_merge,
)
from scripts.reverse.merge import validate as validate_mdg
from scripts.reverse.naming import (
    SemanticId,
    assign_semantic_ids,
    reserved_counters,
    semantic_base,
)
from scripts.reverse.scoring import DEFAULT_WEIGHTS, Weights, parse_style, similarity
from scripts.reverse.style_index import ShapeEntry, StyleIndex

__all__ = [
    "Candidate",
    "CellResult",
    "DocumentResult",
    "RawCell",
    "derive",
    "load_cells",
    "parent_map",
    "SemanticId",
    "assign_semantic_ids",
    "reserved_counters",
    "semantic_base",
    "Containment",
    "resolve_containment",
    "ExistingIndex",
    "Insertion",
    "MergePlan",
    "NewNode",
    "index_existing",
    "plan_merge",
    "render_merge",
    "validate_mdg",
    "DEFAULT_WEIGHTS",
    "Weights",
    "parse_style",
    "similarity",
    "ShapeEntry",
    "StyleIndex",
]
