"""Composition roots for runtime persistence wiring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore, InMemoryGraphStore, OxigraphGraphStore
from pulsara_agent.llm import ModelRole, build_llm_runtime
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory import ArtifactStore, InMemoryArchiveStore, PostgresArtifactStore
from pulsara_agent.memory.durable_hooks import DurableMemoryHooks
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.run_timeline_persistence import RunTimelinePersistenceHook
from pulsara_agent.memory.runtime_persistence import ExecutionEvidencePersistenceHook
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.memory.write_service import MemoryWriteService
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.settings import PulsaraSettings


@dataclass(frozen=True, slots=True)
class RuntimeWiring:
    runtime_session: RuntimeSession
    event_log: EventLog
    graph: GraphStore
    archive: ArtifactStore
    graph_id: str | None
    ledger: ExecutionEvidenceLedger
    memory_write_service: MemoryWriteService


@dataclass(frozen=True, slots=True)
class AgentRuntimeWiring:
    agent_runtime: AgentRuntime
    runtime_wiring: RuntimeWiring


def build_in_memory_runtime_wiring(
    workspace_root: Path,
    *,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
) -> RuntimeWiring:
    event_log = InMemoryEventLog()
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    runtime_session = RuntimeSession(
        workspace_root,
        **_runtime_session_id_kwargs(runtime_session_id),
        event_log=event_log,
    )
    _register_timeline_hook(
        runtime_session=runtime_session,
        graph=graph,
        archive=archive,
        graph_id=graph_id,
    )
    ledger, memory_write_service = _build_ledger_and_service(graph, archive, graph_id)
    return RuntimeWiring(
        runtime_session=runtime_session,
        event_log=event_log,
        graph=graph,
        archive=archive,
        graph_id=graph_id,
        ledger=ledger,
        memory_write_service=memory_write_service,
    )


def build_durable_runtime_wiring(
    settings: PulsaraSettings,
    workspace_root: Path,
    *,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
) -> RuntimeWiring:
    runtime_session_id = runtime_session_id or _new_runtime_session_id()
    event_log = PostgresEventLog(
        dsn=settings.storage.postgres_dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
    )
    runtime_session = RuntimeSession(
        workspace_root,
        runtime_session_id=event_log.runtime_session_id,
        event_log=event_log,
    )
    resolved_graph_id = graph_id or f"graph:runtime/{runtime_session.runtime_session_id}"
    graph = OxigraphGraphStore(settings.storage.oxigraph_url)
    archive = PostgresArtifactStore(dsn=settings.storage.postgres_dsn)
    _register_timeline_hook(
        runtime_session=runtime_session,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
    )
    ledger, memory_write_service = _build_ledger_and_service(graph, archive, resolved_graph_id)
    return RuntimeWiring(
        runtime_session=runtime_session,
        event_log=event_log,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
        ledger=ledger,
        memory_write_service=memory_write_service,
    )


def build_agent_runtime_wiring(
    settings: PulsaraSettings,
    workspace_root: Path,
    *,
    durable: bool,
    model_role: ModelRole,
    options: LLMOptions | None = None,
    system_prompt: str | None = None,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
) -> AgentRuntimeWiring:
    runtime_wiring = (
        build_durable_runtime_wiring(
            settings,
            workspace_root,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
        )
        if durable
        else build_in_memory_runtime_wiring(
            workspace_root,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
        )
    )
    agent_runtime = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=build_llm_runtime(settings.llm),
        memory_hooks=DurableMemoryHooks(
            service=runtime_wiring.memory_write_service,
            sink=runtime_wiring.runtime_session.memory_proposal_sink,
        ),
        tool_result_persistence_hook=ExecutionEvidencePersistenceHook(ledger=runtime_wiring.ledger),
        model_role=model_role,
        options=options,
        system_prompt=system_prompt,
    )
    return AgentRuntimeWiring(
        agent_runtime=agent_runtime,
        runtime_wiring=runtime_wiring,
    )


def _build_ledger_and_service(
    graph: GraphStore,
    archive: ArtifactStore,
    graph_id: str | None,
) -> tuple[ExecutionEvidenceLedger, MemoryWriteService]:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=archive,
        gate=MemoryWriteGate(),
        graph_id=graph_id or DEFAULT_GRAPH_ID,
    )
    return ledger, MemoryWriteService(ledger=ledger)


def _register_timeline_hook(
    *,
    runtime_session: RuntimeSession,
    graph: GraphStore,
    archive: ArtifactStore,
    graph_id: str | None,
) -> None:
    runtime_session.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime_session.event_log,
            graph_id=graph_id,
        ),
    )


def _runtime_session_id_kwargs(runtime_session_id: str | None) -> dict[str, str]:
    if runtime_session_id is None:
        return {}
    return {"runtime_session_id": runtime_session_id}


def _new_runtime_session_id() -> str:
    return f"runtime:{uuid4().hex}"
