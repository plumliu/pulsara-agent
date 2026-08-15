"""Host-scoped product tools for the advisory-memory plane."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import math
from time import monotonic
from typing import TYPE_CHECKING, Mapping, Sequence

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.memory import (
    MAXIMUM_MEMORY_QUERY_RESULTS,
    MemoryQueryResult,
    PostgresMemoryQuery,
)
from pulsara_agent.conversation_kernel.memory.recall import (
    MemoryDenseCandidateDisposition,
    MemoryRetrievalDisposition,
    MemorySearchStageResult,
)
from pulsara_agent.conversation_kernel.context_sources import (
    build_memory_context_source,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    AutomaticMemoryTriggerDisposition,
    FrozenMemoryTriggerPolicy,
    FrozenMemoryProposal,
    MemoryFactKind,
    MemoryKindHint,
    MemoryProducerKind,
    MemoryUsePolicy,
    PreparedMemoryBasisReference,
    PreparedMemoryCandidateAcceptance,
    memory_response_preference_item_payload,
    prepare_memory_candidate,
)
from pulsara_agent.conversation_kernel.memory.reflection import (
    MemoryWriteOptOut,
    PreparedCheapHintReflectionHandoff,
    TurnMemoryUseOptOut,
    normalize_reflection_text,
    prepare_cheap_hint_reflection_handoff,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import (
    KernelToolInvocationContext,
    KernelToolResult,
)
from pulsara_agent.memory.scope import FrozenMemoryReadScopeBinding, MemoryScopeKind
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputSnapshot,
    ContextSourceAbsentFact,
    ContextSourceCandidate,
    ContextSourceAbsenceKind,
    ContextSourceKind,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceFormatHint,
)
from pulsara_agent.retrieval.config import (
    AdvisoryMemoryFeatureConfig,
    DenseRecallPurpose,
    EmbeddingBackendConfig,
    MEMORY_EMBEDDING_CONTRACT,
    RerankBackendConfig,
)
from pulsara_agent.retrieval.embedding.factory import build_embedding_provider
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.retrieval.rerank.factory import build_rerank_provider
from pulsara_agent.retrieval.rerank.protocol import RerankProvider

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.memory.governor import (
        AdvisoryMemoryGovernor,
    )


MEMORY_READ_TOOL_NAMES = frozenset({"memory_search", "memory_get", "memory_explain"})
MEMORY_WRITE_TOOL_NAMES = frozenset({"remember"})
MEMORY_TOOL_NAMES = MEMORY_READ_TOOL_NAMES | MEMORY_WRITE_TOOL_NAMES
MAXIMUM_MEMORY_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024
MEMORY_POINT_READ_TIMEOUT_SECONDS = 10.0
MEMORY_SEARCH_TIMEOUT_SECONDS = 20.0
MEMORY_AUTO_EMBED_TIMEOUT_SECONDS = 3.0
MEMORY_EXPLICIT_EMBED_TIMEOUT_SECONDS = 4.0
MEMORY_EXPLICIT_RERANK_TIMEOUT_SECONDS = 4.0
MAXIMUM_RERANK_CANDIDATES = 20
MAXIMUM_RERANK_QUERY_BYTES = 8 * 1024
MAXIMUM_RERANK_DOCUMENT_BYTES = 8 * 1024
MAXIMUM_RERANK_REQUEST_BYTES = 192 * 1024
MAXIMUM_RERANK_TOKEN_FORMULA = 120_000


class KernelMemoryToolPort:
    """Resolve one model-call capability into a sealed candidate or bounded read."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        session_id: str,
        read_binding: FrozenMemoryReadScopeBinding,
        embedding_config: EmbeddingBackendConfig,
        rerank_config: RerankBackendConfig | None = None,
        feature_config: AdvisoryMemoryFeatureConfig | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        rerank_provider: RerankProvider | None = None,
        io_owner: KernelSessionIO,
        provider_trust_domain_identity: str = "",
    ) -> None:
        self._repository = repository
        self._session_id = session_id
        self._read_binding = read_binding
        self._query = PostgresMemoryQuery(repository.connection_provider)
        self._embedding_config = embedding_config
        self._rerank_config = rerank_config or RerankBackendConfig()
        self._feature_config = feature_config or AdvisoryMemoryFeatureConfig()
        self._io = io_owner
        self._deadlines = KernelExecutionDeadlineFactory()
        self._deadline_factory_bound = False
        self._embedding = embedding_provider
        self._owns_embedding = embedding_provider is None
        self._rerank = rerank_provider
        self._owns_rerank = rerank_provider is None
        self._provider_lock = asyncio.Lock()
        self._remote_tasks: set[asyncio.Task[object]] = set()
        self._recall_presentation_by_membership: dict[
            tuple[tuple[str, str], ...], tuple[str, ...]
        ] = {}
        self._write_opt_out = MemoryWriteOptOut()
        self._turn_use_opt_out = TurnMemoryUseOptOut()
        self._closed = False
        self._governor: AdvisoryMemoryGovernor | None = None
        self._provider_trust_domain_identity = provider_trust_domain_identity

    @property
    def tool_names(self) -> frozenset[str]:
        return MEMORY_TOOL_NAMES

    def bind_governor(self, governor: "AdvisoryMemoryGovernor") -> None:
        if self._governor is not None:
            raise RuntimeError("memory governor is already bound")
        self._governor = governor

    def bind_deadline_factory(
        self, deadline_factory: KernelExecutionDeadlineFactory
    ) -> None:
        """Install the Host policy once without widening the public ctor seam."""

        if self._deadline_factory_bound:
            raise RuntimeError("memory deadline factory is already bound")
        if self._remote_tasks or self._embedding is not None or self._rerank is not None:
            raise RuntimeError("memory deadline factory was bound after admission")
        self._deadlines = deadline_factory
        self._deadline_factory_bound = True

    def offer_candidate_wake(self, candidate_id: str) -> None:
        governor = self._governor
        if governor is not None:
            governor.offer_candidate_wake(candidate_id)

    def adopt_dormant_reflection(
        self, handoff: PreparedCheapHintReflectionHandoff
    ) -> str | None:
        governor = self._governor
        return None if governor is None else governor.adopt_dormant_reflection(handoff)

    def activate_reflection(self, token: str) -> None:
        governor = self._governor
        if governor is not None:
            governor.activate_reflection(token)

    def classify_automatic_trigger(
        self, text: str
    ) -> AutomaticMemoryTriggerDisposition:
        return self.classify_memory_trigger(text).automatic_recall

    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy:
        normalized = normalize_reflection_text(text)
        if self._turn_use_opt_out.excludes(normalized):
            return FrozenMemoryTriggerPolicy(
                AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE,
                MemoryUsePolicy.ALL_DISABLED_BY_USER,
            )
        memory_use = (
            MemoryUsePolicy.WRITE_DISABLED_BY_USER
            if self._write_opt_out.excludes(normalized)
            else MemoryUsePolicy.ENABLED
        )
        automatic = (
            AutomaticMemoryTriggerDisposition.SKIPPED_LOW_INFORMATION
            if len(normalized) < 8
            else AutomaticMemoryTriggerDisposition.ELIGIBLE
        )
        return FrozenMemoryTriggerPolicy(automatic, memory_use)

    def prepare_and_adopt_reflection(
        self,
        *,
        canonical: CanonicalModelInputSnapshot,
        permission: FrozenRunPermissionSnapshot,
        remember_requested: bool,
    ) -> str | None:
        if (
            not self._feature_config.cheap_hint_reflection
            or not self._provider_trust_domain_identity
        ):
            return None
        handoff = prepare_cheap_hint_reflection_handoff(
            canonical=canonical,
            permission=permission,
            workspace_id=self._read_binding.host_workspace_id,
            memory_domain_id=self._read_binding.memory_domain_id,
            workspace_scope_id=next(
                (
                    item.scope_id
                    for item in self._read_binding.readable_scopes
                    if item.kind is MemoryScopeKind.WORKSPACE
                ),
                None,
            ),
            provider_trust_domain_identity=self._provider_trust_domain_identity,
            remember_requested=remember_requested,
            write_opt_out=self._write_opt_out,
            turn_use_opt_out=self._turn_use_opt_out,
        )
        return None if handoff is None else self.adopt_dormant_reflection(handoff)

    async def embed_memory_batch(
        self, texts: Sequence[str], *, timeout_seconds: float
    ) -> Sequence[Sequence[float]] | None:
        provider = await self._embedding_provider()
        if provider is None or not texts:
            return None
        try:
            return await self._run_remote_exact(
                provider.embed_batch(tuple(texts)),
                timeout_seconds=max(0.001, timeout_seconds),
                name="memory-fact-embedding-batch",
            )
        except Exception:
            return None

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        if self._closed:
            return _json_result("TOOL_UNAVAILABLE", {"error": "memory owner is closed"})
        if invocation_context.session_id != self._session_id:
            return _json_result("SYSTEM_ERROR", {"error": "memory session mismatch"})
        policy = invocation_context.memory_context.memory_use_policy
        if (
            tool_name in MEMORY_READ_TOOL_NAMES
            and not policy.allows_reads
        ) or (
            tool_name in MEMORY_WRITE_TOOL_NAMES
            and not policy.allows_writes
        ):
            raise RuntimeError("memory tool escaped its frozen user opt-out policy")
        try:
            if tool_name == "remember":
                return await self._remember(arguments, invocation_context)
            if tool_name == "memory_search":
                return await self._search(arguments)
            if tool_name == "memory_get":
                return await self._get(arguments, explain=False)
            if tool_name == "memory_explain":
                return await self._get(arguments, explain=True)
        except (TypeError, ValueError) as exc:
            return _json_result("INVALID_ARGUMENTS", {"error": str(exc)})
        raise KeyError(tool_name)

    async def _remember(
        self,
        arguments: Mapping[str, object],
        context: KernelToolInvocationContext,
    ) -> KernelToolResult:
        scope_kind = MemoryScopeKind(str(arguments.get("scope") or "USER"))
        scope = next(
            (item for item in self._read_binding.readable_scopes if item.kind is scope_kind),
            None,
        )
        if scope is None:
            raise ValueError("requested memory scope is unavailable in this Host")
        exclusions = _string_sequence(arguments.get("do_not_apply_when"))
        basis_ids = _string_sequence(arguments.get("based_on_memory_ids"))
        citation_handles = _string_sequence(arguments.get("cited_tool_result_handles"))
        proposal = FrozenMemoryProposal(
            statement=str(arguments.get("statement") or ""),
            scope_kind=scope_kind,
            scope_id=scope.scope_id,
            kind_hint=MemoryKindHint(str(arguments.get("kind_hint") or "AUTO")),
            applies_when=(
                None
                if arguments.get("applies_when") is None
                else str(arguments["applies_when"])
            ),
            do_not_apply_when=exclusions,
            based_on_memory_ids=basis_ids,
            cited_tool_result_handles=citation_handles,
        )
        basis_refs = await self._resolve_basis(basis_ids)
        citation_refs = context.memory_context.resolve(citation_handles)
        if scope_kind is MemoryScopeKind.USER and any(
            reference.citation_visibility.value != "USER_SAFE"
            for reference in citation_refs
        ):
            raise ValueError(
                "USER memory cannot cite a workspace-bound ToolResult"
            )
        candidate_id = _stable_id(
            "memory-candidate",
            context.session_id,
            context.assistant_entry_id,
            context.tool_call_id,
        )
        candidate = prepare_memory_candidate(
            candidate_id=candidate_id,
            memory_domain_id=self._read_binding.memory_domain_id,
            origin_workspace_id=context.workspace_id,
            origin_session_id=context.session_id,
            producer_kind=MemoryProducerKind.MAIN_AGENT_REMEMBER,
            proposal=proposal,
            producer_entry_id=context.assistant_entry_id,
            producer_tool_call_id=context.tool_call_id,
            tool_result_refs=citation_refs,
            basis_refs=basis_refs,
            visible_memory=context.memory_context.visible_memory,
        )
        return _json_result(
            "SUCCESS",
            {
                "status": "proposed_for_review",
                "candidate_id": candidate_id,
                "saved_memory_id": None,
                "governance_pending": True,
                "completion_guaranteed": False,
            },
            memory_candidate=candidate,
        )

    async def _resolve_basis(
        self, fact_ids: Sequence[str]
    ) -> tuple[PreparedMemoryBasisReference, ...]:
        refs: list[PreparedMemoryBasisReference] = []
        for ordinal, fact_id in enumerate(fact_ids):
            item = await self._io.run(
                self._query.get,
                read_binding=self._read_binding,
                fact_id=fact_id,
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_POINT_READ_TIMEOUT_SECONDS
                ),
            )
            if item is None:
                raise ValueError("based_on memory is absent or outside the visible scope")
            refs.append(
                PreparedMemoryBasisReference(
                    target_fact_id=item.fact_id,
                    target_scope_kind=MemoryScopeKind(item.scope_kind),
                    target_scope_id=item.scope_id,
                    ordinal=ordinal,
                )
            )
        return tuple(refs)

    async def _search(self, arguments: Mapping[str, object]) -> KernelToolResult:
        total_deadline = self._deadlines.deadline(
            KernelWatchdogOwner.MEMORY_EXPLICIT_RECALL_TOTAL
        )
        query = str(arguments.get("query") or "").strip()
        if not query or len(query.encode("utf-8")) > 32 * 1024:
            raise ValueError("memory query must contain 1..32768 UTF-8 bytes")
        limit = int(arguments.get("limit", 5))
        if not 1 <= limit <= MAXIMUM_MEMORY_QUERY_RESULTS:
            raise ValueError("memory search limit is outside 1..50")
        requested_scope = arguments.get("scope")
        requested_kind = arguments.get("kind")
        if requested_kind is not None:
            MemoryFactKind(str(requested_kind))
        # Query lexical bounds are an admission fence.  A malformed/overbound
        # query must not open the optional embedding or rerank transports.
        query_terms = self._query.tokenize_query(query)
        embedding = None
        provider_acquisition_failed = False
        try:
            embedding = await self._embedding_provider()
        except Exception:
            # Optional remote retrieval must never become the authority for an
            # otherwise valid sparse memory search.
            provider_acquisition_failed = True
        query_embedding = None
        dense_reason = (
            "DISABLED_CONTRACT_MISMATCH"
            if not MEMORY_EMBEDDING_CONTRACT.accepts(self._embedding_config)
            else (
                "UNAVAILABLE"
                if provider_acquisition_failed
                else "NOT_CONFIGURED"
            )
        )
        if embedding is not None:
            try:
                query_embedding = await self._run_remote_exact(
                    embedding.embed(query),
                    timeout_seconds=self._remaining_for(
                        total_deadline,
                        KernelWatchdogOwner.MEMORY_EXPLICIT_QUERY_EMBEDDING,
                    ),
                    name="memory-explicit-query-embedding",
                )
                dense_reason = "AVAILABLE"
            except Exception:
                dense_reason = "UNAVAILABLE"
        result = await self._parallel_recall(
            terms=query_terms,
            limit=limit,
            requested_scope=(
                None if requested_scope is None else MemoryScopeKind(str(requested_scope))
            ),
            requested_kind=(None if requested_kind is None else str(requested_kind)),
            query_embedding=query_embedding,
            automatic=False,
            deadline_monotonic=total_deadline,
        )
        result = await self._rerank_explicit(
            query, result, total_deadline=total_deadline
        )
        try:
            relations = await self._io.run(
                self._query.active_contradictions,
                read_binding=self._read_binding,
                fact_ids=tuple(item.fact_id for item in result.facts),
                deadline_monotonic=total_deadline,
            )
            relation_enrichment = "COMPLETE"
        except Exception:
            # The ranked canonical facts are still useful advisory results.
            # A failed optional warning join cannot negate them.
            relations = ()
            relation_enrichment = "UNAVAILABLE"
        relations, ids = _bounded_memory_relations(
            tuple(item.fact_id for item in result.facts), relations
        )
        memories = [
            {
                "memory_id": item.fact_id,
                "kind": item.fact_kind,
                "scope": item.scope_kind,
                "statement": item.statement,
                "applies_when": item.applies_when,
                "do_not_apply_when": list(item.do_not_apply_when),
                "filter_match": _filter_match(item.match_tier),
                "advisory": True,
                "may_be_stale_or_incomplete": True,
            }
            for item in result.facts
        ]
        return _json_result(
            "SUCCESS",
            {
                "disposition": result.disposition.value,
                "requested_filters": {
                    "scope": requested_scope,
                    "kind": requested_kind,
                },
                "exact_result_count": sum(item.match_tier == 0 for item in result.facts),
                "relaxed_result_count": sum(
                    item.match_tier > 0 for item in result.facts
                ),
                "fallback_applied": any(item.match_tier > 0 for item in result.facts),
                "relaxed_fields": list(result.relaxed_fields),
                "attempted_stages": [
                    {
                        "ordinal": item.ordinal,
                        "scope": item.scope,
                        "kind": item.kind,
                        "new_results": item.new_results,
                    }
                    for item in result.attempted_stages
                ],
                "retrieval_channels": _retrieval_channels(result),
                "vector_cache": _vector_cache(result.dense_disposition),
                "dense_result": (
                    result.dense_disposition.value
                    if query_embedding is not None
                    else dense_reason
                ),
                "dense_match_policy": (
                    "COARSE_V1"
                    if query_embedding is not None
                    else "NOT_APPLICABLE"
                ),
                "rerank": result.rerank_disposition,
                "relation_enrichment": relation_enrichment,
                "filter_fallback": _filter_fallback(result),
                "memories": memories,
                "relation_warnings": [
                    {
                        "kind": "ACTIVE_CONTRADICTION",
                        "memory_id": relation.source_fact_id,
                        "other_memory_id": relation.target_fact_id,
                    }
                    for relation in relations
                ],
                "advisory": True,
                "may_be_stale_or_incomplete": True,
            },
            model_visible_memory_fact_ids=ids,
        )

    async def _get(
        self, arguments: Mapping[str, object], *, explain: bool
    ) -> KernelToolResult:
        memory_id = str(arguments.get("memory_id") or "")
        if not memory_id:
            raise ValueError("memory_id is required")
        item = await self._io.run(
            self._query.get,
            read_binding=self._read_binding,
            fact_id=memory_id,
            deadline_monotonic=self._canonical_deadline(
                MEMORY_POINT_READ_TIMEOUT_SECONDS
            ),
        )
        if item is None:
            return _json_result("APPLICATION_ERROR", {"error": "memory not found"})
        relations = await self._io.run(
            self._query.direct_relations,
            read_binding=self._read_binding,
            fact_id=memory_id,
            deadline_monotonic=self._canonical_deadline(
                MEMORY_POINT_READ_TIMEOUT_SECONDS
            ),
        )
        payload: dict[str, object] = {
            "memory_id": item.fact_id,
            "kind": item.fact_kind,
            "scope": item.scope_kind,
            "lifecycle": item.lifecycle,
            "statement": item.statement,
            "applies_when": item.applies_when,
            "do_not_apply_when": list(item.do_not_apply_when),
            "relations": [
                {
                    "relation_id": relation.relation_id,
                    "source_memory_id": relation.source_fact_id,
                    "target_memory_id": relation.target_fact_id,
                    "kind": relation.relation_kind,
                    "supersede_mode": relation.supersede_mode,
                }
                for relation in relations
            ],
            "advisory": True,
            "may_be_stale_or_incomplete": True,
        }
        if explain:
            provenance = await self._io.run(
                self._query.provenance,
                read_binding=self._read_binding,
                fact_id=memory_id,
                relation_ids=tuple(item.relation_id for item in relations),
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_POINT_READ_TIMEOUT_SECONDS
                ),
            )
            if provenance is None:
                return _json_result(
                    "APPLICATION_ERROR", {"error": "memory not found"}
                )
            projection: dict[str, object] = {
                "disposition": provenance.provenance_disposition,
                "producer_kind": provenance.producer_kind,
                "decision": {
                    "kind": provenance.decision_kind,
                    "reason_code": provenance.decision_reason_code,
                    "public_summary": provenance.decision_public_summary,
                },
                "relation_decisions": [
                    {
                        "relation_id": item.relation_id,
                        "disposition": item.provenance_disposition,
                        "decision_kind": item.decision_kind,
                        "reason_code": item.decision_reason_code,
                        "public_summary": item.decision_public_summary,
                    }
                    for item in provenance.relation_decisions
                ],
            }
            if provenance.provenance_disposition == "SAME_ORIGIN":
                projection["producer_locator"] = {
                    "session_id": provenance.producer_session_id,
                    "turn_id": provenance.producer_turn_id,
                    "entry_id": provenance.producer_entry_id,
                    "tool_call_id": provenance.producer_tool_call_id,
                }
                projection["tool_result_citation_ids"] = list(
                    provenance.tool_result_ids
                )
            else:
                projection["origin_context"] = "WORKSPACE_REDACTED"
            payload["provenance"] = projection
        return _json_result(
            "SUCCESS", payload, model_visible_memory_fact_ids=(item.fact_id,)
        )

    async def _embedding_provider(self) -> EmbeddingProvider | None:
        if not MEMORY_EMBEDDING_CONTRACT.accepts(self._embedding_config):
            return None
        if self._embedding is None and self._embedding_config.api_key:
            async with self._provider_lock:
                if self._embedding is None and not self._closed:
                    self._embedding = build_embedding_provider(self._embedding_config)
        return self._embedding

    async def _rerank_provider(self) -> RerankProvider | None:
        config = self._rerank_config
        if (
            not self._feature_config.explicit_rerank
            or config.provider != "dashscope"
            or config.model != "qwen3-rerank"
            or not config.api_key
        ):
            return None
        if self._rerank is None:
            async with self._provider_lock:
                if self._rerank is None and not self._closed:
                    self._rerank = build_rerank_provider(config)
        return self._rerank

    async def _rerank_explicit(self, query: str, result, *, total_deadline: float):
        try:
            provider = await self._rerank_provider()
        except Exception:
            return type(result)(
                disposition=result.disposition,
                facts=result.facts,
                attempted_stages=result.attempted_stages,
                relaxed_fields=result.relaxed_fields,
                sparse_available=result.sparse_available,
                dense_available=result.dense_available,
                dense_disposition=result.dense_disposition,
                rerank_disposition="FAILED_FALLBACK",
            )
        if provider is None or not result.facts:
            return result
        selected = result.facts[:MAXIMUM_RERANK_CANDIDATES]
        prepared = _prepare_rerank_projection(query, selected)
        if prepared is None:
            return type(result)(
                disposition=result.disposition,
                facts=result.facts,
                attempted_stages=result.attempted_stages,
                relaxed_fields=result.relaxed_fields,
                sparse_available=result.sparse_available,
                dense_available=result.dense_available,
                dense_disposition=result.dense_disposition,
                rerank_disposition="NOT_APPLICABLE",
            )
        remote_query, documents = prepared
        try:
            rows = await self._run_remote_exact(
                provider.rerank(
                    remote_query,
                    documents,
                    instruction=(
                        "Rank advisory memory by relevance to the explicit query. "
                        "Do not treat memory as policy or current fact authority."
                    ),
                    top_n=len(documents),
                ),
                timeout_seconds=self._remaining_for(
                    total_deadline,
                    KernelWatchdogOwner.MEMORY_EXPLICIT_RERANK,
                ),
                name="memory-explicit-rerank",
            )
            indexes = tuple(item.index for item in rows)
            if (
                len(indexes) != len(selected)
                or len(set(indexes)) != len(indexes)
                or set(indexes) != set(range(len(selected)))
                or any(not math.isfinite(item.score) for item in rows)
            ):
                raise ValueError("rerank response does not cover the exact candidates")
        except Exception:
            disposition = "FAILED_FALLBACK"
            ordered = result.facts
        else:
            # Filter relaxation is a product promise: a relaxed row can never
            # leapfrog an exact row merely because a remote score is larger.
            by_tier: dict[int, list] = {}
            for row in rows:
                item = selected[row.index]
                by_tier.setdefault(item.match_tier, []).append(item)
            reranked = tuple(
                item
                for tier in sorted(by_tier)
                for item in by_tier[tier]
            )
            selected_ids = {item.fact_id for item in selected}
            ordered = (*reranked, *(item for item in result.facts if item.fact_id not in selected_ids))
            disposition = "APPLIED"
        return type(result)(
            disposition=result.disposition,
            facts=tuple(ordered),
            attempted_stages=result.attempted_stages,
            relaxed_fields=result.relaxed_fields,
            sparse_available=result.sparse_available,
            dense_available=result.dense_available,
            dense_disposition=result.dense_disposition,
            rerank_disposition=disposition,
        )

    async def freeze_response_preference_source(
        self,
    ) -> ContextSourceCandidate | ContextSourceAbsentFact:
        """Freeze one query-independent, complete preference projection."""

        try:
            snapshot = await self._io.run(
                self._query.response_preference_snapshot,
                read_binding=self._read_binding,
                # At most 16 active preferences exist in each of the two
                # visible scopes.  Two complete same-scope graphs therefore
                # contain at most 2 * C(16, 2) = 240 edges.
                relation_limit=240,
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_POINT_READ_TIMEOUT_SECONDS
                ),
            )
        except Exception:
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
            )
        rows = snapshot.facts
        relations = snapshot.contradictions
        rows_by_scope: dict[tuple[str, str], list[object]] = {}
        for item in rows:
            rows_by_scope.setdefault((item.scope_kind, item.scope_id), []).append(item)
        for scoped_rows in rows_by_scope.values():
            scoped_projection = tuple(
                memory_response_preference_item_payload(
                    memory_id=item.fact_id,
                    scope_kind=item.scope_kind,
                    statement=item.statement,
                )
                for item in scoped_rows
            )
            if len(scoped_rows) > 16 or len(
                canonical_json_bytes(scoped_projection)
            ) > 7 * 1024:
                return build_memory_context_source(
                    kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                    texts=None,
                    absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
                )
        contradicted = {
            value
            for relation in relations
            for value in (relation.source_fact_id, relation.target_fact_id)
        }
        effective = tuple(item for item in rows if item.fact_id not in contradicted)
        warnings = tuple(
            {
                "kind": "ACTIVE_CONTRADICTION",
                "memory_id": relation.source_fact_id,
                "other_memory_id": relation.target_fact_id,
            }
            for relation in relations
        )
        if not effective and not warnings:
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            )
        items = tuple(
            memory_response_preference_item_payload(
                memory_id=item.fact_id,
                scope_kind=item.scope_kind,
                statement=item.statement,
            )
            for item in effective
        )
        body = canonical_json_bytes(
            {
                "advisory": True,
                "items": items,
                "may_be_stale_or_incomplete": True,
                "relation_warnings": warnings,
            }
        )
        if len(body) > 16 * 1024 or len(items) > 32 or len(rows) > 32:
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
            )
        return build_memory_context_source(
            kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
            texts=(body.decode("utf-8"),),
            memory_fact_ids=tuple(
                dict.fromkeys(
                    [
                        *(item.fact_id for item in effective),
                        *(value for relation in relations for value in (
                            relation.source_fact_id,
                            relation.target_fact_id,
                        )),
                    ]
                )
            ),
            domain_identity={
                "items": tuple(
                    sorted((item.fact_id, item.fact_semantic_digest) for item in effective)
                ),
                "warnings": tuple(
                    sorted((item.source_fact_id, item.target_fact_id) for item in relations)
                ),
            },
        )

    async def freeze_automatic_recall_source(
        self,
        query: str,
    ) -> ContextSourceCandidate | ContextSourceAbsentFact:
        normalized = " ".join(query.split())
        if self.classify_automatic_trigger(normalized) is not (
            AutomaticMemoryTriggerDisposition.ELIGIBLE
        ):
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RECALL,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
            )
        embedding = None
        if self._feature_config.automatic_dense:
            try:
                embedding = await self._embedding_provider()
            except Exception:
                # Dense recall is optional; a constructor/configuration error
                # degrades this trigger to the local sparse channel.
                embedding = None
        vector = None
        try:
            query_terms = self._query.tokenize_query(normalized)
        except ValueError:
            # Automatic recall is advisory: a lexical resource-bound input
            # only disables sparse retrieval for this trigger.
            query_terms = ()
        if embedding is not None:
            try:
                vector = await self._run_remote_exact(
                    embedding.embed(normalized),
                    timeout_seconds=self._deadlines.policy.seconds_for(
                        KernelWatchdogOwner.MEMORY_AUTO_QUERY_EMBEDDING
                    ),
                    name="memory-automatic-query-embedding",
                )
            except Exception:
                vector = None
        try:
            result = await self._parallel_recall(
                terms=query_terms,
                limit=5,
                requested_scope=None,
                requested_kind=None,
                query_embedding=vector,
                automatic=True,
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_SEARCH_TIMEOUT_SECONDS
                ),
            )
        except Exception:
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RECALL,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
            )
        filtered_facts = tuple(
            item
            for item in result.facts
            if _sensitive_profile_is_eligible(item, normalized)
        )
        if not filtered_facts:
            absence = (
                ContextSourceAbsenceKind.UNAVAILABLE
                if result.disposition.value == "UNAVAILABLE"
                else ContextSourceAbsenceKind.EXPLICIT_EMPTY
            )
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RECALL,
                texts=None,
                absence_kind=absence,
            )
        try:
            relations = await self._io.run(
                self._query.active_contradictions,
                read_binding=self._read_binding,
                fact_ids=tuple(item.fact_id for item in filtered_facts),
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_SEARCH_TIMEOUT_SECONDS
                ),
            )
        except Exception:
            # Without the same-scope contradiction join we cannot safely
            # present a seemingly complete automatic advisory projection.
            return build_memory_context_source(
                kind=ContextSourceKind.MEMORY_RECALL,
                texts=None,
                absence_kind=ContextSourceAbsenceKind.UNAVAILABLE,
            )
        def item_payload(item):
            return {
                "memory_id": item.fact_id,
                "kind": item.fact_kind,
                "scope": item.scope_kind,
                "statement": item.statement,
                "applies_when": item.applies_when,
                "do_not_apply_when": list(item.do_not_apply_when),
            }

        membership = tuple(
            sorted((item.fact_id, item.fact_semantic_digest) for item in filtered_facts)
        )
        prior_order = self._recall_presentation_by_membership.get(membership)
        by_id = {item.fact_id: item for item in filtered_facts}
        if prior_order is None:
            presentation = filtered_facts
            self._recall_presentation_by_membership[membership] = tuple(
                item.fact_id for item in presentation
            )
            if len(self._recall_presentation_by_membership) > 128:
                oldest = next(iter(self._recall_presentation_by_membership))
                self._recall_presentation_by_membership.pop(oldest, None)
        else:
            presentation = tuple(by_id[item_id] for item_id in prior_order)
        relations, exposed_ids = _bounded_memory_relations(
            tuple(item.fact_id for item in presentation), relations
        )
        warning_identity = tuple(
            sorted(
                (relation.source_fact_id, relation.target_fact_id)
                for relation in relations
            )
        )
        full_items = tuple(item_payload(item) for item in presentation)
        compact_items = full_items[: min(3, len(full_items))]
        ref_items = tuple(
            {
                "kind": item.fact_kind,
                "memory_id": item.fact_id,
                "scope": item.scope_kind,
                "read_with": "memory_get",
            }
            for item in presentation
        )
        relation_warnings = tuple(
            {
                "kind": "ACTIVE_CONTRADICTION",
                "memory_id": relation.source_fact_id,
                "other_memory_id": relation.target_fact_id,
            }
            for relation in relations
        )

        def render(items):
            return canonical_json_bytes(
                {
                    "advisory": True,
                    "items": items,
                    "may_be_stale_or_incomplete": True,
                    "relation_warnings": relation_warnings,
                }
            ).decode("utf-8")

        return build_memory_context_source(
            kind=ContextSourceKind.MEMORY_RECALL,
            texts=(render(full_items), render(compact_items), render(ref_items)),
            memory_fact_ids=exposed_ids,
            domain_identity={"membership": membership, "warnings": warning_identity},
        )

    async def aclose(self) -> None:
        self._closed = True
        tasks = tuple(self._remote_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._rerank is not None and self._owns_rerank:
            await self._rerank.aclose()
        if self._embedding is not None and self._owns_embedding:
            await self._embedding.aclose()

    async def _run_remote_exact(
        self,
        operation,
        *,
        timeout_seconds: float,
        name: str,
    ):
        if self._closed:
            operation.close()
            raise RuntimeError("memory retrieval owner is closed")
        task = asyncio.create_task(operation, name=name)
        self._remote_tasks.add(task)
        try:
            try:
                async with asyncio.timeout(timeout_seconds):
                    return await asyncio.shield(task)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        finally:
            self._remote_tasks.discard(task)

    async def _parallel_recall(
        self,
        *,
        terms: Sequence[str],
        limit: int,
        requested_scope: MemoryScopeKind | None,
        requested_kind: str | None,
        query_embedding: Sequence[float] | None,
        automatic: bool,
        deadline_monotonic: float,
    ) -> MemoryQueryResult:
        """Run sparse and dense reads independently, then canonical-refetch.

        No PostgreSQL transaction spans the two channels or any remote call.
        KernelSessionIO retains physical ownership of both channel reads.
        """

        stages = self._query.filter_stages(
            self._read_binding, requested_scope, requested_kind
        )
        gathered = []
        seen: set[str] = set()
        attempted: list[MemorySearchStageResult] = []
        relaxed: list[str] = []
        sparse_ok = True
        dense_ok = query_embedding is not None
        dense_dispositions: list[MemoryDenseCandidateDisposition] = []
        for ordinal, (scope_filter, kind_filter, label, relaxed_field) in enumerate(
            stages
        ):
            if monotonic() >= deadline_monotonic:
                break
            if relaxed_field is not None:
                relaxed.append(relaxed_field)
            sparse_operation = self._io.run(
                self._query.sparse_candidates,
                read_binding=self._read_binding,
                terms=terms,
                scope_filter=scope_filter,
                kind_filter=kind_filter,
                limit=20 if automatic else 40,
                automatic=automatic,
                deadline_monotonic=deadline_monotonic,
            )
            dense_operation = (
                None
                if query_embedding is None
                else self._io.run(
                    self._query.dense_candidates,
                    read_binding=self._read_binding,
                    vector=query_embedding,
                    scope_filter=scope_filter,
                    kind_filter=kind_filter,
                    limit=20 if automatic else 30,
                    purpose=(
                        DenseRecallPurpose.AUTOMATIC_ROOT
                        if automatic
                        else DenseRecallPurpose.EXPLICIT_SEARCH
                    ),
                    automatic=automatic,
                    deadline_monotonic=deadline_monotonic,
                )
            )
            operations = (
                (sparse_operation,)
                if dense_operation is None
                else (sparse_operation, dense_operation)
            )
            outcomes = await asyncio.gather(*operations, return_exceptions=True)
            sparse_outcome = outcomes[0]
            if isinstance(sparse_outcome, BaseException):
                sparse_ok = False
                sparse = ()
            else:
                sparse = sparse_outcome
            dense = ()
            if dense_operation is not None:
                dense_outcome = outcomes[1]
                if isinstance(dense_outcome, BaseException):
                    dense_ok = False
                    dense_dispositions.append(
                        MemoryDenseCandidateDisposition.UNAVAILABLE
                    )
                else:
                    dense = dense_outcome.facts
                    dense_dispositions.append(dense_outcome.disposition)
            prior_count = len(gathered)
            for item in self._query.fuse_candidates(sparse, dense):
                if item.fact_id in seen:
                    continue
                seen.add(item.fact_id)
                gathered.append(
                    type(item)(
                        **{
                            field: getattr(item, field)
                            for field in (
                                "fact_id",
                                "memory_domain_id",
                                "scope_kind",
                                "scope_id",
                                "fact_kind",
                                "lifecycle",
                                "statement",
                                "applies_when",
                                "do_not_apply_when",
                                "fact_semantic_digest",
                                "sparse_rank",
                                "dense_rank",
                                "fused_score",
                            )
                        },
                        match_tier=ordinal,
                    )
                )
            attempted.append(
                _memory_search_stage_result(
                    ordinal,
                    label,
                    new_results=len(gathered) - prior_count,
                )
            )
            if len(gathered) >= min(limit, 3):
                break
        final = await self._io.run(
            self._query.canonical_refetch,
            read_binding=self._read_binding,
            ranked=gathered[:limit],
            automatic=automatic,
            deadline_monotonic=deadline_monotonic,
        )
        if final:
            disposition = (
                MemoryRetrievalDisposition.COMPLETE
                if sparse_ok and (query_embedding is None or dense_ok)
                else MemoryRetrievalDisposition.PARTIAL
            )
        elif not sparse_ok and query_embedding is not None and not dense_ok:
            disposition = MemoryRetrievalDisposition.UNAVAILABLE
        else:
            disposition = MemoryRetrievalDisposition.NO_MATCH
        return MemoryQueryResult(
            disposition=disposition,
            facts=final,
            attempted_stages=tuple(attempted),
            relaxed_fields=tuple(dict.fromkeys(relaxed)),
            sparse_available=sparse_ok,
            dense_available=dense_ok,
            dense_disposition=_aggregate_dense_dispositions(
                query_embedding is not None, dense_dispositions
            ),
        )

    def _canonical_deadline(self, maximum_seconds: float) -> float:
        return min(
            monotonic() + maximum_seconds,
            self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL),
        )

    def _remaining_for(
        self, total_deadline: float, owner: KernelWatchdogOwner
    ) -> float:
        operation_deadline = min(total_deadline, self._deadlines.deadline(owner))
        remaining = operation_deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{owner.value} deadline expired")
        return remaining


