# Analysis methods

Choose methods that answer the question and fit the observation process. Straightforward totals or comparisons need no hypothesis test. When the question requires deeper analysis, keep the definitions, assumptions, and limitations that affect the conclusion visible.

## Summaries, comparisons, and changing metrics

- Define the population, period, grain, units, exclusions, and denominator. A count of events differs from a count of people; a snapshot differs from activity during a period.
- Choose summaries for the meaning and shape of the data. Distinguish totals, means, medians, rates, quantiles, and spread. State weights and sample versus population standard deviation when relevant. A standard deviation describes variation in observations; a confidence interval describes uncertainty in an estimate.
- Compare like periods and eligible populations. Account for partial periods, timezone boundaries, cohort maturity, seasonality, changing exposure, and evolving definitions when they affect the comparison.
- For change diagnostics, verify the source and calculation first, then decompose by meaningful segments or components. Check whether an aggregate change reflects movement within groups or a shift in their mix. A component's arithmetic contribution does not establish its causal mechanism.
- Distinguish relative percent change from percentage-point change. A zero baseline makes a relative change undefined; show the underlying values rather than inventing a percentage.

## Statistical inference and experiments

Start with the target quantity and study design: independent or paired observations, clusters or repeated measurements, randomization or observational sampling, and the outcome's type. Multiple rows from one subject do not create multiple independent subjects. A normality test or a sample-size cutoff alone does not choose a valid method.

| Question / design | Candidate approach and what to check |
| --- | --- |
| Difference in means, independent groups | Welch's t procedure when appropriate; inspect independence, influential observations and uncertainty in the mean. A justified permutation or bootstrap procedure is another option |
| Before/after on the same units | Pair by identity and use a paired analysis of differences; do not sort two arrays independently and assume alignment |
| Several groups or factors | A suitable ANOVA/regression or robust/generalized alternative; planned contrasts or adjusted post-hoc comparisons as needed |
| Binary or categorical outcomes | Proportion intervals and an appropriate contingency-table or regression method; account for sparse counts and paired/clustered designs |
| Association or prediction | Choose correlation/regression for the intended relationship and data type; inspect residuals, confounding and validation, as applicable |
| Clustered, longitudinal, censored, or complex survey data | Use a method that represents that design, such as mixed models, GEE, survival or survey analysis; consult the chosen library's method guidance |

Rank-based tests do not automatically test a difference in means or medians; state the quantity and assumptions their result supports. Resampling should preserve the design, such as paired units, clusters, or time blocks. Use maintained libraries rather than implementing distribution functions or test statistics from scratch.

Report an effect or estimate in meaningful units, its uncertainty when supported, and enough method detail to interpret it. Statistical significance alone does not establish practical importance or causation. A nonsignificant result does not establish equivalence. Define the comparison family and use appropriate multiplicity control when drawing conclusions from multiple tests; label exploratory findings and data-dependent choices.

For an experiment, consider assignment, exposure, attrition, preselected outcomes, stopping rules, and uncertainty. For a causal claim from observational data, identify the design and assumptions supporting it; otherwise describe association or a hypothesis. Do not choose exclusions, endpoints, or tests merely to obtain a favorable result.

## Predictive models and forecasting

When the user needs predictions, start with a useful baseline and a validation design matching deployment. Split by time, subject, or group where required. Fit preprocessing on the training partition; prevent outcome-derived features, future information, or the same subject from leaking into evaluation.

Use task-appropriate metrics and inspect relevant subgroup or error patterns. Tune on validation data and reserve the test set for evaluation. Separate in-sample fit from held-out performance and disclose extrapolation. Forecast intervals need a defined meaning and method; scenario ranges are not automatically probability intervals.

## Proportionate verification

Recompute a consequential total, rate, contrast, or boundary case from inspectable inputs when practical. Check whether exclusions, weighting, an influential observation, or a reasonable alternative specification could change the main conclusion. Resolve material discrepancies before presenting certainty. Preserve the code, source selection and random seed where applicable so the analysis can be rerun; no fixed number of review rounds is required.
