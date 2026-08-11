"""Concrete SizeResolver wired to pre-loaded registry and styles data.

Given a node type like ``c4.Person``, looks up the shape's palette entry
and returns ``(width, height)``.

Usage::
    size_of = create_size_resolver(registries={...}, styles={...})
    w, h = size_of("c4.Person")
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from mdg_drawio.contracts import (
    DEFAULT_NODE_HEIGHT,
    DEFAULT_NODE_WIDTH,
    index_shapes_by_function,
)

from ._container_layout import estimate_text_width
from ._types import Node, SizeResolver
from .config import Config, ShapeScalingConfig

_ASPECT_RATIO_SEARCH_EXPANSIONS = 8
_ASPECT_RATIO_SEARCH_ITERATIONS = 32
_DIMENSION_PRECISION = 2


@dataclass(frozen=True)
class _TextFragment:
    text: str
    bold: bool
    font_size: int
    line_height: float


def _resolve_shape_entry(
    node_type: str,
    registries: dict[str, dict] | None,
    styles: dict[str, dict] | None,
) -> dict | None:
    """Resolve a node type to its palette style entry (``{shape_id: entry}``).

    Both size and style resolution go through here so a node's dimensions and
    its style string always come from the *same* palette shape. The registry's
    canonical shape id (variant-sorted first) is preferred; a prefix scan of the
    styles keys is the fallback when no registry is available. A row type
    (e.g. uml25's Item/Header/Divider) has no shape id of its own -- it only
    exists nested inside a composite shape -- so it falls back to the
    row-type sidecar last.
    """
    parts = node_type.split(".", 1)
    if len(parts) != 2 or styles is None:
        return None
    library, function = parts
    styles_for_lib = styles.get(library, {})

    if registries is not None:
        reg = registries.get(library, {})
        by_func = index_shapes_by_function(reg.get("shapes", []))
        entries = by_func.get(function)
        if entries:
            entry = styles_for_lib.get(entries[0]["id"])
            if entry is not None:
                return entry

    for shape_id, entry in styles_for_lib.items():
        if shape_id.startswith(f"{library}.{function.lower()}."):
            return entry

    row_types = styles_for_lib.get("_row_types")
    if isinstance(row_types, dict):
        return row_types.get(function)
    return None


def create_size_resolver(
    registries: dict[str, dict] | None = None,
    styles: dict[str, dict] | None = None,
) -> SizeResolver:
    """Build a ``SizeResolver`` backed by pre-loaded data.

    *registries* is ``{library: parsed_yaml_doc}``.
    *styles* is ``{library: {shape_id: entry}}``.
    """

    def size_of(node_type: str) -> tuple[float, float]:
        entry = _resolve_shape_entry(node_type, registries, styles)
        if entry is None:
            return (DEFAULT_NODE_WIDTH, DEFAULT_NODE_HEIGHT)
        raw_w = entry.get("width")
        raw_h = entry.get("height")
        w = float(raw_w) if raw_w is not None else DEFAULT_NODE_WIDTH
        h = float(raw_h) if raw_h is not None else DEFAULT_NODE_HEIGHT
        return (w, h)

    return size_of


def create_style_resolver(
    styles: dict[str, dict] | None = None,
    registries: dict[str, dict] | None = None,
) -> Callable[[str], str]:
    """Build a resolver mapping a node type to its palette style string.

    Shares ``_resolve_shape_entry`` with ``create_size_resolver`` so size and
    style always reference the same palette shape. Lets callers inspect generic
    draw.io layout primitives (e.g. ``childLayout=stackLayout``).
    """

    def style_of(node_type: str) -> str:
        entry = _resolve_shape_entry(node_type, registries, styles)
        if entry is None:
            return ""
        return str(entry.get("style", ""))

    return style_of


def scale_node_sizes(nodes: list[Node], config: Config | None = None) -> None:
    """Grow node geometry to fit text, optionally using symmetric groups.

    This mutates ``nodes`` in-place after palette/default dimensions have been
    resolved. When ``config.shape_scaling.type_groups`` maps several node types
    to the same group, all nodes in that group receive the same final width and
    height. Groups listed in ``aspect_ratio_groups`` grow proportionally.
    """
    scaling = (config or Config()).shape_scaling
    if not scaling.enabled:
        return

    grouped: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        if _is_scaling_candidate(node, scaling):
            grouped[_scale_group(node, scaling)].append(node)

    for group_id, group_nodes in grouped.items():
        aspect_ratio = scaling.aspect_ratio_groups.get(group_id)
        height_scale = scaling.height_scale_groups.get(group_id)
        target_width = 0.0
        target_height = 0.0
        for node in group_nodes:
            width, height = _scaled_size(node, scaling, aspect_ratio)
            target_width = max(target_width, width)
            target_height = max(target_height, height)
        target_height = _apply_height_scale(target_height, height_scale)
        target_width, target_height = _apply_aspect_ratio(
            target_width,
            target_height,
            aspect_ratio,
        )
        for node in group_nodes:
            node.width = max(node.width, target_width)
            node.height = max(node.height, target_height)


def _is_scaling_candidate(node: Node, scaling: ShapeScalingConfig) -> bool:
    if scaling.node_types:
        return node.type in scaling.node_types
    if scaling.type_groups:
        return node.type in scaling.type_groups
    return True


def _scale_group(node: Node, scaling: ShapeScalingConfig) -> str:
    return scaling.type_groups.get(node.type, f"node:{node.id}")


def _scaled_size(
    node: Node,
    scaling: ShapeScalingConfig,
    aspect_ratio: float | None = None,
) -> tuple[float, float]:
    fragments = _text_fragments(node, scaling)
    if not fragments:
        return node.width, node.height

    if aspect_ratio is not None and aspect_ratio > 0:
        return _scaled_aspect_size(node, fragments, scaling, aspect_ratio)

    width = max(node.width, _required_width(fragments, scaling))
    height = max(node.height, _required_height(fragments, width, scaling))
    return width, height


def _scaled_aspect_size(
    node: Node,
    fragments: list[_TextFragment],
    scaling: ShapeScalingConfig,
    aspect_ratio: float,
) -> tuple[float, float]:
    lower = max(node.width, node.height * aspect_ratio)
    upper = max(lower, _initial_aspect_search_width(fragments, scaling))

    for _ in range(_ASPECT_RATIO_SEARCH_EXPANSIONS):
        if _aspect_size_fits(fragments, upper, scaling, aspect_ratio):
            break
        upper *= 2
    else:
        text_height = _required_height(fragments, upper, scaling)
        return _apply_aspect_ratio(upper, text_height, aspect_ratio)

    for _ in range(_ASPECT_RATIO_SEARCH_ITERATIONS):
        mid = (lower + upper) / 2
        if _aspect_size_fits(fragments, mid, scaling, aspect_ratio):
            upper = mid
        else:
            lower = mid
    return _rounded_size(upper, upper / aspect_ratio)


def _initial_aspect_search_width(
    fragments: list[_TextFragment],
    scaling: ShapeScalingConfig,
) -> float:
    if scaling.max_width > 0:
        return scaling.max_width
    return _required_width(fragments, scaling)


def _aspect_size_fits(
    fragments: list[_TextFragment],
    width: float,
    scaling: ShapeScalingConfig,
    aspect_ratio: float,
) -> bool:
    return width / aspect_ratio >= _required_height(fragments, width, scaling)


def _apply_aspect_ratio(
    width: float,
    height: float,
    aspect_ratio: float | None,
) -> tuple[float, float]:
    if aspect_ratio is None or aspect_ratio <= 0 or width <= 0 or height <= 0:
        return width, height
    ratio_width = height * aspect_ratio
    if ratio_width >= width:
        return _rounded_size(ratio_width, height)
    return _rounded_size(width, width / aspect_ratio)


def _apply_height_scale(height: float, height_scale: float | None) -> float:
    if height_scale is None or height_scale <= 0:
        return height
    return height * height_scale


def _rounded_size(width: float, height: float) -> tuple[float, float]:
    return round(width, _DIMENSION_PRECISION), round(height, _DIMENSION_PRECISION)


def _text_fragments(
    node: Node,
    scaling: ShapeScalingConfig,
) -> list[_TextFragment]:
    fragments: list[_TextFragment] = []
    if node.label:
        fragments.extend(_title_fragments(node.label, scaling))
    fragments.extend(
        _extra_text_fragments(node, scaling.leading_extra_text_keys, scaling)
    )
    for part in node.text_parts:
        fragments.extend(_body_fragments(part, scaling))
    fragments.extend(_extra_text_fragments(node, scaling.extra_text_keys, scaling))
    return fragments


def _title_fragments(text: str, scaling: ShapeScalingConfig) -> list[_TextFragment]:
    return [
        _TextFragment(
            text=line,
            bold=True,
            font_size=scaling.title_font_size,
            line_height=scaling.title_line_height,
        )
        for line in _split_text_lines(text)
    ]


def _body_fragments(text: str, scaling: ShapeScalingConfig) -> list[_TextFragment]:
    return [
        _TextFragment(
            text=line,
            bold=False,
            font_size=scaling.font_size,
            line_height=scaling.line_height,
        )
        for line in _split_text_lines(text)
    ]


def _extra_text_fragments(
    node: Node,
    keys: tuple[str, ...],
    scaling: ShapeScalingConfig,
) -> list[_TextFragment]:
    fragments: list[_TextFragment] = []
    for key in keys:
        value = node.extra.get(key)
        if value:
            fragments.extend(_body_fragments(str(value), scaling))
    return fragments


def _split_text_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _required_width(
    fragments: list[_TextFragment],
    scaling: ShapeScalingConfig,
) -> float:
    widest = max(
        estimate_text_width(
            fragment.text,
            font_size=fragment.font_size,
            bold=fragment.bold,
        ) * scaling.width_cushion
        for fragment in fragments
    )
    width = widest + scaling.horizontal_padding
    if scaling.max_width > 0:
        width = min(width, scaling.max_width)
    return width


def _required_height(
    fragments: list[_TextFragment],
    width: float,
    scaling: ShapeScalingConfig,
) -> float:
    available_width = max(width - scaling.horizontal_padding, 1.0)
    height = scaling.extra_line_count * scaling.line_height
    for fragment in fragments:
        height += (
            _wrapped_line_count(fragment, available_width, scaling)
            * fragment.line_height
        )
    if len(fragments) > 1:
        height += (len(fragments) - 1) * scaling.fragment_gap
    return height + scaling.vertical_padding


def _wrapped_line_count(
    fragment: _TextFragment,
    available_width: float,
    scaling: ShapeScalingConfig,
) -> int:
    words = fragment.text.split()
    if not words:
        return 1

    lines = 1
    current_width = 0.0
    space_width = _fragment_text_width(" ", fragment, scaling)
    for word in words:
        word_width = _fragment_text_width(word, fragment, scaling)
        if current_width <= 0:
            lines += max(ceil(word_width / available_width) - 1, 0)
            current_width = _last_line_width(word_width, available_width)
            continue

        next_width = current_width + space_width + word_width
        if next_width <= available_width:
            current_width = next_width
        else:
            lines += max(ceil(word_width / available_width), 1)
            current_width = _last_line_width(word_width, available_width)
    return lines


def _last_line_width(word_width: float, available_width: float) -> float:
    remainder = word_width % available_width
    if remainder > 0:
        return remainder
    return min(word_width, available_width)


def _fragment_text_width(
    text: str,
    fragment: _TextFragment,
    scaling: ShapeScalingConfig,
) -> float:
    return (
        estimate_text_width(
            text,
            font_size=fragment.font_size,
            bold=fragment.bold,
        )
        * scaling.width_cushion
    )
