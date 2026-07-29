---
name: build-data
description: Drawio shape/style data build pipeline. Use when building, regenerating, or fixing the data pipeline — scripts/build_data.py, tools/.
globs:
  - "scripts/build_data.py"
  - "tools/**"
alwaysApply: false
---

# Build Data — Shape/style data pipeline

You own the drawio shape/style data build pipeline. Your scope is
## Pipeline (4 steps)

1. `tools/palette/generate_palette.py` — extracts draw.io palette shapes,
   writes `.drawio` files under `tools/palette/output/`

2. `tools/styles/generate_outputs.py` — parses `.drawio` files, validates
   against `tools/styles/schema.json`, writes JSON under `tools/styles/output/`

3. Copies JSON files (excluding `palette.json`) from `tools/styles/output/`
   into `mdg_drawio/data/`

4. `scripts/build_notation_styles.py` — generates notation style sidecars
   (`c4_styles.json`, `uml25_styles.json`, etc.) into `mdg_drawio/data/notation/`

## Commands

```bash
make build-data    # full pipeline
make clean         # remove generated data
```

## On failure

When a pipeline step fails, inspect the error, read the relevant source files
under `tools/`, and apply the smallest fix. Re-run the failing step until it
passes, then run the full pipeline to confirm end-to-end.

## Conventions

- Never edit `mdg_drawio/data/` directly — the script owns that
- Run `make build-data` after any pipeline changes
- Run roundtrip tests: `pytest tools/styles/test_roundtrip.py -v`
