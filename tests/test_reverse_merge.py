"""Tests for merging new cells into an existing ``.mdg``
(:mod:`mdg_drawio.reverse.merge`).

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
* CLI tests (``mdg_drawio.reverse.merge_cli``) -- the dry-run/--write/abort
  safety contract, via a monkeypatched index.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pytest

from mdg_drawio.contracts import Document
from mdg_drawio.notation import parse as parse_mdg
from mdg_drawio.reverse import fixtures as fx
from mdg_drawio.reverse import merge
from mdg_drawio.reverse.containment import Containment, resolve_containment
from mdg_drawio.reverse.derive import (
    Candidate,
    Cell,
    CellResult,
    DocumentResult,
    RawCell,
    derive,
    load_cells,
    parent_map,
)
from mdg_drawio.reverse.merge import ExistingIndex, Insertion, MergePlan
from mdg_drawio.reverse.merge_cli import main as merge_main
from mdg_drawio.reverse.naming import assign_semantic_ids, reserved_counters
from mdg_drawio.reverse.style_index import StyleIndex

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


def test_index_existing_does_not_treat_an_edges_line_as_a_vertex_declaration() -> None:
    """Regression: a vertex whose OWN declaration is lost (e.g. to an
    earlier bug) but whose edges survive must not look "already
    represented" forever, keyed off the edge line's own first argument --
    sync could then never re-derive and restore the missing vertex."""
    text = 'erd.ZeroToManyMandOne(orphan, e02, "omfattar")\nerd.EntityRect(e02, "B")\n'
    existing = merge.index_existing(text)
    assert "orphan" not in existing.node_ids()
    assert "e02" in existing.node_ids()


def test_index_existing_is_lenient_for_an_unregistered_namespace() -> None:
    """No registry to check against (an unknown/foreign namespace) -- keeps
    the existing, lenient generic-node treatment rather than erroring."""
    text = 'foreignlib.Whatever(x1, "A")\n'
    existing = merge.index_existing(text)
    assert "x1" in existing.node_ids()


def test_is_edge_call_true_for_a_registered_edge_false_for_a_vertex() -> None:
    assert merge._is_edge_call("erd", "ZeroToManyMandOne") is True
    assert merge._is_edge_call("erd", "EntityRect") is False
    assert merge._is_edge_call(None, "EntityRect") is False
    assert merge._is_edge_call("erd", "NotARealFunction") is False


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
    plan = MergePlan([Insertion(1, "    leaf(c1)", colon_fix_line=0)], [], 0, 1, {})
    out = merge.render_merge(existing, plan)
    assert out == "box(b1):\n    leaf(c1)\n"


def test_render_merge_top_level_append_adds_blank_line_separator() -> None:
    existing = merge.index_existing('box(b1, "X")\n')
    plan = MergePlan([Insertion(1, 'box(b2, "Y")', top_level=True)], [], 0, 1, {})
    out = merge.render_merge(existing, plan)
    assert out == 'box(b1, "X")\n\nbox(b2, "Y")\n'


def test_render_merge_top_level_append_no_extra_blank_after_blank_line() -> None:
    existing = merge.index_existing("box(b1)\n\n")
    plan = MergePlan([Insertion(2, "box(b2)", top_level=True)], [], 0, 1, {})
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


# ── robustness fixes (no data) ────────────────────────────────────────────────
def test_index_existing_sees_a_declaration_with_a_trailing_comment() -> None:
    """Regression: a comment after the call must not hide the declaration --
    this project's own .mdg fixtures use exactly this style, and a merge that
    can't see an existing node duplicates it under a fresh id."""
    text = (
        'c4.System_Boundary(sys1, "X"):\n'
        '    c4.Container(web1, "Y")  # legacy, keep as-is\n'
    )
    existing = merge.index_existing(text)
    assert existing.node_line == {"sys1": 0, "web1": 1}


def test_strip_inline_comment_respects_quoted_hashes() -> None:
    assert merge._strip_inline_comment('c4.Person(p1, "a # not a comment")') == (
        'c4.Person(p1, "a # not a comment")'
    )
    assert merge._strip_inline_comment("box(b1)  # real comment") == "box(b1)"


