"""Engine entry point for the `mdg derive` subcommand.

Thin re-export: the CLI-is-a-thin-shell invariant restricts ``cli.py`` to
importing only ``mdg_drawio.engine`` (see ``test_cli_is_thin_shell``). This
module exists purely so the `derive` verb can delegate through engine too,
without cli.py reaching into ``mdg_drawio.reverse`` directly.
"""
from __future__ import annotations

from mdg_drawio.reverse import derive_cli

main = derive_cli.main

__all__ = ["main"]
