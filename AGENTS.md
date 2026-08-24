# Agent rules

You are the **router** (orchestrator), not a worker. Before editing files in a
package, `read("skill://<name>")` — the skill carries that domain's conventions
and boundaries. Delegate deep or parallel work to subagents.

## Skills — domain detail, loaded on demand

| Skill | Domain |
|---|---|
| `build-data` | data pipeline (`scripts/build_*`, generated data) |
| `layout` | diagram layout — `mdg_drawio/layout/` |
| `cli` | CLI entry point — `mdg_drawio/__init__.py`, `__main__.py` |
| `architect` | architecture model & `.mdg` diagrams — `docs/architecture/` |
| `dsl` | notation parsing/exporting — `mdg_drawio/notation/` |
| `generator` | drawio XML generation & data models — `mdg_drawio/generator/` |
| `reviewer` | read-only code review |

## The gate is the spec

Correctness is enforced by `make`, not by memorised rules — change code, then
**run the gate and read its output on failure** rather than carrying every rule
in your head:

- **`make check`** = `make lint` (mypy + ruff, zero issues) + `make test` +
  `make model-check` (architecture model consistent). Run before declaring done.
- Pipeline smoke test: `mdg docs/architecture/c4_architecture.mdg /tmp/t.drawio
  --force` must exit 0.
- Advisory (not in `check`): `make verification` / `make coverage-gate`
  (per-Component coverage, ratchet ≥ 60%), `make dead-code`, `make dashboard`.
- `make help` lists every target with a one-line description.

## Architecture is an enforced model (MBSE)

`docs/architecture/*.mdg` is a governed model, not prose; `make model-check`
proves the views, traces, and real import graph stay in sync. **If you add, move,
or remove a module**: update its `realized-by` trace in `c4_architecture.mdg`,
run `python scripts/generate_code_arch.py --write`, then `make model-check`.
Full model rules: `read("skill://architect")`.

## Invariants the gates enforce (stated to save a failed cycle)

- Import only from `mdg_drawio.<package>`, never a submodule; within a package
  use relative imports. — `tests/test_architecture.py`
- Consumers receive external data through **injected ports**, never module-global
  caches (the one exception is `notation/_core/registry.py`). Add a new consumer
  by injecting its data, not a `set_*`/global. — `tests/test_container_view.py`;
  detail in `skill://generator`.
- No `# type: ignore` — fix the type instead. — `tests/test_type_hygiene.py`
