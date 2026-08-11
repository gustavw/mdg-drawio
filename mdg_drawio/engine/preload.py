"""Startup pre-load of registries and styles into memory."""

from __future__ import annotations

import json

import yaml

from mdg_drawio.notation import (
    DATA_DIR,
    LIBRARIES,
    NOTATION_DIR,
    set_registries,
)


def preload_core() -> tuple[dict[str, dict], dict[str, dict]]:
    """Load all YAML registries and styles JSON into memory.

    Called once at startup. Registries are pushed to Core so parsing never
    touches the filesystem; styles are returned so the CLI can build a
    ``StyleProvider`` and inject it into ``generate`` (see ``engine/convert.py``).
    Neither the generator nor notation holds global style state.
    """
    registries: dict[str, dict] = {}
    for lib in LIBRARIES:
        path = NOTATION_DIR / lib / f"{lib}_registry.yaml"
        if not path.exists():
            raise FileNotFoundError(f"registry not found for {lib!r}: {path}")
        with open(path, encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        if not isinstance(parsed, dict):
            raise ValueError(
                f"registry for {lib!r} is not a mapping (got "
                f"{type(parsed).__name__}): {path}"
            )
        registries[lib] = parsed
    set_registries(registries)

    styles: dict[str, dict] = {}
    for lib in LIBRARIES:
        path = DATA_DIR / "notation" / f"{lib}_styles.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                styles[lib] = json.load(f)

        # Row types (e.g. uml25's Item/Header/Divider) have no independent
        # shape id, so they are generated into a sibling sidecar rather than
        # as extra keys in <lib>_styles.json -- merged here, in memory only,
        # under "_row_types" so PaletteStyleProvider/size_resolver can fall
        # back to it without the on-disk sidecar gaining non-shape-id keys.
        row_types_path = DATA_DIR / "notation" / f"{lib}_row_types.json"
        if row_types_path.exists():
            with open(row_types_path, encoding="utf-8") as f:
                styles.setdefault(lib, {})["_row_types"] = json.load(f)

    return registries, styles
