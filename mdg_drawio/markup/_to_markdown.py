"""HTML -> markdown, block level.

Regex-based, not a general HTML5 parser: recognizes exactly the block tags
this project's mapping table commits to (see ``__init__.py``), flat (no
nested lists/blockquotes/tables). A draw.io cell's ``value`` is frequently
just bare inline markup with no block wrapper at all (e.g. ``Some <b>bold</b>
text``) -- that case is handled too: with no recognized block tag anywhere,
the whole input is treated as one implicit paragraph's inline content.
"""
from __future__ import annotations

import html
import re

from ._inline import html_inline_to_markdown

_BLOCK_TAG_RE = re.compile(
    r"<(?P<tag>h1|h2|h3|p|ul|ol|blockquote|pre|table)\b[^>]*>"
    r"(?P<inner>.*?)</(?P=tag)>"
    r"|(?P<hr><hr\s*/?>)",
    re.IGNORECASE | re.DOTALL,
)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_TABLE_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL_RE = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)


def _render_heading(tag: str, inner: str) -> str:
    level = int(tag[1])
    return f"{'#' * level} {html_inline_to_markdown(inner).strip()}"


def _render_paragraph(inner: str) -> str:
    return html_inline_to_markdown(inner).strip()


def _render_blockquote(inner: str) -> str:
    body = html_inline_to_markdown(inner).strip()
    return "\n".join(f"> {line}" for line in body.splitlines()) or ">"


def _render_pre(inner: str) -> str:
    return "```\n" + html.unescape(inner) + "\n```"


def _render_list(inner: str, *, ordered: bool) -> str:
    items = _LIST_ITEM_RE.findall(inner)
    lines = []
    for n, item in enumerate(items, start=1):
        text = html_inline_to_markdown(item).strip()
        prefix = f"{n}. " if ordered else "- "
        lines.append(f"{prefix}{text}")
    return "\n".join(lines)


def _render_table(inner: str) -> str:
    rows = [_TABLE_CELL_RE.findall(row) for row in _TABLE_ROW_RE.findall(inner)]
    if not rows:
        return ""
    header, *body = rows
    cells = [html_inline_to_markdown(c).strip() for c in header]
    lines = [
        "| " + " | ".join(cells) + " |",
        "| " + " | ".join("---" for _ in cells) + " |",
    ]
    for row in body:
        row_cells = [html_inline_to_markdown(c).strip() for c in row]
        lines.append("| " + " | ".join(row_cells) + " |")
    return "\n".join(lines)


def _render_block(tag: str, inner: str) -> str:
    if tag == "hr":
        return "---"
    if tag in ("h1", "h2", "h3"):
        return _render_heading(tag, inner)
    if tag == "p":
        return _render_paragraph(inner)
    if tag == "blockquote":
        return _render_blockquote(inner)
    if tag == "pre":
        return _render_pre(inner)
    if tag == "ul":
        return _render_list(inner, ordered=False)
    if tag == "ol":
        return _render_list(inner, ordered=True)
    if tag == "table":
        return _render_table(inner)
    raise AssertionError(f"unhandled block tag {tag!r}")  # pragma: no cover


def html_to_markdown(source: str) -> str:
    """Convert *source* (HTML, per this package's mapping table) to markdown."""
    matches = list(_BLOCK_TAG_RE.finditer(source))
    if not matches:
        return html_inline_to_markdown(source).strip()

    blocks: list[str] = []
    cursor = 0
    for match in matches:
        loose = source[cursor : match.start()]
        if loose.strip():
            blocks.append(_render_paragraph(loose))
        if match.group("hr"):
            blocks.append("---")
        else:
            tag = match.group("tag").lower()
            blocks.append(_render_block(tag, match.group("inner")))
        cursor = match.end()
    trailing = source[cursor:]
    if trailing.strip():
        blocks.append(_render_paragraph(trailing))
    return "\n\n".join(blocks)
