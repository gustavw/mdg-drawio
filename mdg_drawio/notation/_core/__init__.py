"""Shared notation infrastructure — DSL engine, registries, styles, and build tools."""

from __future__ import annotations

from .dsl_engine import (
    CALL_RE,
    DslError,
    build_pages_document,
    is_none_literal,
    literal_or_name,
    literal_string,
    literal_value,
    parse_block_source,
    parse_call_arguments,
    parse_frontmatter,
    parse_keyword_int,
    split_pages,
    strip_inline_comment,
)
from .normalize import style_fingerprint
from .palette import anchor_cell, flatten_entries
from .registry import (
    LIBRARIES,
    NOTATION_DIR,
    load_registry,
    set_registries,
    shapes_by_function,
    shapes_by_id,
)
from .styles import DATA_DIR

__all__ = [
    "CALL_RE",
    "DATA_DIR",
    "LIBRARIES",
    "NOTATION_DIR",
    "DslError",
    "anchor_cell",
    "build_pages_document",
    "flatten_entries",
    "is_none_literal",
    "literal_or_name",
    "literal_string",
    "literal_value",
    "load_registry",
    "parse_block_source",
    "parse_call_arguments",
    "parse_frontmatter",
    "parse_keyword_int",
    "set_registries",
    "shapes_by_function",
    "shapes_by_id",
    "split_pages",
    "strip_inline_comment",
    "style_fingerprint",
]
