"""Tests for `mdg sync` (:func:`mdg_drawio.reverse.merge.plan_sync`/
:func:`~mdg_drawio.reverse.merge.render_sync`) and its CLI
(:mod:`mdg_drawio.reverse.sync_cli`).

Two groups:

* pure text-scanning unit tests (no data) -- ``_split_top_level_args``,
  ``_block_range``, ``_merge_ranges``, independent of any real registry data;
* data-gated end-to-end tests against the real C4 registry and palette --
  every removal scenario (top-level vertex, a container's whole subtree, an
  edge whose pair is gone, an edge cascading from a removed endpoint) plus
  additions happening in the same sync, and the CLI's dry-run/--write/abort
  safety contract. These skip without ``make build-data``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mdg_drawio import main as convert_main
from mdg_drawio.contracts import Document, MultiPageDocument
from mdg_drawio.notation import parse as parse_mdg
from mdg_drawio.reverse import fixtures as fx
from mdg_drawio.reverse import merge
from mdg_drawio.reverse.containment import resolve_containment
from mdg_drawio.reverse.derive import derive, load_cells, parent_map
from mdg_drawio.reverse.naming import assign_semantic_ids, reserved_counters
from mdg_drawio.reverse.style_index import StyleIndex
from mdg_drawio.reverse.sync_cli import main as sync_main

INDEX = StyleIndex.load()
needs_data = pytest.mark.skipif(
    not INDEX.entries, reason="needs palette styles (run `make build-data`)"
)


# ── pure text-scanning unit tests (no data) ──────────────────────────────────


def test_split_top_level_args_respects_quoting_and_nesting() -> None:
    assert merge._split_top_level_args('a, "b, c", d') == ["a", '"b, c"', "d"]
    assert merge._split_top_level_args("") == []
    assert merge._split_top_level_args("only_one") == ["only_one"]


def test_merge_ranges_collapses_overlapping_and_adjacent() -> None:
    assert merge._merge_ranges([(0, 2), (2, 4)]) == [(0, 4)]
    assert merge._merge_ranges([(5, 8), (1, 3)]) == [(1, 3), (5, 8)]
    assert merge._merge_ranges([(0, 5), (1, 2)]) == [(0, 5)]
    assert merge._merge_ranges([]) == []


def test_block_range_is_single_line_for_a_leaf_node() -> None:
    existing = merge.index_existing('c4.Person(alice, "Alice")\n')
    assert merge._block_range(existing, "alice") == (0, 1)


def test_block_range_spans_a_containers_nested_children() -> None:
    existing = merge.index_existing(
        'c4.System_Boundary(b1, "B"):\n'
        '    c4.Container(web, "Web")\n'
        '    c4.Container(api, "API")\n'
        'c4.Person(user, "User")\n'
    )
    assert merge._block_range(existing, "b1") == (0, 3)


# ── end-to-end (real registry + palette) ─────────────────────────────────────
def _run_sync_pipeline(
    existing_text: str, drawio_doc: str
) -> tuple[merge.ExistingIndex, merge.SyncPlan, str]:
    # Mirrors sync_cli._plan's real node_ids construction (an already-
    # existing cell keeps its own id, never a freshly-minted one) so this
    # test helper exercises the same path the CLI actually runs. A
    # multi-page drawio's cell_id is page-prefixed ("N:..."), .mdg text
    # never is, so the "already declared" check compares the BARE id (see
    # merge.strip_page_prefix).
    existing = merge.index_existing(existing_text)
    cells = load_cells(drawio_doc)
    result = derive(cells, INDEX)
    reserved = reserved_counters(existing.node_ids())
    existing_ids = existing.node_ids()
    node_ids = {}
    for s in assign_semantic_ids(result, reserved):
        bare_id = merge.strip_page_prefix(s.cell_id)
        node_ids[s.cell_id] = bare_id if bare_id in existing_ids else s.node_id
    raw_cells = parent_map(drawio_doc)
    containments = {
        c.cell_id: c for c in resolve_containment(result, raw_cells, node_ids)
    }
    plan = merge.plan_sync(existing, cells, result, node_ids, containments, raw_cells)
    synced = merge.render_sync(existing, plan)
    return existing, plan, synced


_TWO_SYSTEM_MDG = (
    '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
    'c4.Person(alice, "Alice")\n'
    'c4.System(bob, "Bob System")\n'
)


@needs_data
def test_sync_removes_a_top_level_vertex_no_longer_in_the_drawio() -> None:
    """Only "alice" is drawn any more -- "bob" must disappear, "alice"'s own
    text must survive untouched."""
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(fx.entry_cell(person, cell_id="alice", parent="1"))
    _, plan, synced = _run_sync_pipeline(_TWO_SYSTEM_MDG, doc)
    assert plan.removed_vertex_count == 1
    assert plan.merge_plan.new_node_count == 0
    assert merge.validate(synced) is None
    assert 'c4.Person(alice, "Alice")' in synced.splitlines()
    assert "bob" not in synced


@needs_data
def test_sync_removes_a_containers_whole_subtree() -> None:
    """Deleting a container in draw.io must take its nested children with
    it, even though their own cell_ids never independently appear any more
    either."""
    person = fx.get(INDEX, "c4.person.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(b1, "B"):\n'
        '    c4.Person(inner, "Inner")\n'
        'c4.Person(user, "User")\n'
    )
    doc = fx.document(fx.entry_cell(person, cell_id="user", parent="1"))
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    # Both b1 and inner are individually gone -- each is its own removal root.
    assert plan.removed_vertex_count == 2
    assert merge.validate(synced) is None
    assert "b1" not in synced
    assert "inner" not in synced
    assert 'c4.Person(user, "User")' in synced.splitlines()


@needs_data
def test_sync_reparents_a_survivor_whose_old_container_was_removed() -> None:
    """A child cell that still exists in the .drawio, but whose OLD parent
    container is gone, must be re-derived fresh (re-parented per the
    CURRENT .drawio), not silently dropped just because its old declaration
    line was inside the removed container's block -- and it keeps its OWN
    established id doing so (not a freshly-minted one), the same identity
    convention every other survivor gets, so a later regenerate's geometry
    overlay can still find it by id."""
    person = fx.get(INDEX, "c4.person.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(b1, "B"):\n'
        '    c4.Person(inner, "Inner")\n'
    )
    # "inner" still exists, but now top-level (its container "b1" is gone).
    doc = fx.document(fx.entry_cell(person, cell_id="inner", parent="1"))
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert merge.validate(synced) is None
    document = parse_mdg(synced)
    assert isinstance(document, Document)
    by_id = {n.id: n for n in document.nodes}
    assert "b1" not in by_id
    assert by_id["inner"].parent_id is None


@needs_data
def test_sync_removes_an_edge_whose_pair_no_longer_exists() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        "c4.Rel(alice, bob, \"calls\")\n"
    )
    # Both vertices still drawn, but the connector between them is gone.
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
    )
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert plan.removed_edge_count == 1
    assert merge.validate(synced) is None
    assert 'c4.Person(alice, "Alice")' in synced.splitlines()
    assert 'c4.Person(bob, "Bob")' in synced.splitlines()
    assert not any(line.startswith("c4.Rel(") for line in synced.splitlines())


@needs_data
def test_sync_rewrites_a_surviving_edges_stale_token_when_its_endpoint_is_renamed() -> (
    None
):
    """A vertex whose own declaration was already lost (e.g. an earlier bug)
    but whose edge survives gets re-derived under a fresh id -- but the OLD
    edge line still names the now-undeclared id. It must be rewritten to
    the fresh id, not left dangling, or the very next forward-generate
    rejects it as a reference to nothing (the real bug reported against a
    user's diagram: 'edge ... references unknown cell')."""
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        '---\ntitle: "T"\nmode: layered\n---\n\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(orphan, bob, "calls")\n'
    )
    # "orphan"'s own cell is drawn (and resolves fine), but its .mdg
    # declaration was already lost -- only the edge line above still names it.
    doc = fx.document(
        fx.entry_cell(person, cell_id="orphan", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.edge_cell_xml("e1", source="orphan", target="bob", style=rel.style),
    )
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert merge.validate(synced) is None
    assert "orphan" not in synced
    document = parse_mdg(synced)
    assert isinstance(document, Document)
    by_id = {n.id: n for n in document.nodes}
    assert "bob" in by_id
    new_person = next(n for n in document.nodes if n.id != "bob")
    rel_lines = [line for line in synced.splitlines() if line.startswith("c4.Rel(")]
    assert rel_lines == [f'c4.Rel({new_person.id}, bob, "calls")']


@needs_data
def test_sync_normalizes_a_surviving_edge_id_in_drawio() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        "c4.Rel(alice, bob, \"custom hand-authored label\")\n"
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.edge_cell_xml("e1", source="alice", target="bob", style=rel.style),
    )
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert plan.removed_edge_count == 0
    assert plan.merge_plan.new_edge_count == 0
    assert 'c4.Rel(alice, bob, "custom hand-authored label")' in synced.splitlines()
    assert plan.drawio_id_renames == {"e1": "alice-bob"}

    _, second_plan, second_sync = _run_sync_pipeline(synced, doc)
    assert not second_plan.edge_token_rewrites
    assert second_sync == synced