def test_add_colon_preserves_a_trailing_comment() -> None:
    assert merge._add_colon("box(b1)  # note") == "box(b1): # note"
    assert merge._add_colon("box(b1)") == "box(b1):"


def test_needs_colon_true_despite_indented_content_when_line_lacks_colon() -> None:
    """Regression: indentation alone is not evidence of real children -- only
    a trailing ':' is. A container missing its colon (a typo, or a stray
    indented comment) must still be flagged as needing one."""
    existing = merge.index_existing(
        'c4.System_Boundary(sys1, "X")\n    c4.Container(web1, "Y")\n'
    )
    assert merge._needs_colon(existing, "sys1") is True

    existing2 = merge.index_existing(
        'c4.System_Boundary(sys1, "X")\n    # TODO: add containers here\n'
    )
    assert merge._needs_colon(existing2, "sys1") is True


def test_child_insertion_ignores_orphaned_indentation_without_a_colon() -> None:
    existing = merge.index_existing(
        'c4.System_Boundary(sys1, "X")\n    c4.Container(web1, "Y")\n'
    )
    line, indent = merge._child_insertion(existing, "sys1")
    assert line == 1  # immediately after sys1's own line, not after web1
    assert indent == merge.INDENT_STEP


def test_escape_dsl_string_handles_quotes_backslashes_and_newlines() -> None:
    assert merge._escape_dsl_string('Bad", "Extra') == 'Bad\\", \\"Extra'
    assert merge._escape_dsl_string(r"C:\Users\bob") == r"C:\\Users\\bob"
    assert merge._escape_dsl_string("line1\nline2") == "line1\\nline2"


def test_render_merge_orders_same_line_ties_by_anchor_depth() -> None:
    """Regression: two anchors that happen to compute the same insertion
    line must be spliced deepest-first, so the deeper block lands
    immediately after its own declaration rather than after the shallower
    anchor's appended content."""
    existing = merge.index_existing(
        'c4.System_Boundary(sysA, "A"):\n'
        '    c4.System_Boundary(sysB, "B"):\n'
    )
    shallow = Insertion(2, "    c4.Person(person1)", anchor_depth=0)
    deep = Insertion(2, "        c4.Component(component1)", anchor_depth=1)
    plan = MergePlan([shallow, deep], [], 0, 2, {})
    out = merge.render_merge(existing, plan)
    assert out == (
        'c4.System_Boundary(sysA, "A"):\n'
        '    c4.System_Boundary(sysB, "B"):\n'
        "        c4.Component(component1)\n"
        "    c4.Person(person1)\n"
    )
    # Order of insertions in the plan must not change the result.
    plan_reordered = MergePlan([deep, shallow], [], 0, 2, {})
    assert merge.render_merge(existing, plan_reordered) == out


# ── plan_merge decision logic (no data) ──────────────────────────────────────
# _classify_new_cells and _build_forest are the actual "what's new, and how
# does it nest" decisions -- unlike _render_declaration downstream, they never
# touch the real registry (a Candidate's shape_id is just an opaque string
# here), so -- mirroring test_reverse_containment.py's approach -- they're
# pinned directly with hand-built dataclasses, independent of `make build-data`.
def _result(cell_id: str, shape_id: str | None) -> CellResult:
    chosen = Candidate(shape_id, "lib", 1.0) if shape_id else None
    return CellResult(
        cell_id=cell_id,
        style="irrelevant",
        candidates=[],
        chosen=chosen,
        resolved_by="unique" if chosen else "none",
        confidence=1.0 if chosen else 0.0,
    )


def _doc(*results: CellResult) -> DocumentResult:
    return DocumentResult(list(results), {}, {})


