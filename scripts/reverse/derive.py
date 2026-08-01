"""Document-level reverse derivation.

Parse a ``.drawio`` file into cells, score each cell against the style index,
then resolve ambiguity with library voting + a version-recency prior:

1. Each cell's near-top candidates (within ``band`` of its best similarity)
   form its candidate set. A cell whose whole candidate set sits in one library
   is an *anchor* and casts a vote for that library.
2. ``library_score = anchor_votes + recency_prior``.
3. Each cell resolves to the highest-scoring library among its candidates, then
   to the best-scoring shape within that library.

So a lone UML lifeline (ambiguous uml/uml25, no anchors) falls to the recency
prior -> uml25; add any uml-only shape and its anchor vote pulls the lifeline
to uml.
"""
from __future__ import annotations

import base64
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from scripts.reverse.scoring import DEFAULT_WEIGHTS, Weights, parse_style
from scripts.reverse.style_index import StyleIndex, recency_prior

# How close to the top similarity a candidate must be to count as a near-tie.
DEFAULT_BAND = 0.02
# Below this similarity a cell has no plausible match at all.
DEFAULT_SIM_FLOOR = 0.4

# Confidence multipliers by how the cell was resolved (times the similarity).
_CONFIDENCE: dict[str, float] = {
    "unique": 1.0,
    "single-library": 0.85,
    "library-vote": 0.7,
    "recency-prior": 0.45,
    "none": 0.0,
}


@dataclass(frozen=True)
class Cell:
    """A vertex/edge parsed out of a draw.io file."""

    cell_id: str
    style: str
    value: str
    tokens: dict[str, object] = field(compare=False)


@dataclass(frozen=True)
class Candidate:
    shape_id: str
    library: str
    sim: float


@dataclass
class CellResult:
    cell_id: str
    style: str
    candidates: list[Candidate]
    chosen: Candidate | None
    resolved_by: str
    confidence: float


@dataclass
class DocumentResult:
    cells: list[CellResult]
    library_scores: dict[str, float]
    anchor_votes: dict[str, float]


# ── parsing ──────────────────────────────────────────────────────────────────
def _decompress(text: str) -> str | None:
    """Inflate draw.io's base64 + raw-deflate + url-encoded diagram payload."""
    try:
        raw = zlib.decompress(base64.b64decode(text), -15)
        return unquote(raw.decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError):
        return None


def _model_roots(root: ET.Element) -> list[ET.Element]:
    """Yield every ``<mxGraphModel>`` in the file, inflating compressed ones."""
    if root.tag == "mxGraphModel":
        return [root]
    models: list[ET.Element] = []
    for diagram in root.iter("diagram"):
        inner = diagram.find("mxGraphModel")
        if inner is not None:
            models.append(inner)
        elif diagram.text and diagram.text.strip():
            xml = _decompress(diagram.text.strip())
            if xml:
                models.append(ET.fromstring(xml))
    return models


def _cell_elements(model: ET.Element) -> list[ET.Element]:
    """The style-bearing ``mxCell`` elements, unwrapping ``<object>`` cells.

    drawio encodes an "object" cell's id on the wrapping ``<object>`` element,
    not on its inner ``<mxCell>`` (see ``cellsToXml`` in extract_shapes.js) --
    copy it down so every returned element carries its real id. Without this,
    every object-wrapped cell's inner element reads as id "", so a document
    with more than one (e.g. two C4 Person nodes) collapses to a single cell
    via the id-based dedup in :func:`load_cells`.
    """
    cells: list[ET.Element] = []
    wrapped: set[int] = set()
    for obj in model.iter("object"):
        inner = obj.find("mxCell")
        if inner is None:
            continue
        obj_id = obj.get("id")
        if inner.get("id") is None and obj_id is not None:
            inner.set("id", obj_id)
        cells.append(inner)
        wrapped.add(id(inner))
    for cell in model.iter("mxCell"):
        if id(cell) not in wrapped:
            cells.append(cell)
    return cells


def _parse_root(source: str) -> ET.Element:
    """Parse a ``.drawio`` file path or raw XML string into its root element."""
    text = source
    if not source.lstrip().startswith("<"):
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    return ET.fromstring(text)


def _page_prefix(page_index: int, multi_page: bool) -> str:
    """draw.io ids are page-scoped (each page independently numbers its own
    cells), so the SAME raw id commonly recurs across pages. A single-page
    document's ids are left bare (matching the overwhelmingly common case and
    every existing single-page caller); a multi-page document gets every id
    (and every parent reference to it, see :func:`parent_map`) prefixed with
    its page index so cross-page collisions can never silently merge."""
    return f"{page_index}:" if multi_page else ""


