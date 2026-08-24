"""Engine entry point for the `mdg notation` subcommand.

Prints the DSL palette a notation library exposes -- every function/variant,
its draw.io shape kind, and a ready-to-adapt example call -- so an agent (or
a human) can discover what's callable without reading registry YAML
directly. Read-only: loads the committed registries, never touches
generated_data or writes anything.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from mdg_drawio.notation import LIBRARIES, load_registry


@dataclass(frozen=True)
class ShapeSummary:
    """One registry shape entry, reduced to what's useful for discovery."""

    function: str
    variant: int
    kind: str
    menu_name: str
    example: str


def _shape_summaries(library: str) -> list[ShapeSummary]:
    registry = load_registry(library)
    shapes = registry.get("shapes", [])
    return [
        ShapeSummary(
            function=str(entry["function"]),
            variant=int(entry.get("variant", 1)),
            kind=str(entry.get("kind", "")),
            menu_name=str(entry.get("menu_name", "")),
            example=str(entry.get("example", "")).strip(),
        )
        for entry in shapes
        if isinstance(entry, dict)
    ]


def _require_library(library: str) -> None:
    if library not in LIBRARIES:
        available = ", ".join(LIBRARIES)
        raise ValueError(
            f"unknown notation library {library!r}; available: {available}"
        )


def list_libraries(*, as_json: bool = False) -> str:
    """Every library with its shape/function counts."""
    counts: dict[str, dict[str, int]] = {}
    for lib in LIBRARIES:
        shapes = _shape_summaries(lib)
        counts[lib] = {
            "shapes": len(shapes),
            "functions": len({s.function for s in shapes}),
        }
    if as_json:
        return json.dumps(counts, indent=2)

    lines = ["Available notation libraries:", ""]
    for lib, count in counts.items():
        n_shapes, n_functions = count["shapes"], count["functions"]
        lines.append(f"  {lib:<12} {n_shapes:>3} shapes, {n_functions:>3} functions")
    lines.append("")
    lines.append(
        "Run `mdg notation <library>` for its full palette (add --json for "
        "machine-readable output)."
    )
    return "\n".join(lines)


def render_library(library: str, *, as_json: bool = False) -> str:
    """The full palette for one library: every function/variant + example call."""
    _require_library(library)
    if as_json:
        registry = load_registry(library)
        return json.dumps(registry.get("shapes", []), indent=2)

    shapes = _shape_summaries(library)
    lines = [f"{library} ({len(shapes)} shapes)", ""]
    for shape in shapes:
        header = f"{library}.{shape.function} (variant {shape.variant}, {shape.kind})"
        if shape.menu_name:
            header += f" -- {shape.menu_name}"
        lines.append(header)
        for example_line in shape.example.splitlines() or [""]:
            lines.append(f"    {example_line}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mdg notation")
    parser.add_argument(
        "library",
        nargs="?",
        default=None,
        help="Notation library (e.g. c4, erd, uml). Omit to list all libraries.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    args = parser.parse_args(argv)

    if args.library is None:
        print(list_libraries(as_json=args.json))
    else:
        print(render_library(args.library, as_json=args.json))
    return 0