@needs_data
def test_sync_rejects_a_second_edge_between_the_same_nodes() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        "c4.Rel(alice, bob, \"calls\")\n"
    )
    second_edge = (
        f'<mxCell id="e2" style="{rel.style}" edge="1" parent="1" '
        'source="alice" target="bob" value="emails">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.edge_cell_xml("e1", source="alice", target="bob", style=rel.style),
        second_edge,
    )
    with pytest.raises(ValueError, match="duplicate relationship alice -> bob"):
        _run_sync_pipeline(existing_text, doc)


@needs_data
def test_sync_rejects_existing_duplicate_relationships() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        "c4.Rel(alice, bob, \"calls\")\n"
        "c4.Rel(alice, bob, \"emails\")\n"
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.edge_cell_xml("e1", source="alice", target="bob", style=rel.style),
    )
    with pytest.raises(ValueError, match="duplicate relationship alice -> bob"):
        _run_sync_pipeline(existing_text, doc)


@needs_data
def test_sync_rejects_legacy_parallel_relationships() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "calls")\n'
        'c4.Rel(alice, bob, "emails")\n'
    )
    remaining = (
        f'<mxCell id="alice-&gt;bob-2" style="{rel.style}" edge="1" parent="1" '
        'source="alice" target="bob" value="emails">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        remaining,
    )

    with pytest.raises(ValueError, match="duplicate relationship alice -> bob"):
        _run_sync_pipeline(existing_text, doc)


