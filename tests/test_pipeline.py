"""End-to-end pipeline tests — verify the full .mdg → .drawio conversion.

These tests exercise the Harness guardrails: pre-write XML validation,
diagram ID uniqueness, edge endpoint integrity, and page count parity.
Run with:

    make test
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mdg_drawio import main
from mdg_drawio.contracts import (
    C4_SCALER_MAX_WIDTH,
    C4_SCALER_PERSON_ASPECT_RATIO,
)
from mdg_drawio.engine.validate import _iter_edge_cells
from mdg_drawio.notation import DATA_DIR

# Tests that render palette-driven styling need the generated style sidecars
# (built by `make build-data`); skip them gracefully when absent, as
# tests/test_registries.py does — the data is copyright-derived and not committed.
needs_sidecars = pytest.mark.skipif(
    not DATA_DIR.exists(), reason="needs generated style data (run `make build-data`)"
)

_ARCH_MDG = (
    Path(__file__).parent.parent
    / "docs" / "architecture" / "c4_architecture.mdg"
)
_CODE_ARCH_MDG = (
    Path(__file__).parent.parent
    / "docs" / "architecture" / "code_architecture.mdg"
)


def _run_convert(input_path: Path, output_path: Path) -> int:
    """Run the CLI convert command and return the exit code."""
    return main([str(input_path), str(output_path), "--force"])


def test_convert_produces_valid_xml() -> None:
    """The output .drawio file must be valid, parseable XML."""
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        exit_code = _run_convert(_ARCH_MDG, output_path)
        assert exit_code == 0, f"conversion failed with exit code {exit_code}"
        assert output_path.exists(), "output file not created"

        # Must parse without error
        tree = ET.parse(str(output_path))
        root = tree.getroot()
        assert root.tag == "mxfile", f"root tag is {root.tag!r}, expected 'mxfile'"

    finally:
        output_path.unlink(missing_ok=True)


def test_diagram_ids_are_unique() -> None:
    """Every <diagram> must have a unique id attribute."""
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        assert _run_convert(_ARCH_MDG, output_path) == 0
        root = ET.parse(str(output_path)).getroot()
        diagrams = root.findall("diagram")
        assert len(diagrams) >= 1, "no diagrams in output"

        ids = [d.get("id") or "" for d in diagrams]
        assert all(ids), f"diagram(s) missing id: {ids}"
        assert len(ids) == len(set(ids)), f"duplicate diagram ids: {ids}"

    finally:
        output_path.unlink(missing_ok=True)


def test_page_count_matches_source() -> None:
    """Number of <diagram> elements must match source page count."""
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        assert _run_convert(_ARCH_MDG, output_path) == 0
        root = ET.parse(str(output_path)).getroot()
        diagrams = root.findall("diagram")

        # c4_architecture.mdg has 3 C4 pages: Context, Container, Component
        assert len(diagrams) == 3, f"expected 3 diagrams, got {len(diagrams)}"

    finally:
        output_path.unlink(missing_ok=True)


def test_code_architecture_page_count_matches_source() -> None:
    """The code architecture source is generated as its own one-page file."""
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        assert _run_convert(_CODE_ARCH_MDG, output_path) == 0
        root = ET.parse(str(output_path)).getroot()
        diagrams = root.findall("diagram")

        assert len(diagrams) == 1, f"expected 1 diagram, got {len(diagrams)}"
        assert diagrams[0].get("name") == "Code"

    finally:
        output_path.unlink(missing_ok=True)


def test_root_cells_are_present() -> None:
    """Every <mxGraphModel>.root must contain mxCell id='0' and id='1'."""
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        assert _run_convert(_ARCH_MDG, output_path) == 0
        root = ET.parse(str(output_path)).getroot()

        for d in root.findall("diagram"):
            model = d.find("mxGraphModel")
            assert model is not None, f"diagram {d.get('id')!r}: no mxGraphModel"
            cells_root = model.find("root")
            assert cells_root is not None, f"diagram {d.get('id')!r}: no root"
            cell_ids = {
                c.get("id") for c in cells_root.findall("mxCell")
            }
            assert "0" in cell_ids, f"diagram {d.get('id')!r}: missing mxCell '0'"
            assert "1" in cell_ids, f"diagram {d.get('id')!r}: missing mxCell '1'"

    finally:
        output_path.unlink(missing_ok=True)


def test_edge_endpoints_resolve_to_vertices() -> None:
    """Every edge's source/target must reference existing vertex cells."""
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        assert _run_convert(_ARCH_MDG, output_path) == 0
        root = ET.parse(str(output_path)).getroot()

        for d in root.findall("diagram"):
            model = d.find("mxGraphModel")
            if model is None:
                continue
            cells_root = model.find("root")
            if cells_root is None:
                continue
            cell_ids = {
                c.get("id") for c in cells_root.findall("mxCell")
            }
            for wrapper_tag in ("object", "UserObject"):
                for wrapper in cells_root.findall(wrapper_tag):
                    wid = wrapper.get("id")
                    if wid:
                        cell_ids.add(wid)
                    inner = wrapper.find("mxCell")
                    if inner is not None:
                        iid = inner.get("id")
                        if iid:
                            cell_ids.add(iid)
            # Edges must be collected through the same wrapper-aware helper
            # the real validation uses: C4 Rel edges live inside a
            # <UserObject>, so a plain findall("mxCell") (direct children
            # only) would silently check nothing at all here.
            edge_cells = _iter_edge_cells(cells_root)
            assert edge_cells, (
                f"diagram {d.get('id')!r}: no edge cells found — the scan "
                f"stopped matching the generator's output"
            )
            for cell in edge_cells:
                for attr in ("source", "target"):
                    ref = cell.get(attr, "")
                    if ref:
                        assert ref in cell_ids, (
                            f"diagram {d.get('id')!r}: "
                            f"edge {cell.get('id')!r} {attr}={ref!r} "
                            f"references unknown cell"
                        )

    finally:
        output_path.unlink(missing_ok=True)


