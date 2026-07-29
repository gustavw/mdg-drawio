"""Tests for the traceability meta-model (scripts/check_traceability.py).

The C4 view declares cross-file ``trace`` links to the Code view; these tests
lock in the ruleset that keeps the two consistent, and prove each rule fails on
a deliberately broken model.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_traceability.py"
C4_MDG = ROOT / "docs" / "architecture" / "c4_architecture.mdg"

_REAL_TRACE = "trace layered_co -> code.layout_layered : realized-by"
_XML_TRACE = "trace xml_utils_co -> code.generator_xml_utils : realized-by"
_SATISFIES = "trace overlay_co -> decisions.adr_0004 : satisfies"
_SATISFIES_MODELS = "trace models_contract_co -> decisions.adr_0001 : satisfies"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_traceability", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_traceability"] = module
    spec.loader.exec_module(module)
    return module


def _check_mutated(replace: str, with_: str, tmp_path: Path) -> list[str]:
    """Run the traceability checker on a copy of the C4 file with one edit."""
    src = C4_MDG.read_text(encoding="utf-8")
    assert replace in src, f"fixture text not found: {replace!r}"
    mutated = tmp_path / "c4_architecture.mdg"
    mutated.write_text(src.replace(replace, with_, 1), encoding="utf-8")
    return _load_checker().check(mutated)


def test_real_model_is_consistent() -> None:
    assert _load_checker().check() == []


def test_component_modules_supersede_the_hardcoded_map() -> None:
    """The declared traces reproduce (and extend) the former hardcoded map."""
    mapping = _load_checker().component_modules()

    assert mapping["cli_co"] == {"cli.py"}
    # engine.py was split into three modules, one per engine component.
    assert mapping["engine_convert_co"] == {"engine/convert.py"}
    assert mapping["engine_preload_co"] == {"engine/preload.py"}
    assert mapping["engine_validate_co"] == {"engine/validate.py"}
    # types_co covers both modules it realizes — richer than the old 1:1 map.
    assert mapping["types_co"] == {"layout/_types.py", "layout/layout.py"}


def test_orphan_code_node_is_rejected(tmp_path: Path) -> None:
    """Removing the only trace to a module leaves it orphaned."""
    errors = _check_mutated(_XML_TRACE + "\n", "", tmp_path)

    assert any("orphan" in e and "generator_xml_utils" in e for e in errors)


def test_dangling_trace_target_is_rejected(tmp_path: Path) -> None:
    """A trace to a node that does not exist in the referenced view fails."""
    errors = _check_mutated(
        "code.generator_xml_utils", "code.does_not_exist", tmp_path
    )

    assert any("does not exist in the referenced view" in e for e in errors)


def test_module_under_wrong_component_is_rejected(tmp_path: Path) -> None:
    """A module must be nested under the component package that traces to it."""
    errors = _check_mutated(
        _REAL_TRACE,
        "trace layered_co -> code.generator_generator : realized-by",
        tmp_path,
    )

    assert any("must live in its component's package" in e for e in errors)


def test_unknown_source_component_is_rejected(tmp_path: Path) -> None:
    """A trace whose source is not a declared Component fails."""
    errors = _check_mutated(
        _REAL_TRACE,
        "trace bogus_co -> code.layout_layered : realized-by",
        tmp_path,
    )

    assert any("is not a Component" in e for e in errors)


def test_unknown_relation_type_is_rejected(tmp_path: Path) -> None:
    """A relation type absent from RELATION_RULES is rejected."""
    errors = _check_mutated(
        _REAL_TRACE,
        _REAL_TRACE + "\ntrace layered_co -> code.layout_layered : bogus-rel",
        tmp_path,
    )

    assert any("unknown relation type" in e and "bogus-rel" in e for e in errors)


def test_rule_table_supports_additional_relation_types(tmp_path: Path) -> None:
    """The engine validates any relation type registered in RELATION_RULES.

    Proves the meta-model is a data-driven rule table: the same trace is
    accepted when its type is registered and rejected when it is not.
    """
    checker = _load_checker()
    rules = {
        **checker.RELATION_RULES,
        "annotates": checker.RelationRule(
            description="test-only relation",
            target_alias="code",
            require_package_parity=False,
            require_target_coverage=False,
        ),
    }
    mutated = tmp_path / "c4_architecture.mdg"
    mutated.write_text(
        C4_MDG.read_text(encoding="utf-8").replace(
            _REAL_TRACE,
            _REAL_TRACE + "\ntrace layered_co -> code.layout_layered : annotates",
            1,
        ),
        encoding="utf-8",
    )

    assert checker.check(mutated, rules=rules) == []
    assert any("unknown relation type" in e for e in checker.check(mutated))


def test_malformed_relation_token_is_reported(tmp_path: Path) -> None:
    """A trace whose relation is not a single token must not vanish silently.

    ``: bogus rel`` (a space in the relation) fails ``_TRACE_RE`` and would
    otherwise be stripped by the DSL parser with no error.
    """
    errors = _check_mutated(
        _REAL_TRACE,
        _REAL_TRACE + "\ntrace layered_co -> code.layout_layered : bogus rel",
        tmp_path,
    )

    assert any("malformed trace directive" in e for e in errors)


def test_malformed_trace_syntax_is_reported(tmp_path: Path) -> None:
    """A ``trace`` line missing its arrow/colon structure is reported."""
    errors = _check_mutated(
        _REAL_TRACE,
        _REAL_TRACE + "\ntrace layered_co code.layout_layered realized-by",
        tmp_path,
    )

    assert any("malformed trace directive" in e for e in errors)


def test_malformed_use_syntax_is_reported(tmp_path: Path) -> None:
    """A ``use`` line missing the quotes around its file is reported."""
    errors = _check_mutated(
        'use "code_architecture.mdg" as code',
        "use code_architecture.mdg as code",
        tmp_path,
    )

    assert any("malformed use directive" in e for e in errors)


def test_satisfies_is_registered_in_the_vocabulary() -> None:
    """Phase 5 activates the `satisfies` relation type."""
    assert "satisfies" in _load_checker().RELATION_RULES


def test_orphan_decision_is_rejected(tmp_path: Path) -> None:
    """Every architecture decision must be satisfied by at least one Component."""
    errors = _check_mutated(_SATISFIES + "\n", "", tmp_path)

    assert any("orphan" in e and "adr_0004" in e for e in errors)


def test_satisfies_must_target_the_decisions_view(tmp_path: Path) -> None:
    """A `satisfies` trace pointing at the wrong view is rejected."""
    errors = _check_mutated(
        _SATISFIES_MODELS,
        "trace models_contract_co -> code.contracts_models : satisfies",
        tmp_path,
    )

    assert any("must target the 'decisions' view" in e for e in errors)