@needs_data
def test_sync_updates_only_the_identity_matched_edge_label() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "old", edge_id="rel-1")\n'
    )
    changed = (
        f'<mxCell id="rel-1" style="{rel.style}" edge="1" parent="1" '
        'source="alice" target="bob" value="new">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        changed,
    )

    _, plan, synced = _run_sync_pipeline(existing_text, doc)

    assert plan.removed_edge_count == 0
    assert 'c4.Rel(alice, bob, "new")' in synced.splitlines()
    assert plan.drawio_id_renames == {"rel-1": "alice-bob"}


@needs_data
def test_sync_updates_identity_matched_edge_endpoints() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Person(carol, "Carol")\n'
        'c4.Rel(alice, bob, "calls", edge_id="rel-1")\n'
    )
    changed = (
        f'<mxCell id="rel-1" style="{rel.style}" edge="1" parent="1" '
        'source="alice" target="carol" value="calls">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.entry_cell(person, cell_id="carol", parent="1", x=600),
        changed,
    )

    _, plan, synced = _run_sync_pipeline(existing_text, doc)

    assert plan.removed_edge_count == 1
    assert plan.merge_plan.new_edge_count == 1
    assert 'c4.Rel(alice, carol, "calls")' in synced.splitlines()
    assert plan.drawio_id_renames == {"rel-1": "alice-carol"}


@needs_data
def test_sync_updates_identity_matched_edge_variant() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel_v2 = fx.get(INDEX, "c4.rel.v2")
    existing_text = (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "calls", edge_id="rel-1")\n'
    )
    changed = (
        f'<mxCell id="rel-1" style="{rel_v2.style}" edge="1" parent="1" '
        'mdgType="c4.Rel" mdgVariant="2" '
        'source="alice" target="bob" value="calls">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        changed,
    )

    _, plan, synced = _run_sync_pipeline(existing_text, doc)

    assert plan.removed_edge_count == 0
    assert (
        'c4.Rel(alice, bob, "calls", variant=2)'
        in synced.splitlines()
    )


@needs_data
def test_sync_updates_identity_matched_edge_type() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    optional_rel = fx.get(INDEX, "erd.optionalrel.v1")
    existing_text = (
        'use erd\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'erd.Rel(alice, bob, edge_id="rel-1")\n'
    )
    changed = (
        f'<mxCell id="rel-1" style="{optional_rel.style}" edge="1" parent="1" '
        'source="alice" target="bob">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        changed,
    )

    _, plan, synced = _run_sync_pipeline(existing_text, doc)

    assert plan.removed_edge_count == 0
    assert 'erd.OptionalRel(alice, bob)' in synced.splitlines()
    assert merge.validate(synced) is None