def test_classify_new_cells_separates_existing_new_edges_and_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(merge, "_is_edge_shape", lambda _shape_id: True)
    result = _doc(
        _result("existing1", "lib.box.v1"),  # already in the .mdg
        _result("src1", "lib.leaf.v1"),  # new vertex, edge endpoint
        _result("tgt1", "lib.leaf.v1"),  # new vertex, edge endpoint
        _result("edge1", "lib.rel.v1"),  # a new edge -- resolved for emission
        _result("junk1", None),  # unresolved -- skipped and reported
        _result("new1", "lib.leaf.v1"),  # genuinely new
    )
    raw_cells = {
        "existing1": RawCell(None, "shape=box;", False),
        "src1": RawCell(None, "shape=leaf;", False),
        "tgt1": RawCell(None, "shape=leaf;", False),
        "edge1": RawCell(
            None, "edgeStyle=x;", True, source_id="src1", target_id="tgt1"
        ),
        "junk1": RawCell(None, "shape=unknown;", False),
        "new1": RawCell(None, "shape=leaf;", False),
    }
    node_ids = {"src1": "leaf1", "tgt1": "leaf2"}
    new_cells, new_edges, skipped = merge._classify_new_cells(
        result, existing_ids={"existing1"}, raw_cells=raw_cells, node_ids=node_ids
    )
    assert {c.cell_id for c in new_cells} == {"src1", "tgt1", "new1"}
    assert new_edges == [merge.NewEdge("edge1", "lib.rel.v1", "leaf1", "leaf2")]
    assert len(skipped) == 1
    assert "junk1" in skipped[0]


def test_classify_new_cells_skips_an_edge_with_an_unresolved_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edge whose source/target points at a cell nothing resolved (not a
    new node, not already in the existing file) can't be emitted -- its
    declaration would reference an id that doesn't exist."""
    monkeypatch.setattr(merge, "_is_edge_shape", lambda _shape_id: True)
    result = _doc(
        _result("tgt1", "lib.leaf.v1"),
        _result("edge1", "lib.rel.v1"),
    )
    raw_cells = {
        "tgt1": RawCell(None, "shape=leaf;", False),
        "edge1": RawCell(
            None, "edgeStyle=x;", True, source_id="ghost", target_id="tgt1"
        ),
    }
    new_cells, new_edges, skipped = merge._classify_new_cells(
        result, existing_ids=set(), raw_cells=raw_cells, node_ids={"tgt1": "leaf1"}
    )
    assert [c.cell_id for c in new_cells] == ["tgt1"]
    assert new_edges == []
    assert any("edge1" in s for s in skipped)


def test_classify_new_cells_rejects_vertex_shape_for_raw_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(merge, "_is_edge_shape", lambda _shape_id: False)
    result = _doc(_result("edge1", "uml25.parameter.v1"))
    raw_cells = {
        "edge1": RawCell(
            None, "html=1;", True, source_id="src", target_id="target"
        )
    }

    new_cells, new_edges, skipped = merge._classify_new_cells(
        result,
        existing_ids={"src", "target"},
        raw_cells=raw_cells,
        node_ids={},
    )

    assert new_cells == []
    assert new_edges == []
    assert skipped == [
        "edge1: resolved edge to non-edge shape 'uml25.parameter.v1'"
    ]


def test_label_for_converts_a_div_wrapped_html_value_instead_of_dropping_it() -> None:
    """Regression: a hand-drawn cell's HTML value (draw.io sets html=1 for
    any multi-line label -- each line its own <div>) used to be dropped
    entirely just for containing a '<', losing the label outright instead
    of converting it."""
    cell = Cell(
        cell_id="n1",
        style="whiteSpace=wrap;html=1;",
        value="Missar vi något här emellan??<div>(spelregler/affärskrav)</div>",
        tokens={},
    )
    assert (
        merge._label_for(cell)
        == "Missar vi något här emellan??\n(spelregler/affärskrav)"
    )


def test_label_for_leaves_plain_text_with_no_angle_bracket_untouched() -> None:
    cell = Cell(cell_id="n1", style="", value="Plain label", tokens={})
    assert merge._label_for(cell) == "Plain label"


def test_render_declaration_emits_the_converted_multiline_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        merge, "_shape_meta", lambda _shape_id: ("erd", "EntityRect", 1)
    )
    cell = Cell(
        cell_id="n1",
        style="whiteSpace=wrap;html=1;",
        value="Missar vi något här emellan??<div>(spelregler/affärskrav)</div>",
        tokens={},
    )
    line = merge._render_declaration(cell, "erd.entityrect.v1", "n1", "", False)
    assert line == (
        'erd.EntityRect(n1, "Missar vi något här emellan??\\n'
        '(spelregler/affärskrav)")'
    )


