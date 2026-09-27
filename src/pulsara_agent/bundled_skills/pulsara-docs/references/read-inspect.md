# Read and inspect

For DOCX body text, `Document.iter_inner_content()` reads paragraphs and tables in document order. Read relevant headers, notes, and comments separately, and account for nested/merged cells. Images, text boxes, charts, and equations may require separate extraction or visual inspection. Page citations require a current render; DOCX text positions and cached page fields are not authoritative page numbers.

## Optional inspector

The standard-library helper reads the package without extracting files or fetching links. Replace `/path/to/pulsara-docs` with this Skill's actual directory.

```sh
python /path/to/pulsara-docs/scripts/inspect_docx.py input.docx --text
python /path/to/pulsara-docs/scripts/inspect_docx.py input.docx
python /path/to/pulsara-docs/scripts/inspect_docx.py input.docx --text --part word/footnotes.xml
```

`--text` returns ordered blocks, package errors, and extraction warnings. Without it, the output is an inventory of parts, styles, features, revisions, and external links. For a long or unfamiliar document, inspect that inventory before selecting content.

- `--start 10 --end 20` selects zero-based top-level blocks, start inclusive and end exclusive. Paragraphs and tables share this sequence; a whole table is one block. For large tables, use a library to read relevant rows directly.
- `--part` selects an actual story part from the inventory. Omitting it reads the main body, not every story.
- `--view final` (default) includes inserted text and omits deleted text; `original` reverses this for supported inline insert/delete/move wrappers. Neither option changes the file.

## Interpretation boundaries

The helper retains hidden text and cached field values. It omits drawing, text-box, object, and native equation content; inspect reported features separately when relevant. It supports transitional WordprocessingML, not every OOXML variant. Package checks cover ZIP/XML, content types, relationships, and basic note/comment references, not full schema or layout conformance.

Paragraph-mark, table, numbering, and formatting revisions need a capable editor or targeted inspection beyond this text projection. The helper reports comment contents and reference markers, but does not resolve anchored ranges: locate those using the range markers or a capable editor, rather than guessing from nearby text. Modern replies and resolved states may use extension parts. Preserve original comment IDs when reporting feedback.

Cover the requested scope, expanding extraction when warnings affect the answer. For legacy `.doc` or encrypted inputs, follow [formats and passwords](formats-passwords.md) before using DOCX readers; an encrypted DOCX may be an OLE container rather than a ZIP, so a ZIP error alone does not establish corruption.
