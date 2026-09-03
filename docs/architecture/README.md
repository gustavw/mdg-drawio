# mdg-drawio — Architecture Guide & Code Review

> **This document is the entrypoint to the project.** It walks the architecture
> top-down (System Context → Container → Component → Code), then layers a code
> review onto that same structure so every finding has an architectural home.
> The architecture here is not prose — it is a machine-enforced **model** (see
> [`AGENTS.md`](../../AGENTS.md) → MBSE). This file is the human-readable lens
> over that model.

**Review date:** 2026-07-27 · **Reviewed at commit:** `e6f2f69` · **Reviewer:**
automated multi-agent pass (one reader per container) + manual verification of P1s.

> **Status: remediated (2026-07-28).** Every finding in §5–6 has been fixed on
> branch `review/remediation-2026-07`, with regression tests in
> `tests/test_review_fixes.py`. The tables below are kept as the audit record;
> their `file:line` references point at the pre-fix code (commit `e6f2f69`), so
> line numbers have since shifted. `make check` is green.

---

## 1. What this project is

`mdg-drawio` turns **architecture-as-code** (a compact `.mdg` DSL) into editable
**draw.io diagrams**, preserving stable node identities and manual layout across
regenerations. All seven notation libraries parse through the shared
registry-driven DSL engine; C4 also supplies native builders.

The whole system is a single-shot pipeline:

```
.mdg source ─▶ preload ─▶ parse ─▶ layout ─▶ generate ─▶ validate ─▶ .drawio
             (registries   (.mdg DSL  (4 modes)  (mxGraph    (unique ids,
              + styles)      → AST)              XML)         refs, roots)
```

`engine/convert.py` is the **composition root**: it loads data once, builds the
injected ports (`SizeResolver`, `StyleProvider`), and wires every stage.

### Where to start reading

| You want to… | Go to |
|---|---|
| Understand the boundaries | This file, §3–4 |
| See the enforced model | `c4_architecture.mdg`, `code_architecture.mdg`, `decisions.mdg` |
| Read the rendered diagrams | `*.drawio` (open in draw.io) |
| Learn the DSL | `mdg_drawio/notation/GRAMMAR.md` |
| Know the rules before editing | `AGENTS.md` + the per-package `skill://` files |
| See Component→module→ADR links | `traceability.md` |

---

## 2. Health snapshot

| Gate | Status |
|---|---|
| `make lint` (mypy + ruff, strict) | ✅ clean |
| `make test` | ✅ **210 unit/arch tests + 442 roundtrip passed** |
| `make model-check` (import integrity, traceability, lockfile, matrix) | ✅ all pass |
| `make coverage-gate` (per-Component ≥ 60%, CI) | ✅ pass |

**Per-Component line coverage** (`make verification`). CI now **gates** on this:
`make coverage-gate` fails if any Component drops below `COVERAGE_MIN` (60%, a
ratchet set in the `Makefile`). The three layout modes that held most of the P2
findings are now fully covered; the floor is `generate_co` at 66.9%:

| Component | Coverage | | Component | Coverage |
|---|---|---|---|---|
| `generate_co` | 66.9% ← floor | | `engine_preload_co` | 91.3% |
| `container_co` | 76.8% | | `cli_co` | 91.7% |
| `overlay_co` | 83.3% | | `size_resolver_co` | 94.2% |
| `dsl_engine_co` | 85.1% | | `layered_co` | 95.3% |
| `config_co` | 85.5% | | `models_contract_co` | 95.2% |
| `engine_convert_co` | 87.4% | | `registry_co` | 97.0% |
| `engine_validate_co` | 89.0% | | `sequence_co`/`process_co`/`palettelayout_co` | 100% |

**Overall verdict:** the architecture is genuinely sound — the model gate, DI
discipline, and import boundaries are real and enforced. No P0 (nothing broken
in normal operation). The findings were **latent correctness hazards** (things
that worked only by coincidence) and **error-handling / robustness gaps**
concentrated in the under-tested layout modes and the notation error contract —
all now remediated (§5–6).

---

## 3. The C4 view (Context → Container → Component)

