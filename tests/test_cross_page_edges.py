"""Cross-page edges — an edge whose endpoints are declared on different pages.

Each page is laid out on its own, so such an edge cannot be drawn. The
notation layer checks it against the whole document, the engine leaves it out
of the drawing and gives both endpoints a link to the other in their labels,
and the reverse tooling neither reads those links back as label text nor
deletes the edge because draw.io has no cell for it.

Run with:

    make test
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mdg_drawio import main
from mdg_drawio.contracts import (
    MultiPageDocument,
    Node,
    PageLink,
    page_cell_link,
    page_diagram_id,
    page_links_count_text,
    page_links_html,
    strip_page_links_html,
)
from mdg_drawio.generator.generator import _build_node_cell_attrs
from mdg_drawio.layout.config import ShapeScalingConfig
from mdg_drawio.layout.size_resolver import _text_fragments
from mdg_drawio.notation import DATA_DIR, DslError, parse
from mdg_drawio.reverse.derive import load_cells
from mdg_drawio.reverse.style_index import StyleIndex
from mdg_drawio.reverse.sync_cli import main as sync_main

needs_sidecars = pytest.mark.skipif(
    not DATA_DIR.exists(), reason="needs generated style data (run `make build-data`)"
)
needs_index = pytest.mark.skipif(
    not StyleIndex.load().entries, reason="needs palette styles (run `make build-data`)"
)

_TWO_PAGES = """\
---
page: Customer
---
general.VerticalContainer(customer, "Customer"):
    erd.EntityRect(account, "Account")
    erd.EntityRect(contact, "Contact")
erd.ZeroToManyZeroToOne(account, contract, "pays")
erd.OneToMany(account, contact, "has")
---
page: Contract
---
general.VerticalContainer(contracts, "Contract"):
    erd.EntityRect(contract, "Contract")
