"""Diagram layout system — mode dispatch and public API.

Every layout mode is a `BaseLayout` subclass. To add a new mode, create a module
that defines ``LAYOUT_MODE`` and ``LAYOUT_CLASS`` and add it to the import +
registration block below.

This is the sole contract for the ``mdg_drawio.layout`` package. External
consumers import from here, never from submodules.
"""

from __future__ import annotations

from . import layered, palette, process, sequence
from ._container_layout import (
    absolute_node_boxes,
    build_parent_map,
    estimate_text_width,
    regrow_containers_to_fit_children,
)
from ._types import (
    BaseLayout,
    Edge,
    Node,
    Result,
    SizeResolver,
    Waypoint,
)
from .config import Config, ShapeScalingConfig, resolve_page_size
from .layered import padding_dict
from .size_resolver import create_size_resolver, create_style_resolver, scale_node_sizes

_registry: dict[str, type[BaseLayout]] = {}

for _mod in (layered, palette, process, sequence):
    _registry[_mod.LAYOUT_MODE] = _mod.LAYOUT_CLASS


def register_layout(mode: str, layout_cls: type[BaseLayout]) -> None:
    """Register *layout_cls* for *mode* dynamically at runtime.

    Built-in modes self-register at import time. Use this for custom or
    third-party layout classes. Duplicates overwrite silently (last wins).
    """
    _registry[mode] = layout_cls


def dispatch_layout(mode: str) -> type[BaseLayout] | None:
    """Return the layout class for *mode*, or ``None`` if unregistered."""
    return _registry.get(mode)


def modes() -> list[str]:
    """Return all registered mode names."""
    return list(_registry)


__all__ = [
    "BaseLayout",
    "Config",
    "Edge",
    "Node",
    "Result",
    "ShapeScalingConfig",
    "SizeResolver",
    "Waypoint",
    "absolute_node_boxes",
    "build_parent_map",
    "create_size_resolver",
    "create_style_resolver",
    "dispatch_layout",
    "estimate_text_width",
    "modes",
    "padding_dict",
    "register_layout",
    "regrow_containers_to_fit_children",
    "resolve_page_size",
    "scale_node_sizes",
]
