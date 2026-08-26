"""Tests for :mod:`mdg_drawio.markup` -- markdown <-> HTML conversion for
rich-text shape labels, against the project's committed mapping table.

Three groups: one direction at a time (per table row), round-trip fidelity
(including literal-special-character safety), and a realistic multi-block
document resembling what a ``general.Text``/``general.Note``/``erd.Note``
shape's label commonly holds.
"""

from __future__ import annotations

import pytest

from mdg_drawio.markup import html_to_markdown, markdown_to_html

# ── markdown -> HTML, one table row at a time ────────────────────────────────


@pytest.mark.parametrize(
    ("markdown", "expected_html"),
    [
        ("**bold**", "<p><b>bold</b></p>"),
        ("*italic*", "<p><i>italic</i></p>"),
        ("~~strike~~", "<p><s>strike</s></p>"),
        ("line one\nline two", "<p>line one<br>\nline two</p>"),
        ("---", "<hr>"),
        ("# Heading", "<h1>Heading</h1>"),
        ("## Heading", "<h2>Heading</h2>"),
        ("### Heading", "<h3>Heading</h3>"),
        ("[text](url)", '<p><a href="url">text</a></p>'),
        ("![alt](url)", '<p><img src="url" alt="alt"></p>'),
        ("> quoted", "<blockquote>quoted</blockquote>"),
        ("`code`", "<p><code>code</code></p>"),
    ],
)
def test_markdown_to_html_table_rows(markdown: str, expected_html: str) -> None:
    assert markdown_to_html(markdown) == expected_html


def test_markdown_to_html_paragraph_blank_line_separates() -> None:
    expected = "<p>para one</p>\n<p>para two</p>"
    assert markdown_to_html("para one\n\npara two") == expected


