"""Single descriptor, binding, permission, and taxonomy catalog for built-ins."""

from __future__ import annotations

from pulsara_agent.scheduling.requests import action_schema
from pulsara_agent.capability.source_query import list_input_schema, inspect_input_schema
from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES, session_query_input_schema

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from pulsara_agent.capability.management_intent import capability_management_input_schema

from pulsara_agent.memory.product_contract import (
    MEMORY_COHESIVE_UNIT_GUIDE,
    MEMORY_RETRIEVAL_AUTHORING_GUIDE,
    MEMORY_CONTEXT_PRODUCT_GUIDE,
    memory_kind_product_guide,
)
from pulsara_agent.memory.targets import memory_target_schema

from pulsara_agent.capability.descriptor import (
    BuiltinToolAdvertisePolicy,
    BuiltinToolDescriptor,
    BuiltinToolDomainKind,
)
from pulsara_agent.ports.artifact import ToolArtifactMode
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
    MAXIMUM_TODO_TEXT_UTF8_BYTES,
)
from pulsara_agent.ports.terminal import (
    TERMINAL_MONITOR_TOOL_DESCRIPTION,
    TERMINAL_PROCESS_TOOL_DESCRIPTION,
    TERMINAL_TOOL_DESCRIPTION,
    terminal_monitor_input_schema,
    terminal_input_schema,
    terminal_process_input_schema,
)


DEFAULT_ARTIFACT_READ_CHARS = 20_000
DEFAULT_READ_LINES = 2_000
MAX_READ_LINES = 2_000
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 1_000
DEFAULT_MAX_OUTPUT_CHARS = 32_000

_SOURCE_AUTHORITIES = [
    "explicit_user_instruction",
    "tool_result",
    "document_source",
    "conversation_evidence",
    "model_inference",
    "system_rule",
]
_VERIFICATION_STATUSES = [
    "unverified",
    "inferred",
    "user_confirmed",
    "tool_verified",
    "contradicted",
    "stale",
]


_ACTION_PERMISSION_OVERRIDE_SPECS: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
    "terminal_process": tuple(
        ("action", action, "terminal_process_observe", True)
        for action in ("list", "poll", "wait")
    ),
    "scheduled_tasks": tuple(("action", action, "scheduled_task_read", True) for action in ("list", "get")),
    "terminal_monitor": (
        ("action", "list", "terminal_process_observe", True),
    ),
}


def object_schema(*, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _edit_operation_schema() -> dict[str, Any]:
    logical_lines = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "string",
        },
    }
    inclusive_range = {
        "start_line": {
            "type": "integer",
            "minimum": 1,
        },
        "end_line": {
            "type": "integer",
            "minimum": 1,
        },
    }
    return {
        "oneOf": [
            object_schema(
                properties={
                    "kind": {"const": "replace_lines"},
                    **inclusive_range,
                    "lines": logical_lines,
                },
                required=["kind", "start_line", "end_line", "lines"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "delete_lines"},
                    **inclusive_range,
                },
                required=["kind", "start_line", "end_line"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "insert_before"},
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "lines": logical_lines,
                },
                required=["kind", "line", "lines"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "insert_after"},
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "lines": logical_lines,
                },
                required=["kind", "line", "lines"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "replace_file"},
                    "content": {
                        "type": "string",
                        "description": (
                            'Complete replacement text; an empty string empties the file.'
                        ),
                    },
                },
                required=["kind", "content"],
            ),
        ]
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
    artifact_mode: ToolArtifactMode = ToolArtifactMode.DEFAULT,
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
        artifact_mode=artifact_mode,
        metadata={"source": "explicit_builtin_descriptor"},
    )


def _remember_parameters() -> dict[str, Any]:
    return object_schema(
        properties={
            "based_on": {
                "description": "Saved items this statement depends on, rather than merely "
                "related items, paraphrases or wider-scope copies. GLOBAL "
                "may depend only on GLOBAL; CURRENT_PROJECT may depend on "
                "GLOBAL or this project's memories. Deleting any basis "
                "also deletes this dependent item.",
                "items": memory_target_schema(),
                "maxItems": 8,
                "type": "array",
            },
            "context_target": {
                "description": MEMORY_CONTEXT_PRODUCT_GUIDE,
                "enum": ["GLOBAL", "CURRENT_PROJECT"],
                "type": "string",
            },
            "kind": {
                "description": memory_kind_product_guide()
                + " Choose FACT when uncertain.",
                "enum": ["USER_PROFILE", "RESPONSE_PREFERENCE", "FACT", "DECISION"],
                "type": "string",
            },
            "statement": {
                "description": (
                    MEMORY_COHESIVE_UNIT_GUIDE
                    + " "
                    + MEMORY_RETRIEVAL_AUTHORING_GUIDE
                    + " Maximum 8192 UTF-8 bytes; 2048 for RESPONSE_PREFERENCE."
                ),
                "maxLength": 8192,
                "minLength": 1,
                "type": "string",
            },
        },
        required=["statement", "context_target", "kind"],
    )


_MEMORY_SEARCH_PARAMETERS = object_schema(
    properties={
        "kind": {
            "description": "Preferred kind; omit unless the request requires one kind. Results may "
            "include other kinds when exact matches are insufficient; see "
            "filter_match.",
            "enum": ["USER_PROFILE", "RESPONSE_PREFERENCE", "FACT", "DECISION"],
            "type": "string",
        },
        "limit": {"default": 5, "maximum": 50, "minimum": 1, "type": "integer"},
        "query": {
            "description": "Describe the needed subject using natural language or keywords, with "
            "known project and distinguishing details. Resolve vague references from "
            "available context. Maximum 32768 UTF-8 bytes.",
            "maxLength": 32768,
            "minLength": 1,
            "type": "string",
        },
    },
    required=["query"],
)
_MEMORY_GET_PARAMETERS = object_schema(
    properties={
        "target": memory_target_schema(
            "Copy a MEMORY target from a memory result or reference."
        )
    },
    required=["target"],
)
_REMEMBER_PARAMETERS = _remember_parameters()
_MARK_MEMORY_RELATION_PARAMETERS = object_schema(
    properties={
        "relation_kind": {"enum": ["CONTRADICTS", "SUPERSEDES"], "type": "string"},
        "source": memory_target_schema(
            "For SUPERSEDES, the supported replacement that remains active."
        ),
        "target": memory_target_schema("For SUPERSEDES, the item made inactive."),
    },
    required=["source", "target", "relation_kind"],
)

