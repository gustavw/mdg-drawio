"""Markdown <-> HTML conversion for rich-text shape labels (draw.io cells
render a label as HTML when their style sets ``html=1``).

A small, table-driven subset -- not a general CommonMark or HTML5
implementation. Exactly these constructs convert, both ways:

| draw.io / HTML              | Markdown            | Notes         |
|------------------------------|----------------------|----------------|
| ``<b>``/``<strong>``         | ``**text**``         | Bold           |
| ``<i>``/``<em>``             | ``*text*``           | Italic         |
| ``<s>``                      | ``~~text~~``         | Strikethrough  |
| ``<br>``                     | newline / ``<br>``   | Line break     |
| ``<p>``                      | blank line between   | Paragraph      |
| ``<hr>``                     | ``---``              | Horizontal rule|
| ``<h1>``                     | ``# H``              | Heading 1      |
| ``<h2>``                     | ``## H``             | Heading 2      |
| ``<h3>``                     | ``### H``            | Heading 3      |
| ``<ul><li>``                 | ``- item``           | Unordered list |
| ``<ol><li>``                 | ``1. item``          | Ordered list   |
| ``<a href=url>text</a>``     | ``[text](url)``      | Link           |
| ``<img src=url alt=a>``      | ``![a](url)``        | Image          |
| ``<blockquote>``             | ``> text``           | Blockquote     |
| ``<pre>``                    | fenced code block    | Code block     |
| ``<code>``                   | `` `text` ``         | Inline code    |
| ``<table>``                  | markdown table       | Simple tables  |

Both directions escape/unescape defensively so a plain-text character that
happens to look like syntax (a literal ``*``, a literal ``<``) survives a
round trip instead of being silently reinterpreted -- see
``mdg_drawio.markup._inline`` for exactly how.

Standalone utility, not (yet) wired into the DSL or the generator: nothing
here changes how a ``.mdg`` string literal is parsed or how a label gets
written to ``.drawio`` XML today.
"""
from __future__ import annotations

from ._to_html import markdown_to_html
from ._to_markdown import html_to_markdown

__all__ = ["html_to_markdown", "markdown_to_html"]
