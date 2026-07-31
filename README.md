# mdg-drawio

Python library for generating [draw.io](https://www.diagrams.net/) diagrams from compact JSON documents.

## Requirements

- Python 3.12+
- Node.js and `git` (for `make build-data` only)

The draw.io source is copyright material and is **not** vendored — `make
build-data` clones it (pinned) into `./drawio` for you (git-ignored). Run
`make drawio` on its own to just fetch it.

## Setup

```bash
# Install the package and dev tools
pip install -e '.[dev]'

# Fetch the pinned draw.io source (auto-run by build-data) and generate data
make build-data
```

## Make targets

| Target | Description |
|---|---|
| `make help` | List all targets with one-line descriptions (default goal) |
| `make drawio` | Clone the pinned draw.io source into `./drawio` (copyright; git-ignored) |
| `make build-data` | Fetch draw.io + run the full pipeline into `mdg_drawio/generated_data/notation/` |
| `make check` | Full gate: lint + tests + `model-check` |
| `make test` | Run tests only |
| `make lint` | Run mypy and ruff only |
| `make model-check` | Verify the architecture model is consistent (see Architecture) |
| `make verification` | Per-Component test-coverage report (advisory) |
| `make coverage-gate` | Fail if any Component is below `COVERAGE_MIN` (60%); run in CI |
| `make trace` | Trace which classes/functions each CLI action permutation touches → `action_trace.json` |
| `make dead-code` | Advisory report of `mdg_drawio` definitions no action permutation touches |
| `make dashboard` | Aggregate all quality signals into a self-contained `dashboard.html` (D3) |
| `make clean` | Remove all generated files |

## Architecture

**Start here:** [`docs/architecture/README.md`](docs/architecture/README.md) is
the project entrypoint — a top-down walkthrough (Context → Container → Component
→ Code) with the current code review layered onto each container.

The architecture is maintained as an enforced **model** (Model-Based Systems
Engineering), not just prose — see [`AGENTS.md`](AGENTS.md) →
"Architecture is an enforced model". In short:

- **Views** live in `docs/architecture/`: `c4_architecture.mdg` (Context →
  Container → Component), `code_architecture.mdg` (Container ▸ Component ▸
  module, **generated**), and `decisions.mdg` (ADRs).
- **Traces** (`realized-by`, `satisfies`) link Components to the modules that
  realize them and the decisions they satisfy; a flat view is in
  [`docs/architecture/traceability.md`](docs/architecture/traceability.md).
- `make model-check` proves the views, traces, and code stay in sync — it runs
  in `make check`, so drift fails the build.

When you change code structure, keep the model in sync (see `AGENTS.md`).

## Data pipeline

The style sidecars in `mdg_drawio/generated_data/notation/` are derived from
the draw.io shape library and are **not committed** to this repository
(copyright material). Run `make build-data` once to generate them locally
before running or testing.

```
tools/palette/                    -- extract shapes from draw.io source --> tools/palette/output/
tools/styles/                     -- parse + validate .drawio files     --> tools/styles/output/
scripts/build_data.py             -- orchestrate the full generated-data pipeline
scripts/build_notation_styles.py  -- join registries to palette styles --> mdg_drawio/generated_data/notation/
```

The last step verifies every registry `render.fingerprint` against the palette
and fails loudly on drift.

## Notation registries

`mdg_drawio/notation/` holds the committed, agent-facing DSL definitions:

- `GRAMMAR.md` — the canonical `.mdg` DSL grammar; every registry points here.
- `shape-registry.schema.json` — JSON Schema (v2) for the registries.
- `<lib>/<lib>_registry.yaml` — one shape registry per library
  (archimate3, bpmn2, c4, erd, general, uml, uml25): shape ids, DSL
  signatures, rows/containment rules, metamodel mappings, and a style
  fingerprint binding each shape to its palette cell.
- `<lib>/<lib>_shapes_coverage.mdg` — a DSL document exercising every shape
  of the palette (also the round-trip test fixture).

Registry consistency is enforced by `tests/test_registries.py`.

## Dead-code analysis

The CLI exposes one action (`convert`), but the pipeline branches on real input
dimensions: notation, layout mode (`layered`/`palette`/`process`/`sequence`),
flow `direction`, `--force`, and overlay round-trips.
[`scripts/trace_actions.py`](scripts/trace_actions.py) enumerates that
permutation space, drives each case through `mdg_drawio.cli.main` under
`sys.settrace`, and records the ordered sequence of `mdg_drawio` classes and
functions that execute (`module:qualname`, mirroring `co_qualname`).

Dead-code detection crosses **three signals** for every `def`/`class` in
`mdg_drawio/` (the "universe", parsed with `ast`):

1. **CLI reachability** — did any action permutation execute it?
2. **Test reachability** — does the regression suite execute it? (traced with
   `sys.monitoring`)
3. **Static call-graph reachability** — starting from everything that *is*
   executed (1 + 2), can it be reached by following reference edges (one
   definition calling or nesting another)?

Crossing them classifies each definition:

| Bucket | Meaning |
|---|---|
| **truly dead** | not executed *and* not reachable from live code — a dead island → delete |
| **uncovered** | reachable from live code but no runtime path runs it → add a fixture (a gated branch, not dead) |
| **reached by tests** | no CLI permutation runs it, but the suite does → alive |
| **allowlisted** | structurally unreachable by design (see below) |

Commands:

- `make trace` writes the analysable artifact `action_trace.json` (one entry per
  permutation with its distinct call `sequence`, plus the `touched_union`). Add
  `--full` for the exhaustive cartesian product instead of the covering set.
- `make dead-code` prints the three-way report. It traces the regression suite
  too (slower, most truthful); pass `--no-tests` to skip that. Always exits 0.
  Structurally-unreachable definitions (typing `Protocol`s, ABCs, build-data and
  standalone-`parse()` helpers) live in
  [`scripts/dead_code_allowlist.py`](scripts/dead_code_allowlist.py) with a
  mandatory reason.

[`tests/test_dead_code.py`](tests/test_dead_code.py) runs the covering sweep in
`make test` and **gates** on: no permutation crashes, convertible fixtures still
convert, the allowlist stays honest (no stale entry), and **no truly-dead code**
(unreached *and* unreferenced). "Uncovered" coverage gaps stay advisory, so
ordinary refactors are not blocked.

**Blind spots.** Both reachability signals are heuristics over a dynamic
language, so treat "truly dead" as a strong hint, not a proof:

- Call-graph edges match on the *leaf* name, so `foo()` links to every
  definition named `foo` — a dead island sharing a name with live code looks
  reachable. This *under*-reports dead islands — the safe direction for
  suggested deletions.
- Neither signal follows dynamic dispatch (`getattr`, string-keyed registries,
  `__import__(f"...{notation}.layout")`). Code reached only that way, for an
  input neither the fixtures nor the tests provide, can look truly-dead while
  being live — allowlist it with that reason.
- Dunders, `@property`, and dataclass-generated methods are called implicitly;
  `__all__` re-exports are treated as surface, not use, so unused public API is
  reported and annotated `(exported public API)` for human judgement.

## Reverse derivation (draw.io → shape) — POC

The forward pipeline turns a `.mdg` document into a `.drawio` diagram. The
reverse — take a diagram a user drew directly in the draw.io UI and derive which
registry shape each cell came from — lives in
[`scripts/reverse/`](scripts/reverse/) as a proof of concept.

```bash
make derive FILE=path/to/diagram.drawio      # needs `make build-data`
```

It works in two layers:

1. **Weighted style match** ([`scoring.py`](scripts/reverse/scoring.py)) — a
   cell's style is scored against every registry shape's canonical style.
   Shape-defining tokens (`shape=`, `perimeter=`, bare shape names) carry a high
   weight; cosmetic tokens (colour, font, alignment, spacing) a small one. So a
   recoloured or re-fonted cell still matches its shape, while a cosmetic
   *agreement* still breaks ties between otherwise-identical shapes (e.g. C4
   `Person` vs `Person_Ext`, which differ only by fill colour).
2. **Document-level ranking** ([`derive.py`](scripts/reverse/derive.py)) — most
   shapes (~78%) have a globally-unique style and resolve directly. For the rest,
   unambiguous cells vote for their library and a small version-recency prior
   defaults a lone ambiguous shape to the newest version: a solitary UML lifeline
   resolves to `uml25`, but add any uml-only shape and its anchor vote pulls the
   lifeline to `uml`.
3. **Semantic naming** ([`naming.py`](scripts/reverse/naming.py)) — every
   resolved cell gets a `.mdg`-ready `node_id` (`person1`, `system1`, ...), one
   counter per shape function, in document order. `node_id`s are author-chosen
   per `GRAMMAR.md`; a derived one is a starting point, not a fixed identity —
   pure and re-derivable, so relabelling it later costs nothing. (Aside: `.mdg`
   already accepts a quoted GUID as a `node_id` — `c4.Person("550e8400-...", ...)`
   round-trips today — should this scale to a large EA model needing stable
   external identities instead of mnemonic names.)

Each cell reports its derived shape, node id, similarity, a confidence, and how
it was resolved (`unique` / `single-library` / `library-vote` / `recency-prior`).
The palette styles are draw.io-copyright (git-ignored), so this — and its tests —
need `make build-data`; ground-truth `.drawio` fixtures are synthesized at test
time from the palette, never committed.

**Known limitation.** Ranking resolves *which library*; it does not yet
disambiguate variants that share one style *within* a library (e.g. BPMN
choreography markers). Those need the registry's `discriminator` field populated
with structural rules (child cells / decorators) — a separate, later step.

## Quality dashboard

`make dashboard` aggregates every Makefile quality signal into a single
self-contained `dashboard.html` you can open in any browser (offline — D3 is
vendored and all data inlined):

- **Tests** — totals and a per-module breakdown (from a JUnit run).
- **Coverage** — per-Component line coverage with the `COVERAGE_MIN` ratchet line.
- **Dead code** — reachable / allowlisted / uncovered / truly-dead composition.
- **Model consistency** — the four `make model-check` gates.
- **Lint** — mypy and ruff status.

It runs the suite under coverage, so it is a standalone target (not part of
`make check`). The output is git-ignored and regenerated on demand. Charts and
palette follow the data-viz method (status hues for gates, one sequential ramp
for magnitude, a selectable light/dark theme). D3 v7 is vendored under
`scripts/assets/` (ISC, © Mike Bostock — see `scripts/assets/d3.LICENSE`) so the
dashboard is self-contained and works offline; unlike the draw.io source, its
permissive licence makes redistribution fine.

## Contributing

1. Fork the repository and create a branch.
2. Run `pip install -e '.[dev]'` and `make build-data`.
3. Make your changes and run `make check` before opening a pull request.

## License

MIT - see [LICENSE](LICENSE).
