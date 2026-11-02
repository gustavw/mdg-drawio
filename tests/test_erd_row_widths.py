"""erd table rows: the key and text cells fit their content.

Regression for clipped rows: the layout widened a table for its longest row
label but ignored the key cell, and the generator kept the palette
template's fixed cell widths (30 + 150), so "PK,FK" and long row labels were
cut off.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mdg_drawio.engine.convert import convert

SOURCE = """---
title: "Row widths"
mode: layered
---
erd.Table(t1, "Konfigurationsparametrar"):
    erd.RowKey(t1_r1, "konfigurationsparameter_id", key="PK")
    erd.RowKey(t1_r2, "standardkonfiguration_id", key="FK")
    erd.Row(t1_r3, "Enhet")
erd.Table(t2, "Person"):
    erd.RowKey(t2_r1, "part_id", key="PK,FK")
    erd.Row(t2_r2, "Förnamn")
"""


def _geometries(tmp_path: Path) -> dict[str, dict[str, str]]:
    source = tmp_path / "rows.mdg"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "rows.drawio"
    assert convert(source, output, force=True) == 0
    root = ET.parse(output).getroot()
    geometries = {}
    for cell in root.iter("mxCell"):
        geometry = cell.find("mxGeometry")
        if geometry is not None:
            geometries[cell.get("id", "")] = geometry.attrib
    return geometries


def _width(geometry: dict[str, str]) -> float:
    return float(geometry["width"])


def test_text_cell_fills_the_widened_row(tmp_path: Path) -> None:
    geo = _geometries(tmp_path)
    row = _width(geo["t1_r1"])
    key = _width(geo["t1_r1__c0"])
    text = geo["t1_r1__c1"]
    assert float(text["x"]) == key
    # Geometry is written with two decimals.
    assert key + _width(text) == pytest.approx(row, abs=0.01)
    # The palette template's text cell is 150 wide; the long label needs more.
    assert _width(text) > 150


def test_key_cell_fits_a_composite_key(tmp_path: Path) -> None:
    geo = _geometries(tmp_path)
    assert _width(geo["t2_r1__c0"]) > 30


def test_rows_of_one_table_share_the_key_width(tmp_path: Path) -> None:
    geo = _geometries(tmp_path)
    assert _width(geo["t2_r1__c0"]) == _width(geo["t2_r2__c0"])
    assert float(geo["t2_r2__c1"]["x"]) == _width(geo["t2_r1__c0"])
