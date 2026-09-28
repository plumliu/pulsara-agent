# Create, fill, and format

## Choose the source

- **New document:** `python-docx` suits ordinary construction; Pandoc supports Markdown, citations, and notes. Choose structure and layout for the task, using real heading styles, lists, and fields.
- **Existing design:** preserve its sections, styles, numbering, and headers/footers. Instantiate `.dotx` with a supporting tool; renaming the extension does not make it a `.docx`.
- **Tagged template:** when using `docxtpl`, enable strict missing-variable handling and XML escaping. Tags must follow its run/paragraph/table syntax; formatting can split a placeholder across runs.
- **Untagged form:** map labels to actual cells or paragraphs, checking repeated labels, merged cells, checkboxes, and content controls. Preserve intentional blanks and existing values; resolve missing facts instead of inventing them.

## Optional examples

- [create_report.py](../examples/create_report.py): document elements—headings, a table, header/footer, PAGE field, and separate Latin/CJK fonts.
- [fill_template.py](../examples/fill_template.py): `template.docx data.json output.docx`; fills a prepared template from a JSON object with strict undefined variables, escaping, and a Jinja sandbox. It does not recognize untagged form fields.

Run with `uv run --script /absolute/path/to/example.py ...`. These demonstrate APIs, not a required document design; they write new files only.

## Layout details that matter

- Derive table widths from page width and margins. Keep cell widths/grid/merges consistent, repeat table headers, and allow sensible page breaks.
- Preserve section orientation, first/even-page behavior, and shared header/footer links. Use native numbering for list continuation and restart.
- Use paragraph spacing and keep-with-next settings instead of empty paragraphs or repeated spaces. Provide heading structure, descriptive links, and meaningful image alternative text.
- For CJK text, set the East Asian font (`w:rFonts/@w:eastAsia`) as well as Latin fonts when necessary. Use installed or template fonts; inspect punctuation, wrapping, mixed-script baselines, and any font substitution.
- TOC, PAGE, NUMPAGES, REF, captions, and cross-references have cached results. Writing field codes does not calculate them. If the delivered DOCX needs refreshed caches, update and save it in a capable application. The render helper produces a PDF preview without saving updated DOCX caches; verify displayed values and targets separately.

Pandoc's reference DOCX supplies styles and page properties for generation. For an existing document whose review markup, objects, or fields must survive, use an editor that preserves them; a Markdown round-trip can lose these structures.
