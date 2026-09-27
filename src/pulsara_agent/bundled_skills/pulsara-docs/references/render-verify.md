# Convert, render, and verify

## Match checks to the change

Always reopen the output and verify the requested content and affected structures. Choose visual checks by what changed:

| Change | Visual check |
| --- | --- |
| Reading only | Render when layout, images, extraction gaps, or page citations matter |
| New document, format conversion, or broad layout changes | Inspect every rendered page, in batches as needed |
| Local visible edit | Inspect affected pages and neighboring pagination boundaries; compare page count and expand if flow, fonts, or fields changed |
| Comment text/author/anchors only | Verify comments, anchors, and unchanged body content; render only if review display or layout also matters |

If a local edit's layout impact cannot be bounded, inspect the full output. Missing rendering or vision tools are a verification gap, not evidence of failure in the document itself; deliver a useful checked draft when appropriate and state what remains unchecked.

## Compare source and output

For layout-sensitive work, compare against the source rendered in the target application or a user-provided reference. Keep fonts, review view, and print settings consistent; identify the renderer when reporting comparison results. For format conversion, check preservation of the source's text, tables, images, headers/footers, notes, comments, revisions, fields, and equations.

When previews disagree, inspect the relevant document objects, hidden-content/review/print settings, and font substitution to distinguish content loss from display differences. Page counts and text extraction alone cannot establish preservation. To investigate unexpected changes introduced by saving, a separate **no-edit save** can help isolate the cause.

If material clipping, missing regions, or unintended changes remain, return to the source and choose another editor or conversion path. Keep defective conversions out of deliverables. If no adequate writer is available, provide a source-based reading or change proposal. Keep repairs within the requested scope; do not silently reconstruct or globally restyle the document to resolve a conversion defect.

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

Use [formats and passwords](formats-passwords.md) for `.doc ↔ .docx` conversion or opening a password-encrypted input. Preserve the original and compare content and layout after conversion.

Macro-bearing files, encryption, protected editing, and signatures may require native support. Preserve macros according to the task without executing them; editing signed content can invalidate its signature. Keep conversion separate from preview generation to avoid unintentionally replacing the delivered DOCX with a renderer's round-trip version.

Deliver the editable artifact; include previews when useful and identify consequential renderer differences or unverified features.
