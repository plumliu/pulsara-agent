"""Component-test runtime composition.

This module is intentionally outside ``src/``.  It may assemble deterministic
in-memory adapters, but its outcomes are never durable correctness evidence.
"""

from __future__ import annotations

from pathlib import Path

from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory import InMemoryArchiveStore, InMemoryCandidatePool
from pulsara_agent.memory.candidates.projection_outbox import (
    CandidateProjectionOutboxDispatcher,
    InMemoryCandidateProjectionOutbox,
    MemoryCandidateProjectionCommitPort,
)
from pulsara_agent.memory.governance.batch_input import (
    MemoryGovernanceBatchPreparationCommitPort,
)
from pulsara_agent.memory.governance.claims import (
    InMemoryMemoryGovernanceCandidateClaimRepository,
)
from pulsara_agent.memory.governance.evidence import GovernanceSourceEvidenceBuilder
from pulsara_agent.memory.governance.preparation import (
    InMemoryGovernanceBatchPreparationRepository,
)
from pulsara_agent.memory.reflection.engine import MemoryReflectionOptions
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.runtime.mcp.installation import empty_mcp_installation
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import McpInstalledCapabilitySnapshot
from pulsara_agent.runtime.permission import EffectivePermissionPolicy
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.terminal import TerminalRuntimeBinding
from pulsara_agent.runtime.wiring import (
    AgentRuntimeWiring,
    RuntimeWiring,
    _allowed_write_scopes,
    _build_ledger_and_service,
    _build_memory_governance_executor,
    _governance_async_operation_port,
    _runtime_session_id_kwargs,
    _validate_graph_domain_coupling,
    compose_agent_runtime_wiring,
)
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.settings import PulsaraSettings
from tests.support.artifacts import FakeToolResultArtifactIndex
from tests.support.memory_uow import FakeMemoryWriteUnitOfWork


