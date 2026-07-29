# TODO — Code review remediation

Plan to fix the findings from the 2026-07-27 review. Full context, file:line, and
rationale for every item: [`docs/architecture/README.md`](../docs/architecture/README.md) §5–6.

**Baseline:** `make check` green (442 tests, lint clean, model consistent). No P0s.
The work below is latent-correctness and robustness hardening.

## Status — 2026-07-27: all findings addressed ✅

Every item below is done. `make check` is green (194 tests in `tests/` incl. the
new `tests/test_review_fixes.py`, 442 roundtrip, lint clean, model consistent)
and the pipeline smoke test exits 0.

Two nuances worth knowing:
- **3.5 (parallel-edge overlay key collision):** resolved by *validation*, not a
  key redesign. C4 assigns edge ids as `source->target`, so parallel edges share
  an id — the new duplicate-cell-id check (2.9) now rejects such documents up
  front rather than silently losing one edge's waypoints. Documented in
  `engine/convert.py::_inject_edge_overlay`. A future "true" parallel-edge story
  needs unique edge identity (a separate design decision).
- **Committed diagrams:** the back-edge orientation fix (1.6) changes generated
  edge ids/orientation for cyclic graphs, so `docs/architecture/*.drawio` are now
  mildly stale. They are **not** gated. Refresh with `make diagrams` (never
  `--force`) when convenient.

**Definition of done for every item:** the fix lands with a test that fails
before and passes after, and `make check` stays green. For items in the
under-tested layout modes, add the regression test *first* (it doubles as
coverage — see Phase 4).

---

## Phase 1 — P1 latent hazards & broken contracts (do first)

Each is either valid-only-by-coincidence or a documented contract that is
actually violated. Ordered by impact-to-effort.

- [x] **1.1 Generator boolean flags use ID constants** (S) — `generator/generator.py`
  - `:304` `vertex` → literal `"1"` (not `PAGE_CELL_ID`).
  - `:770` `math` → literal `"0"` (not `ROOT_CELL_ID`).
  - `:342` node parent fallback → `PAGE_CELL_ID` (not hardcoded `"1"`).
  - Test: assert generated node cells carry `vertex="1"` and the graph `math="0"`
    with `PAGE_CELL_ID`/`ROOT_CELL_ID` temporarily monkeypatched to sentinels.
- [x] **1.2 Restore edge-reference validation for wrapped edges** (M) — `engine/validate.py:76`
  - `_validate_edge_references` must unwrap `<object>`/`<UserObject>` (mirror
    `overlay._iter_cells` / the sibling `_collect_wrapper_ids`) before scanning.
  - Test: a document with a UserObject-wrapped edge pointing at a missing node id
    must now fail validation (it passes today).
- [x] **1.3 Enforce the `DslError` line-number contract** (M) — `notation/_core/dsl_engine.py:552` + `notation/c4/__init__.py:130,216`
  - Wrap `build_node`/`build_edge`/`parse_diagram_title` in `parse_block_source`
    with `except (ValueError, TypeError) as exc: raise DslError(str(exc), line_number) from exc`.
  - Also wrap the passthrough branch (`dsl_engine.py:532–542`) and actually use
    the `line_number` the passthrough builders already accept (`:582`).
  - Test: `c4.Person(alice, 123)` and `c4.Rel(None, bob)` raise `DslError` with a
    line number.
- [x] **1.4 Resolve the `_parse_node` label contract** (S) — `notation/c4/__init__.py:123`
  - Decide: default `label = node_id` (like passthrough at `:596`) **or** require
    two positional args. Recommend defaulting to `node_id` for author convenience.
  - Test: `c4.Person(alice)` either produces a node labelled `alice` or raises a
    clear `DslError` — never the current line-less `Node.label is required`.
  - Do together with 1.3.
- [x] **1.5 `resolve_boundary_geometry` string `contains`** (S) — `layout/_container_layout.py:614`
  - Replace the raw `node.extra["contains"]` reads (guard + loop) with `_contains_ids(node)`.
  - Test: a container whose `contains` is a single string id gets the true
    bounding box, not the default min-box.
- [x] **1.6 Decide `reversed_ids` intent** (M) — `layout/layered.py:390` (set at `:73`)
  - Either un-swap endpoints/anchors for reversed edges in `_route_edges`, or
    delete the dead parameter and the tracking set. Pick one explicitly.
  - Test: if edges are meant to render in original direction, assert a back-edge's
    routed endpoints match its original `source_id`/`target_id`.

---

## Phase 2 — P2 robustness: fail loudly and correctly

Turn raw tracebacks and silent fallbacks into actionable errors.

- [x] **2.1 Corrupt/foreign existing output** (S) — `engine/convert.py:543`: wrap
  `read_overlay` in `except (ET.ParseError, OSError)` → warn and regenerate.
- [x] **2.2 Malformed overlay XML context** (S) — `generator/overlay.py:149`:
  re-raise `ValueError(f"Malformed overlay XML in {path!r}: …")`.
