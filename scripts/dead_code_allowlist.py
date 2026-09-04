#!/usr/bin/env python3
"""Definitions that :mod:`scripts.analyze_dead_code` may report as unreached
without it being a defect.

The dead-code report diffs the static universe of ``mdg_drawio`` definitions
against everything the CLI action permutations touch (see
:mod:`scripts.trace_actions`). Some definitions are legitimately never reached
by that sweep:

* the standalone ``parse()`` public API and its module-global fallback, which
  the injected-port pipeline never exercises;
* layout helpers only reached by notation-native documents that the sweep's
  fixtures happen not to contain;
* pure ``@dataclass`` containers built only where neither runtime signal can
  see them: the CLI sweep's merge/sync actions always diff a freshly
  generated document against its own source (zero delta by construction), and
  the regression-suite trace's ``sys.monitoring`` callback gets no frame, so
  it can never inspect ``self`` to attribute a dataclass ``__init__`` to its
  class (see :func:`scripts.trace_actions.trace_regression_suite`).

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
    "mdg_drawio.generator.generator:StyleProvider.row_type_entry":
        "typing.Protocol method; concrete impl runs",
    "mdg_drawio.generator.generator:StyleProvider.edge_label_templates":
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
    # --- public registry/layout API, exercised outside a traced CLI action.
    "mdg_drawio.layout:modes":
        "public registry API; called while permutations are built, before tracing",
    "mdg_drawio.layout:register_layout":
        "self-registration runs at import, before tracing starts",
    "mdg_drawio.notation._core.registry:registry_path":
        "file-fallback for standalone parse(); convert injects preloaded registries",
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
    # --- inert callback: c4 passes ``diagram_title_call=""`` to
    #     parse_block_source, so this handler can never fire (no DSL call has an
    #     empty name). Scaffolding for a DiagramTitle statement not yet wired.
    "mdg_drawio.notation.c4:_parse_diagram_title":
        "wired as a callback but diagram_title_call='' disables it",
    # --- reverse-derivation test fixtures: synthesize .drawio XML for
    #     tests/test_reverse_*.py only, never called from production code
    #     (merge/derive/containment/naming/scoring/style_index). This sweep's
    #     signal is CLI-sweep + static call-graph only, no regression-suite
    #     trace (see tests/test_dead_code.py's own docstring), so a module
    #     reachable only from tests is invisible to it by construction.
    "mdg_drawio.reverse.fixtures:_as_cell":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:cell_xml":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:edge_cell_xml":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:entry_cell":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:group_cell_xml":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:layer_cell_xml":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:library_only_anchor":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    "mdg_drawio.reverse.fixtures:perturb":
        "test-only fixture helper; reached only from tests/test_reverse_*.py",
    # --- mdg_drawio.markup: markdown <-> HTML conversion for rich-text shape
    #     labels. A standalone utility, deliberately not yet wired into the
    #     DSL or the generator (see the package docstring) -- reached only
    #     from tests/test_markup.py today, same "no regression-suite trace"
    #     gap as reverse.fixtures above. Remove these entries once something
    #     in mdg_drawio (not just tests) actually calls this package.
    "mdg_drawio.markup._to_html:markdown_to_html":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_consume_fenced_code":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_consume_blockquote":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_consume_list":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_looks_like_table":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_consume_table":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_split_table_row":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_consume_paragraph":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_consume_table.<locals>._render_row":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_html:_starts_new_block":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:html_to_markdown":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_heading":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_paragraph":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_blockquote":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_pre":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_list":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_table":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._to_markdown:_render_block":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._inline:markdown_inline_to_html":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._inline:markdown_inline_to_html.<locals>._restore":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._inline:html_inline_to_markdown":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._inline:_stash":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    "mdg_drawio.markup._inline:_escape_markdown_text":
        "not yet wired into the DSL/generator; reached only from tests/test_markup.py",
    # --- pure @dataclass containers only ever instantiated where neither
    #     runtime signal can see it: sys.monitoring's PY_START callback
    #     (scripts/trace_actions.py:trace_regression_suite) gets no frame, so
    #     it can never inspect ``self`` and attribute a dataclass __init__ to
    #     its class -- the ONLY signal that can is the CLI sweep's settrace
    #     self-inspection (_class_key), and merge/sync always diff a
    #     freshly-`--force`-generated .drawio against its own source (zero
    #     delta by construction), so the "new element"/"current edge" branches
    #     that build these never run there either. All five are real,
    #     exercised code (see mdg_drawio/reverse/merge.py's _build_forest,
    #     _build_insertions, _build_edge_insertion, _current_edges,
    #     _existing_edges, and tests/test_reverse_merge.py's direct
    #     construction), just structurally invisible to both signals.
    "mdg_drawio.reverse.merge:NewNode":
        "dataclass; self-inspection blind spot + merge sweep always diffs zero-delta",
    "mdg_drawio.reverse.merge:Insertion":
        "dataclass; self-inspection blind spot + merge sweep always diffs zero-delta",
    "mdg_drawio.reverse.merge:NewEdge":
        "dataclass; self-inspection blind spot + merge sweep always diffs zero-delta",
    "mdg_drawio.reverse.merge:CurrentEdge":
        "dataclass; self-inspection blind spot + sync sweep always diffs zero-delta",
    "mdg_drawio.reverse.merge:ExistingEdge":
        "dataclass; self-inspection blind spot + sync sweep always diffs zero-delta",
    # --- same self-inspection blind spot: Weights is only ever built once, as
    #     the DEFAULT_WEIGHTS module constant (scoring.py), which runs at
    #     import time before the CLI-sweep tracer attaches. Every later call
    #     site (derive.py, style_index.py) reuses that constant as a default
    #     argument rather than constructing a new Weights -- so no call site
    #     left in production code can ever be caught by either signal.
    "mdg_drawio.reverse.scoring:Weights":
        "dataclass; only built once, as a module-level default, "
        "before the tracer attaches",
}
