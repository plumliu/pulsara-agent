"""Single descriptor, binding, permission, and taxonomy catalog for built-ins."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityDescriptor,
    CapabilityProviderKind,
)
from pulsara_agent.ports.artifact import ToolArtifactMode
from pulsara_agent.capability.result_contracts import result_render_contract_for_tool
from pulsara_agent.capability.tool_action import (
    fixed_tool_action_policy,
    terminal_monitor_tool_action_policy,
    terminal_process_tool_action_policy,
    terminal_tool_action_policy,
)
from pulsara_agent.ports.tool_execution import ToolInvocationOwnerKind
from pulsara_agent.ports.tool_registry import (
    BuiltinToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import LongHorizonActionClass
from pulsara_agent.ports.terminal import (
    TERMINAL_MONITOR_TOOL_DESCRIPTION,
    TERMINAL_PROCESS_TOOL_DESCRIPTION,
    TERMINAL_TOOL_DESCRIPTION,
    terminal_monitor_input_schema,
    terminal_input_schema,
    terminal_process_input_schema,
)


DEFAULT_ARTIFACT_READ_CHARS = 20_000
DEFAULT_READ_LINES = 500
MAX_READ_LINES = 2_000
DEFAULT_SEARCH_LIMIT = 50
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


class BuiltinToolLongHorizonPolicyKind(StrEnum):
    EVIDENCE_HYDRATION = "evidence_hydration"
    EVIDENCE_ACQUISITION = "evidence_acquisition"
    SYNTHESIS_MUTATION = "synthesis_mutation"
    PROCESS_CONTROL = "process_control"
    USER_INTERACTION = "user_interaction"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_PROCESS = "terminal_process"
    TERMINAL_MONITOR = "terminal_monitor"


_LONG_HORIZON_POLICY_KIND_BY_NAME = {
    "artifact_read": BuiltinToolLongHorizonPolicyKind.EVIDENCE_HYDRATION,
    "ask_plan_question": BuiltinToolLongHorizonPolicyKind.USER_INTERACTION,
    "create_agent_tasks": BuiltinToolLongHorizonPolicyKind.EVIDENCE_ACQUISITION,
    "edit_file": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "enter_plan": BuiltinToolLongHorizonPolicyKind.USER_INTERACTION,
    "exit_plan": BuiltinToolLongHorizonPolicyKind.USER_INTERACTION,
    "list_agents": BuiltinToolLongHorizonPolicyKind.EVIDENCE_HYDRATION,
    "memory_explain": BuiltinToolLongHorizonPolicyKind.EVIDENCE_HYDRATION,
    "memory_get": BuiltinToolLongHorizonPolicyKind.EVIDENCE_HYDRATION,
    "memory_search": BuiltinToolLongHorizonPolicyKind.EVIDENCE_ACQUISITION,
    "read_file": BuiltinToolLongHorizonPolicyKind.EVIDENCE_ACQUISITION,
    "remember_action_boundary": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "remember_claim": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "remember_decision": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "remember_observation": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "remember_preference": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "report_agent_phase": BuiltinToolLongHorizonPolicyKind.PROCESS_CONTROL,
    "report_agent_result": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "search_files": BuiltinToolLongHorizonPolicyKind.EVIDENCE_ACQUISITION,
    "spawn_agent": BuiltinToolLongHorizonPolicyKind.EVIDENCE_ACQUISITION,
    "stop_agent": BuiltinToolLongHorizonPolicyKind.PROCESS_CONTROL,
    "stop_agent_task": BuiltinToolLongHorizonPolicyKind.PROCESS_CONTROL,
    "terminal": BuiltinToolLongHorizonPolicyKind.TERMINAL_COMMAND,
    "terminal_monitor": BuiltinToolLongHorizonPolicyKind.TERMINAL_MONITOR,
    "terminal_process": BuiltinToolLongHorizonPolicyKind.TERMINAL_PROCESS,
    "todo": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
    "wait_agent": BuiltinToolLongHorizonPolicyKind.EVIDENCE_HYDRATION,
    "wait_agent_tasks": BuiltinToolLongHorizonPolicyKind.EVIDENCE_HYDRATION,
    "write_file": BuiltinToolLongHorizonPolicyKind.SYNTHESIS_MUTATION,
}

_ACTION_PERMISSION_OVERRIDE_SPECS: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
    "terminal_process": tuple(
        ("action", action, "terminal_process_observe", True)
        for action in ("list", "log", "poll", "wait")
    ),
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


def builtin_tool_descriptors() -> tuple[CapabilityDescriptor, ...]:
    return tuple(_BUILTIN_DESCRIPTORS[name] for name in sorted(_BUILTIN_DESCRIPTORS))


def _descriptor(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    provider_kind: CapabilityProviderKind = CapabilityProviderKind.BUILTIN,
    is_read_only: bool,
    is_concurrency_safe: bool,
    permission_category: str,
    artifact_mode: ToolArtifactMode = ToolArtifactMode.DEFAULT,
    is_destructive: bool = False,
    is_open_world: bool = False,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=f"{provider_kind.value}:{name}",
        name=name,
        description=description,
        input_schema=input_schema,
        namespace=None,
        provider_kind=provider_kind,
        provider_id=provider_kind.value,
        is_model_callable=True,
        is_read_only=is_read_only,
        is_concurrency_safe=is_concurrency_safe,
        is_destructive=is_destructive,
        is_open_world=is_open_world,
        permission_category=permission_category,
        result_render_contract=result_render_contract_for_tool(name),
        long_horizon_policy=_long_horizon_policy(name),
        advertise_policy=CapabilityAdvertisePolicy.DIRECT,
        artifact_mode=artifact_mode,
        metadata={"source": "explicit_builtin_descriptor"},
    )


def _long_horizon_policy(name: str):
    try:
        kind = _LONG_HORIZON_POLICY_KIND_BY_NAME[name]
    except KeyError as exc:
        raise ValueError(
            f"builtin tool lacks a catalog-owned Long-Horizon policy: {name}"
        ) from exc
    if kind is BuiltinToolLongHorizonPolicyKind.TERMINAL_COMMAND:
        return terminal_tool_action_policy()
    observe_actions = tuple(
        discriminator_value
        for discriminator_field, discriminator_value, _category, allowed in (
            _ACTION_PERMISSION_OVERRIDE_SPECS.get(name, ())
        )
        if discriminator_field == "action" and allowed
    )
    if kind is BuiltinToolLongHorizonPolicyKind.TERMINAL_PROCESS:
        return terminal_process_tool_action_policy(
            observe_actions=observe_actions,
        )
    if kind is BuiltinToolLongHorizonPolicyKind.TERMINAL_MONITOR:
        return terminal_monitor_tool_action_policy(
            observe_actions=observe_actions,
        )
    return fixed_tool_action_policy(LongHorizonActionClass(kind.value))


def _common_memory_properties() -> dict[str, Any]:
    return {
        "statement": {
            "type": "string",
            "description": "The durable memory content as a single declarative statement.",
        },
        "scope": {
            "type": "string",
            "description": "Exact visible scope this memory applies to, e.g. ctx:user or the current ctx:workspace/<id>.",
        },
        "source_authority": {
            "type": "string",
            "enum": _SOURCE_AUTHORITIES,
            "description": "Where the authority for this memory comes from.",
        },
        "verification_status": {
            "type": "string",
            "enum": _VERIFICATION_STATUSES,
            "description": "How well this memory is verified.",
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence node ids that support this memory.",
        },
    }


def _memory_parameters(
    *,
    extra_properties: dict[str, Any] | None = None,
    extra_required: list[str] | None = None,
) -> dict[str, Any]:
    properties = _common_memory_properties()
    properties.update(extra_properties or {})
    return object_schema(
        properties=properties,
        required=[
            "statement",
            "scope",
            "source_authority",
            "verification_status",
            *(extra_required or []),
        ],
    )


_MEMORY_SEARCH_PARAMETERS = object_schema(
    properties={
        "query": {
            "type": "string",
            "description": "Natural-language or lexical query for canonical durable memory.",
        },
        "scope": {
            "type": "string",
            "description": (
                "Optional exact visible memory scope. Omit this field to search all visible scopes. "
                "Only set it when the user explicitly names a scope; do not infer the current workspace."
            ),
        },
        "kind": {
            "type": "string",
            "description": (
                "Optional exact canonical memory type: Claim, Preference, Observation, ActionBoundary, or Decision. "
                "Omit unless the user explicitly names one of these types; do not infer a type from the question."
            ),
        },
        "limit": {
            "type": "integer",
            "default": 5,
            "description": "Maximum results to return.",
        },
        "max_hops": {
            "type": "integer",
            "default": 0,
            "description": (
                "Graph expansion depth: 0 for direct retrieval only, 1 for direct relations, "
                "or 2 for bounded typed multi-hop paths. Choose explicitly from task complexity."
            ),
        },
    },
    required=["query"],
)
_MEMORY_GET_PARAMETERS = object_schema(
    properties={
        "memory_id": {
            "type": "string",
            "description": "Canonical memory node id, e.g. preference:abc.",
        }
    },
    required=["memory_id"],
)
_MEMORY_EXPLAIN_PARAMETERS = object_schema(
    properties={
        "memory_id": {
            "type": "string",
            "description": "Canonical memory node id to explain.",
        }
    },
    required=["memory_id"],
)
_COMMON_PARAMETERS = _memory_parameters()
_ACTION_BOUNDARY_PARAMETERS = _memory_parameters(
    extra_properties={
        "applies_when": {
            "type": "string",
            "description": "Condition under which this action boundary applies.",
        },
        "do_not_apply_when": {
            "type": "string",
            "description": "Condition under which this action boundary does not apply.",
        },
        "trigger_tools": {"type": "array", "items": {"type": "string"}},
        "trigger_actions": {"type": "array", "items": {"type": "string"}},
        "trigger_file_globs": {"type": "array", "items": {"type": "string"}},
        "trigger_scopes": {"type": "array", "items": {"type": "string"}},
        "trigger_keywords": {"type": "array", "items": {"type": "string"}},
        "negative_tools": {"type": "array", "items": {"type": "string"}},
        "negative_actions": {"type": "array", "items": {"type": "string"}},
        "negative_file_globs": {"type": "array", "items": {"type": "string"}},
    },
    extra_required=["applies_when", "do_not_apply_when"],
)
_DECISION_PARAMETERS = _memory_parameters(
    extra_properties={
        "based_on_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Prior memory ids this decision builds on.",
        },
    }
)


_BUILTIN_DESCRIPTORS: dict[str, CapabilityDescriptor] = {
    "artifact_read": _descriptor(
        name="artifact_read",
        description=(
            "Read a canonical tool-output artifact by artifact_id. Large tool "
            "result previews include an exact artifact_id and a suggested "
            "offset_chars; use those values to inspect omitted content. A "
            "preview that says artifact unavailable has no readable handle."
        ),
        input_schema=object_schema(
            properties={
                "artifact_id": {"type": "string", "minLength": 1},
                "mode": {"type": "string", "enum": ["text", "info"], "default": "text"},
                "offset_chars": {"type": "integer", "minimum": 0, "default": 0},
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
        is_concurrency_safe=True,
        permission_category="artifact_read",
        artifact_mode=ToolArtifactMode.NEVER,
    ),
    "read_file": _descriptor(
        name="read_file",
        description=(
            "Read a UTF-8 text file with line numbers and pagination. Relative paths resolve from "
            "workspace_root; absolute paths and ~ may read host-local ordinary text files."
        ),
        input_schema=object_schema(
            properties={
                "path": {
                    "type": "string",
                    "description": "Relative paths resolve from workspace_root; absolute paths and ~ are allowed for text reads.",
                },
                "offset": {"type": "integer", "default": 1},
                "limit": {
                    "type": "integer",
                    "default": DEFAULT_READ_LINES,
                    "maximum": MAX_READ_LINES,
                },
            },
            required=["path"],
        ),
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="filesystem_read",
    ),
    "search_files": _descriptor(
        name="search_files",
        description=(
            "Search text files or find files by name. Relative paths resolve from workspace_root; "
            "absolute paths and ~ are allowed, but broad host roots are rejected outside the workspace."
        ),
        input_schema=object_schema(
            properties={
                "pattern": {"type": "string"},
                "target": {
                    "type": "string",
                    "enum": ["content", "files"],
                    "default": "content",
                },
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "Relative paths resolve from workspace_root. Outside workspace, use a specific file or subdirectory, not broad roots like ~, /, /Users, or /tmp.",
                },
                "file_glob": {"type": "string"},
                "limit": {"type": "integer", "default": DEFAULT_SEARCH_LIMIT},
                "offset": {"type": "integer", "default": 0},
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_only", "count"],
                    "default": "content",
                },
                "context": {"type": "integer", "default": 0},
            },
            required=[],
        ),
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="filesystem_read",
    ),
    "edit_file": _descriptor(
        name="edit_file",
        description="Targeted find-and-replace edit. Returns a unified diff and verifies the write landed.",
        input_schema=object_schema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            required=["path", "old_text", "new_text"],
        ),
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "write_file": _descriptor(
        name="write_file",
        description="Write complete UTF-8 content to a workspace file, replacing existing content atomically.",
        input_schema=object_schema(
            properties={
                "path": {"type": "string"},
                "content": {"type": "string"},
                "create_dirs": {"type": "boolean", "default": True},
            },
            required=["path", "content"],
        ),
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "terminal": _descriptor(
        name="terminal",
        description=TERMINAL_TOOL_DESCRIPTION,
        input_schema=terminal_input_schema(),
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="terminal",
        artifact_mode=ToolArtifactMode.LARGE_OUTPUT,
        is_open_world=True,
    ),
    "terminal_process": _descriptor(
        name="terminal_process",
        description=TERMINAL_PROCESS_TOOL_DESCRIPTION,
        input_schema=terminal_process_input_schema(),
        is_read_only=False,
        is_concurrency_safe=False,
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
        is_concurrency_safe=False,
        permission_category="terminal",
        artifact_mode=ToolArtifactMode.DEFAULT,
        is_destructive=True,
        is_open_world=False,
    ),
    "todo": _descriptor(
        name="todo",
        description="Track the current runtime task plan.",
        input_schema=object_schema(
            properties={
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "list", "clear"],
                },
                "text": {"type": "string"},
                "id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                },
            },
            required=["action"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=True,
        is_concurrency_safe=False,
        permission_category="agent_local",
    ),
    "spawn_agent": _descriptor(
        name="spawn_agent",
        description=(
            "Start an isolated child agent runtime for a bounded subtask. The child has its own runtime "
            "session and event stream; use wait_agent to explicitly collect its result."
        ),
        input_schema=object_schema(
            properties={
                "task": {"type": "string"},
                "label": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["worker", "verifier", "synthesizer", "orchestrator"],
                },
                "context": {"type": "string", "enum": ["isolated", "fork"]},
            },
            required=["task"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="subagent_runtime",
    ),
    "wait_agent": _descriptor(
        name="wait_agent",
        description=(
            "Collect a completed child agent result and mark that result as explicitly consumed by this tool call."
        ),
        input_schema=object_schema(
            properties={
                "subagent_run_id": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            required=["subagent_run_id"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="subagent_runtime",
    ),
    "stop_agent": _descriptor(
        name="stop_agent",
        description="Cancel a running child agent runtime.",
        input_schema=object_schema(
            properties={
                "subagent_run_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            required=["subagent_run_id"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="subagent_runtime",
        is_destructive=True,
    ),
    "list_agents": _descriptor(
        name="list_agents",
        description=(
            "Return a bounded, read-only projection of child agent runs and task-board state. "
            "This never returns child raw transcripts."
        ),
        input_schema=object_schema(
            properties={
                "max_items": {"type": "integer", "default": 50},
                "include_edges": {"type": "boolean", "default": False},
            },
            required=[],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="subagent_runtime",
    ),
    "create_agent_tasks": _descriptor(
        name="create_agent_tasks",
        description=(
            "Create a batch of logical subagent tasks. Tasks with satisfied dependencies start immediately; "
            "tasks with unmet dependencies wait until upstream completion, and upstream failure blocks downstream tasks."
        ),
        input_schema=object_schema(
            properties={
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_key": {"type": "string"},
                            "label": {"type": "string"},
                            "profile": {
                                "type": "string",
                                "enum": [
                                    "research_worker",
                                    "review_worker",
                                    "verification_worker",
                                    "general_worker",
                                ],
                            },
                            "task": {"type": "string"},
                            "display_role": {"type": "string"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["profile", "task"],
                        "additionalProperties": False,
                    },
                }
            },
            required=["tasks"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="subagent_runtime",
    ),
    "wait_agent_tasks": _descriptor(
        name="wait_agent_tasks",
        description=(
            "Wait for one or more logical subagent tasks by task_id. "
            "Timeout returns partial settled results and does not cancel running tasks."
        ),
        input_schema=object_schema(
            properties={
                "task_ids": {"type": "array", "items": {"type": "string"}},
                "settle": {"type": "string", "enum": ["all", "first"]},
                "timeout_seconds": {"type": "number"},
                "include_consumed": {"type": "boolean"},
            },
            required=["task_ids"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="subagent_runtime",
    ),
    "stop_agent_task": _descriptor(
        name="stop_agent_task",
        description="Cancel a logical subagent task and its active child attempt, if any.",
        input_schema=object_schema(
            properties={"task_id": {"type": "string"}, "reason": {"type": "string"}},
            required=["task_id"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="subagent_runtime",
        is_destructive=True,
    ),
    "report_agent_phase": _descriptor(
        name="report_agent_phase",
        description="Child-only tool for reporting current subagent progress without completing the run.",
        input_schema=object_schema(
            properties={
                "phase": {"type": "string"},
                "message": {"type": "string"},
                "progress": {"type": "object"},
            },
            required=["phase"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="agent_local",
    ),
    "report_agent_result": _descriptor(
        name="report_agent_result",
        description=(
            "Child-only tool for submitting the explicit final result. "
            "The child run ends at the next runtime safe point after this succeeds."
        ),
        input_schema=object_schema(
            properties={
                "summary": {"type": "string"},
                "output_preview": {"type": "string"},
                "diagnostics": {"type": "array", "items": {"type": "object"}},
            },
            required=["summary"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="agent_local",
    ),
    "enter_plan": _descriptor(
        name="enter_plan",
        description="Enter Plan workflow, narrowing the session to read-only planning.",
        input_schema=object_schema(
            properties={"reason": {"type": "string", "maxLength": 4096}},
            required=[],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="plan_workflow",
    ),
    "ask_plan_question": _descriptor(
        name="ask_plan_question",
        description="Ask the user a blocking question while in Plan workflow.",
        input_schema=object_schema(
            properties={
                "question": {"type": "string", "minLength": 1, "maxLength": 16384},
                "options": {
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
                                    },
                                    "description": {
                                        "type": "string",
                                        "maxLength": 2048,
                                    },
                                    "recommended": {"type": "boolean"},
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                    ],
                },
                "allow_free_text": {"type": "boolean"},
                "reason": {"type": "string", "maxLength": 4096},
            },
            required=["question", "allow_free_text"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="plan_workflow",
    ),
    "exit_plan": _descriptor(
        name="exit_plan",
        description="Submit a plan draft and ask the user whether to exit Plan workflow.",
        input_schema=object_schema(
            properties={
                "plan": {"type": "string", "minLength": 1, "maxLength": 1048576},
                "summary": {"type": "string", "maxLength": 8192},
            },
            required=["plan"],
        ),
        provider_kind=CapabilityProviderKind.WORKFLOW,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="plan_workflow",
    ),
    "memory_search": _descriptor(
        name="memory_search",
        description="Search canonical durable memory.",
        input_schema=_MEMORY_SEARCH_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="memory_read",
    ),
    "memory_get": _descriptor(
        name="memory_get",
        description="Fetch one canonical durable memory by id with status, evidence ids, and direct graph relations.",
        input_schema=_MEMORY_GET_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="memory_read",
    ),
    "memory_explain": _descriptor(
        name="memory_explain",
        description="Explain one canonical durable memory using materialized fields, edges, and recall signals.",
        input_schema=_MEMORY_EXPLAIN_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="memory_read",
    ),
    "remember_claim": _descriptor(
        name="remember_claim",
        description="Remember a durable factual claim with optional evidence.",
        input_schema=_COMMON_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="memory_write",
    ),
    "remember_preference": _descriptor(
        name="remember_preference",
        description="Remember a durable user or workspace preference.",
        input_schema=_COMMON_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="memory_write",
    ),
    "remember_observation": _descriptor(
        name="remember_observation",
        description="Remember a durable observation grounded in conversation, tool output, or another source.",
        input_schema=_COMMON_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="memory_write",
    ),
    "remember_action_boundary": _descriptor(
        name="remember_action_boundary",
        description="Remember a durable action boundary with explicit apply and non-apply conditions.",
        input_schema=_ACTION_BOUNDARY_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="memory_write",
    ),
    "remember_decision": _descriptor(
        name="remember_decision",
        description="Remember a durable decision, optionally linked to prior memory ids it is based on.",
        input_schema=_DECISION_PARAMETERS,
        provider_kind=CapabilityProviderKind.MEMORY,
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="memory_write",
    ),
}


class BuiltinToolBindingKind(StrEnum):
    FILESYSTEM = "filesystem"
    ARTIFACT_READ = "artifact_read"
    MEMORY_PROPOSAL = "memory_proposal"
    MEMORY_RECALL = "memory_recall"
    MEMORY_QUERY = "memory_query"
    PLAN_WORKFLOW = "plan_workflow"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_PROCESS = "terminal_process"
    TERMINAL_MONITOR = "terminal_monitor"
    TODO_LOCAL_STATE = "todo_local_state"
    SUBAGENT_CONTROL = "subagent_control"


class BuiltinToolAvailabilityKind(StrEnum):
    ALWAYS = "always"
    REQUIRES_ARTIFACT_READ_PORT = "requires_artifact_read_port"
    REQUIRES_MEMORY_PROPOSAL_PORT = "requires_memory_proposal_port"
    REQUIRES_MEMORY_RECALL_PORT = "requires_memory_recall_port"
    REQUIRES_MEMORY_QUERY_PORT = "requires_memory_query_port"
    REQUIRES_TERMINAL_PORTS = "requires_terminal_ports"
    REQUIRES_MAIN_SUBAGENT_CONTROL = "requires_main_subagent_control"
    REQUIRES_CHILD_REPORT_CONTROL = "requires_child_report_control"


class BuiltinTerminalPermissionRuleKind(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ALL_ACTIONS = "all_actions"
    CLOSED_ACTION_SET = "closed_action_set"


@dataclass(frozen=True, slots=True)
class BuiltinToolAvailabilityRequirement:
    kind: BuiltinToolAvailabilityKind
    allowed_invocation_owners: tuple[ToolInvocationOwnerKind, ...]
    requirement_fingerprint: str


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
    rule_fingerprint: str


@dataclass(frozen=True, slots=True)
class BuiltinToolPermissionContract:
    ordered_action_overrides: tuple[BuiltinActionPermissionOverride, ...]
    terminal_rule: BuiltinTerminalPermissionRule
    contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class BuiltinToolRecoveryContract:
    severity: Literal["read_only", "bounded_write", "terminal", "unknown_effect"]
    include_in_unfinished_recovery: bool
    contract_fingerprint: str


@dataclass(frozen=True, slots=True)
class BuiltinToolCatalogEntry:
    name: str
    descriptor: CapabilityDescriptor
    binding_contract: BuiltinToolBindingContract
    execution_binding_kind: BuiltinToolBindingKind
    availability_requirement: BuiltinToolAvailabilityRequirement
    permission_contract: BuiltinToolPermissionContract
    long_horizon_policy_kind: BuiltinToolLongHorizonPolicyKind
    recovery_contract: BuiltinToolRecoveryContract
    tool_family: Literal[
        "filesystem",
        "artifact",
        "memory_read",
        "memory_write",
        "terminal",
        "plan",
        "subagent_parent",
        "subagent_child",
        "local_state",
    ]
    entry_fingerprint: str


_FILESYSTEM = frozenset({"edit_file", "read_file", "search_files", "write_file"})
_MEMORY_PROPOSAL = frozenset(
    {
        "remember_action_boundary",
        "remember_claim",
        "remember_decision",
        "remember_observation",
        "remember_preference",
    }
)
_MEMORY_QUERY = frozenset({"memory_explain", "memory_get"})
_PLAN = frozenset({"ask_plan_question", "enter_plan", "exit_plan"})
_SUBAGENT_PARENT = frozenset(
    {
        "create_agent_tasks",
        "list_agents",
        "spawn_agent",
        "stop_agent",
        "stop_agent_task",
        "wait_agent",
        "wait_agent_tasks",
    }
)
_SUBAGENT_CHILD = frozenset({"report_agent_phase", "report_agent_result"})
_TERMINAL_PROCESS_ACTIONS = (
    "close_stdin",
    "kill",
    "list",
    "log",
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


def _catalog_entry(
    name: str, descriptor: CapabilityDescriptor
) -> BuiltinToolCatalogEntry:
    binding_kind, availability_kind, owners, family = _catalog_shape(name)
    availability_payload = {
        "kind": availability_kind.value,
        "allowed_invocation_owners": tuple(item.value for item in owners),
    }
    availability = BuiltinToolAvailabilityRequirement(
        kind=availability_kind,
        allowed_invocation_owners=owners,
        requirement_fingerprint=context_fingerprint(
            "builtin-tool-availability-requirement:v1", availability_payload
        ),
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
        contract_version="v1",
    )
    if not isinstance(binding, BuiltinToolBindingContract):
        raise TypeError("builtin catalog produced a non-builtin binding")
    permission = _permission_contract(name, binding_kind)
    recovery = _recovery_contract(name)
    long_horizon_policy_kind = _LONG_HORIZON_POLICY_KIND_BY_NAME[name]
    payload = {
        "name": name,
        "descriptor_fingerprint": descriptor.fingerprint(),
        "binding_contract_fingerprint": binding.contract_fact_fingerprint,
        "execution_binding_kind": binding_kind.value,
        "availability_requirement_fingerprint": availability.requirement_fingerprint,
        "permission_contract_fingerprint": permission.contract_fingerprint,
        "long_horizon_policy_kind": long_horizon_policy_kind.value,
        "recovery_contract_fingerprint": recovery.contract_fingerprint,
        "tool_family": family,
    }
    return BuiltinToolCatalogEntry(
        name=name,
        descriptor=descriptor,
        binding_contract=binding,
        execution_binding_kind=binding_kind,
        availability_requirement=availability,
        permission_contract=permission,
        long_horizon_policy_kind=long_horizon_policy_kind,
        recovery_contract=recovery,
        tool_family=family,  # type: ignore[arg-type]
        entry_fingerprint=context_fingerprint("builtin-tool-catalog-entry:v1", payload),
    )


def _catalog_shape(name: str):
    both = (
        ToolInvocationOwnerKind.HOST_MAIN_RUN,
        ToolInvocationOwnerKind.SUBAGENT_CHILD,
    )
    if name == "artifact_read":
        return (
            BuiltinToolBindingKind.ARTIFACT_READ,
            BuiltinToolAvailabilityKind.REQUIRES_ARTIFACT_READ_PORT,
            both,
            "artifact",
        )
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
    if name in _MEMORY_PROPOSAL:
        return (
            BuiltinToolBindingKind.MEMORY_PROPOSAL,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_PROPOSAL_PORT,
            both,
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
    rule_payload = {
        "kind": terminal_kind.value,
        "ordered_terminal_access_actions": access_actions,
        "ordered_scheduling_permission_actions": scheduling_actions,
    }
    rule = BuiltinTerminalPermissionRule(
        kind=terminal_kind,
        ordered_terminal_access_actions=access_actions,
        ordered_scheduling_permission_actions=scheduling_actions,
        rule_fingerprint=context_fingerprint(
            "builtin-terminal-permission-rule:v1", rule_payload
        ),
    )
    payload = {
        "ordered_action_overrides": tuple(asdict(item) for item in overrides),
        "terminal_rule_fingerprint": rule.rule_fingerprint,
    }
    return BuiltinToolPermissionContract(
        ordered_action_overrides=overrides,
        terminal_rule=rule,
        contract_fingerprint=context_fingerprint(
            "builtin-tool-permission-contract:v1", payload
        ),
    )


def _recovery_contract(name: str) -> BuiltinToolRecoveryContract:
    if name in {"terminal", "terminal_monitor", "terminal_process"}:
        severity = "terminal"
    elif name in {"edit_file", "write_file"}:
        severity = "bounded_write"
    elif name in {"artifact_read", "read_file", "search_files"}:
        severity = "read_only"
    else:
        severity = "unknown_effect"
    include = name not in _PLAN
    payload = {"severity": severity, "include_in_unfinished_recovery": include}
    return BuiltinToolRecoveryContract(
        severity=severity,  # type: ignore[arg-type]
        include_in_unfinished_recovery=include,
        contract_fingerprint=context_fingerprint(
            "builtin-tool-recovery-contract:v1", payload
        ),
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
    "BuiltinToolLongHorizonPolicyKind",
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
    "builtin_tool_descriptors",
]
