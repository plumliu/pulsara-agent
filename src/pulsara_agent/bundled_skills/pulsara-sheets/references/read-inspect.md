# Reading and inspection

## Locate the relevant content

Start with sheet names, visibility, and the question's labels or headers. Select the named sheet explicitly; the first or active sheet may be a cover page. Reported used dimensions can include formatting far beyond the data. Read ranges or stream rows rather than printing an entire workbook into context. Inspect hidden sheets or rows when they contribute to the requested result; do not remove them as cleanup.

For ordinary OOXML workbooks, the optional inspector provides metadata or a selected cell range:

```sh
uv run --script /path/to/pulsara-sheets/scripts/inspect_workbook.py input.xlsx
uv run --script /path/to/pulsara-sheets/scripts/inspect_workbook.py input.xlsx --sheet '订单' --range A1:F20
```

It uses openpyxl in read-only mode, never saves the input, and reports formula text and saved cached results separately. A range query returns only the selected cells and their reading context; the no-range call returns the workbook inventory, including package-part hints for charts, tables, pivots, connections, external links, and VBA. These hints establish presence, not whether another tool can preserve or use those features. It does not calculate formulas, render cells, or fully inspect drawing objects, formatting rules, or pivots. Use a capable application or feature-specific API for those details. For old, binary, encrypted, or text inputs, use [the format guide](formats-passwords.md).

## Interpret values correctly

| Representation | What it tells you |
| --- | --- |
| Formula, e.g. `=SUM(B2:B5)` | The stored calculation; openpyxl `data_only=False` |
| Cached result | A saved result; openpyxl `data_only=True`. It may be stale, absent, or a writer's placeholder |
| Stored value and cell type | Number, text, date, boolean, error, or blank; distinguish text identifiers from quantities |
| Number format | Display intent, such as `0.0%` or `yyyy-mm-dd`; not a formatted screen value |

`0.15` formatted as a percentage displays as 15%; a format change does not divide or multiply the value. A literal string beginning with `=` is not necessarily an intended formula. Empty cells, zero, an empty formula result, and `#N/A` are different states; do not collapse them without a stated rule. Numeric identifiers can already have lost precision before the file reaches you.

To trace a result, inspect its formula and relevant precedents, including defined names, other sheets, and external references. Cite the sheet and cell/range in the answer, and identify whether a reported number came from a cache or fresh calculation. An external reference with no accessible source cannot establish current results.

## When extracting a rectangular table

- Identify the real header row, units, date/period columns, and totals. Preserve duplicate headers until their meaning is resolved.
- Merged cells hold a value at the top-left cell. Fill-down may make sense for a known grouped header, but do not spread labels into data automatically.
- Check filters and hidden rows when the request concerns what the user can see; many data readers return all rows.
- Specify types for identifiers, dates, and missing-value markers. With pandas, defaults can reinterpret identifiers and strings such as `NA`; set `dtype` and NA handling according to the source.
- Use a DataFrame for the requested values or transformations; it is not a round-trip representation of workbook formulas, styles, charts, or review objects.

For large reads, stream with `openpyxl.load_workbook(..., read_only=True)` and close the workbook. If declared dimensions are known to be wrong, `reset_dimensions()` permits discovering actual rows; do not treat a formatting-inflated extent as the required scan size.
