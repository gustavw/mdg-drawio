"""mdg-drawio — convert MDG notation files to draw.io diagrams."""
from __future__ import annotations

from mdg_drawio.cli import main
from mdg_drawio.engine import convert

__all__ = ["convert", "main"]
