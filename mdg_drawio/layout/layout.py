"""Canonical layout base class and contract types — re-exported from ``._types``.

Every layout mode (``layered``, ``sequence``, ``process``, ``palette``) inherits
from `BaseLayout` and returns a `Result`. The drawio XML generator relies
only on this contract — it never reaches into layout internals.

This module is a re-export shim. All types live in ``._types`` (the type leaf)
to avoid circular imports. New code should import directly from ``._types``.
"""

from __future__ import annotations

from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
    Waypoint,
)

__all__ = [
    "BaseLayout",
    "Edge",
    "Node",
    "Result",
    "SizeResolver",
    "Waypoint",
]