def test_c4_relationships_render_with_the_palette_label_template() -> None:
    """Regression: every C4 Rel must be emitted inside a <UserObject> that
    carries its c4* attributes and the palette's ``placeholders=1`` label
    template — that is what makes a relationship render like its shape-library
    entry instead of a bare label.

    Layout used to rebuild each Edge field-by-field on its way out of
    ``_route_edges``, dropping ``object_attributes`` and with it the wrapper,
    so every relationship silently degraded to a plain ``value=`` string.
    """
    with tempfile.NamedTemporaryFile(suffix=".drawio", delete=False) as f:
        output_path = Path(f.name)

    try:
        assert _run_convert(_ARCH_MDG, output_path) == 0
        root = ET.parse(str(output_path)).getroot()

        wrapped_rels = [
            wrapper
            for wrapper in root.iter("UserObject")
            if wrapper.get("c4Type") == "Relationship"
        ]
        assert wrapped_rels, "no C4 relationship was wrapped in a <UserObject>"
        for wrapper in wrapped_rels:
            assert wrapper.get("placeholders") == "1"
            assert "%c4Description%" in (wrapper.get("label") or "")
            inner = wrapper.find("mxCell")
            assert inner is not None and inner.get("edge") == "1"
    finally:
        output_path.unlink(missing_ok=True)


def test_validation_rejects_duplicate_ids() -> None:
    """The pre-write validation guardrail blocks duplicate diagram IDs."""
    # Build a deliberately bad XML with duplicate diagram IDs
    bad_xml = """<?xml version="1.0"?>
<mxfile>
  <diagram id="same-id" name="Page 1">
    <mxGraphModel>
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
      </root>
    </mxGraphModel>
  </diagram>
  <diagram id="same-id" name="Page 2">
    <mxGraphModel>
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>"""

    from mdg_drawio.engine.validate import validate_generated_xml

    errors = validate_generated_xml(bad_xml)
    assert errors, "expected validation errors for duplicate diagram IDs"
    assert any("duplicate" in e for e in errors), f"wrong errors: {errors}"


