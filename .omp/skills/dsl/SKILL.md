---
name: dsl
description: DSL parsing and notation engine. Use when working on .mdg parsing, DSL export, or notation-specific parser logic — mdg_drawio/notation/.
globs:
  - "mdg_drawio/notation/**"
## Coverage

Only C4 notation has a working parser. All other notations (UML, ArchiMate,
BPMN, ERD, General, UML 2.5) are stubs with empty `__init__.py` files.

## Public API

```python
from mdg_drawio.notation import parse  # currently C4-only

doc = parse(source)  # -> Document | MultiPageDocument
```

You own the DSL parsing layer in `mdg_drawio/notation/`. Currently only
C4 has a working parser — other notations are stubs.

## Architecture

```
mdg_drawio/notation/
  __init__.py              — public API: exports parse
  _core/                   — shared DSL engine
    __init__.py            — re-exports from dsl_engine
    dsl_engine.py          — CALL_RE, parse_call_arguments, split_pages, etc.
  c4/__init__.py           — C4 parser
  */{name}_registry.yaml   — YAML shape registries
  GRAMMAR.md               — canonical DSL grammar
  shape-registry.schema.json — JSON schema for registries
```

## Public API

```python
from mdg_drawio.notation import parse

doc = parse(source)  # -> Document | MultiPageDocument
```

## Shared engine (`_core/dsl_engine.py`)

Building blocks every notation parser reuses:
- `CALL_RE` — regex for DSL function calls
- `parse_call_arguments()` — parse positional and keyword arguments
- `split_pages()` — split source on `page "Name"` boundaries
- `parse_frontmatter()` — strip YAML frontmatter, return metadata + body
- `build_pages_document()` — assemble multi-page documents

## Id rules

- `node_id` is author-chosen and MUST be unique within the document
- Ids are load-bearing for bidirectional editing
- Edge endpoints reference existing node ids
- Never renumber existing ids when editing — keep them stable

## Conventions

- Parser functions should be pure: input → output, no side effects
- Error messages must include line numbers
- Separate parse from validate — parser returns best-effort data
- Type annotations on all public functions
- Import model types from `mdg_drawio.generator` (package level)

## What you must NOT do

- Edit `mdg_drawio/generator/` — coordinate model changes with the generator skill
- Change public API of the YAML registries without updating GRAMMAR.md
- Import from `mdg_drawio.generator.models` — use `mdg_drawio.generator`

## Keep the architecture model in sync (MBSE)

If you add, remove, or move a module, update the model or CI will fail:

1. update the `realized-by` traces in `docs/architecture/c4_architecture.mdg`
2. `python scripts/generate_code_arch.py --write` (regenerate the code view)
3. `make model-check` — must pass

Full model rules: `read("skill://architect")`.