def test_markdown_to_html_unordered_list() -> None:
    assert markdown_to_html("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"


def test_markdown_to_html_ordered_list() -> None:
    assert markdown_to_html("1. a\n2. b") == "<ol><li>a</li><li>b</li></ol>"


def test_markdown_to_html_fenced_code_block_is_not_inline_formatted() -> None:
    assert markdown_to_html("```\n*not italic*\n```") == "<pre>*not italic*</pre>"


def test_markdown_to_html_simple_table() -> None:
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert markdown_to_html(md) == (
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )


def test_markdown_to_html_bold_before_italic_disambiguates() -> None:
    """``**x**`` must read as bold, not two adjacent italics."""
    assert markdown_to_html("**x**") == "<p><b>x</b></p>"


def test_markdown_to_html_escapes_literal_angle_bracket_and_amp() -> None:
    assert markdown_to_html("a < b & c") == "<p>a &lt; b &amp; c</p>"


def test_markdown_to_html_backslash_escape_suppresses_emphasis() -> None:
    assert markdown_to_html(r"\*not italic\*") == "<p>*not italic*</p>"


def test_markdown_to_html_code_span_protects_its_content() -> None:
    assert markdown_to_html("`**not bold**`") == "<p><code>**not bold**</code></p>"


# ── HTML -> markdown, one table row at a time ────────────────────────────────


@pytest.mark.parametrize(
    ("html", "expected_markdown"),
    [
        ("<b>bold</b>", "**bold**"),
        ("<strong>bold</strong>", "**bold**"),
        ("<i>italic</i>", "*italic*"),
        ("<em>italic</em>", "*italic*"),
        ("<s>strike</s>", "~~strike~~"),
        ("<hr>", "---"),
        ("<hr/>", "---"),
        ("<h1>Heading</h1>", "# Heading"),
        ("<h2>Heading</h2>", "## Heading"),
        ("<h3>Heading</h3>", "### Heading"),
        ('<a href="url">text</a>', "[text](url)"),
        ('<img src="url" alt="alt">', "![alt](url)"),
        ('<img alt="alt" src="url">', "![alt](url)"),  # attribute order varies
        ("<blockquote>quoted</blockquote>", "> quoted"),
        ("<code>code</code>", "`code`"),
    ],
)
def test_html_to_markdown_table_rows(html: str, expected_markdown: str) -> None:
    assert html_to_markdown(html) == expected_markdown


def test_html_to_markdown_paragraphs_get_a_blank_line_between() -> None:
    assert html_to_markdown("<p>para one</p><p>para two</p>") == "para one\n\npara two"


def test_html_to_markdown_unordered_list() -> None:
    assert html_to_markdown("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"


def test_html_to_markdown_ordered_list_numbers_sequentially() -> None:
    assert html_to_markdown("<ol><li>a</li><li>b</li></ol>") == "1. a\n2. b"


def test_html_to_markdown_pre_becomes_a_fenced_code_block() -> None:
    assert html_to_markdown("<pre>raw *text*</pre>") == "```\nraw *text*\n```"


def test_html_to_markdown_simple_table() -> None:
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    assert html_to_markdown(html) == "| A | B |\n| --- | --- |\n| 1 | 2 |"


def test_html_to_markdown_bare_inline_with_no_block_wrapper() -> None:
    """A draw.io cell's ``value`` is frequently bare inline markup with no
    ``<p>``/block wrapper at all -- must still convert, not be dropped."""
    assert html_to_markdown("Some <b>bold</b> text") == "Some **bold** text"


def test_html_to_markdown_div_wrapped_lines_join_with_a_single_newline() -> None:
    """draw.io's own editor wraps each typed line in its own <div> -- a line
    break, not a paragraph -- so consecutive <div>s must read as one
    multi-line label (single "\\n"), not blank-line-separated paragraphs
    (the <p> behaviour)."""
    html = "<div>line one</div><div>line two</div><div>line three</div>"
    assert html_to_markdown(html) == "line one\nline two\nline three"


def test_html_to_markdown_bare_text_then_a_trailing_div() -> None:
    """The real bug this guards: leading plain text followed by exactly one
    <div>-wrapped line, no wrapper around the first line at all -- draw.io
    produces this when a user types a second line onto an existing label."""
    html = "Missar vi något här emellan??<div>(spelregler/affärskrav)</div>"
    expected = "Missar vi något här emellan??\n(spelregler/affärskrav)"
    assert html_to_markdown(html) == expected


def test_html_to_markdown_div_content_still_gets_inline_formatting() -> None:
    assert html_to_markdown("<div><b>bold</b> line</div>") == "**bold** line"


def test_html_to_markdown_nested_inline_inside_a_link() -> None:
    html = '<a href="url"><b>bold link</b></a>'
    assert html_to_markdown(html) == "[**bold link**](url)"


def test_html_to_markdown_url_with_underscore_is_never_escaped() -> None:
    """A URL's own characters are structural, not the author's prose --
    escaping them would corrupt the link target."""
    html = '<a href="http://example.com/foo_bar">link</a>'
    assert html_to_markdown(html) == "[link](http://example.com/foo_bar)"


def test_html_to_markdown_decodes_entities_in_plain_text() -> None:
    assert html_to_markdown("Tom &amp; Jerry") == "Tom & Jerry"


def test_html_to_markdown_escapes_a_literal_asterisk_defensively() -> None:
    """A lone, unpaired ``*`` in real prose must not silently become emphasis
    if the resulting markdown is ever re-parsed."""
    assert html_to_markdown("5 * 3") == r"5 \* 3"


def test_html_to_markdown_does_not_escape_underscore() -> None:
    """This table's italic syntax is ``*text*`` only -- underscore has no
    special meaning here, so escaping it would just be noise."""
    assert html_to_markdown("snake_case_var") == "snake_case_var"


def test_html_to_markdown_code_span_content_is_never_escaped() -> None:
    assert html_to_markdown("<code>**not bold**</code>") == "`**not bold**`"


# ── round-trip fidelity ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "markdown",
    [
        "**bold** and *italic* and ~~strike~~",
        "# Title\n\nSome text with a [link](http://example.com/a_b).",
        "- one\n- two\n- three",
        "1. first\n2. second",
        "> a quoted line",
        "para one\n\npara two",
        "text with `inline code` in it",
    ],
)
def test_markdown_html_markdown_round_trip_is_stable(markdown: str) -> None:
    """Going markdown -> HTML -> markdown -> HTML must reproduce the same
    HTML both times, even if the intermediate markdown text isn't
    byte-identical to the original (escaping is allowed to be more
    conservative on the way back)."""
    html_once = markdown_to_html(markdown)
    back = html_to_markdown(html_once)
    html_twice = markdown_to_html(back)
    assert html_once == html_twice


@pytest.mark.parametrize(
    "text",
    [
        "price is $5 * 3 = $15",
        "a < b and c > d",
        "Tom & Jerry",
        "snake_case_var",
        "a [not a link] here",
        "Wow!",
    ],
)
def test_literal_special_characters_survive_a_semantic_round_trip(text: str) -> None:
    """A character that merely LOOKS like syntax, but isn't part of a real
    paired construct, must render identically before and after a round trip
    even though the escaped markdown text itself may differ."""
    html_once = markdown_to_html(text)
    html_twice = markdown_to_html(html_to_markdown(html_once))
    assert html_once == html_twice


# ── a realistic multi-block note/text-shape label ────────────────────────────


def test_realistic_note_shape_content_round_trips() -> None:
    """The kind of rich content a general.Text/general.Note/erd.Note shape's
    label typically holds: a heading, a paragraph with mixed inline
    formatting, a list, and a code block."""
    markdown = (
        "## Open questions\n\n"
        "This affects the **billing** service and *maybe* `retry_count`.\n\n"
        "- confirm the timeout\n"
        "- check with [the team](https://example.com/team)\n\n"
        "```\n"
        "retry_count = 3\n"
        "```"
    )
    html = markdown_to_html(markdown)
    assert html == (
        "<h2>Open questions</h2>\n"
        "<p>This affects the <b>billing</b> service and <i>maybe</i> "
        "<code>retry_count</code>.</p>\n"
        "<ul><li>confirm the timeout</li>"
        '<li>check with <a href="https://example.com/team">the team</a></li></ul>\n'
        "<pre>retry_count = 3</pre>"
    )
    # And it must render identically after a full round trip.
    assert markdown_to_html(html_to_markdown(html)) == html
