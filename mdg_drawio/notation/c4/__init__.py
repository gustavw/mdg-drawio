"""C4 notation DSL parser.

Parses C4 MDG source text into ``Document`` (or ``MultiPageDocument``).

Uses the shared DSL engine from ``._core`` and the shape registry to map
function names to node/edge types.

Design decisions (improvements over DrawIoGen reference):
- Pure functions — ``parse()`` takes source, returns data. No global state.
- Error messages include line numbers and expected arguments.
- Positional/keyword argument types are validated per call (a bad arg raises a
  ``DslError`` with the line number). Function names are not yet checked against
  the registry — an unknown function is accepted as a ``c4.<Function>`` node.
- The internal node type is ``c4.{Function}`` (PascalCase), matching the
  registry and how the architecture .mdg spells things.
"""

from __future__ import annotations

import ast

from mdg_drawio.contracts import (
    C4_SCALER_SUBTITLE_KEY,
    Diagram,
    Document,
    Edge,
    MultiPageDocument,
    Node,
)

from .._core import (
    DslError,
    build_pages_document,
    extract_block_variables,
    is_none_literal,
    literal_or_name,
    literal_string,
    literal_value,
    parse_block_source,
    parse_bool_metadata,
    parse_frontmatter,
    parse_keyword_int,
    split_pages,
)

_NAMESPACE = "c4"
_DIAGRAM_TITLE_CALLS = frozenset({
    "Context_DiagramTitle",
    "Container_DiagramTitle",
    "Component_DiagramTitle",
})
_EDGE_FUNCTIONS = frozenset({"Rel"})
_DIAGRAM_TITLE_DEFAULT = "C4 Diagram"
_C4_TYPE_LABELS: dict[str, str] = {
    "Person": "Person",
    "Person_Ext": "External Person",
    "System": "Software System",
    "System_Ext": "External Software System",
    "Container": "Container",
    "ContainerDb": "Container",
    "ContainerMicroservice": "Container",
    "ContainerQueue": "Container",
    "ContainerWebBrowser": "Container",
    "Component": "Component",
    "System_Boundary": "System Boundary",
    "Container_Boundary": "Container Boundary",
}
# Scope boundaries carry a c4Application attribute (the "[Software System]" /
# "[Container]" subtitle in draw.io). Unlike other shapes this is a fixed value
# per boundary type, not user-supplied — mirrors draw.io's Sidebar-C4 defaults.
_C4_APPLICATION: dict[str, str] = {
    "System_Boundary": "Software System",
    "Container_Boundary": "Container",
}
type _KeywordValue = str | int | float | bool


def _node_type(function: str) -> str:
    """Map a C4 function name to an internal node type string.

    >>> _node_type("Person")
    'c4.Person'
    """
    return f"{_NAMESPACE}.{function}"


def _subtitle(function: str, c4_type: str, technology: str) -> str:
    application = _C4_APPLICATION.get(function)
    if application:
        return f"[{application}]"
    if technology and (function == "Component" or function.startswith("Container")):
        return f"[{c4_type}: {technology}]"
    return f"[{c4_type}]"


def _parse_keyword_args(
    args: list[ast.AST | ast.keyword],
    line_number: int,
    blocks: dict[str, str] | None = None,
) -> dict[str, _KeywordValue]:
    """Extract C4 keyword arguments while preserving numeric literals."""
    kw_args: dict[str, _KeywordValue] = {}
    for kw in args:
        if not isinstance(kw, ast.keyword) or kw.arg is None:
            continue
        value = literal_value(kw.value, kw.arg, blocks)
        if kw.arg == "visible":
            if not isinstance(value, bool):
                raise DslError("visible= must be True or False", line_number)
        elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise DslError(
                f"{kw.arg}= must be a string, identifier, or number",
                line_number,
            )
        kw_args[kw.arg] = value
    return kw_args


