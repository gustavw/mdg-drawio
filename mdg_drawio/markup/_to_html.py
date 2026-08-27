"""Markdown -> HTML, block level.

Line-based, not a general CommonMark implementation: recognizes exactly the
constructs this project's mapping table commits to (see ``__init__.py``),
nothing more. Blocks are separated by blank lines; each recognized block
consumes its own run of consecutive matching lines.
"""
from __future__ import annotations

import html
import re

from ._inline import markdown_inline_to_html

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_HR_RE = re.compile(r"^-{3,}\s*$")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BLOCKQUOTE_LINE_RE = re.compile(r"^>\s?(.*)$")
_UL_ITEM_RE = re.compile(r"^-\s+(.*)$")
_OL_ITEM_RE = re.compile(r"^\d+\.\s+(.*)$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    start = 0
    for index, char in enumerate(stripped):
        if char != "|":
            continue
        preceding_backslashes = 0
        cursor = index - 1
        while cursor >= 0 and stripped[cursor] == "\\":
            preceding_backslashes += 1
            cursor -= 1
        if preceding_backslashes % 2 == 0:
            cells.append(stripped[start:index].strip())
            start = index + 1
    cells.append(stripped[start:].strip())
    return cells


def _consume_fenced_code(lines: list[str], start: int) -> tuple[int, str]:
    i = start + 1
    content: list[str] = []
    while i < len(lines) and not _FENCE_RE.match(lines[i]):
        content.append(lines[i])
        i += 1
    end = i + 1 if i < len(lines) else i  # skip the closing fence, if any
    joined = "\n".join(content)
    return end, f"<pre>{html.escape(joined, quote=False)}</pre>"


def _consume_blockquote(lines: list[str], start: int) -> tuple[int, str]:
    i = start
    content: list[str] = []
    while i < len(lines) and _BLOCKQUOTE_LINE_RE.match(lines[i]):
        match = _BLOCKQUOTE_LINE_RE.match(lines[i])
        assert match is not None
        content.append(match.group(1))
        i += 1
    inner = markdown_inline_to_html("\n".join(content))
    return i, f"<blockquote>{inner}</blockquote>"


def _consume_list(lines: list[str], start: int, *, ordered: bool) -> tuple[int, str]:
    item_re = _OL_ITEM_RE if ordered else _UL_ITEM_RE
    i = start
    items: list[str] = []
    while i < len(lines) and item_re.match(lines[i]):
        match = item_re.match(lines[i])
        assert match is not None
        items.append(match.group(1))
        i += 1
    tag = "ol" if ordered else "ul"
    rendered = "".join(f"<li>{markdown_inline_to_html(item)}</li>" for item in items)
    return i, f"<{tag}>{rendered}</{tag}>"


def _looks_like_table(lines: list[str], i: int) -> bool:
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and "|" in lines[i + 1]
        and bool(_TABLE_SEPARATOR_RE.match(lines[i + 1]))
    )


def _consume_table(lines: list[str], start: int) -> tuple[int, str]:
    header = _split_table_row(lines[start])
    i = start + 2  # skip the header row and the separator row
    rows: list[list[str]] = []
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        rows.append(_split_table_row(lines[i]))
        i += 1
    head_html = "".join(
        f"<th>{markdown_inline_to_html(cell)}</th>" for cell in header
    )
    def _render_row(row: list[str]) -> str:
        cells = "".join(f"<td>{markdown_inline_to_html(c)}</td>" for c in row)
        return f"<tr>{cells}</tr>"

    body_html = "".join(_render_row(row) for row in rows)
    return i, f"<table><tr>{head_html}</tr>{body_html}</table>"


def _consume_paragraph(lines: list[str], start: int) -> tuple[int, str]:
    i = start
    content: list[str] = []
    while i < len(lines) and lines[i].strip() and not _starts_new_block(lines, i):
        content.append(lines[i])
        i += 1
    joined = "\n".join(content)
    return i, f"<p>{markdown_inline_to_html(joined)}</p>"


def _starts_new_block(lines: list[str], i: int) -> bool:
    line = lines[i]
    return bool(
        _FENCE_RE.match(line)
        or _HR_RE.match(line)
        or _HEADING_RE.match(line)
        or _BLOCKQUOTE_LINE_RE.match(line)
        or _UL_ITEM_RE.match(line)
        or _OL_ITEM_RE.match(line)
        or _looks_like_table(lines, i)
    )


def markdown_to_html(text: str) -> str:
    """Convert *text* (markdown, per this package's mapping table) to HTML."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _FENCE_RE.match(line):
            i, block = _consume_fenced_code(lines, i)
        elif _HR_RE.match(line):
            block, i = "<hr>", i + 1
        elif (heading := _HEADING_RE.match(line)) is not None:
            level = len(heading.group(1))
            inner = markdown_inline_to_html(heading.group(2).strip())
            block = f"<h{level}>{inner}</h{level}>"
            i += 1
        elif _BLOCKQUOTE_LINE_RE.match(line):
            i, block = _consume_blockquote(lines, i)
        elif _UL_ITEM_RE.match(line):
            i, block = _consume_list(lines, i, ordered=False)
        elif _OL_ITEM_RE.match(line):
            i, block = _consume_list(lines, i, ordered=True)
        elif _looks_like_table(lines, i):
            i, block = _consume_table(lines, i)
        else:
            i, block = _consume_paragraph(lines, i)
        blocks.append(block)
    return "\n".join(blocks)
