"""Regression tests for todo/notation-coverage-parser.md.

Each test pins one of the three coverage-sheet failures documented there
(C4's None,None palette-edge form; UML FoundMessage and UML 2.5 Dependency
mixing vertex/edge kinds across their own variants) plus the two bugs found
while fixing them: style resolution ignoring variant entirely, and
PaletteLayout's node/edge reconstruction dropping non-geometry fields.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from mdg_drawio.contracts import Document, NodeChildCell
from mdg_drawio.notation import DATA_DIR, LIBRARIES, NOTATION_DIR, DslError, parse

needs_sidecars = pytest.mark.skipif(
    not DATA_DIR.exists(),
    reason="generated notation sidecars missing — run `make build-data`",
)

# ---------------------------------------------------------------------------
# Variant-aware node/edge classification (UML FoundMessage, UML 2.5 Dependency)
# ---------------------------------------------------------------------------


def test_uml_found_message_variant1_is_a_vertex() -> None:
    doc = parse('use uml\numl.FoundMessage(a, "", variant=1)')
    assert isinstance(doc, Document)
    assert [n.id for n in doc.nodes] == ["a"]
    assert doc.edges == []


def test_uml_found_message_variant2_is_an_edge() -> None:
    doc = parse(
        'use uml\n'
        'uml.Lifeline(a, "")\n'
        'uml.Lifeline(b, "")\n'
        'uml.FoundMessage(a, b, variant=2)'
    )
    assert isinstance(doc, Document)
    assert [(e.source_id, e.target_id) for e in doc.edges] == [("a", "b")]


def test_uml25_dependency_variant1_is_an_edge() -> None:
    doc = parse(
        'use uml25\n'
        'uml25.Class(a, "")\n'
        'uml25.Class(b, "")\n'
        'uml25.Dependency(a, b, variant=1)'
    )
    assert isinstance(doc, Document)
    assert [(e.source_id, e.target_id) for e in doc.edges] == [("a", "b")]


def test_uml25_dependency_variant2_is_a_vertex() -> None:
    doc = parse('use uml25\numl25.Dependency(a, "", variant=2)')
    assert isinstance(doc, Document)
    assert [n.id for n in doc.nodes] == ["a"]
    assert doc.edges == []


@pytest.mark.parametrize(
    "call",
    [
        "uml.FoundMessage(a, b, variant=99)",
        "uml25.Dependency(a, b, variant=99)",
    ],
)
def test_mixed_kind_family_rejects_unknown_variant(call: str) -> None:
    with pytest.raises(DslError, match=r"line 2: .*unsupported variant 99"):
        parse(f"use {call.split('.', 1)[0]}\n{call}")


def test_single_kind_family_rejects_unknown_variant() -> None:
    with pytest.raises(DslError, match=r"line 2: .*unsupported variant 99"):
        parse('use uml\numl.Object(a, "Object", variant=99)')


# uml and uml25 are the only two libraries with a function whose registry
# variants mix vertex and edge kinds (confirmed by scanning every library's
# shapes_by_function()). Every other library's mixed-kind coverage is
# vacuous: there is nothing to mix.
@pytest.mark.parametrize(
    ("call", "vertex_variant", "edge_variant"),
    [
        ("uml25.Constraint", 1, 2),
        ("uml25.Extension", 1, 2),
        ("uml25.Activity", 2, 1),
        ("uml25.Message", 2, 1),
    ],
)
def test_uml25_mixed_kind_families_classify_by_exact_variant(
    call: str, vertex_variant: int, edge_variant: int
) -> None:
    doc = parse(f'use uml25\n{call}(a, "", variant={vertex_variant})')
    assert isinstance(doc, Document)
    assert [n.id for n in doc.nodes] == ["a"]
    assert doc.edges == []

    doc = parse(
        'use uml25\n'
        'uml25.Class(a, "")\n'
        'uml25.Class(b, "")\n'
        f'{call}(a, b, variant={edge_variant})'
    )
    assert isinstance(doc, Document)
    assert [(e.source_id, e.target_id) for e in doc.edges] == [("a", "b")]


@pytest.mark.parametrize(
    ("call", "variant", "expected_kind"),
    [
        ("uml.FoundMessage", 3, "edge"),
        ("uml25.Activity", 3, "vertex"),
        ("uml25.Activity", 4, "edge"),
        ("uml25.Activity", 5, "edge"),
        ("uml25.Message", 3, "vertex"),
    ],
)
def test_remaining_mixed_family_variants_classify_exactly(
    call: str, variant: int, expected_kind: str
) -> None:
    namespace = call.split(".", 1)[0]
    args = 'a, ""' if expected_kind == "vertex" else "a, b"
    doc = parse(f"use {namespace}\n{call}({args}, variant={variant})")
    assert isinstance(doc, Document)
    assert len(doc.nodes) == (expected_kind == "vertex")
    assert len(doc.edges) == (expected_kind == "edge")


# ---------------------------------------------------------------------------
# Phase 3: a passthrough call with no positional arguments at all used to be
# silently dropped (no node, no error) instead of rejected -- the native c4
# parser already errors on this; the passthrough path now matches it.
# ---------------------------------------------------------------------------


def test_passthrough_node_with_no_positional_args_is_rejected() -> None:
    with pytest.raises(DslError, match="missing required argument 'node_id'"):
        parse("use uml\numl.Object()")


def test_passthrough_accepts_unquoted_hyphenated_ids() -> None:
    """An unquoted hyphenated id parses as a chain of subtractions.

    The native c4 parser has always recovered those (``literal_or_name``),
    but the passthrough builders used a narrower extractor, so the identical
    construct was legal in one notation and a confusing "first argument must
    be a node id" error in every other. draw.io's own cell ids -- what the
    reverse derivation seeds node ids from -- routinely contain hyphens.
    """
    doc = parse("use erd\nerd.EntityRect(my-node-1, \"Label\")")
    assert isinstance(doc, Document)
    (node,) = doc.nodes
    assert node.id == "my-node-1"


def test_passthrough_edge_accepts_unquoted_hyphenated_endpoints() -> None:
    doc = parse(
        "use erd\n"
        'erd.EntityRect(a-1, "A")\n'
        'erd.EntityRect(b-2, "B")\n'
        "erd.Rel(a-1, b-2)"
    )
    assert isinstance(doc, Document)
    (edge,) = doc.edges
    assert (edge.source_id, edge.target_id) == ("a-1", "b-2")


# ---------------------------------------------------------------------------
# C4's own edge builder: None, None unconnected palette-edge form
# ---------------------------------------------------------------------------


def test_c4_rel_accepts_none_none_unconnected_form() -> None:
    doc = parse('c4.Rel(None, None, "e.g. Makes API calls")')
    assert isinstance(doc, Document)
    (edge,) = doc.edges
    assert (edge.source_id, edge.target_id) == ("", "")
    assert edge.id  # stable, non-empty, unlike a real "source->target" id


def test_c4_rel_unconnected_edges_get_distinct_ids() -> None:
    doc = parse(
        'c4.Rel(None, None, "one")\n'
        'c4.Rel(None, None, "two")'
    )
    assert isinstance(doc, Document)
    assert len({e.id for e in doc.edges}) == 2


def test_unconnected_edge_id_does_not_collide_with_authored_node_id() -> None:
    doc = parse(
        'c4.Person("palette-edge-line2", "Alice")\n'
        'c4.Rel(None, None, "calls")'
    )
    assert isinstance(doc, Document)
    assert {doc.nodes[0].id, doc.edges[0].id} == {
        "palette-edge-line2",
        "palette-edge-line2-2",
    }


@pytest.mark.parametrize("source_id", ["None", '"a"'])
def test_c4_rel_rejects_one_sided_none(source_id: str) -> None:
    # Exactly one endpoint is None -- the unconnected form requires BOTH.
    target_id = '"b"' if source_id == "None" else "None"
    with pytest.raises(DslError, match="must both be ids, or both be None"):
        parse(f'c4.Rel({source_id}, {target_id})')


# ---------------------------------------------------------------------------
# Style resolution respects the requested variant, not always variant 1
# ---------------------------------------------------------------------------


def test_resolve_style_differs_by_variant() -> None:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider

    registries, styles = preload_core()
    provider = create_style_provider(registries, styles)
    style_v1 = provider.resolve_style("bpmn2.HorizontalLane", 1)
    style_v2 = provider.resolve_style("bpmn2.HorizontalLane", 2)
    assert style_v1 != style_v2
    assert "swimlaneLine=0" in style_v1
    assert "swimlaneLine=1" in style_v2


def test_resolve_edge_style_differs_by_variant() -> None:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider

    registries, styles = preload_core()
    provider = create_style_provider(registries, styles)
    style_v2 = provider.resolve_edge_style("uml.FoundMessage", 2)
    style_v3 = provider.resolve_edge_style("uml.FoundMessage", 3)
    assert style_v2 != style_v3
    assert "endArrow=block" in style_v2
    assert "endArrow=open" in style_v3


# ---------------------------------------------------------------------------
# Phase 2: registry-driven rows.allowed / contains.allowed validation.
#
# Both kinds need real Nodes with parent_id for stack/table layout. That root
# representation does not by itself provide palette-faithful row styling or
# compound cells; the generated row-type sidecars and generator cover those
# below. This section focuses on structural validation.
# ---------------------------------------------------------------------------


def test_row_child_becomes_a_real_contained_node() -> None:
    doc = parse(
        'use uml\n'
        'uml.Class(c1, "Classname"):\n'
        '    uml.Item(f1, "+ field: type")'
    )
    assert isinstance(doc, Document)
    item = next(n for n in doc.nodes if n.id == "f1")
    assert item.parent_id == "c1"
    assert item.child_cells == []


def test_row_function_not_in_rows_allowed_is_rejected() -> None:
    # uml.Class variant=2's rows.allowed is ['Item'] only -- Divider is legal
    # on variant 1 but not variant 2.
    with pytest.raises(DslError, match="not a valid row"):
        parse(
            'use uml\n'
            'uml.Class(c1, "Classname", variant=2):\n'
            '    uml.Divider(d1, "")'
        )


def test_contained_function_outside_contains_allowed_is_rejected() -> None:
    # No current registry entry restricts contains.allowed to specific
    # function names (every real one is '*'), so this exercises the
    # validator directly rather than through a real notation.
    from mdg_drawio.notation._core.dsl_engine import (
        _ContainerFrame,
        _validate_child_allowed,
    )

    frame = _ContainerFrame(
        indent=0,
        node_id="p1",
        namespace="general",
        kind_label="child",
        allowed=frozenset({"Allowed"}),
    )
    with pytest.raises(DslError, match="not a valid child"):
        _validate_child_allowed(frame, "general", "NotAllowed", None, 1)
    _validate_child_allowed(frame, "general", "Allowed", None, 1)


def test_block_on_shape_with_neither_rows_nor_containment_is_rejected() -> None:
    with pytest.raises(DslError, match="neither rows nor containment"):
        parse('use uml\numl.Object(o1, "Object"):\n    uml.Item(i1, "x")')


def test_native_c4_block_is_validated_against_registry() -> None:
    with pytest.raises(DslError, match="neither rows nor containment"):
        parse('c4.Person(p1, "Person"):\n    c4.System(s1, "System")')


def test_row_child_must_use_its_parent_namespace() -> None:
    with pytest.raises(DslError, match=r"expected uml\.\{Item\}"):
        parse(
            'use uml\n'
            'uml.Class(c1, "Classname", variant=2):\n'
            '    uml25.Item(i1, "field")'
        )


def test_wildcard_containment_stays_within_parent_namespace() -> None:
    with pytest.raises(DslError, match=r"expected uml\.\*"):
        parse(
            'use uml\n'
            'uml.Package(p1, "Package"):\n'
            '    c4.Person(person1, "Person")'
        )


def test_edge_cannot_be_nested_as_a_row() -> None:
    with pytest.raises(DslError, match="edges cannot be nested"):
        parse(
            'use uml\n'
            'uml.Class(c1, "Classname"):\n'
            '    uml.Message(None, None, "dispatch")'
        )


def test_unknown_keyword_is_rejected_for_registered_call() -> None:
    with pytest.raises(DslError, match="unknown keyword argument.*surprise"):
        parse('use uml\numl.Object(o1, "Object", surprise="ignored")')


def test_declared_keyword_is_accepted_for_row_type() -> None:
    doc = parse(
        'use uml25\n'
        'uml25.Classifier(c1, "Class"):\n'
        '    uml25.Divider(d1, "", dashed=True)'
    )
    assert isinstance(doc, Document)


# ---------------------------------------------------------------------------
# Phase 3: full positional/keyword argument binding. The registry entry is
# the call's signature -- passing: positional args fill left-to-right from
# the call's positional arguments, passing: keyword_only args must be named,
# and excess/duplicate/missing bindings are line-numbered DslErrors.
#
# erd.RowKey(node_id[, label][, key=]) is the running example: node_id/label
# are passing: positional, key is passing: keyword_only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        'erd.RowKey(row1)',
        'erd.RowKey(row1, "Customer ID")',
        'erd.RowKey(row1, "Customer ID", key="PK")',
    ],
)
def test_registry_bound_call_with_valid_arguments_parses(call: str) -> None:
    doc = parse(f"use erd\n{call}")
    assert isinstance(doc, Document)
    assert doc.nodes[0].id == "row1"


def test_registry_bound_call_missing_required_argument_is_rejected() -> None:
    with pytest.raises(DslError, match="missing required argument 'node_id'"):
        parse("use erd\nerd.RowKey()")


def test_registry_bound_call_rejects_excess_positional_arguments() -> None:
    with pytest.raises(
        DslError, match=r"too many positional arguments \(expected at most 2\)"
    ):
        parse('use erd\nerd.RowKey(row1, "ID", "PK", 42)')


def test_registry_bound_call_rejects_argument_supplied_twice() -> None:
    # erd.RowKey's own arg name for its second positional value is "label"
    # (row_types use "text" for the same role; shapes use "label" -- see
    # _declared_args), so this is the real-data equivalent of supplying the
    # same argument both positionally and by keyword.
    with pytest.raises(DslError, match="label= supplied twice"):
        parse('use erd\nerd.RowKey(row1, "ID", label="ID2")')


def test_registry_bound_call_rejects_duplicate_keyword() -> None:
    with pytest.raises(DslError, match="keyword argument supplied twice: key"):
        parse('use erd\nerd.RowKey(row1, key="PK", key="FK")')


@pytest.mark.parametrize(
    "variants", ["variant=1, variant=1", "variant=1, variant=99"]
)
def test_registry_bound_call_rejects_duplicate_variant(variants: str) -> None:
    with pytest.raises(DslError, match="keyword argument supplied twice: variant"):
        parse(f"use erd\nerd.RowKey(row1, {variants})")


def test_registry_bound_call_still_rejects_unknown_keyword() -> None:
    with pytest.raises(DslError, match="unknown keyword argument.*unknown"):
        parse('use erd\nerd.RowKey(row1, unknown="x")')


def test_registry_bound_edge_rejects_excess_positional_arguments() -> None:
    with pytest.raises(DslError, match="too many positional arguments"):
        parse('use erd\nerd.Rel(None, None, "label", "extra")')


def test_native_c4_node_uses_registry_keyword_binding() -> None:
    doc = parse('c4.Person(node_id=person, label="Person", description="Bio")')
    assert isinstance(doc, Document)
    (node,) = doc.nodes
    assert (node.id, node.label, node.text_parts) == ("person", "Person", ["Bio"])


def test_native_c4_edge_uses_registry_keyword_binding() -> None:
    doc = parse('c4.Rel(source=a, target=b, label="calls")')
    assert isinstance(doc, Document)
    (edge,) = doc.edges
    assert (edge.source_id, edge.target_id, edge.label) == ("a", "b", "calls")


def test_native_c4_call_rejects_excess_positional_arguments() -> None:
    with pytest.raises(DslError, match="too many positional arguments"):
        parse('c4.Person(person, "Person", "Bio", "extra")')


def test_general_textbox_positional_description_is_preserved() -> None:
    """general.Textbox's 3rd positional arg (description) is declared
    passing: positional but was previously dropped entirely -- only the
    first two positional arguments were ever read. It's now bound and
    preserved onto Node.extra like any other declared value."""
    doc = parse('use general\ngeneral.Textbox(n1, "Heading", "Body text")')
    assert isinstance(doc, Document)
    assert doc.nodes[0].extra["description"] == "Body text"


@needs_sidecars
def test_uml25_divider_dashed_keyword_changes_generated_style() -> None:
    root = _generate_xml(
        'use uml25\n'
        'uml25.Classifier(c1, "Class"):\n'
        '    uml25.Divider(solid, "")\n'
        '    uml25.Divider(dashed, "", dashed=True)'
    )
    assert "dashed=" not in (_cell(root, "solid").get("style") or "")
    assert "dashed=1" in (_cell(root, "dashed").get("style") or "")


@pytest.mark.parametrize("library", LIBRARIES)
def test_coverage_generated_cell_counts_match_model(library: str) -> None:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider, generate
    from mdg_drawio.generator.generator import (
        _GenCtx,
        _coerce_variant,
        _compound_row_override,
        _edge_endpoint_label_cells,
    )

    source = (NOTATION_DIR / library / f"{library}_shapes_coverage.mdg").read_text()
    doc = parse(source)
    assert isinstance(doc, Document)
    registries, styles = preload_core()
    style_provider = create_style_provider(registries, styles)
    root = ET.fromstring(generate(doc, style_provider))

    # A nested compound row (erd Row/RowKey) renders as one outer vertex plus
    # its [key tag, text label] sub-cells -- extra real vertices with no
    # corresponding top-level Node, by design (see _compound_row_override).
    def descendant_count(cells: list[NodeChildCell]) -> int:
        return sum(1 + descendant_count(cell.child_cells) for cell in cells)

    extra_compound_cells = sum(
        descendant_count(override[1])
        for node in doc.nodes
        if (override := _compound_row_override(node, style_provider)) is not None
    )
    # A relation with an authored source_label/target_label (e.g. erd.Rel,
    # uml.Relation/Association) renders one extra vertex per endpoint label --
    # also with no corresponding top-level Node (see _edge_endpoint_label_cells).
    gen_ctx = _GenCtx(styles=style_provider)
    extra_edge_label_cells = sum(
        len(_edge_endpoint_label_cells(edge, gen_ctx, _coerce_variant(edge)))
        for edge in doc.edges
    )
    assert sum(
        cell.get("vertex") == "1" for cell in root.iter("mxCell")
    ) == len(doc.nodes) + extra_compound_cells + extra_edge_label_cells
    assert sum(cell.get("edge") == "1" for cell in root.iter("mxCell")) == len(
        doc.edges
    )


# ---------------------------------------------------------------------------
# Phase 2 (continued): palette-faithful row rendering.
#
# Row types with no independent top-level shape (uml25's Item/Header/Divider/
# Note/Lane, erd's Row/EntityText/Anchor, uml's CompositeLabel, bpmn2's
# SwimlaneBoxPart/TableRowBoxPart) used to fall back to DEFAULT_VERTEX_STYLE
# and the default 120x60 size. scripts/build_notation_styles.py now extracts
# a canonical style/geometry for each from its parent shape's own palette
# cells, and PaletteStyleProvider/size_resolver fall back to it. erd's Row
# and RowKey are additionally compound (a wrapper row + [key tag, text label]
# sub-cells), rendered via NodeChildCell -- see _compound_row_override.
# ---------------------------------------------------------------------------


def _style_and_size(node_type: str) -> tuple[str, tuple[float, float]]:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider
    from mdg_drawio.layout.size_resolver import create_size_resolver

    registries, styles = preload_core()
    provider = create_style_provider(registries, styles)
    size_of = create_size_resolver(registries=registries, styles=styles)
    return provider.resolve_style(node_type), size_of(node_type)


@needs_sidecars
@pytest.mark.parametrize(
    ("node_type", "expected_height"),
    [
        ("uml25.Header", 20),
        ("uml25.Item", 20),
        ("uml25.Divider", 8),
        ("uml25.Note", 140),
        ("uml25.Lane", 20),
    ],
)
def test_uml25_orphaned_row_types_resolve_palette_geometry(
    node_type: str, expected_height: float
) -> None:
    style, (width, height) = _style_and_size(node_type)
    assert style and style != "whiteSpace=wrap;html=1;"
    assert width > 0
    assert height == expected_height


@needs_sidecars
@pytest.mark.parametrize(
    "node_type", ["bpmn2.SwimlaneBoxPart", "bpmn2.TableRowBoxPart"]
)
def test_bpmn2_row_types_resolve_palette_style(node_type: str) -> None:
    style, (width, height) = _style_and_size(node_type)
    assert style and style != "whiteSpace=wrap;html=1;"
    assert width > 0 and height > 0


@needs_sidecars
def test_uml_composite_label_resolves_palette_style() -> None:
    style, (width, height) = _style_and_size("uml.CompositeLabel")
    assert style and style != "whiteSpace=wrap;html=1;"
    assert (width, height) != (0, 0)


def _generate_xml(source: str) -> ET.Element:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider, generate

    doc = parse(source)
    assert isinstance(doc, Document)
    registries, styles = preload_core()
    return ET.fromstring(generate(doc, create_style_provider(registries, styles)))


def _cell(root: ET.Element, cell_id: str) -> ET.Element:
    return next(c for c in root.iter("mxCell") if c.get("id") == cell_id)


def _children_of(root: ET.Element, parent_id: str) -> list[ET.Element]:
    return [c for c in root.iter("mxCell") if c.get("parent") == parent_id]


@needs_sidecars
def test_erd_nested_rowkey_renders_key_and_text_subcells() -> None:
    root = _generate_xml(
        'use erd\n'
        'erd.Table(t1, "T"):\n'
        '    erd.RowKey(r1, "UniqueID", key="PK")'
    )
    row = _cell(root, "r1")
    assert row.get("value") == ""
    assert "shape=tableRow" in (row.get("style") or "")
    assert "shape=table;" not in (row.get("style") or "")

    children = _children_of(root, "r1")
    assert len(children) == 2
    assert [c.get("value") for c in children] == ["PK", "UniqueID"]
    assert "fontStyle=1" in (children[0].get("style") or "")
    text_geometry = children[1].find("mxGeometry")
    assert text_geometry is not None
    assert "alternate_bounds" not in text_geometry.attrib
    alternate = text_geometry.find("mxRectangle")
    assert alternate is not None
    assert alternate.get("as") == "alternateBounds"


@needs_sidecars
def test_erd_rel_renders_authored_cardinality_labels() -> None:
    """source_label/target_label are registry-declared passthrough keywords;
    they must render as real label cells, not be silently dropped."""
    root = _generate_xml(
        'use erd\n'
        'erd.Table(a, "A")\n'
        'erd.Table(b, "B")\n'
        'erd.Rel(a, b, "omfattas av", target_label="0..n", variant=4)'
    )
    edge_id = _cell(root, "e_a->b").get("id")
    assert edge_id is not None
    labels = _children_of(root, edge_id)
    assert [c.get("value") for c in labels] == ["0..n"]
    assert "align=right" in (labels[0].get("style") or "")


@needs_sidecars
def test_erd_rel_renders_both_endpoint_labels() -> None:
    root = _generate_xml(
        'use erd\n'
        'erd.Table(a, "A")\n'
        'erd.Table(b, "B")\n'
        'erd.Rel(a, b, "", source_label="M", target_label="N", variant=4)'
    )
    edge_id = _cell(root, "e_a->b").get("id")
    assert edge_id is not None
    labels = _children_of(root, edge_id)
    assert {c.get("value") for c in labels} == {"M", "N"}


@needs_sidecars
def test_erd_nested_plain_row_has_empty_key_subcell() -> None:
    root = _generate_xml(
        'use erd\nerd.Table(t1, "T"):\n    erd.Row(r1, "Row 1")'
    )
    children = _children_of(root, "r1")
    assert len(children) == 2
    assert [c.get("value") for c in children] == ["", "Row 1"]
    assert "editable=1" in (children[0].get("style") or "")
    assert "fontStyle=" not in (children[1].get("style") or "")
    assert "bottom=0" in (_cell(root, "r1").get("style") or "")


@needs_sidecars
def test_erd_standalone_rowkey_keeps_table_hierarchy_and_values() -> None:
    """A top-level RowKey is a table wrapper containing row/key/text cells."""
    root = _generate_xml('use erd\nerd.RowKey(r1, "UniqueID", key="PK")')
    row = _cell(root, "r1")
    assert row.get("value") == ""
    assert "shape=table;" in (row.get("style") or "")
    (table_row,) = _children_of(root, "r1")
    assert "shape=tableRow" in (table_row.get("style") or "")
    assert [c.get("value") for c in _children_of(root, table_row.get("id", ""))] == [
        "PK",
        "UniqueID",
    ]
