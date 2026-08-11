"""Registry validation: schema conformance plus the cross-checks that the
schema cannot express. Runs without the generated palette data; the
fingerprint join tests skip when tools/styles/output/ is absent.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from mdg_drawio.notation import (
    DATA_DIR,
    LIBRARIES,
    NOTATION_DIR,
    load_registry,
)
from mdg_drawio.notation._core import (
    anchor_cell,
    flatten_entries,
    shapes_by_id,
    style_fingerprint,
)
from mdg_drawio.notation._core import registry as _registry_module
from scripts.build_notation_styles import LIBRARY_PALETTE_JSON, PALETTE_OUTPUT_DIR

ID_RE = re.compile(r"^([a-z0-9]+)\.([a-z0-9_]+)\.v([0-9]+)$")

_SCHEMA = json.loads(
    (NOTATION_DIR / "shape-registry.schema.json").read_text(encoding="utf-8")
)
_VALIDATOR = Draft202012Validator(_SCHEMA)

pytestmark = pytest.mark.parametrize("library", LIBRARIES)


@pytest.fixture(autouse=True)
def _registries_from_disk() -> None:
    """Force ``load_registry`` to read YAML from disk for every test here.

    This suite validates the committed on-disk registries. It must not depend on
    another test file resetting the module-global ``_registries`` cache in a
    ``finally`` — an omission there would otherwise make these tests silently
    validate injected in-memory data instead. Reset it explicitly.
    """
    _registry_module.load_registry.cache_clear()
    _registry_module._registries = None


def _example_calls(example: str) -> list[str]:
    """Non-comment lines of an example."""
    return [
        line for line in example.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_schema_valid(library: str) -> None:
    errors = list(_VALIDATOR.iter_errors(load_registry(library)))
    details = "\n".join(
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message[:160]}"
        for e in errors[:20]
    )
    assert not errors, f"{len(errors)} schema errors:\n{details}"


def test_ids_unique_and_consistent(library: str) -> None:
    doc = load_registry(library)
    seen: set[str] = set()
    for shape in doc["shapes"]:
        shape_id = shape["id"]
        assert shape_id not in seen, f"duplicate id {shape_id}"
        seen.add(shape_id)
        m = ID_RE.match(shape_id)
        assert m, f"malformed id {shape_id}"
        assert m.group(1) == doc["library"], f"{shape_id}: wrong library prefix"
        assert m.group(2) == shape["function"].lower(), \
            f"{shape_id}: id does not match function {shape['function']!r}"
        assert int(m.group(3)) == shape["variant"], \
            f"{shape_id}: id does not match variant {shape['variant']}"


def test_menu_index_sequence(library: str) -> None:
    indexes = [s["menu_index"] for s in load_registry(library)["shapes"]]
    assert indexes == list(range(1, len(indexes) + 1)), \
        "menu_index must be 1..N in file order"


def test_related_refs_resolve_within_library(library: str) -> None:
    by_id = shapes_by_id(library)
    for shape in load_registry(library)["shapes"]:
        for group in ("variants", "see_also"):
            for ref in shape["related"][group]:
                assert ref in by_id, \
                    f"{shape['id']}: related.{group} -> {ref} does not resolve"


def test_examples_are_namespaced(library: str) -> None:
    prefix = f"{library}."
    for shape in load_registry(library)["shapes"]:
        calls = _example_calls(shape["example"])
        assert calls, f"{shape['id']}: empty example"
        for call in calls:
            assert call.lstrip().startswith(prefix), \
                f"{shape['id']}: example call not namespaced: {call.strip()!r}"


def test_reviewed_entries_are_actually_reviewed(library: str) -> None:
    for shape in load_registry(library)["shapes"]:
        if shape["status"] != "reviewed":
            continue
        sid, fn = shape["id"], shape["function"]
        blob = yaml.dump(
            {k: shape.get(k) for k in
             ("summary", "discriminator", "use_when", "avoid_when", "example")}
        )
        assert "TODO" not in blob, f"{sid}: reviewed but contains TODO"
        assert shape["summary"].strip().rstrip(".") != fn, \
            f"{sid}: reviewed but summary is the placeholder {shape['summary']!r}"
        assert shape["use_when"] != [f"using {fn}"], \
            f"{sid}: reviewed but use_when is the generated placeholder"


def test_non_buildable_entries_point_elsewhere(library: str) -> None:
    for shape in load_registry(library)["shapes"]:
        if shape["buildable"]:
            continue
        sid = shape["id"]
        assert shape["related"]["see_also"], \
            f"{sid}: buildable:false requires see_also alternatives"
        # The example must not build THIS entry (another variant of the same
        # function is a legitimate alternative, e.g. lifeline.v2 -> v1).
        own_call = f"{library}.{shape['function']}("
        for call in _example_calls(shape["example"]):
            if own_call not in call:
                continue
            variant_match = re.search(r"variant=(\d+)", call)
            call_variant = int(variant_match.group(1)) if variant_match else 1
            assert call_variant != shape["variant"], \
                f"{sid}: buildable:false but example builds it: {call.strip()!r}"


def test_rows_allowed_are_defined_row_types(library: str) -> None:
    doc = load_registry(library)
    defined = {rt["name"] for rt in doc.get("row_types", [])}
    for shape in doc["shapes"]:
        for row in (shape.get("rows") or {}).get("allowed", []):
            assert row in defined, \
                f"{shape['id']}: rows.allowed {row!r} not defined in row_types"


def test_contains_excludes_rows(library: str) -> None:
    for shape in load_registry(library)["shapes"]:
        if "contains" in shape:
            assert not (shape.get("rows") or {}).get("allowed"), \
                f"{shape['id']}: contains and non-empty rows are mutually exclusive"
            assert shape["contains"]["allowed"], \
                f"{shape['id']}: contains.allowed must not be empty"


def test_arg_lists_are_bindable_signatures(library: str) -> None:
    """Registry argument order must form an unambiguous Python-like call."""
    doc = load_registry(library)
    owners = [*doc.get("row_types", []), *doc["shapes"]]
    for owner in owners:
        owner_name = owner.get("id", owner.get("name", "<unknown>"))
        args = owner.get("args", [])
        names = [arg["name"] for arg in args]
        assert len(names) == len(set(names)), (
            f"{owner_name}: duplicate argument names: {names}"
        )

        saw_keyword_only = False
        saw_optional_positional = False
        for arg in args:
            if arg["passing"] == "keyword_only":
                saw_keyword_only = True
                continue
            assert not saw_keyword_only, (
                f"{owner_name}: positional argument follows keyword-only argument"
            )
            assert not (saw_optional_positional and arg["required"]), (
                f"{owner_name}: required positional argument follows an optional one"
            )
            saw_optional_positional = not arg["required"]

        kind = owner.get("kind")
        if kind == "edge":
            assert names[:2] == ["source", "target"], (
                f"{owner_name}: edge signature must start with source, target"
            )
        elif args and (kind == "vertex" or "name" in owner):
            assert names[0] == "node_id", (
                f"{owner_name}: node/row signature must start with node_id"
            )


def test_provenance_shape_count(library: str) -> None:
    doc = load_registry(library)
    assert doc["provenance"]["shape_count"] == len(doc["shapes"])


def test_coverage_file_covers_every_function(library: str) -> None:
    coverage = (
        NOTATION_DIR / library / f"{library}_shapes_coverage.mdg"
    ).read_text(encoding="utf-8")
    assert f"use {library}" in coverage
    doc = load_registry(library)
    root = doc["grammar"]["root"]
    assert f"{library}.{root}(" in coverage, f"root builder {root} missing"
    missing = sorted(
        s["function"] for s in doc["shapes"]
        if f"{library}.{s['function']}(" not in coverage
    )
    assert not missing, f"functions not exercised by coverage file: {missing}"


needs_palette_data = pytest.mark.skipif(
    not PALETTE_OUTPUT_DIR.exists(),
    reason="parsed palette data missing — run `make build-data`",
)

needs_sidecars = pytest.mark.skipif(
    not DATA_DIR.exists(),
    reason="generated notation sidecars missing — run `make build-data`",
)


@needs_palette_data
def test_fingerprints_match_palette(library: str) -> None:
    doc = load_registry(library)
    data = json.loads(
        (PALETTE_OUTPUT_DIR / LIBRARY_PALETTE_JSON[library]).read_text(
            encoding="utf-8"
        )
    )
    flat = flatten_entries(data, doc["provenance"]["pages"])
    assert len(flat) >= len(doc["shapes"]), "palette has fewer entries than registry"
    for shape in doc["shapes"]:
        cells = flat[shape["menu_index"] - 1]
        anchor: dict[str, Any] = anchor_cell(cells, shape["kind"])
        actual = style_fingerprint(anchor.get("style") or "")
        assert actual == shape["render"]["fingerprint"], (
            f"{shape['id']}: registry fingerprint {shape['render']['fingerprint']}"
            f" != palette {actual} — palette drifted or menu_index is wrong"
        )


@needs_sidecars
def test_styles_sidecar_is_fresh(library: str) -> None:
    sidecar_path = DATA_DIR / "notation" / f"{library}_styles.json"
    if not sidecar_path.exists():
        pytest.skip("sidecar not built — run scripts/build_notation_styles.py")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    doc = load_registry(library)
    assert set(sidecar) == {s["id"] for s in doc["shapes"]}, \
        "sidecar out of date with registry — rerun scripts/build_notation_styles.py"
    for shape in doc["shapes"]:
        assert sidecar[shape["id"]]["fingerprint"] == shape["render"]["fingerprint"]


@needs_sidecars
def test_row_type_sidecar_covers_every_non_shape_row_type(library: str) -> None:
    """Every row type without its own shape needs generated render metadata."""
    doc = load_registry(library)
    shape_functions = {shape["function"] for shape in doc["shapes"]}
    expected = {
        row_type["name"]
        for row_type in doc.get("row_types", [])
        if row_type["name"] not in shape_functions
    }
    if library == "erd":
        # RowKey also has a standalone shape, but its nested tableRow template
        # is structurally different and must be generated separately.
        expected.add("RowKey")
    sidecar_path = DATA_DIR / "notation" / f"{library}_row_types.json"
    if not expected and not sidecar_path.exists():
        return
    if not sidecar_path.exists():
        pytest.fail(
            f"missing row-type sidecar for {library}; run `make build-data`"
        )
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert expected == set(sidecar), (
        "row-type sidecar is stale — rerun `make build-data`: "
        f"missing={sorted(expected - set(sidecar))}, "
        f"unexpected={sorted(set(sidecar) - expected)}"
    )
