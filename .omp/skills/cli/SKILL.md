---
name: cli
description: CLI entry point — thin shell around the engine. Use when working on argument parsing or the `mdg` command.
globs:
  - "mdg_drawio/__init__.py"
  - "mdg_drawio/__main__.py"
alwaysApply: false
---

# CLI — Thin entry point

You own the CLI entry point. The CLI is a single-file thin shell — it parses
arguments and delegates every real operation to `mdg_drawio.engine.convert`.

## Architecture

```
mdg_drawio/
  __init__.py      — public API, `main()` entry point, `_build_parser()`
  __main__.py      — `python -m mdg_drawio` support
```

The CLI imports ONLY from `mdg_drawio.engine` and stdlib. It never touches
core, layout, generator, or notation directly.

## Usage

```
mdg --force -i <input.mdg> -o <output.drawio>
```

`-i` and `-o` are required. `--force` skips the overlay read (full regeneration).

## Key functions

- `main(argv=None)` — entry point, returns 0 on success, non-zero on error
- `_build_parser()` — build the ArgumentParser

All pipeline logic lives in `mdg_drawio.engine`:
- `convert(input_path, output_path, force)` — full conversion pipeline
- `preload_core()` — load registries and styles into memory
- `_generate_multipage(pages, size_of, overlays)` — produce multi-diagram mxfile
- `validate_generated_xml(xml_string)` — pre-write validation

## Conventions

- CLI output uses the `mdg:` prefix for consistency
- All output goes to stderr (both errors and success messages)
- Exit code 0 on success, non-zero on error
- Never import from submodules — always from `mdg_drawio.engine`

## What you must NOT do

- Add pipeline logic to the CLI — it belongs in `engine.py`
- Change the public API of other packages (core, layout, notation, generator)
- Introduce new dependencies without documenting why

## Keep the architecture model in sync (MBSE)

If you add, remove, or move a module, update the model or CI will fail:

1. update the `realized-by` traces in `docs/architecture/c4_architecture.mdg`
2. `python scripts/generate_code_arch.py --write` (regenerate the code view)
3. `make model-check` — must pass

Full model rules: `read("skill://architect")`.
