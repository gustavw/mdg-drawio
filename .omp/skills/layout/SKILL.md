---
name: layout
description: Diagram layout system — node positioning and edge routing. Use when working on layout algorithms or layout configuration — mdg_drawio/layout/.
globs:
  - "mdg_drawio/layout/**"
alwaysApply: false
---

# Layout — Node positioning and edge routing

You own the diagram layout system in `mdg_drawio/layout/`. Every layout mode
(`layered`, `sequence`, `process`, `palette`) is a subclass of `BaseLayout`.

## Architecture

```
mdg_drawio/layout/
  __init__.py            — mode dispatcher, public API
  _types.py              — type leaf: imports Node/Edge from generator, defines Result/BaseLayout
  layout.py              — re-export shim from _types
  config.py              — Config dataclass, page size resolution
  size_resolver.py       — concrete SizeResolver wired to core registry
  _container_layout.py   — stacking, grid layout, boundary geometry
  _orthogonal.py         — routing and port assignment utilities
  layered.py             — Sugiyama-style layered graph layout
  sequence.py            — sequence-diagram column layout
  process.py             — process flow left-to-right layout
  palette.py             — read exact positions from a palette file
```

## Core types

| Type | Source | Purpose |
|---|---|---|
| `Node` | `generator.models` | Re-exported — layout mutates `x`, `y` in-place |
| `Edge` | `generator.models` | Re-exported — layout populates `waypoints` |
| `Waypoint` | `generator.GeometryPoint` | Alias for edge routing points |
| `Result` | `_types.py` | Layout output: nodes, edges, page dimensions |
| `Config` | `config.py` | Margins, gaps, direction, aspect ratio |
| `SizeResolver` | `_types.py` | Protocol: `(node_type) -> (width, height)` |
| `BaseLayout` | `_types.py` | ABC with `apply(nodes, edges, size_of, config)` |

## Public API

```python
from mdg_drawio.layout import (
    BaseLayout, Config, Edge, Node, Result, SizeResolver, Waypoint,
    create_size_resolver, dispatch_layout, modes, register_layout,
    resolve_page_size, absolute_node_boxes,
    resolve_boundary_geometry, separate_top_level_boundary_units,
)
```

## Layout modes

| Mode | File | Description |
|---|---|---|
| `layered` | `layered.py` | Sugiyama-style DAG layout (default) |
| `sequence` | `sequence.py` | Participant columns with horizontal edges |
| `process` | `process.py` | Left-to-right flow with optional swimlanes |
| `palette` | `palette.py` | Read exact positions from a pre-baked JSON file |

Add a new mode by creating a module that exports `LAYOUT_MODE` and `LAYOUT_CLASS`,
then add it to the import + registration block in `__init__.py`.

## Conventions

- Layout mutates `Node` and `Edge` objects **in-place** — no copies
- Internal modules use relative imports: `from ._types import Node`
- `Config` is the single source of truth for spacing — no hard-coded defaults
- `_container_layout.py` and `_orthogonal.py` are pure geometry helpers

## What you must NOT do

- Change the `BaseLayout.apply()` contract
- Hard-code spacing defaults in layout algorithms — use `Config`
- Import from submodules externally — internal relative imports (`.config`) are fine
- Layout mutates `Node` objects in-place; `Edge` objects may be recreated by routing passes

## Keep the architecture model in sync (MBSE)

If you add, remove, or move a module, update the model or CI will fail:

1. update the `realized-by` traces in `docs/architecture/c4_architecture.mdg`
2. `python scripts/generate_code_arch.py --write` (regenerate the code view)
3. `make model-check` — must pass

Full model rules: `read("skill://architect")`.
