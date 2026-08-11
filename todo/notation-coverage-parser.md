# TODO - Complete coverage-sheet parsing

Status: all three phases done 2026-08-11 -- conversion, registry-driven block
validation, palette-faithful row rendering, and full positional/keyword
argument binding.

## Goal

Make the C4, UML, and UML 2.5 shape-coverage documents convert successfully,
validate their nested content against the registry, render rows with
palette-faithful style/geometry, and harden argument handling -- in that
order, each phase re-scoped against real data rather than assumption. All
four are done.

Current behavior:

- All seven coverage sheets (ArchiMate, BPMN, C4, ERD, General, UML, UML 2.5)
  convert successfully, including the project's own
  `docs/architecture/*.mdg` and `tests/action_fixtures/*.mdg`.
- Registry dispatch resolves node-versus-edge by the exact
  `(namespace, function, variant)` entry, so mixed-kind function families
  (`uml.FoundMessage`, `uml25.Dependency`, `uml25.Constraint`,
  `uml25.Extension`, `uml25.Activity`, `uml25.Message` -- the complete set
  across all seven registries) classify correctly per variant.
- The C4 edge builder accepts the `None, None` unconnected palette-edge form
  and still rejects one-sided `None` with a line-numbered `DslError`.
- Style resolution (`resolve_style`/`resolve_edge_style`) and `PaletteLayout`
  repositioning both carry the resolved variant and all non-geometry fields
  through, so a variant-2 shape no longer renders with variant 1's style.
- Nested content under a block-opening shape is validated against that
  shape's registry entry: a row name must be in `rows.allowed`, a contained
  function must be in `contains.allowed`, and a shape declaring neither
  cannot open a block at all. Rows and contained children must use the parent
  library, and edges cannot be nested -- all with line-numbered errors.
