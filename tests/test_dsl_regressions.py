"""Regression coverage for source preprocessing and metadata parsing."""

from __future__ import annotations

import pytest

from mdg_drawio.contracts import Document, MultiPageDocument
from mdg_drawio.notation import DslError, parse


def test_single_page_frontmatter_preserves_all_layout_metadata() -> None:
    doc = parse(
        "---\n"
        "title: One page\n"
        "mode: layered\n"
        "direction: LR\n"
        "grid: true\n"
        "---\n"
        'c4.Person(person, "Person")'
    )

    assert isinstance(doc, Document)
    assert doc.diagram.name == "One page"
    assert doc.diagram.mode == "layered"
    assert doc.diagram.direction == "LR"
    assert doc.diagram.grid is True


def test_frontmatter_accepts_yaml_comments() -> None:
    doc = parse(
        "---\n"
        'title: "A # title" # an actual comment\n'
        "grid: true # enabled\n"
        "---\n"
        'c4.Person(person, "Person")'
    )

    assert isinstance(doc, Document)
    assert doc.diagram.name == "A # title"
    assert doc.diagram.grid is True


def test_commented_diagram_title_does_not_override_metadata() -> None:
    doc = parse(
        'page "Right"\n'
        '# c4.Context_DiagramTitle("Wrong", "Bad")\n'
        'c4.Person(person, "Person")'
    )

    assert isinstance(doc, Document)
    assert doc.diagram.name == "Right"
    assert doc.diagram.description == ""


def test_diagram_title_metadata_resolves_block_variables() -> None:
    doc = parse(
        'block title = """Resolved title"""\n'
        'block description = """Resolved description"""\n'
        "c4.Context_DiagramTitle(title, description)"
    )

    assert isinstance(doc, Document)
    assert doc.diagram.name == "Resolved title"
    assert doc.diagram.description == "Resolved description"


@pytest.mark.parametrize("variant", ["1.1", "1e309"])
def test_variant_must_be_a_finite_integer(variant: str) -> None:
    with pytest.raises(DslError, match=r"line 2: variant= must be an integer"):
        parse(f'use c4\nc4.Person(person, "Person", variant={variant})')


def test_escaped_apostrophe_keeps_hash_inside_single_quoted_string() -> None:
    doc = parse(r"c4.Person(person, 'don\'t # truncate me')")

    assert isinstance(doc, Document)
    assert doc.nodes[0].label == "don't # truncate me"


@pytest.mark.parametrize(
    ("source", "expected_line"),
    [
        (
            "---\ntitle: Page\nmode: layered\n---\n\nnot valid",
            6,
        ),
        (
            'block body = """\nfirst\nsecond\n"""\nnot valid',
            5,
        ),
        (
            'use c4\npage "One"\nc4.Person(one, "One")\npage "Two"\nnot valid',
            5,
        ),
        (
            "use c4\n---\npage: One\n---\nc4.Person(one, \"One\")\n"
            "---\npage: Two\n---\nnot valid",
            9,
        ),
    ],
)
def test_errors_retain_original_source_line(source: str, expected_line: int) -> None:
    with pytest.raises(DslError) as exc_info:
        parse(source)

    assert exc_info.value.line_number == expected_line


def test_global_preamble_is_applied_to_each_frontmatter_page() -> None:
    doc = parse(
        "use general\n"
        "---\n"
        "page: One\n"
        "---\n"
        'general.Text(one, "One")\n'
        "---\n"
        "page: Two\n"
        "---\n"
        'general.Text(two, "Two")'
    )

    assert isinstance(doc, MultiPageDocument)
    assert [[node.type for node in page.nodes] for page in doc.pages] == [
        ["general.Text"],
        ["general.Text"],
    ]
