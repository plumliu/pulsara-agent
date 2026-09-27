# Charts and workbook layout

This guide covers implementing comparisons and calculations under specified rules and making their charts legible. Defining metrics or statistical scope, selecting statistical methods, and interpreting results belong to the analysis task.

## Build an editable chart

Prefer a native Excel chart bound to workbook ranges when the user expects to edit the data or chart. Embed a static image only when requested or necessary for an unsupported visual, and identify the loss of editability. Do not silently replace an existing native chart with a screenshot.

1. Locate the exact categories, series names, and values. Exclude headings and total rows unless intended; ensure each series matches its category range. Confirm whether rows or columns represent series.
2. Set the chart type requested by the user. If unspecified, ordinary category comparisons can use bars/columns and time sequences can use lines; a scatter plot needs numeric X values. Choose a suitable ordinary chart directly when the data and requested comparison are clear.
3. Set titles, units, axes, label formats, and legend names. Percentage values use fraction storage with percentage formatting. Time axes and text categories are different; distinguish irregular intervals from equally spaced labels.
4. Anchor and size the chart in an intentional area. Keep it clear of editable cells and other charts. Use a table, supported dynamic range, or documented range update when new rows should appear in it.

For an existing chart, update its series and formatting through a compatible editor and verify it remains native. Check combination charts, secondary axes, pivot charts, and newer chart types against actual API support rather than reconstructing them as simpler charts by default.

## Avoid misleading display mechanics

- For bar/column charts, start the value axis at zero unless the user's requested convention has a clear labeled reason. Do not add 3D perspective or decorative effects that obscure magnitude.
- Keep category order and series colors consistent. Use labels or markers as well as color when distinctions matter.
- Define whether blank cells appear as gaps, zero, or connected points. Check how hidden/filtered rows and error cells are plotted.
- Make secondary-axis units and scaling explicit. Do not add a second axis just to force differently scaled series into a similar silhouette.
- Match number formats and visible precision to the source units. A chart title should describe the data; do not invent a conclusion or statistical relationship.

## Format the workbook

Honor the supplied template. For a new workbook, use restrained styling: clear headers, consistent fonts, readable contrast, sensible widths, wrapped long headings, and number/date formats appropriate to each column. Use an installed font that supports the actual characters, including CJK text. Keep input and calculated areas identifiable without requiring color alone.

Freeze useful header/identifier rows or columns and use filters/tables where appropriate. Avoid merging cells inside sortable data. Format the used area rather than filling entire columns with decorative styles. Check long identifiers, negative numbers, zeroes, empty cells, and `####` displays.

For print/PDF output, set the intended print area, paper orientation, repeating headers, and page breaks. Fit to page width when helpful, but do not squeeze a long table onto one unreadably small page. Screen layout and printed pagination are separate checks.

## Inspect the result

Open the output in a spreadsheet application or render the affected sheet/range. Check chart series against source cells, labels and legend clipping, tick density, title placement, contrast, chart overlap, and text size. A chart's XML or a successful PNG export cannot establish visual correctness; inspect the image. For chart-backed formulas, also check [calculated results](formulas-recalc.md).

If the user requested an Excel chart, the editable workbook is the deliverable. Preview images or PDFs supplement it when useful; they do not replace it.
