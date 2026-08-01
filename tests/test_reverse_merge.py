"""Tests for merging new cells into an existing ``.mdg`` (:mod:`scripts.reverse.merge`).

Three groups:

* pure text-indexing/rendering unit tests (no data) -- ``index_existing``,
  insertion-point/indent detection, colon-fix detection, and
  ``render_merge``'s splice/append/colon-fix mechanics, independent of any
  real registry data;
* data-gated end-to-end tests against the real C4 registry and palette --
  every insertion scenario (append to a container with/without existing
  children, a brand-new nested subtree, dedup of already-represented cells,
  skipped/unresolved cells, edge counting, label extraction, reserved-name
  collision avoidance) and a re-parse validation guard. These skip without
  ``make build-data``;
* CLI tests (``scripts.reverse.merge_cli``) -- the dry-run/--write/abort
  safety contract, via a monkeypatched index.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mdg_drawio.contracts import Document
from mdg_drawio.notation import parse as parse_mdg
from scripts.reverse import fixtures as fx
from scripts.reverse import merge
from scripts.reverse.containment import resolve_containment
from scripts.reverse.derive import derive, load_cells, parent_map
from scripts.reverse.merge import ExistingIndex, Insertion, MergePlan
from scripts.reverse.merge_cli import main as merge_main
from scripts.reverse.naming import assign_semantic_ids, reserved_counters
from scripts.reverse.style_index import StyleIndex

INDEX = StyleIndex.load()
needs_data = pytest.mark.skipif(
    not INDEX.entries, reason="needs palette styles (run `make build-data`)"
)


# ── text indexing / rendering mechanics (no data) ────────────────────────────
def test_index_existing_finds_node_ids_and_indent() -> None:
    text = (
        'c4.System_Boundary(sys1, "X"):\n'
        '    c4.Container(web1, "Y")\n'
    )
    existing = merge.index_existing(text)
    assert existing.node_line == {"sys1": 0, "web1": 1}
    assert existing.node_indent == {"sys1": "", "web1": "    "}


def test_index_existing_keeps_first_on_duplicate_id() -> None:
    text = 'c4.Person(dup, "A")\nc4.Person(dup, "B")\n'
    existing = merge.index_existing(text)
    assert existing.node_line == {"dup": 0}


def test_first_arg_token_bare_and_quoted() -> None:
    assert merge._first_arg_token('sys1, "label"') == "sys1"
    assert merge._first_arg_token('"550e8400-e29b", "label"') == "550e8400-e29b"


def test_first_arg_token_respects_quoting_around_commas() -> None:
    # A comma inside the (quoted) label must not be mistaken for the arg split.
    assert merge._first_arg_token('n1, "a, b, c"') == "n1"


def test_first_arg_token_single_arg_no_comma() -> None:
    assert merge._first_arg_token("solo") == "solo"


def test_child_insertion_mirrors_existing_sibling_indent() -> None:
    text = (
        "box(b1):\n"
        "        leaf(c1)\n"  # unusually 8-space indented, hand-authored
    )
    existing = merge.index_existing(text)
    line, indent = merge._child_insertion(existing, "b1")
    assert line == 2
    assert indent == "        "


def test_child_insertion_falls_back_to_container_indent_plus_step_when_empty() -> None:
    text = "box(b1):\n"
    existing = merge.index_existing(text)
    line, indent = merge._child_insertion(existing, "b1")
    assert line == 1
    assert indent == merge.INDENT_STEP


def test_child_insertion_skips_blank_lines_within_the_block() -> None:
    text = "box(b1):\n    leaf(c1)\n\n    leaf(c2)\nafter(x)\n"
    existing = merge.index_existing(text)
    line, _ = merge._child_insertion(existing, "b1")
    assert line == 4  # right before "after(x)", past the blank line


def test_needs_colon_true_for_a_childless_container() -> None:
    existing = merge.index_existing("box(b1)\n")
    assert merge._needs_colon(existing, "b1") is True


def test_needs_colon_false_when_container_already_has_children() -> None:
    existing = merge.index_existing("box(b1):\n    leaf(c1)\n")
    assert merge._needs_colon(existing, "b1") is False


def test_needs_colon_false_when_line_already_ends_in_colon() -> None:
    existing = merge.index_existing("box(b1):\n")
    assert merge._needs_colon(existing, "b1") is False


def test_render_merge_splices_and_fixes_colon() -> None:
    existing = merge.index_existing("box(b1)\n")
    plan = MergePlan([Insertion(1, "    leaf(c1)", colon_fix_line=0)], [], 0, 1)
    out = merge.render_merge(existing, plan)
    assert out == "box(b1):\n    leaf(c1)\n"


def test_render_merge_top_level_append_adds_blank_line_separator() -> None:
    existing = merge.index_existing('box(b1, "X")\n')
    plan = MergePlan([Insertion(1, 'box(b2, "Y")', top_level=True)], [], 0, 1)
    out = merge.render_merge(existing, plan)
    assert out == 'box(b1, "X")\n\nbox(b2, "Y")\n'


def test_render_merge_top_level_append_no_extra_blank_after_blank_line() -> None:
    existing = merge.index_existing("box(b1)\n\n")
    plan = MergePlan([Insertion(2, "box(b2)", top_level=True)], [], 0, 1)
    out = merge.render_merge(existing, plan)
    assert out.count("\n\n") == 1  # no doubled-up blank line


def test_validate_accepts_a_minimal_valid_document() -> None:
    text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.Person(n1, "A")\n'
    )
    assert merge.validate(text) is None


def test_validate_reports_an_error_for_garbage_input() -> None:
    error = merge.validate("this is not a valid mdg document {{{")
    assert error is not None


# ── end-to-end (real registry + palette) ─────────────────────────────────────
def _run_pipeline(
    existing_text: str, drawio_doc: str
) -> tuple[ExistingIndex, MergePlan, str]:
    existing = merge.index_existing(existing_text)
    cells = load_cells(drawio_doc)
    result = derive(cells, INDEX)
    reserved = reserved_counters(existing.node_ids())
    node_ids = {s.cell_id: s.node_id for s in assign_semantic_ids(result, reserved)}
    raw_cells = parent_map(drawio_doc)
    containments = {
        c.cell_id: c for c in resolve_containment(result, raw_cells, node_ids)
    }
    plan = merge.plan_merge(existing, cells, result, node_ids, containments, raw_cells)
    merged = merge.render_merge(existing, plan)
    return existing, plan, merged


_BASE_MDG = (
    '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
    'c4.System_Boundary(sys1, "Mitt system"):\n'
    '    c4.Container(web1, "Webbapp")\n'
)


@needs_data
def test_merge_appends_new_child_to_container_with_existing_children() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="99", parent="sys1"),
    )
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_node_count == 1
    assert not plan.skipped
    assert merge.validate(merged) is None
    assert '    c4.Person(person1)' in merged.splitlines()
    assert "web1" in merged  # untouched existing content survives


@needs_data
def test_merge_adds_colon_to_a_previously_childless_container() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    component = fx.get(INDEX, "c4.component.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(sys1, "Empty system")\n'
    )
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(component, cell_id="99", parent="sys1"),
    )
    _, plan, merged = _run_pipeline(existing_text, doc)
    assert plan.new_node_count == 1
    lines = merged.splitlines()
    assert 'c4.System_Boundary(sys1, "Empty system"):' in lines
    assert merge.validate(merged) is None


@needs_data
def test_merge_new_top_level_container_with_new_nested_children() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", x=0, parent="1"),
        fx.entry_cell(sys_b, cell_id="sys_new", x=300, parent="1"),
        fx.entry_cell(person, cell_id="p_new", x=300, parent="sys_new"),
    )
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_node_count == 2
    assert merge.validate(merged) is None

    document = parse_mdg(merged)
    assert isinstance(document, Document)
    by_id = {n.id: n for n in document.nodes}
    new_container = next(
        n
        for n in document.nodes
        if n.id not in ("sys1", "web1") and n.parent_id is None
    )
    person_node = next(n for n in document.nodes if n.parent_id == new_container.id)
    assert person_node.id in by_id


@needs_data
def test_merge_skips_a_cell_already_represented_in_the_existing_file() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    container = fx.get(INDEX, "c4.container.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(container, cell_id="web1", parent="sys1"),  # already exists
    )
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_node_count == 0
    assert merged.rstrip() == _BASE_MDG.rstrip()


@needs_data
def test_merge_skips_and_reports_an_unresolved_cell() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    junk = fx.cell_xml(
        "junk", "totallyunrelatedtoken=xyz;", x=0, width=10, height=10, parent="sys1"
    )
    doc = fx.document(fx.entry_cell(sys_b, cell_id="sys1", parent="1"), junk)
    _, plan, _ = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_node_count == 0
    assert any("junk" in s for s in plan.skipped)


@needs_data
def test_merge_counts_new_edges_without_emitting_them() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="p1", parent="sys1"),
        fx.entry_cell(person, cell_id="p2", parent="sys1"),
        fx.edge_cell_xml("e1", source="p1", target="p2", parent="1"),
    )
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_edge_count == 1
    assert "Rel" not in merged


@needs_data
def test_merge_label_prefers_c4name_over_placeholder_value() -> None:
    person = fx.get(INDEX, "c4.person.v1")
    labeled = (
        '<object id="p_new" c4Name="Alice"><mxCell style="'
        f'{person.style}" vertex="1" parent="sys1">'
        '<mxGeometry x="0" y="0" width="120" height="120" as="geometry"/>'
        "</mxCell></object>"
    )
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    doc = fx.document(fx.entry_cell(sys_b, cell_id="sys1", parent="1"), labeled)
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert 'c4.Person(person1, "Alice")' in merged


@needs_data
def test_merge_reserved_counters_avoid_colliding_with_existing_names() -> None:
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(sys1, "X"):\n'
        '    c4.Person(person1, "Existing person")\n'
    )
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="new_person", parent="sys1"),
    )
    _, plan, merged = _run_pipeline(existing_text, doc)
    assert "c4.Person(person2)" in merged.splitlines()[-1] or any(
        "person2" in line for line in merged.splitlines()
    )
    assert merge.validate(merged) is None


@needs_data
def test_merge_multiple_new_cells_at_different_anchors() -> None:
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(sysA, "A"):\n'
        '    c4.Container(a1, "A1")\n\n'
        'c4.System_Boundary(sysB, "B"):\n'
        '    c4.Container(b1, "B1")\n'
    )
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sysA", x=0, parent="1"),
        fx.entry_cell(sys_b, cell_id="sysB", x=300, parent="1"),
        fx.entry_cell(person, cell_id="pA", x=0, parent="sysA"),
        fx.entry_cell(person, cell_id="pB", x=300, parent="sysB"),
    )
    _, plan, merged = _run_pipeline(existing_text, doc)
    assert plan.new_node_count == 2
    assert merge.validate(merged) is None

    document = parse_mdg(merged)
    assert isinstance(document, Document)
    parents = {n.id: n.parent_id for n in document.nodes}
    new_ids = [n for n in parents if n not in ("sysA", "sysB", "a1", "b1")]
    assert len(new_ids) == 2
    assert {parents[n] for n in new_ids} == {"sysA", "sysB"}


# ── CLI (dry-run / --write / abort safety) ───────────────────────────────────
def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@needs_data
def test_merge_cli_dry_run_leaves_the_file_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="99", parent="sys1"),
    )
    mdg_path = _write(tmp_path, "existing.mdg", _BASE_MDG)
    drawio_path = _write(tmp_path, "new.drawio", doc)

    rc = merge_main([str(mdg_path), str(drawio_path)])
    assert rc == 0
    assert mdg_path.read_text(encoding="utf-8") == _BASE_MDG
    out = capsys.readouterr().out
    assert "person1" in out
    assert "dry run" in out


@needs_data
def test_merge_cli_write_applies_the_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="99", parent="sys1"),
    )
    mdg_path = _write(tmp_path, "existing.mdg", _BASE_MDG)
    drawio_path = _write(tmp_path, "new.drawio", doc)

    rc = merge_main([str(mdg_path), str(drawio_path), "--write"])
    assert rc == 0
    written = mdg_path.read_text(encoding="utf-8")
    assert "c4.Person(person1)" in written
    assert merge.validate(written) is None
    assert "wrote 1 new element" in capsys.readouterr().out


@needs_data
def test_merge_cli_write_aborts_when_result_would_be_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="99", parent="sys1"),
    )
    mdg_path = _write(tmp_path, "existing.mdg", _BASE_MDG)
    drawio_path = _write(tmp_path, "new.drawio", doc)
    monkeypatch.setattr(merge, "validate", lambda text: "forced failure for the test")

    rc = merge_main([str(mdg_path), str(drawio_path), "--write"])
    assert rc == 1
    assert mdg_path.read_text(encoding="utf-8") == _BASE_MDG  # untouched
    assert "forced failure" in capsys.readouterr().err


@needs_data
def test_merge_cli_reports_nothing_new_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    container = fx.get(INDEX, "c4.container.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(container, cell_id="web1", parent="sys1"),
    )
    mdg_path = _write(tmp_path, "existing.mdg", _BASE_MDG)
    drawio_path = _write(tmp_path, "new.drawio", doc)

    rc = merge_main([str(mdg_path), str(drawio_path)])
    assert rc == 0
    assert mdg_path.read_text(encoding="utf-8") == _BASE_MDG
    assert "nothing new to merge" in capsys.readouterr().out


def test_merge_cli_returns_error_when_no_style_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        StyleIndex, "load", classmethod(lambda cls, data_dir=None: StyleIndex([]))
    )
    mdg_path = _write(tmp_path, "existing.mdg", _BASE_MDG)
    drawio_path = _write(tmp_path, "new.drawio", "<mxGraphModel><root/></mxGraphModel>")
    rc = merge_main([str(mdg_path), str(drawio_path)])
    assert rc == 2
    assert "make build-data" in capsys.readouterr().err
