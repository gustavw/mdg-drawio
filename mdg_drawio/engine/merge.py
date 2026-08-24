"""Engine entry point for the `mdg merge` subcommand.

Thin re-export: the CLI-is-a-thin-shell invariant restricts ``cli.py`` to
importing only ``mdg_drawio.engine`` (see ``test_cli_is_thin_shell``), the
same way ``convert()`` is the sole entry point for the default action. This
module exists purely so the `merge` verb can delegate through engine too,
without cli.py reaching into ``mdg_drawio.reverse`` directly.
"""
from __future__ import annotations

from mdg_drawio.reverse import merge_cli

main = merge_cli.main

__all__ = ["main"]
