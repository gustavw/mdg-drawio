"""Weighted token similarity between draw.io style strings.

A draw.io style is a ``;``-separated list of tokens, each either bare
(``ellipse``) or ``key=value`` (``fillColor=#083F75``). Token order is not
significant. For reverse matching we weight tokens by the job they do:

* **shape-defining** (``shape=``, ``perimeter=``, and any bare token -- the
  built-in shape name) -- high weight; these decide *which* shape.
* **cosmetic** (colour, font, alignment, spacing, opacity) -- small weight, per
  the design: a user recolouring or re-fonting a cell must still match, yet a
  cosmetic *agreement* still tips otherwise-identical candidates apart.
* **structural** (everything else: rounded, dashed, container, ...) -- the
  medium default.

Similarity is the weighted fraction of agreeing tokens over the union of tokens
in the two styles, so a cosmetic difference costs only its small weight while a
shape difference costs a large one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Tokens whose *key* is inherently shape-defining even though it takes a value.
SHAPE_KEYS: Final[frozenset[str]] = frozenset({"shape", "perimeter"})

# Cosmetic tokens: colour, font, text alignment, and other purely-visual keys.
# These get the small weight so UI edits to them barely affect the match.
COSMETIC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "fillColor",
        "strokeColor",
        "fontColor",
        "gradientColor",
        "gradientDirection",
        "labelBackgroundColor",
        "labelBorderColor",
        "swimlaneFillColor",
        "fontFamily",
        "fontSize",
        "fontStyle",
        "align",
        "verticalAlign",
        "verticalLabelPosition",
        "labelPosition",
        "spacing",
        "spacingLeft",
        "spacingRight",
        "spacingTop",
        "spacingBottom",
        "opacity",
        "shadow",
        "glass",
        "sketch",
        "comic",
    }
)

# Bare tokens carry this sentinel value (distinct from a real "" or None value).
BARE: Final[object] = object()


@dataclass(frozen=True)
class Weights:
    """Per-class token weights. Tunable -- the whole point of the design."""

    shape: float = 10.0
    structural: float = 3.0
    cosmetic: float = 1.0


DEFAULT_WEIGHTS: Final[Weights] = Weights()


def parse_style(style: str) -> dict[str, object]:
    """Parse a draw.io style string into ``{key: value}``.

    Bare tokens map to the :data:`BARE` sentinel so they compare equal to each
    other but never to a ``key=value`` token that happens to share the name.
    """
    tokens: dict[str, object] = {}
    for raw in style.split(";"):
        tok = raw.strip()
        if not tok:
            continue
        if "=" in tok:
            key, value = tok.split("=", 1)
            tokens[key.strip()] = value.strip()
        else:
            tokens[tok] = BARE
    return tokens


def token_weight(key: str, value: object, weights: Weights) -> float:
    """Weight for one token, by its class."""
    if value is BARE or key in SHAPE_KEYS:
        return weights.shape
    if key in COSMETIC_KEYS:
        return weights.cosmetic
    return weights.structural


_MISSING: Final[object] = object()


def similarity(
    query: dict[str, object],
    candidate: dict[str, object],
    weights: Weights = DEFAULT_WEIGHTS,
) -> float:
    """Weighted fraction of agreeing tokens over the union of both styles.

    Returns a value in ``[0, 1]``: ``1.0`` iff the token maps are identical.
    """
    numerator = 0.0
    denominator = 0.0
    for key in query.keys() | candidate.keys():
        query_value = query.get(key, _MISSING)
        candidate_value = candidate.get(key, _MISSING)
        present = query_value if query_value is not _MISSING else candidate_value
        weight = token_weight(key, present, weights)
        denominator += weight
        if (
            query_value is not _MISSING
            and candidate_value is not _MISSING
            and query_value == candidate_value
        ):
            numerator += weight
    return numerator / denominator if denominator else 0.0
