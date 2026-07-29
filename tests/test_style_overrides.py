"""Tests for per-type rendering overrides via the injected StyleProvider.

Each type may carry a ``style`` block (draw.io tokens merged onto the palette
base style) and a ``padding`` block (extra inner insets consumed by the layout).
The mechanism is notation-agnostic; it is seeded with the UML Package top-left
header rule and its matching header clearance.
"""

from __future__ import annotations

import pytest

import mdg_drawio.generator.generator as gen
import mdg_drawio.layout._container_layout as clayout
from mdg_drawio.contracts import PALETTE_MODE, Diagram, Document, Edge, Node
from mdg_drawio.engine.convert import _annotate_type_padding
from mdg_drawio.generator import create_style_provider, generate


def _provider(overrides: dict) -> gen.PaletteStyleProvider:
    """A StyleProvider with no palette data — isolates override behaviour."""
    return create_style_provider(registries={}, styles={}, overrides=overrides)


def _tokens(style: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for part in style.split(";"):
        if not part:
            continue
        key, _, value = part.partition("=")
        out[key] = value if "=" in part else None
    return out


def test_committed_config_pins_uml_package_top_left() -> None:
    """The seeded config aligns the UML Package header to the top-left."""
    provider = create_style_provider(registries={}, styles={})  # committed config

    assert provider.style_corrections("uml.Package") == {
        "align": "left",
        "verticalAlign": "top",
    }
    base = "shape=folder;fontStyle=1;tabPosition=left;html=1;whiteSpace=wrap;"
    corrections = provider.style_corrections("uml.Package")
    result = _tokens(gen._apply_corrections(base, corrections))
    assert result["align"] == "left"
    assert result["verticalAlign"] == "top"


def test_committed_config_gives_uml_package_top_clearance() -> None:
    """The seeded config adds top padding so inner shapes clear the header."""
    provider = create_style_provider(registries={}, styles={})

    assert provider.type_padding("uml.Package").get("top") == 24
    assert provider.type_padding("uml.Class") == {}  # untargeted type


def test_override_replaces_an_existing_token() -> None:
    """An override for an attribute already in the base replaces it cleanly."""
    provider = _provider({"uml.Class": {"style": {"align": "left"}}})
    corrections = provider.style_corrections("uml.Class")
    result = gen._apply_corrections(
        "swimlane;align=center;verticalAlign=top;", corrections
    )

    assert "align=left" in result
    assert "align=center" not in result
    assert result.count("align=") == 1


def test_null_value_deletes_a_token() -> None:
    """A ``null`` override value removes the attribute from the base style."""
    provider = _provider({"x.Widget": {"style": {"collapsible": None}}})
    corrections = provider.style_corrections("x.Widget")
    result = gen._apply_corrections("swimlane;collapsible=1;html=1;", corrections)

    assert "collapsible" not in result
    assert "swimlane" in result and "html=1" in result


def test_untargeted_type_is_unchanged() -> None:
    provider = _provider({"uml.Package": {"style": {"align": "left"}}})
    base = "swimlane;align=center;verticalAlign=top;"

    assert gen._apply_corrections(base, provider.style_corrections("uml.Class")) == base


def test_mechanism_is_notation_agnostic() -> None:
    """Any ``<library>.<Shape>`` type can be overridden, not just UML."""
    provider = _provider(
        {"c4.Container": {"style": {"align": "left"}, "padding": {"top": 12}}}
    )
    result = gen._apply_corrections(
        "rounded=1;align=center;", provider.style_corrections("c4.Container")
    )

    assert "align=left" in result and "align=center" not in result
    assert provider.type_padding("c4.Container") == {"top": 12}


def test_palette_mode_bypasses_style_overrides() -> None:
    """Palette/golden output renders verbatim — overrides must be skipped."""
    provider = _provider({"uml.Package": {"style": {"align": "left"}}})
    doc = Document(
        diagram=Diagram(mode=PALETTE_MODE),
        nodes=[Node(id="p", type="uml.Package", label="P")],
    )

    assert "align=left" not in generate(doc, provider)


def test_non_palette_mode_applies_style_overrides() -> None:
    provider = _provider({"uml.Package": {"style": {"align": "left"}}})
    doc = Document(
        diagram=Diagram(mode="layered"),
        nodes=[Node(id="p", type="uml.Package", label="P")],
    )

    assert "align=left" in generate(doc, provider)


def test_convert_injects_type_padding_into_node_extra() -> None:
    """``_annotate_type_padding`` copies the provider's padding into node.extra."""
    provider = _provider({"uml.Package": {"padding": {"top": 24}}})
    package = Node(id="p", type="uml.Package", label="P")
    other = Node(id="c", type="uml.Class", label="C")

    _annotate_type_padding([package, other], provider)

    assert package.extra["padding_extra_top"] == 24.0
    assert "padding_extra_top" not in other.extra


def test_layout_adds_extra_padding_on_top_of_the_default() -> None:
    """``_padding_values`` adds ``padding_extra_*`` to the resolved inset."""
    package = Node(id="p", type="uml.Package", label="P")
    package.extra["padding_extra_top"] = 24.0
    default = {"top": 30.0, "right": 20.0, "bottom": 20.0, "left": 20.0}

    top, right, bottom, left = clayout._padding_values(package, default)

    assert top == 54.0
    assert (right, bottom, left) == (20.0, 20.0, 20.0)


def test_committed_config_validates_clean() -> None:
    gen._validate_overrides(
        {"overrides": {"uml.Package": {"style": {"align": "left"},
                                       "padding": {"top": 24}}}}
    )


def _pkg(entry: object) -> dict[str, object]:
    return {"overrides": {"uml.Package": entry}}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("not a mapping", "top level must be a mapping"),
        ({"overides": {}}, "unknown top-level key"),
        ({"overrides": ["uml.Package"]}, "'overrides' must be a mapping"),
        (_pkg("not a mapping"), "must be a mapping of sections"),
        (_pkg({"stlye": {"align": "left"}}), "unknown section"),
        (_pkg({"style": "align=left"}), "style must be a mapping"),
        (_pkg({"padding": "top=5"}), "padding must be a mapping"),
        (_pkg({"padding": {"topp": 5}}), "unknown side"),
        (_pkg({"padding": {"top": "big"}}), "must be a number"),
        (_pkg({"padding": {"top": True}}), "must be a number"),
    ],
)
def test_malformed_config_is_rejected(bad: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        gen._validate_overrides(bad)


def test_style_only_entry_without_padding_validates() -> None:
    """An entry with a style block but no padding is valid (padding optional)."""
    gen._validate_overrides(
        {"overrides": {"uml.Package": {"style": {"align": "left"}}}}
    )


def test_load_style_overrides_reads_committed_config() -> None:
    """The committed override config loads + validates via the real file path."""
    gen.load_style_overrides.cache_clear()  # force the read path, not a cache hit
    loaded = gen.load_style_overrides()
    assert isinstance(loaded, dict)
    # Each entry maps sections to attr dicts (the shape the factory consumes).
    for entry in loaded.values():
        assert all(isinstance(attrs, dict) for attrs in entry.values())


def test_coerce_variant_falls_back_on_non_numeric() -> None:
    """Untyped notation data: a bad ``variant`` degrades to 1, never crashes."""
    assert gen._coerce_variant(Edge(id="e", type="c4.Rel", extra={"variant": "x"})) == 1
    assert gen._coerce_variant(Edge(id="e", type="c4.Rel", extra={"variant": 3})) == 3
    assert gen._coerce_variant(Edge(id="e", type="c4.Rel")) == 1