- [x] **2.3 Registry preload guards** (S) — `engine/preload.py:26`: existence check +
  reject non-dict / `None` parse result with a clear per-library message.
- [x] **2.4 Palette load hardening** (M) — `layout/palette.py:50,89`: `encoding="utf-8"`,
  wrap load with the offending path, `.get("x",0)`/`.get("y",0)` on points.
- [x] **2.5 Aspect-ratio validation** (S) — `layout/config.py:99`: `parse_aspect_ratio`
  rejects non-positive / non-numeric with "must be 'W:H' with positive integers".
- [x] **2.6 BOM-tolerant input read** (S) — `engine/convert.py:546`: `encoding="utf-8-sig"`.
- [x] **2.7 CLI error surface** (S) — `cli.py:52`: gate traceback behind
  `--debug`/`MDG_DEBUG`; otherwise `mdg: error: {exc}`. Remove the dead
  `NotImplementedError` branch.
- [x] **2.8 Guard `variant` / padding / registry lookups** (S each):
  - `generator/generator.py:551` — defensive `int(variant)`, name the edge.
  - `layout/_container_layout.py:178` — padding coercion helper naming node+key.
  - `notation/_core/registry.py:48` — `_registries[library]` guard matching the disk path.
  - `notation/_core/dsl_engine.py:406` — `parse_keyword_int` rejects `bool`.
- [x] **2.9 Surface bad node ids instead of fabricating positions** (S):
  - `layout/sequence.py:74` — skip/flag edges whose endpoint isn't a known column.
  - `generator/generator.py:518` — debug-time guard for duplicate ids / dangling endpoints.

---

## Phase 3 — P2 consistency, dead code & docs

Low-risk sweeps; batch into one or two PRs.

- [x] **3.1 Shape-resolution divergence** (M) — `layout/size_resolver.py:51 vs 80`:
  one shared shape-id resolver so a node's size and style come from the same shape.
- [x] **3.2 Registry arg-validation vs docstring** (M) — `notation/c4/__init__.py:11`:
  implement `shapes_by_function("c4")` lookup (reject unknown functions) **or**
  soften the docstring. Prefer implementing.
- [x] **3.3 Edge-endpoint `None` contract** (S) — `notation/c4/__init__.py:216` +
  `contracts/models.py:226`: reject dangling endpoints in `_parse_edge`, or relax the model.
- [x] **3.4 Don't swallow bad positional args** (S) — `notation/c4/__init__.py:138`:
  raise `DslError` for non-string data-source args (or document the leniency).
- [x] **3.5 Overlay key collision for parallel edges** (M) — `generator/overlay.py:95,139`:
  include the edge id in the key on read+write, or document the limitation.
- [x] **3.6 Remove dead surface / fix stale docs** (S–M):
  - `dsl_engine.py:49` `RAW_REF_RE` — remove (unused) or use it.
  - `dsl_engine.py:108` `extract_blocks` — wire into `parse()` (add an
    unterminated-block `DslError`) or remove the export.
  - `registry.py:3,27` + `dsl_engine.py:37` — docstrings point at the phantom
    `dsl_engine.set_registries()`; fix to `engine/preload.py` + `registry.set_registries`.
- [x] **3.7 Precompile DiagramTitle regexes** (S) — `notation/c4/__init__.py:289`.
- [x] **3.8 Readability extractions** (M, optional):
  - `generator/generator.py:516` — extract `_build_edge_style(edge, ctx)`.
  - `layout/layered.py:33` — extract size-defaulting loop + padding-dict build.
  - `layout/layered.py:115` vs `config.py:150` — shared `box_extents(...)`.
  - `layout/config.py:113,157` — drop dead `min_page_*` params and needless `getattr`.
  - `layout/palette.py:41` — `_positions` becomes an instance attr.
  - `layout/process.py:66` — partition passthrough edges by `id(e)`, not value membership.

---

## Phase 4 — Close the coverage gap (cross-cutting)

The layout modes carry most of the P2 correctness bugs *and* the lowest coverage.
The Phase 1–3 regression tests already push these up; finish the job:

- [x] **4.1** `sequence_co` (18%), `process_co` (28%), `palettelayout_co` (33%) →
  toward the ~85% the rest of the codebase enjoys. Track with `make verification`.
- [x] **4.2** Once raised, add a floor gate:
  `python scripts/verification_report.py --min 50` in CI (bump the threshold over time).

---

## Sequencing notes

- Phase 1 is independent per-item; ship as small focused PRs. 1.3 + 1.4 share the
  DSL error path — do them together.
- Phases 2–3 are safe to batch; group by container to keep diffs reviewable.
- Every layout-mode fix in Phases 1–3 should land with the test that also serves
  Phase 4 — don't write coverage tests twice.
- After each PR: `make check` green, and the pipeline smoke test still exits 0:
  `mdg --force -i docs/architecture/c4_architecture.mdg -o /tmp/test.drawio`.
