"""Regression tests for the 2026-07-27 code-review remediation.

Each test pins a specific finding fixed in that pass (see
``docs/architecture/README.md`` §5-6 and ``todo/todo.md``). They are grouped by
container to mirror the review.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from mdg_drawio.contracts import (
    Anchor,
    Diagram,
    Document,
    Edge,
    Node,
    NodeChildCell,
)
from mdg_drawio.engine.validate import validate_generated_xml
from mdg_drawio.layout.config import parse_aspect_ratio, resolve_page_size
from mdg_drawio.notation import DslError, parse
from mdg_drawio.notation._core import parse_keyword_int
from mdg_drawio.notation._core.registry import load_registry, set_registries

# ---------------------------------------------------------------------------
# Generator — boolean flags are literals, not id-constants (finding 1.1 / 5.4)
# ---------------------------------------------------------------------------

def _generate_c4(source: str) -> ET.Element:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider, generate

    registries, styles = preload_core()
    provider = create_style_provider(registries, styles)
    doc = parse(source)
    assert isinstance(doc, Document)
    xml = generate(doc, provider)
    return ET.fromstring(xml)


def test_node_vertex_flag_is_literal_one() -> None:
    root = _generate_c4('c4.System(s, "S", "desc")')
    vertices = [c for c in root.iter("mxCell") if c.get("vertex")]
    # Exactly one vertex (the single System node), flagged "1". Pinning the count
    # also catches a stray or missing vertex cell.
    assert len(vertices) == 1
    assert vertices[0].get("vertex") == "1"


def test_graph_math_flag_is_literal_zero() -> None:
    root = _generate_c4('c4.System(s, "S", "desc")')
    model = root.find(".//mxGraphModel")
    assert model is not None
    assert model.get("math") == "0"


# ---------------------------------------------------------------------------
# Engine validation — wrapped edges + duplicate ids (findings 1.2 / 2.9)
# ---------------------------------------------------------------------------

def test_validation_catches_dangling_reference_in_wrapped_edge() -> None:
    """A UserObject-wrapped edge pointing at a missing node must be rejected."""
    xml = """<mxfile><diagram id="d0"><mxGraphModel><root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="n1" vertex="1" parent="1"/>
        <UserObject id="e1" label="r">
          <mxCell edge="1" parent="1" source="n1" target="ghost"/>
        </UserObject>
      </root></mxGraphModel></diagram></mxfile>"""
    errors = validate_generated_xml(xml)
    assert any("references unknown cell" in e and "ghost" in e for e in errors), errors


def test_validation_catches_duplicate_cell_ids() -> None:
    xml = """<mxfile><diagram id="d0"><mxGraphModel><root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="dup" vertex="1" parent="1"/>
        <mxCell id="dup" vertex="1" parent="1"/>
      </root></mxGraphModel></diagram></mxfile>"""
    errors = validate_generated_xml(xml)
    assert any("duplicate cell id" in e and "dup" in e for e in errors), errors


# ---------------------------------------------------------------------------
# DSL error contract — line numbers on builder errors (findings 1.3 / 1.4)
# ---------------------------------------------------------------------------

def test_bad_argument_type_raises_dslerror_with_line_number() -> None:
    with pytest.raises(DslError) as exc:
        parse('c4.Person(alice, 123)')
    # The bad arg is on the only line, so the reported line must be exactly 1.
    assert exc.value.line_number == 1
    assert "line 1" in str(exc.value)


def test_node_without_label_stays_empty() -> None:
    """Reversal of the original 2026-07-27 finding: defaulting an omitted
    label to the node's own id was scoped to this one shared builder every
    notation goes through, not just c4 -- it forced a visible id string onto
    intentionally icon-only shapes (e.g. a BPMN start/end event) once other
    notations started rendering through the same forward pipeline. An
    omitted label now stays truly empty; c4 authors already always supply
    one explicitly in every real document."""
    doc = parse('c4.Person(alice)')
    assert isinstance(doc, Document)
    node = next(n for n in doc.nodes if n.id == "alice")
    assert node.label == ""


def test_dangling_edge_endpoint_raises_dslerror() -> None:
    # The None *source* is what's rejected — pin the message so an unrelated
    # future DslError can't satisfy the test.
    with pytest.raises(DslError, match="source"):
        parse('c4.Rel(None, bob)')


def test_foreign_root_sets_title_without_rendering_a_node() -> None:
    doc = parse('use bpmn2\nbpmn2.BPMN("Orders")\nbpmn2.User(task, "Take order")')
    assert isinstance(doc, Document)
    assert doc.diagram.name == "Orders"
    assert [(node.id, node.type) for node in doc.nodes] == [
        ("task", "bpmn2.User")
    ]


def test_foreign_node_and_edge_variants_are_preserved() -> None:
    doc = parse(
        'use bpmn2\n'
        'bpmn2.DataObject(data, "", variant=2)\n'
        'bpmn2.User(task, "Task")\n'
        'general.Rel(data, task, variant=2)'
    )
    assert isinstance(doc, Document)
    assert next(node for node in doc.nodes if node.id == "data").variant == 2
    assert doc.edges[0].extra["variant"] == 2


def test_foreign_palette_edges_allow_none_endpoints_with_unique_ids() -> None:
    doc = parse(
        "use bpmn2\n"
        "bpmn2.Association(None, None)\n"
        "bpmn2.MessageFlow(None, None)"
    )
    assert isinstance(doc, Document)
    assert [edge.id for edge in doc.edges] == ["palette-edge-1", "palette-edge-2"]


def _generate_c4_xml(source: str) -> str:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider, generate

    registries, styles = preload_core()
    provider = create_style_provider(registries, styles)
    doc = parse(source)
    assert isinstance(doc, Document)
    return generate(doc, provider)


def test_parallel_native_edges_get_disambiguated_generated_ids() -> None:
    """Two c4.Rel calls between the same (source, target) pair both compute
    the identical `f"{source}->{target}"` id at parse time -- without
    disambiguation at generation, this is a duplicate cell id."""
    xml = _generate_c4_xml(
        'c4.Person(a, "A")\n'
        'c4.System(b, "B")\n'
        'c4.Rel(a, b, "one")\n'
        'c4.Rel(a, b, "two")'
    )
    assert validate_generated_xml(xml) == []
    root = ET.fromstring(xml)
    # A c4 Rel is wrapped in a <UserObject> (label-template substitution);
    # the id lives there, not on the plain mxCell nested inside it.
    edge_ids = [
        obj.get("id") for obj in root.iter("UserObject") if "source" in obj[0].attrib
    ]
    assert edge_ids == ["a->b", "a->b-2"]


def test_parallel_passthrough_edges_get_disambiguated_generated_ids() -> None:
    """Foreign-namespace edges leave ``Edge.id`` empty and rely on the
    generator's own fallback (``f"e_{source}->{target}"``) -- the same
    collision as the native case above, one layer later."""
    xml = _generate_c4_xml(
        'use bpmn2\n'
        'bpmn2.User(a, "A")\n'
        'bpmn2.User(b, "B")\n'
        'bpmn2.Association(a, b)\n'
        'bpmn2.MessageFlow(a, b)'
    )
    assert validate_generated_xml(xml) == []


def _generate_document_xml(document: Document) -> str:
    from mdg_drawio.engine.preload import preload_core
    from mdg_drawio.generator import create_style_provider, generate

    registries, styles = preload_core()
    return generate(document, create_style_provider(registries, styles))


def test_edge_id_does_not_collide_with_authored_node_id() -> None:
    document = Document(
        diagram=Diagram(),
        nodes=[
            Node(id="a", type="c4.System"),
            Node(id="b", type="c4.System"),
            Node(id="same", type="c4.System"),
        ],
        edges=[Edge(id="same", type="c4.Rel", source_id="a", target_id="b")],
    )

    xml = _generate_document_xml(document)

    assert validate_generated_xml(xml) == []
    assert 'id="same-2"' in xml


def test_explicit_edge_id_collision_fails_instead_of_changing_identity() -> None:
    with pytest.raises(ValueError, match="explicit edge_id 'same' is already in use"):
        _generate_c4_xml(
            'c4.Person(a, "A")\n'
            'c4.System(same, "B")\n'
            'c4.Rel(a, same, edge_id="same")'
        )


def test_generated_edge_carries_type_and_variant_provenance() -> None:
    root = ET.fromstring(
        _generate_c4_xml(
            'c4.Person(a, "A")\n'
            'c4.System(b, "B")\n'
            'c4.Rel(a, b, variant=2, edge_id="relationship-1")'
        )
    )
    edge = next(cell for cell in root.iter("mxCell") if cell.get("edge") == "1")
    assert edge.get("mdgType") == "c4.Rel"
    assert edge.get("mdgVariant") == "2"


def test_generated_child_id_does_not_collide_with_later_authored_node() -> None:
    document = Document(
        diagram=Diagram(),
        nodes=[
            Node(
                id="a",
                type="c4.System",
                child_cells=[NodeChildCell(label="child")],
            ),
            Node(id="a__c0", type="c4.System"),
        ],
    )

    xml = _generate_document_xml(document)

    assert validate_generated_xml(xml) == []
    assert 'id="a__c1"' in xml


def test_parallel_edge_overlays_preserve_individual_routes() -> None:
    from mdg_drawio.engine.convert import _inject_edge_overlay
    from mdg_drawio.generator.overlay import read_overlay_xml

    xml = """<mxfile><diagram><mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="a" vertex="1" parent="1"/>
      <mxCell id="b" vertex="1" parent="1"/>
      <mxCell id="a-&gt;b" edge="1" parent="1" source="a" target="b">
        <mxGeometry relative="1" as="geometry"><Array as="points">
          <mxPoint x="10" y="20"/>
        </Array></mxGeometry>
      </mxCell>
      <mxCell id="a-&gt;b-2" edge="1" parent="1" source="a" target="b">
        <mxGeometry relative="1" as="geometry"><Array as="points">
          <mxPoint x="30" y="40"/>
        </Array></mxGeometry>
      </mxCell>
    </root></mxGraphModel></diagram></mxfile>"""
    (overlay,) = read_overlay_xml(xml)
    edges = [
        Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b"),
        Edge(id="a->b", type="c4.Rel", source_id="a", target_id="b"),
    ]

    _inject_edge_overlay(edges, overlay)

    assert [(p.x, p.y) for p in edges[0].waypoints] == [(10.0, 20.0)]
    assert [(p.x, p.y) for p in edges[1].waypoints] == [(30.0, 40.0)]


def test_parallel_edge_overlays_follow_stable_ids_after_reordering() -> None:
    from mdg_drawio.engine.convert import _inject_edge_overlay
    from mdg_drawio.generator.overlay import read_overlay_xml

    xml = """<mxfile><diagram><mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="a" vertex="1" parent="1"/>
      <mxCell id="b" vertex="1" parent="1"/>
      <mxCell id="first" edge="1" parent="1" source="a" target="b">
        <mxGeometry relative="1" as="geometry"><Array as="points">
          <mxPoint x="10" y="20"/>
        </Array></mxGeometry>
      </mxCell>
      <mxCell id="second" edge="1" parent="1" source="a" target="b">
        <mxGeometry relative="1" as="geometry"><Array as="points">
          <mxPoint x="30" y="40"/>
        </Array></mxGeometry>
      </mxCell>
    </root></mxGraphModel></diagram></mxfile>"""
    (overlay,) = read_overlay_xml(xml)
    edges = [
        Edge(id="second", type="c4.Rel", source_id="a", target_id="b"),
        Edge(id="first", type="c4.Rel", source_id="a", target_id="b"),
    ]

    _inject_edge_overlay(edges, overlay)

    assert [(point.x, point.y) for point in edges[0].waypoints] == [(30.0, 40.0)]
    assert [(point.x, point.y) for point in edges[1].waypoints] == [(10.0, 20.0)]


def test_default_anchor_emits_no_attachment_tokens() -> None:
    import mdg_drawio.generator.generator as generator

    assert generator._anchor_tokens("exit", Anchor()) == ""
    assert generator._anchor_tokens("exit", Anchor(x=0.0, y=0.5)) == (
        "exitX=0.0;exitY=0.5"
    )


# ---------------------------------------------------------------------------
# DSL primitives — parse_keyword_int rejects bool (finding 2.8)
# ---------------------------------------------------------------------------

def test_parse_keyword_int_rejects_bool() -> None:
    with pytest.raises(DslError):
        parse_keyword_int({"variant": True}, "variant", 1, 7)


# ---------------------------------------------------------------------------
# Layout config — aspect ratio validation (finding 2.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["4:0", "0:3", "4:x", "-1:2"])
def test_invalid_aspect_ratio_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="aspect_ratio"):
        parse_aspect_ratio(value)


def test_valid_aspect_ratio_parses() -> None:
    assert parse_aspect_ratio("16:9") == (16, 9)


def test_aspect_ratio_only_expands_margin_adjusted_page() -> None:
    page_width, page_height = resolve_page_size(
        content_width=400,
        content_height=299,
        margin_x=40,
        margin_y=40,
        aspect_ratio="4:3",
    )

    assert page_width >= 480
    assert page_height >= 379
    assert page_width / page_height == pytest.approx(4 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# Registry — unknown library gives an actionable error (finding 2.8)
# ---------------------------------------------------------------------------

def test_load_registry_unknown_library_reports_expected_set() -> None:
    import mdg_drawio.notation._core.registry as reg

    load_registry.cache_clear()
    set_registries({"c4": {"shapes": []}})
    try:
        with pytest.raises(KeyError, match="expected one of"):
            load_registry("nope")
    finally:
        # Reset the module-global cache so other tests re-read from disk.
        reg._registries = None
        load_registry.cache_clear()
