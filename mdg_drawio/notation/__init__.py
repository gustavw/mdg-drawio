"""Notation DSL parsers and shape infrastructure — public API.

This is the sole contract for the ``mdg_drawio.notation`` package. External
consumers import from here, never from submodules.
"""

from __future__ import annotations

from ._core.dsl_engine import DslError, split_pages
from ._core.registry import (
    LIBRARIES,
    NOTATION_DIR,
    load_registry,
    set_registries,
    shapes_by_function,
    shapes_by_id,
)
from ._core.styles import DATA_DIR
from .c4 import parse

__all__ = [
    "DATA_DIR",
    "LIBRARIES",
    "NOTATION_DIR",
    "DslError",
    "load_registry",
    "parse",
    "set_registries",
    "shapes_by_function",
    "shapes_by_id",
    "split_pages",
]
