"""Draw.io XML generation and shared data models — public API.

This is the sole contract for the ``mdg_drawio.generator`` package. External
consumers import from here, never from submodules.
"""

from __future__ import annotations

from mdg_drawio.contracts import (
    BoundaryPadding,
    Diagram,
    Document,
    Edge,
    EdgeAnchorOverlay,
    GeometryOverlay,
    GeometryPoint,
    MultiPageDocument,
    Node,
)

from .generator import (
    PaletteStyleProvider,
    StyleProvider,
    create_style_provider,
    generate,
    load_style_overrides,
)
from .overlay import read_overlay, read_overlay_xml
from .xml_utils import to_string

__all__ = [
    "BoundaryPadding",
    "Diagram",
    "Document",
    "Edge",
    "EdgeAnchorOverlay",
    "GeometryOverlay",
    "GeometryPoint",
    "MultiPageDocument",
    "Node",
    "PaletteStyleProvider",
    "StyleProvider",
    "create_style_provider",
    "generate",
    "load_style_overrides",
    "read_overlay",
    "read_overlay_xml",
    "to_string",
]
