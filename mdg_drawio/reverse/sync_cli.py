"""CLI: reconcile an existing ``.mdg`` file against a hand-edited ``.drawio``,
treating draw.io as the source of truth.

    mdg sync EXISTING.mdg DIAGRAM.drawio          # dry run
    mdg sync EXISTING.mdg DIAGRAM.drawio --write  # writes

Dry run is the default: it prints a unified diff of what WOULD change and
never touches ``EXISTING.mdg``. ``--write`` re-parses the result through the
same parser the real pipeline uses (:func:`mdg_drawio.reverse.merge.validate`)
before writing anything -- if it doesn't parse cleanly, the file is left
untouched and the error is reported. See :func:`mdg_drawio.reverse.merge.
plan_sync` for what "removed" means and its limits (an edge's identity is a
(source, target) pair, not a stable id -- see that module's docstring).

Unlike ``mdg merge`` (which only ever adds), ``sync`` also DELETES existing
``.mdg`` content: any vertex or edge whose draw.io cell no longer exists is
removed, subtree and all. Review the diff before passing --write.
"""
from __future__ import annotations

import argparse
import difflib
import sys

from . import merge
from .containment import resolve_containment
from .derive import derive, load_cells, parent_map, rewrite_cell_ids
from .naming import assign_semantic_ids, reserved_counters
from .style_index import StyleIndex


def _plan(
    existing_text: str, drawio_path: str, index: StyleIndex
) -> tuple[merge.SyncPlan, str]:
    existing = merge.index_existing(existing_text)
    cells = load_cells(drawio_path)
    result = derive(cells, index)
    reserved = reserved_counters(existing.node_ids())
    # A cell whose raw id ALREADY matches an existing declared node_id keeps
    # THAT id -- never the fresh one assign_semantic_ids mints for every
    # resolved cell unconditionally -- regardless of whether its declaration
    # ends up removed, reparented, or left alone below. Without this, a
    # survivor being reparented (see plan_sync) would get renamed out from
    # under itself: resolve_containment would report a *different* cell's
    # freshly-minted (not yet corrected) name as its container, so anything
    # nested under an otherwise-untouched, already-existing container would
    # falsely look reparented too, purely because of container identity
    # churn its own containment never actually had.
    existing_ids = existing.node_ids()
    node_ids = {
        s.cell_id: s.cell_id if s.cell_id in existing_ids else s.node_id
        for s in assign_semantic_ids(result, reserved)
    }
    raw_cells = parent_map(drawio_path)
    containments = {
        c.cell_id: c for c in resolve_containment(result, raw_cells, node_ids)
    }

    plan = merge.plan_sync(existing, cells, result, node_ids, containments, raw_cells)
    synced_text = merge.render_sync(existing, plan)
    return plan, synced_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mdg sync")
    parser.add_argument("mdg", help="path to the existing .mdg file")
    parser.add_argument("drawio", help="path to the hand-edited .drawio file")
    parser.add_argument(
        "--write", action="store_true", help="write the synced result in place"
    )
    args = parser.parse_args(argv)

    index = StyleIndex.load()
    if not index.entries:
        print("no style data -- run `make build-data` first", file=sys.stderr)
        return 2

    with open(args.mdg, encoding="utf-8") as handle:
        existing_text = handle.read()

    plan, synced_text = _plan(existing_text, args.drawio, index)

    if plan.merge_plan.skipped:
        print("skipped (could not derive a shape):", file=sys.stderr)
        for reason in plan.merge_plan.skipped:
            print(f"  {reason}", file=sys.stderr)

    if not plan.merge_plan.insertions and not plan.removed_ranges:
        print("nothing to sync -- already up to date.")
        return 0

    error = merge.validate(synced_text)
    if error is not None:
        print(f"sync would produce invalid .mdg -- aborting: {error}", file=sys.stderr)
        return 1

    # Every newly-minted node gets its .drawio cell id renamed to match --
    # otherwise the NEXT plain regenerate's geometry overlay (which matches
    # a node by id) can never find that cell again, and it silently loses
    # whatever manual layout it has the moment sync runs.
    synced_drawio = rewrite_cell_ids(args.drawio, plan.merge_plan.renamed_ids)
    renamed_count = len(plan.merge_plan.renamed_ids) if synced_drawio else 0

    if not args.write:
        diff = difflib.unified_diff(
            existing_text.splitlines(keepends=True),
            synced_text.splitlines(keepends=True),
            fromfile=args.mdg,
            tofile=f"{args.mdg} (synced)",
        )
        sys.stdout.writelines(diff)
        print(
            f"\n{plan.merge_plan.new_node_count} new element(s), "
            f"{plan.merge_plan.new_edge_count} new edge(s), "
            f"{plan.removed_vertex_count} removed element(s), "
            f"{plan.removed_edge_count} removed edge(s) -- "
            "dry run, use --write to apply."
        )
        if renamed_count:
            print(
                f"{renamed_count} cell id(s) in {args.drawio} would also be "
                "renamed to match, so a later plain regenerate can still "
                "find them and keep their manual layout."
            )
        return 0

    with open(args.mdg, "w", encoding="utf-8") as handle:
        handle.write(synced_text)
    if synced_drawio is not None:
        with open(args.drawio, "w", encoding="utf-8") as handle:
            handle.write(synced_drawio)
    rename_note = (
        f", renamed {renamed_count} cell id(s) in {args.drawio}"
        if renamed_count
        else ""
    )
    print(
        f"wrote {plan.merge_plan.new_node_count} new element(s), "
        f"{plan.merge_plan.new_edge_count} new edge(s), "
        f"{plan.removed_vertex_count} removed element(s), "
        f"{plan.removed_edge_count} removed edge(s) to {args.mdg}{rename_note}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
