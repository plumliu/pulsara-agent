#!/usr/bin/env python3
"""Generate the bundled Code Mode API guide from execution inputs and owner returns."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
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


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/pulsara_agent/bundled_skills/pulsara-code-mode/references/tool-calling/api"


def _business_schema(name: str) -> tuple[str, dict[str, object]]:
    """Build-time routing to existing business owners; never an execution surface."""
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


def _tool_page(name: str, arguments: str, value: str) -> str:
    descriptor = execution_catalog_entry(name, code_mode=True).descriptor
    type_name = _type_name(name)
    extra, adapter, argument_type = "", "", f"{type_name}Args"
    if name == "run_agent":
        extra = (
            "Pass one ordered assignments array, including for a single assignment. "
            "The generated argument type describes the resolved assignments. Before dispatch, "
            "`instruction` and `depends_on` may also be synchronous JavaScript functions of "
            "preallocated `{agents,works}` Targets. They return a string and a WORK Target "
            "array respectively; they cannot call tools, await, or return a Promise.\n\n"
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
        argument_type = "CallAssignment[]"
    elif name == "decide":
        extra = (
            "`tools.decide.available()` is a local synchronous boolean, frozen at Cell "
            "startup; it does not call a service or test service health. Actual decide calls "
            "are asynchronous. The successful decisions array follows inputs order.\n\n"
        )
    declarations = arguments + "\n" + value + "\n" + adapter + (
        'type Successful<T> = T extends { meta: { status: infer S } }\n'
        '  ? "SUCCESS" extends S ? T & { meta: { status: "SUCCESS" } } : never\n'
        '  : never;\n'
    )
    if "JsonValue" in declarations:
        declarations = (
            "type JsonValue = null | boolean | number | string | JsonValue[] "
            "| { [key: string]: JsonValue };\n\n" + declarations
        )
    signature = (f"declare const tools: {{\n  {name}(args: {argument_type}): "
        f"Promise<Successful<{type_name}Value>>;\n}};\n")
    if name == "decide":
        signature = (
            "declare const tools: {\n  decide: ((args: DecideArgs) => "
            "Promise<Successful<DecideValue>>) "
            "& { available(): boolean };\n};\n"
        )
    return (
        f"# `tools.{name}(args)`\n\n"
        f"{descriptor.description}\n\n"
        f"{extra}"
        "See the [API rules](index.md) for successful resolution, actual `err.result` "
        "and explicit output. Array constraints are retained in type annotations.\n\n"
        "```ts\n" + declarations + "\n" + signature + "```\n"
    )



def reference_documents() -> dict[str, str]:
    """Generate only the exact current JS directory, with no native-only additions."""
    from jsonschema import Draft202012Validator

    documents = {}
    schemas, tools = [], []
    families: dict[str, list[str]] = defaultdict(list)
    for name in sorted(CODE_MODE_SHARED_TOOL_NAMES):
        descriptor = execution_catalog_entry(name, code_mode=True).descriptor
        if descriptor.input_schema is None:
            raise ValueError(f"No argument declaration for tools.{name}")
        inputs = (run_agent_script_input_schema() if name == "run_agent"
                  else thaw_tool_json_object(descriptor.input_schema))
        family, business_output = _business_schema(name)
        outputs = {"anyOf": [business_output,
            tool_business_value_schema(statuses=("ERROR", "CANCELLED"))]}
        Draft202012Validator.check_schema(inputs)
        Draft202012Validator.check_schema(outputs)
        type_name = _type_name(name)
        schemas.extend([{"name": f"{type_name}Args", "schema": inputs},
                        {"name": f"{type_name}Value", "schema": outputs}])
        tools.append(name)
        families[family].append(name)
    declarations = _render_declarations(schemas)
    for name in tools:
        type_name = _type_name(name)
        documents[f"{name}.md"] = _tool_page(name,
            declarations[f"{type_name}Args"], declarations[f"{type_name}Value"])
    lines = [
        "# Code Mode API reference", "",
        "Read the relevant tool page before composing calls or accessing result fields. "
        "These pages describe the actual JS directory; they do not add native tools "
        "or expose Cell variables to ROOT. TypeScript here describes interfaces; "
        "write JavaScript in a Cell, without copying export, interface or import declarations.", "",
        "Calls take one declared object or array argument. Await the complete saved "
        "business value; successful delivery of a SUCCESS value resolves normally. "
        "A thrown error may carry the actual complete business value as err.result. "
        "Its meta.status remains SUCCESS, ERROR or CANCELLED according to the business "
        "outcome; a save or delivery failure does not change a known SUCCESS. Read the "
        "error's message for the save or delivery failure. Errors "
        "without a business value omit it. Narrow each page's Value type by "
        "meta.status when handling an actual err.result. Common early rejection or "
        "execution failures contain only meta and text content; domain failures keep "
        "their declared fields. Do not repeat an action to recover its response.", "",
        'Tool-call errors have name "ToolError", code and message, and result when an '
        "actual business value exists; ordinary JavaScript errors such as TypeError "
        "need not have this interface.", "",
        "Missing fields differ from null, and content item order is preserved. "
        "JsonValue means JSON null, boolean, number, string, array or object, including "
        "unknown external MCP business values. Array length and numeric constraints "
        "remain those of the actual argument schema, including displayed annotations.", "",
        "Use console.log or a top-level return to disclose selected values to ROOT. "
        "Values held only in Cell memory are not model-visible. Native observation "
        "time, page controls and NEXT are outside these JS values. Actual image items "
        "are recognized recursively in explicit output; bare Targets remain text.", "",
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
