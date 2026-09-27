# Complementary views of one analysis

Use multiple views when they help the user understand different aspects of the same result. State the question each view answers before choosing its chart. Add a view when it contributes a useful comparison, explanation, or check; remove views that repeat the same information in another shape. A direct question may need only one chart or a number.

Useful perspectives include overall structure, group sizes, distinguishing features, variation within groups, and uncertainty or stability. Select the perspectives that matter to this task. Pair a chart with exact values or a concise explanation when these make it easier to interpret. Summarize what the views jointly support, including disagreements or ambiguities.

## Example: understand customer groups across several sources

**Illustrative task:** "Use our customer, order, and refund data to group customers by purchasing behavior. Explain how the groups differ and whether they are useful for planning follow-up research."

This is a design example, not a dataset or a set of findings. Adapt the features, method, and views to the actual data; do not assume a particular number of clusters or invent segment names in advance.

### Prepare a meaningful observation

Build one row per customer for a defined observation window and as-of date. Customers contain registration dates and acquisition channels; orders contain purchase dates, amounts and categories; refunds may have several rows per order. Aggregate refunds to the order grain before joining, then aggregate orders to customers. Verify key cardinality and reconcile counts and amounts so a join cannot multiply purchases or refunds. If category data is at order-line grain, reconcile that separately before combining customer summaries.

Possible features include purchase recency, order frequency, net spending, typical order value, refund rate, and category diversity. Define cancellations, refund timing and denominators. Keep customers with no orders visible in the population accounting; an undefined recency or refund rate needs an explicit treatment. Account for different customer tenures and incomplete observation windows before comparing activity.

Choose transformations, scaling, feature weights and a clustering method appropriate to the data. Highly correlated spending features can dominate the distance if they repeat the same signal. Fit on the justified feature representation. A two-dimensional PCA projection can help display the result without becoming the clustering input by default; PCA centers features but does not itself scale their different units.

### Choose views by the question they answer

The following are options for this task, not a required six-chart template.

| Question | Useful view | Interpretation to retain |
| --- | --- | --- |
| How are customers arranged, and where do groups overlap? | PCA scatterplot colored by cluster | Label PC axes with explained variance. Two-dimensional proximity and separation may omit important structure; the projection alone cannot validate clusters. |
| How large is each group? | Horizontal bars with customer counts and shares | Name the denominator. Account for excluded customers and any noise/unassigned points separately. Bars suit size comparisons; a pie can serve a simple composition question with few categories. Cluster counts are categorical bars, not a histogram of a continuous variable. |
| What distinguishes the groups? | Feature-profile heatmap, alongside a table in original units | Label the normalization and summary statistic. Normalize each feature consistently across clusters; normalizing each cluster independently can hide magnitude differences. Show raw-unit values so color does not become the only explanation. |
| Does a group summary hide very different customers? | Boxplots or distributions of the relevant features | Show spread, overlap and unusual values. A group mean or median is not a typical value for every member. |
| Does the grouping largely reflect lifecycle or acquisition mix? | Tenure/cohort distributions or channel composition by cluster | Check this when these factors affect interpretation. Associations can motivate further investigation; they do not establish why a group behaves differently. |
| Is the grouping reasonably reliable for the intended use? | Suitable separation diagnostics and, when needed, stability comparisons | For a compatible method, evaluate silhouette in the fitted representation with an appropriate distance, not only in the display projection. Sensitivity to seeds, preprocessing or plausible parameters can reveal fragile groups; numeric cluster IDs alone cannot compare refits. No diagnostic establishes business usefulness by itself. |

The heatmap and distributions describe features used to form the clusters; they are not independent validation. If the data offers weak separation, report the overlap or instability. A useful result can be that discrete customer segments are not well supported.

### Compose an inspectable result

- Use consistent cluster labels, colors and units across the views. Give profiles descriptive names only after checking their actual features; retain a stable label for cross-reference.
- Compute features, assignments, coordinates and any diagnostics in Python. For independent views, write separate self-contained files such as `cluster-scatter.html`, `cluster-sizes.html` and `cluster-profiles.html`, then subscribe each with its own `visualization_render` call. Subscribe sequentially in the desired reading order; each file includes its own required resources.
- Combine views in one HTML when linked selection or shared controls help. Within that HTML, a group selection can highlight related marks or update subgroup summaries; separate subscriptions do not automatically share selections. Label fixed population baselines separately and provide a clear reset for interactive selections.
- UI filtering inspects the existing fit; it does not silently refit clusters or PCA. Changing the model requires new computation. If the scatterplot uses a sample, label it and keep full-population counts identifiable; point selections describe that displayed sample.
- Explain the important group differences, remaining ambiguity and what further evidence would support the user's intended action. Any conclusions, numbers and names must come from the supplied data.

## Transfer the principle to other tasks

- **A metric changed:** a time series locates the change, segment comparisons show where it occurred, and contribution or composition views distinguish volume from rate or mix changes when the metric permits that decomposition.
- **Evaluate a regression model:** held-out observed-versus-predicted values show fit, residual plots expose systematic errors, and subgroup error summaries reveal where aggregate performance hides weaknesses.
- **Compare experimental conditions:** an effect estimate with uncertainty answers the comparison, while group distributions reveal spread or outliers. Preserve the experiment's observation unit in both views.

Choose the combination that resolves the user's question. These examples do not impose a dashboard or a full diagnostic workflow on every analysis.
