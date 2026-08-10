# TODO - Complete coverage-sheet parsing

Status: Phase 1, Phase 2, and the safe subset of Phase 3 done 2026-08-10. The
higher-risk parts of Phase 3 are explicitly deferred -- see Phase 3 below.

## Goal

Make the C4, UML, and UML 2.5 shape-coverage documents convert successfully,
validate their nested content against the registry, and harden argument
handling -- in that order, each phase re-scoped against real data rather than
assumption. Phases 1 and 2 are done; Phase 3 shipped its safe subset and
explicitly deferred the rest (see Phase 3 below).

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
  cannot open a block at all -- all with line-numbered errors. Both kinds
  render as real `Node`s with `parent_id` (see Phase 2's corrected premise).
- A passthrough call with no positional arguments is rejected instead of
  silently producing nothing.
- `tests/test_notation_coverage_parser.py` pins all of the above.
- Full registry-driven positional/keyword argument validation is deferred
  (see Phase 3) -- it needs a schema change and generator work first, not
  just a parser check.

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

## Phase 2 - Render rows and containment correctly (done, revised scope)

Estimated effort: 1-3 days. Actual: done 2026-08-10, with a materially
smaller scope than originally planned -- see "Corrected premise" below.

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

So `rows.allowed` and `contains` shapes both correctly render today as real
`Node`s with `parent_id`, positioned by the container layout engine -- that
part was never broken. The genuinely missing piece was **structural
validation**: nothing checked that a nested function name was actually legal
for its container, or that a block was only opened on a shape that declares
one of the two.

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

### Explicitly not done (dropped, not deferred)

- ~~Parse top-level `row_types` signatures and declared arguments~~ /
  ~~Preserve registry-declared keyword arguments and fixed template
  fields~~ -- these only made sense under the `NodeChildCell` premise. Row
  calls parse through the same generic passthrough-node builder as any other
  vertex now, which already handles `variant=` and the standard
  `(node_id, label)` positional shape; no row-specific argument handling is
  needed.
- ~~Build recursive `NodeChildCell` data for compartment rows~~ -- superseded
  by the corrected premise above. `NodeChildCell` remains legitimate, unused
  future scaffolding (see `scripts/dead_code_allowlist.py`) for whatever
  actually needs static compartment content -- not this feature.
- ~~Add generator/XML tests for representative UML and UML 2.5
  compartments~~ -- no generator change was made (rows already rendered
  correctly), so there is nothing new to test at that layer.

## Phase 3 - Production hardening

Estimated total effort through this phase: 3-5 days. A safe, bounded subset
shipped 2026-08-10; the rest is deferred with a concrete reason, not silently
dropped.

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
  was already continuously enforced, not something to newly build:
  `mdg_drawio/engine/convert.py` calls `validate_generated_xml()`
  unconditionally on every conversion (unique cell ids, edge endpoint
  references, root-cell structure), and `test_convertible_fixtures_convert`
  already requires every coverage sheet, `docs/architecture/*`, and
  `tests/action_fixtures/*` to pass it.
- [x] Convert every notation coverage sheet in the pipeline test suite --
  true since Phase 1; reconfirmed here.
- [x] Remove expected-failure branches from `tests/test_dead_code.py` --
  every fixture the sweep traces now converts, so
  `test_convertible_fixtures_convert`'s `"ok"` vs. `"exit=1"` branch was
  dead code (the `"exit=1"` branch was never taken). Simplified to a flat
  "every traced fixture converts" assertion.
- [x] Update stale parser-scope documentation and dead-code allowlist
  entries -- this document's own Phase 2 write-up, plus the fix required
  when Phase 2's validation surfaced the `uml.package.v1` registry gap.
- [x] A related gap found while scoping this phase, not on the original
  list: a passthrough node call with zero positional arguments (e.g.
  `uml.Object()`) was silently dropped -- no node, no error -- instead of
  rejected. The native C4 parser already errors on this
  (`_parse_node`'s `requires at least a node_id argument`); the shared
  passthrough builder now matches it.

### Deferred -- needs its own scoping, not mechanical validation

- [ ] Validate required and optional positional arguments from registry
  metadata.
- [ ] Reject unknown keyword arguments unless explicitly supported.

These two looked like straightforward validation until checked against real
registry data. They are not, for reasons that would make a fast attempt
actively harmful:

- The registry's `args:` list conflates true positional parameters
  (`node_id`, `label`, `source`, `target`) with parameters real documents
  only ever pass by keyword (e.g. `general.Rel`'s `type`, `erd.Rel`'s
  `target_label`/`source_label`, `erd.RowKey`'s `key`, `uml25.Divider`'s
  `dashed`, plus a couple dozen more one-off keywords across `uml25`
  specifically) -- there is no schema field distinguishing "positional" from
  "keyword-only," so a generic positional-arg validator cannot be written
  without first extending the schema.
- Worse: the passthrough builders (`_build_passthrough_node`,
  `_build_passthrough_edge`) only ever read the `variant=` keyword today.
  Every other keyword in that list above -- `target_label`, `source_label`,
  `type`, `key`, `dashed`, and more -- is silently accepted and discarded;
  it never reaches the `Node`/`Edge` model, and the generator has no
  rendering support for any of them either (confirmed: neither string
  appears anywhere in `mdg_drawio/generator/generator.py`). "Reject unknown
  keyword arguments" on top of that would either (a) start rejecting
  keywords real coverage sheets already use, breaking currently-passing
  fixtures, or (b) require first building parse -> model -> generator
  support for a few dozen keyword parameters end to end -- a real feature
  addition, not hardening of existing behavior.
- This needs its own scoped initiative (likely its own todo document): pick
  the keywords worth supporting, extend the registry schema to mark them
  keyword-only vs. positional, thread them through the model and generator,
  *then* validate/reject. Attempting it inside "Phase 3 hardening" risks
  exactly the kind of premise-didn't-survive-contact rework Phase 2 already
  went through once.
- [ ] Add representative visual or geometry assertions for compartmented
  shapes -- moot as originally worded (there is no compartment
  representation; see Phase 2's corrected premise). If revived, reword to
  "assert row-bearing shapes (`uml.Class`, `erd.Table`, ...) produce correct
  stack/table layout geometry" and extend
  `test_stacklayout_container_children_stack_tightly`'s pattern to more
  shapes -- a small, real, but separate task from argument validation.

## Delivery (as it actually happened)

Three changes, each gated on verifying the previous phase's assumptions
against real data before building the next:

1. Variant-aware registry dispatch and endpoint-free edge support, making all
   coverage sheets parse and convert (Phase 1).
2. Registry-driven rows/containment *validation* -- not a rendering change;
   real `Node`+`parent_id` containment was already correct (Phase 2).
3. The safe subset of argument-handling hardening; full positional/keyword
   validation deferred pending a schema change and generator work it
   depends on (Phase 3).

## Definition of done

- [x] `mdg --force` exits 0 for every `*_shapes_coverage.mdg` file.
- [x] Mixed-kind variants produce the correct `Node` or `Edge` model type,
  for every such family across all seven registries.
- [x] Row children and contained children both render as real `Node`s with
  correct `parent_id` relationships (not child cells -- see Phase 2's
  corrected premise), validated against `rows.allowed`/`contains.allowed`.
- [x] Endpoint-free palette edges have stable unique ids.
- [x] Invalid blocks, variants, and endpoints raise line-numbered errors.
  Full argument (positional/keyword) validation remains open -- see Phase 3.
- [x] `make check` and the required architecture pipeline smoke test pass.
