# Exploration and transformation

## EDA: learn what matters for this question

For unfamiliar data, inspect its shape, schema, representative records, and what one observation represents. Look at relevant distributions, missing values, repeated keys, date coverage, category levels, units, and unusual values. Expand to a fuller profile when the task is exploratory or early findings affect other fields.

Use plots to investigate patterns: distributions for shape and tails, time plots for gaps and changes, scatterplots for relationships, and grouped views for differences hidden by an overall average. Distinguish a candidate finding from a tested explanation. An apparent anomaly may be valid, a unit issue, a different population, or a measurement problem.

For research data, identify the experimental unit, technical versus independent replicates, pairing, clusters, detection limits, and treatment timing. For business data, identify the entity, event or snapshot grain, eligibility, and metric definition. Repeated rows can encode real repeated observations.

## ETL: make each transformation explicit

Build a rerunnable script when the task needs multi-step preparation. Preserve the raw inputs; write transformed data to a separate destination unless an overwrite was requested. Keep the source-to-output mapping and decisions needed to explain a material change. Simple work needs no extra pipeline framework or long-running service.

- **Types and units:** preserve identifier strings, normalize units explicitly, and separate parse failures from genuine missing values. Confirm date formats and timezones; local dates and UTC dates can assign an event to different periods.
- **Missing values:** distinguish unknown, not applicable, unobserved, censored, and measured zero. Choose exclusion, imputation, a separate group, or a method that handles missingness based on the question. Report consequential losses or assumptions. Fit learned imputations on training data when evaluating predictions.
- **Duplicates:** use a meaningful key and determine whether repetition is erroneous before removing it. When resolving multiple versions of an entity, use the documented version or timestamp rule.
- **Outliers:** inspect and explain them; do not delete them simply for being extreme. Use a suitable robust summary or sensitivity check when they dominate a result.
- **Joins:** state the expected relationship (one-to-one, many-to-one, etc.), check unmatched keys and row multiplication, and reconcile important totals before and after the join. In pandas, `validate=` checks cardinality and `indicator=True` exposes matches. Pandas can match null keys to each other; decide whether that behavior is valid for this dataset.
- **Aggregation and reshape:** preserve the intended observation unit. Combine rates from numerators and denominators or justified weights, rather than averaging rates indiscriminately. Keep null groups visible when they matter; for pandas this may require `groupby(..., dropna=False)`. Use `sum(min_count=1)` when an all-missing group must remain unknown.

An absent period or category is not automatically zero. Reindex for a chart only after deciding what missing combinations mean. Keep the distinction in both transformed data and labels.

## Check the result at the relevant boundaries

Track input and output row counts, excluded records, unmatched keys, and changed types where they could alter the answer. Spot-check a record through a material transformation and reconcile additive totals when conservation is expected. Explain deliberate changes such as currency conversion or deduplication.

For chunked or incremental processing, ensure the result matches the intended full-data operation: global deduplication, quantiles, distinct counts, and cross-chunk joins cannot be replaced by arbitrary combinations of per-chunk results. Use an engine or algorithm that supports the needed semantics.

When exporting CSV, retain unambiguous types and missing-value conventions. If text will be opened in spreadsheet software, prevent untrusted text from being interpreted as formulas using an output-appropriate approach, and disclose any transformation. Use `pulsara-sheets` for typed, editable workbook output.