@needs_data
def test_sync_updates_identity_matched_edge_visibility() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "calls", edge_id="rel-1")\n'
    )
    changed = (
        f'<mxCell id="rel-1" style="{rel.style}" edge="1" parent="1" '
        'source="alice" target="bob" value="calls" visible="0">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        changed,
    )

    _, plan, synced = _run_sync_pipeline(existing_text, doc)

    assert plan.removed_edge_count == 0
    assert (
        'c4.Rel(alice, bob, "calls", visible=False)'
        in synced.splitlines()
    )


@needs_data
def test_sync_removes_an_edge_whose_endpoint_was_removed() -> None:
    """"bob" itself is gone -- the edge referencing it must go too, even
    though nothing else about the edge's pair changed."""
    person = fx.get(INDEX, "c4.person.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        "c4.Rel(alice, bob, \"calls\")\n"
    )
    doc = fx.document(fx.entry_cell(person, cell_id="alice", parent="1"))
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert plan.removed_vertex_count == 1
    assert plan.removed_edge_count == 1
    assert merge.validate(synced) is None
    assert "bob" not in synced
    assert not any(line.startswith("c4.Rel(") for line in synced.splitlines())


@needs_data
def test_sync_adds_and_removes_in_the_same_run() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),  # survives
        fx.entry_cell(person, cell_id="carol", parent="1", x=300),  # new
        # "bob" is simply absent -- removed.
    )
    _, plan, synced = _run_sync_pipeline(_TWO_SYSTEM_MDG, doc)
    assert plan.removed_vertex_count == 1
    assert plan.merge_plan.new_node_count == 1
    assert merge.validate(synced) is None
    assert 'c4.Person(alice, "Alice")' in synced.splitlines()
    assert "bob" not in synced
    document = parse_mdg(synced)
    assert isinstance(document, Document)
    assert len(document.nodes) == 2  # alice (kept) + carol (new); bob gone


@needs_data
def test_sync_reparents_a_survivor_dragged_into_a_new_container() -> None:
    """A cell whose OWN id is unchanged, but that the user visually moved
    into a container in draw.io, must have its .mdg declaration re-nested
    to match -- otherwise its stale top-level declaration silently
    disagrees with where it actually sits, and a later plain regenerate's
    overlay applies its now-container-relative geometry as if it were
    still page-absolute, visibly misplacing it. Its existing edge must
    survive completely untouched -- reparenting is not removal."""
    person = fx.get(INDEX, "c4.person.v1")
    boundary = fx.get(INDEX, "c4.system_boundary.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        '---\ntitle: "T"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "custom hand-authored label")\n'
    )
    doc = fx.document(
        fx.entry_cell(boundary, cell_id="box1", parent="1"),
        fx.entry_cell(person, cell_id="alice", parent="box1"),  # now inside box1
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.edge_cell_xml("e1", source="alice", target="bob", style=rel.style),
    )
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert merge.validate(synced) is None
    # The edge's exact existing text must be preserved, not removed+recreated.
    assert 'c4.Rel(alice, bob, "custom hand-authored label")' in synced.splitlines()
    assert plan.removed_edge_count == 0

    document = parse_mdg(synced)
    assert isinstance(document, Document)
    by_id = {n.id: n for n in document.nodes}
    assert by_id["bob"].parent_id is None
    box = next(n for n in document.nodes if n.type == "c4.System_Boundary")
    assert by_id["alice"].parent_id == box.id


@needs_data
def test_sync_leaves_an_unmoved_survivor_completely_untouched() -> None:
    """A cell whose container is unchanged must not be touched at all --
    reparent detection must not misfire on the common case."""
    person = fx.get(INDEX, "c4.person.v1")
    boundary = fx.get(INDEX, "c4.system_boundary.v1")
    existing_text = (
        '---\ntitle: "T"\nmode: layered\n---\n\n'
        'c4.System_Boundary(box1, "Box"):\n'
        '    c4.Person(alice, "Alice")\n'
    )
    doc = fx.document(
        fx.entry_cell(boundary, cell_id="box1", parent="1"),
        fx.entry_cell(person, cell_id="alice", parent="box1"),
    )
    _, plan, synced = _run_sync_pipeline(existing_text, doc)
    assert plan.merge_plan.new_node_count == 0
    assert plan.removed_vertex_count == 0
    assert synced.rstrip() == existing_text.rstrip()