### System Context
One system, three actors: the **Author/Developer** writes `.mdg` and refines
layout in **draw.io**; an **AI Assistant** reads/edits the same `.mdg` notation
(never raw XML). Source of truth: `c4_architecture.mdg`, page *Context*.

### Containers
Seven Python containers with a strict dependency shape:

```
        CLI ──▶ Engine ──▶ { Layout, Generator, Notation }
                  │              │         │         │
                  └──────────────┴─────────┴─────────┴──▶ Contracts (leaf)
                                                Notation ──▶ Generated Data
```

- **Contracts** — dependency-free shared kernel (`Node`, `Edge`, `Document`,
  constants). Everything depends on it; it depends on nothing (ADR-0001).
- **Engine** — orchestration (`convert`), one-time `preload`, pre-write `validate`.
- **Layout** — 4 modes (layered/process/sequence/palette) behind one
  `BaseLayout`/`Result` contract (ADR-0005), fed sizes via injected `SizeResolver`.
- **Generator** — `Document` → mxGraph XML via `xml.etree` (never string-concat,
  so XML-injection-safe), with `overlay` reading old XML to preserve layout
  (ADR-0004). Never calls notation at runtime (ADR-0007).
- **Notation** — `_core` DSL engine + per-library registries; C4 is the only
  live parser.
- **Generated Data** — draw.io-derived style JSON, `make build-data`, uncommitted.

### Components & traces
The Component page decomposes each container into modules, and `realized-by` /
`satisfies` traces bind every Component to its code module and the ADR it
implements. `make model-check` proves these stay in sync with the real import
graph — see `traceability.md` for the flat matrix.

---

## 4. The Code view

`code_architecture.mdg` is **generated** (`scripts/generate_code_arch.py`) and
equals the real import graph (lockfile-enforced). Key facts it encodes:

- **No submodule imports across packages** — always `from mdg_drawio.<pkg>`.
- **Relative imports within a package** — `.sibling`, never absolute.
- **DI, not module globals** — the one sanctioned global cache is
  `notation/_core/registry.py` (standalone `parse()` support), whitelisted and
  drift-guarded by `test_container_view.py`.

---

## 5. Code review — findings by container

Severity: **P0** broken · **P1** important · **P2** nice-to-have.
Effort: **S** <5min · **M** <30min · **L** >30min. No P0s found.

> **All findings below are fixed** (branch `review/remediation-2026-07`). The
> tables are the original audit; `Location` columns cite the pre-fix commit
> `e6f2f69`, so line numbers have shifted. Each row's `Fix` is the approach that
> was taken. Regression tests: `tests/test_review_fixes.py`.

### 5.1 Contracts
Clean. Dataclasses throughout, no mutable-default args (all use
`field(default_factory=...)`), `frozen` on leaf types, zero project imports.
`__post_init__` validation is the *cause* of two findings below — but that's a
contract-boundary mismatch in callers, not a defect here.

### 5.2 Engine & CLI

| Sev | Issue | Location | Fix | Effort |
|---|---|---|---|---|
| **P1** | **Edge-reference validation is a dead guardrail for C4.** `_validate_edge_references` scans `root_el.findall("mxCell")` (direct children only), but the generator wraps every C4 edge in `<UserObject>`. Wrapped edges are never checked, so dangling source/target refs pass validation. | `engine/validate.py:76` | Unwrap `<object>`/`<UserObject>` when collecting edge cells (mirror `overlay._iter_cells` / the sibling `_collect_wrapper_ids`). | M |
| P2 | Reading a corrupt/foreign existing output crashes with a raw traceback (`ET.ParseError`/`OSError` from `read_overlay` uncaught). | `engine/convert.py:543` | `try/except (ET.ParseError, OSError)` → warn and regenerate without overlay. | S |
| P2 | Registry YAML load unguarded & asymmetric with styles load; empty file → `None` registry pushed silently. | `engine/preload.py:26` | Existence check + reject non-dict parse result with a clear message. | S |
| P2 | Input not BOM-tolerant — a leading `﻿` breaks the anchored frontmatter regex → silent misdetection. | `engine/convert.py:546` | Read with `encoding="utf-8-sig"`. | S |
| P2 | Overlay-count mismatch warning always says "extra overlays ignored" even when overlays < pages. | `engine/convert.py:410` | Branch the message on `>` vs `<`. | S |
| P2 | CLI catch-all prints a full traceback for user-recoverable errors; `NotImplementedError` branch is effectively dead. | `cli.py:52` | Gate traceback behind `--debug`/`MDG_DEBUG`; otherwise `mdg: error: {exc}`. | S |