@needs_sidecars
def test_stacklayout_container_children_stack_tightly(tmp_path: Path) -> None:
    """A ``childLayout=stackLayout`` shape (UML class) must have its members
    stacked tightly and be sized to the exact stacked height, so draw.io's
    stack re-layout on load is a no-op (no shrink/reflow/overlap)."""
    input_path = tmp_path / "cls.mdg"
    output_path = tmp_path / "cls.drawio"
    input_path.write_text(
        '---\ntitle: "t"\nmode: layered\n---\n\n'
        'uml.Class(c1, "MyClass"):\n'
        '    uml.Item(c1__1, "+a")\n'
        '    uml.Item(c1__2, "+b")\n'
        '    uml.Item(c1__3, "+c")\n',
        encoding="utf-8",
    )
    assert _run_convert(input_path, output_path) == 0

    root = ET.parse(str(output_path)).getroot()
    cells = {}
    for el in root.iter():
        if el.tag in ("mxCell", "object", "UserObject"):
            mx = el if el.tag == "mxCell" else el.find("mxCell")
            if mx is None or mx.get("vertex") != "1":
                continue
            geo = mx.find("mxGeometry")
            if geo is None:
                continue
            cells[el.get("id") or mx.get("id")] = (mx.get("parent"), geo)

    cls_geo = cells["c1"][1]
    start_size = 26  # uml.class startSize
    item_h = float(cells["c1__1"][1].get("height", "0"))
    # Class height == startSize + N*item_height (matches draw.io stackLayout).
    assert float(cls_geo.get("height", "0")) == start_size + 3 * item_h
    # Members stack directly below the title band, in order, filling class width.
    cls_w = float(cls_geo.get("width", "0"))
    for i, cid in enumerate(("c1__1", "c1__2", "c1__3")):
        parent, geo = cells[cid]
        assert parent == "c1"
        assert float(geo.get("y", "0")) == start_size + i * item_h
        assert float(geo.get("x", "0")) == 0.0
        assert float(geo.get("width", "0")) == cls_w


def _boundary_child_positions(
    output_path: Path, child_ids: tuple[str, ...]
) -> dict[str, tuple[float, float]]:
    """Return ``{id: (x, y)}`` for the given boundary-child cells."""
    root = ET.parse(str(output_path)).getroot()
    out: dict[str, tuple[float, float]] = {}
    for el in root.iter():
        cid = el.get("id")
        if cid not in child_ids:
            continue
        mx = el if el.tag == "mxCell" else el.find("mxCell")
        geo = mx.find("mxGeometry") if mx is not None else None
        if geo is not None:
            out[cid] = (float(geo.get("x", "0")), float(geo.get("y", "0")))
    return out


def _direction_source(direction: str) -> str:
    """A minimal two-container c4 page (a→b) with a direction override."""
    return (
        f'---\npage: "P"\nmode: layered\ndirection: {direction}\n---\n\n'
        'c4.System_Boundary(sys, "S"):\n'
        '    c4.Container(a, "A", "first", technology="Python")\n'
        '    c4.Container(b, "B", "second", technology="Python")\n\n'
        'c4.Rel(a, b, "Calls", description="")\n'
    )


def test_frontmatter_direction_tb_stacks_vertically(tmp_path: Path) -> None:
    """`direction: TB` ranks a→b top-to-bottom (b below a, same column)."""
    src = tmp_path / "tb.mdg"
    src.write_text(_direction_source("TB"), encoding="utf-8")
    assert _run_convert(src, tmp_path / "tb.drawio") == 0

    pos = _boundary_child_positions(tmp_path / "tb.drawio", ("a", "b"))
    assert pos["b"][1] > pos["a"][1]      # b is below a
    assert pos["a"][0] == pos["b"][0]     # same column


def test_frontmatter_direction_lr_spreads_horizontally(tmp_path: Path) -> None:
    """`direction: LR` ranks a→b left-to-right (b right of a, same row)."""
    src = tmp_path / "lr.mdg"
    src.write_text(_direction_source("LR"), encoding="utf-8")
    assert _run_convert(src, tmp_path / "lr.drawio") == 0

    pos = _boundary_child_positions(tmp_path / "lr.drawio", ("a", "b"))
    assert pos["b"][0] > pos["a"][0]      # b is right of a
    assert pos["a"][1] == pos["b"][1]     # same row