@needs_data
def test_sync_updates_a_surviving_nodes_label_without_losing_other_arguments() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    existing_text = (
        '---\ntitle: "T"\nmode: layered\n---\n\n'
        'c4.Person(alice, "Old label", "Keep description", technology="web") '
        '# keep comment\n'
    )
    labeled_cell = (
        '<object id="alice" c4Name="New label"><mxCell style="'
        f'{person.style}" vertex="1" parent="1">'
        '<mxGeometry x="0" y="0" width="120" height="120" as="geometry"/>'
        '</mxCell></object>'
    )

    _, plan, synced = _run_sync_pipeline(existing_text, fx.document(labeled_cell))

    assert len(plan.node_label_rewrites) == 1
    assert plan.merge_plan.new_node_count == 0
    assert plan.removed_vertex_count == 0
    assert (
        'c4.Person(alice, "New label", "Keep description", technology="web") '
        '# keep comment'
    ) in synced.splitlines()
    assert merge.validate(synced) is None


@needs_data
def test_sync_updates_a_label_containing_an_equals_sign() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    existing_text = (
        '---\ntitle: "T"\nmode: layered\n---\n\n'
        'c4.Person(alice, "status=old", "Keep description")\n'
    )
    labeled_cell = (
        '<object id="alice" c4Name="status=new"><mxCell style="'
        f'{person.style}" vertex="1" parent="1">'
        '<mxGeometry x="0" y="0" width="120" height="120" as="geometry"/>'
        '</mxCell></object>'
    )

    _, _, synced = _run_sync_pipeline(existing_text, fx.document(labeled_cell))

    assert 'c4.Person(alice, "status=new", "Keep description")' in synced
    assert merge.validate(synced) is None


@needs_data
def test_sync_reports_nothing_to_sync_when_nothing_changed() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    system = fx.get(INDEX, "c4.system.v1")
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(system, cell_id="bob", parent="1", x=300),
    )
    _, plan, synced = _run_sync_pipeline(_TWO_SYSTEM_MDG, doc)
    assert plan.removed_vertex_count == 0
    assert plan.merge_plan.new_node_count == 0
    assert synced.rstrip() == _TWO_SYSTEM_MDG.rstrip()


# ── CLI (dry-run / --write / abort safety) ───────────────────────────────────
def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@needs_data
def test_sync_cli_dry_run_leaves_the_file_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(fx.entry_cell(person, cell_id="alice", parent="1"))
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    rc = sync_main([str(mdg_path), str(drawio_path)])
    assert rc == 0
    assert mdg_path.read_text(encoding="utf-8") == _TWO_SYSTEM_MDG
    out = capsys.readouterr().out
    assert "removed element" in out
    assert "dry run" in out


@needs_data
def test_sync_cli_write_applies_the_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(fx.entry_cell(person, cell_id="alice", parent="1"))
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    rc = sync_main([str(mdg_path), str(drawio_path), "--write"])
    assert rc == 0
    written = mdg_path.read_text(encoding="utf-8")
    assert "bob" not in written
    assert merge.validate(written) is None
    assert "wrote" in capsys.readouterr().out


@needs_data
def test_sync_cli_write_applies_a_drawio_label_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    labeled_cell = (
        '<object id="alice" c4Name="Renamed in draw.io"><mxCell style="'
        f'{person.style}" vertex="1" parent="1">'
        '<mxGeometry x="0" y="0" width="120" height="120" as="geometry"/>'
        '</mxCell></object>'
    )
    mdg_text = '---\ntitle: "T"\nmode: layered\n---\n\nc4.Person(alice, "Alice")\n'
    mdg_path = _write(tmp_path, "existing.mdg", mdg_text)
    drawio_path = _write(tmp_path, "diagram.drawio", fx.document(labeled_cell))

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0

    written = mdg_path.read_text(encoding="utf-8")
    assert 'c4.Person(alice, "Renamed in draw.io")' in written
    assert "1 updated label(s)" in capsys.readouterr().out
    assert merge.validate(written) is None


@needs_data
def test_sync_cli_write_renames_the_new_cells_drawio_id_to_match(
    tmp_path: Path,
) -> None:
    """A cell drawn directly in draw.io (a raw, non-semantic id, never
    round-tripped through this tool before) must have its .drawio id
    renamed to the fresh semantic id sync just minted for it in the .mdg --
    an already-existing cell's id (already matching) is left untouched."""
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),  # already exists
        fx.entry_cell(person, cell_id="raw123", parent="1", x=300),  # new
    )
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0

    document = parse_mdg(mdg_path.read_text(encoding="utf-8"))
    assert isinstance(document, Document)
    new_id = next(n.id for n in document.nodes if n.id != "alice")

    drawio_root = ET.parse(str(drawio_path)).getroot()
    ids = {el.get("id") for el in drawio_root.iter() if el.get("id")}
    assert new_id in ids, "new cell's .drawio id not renamed to match"
    assert "raw123" not in ids
    assert "alice" in ids, "an already-represented cell's id must be left alone"


