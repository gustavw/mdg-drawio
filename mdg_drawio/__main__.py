"""Allow `python -m mdg_drawio` as an entry point."""
from __future__ import annotations

from mdg_drawio import main

if __name__ == "__main__":
    raise SystemExit(main())
