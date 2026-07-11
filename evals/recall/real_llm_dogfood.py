"""Long-form real-provider dogfood for semantic durable-memory recall."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg

from pulsara_agent.entities.memory import ActionBoundary, Decision, Observation
from pulsara_agent.event import ToolCallStartEvent, ToolResultTextDeltaEvent
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.jsonld import utc_now
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.canonical.vector_index_sync import MemoryVectorIndexSync
from pulsara_agent.memory.scope import workspace_scope
from pulsara_agent.ontology import memory
from pulsara_agent.settings import PulsaraSettings


@dataclass(frozen=True, slots=True)
class DogfoodTurn:
    label: str
    run_id: str
    status: str
    final_text: str
    tool_names: tuple[str, ...]
    tool_result_texts: tuple[str, ...]
    projection_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryRecallDogfoodReport:
    graph_id: str
    target_ids: dict[str, str]
    vector_row_count: int
    turns: tuple[DogfoodTurn, ...]
    traces: tuple[dict[str, Any], ...]
    usage_count: int
    resources_closed: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str)


async def run_memory_recall_dogfood(
    workspace_root: Path,
    *,
    settings: PulsaraSettings,
) -> MemoryRecallDogfoodReport:
    """Run a five-turn conversation plus a cross-dialogue recall turn."""

    dsn = settings.storage.postgres_dsn
    root_a = workspace_root / "memory-dogfood-a"
    root_b = workspace_root / "memory-dogfood-b"
    hidden_root = workspace_root / "memory-dogfood-hidden"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    hidden_root.mkdir(parents=True, exist_ok=True)
    domain_id = f"u_recall_dogfood_{uuid4().hex[:12]}"
    core = HostCore(settings, durable=True)
    session_a = await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=root_a,
            memory_domain_id=domain_id,
        ),
        model_role=ModelRole.FLASH,
        options=LLMOptions(),
        system_prompt=_DOGFOOD_SYSTEM_PROMPT,
        memory_reflection=False,
    )
    graph_id = session_a.workspace.memory_domain.graph_id
    resources = session_a.wiring.runtime_wiring.retrieval_resources
    if resources is None or resources.embedding is None or resources.rerank is None:
        await core.shutdown()
        raise RuntimeError("Dogfood requires configured embedding and rerank providers")

    target_ids = _seed_memories(
        session_a,
        graph_id=graph_id,
        hidden_scope=workspace_scope(str(hidden_root)),
    )
    search_sync = MemorySearchIndexSync(dsn=dsn)
    search_sync.rebuild(graph_id=graph_id)
    vector_sync = MemoryVectorIndexSync(
        dsn=dsn,
        provider=resources.embedding,
        provider_name=settings.retrieval.embedding.provider,
    )
    await vector_sync.rebuild(graph_id=graph_id)
    vector_row_count = _vector_row_count(dsn, graph_id)

    turns: list[DogfoodTurn] = []
    session_b = None
    runtime_session_ids = [session_a.runtime_session_id]
    try:
        turns.append(
            await _run_turn(
                session_a,
                "auto_timezone",
                "Our weekly digest is about to run. Which local timezone should the schedule follow? "
                "Answer directly without calling tools.",
            )
        )
        turns.append(
            await _run_turn(
                session_a,
                "explicit_persistence",
                "Search durable memory: What persistence technology did Project Lumen settle on?",
            )
        )
        turns.append(
            await _run_turn(
                session_a,
                "explicit_billing_guardrail",
                "Search durable memory: I am about to edit production billing configuration. "
                "What standing safety rule applies?",
            )
        )
        turns.append(
            await _run_turn(
                session_a,
                "explicit_timezone_repeat",
                "Search durable memory: Which timezone governs the weekly digest schedule?",
            )
        )
        turns.append(
            await _run_turn(
                session_a,
                "unrelated_negative",
                "Compute 17 multiplied by 19. Answer with the number only and do not call tools.",
            )
        )

        session_b = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=root_b,
                memory_domain_id=domain_id,
            ),
            model_role=ModelRole.FLASH,
            options=LLMOptions(),
            system_prompt=_DOGFOOD_SYSTEM_PROMPT,
            memory_reflection=False,
        )
        runtime_session_ids.append(session_b.runtime_session_id)
        turns.append(
            await _run_turn(
                session_b,
                "cross_dialogue_persistence",
                "Search durable memory: Remind me of Project Lumen's chosen persistence stack.",
            )
        )

        traces = _load_traces(dsn, graph_id, {turn.run_id for turn in turns})
        usage_count = _usage_count(dsn, graph_id, {turn.run_id for turn in turns})
    finally:
        await core.shutdown()
        resources_closed = resources.closed
        _cleanup(dsn, graph_id=graph_id, runtime_session_ids=runtime_session_ids)

    return MemoryRecallDogfoodReport(
        graph_id=graph_id,
        target_ids=target_ids,
        vector_row_count=vector_row_count,
        turns=tuple(turns),
        traces=traces,
        usage_count=usage_count,
        resources_closed=resources_closed,
    )


async def _run_turn(session, label: str, prompt: str) -> DogfoodTurn:
    result = await session.run_turn(prompt)
    events = session.wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
    return DogfoodTurn(
        label=label,
        run_id=result.state.run_id,
        status=result.status.value,
        final_text=result.final_text.strip(),
        tool_names=tuple(
            event.tool_call_name
            for event in events
            if isinstance(event, ToolCallStartEvent)
        ),
        tool_result_texts=tuple(
            event.delta
            for event in events
            if isinstance(event, ToolResultTextDeltaEvent)
        ),
        projection_ids=tuple(
            (result.state.memory_projection or {}).get("included_memory_ids") or ()
        ),
    )


def _seed_memories(session, *, graph_id: str, hidden_scope: str) -> dict[str, str]:
    now = utc_now()
    common = {
        "status": memory.NodeStatus.ACTIVE,
        "confidence_level": memory.ConfidenceLevel.HIGH,
        "verification_status": memory.VerificationStatus.USER_CONFIRMED,
        "source_authority": memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        "created_at": now,
        "updated_at": now,
        "gate_reason": "real llm long memory recall dogfood seed",
    }
    ids = {
        "timezone": f"observation:dogfood-timezone-{uuid4().hex}",
        "persistence": f"decision:dogfood-persistence-{uuid4().hex}",
        "billing": f"action-boundary:dogfood-billing-{uuid4().hex}",
        "persistence_distractor": f"decision:dogfood-atlas-{uuid4().hex}",
        "timezone_distractor": f"observation:dogfood-utc-{uuid4().hex}",
        "hidden": f"decision:dogfood-hidden-{uuid4().hex}",
    }
    nodes = (
        Observation(
            id=ids["timezone"],
            statement="The weekly digest schedule is interpreted in Asia/Shanghai local time.",
            scope="ctx:user",
            **common,
        ),
        Decision(
            id=ids["persistence"],
            statement="Project Lumen selected PostgreSQL with pgvector as its durable persistence stack.",
            scope="ctx:user",
            **common,
        ),
        ActionBoundary(
            id=ids["billing"],
            statement=(
                "Before editing production billing configuration, obtain explicit user confirmation "
                "and create a recoverable backup."
            ),
            scope="ctx:user",
            applies_when="Any change targets production billing configuration.",
            do_not_apply_when="The work is read-only or confined to a disposable sandbox.",
            trigger_keywords=(
                "production billing",
                "billing configuration",
                "payment settings",
            ),
            **common,
        ),
        Decision(
            id=ids["persistence_distractor"],
            statement="Prototype Atlas uses SQLite for disposable local demonstrations.",
            scope="ctx:user",
            **common,
        ),
        Observation(
            id=ids["timezone_distractor"],
            statement="Infrastructure audit logs are normalized to UTC before archival.",
            scope="ctx:user",
            **common,
        ),
        Decision(
            id=ids["hidden"],
            statement="The hidden workspace uses MySQL for Project Lumen persistence.",
            scope=hidden_scope,
            **common,
        ),
    )
    graph = session.wiring.runtime_wiring.graph
    for node in nodes:
        graph.put_jsonld(node.to_jsonld(), graph_id=graph_id)
    return ids


def _vector_row_count(dsn: str, graph_id: str) -> int:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM memory_vector_index WHERE graph_id = %s",
                (graph_id,),
            )
            return int(cursor.fetchone()[0])


def _load_traces(
    dsn: str, graph_id: str, run_ids: set[str]
) -> tuple[dict[str, Any], ...]:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, trigger_kind, included_ids, filtered_ids, warnings, metadata, latency_ms
                FROM recall_traces
                WHERE graph_id = %s AND run_id = ANY(%s)
                ORDER BY created_at, trace_id
                """,
                (graph_id, list(run_ids)),
            )
            return tuple(
                {
                    "run_id": row[0],
                    "trigger_kind": row[1],
                    "included_ids": row[2],
                    "filtered_ids": row[3],
                    "warnings": row[4],
                    "metadata": row[5],
                    "latency_ms": row[6],
                }
                for row in cursor.fetchall()
            )


