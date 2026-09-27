# Verification and delivery

Choose checks that match the actual changes. A local cell update does not require rebuilding, rendering, or auditing every sheet.

| Task | Verify |
| --- | --- |
| Read-only answer | Correct sheet/range, types, units, and formula/cache provenance; source remains unchanged |
| Fill or patch cells | Requested values/types; surrounding formulas, styles, and relevant validation rules preserved |
| Formula change | Formula survives saving; affected results calculated and reconciled where an engine is available |
| New styled workbook / chart | Data and native objects reopen correctly; inspect the affected visual output |
| Structural edit / format conversion | Dependent references, table/chart ranges, defined names, affected advanced objects, and relevant layout |
| CSV import/export | Row/column coverage, encoding, quoting, identifiers, dates, precision, and selected formula/value policy |

Check the saved output, not only the in-memory object. For edited workbooks, compare affected behavior and content with the original; OOXML serializers may rewrite package bytes without changing workbook semantics. Conversely, a ZIP that parses successfully can still contain broken formulas or missing objects. Package-part presence alone does not prove object preservation.

## Visual checks

Use a native spreadsheet application or an available renderer. For print/PDF output, LibreOffice can export a working copy:

```sh
mkdir preview
soffice -env:UserInstallation=file:///absolute/task/lo-profile --headless --convert-to pdf --outdir preview output.xlsx
pdftoppm -png -r 120 preview/output.pdf preview/page
```

Replace the profile URI with a valid file URI for a separate task-owned directory, for example using `Path(...).resolve().as_uri()`; remove it after the process exits. On macOS, `soffice` may be `/Applications/LibreOffice.app/Contents/MacOS/soffice`; Windows provides `soffice.com`. Inspect the produced pages or relevant screenshots. Check clipped text, missing glyphs, number formats, overlapping objects, chart labels, and pagination. If the workbook's existing print area excludes the changed region, inspect that region in-app or adjust print settings on a preview-only copy. Do not change the deliverable's print setup simply to generate a convenient screenshot.

A PDF can verify visible layout, but not whether a chart remains editable, a validation rule works, or a query refreshes. Verify those with the appropriate API or application. If there is no usable renderer, state the visual verification gap rather than substituting a hand-drawn approximation of the sheet.

## Deliver

Return the requested workbook/text file with a concise summary. Mention meaningful limits such as unverified calculation, unavailable external sources, or an agreed static substitute for a native object. Include preview files or reusable task code when they help the user; do not add internal inspection dumps or invented audit tabs to every deliverable.