from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    PROFILES, target_schema,
)


def _run_agent_schema() -> dict[str, Any]:
    works = {"type": "array", "maxItems": 16, "uniqueItems": True,
             "items": target_schema("WORK")}
    return object_schema(properties={
        "instruction": {"type": "string", "minLength": 1, "maxLength": 65536},
        "target": target_schema("AGENT"),
        "name": {"type": "string", "minLength": 1, "maxLength": 256},
        "profile": {"type": "string", "enum": list(PROFILES)},
        "context": {"oneOf": [
            object_schema(properties={"mode": {"const": "none"}}, required=["mode"]),
            object_schema(properties={"mode": {"const": "last_n"}, "turns": {"type": "integer", "minimum": 1, "maximum": 3}}, required=["mode", "turns"]),
        ]},
        "materials": works,
        "depends_on": works,
    }, required=["instruction"])


def _wait_agent_schema() -> dict[str, Any]:
    return object_schema(properties={
        "targets": {"type": "array", "minItems": 1, "uniqueItems": True,
                    "items": target_schema("AGENT", allow_root=True)},
        "settle": {"type": "string", "enum": ["all", "first"], "default": "all"},
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 300, "default": 30},
    }, required=["targets"])


def _exec_schema() -> dict[str, Any]:
    observation = {"type": "integer", "minimum": 0, "maximum": 30000, "default": 1000}
    return {"oneOf": [object_schema(properties={source: {"type": "string", **({"minLength": 1} if source == "path" else {})},
        "yield_time_ms": observation}, required=[source]) for source in ("code", "path")]}


