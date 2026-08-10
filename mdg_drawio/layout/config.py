"""Layout configuration — notation-modifiable parameters.

A ``Config`` is the single source of truth for margins, gaps, direction,
aspect ratio, and boundary padding. Each notation module exports a
``get_layout_config(mode: str) -> Config`` function. The CLI resolves the
config for the active notation/mode and passes it to ``BaseLayout.apply()``.

Layout algorithms read from ``config`` exclusively — they carry no hard-coded
defaults of their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mdg_drawio.contracts import (
    DEFAULT_BOTTOM_PADDING,
    DEFAULT_MARGIN_X,
    DEFAULT_MARGIN_Y,
    DEFAULT_TOP_PADDING,
    BoundaryPadding,
)


@dataclass
class ShapeScalingConfig:
    """Text-driven node sizing policy.

    When enabled, nodes are grown to fit their text. ``type_groups`` lets a
    notation opt into symmetric sizing: every node type mapped to the same
    group receives the maximum required width and height for that group.
    """

    enabled: bool = False
    node_types: set[str] = field(default_factory=set)
    type_groups: dict[str, str] = field(default_factory=dict)
    aspect_ratio_groups: dict[str, float] = field(default_factory=dict)
    height_scale_groups: dict[str, float] = field(default_factory=dict)
    leading_extra_text_keys: tuple[str, ...] = ()
    extra_text_keys: tuple[str, ...] = ("technology",)
    horizontal_padding: float = 36
    vertical_padding: float = 34
    font_size: int = 11
    title_font_size: int = 16
    line_height: float = 14
    title_line_height: float = 18
    fragment_gap: float = 3
    extra_line_count: float = 0
    width_cushion: float = 1.12
    max_width: float = 420


@dataclass
class Config:
    """Notation-adjustable layout parameters.

    Every field has a sensible default.  Per-notation overrides go in
    ``mdg_drawio/notation/<lib>/layout.py``.
    """

    margin_x: float = DEFAULT_MARGIN_X
    margin_y: float = DEFAULT_MARGIN_Y
    column_gap: float = 40
    row_gap: float = 50
    rank_gap: float = 80
    direction: str = "LR"
    aspect_ratio: str | None = "4:3"
    boundary_padding: BoundaryPadding = field(
        default_factory=lambda: BoundaryPadding(
            top=DEFAULT_TOP_PADDING,
            right=DEFAULT_TOP_PADDING,
            bottom=DEFAULT_BOTTOM_PADDING,
            left=DEFAULT_TOP_PADDING,
        )
    )
    shape_scaling: ShapeScalingConfig = field(default_factory=ShapeScalingConfig)
    rank_exclude_ids: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Page size resolution
# ---------------------------------------------------------------------------


def parse_aspect_ratio(value: str) -> tuple[int, int]:
    """Parse ``"W:H"`` into ``(width, height)`` positive integers."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"aspect_ratio must be 'W:H', got {value!r}")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"aspect_ratio must be 'W:H' with positive integers, got {value!r}"
        ) from exc
    if width <= 0 or height <= 0:
        raise ValueError(
            f"aspect_ratio must be 'W:H' with positive integers, got {value!r}"
        )
    return width, height


def resolve_page_size(
    *,
    content_width: float,
    content_height: float,
    margin_x: float,
    margin_y: float,
    aspect_ratio: str | None,
) -> tuple[int, int]:
    """Compute page dimensions from content bounds and optional aspect ratio.

    The page is at least as large as the content + margins. If the content
    aspect differs from *aspect_ratio*, the page is expanded to fill the ratio
    so the diagram sits comfortably on a standard canvas. If *aspect_ratio* is
    empty or ``None``, the content bounds plus margins are used directly.
    """
    if not nodes_placed(content_width, content_height):
        page_w = margin_x * 2
        page_h = margin_y * 2
    else:
        page_w = content_width + 2 * margin_x
        page_h = content_height + 2 * margin_y

        if aspect_ratio:
            a_w, a_h = parse_aspect_ratio(aspect_ratio)
            target_ratio = a_w / a_h
            content_ratio = content_width / content_height

            if content_ratio > target_ratio:
                page_h = page_w * a_h / a_w
            else:
                page_w = page_h * a_w / a_h

    return int(page_w), int(page_h)


def nodes_placed(content_width: float, content_height: float) -> bool:
    """True when content extents are non-zero (nodes were laid out)."""
    return content_width > 0 and content_height > 0