def test_tb_degenerate_container_grid_packs_not_single_column(
    tmp_path: Path,
) -> None:
    """In TB, a container whose children form a chain (degenerate ranking → no
    rank wider than one) must grid-pack, not stack into one tall column.

    Without this, a coupled cluster (e.g. the Layout Engine's modules, cyclic via
    TYPE_CHECKING) produced an N-tall strip. Grid uses the secondary axis (LR),
    so at least two children share a row.
    """
    chain = "".join(
        f'    c4.Container(n{i}, "N{i}", "d", technology="Python")\n'
        for i in range(6)
    )
    rels = "".join(f'c4.Rel(n{i}, n{i + 1}, "x", description="")\n' for i in range(5))
    src = tmp_path / "chain.mdg"
    src.write_text(
        f'---\npage: "P"\nmode: layered\ndirection: TB\n---\n\n'
        f'c4.System_Boundary(sys, "S"):\n{chain}\n{rels}',
        encoding="utf-8",
    )
    assert _run_convert(src, tmp_path / "chain.drawio") == 0

    pos = _boundary_child_positions(
        tmp_path / "chain.drawio", tuple(f"n{i}" for i in range(6))
    )
    rows_by_y: dict[float, int] = {}
    for _x, y in pos.values():
        rows_by_y[y] = rows_by_y.get(y, 0) + 1
    assert max(rows_by_y.values()) >= 2, "expected a grid (≥2 per row), got a column"


def test_invalid_frontmatter_direction_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown `direction:` value fails loudly (clean nonzero exit + message)."""
    src = tmp_path / "bad.mdg"
    src.write_text(_direction_source("sideways"), encoding="utf-8")

    assert _run_convert(src, tmp_path / "bad.drawio") == 1
    assert "direction: sideways" in capsys.readouterr().err


def _archimate_grid_source(mode: str, grid: bool) -> str:
    """A Grouping containing 4 sibling BusinessServices, optionally gridded."""
    grid_line = "grid: true\n" if grid else ""
    return (
        f'---\npage: "P"\nmode: {mode}\n{grid_line}---\n\n'
        'archimate3.Grouping(gp1, "Grouping"):\n'
        '    archimate3.BusinessService(bs1, "A")\n'
        '    archimate3.BusinessService(bs2, "B")\n'
        '    archimate3.BusinessService(bs3, "C")\n'
        '    archimate3.BusinessService(bs4, "D")\n'
    )


def test_frontmatter_grid_true_forces_square_arrangement(tmp_path: Path) -> None:
    """`grid: true` under `mode: layered` packs 4 siblings into a 2x2 grid,
    not a single column."""
    src = tmp_path / "grid.mdg"
    src.write_text(_archimate_grid_source("layered", grid=True), encoding="utf-8")
    assert _run_convert(src, tmp_path / "grid.drawio") == 0

    pos = _boundary_child_positions(
        tmp_path / "grid.drawio", ("bs1", "bs2", "bs3", "bs4")
    )
    xs = {x for x, _y in pos.values()}
    ys = {y for _x, y in pos.values()}
    assert len(xs) == 2, "expected 2 distinct columns"
    assert len(ys) == 2, "expected 2 distinct rows"


def test_invalid_frontmatter_grid_with_process_mode_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`grid: true` combined with `mode: process` fails loudly, not silently."""
    src = tmp_path / "bad_grid.mdg"
    src.write_text(_archimate_grid_source("process", grid=True), encoding="utf-8")

    assert _run_convert(src, tmp_path / "bad_grid.drawio") == 1
    assert "grid: true" in capsys.readouterr().err


def _edge_hidden(output_path: Path, source: str, target: str) -> bool:
    """Whether the edge cell for source->target is actually invisible.

    draw.io hides a cell via the mxCell ``visible`` attribute, not a style
    token -- a style-only "hidden=1" is inert and the edge still renders.
    """
    root = ET.parse(str(output_path)).getroot()
    for el in root.iter("mxCell"):
        if el.get("source") == source and el.get("target") == target:
            return el.get("visible") == "0"
    raise AssertionError(f"no edge cell found for {source}->{target}")