@needs_data
def test_sync_cli_derives_edge_id_and_removes_legacy_mdg_keyword(
    tmp_path: Path,
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    edge = (
        f'<mxCell id="random-edge" style="{rel.style}" edge="1" parent="1" '
        'source="alice" target="bob" value="calls">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
        '<mxCell id="edge-label" parent="random-edge" value="label">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        edge,
    )
    mdg_text = (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "calls", edge_id="random-edge")\n'
    )
    mdg_path = _write(tmp_path, "existing.mdg", mdg_text)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0

    assert mdg_path.read_text(encoding="utf-8") == (
        'c4.Person(alice, "Alice")\n'
        'c4.Person(bob, "Bob")\n'
        'c4.Rel(alice, bob, "calls")\n'
    )
    root = ET.parse(drawio_path).getroot()
    assert any(cell.get("id") == "alice-bob" for cell in root.iter())
    assert not any(cell.get("id") == "random-edge" for cell in root.iter())
    label = next(cell for cell in root.iter() if cell.get("id") == "edge-label")
    assert label.get("parent") == "alice-bob"


@needs_data
def test_sync_cli_rejects_duplicate_drawio_relationships(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(person, cell_id="bob", parent="1", x=300),
        fx.edge_cell_xml("edge-1", source="alice", target="bob", style=rel.style),
        fx.edge_cell_xml("edge-2", source="alice", target="bob", style=rel.style),
    )
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 1
    assert "duplicate relationship alice -> bob in draw.io" in capsys.readouterr().err
    assert mdg_path.read_text(encoding="utf-8") == _TWO_SYSTEM_MDG


def _geometry(root: ET.Element, cell_id: str) -> ET.Element:
    """The ``<mxGeometry>`` for *cell_id*, unwrapping object/UserObject --
    the forward generator (unlike sync's fixtures) wraps a c4.Person cell,
    putting its id on the wrapper and its geometry on the inner mxCell."""
    for el in root.iter():
        if el.get("id") == cell_id:
            mx = el if el.tag == "mxCell" else el.find("mxCell")
            assert mx is not None
            geo = mx.find("mxGeometry")
            assert geo is not None
            return geo
    raise AssertionError(f"cell {cell_id!r} not found")


@needs_data
def test_sync_write_lets_a_later_plain_regenerate_keep_the_new_cells_layout(
    tmp_path: Path,
) -> None:
    """End-to-end regression: without the id rename above, a cell's .drawio
    id (``raw123``) and its fresh .mdg id (whatever sync minted) disagree,
    so a later PLAIN regenerate's geometry overlay -- which matches a node
    by id -- can never find that cell again. Any manual position given to
    it after sync would be silently discarded on the next regenerate
    instead of preserved, exactly like every other node's already is."""
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(fx.entry_cell(person, cell_id="raw123", parent="1"))
    mdg_path = _write(
        tmp_path, "existing.mdg", '---\ntitle: "T"\nmode: layered\n---\n\n'
    )
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0
    document = parse_mdg(mdg_path.read_text(encoding="utf-8"))
    assert isinstance(document, Document)
    new_id = document.nodes[0].id

    # Simulate a manual move in draw.io.
    tree = ET.parse(str(drawio_path))
    geo = _geometry(tree.getroot(), new_id)
    geo.set("x", "777")
    geo.set("y", "666")
    tree.write(str(drawio_path), encoding="utf-8")

    # A plain regenerate (no --force) reads the overlay from *drawio_path*.
    assert convert_main([str(mdg_path), str(drawio_path)]) == 0

    moved = _geometry(ET.parse(str(drawio_path)).getroot(), new_id)
    assert float(moved.get("x", "0")) == 777.0, "moved x not preserved"
    assert float(moved.get("y", "0")) == 666.0, "moved y not preserved"


@needs_data
def test_sync_cli_write_aborts_when_result_would_be_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(fx.entry_cell(person, cell_id="alice", parent="1"))
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)
    monkeypatch.setattr(merge, "validate", lambda text: "forced failure for the test")

    rc = sync_main([str(mdg_path), str(drawio_path), "--write"])
    assert rc == 1
    assert mdg_path.read_text(encoding="utf-8") == _TWO_SYSTEM_MDG  # untouched
    assert "forced failure" in capsys.readouterr().err


