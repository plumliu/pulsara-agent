# Formats, text files, and passwords

## Choose a path for the actual format

Renaming an extension does not convert a file. XLSX/XLSM are ZIP-based OOXML; XLSB contains binary workbook parts; old XLS and encrypted OOXML commonly use OLE containers. Some files named `.xls` contain HTML or text. Let a capable reader identify the input.

| Input | Reading | Editing or conversion |
| --- | --- | --- |
| XLSX | openpyxl or a capable application | openpyxl for supported structures; native editor for advanced objects |
| XLSM / XLTM | Macro-aware reader; keep macros inactive | Preserve the macro-capable format and VBA when required; use a capable application for controls/signatures |
| XLTX template | OOXML reader | When making an ordinary XLSX document, clear the template flag and use the matching extension |
| Legacy XLS | xlrd or a capable application | Prefer a capable native editor when preserving XLS; convert a copy when XLSX is requested or needed |
| XLSB | A capable application, python-calamine, or pyxlsb for supported extraction | Prefer an application supporting the requested features; extraction does not imply editable round-trip support |
| ODS | LibreOffice or a suitable reader | Retain ODS if requested; verify formula and layout changes when converting to Excel |
| CSV / TSV | `csv`, pandas, or another reader with explicit import rules | Text transformation or typed XLSX creation; no workbook objects are present |

Libraries support different subsets of formulas, cached values, dates, and objects. xlrd reads legacy XLS and does not edit it. Do not treat values-only extraction as preservation of a workbook. When legacy output is requested, account for its actual format limits and surface concrete losses before choosing an alternative.

For a simple XLS → XLSX conversion, installed LibreOffice offers:

```sh
mkdir converted
soffice -env:UserInstallation=file:///absolute/task/lo-profile --headless --convert-to 'xlsx:Calc MS Excel 2007 XML' --outdir converted input.xls
```

Replace the profile URI with a valid file URI for a separate task-owned directory, for example using `Path(...).resolve().as_uri()`; remove it after the process exits. On macOS, `soffice` may be `/Applications/LibreOffice.app/Contents/MacOS/soffice`; Windows provides `soffice.com`. Prefer Excel for fidelity-sensitive Excel files. Verify sheet names, values/formulas, dates, objects, and visible layout in the converted copy. Conversion can change macros, unsupported functions, chart types, and print layout; do not promise a lossless round trip.

## CSV/TSV import and export

- Determine encoding, delimiter, quoting, decimal convention, headers, and date convention from the source. UTF-8 with BOM can help some Excel imports; do not change an explicitly required encoding. Open CSV files with `newline=''` when using Python's `csv` module.
- Preserve identifiers such as `00123`, postal codes, phone numbers, and long account numbers as text. Excel numbers cannot preserve arbitrary integer precision. `03/04/2026` needs a known date convention; number formatting alone cannot recover a date or identifier already misread.
- Keep empty fields, literal `NA`, zero, and missing-value markers distinct according to the requested rules. A negative number is not an instruction or a missing value.
- When creating XLSX, write untrusted formula-like text as literal strings. CSV quoting does not reliably prevent spreadsheet applications from evaluating leading `=`, `+`, `-`, or `@`. For CSV intended to be opened in Excel, choose a text-import route or agreed escaping that preserves the task's meaning; do not silently alter a machine-data export.
- CSV has no native types, styles, multiple sheets, formulas with caches, or charts. Specify the exported sheet and whether formula cells yield expressions or calculated values. Check those values first; do not silently export stale/blank caches as current results.

For workbook dates, respect its 1900/1904 date system and use library date conversion. Real datetimes and Excel serial numbers need deliberate conversion; retain timezone information separately when Excel's timezone-naive representation cannot carry it.

## Passwords and protection

Distinguish **a password required to open the file**, **worksheet/workbook editing protection**, and **account/rights-managed access**. Sheet protection is not file encryption; opening a workbook does not authorize removing its editing restrictions.

Use the user's supplied opening password in a capable application. If a library needs plaintext, `msoffcrypto-tool` can decrypt supported Office encryption into a buffer or a separate temporary file. Consult its support for the actual encryption scheme: OOXML and legacy XLS have different support, and a rejected password differs from unsupported encryption. Never guess or brute-force passwords.

Keep passwords in an interactive non-echoing prompt or private input channel, not command arguments, saved scripts, or logs. Preserve the encrypted original, and reopen the decrypted result in its actual format. Delete temporary plaintext after a read-only task; identify a delivered plaintext copy as unencrypted. For unsupported encryption or rights management, use the authorized native application/account or request an accessible copy.