_BUILTIN_DESCRIPTORS: dict[str, BuiltinToolDescriptor] = {
    "open_in_pulsara": _descriptor(
        name="open_in_pulsara", description=("ROOT only. Show targets in this conversation's tabs: PATH files, URL webpages, PROCESS "
 'terminals, runtime MCP_SERVER connections, or PANEL overview entries. Group related targets; '
 'the last accepted target is selected. Invalid items '
 'return their zero-based index while valid items proceed. Results acknowledge requests, not '
 "completed display or webpage contents. Copy each result item's target for reuse. "
 'PANEL browser creates a blank tab; duplicates share one tab within '
 'a call and later calls create new tabs.'),
        input_schema={'additionalProperties': False,
 'properties': {'targets': {'items': {'anyOf': [{'additionalProperties': False,
                                                 'properties': {'path': {'description': 'File '
                                                                                        'path; '
                                                                                        'workspace-relative, '
                                                                                        'absolute, '
                                                                                        '~ and '
                                                                                        '${PULSARA_HOME} '
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
                                                {'additionalProperties': False,
                                                 'properties': {'process_id': {'description': 'process_id '
                                                                                              'from '
                                                                                              'terminal '
                                                                                              'or '
                                                                                              'terminal_process.',
                                                                               'minLength': 1,
                                                                               'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'PROCESS'}},
                                                 'required': ['kind', 'process_id'],
                                                 'type': 'object'},
                                                {'additionalProperties': False,
                                                 'properties': {'runtime_server_id': {'description': 'runtime_server_id '
                                                                                                     'from '
                                                                                                     'inspect_capability.',
                                                                                      'minLength': 1,
                                                                                      'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'MCP_SERVER'}},
                                                 'required': ['kind', 'runtime_server_id'],
                                                 'type': 'object'},
                                                {'additionalProperties': False,
                                                 'properties': {'name': {'enum': ['capabilities',

                                                                                  'background',
                                                                                  'browser'],
                                                                         'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'PANEL'}},
                                                 'required': ['kind', 'name'],
                                                 'type': 'object'}]},
                            'minItems': 1,
                            'type': 'array'}},
 'required': ['targets'],
 'type': 'object'}, is_read_only=True, permission_category="filesystem_read",
    ),
    "list_capabilities": _descriptor(
        name="list_capabilities",
        description=(
            "Find Skills, Hooks, MCP servers, tools, resources, templates and prompts. Use {} for an overview, or filter the list. Copy an item's target into inspect_capability or a supported manage_capability action; use source.target as parent to find a Plugin's components. Continue with next_offset and the same filters. Results describe the observed catalog and its completeness. SUBAGENT callers can query only their assigned MCP runtime."
        ),
        input_schema=list_input_schema(),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "inspect_capability": _descriptor(
        name="inspect_capability",
        description=(
            'Inspect a known capability target; list_capabilities supplies copyable targets. Read Skill bodies with read_file using a PATH target built from skill_path. For MCP tools, follow invocation.mode: DIRECT uses the listed named tool; META uses the returned input_schema and invocation.target with use_new_mcp_tool, including when an older named entry remains listed; UNAVAILABLE gives the current obstacle. This reads metadata. Use read_mcp_resource for resource content and get_mcp_prompt for rendered prompts.'
        ),
        input_schema=inspect_input_schema(),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "manage_capability": _descriptor(
        name="manage_capability",
        description=(
            "Install and manage capabilities requested by the user. ROOT agent only; available in every permission mode.\nSet action and send only its listed fields; fields are required unless marked optional. Copy targets from list/inspect or a management result; a known exact target can be used directly. For ADD_LOCAL_MCP, construct a LOCAL MCP_CONNECTION target with the destination scope and new server_id. Installation targets use source-local IDs, not runtime_server_id; runtime and MCP tool/resource/prompt targets are for inspection, not management.\nINSTALL_LOOSE_SKILL: scope, source_path; optional name, description. Install an independent Skill into a new destination.\nSET_LOOSE_SKILL_ENABLED: target (SKILL), enabled. Toggle an eligible independent Skill.\nREMOVE_LOOSE_SKILL: target (SKILL). Remove an eligible independent Skill directory.\nTRUST_HOOK_SOURCE: target (HOOK_SOURCE). Review and trust current Hook definitions.\nREVOKE_HOOK_TRUST: target (HOOK_SOURCE). Remove trust, keeping definitions.\nSET_HOOK_SOURCE_ENABLED: target (HOOK_SOURCE), enabled. Toggle the Hook source.\nADD_LOCAL_MCP: target (LOCAL MCP_CONNECTION); optional config. Add an independent connection.\nUPDATE_LOCAL_MCP: target (LOCAL MCP_CONNECTION); optional config, expected_identity. Replace an independent connection's settings.\nREMOVE_LOCAL_MCP: target (LOCAL MCP_CONNECTION); optional expected_identity. Remove the entry and dedicated local credentials, keeping the server program.\nINSTALL_PLUGIN: scope, source_path; optional replace, source_format. Install the whole Plugin disabled; enable separately.\nSET_PLUGIN_ENABLED: target (PLUGIN), enabled; optional expected_package_install_id. Toggle the whole Plugin, with component review on enable.\nREMOVE_PLUGIN: target (PLUGIN); optional expected_package_install_id. Remove the Plugin and dedicated credentials, preserving its data and original source.\nCONFIGURE_PLUGIN_MCP_CONNECTION: target (PLUGIN MCP_CONNECTION); optional overlay, expected_overlay, expected_package_install_id. Configure a Plugin connection.\nAUTHORIZE_MCP: target (LOCAL or PLUGIN MCP_CONNECTION). Authorize an already configured OAuth connection.\nCLEAR_MCP_AUTHORIZATION: target (LOCAL or PLUGIN MCP_CONNECTION). Clear local OAuth grants, keeping configuration and remote tokens.\nFor both OAuth actions, optional expected_identity applies only to LOCAL targets; optional expected_package_install_id applies only to PLUGIN targets. For MCP_CONNECTION and HOOK_SOURCE targets, LOCAL/PLUGIN selects source_kind.\nUse inspect_capability for eligibility. Plugin Skills are managed through their source Plugin; bundled Skills are read-only. Hook trust, Hook enablement and Plugin enablement are separate settings. Edit loose Skill content with file tools or terminal; pulsara-skill-creator describes authoring.\nThe runtime collects required review and confirmation; READ_ONLY mutations use a user-submitted form. Users enter credentials privately in the editor. Wait for the result: status reports the mutation, current the resulting state, and adoption its availability for future use. A completed mutation needs no routine reload or replay."
        ),
        input_schema=capability_management_input_schema(),
        is_read_only=False,
        permission_category="plugin_control",
        artifact_mode=ToolArtifactMode.NEVER,
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
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "artifact_read": _descriptor(
        name="artifact_read",
        description=(
            ('Read a page of saved tool output using its ARTIFACT target. Continue with next_offset_chars '
 'while has_more is true; pages may be shorter than max_chars. Offsets address the saved body; '
 'source_coverage says whether it covers the original observation or only its retained portion. '
 'Use artifact_export for extensive retrieval or processing.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'max_chars': {'default': 20000, 'maximum': 32000, 'minimum': 1, 'type': 'integer'},
                'offset_chars': {'default': 0,
                                 'description': 'Zero-based character offset; copy '
                                                'next_offset_chars to continue.',
                                 'minimum': 0,
                                 'type': 'integer'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'ARTIFACT'},
                                          'artifact_id': {'description': 'Exact saved tool-output '
                                                                         'ID from this '
                                                                         'conversation and '
                                                                         'project.',
                                                          'minLength': 1,
                                                          'type': 'string'}},
                           'required': ['kind', 'artifact_id'],
                           'additionalProperties': False}},
 'required': ['target'],
 'type': 'object'},
        is_read_only=True,
        permission_category="artifact_read",
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "artifact_export": _descriptor(
        name="artifact_export",
        description=(
            ('Export an ARTIFACT target to a new UTF-8 file at path. A prior artifact_read is unnecessary. '
 'Export preserves the saved body, including plain terminal output; source_coverage reports '
 'original or retained coverage. Parent directories are created and existing paths left unchanged. '
 'The exported file remains independently of the conversation.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'path': {'description': 'New file path, relative to the workspace or absolute; ~ '
                                        'is supported.',
                         'minLength': 1,
                         'type': 'string'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'ARTIFACT'},
                                          'artifact_id': {'description': 'Exact saved tool-output '
                                                                         'ID from this '
                                                                         'conversation and '
                                                                         'project.',
                                                          'minLength': 1,
                                                          'type': 'string'}},
                           'required': ['kind', 'artifact_id'],
                           'additionalProperties': False}},
 'required': ['target', 'path'],
 'type': 'object'},
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "use_new_mcp_tool": _descriptor(
        name="use_new_mcp_tool",
        description=(
            'Execute an MCP tool inspected with invocation.mode=META. Copy invocation.target and supply arguments matching the returned input_schema, including when an older same-name tool remains listed. The remote action may require confirmation. Reuse the target for later calls; inspect again if it is rejected.'
        ),
        input_schema={'type': 'object',
 'properties': {'target': {'type': 'object',
                           'properties': {'kind': {'const': 'MCP_TOOL_REF'},
                                          'tool_ref': {'description': 'Exact reference from '
                                                                      'invocation.target, scoped '
                                                                      'to this agent and '
                                                                      'conversation.',
                                                       'maxLength': 160,
                                                       'pattern': '^mcpref_[A-Za-z0-9_-]+$',
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
        artifact_mode=ToolArtifactMode.DEFAULT,
    ),
    "read_mcp_resource": _descriptor(
        name="read_mcp_resource",
        description=(
            'Read an MCP resource. Copy its target from list/inspect. MCP_RESOURCE: target only, with the concrete URI in target.uri. MCP_RESOURCE_TEMPLATE: target and uri; supply a concrete URI matching target.uri_template. Results are external reference content. If the saved-output guidance supplies an ARTIFACT target, copy it into artifact_read to page the saved result. Another remote read fetches fresh content, not the next page.'
        ),
        input_schema={'type': 'object',
 'oneOf': [{'type': 'object',
            'properties': {'target': {'type': 'object',
                                      'properties': {'kind': {'const': 'MCP_RESOURCE'},
                                                     'server_id': {'type': 'string',
                                                                   'minLength': 1,
                                                                   'maxLength': 256},
                                                     'uri': {'type': 'string',
                                                             'minLength': 1,
                                                             'maxLength': 32768}},
                                      'required': ['kind', 'server_id', 'uri'],
                                      'additionalProperties': False}},
            'required': ['target'],
            'additionalProperties': False},
           {'type': 'object',
            'properties': {'target': {'type': 'object',
                                      'properties': {'kind': {'const': 'MCP_RESOURCE_TEMPLATE'},
                                                     'server_id': {'type': 'string',
                                                                   'minLength': 1,
                                                                   'maxLength': 256},
                                                     'uri_template': {'type': 'string',
                                                                      'minLength': 1,
                                                                      'maxLength': 32768}},
                                      'required': ['kind', 'server_id', 'uri_template'],
                                      'additionalProperties': False},
                           'uri': {'description': 'Concrete URI matching the selected template.',
                                   'maxLength': 32768,
                                   'minLength': 1,
                                   'type': 'string'}},
            'required': ['target', 'uri'],
            'additionalProperties': False}]},
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "get_mcp_prompt": _descriptor(
        name="get_mcp_prompt",
        description=(
            'Fetch and render a listed MCP prompt. Copy its MCP_PROMPT target and supply any declared arguments. The returned messages are external guidance for the current request; fetching them does not execute tools.'
        ),
        input_schema={'type': 'object',
 'properties': {'target': {'type': 'object',
                           'properties': {'kind': {'const': 'MCP_PROMPT'},
                                          'server_id': {'type': 'string',
                                                        'minLength': 1,
                                                        'maxLength': 256},
                                          'name': {'type': 'string',
                                                   'minLength': 1,
                                                   'maxLength': 8192}},
                           'required': ['kind', 'server_id', 'name'],
                           'additionalProperties': False},
                'arguments': {'additionalProperties': {'type': 'string'},
                              'description': 'Use declared argument names, including every '
                                             'required name, with string values. Omit or use {} '
                                             'when none are needed.',
                              'type': 'object'}},
 'required': ['target'],
 'additionalProperties': False},
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "read_file": _descriptor(
        name="read_file",
        description=(
            ('Read a local UTF-8 file at a PATH target. Returns content_revision for edit_file. Lines use '
 'N|text labels; only text is file content. Read the affected lines or adjacent gaps before line '
 'edits. Whole-file replacement needs a current revision but not a full display. Continue '
 'truncated reads with the returned offset hint.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'limit': {'default': 2000, 'maximum': 2000, 'minimum': 1, 'type': 'integer'},
                'offset': {'default': 1,
                           'description': 'First line to return, numbered from 1.',
                           'minimum': 1,
                           'type': 'integer'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                          'path': {'description': 'Local text path; relative paths '
                                                                  'start at the workspace. ../, '
                                                                  'absolute paths, ~ and cataloged '
                                                                  '${PULSARA_HOME}/ paths are '
                                                                  'supported.',
                                                   'minLength': 1,
                                                   'type': 'string'}},
                           'required': ['kind', 'path'],
                           'additionalProperties': False}},
 'required': ['target'],
 'type': 'object'},
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "view_image": _descriptor(
        name="view_image",
        description=(
            ('Inspect an image using one target: PATH for a local PNG, JPEG, static WebP, SVG or HTML file; '
 'IMAGE_REF for a saved image reference. SVG renders without cropping, scripts or external '
 'resources. HTML captures its content root or a 1200×800 light-theme viewport with reduced motion '
 'and network access disabled. This is a still view for the model. Use open_in_pulsara to show '
 'files to the user.')
        ),
        input_schema={'type': 'object',
 'properties': {'target': {'oneOf': [{'type': 'object',
                                      'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                                     'path': {'type': 'string', 'minLength': 1}},
                                      'required': ['kind', 'path'],
                                      'additionalProperties': False},
                                     {'type': 'object',
                                      'properties': {'kind': {'type': 'string',
                                                              'const': 'IMAGE_REF'},
                                                     'image_ref': {'description': 'Exact image: '
                                                                                  'reference from '
                                                                                  'an earlier '
                                                                                  'image or '
                                                                                  'historical '
                                                                                  'image '
                                                                                  'placeholder in '
                                                                                  'this workspace.',
                                                                   'minLength': 1,
                                                                   'type': 'string'}},
                                      'required': ['kind', 'image_ref'],
                                      'additionalProperties': False}]}},
 'required': ['target'],
 'additionalProperties': False},
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "search_content": _descriptor(
        name="search_content",
        description=(
            ('Search text with a Rust regular expression. Optional PATH target selects a file or directory; '
 'omit it for the workspace root. content returns matching lines; files_only returns file targets; '
 'count returns matching-line counts per file. Results are sorted by path and line and paginate in '
 'every mode. Copy a file target to read_file for its revision and edit context.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'file_glob': {'description': 'Basename or relative-path glob, also applied to a '
                                             'single-file query.',
                              'minLength': 1,
                              'type': 'string'},
                'limit': {'default': 50, 'maximum': 1000, 'minimum': 1, 'type': 'integer'},
                'offset': {'default': 0,
                           'description': 'Zero-based result offset; count mode pages files. Use '
                                          'the returned continuation hint.',
                           'minimum': 0,
                           'type': 'integer'},
                'output_mode': {'default': 'content',
                                'enum': ['content', 'files_only', 'count'],
                                'type': 'string'},
                'pattern': {'minLength': 1, 'type': 'string'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                          'path': {'description': 'File or directory; relative to '
                                                                  'the workspace or an explicit '
                                                                  'external path. Broad external '
                                                                  'roots are rejected.',
                                                   'minLength': 1,
                                                   'type': 'string'}},
                           'required': ['kind', 'path'],
                           'additionalProperties': False,
                           'description': 'Omit to search the workspace root.'}},
 'required': ['pattern'],
 'type': 'object'},
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "find_files": _descriptor(
        name="find_files",
        description=(
            ('Find file paths by basename or relative-path glob. Optional PATH target selects a file or '
 'directory; omit it for the workspace root. *.py matches all depths; src/*.py one level; '
 'src/**/*.py recursively. Use *name* for a name fragment. Results are sorted by path; continue '
 'with the returned offset hint and same query. Copy a returned file target to a file tool.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'glob': {'minLength': 1, 'type': 'string'},
                'limit': {'default': 50, 'maximum': 1000, 'minimum': 1, 'type': 'integer'},
                'offset': {'default': 0,
                           'description': 'Zero-based file offset; use the returned continuation '
                                          'hint.',
                           'minimum': 0,
                           'type': 'integer'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                          'path': {'description': 'File or directory; relative to '
                                                                  'the workspace or an explicit '
                                                                  'external path. Broad external '
                                                                  'roots are rejected.',
                                                   'minLength': 1,
                                                   'type': 'string'}},
                           'required': ['kind', 'path'],
                           'additionalProperties': False,
                           'description': 'Omit to search the workspace root.'}},
 'required': ['glob'],
 'type': 'object'},
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "edit_file": _descriptor(
        name="edit_file",
        description=(
            ('Edit an existing UTF-8 file at a PATH target. Supply base_revision from read_file and operations '
 'separately. Anchors are 1-based in that revision; ranges are inclusive and operations must not '
 'overlap. Line edits require the affected lines or adjacent gaps to have been read. replace_file '
 'must be the sole operation; it needs a current revision but not a full display. Results include '
 'the new revision, diff and changed line windows. NO_OP means no bytes changed. Use write_file '
 'for a new path.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'base_revision': {'description': 'Exact content_revision returned by read_file for '
                                                 'this path.',
                                  'pattern': '^revision:[a-z2-7]{51}[aq]$',
                                  'type': 'string'},
                'operations': {'description': 'Replacement text is literal file content without '
                                              'read_file line labels. Each lines item contains one '
                                              'line without CR, LF or NUL; an empty string is a '
                                              'blank line.',
                               'items': {'oneOf': [{'additionalProperties': False,
                                                    'properties': {'end_line': {'minimum': 1,
                                                                                'type': 'integer'},
                                                                   'kind': {'const': 'replace_lines'},
                                                                   'lines': {'items': {'type': 'string'},
                                                                             'minItems': 1,
                                                                             'type': 'array'},
                                                                   'start_line': {'minimum': 1,
                                                                                  'type': 'integer'}},
                                                    'required': ['kind',
                                                                 'start_line',
                                                                 'end_line',
                                                                 'lines'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'end_line': {'minimum': 1,
                                                                                'type': 'integer'},
                                                                   'kind': {'const': 'delete_lines'},
                                                                   'start_line': {'minimum': 1,
                                                                                  'type': 'integer'}},
                                                    'required': ['kind', 'start_line', 'end_line'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'kind': {'const': 'insert_before'},
                                                                   'line': {'minimum': 1,
                                                                            'type': 'integer'},
                                                                   'lines': {'items': {'type': 'string'},
                                                                             'minItems': 1,
                                                                             'type': 'array'}},
                                                    'required': ['kind', 'line', 'lines'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'kind': {'const': 'insert_after'},
                                                                   'line': {'minimum': 1,
                                                                            'type': 'integer'},
                                                                   'lines': {'items': {'type': 'string'},
                                                                             'minItems': 1,
                                                                             'type': 'array'}},
                                                    'required': ['kind', 'line', 'lines'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'content': {'description': 'Complete '
                                                                                              'replacement '
                                                                                              'text; '
                                                                                              'an '
                                                                                              'empty '
                                                                                              'string '
                                                                                              'empties '
                                                                                              'the '
                                                                                              'file.',
                                                                               'type': 'string'},
                                                                   'kind': {'const': 'replace_file'}},
                                                    'required': ['kind', 'content'],
                                                    'type': 'object'}]},
                               'minItems': 1,
                               'type': 'array'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                          'path': {'description': 'Existing file path, relative to '
                                                                  'the workspace or absolute; ~ is '
                                                                  'supported.',
                                                   'minLength': 1,
                                                   'type': 'string'}},
                           'required': ['kind', 'path'],
                           'additionalProperties': False}},
 'required': ['target', 'base_revision', 'operations'],
 'type': 'object'},
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "write_file": _descriptor(
        name="write_file",
        description=(
            ('Create a new UTF-8 text file at a PATH target, creating parent directories as needed. The path '
 'may not exist yet. Existing paths are left unchanged. Use read_file then edit_file to change an '
 'existing file.')
        ),
        input_schema={'additionalProperties': False,
 'properties': {'content': {'description': 'Complete file text; an empty string creates an empty '
                                           'file.',
                            'type': 'string'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                          'path': {'description': 'New file path, relative to the '
                                                                  'workspace or absolute; ~ is '
                                                                  'supported.',
                                                   'minLength': 1,
                                                   'type': 'string'}},
                           'required': ['kind', 'path'],
                           'additionalProperties': False}},
 'required': ['target', 'content'],
 'type': 'object'},
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "terminal": _descriptor(
        name="terminal",
        description=TERMINAL_TOOL_DESCRIPTION,
        input_schema=terminal_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        artifact_mode=ToolArtifactMode.LARGE_OUTPUT,
        is_open_world=True,
    ),
    "terminal_process": _descriptor(
        name="terminal_process",
        description=TERMINAL_PROCESS_TOOL_DESCRIPTION,
        input_schema=terminal_process_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        artifact_mode=ToolArtifactMode.LARGE_OUTPUT,
        is_destructive=True,
        is_open_world=True,
    ),
    "terminal_monitor": _descriptor(
        name="terminal_monitor",
        description=TERMINAL_MONITOR_TOOL_DESCRIPTION,
        input_schema=terminal_monitor_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        artifact_mode=ToolArtifactMode.DEFAULT,
        is_destructive=True,
        is_open_world=False,
    ),
    "search_sessions": _descriptor(
        name="search_sessions",
        description=(
            "Find saved conversations by title or message keywords; omit query to list by activity. This session's body hits cover only earlier content removed by compaction; other accessible sessions use saved ROOT history. Copy item.target (SESSION) to search/read a conversation, or hit.target (SESSION_ENTRY) to read a body hit. Title-only hits have no hit.\n"
            'First page: optional query, lifecycle, limit.\n'
            'Continue: cursor; optional limit. Send no other fields on continuation. Omit unused fields; null is not accepted.'
        ),
        input_schema=session_query_input_schema(
            "search_sessions",
            default_chars=DEFAULT_ARTIFACT_READ_CHARS,
            maximum_chars=DEFAULT_MAX_OUTPUT_CHARS,
        ),
        is_read_only=True,
        permission_category="session_read",
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "search_session_content": _descriptor(
        name="search_session_content",
        description=(
            "Find saved questions and their answer process by keyword relevance. First page: query; optional target (SESSION), include_tools, limit. Omit target for this conversation's earlier content removed by compaction; messages still in context are excluded. Other accessible conversations use saved ROOT history. Results preserve speakers, times and historical quotes/comments; [truncated] marks omitted text. Each item.target anchors its question; copy a message.target to read_session_content to expand that message. Images are presence markers; generated markers and image metadata are not search terms.\n"
            'Continue: cursor; optional limit. Send no other fields on continuation. Omit unused fields; null is not accepted.'
        ),
        input_schema=session_query_input_schema(
            "search_session_content",
            default_chars=DEFAULT_ARTIFACT_READ_CHARS,
            maximum_chars=DEFAULT_MAX_OUTPUT_CHARS,
        ),
        is_read_only=True,
        permission_category="session_read",
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "read_session_content": _descriptor(
        name="read_session_content",
        description=(
            "Read saved messages directly or expand a search hit; searching first is optional. First page: optional target (SESSION or SESSION_ENTRY), direction, include_tools, limit, max_chars. Omit target for this conversation's earlier content removed by compaction; SESSION_ENTRY may omit session_id for the same range. Other accessible conversations use saved ROOT history. A SESSION_ENTRY anchor is read first. Long messages start at the beginning and continue across pages before adjacent messages; partial marks a slice. Tool output may have missing saved content as shown by source_coverage, display_kind and artifact_disposition; pagination cannot recover it. Images are text placeholders; same-workspace placeholders provide an IMAGE_REF target to copy into view_image. Increase max_chars if a placeholder cannot fit.\n"
            'Continue: cursor; optional limit, max_chars. Send no other fields on continuation. Omit unused fields; null is not accepted.'
        ),
        input_schema=session_query_input_schema(
            "read_session_content",
            default_chars=DEFAULT_ARTIFACT_READ_CHARS,
            maximum_chars=DEFAULT_MAX_OUTPUT_CHARS,
        ),
        is_read_only=True,
        permission_category="session_read",
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "scheduled_tasks": _descriptor(
        name="scheduled_tasks",
        description=(
            'Manage saved schedules from the ROOT conversation. Set action and send only its listed fields; fields are required unless marked optional. Copy SCHEDULE targets from results or use a known exact target. session takes a SESSION target; omission/null means this session.\n'
            'list: optional session, status, cursor.\n'
            'get: target.\n'
            'create: values; optional session, status.\n'
            'update: target, expected_revision, values.\n'
            'pause: target, expected_revision.\n'
            'resume: target, expected_revision.\n'
            'delete: target, expected_revision.\n'
            'switch: target, expected_revision, replacement. target is the source ACTIVE schedule. replacement is {kind:"existing",target,expected_revision} for a PAUSED schedule, or {kind:"create",values} for a new schedule in the same session.\n'
            "Use list/get for current values and revision; reread on revision conflict. session filters a list or selects a new schedule's parent, never changes an existing schedule's owner. Continue lists with next_cursor and the same filters.\n"
            'values is the complete configuration; preserve unrequested fields on update. Choose a short name if unspecified. prompt contains self-contained future instructions and output expectations; timing and permissions have separate fields. Supply an IANA timezone; use Asia/Shanghai for new tasks unless requested otherwise. For create, use current permissions or a requested narrower preset. Saving configuration requires current permissions to cover permission_mode; preserve saved permissions unless a change is requested.\n'
            'Each session supports one ACTIVE plan and any number of PAUSED plans. create with omitted/null status saves PAUSED; use ACTIVE to start automatic scheduling. An authorized replacement uses switch atomically; adding a task alone does not request replacement. Separate automatic plans need separate sessions.\n'
            'Calendar rules use local HH:mm and YYYY-MM-DD start_date; weekdays are Monday=1 through Sunday=7. once and interval timestamps are whole-second UTC; new or changed once times must be in the future. Intervals keep their fixed anchor. Missing month days and nonexistent DST times are skipped; repeated DST times fire once. Explain saved times in ordinary language.\n'
            "Edits preserve queued instructions. pause/delete/switch cancel the affected source plan's pending inputs and leave running replies alone; delete keeps the chat. Stopping a reply leaves its schedule active. Resume uses the next future recurrence without catch-up; an overdue untriggered once plan becomes due."
        ),
        input_schema=action_schema(),
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
                        "Complete ordered snapshot, up to 4 KiB of canonical JSON."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAXIMUM_TODO_TEXT_UTF8_BYTES,
                                "description": (
                                    "Unique task label: one plain line in Unicode NFC, without surrounding whitespace, up to 192 UTF-8 bytes."
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
    "run_agent": _descriptor(
        name="run_agent",
        description="Submit work to a new or existing subagent and return its AGENT and WORK references immediately. Omit target to create an Agent; name and context apply only to creation. Reuse target to continue its history. materials copies completed work; depends_on waits for successful work and supplies its final. Ordinary final completes work.",
        input_schema=_run_agent_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "list_agents": _descriptor(
        name="list_agents",
        description="List this ROOT's subagents with current and queued work. Continue with next_cursor.",
        input_schema=object_schema(properties={
            "cursor": {"type": "string", "minLength": 1},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
        }, required=[]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "inspect_agent": _descriptor(
        name="inspect_agent",
        description="Inspect an AGENT's paginated work or one exact WORK's state and result. WORK results belong to that request, including stopped diagnostics.",
        input_schema={"oneOf": [
            object_schema(properties={"target": target_schema("AGENT"),
                "cursor": {"type": "string", "minLength": 1},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50}}, required=["target"]),
            object_schema(properties={"target": target_schema("WORK")}, required=["target"]),
        ]},
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "send_agent_message": _descriptor(
        name="send_agent_message",
        description="Send a message to an Agent's current active work. ROOT addresses this collaboration's ROOT. queued means accepted into that turn; not_started or inactive means not sent. This does not start work or load an inactive Agent.",
        input_schema=object_schema(properties={"target": target_schema("AGENT", allow_root=True),
            "message": {"type": "string", "minLength": 1, "maxLength": 16384}}, required=["target", "message"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "wait_agent": _descriptor(
        name="wait_agent",
        description="Wait for a known subagent's message or current work result. Do not wait for ROOT unless ROOT has explicitly asked you to wait for its reply. Targets are AGENT references; an unstarted target returns unavailable. Waiting binds current work and never follows its next request. Timeout ends this observation only.",
        input_schema=_wait_agent_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "stop_agent": _descriptor(
        name="stop_agent",
        description="Stop one WORK, or an AGENT's current and already queued work. Return actual outcomes; completed history stays terminal. The Agent and its history remain available for new work.",
        input_schema=object_schema(properties={"target": {"oneOf": [target_schema("AGENT"), target_schema("WORK")]}}, required=["target"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "exec": _descriptor(
        name="exec",
        description="Start an independent JavaScript script from code or a workspace path and return observed output and state. Top-level await is supported. Observation timeout does not stop the script. ROOT with bypass permissions only.",
        input_schema=_exec_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "wait_exec": _descriptor(
        name="wait_exec",
        description="Read subsequent output and state from the same live script. This does not execute its code again; observation timeout leaves it running.",
        input_schema=object_schema(properties={"cell_id": {"type": "string", "minLength": 1},
            "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 30000, "default": 1000}}, required=["cell_id"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "stop_exec": _descriptor(
        name="stop_exec",
        description="Stop further JavaScript execution and tool submissions in a cell. Already accepted Work is not automatically cancelled; use stop_agent for that work.",
        input_schema=object_schema(properties={"cell_id": {"type": "string", "minLength": 1}}, required=["cell_id"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "enter_plan": _descriptor(
        name="enter_plan",
        description=(
            "Start a read-only planning phase when the user wants to review an approach before "
            "implementation, or an unresolved consequential implementation choice needs their decision. "
            "Investigate, use ask_plan_question for blocking choices, then submit the complete proposal "
            "with exit_plan. Use this as the only tool call in the response; it takes control of the turn "
            "and other calls in that response will not run."
        ),
        input_schema=object_schema(
            properties={
                "reason": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": (
                        "Decision or objective that needs a planning phase; up to 4096 UTF-8 bytes."
                    ),
                }
            },
            required=[],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "ask_plan_question": _descriptor(
        name="ask_plan_question",
        description=(
            "During an active planning phase, ask one question whose answer materially changes the plan. "
            "The user's answer becomes this tool's result, then planning continues. Use this as the only "
            "tool call in the response; other calls in that response will not run."
        ),
        input_schema=object_schema(
            properties={
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 16384,
                    "description": (
                        "One self-contained question with the context needed to decide; up to 16384 UTF-8 bytes."
                    ),
                },
                "options": {
                    "description": (
                        "Omit or use [] for an open question with allow_free_text=true; otherwise supply two or three choices with unique labels."
                    ),
                    "anyOf": [
                        {"type": "array", "maxItems": 0},
                        {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 256,
                                        "description": (
                                            "Distinct choice label; up to 256 UTF-8 bytes."
                                        ),
                                    },
                                    "description": {
                                        "type": "string",
                                        "maxLength": 2048,
                                        "description": (
                                            "Effect or tradeoff of this choice; up to 2048 UTF-8 bytes."
                                        ),
                                    },
                                    "recommended": {
                                        "type": "boolean",
                                        "description": (
                                            "At most one choice may be true; omitted means false."
                                        ),
                                    },
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                    ],
                },
                "allow_free_text": {
                    "type": "boolean",
                    "description": (
                        "Allow a custom answer alongside the choices. Required true when options is omitted or empty."
                    ),
                },
            },
            required=["question", "allow_free_text"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "exit_plan": _descriptor(
        name="exit_plan",
        description=(
            "Submit a complete plan for user review, or leave planning without a proposal. A nonblank plan "
            "opens review: approval resumes implementation, revision continues read-only planning with "
            "feedback, and cancellation ends planning. Supply a complete replacement plan after revision. "
            "Omitting plan, passing null or blank text exits without review and automatically resumes with "
            "the permissions saved on entry. Use this as the only tool call in the response; other calls in "
            "that response will not run."
        ),
        input_schema=object_schema(
            properties={
                "plan": {
                    "type": ["string", "null"],
                    "maxLength": 1048576,
                    "description": (
                        "Complete proposal: outcome, affected components, ordered changes, constraints and validation; up to 1048576 UTF-8 bytes."
                    ),
                },
                "summary": {
                    "type": "string",
                    "maxLength": 8192,
                    "description": (
                        "Brief overview of the complete plan; up to 8192 UTF-8 bytes."
                    ),
                },
            },
            required=[],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "memory_search": _descriptor(
        name="memory_search",
        description=(
            "Find saved user or project information by meaning or keywords across "
            "readable memory contexts. Use memory_get when you already have a MEMORY "
            "target; copy a result's target to read it. retrieval_summary reports search completeness, filter expansion, "
            "ranking and relation-check availability; filter_match identifies relaxed "
            "matches, and relation_warnings identifies active contradictions."
        ),
        input_schema=_MEMORY_SEARCH_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=True,
        permission_category="memory_read",
    ),
    "memory_get": _descriptor(
        name="memory_get",
        description=(
            "Read a saved memory by target, including its statement, kind, context, "
            "recorded time, lifecycle, direct relations and any user-edit note. Copy a "
            "MEMORY target from memory_search or another memory result; a known exact "
            "target can be used directly."
        ),
        input_schema=_MEMORY_GET_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=True,
        permission_category="memory_read",
    ),
    "remember": _descriptor(
        name="remember",
        description=(
            "Save reusable information for future conversations. Source-faithful "
            "lightweight inferences from conversation, behavior, tool choice or planning "
            "are welcome; self-report, repetition and high confidence are not required. "
            "Save natural-language context, including attributed observations from tool "
            "results. Exclude secrets, credentials, raw tool dumps, detailed executable "
            "procedures and permission overrides. Saving takes effect immediately and "
            "creates no task, reminder or execution authority. SAVED returns the new "
            "MEMORY target; ALREADY_PRESENT returns the existing item without changing its "
            "origin or dependencies. Users can delete saved items on the Memory page. "
            "Supply statement, context_target and kind; optional based_on contains the "
            "MEMORY targets this statement depends on."
        ),
        input_schema=_REMEMBER_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=False,
        permission_category="memory_write",
    ),
    "mark_memory_relation": _descriptor(
        name="mark_memory_relation",
        description=(
            "Relate two inspected saved memories about the same subject and conditions. "
            "Supply source and target as MEMORY targets, plus relation_kind. New relations "
            "require both items active and in the same context. Related memories returned by "
            "remember are suggestions to inspect, not established relations.\n"
            "CONTRADICTS: keep two incompatible memories of the same kind active when "
            "neither is the supported replacement; endpoint order does not matter.\n"
            "SUPERSEDES: source is the supported replacement, including a corrected "
            "statement or kind; make target inactive without deleting it."
        ),
        input_schema=_MARK_MEMORY_RELATION_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=False,
        permission_category="memory_write",
    ),
}


class BuiltinToolBindingKind(StrEnum):
    SESSION_QUERY = "session_query"
    FILESYSTEM = "filesystem"
    ARTIFACT_READ = "artifact_read"
    ARTIFACT_EXPORT = "artifact_export"
    MEMORY_MUTATION = "memory_mutation"
    MEMORY_RECALL = "memory_recall"
    MEMORY_QUERY = "memory_query"
    PLAN_WORKFLOW = "plan_workflow"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_PROCESS = "terminal_process"
    TERMINAL_MONITOR = "terminal_monitor"
    SCHEDULED_TASK_CONTROL = "scheduled_task_control"
    TODO_LOCAL_STATE = "todo_local_state"
    SUBAGENT_CONTROL = "subagent_control"
    CODE_MODE = "code_mode"
    MCP_CATALOG = "mcp_catalog"
    PLUGIN_CONTROL = "plugin_control"


class BuiltinToolAvailabilityKind(StrEnum):
    REQUIRES_SESSION_QUERY_PORT = "requires_session_query_port"
    ALWAYS = "always"
    REQUIRES_SCHEDULED_TASK_PORT = "requires_scheduled_task_port"
    REQUIRES_ARTIFACT_READ_PORT = "requires_artifact_read_port"
    REQUIRES_MEMORY_MUTATION_PORT = "requires_memory_mutation_port"
    REQUIRES_MEMORY_RECALL_PORT = "requires_memory_recall_port"
    REQUIRES_MEMORY_QUERY_PORT = "requires_memory_query_port"
    REQUIRES_TERMINAL_PORTS = "requires_terminal_ports"
    REQUIRES_MAIN_SUBAGENT_CONTROL = "requires_main_subagent_control"
    REQUIRES_SHARED_AGENT_CONTROL = "requires_shared_agent_control"
    REQUIRES_CODE_MODE = "requires_code_mode"
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
        "artifact",
        "memory_read",
        "memory_write",
        "terminal",
        "plan",
        "subagent_parent",
        "subagent_shared",
        "code_mode",
        "local_state",
        "mcp",
    ]
    entry_fingerprint: str


_FILESYSTEM = frozenset(
    {
        "edit_file",
        "read_file",
        "search_content",
        "find_files",
        "view_image",
        "write_file",
    }
)
_MEMORY_MUTATION = frozenset({"remember", "mark_memory_relation"})
_MEMORY_QUERY = frozenset({"memory_get"})
_PLAN = frozenset({"ask_plan_question", "enter_plan", "exit_plan"})
_SUBAGENT_PARENT = frozenset({"run_agent", "list_agents", "inspect_agent", "stop_agent"})
_SUBAGENT_SHARED = frozenset({"send_agent_message", "wait_agent"})
_CODE_MODE = frozenset({"exec", "wait_exec", "stop_exec"})
_TERMINAL_PROCESS_ACTIONS = (
    "close_stdin",
    "kill",
    "list",
    "poll",
    "submit",
    "wait",
    "write",
)
_TERMINAL_MONITOR_ACTIONS = ("cancel", "list", "register")


def builtin_tool_catalog() -> tuple[BuiltinToolCatalogEntry, ...]:
    return _BUILTIN_TOOL_CATALOG


def builtin_tool_catalog_entry(name: str) -> BuiltinToolCatalogEntry:
    try:
        return _BUILTIN_TOOL_CATALOG_BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown builtin tool catalog entry: {name}") from exc


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
    name: str, descriptor: BuiltinToolDescriptor
) -> BuiltinToolCatalogEntry:
    binding_kind, availability_kind, owners, family = _catalog_shape(name)
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
        contract_id=f"pulsara.{origin.value}.{name}",
        contract_version="v2" if name == "exit_plan" else "v1",
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
    if name in SESSION_QUERY_TOOL_NAMES:
        return (BuiltinToolBindingKind.SESSION_QUERY,
                BuiltinToolAvailabilityKind.REQUIRES_SESSION_QUERY_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "session_query")
    if name in {"artifact_read", "artifact_export"}:
        return (
            BuiltinToolBindingKind.ARTIFACT_READ
            if name == "artifact_read" else BuiltinToolBindingKind.ARTIFACT_EXPORT,
            BuiltinToolAvailabilityKind.REQUIRES_ARTIFACT_READ_PORT,
            both,
            "artifact",
        )
    if name in {"reload_capabilities", "manage_capability"}:
        return (
            BuiltinToolBindingKind.PLUGIN_CONTROL,
            BuiltinToolAvailabilityKind.ALWAYS,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "plugin_control",
        )
    if name in {"list_capabilities", "inspect_capability"}:
        return (BuiltinToolBindingKind.MCP_CATALOG, BuiltinToolAvailabilityKind.ALWAYS, both, "mcp")
    if name in {
        "get_mcp_prompt",
        "use_new_mcp_tool",
        "read_mcp_resource",
    }:
        return (
            BuiltinToolBindingKind.MCP_CATALOG,
            BuiltinToolAvailabilityKind.REQUIRES_MCP_PORT,
            both,
            "mcp",
        )
    if name == "open_in_pulsara":
        return (BuiltinToolBindingKind.FILESYSTEM, BuiltinToolAvailabilityKind.ALWAYS,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "filesystem")
    if name in _FILESYSTEM:
        return (
            BuiltinToolBindingKind.FILESYSTEM,
            BuiltinToolAvailabilityKind.ALWAYS,
            both,
            "filesystem",
        )
    if name == "terminal":
        return (
            BuiltinToolBindingKind.TERMINAL_COMMAND,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name == "terminal_process":
        return (
            BuiltinToolBindingKind.TERMINAL_PROCESS,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name == "terminal_monitor":
        return (
            BuiltinToolBindingKind.TERMINAL_MONITOR,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name == "scheduled_tasks":
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
    if name == "memory_search":
        return (
            BuiltinToolBindingKind.MEMORY_RECALL,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_RECALL_PORT,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "memory_read",
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
    if name in _SUBAGENT_SHARED:
        return (
            BuiltinToolBindingKind.SUBAGENT_CONTROL,
            BuiltinToolAvailabilityKind.REQUIRES_SHARED_AGENT_CONTROL,
            both,
            "subagent_shared",
        )
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
    elif binding_kind is BuiltinToolBindingKind.TERMINAL_PROCESS:
        terminal_kind = BuiltinTerminalPermissionRuleKind.CLOSED_ACTION_SET
        access_actions = _TERMINAL_PROCESS_ACTIONS
        scheduling_actions = ()
    elif binding_kind is BuiltinToolBindingKind.TERMINAL_MONITOR:
        terminal_kind = BuiltinTerminalPermissionRuleKind.CLOSED_ACTION_SET
        access_actions = _TERMINAL_MONITOR_ACTIONS
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
    if name in {"terminal", "terminal_monitor", "terminal_process"}:
        severity = "terminal"
    elif name in {"edit_file", "write_file", "artifact_export"}:
        severity = "bounded_write"
    elif name in SESSION_QUERY_TOOL_NAMES or name in {
        "artifact_read",
        "open_in_pulsara",
        "list_capabilities",
        "inspect_capability",
        "get_mcp_prompt",
        "read_file",
        "view_image",
        "read_mcp_resource",
        "reload_capabilities",
        "search_content",
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