### 5.3 Layout Engine

| Sev | Issue | Location | Fix | Effort |
|---|---|---|---|---|
| **P1** | **`resolve_boundary_geometry` mishandles a string `contains`.** It reads `node.extra["contains"]` raw instead of via `_contains_ids`; a single-string id iterates per-character, matches nothing, and silently returns the default min-box instead of the true bounding box. | `layout/_container_layout.py:614` | Use `_contains_ids(node)` like every other call site. | S |
| **P1** | **`reversed_ids` is a dead parameter — back-edge orientation is never restored.** `_route_edges` accepts `reversed_ids` but ignores it; reversed edges keep swapped endpoints, masked only because they're also `hidden=True`. Latent: any change that renders hidden edges draws them backwards. | `layout/layered.py:390` (set at `:73`) | Either un-swap endpoints for reversed edges in `_route_edges`, or delete the parameter and tracking. Decide explicitly. | M |
| P2 | Aspect ratio `"W:0"` → `ZeroDivisionError`; `"W:x"` → raw `int()` `ValueError` instead of the clear "must be 'W:H'" message. User-configurable input. | `layout/config.py:99,133` | Validate positive integers in `parse_aspect_ratio`. | S |
| P2 | `size_of` and `style_of` resolve a node's shape by **different** logic (`index_shapes_by_function[0]` vs a `startswith` prefix scan) — size and style can come from different palette shapes. | `layout/size_resolver.py:51 vs 80` | One shared shape-id resolver used by both. | M |
| P2 | Recursive DFS in cycle detection can hit the recursion limit on a deep graph (only unbounded-recursion step; rest is iterative). | `layout/layered.py:154` | Convert to an explicit stack. | M |
| P2 | Sequence layout invents anchor positions for edges with unknown endpoints instead of surfacing the bad node id. (Under-tested: 18%.) | `layout/sequence.py:74` | Skip/flag edges whose endpoint isn't a known column. | S |
| P2 | Palette load has no `encoding`, no missing-file/bad-JSON guard, and `KeyError`s on points lacking `x`/`y`. (Under-tested: 33%.) | `layout/palette.py:50,89` | `encoding="utf-8"`, wrap load with the offending path, `.get` with defaults. | M |
| P2 | `process._reroute_edges` classifies passthrough edges with `e not in rank_edges` — O(n²) and value-equality can misclassify distinct-but-equal edges. (Under-tested: 28%.) | `layout/process.py:66` | Partition by `id(e)`/`e.id`, not value membership. | S |
| P2 | Shared mutable class attribute `_positions = []` on `PaletteLayout`. | `layout/palette.py:41` | Initialize per-instance in `__init__`. | S |
| P2 | `LayeredLayout.apply` is an ~80-line God step in an otherwise clean module. | `layout/layered.py:33` | Extract the size-defaulting loop and padding-dict build into named helpers. | M |
| P2 | Dead legacy params (`min_page_*`, neutralized by `_ = …`) and needless `getattr(n,"x",0.0)` on always-present dataclass fields. | `layout/config.py:113,157` | Remove dead params; use direct attribute access. | M |
| P2 | Duplicated content-extent geometry between `layered._content_extents` and `config.compute_content_extents`. | `layout/layered.py:115` | Shared `box_extents(...)` helper. | S |
| P2 | Non-numeric `padding_*` in `node.extra` → bare `float()` error with no node/key context. | `layout/_container_layout.py:178` | Coerce via a helper that names the node id and key. | S |

### 5.4 Generator

