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
from typing import Any

from mdg_drawio.notation import LIBRARIES, NOTATION_DIR, load_registry


@dataclass(frozen=True)
class ShapeSummary:
    """One registry shape entry, reduced to what's useful for discovery."""

    function: str
    variant: int
    kind: str
    menu_name: str
    example: str


@dataclass(frozen=True)
class RowTypeSummary:
    """One registry row-type entry (a compartment child, not a standalone shape)."""

    signature: str
    summary: str


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


def _row_type_summaries(library: str) -> list[RowTypeSummary]:
    registry = load_registry(library)
    row_types = registry.get("row_types", [])
    summaries = []
    for entry in row_types:
        if not isinstance(entry, dict):
            continue
        parts = []
        for arg in entry.get("args", []):
            name = arg["name"]
            keyword_only = arg.get("passing") == "keyword_only"
            parts.append(f"{name}=..." if keyword_only else name)
        signature = f"{library}.{entry['name']}({', '.join(parts)})"
        summaries.append(
            RowTypeSummary(signature=signature, summary=str(entry.get("summary", "")))
        )
    return summaries


def grammar_text() -> str:
    """The canonical `.mdg` DSL grammar spec (GRAMMAR.md), verbatim."""
    return (NOTATION_DIR / "GRAMMAR.md").read_text(encoding="utf-8")


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
        "machine-readable output). `mdg notation --grammar` prints the "
        "canonical .mdg DSL grammar spec (document structure, calls, "
        "nesting, edges, ids)."
    )
    return "\n".join(lines)


def _grammar_header_lines(library: str, grammar: dict[str, Any]) -> list[str]:
    """Root builder + library-specific notes, from the registry's ``grammar``."""
    lines = []
    root = grammar.get("root")
    if root:
        lines.append(
            f"Root builder: {library}.{root}(...) "
            "-- exactly one per document, everything else nests under it"
        )
    notes: list[str] = grammar.get("notes") or []
    for note in notes:
        lines.append(f"Note: {note}")
    if lines:
        lines.append("")
    return lines


def _row_type_lines(library: str) -> list[str]:
    """Compartment row types (not standalone shapes), from registry ``row_types``."""
    row_types = _row_type_summaries(library)
    if not row_types:
        return []
    lines = [
        "Row types (compartment children, not standalone shapes -- see "
        "GRAMMAR.md 'Nesting: rows vs containment'):"
    ]
    for row_type in row_types:
        lines.append(f"  {row_type.signature}")
        if row_type.summary:
            lines.append(f"      {row_type.summary}")
    lines.append("")
    return lines


def render_library(library: str, *, as_json: bool = False) -> str:
    """The full palette for one library: every function/variant + example call."""
    _require_library(library)
    registry = load_registry(library)
    if as_json:
        return json.dumps(registry.get("shapes", []), indent=2)

    shapes = _shape_summaries(library)
    lines = [f"{library} ({len(shapes)} shapes)", ""]
    lines.extend(_grammar_header_lines(library, registry.get("grammar", {})))
    lines.extend(_row_type_lines(library))

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
    parser.add_argument(
        "--grammar",
        action="store_true",
        help="Print the canonical .mdg DSL grammar spec (GRAMMAR.md) and exit",
    )
    args = parser.parse_args(argv)

    if args.grammar:
        print(grammar_text(), end="")
        return 0

    if args.library is None:
        print(list_libraries(as_json=args.json))
    else:
        print(render_library(args.library, as_json=args.json))
    return 0
