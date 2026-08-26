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
from itertools import chain
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .scoring import DEFAULT_WEIGHTS, Weights, parse_style
from .style_index import StyleIndex, recency_prior

# How close to the top similarity a candidate must be to count as a near-tie.
DEFAULT_BAND = 0.02

# draw.io wraps a cell carrying extra (non-style) attributes -- a C4 cell's
# c4Name/c4Type, a BPMN task's label -- in one of two tags depending on the
# app version that saved the file: the legacy ``<object>``, or the current
# ``<UserObject>`` (also what this project's own generator emits, see
# mdg_drawio/generator/generator.py's _wrap_object). Both carry the cell's
# real id on the wrapper, not its inner <mxCell>, so both must be recognized
# or a hand-edited file re-saved by a current draw.io (or produced by this
# tool's own forward pipeline) silently loses every wrapped cell.
_OBJECT_TAGS = ("object", "UserObject")


def _iter_object_cells(model: ET.Element) -> list[ET.Element]:
    return list(chain.from_iterable(model.iter(tag) for tag in _OBJECT_TAGS))
# Below this similarity a cell has no plausible match at all.
DEFAULT_SIM_FLOOR = 0.4

# Confidence multipliers by how the cell was resolved (times the similarity).
_CONFIDENCE_UNIQUE = 1.0
_CONFIDENCE_SINGLE_LIBRARY = 0.85
_CONFIDENCE_LIBRARY_VOTE = 0.7
_CONFIDENCE_RECENCY_PRIOR = 0.45
_CONFIDENCE_NONE = 0.0
_CONFIDENCE: dict[str, float] = {
    "unique": _CONFIDENCE_UNIQUE,
    "single-library": _CONFIDENCE_SINGLE_LIBRARY,
    "library-vote": _CONFIDENCE_LIBRARY_VOTE,
    "recency-prior": _CONFIDENCE_RECENCY_PRIOR,
    "none": _CONFIDENCE_NONE,
}


@dataclass(frozen=True)
class Cell:
    """A vertex/edge parsed out of a draw.io file.

    ``object_attrs`` holds an object-wrapped cell's own attributes (its id,
    plus custom fields like C4's ``c4Name``/``c4Type``/``c4Description``): a
    C4 object cell's plain ``value`` is an unsubstituted ``%c4Name%``-style
    template, so the user-typed label lives here instead.
    """

    cell_id: str
    style: str
    value: str
    tokens: dict[str, object] = field(compare=False)
    object_attrs: dict[str, str] = field(default_factory=dict, compare=False)
    # From the source ``mxCell``'s own ``edge="1"`` attribute -- ground truth
    # for whether this cell IS an edge, independent of what it scores against.
    # Used to keep an edge cell from ever resolving to a vertex-kind shape (or
    # vice versa): see the kind filter in :func:`_near_candidates`.
    is_edge: bool = False


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

# Passed to zlib as negative wbits, telling it to expect a raw deflate stream
# (no zlib/gzip header) -- draw.io's own compression format for a <diagram>
# payload.
_RAW_DEFLATE_WBITS = 15


def _decompress(text: str) -> str | None:
    """Inflate draw.io's base64 + raw-deflate + url-encoded diagram payload."""
    try:
        raw = zlib.decompress(base64.b64decode(text), -_RAW_DEFLATE_WBITS)
        return unquote(raw.decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError):
        return None


def _page_models(root: ET.Element) -> list[tuple[ET.Element | None, ET.Element]]:
    """Every page's ``(owning <diagram> element, <mxGraphModel>)``, inflating
    compressed diagrams. The owning element is ``None`` for a bare
    ``<mxGraphModel>``-rooted document (no ``<diagram>``/``<mxfile>``
    wrapper) -- :func:`rewrite_cell_ids` needs it to splice a rewritten
    compressed page back in; :func:`_model_roots` just drops it.
    """
    if root.tag == "mxGraphModel":
        return [(None, root)]
    pairs: list[tuple[ET.Element | None, ET.Element]] = []
    for diagram in root.iter("diagram"):
        inner = diagram.find("mxGraphModel")
        if inner is not None:
            pairs.append((diagram, inner))
        elif diagram.text and diagram.text.strip():
            xml = _decompress(diagram.text.strip())
            if xml:
                pairs.append((diagram, ET.fromstring(xml)))
    return pairs


