"""Explicit built-in capability descriptors.

This module is the declaration truth for core model-callable capabilities.
ToolRegistry remains the execution binding registry and is not used to infer
descriptor metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityArtifactMode,
    CapabilityDescriptor,
    CapabilityProviderKind,
)
from pulsara_agent.capability.provider import CapabilityProviderOutput
from pulsara_agent.capability.types import CapabilityResolveContext


DEFAULT_ARTIFACT_READ_CHARS = 20_000
DEFAULT_READ_LINES = 500
MAX_READ_LINES = 2_000
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_MAX_OUTPUT_CHARS = 32_000
DEFAULT_WAIT_TIMEOUT_SECONDS = 30
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


def object_schema(*, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class BuiltinToolCapabilityProvider:
    provider_id: str = "builtin-tools"

    def resolve(
        self,
        context: CapabilityResolveContext,
        *,
        bound_tool_names: frozenset[str],
    ) -> CapabilityProviderOutput:
        del context
        return CapabilityProviderOutput(descriptors=tuple(
            descriptor
            for name, descriptor in sorted(_BUILTIN_DESCRIPTORS.items())
            if name in bound_tool_names
        ))


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
    artifact_mode: CapabilityArtifactMode = CapabilityArtifactMode.DEFAULT,
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
        advertise_policy=CapabilityAdvertisePolicy.DIRECT,
        artifact_mode=artifact_mode,
        metadata={"source": "explicit_builtin_descriptor"},
    )


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
        "limit": {"type": "integer", "default": 5, "description": "Maximum results to return."},
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
        "memory_id": {"type": "string", "description": "Canonical memory node id, e.g. preference:abc."}
    },
    required=["memory_id"],
)
_MEMORY_EXPLAIN_PARAMETERS = object_schema(
    properties={"memory_id": {"type": "string", "description": "Canonical memory node id to explain."}},
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
            "Read a retained tool result artifact by artifact_id. Use this when a tool result includes artifacts[] "
            "and you need details beyond the inline output_preview. Prefer preview.read_more.suggested_offset_chars "
            "when present instead of rereading from 0."
        ),
        input_schema=object_schema(
            properties={
                "artifact_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["text", "info"], "default": "text"},
                "offset_chars": {"type": "integer", "default": 0},
                "max_chars": {"type": "integer", "default": DEFAULT_ARTIFACT_READ_CHARS},
            },
            required=["artifact_id"],
        ),
        is_read_only=True,
        is_concurrency_safe=True,
        permission_category="artifact_read",
        artifact_mode=CapabilityArtifactMode.NEVER,
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
                "limit": {"type": "integer", "default": DEFAULT_READ_LINES, "maximum": MAX_READ_LINES},
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
                "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "Relative paths resolve from workspace_root. Outside workspace, use a specific file or subdirectory, not broad roots like ~, /, /Users, or /tmp.",
                },
                "file_glob": {"type": "string"},
                "limit": {"type": "integer", "default": DEFAULT_SEARCH_LIMIT},
                "offset": {"type": "integer", "default": 0},
                "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "default": "content"},
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
        description="Run a shell command inside workspace_root. Large output is retained as an artifact.",
        input_schema=object_schema(
            properties={
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "terminal_session_id": {"type": "string", "default": "default"},
                "yield_time_ms": {"type": "integer", "default": 10_000},
                "tty": {"type": "boolean", "default": False},
                "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
            },
            required=["command"],
        ),
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="terminal",
        artifact_mode=CapabilityArtifactMode.LARGE_OUTPUT,
        is_open_world=True,
    ),
    "terminal_process": _descriptor(
        name="terminal_process",
        description="List, inspect, poll, wait for, kill, or send stdin to managed terminal processes.",
        input_schema=object_schema(
            properties={
                "action": {
                    "type": "string",
                    "enum": ["list", "log", "poll", "wait", "kill", "write", "submit", "close_stdin"],
                },
                "process_id": {"type": "string"},
                "data": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": DEFAULT_WAIT_TIMEOUT_SECONDS},
                "max_output_chars": {"type": "integer", "default": DEFAULT_MAX_OUTPUT_CHARS},
                "include_finished": {"type": "boolean", "default": True},
                "include_running": {"type": "boolean", "default": True},
            },
            required=["action"],
        ),
        is_read_only=False,
        is_concurrency_safe=False,
        permission_category="terminal",
        artifact_mode=CapabilityArtifactMode.LARGE_OUTPUT,
        is_destructive=True,
        is_open_world=True,
    ),
    "todo": _descriptor(
        name="todo",
        description="Track the current runtime task plan.",
        input_schema=object_schema(
            properties={
                "action": {"type": "string", "enum": ["add", "update", "list", "clear"]},
                "text": {"type": "string"},
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
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
                "role": {"type": "string", "enum": ["worker", "verifier", "synthesizer", "orchestrator"]},
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
            properties={"subagent_run_id": {"type": "string"}, "reason": {"type": "string"}},
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
                                "enum": ["research_worker", "review_worker", "verification_worker", "general_worker"],
                            },
                            "task": {"type": "string"},
                            "display_role": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
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
        input_schema=object_schema(properties={"reason": {"type": "string"}}, required=[]),
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
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "recommended": {"type": "boolean"},
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                },
                "allow_free_text": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            required=["question"],
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
            properties={"plan": {"type": "string"}, "summary": {"type": "string"}},
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
