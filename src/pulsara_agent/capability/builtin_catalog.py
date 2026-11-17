"""Single descriptor, binding, permission, and taxonomy catalog for built-ins."""

from __future__ import annotations

from pulsara_agent.tools.builtins.media import MEDIA_TOOL_NAMES, media_parameters_schema
from pulsara_agent.web.contracts import WEB_TOOL_NAMES, web_parameters_schema

from pulsara_agent.scheduling.requests import manage_schedule_schema, query_schedules_schema
from pulsara_agent.capability.source_query import query_input_schema, capability_target_schema
from pulsara_agent.tools.builtins.filesystem_contracts import (
    read_file_parameters_schema, write_file_parameters_schema,
    search_content_parameters_schema, find_files_parameters_schema, edit_file_parameters_schema,
)
from pulsara_agent.ports.session_content import (
    SESSION_QUERY_TOOL_NAMES,
    session_query_input_schema,
)

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any, Literal
from pulsara_agent.identity import ID_PAYLOAD_PATTERN

from pulsara_agent.capability.management_intent import capability_management_input_schema

from pulsara_agent.memory.inputs import memory_input_schema
from pulsara_agent.primitives.plan_workflow import plan_enter_schema, plan_question_schema, plan_exit_schema

from pulsara_agent.capability.descriptor import (
    BuiltinToolAdvertisePolicy,
    BuiltinToolDescriptor,
    BuiltinToolDomainKind,
)
from pulsara_agent.capability.result_contracts import result_render_contract_for_tool
from pulsara_agent.ports.tool_execution import ToolInvocationOwnerKind
from pulsara_agent.ports.tool_registry import (
    BuiltinToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
    tool_binding_contract_identity_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.todo import (
    MAXIMUM_TODO_ITEMS,
)
from pulsara_agent.ports.terminal import (
    EXEC_PROCESS_TOOL_DESCRIPTION,
    MANAGE_PROCESS_TOOL_DESCRIPTION,
    QUERY_PROCESSES_TOOL_DESCRIPTION,
    exec_process_input_schema,
    manage_process_input_schema,
    query_processes_input_schema,
)
from pulsara_agent.code_mode.contracts import (
    exec_cell_parameters_schema,
    manage_cell_parameters_schema,
    query_cells_parameters_schema,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    query_agents_input_schema,
    run_agent_parameters_schema,
    wait_agent_parameters_schema,
    wait_message_parameters_schema,
    target_schema,
)





_ACTION_PERMISSION_OVERRIDE_SPECS: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
    "manage_process": tuple(
        ("action", action, "terminal_process_observe", True)
        for action in ("wait",)
    ),
    "manage_cell": (("action", "wait", "subagent_runtime", True),),
}


def object_schema(*, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }




def builtin_tool_descriptors() -> tuple[BuiltinToolDescriptor, ...]:
    return tuple(_BUILTIN_DESCRIPTORS[name] for name in sorted(_BUILTIN_DESCRIPTORS))


def _descriptor(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    provider_kind: BuiltinToolDomainKind = BuiltinToolDomainKind.BUILTIN,
    is_read_only: bool,
    permission_category: str,
    is_destructive: bool = False,
    is_open_world: bool = False,
) -> BuiltinToolDescriptor:
    return BuiltinToolDescriptor(
        id=f"{provider_kind.value}:{name}",
        name=name,
        description=description,
        input_schema=input_schema,
        namespace=None,
        provider_kind=provider_kind,
        provider_id=provider_kind.value,
        is_model_callable=True,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        is_open_world=is_open_world,
        permission_category=permission_category,
        result_render_contract=result_render_contract_for_tool(name),
        advertise_policy=BuiltinToolAdvertisePolicy.DIRECT,
        metadata={"source": "explicit_builtin_descriptor"},
    )