| Sev | Issue | Location | Fix | Effort |
|---|---|---|---|---|
| **P1** | **`vertex` flag set from the `PAGE_CELL_ID` constant.** Every node cell emits `vertex=PAGE_CELL_ID`; it's valid only because that constant *coincidentally* equals `"1"`. Changing the id constant (its entire purpose) would make draw.io stop treating nodes as vertices. | `generator/generator.py:304` | Use the literal `"1"` (as child cells at `:457` and edge children at `:643` already do). | S |
| P2 | Same class of bug: `math` graph attr set from `ROOT_CELL_ID` (works only because it equals `"0"`). | `generator/generator.py:770` | Use the literal `"0"`. | S |
| P2 | Inverse inconsistency: node parent falls back to hardcoded `"1"` instead of `PAGE_CELL_ID`. | `generator/generator.py:342` | `node.parent_id or PAGE_CELL_ID`. | S |
| P2 | Overlay edge identity was ambiguous when parallel edges shared endpoints. | `generator/overlay.py`, `generator/generator.py` | Enforce one directed relationship per endpoint pair and derive its id as `<source>-<target>`. | M |
| P2 | No duplicate-cell-id / dangling-edge detection at generation (draw.io tolerates → silent corruption). | `generator/generator.py:518` | Debug-time guard: assert unique emitted ids and edge endpoints exist. | M |
| P2 | Malformed overlay XML → bare `ParseError` with no path context. | `generator/overlay.py:149` | Re-raise `ValueError(f"Malformed overlay XML in {path!r}: …")`. | S |
| P2 | `int(edge.extra.get("variant", 1))` → uninformative `ValueError` on non-numeric variant. | `generator/generator.py:551` | Coerce defensively / name the edge. | S |
| P2 | `_append_edge` mixes ~6 concerns (id fallback, token stripping, routing, overrides, UserObject wrap, recursion). | `generator/generator.py:516` | Extract `_build_edge_style(edge, ctx)`. | M |

> **Verified clean:** no XML injection (all output via `ET.SubElement`/`tostring`,
> which escapes), no `None` in attribute values, no bare `except`, `.find` results
> all None-guarded in overlay.

### 5.5 Notation & DSL

| Sev | Issue | Location | Fix | Effort |
|---|---|---|---|---|
| **P1** | **Builder/validation errors escape the `DslError` contract without line numbers.** `parse_block_source` calls `build_node`/`build_edge` unwrapped; `literal_string` and `Node`/`Edge.__post_init__` raise plain `ValueError`. Both module docstrings promise "errors always include line numbers" — violated in practice (`c4.Person(alice, 123)` → line-less `ValueError`). | `notation/_core/dsl_engine.py:552`; `notation/c4/__init__.py:130,216` | Wrap builder calls in `try/except (ValueError, TypeError) → raise DslError(str(exc), line_number)`. | M |
| **P1** | **`_parse_node` accepts id-only, but `Node` requires a non-empty label** → contradictory, line-less error for input the parser's own guard implied was OK (`c4.Person(alice)` → `ValueError: Node.label is required`). | `notation/c4/__init__.py:123`; `contracts/models.py:182` | Decide the contract: default `label = node_id` (like passthrough at `:596`) or require 2 positional args with a `DslError`. | S |
| P2 | Docstring claims registry arg-validation that doesn't exist — unknown functions silently become nodes (`Foobar(x,"L")` parses as type `c4.Foobar`). Typos mislabel instead of erroring. | `notation/c4/__init__.py:11` | Implement the lookup against `shapes_by_function("c4")` (preferred) or soften the docstring. | M |
| P2 | `literal_or_name_or_none` permits `None` for edge endpoints, but `Edge.__post_init__` rejects an asymmetric `None`/value pair → confusing cross-layer error (`c4.Rel(None, bob)`). | `notation/c4/__init__.py:216`; `contracts/models.py:226` | Reject the dangling endpoint in `_parse_edge` with a `DslError`, or relax the model. | S |
| P2 | Passthrough builders take an unused `line_number` and can emit line-less `ValueError` (foreign-namespace path, not covered by the P1 wrap). | `notation/_core/dsl_engine.py:582` | Use `line_number`; wrap the passthrough branch too. | S |
| P2 | `_parse_node` swallows non-string extra positional args with a bare `pass` — a typo'd data-source arg vanishes silently. | `notation/c4/__init__.py:138` | Raise `DslError` (with line) or document the leniency. | S |
| P2 | `parse_keyword_int` treats `bool` as a valid int (`True → 1`); unguarded in a public `_core` helper. | `notation/_core/dsl_engine.py:406` | Add `and not isinstance(raw, bool)`. | S |
| P2 | `load_registry` does an unguarded `_registries[library]` → bare `KeyError` (disk path gives a helpful one). | `notation/_core/registry.py:48` | Guard with the same `expected one of LIBRARIES` message. | S |
| P2 | Dead/stale surface: `RAW_REF_RE` (defined+exported, no consumers); `extract_blocks` exported but never wired into `parse()` (block feature effectively dead, unterminated blocks give a misleading error); docstrings reference a nonexistent `dsl_engine.set_registries()`. | `notation/_core/dsl_engine.py:49,108`; `registry.py:3,27` | Remove dead exports or finish wiring; fix the docstrings to point at `engine/preload.py`. | S–M |
| P2 | DiagramTitle regexes recompiled inside a per-page loop. | `notation/c4/__init__.py:289` | Precompile at module scope. | S |

