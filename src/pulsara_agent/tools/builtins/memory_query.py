"""Read-only tools for canonical durable-memory recall."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.memory.recall.explain import explain_memory, explanation_to_payload
from pulsara_agent.memory.recall.graph import RecallPath
from pulsara_agent.memory.canonical.query import CanonicalNodeView, MemoryQuery
from pulsara_agent.memory.recall.service import MemoryRecallService, RecallQuery, RecallStatus, RecallTrigger
from pulsara_agent.memory.scope import CTX_USER, format_scope_list, is_valid_scope
from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolRuntimeContext
from pulsara_agent.tools.builtins.schemas import (
    bounded_int_arg,
    int_arg,
    json_text,
    object_schema,
    required_str_arg,
    str_arg,
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
            "description": "Optional memory type: Claim, Preference, Observation, ActionBoundary, or Decision.",
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
            "description": "Canonical memory node id to explain from materialized graph data.",
        }
    },
    required=["memory_id"],
)


@dataclass(frozen=True, slots=True)
class MemorySearchTool:
    recall: MemoryRecallService
    graph_id: str | None = None
    read_scopes: frozenset[str] | None = None

    name: ClassVar[str] = "memory_search"
    description: ClassVar[str] = (
        "Search canonical durable memory. Use this for user preferences, prior durable decisions, "
        "remembered observations, and other semantic memories. If no result is found, say so; do not guess."
    )
    parameters: ClassVar[dict[str, Any]] = _MEMORY_SEARCH_PARAMETERS
    is_read_only: ClassVar[bool] = True
    is_concurrency_safe: ClassVar[bool] = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        return asyncio.run(self.execute_async(call, runtime_context=None))

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext | None,
    ) -> ToolExecutionResult:
        query_text = required_str_arg(call.arguments, "query")
        scope = str_arg(call.arguments, "scope")
        scope = scope.strip() if scope is not None else None
        if scope == "":
            scope = None
        kind = str_arg(call.arguments, "kind")
        limit = bounded_int_arg(call.arguments, "limit", default=5, minimum=1, maximum=20)
        max_hops = int_arg(call.arguments, "max_hops", 0)
        if max_hops not in {0, 1, 2}:
            raise ValueError("max_hops must be 0, 1, or 2")
        scope_error = _scope_error_payload(scope, self.read_scopes)
        if scope_error is not None:
            return _tool_success(call, scope_error)
        scopes = (scope,) if scope else _default_scopes(self.read_scopes)
        event_context = runtime_context.event_context if runtime_context is not None else None
        result = await self.recall.recall(
            RecallQuery(
                text=query_text,
                scopes=scopes,
                types=(kind,) if kind else (),
                limit=limit,
                max_hops=max_hops,
                trigger=RecallTrigger.EXPLICIT_SEARCH,
                session_id=runtime_context.runtime_session_id if runtime_context is not None else None,
                run_id=event_context.run_id if event_context is not None else None,
                turn_id=event_context.turn_id if event_context is not None else None,
                reply_id=event_context.reply_id if event_context is not None else None,
            ),
            graph_id=self.graph_id,
        )
        if result.status is RecallStatus.UNAVAILABLE:
            payload = {
                "status": result.status.value,
                "reason": "recall_backend_unavailable",
                "warnings": list(result.warnings),
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
                        "channel_scores": item.channel_scores,
                        "conflicts_with": list(item.conflicts_with),
                        "direct_match": item.direct_match,
                        "hop_count": item.hop_count,
                        "paths": [_path_payload(path) for path in item.paths],
                    }
                    for item in result.items
                ],
                "filtered_ids": list(result.filtered_ids),
            }
        return _tool_success(call, payload)


@dataclass(frozen=True, slots=True)
class MemoryGetTool:
    memory_query: MemoryQuery
    graph_id: str | None = None
    read_scopes: frozenset[str] | None = None

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
            elif not _is_view_visible(views[0], self.read_scopes):
                payload = _forbidden_memory_payload(memory_id)
            else:
                payload = {"status": "ok", "memory": _view_payload(views[0])}
        return _tool_success(call, payload)


@dataclass(frozen=True, slots=True)
class MemoryExplainTool:
    memory_query: MemoryQuery
    graph_id: str | None = None
    read_scopes: frozenset[str] | None = None

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
            elif not _is_view_visible(views[0], self.read_scopes):
                payload = _forbidden_memory_payload(memory_id)
            else:
                payload = {
                    "status": "ok",
                    "explanation": explanation_to_payload(explain_memory(views[0])),
                }
        return _tool_success(call, payload)


def _tool_success(call: ToolCall, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.SUCCESS,
        output=json_text(payload),
    )


def _path_payload(path: RecallPath) -> dict[str, Any]:
    return {
        "seed_memory_id": path.seed_memory_id,
        "target_memory_id": path.target_memory_id,
        "steps": [
            {
                "from_id": step.from_id,
                "to_id": step.to_id,
                "predicate": step.predicate,
                "edge_source_id": step.edge_source_id,
                "edge_target_id": step.edge_target_id,
                "traversal": step.traversal,
            }
            for step in path.steps
        ],
    }


def _default_scopes(read_scopes: frozenset[str] | None) -> tuple[str, ...]:
    return tuple(sorted(_effective_read_scopes(read_scopes)))


def _scope_error_payload(scope: str | None, read_scopes: frozenset[str] | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    if not is_valid_scope(scope):
        return {
            "status": "empty",
            "results": [],
            "reason": "invalid_scope",
            "guidance": ["Use ctx:user or one of the visible ctx:workspace/<id> scopes."],
        }
    effective_read_scopes = _effective_read_scopes(read_scopes)
    if scope not in effective_read_scopes:
        return {
            "status": "empty",
            "results": [],
            "reason": "scope_not_visible",
            "visible_scopes": format_scope_list(effective_read_scopes),
            "guidance": ["The requested memory scope is not visible in this runtime."],
        }
    return None


def _is_view_visible(view: CanonicalNodeView, read_scopes: frozenset[str] | None) -> bool:
    return view.scope in _effective_read_scopes(read_scopes)


def _effective_read_scopes(read_scopes: frozenset[str] | None) -> frozenset[str]:
    return frozenset({CTX_USER}) if read_scopes is None else read_scopes


def _forbidden_memory_payload(memory_id: str) -> dict[str, Any]:
    return {
        "status": "empty",
        "memory_id": memory_id,
        "reason": "scope_not_visible",
        "guidance": ["No canonical memory with this id was found in the visible scopes."],
    }


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
