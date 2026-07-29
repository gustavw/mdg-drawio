"""Location of the generated notation style/palette sidecars.

The style JSON files under :data:`DATA_DIR` are read once at startup (see
``engine/preload.py``) and handed to the generator's ``StyleProvider`` by
injection. Notation no longer caches style data itself — hence this module is
now just the path constant.
"""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).parent.parent.parent / "generated_data"
