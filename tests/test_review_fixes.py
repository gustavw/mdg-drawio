"""Regression tests for the 2026-07-27 code-review remediation.

Each test pins a specific finding fixed in that pass (see
``docs/architecture/README.md`` §5-6 and ``todo/todo.md``). They are grouped by
container to mirror the review.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from mdg_drawio.contracts import Document
from mdg_drawio.engine.validate import validate_generated_xml
from mdg_drawio.layout.config import parse_aspect_ratio
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


def test_node_without_label_defaults_label_to_id() -> None:
    doc = parse('c4.Person(alice)')
    assert isinstance(doc, Document)
    node = next(n for n in doc.nodes if n.id == "alice")
    assert node.label == "alice"


def test_dangling_edge_endpoint_raises_dslerror() -> None:
    # The None *source* is what's rejected — pin the message so an unrelated
    # future DslError can't satisfy the test.
    with pytest.raises(DslError, match="source"):
        parse('c4.Rel(None, bob)')


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
