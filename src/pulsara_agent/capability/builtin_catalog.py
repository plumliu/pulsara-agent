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
            "based_on_memory_ids": {
                "description": "Saved items this statement depends on, rather than merely "
                "related items, paraphrases or wider-scope copies. GLOBAL "
                "may depend only on GLOBAL; CURRENT_PROJECT may depend on "
                "GLOBAL or this project's memories. Deleting any basis "
                "also deletes this dependent item.",
                "items": {
                    "description": "Exact memory_id from a memory result.",
                    "minLength": 1,
                    "type": "string",
                },
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
        "memory_id": {
            "description": "Exact memory_id from a memory result or reference.",
            "minLength": 1,
            "type": "string",
        }
    },
    required=["memory_id"],
)
_REMEMBER_PARAMETERS = _remember_parameters()
_MARK_MEMORY_RELATION_PARAMETERS = object_schema(
    properties={
        "relation_kind": {"enum": ["CONTRADICTS", "SUPERSEDES"], "type": "string"},
        "source_memory_id": {
            "description": "Exact memory_id from a memory result; for SUPERSEDES, the "
            "replacement that remains active.",
            "minLength": 1,
            "type": "string",
        },
        "target_memory_id": {
            "description": "Exact memory_id from a memory result; for SUPERSEDES, the "
            "item made inactive.",
            "minLength": 1,
            "type": "string",
        },
    },
    required=["source_memory_id", "target_memory_id", "relation_kind"],
)

_SUBAGENT_TASK_DESCRIPTION = (
    "Self-contained objective, deliverable, constraints and exact source or file locations; up to "
    "65536 UTF-8 bytes."
)
_SUBAGENT_PROFILE_DESCRIPTION = (
    "Working style: general_worker executes; research_worker investigates with sources; "
    "review_worker finds defects; verification_worker runs checks; synthesizer combines supplied "
    "findings."
)
_SUBAGENT_CONTEXT_DESCRIPTION = (
    "Omit for no history. last_n adds recent ROOT conversation turns, excluding tool calls and "
    "results. worker_history adds a started, finished worker's readable public history from this "
    "conversation. The new worker uses current tools and permissions."
)


def _subagent_context_schema() -> dict[str, Any]:
    # Describe the existing _parse_context variants; runtime history eligibility
    # remains owned by subagent admission, not by this static schema.
    fields = {
        "none": {},
        "last_n": {
            "turns": {
                "type": "integer", "minimum": 1, "maximum": 3,
            },
        },
        "worker_history": {
            "task_id": {
                "type": "string", "minLength": 1,
            },
        },
    }
    return {
        "description": _SUBAGENT_CONTEXT_DESCRIPTION,
        "oneOf": [
            object_schema(
                properties={"mode": {"const": mode}, **properties},
                required=["mode", *properties],
            )
            for mode, properties in fields.items()
        ],
    }


def _subagent_model_schema() -> dict[str, Any]:
    variants = {
        "effort": {"value": {"type": ["string", "null"]}},
        "toggle": {"enabled": {"type": "boolean"}},
        "budget_tokens": {"tokens": {"type": "integer", "minimum": 1}},
    }
    return {
        **object_schema(
            properties={
                "connection_id": {"type": "string", "description": "Exact connection_id from list_agent_models."},
                "reasoning": {
                    "description": "Copy a supported choice from list_agent_models; effort.value may be null when listed. Omit for this connection's default.",
                    "oneOf": [
                        object_schema(
                            properties={"kind": {"const": kind}, **properties},
                            required=["kind", *properties],
                        )
                        for kind, properties in variants.items()
                    ],
                },
            },
            required=["connection_id"],
        ),
        "description": "Omit to inherit the assigning agent's model and reasoning; otherwise select a saved connection.",
    }


def _wait_agent_schema() -> dict[str, Any]:
    timeout = {
        "type": "number", "minimum": 0, "maximum": 300, "default": 30,
        "description": "0 returns an immediate snapshot.",
    }
    return {
        "type": "object",
        "oneOf": [
            object_schema(properties={"timeout_seconds": timeout}, required=[]),
            object_schema(
                properties={
                    "task_ids": {
                        "type": "array", "minItems": 1, "maxItems": 16,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 512},
                        "description": "Exact task IDs returned by spawn_agent, create_agent_tasks or list_agents.",
                    },
                    "settle": {
                        "type": "string", "enum": ["all", "first"], "default": "all",
                    },
                    "timeout_seconds": timeout,
                },
                required=["task_ids"],
            ),
        ],
    }


