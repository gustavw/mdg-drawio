---
name: mdg-convert
description: Bundles the MDG-to-drawio conversion workflow. Use when asked to convert, generate, or produce .drawio output from .mdg source files.
---

# MDG → Drawio Conversion Workflow

This skill bundles the end-to-end pipeline knowledge.

## Pipeline overview

```
.mdg source → [parse] Document → [layout] position →
  [generator] XML → .drawio file
```

## Pipeline steps

| Step | Skill | Key function |
|---|---|---|
| 1. Parse | `dsl` | `parse(source)` → `Document` / `MultiPageDocument` |
| 2. Size resolve | `layout` | `create_size_resolver()` → `SizeResolver` callable |
| 3. Layout | `layout` | `layout_cls = dispatch_layout(mode); layout = layout_cls(); layout.apply(nodes, edges, size_of)` → `Result` |
| 4. Generate | `generator` | `generate(document)` → drawio XML string |
| 5. Write | `cli` | Validate XML, write to disk |

## Pre-write XML validation

Always validate before writing:

1. **Unique diagram IDs** — every `<diagram>` must have a unique `id`
2. **Root cells present** — `<mxGraphModel><root>` must contain `mxCell id="0"` and `mxCell id="1" parent="0"`
3. **Edge endpoint integrity** — edge `source`/`target` must reference existing vertex cell IDs
4. **Valid XML** — must parse with `ET.fromstring()` without error

## Known gaps

- Only C4 notation is implemented for parsing
- UML, ArchiMate, BPMN, ERD, and General are stubs
- The Code page in `c4_architecture.mdg` uses UML notation — edges treated as passthrough

## CLI command reference

Positional args, not flags: first is always input, second (optional) is
output — never guessed from extension, so a wrong guess can't overwrite the
`.mdg` source. Omitting output derives `<input>.drawio` alongside the input.

```bash
mdg input.mdg output.drawio --force
mdg input.mdg --force                       # writes input.drawio alongside it
python -m mdg_drawio input.mdg output.drawio --force

mdg merge existing.mdg new.drawio [--write]  # splice hand-drawn cells into existing.mdg
mdg derive diagram.drawio [--json]           # print which registry shape each cell derives to
```

## Testing the pipeline

```bash
make lint
make test
python -m mdg_drawio docs/architecture/c4_architecture.mdg /tmp/test.drawio --force
```
