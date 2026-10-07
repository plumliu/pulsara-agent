"""Measure built-in contracts with Pulsara's own heuristic (no provider calls).

Run before and after a contract edit with --output pointing to separate JSON files.
The function JSON measure includes the shared OpenAI function definition,
but no API wrapper or request envelope.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.llm.adapters.openai.function_tools import openai_function_definition
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.ports.tool_execution import thaw_tool_json_object


def measure():
    estimator = PulsaraHeuristicTokenEstimatorV3()
    rows = {}
    for entry in builtin_tool_catalog():
        descriptor = entry.descriptor
        tool = ToolSpec(
            descriptor.name,
            descriptor.description,
            thaw_tool_json_object(descriptor.input_schema),
        )
        function = openai_function_definition(tool)
        rows[tool.name] = {
            "function_json_tokens": estimator.estimate_json(function),
            "function": function,
        }
    return {
        "estimator": "pulsara_heuristic:v3",
        "function_method": "estimate_json(openai_function_definition); no API wrapper/envelope",
        "tools": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = measure()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        f"Measured {len(report['tools'])} built-ins with {report['estimator']}: {args.output}"
    )