"""

_HREF_RE = re.compile(r'href="([^"]*)"')


def _with(old: str, new: str) -> str:
    assert old in _TWO_PAGES
    return _TWO_PAGES.replace(old, new, 1)


def _decoded_target(href: str) -> tuple[str, str]:
    """(page id, cell id) a generated link opens."""
    link = html.unescape(href)
    actions = json.loads(link.split(",", 1)[1])["actions"]
    page_id = actions[0]["open"].split(",", 1)[1]
    return page_id, actions[1]["select"]["cells"][0]


# ── Contracts: the label fragment ────────────────────────────────────────────
def test_links_fragment_holds_one_hidden_anchor_per_link_sorted_by_title() -> None:
    fragment = page_links_html(
        [
            PageLink("Zeta <z> & co", page_diagram_id(2), "z1"),
            PageLink("Alpha", page_diagram_id(1), "a1"),
        ]
    )
    assert "data-mdg-page-links" in fragment
    assert "\u21972" in fragment
    titles = re.findall(r'display:none">([^<]*)</a>', fragment)
    assert titles == ["Alpha", "Zeta &lt;z&gt; &amp; co"]
    targets = [_decoded_target(h) for h in _HREF_RE.findall(fragment)]
    assert targets == [("diagram-1", "a1"), ("diagram-2", "z1")]


def test_no_links_add_nothing() -> None:
    assert page_links_html([]) == ""
    assert page_links_count_text(0) == ""


def test_the_count_is_measured_with_the_label() -> None:
    node = Node(id="a", type="erd.EntityRect", label="Account")
    node.page_links = [PageLink("B", "diagram-1", "b")] * 3
    fragments = _text_fragments(node, ShapeScalingConfig())
    assert fragments[-1].text == "Account" + page_links_count_text(3)
    assert page_links_count_text(3) in page_links_html(node.page_links)


def test_a_plain_text_label_stays_plain_text_when_links_force_html() -> None:
    node = Node(id="a", type="x.Shape", label="<<interface>> A & B\nnext")
    node.page_links = [PageLink("B", "diagram-1", "b")]
    attrs = _build_node_cell_attrs(node, "a", "1", "shape=rect;")
    assert "html=1" in attrs["style"]
    assert strip_page_links_html(attrs["value"]) == (
        "&lt;&lt;interface&gt;&gt; A &amp; B<br>next"
    )
    # A label that is already HTML is left as written.
    attrs = _build_node_cell_attrs(node, "a", "1", "shape=rect;html=1;")
    assert strip_page_links_html(attrs["value"]) == node.label


@pytest.mark.parametrize(
    "label",
    ["Contract", "A <b>bold</b> name", "<span>kept</span> text", ""],
)
def test_strip_removes_exactly_the_generated_fragment(label: str) -> None:
    links = [PageLink("Account \u2013 pays (Customer)", "diagram-0", "account")]
    assert strip_page_links_html(label + page_links_html(links)) == label
    assert strip_page_links_html(label) == label


def test_strip_tolerates_draw_io_reordering_the_attributes() -> None:
    resaved = (
        'Contract<span style="font-size:9px;" data-mdg-page-links="1"> \u21971'
        '<a style="display:none" '
        f'href="{page_cell_link("diagram-0", "a")}">A</a></span>'
    )
    assert strip_page_links_html(resaved) == "Contract"


# ── Notation: whole-document validation ──────────────────────────────────────
def test_cross_page_edge_is_parsed_on_its_page_with_its_line() -> None:
    document = parse(_TWO_PAGES)
    assert isinstance(document, MultiPageDocument)
    edge = next(e for e in document.pages[0].edges if e.target_id == "contract")
    assert edge.source_id == "account"
    assert edge.line == 7


def test_an_edge_between_two_nodes_of_another_page_fails_at_its_line() -> None:
    source = _with('"Contract")\n', '"Contract")\n    erd.EntityRect(c2, "C2")\n')
    source = source.replace("(account, contract,", "(c2, contract,")
    with pytest.raises(DslError, match="neither node is on this page") as raised:
        parse(source)
    assert raised.value.line_number == 7


def test_an_edge_on_a_third_page_joining_two_others_fails_at_its_line() -> None:
    pays = 'erd.ZeroToManyZeroToOne(account, contract, "pays")\n'
    source = _TWO_PAGES.replace(pays, "") + "---\npage: Third\n---\n" + pays
    with pytest.raises(DslError, match="neither node is on this page") as raised:
        parse(source)
    assert raised.value.line_number == 16


def test_the_same_cross_page_edge_on_both_pages_is_an_error() -> None:
    source = _TWO_PAGES + 'erd.ZeroToManyZeroToOne(account, contract, "pays")\n'
    with pytest.raises(DslError, match="already declared at line 7") as raised:
        parse(source)
    assert raised.value.line_number == 14


def test_an_endpoint_on_several_other_pages_is_ambiguous() -> None:
    source = _TWO_PAGES + (
        "---\npage: Third\n---\n"
        'erd.EntityRect(contract, "Contract")\n'
    )
    with pytest.raises(DslError, match="more than one other page"):
        parse(source)


def test_an_id_on_the_edges_own_page_wins_over_other_pages() -> None:
    source = _TWO_PAGES + (
        "---\npage: Third\n---\n"
        'erd.EntityRect(account, "Account copy")\n'
    )
    assert isinstance(parse(source), MultiPageDocument)


def test_an_edge_to_an_undeclared_node_is_left_to_the_caller() -> None:
    """Dangling endpoints are not a cross-page concern: parsing accepts them
    as it always has, and checkers such as the architecture model's report
    them."""
    source = _with("(account, contract,", "(account, missing,")
    document = parse(source)
    assert isinstance(document, MultiPageDocument)
    assert any(e.target_id == "missing" for e in document.pages[0].edges)


# ── Engine: links instead of an edge ─────────────────────────────────────────
def _convert(tmp_path: Path, source: str, *, force: bool = True) -> ET.Element:
    mdg_path = tmp_path / "model.mdg"
    mdg_path.write_text(source, encoding="utf-8")
    drawio_path = tmp_path / "model.drawio"
    argv = [str(mdg_path), str(drawio_path)] + (["--force"] if force else [])
    assert main(argv) == 0
    return ET.parse(drawio_path).getroot()


def _cell(diagram: ET.Element, cell_id: str) -> ET.Element:
    return next(c for c in diagram.iter("mxCell") if c.get("id") == cell_id)


@needs_sidecars
def test_cross_page_edge_becomes_a_link_at_both_ends(tmp_path: Path) -> None:
    root = _convert(tmp_path, _TWO_PAGES)
    customer, contract = root.findall("diagram")
    assert [customer.get("id"), contract.get("id")] == ["diagram-0", "diagram-1"]

    edges = [c for d in (customer, contract) for c in d.iter("mxCell") if c.get("edge")]
    pairs = {(c.get("source"), c.get("target")) for c in edges}
    assert ("account", "contract") not in pairs, "a cross-page edge is not drawn"
    assert ("account", "contact") in pairs, "a same-page edge still is"

    account = _cell(customer, "account")
    assert "html=1" in account.get("style", "")
    assert strip_page_links_html(account.get("value", "")) == "Account"
    assert [_decoded_target(h) for h in _HREF_RE.findall(account.get("value", ""))] == [
        ("diagram-1", "contract")
    ]
    assert "Contract \u2013 pays (Contract)" in account.get("value", "")

    target = _cell(contract, "contract")
    assert [_decoded_target(h) for h in _HREF_RE.findall(target.get("value", ""))] == [
        ("diagram-0", "account")
    ]
    assert "Account \u2013 pays (Customer)" in target.get("value", "")

    contact = _cell(customer, "contact")
    assert contact.get("value") == "Contact", "no cross-page edge, no links"


@needs_sidecars
def test_a_hidden_cross_page_edge_is_neither_drawn_nor_linked(tmp_path: Path) -> None:
    source = _TWO_PAGES.replace('"pays")', '"pays", visible=False)')
    root = _convert(tmp_path, source)
    for diagram in root.findall("diagram"):
        for cell in diagram.iter("mxCell"):
            assert "data-mdg-page-links" not in (cell.get("value") or "")
            assert (cell.get("source"), cell.get("target")) != ("account", "contract")


@needs_sidecars
def test_a_shared_section_edge_is_drawn_once_and_never_linked(tmp_path: Path) -> None:
    source = (
        'erd.EntityRect(shared, "Shared")\n'
        'erd.OneToMany(shared, contract, "refs")\n'
        + _TWO_PAGES
        + "---\npage: Third\n---\n"
        'erd.EntityRect(other, "Other")\n'
    )
    root = _convert(tmp_path, source)
    customer, contract_page, third = root.findall("diagram")
    drawn = [
        name
        for name, diagram in (("customer", customer), ("contract", contract_page),
                              ("third", third))
        for cell in diagram.iter("mxCell")
        if (cell.get("source"), cell.get("target")) == ("shared", "contract")
    ]
    assert drawn == ["contract"]
    contract = _cell(contract_page, "contract")
    assert len(_HREF_RE.findall(contract.get("value", ""))) == 1, "only Account"
    assert "Shared" not in contract.get("value", "")
    for diagram in (customer, third):
        shared = _cell(diagram, "shared").get("value") or ""
        assert "data-mdg-page-links" not in shared


@needs_sidecars
def test_regenerating_keeps_the_layout_and_the_links(tmp_path: Path) -> None:
    first = _convert(tmp_path, _TWO_PAGES)
    again = _convert(tmp_path, _TWO_PAGES, force=False)

    def geometry(root: ET.Element) -> dict[str, dict[str, float]]:
        # Compared as numbers: the generator writes "40" where the overlay
        # writes "40.0".
        return {
            c.get("id", ""): {k: float(v) for k, v in g.attrib.items() if k != "as"}
            for c in root.iter("mxCell")
            if c.get("vertex") and (g := c.find("mxGeometry")) is not None
        }

    assert geometry(again) == geometry(first)
    assert _cell(again.findall("diagram")[0], "account").get("value") == _cell(
        first.findall("diagram")[0], "account"
    ).get("value")


# ── Reverse: links are not label text, and the edge is not a deletion ───────
@needs_sidecars
def test_reverse_loading_sees_the_authored_labels(tmp_path: Path) -> None:
    _convert(tmp_path, _TWO_PAGES)
    cells = load_cells(str(tmp_path / "model.drawio"))
    values = {c.cell_id.split(":", 1)[1]: c.value for c in cells}
    assert values["account"] == "Account"
    assert values["contract"] == "Contract"


@needs_sidecars
@needs_index
def test_sync_of_an_unchanged_drawing_changes_nothing(tmp_path: Path) -> None:
    _convert(tmp_path, _TWO_PAGES)
    mdg_path = tmp_path / "model.mdg"
    assert sync_main([str(mdg_path), str(tmp_path / "model.drawio"), "--write"]) == 0
    assert mdg_path.read_text(encoding="utf-8") == _TWO_PAGES


@needs_sidecars
@needs_index
def test_sync_drops_a_cross_page_edge_once_an_endpoint_is_gone(tmp_path: Path) -> None:
    _convert(tmp_path, _TWO_PAGES)
    drawio_path = tmp_path / "model.drawio"
    tree = ET.parse(drawio_path)
    contract_page = tree.getroot().findall("diagram")[1]
    graph_root = contract_page.find("mxGraphModel/root")
    assert graph_root is not None
    graph_root.remove(_cell(contract_page, "contract"))
    tree.write(drawio_path, encoding="utf-8")

    mdg_path = tmp_path / "model.mdg"
    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0
    synced = mdg_path.read_text(encoding="utf-8")
    assert "contract," not in synced
    assert "erd.OneToMany(account, contact" in synced


@needs_sidecars
@needs_index
def test_sync_drops_a_same_page_edge_whose_endpoint_moved_page(tmp_path: Path) -> None:
    """Cutting a node and pasting it on another page deletes its edges in
    draw.io; the edge was a same-page edge in the .mdg, so sync removes it
    rather than keeping it as if it were a cross-page edge."""
    _convert(tmp_path, _TWO_PAGES)
    drawio_path = tmp_path / "model.drawio"
    tree = ET.parse(drawio_path)
    customer, contract_page = tree.getroot().findall("diagram")
    customer_root = customer.find("mxGraphModel/root")
    contract_root = contract_page.find("mxGraphModel/root")
    assert customer_root is not None and contract_root is not None
    contact = _cell(customer, "contact")
    customer_root.remove(contact)
    contact.set("parent", "1")
    contract_root.append(contact)
    for cell in list(customer_root):
        if (cell.get("source"), cell.get("target")) == ("account", "contact"):
            customer_root.remove(cell)
    tree.write(drawio_path, encoding="utf-8")

    mdg_path = tmp_path / "model.mdg"
    assert sync_main([str(mdg_path), str(drawio_path), "--write"]) == 0
    synced = mdg_path.read_text(encoding="utf-8")
    assert "erd.OneToMany(account, contact" not in synced
    assert 'erd.ZeroToManyZeroToOne(account, contract, "pays")' in synced
