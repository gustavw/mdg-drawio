#!/usr/bin/env python3
"""One-shot migration of the shape registries to schema v3.

Adds the ``passing`` field (see todo/notation-coverage-parser.md Phase 3)
to every declared arg across every library's ``shapes[].args`` and
``row_types[].args``: ``positional`` for the handful of structural names
every DSL call binds by position (``node_id``, ``label``, ``text``,
``source``, ``target``, ``description``), ``keyword_only`` for everything
else. C4 relationship ``description`` is the one name-specific exception: its
native builder and every example treat it as keyword-only. The split was
verified empirically: every non-structural arg name is used exclusively via
``name=value`` syntax across every coverage sheet and
``docs/architecture/*.mdg`` (see the migration's own commit message for the
survey), never positionally -- ``passing`` codifies what real documents
already do, it does not change any of them.

Idempotent: running it on already-migrated registries is a no-op apart from
file rewrite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent
NOTATION = ROOT / "mdg_drawio" / "notation"

LIBRARIES = ("archimate3", "bpmn2", "c4", "erd", "general", "uml", "uml25")

# Mirrors scripts/migrate_registry_v2.py's dumper: kept self-contained here
# rather than cross-imported, since scripts/ isn't a real package (each
# one-shot migration script is a standalone unit).
SHAPE_KEY_ORDER = ["id", "menu_index", "kind", "buildable", "function",
                   "variant", "menu_name", "render", "tag", "status",
                   "summary", "discriminator", "use_when", "avoid_when",
                   "aliases", "args", "rows", "contains", "containment",
                   "endpoints", "related", "example", "metamodel"]
ROOT_KEY_ORDER = ["library", "version", "provenance", "grammar", "row_types",
                  "shapes"]


class _RegistryDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: _RegistryDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_RegistryDumper.add_representer(str, _str_presenter)


def _ordered(d: dict[str, Any], key_order: list[str]) -> dict[str, Any]:
    out = {k: d[k] for k in key_order if k in d}
    out.update({k: v for k, v in d.items() if k not in out})
    return out


def dump_registry(doc: dict[str, Any], path: Path, header: str) -> None:
    doc = _ordered(doc, ROOT_KEY_ORDER)
    doc["shapes"] = [_ordered(s, SHAPE_KEY_ORDER) for s in doc["shapes"]]
    body = yaml.dump(doc, Dumper=_RegistryDumper, sort_keys=False,
                      allow_unicode=True, width=100, default_flow_style=False)
    path.write_text(header + body, encoding="utf-8")

# The only argument names any DSL call binds by position, across every
# library -- confirmed empirically (see module docstring). Every other
# declared arg name is keyword_only.
POSITIONAL_NAMES = frozenset(
    {"node_id", "label", "text", "source", "target", "description"}
)


def _passing(library: str, function: str, name: str) -> str:
    if library == "c4" and function == "Rel" and name == "description":
        return "keyword_only"
    return "positional" if name in POSITIONAL_NAMES else "keyword_only"


def _add_passing(
    args: list[dict[str, Any]] | None, library: str, function: str
) -> None:
    for arg in args or []:
        arg["passing"] = _passing(library, function, arg["name"])


def migrate_library(lib: str) -> int:
    reg_path = NOTATION / lib / f"{lib}_registry.yaml"
    original = reg_path.read_text(encoding="utf-8")
    header_lines = []
    for line in original.splitlines():
        if line.startswith("#") or not line.strip():
            header_lines.append(line)
        else:
            break
    header = ("\n".join(header_lines) + "\n") if header_lines else ""

    doc = yaml.safe_load(original)
    changed = 0
    for shape in doc["shapes"]:
        before = [dict(a) for a in shape.get("args") or []]
        _add_passing(shape.get("args"), lib, shape["function"])
        if shape.get("args") != before:
            changed += 1
    for row_type in doc.get("row_types") or []:
        before = [dict(a) for a in row_type.get("args") or []]
        _add_passing(row_type.get("args"), lib, row_type["name"])
        if row_type.get("args") != before:
            changed += 1

    dump_registry(doc, reg_path, header)
    return changed


def main() -> None:
    total = 0
    for lib in LIBRARIES:
        changed = migrate_library(lib)
        total += changed
        print(f"{lib}: {changed} arg lists updated")

    from jsonschema import Draft202012Validator

    schema = json.loads((NOTATION / "shape-registry.schema.json").read_text())
    validator = Draft202012Validator(schema)
    failures = 0
    for lib in LIBRARIES:
        doc = yaml.safe_load((NOTATION / lib / f"{lib}_registry.yaml").read_text())
        errors = list(validator.iter_errors(doc))
        for e in errors[:10]:
            pe = "/".join(str(p) for p in e.absolute_path)
            print(f"  SCHEMA ERROR [{lib}] {pe}: {e.message[:140]}")
        failures += len(errors)
    if failures:
        sys.exit(f"{failures} schema errors after migration")
    print(f"all registries valid against schema v3 ({total} arg lists touched)")


if __name__ == "__main__":
    main()
