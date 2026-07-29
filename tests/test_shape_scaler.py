"""Text-driven shape scaling tests."""

from __future__ import annotations

import pytest

from mdg_drawio.contracts import Node
from mdg_drawio.layout import Config, ShapeScalingConfig, scale_node_sizes


def test_shape_scaler_grows_grouped_nodes_symmetrically() -> None:
    nodes = [
        Node(id="short", type="c4.System", label="API", width=100, height=50),
        Node(
            id="long",
            type="c4.System_Ext",
            label="External analytics platform with a long visible name",
            text_parts=["Stores operational telemetry for reporting."],
            width=100,
            height=50,
        ),
        Node(id="other", type="c4.Person", label="User", width=100, height=50),
    ]
    config = Config(
        shape_scaling=ShapeScalingConfig(
            enabled=True,
            type_groups={
                "c4.System": "system",
                "c4.System_Ext": "system",
            },
        )
    )

    scale_node_sizes(nodes, config)

    assert (nodes[0].width, nodes[0].height) == (nodes[1].width, nodes[1].height)
    assert nodes[0].width > 100 or nodes[0].height > 50
    assert (nodes[2].width, nodes[2].height) == (100, 50)


def test_shape_scaler_can_scale_individual_nodes_without_groups() -> None:
    nodes = [
        Node(
            id="long",
            type="example.Box",
            label="A generic notation node with long text",
            width=80,
            height=30,
        )
    ]
    config = Config(shape_scaling=ShapeScalingConfig(enabled=True))

    scale_node_sizes(nodes, config)

    # Long text forces growth on both axes; a no-op / constant scaler fails here.
    assert nodes[0].width > 80
    assert nodes[0].height > 30


def test_shape_scaler_preserves_group_aspect_ratio() -> None:
    nodes = [
        Node(
            id="person",
            type="example.Person",
            label="Actor with a long visible name",
            width=100,
            height=50,
        ),
        Node(
            id="person_ext",
            type="example.PersonExt",
            label="External actor",
            width=100,
            height=50,
        ),
    ]
    config = Config(
        shape_scaling=ShapeScalingConfig(
            enabled=True,
            type_groups={
                "example.Person": "person",
                "example.PersonExt": "person",
            },
            aspect_ratio_groups={"person": 2.0},
        )
    )

    scale_node_sizes(nodes, config)

    assert (nodes[0].width, nodes[0].height) == (nodes[1].width, nodes[1].height)
    assert nodes[0].width / nodes[0].height == pytest.approx(2.0)


def test_shape_scaler_can_apply_group_height_scale() -> None:
    unscaled_nodes = [
        Node(
            id="long",
            type="example.Box",
            label="Long text box",
            text_parts=[
                "This text wraps across several lines so the text-driven height "
                "estimate is larger than the palette height."
            ],
            width=80,
            height=20,
        )
    ]
    scaled_nodes = [
        Node(
            id="long",
            type="example.Box",
            label="Long text box",
            text_parts=[
                "This text wraps across several lines so the text-driven height "
                "estimate is larger than the palette height."
            ],
            width=80,
            height=20,
        )
    ]
    base_scaling = ShapeScalingConfig(
        enabled=True,
        type_groups={"example.Box": "box"},
        max_width=180,
    )

    scale_node_sizes(unscaled_nodes, Config(shape_scaling=base_scaling))
    scale_node_sizes(
        scaled_nodes,
        Config(
            shape_scaling=ShapeScalingConfig(
                enabled=True,
                type_groups={"example.Box": "box"},
                height_scale_groups={"box": 0.8},
                max_width=180,
            )
        ),
    )

    assert scaled_nodes[0].height < unscaled_nodes[0].height
    assert scaled_nodes[0].height == pytest.approx(unscaled_nodes[0].height * 0.8)


def _height_for_text(text_parts: list[str], max_width: float) -> float:
    """Scale one fixed-width node holding *text_parts* and return its height."""
    node = Node(
        id="wrap",
        type="example.Box",
        label="Title",
        text_parts=text_parts,
        width=100,
        height=20,
    )
    scale_node_sizes(
        [node],
        Config(shape_scaling=ShapeScalingConfig(enabled=True, max_width=max_width)),
    )
    return node.height


def test_wrapped_text_height_grows_with_more_lines() -> None:
    """A capped width forces wrapping; more text must yield more height."""
    short = _height_for_text(["One short line."], max_width=160)
    long = _height_for_text(
        ["This body is long enough that it must wrap onto several lines."],
        max_width=160,
    )

    assert long > short


def test_wrapped_text_height_is_monotonic_in_content() -> None:
    """Adding words never shrinks the estimated height at a fixed width."""
    base = ["alpha beta gamma"]
    more = ["alpha beta gamma delta epsilon zeta eta theta iota kappa"]

    # 3 words → 10 words at a fixed narrow width forces strictly more wrapped
    # lines, so height must strictly increase (a constant-height mutation fails).
    assert _height_for_text(more, max_width=140) > _height_for_text(
        base, max_width=140
    )


def test_single_word_wider_than_box_still_scales() -> None:
    """A word longer than the available width must not error or lose height.

    This exercises the long-word overflow branch of the wrap estimator, where a
    single unbreakable token spans more than one line.
    """
    height = _height_for_text(
        ["Supercalifragilisticexpialidocioussupercalifragilistic"],
        max_width=120,
    )

    assert height > 20
