# Formulas and recalculation

## Construct editable calculations

Use formulas for values meant to update with user inputs. Put user-adjustable assumptions in labeled cells and reference them. Use understandable expressions with consistent ranges, units, and periods; add useful intermediate calculations when they make dependencies easier to follow. Preserve valid template conventions during a narrow edit.

- OOXML formula-writing APIs generally expect English function names and comma separators, even for localized Excel interfaces. Verify newer functions and dynamic arrays against the chosen writer and target application; do not impose a permanent function blacklist.
- Anchor only the row/column that should stay fixed when filling formulas. Quote sheet names and escape apostrophes using the library's quoting helper.
- Exclude headers and totals from detail sums where appropriate. Table expansion, filtered totals (`SUBTOTAL` versus `SUM`), and inclusive date boundaries need explicit behavior.
- Missing input, a zero amount, and a failed lookup have different meanings. Use `IFERROR` only for an intended fallback, not to conceal broken references or unsupported functions.
- Do not overwrite spill cells, array-formula regions, or What-If Data Tables one cell at a time. Use an API that understands the affected structure.

## Distinguish four states

| State | Evidence |
| --- | --- |
| Formula saved | Reopened formula text is present |
| Recalculation requested | Calculation flags ask an application to calculate later |
| Saved cache present | A stored value exists; it can be stale or a placeholder |
| Result verified | A compatible engine calculated it and relevant outputs match independent expectations |

openpyxl does not evaluate formulas. XlsxWriter can store an explicitly supplied formula result and otherwise commonly writes a zero placeholder with recalculation requested. Neither behavior proves an engine evaluated the workbook. Never replace formulas with computed constants merely to fill caches, or invent cached results to make a file appear verified.

## Calculate using an available engine

Prefer the target application for Excel-specific functions and objects. Open a working copy with macros disabled and link updates controlled, recalculate, save, and reopen to read both formulas and results. Refreshing pivots, queries, and external sources is a separate action; use the user's requested refresh scope and available credentials.

For ordinary compatible XLSX files, installed LibreOffice can recalculate a copy through XLSX export. Use a fresh output directory and isolated temporary profile. For example, adapt these paths in a suitable shell:

```sh
mkdir recalculated
soffice -env:UserInstallation=file:///absolute/task/lo-profile --headless --convert-to 'xlsx:Calc MS Excel 2007 XML' --outdir recalculated work.xlsx
```

On macOS the executable may be `/Applications/LibreOffice.app/Contents/MacOS/soffice`; on Windows use the installed `soffice.com`. Build a valid file URI for the profile, for example with `Path(...).resolve().as_uri()`, rather than interpolating unescaped spaces. Use only a task-owned profile and clean it after the process exits. Do not terminate another user session to release a file lock.

Check for a newly created, nonempty output; a successful process exit alone does not establish conversion. Compare formulas, key results, and affected objects after export. LibreOffice may translate formulas or change features, so use the exported copy as the deliverable only when it meets preservation requirements. Do not use this ordinary-XLSX route to strip macros from XLSM.

## Check the calculated result

Reopen one view with formulas and another with `data_only=True`; never save the latter over the workbook. Check changed formulas, their dependent outputs, and meaningful totals/boundaries. Look for error-typed cells such as `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, and `#SPILL!`; a literal string containing an error label is not necessarily an Excel error.

Compare affected results against independently computed values or known cases. Preserve intentional error states and distinguish pre-existing errors from introduced failures. For an interactive model, change a representative input in a disposable copy, recalculate, and confirm dependent outputs move correctly.

If an engine is unavailable or does not support the affected formulas, deliver editable formulas when useful and state exactly which calculated results remain unverified. Independently computed control totals can still help, but static scans, recalculation flags, and placeholder caches do not establish calculation success.