def test_edge_between_direct_parent_child_is_hidden(tmp_path: Path) -> None:
    """An explicit relationship edge between a container and its direct
    nested child is hidden in the rendering -- nesting already shows it."""
    src = tmp_path / "hide_direct.mdg"
    src.write_text(
        '---\npage: "P"\nmode: layered\n---\n\n'
        'archimate3.Grouping(gp1, "Grouping"):\n'
        '    archimate3.BusinessFunction(bf1, "Function")\n'
        'archimate3.Composition(gp1, bf1, "")\n',
        encoding="utf-8",
    )
    assert _run_convert(src, tmp_path / "hide_direct.drawio") == 0
    assert _edge_hidden(tmp_path / "hide_direct.drawio", "gp1", "bf1")


def test_edge_between_grandparent_grandchild_stays_visible(tmp_path: Path) -> None:
    """A relationship edge between a grandparent and grandchild (not a direct
    nesting pair) is a real cross-level relationship and must stay visible."""
    src = tmp_path / "keep_visible.mdg"
    src.write_text(
        '---\npage: "P"\nmode: layered\n---\n\n'
        'archimate3.Grouping(gp1, "Grouping"):\n'
        '    archimate3.BusinessFunction(bf1, "Function"):\n'
        '        archimate3.BusinessService(bs1, "Service")\n'
        'archimate3.Composition(gp1, bs1, "")\n',
        encoding="utf-8",
    )
    assert _run_convert(src, tmp_path / "keep_visible.drawio") == 0
    assert not _edge_hidden(tmp_path / "keep_visible.drawio", "gp1", "bs1")