OPEN_IN_PULSARA_DESCRIPTION = (
    "ROOT only. Show files, webpages, subagent tasks, existing terminal "
    "processes, MCP connections or overview panels in this conversation's tabs. "
    "Group related targets in one call; the last accepted target is selected. "
    "Invalid targets are reported by zero-based input index while valid targets "
    "proceed. The result acknowledges display requests; display occurs afterward "
    "and does not return webpage contents. The tasks panel shows subagents. A "
    "browser panel target creates a blank tab; duplicates share one tab within a "
    "call and later calls create new tabs."
)
OPEN_IN_PULSARA_SCHEMA = object_schema(
    required=["targets"],
    properties={"targets": {
        "type": "array",
        "minItems": 1,
        "items": {"anyOf": [
            object_schema(required=["type", "path"], properties={
                "type": {"const": "file"},
                "path": {"type": "string", "minLength": 1, "description": (
                    'File path; workspace-relative, absolute, ~ and ${PULSARA_HOME} paths are supported.'
                )},
            }),
            object_schema(required=["type", "url"], properties={
                "type": {"const": "browser"},
                "url": {"type": "string", "minLength": 1,
                        "description": 'HTTP(S) URL without embedded credentials.'},
            }),
            object_schema(required=["type", "process_id"], properties={
                "type": {"const": "terminal"},
                "process_id": {"type": "string", "minLength": 1,
                               "description": 'process_id from terminal or terminal_process.'},
            }),
            object_schema(required=["type", "task_id"], properties={
                "type": {"const": "task"},
                "task_id": {"type": "string", "minLength": 1, "description": (
                    'task_id from spawn_agent, create_agent_tasks or list_agents; opens its group and selects the task.'
                )},
            }),
            object_schema(required=["type", "runtime_server_id"], properties={
                "type": {"const": "mcp"},
                "runtime_server_id": {"type": "string", "minLength": 1,
                                      "description": 'runtime_server_id from inspect_capability.'},
            }),
            object_schema(required=["type", "name"], properties={
                "type": {"const": "panel"},
                "name": {"type": "string", "enum": ["capabilities", "tasks", "background", "browser"],
                         },
            }),
        ]},
    }},
)