def build_component_runtime_wiring(
    workspace_root: Path,
    *,
    runtime_session_id: str | None = None,
    reopen_deadline_monotonic: float | None = None,
    graph_id: str | None = None,
    memory_domain: MemoryDomainContext | None = None,
    terminal_binding: TerminalRuntimeBinding | None = None,
    mcp_installation: McpInstalledCapabilitySnapshot | None = None,
) -> RuntimeWiring:
    resolved_graph_id = graph_id or (
        memory_domain.graph_id if memory_domain is not None else None
    )
    _validate_graph_domain_coupling(resolved_graph_id, memory_domain)
    event_log = InMemoryEventLog()
    graph = InMemoryGraphStore()
    archive = InMemoryArchiveStore()
    candidate_pool = InMemoryCandidatePool()
    installation = mcp_installation or empty_mcp_installation()
    runtime_session = RuntimeSession(
        workspace_root,
        **_runtime_session_id_kwargs(runtime_session_id),
        event_log=event_log,
        archive=archive,
        tool_result_artifacts=FakeToolResultArtifactIndex(),
        reopen_deadline_monotonic=reopen_deadline_monotonic,
        terminal_binding=terminal_binding,
        dynamic_tool_installations=installation.ordered_binding_installations,
        allow_unbootstrapped_test_events=True,
    )
    candidate_projection_outbox = InMemoryCandidateProjectionOutbox()
    candidate_projection_dispatcher = CandidateProjectionOutboxDispatcher(
        runtime_session_id=runtime_session.runtime_session_id,
        repository=candidate_projection_outbox,
        candidate_pool=candidate_pool,
    )
    candidate_projection_commit_port = MemoryCandidateProjectionCommitPort(
        runtime_session=runtime_session,
        repository=candidate_projection_outbox,
        dispatcher=candidate_projection_dispatcher,
    )
    ledger, memory_write_service = _build_ledger_and_service(
        graph,
        resolved_graph_id,
    )
    memory_governance_executor = _build_memory_governance_executor(
        candidate_pool=candidate_pool,
        memory_write_service=memory_write_service,
        event_log=event_log,
        event_commit_port=lambda events: (
            runtime_session.write_events_from_thread(events).committed_events
        ),
        async_operation_port=_governance_async_operation_port(runtime_session),
        graph=graph,
        graph_id=resolved_graph_id,
        runtime_session_id=runtime_session.runtime_session_id,
        memory_write_uow_factory=lambda: FakeMemoryWriteUnitOfWork(
            graph=graph,
            candidate_pool=candidate_pool,
            memory_write_service=memory_write_service,
            graph_id=resolved_graph_id,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
        allowed_write_scopes=_allowed_write_scopes(memory_domain),
    )
    governance_claims = InMemoryMemoryGovernanceCandidateClaimRepository(
        candidate_pool=candidate_pool
    )
    governance_preparations = InMemoryGovernanceBatchPreparationRepository()
    governance_evidence = GovernanceSourceEvidenceBuilder(
        runtime_session_id=runtime_session.runtime_session_id,
        event_log=event_log,
        archive=archive,
    )
    governance_preparation_commit = MemoryGovernanceBatchPreparationCommitPort(
        runtime_session=runtime_session,
        claim_repository=governance_claims,
        preparation_repository=governance_preparations,
    )
    return RuntimeWiring(
        runtime_session=runtime_session,
        event_log=event_log,
        graph=graph,
        archive=archive,
        graph_id=resolved_graph_id,
        ledger=ledger,
        candidate_pool=candidate_pool,
        candidate_projection_commit_port=candidate_projection_commit_port,
        memory_governance_executor=memory_governance_executor,
        memory_governance_claim_repository=governance_claims,
        memory_governance_preparation_repository=governance_preparations,
        memory_governance_evidence_builder=governance_evidence,
        memory_governance_preparation_commit_port=governance_preparation_commit,
        memory_recall_service=None,
        memory_query=None,
        memory_domain=memory_domain,
        working_context_store=None,
        mcp_installation=installation,
    )


def build_component_agent_runtime_wiring(
    settings: PulsaraSettings,
    workspace_root: Path,
    *,
    model_role: ModelRole,
    options: LLMOptions | None = None,
    system_prompt: str | None = None,
    runtime_session_id: str | None = None,
    reopen_deadline_monotonic: float | None = None,
    graph_id: str | None = None,
    memory_domain: MemoryDomainContext | None = None,
    memory_reflection: bool = True,
    memory_reflection_options: MemoryReflectionOptions | None = None,
    terminal_binding: TerminalRuntimeBinding | None = None,
    capability_runtime: CapabilityRuntime | None = None,
    enable_workspace_skills: bool = True,
    permission_policy: EffectivePermissionPolicy | None = None,
    mcp_supervisor: McpServerSupervisor | None = None,
    mcp_installation: McpInstalledCapabilitySnapshot | None = None,
) -> AgentRuntimeWiring:
    installation = mcp_installation or empty_mcp_installation()
    runtime_wiring = build_component_runtime_wiring(
        workspace_root,
        runtime_session_id=runtime_session_id,
        reopen_deadline_monotonic=reopen_deadline_monotonic,
        graph_id=graph_id,
        memory_domain=memory_domain,
        terminal_binding=terminal_binding,
        mcp_installation=installation,
    )
    return compose_agent_runtime_wiring(
        settings=settings,
        runtime_wiring=runtime_wiring,
        model_role=model_role,
        options=options,
        system_prompt=system_prompt,
        memory_reflection=memory_reflection,
        memory_reflection_options=memory_reflection_options,
        capability_runtime=capability_runtime,
        enable_workspace_skills=enable_workspace_skills,
        permission_policy=permission_policy,
        mcp_supervisor=mcp_supervisor,
        mcp_installation=installation,
    )


__all__ = [
    "build_component_agent_runtime_wiring",
    "build_component_runtime_wiring",
]
