# Explain findings with charts

Choose each figure for the question it answers. A number or table may be enough; add a figure when its pattern, comparison or distribution makes the result easier to understand. Publish saved images or HTML through `pulsara-viz` using the conversation's display rules.

## Match the view to the question

| Question | Useful views | Interpretation to carry into the figure |
| --- | --- | --- |
| When did the change occur? | Time series; aligned series for related measures | Label the period, frequency and comparison baseline. |
| Where does the difference come from? | Ranked bars, contribution views or small multiples | Keep comparable scales and distinguish totals, shares and rates. |
| What does the average conceal? | Histograms, boxplots or individual observations | Show group sizes, spread and unusual values; identify missing data separately. |
| How do variables relate? | Scatterplots or grouped trends | Preserve units and relevant groups; describe association separately from causation. |
| How uncertain or reliable is the comparison? | Effect estimates with intervals; residual or subgroup views for model results | State what the interval or error measure represents and which observations it covers. |

Use complementary views when they reveal distinct aspects of the same result. Keep category colors, units, denominators and comparison periods consistent. Pair a pattern with exact values when precision matters. [Multi-view analysis](multi-view-analysis.md) develops this approach with a customer-grouping example.

## Arrange the explanation

Organize figures around the questions they answer, with each explanation beside its evidence. Prefer compact figures across separate galleries over a long scrolling HTML page. Group alternative views together; keep linked views and shared controls in one HTML file. Views needed for simultaneous comparison belong side by side in one figure or beside consecutive explanations, rather than hidden on alternative gallery pages.

Lead with the finding, then show the comparisons that explain it. Give each group a distinct purpose; omit views that only repeat an established result. Choose layout and chart count to match the question.

## Make interaction informative

Use a filter, selection or slider when it lets the reader explore a relevant comparison. Keep the current selection visible, provide a clear reset, and update labels, summaries and denominators with the plotted scope. Distinguish a filtered subset from a fixed population baseline. Separate HTML files have independent controls, even within one gallery.

Compute substantive results with Python or SQL and embed the data needed for the chosen interactions. Filtering existing results does not refit a model or rerun an analysis; a changed computation requires a new result. Label sampling or aggregation, and keep conclusions tied to the displayed evidence.

## Produce the figure

Use suitable plotting libraries for saved images or HTML, and choose the internal HTML layout and drawing technique to fit the task. [HTML environment](html-environment.md) describes optional host integration and screenshot behavior. The adaptable [analyze_csv.py](../examples/analyze_csv.py) example writes a standalone HTML figure; it is not a required chart template.
