"""Lockfile tests for the Code-view module import graph.

``docs/architecture/code_architecture.mdg`` is generated from the real import
graph by ``scripts/generate_code_arch.py``. These tests guarantee the two can
never drift: the committed diagram must equal what the generator produces from
the current source tree (so it both follows the code and adds no extra edges),
and every significant module must be modelled as a node.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "generate_code_arch.py"
CODE_ARCH_MDG = ROOT / "docs" / "architecture" / "code_architecture.mdg"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_code_arch", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_view_matches_import_graph() -> None:
    """The committed Code view must equal the generated import graph.

    Because the generator derives every node and edge purely from imports,
    equality here means the diagram follows the code exactly — no missing and
    no extra relationships.
    """
    gen = _load_generator()

    assert CODE_ARCH_MDG.read_text(encoding="utf-8") == gen.render(), (
        "code_architecture.mdg is out of sync with the import graph. "
        "Run: python scripts/generate_code_arch.py --write"
    )


def test_every_significant_module_is_modelled() -> None:
    """No significant module may be silently dropped from the Code view."""
    gen = _load_generator()
    content = CODE_ARCH_MDG.read_text(encoding="utf-8")
    declared = set(re.findall(r"uml\.Class\((\w+),", content))

    missing = [
        gen._node_id(gen._rel(path))
        for path in gen._significant_modules()
        if gen._node_id(gen._rel(path)) not in declared
    ]

    assert not missing, f"modules not modelled as nodes: {sorted(missing)}"


def test_plain_import_is_captured_as_a_dependency() -> None:
    """A plain ``import mdg_drawio.x`` is a real dependency, not just ``from``.

    The codebase currently has no live plain internal import, so this guards the
    resolver capability directly: ``_import_targets`` must resolve a plain
    ``import mdg_drawio.generator as g`` to the generator package (else such a
    dependency would silently vanish from the Code view).
    """
    import ast

    gen = _load_generator()
    node = ast.parse("import mdg_drawio.generator as g").body[0]
    source = ROOT / "mdg_drawio" / "engine" / "preload.py"

    assert "mdg_drawio.generator" in gen._import_targets(node, source)


def test_every_edge_endpoint_is_declared() -> None:
    """Every dependency edge must connect declared nodes (no dangling ids)."""
    content = CODE_ARCH_MDG.read_text(encoding="utf-8")
    declared = set(re.findall(r"uml\.(?:Class|Package)\((\w+),", content))
    edges = re.findall(r"uml\.Dependency\((\w+),\s*(\w+)\)", content)

    dangling = sorted(
        {endpoint for edge in edges for endpoint in edge} - declared
    )

    assert not dangling, f"edge endpoints missing a node declaration: {dangling}"