def load_cells(source: str) -> list[Cell]:
    """Parse a ``.drawio`` file path or raw XML string into cells."""
    root = _parse_root(source)
    models = _model_roots(root)
    multi_page = len(models) > 1
    seen: set[str] = set()
    cells: list[Cell] = []
    for page_index, model in enumerate(models):
        prefix = _page_prefix(page_index, multi_page)
        for element in _cell_elements(model):
            style = element.get("style") or ""
            cell_id = prefix + (element.get("id") or "")
            if not style.strip() or cell_id in seen:
                continue
            seen.add(cell_id)
            cells.append(
                Cell(
                    cell_id=cell_id,
                    style=style,
                    value=element.get("value") or "",
                    tokens=parse_style(style),
                )
            )
    return cells


@dataclass(frozen=True)
class RawCell:
    """A cell's raw containment context: its parent id and own style, from an
    UNFILTERED pass over every cell in the document -- including the styleless
    "layer" cells and Ctrl+G "group" cells that :func:`load_cells` excludes,
    since a resolved cell's containment ancestry commonly passes through them.
    """

    parent_id: str | None
    style: str
    is_edge: bool


def parent_map(source: str) -> dict[str, RawCell]:
    """id -> raw containment context, for every cell in the document.

    Uses the same page-prefixing convention as :func:`load_cells` (applied to
    both a cell's own id and its parent reference), so ids line up between the
    two when walking a resolved cell's containment ancestry.
    """
    root = _parse_root(source)
    models = _model_roots(root)
    multi_page = len(models) > 1
    mapping: dict[str, RawCell] = {}
    for page_index, model in enumerate(models):
        prefix = _page_prefix(page_index, multi_page)
        for element in _cell_elements(model):
            raw_id = element.get("id")
            if not raw_id:
                continue
            cell_id = prefix + raw_id
            if cell_id in mapping:
                continue
            raw_parent = element.get("parent")
            mapping[cell_id] = RawCell(
                parent_id=(prefix + raw_parent) if raw_parent else None,
                style=element.get("style") or "",
                is_edge=element.get("edge") == "1",
            )
    return mapping


# ── resolution ─────────────────────────────────────────────────────────────--
def _near_candidates(
    cell: Cell, index: StyleIndex, weights: Weights, band: float, floor: float
) -> list[Candidate]:
    """The cell's candidates within ``band`` of its top similarity."""
    scored = index.score_all(cell.tokens, weights)
    if not scored or scored[0].sim < floor:
        return []
    top = scored[0].sim
    return [
        Candidate(s.entry.shape_id, s.entry.library, s.sim)
        for s in scored
        if s.sim >= top - band
    ]


def _library_scores(
    per_cell: list[tuple[Cell, list[Candidate]]], libraries: set[str]
) -> tuple[dict[str, float], dict[str, float]]:
    """Anchor votes (single-library cells) plus the recency prior per library."""
    anchor_votes: dict[str, float] = defaultdict(float)
    for _cell, near in per_cell:
        libs = {c.library for c in near}
        if len(libs) == 1 and near:
            anchor_votes[next(iter(libs))] += 1.0
    scores: dict[str, float] = {}
    for lib in libraries | set(anchor_votes):
        scores[lib] = anchor_votes[lib] + recency_prior(lib)
    return dict(anchor_votes), scores


def _resolve_cell(
    near: list[Candidate],
    scores: dict[str, float],
    anchor_votes: dict[str, float],
) -> tuple[Candidate | None, str]:
    """Pick a candidate for one cell and label how it was decided."""
    if not near:
        return None, "none"
    libs = {c.library for c in near}
    if len(libs) == 1:
        chosen = max(near, key=lambda c: c.sim)
        label = "unique" if len({c.shape_id for c in near}) == 1 else "single-library"
        return chosen, label
    best_lib = max(libs, key=lambda lib: (scores[lib], recency_prior(lib)))
    in_lib = [c for c in near if c.library == best_lib]
    chosen = max(in_lib, key=lambda c: c.sim)
    decided = "library-vote" if anchor_votes.get(best_lib, 0.0) > 0 else "recency-prior"
    return chosen, decided


def derive(
    cells: list[Cell],
    index: StyleIndex,
    weights: Weights = DEFAULT_WEIGHTS,
    band: float = DEFAULT_BAND,
    sim_floor: float = DEFAULT_SIM_FLOOR,
) -> DocumentResult:
    """Resolve every cell in a document to its most likely registry shape."""
    per_cell = [
        (cell, _near_candidates(cell, index, weights, band, sim_floor))
        for cell in cells
    ]
    anchor_votes, scores = _library_scores(per_cell, index.libraries())
    results: list[CellResult] = []
    for cell, near in per_cell:
        chosen, resolved_by = _resolve_cell(near, scores, anchor_votes)
        sim = chosen.sim if chosen else 0.0
        confidence = round(sim * _CONFIDENCE[resolved_by], 3)
        results.append(
            CellResult(
                cell_id=cell.cell_id,
                style=cell.style,
                candidates=near[:8],
                chosen=chosen,
                resolved_by=resolved_by,
                confidence=confidence,
            )
        )
    return DocumentResult(results, scores, anchor_votes)
