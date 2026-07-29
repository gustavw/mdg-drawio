#!/usr/bin/env python3
"""One-shot migration of the shape registries to schema v2.

Transformations (see REVIEW_TASKS.md / FIX_PLAN.md):
  B7  general: function names -> UpperCamelCase, old names kept in aliases
  B3  examples: one call style everywhere: <lib>.<Function>(...)
  B6  version = notation version; provenance = {source_file, palette,
      shape_count, pages}
  A3  grammar block in every registry, pointing at notation/GRAMMAR.md
  A6  row_types section for every library that uses rows
  A2  contains block on container shapes
  A5  c4 metamodel blocks (spec: c4)
  B4  c4 DiagramTitle args/examples reconciled; boundary examples get children
  A1  render.fingerprint per shape; c4 menu_index re-derived from the palette
  --  kind fixes: entries that are pure edges in the palette but were recorded
      as vertices become proper edges
  B2  archimate3 'reviewed' entries that still carry TODO markers -> 'drafted'

Requires tools/styles/output/ (run `make build-data` first). Idempotent: running
it on already-migrated registries is a no-op apart from file rewrite.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent

NOTATION = ROOT / "mdg_drawio" / "notation"
DATA = ROOT / "tools" / "styles" / "output"

LIBS: dict[str, dict[str, str]] = {
    "archimate3": {"data": "Business/ArchiMate_3.2.json", "palette": "draw.io ArchiMate 3.2", "version": "3.2", "root": "ArchiMate3"},
    "bpmn2": {"data": "Business/BPMN_2.0.json", "palette": "draw.io BPMN 2.0", "version": "2.0", "root": "BPMN"},
    "c4": {"data": "Software/C4.json", "palette": "draw.io C4", "version": "1", "root": "C4"},
    "erd": {"data": "Software/Entity_Relation.json", "palette": "draw.io Entity Relation", "version": "1", "root": "ERD"},
    "general": {"data": "Standard/General.json", "palette": "draw.io General", "version": "1", "root": "General"},
    "uml": {"data": "Software/UML.json", "palette": "draw.io UML (classic)", "version": "classic", "root": "UML"},
    "uml25": {"data": "Software/UML_2.5.json", "palette": "draw.io UML 2.5", "version": "2.5", "root": "UML25"},
}

# Palette page order in the MENU (data files may store pages in another order).
# archimate3 order was recovered by label-matching registry entries to pages.
PAGE_ORDER: dict[str, list[str]] = {
    "archimate3": ["Generic", "Relationships", "Motivation", "Strategy",
                   "Business", "Application", "Technology",
                   "Implementation and Migration"],
    "general": ["general"],  # registry covers only the first palette page
}

# c4: registry menu_index was authored in coverage-file order, not palette
# order. old menu_index -> actual palette slot.
C4_REMAP = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, 10: 10,
            11: 14, 12: 15, 13: 16, 14: 17, 15: 18, 16: 19,
            17: 11, 18: 12, 19: 13}

GRAMMAR_NOTES: dict[str, list[str]] = {
    "archimate3": ["Grouping and Location are containers (contains: '*')."],
    "bpmn2": [
        "Pools and the cross-functional table use TableRowBoxPart/SwimlaneBoxPart rows.",
        "Lanes, Group and *Expanded sub-processes contain child nodes (contains: '*').",
    ],
    "c4": [
        "System_Boundary and Container_Boundary contain child nodes (contains: '*').",
        "DiagramTitle and Legend shapes are annotations, not model elements.",
    ],
    "erd": ["Anchor rows may nest child Anchor rows (see the coverage file)."],
    "general": [
        "Function names are UpperCamelCase since v2; pre-v2 lowercase names are kept in aliases.",
        "Container, HorizontalContainer and VerticalContainer contain child nodes.",
        "Only the first palette page ('general') is registered; 'misc' and 'advanced' are not.",
    ],
    "uml": ["Compartment children use Item/Divider/CompositeLabel rows."],
    "uml25": [],
}

def ARG(name: str, required: bool) -> dict[str, Any]:
    return {"name": name, "required": required}

ROW_TYPES: dict[str, list[dict[str, Any]]] = {
    "uml25": [
        {"name": "Item", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Left-aligned member/content row (the default)."},
        {"name": "Header", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Center-aligned compartment label (e.g. 'attributes', 'operations')."},
        {"name": "Note", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Bold stub row, typically an internal-structure placeholder."},
        {"name": "Divider", "args": [ARG("node_id", True), ARG("text", False), ARG("dashed", False)],
         "summary": "Separator line; dashed=True for a dashed one."},
        {"name": "Lane", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "partialRectangle swimlane cell (Activity Partition, Lifeline timing lanes)."},
    ],
    "uml": [
        {"name": "Item", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Compartment member row (field, method)."},
        {"name": "Divider", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Separator line between compartments."},
        {"name": "CompositeLabel", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Label row of a composite shape."},
    ],
    "erd": [
        {"name": "Row", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Plain table row."},
        {"name": "RowKey", "args": [ARG("node_id", True), ARG("text", False), ARG("key", False)],
         "summary": "Key row; key= sets the key tag (e.g. 'PK', 'FK1')."},
        {"name": "Item", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "List item row."},
        {"name": "EntityText", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Free-text block row inside an entity."},
        {"name": "Anchor", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Anchor row; may nest child Anchor rows."},
    ],
    "bpmn2": [
        {"name": "TableRowBoxPart", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Cross-functional table row (actor band); nests SwimlaneBoxPart cells."},
        {"name": "SwimlaneBoxPart", "args": [ARG("node_id", True), ARG("text", False)],
         "summary": "Swimlane cell within a pool or table row."},
    ],
}

# function (per library) -> contains.allowed
CONTAINS: dict[str, Any] = {
    "c4": {"System_Boundary": ["*"], "Container_Boundary": ["*"]},
    "archimate3": {"Grouping": ["*"], "Location": ["*"]},
    "uml25": {"Diagram": ["*"]},
    "general": {"Container": ["*"], "HorizontalContainer": ["*"], "VerticalContainer": ["*"]},
    "bpmn2": "auto",  # Group, *Expanded, lanes/swimlanes (not the *BoxPart row types)
}


def bpmn2_is_container(function: str) -> bool:
    if function in ("Group", "HorizontalLane", "VerticalLane",
                    "HorizontalSwimlane", "VerticalSwimlane"):
        return True
    return function.endswith("Expanded")


# c4 metamodel: (function, variant) -> (tag, element, classification, endpoints)
C4_METAMODEL: dict[tuple[str, int], tuple[str, str, dict[str, str] | None, bool]] = {
    ("Person", 1): ("metaclass", "Person", {"external": "false"}, False),
    ("Person_Ext", 1): ("notation", "Person", {"external": "true"}, False),
    ("System", 1): ("metaclass", "SoftwareSystem", {"external": "false"}, False),
    ("System_Ext", 1): ("notation", "SoftwareSystem", {"external": "true"}, False),
    ("Container", 1): ("metaclass", "Container", {"external": "false"}, False),
    ("ContainerDb", 1): ("notation", "Container", {"technology_hint": "database"}, False),
    ("ContainerMicroservice", 1): ("notation", "Container", {"technology_hint": "microservice"}, False),
    ("ContainerQueue", 1): ("notation", "Container", {"technology_hint": "message_broker"}, False),
    ("ContainerWebBrowser", 1): ("notation", "Container", {"technology_hint": "web_browser"}, False),
    ("Component", 1): ("metaclass", "Component", None, False),
    ("System_Boundary", 1): ("metaclass", "SystemBoundary", None, False),
    ("Container_Boundary", 1): ("metaclass", "ContainerBoundary", None, False),
    ("Rel", 1): ("metaclass", "Relationship", None, True),
    ("Rel", 2): ("notation", "Relationship", None, True),
    ("Rel", 3): ("notation", "Relationship", None, True),
}

C4_EXAMPLES: dict[tuple[str, int], str] = {
    ("Context_DiagramTitle", 1): 'c4.Context_DiagramTitle(t1, "[System Context] Diagram title", "Diagram short description")\n',
    ("Container_DiagramTitle", 1): 'c4.Container_DiagramTitle(t1, "[Containers] Diagram title", "Diagram short description")\n',
    ("Component_DiagramTitle", 1): 'c4.Component_DiagramTitle(t1, "[Components] Diagram title", "Diagram short description")\n',
    ("Legend", 1): 'c4.Legend(l1, "Legend")\n',
    ("System_Boundary", 1): 'c4.System_Boundary(b1, "System name"):\n    c4.Container(c1, "Container name", "Description of container.")\n',
    ("Container_Boundary", 1): 'c4.Container_Boundary(b1, "Container name"):\n    c4.Component(c1, "Component name", "Description of component.")\n',
}

SHAPE_KEY_ORDER = ["id", "menu_index", "kind", "buildable", "function",
                   "variant", "menu_name", "render", "tag", "status",
                   "summary", "discriminator", "use_when", "avoid_when",
                   "aliases", "args", "rows", "contains", "endpoints",
                   "related", "example", "metamodel"]
ROOT_KEY_ORDER = ["library", "version", "provenance", "grammar", "row_types",
                  "shapes"]


# ── YAML output ──────────────────────────────────────────────────────────────

class RegistryDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: RegistryDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


RegistryDumper.add_representer(str, _str_presenter)


def ordered(d: dict[str, Any], key_order: list[str]) -> dict[str, Any]:
    out = {k: d[k] for k in key_order if k in d}
    out.update({k: v for k, v in d.items() if k not in out})
    return out


def dump_registry(doc: dict[str, Any], path: Path, header: str) -> None:
    doc = ordered(doc, ROOT_KEY_ORDER)
    doc["shapes"] = [ordered(s, SHAPE_KEY_ORDER) for s in doc["shapes"]]
    body = yaml.dump(doc, Dumper=RegistryDumper, sort_keys=False,
                     allow_unicode=True, width=100, default_flow_style=False)
    path.write_text(header + body, encoding="utf-8")


# ── transformations ──────────────────────────────────────────────────────────

def rewrite_example(lib: str, example: str) -> str:
    """B3: normalize call style to <lib>.<Function>(...). Idempotent: only
    lines that still carry the old call styles are touched."""
    lines = []
    for line in example.splitlines():
        # bpmn2/c4 template artifact: 'n1.' used as namespace and 'c1' as the
        # root node id — swap both only on lines that have the artifact
        line = re.sub(r"^(\s*)n1\.([A-Za-z_]\w*)\(c1\b", rf"\1{lib}.\2(n1", line)
        line = re.sub(r"^(\s*)n1\.([A-Za-z_]\w*)\(", rf"\1{lib}.\2(", line)
        # uml: bare calls with no namespace
        line = re.sub(r"^(\s*)([A-Z]\w*)\(", rf"\1{lib}.\2(", line)
        lines.append(line)
    out = "\n".join(lines)
    if example.endswith("\n"):
        out += "\n"
    return out


def first_label(example: str) -> str:
    m = re.search(r'"([^"]*)"', example)
    return m.group(1) if m else ""


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

    from mdg_drawio.notation._core.normalize import style_fingerprint
    from mdg_drawio.notation._core.palette import (
        anchor_cell,
        flatten_entries,
        top_level,
    )

    def convert_to_edge(
        lib: str, shape: dict[str, Any], cells: list[dict[str, Any]]
    ) -> None:
        """Fix registry kind bugs: palette entry is a pure edge but the registry
        recorded a vertex."""
        label = first_label(shape.get("example", ""))
        anchor = anchor_cell(cells, "edge")
        style = anchor.get("style") or ""
        direction = "none" if ("endArrow=none" in style and "startArrow" not in style) \
            else "source_to_target"
        shape["kind"] = "edge"
        shape.pop("rows", None)
        shape.pop("contains", None)
        shape["endpoints"] = {"direction": direction}
        shape["args"] = [ARG("source", True), ARG("target", True), ARG("label", False)]
        call = f'{lib}.{shape["function"]}(None, None, "{label}"'
        if shape.get("variant", 1) > 1:
            call += f', variant={shape["variant"]}'
        shape["example"] = call + ")\n"

    def migrate_library(lib: str) -> list[str]:
        cfg = LIBS[lib]
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
        data = json.loads((DATA / cfg["data"]).read_text(encoding="utf-8"))
        report: list[str] = []

        pages = PAGE_ORDER.get(lib, [dg["name"] for dg in data["diagrams"]])
        flat = flatten_entries(data, pages)

        # B6: version + provenance
        doc["version"] = cfg["version"]
        prov = doc.get("provenance") or {}
        new_prov: dict[str, Any] = {
            "source_file": data.get("source_file", f"{lib}.drawio"),
            "palette": cfg["palette"],
            "shape_count": len(doc["shapes"]),
            "pages": pages,
        }
        if "metamodel_source" in prov:
            new_prov["metamodel_source"] = prov["metamodel_source"]
        doc["provenance"] = new_prov

        # A3: grammar block
        grammar: dict[str, Any] = {"spec": "../GRAMMAR.md", "root": cfg["root"]}
        notes = GRAMMAR_NOTES.get(lib) or []
        if notes:
            grammar["notes"] = notes
        if lib == "uml25" and isinstance(doc.get("grammar"), dict):
            existing = {k: v for k, v in doc["grammar"].items() if k not in grammar}
            grammar.update(existing)
        doc["grammar"] = grammar

        # A6: row_types
        if lib in ROW_TYPES:
            doc["row_types"] = ROW_TYPES[lib]
        else:
            doc.pop("row_types", None)

        # c4 remap: menu_index -> actual palette slot, then re-sort. Only applies
        # to the pre-v2 ordering (Rel at 17-19); already-migrated files are left
        # alone so the migration stays idempotent.
        if lib == "c4":
            rel_indexes = sorted(
                s["menu_index"] for s in doc["shapes"] if s["function"] == "Rel"
            )
            if rel_indexes == [17, 18, 19]:
                for s in doc["shapes"]:
                    s["menu_index"] = C4_REMAP[s["menu_index"]]
                doc["shapes"].sort(key=lambda s: s["menu_index"])
                report.append("menu_index re-derived from palette (Rel now 11-13)")

        doc["shapes"].sort(key=lambda s: s["menu_index"])

        n_edgefix = n_drafted = n_renamed = 0
        for s in doc["shapes"]:
            cells = flat[s["menu_index"] - 1]

            # kind fix: pure-edge palette entry recorded as vertex
            top = top_level(cells)
            if s["kind"] != "edge" and top and all(c.get("edge") for c in top):
                convert_to_edge(lib, s, cells)
                n_edgefix += 1

            # B7: general renames
            if lib == "general" and not s["function"][0].isupper():
                old = s["function"]
                new = old[0].upper() + old[1:]
                s["function"] = new
                aliases = s.get("aliases") or []
                if old not in aliases:
                    aliases.append(old)
                s["aliases"] = aliases
                s["example"] = s["example"].replace(f"general.{old}(", f"general.{new}(")
                n_renamed += 1

            # B3: example call style
            s["example"] = rewrite_example(lib, s["example"])

            # B2: honest status for archimate3 'reviewed' entries with TODOs
            if lib == "archimate3" and s["status"] == "reviewed" \
                    and "TODO" in str(s.get("discriminator", "")):
                s["status"] = "drafted"
                n_drafted += 1

            # A2: contains
            rule = CONTAINS.get(lib)
            allowed = None
            if rule == "auto":
                if bpmn2_is_container(s["function"]):
                    allowed = ["*"]
            elif isinstance(rule, dict):
                allowed = rule.get(s["function"])
            if allowed is not None and s["kind"] == "vertex":
                if not (s.get("rows") or {}).get("allowed"):
                    s["contains"] = {"allowed": allowed}
                    s["rows"] = {"allowed": []}

            # A5 + B4: c4 metamodel, args, examples
            if lib == "c4":
                key = (s["function"], s["variant"])
                if key in C4_METAMODEL:
                    tag, element, classification, is_rel = C4_METAMODEL[key]
                    s["tag"] = tag
                    mm: dict[str, Any] = {"spec": "c4", "element": element}
                    if is_rel:
                        mm["endpoints"] = {"source": "Element", "target": "Element"}
                    if classification:
                        mm["classification"] = classification
                    s["metamodel"] = mm
                if "DiagramTitle" in s["function"]:
                    s["args"] = [ARG("node_id", True), ARG("label", False),
                                 ARG("description", False)]
                if key in C4_EXAMPLES:
                    s["example"] = C4_EXAMPLES[key]

            # A1: fingerprint
            anchor = anchor_cell(cells, s["kind"])
            s["render"] = {"fingerprint": style_fingerprint(anchor.get("style") or "")}

        if n_edgefix:
            report.append(f"{n_edgefix} vertex->edge kind fixes")
        if n_drafted:
            report.append(f"{n_drafted} reviewed-with-TODO -> drafted")
        if n_renamed:
            report.append(f"{n_renamed} functions renamed to UpperCamelCase")

        dump_registry(doc, reg_path, header)
        return report

    def main() -> None:
        if not DATA.exists():
            sys.exit("tools/styles/output/ missing — run `make build-data` first")
        for lib in LIBS:
            report = migrate_library(lib)
            print(f"{lib}: migrated" + (f" ({'; '.join(report)})" if report else ""))

        # validate everything against the v2 schema
        from jsonschema import Draft202012Validator
        schema = json.loads(
            (NOTATION / "shape-registry.schema.json").read_text()
        )
        validator = Draft202012Validator(schema)
        failures = 0
        for lib in LIBS:
            doc = yaml.safe_load(
                (NOTATION / lib / f"{lib}_registry.yaml").read_text()
            )
            errors = list(validator.iter_errors(doc))
            for e in errors[:10]:
                pe = "/".join(str(p) for p in e.absolute_path)
                print(f"  SCHEMA ERROR [{lib}] {pe}: {e.message[:140]}")
            failures += len(errors)
        if failures:
            sys.exit(f"{failures} schema errors after migration")
        print("all registries valid against schema v2")

    main()