---

## 6. Remediation log

All findings from §5 were fixed on branch `review/remediation-2026-07`
(regression tests in `tests/test_review_fixes.py`). Highlights:

**P1 — latent hazards & broken contracts (all fixed)**
1. **`vertex`/`math` id-constants** → literal boolean flags; parent uses `PAGE_CELL_ID`.
2. **Dead edge-reference validation** → `validate` now unwraps `<UserObject>` edges
   (restoring the ADR-0003 guardrail) and also rejects duplicate cell ids.
3. **`DslError` line-number contract** → builder/model `ValueError`s are funnelled
   through `DslError` with the line number (native + passthrough paths).
4. **`_parse_node` label contract** → label defaults to the node id.
5. **`resolve_boundary_geometry` string `contains`** → uses `_contains_ids`.
6. **`reversed_ids`** → back-edge orientation is restored in place after ranking
   (tracked by object identity), which also fixed a latent duplicate-id in cyclic
   graphs.

**P2 / P3** — robustness (guarded overlay/registry/palette I/O, BOM-tolerant read,
aspect-ratio validation, `MDG_DEBUG`-gated traceback, defensive coercions,
sequence-layout skips unknown participants) and consistency/dead-code cleanups
(unified size/style resolution, removed dead `RAW_REF_RE`/`extract_blocks`, fixed
stale docstrings, precompiled regexes). See §5 tables for the full list.

**Coverage gate (done).** `sequence`/`process`/`palette` — the three lowest
Components at review time — now have direct unit tests
(`tests/test_layout_modes.py`) and sit at 100% line coverage. CI enforces a
per-Component floor via `make coverage-gate` (`COVERAGE_MIN` = 60%, a ratchet;
see §2).

**Relationship-identity follow-up (completed).** A connected relationship's
identity is derived from its unique directed endpoint pair as
`<source>-<target>`. Sync normalizes draw.io ids and removes legacy `edge_id`
keywords. Parallel relationships are rejected explicitly instead of relying on
order-sensitive suffixes.

---

## 7. What is already strong (keep it)

- **The model gate is real.** Import integrity, Component↔module parity, and the
  code-view lockfile are CI-enforced — the architecture cannot silently drift.
- **Dependency injection is disciplined.** Ports (`SizeResolver`, `StyleProvider`)
  with a single whitelisted global cache, guarded by a drift test.
- **XML generation is injection-safe by construction** (`xml.etree`, never
  string concatenation).
- **Contracts is a true dependency-free kernel**, and ADRs 0001–0007 each trace
  to the Component that satisfies them.

The gaps above are the difference between "works in the happy path" and "fails
loudly and correctly on the unhappy path" — not structural debt.