_BUILTIN_DESCRIPTORS: dict[str, BuiltinToolDescriptor] = {
    "open_in_pulsara": _descriptor(
        name="open_in_pulsara", description=OPEN_IN_PULSARA_DESCRIPTION,
        input_schema=OPEN_IN_PULSARA_SCHEMA, is_read_only=True, permission_category="filesystem_read",
    ),
    "list_capabilities": _descriptor(
        name="list_capabilities",
        description=(
            'Find Skills, Hooks, MCP servers, tools, resources, templates and prompts. Use {} for '
            "an overview, or filter the list. Copy an item's target into inspect_capability; use "
            "source.target as parent to find a Plugin's components. Continue with next_offset and "
            'the same filters. Results describe the observed catalog and its completeness. SUBAGENT'
            ' callers can query only their assigned MCP runtime.'
        ),
        input_schema=list_input_schema(),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "inspect_capability": _descriptor(
        name="inspect_capability",
        description=(
            'Inspect a known capability target; list_capabilities supplies copyable targets. Read '
            'Skill bodies with read_file. For MCP tools, follow invocation.mode: DIRECT uses the '
            'listed named tool; META uses the returned input_schema and tool_ref with '
            'use_new_mcp_tool, including when an older named entry remains listed; UNAVAILABLE '
            'gives the current obstacle. This reads metadata. Use read_mcp_resource for resource '
            'content and get_mcp_prompt for rendered prompts.'
        ),
        input_schema=inspect_input_schema(),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "manage_capability": _descriptor(
        name="manage_capability",
        description=(
            'Install and manage capabilities requested by the user. ROOT agent only; available in '
            'every permission mode. action and scope are required. Send only the additional fields '
            'listed for that action; fields are required unless marked optional.\n'
            'INSTALL_LOOSE_SKILL: source_path; optional name, description. Install an independent '
            'Skill into a new destination.\n'
            'SET_LOOSE_SKILL_ENABLED: skill_path, enabled. Toggle an eligible independent Skill.\n'
            'REMOVE_LOOSE_SKILL: skill_path. Remove an independent Skill directory after inspecting'
            ' removal eligibility.\n'
            'TRUST_HOOK_SOURCE: source_kind; optional plugin_id. Review and trust current Hook '
            'definitions.\n'
            'REVOKE_HOOK_TRUST: source_kind; optional plugin_id. Remove trust, keeping definitions.'
            '\n'
            'SET_HOOK_SOURCE_ENABLED: source_kind, enabled; optional plugin_id. Toggle the Hook '
            'source.\n'
            'ADD_LOCAL_MCP: server_id; optional config. Add an independent connection.\n'
            'UPDATE_LOCAL_MCP: server_id; optional config, expected_identity. Replace an '
            "independent connection's settings.\n"
            'REMOVE_LOCAL_MCP: server_id; optional expected_identity. Remove the entry and '
            'dedicated local credentials, keeping the server program.\n'
            'INSTALL_PLUGIN: source_path; optional replace, source_format. Install the whole Plugin'
            ' disabled; enable separately.\n'
            'SET_PLUGIN_ENABLED: plugin_id, enabled; optional expected_package_install_id. Toggle '
            'the whole Plugin, with component review on enable.\n'
            'REMOVE_PLUGIN: plugin_id; optional expected_package_install_id. Remove the Plugin and '
            'dedicated credentials, preserving its data and original source.\n'
            'CONFIGURE_PLUGIN_MCP_CONNECTION: plugin_id, server_id; optional overlay, '
            'expected_overlay, expected_package_install_id. Configure a Plugin connection.\n'
            'AUTHORIZE_MCP: server_id; optional plugin_id, expected_identity, '
            'expected_package_install_id. Authorize an already configured OAuth connection.\n'
            'CLEAR_MCP_AUTHORIZATION: server_id; optional plugin_id, expected_identity, '
            'expected_package_install_id. Clear local OAuth grants, keeping configuration and '
            'remote tokens.\n'
            'Use list_capabilities/inspect_capability for identities and eligibility. Loose-Skill '
            'actions apply to independent copies; Plugin components are managed through their '
            'Plugin, and bundled Skills are read-only. Hook trust, Hook enablement and Plugin '
            'enablement are separate settings. Edit loose Skill content with file tools or '
            'terminal; pulsara-skill-creator describes authoring.\n'
            'The runtime collects required review and confirmation; READ_ONLY mutations use a user-'
            'submitted form. Users enter credentials privately in the editor. Wait for the result: '
            'status reports the mutation, current the resulting state, and adoption its '
            'availability for future use. A completed mutation needs no routine reload or replay.'
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
            "Read a page of saved tool output using its artifact_id. Continue with "
            "next_offset_chars while has_more is true; pages may be shorter than "
            "max_chars. source_coverage identifies whether the body covers the original "
            "observation or only its retained portion; offsets address that saved body. "
            "Use artifact_export for extensive retrieval or processing."
        ),
        input_schema=object_schema(
            properties={
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        'artifact_id returned by a tool result in this conversation and project.'
                    ),
                },
                "offset_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": (
                        'Zero-based character offset; copy next_offset_chars to continue.'
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32_000,
                    "default": DEFAULT_ARTIFACT_READ_CHARS,
                },
            },
            required=["artifact_id"],
        ),
        is_read_only=True,
        permission_category="artifact_read",
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "artifact_export": _descriptor(
        name="artifact_export",
        description=(
            "Export a saved tool-result artifact to a new UTF-8 file for searching, "
            "processing or extensive retrieval. artifact_read provides paged excerpts "
            "instead; a prior page read is unnecessary. The file contains the saved body "
            "itself, including plain output text for terminal artifacts. Its coverage is "
            "the original observation or its retained portion, as reported by "
            "source_coverage. Parent directories are created; an existing path is left "
            "unchanged. The exported file remains independently of the conversation."
        ),
        input_schema=object_schema(
            properties={
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": 'artifact_id returned by a tool result in this conversation and project.',
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": 'New file path, relative to the workspace or absolute; ~ is supported.',
                },
            },
            required=["artifact_id", "path"],
        ),
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "use_new_mcp_tool": _descriptor(
        name="use_new_mcp_tool",
        description=(
            'Execute an MCP tool inspected with invocation.mode=META, using its returned '
            'input_schema even when an older same-name tool remains listed. The remote action may '
            'require confirmation. Reuse tool_ref for later calls; inspect again if it is rejected.'
        ),
        input_schema=object_schema(
            properties={
                "tool_ref": {
                    "type": "string",
                    "pattern": "^mcpref_[A-Za-z0-9_-]+$",
                    "maxLength": 160,
                    "description": (
                        'Exact inspected tool_ref, scoped to this agent and conversation.'
                    ),
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        'Arguments matching the inspected input_schema; use {} when no inputs are needed.'
                    ),
                },
            },
            required=["tool_ref", "arguments"],
        ),
        is_read_only=False,
        permission_category="mcp_dynamic",
        artifact_mode=ToolArtifactMode.DEFAULT,
    ),
    "read_mcp_resource": _descriptor(
        name="read_mcp_resource",
        description=(
            'Fetch one listed MCP resource. Use a fixed URI or fill a listed URI template. This '
            'returns external reference content. If the preview supplies artifact_id, continue '
            'through artifact_read: it pages the saved result. Another remote read fetches fresh '
            'content, not the next page.'
        ),
        input_schema=object_schema(
            properties={
                "server_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": (
                        'Copy from the resource or template listing that supplied the URI.'
                    ),
                },
                "uri": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 32768,
                    "description": (
                        'Exact listed resource URI, or a concrete URI with all listed template variables '
                        'filled.'
                    ),
                },
            },
            required=["server_id", "uri"],
        ),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "get_mcp_prompt": _descriptor(
        name="get_mcp_prompt",
        description=(
            'Fetch and render a prompt advertised by list_capabilities. Use the listed name as '
            'prompt_name and copy server_id from that same item. The returned messages are external'
            ' guidance for the current request; fetching them does not execute tools.'
        ),
        input_schema=object_schema(
            properties={
                "server_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "prompt_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8192,
                },
                "arguments": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        'Use declared argument names, including every required name, with string values. Omit '
                        'or use {} when none are needed.'
                    ),
                },
            },
            required=["server_id", "prompt_name"],
        ),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "read_file": _descriptor(
        name="read_file",
        description=(
            "Read lines from a local UTF-8 file and obtain its current content_revision "
            "for edit_file. Lines are displayed as N|text; N| is a line label, while text"
            " is the file content. Read the lines or adjacent gaps needed for line edits;"
            " a whole-file replacement needs the current revision but not a full display."
            " Follow the returned offset hint for truncated output."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Local text path; relative paths start at the workspace. ../, absolute paths,"
                        " ~ and cataloged ${PULSARA_HOME}/ paths are supported."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": (
                        'First line to return, numbered from 1.'
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "default": DEFAULT_READ_LINES,
                    "maximum": MAX_READ_LINES,
                },
            },
            required=["path"],
        ),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "view_image": _descriptor(
        name="view_image",
        description=(
            "Inspect a local PNG, JPEG, static WebP or SVG, or capture a local HTML file "
            "as an image for the model. Supply exactly one of path or image_ref. SVG is "
            "rendered without cropping, with scripts and external resources disabled. "
            "HTML uses a 1200×800 light-theme viewport with reduced motion and network "
            "access disabled, capturing the content root or viewport. The image is a "
            "still view of the current file. Use open_in_pulsara to show content to the "
            "user."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        'Local image or HTML path; workspace-relative, ../, absolute and ~ paths are supported.'
                    ),
                },
                "image_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        'Exact image: reference from an earlier image or historical image placeholder in this workspace.'
                    ),
                },
            },
            required=[],
        ),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "search_content": _descriptor(
        name="search_content",
        description=(
            "Search text with a Rust regular expression. content returns matching lines; "
            "files_only returns paths; count returns matching-line counts per file. "
            "Results are sorted by path and line and paginate in every mode. read_file "
            "provides the revision and line context needed for edits."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "default": ".",
                    "description": (
                        "File or directory; relative to the workspace or an explicit external path. "
                        "Broad external roots are rejected."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_LIMIT,
                    "default": DEFAULT_SEARCH_LIMIT,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": 'Zero-based result offset; count mode pages files. Use the returned continuation hint.',
                },
                "pattern": {"type": "string", "minLength": 1},
                "file_glob": {
                    "type": "string",
                    "minLength": 1,
                    "description": 'Basename or relative-path glob, also applied to a single-file query.',
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_only", "count"],
                    "default": "content",
                },
            },
            required=["pattern"],
        ),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "find_files": _descriptor(
        name="find_files",
        description=(
            "Find paths by basename or relative-path glob without reading file contents. "
            "*.py matches all depths; src/*.py matches one level; src/**/*.py is "
            "recursive. Use *name* for a name fragment. Results are sorted by path; "
            "continue with the returned offset hint."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "default": ".",
                    "description": (
                        "File or directory; relative to the workspace or an explicit external path. "
                        "Broad external roots are rejected."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_LIMIT,
                    "default": DEFAULT_SEARCH_LIMIT,
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": 'Zero-based file offset; use the returned continuation hint.',
                },
                "glob": {"type": "string", "minLength": 1},
            },
            required=["glob"],
        ),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "edit_file": _descriptor(
        name="edit_file",
        description=(
            "Edit an existing UTF-8 file using the content_revision from read_file. All "
            "line anchors are 1-based in that original revision; ranges are inclusive and"
            " operations use distinct, non-overlapping ranges or gaps. Line edits require"
            " the affected lines or adjacent gaps to have been read. replace_file "
            "replaces the whole file as the sole operation and needs a current revision, "
            "without requiring the whole file to be displayed. Results provide a diff, "
            "the new revision and changed line windows; NO_OP means no bytes changed. Use"
            " write_file for a new path."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        'Existing file path, relative to the workspace or absolute; ~ is supported.'
                    ),
                },
                "base_revision": {
                    "type": "string",
                    "pattern": "^revision:[a-z2-7]{51}[aq]$",
                    "description": (
                        'Exact content_revision returned by read_file for this path.'
                    ),
                },
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "items": _edit_operation_schema(),
                    "description": (
                        "Replacement text is literal file content without read_file line labels. Each"
                        " lines item contains one line without CR, LF or NUL; an empty string is a "
                        "blank line."
                    ),
                },
            },
            required=["path", "base_revision", "operations"],
        ),
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "write_file": _descriptor(
        name="write_file",
        description=(
            "Create a new UTF-8 text file, creating parent directories as needed. "
            "Existing paths are left unchanged. Use read_file followed by edit_file to "
            "change or replace an existing file."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        'New file path, relative to the workspace or absolute; ~ is supported.'
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        'Complete file text; an empty string creates an empty file.'
                    ),
                },
            },
            required=["path", "content"],
        ),
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
            "Find saved conversations by title or message keywords; omit query to list by "
            "activity. This session's message hits cover only earlier content removed by "
            "compaction; other accessible sessions use saved ROOT conversation history. "
            "Body hits provide session_id and entry_id for read_session_content; "
            "title-only hits have no entry.\n"
            "First page: optional query, lifecycle, limit.\n"
            "Continue: cursor; optional limit. Send no other fields on continuation. Omit "
            "unused fields; null is not accepted."
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
            "Find saved questions and their answer process by keyword relevance. Omit "
            "session_id for this conversation's earlier messages removed by compaction; "
            "messages still in context are excluded. Other accessible conversations use "
            "saved ROOT conversation history. Results preserve speakers, times and "
            "historical quotes/comments; [truncated] marks omitted text. Each item's "
            "entry_id anchors its question; use a message's entry_id with "
            "read_session_content to expand that message. Images are presence markers; "
            "generated markers and image metadata are not search terms.\n"
            "First page: query; optional session_id, include_tools, limit.\n"
            "Continue: cursor; optional limit. Send no other fields on continuation. Omit "
            "unused fields; null is not accepted."
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
            "Read saved messages directly or expand a search hit; searching first is "
            "optional. Omit session_id for this conversation's earlier messages removed "
            "by compaction. Other accessible conversations use saved ROOT conversation "
            "history. An entry_id anchor is read first. Long messages start at the "
            "beginning and continue across pages before adjacent messages; partial marks "
            "a slice. Tool output may have missing saved content as shown by "
            "source_coverage, display_kind and artifact_disposition; pagination cannot "
            "recover it. Images are text placeholders; only same-workspace placeholders "
            "include image_ref. Increase max_chars if a placeholder is incomplete.\n"
            "First page: optional session_id, entry_id, direction, include_tools, limit, "
            "max_chars.\n"
            "Continue: cursor; optional limit, max_chars. Send no other fields on "
            "continuation. Omit unused fields; null is not accepted."
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
            'Manage saved schedules in the ROOT conversation. Set action and send only its listed '
            'fields; fields are required unless marked optional.\n'
            'list: optional session_id, status, cursor.\n'
            'get: task_id.\n'
            'create: values; optional session_id, status.\n'
            'update: task_id, expected_revision, values.\n'
            'pause: task_id, expected_revision.\n'
            'resume: task_id, expected_revision.\n'
            'delete: task_id, expected_revision.\n'
            'switch: task_id, expected_revision, target.\n'
            'Use list/get to find existing work and obtain its current values and revision. Update '
            'an existing task or create a separate task as requested. Reread on revision conflict.\n'
            'values is the complete configuration; preserve unrequested fields on update. Choose a '
            'short name if unspecified. prompt contains self-contained future instructions and '
            'output expectations; timing and permissions use their own fields. timezone is an IANA '
            'name, defaulting to Asia/Shanghai for new tasks unless requested otherwise. For '
            'create, use current permissions or a requested narrower preset. Saving requires '
            'current permissions to cover permission_mode; preserve saved permissions unless a '
            'change is requested.\n'
            'Each session supports one ACTIVE plan and any number of PAUSED plans. create with '
            'omitted/null status saves PAUSED; use ACTIVE to start automatic scheduling. An '
            'authorized replacement uses switch to atomically pause the source ACTIVE plan and '
            'activate an existing PAUSED target or create an ACTIVE target in the same session. '
            'Adding a task alone does not request replacement. Separate automatic plans need '
            'separate sessions.\n'
            'Calendar rules use local HH:mm and YYYY-MM-DD start_date in the saved timezone; '
            'weekdays are Monday=1 through Sunday=7. once and interval timestamps are whole-second '
            'UTC; new or changed once times must be in the future. Intervals use their fixed '
            'anchor. Missing month days and nonexistent DST times are skipped; repeated DST times '
            'fire once. Explain saved times in ordinary language.\n'
            'Edits preserve queued instructions. pause/delete/switch cancel the affected source '
            "task's pending inputs and leave running replies alone; delete keeps the chat. Stopping"
            ' a reply leaves its schedule active. Resume uses the next future recurrence without '
            'catch-up; an overdue untriggered once plan becomes due.'
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
    "list_agent_models": _descriptor(
        name="list_agent_models",
        description=(
            "List saved model connections and their supported reasoning choices for delegated tasks. Use "
            "the returned connection IDs and choices when selecting a different worker model."
        ),
        input_schema=object_schema(properties={}, required=[]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True,
        permission_category="subagent_runtime",
    ),
    "spawn_agent": _descriptor(
        name="spawn_agent",
        description=(
            "Delegate one self-contained task and return its task_id and initial status. Execution is "
            "asynchronous and may queue for capacity. Use create_agent_tasks for batches or success "
            "dependencies. context selects history for the new worker; worker_history creates a new task "
            "continuing a finished worker's public history. Workers share the workspace. See the "
            "pulsara-subagent Skill for workflow examples."
        ),
        input_schema=object_schema(
            properties={
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 65536,
                    "description": _SUBAGENT_TASK_DESCRIPTION,
                },
                "task_name": {
                    "type": "string",
                    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                    "description": (
                        "Case-sensitive display name for this task."
                    ),
                },
                "profile": {
                    "type": "string",
                    "enum": [
                        "general_worker",
                        "research_worker",
                        "review_worker",
                        "verification_worker",
                        "synthesizer",
                    ],
                    "default": "general_worker",
                    "description": _SUBAGENT_PROFILE_DESCRIPTION,
                },
                "context": _subagent_context_schema(),
                "model": _subagent_model_schema(),
                "material_task_ids": {"type": "array", "maxItems": 16, "uniqueItems": True, "items": {"type": "string", "minLength": 1}, "description": "Exact IDs of finished tasks in this conversation whose results or public failure details should be supplied as material."},
            },
            required=["task"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="subagent_runtime",
    ),
    "wait_agent": _descriptor(
        name="wait_agent",
        description=(
            "Wait for delegated task completion. Results arrive as separate conversation messages after "
            "this call.\nWith task_ids: optional settle and timeout_seconds. settle=first waits for any "
            "target to finish; settle=all waits for every target.\nWithout task_ids: optional "
            "timeout_seconds is the only field; wait for any ready result or for nothing to remain "
            "pending.\nFinished includes success, failure and cancellation. Current-reply guidance "
            "interrupts the wait with steer_available; later-reply queued guidance does not. timeout ends "
            "this wait while tasks continue."
        ),
        input_schema=_wait_agent_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="subagent_runtime",
    ),
    "stop_agent": _descriptor(
        name="stop_agent",
        description=(
            "Cancel a queued, dependency-waiting or running task and return its status. Finished tasks "
            "retain their final state. Cancelling a prerequisite blocks its dependents. Existing side "
            "effects, unrelated tasks and background commands remain."
        ),
        input_schema=object_schema(
            properties={
                "task_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Exact task_id returned by spawn_agent, create_agent_tasks or list_agents.",
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "description": (
                        "Cancellation note; stored with the call, not delivered as a worker message."
                    ),
                },
            },
            required=["task_id"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="subagent_runtime",
        is_destructive=True,
    ),
    "list_agents": _descriptor(
        name="list_agents",
        description=(
            "List this conversation's delegated tasks, including exact IDs, status, objectives, "
            "dependencies, pending-message counts and result summaries. Results are status snapshots; use "
            "wait_agent to wait for completion. Continue paging with next_cursor and unchanged max_items "
            "and include_dependencies. If a cursor is rejected, omit it to start a new listing."
        ),
        input_schema=object_schema(
            properties={
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 50,
                },
                "include_dependencies": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Include each task's prerequisites and their statuses."
                    ),
                },
                "cursor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": (
                        "Exact next_cursor from the preceding page; omit for the first page."
                    ),
                },
            },
            required=[],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True,
        permission_category="subagent_runtime",
    ),
    "create_agent_tasks": _descriptor(
        name="create_agent_tasks",
        description=(
            "Schedule tasks together and return task IDs and initial statuses. Independent tasks run in "
            "parallel as capacity becomes available. A depends_on prerequisite must succeed before its "
            "dependent starts; its result becomes task context. An unsuccessful prerequisite blocks the "
            "dependent. Use material_task_ids for finished results or failures without waiting for success, "
            "and context for conversation history. Dependencies must be acyclic, with at most 64 references "
            "in one batch. Use spawn_agent for one task. See the pulsara-subagent Skill for workflow "
            "examples."
        ),
        input_schema=object_schema(
            properties={
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_key": {
                                "type": "string",
                                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                                "description": (
                                    "Unique, case-sensitive key that other tasks in this batch can reference in depends_on."
                                ),
                            },
                            "label": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 256,
                                "description": (
                                    "Display name; dependencies use task_key or task_id."
                                ),
                            },
                            "profile": {
                                "type": "string",
                                "enum": [
                                    "research_worker",
                                    "review_worker",
                                    "verification_worker",
                                    "general_worker",
                                    "synthesizer",
                                ],
                                "description": _SUBAGENT_PROFILE_DESCRIPTION,
                                "default": "general_worker",
                            },
                            "task": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 65536,
                                "description": _SUBAGENT_TASK_DESCRIPTION,
                            },
                            "display_role": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 256,
                                "description": (
                                    "Display-only role; profile selects working style."
                                ),
                            },
                            "context": _subagent_context_schema(),
                            "model": _subagent_model_schema(),
                            "material_task_ids": {"type": "array", "maxItems": 16, "uniqueItems": True, "items": {"type": "string", "minLength": 1}, "description": "Exact IDs of finished tasks in this conversation whose results or public failure details should be supplied as material."},
                            "depends_on": {
                                "type": "array",
                                "maxItems": 16,
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                                "description": (
                                    "Prerequisites: another item's exact task_key in this batch, or an earlier task's complete task_id. Omit or use [] for independent work."
                                ),
                            },
                        },
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                }
            },
            required=["tasks"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="subagent_runtime",
    ),
    "send_agent_message": _descriptor(
        name="send_agent_message",
        description=(
            "Send additional guidance to an ACTIVE worker by task_id. A queued result means the message is "
            "accepted for delivery; the worker may read it later. For follow-up after a worker finishes, "
            "use spawn_agent with context.mode=worker_history and its old task_id."
        ),
        input_schema=object_schema(
            properties={
                "task_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Exact task_id returned by spawn_agent, create_agent_tasks or list_agents.",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 16384,
                    "description": (
                        "Self-contained guidance, evidence or correction; up to 16384 UTF-8 bytes."
                    ),
                },
            },
            required=["task_id", "message"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="subagent_runtime",
    ),
    "report_agent_result": _descriptor(
        name="report_agent_result",
        description=(
            "Finish your worker task and deliver its outcome to the ROOT agent. Use this as the only tool "
            "call in the response when the work is complete. Provide a self-contained summary with "
            "evidence, constraints and useful file or artifact locations. Optional data carries structured "
            "results; output_preview and diagnostics provide supporting detail."
        ),
        input_schema=object_schema(
            properties={
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 16384,
                    "description": (
                        "Final conclusion, evidence, constraints and useful source locations; understandable on its own."
                    ),
                },
                "data": {"type": "object", "description": "Structured result. Summary UTF-8 bytes plus canonical data JSON share a 16 KiB budget."},
                "output_preview": {
                    "type": "string",
                    "maxLength": 32768,
                    "description": (
                        "Supporting excerpts, changed files or command outcomes; up to 32768 UTF-8 bytes."
                    ),
                },
                "diagnostics": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "object"},
                    "description": (
                        "Structured findings; canonical JSON is limited to 8 KiB per item and 64 KiB for the array."
                    ),
                },
            },
            required=["summary"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="agent_local",
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
            "readable memory contexts. Use memory_get when you already have the "
            "memory_id. retrieval_summary reports search completeness, filter expansion, "
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
            "Read a saved memory by its exact ID, including its statement, kind, context, "
            "recorded time, lifecycle, direct relations and any user-edit note. Use "
            "memory_search to discover an ID."
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
            "memory_id; ALREADY_PRESENT returns the existing item without changing its "
            "origin or dependencies. Users can delete saved items on the Memory page."
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
            "Both must be active and in the same context. Related memories returned by "
            "remember are suggestions to inspect, not established relations.\n"
            "CONTRADICTS: keep two incompatible memories of the same kind active when "
            "neither is the supported replacement.\n"
            "SUPERSEDES: the source is the supported replacement, including a corrected "
            "statement or kind; make the target inactive without deleting it."
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
    REQUIRES_CHILD_REPORT_CONTROL = "requires_child_report_control"
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
        "subagent_child",
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
_SUBAGENT_PARENT = frozenset(
    {
        "create_agent_tasks",
        "list_agents",
        "list_agent_models",
        "spawn_agent",
        "stop_agent",
        "send_agent_message",
        "wait_agent",
    }
)
_SUBAGENT_CHILD = frozenset({"report_agent_result"})
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
            if name in _SUBAGENT_PARENT | _SUBAGENT_CHILD
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
        ToolInvocationOwnerKind.SUBAGENT_CHILD,
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
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
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
            both,
            "memory_read",
        )
    if name in _MEMORY_QUERY:
        return (
            BuiltinToolBindingKind.MEMORY_QUERY,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_QUERY_PORT,
            both,
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
    if name in _SUBAGENT_CHILD:
        return (
            BuiltinToolBindingKind.SUBAGENT_CONTROL,
            BuiltinToolAvailabilityKind.REQUIRES_CHILD_REPORT_CONTROL,
            (ToolInvocationOwnerKind.SUBAGENT_CHILD,),
            "subagent_child",
        )
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
SUBAGENT_SYSTEM_TOOL_NAMES = frozenset(
    entry.name
    for entry in _BUILTIN_TOOL_CATALOG
    if entry.tool_family == "subagent_parent"
)
SUBAGENT_CHILD_REPORT_TOOL_NAMES = frozenset(
    entry.name
    for entry in _BUILTIN_TOOL_CATALOG
    if entry.tool_family == "subagent_child"
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
    "SUBAGENT_CHILD_REPORT_TOOL_NAMES",
    "SUBAGENT_SYSTEM_TOOL_NAMES",
    "TERMINAL_TOOL_NAMES",
    "builtin_tool_catalog",
    "builtin_tool_catalog_entry",
    "builtin_action_permission_override",
    "builtin_availability_requirement_identity_fingerprint",
    "builtin_permission_contract_identity_fingerprint",
    "builtin_tool_descriptors",
]
