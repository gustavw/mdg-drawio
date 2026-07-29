"""Preload / dependency-injection contract tests.

The engine pre-loads registry data into memory once at startup and builds a
``StyleProvider`` that the generator receives by injection (the injected-ports
invariant in AGENTS.md; detail in skill://generator). Notation's registry keeps its
cache + setter (it feeds parsing); the generator holds no global state, and
notation no longer caches style data at all.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

import mdg_drawio.generator.generator as gen
from mdg_drawio.contracts import Document, Node
from mdg_drawio.engine.preload import preload_core
from mdg_drawio.generator import create_style_provider, generate
from mdg_drawio.notation import parse
from mdg_drawio.notation._core import registry as registry_module


def _reset_registry_state() -> None:
    registry_module.load_registry.cache_clear()
    registry_module._registries = None


def test_preloaded_registries_do_not_touch_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preloaded registry data must be authoritative (the single parse cache)."""
    _reset_registry_state()

    def _deny(library: str) -> Path:
        raise AssertionError(f"unexpected registry file read: {library}")

    monkeypatch.setattr(registry_module, "registry_path", _deny)

    expected = {"shapes": [{"id": "c4.person.v1", "function": "Person"}]}
    try:
        registry_module.set_registries({"c4": expected})
        assert registry_module.load_registry("c4") == expected
    finally:
        _reset_registry_state()


def test_generation_is_filesystem_free_given_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a StyleProvider is built, generation reads nothing from disk.

    The provider *is* the generator's preloaded data (built by the composition
    root). Generation uses only the injected, in-memory provider.
    """
    provider = create_style_provider(  # build reads the committed config once
        registries={}, styles={}, overrides=gen.load_style_overrides()
    )

    def _deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("generation touched the filesystem")

    monkeypatch.setattr(registry_module, "registry_path", _deny)
    monkeypatch.setattr(gen, "load_style_overrides", _deny)

    doc = Document(nodes=[Node(id="p", type="uml.Package", label="P")])
    xml = generate(doc, provider)

    # The override was applied — proving it came from the injected provider, not
    # a file read (which would have raised above).
    assert "align=left" in xml


def test_full_pipeline_is_filesystem_free_after_preload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole conversion (parse -> layout -> generate) must not re-read the
    preloaded registry files once they are in memory.

    Runs the real ``convert()`` with its internal re-preload neutralized and the
    registry file seam poisoned. Building the StyleProvider reads the small
    committed config once (allowed, load-once); parse/layout/generate then run on
    in-memory data alone.
    """
    convert_module = importlib.import_module("mdg_drawio.engine.convert")
    _reset_registry_state()
    registries, styles = preload_core()  # the one allowed data read — at startup

    monkeypatch.setattr(
        convert_module, "preload_core", lambda: (registries, styles)
    )

    def _deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("pipeline read registry data after preload")

    monkeypatch.setattr(registry_module, "registry_path", _deny)

    src = tmp_path / "in.mdg"
    src.write_text(
        'c4.Person(author, "Author")\n'
        'c4.System(sys, "System")\n'
        'c4.Rel(author, sys, "Uses")\n',
        encoding="utf-8",
    )
    out = tmp_path / "out.drawio"
    try:
        assert convert_module.convert(src, out, force=True) == 0
        assert out.exists()
    finally:
        _reset_registry_state()


def test_set_registries_replaces_cached_preload_data() -> None:
    """Installing new registries must clear stale cached registry results."""
    _reset_registry_state()
    first_registry = {"shapes": [{"function": "Association", "kind": "node"}]}
    second_registry = {"shapes": [{"function": "Association", "kind": "edge"}]}
    try:
        registry_module.set_registries({"uml": first_registry})
        assert registry_module.load_registry("uml") == first_registry

        registry_module.set_registries({"uml": second_registry})
        assert registry_module.load_registry("uml") == second_registry
    finally:
        _reset_registry_state()


def test_direct_parse_uses_registry_fallback_for_foreign_edges() -> None:
    """Direct parser use should classify foreign notation edges without preload."""
    _reset_registry_state()
    try:
        doc = parse(
            'c4.Person(a, "A")\n'
            'c4.Person(b, "B")\n'
            'uml.Association(a, b, "Uses")\n'
        )
        assert isinstance(doc, Document)
        assert [edge.type for edge in doc.edges] == ["uml.Association"]
        assert [node.id for node in doc.nodes] == ["a", "b"]
    finally:
        _reset_registry_state()
