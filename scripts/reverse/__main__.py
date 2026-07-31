"""CLI: derive registry shapes from a hand-drawn ``.drawio`` file.

    python -m scripts.reverse path/to/diagram.drawio
    python -m scripts.reverse path/to/diagram.drawio --json

Prints, per cell, the derived shape, its library, similarity, confidence, and
how it was resolved (unique / single-library / library-vote / recency-prior),
plus the document-level library scores. Requires ``make build-data``.
"""
from __future__ import annotations

import argparse
import json
import sys

from scripts.reverse.derive import DocumentResult, derive, load_cells
from scripts.reverse.style_index import StyleIndex


def _as_dict(result: DocumentResult) -> dict[str, object]:
    return {
        "library_scores": result.library_scores,
        "cells": [
            {
                "cell_id": c.cell_id,
                "shape_id": c.chosen.shape_id if c.chosen else None,
                "library": c.chosen.library if c.chosen else None,
                "similarity": round(c.chosen.sim, 3) if c.chosen else 0.0,
                "confidence": c.confidence,
                "resolved_by": c.resolved_by,
                "alternatives": [
                    f"{cand.library}:{cand.shape_id} ({cand.sim:.3f})"
                    for cand in c.candidates
                ],
            }
            for c in result.cells
        ],
    }


def _print_table(result: DocumentResult) -> None:
    scores = ", ".join(
        f"{lib}={score:.2f}"
        for lib, score in sorted(
            result.library_scores.items(), key=lambda kv: kv[1], reverse=True
        )
        if score
    )
    print(f"library scores: {scores}\n")
    header = f"{'cell':<8}{'shape':<34}{'lib':<10}{'sim':>6}{'conf':>7}  how"
    print(header)
    print("-" * len(header))
    for cell in result.cells:
        if cell.chosen:
            print(
                f"{cell.cell_id:<8}{cell.chosen.shape_id:<34}"
                f"{cell.chosen.library:<10}{cell.chosen.sim:>6.3f}"
                f"{cell.confidence:>7.3f}  {cell.resolved_by}"
            )
        else:
            print(f"{cell.cell_id:<8}{'(no match)':<34}{'':<10}{'':>6}{'':>7}  none")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.reverse")
    parser.add_argument("drawio", help="path to a .drawio file")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    index = StyleIndex.load()
    if not index.entries:
        print("no style data -- run `make build-data` first", file=sys.stderr)
        return 2

    cells = load_cells(args.drawio)
    result = derive(cells, index)
    if args.json:
        print(json.dumps(_as_dict(result), indent=2))
    else:
        _print_table(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
