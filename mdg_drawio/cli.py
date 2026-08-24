"""CLI entry point for the `mdg` command.

Thin shell around the engine. Parses arguments, delegates to
``mdg_drawio.engine.convert`` (default action) or ``mdg_drawio.engine.merge``
(``merge`` verb) -- CLI never imports outside ``mdg_drawio.engine`` (see
``test_cli_is_thin_shell``).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mdg_drawio.engine import convert, merge

# Recognized first-token verbs. Anything else (including every existing
# `-i/-o/--force` invocation) is the implicit, backward-compatible `convert`
# action -- no verb required, so `main(["-i", ..., "-o", ...])` keeps working.
_SUBCOMMANDS = ("merge",)


def _build_convert_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the (default) convert action."""
    parser = argparse.ArgumentParser(
        prog="mdg",
        description="Convert MDG notation files to draw.io diagrams.",
        epilog="Also available: `mdg merge EXISTING.mdg NEW.drawio [--write]` "
        "-- splice hand-drawn .drawio cells into an existing .mdg file.",
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


def _run_convert(argv: Sequence[str]) -> int:
    args = _build_convert_parser().parse_args(argv)
    return convert(args.input, args.output, args.force)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns 0 on success, non-zero on error."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in _SUBCOMMANDS:
        verb, rest = raw_argv[0], raw_argv[1:]
    else:
        verb, rest = "convert", raw_argv

    try:
        if verb == "merge":
            return merge.main(rest)
        return _run_convert(rest)
    except Exception as exc:
        print(f"mdg: error: {exc}", file=sys.stderr)
        # Full traceback only when explicitly requested — a bad input file
        # should read as a one-line error, not a stack trace.
        if os.environ.get("MDG_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1
