"""Allow `python -m mdg_drawio.reverse` as an entry point for the derive CLI."""
from __future__ import annotations

from .derive_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
