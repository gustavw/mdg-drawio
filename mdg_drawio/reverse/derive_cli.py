"""CLI: derive registry shapes from a hand-drawn ``.drawio`` file.

    mdg derive path/to/diagram.drawio
    mdg derive path/to/diagram.drawio --json

Prints, per cell, the derived shape, its library, a generated semantic
``.mdg`` node id (``person1``, ``system1``, ...; see
:mod:`mdg_drawio.reverse.naming`), its nesting (nearest container + depth; see
:mod:`mdg_drawio.reverse.containment`), similarity, confidence, and how it was
resolved (unique / single-library / library-vote / recency-prior), plus the
document-level library scores. Requires ``make build-data``.
"""
from __future__ import annotations

import argparse
import json
import sys

from .containment import Containment, resolve_containment
from .derive import DocumentResult, derive, load_cells, parent_map
from .naming import assign_semantic_ids
from .style_index import StyleIndex


def _as_dict(
    result: DocumentResult,
    node_ids: dict[str, str],
    containments: dict[str, Containment],
) -> dict[str, object]:
    return {
        "library_scores": result.library_scores,
        "cells": [
            {
                "cell_id": c.cell_id,
                "node_id": node_ids.get(c.cell_id),
                "shape_id": c.chosen.shape_id if c.chosen else None,
                "library": c.chosen.library if c.chosen else None,
                "similarity": round(c.chosen.sim, 3) if c.chosen else 0.0,
                "confidence": c.confidence,
                "resolved_by": c.resolved_by,
                "container_node_id": (
                    containments[c.cell_id].container_node_id
                    if c.cell_id in containments
                    else None
                ),
                "depth": (
                    containments[c.cell_id].depth if c.cell_id in containments else 0
                ),
                "containment_warnings": list(
                    containments[c.cell_id].warnings
                    if c.cell_id in containments
                    else ()
                ),
                "alternatives": [
                    f"{cand.library}:{cand.shape_id} ({cand.sim:.3f})"
                    for cand in c.candidates
                ],
            }
            for c in result.cells
        ],
    }


def _print_table(
    result: DocumentResult,
    node_ids: dict[str, str],
    containments: dict[str, Containment],
) -> None:
    scores = ", ".join(
        f"{lib}={score:.2f}"
        for lib, score in sorted(
            result.library_scores.items(), key=lambda kv: kv[1], reverse=True
        )
        if score
    )
    print(f"library scores: {scores}\n")
    # An explicit space between every field (rather than relying solely on
    # fixed width) guarantees a visible gap even when a long node_id or shape
    # id overflows its nominal column.
    header = (
        f"{'cell':<8} {'node_id':<16} {'shape':<34} {'lib':<10} "
        f"{'sim':>6} {'conf':>7}  {'parent':<16} {'d':>3}  how"
    )
    print(header)
    print("-" * len(header))
    warnings: list[str] = []
    for cell in result.cells:
        node_id = node_ids.get(cell.cell_id, "")
        containment = containments.get(cell.cell_id)
        parent = containment.container_node_id if containment else None
        depth = containment.depth if containment else 0
        for w in containment.warnings if containment else ():
            warnings.append(f"{cell.cell_id}: {w}")
        if cell.chosen:
            print(
                f"{cell.cell_id:<8} {node_id:<16} {cell.chosen.shape_id:<34} "
                f"{cell.chosen.library:<10} {cell.chosen.sim:>6.3f} "
                f"{cell.confidence:>7.3f}  {parent or '-':<16} {depth:>3}  "
                f"{cell.resolved_by}"
            )
        else:
            print(
                f"{cell.cell_id:<8} {'':<16} {'(no match)':<34} "
                f"{'':<10} {'':>6} {'':>7}  {'-':<16} {'':>3}  none"
            )
    if warnings:
        print("\ncontainment warnings:")
        for w in warnings:
            print(f"  {w}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mdg derive")
    parser.add_argument("drawio", help="path to a .drawio file")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    index = StyleIndex.load()
    if not index.entries:
        print("no style data -- run `make build-data` first", file=sys.stderr)
        return 2

    cells = load_cells(args.drawio)
    result = derive(cells, index)
    node_ids = {s.cell_id: s.node_id for s in assign_semantic_ids(result)}
    raw_cells = parent_map(args.drawio)
    containments = {
        c.cell_id: c for c in resolve_containment(result, raw_cells, node_ids)
    }
    if args.json:
        print(json.dumps(_as_dict(result, node_ids, containments), indent=2))
    else:
        _print_table(result, node_ids, containments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
