"""Read-only tools for canonical durable-memory recall."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.memory.recall.explain import explain_memory, explanation_to_payload
from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService, RecallQuery, RecallStatus, RecallTrigger
from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import int_arg, json_text, object_schema, required_str_arg, str_arg


_MEMORY_SEARCH_PARAMETERS = object_schema(
    properties={
        "query": {
            "type": "string",
            "description": "Natural-language or lexical query for canonical durable memory.",
        },
        "scope": {
            "type": "string",
            "description": "Optional exact memory scope, e.g. ctx:user or ctx:workspace/project.",
        },
        "kind": {
            "type": "string",
            "description": "Optional memory type: Claim, Preference, Observation, ActionBoundary, or Decision.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum results to return.",
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

_MEMORY_RELATED_PARAMETERS = object_schema(
    properties={
        "memory_id": {
            "type": "string",
            "description": "Canonical memory node id to inspect for direct graph relations.",
        }
    },
    required=["memory_id"],
)

_MEMORY_EXPLAIN_PARAMETERS = object_schema(
    properties={
        "memory_id": {
            "type": "string",
            "description": "Canonical memory node id to explain from materialized graph data.",
        }
    },
    required=["memory_id"],
)


@dataclass(frozen=True, slots=True)
class MemorySearchTool:
    recall: MemoryRecallService
    graph_id: str | None = None

    name: ClassVar[str] = "memory_search"
    description: ClassVar[str] = (
        "Search canonical durable memory. Use this for user preferences, prior durable decisions, "
        "remembered observations, and other semantic memories. If no result is found, say so; do not guess."
    )
    parameters: ClassVar[dict[str, Any]] = _MEMORY_SEARCH_PARAMETERS
    is_read_only: ClassVar[bool] = True
    is_concurrency_safe: ClassVar[bool] = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        query_text = required_str_arg(call.arguments, "query")
        scope = str_arg(call.arguments, "scope")
        kind = str_arg(call.arguments, "kind")
        limit = max(1, min(int_arg(call.arguments, "limit", 5), 20))
        result = asyncio.run(
            self.recall.recall(
                RecallQuery(
                    text=query_text,
                    scopes=(scope,) if scope else (),
                    types=(kind,) if kind else (),
                    limit=limit,
                    trigger=RecallTrigger.EXPLICIT_SEARCH,
                ),
                graph_id=self.graph_id,
            )
        )
        if result.status is RecallStatus.UNAVAILABLE:
            payload = {
                "status": result.status.value,
                "warnings": list(result.warnings),
                "guidance": list(result.guidance),
                "fallback": "history_search_or_current_files",
                "can_retry": False,
            }
        elif result.status is RecallStatus.EMPTY:
            payload = {
                "status": result.status.value,
                "results": [],
                "filtered_ids": list(result.filtered_ids),
                "guidance": list(result.guidance),
            }
        else:
            payload = {
                "status": result.status.value,
                "results": [
                    {
                        "memory_id": item.memory_id,
                        "type": item.memory_type,
                        "scope": item.scope,
                        "status": item.status.value,
                        "snippet": item.snippet,
                        "score": item.score,
                        "why": list(item.why),
                        "deep_recall": item.deep_recall,
                    }
                    for item in result.items
                ],
                "filtered_ids": list(result.filtered_ids),
            }
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
        )


@dataclass(frozen=True, slots=True)
class MemoryGetTool:
    memory_query: MemoryQuery
    graph_id: str | None = None

    name: ClassVar[str] = "memory_get"
    description: ClassVar[str] = (
        "Fetch one canonical durable memory by id with status, evidence ids, and direct graph relations."
    )
    parameters: ClassVar[dict[str, Any]] = _MEMORY_GET_PARAMETERS
    is_read_only: ClassVar[bool] = True
    is_concurrency_safe: ClassVar[bool] = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        memory_id = required_str_arg(call.arguments, "memory_id")
        try:
            views = self.memory_query.fetch_nodes([memory_id], graph_id=self.graph_id)
        except Exception as exc:
            payload = {
                "status": "unavailable",
                "reason": "recall_backend_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "can_retry": False,
            }
        else:
            if not views:
                payload = {
                    "status": "empty",
                    "memory_id": memory_id,
                    "guidance": ["No canonical memory with this id was found."],
                }
            else:
                payload = {"status": "ok", "memory": _view_payload(views[0])}
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
        )


@dataclass(frozen=True, slots=True)
class MemoryRelatedTool:
    memory_query: MemoryQuery
    graph_id: str | None = None

    name: ClassVar[str] = "memory_related"
    description: ClassVar[str] = (
        "Return direct materialized graph relations for one canonical durable memory. "
        "This is read-only and never infers missing relations."
    )
    parameters: ClassVar[dict[str, Any]] = _MEMORY_RELATED_PARAMETERS
    is_read_only: ClassVar[bool] = True
    is_concurrency_safe: ClassVar[bool] = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        memory_id = required_str_arg(call.arguments, "memory_id")
        try:
            views = self.memory_query.fetch_nodes([memory_id], graph_id=self.graph_id)
        except Exception as exc:
            payload = _unavailable_payload(exc)
        else:
            if not views:
                payload = {
                    "status": "empty",
                    "memory_id": memory_id,
                    "relations": [],
                    "guidance": ["No canonical memory with this id was found."],
                }
            else:
                view = views[0]
                payload = {
                    "status": "ok",
                    "memory_id": view.id,
                    "incoming": [
                        {"predicate": predicate, "source_id": source_id}
                        for predicate, source_id in view.incoming
                    ],
                    "outgoing": [
                        {"predicate": predicate, "target_id": target_id}
                        for predicate, target_id in view.outgoing
                    ],
                }
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
        )


@dataclass(frozen=True, slots=True)
class MemoryExplainTool:
    memory_query: MemoryQuery
    graph_id: str | None = None

    name: ClassVar[str] = "memory_explain"
    description: ClassVar[str] = (
        "Explain one canonical durable memory using only materialized edges, fields, and recall signals. "
        "If the graph has no supporting edge, the explanation will stay silent about that relation."
    )
    parameters: ClassVar[dict[str, Any]] = _MEMORY_EXPLAIN_PARAMETERS
    is_read_only: ClassVar[bool] = True
    is_concurrency_safe: ClassVar[bool] = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        memory_id = required_str_arg(call.arguments, "memory_id")
        try:
            views = self.memory_query.fetch_nodes([memory_id], graph_id=self.graph_id)
        except Exception as exc:
            payload = _unavailable_payload(exc)
        else:
            if not views:
                payload = {
                    "status": "empty",
                    "memory_id": memory_id,
                    "guidance": ["No canonical memory with this id was found."],
                }
            else:
                payload = {
                    "status": "ok",
                    "explanation": explanation_to_payload(explain_memory(views[0])),
                }
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=json_text(payload),
        )


def _view_payload(view: CanonicalNodeView) -> dict[str, Any]:
    return {
        "memory_id": view.id,
        "type": view.memory_type,
        "scope": view.scope,
        "status": view.status.value,
        "statement": view.statement,
        "summary": view.summary,
        "source_authority": view.source_authority.value if view.source_authority else None,
        "verification_status": view.verification_status.value if view.verification_status else None,
        "confidence_level": view.confidence_level.value if view.confidence_level else None,
        "applies_when": view.applies_when,
        "do_not_apply_when": view.do_not_apply_when,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
        "evidence_ids": list(view.evidence_ids),
        "outgoing": [
            {"predicate": predicate, "target_id": target_id}
            for predicate, target_id in view.outgoing
        ],
        "incoming": [
            {"predicate": predicate, "source_id": source_id}
            for predicate, source_id in view.incoming
        ],
    }


def _unavailable_payload(exc: Exception) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "recall_backend_unavailable",
        "error": f"{type(exc).__name__}: {exc}",
        "can_retry": False,
    }
