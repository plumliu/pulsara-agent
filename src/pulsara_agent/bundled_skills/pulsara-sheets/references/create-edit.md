# Creation, templates, and editing

## Pick an editor for the affected features

- **XlsxWriter:** create new XLSX files with formulas, styles, tables, validations, and native charts. It cannot open and edit an existing workbook.
- **openpyxl:** read and edit supported XLSX/XLSM structures, or create ordinary workbooks. Load the editable copy with formulas retained; use `rich_text=True` for in-cell rich text, `keep_links=True` for external-link caches, and `keep_vba=True` for a macro-bearing input. These flags do not guarantee preservation of every Excel feature.
- **Native application or suitable feature-specific API:** prefer when saving with the chosen library would lose required slicers, controls, drawings, Power Query, Data Models, complex pivots, signatures, or other unsupported features anywhere in the workbook. Confirm the application/API is available and supports the particular operation.

Inspect library warnings. If an important feature would be dropped, choose a capable editor before saving; do not deliver a simplified workbook without explaining the change. An unchanged open-and-save copy can help determine whether a tool loses features before attempting a sensitive edit. Use that check for a concrete preservation concern, not for every cell update.

## Create an ordinary workbook

Use the simplest structure that fits the requested deliverable. Put editable inputs and meaningful calculations in identifiable cells; separate sheets when they help navigation or preserve a supplied template. Do not add analysis, audit, or instruction tabs for their own sake.

The optional [creation example](../examples/create_workbook.py) makes a small demonstration workbook with typed dates, leading-zero identifiers, formulas with independently supplied sample results, a table, and an editable chart:

```sh
uv run --script /path/to/pulsara-sheets/examples/create_workbook.py demo.xlsx
```

Adapt the example's clearly marked sample data to the task. It refuses to overwrite a file. Its supplied formula results are sample caches, not evidence of spreadsheet-engine recalculation.

Write numbers as numbers and identifiers as text. Write intended formulas through a formula API; write imported literal text through a text API. In XlsxWriter, `write_string()` keeps literal text literal; disabling `strings_to_formulas` and `strings_to_urls` also prevents automatic interpretation by generic writes. In openpyxl, explicitly set `cell.data_type = 's'` after assigning a formula-like literal. Preserve dates, precision, and locale semantics as described in [formats](formats-passwords.md).

Use real Excel Tables when filtering, structured references, and expansion are useful. Headers must be nonempty strings with unique names within the table. Keep totals outside detail-series ranges unless the task explicitly includes them. For large exports, check the selected streaming mode's feature support: XlsxWriter constant-memory mode does not support tables, and requires row-order writes.

## Fill a template or patch cells

Locate the intended sheet, label, named range, or placeholder and inspect its present value. Map replacements to exact targets. A whole-cell placeholder can become a typed number/date; a placeholder embedded in a sentence should remain text. Avoid global string replacement across formulas or unrelated worksheets.

For a supported ordinary XLSX, a narrow edit can be as small as:

```python
from pathlib import Path
from openpyxl import load_workbook

source, output = Path('template.xlsx'), Path('filled.xlsx')
if output.exists():
    raise FileExistsError(output)
wb = load_workbook(source, data_only=False, rich_text=True, keep_links=True)
try:
    cell = wb['Order']['B4']
    if cell.value != '{{customer}}':
        raise ValueError('Expected customer placeholder was not found at Order!B4')
    cell.value = 'Example customer'
    cell.data_type = 's'
    wb.save(output)
finally:
    wb.close()
```

Assigning a value preserves that cell's existing style. Copy style components with `copy.copy()` when new cells should match their neighbors. For merged cells, edit the top-left anchor. Preserve template formulas, validations, conditional formatting, print settings, and hidden content outside the requested change.

## Structural edits and native objects

Inserting/deleting rows or columns, renaming sheets, moving ranges, or expanding tables can affect references elsewhere. openpyxl does not automatically maintain all dependent formulas, defined names, tables, charts, validations, conditional-format rules, merges, and drawing anchors. Formula translation changes relative references in the translated expression; it is not a workbook dependency engine. Prefer native structural operations for complex workbooks, or explicitly update and verify the affected objects in a simple known layout. Do not rewrite formulas with blanket text replacement.

Copying a worksheet also need not copy its charts, images, or other objects. Check actual API behavior before treating a copied tab as a complete clone.

An Excel Table, PivotTable, and What-If Data Table have different semantics. For requested native pivots, use an application/API that can create and refresh them; verify source range, aggregation, filters, cache, and refreshed result. A grouped table produced by pandas is a static summary, not an interactive PivotTable. Likewise, preserving VBA bytes does not validate controls or execute macros, and recalculating formulas does not refresh Power Query or external connections.

For a proven library preservation gap and a small understood OOXML change, targeted package editing can preserve untouched parts. Use mature ZIP/XML tooling, preserve relationships, namespaces, and content types, and validate in an application. Shared strings, shared/array formulas, styles, and stale calculation caches make arbitrary XML replacement unsafe; do not implement a general spreadsheet parser or calculation engine for a local edit.