def _usage_count(dsn: str, graph_id: str, run_ids: set[str]) -> int:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM recall_usages AS usage
                JOIN recall_traces AS trace ON trace.trace_id = usage.trace_id
                WHERE usage.graph_id = %s AND trace.run_id = ANY(%s)
                """,
                (graph_id, list(run_ids)),
            )
            return int(cursor.fetchone()[0])


def _cleanup(dsn: str, *, graph_id: str, runtime_session_ids: list[str]) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM recall_traces WHERE graph_id = %s", (graph_id,))
            cursor.execute(
                "DELETE FROM memory_write_outbox WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute(
                "DELETE FROM memory_relations WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute(
                "DELETE FROM graph_documents WHERE graph_id = %s", (graph_id,)
            )
            cursor.execute("DELETE FROM memory_nodes WHERE graph_id = %s", (graph_id,))
            cursor.execute(
                "DELETE FROM artifacts WHERE session_id = ANY(%s)",
                (runtime_session_ids,),
            )
            cursor.execute(
                "DELETE FROM sessions WHERE id = ANY(%s)", (runtime_session_ids,)
            )


_DOGFOOD_SYSTEM_PROMPT = """
You are participating in a realistic durable-memory recall dogfood.

Follow these rules:
1. If the user message begins with "Search durable memory:", call memory_search exactly once before answering.
2. Use a concise semantic query and ground the answer only in the tool result. Omit the scope argument unless the user explicitly names an exact memory scope.
3. For other messages, do not call tools; use Recalled Memory when it is relevant.
4. Never expose a memory from an invisible workspace scope.
5. Answer naturally and concisely. When a memory tool returns a memory_id, include that id in square brackets.
6. If durable memory has no relevant answer, say that it did not contain one rather than guessing.
""".strip()
