"""Composition roots for runtime persistence wiring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pulsara_agent.capability import CapabilityResolver, LocalSkillResolver
from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore, InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.llm import ModelRole, build_llm_runtime
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory import (
    ArtifactStore,
    CandidatePool,
    InMemoryArchiveStore,
    InMemoryCandidatePool,
    LexicalMemoryRecallService,
    MemoryGovernanceEngine,
    MemoryGovernanceExecutor,
    MemoryGovernanceOptions,
    MemoryRecallService,
    PostgresMemoryQuery,
    PostgresArtifactStore,
    PostgresCandidatePool,
)
from pulsara_agent.memory.hooks.durable import DurableMemoryHooks, ReflectiveMemoryHooks
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.reflection.engine import MemoryReflectionEngine, MemoryReflectionOptions
from pulsara_agent.memory.hooks.run_timeline_persistence import RunTimelinePersistenceHook
from pulsara_agent.memory.hooks.runtime_persistence import ExecutionEvidencePersistenceHook
from pulsara_agent.memory.recall.trace import PostgresRecallTraceStore
from pulsara_agent.memory.canonical.unit_of_work import MemoryWriteUnitOfWork
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.memory.scope import CTX_USER, MemoryDomainContext
from pulsara_agent.memory.working_context import PostgresWorkingContextStore
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.permission import EffectivePermissionPolicy, default_permission_policy
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.terminal import TerminalSessionManager
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
    memory_recall_service: MemoryRecallService | None = None
    memory_query: PostgresMemoryQuery | None = None
    memory_governance_engine: MemoryGovernanceEngine | None = None
    memory_domain: MemoryDomainContext | None = None
    working_context_store: PostgresWorkingContextStore | None = None


@dataclass(frozen=True, slots=True)
class AgentRuntimeWiring:
    agent_runtime: AgentRuntime
    runtime_wiring: RuntimeWiring


def build_in_memory_runtime_wiring(
    workspace_root: Path,
    *,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
    memory_domain: MemoryDomainContext | None = None,
    terminal_session_manager: TerminalSessionManager | None = None,
    terminal_owner_host_session_id: str | None = None,
    owns_terminal_session_manager: bool = True,
) -> RuntimeWiring:
    resolved_graph_id = graph_id or (memory_domain.graph_id if memory_domain is not None else None)
    _validate_graph_domain_coupling(resolved_graph_id, memory_domain)
    event_log = InMemoryEventLog()
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    runtime_session = RuntimeSession(
        workspace_root,
        **_runtime_session_id_kwargs(runtime_session_id),
        event_log=event_log,
        terminal_session_manager=terminal_session_manager,
        terminal_owner_host_session_id=terminal_owner_host_session_id,
        owns_terminal_session_manager=owns_terminal_session_manager,
    )
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
        allowed_write_scopes=_allowed_write_scopes(memory_domain),
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
        memory_recall_service=None,
        memory_query=None,
        memory_domain=memory_domain,
        working_context_store=None,
    )


def build_durable_runtime_wiring(
    settings: PulsaraSettings,
    workspace_root: Path,
    *,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
    memory_domain: MemoryDomainContext | None = None,
    terminal_session_manager: TerminalSessionManager | None = None,
    terminal_owner_host_session_id: str | None = None,
    owns_terminal_session_manager: bool = True,
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
        terminal_session_manager=terminal_session_manager,
        terminal_owner_host_session_id=terminal_owner_host_session_id,
        owns_terminal_session_manager=owns_terminal_session_manager,
    )
    resolved_graph_id = graph_id or (
        memory_domain.graph_id
        if memory_domain is not None
        else f"graph:runtime/{runtime_session.runtime_session_id}"
    )
    _validate_graph_domain_coupling(resolved_graph_id, memory_domain)
    graph = PostgresGraphStore(settings.storage.postgres_dsn)
    archive = PostgresArtifactStore(dsn=settings.storage.postgres_dsn)
    candidate_pool = PostgresCandidatePool(dsn=settings.storage.postgres_dsn)
    memory_query = PostgresMemoryQuery(dsn=settings.storage.postgres_dsn)
    working_context_store = (
        PostgresWorkingContextStore(dsn=settings.storage.postgres_dsn)
        if memory_domain is not None
        else None
    )
    memory_recall_service = LexicalMemoryRecallService(
        memory_query=memory_query,
        trace_store=PostgresRecallTraceStore(dsn=settings.storage.postgres_dsn),
    )
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
        memory_write_uow_factory=lambda: MemoryWriteUnitOfWork(
            dsn=settings.storage.postgres_dsn,
            runtime_session_id=runtime_session.runtime_session_id,
            graph_id=resolved_graph_id,
            archive=archive,
            workspace_root=workspace_root,
        ),
        allowed_write_scopes=_allowed_write_scopes(memory_domain),
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
        memory_recall_service=memory_recall_service,
        memory_query=memory_query,
        memory_domain=memory_domain,
        working_context_store=working_context_store,
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
    memory_domain: MemoryDomainContext | None = None,
    memory_reflection: bool = True,
    memory_reflection_options: MemoryReflectionOptions | None = None,
    terminal_session_manager: TerminalSessionManager | None = None,
    terminal_owner_host_session_id: str | None = None,
    owns_terminal_session_manager: bool = True,
    capability_resolver: CapabilityResolver | None = None,
    enable_workspace_skills: bool = True,
    permission_policy: EffectivePermissionPolicy | None = None,
) -> AgentRuntimeWiring:
    runtime_wiring = (
        build_durable_runtime_wiring(
            settings,
            workspace_root,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            memory_domain=memory_domain,
            terminal_session_manager=terminal_session_manager,
            terminal_owner_host_session_id=terminal_owner_host_session_id,
            owns_terminal_session_manager=owns_terminal_session_manager,
        )
        if durable
        else build_in_memory_runtime_wiring(
            workspace_root,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            memory_domain=memory_domain,
            terminal_session_manager=terminal_session_manager,
            terminal_owner_host_session_id=terminal_owner_host_session_id,
            owns_terminal_session_manager=owns_terminal_session_manager,
        )
    )
    llm_runtime = build_llm_runtime(settings.llm)
    runtime_wiring = _with_memory_governance_engine(runtime_wiring, llm_runtime=llm_runtime)
    effective_permission_policy = permission_policy or default_permission_policy(
        workspace_kind=runtime_wiring.memory_domain.workspace_kind
        if runtime_wiring.memory_domain is not None
        else "transient"
    )
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
        capability_resolver=capability_resolver
        if capability_resolver is not None
        else (LocalSkillResolver() if enable_workspace_skills else None),
        memory_domain=runtime_wiring.memory_domain,
        workspace_kind=runtime_wiring.memory_domain.workspace_kind if runtime_wiring.memory_domain is not None else "transient",
        permission_policy=effective_permission_policy,
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
        memory_recall_service=runtime_wiring.memory_recall_service,
        memory_query=runtime_wiring.memory_query,
        memory_domain=runtime_wiring.memory_domain,
        working_context_store=runtime_wiring.working_context_store,
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
            event_store=runtime_wiring.event_log,
            recall=runtime_wiring.memory_recall_service,
            memory_query=runtime_wiring.memory_query,
            graph_id=runtime_wiring.graph_id,
            read_scopes=_read_scopes(runtime_wiring.memory_domain),
            working_context_store=runtime_wiring.working_context_store,
            working_context_domain=runtime_wiring.memory_domain,
        )
    reflection = MemoryReflectionEngine(
        llm_runtime=llm_runtime,
        candidate_pool=runtime_wiring.candidate_pool,
        graph=runtime_wiring.graph,
        graph_id=runtime_wiring.graph_id,
        allowed_scopes=_allowed_write_scopes(runtime_wiring.memory_domain),
        options=memory_reflection_options or MemoryReflectionOptions(),
    )
    return ReflectiveMemoryHooks(
        candidate_pool=runtime_wiring.candidate_pool,
        sink=runtime_wiring.runtime_session.memory_proposal_sink,
        event_store=runtime_wiring.event_log,
        recall=runtime_wiring.memory_recall_service,
        memory_query=runtime_wiring.memory_query,
        graph_id=runtime_wiring.graph_id,
        read_scopes=_read_scopes(runtime_wiring.memory_domain),
        working_context_store=runtime_wiring.working_context_store,
        working_context_domain=runtime_wiring.memory_domain,
        reflection=reflection,
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
    memory_write_uow_factory=None,
    allowed_write_scopes: frozenset[str],
) -> MemoryGovernanceExecutor:
    return MemoryGovernanceExecutor(
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        event_log=event_log,
        graph=graph,
        graph_id=graph_id,
        runtime_session_id=runtime_session_id,
        memory_write_uow_factory=memory_write_uow_factory,
        allowed_write_scopes=allowed_write_scopes,
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


def _allowed_write_scopes(memory_domain: MemoryDomainContext | None) -> frozenset[str]:
    if memory_domain is None:
        return frozenset({CTX_USER})
    return memory_domain.allowed_write_scopes


def _read_scopes(memory_domain: MemoryDomainContext | None) -> frozenset[str]:
    if memory_domain is None:
        return frozenset({CTX_USER})
    return memory_domain.read_scopes


def _validate_graph_domain_coupling(
    graph_id: str | None,
    memory_domain: MemoryDomainContext | None,
) -> None:
    if graph_id is not None and graph_id.startswith("graph:user/") and memory_domain is None:
        raise ValueError("graph:user/* memory graphs require memory_domain so read scopes are explicit")
