---
name: cli
description: CLI entry point — thin shell around the engine. Use when working on argument parsing or the `mdg` command.
globs:
  - "mdg_drawio/cli.py"
  - "mdg_drawio/__init__.py"
  - "mdg_drawio/__main__.py"
alwaysApply: false
---

# CLI — Thin entry point

You own the CLI entry point. The CLI is a single-file thin shell — it parses
arguments and delegates every real operation to `mdg_drawio.engine`.

## Architecture

```
mdg_drawio/
  cli.py           — `main()` entry point, verb dispatch, `_build_convert_parser()`
  __init__.py      — re-exports `main` for `[project.scripts] mdg = "mdg_drawio:main"`
  __main__.py      — `python -m mdg_drawio` support
```

The CLI imports ONLY from `mdg_drawio.engine` and stdlib. It never touches
core, layout, generator, notation, or `mdg_drawio.reverse` directly — a verb
that needs another subsystem (`merge`/`sync`/`derive` need
`mdg_drawio.reverse`; `notation` needs `mdg_drawio.notation`) is exposed as a
thin re-export module under `mdg_drawio/engine/` instead (`engine/merge.py`,
`engine/sync.py`, `engine/derive.py`, `engine/notation_info.py`).

## Usage

```
mdg <input.mdg> [output.drawio] [--force]
mdg merge <existing.mdg> <new.drawio> [--write]
mdg sync <existing.mdg> <diagram.drawio> [--write]
mdg derive <diagram.drawio> [--json]
mdg notation [library] [--json]
mdg                          # or `mdg -h` / `mdg --help`: overview of all commands
```

`convert` (no verb) takes positional args, never flags: first is always
input, second (optional) is output. Roles are assigned by POSITION, never
guessed from the file extension — a wrong guess would silently overwrite the
`.mdg` source instead of the `.drawio` output. The extension is only used to
validate each argument (clear error on a `.mdg`/`.drawio` mismatch), never to
infer which is which. Omitting output derives `<input>.drawio` alongside it.
`--force` skips the overlay read (full regeneration).

`merge`/`sync`/`derive`/`notation` are real subcommands (first positional
token), recognized before the convert parser ever runs — see `_SUBCOMMANDS`
in `cli.py`. Bare `mdg` (no args) or `mdg -h`/`--help` shows a hand-authored
`_TOP_LEVEL_HELP` overview of every command instead of convert's own help —
keep that string in sync when you add or change a verb.

## Key functions

- `main(argv=None)` — entry point: verb dispatch, then delegates. Returns 0
  on success, non-zero on error.
- `_build_convert_parser()` — build the convert action's ArgumentParser
- `_require_suffix(path, suffix, role)` — validates an argument's extension

All pipeline logic lives in `mdg_drawio.engine`:
- `convert(input_path, output_path, force)` — full conversion pipeline
- `merge.main(argv)` — re-export of `mdg_drawio.reverse.merge_cli.main`
  (adds new hand-drawn cells, never removes)
- `sync.main(argv)` — re-export of `mdg_drawio.reverse.sync_cli.main`
  (adds AND removes -- draw.io as the sole source of truth)
- `derive.main(argv)` — re-export of `mdg_drawio.reverse.derive_cli.main`
- `notation_info.main(argv)` — lists notation libraries, or one library's
  full DSL palette (function/variant + example call), from its registry
- `preload_core()` — load registries and styles into memory
- `_generate_multipage(pages, size_of, overlays)` — produce multi-diagram mxfile
- `validate_generated_xml(xml_string)` — pre-write validation

## Conventions

- CLI output uses the `mdg:` prefix for consistency
- All output goes to stderr (both errors and success messages)
- Exit code 0 on success, non-zero on error
- Never import from submodules — always from `mdg_drawio.engine`

## What you must NOT do

- Add pipeline logic to the CLI — it belongs in `mdg_drawio/engine/`
- Change the public API of other packages (core, layout, notation, generator)
- Introduce new dependencies without documenting why

## Keep the architecture model in sync (MBSE)

If you add, remove, or move a module, update the model or CI will fail:

1. update the `realized-by` traces in `docs/architecture/c4_architecture.mdg`
2. `python scripts/generate_code_arch.py --write` (regenerate the code view)
3. `make model-check` — must pass

Full model rules: `read("skill://architect")`.
