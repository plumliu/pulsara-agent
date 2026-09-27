# /// script
# requires-python = ">=3.10"
# dependencies = ["pandas>=2.2", "plotly>=6"]
# ///
"""Example: sum additive CSV observations by month/category and write inline HTML.

Input: date (YYYY-MM-DD), category (nonempty text), value (finite number).
Adapt the schema, aggregation and missing-value policy for the actual question.
"""

from __future__ import annotations

import argparse
from html import escape
import json
import math
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go


def summarize(source: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    # Keep categories such as "NA" and identifiers with leading zeroes as text.
    frame = pd.read_csv(
        source,
        encoding="utf-8-sig",
        usecols=["date", "category", "value"],
        dtype="string",
        keep_default_na=False,
    )
    if frame.empty:
        raise ValueError("No observations; inspect the input before choosing a display.")
    if frame.apply(lambda column: column.str.strip().eq("")).any().any():
        raise ValueError("Missing required data; choose a handling policy before aggregation.")
    frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
    frame["value"] = pd.to_numeric(frame["value"], errors="raise").astype(float)
    if frame["date"].isna().any() or not all(math.isfinite(x) for x in frame["value"]):
        raise ValueError("Dates and values must be valid and finite.")
    frame["month"] = frame["date"].dt.to_period("M").astype(str)
    summary = (
        frame.groupby(["month", "category"], as_index=False, sort=True, dropna=False)
        .agg(total=("value", "sum"), observations=("value", "size"))
    )
    if not all(math.isfinite(x) for x in summary["total"]):
        raise ValueError("Aggregation produced a nonfinite total; inspect the values.")
    return frame, summary


def create(source: Path, output: Path, *, unit: str = "Value") -> dict[str, object]:
    if output.suffix.lower() != ".html":
        raise ValueError("Choose an .html output for visualization_render.")
    if output.exists():
        raise FileExistsError(f"Choose a new output path: {output}")
    frame, summary = summarize(source)
    months = pd.period_range(frame["date"].min(), frame["date"].max(), freq="M").astype(str).tolist()
    figure = go.Figure()
    for category, rows in summary.groupby("category", sort=True):
        monthly = rows.set_index("month")["total"].reindex(months)
        figure.add_trace(
            go.Bar(
                name=escape(str(category)),
                x=months,
                y=[None if pd.isna(x) else float(x) for x in monthly],
                hovertemplate="%{x}<br>%{y:,.4g}<extra>%{fullData.name}</extra>",
            )
        )
    figure.update_layout(
        template="plotly_white",
        barmode="group",
        autosize=True,
        margin={"l": 56, "r": 16, "t": 12, "b": 48},
        font={"family": "system-ui, sans-serif", "color": "#30343b"},
        paper_bgcolor="#fbfaf6",
        plot_bgcolor="#fbfaf6",
        colorway=["#5574a3", "#b57638", "#49887b", "#9a6895"],
        legend={"orientation": "h", "y": 1.12, "x": 0},
        xaxis={"title": "Month", "type": "category", "categoryorder": "array", "categoryarray": months},
        yaxis={"title": escape(unit), "rangemode": "tozero"},
    )
    chart = figure.to_html(
        full_html=False,
        include_plotlyjs=True,
        include_mathjax=False,
        div_id="monthly-chart",
        default_height="360px",
        config={"responsive": True, "displayModeBar": False},
    )
    table = summary.to_html(index=False, escape=True, border=0, classes="summary")
    start = frame["date"].min().date().isoformat()
    end = frame["date"].max().date().isoformat()
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monthly totals</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #fbfaf6; color: #30343b; font: 14px/1.55 system-ui,sans-serif; }}
main {{ width: 100%; max-width: 960px; padding: 20px; margin: auto; }}
h1 {{ font-size: 22px; margin: 0 0 6px; }}
p {{ margin: 6px 0; overflow-wrap: anywhere; }}
.note {{ color: #62666d; font-size: 12px; }}
#monthly-chart {{ width: 100%; }}
summary {{ cursor: pointer; padding: 10px 0; }}
.table-wrap {{ overflow: auto; max-height: 280px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th,td {{ border-bottom: 1px solid #ddd9ce; padding: 7px 12px; text-align: left; white-space: nowrap; }}
@media(max-width: 500px) {{ main {{ padding: 12px; }} h1 {{ font-size: 19px; }} }}
</style></head><body><main data-pulsara-visualization-root>
<h1>Monthly totals</h1>
<p class="note">Source: {escape(source.name)} · {len(frame):,} observations · {start} to {end}</p>
<p class="note">Sum of observed values, in {escape(unit)}. Unobserved periods remain gaps.</p>
{chart}
<p class="note">Hover for values. Select a legend item to hide or show its series.</p>
<details><summary>Inspect calculated values</summary><div class="table-wrap">{table}</div></details>
</main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(html)
    return {
        "html_path": str(output.resolve()),
        "input_rows": len(frame),
        "result_rows": len(summary),
        "period": [start, end],
        "next_step": "Call visualization_render with path set to html_path; use review=true for a preview.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--unit", default="Value", help="Label for the additive measure")
    args = parser.parse_args()
    print(json.dumps(create(args.source, args.output, unit=args.unit), ensure_ascii=False))


if __name__ == "__main__":
    main()
