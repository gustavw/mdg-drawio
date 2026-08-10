# TODO - Complete coverage-sheet parsing

Status: Phase 1 done 2026-08-10. Phase 2 and Phase 3 open.

## Goal

Make the C4, UML, and UML 2.5 shape-coverage documents convert successfully,
then make their compartment and containment output structurally correct.

Current behavior:

- All seven coverage sheets (ArchiMate, BPMN, C4, ERD, General, UML, UML 2.5)
  convert successfully.
- Registry dispatch resolves node-versus-edge by the exact
  `(namespace, function, variant)` entry, so mixed-kind function families
  (`uml.FoundMessage`, `uml25.Dependency`) classify correctly per variant.
- The C4 edge builder accepts the `None, None` unconnected palette-edge form
  and still rejects one-sided `None` with a line-numbered `DslError`.
- Style resolution (`resolve_style`/`resolve_edge_style`) and `PaletteLayout`
  repositioning both carry the resolved variant and all non-geometry fields
  through, so a variant-2 shape no longer renders with variant 1's style.
- `tests/test_notation_coverage_parser.py` pins the mixed-kind classification,
  the C4 `None, None` form, and variant-aware style resolution.
- Phase 2 (rows/containment correctness) has not been started: the shared
  parser still treats every indented child as a contained node, not as
  registry-driven rows vs. containment.

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

## Phase 2 - Render rows and containment correctly

Estimated effort: 1-3 days.

The shared parser currently treats every indented child as a contained diagram
node. Registry semantics require two distinct paths:

- `rows.allowed`: build compartment content as `NodeChildCell` structures.
- `contains.allowed`: build real child nodes with `parent_id` containment.

Tasks:

- [ ] Resolve the parent registry entry before processing its block.
- [ ] Reject blocks for shapes that declare neither rows nor containment.
- [ ] Validate row function names against `rows.allowed`.
- [ ] Parse top-level `row_types` signatures and declared arguments.
- [ ] Build recursive `NodeChildCell` data for compartment rows.
- [ ] Validate contained node functions against `contains.allowed`.
- [ ] Preserve registry-declared keyword arguments and fixed template fields.
- [ ] Keep all validation errors line-numbered and actionable.
- [ ] Add generator/XML tests for representative UML and UML 2.5 compartments.
- [ ] Add containment tests proving rows do not become independent vertices.

The generator already supports recursive node child cells, so this phase is
primarily registry-driven parser and model-assembly work.

## Phase 3 - Production hardening

Estimated total effort through this phase: 3-5 days.

- [ ] Validate required and optional positional arguments from registry metadata.
- [ ] Reject unknown keyword arguments unless explicitly supported.
- [ ] Test every mixed-kind function family across all registries.
- [ ] Convert every notation coverage sheet in the pipeline test suite.
- [ ] Verify generated cell counts, unique ids, and endpoint integrity.
- [ ] Add representative visual or geometry assertions for compartmented shapes.
- [ ] Remove expected-failure branches from `tests/test_dead_code.py`.
- [ ] Update stale parser-scope documentation and dead-code allowlist entries.

## Recommended delivery

Split the work into two changes:

1. Variant-aware registry dispatch and endpoint-free edge support, making all
   coverage sheets parse and convert.
2. Registry-driven rows and containment, making the generated UML output
   structurally and visually correct.

## Definition of done

- `mdg --force` exits 0 for every `*_shapes_coverage.mdg` file.
- Mixed-kind variants produce the correct `Node` or `Edge` model type.
- Row children render as child cells, not standalone vertices.
- Contained children retain correct parent relationships.
- Endpoint-free palette edges have stable unique ids.
- Invalid blocks, arguments, variants, and endpoints raise line-numbered errors.
- `make check` and the required architecture pipeline smoke test pass.