def test_unknown_use_statement_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `use` naming a library that does not exist fails loudly.

    It used to fall back to c4 in silence, so a typo (`use bpnm2`) laid the
    whole page out with the wrong notation's config, scaling and rank
    exclusions with no diagnostic at all.
    """
    src = tmp_path / "bad_use.mdg"
    src.write_text(
        '---\npage: "P"\nmode: layered\n---\n\nuse bpnm2\n'
        'c4.System(s, "S")\n',
        encoding="utf-8",
    )

    assert _run_convert(src, tmp_path / "bad_use.drawio") == 1
    assert "use bpnm2" in capsys.readouterr().err


def test_page_without_use_statement_defaults_to_c4(tmp_path: Path) -> None:
    """No `use` line at all is still legal — c4 stays the default notation."""
    src = tmp_path / "no_use.mdg"
    src.write_text(
        '---\npage: "P"\nmode: layered\n---\n\nc4.System(s, "S")\n',
        encoding="utf-8",
    )

    assert _run_convert(src, tmp_path / "no_use.drawio") == 0


def _inner_cell(root: ET.Element, cell_id: str) -> ET.Element:
    """Return the ``<mxCell>`` for *cell_id*, unwrapping object/UserObject."""
    for el in root.iter():
        if el.tag in ("object", "UserObject") and el.get("id") == cell_id:
            inner = el.find("mxCell")
            assert inner is not None
            return inner
        if el.tag == "mxCell" and el.get("id") == cell_id:
            return el
    raise AssertionError(f"cell {cell_id!r} not found")


def _edge_cell(root: ET.Element, source: str, target: str) -> ET.Element:
    for el in root.iter():
        if (
            el.tag == "mxCell"
            and el.get("edge") == "1"
            and el.get("source") == source
            and el.get("target") == target
        ):
            return el
    raise AssertionError(f"edge {source}->{target} not found")


def _node_size(root: ET.Element, cell_id: str) -> tuple[float, float]:
    geo = _inner_cell(root, cell_id).find("mxGeometry")
    assert geo is not None
    return (
        float(geo.get("width", "0")),
        float(geo.get("height", "0")),
    )


def test_c4_category_shapes_scale_symmetrically(tmp_path: Path) -> None:
    """C4 shape groups scale uniformly per category."""
    src = tmp_path / "scale.mdg"
    out = tmp_path / "scale.drawio"
    src.write_text(
        '---\ntitle: "scale"\nmode: layered\n---\n\n'
        'c4.Person(person_short, "User", "Short description.")\n'
        'c4.Person_Ext(person_long, "Diagram Author With Long Visible Name", '
        '"Writes MDG source files, reviews generated diagrams, and adjusts the '
        'visual layout in draw.io when needed.")\n'
        'c4.System(sys_short, "API", "Short description.")\n'
        'c4.System_Ext(sys_long, "External analytics platform", '
        '"Stores operational telemetry and exposes long reporting workflows.")\n'
        'c4.Container(container_short, "CLI", "Short description.")\n'
        'c4.ContainerDb(container_long, "Operational reporting database", '
        '"Stores generated diagram metadata, overlays, and registry snapshots.")\n'
        'c4.Component(component_short, "Parser", "Short description.")\n'
        'c4.Component(component_long, "Shape scaling and symmetry policy", '
        '"Measures text and grows every component in the same group together.")\n',
        encoding="utf-8",
    )

    assert _run_convert(src, out) == 0

    root = ET.parse(str(out)).getroot()
    person_width, person_height = _node_size(root, "person_short")
    assert (person_width, person_height) == _node_size(root, "person_long")
    assert person_width / person_height == pytest.approx(
        C4_SCALER_PERSON_ASPECT_RATIO,
        rel=1e-4,
    )
    assert _node_size(root, "sys_short") == _node_size(root, "sys_long")
    assert _node_size(root, "container_short") == _node_size(root, "container_long")
    assert _node_size(root, "component_short") == _node_size(root, "component_long")


def test_architecture_component_shapes_have_header_room(tmp_path: Path) -> None:
    """Architecture components with long text need enough height for C4 headers."""
    out = tmp_path / "architecture.drawio"

    assert _run_convert(_ARCH_MDG, out) == 0

    root = ET.parse(str(out)).getroot()
    models_width, models_height = _node_size(root, "models_contract_co")
    # Long-text component is capped at the max width (not grown past it).
    assert models_width == C4_SCALER_MAX_WIDTH
    # Enough height for the C4 header band plus body text (regression guard on
    # the rendered height for this known component).
    assert models_height == pytest.approx(204.0)
    # A sibling component with equally long text resolves to the same box.
    assert _node_size(root, "container_co") == (models_width, models_height)


def test_overlay_preserves_geometry_edges_and_anchors(tmp_path: Path) -> None:
    """Manual node positions, edge anchors, and elbow waypoints in an existing
    .drawio must survive regeneration WITHOUT --force (the overlay round-trip).

    C4 nodes and edges are ``<UserObject>``-wrapped, so this also guards that the
    overlay reader unwraps wrappers — otherwise every manual edit is silently
    discarded on the next regeneration.
    """
    src = tmp_path / "rt.mdg"
    out = tmp_path / "rt.drawio"
    src.write_text(
        '---\ntitle: "t"\nmode: layered\n---\n\n'
        'c4.System(a, "A")\n'
        'c4.System(b, "B")\n'
        'c4.Rel(a, b, "calls", technology="HTTP")\n',
        encoding="utf-8",
    )

    # 1. Initial generation.
    assert main([str(src), str(out), "--force"]) == 0

    # 2. Simulate manual draw.io edits: move node "a"; add exit/entry anchors
    #    and an elbow waypoint to the edge.
    tree = ET.parse(str(out))
    root = tree.getroot()
    a_geo = _inner_cell(root, "a").find("mxGeometry")
    assert a_geo is not None
    a_geo.set("x", "999")
    a_geo.set("y", "888")
    edge = _edge_cell(root, "a", "b")
    anchors = "exitX=0.75;exitY=0.5;entryX=0;entryY=0.5;"
    edge.set("style", edge.get("style", "") + anchors)
    edge_geo = edge.find("mxGeometry")
    assert edge_geo is not None
    # Replace any existing <Array as="points"> (the initial generation now
    # correctly writes a real routed waypoint) rather than appending a
    # second one -- draw.io itself would never have two, and .find() below
    # would silently pick up the first (stale) one instead of this edit.
    existing_array = edge_geo.find('Array[@as="points"]')
    if existing_array is not None:
        edge_geo.remove(existing_array)
    array = ET.SubElement(edge_geo, "Array", {"as": "points"})
    ET.SubElement(array, "mxPoint", {"x": "500", "y": "500"})
    tree.write(str(out), encoding="utf-8")

    # 3. Regenerate WITHOUT --force → engine reads the overlay from *out*.
    assert main([str(src), str(out)]) == 0

    # 4. The manual edits must be preserved.
    root2 = ET.parse(str(out)).getroot()

    moved = _inner_cell(root2, "a").find("mxGeometry")
    assert moved is not None
    assert float(moved.get("x", "0")) == 999.0, "node x position not preserved"
    assert float(moved.get("y", "0")) == 888.0, "node y position not preserved"

    edge2 = _edge_cell(root2, "a", "b")
    style2 = edge2.get("style", "")
    assert "exitX=0.75" in style2, "edge exit anchor not preserved"
    assert "entryX=0" in style2, "edge entry anchor not preserved"

    edge2_geo = edge2.find("mxGeometry")
    assert edge2_geo is not None
    points = edge2_geo.find('Array[@as="points"]')
    assert points is not None, "edge waypoints not preserved"
    assert any(
        float(p.get("x", "0")) == 500.0 and float(p.get("y", "0")) == 500.0
        for p in points.findall("mxPoint")
    ), "edge waypoint coordinates not preserved"


def test_overlay_preserves_a_manually_changed_text_alignment(
    tmp_path: Path,
) -> None:
    """A user switching a node's label to left-aligned directly in draw.io
    must survive a plain regenerate, not just its position -- c4.Person's own
    palette style bakes in ``align=center``, so this also guards that the
    override cleanly replaces it rather than leaving both tokens in the
    style string (draw.io's own last-wins parsing would still render
    correctly, but a duplicate token is exactly what _apply_corrections's
    "stays clean" discipline elsewhere in this file exists to avoid)."""
    src = tmp_path / "rt.mdg"
    out = tmp_path / "rt.drawio"
    src.write_text('c4.Person(a, "A")\n', encoding="utf-8")

    assert main([str(src), str(out), "--force"]) == 0
    tree = ET.parse(str(out))
    root = tree.getroot()
    cell = _inner_cell(root, "a")
    style = cell.get("style", "")
    assert "align=center" in style
    cell.set("style", style.replace("align=center", "align=left"))
    tree.write(str(out), encoding="utf-8")

    assert main([str(src), str(out)]) == 0
    style2 = _inner_cell(ET.parse(str(out)).getroot(), "a").get("style", "")
    assert style2.count("align=") == 1, f"duplicate align token: {style2!r}"
    assert "align=left" in style2

    # --force still fully regenerates, discarding the manual style edit too.
    assert main([str(src), str(out), "--force"]) == 0
    style3 = _inner_cell(ET.parse(str(out)).getroot(), "a").get("style", "")
    assert "align=center" in style3


def test_overlay_preserves_a_manually_changed_fill_color_for_a_decorative_notation(
    tmp_path: Path,
) -> None:
    """A user recolouring an ERD entity directly in draw.io must survive a
    plain regenerate -- erd doesn't encode any real distinction in colour,
    so it's treated as decorative, the same as text alignment."""
    src = tmp_path / "rt.mdg"
    out = tmp_path / "rt.drawio"
    src.write_text('erd.EntityRect(e1, "Regelverk")\n', encoding="utf-8")

    assert main([str(src), str(out), "--force"]) == 0
    tree = ET.parse(str(out))
    root = tree.getroot()
    cell = _inner_cell(root, "e1")
    style = cell.get("style", "")
    assert "fillColor=" not in style, "test assumes no pre-existing fillColor"
    cell.set("style", style + "fillColor=#FFF2CC;strokeColor=#D6B656;")
    tree.write(str(out), encoding="utf-8")

    assert main([str(src), str(out)]) == 0
    style2 = _inner_cell(ET.parse(str(out)).getroot(), "e1").get("style", "")
    assert style2.count("fillColor=") == 1, f"duplicate fillColor token: {style2!r}"
    assert "fillColor=#FFF2CC" in style2
    assert "strokeColor=#D6B656" in style2


