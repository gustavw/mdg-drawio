"""Shared layout types — this module is the type leaf.

Every submodule in ``mdg_drawio.layout`` imports shared types from here.
``Node`` and ``Edge`` are re-exported from ``generator.models`` — layout
mutates them in-place. ``Waypoint`` is ``GeometryPoint`` from generator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from mdg_drawio.contracts import Edge, GeometryPoint, Node

if TYPE_CHECKING:
    from .config import Config

# Alias for layout code that uses Waypoint
Waypoint = GeometryPoint


@dataclass
class Result:
    """Complete output from any layout. The generator consumes exactly this."""

    nodes: list[Node]
    edges: list[Edge]
    page_width: int
    page_height: int


class SizeResolver(Protocol):
    """Return ``(width, height)`` for a node type string."""

    def __call__(self, node_type: str) -> tuple[float, float]: ...


def resolve_node_size(
    size_of: SizeResolver, node: Node
) -> tuple[float, float]:
    """Resolve a node's size while preserving the legacy resolver port.

    Registry-backed resolvers expose ``resolve_variant`` so variants can have
    distinct geometry.  Hand-written/test resolvers that implement only the
    original ``Callable[[str], tuple[float, float]]`` contract keep working.
    """
    variant_resolver = getattr(size_of, "resolve_variant", None)
    if callable(variant_resolver):
        width, height = variant_resolver(node.type, node.variant)
        return float(width), float(height)
    width, height = size_of(node.type)
    return float(width), float(height)


class BaseLayout(ABC):
    """Canonical base for every layout mode.

    Subclasses override ``apply()`` to compute positions and routing.
    Configuration is passed via *config* — layout algorithms carry no
    hard-coded spacing defaults of their own.
    """

    @abstractmethod
    def apply(
        self,
        nodes: list[Node],
        edges: list[Edge],
        size_of: SizeResolver,
        config: Config | None = None,
    ) -> Result:
        """Compute positions for *nodes* and route *edges*.

        Must return a `Result` with every node's geometry resolved and
        every edge's waypoints populated.

        *config* provides notation-specific margins, gaps, direction, and
        aspect ratio.  When ``None``, ``Config()`` defaults are used.
        """
        ...