@needs_data
def test_sync_cli_reports_nothing_to_sync_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person = fx.get(INDEX, "c4.person.v1")
    system = fx.get(INDEX, "c4.system.v1")
    doc = fx.document(
        fx.entry_cell(person, cell_id="alice", parent="1"),
        fx.entry_cell(system, cell_id="bob", parent="1", x=300),
    )
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(tmp_path, "diagram.drawio", doc)

    rc = sync_main([str(mdg_path), str(drawio_path)])
    assert rc == 0
    assert mdg_path.read_text(encoding="utf-8") == _TWO_SYSTEM_MDG
    assert "nothing to sync" in capsys.readouterr().out


_TWO_PAGE_MDG = (
    '---\ntitle: "T"\npage: "1. First"\nmode: layered\n---\n\n'
    'c4.System_Boundary(box1, "Box 1"):\n'
    '    c4.Person(alice, "Alice")\n\n'
    '---\ntitle: "T"\npage: "2. Second"\nmode: layered\n---\n\n'
    'c4.System_Boundary(box2, "Box 2"):\n'
    '    c4.Person(bob, "Bob")\n'
)


@needs_data
def test_sync_cli_multi_page_unmodified_diagram_is_a_no_op(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression for a multi-page ``.drawio``'s cell ids being page-prefixed
    (``"N:..."``, see ``derive._page_prefix``) while ``.mdg`` text never is:
    every comparison between the two must strip that prefix first, or every
    already-declared node looks gone and every current cell looks brand new.
    Round-tripping an untouched multi-page diagram through convert -> sync
    must report nothing to do, not silently collapse both pages' content
    into one under freshly-minted ids."""
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_PAGE_MDG)
    drawio_path = tmp_path / "diagram.drawio"
    assert convert_main([str(mdg_path), str(drawio_path)]) == 0

    rc = sync_main([str(mdg_path), str(drawio_path)])
    assert rc == 0
    assert mdg_path.read_text(encoding="utf-8") == _TWO_PAGE_MDG
    assert "nothing to sync" in capsys.readouterr().out


@needs_data
def test_sync_cli_multi_page_new_cell_nests_under_its_own_pages_container(
    tmp_path: Path,
) -> None:
    """A cell hand-drawn inside page 2's container must be added nested
    under THAT container, on page 2 -- page 1 must survive untouched. Before
    the page-prefix fix, every survivor misresolved as removed and every
    new/survivor alike was re-added at the physical end of the file, so
    this collapsed both pages into whichever came last."""
    person = fx.get(INDEX, "c4.person.v1")
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_PAGE_MDG)
    drawio_path = tmp_path / "diagram.drawio"
    assert convert_main([str(mdg_path), str(drawio_path)]) == 0

    root = ET.parse(drawio_path).getroot()
    model = root.findall("diagram")[1].find("mxGraphModel")
    assert model is not None
    page_two_root = model.find("root")
    assert page_two_root is not None
    new_cell = ET.fromstring(
        fx.entry_cell(person, cell_id="carol", parent="box2", x=300)
    )
    page_two_root.append(new_cell)
    ET.ElementTree(root).write(drawio_path, encoding="unicode")

    rc = sync_main([str(mdg_path), str(drawio_path), "--write"])
    assert rc == 0
    synced = mdg_path.read_text(encoding="utf-8")
    assert merge.validate(synced) is None

    page_one, page_two = synced.split('page: "2. Second"')
    # Page 1 survives byte-for-byte -- nothing "removed and re-added" onto it.
    assert page_one == _TWO_PAGE_MDG.split('page: "2. Second"')[0]
    assert 'c4.Person(bob, "Bob")' in page_two  # page 2's own survivor, untouched

    document = parse_mdg(synced)
    assert isinstance(document, MultiPageDocument)
    page1, page2 = document.pages
    assert {n.id for n in page1.nodes} == {"box1", "alice"}  # untouched
    by_id = {n.id: n for n in page2.nodes}
    new_ids = set(by_id) - {"box2", "bob"}
    assert len(new_ids) == 1
    (new_id,) = new_ids
    assert by_id[new_id].parent_id == "box2"  # nested under page 2's container
    new_line = next(line for line in page_two.splitlines() if new_id in line)
    assert new_line.startswith("    ")  # indented, not top-level


def test_sync_cli_returns_error_when_no_style_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        StyleIndex, "load", classmethod(lambda cls, data_dir=None: StyleIndex([]))
    )
    mdg_path = _write(tmp_path, "existing.mdg", _TWO_SYSTEM_MDG)
    drawio_path = _write(
        tmp_path, "diagram.drawio", "<mxGraphModel><root/></mxGraphModel>"
    )
    rc = sync_main([str(mdg_path), str(drawio_path)])
    assert rc == 2
    assert "make build-data" in capsys.readouterr().err


_ERD_TABLE_MDG = (
    "use erd\n\n"
    'erd.Table(t1, "Entity"):\n'
    '    erd.RowKey(t1_pk, "id", key="PK")\n'
    '    erd.Row(t1_col, "name")\n'
    '    erd.RowKey(t1_fk, "other_id", key="FK")\n'
)


def _cell_by_id(root: ET.Element, cell_id: str) -> ET.Element:
    return next(c for c in root.iter("mxCell") if c.get("id") == cell_id)


@needs_data
def test_sync_cli_keeps_unmodified_erd_table_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: an erd Table's Row/RowKey cells (and their key/text
    sub-cells) never resolved to a shape, so sync saw every row as moved out
    of its table and deleted it from the ``.mdg``, reporting each sub-cell
    as "could not derive a shape". An untouched round trip must be a no-op."""
    mdg_path = _write(tmp_path, "existing.mdg", _ERD_TABLE_MDG)
    drawio_path = tmp_path / "diagram.drawio"
    assert convert_main([str(mdg_path), str(drawio_path)]) == 0

    rc = sync_main([str(mdg_path), str(drawio_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "could not derive" not in captured.err
    assert "nothing to sync" in captured.out
    assert mdg_path.read_text(encoding="utf-8") == _ERD_TABLE_MDG


@needs_data
def test_sync_cli_applies_erd_row_text_and_key_edits(tmp_path: Path) -> None:
    """A row's text and key tag live in its sub-cells, not on the row cell."""
    mdg_path = _write(tmp_path, "existing.mdg", _ERD_TABLE_MDG)
    drawio_path = tmp_path / "diagram.drawio"
    assert convert_main([str(mdg_path), str(drawio_path)]) == 0

    tree = ET.parse(drawio_path)
    _cell_by_id(tree.getroot(), "t1_col__c1").set("value", "full_name")
    _cell_by_id(tree.getroot(), "t1_fk__c0").set("value", "FK1")
    tree.write(drawio_path, encoding="unicode")

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0
    synced = mdg_path.read_text(encoding="utf-8")
    assert '    erd.Row(t1_col, "full_name")\n' in synced
    assert '    erd.RowKey(t1_fk, "other_id", key="FK1")\n' in synced
    assert '    erd.RowKey(t1_pk, "id", key="PK")\n' in synced


@needs_data
def test_sync_cli_adds_a_hand_drawn_erd_row_inside_its_table(tmp_path: Path) -> None:
    """A row copied in draw.io lands nested under its table, typed by its key
    tag (RowKey with one, Row without), never as stray sub-cell nodes."""
    mdg_path = _write(tmp_path, "existing.mdg", _ERD_TABLE_MDG)
    drawio_path = tmp_path / "diagram.drawio"
    assert convert_main([str(mdg_path), str(drawio_path)]) == 0

    tree = ET.parse(drawio_path)
    page_root = next(tree.getroot().iter("root"))
    for suffix, parent, value in (
        ("", "t1", None),
        ("__c0", "hand", "FK2"),
        ("__c1", "hand", "third_id"),
    ):
        copied = ET.fromstring(
            ET.tostring(_cell_by_id(tree.getroot(), f"t1_fk{suffix}"))
        )
        copied.set("id", f"hand{suffix}")
        copied.set("parent", parent)
        if value is not None:
            copied.set("value", value)
        page_root.append(copied)
    tree.write(drawio_path, encoding="unicode")

    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0
    synced = mdg_path.read_text(encoding="utf-8")
    assert merge.validate(synced) is None
    document = parse_mdg(synced)
    assert isinstance(document, Document)
    by_id = {n.id: n for n in document.nodes}
    new_ids = set(by_id) - {"t1", "t1_pk", "t1_col", "t1_fk"}
    assert len(new_ids) == 1
    (new_id,) = new_ids
    assert by_id[new_id].parent_id == "t1"
    assert f'    erd.RowKey({new_id}, "third_id", key="FK2")' in synced


def test_rewrite_node_label_line_leaves_a_block_reference_label_alone() -> None:
    """Replacing a ``block`` reference with an inline string would silently
    detach the node from its block."""
    line = "general.Text(legend, legend_text)"
    assert merge._rewrite_node_label_line(line, line, "inline text") is None
    kwarg_line = "general.Text(legend, label=legend_text)"
    assert merge._rewrite_node_label_line(kwarg_line, kwarg_line, "x") is None
