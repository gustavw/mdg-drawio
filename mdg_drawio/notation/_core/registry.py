"""Shape-registry loading.

Registries are pre-loaded at startup by ``engine/preload.py`` via
``set_registries`` (defined below). When no pre-loaded data is present
(tests, scripts calling ``parse`` standalone), ``load_registry`` falls back to
reading the YAML from disk.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from mdg_drawio.contracts import index_shapes_by_function

NOTATION_DIR = Path(__file__).parent.parent

LIBRARIES = ("archimate3", "bpmn2", "c4", "erd", "general", "uml", "uml25")

# Pre-loaded cache — populated by engine/preload.py at startup.
_registries: dict[str, dict[str, Any]] | None = None


def set_registries(registries: dict[str, dict[str, Any]]) -> None:
    """Store raw registry documents for load_registry().

    Called by ``engine/preload.py`` during startup pre-load.
    """
    global _registries
    load_registry.cache_clear()
    _registries = dict(registries)


def registry_path(library: str) -> Path:
    if library not in LIBRARIES:
        raise KeyError(f"unknown library {library!r}; expected one of {LIBRARIES}")
    return NOTATION_DIR / library / f"{library}_registry.yaml"


@cache
def load_registry(library: str) -> dict[str, Any]:
    """The parsed registry document for a library.

    Uses pre-loaded data when available; falls back to reading the YAML
    file from disk.
    """
    if _registries is not None:
        if library not in _registries:
            raise KeyError(
                f"unknown library {library!r}; expected one of {LIBRARIES}"
            )
        return _registries[library]
    with open(registry_path(library), encoding="utf-8") as f:
        doc: dict[str, Any] = yaml.safe_load(f)
    return doc


def shapes_by_id(library: str) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in load_registry(library)["shapes"]}


def shapes_by_function(library: str) -> dict[str, list[dict[str, Any]]]:
    """Function name -> shape entries sorted by variant.

    Grouping goes through ``contracts.index_shapes_by_function``, the declared
    single source for the ``{function: [entries]}`` transformation; only the
    variant ordering (which callers here rely on to pick the canonical entry)
    is added on top.
    """
    out = index_shapes_by_function(load_registry(library)["shapes"])
    for entries in out.values():
        entries.sort(key=lambda s: int(s["variant"]))
    return out