def _parse_node(
    function: str,
    args: list[ast.AST | ast.keyword],
    line_number: int,
    blocks: dict[str, str] | None = None,
) -> Node:
    """Build a ``Node`` from a C4 node call.

    Expected positional args: (node_id, label, description?)
    Keyword args: variant=N, technology=

    *blocks* is the table of declared ``block`` variables -- consulted only
    for string-valued fields (label, data-source parts, keyword values), never
    for ``node_id`` (see ``literal_value``'s own docstring for why).
    """
    pos_args = [a for a in args if not isinstance(a, ast.keyword)]
    kw_args = _parse_keyword_args(args, line_number, blocks)

    if len(pos_args) < 1:
        raise DslError(
            f"{function}() requires at least a node_id argument",
            line_number,
        )

    node_id = literal_or_name(pos_args[0], "node_id")
    # Label is optional in the DSL; an omitted label stays empty rather than
    # defaulting to the node id (see tests/test_review_fixes.py::
    # test_node_without_label_stays_empty for why the earlier id-fallback was
    # reverted -- it was scoped here to c4 alone, but the equivalent default
    # in the shared dsl_engine.py builder broke every other notation's
    # intentionally-unlabeled shapes once they started forward-rendering too).
    label = literal_string(pos_args[1], "label", blocks) if len(pos_args) >= 2 else ""
    variant = parse_keyword_int(kw_args, "variant", 1, line_number)
    technology = kw_args.get("technology", "")
    if not isinstance(technology, str):
        raise DslError("technology= must be a string or identifier", line_number)

    # Data sources start at the 3rd positional arg. A non-string here is a
    # authoring error, not something to silently drop (the surrounding
    # ``parse_block_source`` wraps the ValueError with the line number).
    parts: list[str] = [
        literal_string(pos_args[i], f"argument {i + 1}", blocks)
        for i in range(2, len(pos_args))
    ]

    extra: dict[str, object] = {}
    if technology:
        extra["technology"] = technology

    # Supply the attribute VALUES that the palette's label template substitutes
    # (via placeholders=1). The template itself — fonts, layout, where the
    # technology appears — is inherited from the palette by the generator, so
    # rendering stays 1:1 with the draw.io shape library.
    c4_type = _C4_TYPE_LABELS.get(function, function)
    extra[C4_SCALER_SUBTITLE_KEY] = _subtitle(function, c4_type, technology)
    obj_attrs: dict[str, str | int | float | None] = {
        "c4Name": label,
        "c4Type": c4_type,
    }
    if parts:
        obj_attrs["c4Description"] = parts[0]
    if technology:
        obj_attrs["c4Technology"] = technology
    application = _C4_APPLICATION.get(function)
    if application:
        obj_attrs["c4Application"] = application

    return Node(
        id=node_id,
        type=_node_type(function),
        label=label,
        text_parts=parts,
        variant=variant,
        element_name=function,
        extra=extra,
        object_attributes=obj_attrs,
    )


def _select_rel_variant(
    kw_args: dict[str, _KeywordValue],
    technology: str,
    rel_text: str,
    line_number: int,
) -> int:
    """Pick the palette Rel variant matching the content (author override wins).

    v1 = description + [technology], v2 = description only, v3 = plain connector.
    The generator inherits the matching palette label template for the variant.
    """
    variant = parse_keyword_int(kw_args, "variant", 1, line_number)
    if "variant" in kw_args:
        return variant
    if technology:
        return 1
    if rel_text:
        return 2
    return 3


def _parse_edge(
    function: str,
    args: list[ast.AST | ast.keyword],
    line_number: int,
    blocks: dict[str, str] | None = None,
) -> Edge:
    """Build an ``Edge`` from a C4 edge call.

    Expected positional args: (source_id, target_id, label?)
    Keyword args: technology=, description=, variant=N

    *blocks* is consulted only for the label/keyword string fields, never for
    ``source``/``target`` (see ``literal_value``'s docstring).
    """
    pos_args = [a for a in args if not isinstance(a, ast.keyword)]
    kw_args = _parse_keyword_args(args, line_number, blocks)

    if len(pos_args) < 2:
        raise DslError(
            f"{function}() requires at least source and target arguments",
            line_number,
        )

    # Both endpoints are required and must be real ids, UNLESS both are the
    # literal ``None`` -- the grammar's unconnected palette-edge form, used by
    # coverage sheets to render a shape's edge style with nothing attached. A
    # dangling single ``None`` (only one endpoint missing) is still rejected
    # here, with a line number, rather than deferred to a confusing
    # model-level error.
    source_is_none = is_none_literal(pos_args[0])
    target_is_none = is_none_literal(pos_args[1])
    if source_is_none != target_is_none:
        raise DslError(
            f"{function}(): source and target must both be ids, or both be "
            f"None (the unconnected palette form)",
            line_number,
        )
    unconnected = source_is_none and target_is_none
    source_id = "" if unconnected else literal_or_name(pos_args[0], "source")
    target_id = "" if unconnected else literal_or_name(pos_args[1], "target")
    label = literal_string(pos_args[2], "label", blocks) if len(pos_args) >= 3 else ""
    technology = kw_args.get("technology", "")
    if not isinstance(technology, str):
        raise DslError("technology= must be a string or identifier", line_number)
    description = kw_args.get("description", "")
    if not isinstance(description, str):
        raise DslError("description= must be a string or identifier", line_number)
    rel_text = label or description
    variant = _select_rel_variant(kw_args, technology, rel_text, line_number)
    visible_value = kw_args.get("visible")
    visible = visible_value if isinstance(visible_value, bool) else None

    extra: dict[str, object] = {}
    if technology:
        extra["technology"] = technology
    if variant != 1:
        extra["variant"] = variant

    # Values the palette Rel template substitutes; template inherited by generator.
    obj_attrs: dict[str, str | int | float | None] = {"c4Type": "Relationship"}
    if rel_text:
        obj_attrs["c4Description"] = rel_text
    if technology:
        obj_attrs["c4Technology"] = technology

    # An unconnected palette edge has no endpoints to derive an id from --
    # the line number is stable and unique (one call per source line), same
    # role as the shared passthrough builder's "palette-edge-N" counter.
    edge_id = (
        f"palette-edge-line{line_number}"
        if unconnected
        else f"{source_id}->{target_id}"
    )
    return Edge(
        id=edge_id,
        type=f"{_NAMESPACE}.{function}",
        source_id=source_id,
        target_id=target_id,
        label=label,
        description=description,
        visible=visible,
        extra=extra,
        object_attributes=obj_attrs,
    )


