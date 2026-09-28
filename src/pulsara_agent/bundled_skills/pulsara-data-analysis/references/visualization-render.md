# Charts and HTML visualization

Compute substantive results with Python or SQL, then choose a suitable display for EDA views or final findings:

- **Saved image:** use `![description](path)` in the reply; the user clicks it to open the image preview. Use an existing file's workspace-relative or absolute path.
- **Static SVG:** put the SVG directly in a fenced `svg` code block; the frontend renders it. Neither this nor an image link requires an HTML wrapper or a tool call.
- **Interactive chart, dashboard, or richer HTML layout:** write self-contained HTML and call `visualization_render` to embed it beneath the reply, following the contract below.

Use a chart library for suitable charts. A small table or numeric answer can remain ordinary Markdown.

## Produce a self-contained HTML file

Write a complete UTF-8 HTML document, normally under `.pulsara/visualizations/<descriptive-name>.html`. Data, CSS, JavaScript, fonts and images required by the display must be inline or supported embedded data. The embedded renderer cannot load CDN scripts, companion JSON files, `file:` URLs, development servers, or external network resources. It does not inject a chart library.

- Plotly can export an interactive fragment with `fig.to_html(full_html=False, include_plotlyjs=True, include_mathjax=False, config={"responsive": True, "displayModeBar": False})`; place it in a complete responsive HTML document. For several charts in one document, inline the library once and omit it from subsequent fragments.
- If using ECharts, Vega, D3, or another library, obtain the needed library through available authorized tools and embed it. Check whether the chosen chart also loads maps, workers, fonts, or other external resources.
- Embed static SVG or PNG data when it is part of the HTML layout; standalone figures can use the image/SVG forms above. Provide a separate export when the user needs a publication or reusable image.
- Use library serialization for figures. Escape untrusted labels in HTML; when embedding custom JSON into a script element, escape `<` in the serialized JSON (for example as `\u003c`) so dataset text cannot terminate the script. Insert plain text through `textContent`, not `innerHTML`.

Prefer a compact chart or a small set of related views. Mark the **one visible containing element** with `data-pulsara-visualization-root` for a chart/card; an entire page can omit it. Make the content fit narrow viewports, give charts sensible height, wrap controls, and keep wide tables in their own scroll container. Avoid fixed desktop page widths or large empty sections.

For several views of one analysis, independent charts can each have their own self-contained HTML and `visualization_render` subscription. Put the central finding first and supporting views after it. Combine charts in one HTML when linked selection, shared controls, or side-by-side comparison helps; sections or tabs are optional. If a chart starts in a hidden tab, resize it when shown. Keep category colors consistent and label each view's scope. Separate HTML displays have independent interaction state; shared filtering belongs within a combined view.

## Use the tool's actual contract

After writing the file, call the tool, for example:

```json
{"path":".pulsara/visualizations/category-trends.html","review":true}
```

Relative paths are anchored to the workspace root, not a terminal's most recent `cd`. Use the correct workspace-relative or absolute path.

- The call subscribes the file for display beneath the next assistant message without tool calls. It does not itself write HTML. You may continue editing; publication uses the file as it exists when that message is saved.
- `review=true` also attempts an immediate screenshot. Inspect the returned image for missing marks, clipped labels, wrong scales, or unusable layout. A successful subscription alone does not establish visual correctness. If no preview is produced, report a material verification gap when relevant, while keeping the existing subscription in mind.
- A screenshot reflects that instant. Recheck material visual changes before delivery. Screenshot review does not prove that filtering or other interactions work; test relevant interactions with an available browser tool when needed.
- To display multiple charts beneath that reply, save each as a distinct file and call `visualization_render` once per path. Call them sequentially when display order matters. Calling again with the same path before that reply produces one display, using the file's publication-time contents. A later edit cannot alter an already published historical display.
- Use `visualization_ref` only for a real previously published reference from the current session, and never together with `path`. A preview's `image_ref` is not an HTML reference.

Frontend hover, filtering, sorting, and series selection can operate on the embedded data. The page cannot call back to Python or load new data. More computation requires the agent to run it and publish another result. Derive filtered numbers and annotations consistently; do not keep a whole-dataset claim beside a filtered subgroup. A client filter over preaggregated data can answer only questions that preserve the necessary dimensions, weights and denominators.

## Choose an honest, useful display

Use lines for ordered time, bars for categorical comparisons, scatterplots for relationships, and suitable distribution plots for shape. Keep units and comparison periods explicit, avoid misleading axes, and distinguish missing observations from zero. Label uncertainty by its meaning; do not turn spread into a confidence interval. Use sufficient contrast and labels or shapes as well as color.

Render only the data needed to answer the question. Aggregate when appropriate; if sampling or binning is used, label it. Include a small table or exact values when they help inspection. Keep source/period and material transformation notes close to the relevant view, and make conclusions match the plotted scope.

## Optional Python example

[`examples/analyze_csv.py`](../examples/analyze_csv.py) reads a UTF-8 CSV with `date,category,value`, summarizes **additive values** by month/category, and creates a self-contained Plotly view with hover, legend selection and an inspectable result table. It uses pandas/Plotly via inline dependencies:

```sh
uv run --script /path/to/pulsara-data-analysis/examples/analyze_csv.py \
  observations.csv .pulsara/visualizations/monthly-values.html --unit "Units"
```

Use this Skill's actual directory in place of `/path/to/pulsara-data-analysis`. The example requires ISO dates and nonempty categories and finite values; it fails on unresolved required data instead of choosing a missing-value policy. It uses floating-point values, retains repeated observations, and leaves absent periods as gaps; use decimal or integer-unit calculations when exact monetary arithmetic is required. Summation is not suitable for arbitrary rates, balances, or measurements: adapt the script's aggregation and checks to the actual question. No data in the example is a user finding until supplied and computed.

The script only creates the file and reports its path. Call `visualization_render` afterward. Use, adapt, or replace this example; it is not a required schema or a general analysis engine.
