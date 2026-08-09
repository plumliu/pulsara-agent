"""Legacy memory facade with lazy compatibility exports.

Stage 2 memory production code imports the PostgreSQL-only kernel modules
directly.  Importing a focused ``pulsara_agent.memory.*`` module must not
eagerly load the old governance/EventLog/Oxigraph graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_MODULES = (
    "pulsara_agent.memory.artifacts.archive",
    "pulsara_agent.memory.candidates.pool",
    "pulsara_agent.memory.governance.dedupe",
    "pulsara_agent.memory.recall.explain",
    "pulsara_agent.memory.governance.executor",
    "pulsara_agent.memory.governance.engine",
    "pulsara_agent.memory.governance.relatedness",
    "pulsara_agent.memory.canonical.ledger",
    "pulsara_agent.memory.canonical.lifecycle",
    "pulsara_agent.memory.artifacts.postgres_archive",
    "pulsara_agent.memory.recall.projection",
    "pulsara_agent.memory.recall.projection_ledger",
    "pulsara_agent.memory.foundation.protocols",
    "pulsara_agent.replay.provenance",
    "pulsara_agent.memory.canonical.query",
    "pulsara_agent.memory.recall.service",
    "pulsara_agent.memory.foundation.records",
    "pulsara_agent.ports.artifact",
    "pulsara_agent.memory.recall.rerank",
    "pulsara_agent.memory.scope",
    "pulsara_agent.memory.working_context",
    "pulsara_agent.memory.reflection.engine",
    "pulsara_agent.memory.foundation.run_timeline_query",
    "pulsara_agent.memory.recall.trace",
    "pulsara_agent.memory.recall.hybrid",
    "pulsara_agent.memory.recall.graph",
    "pulsara_agent.memory.recall.sparse",
    "pulsara_agent.memory.recall.dense",
    "pulsara_agent.memory.recall.semantic_rerank",
    "pulsara_agent.memory.canonical.vector_index_sync",
    "pulsara_agent.memory.canonical.vector_query",
    "pulsara_agent.memory.canonical.unit_of_work",
    "pulsara_agent.memory.canonical.write_service",
)


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    for module_name in _LAZY_MODULES:
        module = import_module(module_name)
        try:
            value = getattr(module, name)
        except AttributeError:
            continue
        globals()[name] = value
        return value
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "ArtifactStore",
    "ArtifactContentConflict",
    "ArtifactPutConfirmation",
    "ArtifactWriteResult",
    "CandidateOrigin",
    "CandidateRelatedness",
    "CanonicalNodeView",
    "CandidatePool",
    "CandidatePoolProposal",
    "CTX_USER",
    "ClaimKind",
    "ContradictAndSubmitDecision",
    "CorrectAndSubmitDecision",
    "CanonicalMemoryLedger",
    "Explanation",
    "ExplanationClaim",
    "GovernanceDecision",
    "GovernanceRelatednessService",
    "GovernanceWriteOutcome",
    "InMemoryArchiveStore",
    "InMemoryCandidatePool",
    "MemoryGovernanceApplyResult",
    "MemoryDomainContext",
    "MemoryGovernanceEngine",
    "MemoryGovernanceExecutor",
    "MemoryLifecycle",
    "MemoryGovernanceOptions",
    "MemoryGovernanceRelatednessOptions",
    "MemoryGovernanceOutput",
    "MemoryGovernanceRunResult",
    "MemoryQuery",
    "MemoryRelationEdge",
    "MemoryRecallService",
    "HybridMemoryRecallService",
    "GraphCandidateService",
    "SparseCandidateService",
    "DenseCandidateService",
    "RecallRerankService",
    "MemoryVectorIndexSync",
    "MemoryVectorQuery",
    "VectorSyncResult",
    "VectorSyncStatus",
    "MemoryGovernanceDecisionRecord",
    "MemoryWriteOutcome",
    "MemoryWriteService",
    "MemoryWriteUnitOfWork",
    "MergeAndSubmitDecision",
    "MemoryReflectionEngine",
    "MemoryReflectionHint",
    "MemoryReflectionOptions",
    "NoWriteOutcome",
    "PooledMemoryCandidate",
    "PostgresArtifactStore",
    "PostgresCandidatePool",
    "PostgresWorkingContextStore",
    "PostgresMemoryQuery",
    "PostgresRecallTraceStore",
    "ProjectionBuilder",
    "ProjectionLedger",
    "RecallItem",
    "RecallPath",
    "RecallPathStep",
    "RecallQuery",
    "RecallResult",
    "RecallStatus",
    "RecallTrigger",
    "RecallTraceStore",
    "RelatedCanonicalMemory",
    "RelatednessAvailability",
    "RelatednessBatchResult",
    "RelatednessExecutionContext",
    "LexicalMemoryRecallService",
    "RuntimeEventReadStore",
    "RuntimeEventRef",
    "RuntimeEventSpan",
    "RunTimelineExportLimitExceeded",
    "RunTimelinePage",
    "RunTimelinePageCursor",
    "RunTimelineSummary",
    "RunTimelineToolTrace",
    "SkipDecision",
    "SubmitAsIsDecision",
    "SupersedeAndSubmitDecision",
    "WriteFailedOutcome",
    "WriteSucceededOutcome",
    "WorkingContextSummary",
    "WorkingContextUpdate",
    "already_exists",
    "candidate_fingerprint",
    "canonical_project_key",
    "direct_relation_rerank",
    "explain_memory",
    "explanation_to_payload",
    "format_scope_list",
    "governance_batch_context",
    "is_valid_flat_id",
    "is_valid_scope",
    "load_run_timeline",
    "load_run_timeline_page",
    "new_governance_batch_id",
    "parse_scope",
    "propose_working_context_update",
    "runtime_event_span_from_events",
    "scopes_for_domain",
    "summarize_run_timeline",
    "summarize_persisted_run_timeline",
    "validate_explanation",
    "workspace_scope_key",
    "workspace_scope",
    "working_context_projection",
]
