"""Composition roots for runtime persistence wiring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore, InMemoryGraphStore, OxigraphGraphStore
from pulsara_agent.llm import ModelRole, build_llm_runtime
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory import (
    ArtifactStore,
    CandidatePool,
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    MemoryGovernanceEngine,
    MemoryGovernanceExecutor,
    MemoryGovernanceOptions,
    PostgresArtifactStore,
    PostgresCandidatePool,
)
from pulsara_agent.memory.durable_hooks import DurableMemoryHooks, ReflectiveMemoryHooks
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.reflection import MemoryReflectionEngine, MemoryReflectionOptions
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
    candidate_pool: CandidatePool
    memory_governance_executor: MemoryGovernanceExecutor
    memory_governance_engine: MemoryGovernanceEngine | None = None


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
    candidate_pool = InMemoryCandidatePool()
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
    memory_governance_executor = _build_memory_governance_executor(
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        event_log=event_log,
        graph=graph,
        graph_id=graph_id,
        runtime_session_id=runtime_session.runtime_session_id,
    )
    return RuntimeWiring(
        runtime_session=runtime_session,
        event_log=event_log,
        graph=graph,
        archive=archive,
        graph_id=graph_id,
        ledger=ledger,
        candidate_pool=candidate_pool,
        memory_governance_executor=memory_governance_executor,
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
    candidate_pool = PostgresCandidatePool(dsn=settings.storage.postgres_dsn)
    _register_timeline_hook(
        runtime_session=runtime_session,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
    )
    ledger, memory_write_service = _build_ledger_and_service(graph, archive, resolved_graph_id)
    memory_governance_executor = _build_memory_governance_executor(
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        event_log=event_log,
        graph=graph,
        graph_id=resolved_graph_id,
        runtime_session_id=runtime_session.runtime_session_id,
    )
    return RuntimeWiring(
        runtime_session=runtime_session,
        event_log=event_log,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
        ledger=ledger,
        candidate_pool=candidate_pool,
        memory_governance_executor=memory_governance_executor,
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
    memory_reflection: bool = True,
    memory_reflection_options: MemoryReflectionOptions | None = None,
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
    llm_runtime = build_llm_runtime(settings.llm)
    runtime_wiring = _with_memory_governance_engine(runtime_wiring, llm_runtime=llm_runtime)
    agent_runtime = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=llm_runtime,
        memory_hooks=_build_memory_hooks(
            runtime_wiring=runtime_wiring,
            llm_runtime=llm_runtime,
            memory_reflection=memory_reflection,
            memory_reflection_options=memory_reflection_options,
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


def _with_memory_governance_engine(runtime_wiring: RuntimeWiring, *, llm_runtime) -> RuntimeWiring:
    return RuntimeWiring(
        runtime_session=runtime_wiring.runtime_session,
        event_log=runtime_wiring.event_log,
        graph=runtime_wiring.graph,
        archive=runtime_wiring.archive,
        graph_id=runtime_wiring.graph_id,
        ledger=runtime_wiring.ledger,
        candidate_pool=runtime_wiring.candidate_pool,
        memory_governance_executor=runtime_wiring.memory_governance_executor,
        memory_governance_engine=MemoryGovernanceEngine(
            llm_runtime=llm_runtime,
            executor=runtime_wiring.memory_governance_executor,
            options=MemoryGovernanceOptions(),
        ),
    )


def _build_memory_hooks(
    *,
    runtime_wiring: RuntimeWiring,
    llm_runtime,
    memory_reflection: bool,
    memory_reflection_options: MemoryReflectionOptions | None,
):
    if not memory_reflection:
        return DurableMemoryHooks(
            candidate_pool=runtime_wiring.candidate_pool,
            sink=runtime_wiring.runtime_session.memory_proposal_sink,
        )
    reflection = MemoryReflectionEngine(
        llm_runtime=llm_runtime,
        candidate_pool=runtime_wiring.candidate_pool,
        graph=runtime_wiring.graph,
        graph_id=runtime_wiring.graph_id,
        options=memory_reflection_options or MemoryReflectionOptions(),
    )
    return ReflectiveMemoryHooks(
        candidate_pool=runtime_wiring.candidate_pool,
        sink=runtime_wiring.runtime_session.memory_proposal_sink,
        reflection=reflection,
        event_store=runtime_wiring.event_log,
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


def _build_memory_governance_executor(
    *,
    candidate_pool: CandidatePool,
    memory_write_service: MemoryWriteService,
    event_log: EventLog,
    graph: GraphStore,
    graph_id: str | None,
    runtime_session_id: str,
) -> MemoryGovernanceExecutor:
    return MemoryGovernanceExecutor(
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        event_log=event_log,
        graph=graph,
        graph_id=graph_id,
        runtime_session_id=runtime_session_id,
    )


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
