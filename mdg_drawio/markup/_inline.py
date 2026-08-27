"""Inline-span conversion shared by both directions: bold/italic/strike/code/
link/image/line-break, plus the escaping that keeps a round trip faithful.

Not full CommonMark/HTML5 -- a small, table-driven subset (see the package
docstring in ``__init__.py`` for the exact mapping this project commits to).
"""
from __future__ import annotations

import html
import re

# A backslash-escaped markdown-special character is protected from being
# reinterpreted as syntax (e.g. ``\*`` -> a literal ``*``, not emphasis).
# NOT ``_``: this table's italic syntax is ``*text*`` only, so underscore
# carries no meaning here -- escaping it would just add noise to ordinary
# text (identifiers, snake_case, URLs) for no protective benefit.
_ESCAPABLE = r"\\`*~\[\]!|"
_ESCAPE_RE = re.compile(r"\\([" + _ESCAPABLE + r"])")
_MD_SPECIAL_RE = re.compile(r"([" + _ESCAPABLE + r"])")

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^\n]+?)\*\*")
_ITALIC_RE = re.compile(r"\*([^\n*]+?)\*")
_STRIKE_RE = re.compile(r"~~([^\n]+?)~~")

_PLACEHOLDER_RE = re.compile(r"\x00(ESC|CODE)(\d+)\x00")


def _stash(store: list[str], value: str, tag: str) -> str:
    store.append(value)
    return f"\x00{tag}{len(store) - 1}\x00"


def markdown_inline_to_html(text: str) -> str:
    """Convert markdown inline spans in *text* to HTML, escaping any plain
    text so literal ``&``/``<``/``>`` can't be mistaken for real markup.

    Order matters: escaped characters and code spans are protected FIRST (so
    ``` `**not bold**` ``` and ``\\*literal\\*`` survive untouched), then
    images before links (their syntax overlaps), then bold before italic
    (so ``**x**`` isn't read as two italics).
    """
    escapes: list[str] = []
    text = _ESCAPE_RE.sub(lambda m: _stash(escapes, m.group(1), "ESC"), text)

    codes: list[str] = []
    text = _CODE_SPAN_RE.sub(
        lambda m: _stash(codes, html.escape(m.group(1), quote=False), "CODE"), text
    )

    text = html.escape(text, quote=False)
    text = _IMAGE_RE.sub(r'<img src="\2" alt="\1">', text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = text.replace("\n", "<br>\n")

    def _restore(m: re.Match[str]) -> str:
        kind, index = m.group(1), int(m.group(2))
        if kind == "CODE":
            return f"<code>{codes[index]}</code>"
        return escapes[index]

    return _PLACEHOLDER_RE.sub(_restore, text)


# ---------------------------------------------------------------------------
# HTML -> markdown inline
# ---------------------------------------------------------------------------

# One combined left-to-right scan, alternated over named groups, rather than
# sequential whole-string substitutions: a sequential pass has no way to tell
# freshly-inserted markdown syntax (must NOT be escaped) apart from the
# original plain text (MUST be escaped) once they're interleaved in the same
# string. Scanning once and escaping only the untouched gaps between matches
# keeps the two apart by construction. Captured tag content is recursively
# reprocessed (handles nesting, e.g. ``<a href="u"><b>text</b></a>``); an
# attribute value (``href``/``src``/``alt``) is taken verbatim, never
# escaped -- a URL's own underscores etc. are not the author's prose.
_INLINE_TOKEN_RE = re.compile(
    r"<code\b[^>]*>(?P<code>.*?)</code>"
    r"|<img\b(?P<img_attrs>[^>]*)/?>"
    r'|<a\b[^>]*?\bhref="(?P<href>[^"]*)"[^>]*>(?P<link_text>.*?)</a>'
    r"|<(?:b|strong)\b[^>]*>(?P<bold>.*?)</(?:b|strong)>"
    r"|<(?:i|em)\b[^>]*>(?P<italic>.*?)</(?:i|em)>"
    r"|<s\b[^>]*>(?P<strike>.*?)</s>"
    r"|<div\b[^>]*>(?P<div>.*?)</div>"
    r"|<br\s*/?>",
    re.IGNORECASE | re.DOTALL,
)
_IMG_SRC_ATTR_RE = re.compile(r'\bsrc="([^"]*)"', re.IGNORECASE)
_IMG_ALT_ATTR_RE = re.compile(r'\balt="([^"]*)"', re.IGNORECASE)


def _escape_markdown_text(text: str) -> str:
    """Entity-decode, then backslash-escape markdown-special characters in
    real plain text, so it can't be misread as syntax on a later parse."""
    return _MD_SPECIAL_RE.sub(r"\\\1", html.unescape(text))


def _render_inline_token(match: re.Match[str]) -> str:
    """The markdown for one ``_INLINE_TOKEN_RE`` match."""
    if match.group("code") is not None:
        return f"`{html.unescape(match.group('code'))}`"
    if match.group("img_attrs") is not None:
        attrs = match.group("img_attrs")
        src_match = _IMG_SRC_ATTR_RE.search(attrs)
        alt_match = _IMG_ALT_ATTR_RE.search(attrs)
        src = html.unescape(src_match.group(1)) if src_match else ""
        alt = html.unescape(alt_match.group(1)) if alt_match else ""
        return f"![{alt}]({src})"
    if match.group("href") is not None:
        text = html_inline_to_markdown(match.group("link_text"))
        return f"[{text}]({html.unescape(match.group('href'))})"
    if match.group("bold") is not None:
        return f"**{html_inline_to_markdown(match.group('bold'))}**"
    if match.group("italic") is not None:
        return f"*{html_inline_to_markdown(match.group('italic'))}*"
    if match.group("strike") is not None:
        return f"~~{html_inline_to_markdown(match.group('strike'))}~~"
    if match.group("div") is not None:
        # draw.io's own editor wraps each typed line in its own <div> -- a
        # line break, not a semantic block/paragraph -- so a leading "\n"
        # (not the block layer's blank-line-separated "\n\n") is what makes
        # a multi-<div> label read as one multi-line label rather than
        # several disconnected paragraphs. A <div> with nothing before it
        # leaves a leading "\n" a caller is expected to .strip() away
        # (html_to_markdown's entry points already do).
        return "\n" + html_inline_to_markdown(match.group("div"))
    return "\n"  # <br>


def html_inline_to_markdown(fragment: str) -> str:
    """Convert HTML inline markup in *fragment* to markdown, protecting code
    spans (kept literal) and escaping plain text for round-trip fidelity."""
    out: list[str] = []
    pos = 0
    for match in _INLINE_TOKEN_RE.finditer(fragment):
        if match.start() > pos:
            out.append(_escape_markdown_text(fragment[pos : match.start()]))
        out.append(_render_inline_token(match))
        pos = match.end()
    if pos < len(fragment):
        out.append(_escape_markdown_text(fragment[pos:]))
    return "".join(out)
