#!/usr/bin/env python3
"""Build notation shape and row-type sidecars in generated_data/notation/.

Joins each registry entry to its palette entry (provenance.pages order +
menu_index) and FAILS if any committed render.fingerprint no longer matches
the palette — palette drift must be a loud error, not silent misrendering.

The generated ``<lib>_styles.json`` and ``<lib>_row_types.json`` files are
derived from the draw.io shape library and gitignored with the rest of
``mdg_drawio/generated_data/``. Runs as the final step of ``make build-data``.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
PALETTE_OUTPUT_DIR = ROOT / "tools" / "styles" / "output"
LIBRARY_PALETTE_JSON: dict[str, str] = {
    "archimate3": "Business/ArchiMate_3.2.json",
    "bpmn2": "Business/BPMN_2.0.json",
    "c4": "Software/C4.json",
    "erd": "Software/Entity_Relation.json",
    "general": "Standard/General.json",
    "uml": "Software/UML.json",
    "uml25": "Software/UML_2.5.json",
}


# (style signal, candidate row-type names in priority order) -- the first
# candidate present in the shape's own rows_allowed wins. Matched in order,
# so more specific signals (a divider line) are tried before generic ones
# (plain centered/left text).
_ROW_STYLE_SIGNALS: tuple[tuple[Callable[[str], bool], tuple[str, ...]], ...] = (
    (lambda s: s.startswith("line;"), ("Divider",)),
    (lambda s: "shape=tableRow" in s, ("Row", "RowKey", "TableRowBoxPart")),
    (lambda s: "shape=partialRectangle" in s, ("Lane",)),
    (lambda s: s.startswith("swimlane;"), ("SwimlaneBoxPart", "Note")),
    (lambda s: "align=center" in s, ("Header",)),
)


def _classify_row_cell(style: str, rows_allowed: list[str]) -> str | None:
    """Classify a nested cell against the shape's own declared row types.

    Row types (Item, Header, Divider, ...) are never independently placeable
    in the real draw.io palette -- they only exist as cells nested inside a
    composite shape (a Classifier, a Table, ...). There is no field in the
    raw data that labels a cell "this is a Header"; classification uses the
    same visual signals a human already used to write each row_type's
    registry docstring (a center-aligned text cell is a Header, a
    'line;'-styled cell is a Divider, and so on -- see ``_ROW_STYLE_SIGNALS``).

    ``container=1`` cells are internal composite scaffolding (e.g. a nested
    sub-state placeholder), never an author-facing row -- excluded outright.
    When a shape declares exactly one row type, every remaining direct child
    is that type, no style inspection needed. A plain text cell that matches
    no signal defaults to Item, the row_types' own documented "default" row.
    """
    if "container=1" in style:
        return None
    if len(rows_allowed) == 1:
        return rows_allowed[0]
    for matches, candidates in _ROW_STYLE_SIGNALS:
        if not matches(style):
            continue
        for name in candidates:
            if name in rows_allowed:
                return name
    if "Item" in rows_allowed:
        return "Item"
    return None


def _cells_by_parent(cells: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for cell in cells:
        by_parent.setdefault(str(cell.get("parent")), []).append(cell)
    return by_parent


# Row types known (from real registry rows.allowed data) to nest further row
# instances directly inside themselves: bpmn2's TableRowBoxPart contains
# SwimlaneBoxPart cells, uml25's Lane contains further Lane cells. Every
# other row type's children are fixed template internals (e.g. uml25's Note
# ships example "property"/"connector" decoration, not further Header/Note
# rows) and must NOT be walked, or they get misclassified by the same style
# heuristics that correctly classify genuine rows.
_RECURSIVE_ROW_TYPES = frozenset({"TableRowBoxPart", "Lane"})

# Some row types also have a standalone palette shape but use a different
# nested template. ERD RowKey is a mini table when placed at the top level and
# a tableRow with two child cells when nested inside Table, so both forms must
# be retained independently.
_REQUIRED_NESTED_TEMPLATES: dict[str, frozenset[str]] = {
    "erd": frozenset({"RowKey"}),
}


def _collect_row_type_cells(
    cells: list[dict[str, Any]], anchor_id: str, rows_allowed: list[str]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Walk the cell tree from the anchor, classifying descendants.

    Returns ``(first_occurrence_by_type, consumed_ids)``. ``consumed_ids`` is
    every cell classified as any row type (not just the first), so callers
    can tell a row's genuine compound sub-cells (e.g. erd Table row's
    [key tag, text label] pair) apart from a sibling/nested row occurrence.
    """
    by_parent = _cells_by_parent(cells)
    found: dict[str, dict[str, Any]] = {}
    consumed_ids: set[str] = set()
    frontier = [anchor_id]
    while frontier:
        parent_id = frontier.pop(0)
        for cell in by_parent.get(parent_id, []):
            row_type = _classify_row_cell(cell.get("style") or "", rows_allowed)
            if row_type is None:
                continue
            # ERD tables declare both Row and RowKey. Their wrapper styles are
            # nearly identical, but keyed rows have a non-empty first child;
            # plain rows have an empty editable key cell. Classify from that
            # structural distinction instead of aliasing both to the first row.
            if row_type == "Row" and "RowKey" in rows_allowed:
                children = by_parent.get(str(cell.get("id")), [])
                if children and children[0].get("value") not in (None, ""):
                    row_type = "RowKey"
            consumed_ids.add(str(cell.get("id")))
            found.setdefault(row_type, cell)
            if row_type in _RECURSIVE_ROW_TYPES:
                frontier.append(str(cell.get("id")))
    return found, consumed_ids


