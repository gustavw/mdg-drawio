"""Palette-entry grouping for parsed palette data.

The palette fixtures assign one sequential cell id per MENU ENTRY and re-id
inner cells as "<entry>_<inner>" (see tools/palette/generate_palette.py), so
entry boundaries are recoverable from cell ids. menu_index in a registry runs
sequentially across pages in provenance.pages order.
"""
from __future__ import annotations

from typing import Any

Cell = dict[str, Any]


def entry_groups(diagram: dict[str, Any]) -> list[tuple[int, list[Cell]]]:
    """Group a diagram's cells by palette-entry number, in entry order."""
    groups: dict[int, list[Cell]] = {}
    for cell in diagram.get("cells", []):
        cid = str(cell.get("id"))
        if cid in ("0", "1"):
            continue
        try:
            entry_no = int(cid.split("_", 1)[0])
        except ValueError:
            continue
        groups.setdefault(entry_no, []).append(cell)
    return sorted(groups.items())


def flatten_entries(
    data: dict[str, Any], pages: list[str]
) -> list[list[Cell]]:
    """All palette entries of a library, in menu order (menu_index - 1 indexes
    this list). `pages` is the registry's provenance.pages."""
    by_name = {dg["name"]: dg for dg in data.get("diagrams", [])}
    missing = [p for p in pages if p not in by_name]
    if missing:
        raise KeyError(f"pages not found in palette data: {missing}")
    flat: list[list[Cell]] = []
    for name in pages:
        flat.extend(cells for _, cells in entry_groups(by_name[name]))
    return flat


def top_level(cells: list[Cell]) -> list[Cell]:
    return [c for c in cells if c.get("parent") == "1"]


def anchor_cell(cells: list[Cell], kind: str) -> Cell:
    """The cell that carries the entry's identity: for edge shapes the first
    top-level edge cell; otherwise the first top-level vertex cell (falling
    back to the first top-level cell)."""
    top = top_level(cells)
    if not top:
        raise ValueError("palette entry has no top-level cells")
    if kind == "edge":
        for c in top:
            if c.get("edge"):
                return c
        raise ValueError("edge shape but no top-level edge cell in entry")
    for c in top:
        if not c.get("edge"):
            return c
    return top[0]
