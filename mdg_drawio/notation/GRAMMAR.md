# MDG DSL — canonical grammar

This is the language spec for `.mdg` documents. Every shape registry
(`<lib>/<lib>_registry.yaml`) points here via its `grammar.spec` field and adds
only library-specific notes. An agent should read this file once, then work
from the registry of the library it is using.

## Document structure

```mdg
---
title: "Payment system — container view"
mode: palette
---

use c4

c4.C4("Payment system — container view")
    c4.Person(p1, "Customer", "A paying customer.")
    c4.System_Boundary(b1, "Payment system"):
        c4.Container(api, "API", "Handles payment requests.")
    c4.Rel(p1, api, "Uses", technology="HTTPS")
```

1. **Front matter** — YAML between `---` fences. `title` is the document title;
   `mode` declares the document flavor (`palette` for shape-coverage sheets;
   diagram documents may omit it).
2. **`use <lib>`** — imports a library namespace. One per library used.
3. **Root builder** — exactly one call to the library's document-root function
   (`grammar.root` in the registry, e.g. `c4.C4(...)`, `uml25.UML25(...)`).
   All content nests under it, indented 4 spaces. The root builder is a DSL
   construct, not a palette shape; it does not appear in `shapes`.

## Calls

```
<lib>.<Function>(<node_id>, "label", keyword=value, variant=N)
```

- **Function names** are UpperCamelCase, exactly as in the registry's
  `function` field.
- **Arguments** are declared per shape or row type in registry `args`, which
  is the call's enforced signature. `passing: positional` means Python-style
  positional-or-keyword and binds positional values left-to-right in declaration
  order; `passing: keyword_only` requires `name=value`. `required: true` must
  be bound. Missing required, excess positional, duplicate, and unknown
  arguments are line-numbered errors. The first arg of a vertex is always its
  `node_id`.
- **`variant=N`** selects which palette shape of a function family to render
  (default 1). Variants are listed in `related.variants`; how they differ is
  in each entry's `discriminator`.
- **Keyword args** commonly carry fixed template parts (e.g. `keyword=`,
  `name=`, `entry=`, `technology=`). Consult `passing` rather than assuming a
  parameter is keyword-only from its name. Repeating/scaling content goes in
  child rows instead (see below).

## Ids

`node_id` and child ids (`n1`, `c1`, `b1`, …) are author-chosen and MUST be
unique within the document. Ids in registry examples are placeholders — rename
and scale freely. Ids are load-bearing for bidirectional editing: they are
stable anchors when a diagram is regenerated, so keep them meaningful and do
not renumber existing ids when editing a document.

## Nesting: rows vs containment

A **trailing colon** opens a block; children are indented 4 spaces. What the
children MEAN depends on the registry entry of the parent function:

- Parent declares non-empty **`rows.allowed`** → children are **rows**:
  compartment content stacked inside the shape (class members, table rows,
  swimlane cells). Legal row types are listed in `rows.allowed`; their
  signatures are in the registry's top-level `row_types` section.

  ```mdg
  uml25.Classifier(c1, "Customer"):
      uml25.Item(a1, "+ id: UUID")
      uml25.Divider(d1, "")
      uml25.Item(o1, "+ rename(name)")
  ```

- Parent declares **`contains`** → children are **contained nodes**: real
  vertices placed inside the container (boundary, grouping, lane, frame).
  `contains.allowed` lists legal child functions; `"*"` means any vertex of
  the same library.

  ```mdg
  c4.System_Boundary(b1, "Payment system"):
      c4.Container(api, "API", "Handles requests.")
  ```

- Parent declares **neither** → it cannot take a block; a trailing colon is an
  error.

No shape has both rows and containment children.

## Edges

Edges connect nodes by id:

```mdg
c4.Rel(p1, api, "Uses", technology="HTTPS")
```

- First two positional args are `source` and `target` node ids.
- `None, None` is the unconnected form used in palette/coverage sheets where
  no real nodes exist; real documents should reference node ids.
- Edges take no block (no rows, no containment).
- `endpoints.direction` in the registry documents which way the arrow points;
  `metamodel.endpoints` (when present) gives the spec-typed source/target.

## Labels

Keep literal notation markup in labels verbatim: guillemets `<<keyword>>`,
braces `{abstract}`, brackets `[state]`, and `\n` for a line break. The
renderer does not add or strip notation markup.

## Text block variables

A `block` statement declares a reusable, multi-line text variable:

```mdg
block notes = """
# Open questions
**billing** service, *maybe* retry_count.
"""

general.Text(n1, notes)
```

- Syntax: `block NAME = """<content>"""`, where `NAME` is a bare identifier
  and `<content>` spans one or more lines between the triple-quote fences. A
  single leading and trailing newline right after/before the fences is
  dropped; everything else in the content is kept as-is.
- `NAME` may then be used **in place of a string literal** anywhere a call
  expects one (a label, a keyword value, a data-source part, …) — it is
  substituted with the block's content, e.g. `general.Text(n1, notes)`.
- Declarations are file-wide and order-independent: a block may be declared
  anywhere in the document (including after its own use) and is visible on
  every page of a multi-page document.
- Declaring the same `NAME` twice keeps the first declaration; later ones are
  ignored.
- A block variable can never be used where an id is expected (`node_id`,
  edge `source`/`target`) — those positions always treat a bare identifier as
  an id reference, never as a block substitution.

## Comments

`#` starts a comment (whole-line or trailing). Coverage sheets use trailing
comments to record palette positions (`# 12. Container_Boundary`).

## Kinds

- **vertex** — a node shape; has args and possibly rows or containment.
- **edge** — a connector; has endpoints, no children.
- **diagram** — a pre-composed fragment. Entries with `buildable: false`
  should not be assembled call-by-call; use the primitives referenced in
  `related.see_also` instead.

## How an agent should use a registry

1. Read `grammar` (this spec + library notes) and `row_types`.
2. Pick shapes by `summary`/`use_when`/`avoid_when`; between variants of the
   same function, decide with `discriminator`.
3. Copy the entry's `example` and rename placeholder ids.
4. Respect `rows.allowed`/`contains.allowed` for nesting and `args` for
   signatures.
5. `metamodel` (when present) tells you what the shape MEANS in the notation's
   spec — use it to keep diagrams semantically sensible, e.g. matching
   `metamodel.endpoints` element kinds when connecting edges.