def _is_edge(function: str, args: list[ast.AST | ast.keyword]) -> bool:
    """Return True if *function* is a C4 edge function."""
    return function in _EDGE_FUNCTIONS


def _parse_diagram_title(
    args: list[ast.AST | ast.keyword], line_number: int, default: str
) -> str:
    """Extract the diagram title from a DiagramTitle call."""
    pos_args = [a for a in args if not isinstance(a, ast.keyword)]
    if pos_args:
        return literal_string(pos_args[0], "title")
    return default


def parse_page(
    source: str,
    page_name: str,
    _page_index: int = 0,
    *,
    blocks: dict[str, str] | None = None,
) -> Document:
    """Parse a single page of C4 DSL source into ``Document``.

    *blocks* is the file-wide table of declared ``block`` variables (see
    ``extract_block_variables``); it is bound into the node/edge builder
    closures below so ``_parse_node``/``_parse_edge`` can resolve them.
    """
    metadata, body = parse_frontmatter(source)
    mode = metadata.get("mode", "")
    grid = parse_bool_metadata(metadata, "grid")
    if grid and mode == "process":
        raise DslError(
            "`grid: true` is only valid with the layered layout mode "
            f"(frontmatter `mode:`); got `mode: {mode}`"
        )

    diagram_title_name = ""
    diagram_description = ""

    def _build_node(
        function: str, args: list[ast.AST | ast.keyword], line_number: int
    ) -> Node:
        nonlocal diagram_title_name, diagram_description
        if function in _DIAGRAM_TITLE_CALLS:
            positional = [arg for arg in args if not isinstance(arg, ast.keyword)]
            if positional:
                diagram_title_name = literal_string(
                    positional[0], "title", blocks
                )
            if len(positional) >= 2:
                diagram_description = literal_string(
                    positional[1], "description", blocks
                )
        return _parse_node(function, args, line_number, blocks)

    def _build_edge(
        function: str, args: list[ast.AST | ast.keyword], line_number: int
    ) -> Edge:
        return _parse_edge(function, args, line_number, blocks)

    nodes, edges, diagram_name = parse_block_source(
        body,
        namespace=_NAMESPACE,
        diagram_title_call="",
        diagram_name_default=page_name,
        parse_diagram_title=_parse_diagram_title,
        is_edge=_is_edge,
        build_node=_build_node,
        build_edge=_build_edge,
        blocks=blocks,
    )
    diagram_name = diagram_name or _DIAGRAM_TITLE_DEFAULT

    # DiagramTitle is metadata as well as a rendered node. Capture it through
    # the normal parsed-call path so comments cannot masquerade as calls and
    # block variables are resolved consistently with node labels.
    if diagram_title_name and not page_name:
        diagram_name = diagram_title_name

    return Document(
        diagram=Diagram(
            name=diagram_name,
            description=diagram_description,
            mode=mode,
            direction=metadata.get("direction", ""),
            grid=grid,
        ),
        nodes=nodes,
        edges=edges,
    )


def parse(source: str) -> Document | MultiPageDocument:
    """Parse a C4 MDG source file into ``Document`` or ``MultiPageDocument``.

    Args:
        source: The raw .mdg file contents.

    Returns:
        ``Document`` for single-page documents.
        ``MultiPageDocument`` for multi-page documents (contains
        ``page "Name"`` statements).

    Raises:
        DslError: On syntax or semantic errors.

    Example:
        >>> text = open("diagram.mdg").read()
        >>> doc = parse(text)
        >>> len(doc.nodes) if isinstance(doc, Document) else len(doc.pages)
    """
    blocks, source = extract_block_variables(source)
    pages = split_pages(source)

    def _build_page(page_source: str, page_name: str, page_index: int) -> Document:
        return parse_page(page_source, page_name, page_index, blocks=blocks)

    return build_pages_document(pages, _build_page)