def test_render_edge_uses_c4_description_as_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(merge, "_shape_meta", lambda _shape_id: ("c4", "Rel", 1))
    cell = Cell(
        cell_id="edge1",
        style="edgeStyle=orthogonalEdgeStyle;",
        value="<div>%c4Description%</div>",
        tokens={},
        object_attrs={"c4Description": "Calls API"},
    )

    assert merge._render_edge_declaration(cell, "c4.rel.v1", "a", "b") == (
        'c4.Rel(a, b, "Calls API")'
    )


def test_build_forest_groups_new_cells_by_nearest_existing_anchor() -> None:
    new_cells = [_result("p1", "lib.person.v1"), _result("p2", "lib.person.v1")]
    containments = {
        "p1": Containment("p1", "boxA", 1, ()),
        "p2": Containment("p2", "boxB", 1, ()),
    }
    roots_by_anchor = merge._build_forest(
        new_cells,
        containments,
        existing_ids={"boxA", "boxB"},
        id_of_node_id={"boxA": "boxA", "boxB": "boxB"},
    )
    assert {a: [n.cell_id for n in roots] for a, roots in roots_by_anchor.items()} == {
        "boxA": ["p1"],
        "boxB": ["p2"],
    }


def test_build_forest_nests_a_new_cell_under_a_new_container() -> None:
    """A new leaf whose container is ALSO new must appear as that new
    container's CHILD in the forest, not as a second top-level root."""
    new_cells = [_result("box1", "lib.box.v1"), _result("leaf1", "lib.leaf.v1")]
    containments = {
        "box1": Containment("box1", "existingRoot", 1, ()),
        "leaf1": Containment("leaf1", "box1_node", 2, ()),  # box1's own node_id
    }
    roots_by_anchor = merge._build_forest(
        new_cells,
        containments,
        existing_ids={"existingRoot"},
        id_of_node_id={"existingRoot": "existingRoot", "box1_node": "box1"},
    )
    assert list(roots_by_anchor) == ["existingRoot"]
    (box_node,) = roots_by_anchor["existingRoot"]
    assert box_node.cell_id == "box1"
    assert [child.cell_id for child in box_node.children] == ["leaf1"]


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
def test_merge_emits_a_new_edge_between_two_new_nodes() -> None:
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="p1", parent="sys1"),
        fx.entry_cell(person, cell_id="p2", parent="sys1"),
        fx.edge_cell_xml("e1", source="p1", target="p2", parent="1", style=rel.style),
    )
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_edge_count == 1
    assert merge.validate(merged) is None
    document = parse_mdg(merged)
    assert isinstance(document, Document)
    by_parent = {n.id: n.parent_id for n in document.nodes}
    person1, person2 = [
        n for n, p in by_parent.items() if p == "sys1" and n != "web1"
    ]
    assert any(
        line.startswith("c4.Rel(") and person1 in line and person2 in line
        for line in merged.splitlines()
    )


@needs_data
def test_merge_edge_endpoint_can_reference_an_already_existing_node() -> None:
    """An edge doesn't need BOTH endpoints to be new -- one end can point at
    a node already declared in the existing file (identity = cell_id ==
    node_id, per the module docstring)."""
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="p1", parent="sys1"),
        fx.edge_cell_xml("e1", source="p1", target="web1", parent="1", style=rel.style),
    )
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert plan.new_edge_count == 1
    assert merge.validate(merged) is None
    assert any(
        line.startswith("c4.Rel(") and "web1" in line for line in merged.splitlines()
    )


@needs_data
def test_merge_does_not_duplicate_an_edge_already_present_verbatim() -> None:
    """Re-running a merge against an unchanged source must not duplicate an
    edge it already emitted -- see the module docstring for the narrower
    dedup this covers (exact rendered line only)."""
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    rel = fx.get(INDEX, "c4.rel.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(sys1, "X"):\n'
        '    c4.Person(person1)\n'
        '    c4.Person(person2)\n\n'
        "c4.Rel(person1, person2)\n"
    )
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(person, cell_id="person1", parent="sys1"),
        fx.entry_cell(person, cell_id="person2", parent="sys1"),
        fx.edge_cell_xml(
            "e1", source="person1", target="person2", parent="1", style=rel.style
        ),
    )
    _, plan, merged = _run_pipeline(existing_text, doc)
    assert plan.new_edge_count == 0
    assert merged.rstrip() == existing_text.rstrip()


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
    assert "    c4.Person(person2)" in merged.splitlines()
    assert merged.count("person1") == 1  # the existing one, not re-emitted
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


