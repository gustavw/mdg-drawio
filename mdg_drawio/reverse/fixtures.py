"""Synthesize ``.drawio`` test data from the palette styles.

Ground-truth fixtures for the reverse derivation: we know exactly which shape
each cell came from because we built the cell *from* that shape's canonical
style. Because those styles are draw.io-copyright (they live in the git-ignored
``generated_data``), fixtures are generated at test time and never committed.

Supports the three cases the design needs to exercise:

* canonical -- drag a shape as-is (the exact-match baseline);
* perturbed -- recolour / re-font / re-align it (the robustness case);
* scenario -- several shapes in one document (the ranking case).
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .derive import Cell, derive
from .style_index import ShapeEntry, StyleIndex

_HEADER = '<mxfile><diagram name="Page-1"><mxGraphModel><root>'
_ROOT_CELLS = '<mxCell id="0"/><mxCell id="1" parent="0"/>'
_FOOTER = "</root></mxGraphModel></diagram></mxfile>"

# Escaped so a style string's own literal quotes don't terminate the
# surrounding style="..." XML attribute.
QUOTE = '"'


def perturb(style: str, **overrides: str) -> str:
    """Return ``style`` with the given ``key=value`` tokens set/replaced.

    Used to simulate a user editing cosmetic tokens in the UI, e.g.
    ``perturb(s, fillColor="#FF0000", fontFamily="Comic Sans MS")``.
    """
    tokens = [t for t in style.split(";") if t.strip()]
    kept = [t for t in tokens if t.split("=", 1)[0] not in overrides]
    added = [f"{key}={value}" for key, value in overrides.items()]
    return ";".join(kept + added) + ";"


def cell_xml(
    cell_id: str, style: str, x: int, width: int, height: int, parent: str = "1"
) -> str:
    """One vertex ``mxCell`` at ``(x, 40)`` with the given style and parent."""
    geo = (
        f'<mxGeometry x="{x}" y="40" width="{width}" '
        f'height="{height}" as="geometry"/>'
    )
    return (
        f'<mxCell id="{cell_id}" value="" style="{escape(style, {QUOTE: "&quot;"})}" '
        f'vertex="1" parent="{parent}">{geo}</mxCell>'
    )


_GROUP_CELL_SIZE = 200


def group_cell_xml(cell_id: str, parent: str = "1") -> str:
    """A Ctrl+G "group" cell: a vertex whose style is just the bare token
    ``group`` -- a UI bounding box, not a real shape."""
    return cell_xml(
        cell_id,
        "group;",
        x=0,
        width=_GROUP_CELL_SIZE,
        height=_GROUP_CELL_SIZE,
        parent=parent,
    )


def layer_cell_xml(cell_id: str, parent: str = "1") -> str:
    """A draw.io "layer" cell: no ``vertex``/``edge``, no style -- purely
    organisational, never a real shape."""
    return f'<mxCell id="{cell_id}" value="Layer" parent="{parent}"/>'


def edge_cell_xml(
    cell_id: str,
    source: str,
    target: str,
    parent: str = "1",
    style: str = "edgeStyle=orthogonalEdgeStyle;html=1;",
) -> str:
    """A relationship edge between two node ids. Defaults to a generic style
    that doesn't resolve to any registry shape; pass a real entry's style
    (e.g. ``fx.get(index, "c4.rel.v1").style``) to synthesize one that does.
    """
    return (
        f'<mxCell id="{cell_id}" style="{escape(style, {QUOTE: "&quot;"})}" '
        f'edge="1" parent="{parent}" source="{source}" target="{target}">'
        f'<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def document(*cells: str) -> str:
    """Wrap serialized cells into a complete ``.drawio`` document string."""
    return _HEADER + _ROOT_CELLS + "".join(cells) + _FOOTER


def entry_cell(
    entry: ShapeEntry,
    cell_id: str = "10",
    x: int = 0,
    style: str | None = None,
    parent: str = "1",
) -> str:
    """A fixture cell for one shape entry (its canonical style by default).

    Serialized as an ``edge="1"`` cell for an edge-kind entry, a plain
    ``vertex="1"`` cell otherwise -- derivation now checks that attribute
    against the candidate's registry ``kind`` (see :func:`.derive._near_
    candidates`), so a relationship shape's style wrapped in a vertex cell
    would no longer resolve to itself.
    """
    resolved_style = style if style is not None else entry.style
    if entry.kind == "edge":
        return edge_cell_xml(
            cell_id,
            source=f"{cell_id}-src",
            target=f"{cell_id}-tgt",
            parent=parent,
            style=resolved_style,
        )
    return cell_xml(cell_id, resolved_style, x, 120, 120, parent)


def get(index: StyleIndex, shape_id: str) -> ShapeEntry:
    """The entry for an exact shape id (raises if absent)."""
    for entry in index.entries:
        if entry.shape_id == shape_id:
            return entry
    raise KeyError(shape_id)


def find(index: StyleIndex, library: str, needle: str) -> ShapeEntry:
    """First entry in ``library`` whose shape id contains ``needle``."""
    for entry in sorted(index.entries, key=lambda e: e.shape_id):
        if entry.library == library and needle in entry.shape_id:
            return entry
    raise KeyError(f"{library}:{needle}")


def _as_cell(entry: ShapeEntry) -> Cell:
    """A throwaway derivation cell carrying one entry's canonical style."""
    return Cell(
        entry.shape_id, entry.style, "", entry.tokens, is_edge=entry.kind == "edge"
    )


def library_only_anchor(index: StyleIndex, library: str) -> ShapeEntry:
    """A shape whose style resolves unambiguously to ``library`` -- an anchor.

    Dropped into a document on its own it votes cleanly for ``library`` (its
    candidate set lies entirely within it), so it can pull an ambiguous
    cross-version shape toward the older library in a scenario fixture.
    """
    for entry in sorted(index.entries, key=lambda e: e.shape_id):
        if entry.library != library:
            continue
        result = derive([_as_cell(entry)], index)
        cell = result.cells[0]
        if (
            cell.chosen
            and cell.resolved_by in ("unique", "single-library")
            and {c.library for c in cell.candidates} == {library}
        ):
            return entry
    raise KeyError(f"no unique anchor for {library}")
