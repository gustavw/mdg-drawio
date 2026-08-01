"""CLI: merge newly hand-drawn cells from a ``.drawio`` into an existing
``.mdg`` file.

    python -m scripts.reverse.merge_cli EXISTING.mdg NEW.drawio          # dry run
    python -m scripts.reverse.merge_cli EXISTING.mdg NEW.drawio --write  # writes

Dry run is the default: it prints a unified diff of what WOULD change and
never touches ``EXISTING.mdg``. ``--write`` re-parses the merged result
through the same parser the real pipeline uses (:func:`scripts.reverse.merge.
validate`) before writing anything -- if it doesn't parse cleanly, the file is
left untouched and the error is reported. See :mod:`scripts.reverse.merge` for
what "new" means and what this does and does not emit (vertices only, no
edges yet).
"""
from __future__ import annotations

import argparse
import difflib
import sys

from scripts.reverse import merge
from scripts.reverse.containment import resolve_containment
from scripts.reverse.derive import derive, load_cells, parent_map
from scripts.reverse.naming import assign_semantic_ids, reserved_counters
from scripts.reverse.style_index import StyleIndex


def _plan(
    existing_text: str, drawio_path: str, index: StyleIndex
) -> tuple[merge.MergePlan, str]:
    existing = merge.index_existing(existing_text)
    cells = load_cells(drawio_path)
    result = derive(cells, index)
    reserved = reserved_counters(existing.node_ids())
    node_ids = {s.cell_id: s.node_id for s in assign_semantic_ids(result, reserved)}
    raw_cells = parent_map(drawio_path)
    containments = {
        c.cell_id: c for c in resolve_containment(result, raw_cells, node_ids)
    }
    plan = merge.plan_merge(existing, cells, result, node_ids, containments, raw_cells)
    merged_text = merge.render_merge(existing, plan)
    return plan, merged_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.reverse.merge_cli")
    parser.add_argument("mdg", help="path to the existing .mdg file")
    parser.add_argument("drawio", help="path to the hand-drawn .drawio file")
    parser.add_argument(
        "--write", action="store_true", help="write the merged result in place"
    )
    args = parser.parse_args(argv)

    index = StyleIndex.load()
    if not index.entries:
        print("no style data -- run `make build-data` first", file=sys.stderr)
        return 2

    with open(args.mdg, encoding="utf-8") as handle:
        existing_text = handle.read()

    plan, merged_text = _plan(existing_text, args.drawio, index)

    if plan.skipped:
        print("skipped (could not derive a shape):", file=sys.stderr)
        for reason in plan.skipped:
            print(f"  {reason}", file=sys.stderr)
    if plan.new_edge_count:
        print(
            f"note: {plan.new_edge_count} new edge(s) found in the .drawio -- "
            "not yet emitted (vertices only; see scripts/reverse/merge.py)",
            file=sys.stderr,
        )

    if not plan.insertions:
        print("nothing new to merge.")
        return 0

    error = merge.validate(merged_text)
    if error is not None:
        print(f"merge would produce invalid .mdg -- aborting: {error}", file=sys.stderr)
        return 1

    if not args.write:
        diff = difflib.unified_diff(
            existing_text.splitlines(keepends=True),
            merged_text.splitlines(keepends=True),
            fromfile=args.mdg,
            tofile=f"{args.mdg} (merged)",
        )
        sys.stdout.writelines(diff)
        print(
            f"\n{plan.new_node_count} new element(s) -- "
            "dry run, use --write to apply."
        )
        return 0

    with open(args.mdg, "w", encoding="utf-8") as handle:
        handle.write(merged_text)
    print(f"wrote {plan.new_node_count} new element(s) to {args.mdg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
