"""Shipped capability instructions provide executable current-contract examples."""

import json
from pathlib import Path
import re

import pytest

from pulsara_agent.capability.management_intent import (
    parse_capability_management_intent,
)
from pulsara_agent.capability.source_query import parse_query
from pulsara_agent.hooks.config_parser import parse_hook_declarations


@pytest.mark.parametrize(
    "relative_path",
    [
        "pulsara-mcp-installer/SKILL.md",
        "pulsara-mcp-installer/references/connection-configuration.md",
        "pulsara-plugin-installer/SKILL.md",
        "pulsara-plugin-installer/references/hook-source-management.md",
        "pulsara-skill-installer/SKILL.md",
    ],
)
def test_bundled_capability_examples_use_the_actual_native_parsers(relative_path):
    root = Path(__file__).parents[1] / "src/pulsara_agent/bundled_skills"
    blocks = re.findall(r"```json\n(.*?)\n```", (root / relative_path).read_text(), re.S)
    assert blocks
    for source in blocks:
        value = json.loads(source)
        if "hooks" in value:
            declarations = parse_hook_declarations(source.encode())
            assert declarations.definitions and not declarations.diagnostics
        elif "transport" in value:
            parsed = parse_capability_management_intent({
                "target": {"kind": "MCP_CONNECTION", "config_path": "/project/.pulsara/mcp.yaml", "server_id": "docs"},
                "action": "ADD_LOCAL_MCP",
                "config": value,
            })
            assert parsed.to_dict()["config"] == value
        elif "action" in value:
            assert parse_capability_management_intent(value).to_dict() == value
        else:
            assert parse_query(value) == value
