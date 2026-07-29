"""CLI entry point for the `mdg` command.

Thin shell around the conversion engine. Parses arguments, delegates to
``mdg_drawio.engine.convert``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mdg_drawio.engine import convert


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="mdg",
        description="Convert MDG notation files to draw.io diagrams.",
    )
    parser.add_argument(
        "-i", "--input",
        type=Path,
        required=True,
        help="Input .mdg file",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        required=True,
        help="Output .drawio file",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Full regeneration, ignoring existing geometry overlay",
    )
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns 0 on success, non-zero on error."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return convert(args.input, args.output, args.force)
    except Exception as exc:
        print(f"mdg: error: {exc}", file=sys.stderr)
        # Full traceback only when explicitly requested — a bad input file
        # should read as a one-line error, not a stack trace.
        if os.environ.get("MDG_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1
