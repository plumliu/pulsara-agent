#!/usr/bin/env python3
"""Generate the bundled Code Mode API guide from execution inputs and owner returns."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import subprocess

from pulsara_agent.capability.builtin_catalog import execution_catalog_entry
from pulsara_agent.capability.source_query import (
    call_mcp_tool_output_schema,
    query_output_schema,
    read_mcp_resource_output_schema,
)
from pulsara_agent.conversation_kernel.decision_tools import decision_output_schema
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    collaboration_output_schema,
    run_agent_script_input_schema,
)
from pulsara_agent.conversation_kernel.tool_surface import CODE_MODE_SHARED_TOOL_NAMES
from pulsara_agent.ports.session_content import session_query_result_schema
from pulsara_agent.ports.terminal import terminal_result_schema
from pulsara_agent.ports.tool_execution import (
    thaw_tool_json_object,
    tool_business_value_schema,
)
from pulsara_agent.tools.builtins.filesystem_contracts import file_tool_result_schema
from pulsara_agent.tools.builtins.media import media_business_output_schema
from pulsara_agent.web.contracts import WEB_TOOL_NAMES, web_result_schema


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/pulsara_agent/bundled_skills/pulsara-code-mode/references/tool-calling/api"


def _business_schema(name: str) -> tuple[str, dict[str, object]]:
    """Build-time routing to existing business owners; never an execution surface."""
    if name in WEB_TOOL_NAMES:
        return "web", web_result_schema(name)
    if name in {"read_file", "write_file", "edit_file", "find_files", "search_file_content"}:
        return "files", file_tool_result_schema(name)
    if name in {"exec_process", "query_processes", "manage_process"}:
        return "terminal", terminal_result_schema(name)
    if name in {"search_sessions", "search_session_content", "read_session_content"}:
        return "history", session_query_result_schema(name)
    if name in {"view_image", "read_video_frames", "read_transcript"}:
        return "media", media_business_output_schema(name)
    if name in {"query_agents", "send_agent_message", "stop_agent", "run_agent", "wait_agent"}:
        return "agents", collaboration_output_schema(name)
    if name == "decide":
        return "decision", decision_output_schema()
    if name == "query_capabilities":
        return "mcp", query_output_schema()
    if name == "call_mcp_tool":
        return "mcp", call_mcp_tool_output_schema()
    if name == "read_mcp_resource":
        return "mcp", read_mcp_resource_output_schema()
    raise ValueError(f"No business result declaration for tools.{name}")


def _type_name(name: str) -> str:
    return "".join(part.title() for part in name.split("_"))


def _render_declarations(entries: list[dict[str, object]]) -> dict[str, str]:
    process = subprocess.run(
        ["node", str(ROOT / "tools/render_code_mode_types.mjs")],
        input=json.dumps(entries, ensure_ascii=False),
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return json.loads(process.stdout)


def _with_statuses(schema: dict, statuses: list[str]) -> dict:
    properties = schema["properties"]
    meta = properties["meta"]
    return {**schema, "properties": {**properties, "meta": {
        **meta, "properties": {**meta["properties"], "status": {"enum": statuses}},
    }}}


def _result_branches(schema: dict, statuses: set[str]) -> list[dict]:
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            return [
                result for branch in schema[keyword]
                for result in _result_branches(branch, statuses)
            ]
    status_schema = schema["properties"]["meta"]["properties"]["status"]
    return [_with_statuses(schema, selected)] if (
        selected := [status for status in status_schema["enum"] if status in statuses]
    ) else []


def _result_schema(branches: list[dict]) -> dict:
    return branches[0] if len(branches) == 1 else {"anyOf": branches}


def _plain_failure(schema: dict) -> bool:
    common = tool_business_value_schema(statuses=("ERROR", "CANCELLED"))
    content = schema["properties"]["content"]
    items = content.get("items", {})
    if set(items) == {"oneOf"} and len(items["oneOf"]) == 1:
        schema = {**schema, "properties": {
            **schema["properties"], "content": {**content, "items": items["oneOf"][0]},
        }}
    return schema == common


def _tool_page(name: str, declarations: str, *, shared_failure: bool) -> str:
    descriptor = execution_catalog_entry(name, code_mode=True).descriptor
    type_name = _type_name(name)
    extra, adapter, argument_type = "", "", f"{type_name}Args"
    if name == "run_agent":
        extra = (
            "An assignment can include up to 16 WORK materials and 16 dependencies, "
            "with at most 64 dependency edges per batch. Instructions and titles have UTF-8 "
            "limits of 65,536 and 256 bytes. See [Agent coordination](../../agent-orchestration/coordination.md).\n\n"
        )
        adapter = (
            "type Assignment = RunAgentArgs[number];\n"
            "type BatchRefs = {\n"
            "  agents: NonNullable<Assignment[\"target\"]>[];\n"
            "  works: NonNullable<Assignment[\"depends_on\"]>;\n"
            "};\n"
            "type CallAssignment = Omit<Assignment, \"instruction\" | \"depends_on\"> & {\n"
            "  instruction: Assignment[\"instruction\"] | ((refs: BatchRefs) => Assignment[\"instruction\"]);\n"
            "  depends_on?: Assignment[\"depends_on\"] | ((refs: BatchRefs) => NonNullable<Assignment[\"depends_on\"]>);\n"
            "};\n"
        )
        argument_type = "[CallAssignment, ...CallAssignment[]]"
    elif name == "decide":
        extra = (
            "`available()` checks configuration without testing service health.\n\n"
        )
    if re.search(r"\bJsonValue\b", declarations):
        declarations = (
            "type JsonValue = null | boolean | number | string | JsonValue[] "
            "| { [key: string]: JsonValue };\n\n" + declarations
        )
    if shared_failure:
        declarations += (
            f'export type {type_name}Failure = Omit<{type_name}Success, "meta"> & {{\n'
            '  meta: {status: "ERROR" | "CANCELLED"};\n'
            '};\n'
        )
    signature = (f"declare const tools: {{\n  {name}(args: {argument_type}): "
        f"Promise<{type_name}Success>;\n}};\n")
    example = ""
    if name == "decide":
        signature = (
            "declare const tools: {\n  decide: ((args: DecideArgs) => "
            "Promise<DecideSuccess>) "
            "& { available(): boolean };\n};\n"
        )
        example = (
            '\n```js\n'
            'const condition = "Does this describe an export failure?";\n'
            'const inputs = ["Export did not create a file.", "The file opens correctly."];\n'
            'if (tools.decide.available()) {\n'
            '  const result = await tools.decide({condition, inputs});\n'
            '  console.log(inputs.filter((_, i) => result.content[0].value.decisions[i]));\n'
            '} else {\n'
            '  console.log({condition, inputs});\n'
            '}\n```\n'
        )
    return (
        f"# `tools.{name}(args)`\n\n"
        f"{descriptor.description}\n\n"
        f"{extra}"
        "```ts\n" + declarations + "\n" + adapter + signature + "```\n" + example
    )



def reference_documents() -> dict[str, str]:
    """Generate only the exact current JS directory, with no native-only additions."""
    from jsonschema import Draft202012Validator

    documents = {}
    schemas, tools = [], {}
    families: dict[str, list[str]] = defaultdict(list)
    for name in sorted(CODE_MODE_SHARED_TOOL_NAMES):
        descriptor = execution_catalog_entry(name, code_mode=True).descriptor
        if descriptor.input_schema is None:
            raise ValueError(f"No argument declaration for tools.{name}")
        inputs = (run_agent_script_input_schema() if name == "run_agent"
                  else thaw_tool_json_object(descriptor.input_schema))
        family, business_output = _business_schema(name)
        Draft202012Validator.check_schema(inputs)
        Draft202012Validator.check_schema(business_output)
        type_name = _type_name(name)
        success = _result_schema(_result_branches(business_output, {"SUCCESS"}))
        failures = [
            branch for branch in _result_branches(business_output, {"ERROR", "CANCELLED"})
            if not _plain_failure(branch)
        ]
        definitions = {f"{type_name}Success": success}
        # A mixed-status owner value can keep the exact same fields on failure.
        # Reuse its declaration when status is the only difference.
        shared_failure = (
            len(failures) == 1
            and failures[0]["properties"]["meta"] == tool_business_value_schema(
                statuses=("ERROR", "CANCELLED")
            )["properties"]["meta"]
            and _with_statuses(failures[0], ["SUCCESS"]) == success
        )
        if failures and not shared_failure:
            definitions[f"{type_name}Failure"] = _result_schema(failures)
        schemas.append({"name": f"{type_name}Args", "schema": inputs, "definitions": definitions})
        tools[name] = shared_failure
        families[family].append(name)
    declarations = _render_declarations(schemas)
    for name, shared_failure in tools.items():
        type_name = _type_name(name)
        documents[f"{name}.md"] = _tool_page(
            name, declarations[f"{type_name}Args"], shared_failure=shared_failure,
        )
    lines = [
        "# Code Mode API reference", "",
        "Read the tool's inputs and result before writing a call. The declarations "
        "are TypeScript for reference; write JavaScript in the Cell. See "
        "[shared output and error rules](../results-and-errors.md).", "",
    ]
    for family, names in sorted(families.items()):
        lines.extend([f"## {family}", ""])
        lines.extend(f"- [tools.{name}]({name}.md)" for name in names)
        lines.append("")
    documents["index.md"] = "\n".join(lines)
    return documents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify checked-in guides match their owners")
    args = parser.parse_args()
    documents = reference_documents()
    if args.check:
        stale = [name for name, body in documents.items()
                 if not (OUTPUT / name).is_file() or (OUTPUT / name).read_text() != body]
        extra = sorted(path.name for path in OUTPUT.glob("*.md") if path.name not in documents)
        if stale or extra:
            raise SystemExit(f"Code Mode API reference differs: changed={stale}, extra={extra}")
    else:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for name, body in documents.items():
            (OUTPUT / name).write_text(body)
    print(f"Code Mode API reference: {len(documents) - 1} tools")


if __name__ == "__main__":
    main()