def _row_type_entry(
    cell: dict[str, Any], children: list[dict[str, Any]]
) -> dict[str, Any]:
    """A row-type sidecar entry: style/geometry, plus compound sub-cells if any.

    A swimlane-styled row (e.g. uml25's Note) carries fixed example decoration
    of its own -- not a compound cell pair meant to be reconstructed per
    instance -- so its children are never recorded here, only genuinely
    compound rows like erd's [key tag, text label] table row are.
    """
    geometry = cell.get("geometry") or {}
    entry: dict[str, Any] = {
        "style": cell.get("style") or "",
        "width": geometry.get("width"),
        "height": geometry.get("height"),
    }
    if children and not (cell.get("style") or "").startswith("swimlane;"):
        entry["cells"] = [
            {"style": child.get("style") or "", "geometry": child.get("geometry") or {}}
            for child in children
        ]
    return entry


def _build_row_types(
    registry: dict[str, Any],
    sidecar: dict[str, Any],
    anchor_cell: Callable[[list[dict[str, Any]], str], dict[str, Any]],
) -> dict[str, Any]:
    """Canonical style/geometry for every row type with no top-level shape.

    A row type that also has its own independently-placeable palette entry
    (e.g. uml.Item, erd.Item) already resolves correctly through the normal
    shapes: lookup and is skipped here -- extracting it too would risk
    picking a less-representative variant. The first (menu-order) match
    across all shapes that use a row type becomes its canonical style.
    """
    row_type_names = {rt["name"] for rt in registry.get("row_types") or []}
    if not row_type_names:
        return {}
    by_function = {s["function"] for s in registry["shapes"]}
    required_nested = _REQUIRED_NESTED_TEMPLATES.get(
        str(registry["library"]), frozenset()
    )

    row_types: dict[str, Any] = {}
    for shape in registry["shapes"]:
        rows_allowed = (shape.get("rows") or {}).get("allowed") or []
        if not rows_allowed:
            continue
        entry = sidecar.get(shape["id"])
        if not entry:
            continue
        cells = entry["cells"]
        anchor_id = str(anchor_cell(cells, shape["kind"])["id"])
        classified, consumed_ids = _collect_row_type_cells(
            cells, anchor_id, rows_allowed
        )
        by_parent = _cells_by_parent(cells)
        for row_type, cell in classified.items():
            if row_type in row_types or (
                row_type in by_function
                and row_type not in required_nested
            ):
                continue
            children = [
                c
                for c in by_parent.get(str(cell.get("id")), [])
                if str(c.get("id")) not in consumed_ids
            ]
            row_types[row_type] = _row_type_entry(cell, children)

    required = (row_type_names - by_function) | (
        row_type_names & required_nested
    )
    missing = required - row_types.keys()
    if missing:
        raise ValueError(
            "no palette-derived style found for row types: "
            f"{sorted(missing)}"
        )
    return row_types