def _model_roots(root: ET.Element) -> list[ET.Element]:
    """Every ``<mxGraphModel>`` in the file, inflating compressed ones."""
    return [model for _diagram, model in _page_models(root)]


def _cell_elements(model: ET.Element) -> list[ET.Element]:
    """The style-bearing ``mxCell`` elements, unwrapping ``<object>``/
    ``<UserObject>`` cells.

    drawio encodes such a cell's id on the wrapping element, not on its inner
    ``<mxCell>`` (see ``cellsToXml`` in extract_shapes.js) -- copy it down so
    every returned element carries its real id. Without this, every wrapped
    cell's inner element reads as id "", so a document with more than one
    (e.g. two C4 Person nodes) collapses to a single cell via the id-based
    dedup in :func:`load_cells`.
    """
    cells: list[ET.Element] = []
    wrapped: set[int] = set()
    for obj in _iter_object_cells(model):
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


def _page_id_map(
    model: ET.Element, prefix: str, renames: dict[str, str]
) -> dict[str, str]:
    """This page's slice of *renames* (page-prefixed cell_id -> new id),
    reduced to bare (unprefixed) raw ids -- draw.io attributes are always
    page-local, never carrying the prefix :func:`load_cells`/:func:`parent_map`
    add for their own cross-page bookkeeping."""
    id_map: dict[str, str] = {}
    for element in _cell_elements(model):
        raw_id = element.get("id")
        if raw_id is None:
            continue
        new_id = renames.get(prefix + raw_id)
        if new_id is not None:
            id_map[raw_id] = new_id
    return id_map


def _rename_page_ids(model: ET.Element, id_map: dict[str, str]) -> None:
    """Rename every ``id`` in *id_map*, fixing up every ``parent``/``source``/
    ``target`` reference to it -- covers both an unwrapped ``mxCell`` and an
    ``<object>``/``<UserObject>`` wrapper (and, harmlessly, an inner ``mxCell``
    that independently repeats the wrapper's id)."""
    for el in model.iter():
        old_id = el.get("id")
        if old_id is not None and old_id in id_map:
            el.set("id", id_map[old_id])
        for attr in ("parent", "source", "target"):
            old_ref = el.get(attr)
            if old_ref is not None and old_ref in id_map:
                el.set(attr, id_map[old_ref])


def rewrite_cell_ids(source: str, renames: dict[str, str]) -> str | None:
    """Rename draw.io cell ids per *renames* (page-prefixed cell_id -> new
    id, the same convention as :attr:`Cell.cell_id`), fixing up every
    parent/source/target reference along the way. Returns the rewritten XML
    text, or ``None`` if nothing in *renames* matched any cell actually in
    the document (including when *renames* itself is empty).

    Keeps a newly ``mdg sync``'d cell's ``.drawio`` id in step with the
    fresh semantic id ``sync`` just minted for it in the ``.mdg`` --
    otherwise a later plain regenerate's geometry overlay (which matches a
    node by id) can never find that cell again, and whatever manual layout
    it had is silently discarded the moment ``sync`` runs, not preserved.

    A page stored as compressed ``<diagram>`` text is rewritten as inline
    ``<mxGraphModel>`` XML instead of being re-compressed -- draw.io reads
    both forms interchangeably, and re-implementing draw.io's own
    compression format against a live file, with no round-trip safety net
    other than this function itself, is a needless risk for what is
    otherwise a purely cosmetic storage choice. A page with nothing to
    rename is left completely untouched, compressed or not.
    """
    if not renames:
        return None
    root = _parse_root(source)
    pairs = _page_models(root)
    multi_page = len(pairs) > 1
    changed = False

    for page_index, (diagram, model) in enumerate(pairs):
        prefix = _page_prefix(page_index, multi_page)
        id_map = _page_id_map(model, prefix, renames)
        if not id_map:
            continue
        _rename_page_ids(model, id_map)
        changed = True
        if diagram is not None and diagram.find("mxGraphModel") is None:
            diagram.text = None
            diagram.append(model)

    return ET.tostring(root, encoding="unicode") if changed else None