@needs_data
def test_merge_nested_new_cells_place_correctly_regardless_of_source_order() -> None:
    """Regression for the anchor-line-tie bug: sysB is nested inside sysA and
    both are currently childless, so their "append a new child" points used
    to coincide -- and the resulting nesting depended on which cell the
    incoming .drawio happened to list first. Both orders must now nest
    identically and correctly."""
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    person = fx.get(INDEX, "c4.person.v1")
    component = fx.get(INDEX, "c4.component.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(sysA, "A"):\n'
        '    c4.System_Boundary(sysB, "B"):\n'
    )
    sysA_cell = fx.entry_cell(sys_b, cell_id="sysA", x=0, parent="1")
    sysB_cell = fx.entry_cell(sys_b, cell_id="sysB", x=0, parent="sysA")
    cell_a = fx.entry_cell(person, cell_id="p_new", x=0, parent="sysA")
    cell_b = fx.entry_cell(component, cell_id="c_new", x=100, parent="sysB")

    results = []
    for order in ([sysA_cell, sysB_cell, cell_b, cell_a],
                   [sysA_cell, sysB_cell, cell_a, cell_b]):
        doc = fx.document(*order)
        _, plan, merged = _run_pipeline(existing_text, doc)
        assert merge.validate(merged) is None
        document = parse_mdg(merged)
        assert isinstance(document, Document)
        parents = {n.id: n.parent_id for n in document.nodes}
        results.append(
            {n: parents[n] for n in parents if n not in ("sysA", "sysB")}
        )

    assert results[0] == results[1]
    (person_node,) = [n for n, p in results[0].items() if p == "sysA"]
    (component_node,) = [n for n, p in results[0].items() if p == "sysB"]
    assert person_node != component_node


@needs_data
def test_merge_does_not_duplicate_a_node_declared_with_a_trailing_comment() -> None:
    """Regression: index_existing must see past a trailing '#' comment on a
    declaration -- otherwise the merge tool believes an already-drawn shape
    is new and inserts a duplicate."""
    sys_b = fx.get(INDEX, "c4.system_boundary.v1")
    container = fx.get(INDEX, "c4.container.v1")
    existing_text = (
        '---\ntitle: "T"\npage: "P"\nmode: layered\n---\n\n'
        'c4.System_Boundary(sys1, "Mitt system"):\n'
        '    c4.Container(web1, "Webbapp")  # legacy, keep as-is\n'
    )
    doc = fx.document(
        fx.entry_cell(sys_b, cell_id="sys1", parent="1"),
        fx.entry_cell(container, cell_id="web1", parent="sys1"),
    )
    _, plan, merged = _run_pipeline(existing_text, doc)
    assert plan.new_node_count == 0
    assert merged.count("Container(web1") == 1


@needs_data
def test_merge_label_with_quote_comma_quote_round_trips_correctly() -> None:
    """Regression: XML-escaping a label meant for a Python-parsed string
    literal let a label like ``Bad", "Extra`` silently truncate and fabricate
    an extra positional argument. Must now round-trip exactly."""
    person = fx.get(INDEX, "c4.person.v1")
    tricky_label = 'Bad", "Extra'
    labeled = (
        f'<object id="p_new" c4Name="{xml_escape(tricky_label, {chr(34): "&quot;"})}">'
        f'<mxCell style="{person.style}" vertex="1" parent="1">'
        '<mxGeometry x="0" y="0" width="120" height="120" as="geometry"/>'
        "</mxCell></object>"
    )
    doc = fx.document(labeled)
    _, plan, merged = _run_pipeline(_BASE_MDG, doc)
    assert merge.validate(merged) is None
    document = parse_mdg(merged)
    assert isinstance(document, Document)
    new_node = next(n for n in document.nodes if n.id not in ("sys1", "web1"))
    assert new_node.label == tricky_label


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