_BUILTIN_DESCRIPTORS: dict[str, BuiltinToolDescriptor] = {
    **{name: _descriptor(name=name, description=description,
        input_schema=web_parameters_schema(name), is_read_only=True,
        permission_category="external_read") for name, description in (
        ("web_search", "Search webpages for sources and relevant summaries. Pass query, optional count (1–20, default 10), include_domains, exclude_domains, after and before (YYYY-MM-DD). Domains include their subdomains; exclusions take precedence. Returns ranked URL Targets with available title, snippet and date. Search summaries are not page contents; use web_fetch to read selected sources."),
        ("web_fetch", "Read extracted Markdown from one HTTP(S) webpage. Pass target:URL. Returns the requested URL Target, available title and extracted text."),
    )},
    **{name: _descriptor(name=name, description=description,
        input_schema=media_parameters_schema(name), is_read_only=True,
        permission_category="filesystem_read") for name, description in (
        ("read_video_frames", "Read local video frames. Pass target:PATH, from?, to?, action? and crop?. overview locates content across a range; detail reads text or objects within explicit from/to at most two seconds apart; motion examines changes and order. detail samples start, midpoint and just before the end. Decimal seconds and normalized crop are supported. Returned indices describe the images that follow them."),
        ("read_transcript", "Read timed original text from local video or audio. Pass target:PATH with optional from/to seconds. Embedded text subtitles are preferred; cached or configured ASR fills gaps. The complete requested range is acquired before display pagination. Transcription does not verify subtitles against speech or interpret music."),
        ("annotate_media", "ROOT direct only. As you review a local video or audio file, share your own observations and interpretations with the user as a timeline note on the relevant passage. The user can read it while you continue reviewing and click it to revisit that passage. Provide target, from/to seconds and nonblank plain text. Notes are append-only and leave playback and the source file unchanged."),
    )},
    "open_in_pulsara": _descriptor(
        name="open_in_pulsara", description=(
            "ROOT only. Show local PATH files, URL webpages, or AGENT read-only conversations "
            "in this conversation's tabs. Group related targets; the last accepted target is "
            "selected. Invalid items return their zero-based index while valid items proceed. "
            "Results acknowledge requests, not completed display or webpage contents."
        ),
        input_schema={'additionalProperties': False,
 'properties': {'targets': {'items': {'anyOf': [{'additionalProperties': False,
                                                 'properties': {'path': {'description': 'File '
                                                                                        'path; '
                                                                                        'workspace-relative, '
                                                                                        'absolute, '
                                                                                        'and ~ '
                                                                                        'paths are '
                                                                                        'supported.',
                                                                         'minLength': 1,
                                                                         'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'PATH'}},
                                                 'required': ['kind', 'path'],
                                                 'type': 'object'},
                                                {'additionalProperties': False,
                                                 'properties': {'url': {'description': 'HTTP(S) '
                                                                                       'URL '
                                                                                       'without '
                                                                                       'embedded '
                                                                                       'credentials.',
                                                                        'minLength': 1,
                                                                        'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'URL'}},
                                                 'required': ['kind', 'url'],
                                                 'type': 'object'},
                                                target_schema('AGENT')]},
                            'minItems': 1,
                            'type': 'array'}},
 'required': ['targets'],
 'type': 'object'}, is_read_only=True, permission_category="filesystem_read",
    ),
    "query_capabilities": _descriptor(
        name="query_capabilities",
        description=(
            'Discover installed capabilities. kind is required: SKILL, HOOK, MCP, or PLUGIN. List SKILL, HOOK or MCP with kind, scope? and parent?; list PLUGIN with kind and scope?. Inspect with kind and target only. MCP also accepts query to search tool names, descriptions and services; do not combine query with target. Tool hits include input_schema and invocation.target for call_mcp_tool; a service hit does not expand its tools. Read Skill or Hook files when details are needed. Subagents may query MCP only. Omit unused fields; null is not omission.'
        ),
        input_schema=query_input_schema(),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "manage_capability": _descriptor(
        name="manage_capability",
        description=(
            "Install and manage capabilities. Use only the selected action's fields; ? means optional, not null. INSTALL_LOOSE_SKILL(target:PATH, scope); SET_LOOSE_SKILL_ENABLED(target:SKILL, enabled); REMOVE_LOOSE_SKILL(target:SKILL). TRUST_HOOK_SOURCE(target:HOOK_SOURCE); REVOKE_HOOK_TRUST(target:HOOK_SOURCE); SET_HOOK_SOURCE_ENABLED(target:HOOK_SOURCE, enabled). ADD_LOCAL_MCP(target:MCP_CONNECTION, config?); UPDATE_MCP_CONNECTION(target:MCP_CONNECTION, config?); REMOVE_LOCAL_MCP(target:MCP_CONNECTION); AUTHORIZE_MCP(target:MCP_CONNECTION); CLEAR_MCP_AUTHORIZATION(target:MCP_CONNECTION). INSTALL_PLUGIN(target:PATH, scope, replace?, source_format?); SET_PLUGIN_ENABLED(target:PLUGIN, enabled); REMOVE_PLUGIN(target:PLUGIN). Copy exact installation targets from query_capabilities. Omit config to open the connection editor; an update object changes only supplied fields, while Plugin config:null restores package defaults. Enter credentials privately in the editor. Hook trust and enablement are separate. A successful change needs no routine reload."
        ),
        input_schema=capability_management_input_schema(),
        is_read_only=False,
        permission_category="plugin_control",
    ),
    "reload_capabilities": _descriptor(
        name="reload_capabilities",
        description=(
            'Refresh all local Skill, MCP and Hook sources for future use, including USER and '
            'current-workspace Hook definitions and trust settings. Call with {} after external '
            'source changes. Installation, enablement and trust are managed with manage_capability.'
            ' Current instructions, tools and earlier messages stay fixed. ROOT agent only, in '
            'bypass-permissions mode.'
        ),
        input_schema=object_schema(properties={}, required=[]),
        is_read_only=True,
        permission_category="plugin_control",
    ),
    'call_mcp_tool': _descriptor(
        name='call_mcp_tool',
        description='Call an MCP tool using the invocation.target returned by query_capabilities. Supply arguments matching its input_schema. Read the relevant parameter and available output descriptions before composing calls; output_schema may be absent. The reference can be reused in this conversation, including Code Mode. If it is stale or unavailable, query the tool again; do not automatically repeat a call whose effect is uncertain.',
        input_schema={'type': 'object',
 'properties': {'target': {'type': 'object',
                           'properties': {'kind': {'const': 'MCP_TOOL_REF'},
                                          'tool_ref': {'description': 'Canonical mcpref ID copied from '
                                                                      'invocation.target, scoped '
                                                                      'to this agent and '
                                                                      'conversation.',
                                                       'pattern': '^mcpref:' + ID_PAYLOAD_PATTERN + '$',
                                                       'type': 'string'}},
                           'required': ['kind', 'tool_ref'],
                           'additionalProperties': False},
                'arguments': {'description': 'Arguments matching the inspected input_schema; use '
                                             '{} when no inputs are needed.',
                              'type': 'object'}},
 'required': ['target', 'arguments'],
 'additionalProperties': False},
        is_read_only=False,
        permission_category="mcp_dynamic",
    ),
    "read_mcp_resource": _descriptor(
        name="read_mcp_resource",
        description="Discover or read MCP resources. target:MCP_CONNECTION lists that service's resources and URI templates; no uri is accepted. target:MCP_RESOURCE reads its exact URI; supply values for template variables in the URI before reading. Resource listings are metadata, not resource content. Omit unused fields; use the returned content in its given order.",
        input_schema=object_schema(properties={"target": {"oneOf": [
            capability_target_schema("MCP_CONNECTION"), capability_target_schema("MCP_RESOURCE")]}}, required=["target"]),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "read_file": _descriptor(
        name="read_file",
        description=(
            'Read a UTF-8 text file, or continue a saved tool result. PATH: target, offset?, limit?; offset is a nonzero LF line number, negative from the end, and limit is a positive number of lines. NEXT: target only, copied unchanged from a tool result. NEXT continues that frozen result without running its tool again; it cannot be used in Code Mode. Paths may be workspace-relative, absolute or ~ paths. Omit unused fields; null is invalid.'
        ),
        input_schema=read_file_parameters_schema(),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "view_image": _descriptor(
        name="view_image",
        description=(
            ('Read a local image, render a local HTML file, or reread a known image reference. Pass only target: PATH or IMAGE_REF. Image references may come from any conversation. Local HTML rendering produces an image; it does not publish the page. Readable image content is returned in order with its explanation.')
        ),
        input_schema=media_parameters_schema("view_image"),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    'search_file_content': _descriptor(
        name='search_file_content',
        description='Search file text with a regular expression and return matching lines with paths and line numbers. Pass pattern, optional target:PATH, file_glob and output_mode. An omitted target searches the workspace; file_glob filters relative paths. output_mode selects matching lines, matching file paths, or counts. The complete search result is saved before display pagination. Use find_files to search names instead.',
        input_schema=search_content_parameters_schema(),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "find_files": _descriptor(
        name="find_files",
        description='Find files and directories by name or path pattern. Pass glob and optional target:PATH; an omitted target searches the workspace. Results contain paths, not file contents. Use search_file_content for text inside files.',
        input_schema=find_files_parameters_schema(),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "edit_file": _descriptor(
        name="edit_file",
        description=(
            "Edit an existing UTF-8 text file. Exact replacement: target:PATH, old_text, new_text; old_text must match exactly once, counting overlapping occurrences. Whole-file replacement: target:PATH, content. Use only one branch's fields. Empty content empties the file. Reading first is optional. The result contains the complete diff; a concurrent file change detected before commit rejects the edit."
        ),
        input_schema=edit_file_parameters_schema(),
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "write_file": _descriptor(
        name="write_file",
        description='Create a new file without overwriting an existing path. Pass target:PATH and exactly one of content or tool_result_target. content writes the supplied text; tool_result_target:SESSION_ENTRY exports that complete saved tool result, including unread pages, as UTF-8. A NEXT target, ordinary message or Cell output is not an export source. The result reports the created path and bytes written.',
        input_schema=write_file_parameters_schema(),
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "exec_process": _descriptor(
        name="exec_process",
        description=EXEC_PROCESS_TOOL_DESCRIPTION,
        input_schema=exec_process_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        is_open_world=True,
    ),
    "query_processes": _descriptor(
        name="query_processes",
        description=QUERY_PROCESSES_TOOL_DESCRIPTION,
        input_schema=query_processes_input_schema(),
        is_read_only=True,
        permission_category="terminal_process_observe",
    ),
    "manage_process": _descriptor(
        name="manage_process",
        description=MANAGE_PROCESS_TOOL_DESCRIPTION,
        input_schema=manage_process_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        is_destructive=True,
        is_open_world=True,
    ),
    "search_sessions": _descriptor(
        name="search_sessions",
        description=(
            'Find saved conversations by title or dialogue keywords; omit query to list conversations by activity. Optional lifecycle selects open or archived conversations. Body matches in this conversation cover earlier content outside the current context; other accessible conversations use saved dialogue. SESSION targets identify conversations; SESSION_ENTRY targets anchor dialogue hits. Images are references, not automatically loaded.'
        ),
        input_schema=session_query_input_schema("search_sessions"),
        is_read_only=True,
        permission_category="session_read",
    ),
    "search_session_content": _descriptor(
        name="search_session_content",
        description=(
            'Search saved dialogue by relevance. Pass target:SESSION and query. Results include user messages and steers, assistant commentary and final answers; tools and private reasoning are excluded. Each hit anchors a saved entry for read_session_content. Use view_image to inspect a referenced image.'
        ),
        input_schema=session_query_input_schema("search_session_content"),
        is_read_only=True,
        permission_category="session_read",
    ),
    "read_session_content": _descriptor(
        name="read_session_content",
        description=(
            'Read saved dialogue. Pass target and required nonzero limit. SESSION: limit must be negative and reads from the latest message backwards. SESSION_ENTRY: positive reads forwards from that entry, negative reads backwards; either includes the anchor. Results preserve the chosen message order and labels. Tool calls, tool results and private reasoning are excluded. Images remain references until view_image is called.'
        ),
        input_schema=session_query_input_schema("read_session_content"),
        is_read_only=True,
        permission_category="session_read",
    ),
    "query_schedules": _descriptor(
        name="query_schedules",
        description=(
            'Query schedules in this conversation. List: omit target, optional status ALL, ACTIVE or PAUSED. Inspect: target:SCHEDULE only. Both return full saved configurations; revision is managed automatically. Omit unused fields; null is invalid.'
        ),
        input_schema=query_schedules_schema(),
        is_read_only=True,
        permission_category="scheduled_task_read",
    ),
    "manage_schedule": _descriptor(
        name="manage_schedule",
        description=(
            "Manage schedules in this conversation. Use only the selected action's fields: create(values, status?); update(target:SCHEDULE, values); pause(target); resume(target); delete(target); switch(target, replacement). create values contain name, prompt, schedule and optional timezone. update changes only supplied values; a supplied schedule replaces the entire rule. replacement is {kind:existing,target:SCHEDULE} or {kind:create,values}. Query before modifying an existing schedule. Pausing, switching or deleting cancels its unconsumed queued inputs; already running replies continue. Execution permissions are taken from the current turn when creating a schedule."
        ),
        input_schema=manage_schedule_schema(),
        provider_kind=BuiltinToolDomainKind.BUILTIN,
        is_read_only=False,
        permission_category="scheduled_task_write",
    ),
    "todo": _descriptor(
        name="todo",
        description=(
            "Replace the current task's ordered progress checklist with the supplied items. Include every "
            "item to retain; items=[] clears it. Use at most one in_progress item, and update completed and "
            "next active items together. The ROOT checklist clears when execution becomes idle and remains "
            "while waiting for user input. This checklist is separate from the delegated tasks shown in the "
            "Tasks tab."
        ),
        input_schema=object_schema(
            properties={
                "items": {
                    "type": "array",
                    "maxItems": MAXIMUM_TODO_ITEMS,
                    "description": (
                        "Complete ordered progress list; an empty list clears it."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "Unique nonempty task label on one plain line, without surrounding whitespace."
                                ),
                            },
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                ],
                            },
                        },
                        "required": ["text", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            required=["items"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True,
        permission_category="agent_local",
    ),
    "query_agents": _descriptor(
        name="query_agents",
        description=(
            "Query collaborating agents and work. No target lists agents; target:AGENT lists that agent's work; target:WORK reads one work's submitted answer or stop explanation. Pass only target when selecting one object. A missing title does not establish that the agent is idle."
        ),
        input_schema=query_agents_input_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "send_agent_message": _descriptor(
        name="send_agent_message",
        description='Send a message to target:AGENT, with message. An active recipient receives it during its current work; an idle recipient is not started by the message. A recipient without a started work cannot receive it. Unconsumed messages are rejected when the intended recipient turn ends; they are not redirected to a later task.',
        input_schema=object_schema(properties={"target": target_schema("AGENT", allow_root=True),
            "message": {"type": "string", "minLength": 1, "maxLength": 16384}}, required=["target", "message"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "wait_message": _descriptor(
        name="wait_message",
        description="Wait for another agent's message in a subagent conversation. Optional timeout_seconds bounds this wait; a normal timeout succeeds. Message text arrives through the conversation input, separately from this tool's short result. This wait does not start or reassign work.",
        input_schema=wait_message_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "stop_agent": _descriptor(
        name="stop_agent",
        description="Stop target:AGENT or target:WORK. An agent target stops its running and queued work; a work target stops only that work. Submitted tool calls settle their actual effects. meta reports queued_work_count, running_work_count, completed_work_count and stopped_work_count for the listed work. Stopping does not undo effects already performed.",
        input_schema=object_schema(properties={"target": {"oneOf": [target_schema("AGENT"), target_schema("WORK")]}}, required=["target"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "exec_cell": _descriptor(
        name="exec_cell",
        description="Start a JavaScript Cell. Pass exactly one of code (inline JavaScript) or target:PATH (a UTF-8 source file). Read the needed tool interfaces before writing the business script. Tool calls return full saved business values; console.log and a non-undefined top-level return send selected values separately to the conversation. This tool's result reports the Cell start, not its eventual output.",
        input_schema=exec_cell_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "query_cells": _descriptor(
        name="query_cells",
        description='Query Code Mode Cells. Omit target to list retained Cells; pass target:CELL only for its state, pending_calls and complete executed source. Cell output is delivered separately. There are no source offsets or query cursors.',
        input_schema=query_cells_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "manage_cell": _descriptor(
        name="manage_cell",
        description="Control target:CELL. wait(target, action:wait, yield_time_ms?) waits briefly for state changes; stop(target, action:stop) stops further script execution. Use only the selected action's fields. Submitted tool calls still settle actual effects. Output already delivered is not withdrawn.",
        input_schema=manage_cell_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "enter_plan": _descriptor(
        name="enter_plan",
        description=(
            'Enter read-only planning mode. Call alone with {}. The conversation continues automatically in a new read-only turn. Calling again while already planning succeeds without another mode change.'
        ),
        input_schema=plan_enter_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "ask_plan_question": _descriptor(
        name="ask_plan_question",
        description=(
            'Ask one question while planning. Pass question and optional options; each option has label, description? and recommended?. The user can always answer freely. The call waits for an answer or cancellation; after ten minutes it returns cancellation and planning continues so you can choose the next step. Call alone.'
        ),
        input_schema=plan_question_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "exit_plan": _descriptor(
        name="exit_plan",
        description=(
            "Leave planning or submit a plan for approval. With plan: submit its complete Markdown, optional summary names the saved file; wait for the user's decision. Without plan: leave planning without requesting approval or creating a plan file. Revision, cancellation and ten-minute timeout continue the current read-only turn. Approval or no-plan exit resumes the previous permission mode in a new turn. Call alone."
        ),
        input_schema=plan_exit_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    'query_memory': _descriptor(
        name='query_memory',
        description=(
            "Search or read saved memory. Search: query and optional limit across all kinds. Exact read: target:MEMORY only. Use one branch's fields, without null. Both return complete memory objects and timestamps; the runtime keeps the concurrency condition for later edits and deletion."
        ),
        input_schema=memory_input_schema('query_memory'),
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=True,
        permission_category="memory_read",
    ),

    "remember": _descriptor(
        name="remember",
        description=(
            'Save one reusable memory. Pass context_kind, kind, title and statement when it is a profile or fact. A response preference is entirely in title; its statement is stored empty. Title and kind are fixed after creation. The result includes the new Target and up to three related memories for optional cleanup with manage_memory. Saving creates information, not a task or permission.'
        ),
        input_schema=memory_input_schema('remember'),
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=False,
        permission_category="memory_write",
    ),
    'manage_memory': _descriptor(
        name='manage_memory',
        description=(
            'Change or delete an inspected memory. edit: target:MEMORY, action:edit, statement? and context_kind?; supply at least one change, and omitted fields stay unchanged. delete: target:MEMORY, action:delete only. Query first if no prior snapshot exists. The runtime handles concurrency checks; if the memory changed, query again before deciding what to do. Title and kind are fixed; replace the memory to change them.'
        ),
        input_schema=memory_input_schema('manage_memory'),
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=False,
        permission_category="memory_write",
    ),
}


# Script APIs have Host execution metadata, but no native capability registration.
# Reuse the dispatch descriptor value type; is_model_callable is always false.
_SCRIPT_API_DESCRIPTORS = {
    "run_agent": _descriptor(
        name="run_agent",
        description="Submit an ordered array of assignments through tools.run_agent in ROOT Code Mode. Each item needs instruction; target:AGENT reuses an agent, omission creates one. Optional work_title names this Work. materials accepts CONTEXT_MATERIAL with turns 1–3 or a completed WORK, for new and reused agents. depends_on waits for the specified Work results. Success content records preserve assignment order and contain agent, work and work_status. Acceptance is not completion; use wait_agent or query_agents for progress and answers.",
        input_schema=run_agent_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "decide": _descriptor(
        name="decide",
        description=(
            "Evaluate one natural-language yes/no condition independently for each supplied text "
            "using the configured Decision model. Returns one content object with {decisions: boolean[]} in input "
            "order. Compose with any tools or agent operations for filtering, branching, loops or "
            "further calls. Include needed context in each input. Guard calls with "
            "if (tools.decide.available()); this local synchronous method reports configuration "
            "availability captured at Cell startup. Otherwise pass the condition and material to "
            "ROOT to continue. Use JavaScript for exact comparisons. Empty inputs return an empty "
            "array. Service failures reject the call. Decision configuration is independent of "
            "memory reranking."
        ),
        input_schema=object_schema(properties={
            "condition": {"type": "string", "minLength": 1,
                "description": "One explicit yes/no condition to evaluate independently for each input."},
            "inputs": {"type": "array", "items": {"type": "string"},
                "description": "Texts to judge in order. Include the context needed for each judgment in that text."},
        }, required=["condition", "inputs"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
}
_SCRIPT_API_DESCRIPTORS = {
    name: replace(descriptor, is_model_callable=False)
    for name, descriptor in _SCRIPT_API_DESCRIPTORS.items()
}
_SCRIPT_API_DESCRIPTORS["wait_agent"] = replace(
    _descriptor(name="wait_agent", input_schema=wait_agent_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW, is_read_only=True,
        permission_category="subagent_runtime",
        description=("Wait in ROOT Code Mode with exactly one of works:WORK[] or agents:AGENT[]. "
            "settle is all (default) or first; timeout_seconds is 0–300 (default 30). "
            "WORK waits for terminal states, AGENT for idle or a selected sender's message. "
            "Already satisfied targets count immediately. Success returns every target in input order: "
            "WORK records have agent/work/work_status; AGENT records have agent/is_idle. "
            "meta.wait_reason explains return; timeout is successful observation, not work completion. "
            "Message bodies arrive separately in normal conversation input; failures reject with err.result when available.")),
    is_model_callable=False,
)
_SCRIPT_API_DESCRIPTORS["read_file"] = replace(
    _BUILTIN_DESCRIPTORS["read_file"],
    input_schema=read_file_parameters_schema(native=False),
    description="Read a local UTF-8 file. Pass target:PATH with optional nonzero offset and positive limit in lines. Omission reads the full file; negative offset locates from the tail while preserving original line order. Returns the complete saved business value without native pages or NEXT.",
    is_model_callable=False,
)


class BuiltinToolBindingKind(StrEnum):
    SESSION_QUERY = "session_query"
    FILESYSTEM = "filesystem"
    WEB = "web"
    MEMORY_MUTATION = "memory_mutation"
    MEMORY_QUERY = "memory_query"
    PLAN_WORKFLOW = "plan_workflow"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_PROCESS = "terminal_process"
    SCHEDULED_TASK_CONTROL = "scheduled_task_control"
    TODO_LOCAL_STATE = "todo_local_state"
    SUBAGENT_CONTROL = "subagent_control"
    CODE_MODE = "code_mode"
    DECISION = "decision"
    MCP_CATALOG = "mcp_catalog"
    PLUGIN_CONTROL = "plugin_control"


class BuiltinToolAvailabilityKind(StrEnum):
    REQUIRES_SESSION_QUERY_PORT = "requires_session_query_port"
    ALWAYS = "always"
    REQUIRES_SCHEDULED_TASK_PORT = "requires_scheduled_task_port"
    REQUIRES_MEMORY_MUTATION_PORT = "requires_memory_mutation_port"
    REQUIRES_MEMORY_QUERY_PORT = "requires_memory_query_port"
    REQUIRES_TERMINAL_PORTS = "requires_terminal_ports"
    REQUIRES_MAIN_SUBAGENT_CONTROL = "requires_main_subagent_control"
    REQUIRES_SHARED_AGENT_CONTROL = "requires_shared_agent_control"
    REQUIRES_CODE_MODE = "requires_code_mode"
    REQUIRES_DECISION_PORT = "requires_decision_port"
    REQUIRES_MCP_PORT = "requires_mcp_port"


class BuiltinTerminalPermissionRuleKind(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ALL_ACTIONS = "all_actions"
    CLOSED_ACTION_SET = "closed_action_set"


@dataclass(frozen=True, slots=True)
class BuiltinToolAvailabilityRequirement:
    kind: BuiltinToolAvailabilityKind
    allowed_invocation_owners: tuple[ToolInvocationOwnerKind, ...]


@dataclass(frozen=True, slots=True)
class BuiltinActionPermissionOverride:
    discriminator_field: str
    discriminator_value: str
    permission_category: str
    allowed_in_read_only: bool


@dataclass(frozen=True, slots=True)
class BuiltinTerminalPermissionRule:
    kind: BuiltinTerminalPermissionRuleKind
    ordered_terminal_access_actions: tuple[str, ...]
    ordered_scheduling_permission_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BuiltinToolPermissionContract:
    ordered_action_overrides: tuple[BuiltinActionPermissionOverride, ...]
    terminal_rule: BuiltinTerminalPermissionRule


@dataclass(frozen=True, slots=True)
class BuiltinToolRecoveryContract:
    severity: Literal["read_only", "bounded_write", "terminal", "unknown_effect"]
    include_in_unfinished_recovery: bool


@dataclass(frozen=True, slots=True)
class BuiltinToolCatalogEntry:
    name: str
    descriptor: BuiltinToolDescriptor
    binding_contract: BuiltinToolBindingContract
    execution_binding_kind: BuiltinToolBindingKind
    availability_requirement: BuiltinToolAvailabilityRequirement
    permission_contract: BuiltinToolPermissionContract
    recovery_contract: BuiltinToolRecoveryContract
    tool_family: Literal[
        "session_query",
        "filesystem",
        "web",
        "memory_read",
        "memory_write",
        "terminal",
        "plan",
        "subagent_parent",
        "subagent_shared",
        "code_mode",
        "scheduled_tasks",
        "plugin_control",
        "local_state",
        "mcp",
    ]
    entry_fingerprint: str


_FILESYSTEM = frozenset(
    {
        "edit_file",
        "read_file",
        "search_file_content",
        "find_files",
        "view_image",
        "write_file",
    }
)
_MEMORY_MUTATION = frozenset({"remember", "manage_memory"})
_MEMORY_QUERY = frozenset({"query_memory"})
_PLAN = frozenset({"ask_plan_question", "enter_plan", "exit_plan"})
_SUBAGENT_PARENT = frozenset({"run_agent", "stop_agent", "wait_agent"})
_SUBAGENT_SHARED = frozenset({"query_agents", "send_agent_message", "wait_message"})
_CODE_MODE = frozenset({"exec_cell", "query_cells", "manage_cell"})
_TERMINAL_PROCESS_ACTIONS = (
    "close_stdin",
    "kill",
    "wait",
    "write",
)


def builtin_tool_catalog() -> tuple[BuiltinToolCatalogEntry, ...]:
    return _BUILTIN_TOOL_CATALOG


def builtin_tool_catalog_entry(name: str) -> BuiltinToolCatalogEntry:
    try:
        return _BUILTIN_TOOL_CATALOG_BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown builtin tool catalog entry: {name}") from exc


def builtin_tool_descriptor(name: str) -> BuiltinToolDescriptor:
    """A native tool definition; script-only APIs are not in this inventory."""
    return builtin_tool_catalog_entry(name).descriptor


def script_api_catalog_entry(name: str) -> BuiltinToolCatalogEntry:
    return _SCRIPT_API_CATALOG_BY_NAME[name]


def execution_catalog_entry(name: str, *, code_mode: bool = False) -> BuiltinToolCatalogEntry:
    """Shared Host settlement metadata, selected at the actual invocation boundary."""
    if name in _SCRIPT_API_CATALOG_BY_NAME and (code_mode or name not in _BUILTIN_DESCRIPTORS):
        return script_api_catalog_entry(name)
    return builtin_tool_catalog_entry(name)


def builtin_action_permission_override(
    name: str,
    arguments: dict[str, object],
) -> BuiltinActionPermissionOverride | None:
    entry = builtin_tool_catalog_entry(name)
    for override in entry.permission_contract.ordered_action_overrides:
        if arguments.get(override.discriminator_field) == override.discriminator_value:
            return override
    return None


def builtin_availability_requirement_identity_fingerprint(
    requirement: BuiltinToolAvailabilityRequirement,
) -> str:
    return context_fingerprint(
        "builtin-tool-availability-requirement:v1",
        {
            "kind": requirement.kind.value,
            "allowed_invocation_owners": tuple(
                item.value for item in requirement.allowed_invocation_owners
            ),
        },
    )


def _terminal_rule_identity_fingerprint(
    rule: BuiltinTerminalPermissionRule,
) -> str:
    return context_fingerprint(
        "builtin-terminal-permission-rule:v1",
        {
            "kind": rule.kind.value,
            "ordered_terminal_access_actions": (
                rule.ordered_terminal_access_actions
            ),
            "ordered_scheduling_permission_actions": (
                rule.ordered_scheduling_permission_actions
            ),
        },
    )


def builtin_permission_contract_identity_fingerprint(
    contract: BuiltinToolPermissionContract,
) -> str:
    return context_fingerprint(
        "builtin-tool-permission-contract:v1",
        {
            "ordered_action_overrides": tuple(
                asdict(item) for item in contract.ordered_action_overrides
            ),
            "terminal_rule_fingerprint": _terminal_rule_identity_fingerprint(
                contract.terminal_rule
            ),
        },
    )


def _recovery_contract_identity_fingerprint(
    contract: BuiltinToolRecoveryContract,
) -> str:
    return context_fingerprint(
        "builtin-tool-recovery-contract:v1",
        {
            "severity": contract.severity,
            "include_in_unfinished_recovery": (
                contract.include_in_unfinished_recovery
            ),
        },
    )


def _catalog_entry(
    name: str, descriptor: BuiltinToolDescriptor, *, code_mode: bool = False
) -> BuiltinToolCatalogEntry:
    binding_kind, availability_kind, owners, family = _catalog_shape(name)
    if code_mode:
        owners = (ToolInvocationOwnerKind.HOST_MAIN_RUN,)
        family = "code_mode"
    availability = BuiltinToolAvailabilityRequirement(
        kind=availability_kind,
        allowed_invocation_owners=owners,
    )
    origin = (
        ToolBindingOrigin.WORKFLOW
        if name in _PLAN
        else (
            ToolBindingOrigin.SUBAGENT_SYSTEM
            if name in _SUBAGENT_PARENT | _SUBAGENT_SHARED
            else ToolBindingOrigin.BUILTIN
        )
    )
    binding = build_tool_binding_contract(
        tool_name=name,
        origin=origin,
    )
    if not isinstance(binding, BuiltinToolBindingContract):
        raise TypeError("builtin catalog produced a non-builtin binding")
    permission = _permission_contract(name, binding_kind)
    recovery = _recovery_contract(name)
    payload = {
        "name": name,
        "descriptor_fingerprint": descriptor.fingerprint(),
        "binding_contract_fingerprint": (
            tool_binding_contract_identity_fingerprint(binding)
        ),
        "execution_binding_kind": binding_kind.value,
        "availability_requirement_fingerprint": (
            builtin_availability_requirement_identity_fingerprint(availability)
        ),
        "permission_contract_fingerprint": (
            builtin_permission_contract_identity_fingerprint(permission)
        ),
        "recovery_contract_fingerprint": (
            _recovery_contract_identity_fingerprint(recovery)
        ),
        "tool_family": family,
    }
    return BuiltinToolCatalogEntry(
        name=name,
        descriptor=descriptor,
        binding_contract=binding,
        execution_binding_kind=binding_kind,
        availability_requirement=availability,
        permission_contract=permission,
        recovery_contract=recovery,
        tool_family=family,  # type: ignore[arg-type]
        entry_fingerprint=context_fingerprint("builtin-tool-catalog-entry:v1", payload),
    )


def _catalog_shape(name: str):
    both = (
        ToolInvocationOwnerKind.HOST_MAIN_RUN,
        ToolInvocationOwnerKind.SUBAGENT,
    )
    if name in WEB_TOOL_NAMES:
        return (BuiltinToolBindingKind.WEB, BuiltinToolAvailabilityKind.ALWAYS, both, "web")
    if name in SESSION_QUERY_TOOL_NAMES:
        return (BuiltinToolBindingKind.SESSION_QUERY,
                BuiltinToolAvailabilityKind.REQUIRES_SESSION_QUERY_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "session_query")
    if name in {"reload_capabilities", "manage_capability"}:
        return (
            BuiltinToolBindingKind.PLUGIN_CONTROL,
            BuiltinToolAvailabilityKind.ALWAYS,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "plugin_control",
        )
    if name == "query_capabilities":
        return (BuiltinToolBindingKind.MCP_CATALOG, BuiltinToolAvailabilityKind.ALWAYS, both, "mcp")
    if name in {
        "call_mcp_tool",
        "read_mcp_resource",
    }:
        return (
            BuiltinToolBindingKind.MCP_CATALOG,
            BuiltinToolAvailabilityKind.REQUIRES_MCP_PORT,
            both,
            "mcp",
        )
    if name in {"open_in_pulsara", "view_image"} or name in MEDIA_TOOL_NAMES:
        return (BuiltinToolBindingKind.FILESYSTEM, BuiltinToolAvailabilityKind.ALWAYS,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "filesystem")
    if name in _FILESYSTEM:
        return (
            BuiltinToolBindingKind.FILESYSTEM,
            BuiltinToolAvailabilityKind.ALWAYS,
            both,
            "filesystem",
        )
    if name == "exec_process":
        return (
            BuiltinToolBindingKind.TERMINAL_COMMAND,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name in {"query_processes", "manage_process"}:
        return (
            BuiltinToolBindingKind.TERMINAL_PROCESS,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name in {"query_schedules", "manage_schedule"}:
        return (BuiltinToolBindingKind.SCHEDULED_TASK_CONTROL,
                BuiltinToolAvailabilityKind.REQUIRES_SCHEDULED_TASK_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "scheduled_tasks")
    if name == "todo":
        return (
            BuiltinToolBindingKind.TODO_LOCAL_STATE,
            BuiltinToolAvailabilityKind.ALWAYS,
            both,
            "local_state",
        )
    if name in _PLAN:
        return (
            BuiltinToolBindingKind.PLAN_WORKFLOW,
            BuiltinToolAvailabilityKind.ALWAYS,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "plan",
        )
    if name in _MEMORY_QUERY:
        return (
            BuiltinToolBindingKind.MEMORY_QUERY,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_QUERY_PORT,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "memory_read",
        )
    if name in _MEMORY_MUTATION:
        return (
            BuiltinToolBindingKind.MEMORY_MUTATION,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_MUTATION_PORT,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "memory_write",
        )
    if name in _SUBAGENT_PARENT:
        return (
            BuiltinToolBindingKind.SUBAGENT_CONTROL,
            BuiltinToolAvailabilityKind.REQUIRES_MAIN_SUBAGENT_CONTROL,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "subagent_parent",
        )
    if name == "wait_message":
        return (BuiltinToolBindingKind.SUBAGENT_CONTROL,
                BuiltinToolAvailabilityKind.REQUIRES_SHARED_AGENT_CONTROL,
                (ToolInvocationOwnerKind.SUBAGENT,), "subagent_shared")
    if name in _SUBAGENT_SHARED:
        return (
            BuiltinToolBindingKind.SUBAGENT_CONTROL,
            BuiltinToolAvailabilityKind.REQUIRES_SHARED_AGENT_CONTROL,
            both,
            "subagent_shared",
        )
    if name == "decide":
        return (BuiltinToolBindingKind.DECISION,
                BuiltinToolAvailabilityKind.REQUIRES_DECISION_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "code_mode")
    if name in _CODE_MODE:
        return (BuiltinToolBindingKind.CODE_MODE,
                BuiltinToolAvailabilityKind.REQUIRES_CODE_MODE,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "code_mode")
    raise ValueError(f"builtin tool is absent from the closed catalog matrix: {name}")


def _permission_contract(
    name: str, binding_kind: BuiltinToolBindingKind
) -> BuiltinToolPermissionContract:
    overrides = tuple(
        BuiltinActionPermissionOverride(
            discriminator_field=discriminator_field,
            discriminator_value=discriminator_value,
            permission_category=permission_category,
            allowed_in_read_only=allowed_in_read_only,
        )
        for (
            discriminator_field,
            discriminator_value,
            permission_category,
            allowed_in_read_only,
        ) in _ACTION_PERMISSION_OVERRIDE_SPECS.get(name, ())
    )
    if binding_kind is BuiltinToolBindingKind.TERMINAL_COMMAND:
        terminal_kind = BuiltinTerminalPermissionRuleKind.ALL_ACTIONS
        access_actions: tuple[str, ...] = ()
        scheduling_actions: tuple[str, ...] = ()
    elif name in {"query_processes"}:
        terminal_kind = BuiltinTerminalPermissionRuleKind.ALL_ACTIONS
        access_actions = ()
        scheduling_actions = ()
    elif binding_kind is BuiltinToolBindingKind.TERMINAL_PROCESS:
        terminal_kind = BuiltinTerminalPermissionRuleKind.CLOSED_ACTION_SET
        access_actions = _TERMINAL_PROCESS_ACTIONS
        scheduling_actions = ()
    else:
        terminal_kind = BuiltinTerminalPermissionRuleKind.NOT_APPLICABLE
        access_actions = ()
        scheduling_actions = ()
    rule = BuiltinTerminalPermissionRule(
        kind=terminal_kind,
        ordered_terminal_access_actions=access_actions,
        ordered_scheduling_permission_actions=scheduling_actions,
    )
    return BuiltinToolPermissionContract(
        ordered_action_overrides=overrides,
        terminal_rule=rule,
    )


def _recovery_contract(name: str) -> BuiltinToolRecoveryContract:
    if name in {"exec_process", "manage_process"}:
        severity = "terminal"
    elif name in {"edit_file", "write_file"}:
        severity = "bounded_write"
    elif name in WEB_TOOL_NAMES or name in MEDIA_TOOL_NAMES or name in SESSION_QUERY_TOOL_NAMES or name in {
        "open_in_pulsara",
        "query_capabilities",
        "query_agents",
        "query_processes",
        "query_schedules",
        "query_cells",
        "decide",
        "read_file",
        "view_image",
        "read_mcp_resource",
        "reload_capabilities",
        "search_file_content",
        "find_files",
        "todo",
    }:
        severity = "read_only"
    else:
        severity = "unknown_effect"
    include = name not in _PLAN
    return BuiltinToolRecoveryContract(
        severity=severity,  # type: ignore[arg-type]
        include_in_unfinished_recovery=include,
    )


_BUILTIN_TOOL_CATALOG = tuple(
    _catalog_entry(name, descriptor)
    for name, descriptor in sorted(_BUILTIN_DESCRIPTORS.items())
)
_BUILTIN_TOOL_CATALOG_BY_NAME = {entry.name: entry for entry in _BUILTIN_TOOL_CATALOG}
if set(_BUILTIN_TOOL_CATALOG_BY_NAME) != set(_BUILTIN_DESCRIPTORS):
    raise RuntimeError("builtin tool catalog and descriptor inventory drifted")

_SCRIPT_API_CATALOG_BY_NAME = {
    name: _catalog_entry(name, descriptor, code_mode=True)
    for name, descriptor in _SCRIPT_API_DESCRIPTORS.items()
}

FILE_WRITE_TOOL_NAMES = frozenset(
    entry.name
    for entry in _BUILTIN_TOOL_CATALOG
    if entry.descriptor.permission_category == "filesystem_write"
)
TERMINAL_TOOL_NAMES = frozenset(
    entry.name for entry in _BUILTIN_TOOL_CATALOG if entry.tool_family == "terminal"
)
PLAN_WORKFLOW_TOOL_NAMES = frozenset(
    entry.name for entry in _BUILTIN_TOOL_CATALOG if entry.tool_family == "plan"
)
READ_ONLY_RECOVERY_TOOL_NAMES = frozenset(
    entry.name
    for entry in _BUILTIN_TOOL_CATALOG
    if entry.recovery_contract.severity == "read_only"
)
__all__ = [
    "BuiltinActionPermissionOverride",
    "BuiltinTerminalPermissionRule",
    "BuiltinTerminalPermissionRuleKind",
    "BuiltinToolAvailabilityKind",
    "BuiltinToolAvailabilityRequirement",
    "BuiltinToolBindingKind",
    "BuiltinToolCatalogEntry",
    "BuiltinToolPermissionContract",
    "BuiltinToolRecoveryContract",
    "FILE_WRITE_TOOL_NAMES",
    "PLAN_WORKFLOW_TOOL_NAMES",
    "READ_ONLY_RECOVERY_TOOL_NAMES",
    "TERMINAL_TOOL_NAMES",
    "builtin_tool_catalog",
    "builtin_tool_catalog_entry",
    "builtin_action_permission_override",
    "builtin_availability_requirement_identity_fingerprint",
    "builtin_permission_contract_identity_fingerprint",
    "builtin_tool_descriptors",
]