def _string_sequence(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("memory reference list is invalid")
    return tuple(value)


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = "\x00".join(parts).encode("utf-8")
    return f"{prefix}:" + sha256(prefix.encode("utf-8") + b"\x00" + encoded).hexdigest()


def _filter_match(tier: int) -> str:
    return {
        0: "EXACT",
        1: "KIND_RELAXED",
        2: "SCOPE_RELAXED",
        3: "KIND_AND_SCOPE_RELAXED",
    }.get(tier, "KIND_AND_SCOPE_RELAXED")


def _memory_search_stage_result(
    ordinal: int, label: str, *, new_results: int
) -> MemorySearchStageResult:
    return MemorySearchStageResult(
        ordinal=ordinal,
        scope=(
            "REQUESTED" if label in {"EXACT", "RELAX_KIND"} else "ALL_VISIBLE"
        ),
        kind=("REQUESTED" if label in {"EXACT", "RELAX_SCOPE"} else "ANY"),
        new_results=new_results,
    )


def _aggregate_dense_dispositions(
    requested: bool,
    values: Sequence[MemoryDenseCandidateDisposition],
) -> MemoryDenseCandidateDisposition:
    if not requested:
        return MemoryDenseCandidateDisposition.NOT_REQUESTED
    successful = tuple(
        value
        for value in values
        if value is not MemoryDenseCandidateDisposition.UNAVAILABLE
    )
    if not successful:
        return MemoryDenseCandidateDisposition.UNAVAILABLE
    for disposition in (
        MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN,
        MemoryDenseCandidateDisposition.BOUNDED_TOP_K,
        MemoryDenseCandidateDisposition.EXHAUSTED_VISIBLE_SET,
        MemoryDenseCandidateDisposition.NO_ELIGIBLE_MATCH,
    ):
        if disposition in successful:
            return disposition
    return MemoryDenseCandidateDisposition.UNAVAILABLE


def _vector_cache(disposition: MemoryDenseCandidateDisposition) -> str:
    if disposition is MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN:
        return "PARTIAL"
    if disposition in {
        MemoryDenseCandidateDisposition.BOUNDED_TOP_K,
        MemoryDenseCandidateDisposition.EXHAUSTED_VISIBLE_SET,
        MemoryDenseCandidateDisposition.NO_ELIGIBLE_MATCH,
    }:
        return "AVAILABLE"
    return "NOT_AVAILABLE"


def _retrieval_channels(result: MemoryQueryResult) -> list[str]:
    channels: list[str] = []
    if result.sparse_available:
        channels.append("SPARSE_FTS")
    if _vector_cache(result.dense_disposition) != "NOT_AVAILABLE":
        channels.append("VECTOR")
    if result.rerank_disposition == "APPLIED":
        channels.append("RERANK")
    return channels


def _filter_fallback(result: MemoryQueryResult) -> str:
    values = set(result.relaxed_fields)
    if not values:
        return "NOT_NEEDED"
    if not result.facts:
        return "EXHAUSTED"
    if "scope+kind" in values or values == {"scope", "kind"}:
        return "KIND_AND_SCOPE"
    if "scope" in values:
        return "SCOPE"
    if "kind" in values:
        return "KIND"
    return "EXHAUSTED"


def _bounded_memory_relations(fact_ids, relations):
    """Keep warnings only while the closed ToolResult exposure header fits."""

    exposed = list(dict.fromkeys(str(value) for value in fact_ids))
    selected = []
    for relation in relations:
        additions = tuple(
            value
            for value in (
                relation.source_fact_id,
                relation.target_fact_id,
            )
            if value not in exposed
        )
        if len(exposed) + len(additions) > 50:
            continue
        exposed.extend(additions)
        selected.append(relation)
    return tuple(selected), tuple(exposed)


def _json_result(
    state: str,
    payload: Mapping[str, object],
    *,
    memory_candidate: PreparedMemoryCandidateAcceptance | None = None,
    model_visible_memory_fact_ids: tuple[str, ...] = (),
) -> KernelToolResult:
    ordered_payload: dict[str, object] = {}
    if model_visible_memory_fact_ids:
        # The exposure header is an independent canonical column as well as
        # the first bytes of the public JSON body.  A Round 1 HEAD_TAIL preview
        # therefore cannot hide the IDs that its body exposed to the model.
        ordered_payload["model_visible_memory_ids"] = list(
            model_visible_memory_fact_ids
        )
    for key in sorted(payload):
        ordered_payload[key] = payload[key]
    encoded = json.dumps(
        ordered_payload,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAXIMUM_MEMORY_TOOL_OUTPUT_BYTES:
        return KernelToolResult(
            state="SYSTEM_ERROR",
            content=b'{"error":"memory tool output exceeded its bound"}',
        )
    text = encoded.decode("utf-8")
    return KernelToolResult(
        state=state,
        content=encoded,
        memory_candidate=memory_candidate,
        output_artifact_candidate=ToolOutputArtifactCandidate(
            role="OUTPUT",
            text=text,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            original_utf8_bytes=len(encoded),
            # This is a closed memory payload rather than the legacy
            # ``{"output": ...}`` envelope understood by the generic JSON
            # preview renderer.  Archive exact bytes and let Round 1 perform
            # deterministic text head/tail display.
            source_format_hint=ToolOutputSourceFormatHint.TEXT,
        ),
        model_visible_memory_fact_ids=model_visible_memory_fact_ids,
    )


def _prepare_rerank_projection(query: str, facts) -> tuple[str, tuple[str, ...]] | None:
    estimator = PulsaraHeuristicTokenEstimatorV1()
    query_bytes = query.encode("utf-8")
    query_tokens = estimator.estimate_text(query)
    if (
        not query_bytes
        or len(query_bytes) > MAXIMUM_RERANK_QUERY_BYTES
        or query_tokens > 4_000
    ):
        return None
    documents: list[str] = []
    document_token_ceilings: list[int] = []
    for fact in facts:
        raw = canonical_json_bytes(
            {
                "kind": fact.fact_kind,
                "scope": fact.scope_kind,
                "statement": fact.statement,
                "applies_when": fact.applies_when,
                "do_not_apply_when": fact.do_not_apply_when,
            }
        )
        projected = _utf8_head_tail(raw, MAXIMUM_RERANK_DOCUMENT_BYTES)
        projected_text = projected.decode("utf-8")
        token_ceiling = estimator.estimate_text(projected_text)
        if token_ceiling > 4_000:
            return None
        documents.append(projected_text)
        document_token_ceilings.append(token_ceiling)
    encoded = canonical_json_bytes(
        {"model": "qwen3-rerank", "query": query, "documents": documents}
    )
    if (
        len(encoded) > MAXIMUM_RERANK_REQUEST_BYTES
        or query_tokens * len(documents) + sum(document_token_ceilings)
        > MAXIMUM_RERANK_TOKEN_FORMULA
    ):
        return None
    return query, tuple(documents)


def _utf8_head_tail(value: bytes, maximum_bytes: int) -> bytes:
    if len(value) <= maximum_bytes:
        return value
    marker = b"\n...[rerank projection omitted]...\n"
    available = maximum_bytes - len(marker)
    head = value[: available // 2]
    while head:
        try:
            head.decode("utf-8")
            break
        except UnicodeDecodeError:
            head = head[:-1]
    tail = value[-(available - len(head)) :]
    while tail:
        try:
            tail.decode("utf-8")
            break
        except UnicodeDecodeError:
            tail = tail[1:]
    return head + marker + tail


_SENSITIVE_PROFILE_RELEVANCE: tuple[
    tuple[frozenset[str], frozenset[str]], ...
] = (
    (
        frozenset(
            {
                "health",
                "medical",
                "diagnosis",
                "allerg",
                "pregnan",
                "disability",
                "健康",
                "疾病",
                "诊断",
                "过敏",
                "怀孕",
                "残疾",
            }
        ),
        frozenset(
            {
                "health",
                "medical",
                "doctor",
                "medicine",
                "allerg",
                "food",
                "meal",
                "eat",
                "diet",
                "restaurant",
                "safe",
                "safety",
                "accurate",
                "健康",
                "医疗",
                "医生",
                "药",
                "过敏",
                "食物",
                "吃",
                "饮食",
                "餐",
                "安全",
                "准确",
            }
        ),
    ),
    (
        frozenset(
            {
                "address",
                "location",
                "postcode",
                "zip code",
                "住址",
                "地址",
                "位置",
                "定位",
                "邮编",
            }
        ),
        frozenset(
            {
                "address",
                "location",
                "direction",
                "route",
                "travel",
                "weather",
                "timezone",
                "shipping",
                "delivery",
                "地址",
                "位置",
                "路线",
                "出行",
                "旅行",
                "天气",
                "时区",
                "寄送",
                "配送",
            }
        ),
    ),
    (
        frozenset(
            {
                "identity",
                "passport",
                "phone",
                "email",
                "contact",
                "身份证",
                "护照",
                "电话",
                "邮箱",
                "联系方式",
            }
        ),
        frozenset(
            {
                "identity",
                "passport",
                "phone",
                "email",
                "contact",
                "account",
                "身份证",
                "护照",
                "电话",
                "邮箱",
                "联系",
                "账号",
            }
        ),
    ),
    (
        frozenset(
            {
                "religion",
                "politic",
                "sexual",
                "race",
                "ethnic",
                "宗教",
                "政治",
                "性取向",
                "种族",
                "民族",
            }
        ),
        frozenset(
            {
                "religion",
                "politic",
                "sexual",
                "race",
                "ethnic",
                "宗教",
                "政治",
                "性取向",
                "种族",
                "民族",
            }
        ),
    ),
)


def _sensitive_profile_is_eligible(item, query: str) -> bool:
    if item.fact_kind != MemoryFactKind.USER_PROFILE.value:
        return True
    statement = item.statement.casefold()
    lowered_query = query.casefold()
    matched_sensitive_category = False
    for sensitive_markers, relevance_markers in _SENSITIVE_PROFILE_RELEVANCE:
        if not any(marker in statement for marker in sensitive_markers):
            continue
        matched_sensitive_category = True
        if any(marker in lowered_query for marker in relevance_markers):
            return True
    return not matched_sensitive_category


__all__ = [
    "KernelMemoryToolPort",
    "MEMORY_READ_TOOL_NAMES",
    "MEMORY_TOOL_NAMES",
    "MEMORY_WRITE_TOOL_NAMES",
]
