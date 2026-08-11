#!/usr/bin/env python3
"""Definitions that :mod:`scripts.analyze_dead_code` may report as unreached
without it being a defect.

The dead-code report diffs the static universe of ``mdg_drawio`` definitions
against everything the CLI action permutations touch (see
:mod:`scripts.trace_actions`). Some definitions are legitimately never reached
by that sweep:

* code behind notations the pipeline does not yet convert (only ``c4`` runs);
* the standalone ``parse()`` public API and its module-global fallback, which
  the injected-port pipeline never exercises;
* layout helpers only reached by notation-native documents that the sweep's
  fixtures happen not to contain.

Each entry maps ``"module:qualname"`` -> reason. The reason is mandatory: an
allowlist without justification is just suppressed signal.

**This list is kept honest by a test.** ``tests/test_dead_code.py`` fails if any
entry here is either not a real definition or *is* actually touched by the
covering sweep (a stale entry). Remove an entry the moment it no longer applies;
never add one to silence a genuine dead-code finding — delete the dead code
instead.
"""

from __future__ import annotations

# module:qualname -> why it is allowed to be unreached by the action sweep.
#
# Only structurally-unreachable definitions belong here: things the convert
# pipeline can never call by construction (typing contracts, abstract bases,
# APIs on the standalone/build-data paths). Definitions that *look* like they
# should run in convert but do not are deliberately kept OUT of this list so the
# report surfaces them for review.
ALLOWLIST: dict[str, str] = {
    # --- typing.Protocol contracts: structural types, never called directly.
    # The concrete implementation (PaletteStyleProvider) is what runs.
    "mdg_drawio.generator.generator:StyleProvider":
        "typing.Protocol; concrete impl is PaletteStyleProvider",
    "mdg_drawio.generator.generator:StyleProvider.resolve_style":
        "typing.Protocol method; concrete impl runs",
    "mdg_drawio.generator.generator:StyleProvider.resolve_edge_style":
        "typing.Protocol method; concrete impl runs",
    "mdg_drawio.generator.generator:StyleProvider.label_template":
        "typing.Protocol method; concrete impl runs",
    "mdg_drawio.generator.generator:StyleProvider.style_corrections":
        "typing.Protocol method; concrete impl runs",
    "mdg_drawio.generator.generator:StyleProvider.type_padding":
        "typing.Protocol method; concrete impl runs",
    "mdg_drawio.layout._types:SizeResolver":
        "typing.Protocol; concrete resolver is injected",
    "mdg_drawio.layout._types:SizeResolver.__call__":
        "typing.Protocol method; concrete resolver runs",
    "mdg_drawio.notation._core.dsl_engine:NodeBuilder":
        "typing.Protocol for DSL node construction",
    "mdg_drawio.notation._core.dsl_engine:NodeBuilder.__call__":
        "typing.Protocol method",
    "mdg_drawio.notation._core.dsl_engine:EdgeBuilder":
        "typing.Protocol for DSL edge construction",
    "mdg_drawio.notation._core.dsl_engine:EdgeBuilder.__call__":
        "typing.Protocol method",
    "mdg_drawio.notation._core.dsl_engine:IsEdge":
        "typing.Protocol for DSL edge detection",
    "mdg_drawio.notation._core.dsl_engine:IsEdge.__call__":
        "typing.Protocol method",
    # --- abstract base: the concrete layout subclasses run, not the ABC.
    "mdg_drawio.layout._types:BaseLayout":
        "ABC; concrete layouts (Layered/Sequence/...) are what run",
    "mdg_drawio.layout._types:BaseLayout.apply":
        "@abstractmethod; overridden by every concrete layout",
    # --- public registry/layout API, exercised at import time or by tests,
    #     never by the convert pipeline itself.
    "mdg_drawio.layout:modes":
        "public registry API; called by tooling/tests, not convert",
    "mdg_drawio.layout:register_layout":
        "self-registration runs at import, before tracing starts",
    "mdg_drawio.notation._core.registry:registry_path":
        "file-fallback for standalone parse(); convert injects preloaded registries",
    "mdg_drawio.notation._core.registry:shapes_by_id":
        "public registry API used by tests/build-data, not convert",
    # --- notation build-data pipeline (scripts/build_notation_styles.py and
    #     scripts/migrate_registry_v2.py), not the convert path.
    "mdg_drawio.notation._core.normalize:normalize_style":
        "build-data style pipeline only",
    "mdg_drawio.notation._core.normalize:style_fingerprint":
        "build-data style pipeline only",
    "mdg_drawio.notation._core.palette:anchor_cell":
        "build-data palette pipeline only",
    "mdg_drawio.notation._core.palette:entry_groups":
        "build-data palette pipeline only",
    "mdg_drawio.notation._core.palette:flatten_entries":
        "build-data palette pipeline only",
    "mdg_drawio.notation._core.palette:top_level":
        "build-data palette pipeline only",
    # --- edge child-cell rendering only. Node child cells (NodeChildCell) are
    #     reachable since Phase 2's compound-row rendering (erd Row/RowKey's
    #     [key tag, text label] sub-cells; see
    #     mdg_drawio.generator.generator:_compound_row_override and
    #     todo/notation-coverage-parser.md Phase 2). The edge-side equivalent
    #     (Edge.child_cells: list[ChildCell]) has no notation emitting it yet.
    "mdg_drawio.contracts.models:ChildCell":
        "edge child-cell rendering; no notation emits edge child_cells yet",
    "mdg_drawio.generator.generator:_append_edge_child":
        "edge child-cell rendering; no notation emits edge child_cells yet",
    # --- inert callback: c4 passes ``diagram_title_call=""`` to
    #     parse_block_source, so this handler can never fire (no DSL call has an
    #     empty name). Scaffolding for a DiagramTitle statement not yet wired.
    "mdg_drawio.notation.c4:_parse_diagram_title":
        "wired as a callback but diagram_title_call='' disables it",
}
