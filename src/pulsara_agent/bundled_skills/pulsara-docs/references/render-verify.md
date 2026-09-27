# Convert, render, and verify

## Match checks to the change

Always reopen the output and verify the requested content and affected structures. Choose visual checks by what changed:

| Change | Visual check |
| --- | --- |
| Reading only | Render when layout, images, extraction gaps, or page citations matter |
| New document or broad layout changes | Inspect every rendered page, in batches as needed |
| Local visible edit | Inspect affected pages and neighboring pagination boundaries; compare page count and expand if flow, fonts, or fields changed |
| Comment text/author/anchors only | Verify comments, anchors, and unchanged body content; render only if review display or layout also matters |

If a local edit's layout impact cannot be bounded, inspect the full output. Missing rendering or vision tools are a verification gap, not evidence of failure in the document itself; deliver a useful checked draft when appropriate and state what remains unchecked.

## Optional renderer

Use native Word when exact Word behavior or its review UI is required. Otherwise the helper uses installed LibreOffice and Poppler (`pdftoppm`):

```sh
python /path/to/pulsara-docs/scripts/render_docx.py output.docx --out-dir preview
python /path/to/pulsara-docs/scripts/render_docx.py output.docx --out-dir changed-pages --pages 3:5
```

Choose a new output directory. `--pages` is one-based and inclusive; it rasterizes selected pages after producing the complete PDF. `--dpi` controls image resolution; `--soffice` and `--pdftoppm` provide explicit executable paths. The helper uses an isolated temporary LibreOffice profile and leaves the source DOCX unchanged.

Open the generated images to check clipping, overlap, blank pages, fonts, tables, headers/footers, page numbers, and equations. A successful render alone does not perform this inspection. Verify the final delivered DOCX version; repeat affected checks after later edits.

Package/XML checks do not prove full schema conformance. Comments, revisions, fields, and embedded objects also need their relevant semantic checks; appearance alone cannot prove their correctness or editability.

## Conversion boundaries

Convert binary `.doc` using a capable application, preserving the original and comparing content and layout with the `.docx` output. Changing the extension is not conversion.

Macro-bearing files, encryption, protected editing, and signatures may require native support. Preserve macros according to the task without executing them; editing signed content can invalidate its signature. Keep conversion separate from preview generation to avoid unintentionally replacing the delivered DOCX with a renderer's round-trip version.

Deliver the editable artifact; include previews when useful and identify consequential renderer differences or unverified features.
