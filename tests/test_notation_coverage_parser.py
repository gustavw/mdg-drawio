"""Regression tests for todo/notation-coverage-parser.md Phase 1.

Each test pins one of the three coverage-sheet failures documented there
(C4's None,None palette-edge form; UML FoundMessage and UML 2.5 Dependency
mixing vertex/edge kinds across their own variants) plus the two bugs found
while fixing them: style resolution ignoring variant entirely, and
PaletteLayout's node/edge reconstruction dropping non-geometry fields.
"""

from __future__ import annotations

import pytest

from mdg_drawio.contracts import Document
from mdg_drawio.notation import DslError, parse

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
