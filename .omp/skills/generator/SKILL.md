---
name: generator
description: Draw.io XML generation and data models. Use when generating drawio output, defining data models, or working on the forward-generation pipeline — mdg_drawio/generator/.
globs:
  - "mdg_drawio/generator/**"
alwaysApply: false
---

# Generator — XML generation and data models

You own the draw.io XML generation layer and the shared data models in
`mdg_drawio/generator/`. Your job is to define the data contract and
convert it into valid, openable `.drawio` files.

## Architecture

```
mdg_drawio/generator/
  __init__.py         — public API: generate(), model re-exports
  models.py           — Node, Edge, Document, Diagram, and supporting types
  generator.py        — Document → mxfile XML string
  xml_utils.py        — XML pretty-printing (to_string, indent, render_tree)
```

## Data model

`Node` and `Edge` are the canonical types used by the entire pipeline — from
DSL parsing through layout to XML generation. They are defined here as
`@dataclass` with `__post_init__` validation.

### Key types

| Type | Purpose |
|---|---|
| `Node` | A diagram node — shape, container, or swimlane |
| `Edge` | A diagram edge — relationship, association, or flow |
| `Document` | A single diagram page |
| `Diagram` | Page metadata (name, dimensions) |
| `MultiPageDocument` | Multi-page document container |
| `GeometryPoint` | A single point in an edge's geometry path |
| `BoundaryPadding` | Padding for container boundaries |

### Required fields

- `Node`: `id`, `type`, `label` (validated in `__post_init__`)
- `Edge`: `type` (validated in `__post_init__`)

## Public API (re-exported in `__init__.py`)

```python
from mdg_drawio.generator import (
    BoundaryPadding, Diagram, Document, Edge,
    GeometryPoint, MultiPageDocument, Node,
    generate, to_string,
)
```

## Dependency injection — the StyleProvider port

`generate(document, styles)` depends on the `StyleProvider` **protocol**, injected
by the caller — never on a module-global style cache. `create_style_provider(...)`
is the factory; `engine/convert.py` (the composition root) builds it from the
data `preload` returns and injects it. A drift test
(`tests/test_container_view.py::test_data_flows_by_injection_not_module_globals`)
fails any module outside the whitelist that mutates module-global state, so **add
a new consumer by injecting its data through a port, not a `set_*`/global cache.**

## Conventions

- Prefer `@dataclass` over `TypedDict` — gives `__init__`, `__repr__`, `__eq__` for free
- Add `__post_init__` validation for required fields
- No `total=False` TypedDicts — use explicit `Optional`/`None` defaults
- `GeometryPoint` is `frozen=True` (immutable, hashable)
- Keep XML generation pure: input is `Document`, output is an XML string

## What you must NOT do

- Edit anything outside `mdg_drawio/generator/`
- Change the data model contract in a way that breaks layout or DSL parsing
- Import from submodules externally — internal relative imports (`.models`) are fine

## Keep the architecture model in sync (MBSE)

If you add, remove, or move a module, update the model or CI will fail:

1. update the `realized-by` traces in `docs/architecture/c4_architecture.mdg`
2. `python scripts/generate_code_arch.py --write` (regenerate the code view)
3. `make model-check` — must pass

Full model rules: `read("skill://architect")`.