def _build_library(
    library: str,
    registry: dict[str, Any],
    anchor_cell: Callable[[list[dict[str, Any]], str], dict[str, Any]],
    flatten_entries: Callable[[dict[str, Any], list[str]], list[list[dict[str, Any]]]],
    style_fingerprint: Callable[[str], str],
) -> tuple[dict[str, Any], list[str]]:
    data = json.loads(
        (PALETTE_OUTPUT_DIR / LIBRARY_PALETTE_JSON[library]).read_text(
            encoding="utf-8"
        )
    )
    flat = flatten_entries(data, registry["provenance"]["pages"])

    sidecar: dict[str, Any] = {}
    errors: list[str] = []
    for shape in registry["shapes"]:
        cells = flat[shape["menu_index"] - 1]
        # Guard the object/value layer, which the style fingerprint does NOT
        # cover: a value of "[object Object]" is the unambiguous signature of
        # the extractor stringifying an <object> wrapper instead of emitting
        # it (see tools/palette/extract_shapes.js setValue/object handling).
        # No legitimate shape has that value, so treat it as a loud failure
        # rather than let corrupted metadata ship silently.
        if any(c.get("value") == "[object Object]" for c in cells):
            errors.append(
                f"{shape['id']}: object-layer corruption — a cell value is "
                f"'[object Object]' (menu_index {shape['menu_index']}); the "
                "palette extractor dropped an <object> wrapper"
            )
            continue
        anchor = anchor_cell(cells, shape["kind"])
        style = anchor.get("style") or ""
        fingerprint = style_fingerprint(style)
        expected = shape["render"]["fingerprint"]
        if fingerprint != expected:
            errors.append(
                f"{shape['id']}: fingerprint mismatch — registry {expected}, "
                f"palette {fingerprint} (menu_index {shape['menu_index']}); "
                "the palette drifted or menu_index is wrong"
            )
            continue
        geometry = anchor.get("geometry") or {}
        sidecar[shape["id"]] = {
            "fingerprint": fingerprint,
            "style": style,
            "width": geometry.get("width"),
            "height": geometry.get("height"),
            "cells": cells,
        }
    return sidecar, errors


def _write_row_types_sidecar(
    library: str,
    registry: dict[str, Any],
    sidecar: dict[str, Any],
    out_dir: Path,
    anchor_cell: Callable[[list[dict[str, Any]], str], dict[str, Any]],
) -> int:
    """Write (or clean up) <lib>_row_types.json; returns the entry count.

    Row types (e.g. uml25's Item/Header/Divider) have no independent shape id
    of their own, so they live in a separate sidecar rather than as extra
    keys in <lib>_styles.json -- several consumers (test_styles_sidecar_is_fresh,
    scripts/reverse/style_index.py) assume every key there is a real registry
    shape id.
    """
    row_types = _build_row_types(registry, sidecar, anchor_cell)
    row_types_path = out_dir / f"{library}_row_types.json"
    if row_types:
        row_types_path.write_text(json.dumps(row_types, indent=1), encoding="utf-8")
    elif row_types_path.exists():
        row_types_path.unlink()
    return len(row_types)


def _main() -> None:
    sys.path.insert(0, str(ROOT))

    from mdg_drawio.notation import LIBRARIES, load_registry
    from mdg_drawio.notation._core.normalize import style_fingerprint
    from mdg_drawio.notation._core.palette import anchor_cell, flatten_entries
    from mdg_drawio.notation._core.styles import DATA_DIR

    if not PALETTE_OUTPUT_DIR.exists():
        sys.exit("tools/styles/output/ missing — run `make build-data` first")
    out_dir = DATA_DIR / "notation"
    out_dir.mkdir(parents=True, exist_ok=True)
    all_errors: list[str] = []
    for library in LIBRARIES:
        registry = load_registry(library)
        sidecar, errors = _build_library(
            library, registry, anchor_cell, flatten_entries, style_fingerprint
        )
        all_errors.extend(errors)
        path = out_dir / f"{library}_styles.json"
        path.write_text(json.dumps(sidecar, indent=1), encoding="utf-8")
        row_type_count = _write_row_types_sidecar(
            library, registry, sidecar, out_dir, anchor_cell
        )
        print(
            f"{library}: {len(sidecar)} shapes, {row_type_count} row types -> "
            f"{path.relative_to(ROOT)}"
        )
    if all_errors:
        print(f"\n{len(all_errors)} PALETTE VALIDATION ERRORS:", file=sys.stderr)
        for e in all_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
