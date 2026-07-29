"""
Round-trip parity tests for drawio_codec.

Each .drawio file in the same directory is:
  1. Parsed to a dict
  2. Schema-validated
  3. Written to a temp file
  4. Re-parsed
  5. Compared dict-for-dict with the original parse result

Run with:  python -m pytest test_roundtrip.py -v
"""

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from drawio_codec import parse, write, validate


DRAWIO_DIR = Path(__file__).parent
PALETTES_DIR = DRAWIO_DIR.parent / "palette" / "output"

if not PALETTES_DIR.exists():
    pytest.skip(f"{PALETTES_DIR} not found — run 'make build-data' first", allow_module_level=True)

DRAWIO_FILES = sorted(PALETTES_DIR.rglob("*.drawio"))


def _file_id(p: Path) -> str:
    """Return a human-readable test ID like 'Standard/General'."""
    return str(p.relative_to(PALETTES_DIR).with_suffix(""))


def _normalize_cell(cell: dict) -> dict:
    """
    Return a copy of a cell dict with any floating-point values that are
    whole numbers converted to int, so 380.0 == 380 in comparisons.
    """
    out = {}
    for k, v in cell.items():
        if k == "geometry":
            out[k] = _normalize_geom(v)
        elif k == "object_attrs":
            out[k] = v
        elif isinstance(v, float) and v.is_integer():
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _normalize_geom(geom: dict) -> dict:
    out = {}
    for k, v in geom.items():
        if k in ("source_point", "target_point", "offset"):
            out[k] = {pk: (int(pv) if isinstance(pv, float) and pv.is_integer() else pv)
                      for pk, pv in v.items()}
        elif k == "waypoints":
            out[k] = [
                {pk: (int(pv) if isinstance(pv, float) and pv.is_integer() else pv)
                 for pk, pv in wp.items()}
                for wp in v
            ]
        elif k == "alternate_bounds":
            out[k] = {pk: (int(pv) if isinstance(pv, float) and pv.is_integer() else pv)
                      for pk, pv in v.items()}
        elif isinstance(v, float) and v.is_integer():
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _normalize(data: dict) -> dict:
    """Normalize a parsed drawio dict for comparison."""
    result = {k: v for k, v in data.items() if k not in ("diagrams",)}
    result["diagrams"] = []
    for diag in data.get("diagrams", []):
        norm_diag = {k: v for k, v in diag.items() if k not in ("graph_model", "cells")}
        # Normalize graph_model numbers
        norm_diag["graph_model"] = {
            k: (int(v) if isinstance(v, float) and v.is_integer() else v)
            for k, v in diag.get("graph_model", {}).items()
        }
        norm_diag["cells"] = [_normalize_cell(c) for c in diag.get("cells", [])]
        result["diagrams"].append(norm_diag)
    return result


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_parse_produces_valid_schema(drawio_path: Path):
    data = parse(drawio_path)
    errors = validate(data)
    assert errors == [], f"Schema validation failed for {drawio_path.name}:\n" + "\n".join(errors)


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_roundtrip_parity(drawio_path: Path):
    """Parse → write → re-parse must produce the same structure."""
    original = parse(drawio_path)

    with tempfile.NamedTemporaryFile(suffix=".drawio", mode="w", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        write(original, tmp_path)
        reconstructed = parse(tmp_path)
        # Patch source_file so it doesn't cause a mismatch on the name alone
        reconstructed["source_file"] = original["source_file"]

        orig_norm = _normalize(original)
        reco_norm = _normalize(reconstructed)

        assert orig_norm == reco_norm, _diff_report(orig_norm, reco_norm, drawio_path.name)
    finally:
        tmp_path.unlink(missing_ok=True)


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_cell_count_preserved(drawio_path: Path):
    """Every cell in the original must appear in the roundtripped output."""
    original = parse(drawio_path)

    with tempfile.NamedTemporaryFile(suffix=".drawio", mode="w", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        write(original, tmp_path)
        reconstructed = parse(tmp_path)

        for orig_diag, reco_diag in zip(
            original["diagrams"], reconstructed["diagrams"], strict=True
        ):
            orig_ids = {c["id"] for c in orig_diag["cells"]}
            reco_ids = {c["id"] for c in reco_diag["cells"]}
            assert orig_ids == reco_ids, (
                f"Cell ID mismatch in diagram '{orig_diag['name']}': "
                f"missing={orig_ids - reco_ids}, extra={reco_ids - orig_ids}"
            )
    finally:
        tmp_path.unlink(missing_ok=True)


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_geometry_preserved(drawio_path: Path):
    """All geometry values must survive the round-trip exactly."""
    original = parse(drawio_path)

    with tempfile.NamedTemporaryFile(suffix=".drawio", mode="w", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        write(original, tmp_path)
        reconstructed = parse(tmp_path)

        for orig_diag, reco_diag in zip(
            original["diagrams"], reconstructed["diagrams"], strict=True
        ):
            orig_cells = {c["id"]: c for c in orig_diag["cells"]}
            reco_cells = {c["id"]: c for c in reco_diag["cells"]}

            for cell_id, orig_cell in orig_cells.items():
                if "geometry" not in orig_cell:
                    continue
                assert cell_id in reco_cells, f"Cell {cell_id!r} missing after roundtrip"
                reco_cell = reco_cells[cell_id]
                assert "geometry" in reco_cell, f"Geometry missing for cell {cell_id!r}"

                orig_g = _normalize_geom(orig_cell["geometry"])
                reco_g = _normalize_geom(reco_cell["geometry"])
                assert orig_g == reco_g, (
                    f"Geometry mismatch for cell {cell_id!r}:\n"
                    f"  original:      {orig_g}\n"
                    f"  reconstructed: {reco_g}"
                )
    finally:
        tmp_path.unlink(missing_ok=True)


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_no_redundant_alternate_bounds(drawio_path: Path):
    """Parsed geometry must never contain alternate_bounds identical to regular bounds."""
    data = parse(drawio_path)
    for diag in data["diagrams"]:
        for cell in diag["cells"]:
            geom = cell.get("geometry", {})
            ab = geom.get("alternate_bounds")
            if ab is None:
                continue
            assert not (
                ab.get("width") == geom.get("width") and
                ab.get("height") == geom.get("height") and
                ab.get("x") == geom.get("x") and
                ab.get("y") == geom.get("y")
            ), (
                f"Cell {cell['id']!r} in {drawio_path.name}: "
                f"alternate_bounds {ab} duplicates geometry bounds"
            )


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_no_tablerow_y(drawio_path: Path):
    """tableRow cells must not carry a y coordinate — layout is managed by childLayout=tableLayout."""
    data = parse(drawio_path)
    for diag in data["diagrams"]:
        for cell in diag["cells"]:
            if "shape=tableRow" not in cell.get("style", ""):
                continue
            y = cell.get("geometry", {}).get("y")
            assert y is None, (
                f"Cell {cell['id']!r} in {drawio_path.name}: "
                f"tableRow cell has y={y} (should be absent)"
            )


@pytest.mark.parametrize("drawio_path", DRAWIO_FILES, ids=[_file_id(f) for f in DRAWIO_FILES])
def test_no_cells_dropped(drawio_path: Path):
    """Every <mxCell> and <object> in the raw XML must appear in the parsed output."""
    xml_root = ET.parse(drawio_path).getroot()
    data = parse(drawio_path)

    xml_diagrams = [el for el in xml_root if el.tag == "diagram"]
    assert len(xml_diagrams) == len(data["diagrams"]), (
        f"{drawio_path.name}: XML has {len(xml_diagrams)} diagrams, "
        f"codec produced {len(data['diagrams'])}"
    )

    for xml_diag, codec_diag in zip(xml_diagrams, data["diagrams"], strict=True):
        xml_count = sum(
            1 for r in xml_diag.iter("root")
            for child in r
            if child.tag in ("mxCell", "object")
        )
        codec_count = len(codec_diag["cells"])
        assert xml_count == codec_count, (
            f"{drawio_path.name} / {codec_diag['name']!r}: "
            f"raw XML has {xml_count} cell elements, codec produced {codec_count}"
        )


# ---------------------------------------------------------------------------
# Combined full-parity round-trip
# ---------------------------------------------------------------------------

def test_all_shapes_full_parity():
    """
    Collect every real cell from every palette file, merge them into a single
    combined drawio file with globally unique ids, round-trip through write/parse,
    and verify every attribute of every cell survives unchanged.

    This is stronger than the per-file roundtrip tests because it proves parity
    holds when all shapes co-exist in one file, catching any codec behaviour that
    is scale- or context-dependent.
    """
    # Pass 1 — build a global id remap so cells from different files never clash.
    # Key: (file_idx, diag_idx, original_id)  →  new unique id string.
    id_remap: dict[tuple[int, int, str], str] = {}
    for file_idx, drawio_path in enumerate(DRAWIO_FILES):
        data = parse(drawio_path)
        for diag_idx, diag in enumerate(data["diagrams"]):
            for cell in diag["cells"]:
                key = (file_idx, diag_idx, cell["id"])
                id_remap[key] = f"{file_idx}_{diag_idx}_{cell['id']}"

    def _remap(file_idx: int, diag_idx: int, orig: str) -> str:
        return id_remap.get((file_idx, diag_idx, orig), "1")

    # Pass 2 — rebuild every non-structural cell with remapped ids/refs.
    # Store the remapped version as the expected value for comparison later.
    expected: dict[str, dict] = {}   # new_id → expected cell dict
    cells_to_write: list[dict] = [{"id": "0"}, {"id": "1", "parent": "0"}]

    for file_idx, drawio_path in enumerate(DRAWIO_FILES):
        data = parse(drawio_path)
        for diag_idx, diag in enumerate(data["diagrams"]):
            for cell in diag["cells"]:
                if cell["id"] in ("0", "1"):
                    continue   # skip per-diagram structural roots

                new_id = _remap(file_idx, diag_idx, cell["id"])
                new_cell: dict = {**cell, "id": new_id}

                orig_parent = cell.get("parent", "1")
                if orig_parent not in ("0", "1"):
                    new_cell["parent"] = _remap(file_idx, diag_idx, orig_parent)
                else:
                    new_cell["parent"] = "1"

                for ref in ("source", "target"):
                    if ref in cell:
                        new_cell[ref] = _remap(file_idx, diag_idx, cell[ref])

                cells_to_write.append(new_cell)
                expected[new_id] = new_cell

    assert expected, "No cells collected — run 'make build-data' first"

    combined = {
        "source_file": "combined.drawio",
        "host": "app.diagrams.net",
        "diagrams": [{
            "id": "combined-1",
            "name": "All Shapes",
            "graph_model": {"dx": 1200, "dy": 900},
            "cells": cells_to_write,
        }],
    }

    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        write(combined, tmp_path)
        reconstructed = parse(tmp_path)

        reco_by_id = {
            c["id"]: c
            for diag in reconstructed["diagrams"]
            for c in diag["cells"]
        }

        failures: list[str] = []
        for new_id, exp_cell in expected.items():
            if new_id not in reco_by_id:
                failures.append(f"cell {new_id!r} missing after roundtrip")
                continue

            exp_norm = _normalize_cell(exp_cell)
            reco_norm = _normalize_cell(reco_by_id[new_id])

            if exp_norm != reco_norm:
                failures.append(
                    f"cell {new_id!r} attribute mismatch:\n"
                    f"  expected:      {exp_norm}\n"
                    f"  reconstructed: {reco_norm}"
                )

        assert not failures, (
            f"{len(failures)} of {len(expected)} cell(s) failed full parity "
            f"in combined roundtrip:\n" + "\n".join(failures[:5])
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _diff_report(a: dict, b: dict, label: str) -> str:
    """Produce a compact diff of two dicts for assertion messages."""
    lines = [f"Round-trip mismatch in {label}:"]
    for diag_a, diag_b in zip(a.get("diagrams", []), b.get("diagrams", [])):
        cells_a = {c["id"]: c for c in diag_a.get("cells", [])}
        cells_b = {c["id"]: c for c in diag_b.get("cells", [])}
        for cid in sorted(cells_a):
            ca, cb = cells_a.get(cid), cells_b.get(cid)
            if ca != cb:
                lines.append(f"  Cell {cid!r}:")
                lines.append(f"    original:      {ca}")
                lines.append(f"    reconstructed: {cb}")
    return "\n".join(lines) if len(lines) > 1 else f"Dicts differ in {label} (no cell-level detail available)"
