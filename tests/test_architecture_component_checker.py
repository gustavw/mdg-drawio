"""Regression tests for the architecture component checker script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check_architecture_components.py"
ARCHITECTURE_MDG = ROOT / "docs" / "architecture" / "c4_architecture.mdg"
CODE_ARCHITECTURE_MDG = ROOT / "docs" / "architecture" / "code_architecture.mdg"


def _run_checker_paths(
    paths: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(CHECKER)]
    if paths is not None:
        args.extend(str(path) for path in paths)
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_checker(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    mdg_path = tmp_path / "architecture.mdg"
    mdg_path.write_text(source, encoding="utf-8")
    return _run_checker_paths([mdg_path, CODE_ARCHITECTURE_MDG])


def test_checker_passes_for_real_architecture_file() -> None:
    result = _run_checker_paths()

    assert result.returncode == 0
    assert "All checks pass" in result.stdout


def test_checker_uses_parser_for_quoted_relationship_ids(tmp_path: Path) -> None:
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        "c4.Rel(author, drawiogen_sys",
        'c4.Rel("author", "drawiogen_sys"',
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 0


def test_checker_validates_relationship_endpoints_on_all_pages(
    tmp_path: Path,
) -> None:
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        "c4.Rel(author, drawiogen_sys",
        "c4.Rel(author, missing_sys",
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "DANGLING TARGET: Context" in result.stdout


def test_checker_enforces_relationship_count_snapshot(tmp_path: Path) -> None:
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        'c4.Rel(drawiogen_sys, drawio, "Converts MDG to draw.io; '
        'round-trip planned", description="")\n',
        "",
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "RELATION COUNT MISMATCH: Context" in result.stdout


def test_checker_enforces_context_relationship_semantics(tmp_path: Path) -> None:
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        "c4.Rel(author, drawiogen_sys",
        "c4.Rel(author, drawiogen",
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "CONTEXT POLICY" in result.stdout


def test_checker_requires_component_relationship_classification(
    tmp_path: Path,
) -> None:
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    source = source.replace(
        '  c4.Person(author_co, "Diagram Author / Developer"',
        (
            '  c4.Component(unmapped_co, "Unmapped", '
            '"Synthetic test component.", technology="Python")\n\n'
            '  c4.Person(author_co, "Diagram Author / Developer"'
        ),
        1,
    )
    source = source.replace(
        'c4.Rel(author_co, cli_co, "Runs", description="")',
        (
            'c4.Rel(unmapped_co, cli_co, "Calls", description="")\n'
            'c4.Rel(author_co, cli_co, "Runs", description="")'
        ),
        1,
    )

    result = _run_checker(source, tmp_path)

    assert result.returncode == 1
    assert "UNMAPPED RELATION ENDPOINT" in result.stdout


def test_checker_rejects_documented_edge_without_import(tmp_path: Path) -> None:
    """Forward direction: the diagram may not claim dependencies the code lacks.

    A Component ``c4.Rel`` between two mapped components in different packages
    must correspond to a real cross-package import. Here contracts does not
    import cli, so the fabricated edge must be rejected.
    """
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        'c4.Rel(engine_convert_co, contracts_co_boundary, '
        '"Uses contracts", description="")',
        'c4.Rel(engine_convert_co, contracts_co_boundary, '
        '"Uses contracts", description="")\n'
        'c4.Rel(models_contract_co, cli_co, "Fictional dependency", '
        'description="")',
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "MISSING IMPORT" in result.stdout


def test_cross_package_edge_is_verified_per_component_module(
    tmp_path: Path,
) -> None:
    """A cross-package edge must be backed by the *source Component's* modules.

    ``engine_convert_co`` imports ``mdg_drawio.layout`` (via convert.py), but
    ``engine_validate_co`` (validate.py) imports nothing. Re-pointing the
    convert→layout edge at validate must fail: a sibling module importing the
    target may no longer satisfy another Component's edge.
    """
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        'c4.Rel(engine_convert_co, layout_co_boundary, "Resolves layout config"',
        'c4.Rel(engine_validate_co, layout_co_boundary, "Resolves layout config"',
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "MISSING IMPORT" in result.stdout
    assert "engine_validate_co" in result.stdout


def test_checker_rejects_import_without_documented_edge(tmp_path: Path) -> None:
    """Reverse direction: the code may not add undocumented cross-package deps.

    ``engine`` imports ``contracts`` and the only Component edge recording that
    dependency is engine_convert_co -> contracts_co_boundary. Removing it leaves
    a real import with no matching relationship, which must be rejected.
    """
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        'c4.Rel(engine_convert_co, contracts_co_boundary, '
        '"Uses contracts", description="")\n',
        "",
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "MISSING EDGE" in result.stdout
    assert "engine_convert_co" in result.stdout


def test_reverse_import_check_is_component_granular(tmp_path: Path) -> None:
    """A sibling Component's documented package edge cannot hide an import."""
    source = ARCHITECTURE_MDG.read_text(encoding="utf-8")
    mutated = source.replace(
        'c4.Rel(engine_convert_co, layout_co_boundary, "Resolves layout config"',
        'c4.Rel(engine_validate_co, layout_co_boundary, "Resolves layout config"',
        1,
    )

    result = _run_checker(mutated, tmp_path)

    assert result.returncode == 1
    assert "MISSING EDGE: engine_convert_co" in result.stdout
