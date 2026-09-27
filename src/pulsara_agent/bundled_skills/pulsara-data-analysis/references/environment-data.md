# Environment and data access

## Prepare what the task needs

Check the working directory, available interpreter, and imports before installing anything. Reuse a suitable project environment without changing its dependency declarations just for an unrelated analysis. When isolation is useful, create a task-owned environment with uv, Python venv, or Conda. Pulsara does not manage a separate analysis runtime.

For example, choose an unused task environment path, then install the packages needed for the current work:

```sh
uv venv .pulsara/analysis-venv
uv pip install --python .pulsara/analysis-venv/bin/python pandas plotly
.pulsara/analysis-venv/bin/python analysis.py
```

On Windows, the interpreter is `.pulsara/analysis-venv/Scripts/python.exe`. Without uv, use `python -m venv` and the new interpreter's `-m pip`. Do not replace a populated environment or install into a system interpreter as an implicit fallback. Network or installation failures do not make unavailable capabilities usable; use an available suitable method or report the specific blocker.

For a standalone script, PEP 723 inline dependencies and `uv run --script path/to/script.py ...` are another option. The optional example uses this route. Installing a library is ordinary task preparation; it does not require adding that library to Pulsara's dependencies.

| Work | Useful choices; select only what applies |
| --- | --- |
| Small, straightforward parsing or arithmetic | Python standard library: csv, json, decimal, statistics |
| Tabular transformations | pandas; Polars or DuckDB for suitable larger or query-oriented tasks |
| Excel input | A reader for the actual format: openpyxl for OOXML, or an appropriate XLS/XLSB reader |
| Parquet / Arrow | pyarrow or an available compatible engine |
| Statistical tests, intervals, models | scipy, statsmodels; scikit-learn for predictive workflows |
| Interactive HTML charts | Plotly with the JavaScript library embedded, or another library bundled into the HTML |
| Static scientific figures | matplotlib or an appropriate plotting library; embed SVG/PNG into HTML if conversation display is useful |

Library choices are not a required stack. Check the installed version's API when a method or format is uncertain. Keep package versions with a reproducible script or environment when results depend on them.

## Read data without changing its meaning

- CSV/TSV: inspect encoding, delimiter, header rows, decimal conventions, and missing-value markers. Keep identifiers such as `00123` as strings; legitimate codes like `NA` must not become missing through an unchecked parser default. Validate date and numeric conversions instead of silently coercing failed parses.
- Excel: determine actual sheets, headers, merged regions, and formula/cached-value behavior. A cached result may be absent or stale. Do not save a values-only inspection over the source workbook; consult `pulsara-sheets` for recalculation, encryption, conversions, or preservation of workbook objects.
- JSON: identify the record collection and nested/repeated fields before flattening. Exploding an array changes the row grain. Parquet schemas provide types but do not establish the meaning of a row or measure.
- Database or API: use an available authorized connector and existing credentials. Start with the relevant schema and targeted queries; retain the query and parameters behind an important result. Source rows and documentation establish definitions, not just table names. Reads do not imply permission for database writes or external publication.

For large data, use column selection, predicate pushdown, lazy execution, aggregation, or chunking. Treat previews and samples as samples; do not present sample counts or distributions as full-data results. Increase coverage when the question requires it. Avoid duplicating an entire dataset into a browser when aggregates answer the question.

Use approved sources and output locations. Do not upload private data to a chart service or external API merely to analyze or display it. Remote URLs embedded in data do not by themselves authorize fetching them.
