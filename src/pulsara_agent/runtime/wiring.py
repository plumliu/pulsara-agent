"""Composition roots for runtime persistence wiring."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pulsara_agent.capability import CapabilityResolver, LocalSkillResolver
from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog, InMemoryEventLog, PostgresEventLog
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore, InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.graph.durable_facade import DurableGraphFacade
from pulsara_agent.graph.oxigraph import OxigraphGraphStore
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
    MemoryRecallService,
    PostgresMemoryQuery,
    PostgresArtifactStore,
    PostgresCandidatePool,
)
from pulsara_agent.memory.recall.hybrid import HybridMemoryRecallService
from pulsara_agent.memory.recall.graph import GraphCandidateService
from pulsara_agent.memory.recall.sparse import SparseCandidateService
from pulsara_agent.memory.recall.dense import DenseCandidateService
from pulsara_agent.memory.recall.semantic_rerank import RecallRerankService
from pulsara_agent.memory.canonical.vector_query import MemoryVectorQuery
from pulsara_agent.memory.canonical.outbox_replay_hook import CanonicalMutationOutboxReplayHook
from pulsara_agent.memory.hooks.durable import DurableMemoryHooks, ReflectiveMemoryHooks
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.reflection.engine import MemoryReflectionEngine, MemoryReflectionOptions
from pulsara_agent.memory.hooks.run_timeline_persistence import RunTimelinePersistenceHook
from pulsara_agent.memory.hooks.runtime_persistence import ExecutionEvidencePersistenceHook
from pulsara_agent.memory.recall.trace import PostgresRecallTraceStore
from pulsara_agent.memory.governance.coordinator import MemoryGovernanceCoordinator
from pulsara_agent.memory.governance.relatedness import (
    GovernanceRelatednessService,
    MemoryGovernanceRelatednessOptions,
)
from pulsara_agent.memory.canonical.unit_of_work import (
    GovernanceWriteUnitOfWork,
    InMemoryMemoryWriteUnitOfWork,
    MemoryWriteUnitOfWork,
)
from pulsara_agent.memory.canonical.mutation_outbox import CanonicalMutationSurface, MutationOutboxWriter
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.memory.scope import CTX_USER, MemoryDomainContext
from pulsara_agent.memory.working_context import PostgresWorkingContextStore
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.permission import EffectivePermissionPolicy, default_permission_policy
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.terminal import TerminalRuntimeBinding
from pulsara_agent.runtime.tool_artifacts import InMemoryToolResultArtifactIndex, PostgresToolResultArtifactIndex
from pulsara_agent.retrieval.runtime import RetrievalRuntimeResources
from pulsara_agent.retrieval.tokenizer.factory import build_tokenizer
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
    retrieval_resources: RetrievalRuntimeResources | None = None
    governance_coordinator: MemoryGovernanceCoordinator | None = None
    governance_relatedness: GovernanceRelatednessService | None = None


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
    terminal_binding: TerminalRuntimeBinding | None = None,
) -> RuntimeWiring:
    warnings.warn(
        "build_in_memory_runtime_wiring() is compatibility/test-only; "
        "production runtimes require PostgreSQL durable wiring",
        DeprecationWarning,
        stacklevel=2,
    )
    resolved_graph_id = graph_id or (memory_domain.graph_id if memory_domain is not None else None)
    _validate_graph_domain_coupling(resolved_graph_id, memory_domain)
    event_log = InMemoryEventLog()
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    tool_result_artifacts = InMemoryToolResultArtifactIndex()
    candidate_pool = InMemoryCandidatePool()
    runtime_session = RuntimeSession(
        workspace_root,
        **_runtime_session_id_kwargs(runtime_session_id),
        event_log=event_log,
        archive=archive,
        tool_result_artifacts=tool_result_artifacts,
        terminal_binding=terminal_binding,
    )
    _register_timeline_hook(
        runtime_session=runtime_session,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
        mutation_outbox=None,
    )
    ledger, memory_write_service = _build_ledger_and_service(
        graph,
        archive,
        resolved_graph_id,
        mutation_outbox=None,
    )
    memory_governance_executor = _build_memory_governance_executor(
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        event_log=event_log,
        graph=graph,
        graph_id=resolved_graph_id,
        runtime_session_id=runtime_session.runtime_session_id,
        memory_write_uow_factory=lambda: InMemoryMemoryWriteUnitOfWork(
            graph=graph,
            candidate_pool=candidate_pool,
            memory_write_service=memory_write_service,
            graph_id=resolved_graph_id,
        ),
        allowed_write_scopes=_allowed_write_scopes(memory_domain),
        stored_event_publisher=runtime_session.publish_stored_events,
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
    terminal_binding: TerminalRuntimeBinding | None = None,
    retrieval_resources: RetrievalRuntimeResources | None = None,
    governance_coordinator: MemoryGovernanceCoordinator | None = None,
) -> RuntimeWiring:
    runtime_session_id = runtime_session_id or _new_runtime_session_id()
    event_log = PostgresEventLog(
        dsn=settings.storage.postgres_dsn,
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
    )
    archive = PostgresArtifactStore(dsn=settings.storage.postgres_dsn)
    tool_result_artifacts = PostgresToolResultArtifactIndex(dsn=settings.storage.postgres_dsn)
    runtime_session = RuntimeSession(
        workspace_root,
        runtime_session_id=event_log.runtime_session_id,
        event_log=event_log,
        archive=archive,
        tool_result_artifacts=tool_result_artifacts,
        terminal_binding=terminal_binding,
    )
    resolved_graph_id = graph_id or (
        memory_domain.graph_id
        if memory_domain is not None
        else f"graph:runtime/{runtime_session.runtime_session_id}"
    )
    _validate_graph_domain_coupling(resolved_graph_id, memory_domain)
    postgres_graph = PostgresGraphStore(settings.storage.postgres_dsn)
    if not settings.storage.oxigraph_url.strip():
        raise ValueError("durable runtime wiring requires a non-empty Oxigraph URL")
    oxigraph_graph = OxigraphGraphStore(settings.storage.oxigraph_url)
    graph: GraphStore = DurableGraphFacade(postgres=postgres_graph, oxigraph=oxigraph_graph)
    candidate_pool = PostgresCandidatePool(dsn=settings.storage.postgres_dsn)
    memory_query = PostgresMemoryQuery(dsn=settings.storage.postgres_dsn)
    tokenizer = build_tokenizer(settings.retrieval.tokenizer)
    working_context_store = (
        PostgresWorkingContextStore(dsn=settings.storage.postgres_dsn)
        if memory_domain is not None
        else None
    )
    sparse_recall = SparseCandidateService(
        memory_query=memory_query,
        tokenizer=tokenizer,
    )
    dense_recall = (
        DenseCandidateService(
            provider=retrieval_resources.embedding,
            vector_query=MemoryVectorQuery(settings.storage.postgres_dsn),
            provider_name=settings.retrieval.embedding.provider,
        )
        if retrieval_resources is not None and retrieval_resources.embedding is not None
        else None
    )
    semantic_reranker = (
        RecallRerankService(provider=retrieval_resources.rerank)
        if retrieval_resources is not None and retrieval_resources.rerank is not None
        else None
    )
    relatedness_config = settings.retrieval.governance_relatedness
    governance_relatedness = GovernanceRelatednessService(
        memory_query=memory_query,
        tokenizer=tokenizer,
        embedding=(retrieval_resources.embedding if retrieval_resources is not None else None),
        vector_query=(
            MemoryVectorQuery(settings.storage.postgres_dsn)
            if retrieval_resources is not None and retrieval_resources.embedding is not None
            else None
        ),
        reranker=(retrieval_resources.rerank if retrieval_resources is not None else None),
        provider_name=settings.retrieval.embedding.provider,
        options=MemoryGovernanceRelatednessOptions(
            policy_version=relatedness_config.policy_version,
            fixture_version=relatedness_config.fixture_version,
            candidate_limit=relatedness_config.candidate_limit,
            lexical_limit=relatedness_config.lexical_limit,
            vector_limit=relatedness_config.vector_limit,
            rerank_top_m=relatedness_config.rerank_top_m,
            dense_candidate_min_score=relatedness_config.dense_candidate_min_score,
            rerank_candidate_min_score=relatedness_config.rerank_candidate_min_score,
            max_inline_gap_embeds=relatedness_config.max_inline_gap_embeds,
            provider_timeout_seconds=relatedness_config.provider_timeout_seconds,
        ),
    )
    memory_recall_service = HybridMemoryRecallService(
        memory_query=memory_query,
        sparse=sparse_recall,
        dense=dense_recall,
        reranker=semantic_reranker,
        trace_store=PostgresRecallTraceStore(dsn=settings.storage.postgres_dsn),
        graph_candidates=GraphCandidateService(memory_query=memory_query),
    )
    _register_timeline_hook(
        runtime_session=runtime_session,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
        mutation_outbox=MutationOutboxWriter(dsn=settings.storage.postgres_dsn),
    )
    runtime_session.hook_manager.register_event(
        None,
        CanonicalMutationOutboxReplayHook(
            dsn=settings.storage.postgres_dsn,
            graph_id=resolved_graph_id,
            oxigraph_url=settings.storage.oxigraph_url,
        ),
    )
    ledger, memory_write_service = _build_ledger_and_service(
        graph,
        archive,
        resolved_graph_id,
        mutation_outbox=MutationOutboxWriter(dsn=settings.storage.postgres_dsn),
    )
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
        stored_event_publisher=runtime_session.publish_stored_events,
        async_surfaces=(
            CanonicalMutationSurface.SEARCH_INDEX.value,
            CanonicalMutationSurface.OXIGRAPH.value,
            *(
                (CanonicalMutationSurface.VECTOR_INDEX.value,)
                if retrieval_resources is not None and retrieval_resources.embedding is not None
                else ()
            ),
        ),
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
        retrieval_resources=retrieval_resources,
        governance_coordinator=governance_coordinator,
        governance_relatedness=governance_relatedness,
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
    terminal_binding: TerminalRuntimeBinding | None = None,
    capability_resolver: CapabilityResolver | None = None,
    enable_workspace_skills: bool = True,
    permission_policy: EffectivePermissionPolicy | None = None,
    retrieval_resources: RetrievalRuntimeResources | None = None,
    governance_coordinator: MemoryGovernanceCoordinator | None = None,
) -> AgentRuntimeWiring:
    runtime_wiring = (
        build_durable_runtime_wiring(
            settings,
            workspace_root,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            memory_domain=memory_domain,
            terminal_binding=terminal_binding,
            retrieval_resources=retrieval_resources,
            governance_coordinator=governance_coordinator,
        )
        if durable
        else build_in_memory_runtime_wiring(
            workspace_root,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            memory_domain=memory_domain,
            terminal_binding=terminal_binding,
        )
    )
    llm_runtime = build_llm_runtime(settings.llm)
    runtime_wiring = _with_memory_governance_engine(runtime_wiring, llm_runtime=llm_runtime)
    effective_permission_policy = permission_policy or default_permission_policy()
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
        retrieval_resources=runtime_wiring.retrieval_resources,
        governance_coordinator=runtime_wiring.governance_coordinator,
        governance_relatedness=runtime_wiring.governance_relatedness,
        memory_governance_engine=MemoryGovernanceEngine(
            llm_runtime=llm_runtime,
            executor=runtime_wiring.memory_governance_executor,
            options=MemoryGovernanceOptions(),
            relatedness_service=runtime_wiring.governance_relatedness,
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
    mutation_outbox: MutationOutboxWriter | None = None,
) -> tuple[ExecutionEvidenceLedger, MemoryWriteService]:
    ledger = ExecutionEvidenceLedger(
        graph=graph,
        archive=archive,
        gate=MemoryWriteGate(),
        graph_id=graph_id or DEFAULT_GRAPH_ID,
        mutation_outbox=mutation_outbox,
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
    memory_write_uow_factory: Callable[[], GovernanceWriteUnitOfWork],
    allowed_write_scopes: frozenset[str],
    stored_event_publisher: Callable[[list[AgentEvent]], None] | None = None,
    async_surfaces: tuple[str, ...] = (
        CanonicalMutationSurface.SEARCH_INDEX.value,
        CanonicalMutationSurface.OXIGRAPH.value,
    ),
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
        stored_event_publisher=stored_event_publisher,
        async_surfaces=async_surfaces,
    )


def _register_timeline_hook(
    *,
    runtime_session: RuntimeSession,
    graph: GraphStore,
    archive: ArtifactStore,
    graph_id: str | None,
    mutation_outbox: MutationOutboxWriter | None = None,
) -> None:
    runtime_session.hook_manager.register_event(
        None,
        RunTimelinePersistenceHook(
            graph=graph,
            archive=archive,
            event_store=runtime_session.event_log,
            graph_id=graph_id,
            mutation_outbox=mutation_outbox,
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
