"""erd tables: one line under the key block, not under every key row.

draw.io's own ER "Table 2" (PK,FK1 / PK,FK2 / Row 3 / Row 4) draws the
separator only under the last key row: the first key row has ``bottom=0``,
the second ``bottom=1``, the plain rows ``bottom=0``. The table has
``rowLines=0``, so the line marks the end of the keys, it is not a row line.
The generator used the single-key "Table 1" row template (``bottom=1``) for
every RowKey, which drew a line between key rows.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from mdg_drawio.engine.convert import convert

SOURCE = """---
title: "Key block"
mode: layered
---
erd.Table(t1, "Kundavtal", variant=2):
    erd.RowKey(t1_r1, "kundavtal_id", key="PK")
    erd.RowKey(t1_r2, "part_id", key="FK1")
    erd.RowKey(t1_r3, "kundpost_id", key="FK2")
    erd.Row(t1_r4, "Avtalsnummer")
    erd.Row(t1_r5, "Status")
erd.Table(t2, "Person"):
    erd.RowKey(t2_r1, "part_id", key="PK,FK1")
    erd.Row(t2_r2, "Förnamn")
erd.Table(t3, "Koppling"):
    erd.RowKey(t3_r1, "a_id", key="PK,FK1")
    erd.RowKey(t3_r2, "b_id", key="PK,FK2")
"""


def _bottoms(tmp_path: Path) -> dict[str, str]:
    source = tmp_path / "keys.mdg"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "keys.drawio"
    assert convert(source, output, force=True) == 0
    bottoms = {}
    for cell in ET.parse(output).getroot().iter("mxCell"):
        style = cell.get("style") or ""
        if "shape=tableRow" in style:
            tokens = dict(t.split("=", 1) for t in style.split(";") if "=" in t)
            bottoms[cell.get("id", "")] = tokens.get("bottom", "")
    return bottoms


def test_only_the_last_key_row_has_the_separator(tmp_path: Path) -> None:
    bottoms = _bottoms(tmp_path)
    assert [bottoms[f"t1_r{i}"] for i in range(1, 6)] == ["0", "0", "1", "0", "0"]


def test_a_single_key_row_keeps_its_separator(tmp_path: Path) -> None:
    bottoms = _bottoms(tmp_path)
    assert bottoms["t2_r1"] == "1"
    assert bottoms["t2_r2"] == "0"


def test_a_table_of_only_keys_ends_with_the_separator(tmp_path: Path) -> None:
    bottoms = _bottoms(tmp_path)
    assert (bottoms["t3_r1"], bottoms["t3_r2"]) == ("0", "1")
