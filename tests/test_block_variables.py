"""Tests for ``block NAME = \"\"\"...\"\"\"`` text-variable declarations.

A ``block`` statement declares a reusable, multi-line text variable that can
substitute for a string literal anywhere a call expects one (label, keyword
value, data-source part, ...). See ``mdg_drawio/notation/GRAMMAR.md`` for the
grammar and ``extract_block_variables`` in ``dsl_engine.py`` for the
implementation notes this file pins.
"""

from __future__ import annotations

import pytest

from mdg_drawio.contracts import Document, MultiPageDocument
from mdg_drawio.notation import DslError, parse
from mdg_drawio.notation._core.dsl_engine import extract_block_variables

# ---------------------------------------------------------------------------
# extract_block_variables — the pre-pass in isolation
# ---------------------------------------------------------------------------


def test_extract_block_variables_finds_a_declaration_and_strips_it() -> None:
    source = 'block var = """\nhello\nworld\n"""\ngeneral.Text(n1, var)'
    blocks, cleaned = extract_block_variables(source)
    assert blocks == {"var": "hello\nworld"}
    assert "block var" not in cleaned
    assert "general.Text(n1, var)" in cleaned


def test_extract_block_variables_drops_only_one_leading_and_trailing_newline() -> None:
    source = 'block var = """\n\nhello\n\n"""'
    blocks, _ = extract_block_variables(source)
    assert blocks["var"] == "\nhello\n"


def test_extract_block_variables_single_line_content() -> None:
    blocks, _ = extract_block_variables('block var = """hello"""')
    assert blocks["var"] == "hello"


def test_extract_block_variables_first_declaration_wins_on_duplicate_name() -> None:
    source = 'block var = """first"""\nblock var = """second"""'
    blocks, _ = extract_block_variables(source)
    assert blocks["var"] == "first"


def test_extract_block_variables_unclosed_block_is_left_untouched() -> None:
    source = 'block var = """\nhello'
    blocks, cleaned = extract_block_variables(source)
    assert blocks == {}
    assert cleaned == source


# ---------------------------------------------------------------------------
# Substitution into passthrough (foreign-namespace) calls
# ---------------------------------------------------------------------------


def test_block_variable_substitutes_into_a_general_text_label() -> None:
    source = (
        'use general\n'
        'block var = """\n'
        '# hello\n'
        '**world**\n'
        '"""\n'
        'general.Text(n1, var)'
    )
    doc = parse(source)
    assert isinstance(doc, Document)
    assert doc.nodes[0].label == "# hello\n**world**"


def test_block_variable_declared_after_its_use_still_resolves() -> None:
    """Order-independence: the block need not precede the call that uses it."""
    source = (
        'use general\n'
        'general.Text(n1, var)\n'
        'block var = """hello"""'
    )
    doc = parse(source)
    assert isinstance(doc, Document)
    assert doc.nodes[0].label == "hello"


def test_block_variable_substitutes_into_a_general_textbox_third_positional() -> None:
    source = (
        'use general\n'
        'block body = """Body text"""\n'
        'general.Textbox(n1, "Heading", body)'
    )
    doc = parse(source)
    assert isinstance(doc, Document)
    assert doc.nodes[0].extra["description"] == "Body text"


def test_undeclared_bare_identifier_is_kept_as_its_own_spelling() -> None:
    """No matching ``block`` declaration -- unchanged behavior: a bare
    identifier used as a label is kept as its literal spelling."""
    doc = parse('use general\ngeneral.Text(n1, not_a_block)')
    assert isinstance(doc, Document)
    assert doc.nodes[0].label == "not_a_block"


# ---------------------------------------------------------------------------
# Substitution into native c4 calls (node label, edge label, keyword args)
# ---------------------------------------------------------------------------


def test_block_variable_substitutes_into_a_native_c4_node_label() -> None:
    source = 'block var = """Customer"""\nc4.Person(p1, var)'
    doc = parse(source)
    assert isinstance(doc, Document)
    assert doc.nodes[0].label == "Customer"


def test_block_variable_substitutes_into_a_native_c4_edge_label() -> None:
    source = (
        'block var = """Uses"""\n'
        'c4.Person(p1, "P")\n'
        'c4.System(s1, "S")\n'
        'c4.Rel(p1, s1, var)'
    )
    doc = parse(source)
    assert isinstance(doc, Document)
    (edge,) = doc.edges
    assert edge.label == "Uses"


def test_block_variable_substitutes_into_a_native_c4_keyword_argument() -> None:
    source = (
        'block tech = """HTTPS"""\n'
        'c4.Person(p1, "P")\n'
        'c4.System(s1, "S")\n'
        'c4.Rel(p1, s1, "Uses", technology=tech)'
    )
    doc = parse(source)
    assert isinstance(doc, Document)
    (edge,) = doc.edges
    assert edge.extra["technology"] == "HTTPS"


# ---------------------------------------------------------------------------
# A block variable never satisfies an id-typed argument
# ---------------------------------------------------------------------------


def test_block_variable_name_used_as_node_id_is_kept_literal() -> None:
    """A declared block's NAME, used where a node_id is expected, must stay
    the identifier itself -- never resolve to the block's text content."""
    source = 'block var = """not an id"""\nc4.Person(var, "Label")'
    doc = parse(source)
    assert isinstance(doc, Document)
    assert doc.nodes[0].id == "var"


def test_block_variable_name_used_as_edge_endpoint_is_kept_literal() -> None:
    source = (
        'block var = """not an id"""\n'
        'c4.Person(var, "P")\n'
        'c4.System(s1, "S")\n'
        'c4.Rel(var, s1, "Uses")'
    )
    doc = parse(source)
    assert isinstance(doc, Document)
    (edge,) = doc.edges
    assert edge.source_id == "var"


# ---------------------------------------------------------------------------
# Multi-page: declarations are file-wide, visible on every page
# ---------------------------------------------------------------------------


def test_block_variable_is_visible_across_pages_regardless_of_page() -> None:
    source = (
        'use general\n'
        'page "A"\n'
        'general.Text(n1, shared)\n'
        'page "B"\n'
        'block shared = """hello"""\n'
        'general.Text(n2, shared)'
    )
    doc = parse(source)
    assert isinstance(doc, MultiPageDocument)
    page_a, page_b = doc.pages
    assert page_a.nodes[0].label == "hello"
    assert page_b.nodes[0].label == "hello"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_unclosed_block_falls_through_to_a_line_numbered_dsl_error() -> None:
    source = 'block var = """\nhello'
    with pytest.raises(DslError):
        parse(source)
