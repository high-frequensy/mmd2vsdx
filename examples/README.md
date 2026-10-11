# Examples

Each `<name>.mmd` is converted to `<name>.vsdx`. `<name>.png` is Mermaid's own render; `<name>_preview.png` is what
libvisio (LibreOffice's Visio reader) draws from the `.vsdx`. libvisio ignores some details (rounded corners, curves),
so Visio itself looks closer to the Mermaid render.

| Example | Shows |
|---|---|
| `shapes` | Every classic shape, line style (solid, dotted, thick) and arrow end |
| `shapes_v11` | Mermaid v11 `@{ shape: … }` shapes |
| `connectors` | Edge labels, bidirectional and circle/cross ends, subgraph ends, `linkStyle` |
| `curves` | Curved edges (`curve: basis`) |
| `styles` | `style`, `linkStyle` and `classDef` colours, widths and dashes; a diagram title |
| `text_styles` | Markdown strings, bold, italic, underline, strikethrough, wrapping |
| `fonts_and_colours` | Per-run font family, size and colour with `<span style>` |
| `swimlane` | Swimlane layout |
| `handdrawn` | The handDrawn look |
| `dark_theme` | A dark theme |

Convert one yourself: `mmd2vsdx shapes.mmd -o out/shapes.vsdx`