- Rows render as real child `Node`s with `parent_id` (needed by draw.io's
  stack/table layouts) using palette-faithful style and geometry:
  `scripts/build_notation_styles.py` extracts a canonical style/geometry for
  every row type with no independent top-level shape (uml25's
  Item/Header/Divider/Note/Lane, erd's Row/EntityText/Anchor, uml's
  CompositeLabel, bpmn2's SwimlaneBoxPart/TableRowBoxPart) from its parent
  shape's own nested palette cells, and `PaletteStyleProvider`/
  `create_size_resolver` fall back to it. erd's Row/RowKey are additionally
  compound (a wrapper row plus [key tag, text label] sub-cells); these render
  via `NodeChildCell`, with `key=` preserved from parse through to the XML.
- Every registered buildable DSL call is bound against its resolved registry
  entry like a Python
  function call: the `passing` field (`positional`/`keyword_only`) on each
  declared arg decides whether it can be filled positionally, and binding
  rejects a missing required argument, excess positional arguments, and an
  argument supplied both positionally and by keyword -- all with
  line-numbered errors. Structural values map onto the model
  (`node_id`->id, `label`/`text`->label, `source`/`target`->edge endpoints);
  every other declared value lands on `Node.extra`/`Edge.extra`.
- `tests/test_notation_coverage_parser.py` pins all of the above.

## Immediate failures (resolved in Phase 1)

### C4

`c4_shapes_coverage.mdg` fails on the documented palette-edge form:

```mdg
c4.Rel(None, None, "e.g. Makes API calls", ...)
```

The C4-specific edge builder requires real source and target identifiers, while
the grammar permits `None, None` for unconnected coverage-sheet edges.

### UML

`uml_shapes_coverage.mdg` misclassifies `FoundMessage(..., variant=1)` as an
edge. The registry defines:

- variant 1 as a vertex
- variants 2 and 3 as edges

The shared parser currently classifies by function name and treats a function as
an edge when any of its registry variants is an edge.

### UML 2.5

`uml25_shapes_coverage.mdg` has the inverse mixed-kind case for `Dependency`:

- variant 1 is an edge
- variant 2 is a vertex

The same function-level classification therefore parses variant 2 using the
edge contract.

## Phase 1 - Make all three convert (done)

Estimated effort: 2-4 hours. Actual: done 2026-08-10.

- [x] Resolve the exact registry entry by `(namespace, function, variant)`.
- [x] Decide node versus edge from that entry's `kind`, not from all entries
  sharing the function name.
- [x] Use the selected entry consistently for node/edge dispatch, style, and
  variant handling. Full registry-driven argument validation remains Phase 3.
- [x] Support `None, None` palette edges in the C4-specific builder.
- [x] Give every endpoint-free edge a stable, unique generated id.
- [x] Keep one-sided edges invalid with a line-numbered `DslError`.
- [x] Add regression tests for mixed vertex/edge function families.
- [x] Update the action-trace expectations when each coverage sheet converts
  (`tests/test_dead_code.py::test_convertible_fixtures_convert` now lists all
  seven notations).

This phase makes the files convert, but it does not guarantee correct UML
compartment rendering (Phase 2).

Also fixed while verifying Phase 1 (found via manual testing, not in the
original scope, but blocking correct coverage-sheet output):

- `resolve_style`/`resolve_edge_style` in `mdg_drawio/generator/generator.py`
  ignored the requested variant and always resolved variant 1's style.
- `PaletteLayout.apply()` in `mdg_drawio/layout/palette.py` reconstructed
  `Node`/`Edge` field-by-field, silently dropping `variant`,
  `object_attributes`, and any other field not in the hand-picked list back to
  its dataclass default. Now uses `dataclasses.replace()`.

## Phase 2 - Rows and containment (done)

Estimated effort: 1-3 days. Structural validation shipped 2026-08-10;
palette-faithful row rendering shipped 2026-08-11.

### Corrected premise

This phase originally assumed `rows.allowed` shapes needed a representation
change: build `NodeChildCell` compartment structures instead of real
`Node`s. That assumption did not survive contact with the actual palette
data and an existing test:

- The generated palette style for `uml.class.v1` is
  `swimlane;...;childLayout=stackLayout;...` -- a genuine draw.io swimlane
  container where children are real child vertices that draw.io auto-stacks,
  not static compartment text. ERD tables are the same
  (`childLayout=tableLayout`).
- `tests/test_pipeline.py::test_stacklayout_container_children_stack_tightly`
  already depends on `uml.Class` -> `uml.Item` rows becoming real contained
  nodes with real layout-computed geometry, and was passing on `main` before
  this phase started.
- The project's own self-hosted architecture doc
  (`docs/architecture/code_architecture.mdg`) nests `uml.Package` ->
  `uml.Package`/`uml.Class` as real containment, which also already worked.

So `rows.allowed` and `contains` shapes both need real `Node`s with
`parent_id`, positioned by the container layout engine. However,
`childLayout` only describes the parent layout; it does not by itself supply
each row's style, dimensions, or compound cells. UML 2.5 `Header`, `Item`,
and `Divider`, for example, are not entries in `shapes`, so they used to
render with the generic `whiteSpace=wrap;html=1` style and default 60px
height instead of their distinct palette styles and roughly 20/20/8px
heights; ERD compound rows dropped fields such as `key=` entirely. Both the
structural-validation gap and this rendering gap have now shipped -- see
"What shipped" below.

### What shipped

- [x] Resolve the parent registry entry before processing its block.
- [x] Reject blocks for shapes that declare neither rows nor containment
  (with a lenient fallback for unregistered functions and `kind: "diagram"`
  reference entries, which carry no contract to validate against).
- [x] Validate row function names against `rows.allowed`.
- [x] Validate contained node functions against `contains.allowed`.
- [x] Keep all validation errors line-numbered and actionable.
- [x] Add tests proving rows/contained children still become real `Node`s
  with `parent_id` (not `NodeChildCell`), plus rejection tests for an
  invalid row, an invalid contained child, and a block on a shape with
  neither.
- [x] Fix a real registry gap this validation surfaced: `uml.package.v1` was
  missing `contains: {allowed: ['*']}` even though the project's own
  architecture doc already nests packages and classes inside it.

### Row rendering: what shipped (2026-08-11)

- [x] Bind every row type with no independent top-level shape to
  palette-derived style and dimensions, without hard-coding copyrighted
  palette styles in parser code. `scripts/build_notation_styles.py` classifies
  each nested cell under a `rows.allowed` shape against the shape's own
  declared row types (using the same visual signals -- alignment, a `line;`
  prefix, a `shape=tableRow`/`partialRectangle`/`swimlane` prefix -- a human
  already used to write each row_type's registry docstring), and keeps the
  first (menu-order) match as that row type's canonical style/geometry. The
  result is written to a new, gitignored `<lib>_row_types.json` sidecar
  (kept separate from `<lib>_styles.json` because several consumers --
  `test_styles_sidecar_is_fresh`, `scripts/reverse/style_index.py` -- assume
  every key there is a real registry shape id). `preload_core()` merges it
  into `styles[lib]["_row_types"]` in memory. Covers all 11 orphaned row-type
  names: uml25's Item/Header/Divider/Note/Lane, erd's Row/EntityText/Anchor,
  plus uml's CompositeLabel and bpmn2's SwimlaneBoxPart/TableRowBoxPart;
  erd.RowKey has a standalone shape but also needs its distinct nested keyed
  row template.
- [x] `PaletteStyleProvider.resolve_style`/`resolve_edge_style`
  (`mdg_drawio/generator/generator.py`) and `create_size_resolver`/
  `create_style_resolver` (`mdg_drawio/layout/size_resolver.py`) fall back to
  the row-type sidecar when a function has no top-level registered shape.
- [x] Preserve declared row keywords (`key=`, `dashed=`, and similar) through
  parse -> model: `_build_passthrough_node` now captures every validated
  keyword argument (already checked against the registry by
  `_validate_keyword_args`) onto `Node.extra`.
- [x] Model erd's compound rows (`Row`/`RowKey`: a wrapper row plus
  `[key tag, text label]` sub-cells) with `NodeChildCell`, constructed at
  render time in `_compound_row_override`
  (`mdg_drawio/generator/generator.py`) from the row-type sidecar's
  compound-cell template plus the node's own label/`key=` value. Only applies
  when nested (`node.parent_id` is set): `erd.RowKey` also has its own
  independent top-level palette entry (a standalone "Table Row" shape, itself
  wrapped in a mini `shape=table` container) which supplies the correct outer
  wrapper for standalone use and is the wrong outer style once nested inside
  a real Table (a table-within-a-row). A standalone `erd.RowKey(...)` keeps
  that wrapper and reconstructs its nested row/key/text hierarchy from the
  row-type template.
- [x] Add generator/XML tests for UML 2.5 row-type geometry (`Header`/
  `Item`/`Divider`/`Note`/`Lane`), BPMN row types (`SwimlaneBoxPart`/
  `TableRowBoxPart`), UML's `CompositeLabel`, and ERD's compound rows
  (nested `RowKey` renders `[key, text]` sub-cells; nested plain `Row` gets
  an empty key sub-cell with the plain-row style; standalone `RowKey` keeps
  its table wrapper and renders its row/key/text hierarchy) --
  `tests/test_notation_coverage_parser.py`, gated on `needs_sidecars` like
  other build-data-dependent tests.
- [x] `test_coverage_generated_cell_counts_match_model`'s vertex-count
  invariant updated to account for the extra real vertices a compound row's
  sub-cells legitimately add (no corresponding top-level `Node`).
- [x] `scripts/dead_code_allowlist.py` updated: `NodeChildCell` and the node
  child-cell rendering path in `generator.py` are now genuinely reached
  (removed from the allowlist); the edge-side equivalent (`ChildCell`,
  `_append_edge_child`) still has no notation emitting it and remains listed.

## Phase 3 - Production hardening (done)

Estimated total effort through this phase: 3-5 days. A safe, bounded subset
shipped 2026-08-10; full positional/keyword argument binding -- the piece
that subset deferred -- shipped 2026-08-11.

### What shipped

- [x] Test every mixed-kind function family across all registries. Scanning
  every library's `shapes_by_function()` confirms `uml` and `uml25` are the
  *only* libraries with a function whose variants mix vertex and edge kinds.
  Beyond Phase 1's `FoundMessage`/`Dependency`, that's `uml25.Constraint`,
  `uml25.Extension`, `uml25.Activity`, `uml25.Message` -- all now covered by
  `test_uml25_mixed_kind_families_classify_by_exact_variant`
  (`tests/test_notation_coverage_parser.py`). Every one already classified
  correctly under Phase 1's fix; this only adds the regression pin.
- [x] Verify generated cell counts, unique ids, and endpoint integrity. This
  is enforced in two layers:
  `mdg_drawio/engine/convert.py` calls `validate_generated_xml()`
  unconditionally on every conversion (unique cell ids, edge endpoint
  references, root-cell structure), while
  `test_coverage_generated_cell_counts_match_model` checks every coverage
  sheet's generated vertex/edge counts against its parsed model.
- [x] Convert every notation coverage sheet in the pipeline test suite --
  true since Phase 1; reconfirmed here.
- [x] Remove expected-failure branches from `tests/test_dead_code.py` --
  every fixture the sweep traces now converts, so
  `test_convertible_fixtures_convert`'s `"ok"` vs. `"exit=1"` branch was
  dead code (the `"exit=1"` branch was never taken). Simplified to a flat
  "every traced fixture converts" assertion.
- [x] Update stale parser-scope documentation and dead-code allowlist
  comments -- this document's own Phase 2 write-up, plus the fix required
  when Phase 2's validation surfaced the `uml.package.v1` registry gap.
- [x] A related gap found while scoping this phase, not on the original
  list: a passthrough node call with zero positional arguments (e.g.
  `uml.Object()`) was silently dropped -- no node, no error -- instead of
  rejected. The native C4 parser already errors on this
  (`_parse_node`'s `requires at least a node_id argument`); the shared
  passthrough builder now matches it.
- [x] Reject unknown keyword arguments for registered shapes and row types,
  while continuing to accept declared keywords even where rendering support
  for their values is still pending.
- [x] Reject undeclared variants for every registered function family. The
  earlier single-kind fallback silently paired an invalid variant number with
  variant 1's style.

### Positional/keyword argument binding: what shipped (2026-08-11)

The blocker was exactly as described: the registry's `args:` list conflated
true positional parameters with ones real documents only ever pass by
keyword, with no field distinguishing them. Fixed by extending the schema
rather than working around it:

- [x] `shape-registry.schema.json`'s `$defs/arg` gained a required `passing`
  field (`positional` | `keyword_only`). `scripts/migrate_registry_v3.py`
  (one-shot, mirrors `migrate_registry_v2.py`'s pattern) added it to all 1,734
  declared arguments across 769 signature lists in all seven registries'
  `shapes` and `row_types`, using a
  mostly name-based rule verified empirically against every coverage sheet and
  `docs/architecture/*.mdg`: `node_id`/`label`/`text`/`source`/`target`/
  `description` are positional (the only names ever used positionally
  anywhere in the codebase), except C4 relationship `description`, whose
  native contract and examples make it keyword-only; every other declared
  name (`key`, `dashed`,
  `type`, `target_label`, `source_label`, and ~20 uml25 one-offs) is
  `keyword_only`, matching how real documents already call them -- the
  migration reclassifies existing data, it does not change behavior for any
  currently-valid document.
- [x] `_declared_args(ns, function, entry)` (`dsl_engine.py`) resolves the
  arg-spec list to bind against: the shape entry's own `args` if one was
  found, else a matching row type's `args`, else `None` for an unregistered
  function (kept lenient) or a `kind: "diagram"` reference entry (no real
  contract, same reasoning as Phase 2's containment leniency).
- [x] `_bind_registry_args` binds a call's positional and keyword arguments
  against the resolved signature, Python-call-style: positional values fill
  `passing: positional` args left-to-right; keyword values bind by name;
  excess positional arguments, an argument supplied both ways, and a missing
  required argument are all line-numbered `DslError`s.
- [x] Apply the same binding contract to notation-native calls before their
  specialized builder runs. Bound positional-or-keyword values are normalized
  into the builder's legacy positional slots, while keyword-only values and
  `variant=` remain keywords. This keeps C4's palette-specific construction
  without giving it weaker argument semantics than registry-driven calls.
- [x] `_build_passthrough_node`/`_build_passthrough_edge` consume the bound
  values: `node_id` -> `Node.id`, `label`/`text` -> `Node.label`,
  `source`/`target` -> edge endpoints, `label` -> `Edge.label`, every other
  declared value -> `Node.extra`/`Edge.extra` (rendering semantics for any
  given extra value remain whatever they already were -- this phase is
  argument validation and structural mapping, not new rendering).
- [x] Fixed a real registry gap this validation surfaced:
  `erd.rowkey.v1` (variant 1) was missing its own `key` arg (variants 2/3
  already had it) -- previously masked because keyword validation used to
  unconditionally merge in a same-named row type's args even when a shape
  entry already matched, silently papering over the gap instead of catching
  it.
- [x] Regression tests cover the full example from this phase's design
  discussion (`erd.RowKey(node_id[, label][, key=])`): valid calls with 1-3
  arguments, missing required `node_id`, excess positional arguments, an
  argument supplied both positionally and by keyword, the pre-existing
  unknown-keyword rejection, and an edge case (`erd.Rel`); plus
  `general.Textbox`'s positional `description` (previously silently
  dropped, now preserved onto `Node.extra` like any other declared value).

## Delivery (as it actually happened)

Five changes, each gated on verifying the previous phase's assumptions
against real data before building the next:

1. Variant-aware registry dispatch and endpoint-free edge support, making all
   coverage sheets parse and convert (Phase 1).
2. Registry-driven rows/containment validation: real `Node`+`parent_id`
   containment, validated against `rows.allowed`/`contains.allowed` (Phase 2,
   structural validation).
3. Palette-faithful row rendering: build-time style/geometry extraction for
   every row type with no independent top-level shape, plus compound-cell
   rendering for erd's Row/RowKey and `key=`/`dashed=` preservation
   (Phase 2, rendering).
4. Safe argument hardening: zero-arg, unknown-keyword, and invalid-variant
   rejection (Phase 3, first pass).
5. Full positional/keyword argument binding: a `passing` schema field plus a
   one-shot registry migration turned the registry's `args:` list into a real
   callable signature, checked exactly like a Python function call
   (Phase 3, completed).

## Definition of done

- [x] `mdg --force` exits 0 for every `*_shapes_coverage.mdg` file.
- [x] Mixed-kind variants produce the correct `Node` or `Edge` model type,
  for every such family across all seven registries.
- [x] Row children and contained children use correct `parent_id`
  relationships, validate against `rows.allowed`/`contains.allowed`, and
  render with palette-faithful styles, dimensions, and compound cells.
- [x] Endpoint-free palette edges have stable unique ids.
- [x] Invalid blocks, variants, endpoints, and unknown keywords raise
  line-numbered errors.
- [x] Full required/optional positional argument binding: every declared
  registry arg is bound like a Python call against the resolved
  `(namespace, function, variant)` signature, with `passing` distinguishing
  positional from keyword-only.
- [x] `make check` and the required architecture pipeline smoke test pass.