def _object_attrs_by_id(model: ET.Element, prefix: str) -> dict[str, dict[str, str]]:
    """Every ``<object>``/``<UserObject>``-wrapped cell's own attributes, keyed
    by the same page-prefixed id used for :attr:`Cell.cell_id` -- these carry
    the user-typed label data a C4 object cell's plain ``value`` does not (see
    :class:`Cell`)."""
    out: dict[str, dict[str, str]] = {}
    for obj in _iter_object_cells(model):
        raw_id = obj.get("id")
        if raw_id:
            out[prefix + raw_id] = dict(obj.attrib)
    return out


def load_cells(source: str) -> list[Cell]:
    """Parse a ``.drawio`` file path or raw XML string into cells.

    A styled element with no ``id`` at all is skipped, matching
    :func:`parent_map`'s existing behaviour -- without this, the two
    functions disagreed (this one kept an id-less cell as ``cell_id==""``,
    the other dropped it), so a cell with no id would silently vanish from
    containment resolution (no entry in ``parent_map``, no warning) with no
    signal that anything was lost. A cell that can't even be identified
    can't be meaningfully tracked by anything downstream anyway.
    """
    root = _parse_root(source)
    models = _model_roots(root)
    multi_page = len(models) > 1
    seen: set[str] = set()
    cells: list[Cell] = []
    for page_index, model in enumerate(models):
        prefix = _page_prefix(page_index, multi_page)
        object_attrs = _object_attrs_by_id(model, prefix)
        for element in _cell_elements(model):
            style = element.get("style") or ""
            raw_id = element.get("id")
            if not raw_id or not style.strip():
                continue
            cell_id = prefix + raw_id
            if cell_id in seen:
                continue
            seen.add(cell_id)
            cells.append(
                Cell(
                    cell_id=cell_id,
                    style=style,
                    value=element.get("value") or "",
                    tokens=parse_style(style),
                    object_attrs=object_attrs.get(cell_id, {}),
                    is_edge=element.get("edge") == "1",
                )
            )
    return cells


@dataclass(frozen=True)
class RawCell:
    """A cell's raw containment context: its parent id and own style, from an
    UNFILTERED pass over every cell in the document -- including the styleless
    "layer" cells and Ctrl+G "group" cells that :func:`load_cells` excludes,
    since a resolved cell's containment ancestry commonly passes through them.

    ``source_id``/``target_id`` are only meaningful when ``is_edge`` -- the
    endpoints an edge cell connects, page-prefixed the same as ``parent_id``.
    """

    parent_id: str | None
    style: str
    is_edge: bool
    source_id: str | None = None
    target_id: str | None = None


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
            raw_source = element.get("source")
            raw_target = element.get("target")
            mapping[cell_id] = RawCell(
                parent_id=(prefix + raw_parent) if raw_parent else None,
                style=element.get("style") or "",
                is_edge=element.get("edge") == "1",
                source_id=(prefix + raw_source) if raw_source else None,
                target_id=(prefix + raw_target) if raw_target else None,
            )
    return mapping


# ── resolution ─────────────────────────────────────────────────────────────--
def _near_candidates(
    cell: Cell, index: StyleIndex, weights: Weights, band: float, floor: float
) -> list[Candidate]:
    """The cell's candidates within ``band`` of its top similarity.

    Scored only against entries whose registry ``kind`` agrees with whether
    ``cell`` is itself an edge -- an edge cell can never resolve to a vertex
    (or vice versa), no matter how similar their styles score. Without this,
    a bare/default edge style can out-score every real edge candidate against
    a vertex shape whose canonical style happens to be near-empty boilerplate
    (e.g. a composite shape's anchor cell), silently "resolving" the edge to
    something it structurally cannot be.
    """
    scored = [
        s for s in index.score_all(cell.tokens, weights)
        if (s.entry.kind == "edge") == cell.is_edge
    ]
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


# How many near-candidates a cell keeps for its ``alternatives`` display --
# just enough for a human to sanity-check an ambiguous resolution.
_MAX_DISPLAYED_CANDIDATES = 8


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
                candidates=near[:_MAX_DISPLAYED_CANDIDATES],
                chosen=chosen,
                resolved_by=resolved_by,
                confidence=confidence,
            )
        )
    return DocumentResult(results, scores, anchor_votes)
