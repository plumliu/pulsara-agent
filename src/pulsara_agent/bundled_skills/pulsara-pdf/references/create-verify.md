# Creation and verification

## Choose the authoring route

For an existing Word document or workbook, edit and export through a tool that preserves its required features; use the corresponding Skill when available. Keep the editable source if it is part of the requested deliverable.

For a new PDF, choose a generator that suits the content. ReportLab's flowable layout supports flowing paragraphs and tables; its canvas supports deliberate coordinate placement. HTML/CSS export can suit an existing web layout. Let the user's content and reference design determine structure, typography, and page size.

## Layout details

- Use fonts available to the renderer that cover the actual characters, including CJK, symbols, and math. Configure the engine's line breaking as needed; font selection alone does not ensure correct wrapping. Verify font embedding or substitution in the output.
- For flowing content, account for long paragraphs, table rows, repeated headers, and page breaks. Derive widths from the usable page area and keep figures at a readable scale.
- Escape literal text when passing it to a markup-aware API such as ReportLab's `Paragraph`. Use deliberate markup for styles, links, or mathematical notation supported by the chosen renderer.

## Inspect the saved result

Reopen the PDF and check the requested content, page order, and any requested links or bookmarks. Compare exported content with its source when converting. Successful parsing or generation does not verify appearance.

Render with an available engine, such as Poppler or PyMuPDF, and actually inspect the images. For example, to inspect physical page 3 with Poppler:

```sh
mkdir -p preview
pdftoppm -f 3 -l 3 -r 144 -png output.pdf preview/page
```

Choose pages and resolution for the task. Check clipping, overlap, missing glyphs, table alignment, figure legibility, and pagination. For a new or broadly changed document, inspect the page layouts throughout, in batches as needed. A local change can focus on affected pages and neighboring flow; expand if the impact is unclear. Read-only extraction needs visual checks where appearance or uncertainty affects the answer.

Verify the final saved version after corrections. If visual inspection is unavailable, distinguish the content checks performed from the unchecked layout.