def test_overlay_does_not_preserve_fill_color_for_a_color_semantic_notation(
    tmp_path: Path,
) -> None:
    """A user recolouring a C4 Person directly in draw.io must NOT survive a
    plain regenerate -- C4 (and ArchiMate/UML/UML2.5) use colour to encode a
    real .mdg-driven type distinction (e.g. Person vs Person_Ext), so
    freezing a manual colour there could mask an intentional variant
    change (see convert.py's _COLOR_SEMANTIC_LIBRARIES)."""
    src = tmp_path / "rt.mdg"
    out = tmp_path / "rt.drawio"
    src.write_text('c4.Person(a, "A")\n', encoding="utf-8")

    assert main([str(src), str(out), "--force"]) == 0
    tree = ET.parse(str(out))
    root = tree.getroot()
    cell = _inner_cell(root, "a")
    cell.set("style", cell.get("style", "") + "fillColor=#FF0000;")
    tree.write(str(out), encoding="utf-8")

    assert main([str(src), str(out)]) == 0
    style2 = _inner_cell(ET.parse(str(out)).getroot(), "a").get("style", "")
    assert "fillColor=#FF0000" not in style2


@needs_sidecars
def test_overlay_regrows_container_to_fit_a_manually_moved_child(
    tmp_path: Path,
) -> None:
    """Regression: a child dragged far outside a container's auto-packed
    bounds kept its own moved position on a plain regenerate (the overlay
    round-trip already worked per-node) while the CONTAINER stayed at its
    stale auto-computed size, clipping/overflowing the child -- nothing
    re-grew the parent after the second overlay pass restored the child."""
    src = tmp_path / "rt.mdg"
    out = tmp_path / "rt.drawio"
    src.write_text(
        '---\ntitle: "t"\nmode: layered\n---\n\n'
        'general.VerticalContainer(v1, "Group"):\n'
        '    erd.EntityRect(e1, "One")\n'
        '    erd.EntityRect(e2, "Two")\n',
        encoding="utf-8",
    )

    assert main([str(src), str(out), "--force"]) == 0

    # Drag e1 far outside the container's current (tightly auto-packed) bounds.
    tree = ET.parse(str(out))
    root = tree.getroot()
    e1_geo = _inner_cell(root, "e1").find("mxGeometry")
    assert e1_geo is not None
    moved_x, moved_y = 900.0, 700.0
    e1_geo.set("x", str(moved_x))
    e1_geo.set("y", str(moved_y))
    tree.write(str(out), encoding="utf-8")

    # Regenerate WITHOUT --force -> engine reads the overlay from *out*.
    assert main([str(src), str(out)]) == 0

    root2 = ET.parse(str(out)).getroot()
    e1_geo2 = _inner_cell(root2, "e1").find("mxGeometry")
    assert e1_geo2 is not None
    assert float(e1_geo2.get("x", "0")) == moved_x, "child x not preserved"
    assert float(e1_geo2.get("y", "0")) == moved_y, "child y not preserved"
    e1_width = float(e1_geo2.get("width", "0"))
    e1_height = float(e1_geo2.get("height", "0"))

    v1_geo = _inner_cell(root2, "v1").find("mxGeometry")
    assert v1_geo is not None
    v1_width = float(v1_geo.get("width", "0"))
    v1_height = float(v1_geo.get("height", "0"))
    assert v1_width >= moved_x + e1_width, "container not widened to fit moved child"
    assert (
        v1_height >= moved_y + e1_height
    ), "container not heightened to fit moved child"


def test_convert_reports_dsl_parse_errors(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Malformed DSL should return a clean CLI error, not a traceback."""
    input_path = tmp_path / "bad.mdg"
    output_path = tmp_path / "bad.drawio"
    input_path.write_text("c4.Person(\n", encoding="utf-8")

    exit_code = _run_convert(input_path, output_path)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert not output_path.exists()
    assert "mdg: error: parse failed: line 1:" in captured.err
    assert "Traceback" not in captured.err
