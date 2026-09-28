# Reading and extraction

## Choose a method from the document

Read relevant pages with an available text extractor such as pdftotext, pypdf, PyMuPDF, or pdfplumber. Check reading order when columns, tables, captions, or footnotes interact; extraction order need not match visual order. Region-based extraction can separate columns after their bounds are identified on the actual page.

Missing or garbled text can indicate scans, damaged font mappings, or a poor existing OCR layer. Render the affected page before interpreting empty extraction as empty content. A file can mix native text and scanned pages. For encrypted inputs, use the user's supplied password through a reader that supports the encryption.

If OCR or layout recognition is needed, choose a local engine, available vision capability, or external service according to language, layout, accuracy, and environment. Discuss a route with the user when it needs unavailable credentials, incurs an unapproved cost, or uploads the document without existing authorization. No particular OCR stack is required. Distinguish extracted OCR text from a searchable PDF with a text layer, and produce the form the user needs. Check uncertain recognition against the page, especially numbers, signs, and formulas.

Retain page boundaries and useful section/table labels. Physical PDF page numbers are one-based in user-facing citations and may differ from printed page labels; identify which you use. Process long documents in relevant pages or batches while covering the requested scope.

## Tables and source locations

A PDF table may consist only of positioned characters and lines. Select extraction settings from its layout: pdfplumber's line strategies suit ruled tables; text strategies can help borderless ones. Crop unrelated surrounding content when it interferes. Judge the extracted rows against headers, merged cells, units, footnotes, and page continuations; the first row is not automatically a header. Preserve identifiers and ambiguous values before deciding their types.

This optional example keeps each detected table separate with its physical page and bounding box:

```python
import pdfplumber
from pdfplumber.table import TableSettings

def read_tables(source, page_number, settings=None, bbox=None):
    resolved = TableSettings.resolve(settings)
    with pdfplumber.open(source) as pdf:
        if not 1 <= page_number <= len(pdf.pages):
            raise ValueError("page_number is outside the PDF's one-based page range")
        page = pdf.pages[page_number - 1]
        region = page.crop(bbox) if bbox is not None else page
        return [
            {"page": page.page_number, "bbox": table.bbox,
             "rows": table.extract(**(resolved.text_settings or {}))}
            for table in region.find_tables(table_settings=resolved)
        ]
```

Choose the one-based `page_number` and optional crop from the source. In pdfplumber, `bbox` is `(x0, top, x1, bottom)` in PDF points; other libraries may use different coordinate origins. Settings above apply to both table detection and cell text extraction.

For unclear detection, inside the open-file block before returning, save `region.to_image(resolution=144).debug_tablefinder(table_settings=resolved).save("table-debug.png")` and view that image. Adjust the relevant settings; an empty result does not establish that the page has no table.

Combine tables across pages only after matching their columns, units, and continuation. Retain page provenance in exported data. In XLSX output, preserve cell types and literal text.

## Images and figures

Choose between extracting an embedded raster image and rendering the page or a region. A visible figure may combine vectors, text, multiple images, and transparency masks; one extracted image need not reproduce it. PyMuPDF image extraction or Poppler's pdfimages can retrieve embedded images; page rendering captures the composed appearance. Include labels/captions when the request needs them. Retain the source page and, for clipped figures, the crop bounds. pdfplumber's image metadata alone is not an extracted picture.
