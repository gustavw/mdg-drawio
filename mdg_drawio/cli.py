"""CLI entry point for the `mdg` command.

Thin shell around the engine. Parses arguments, delegates to
``mdg_drawio.engine.convert`` (default action), ``mdg_drawio.engine.merge``
(``merge`` verb), ``mdg_drawio.engine.derive`` (``derive`` verb), or
``mdg_drawio.engine.notation_info`` (``notation`` verb) -- CLI never imports
outside ``mdg_drawio.engine`` (see ``test_cli_is_thin_shell``).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mdg_drawio.engine import convert, derive, merge, notation_info

# Recognized first-token verbs. Anything else is the implicit `convert`
# action -- no verb required, so `main(["diagram.mdg"])` dispatches to it.
_SUBCOMMANDS = ("merge", "derive", "notation")

_MDG_SUFFIX = ".mdg"
_DRAWIO_SUFFIX = ".drawio"

# Hand-authored (not argparse-generated) so it stays a single, scannable
# reference covering every verb -- argparse's own per-verb --help remains
# available for the details (`mdg merge --help`, etc.). Shown for bare `mdg`
# and `mdg -h`/`--help`; anything else dispatches to a verb first.
_TOP_LEVEL_HELP = """\
mdg -- convert MDG notation source into draw.io diagrams (and back).

usage:
  mdg <input.mdg> [output.drawio] [--force]
  mdg merge <existing.mdg> <new.drawio> [--write]
  mdg derive <diagram.drawio> [--json]
  mdg notation [library] [--json]

commands:
  (default)  Convert an .mdg file to a .drawio diagram. Output is optional --
             derived from the input's stem when omitted (foo.mdg -> foo.drawio).
             Roles are POSITIONAL (first = input, second = output), never
             guessed from extension. --force ignores any existing .drawio's
             overlay (manual layout) and fully regenerates.
  merge      Splice hand-drawn cells from an existing .drawio into an
             existing .mdg file's text, correctly indented. Dry-run by
             default (prints a diff); --write applies it after re-validating.
  derive     Read-only: print which registry shape each cell in a .drawio
             file resolves to (similarity score, confidence, how it was
             decided). Writes nothing.
  notation   List the available notation libraries (c4, erd, uml, ...), or
             `mdg notation <library>` to print its full DSL palette --
             every function/variant with a ready-to-adapt example call.
             --json for machine-readable output.

Run `mdg <command> --help` for that command's full option list.
"""


def _require_suffix(path: Path, suffix: str, role: str) -> Path:
    """Validate *path*'s extension for *role* ("input"/"output").

    Roles are always assigned by POSITION (first argument = input, second =
    output), never guessed from the extension -- a wrong guess would mean
    writing generated XML over the user's own .mdg source instead of the
    .drawio output. The extension is only ever used to catch a mistake
    early, with a clear message, not to decide which file is which.
    """
    if path.suffix.lower() != suffix:
        raise ValueError(f"expected a {suffix} file for {role}, got {path}")
    return path


def _build_convert_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the (default) convert action."""
    parser = argparse.ArgumentParser(
        prog="mdg",
        description="Convert an MDG notation file to a draw.io diagram.",
        epilog="Also available: `mdg merge EXISTING.mdg NEW.drawio [--write]`, "
        "`mdg derive DIAGRAM.drawio [--json]`, `mdg notation [library]`. "
        "Run `mdg` with no arguments for an overview of all commands.",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input .mdg file",
    )
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=None,
        help="Output .drawio file (default: <input>.drawio, same directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Full regeneration, ignoring existing geometry overlay",
    )
    return parser


def _run_convert(argv: Sequence[str]) -> int:
    args = _build_convert_parser().parse_args(argv)
    input_path = _require_suffix(args.input, _MDG_SUFFIX, "input")
    output_path = (
        _require_suffix(args.output, _DRAWIO_SUFFIX, "output")
        if args.output is not None
        else input_path.with_suffix(_DRAWIO_SUFFIX)
    )
    return convert(input_path, output_path, args.force)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns 0 on success, non-zero on error."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # No verb requested at all, or an explicit bare -h/--help: show the
    # hand-authored overview of every command, not just convert's own help
    # (which is what `_run_convert(["-h"])` would otherwise show).
    if not raw_argv or raw_argv[0] in ("-h", "--help"):
        print(_TOP_LEVEL_HELP, end="")
        return 0 if raw_argv else 1

    if raw_argv[0] in _SUBCOMMANDS:
        verb, rest = raw_argv[0], raw_argv[1:]
    else:
        verb, rest = "convert", raw_argv

    try:
        if verb == "merge":
            return merge.main(rest)
        if verb == "derive":
            return derive.main(rest)
        if verb == "notation":
            return notation_info.main(rest)
        return _run_convert(rest)
    except Exception as exc:
        print(f"mdg: error: {exc}", file=sys.stderr)
        # Full traceback only when explicitly requested — a bad input file
        # should read as a one-line error, not a stack trace.
        if os.environ.get("MDG_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1
