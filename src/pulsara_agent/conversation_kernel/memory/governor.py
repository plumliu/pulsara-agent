"""Host-local best-effort governance for advisory memory.

This owner deliberately has no durable attempt, lease, retry queue, event, or
recovery state.  PostgreSQL owns only proposal/fact/relation truth.  A wake can
be lost and a claimed candidate may remain PROCESSING forever.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from typing import Protocol

from pulsara_agent.conversation_kernel.auxiliary_model import AuxiliaryJsonModelPort
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory.contracts import (
    MAXIMUM_GOVERNANCE_FINAL_WIRE_BYTES,
    MAXIMUM_GOVERNANCE_INPUT_TOKENS,
    MAXIMUM_GOVERNANCE_OUTPUT_BYTES,
    MAXIMUM_GOVERNANCE_OUTPUT_TOKENS,
    MAXIMUM_GOVERNANCE_PRODUCER_TURN_BYTES,
    FrozenMemoryCandidateForGovernance,
    FrozenMemoryGovernanceDecision,
    FrozenMemoryGovernanceEvidence,
    FrozenMemoryGovernanceSourceBlock,
    FrozenMemoryGovernanceSourceCoverage,
    FrozenMemoryGovernanceSourceItem,
    FrozenMemoryProposal,
    FrozenMemoryPublicFactProjection,
    MemoryDecisionKind,
    MemoryDecisionReasonCode,
    MemoryFactKind,
    MemoryGovernanceConfirmation,
    MemoryGovernanceChronology,
    MemoryGovernanceEvidenceRole,
    MemoryGovernanceSourceBlockKind,
    MemoryKindHint,
    MemoryProducerKind,
    MemorySupersedeMode,
    MODEL_GOVERNANCE_SKIP_REASON_CODES,
    PreparedMemoryCandidateAcceptance,
    PreparedExistingSourceRelationSettlement,
    canonical_json_bytes,
    legal_memory_final_kinds,
    memory_fact_semantic_digest,
    memory_governance_source_item_payload,
    memory_governance_source_projection_bytes,
    prepare_memory_candidate,
    prepare_memory_governance_acceptance,
    memory_public_fact_payload,
    normalize_memory_text,
    validate_final_kind_shape,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.memory.recall import PostgresMemoryQuery
from pulsara_agent.conversation_kernel.memory.reflection import (
    PreparedCheapHintReflectionCandidateBatch,
    PreparedCheapHintReflectionHandoff,
    cheap_hint_handoff_identity_digest,
)
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.memory.scope import FrozenMemoryReadScopeBinding, MemoryScopeKind
from pulsara_agent.memory.product_contract import (
    MEMORY_GOVERNANCE_CONTRACT_ID,
    MEMORY_GOVERNANCE_SYSTEM_PROMPT_V2,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose


MAXIMUM_HINT_REVIEW_INPUT_BYTES = 64 * 1024
MAXIMUM_HINT_REVIEW_OUTPUT_BYTES = 8 * 1024
MAXIMUM_RELATED_MEMORIES = 8
MAXIMUM_REFLECTION_QUEUE = 16
MAXIMUM_EMBEDDING_SCAN = 100
MAXIMUM_EMBEDDING_CALLS = 5
MAXIMUM_EMBEDDING_BATCH = 10


class MemoryEmbeddingMaintenancePort(Protocol):
    async def embed_memory_batch(
        self, texts: Sequence[str], *, timeout_seconds: float
    ) -> Sequence[Sequence[float]] | None: ...


@dataclass(frozen=True, slots=True)
class _ReflectionAttempt:
    token: str
    handoff: PreparedCheapHintReflectionHandoff


@dataclass(frozen=True, slots=True)
class _GovernancePacketVariant:
    packet: str
    allowed_targets: Mapping[str, FrozenMemoryPublicFactProjection]


class AdvisoryMemoryGovernor:
    """The only process-local owner of governance/reflection provider calls."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        guard: HostWriterGuard,
        read_binding: FrozenMemoryReadScopeBinding,
        model: AuxiliaryJsonModelPort,
        input_reader: CanonicalProviderInputReader,
        io_owner: KernelSessionIO,
        deadline_factory: KernelExecutionDeadlineFactory,
        provider_trust_domain_identity: str,
        embedding_port: MemoryEmbeddingMaintenancePort | None = None,
        hint_review_allow_cross_provider: bool = False,
    ) -> None:
        if not provider_trust_domain_identity:
            raise ValueError("memory governor trust-domain identity is required")
        self._repository = repository
        self._guard = guard
        self._read_binding = read_binding
        self._query = PostgresMemoryQuery(repository.connection_provider)
        self._model = model
        self._input_reader = input_reader
        self._io = io_owner
        self._deadlines = deadline_factory
        self._trust_domain = provider_trust_domain_identity
        self._embedding_port = embedding_port
        self._allow_cross_provider = hint_review_allow_cross_provider
        self._wake = asyncio.Event()
        self._auxiliary_lane = asyncio.Lock()
        self._dormant: dict[str, PreparedCheapHintReflectionHandoff] = {}
        self._reflections: deque[_ReflectionAttempt] = deque()
        self._closing = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("memory governor is already started")
        self._task = asyncio.create_task(
            self._run(), name=f"advisory-memory-governor:{self._guard.session_id}"
        )
        # Host-open bounded scan.  This is only a lossy wake, not recovery.
        self._wake.set()

    def offer_governance_wake(self) -> None:
        if not self._closing:
            self._wake.set()

    def adopt_dormant_reflection(
        self, handoff: PreparedCheapHintReflectionHandoff
    ) -> str | None:
        if self._closing or handoff.session_id != self._guard.session_id:
            return None
        if (
            handoff.provider_trust_domain_identity != self._trust_domain
            and not self._allow_cross_provider
        ):
            return None
        token = "memory-reflection:" + sha256(
            cheap_hint_handoff_identity_digest(handoff).encode("utf-8")
        ).hexdigest()
        if len(self._dormant) + len(self._reflections) >= MAXIMUM_REFLECTION_QUEUE:
            return None
        existing = self._dormant.get(token)
        if existing is not None and existing != handoff:
            raise RuntimeError("reflection token names a different handoff")
        self._dormant[token] = handoff
        return token

    def activate_reflection(self, token: str) -> None:
        handoff = self._dormant.pop(token, None)
        if handoff is None or self._closing:
            return
        self._reflections.append(_ReflectionAttempt(token, handoff))
        self._wake.set()

    async def aclose(self, *, deadline_monotonic: float) -> None:
        self._closing = True
        self._dormant.clear()
        self._reflections.clear()
        self._wake.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        expired = False
        remaining = max(0.0, deadline_monotonic - monotonic())
        if remaining:
            done, _ = await asyncio.wait((task,), timeout=remaining)
            expired = not done
        else:
            expired = not task.done()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        await asyncio.gather(task, return_exceptions=True)
        if expired:
            raise TimeoutError("memory governor exited after Host close deadline")

    async def _run(self) -> None:
        try:
            while not self._closing:
                await self._wake.wait()
                self._wake.clear()
                await self._drain_governance()
                await self._drain_reflections()
                await self._maintain_embeddings()
        except asyncio.CancelledError:
            raise

    async def _drain_governance(self) -> None:
        while not self._closing:
            deadline = self._deadlines.deadline(
                KernelWatchdogOwner.MEMORY_GOVERNANCE_ATTEMPT
            )
            try:
                candidate = await self._io.run(
                    self._repository.claim_memory_candidate_for_governance,
                    self._guard,
                    processing_started_at=datetime.now(timezone.utc),
                    deadline_monotonic=deadline,
                )
            except Exception:
                return
            if candidate is None:
                return
            try:
                await self._govern(candidate, deadline_monotonic=deadline)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Weak completion: even abandonment is best effort.
                try:
                    await self._io.run(
                        self._repository.abandon_memory_candidate,
                        self._guard,
                        candidate_id=candidate.prepared.candidate_id,
                        reason_code=(
                            MemoryDecisionReasonCode.ABANDONED_GOVERNANCE_FAILURE.value
                        ),
                        public_summary=None,
                        decided_at=datetime.now(timezone.utc),
                        deadline_monotonic=deadline,
                    )
                except Exception:
                    pass

    async def _govern(
        self,
        candidate: FrozenMemoryCandidateForGovernance,
        *,
        deadline_monotonic: float,
    ) -> None:
        prepared_candidate = candidate.prepared
        evidence: FrozenMemoryGovernanceEvidence | None = None
        allowed_targets: Mapping[str, FrozenMemoryPublicFactProjection] = {}
        if prepared_candidate.visible_memory.disposition.value == "OVERFLOW":
            decision = FrozenMemoryGovernanceDecision(
                MemoryDecisionKind.SKIP,
                reason_code=(
                    MemoryDecisionReasonCode.MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW.value
                ),
            )
        else:
            evidence = await self._io.run(
                self._repository.read_memory_governance_evidence,
                self._guard,
                candidate=candidate,
                deadline_monotonic=deadline_monotonic,
            )
            if not evidence.model_visible_complete:
                decision = FrozenMemoryGovernanceDecision(
                    MemoryDecisionKind.SKIP,
                    reason_code=(
                        MemoryDecisionReasonCode.MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW.value
                    ),
                )
                acceptance = prepare_memory_governance_acceptance(
                    candidate=prepared_candidate,
                    decision=decision,
                )
                await self._settle_acceptance(
                    acceptance, deadline_monotonic=deadline_monotonic
                )
                return
            historical = (
                None
                if evidence.producer_cut is None
                else await self._io.run(
                    self._input_reader.read_memory_governance_historical_snapshot,
                    evidence.producer_cut,
                    deadline_monotonic=deadline_monotonic,
                )
            )
            evidence = _finalize_governance_source_envelope(
                evidence,
                historical=historical,
            )
            if not (
                evidence.source_coverage.origin_turn_human_source_complete
                and evidence.source_coverage.post_proposal_human_source_complete
            ):
                decision = FrozenMemoryGovernanceDecision(
                    MemoryDecisionKind.SKIP,
                    reason_code=(
                        MemoryDecisionReasonCode.INSUFFICIENT_SOURCE_SUPPORT.value
                    ),
                )
                acceptance = prepare_memory_governance_acceptance(
                    candidate=prepared_candidate,
                    decision=decision,
                    basis_items=evidence.basis_items,
                )
                await self._settle_acceptance(
                    acceptance, deadline_monotonic=deadline_monotonic
                )
                return
            packet_variants = await self._governance_packet(
                prepared_candidate,
                evidence=evidence,
                deadline_monotonic=deadline_monotonic,
            )
            async with self._auxiliary_lane:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    return
                policy = self._deadlines.policy.bounded_auxiliary_transport(remaining)
                selected = self._model.prepare_first_fitting_json_call(
                    purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
                    message_variants=tuple(
                        (
                            LLMMessage.system(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V2),
                            LLMMessage.user(variant.packet),
                        )
                        for variant in packet_variants
                    ),
                    maximum_input_tokens=MAXIMUM_GOVERNANCE_INPUT_TOKENS,
                    maximum_input_bytes=MAXIMUM_GOVERNANCE_FINAL_WIRE_BYTES,
                    maximum_output_tokens=MAXIMUM_GOVERNANCE_OUTPUT_TOKENS,
                    timeout_policy=policy,
                    maximum_result_bytes=MAXIMUM_GOVERNANCE_OUTPUT_BYTES,
                )
                if selected is None:
                    decision = FrozenMemoryGovernanceDecision(
                        MemoryDecisionKind.SKIP,
                        reason_code=(
                            MemoryDecisionReasonCode.INSUFFICIENT_SOURCE_SUPPORT.value
                        ),
                    )
                else:
                    call, selected_ordinal = selected
                    selected_variant = packet_variants[selected_ordinal]
                    allowed_targets = selected_variant.allowed_targets
                    fence_current = await self._io.run(
                        self._repository.confirm_memory_governance_terminal_fence,
                        self._guard,
                        candidate=candidate,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if not fence_current:
                        raise ConversationKernelConflict(
                            "memory governance terminal fence changed before provider open"
                        )
                    provider_remaining = deadline_monotonic - monotonic()
                    if provider_remaining <= 0:
                        return
                    async with asyncio.timeout(provider_remaining):
                        output = await self._model.complete_prepared_json(call)
                    decision = _parse_governance_decision(
                        output,
                        allowed_targets,
                        legal_final_kinds=legal_memory_final_kinds(
                            prepared_candidate.proposal
                        ),
                    )
        acceptance = prepare_memory_governance_acceptance(
            candidate=prepared_candidate,
            decision=decision,
            basis_items=() if evidence is None else evidence.basis_items,
            relation_targets=_selected_governance_relation_targets(
                decision, allowed_targets
            ),
        )
        await self._settle_acceptance(
            acceptance, deadline_monotonic=deadline_monotonic
        )

    async def _governance_packet(
        self,
        candidate: PreparedMemoryCandidateAcceptance,
        *,
        evidence,
        deadline_monotonic: float,
    ) -> tuple[_GovernancePacketVariant, ...]:
        proposal = candidate.proposal
        existing: list[dict[str, object]] = []
        exact_kinds = (
            ()
            if proposal.kind_hint is MemoryKindHint.AUTO
            else (MemoryFactKind(proposal.kind_hint.value),)
        )
        for kind in exact_kinds:
            try:
                validate_final_kind_shape(proposal, kind)
            except ValueError:
                continue
            semantic = memory_fact_semantic_digest(
                kind=kind,
                statement=proposal.statement,
                applies_when=proposal.applies_when,
                do_not_apply_when=proposal.do_not_apply_when,
            )
            winner = await self._io.run(
                self._query.find_active_semantic,
                read_binding=self._read_binding,
                scope_kind=proposal.scope_kind,
                scope_id=proposal.scope_id,
                fact_semantic_digest=semantic,
                deadline_monotonic=deadline_monotonic,
            )
            if winner is not None:
                existing.append(_fact_projection(winner))
        related = ()
        if evidence.source_coverage.relation_authority:
            query_embedding = None
            if self._embedding_port is not None:
                remaining = deadline_monotonic - monotonic()
                if remaining > 0:
                    try:
                        vectors = await self._embedding_port.embed_memory_batch(
                            (proposal.statement,), timeout_seconds=remaining
                        )
                    except Exception:
                        vectors = None
                    if vectors is not None and len(vectors) == 1:
                        query_embedding = vectors[0]
            sparse_operation = self._io.run(
                self._query.governance_sparse_candidates,
                read_binding=self._read_binding,
                scope_kind=proposal.scope_kind,
                query=proposal.statement,
                deadline_monotonic=deadline_monotonic,
            )
            dense_operation = (
                None
                if query_embedding is None
                else self._io.run(
                    self._query.governance_dense_candidates,
                    read_binding=self._read_binding,
                    scope_kind=proposal.scope_kind,
                    query_embedding=query_embedding,
                    deadline_monotonic=deadline_monotonic,
                )
            )
            operations = (
                (sparse_operation,)
                if dense_operation is None
                else (sparse_operation, dense_operation)
            )
            channel_outcomes = await asyncio.gather(
                *operations, return_exceptions=True
            )
            sparse = (
                ()
                if isinstance(channel_outcomes[0], BaseException)
                else channel_outcomes[0]
            )
            dense = (
                ()
                if dense_operation is None
                or isinstance(channel_outcomes[1], BaseException)
                else channel_outcomes[1].facts
            )
            related = await self._io.run(
                self._query.finalize_governance_related,
                read_binding=self._read_binding,
                scope_kind=proposal.scope_kind,
                scope_id=proposal.scope_id,
                sparse=sparse,
                dense=dense,
                exclude_fact_id=None,
                limit=MAXIMUM_RELATED_MEMORIES,
                deadline_monotonic=deadline_monotonic,
            )
        targets = {
            item.fact_id: _frozen_fact_projection(item)
            for item in related
        }
        source_items = (
            *evidence.producer_call_context,
            *evidence.producer_public_output,
            *evidence.post_proposal_turn_suffix,
        )
        citation_projection = [
            {
                "source": f"cited-observation:{item.ordinal + 1}",
                "source_product_label": (
                    "候选明确引用的主要观察"
                    if item.evidence_kind.value == "PRIMARY_OBSERVATION"
                    else "候选明确引用的记忆读取暴露"
                ),
                "evidence_role": item.evidence_kind.value,
                "result_state": item.result_state,
                "observed_at": item.observed_at_iso,
                "observation_duration_microseconds": (
                    item.observation_duration_microseconds
                ),
                "tool_reported_duration_microseconds": (
                    item.tool_reported_duration_microseconds
                ),
                "body": item.body,
                "truncated": item.truncated,
            }
            for item in evidence.tool_result_evidence
        ]
        legal_kinds = tuple(
            item.value for item in legal_memory_final_kinds(proposal)
        )
        value = {
            "contract": MEMORY_GOVERNANCE_CONTRACT_ID,
            "terminal_source_fence": {
                "status": evidence.terminal_fence.terminal_status,
                "outcome": _terminal_outcome_product_label(
                    evidence.terminal_fence.terminal_status
                ),
            },
            "producer": (
                "The main model proposed this while replying."
                if candidate.producer_kind
                is MemoryProducerKind.MAIN_AGENT_REMEMBER
                else "Terminal-turn lightweight hint review proposed this from the user's words."
            ),
            "candidate": {
                "statement": proposal.statement,
                "scope_kind": proposal.scope_kind.value,
                "kind_hint": proposal.kind_hint.value,
                "applies_when": proposal.applies_when,
                "do_not_apply_when": proposal.do_not_apply_when,
                "basis_memory_ids": tuple(
                    item.target_fact_id for item in candidate.basis_refs
                ),
                "visible_memory_disposition": candidate.visible_memory.disposition.value,
                "legal_final_kinds": legal_kinds,
            },
            "source_coverage": {
                "causal_context_complete": (
                    evidence.source_coverage.causal_context_complete
                ),
                "origin_turn_human_source_complete": (
                    evidence.source_coverage.origin_turn_human_source_complete
                ),
                "post_proposal_human_source_complete": (
                    evidence.source_coverage.post_proposal_human_source_complete
                ),
                "omitted_causal_items": (
                    evidence.source_coverage.omitted_causal_items
                ),
                "omitted_assistant_or_tool_items": (
                    evidence.source_coverage.omitted_assistant_or_tool_items
                ),
                "omitted_post_proposal_human_items": (
                    evidence.source_coverage.omitted_post_proposal_human_items
                ),
                "relation_authority": evidence.source_coverage.relation_authority,
            },
            "based_on_items": [
                memory_public_fact_payload(item) for item in evidence.basis_items
            ],
            "all_model_visible_memory": [
                memory_public_fact_payload(item)
                for item in evidence.model_visible_items
            ],
            "exact_existing_sources": existing,
        }
        return _governance_packet_variants(
            base=value,
            source_items=source_items,
            citation_items=tuple(citation_projection),
            related_items=tuple(_fact_projection(item) for item in related),
            targets=targets,
            legal_final_kinds=legal_kinds,
        )

    async def _settle_acceptance(
        self, prepared, *, deadline_monotonic: float
    ) -> None:
        outcome = None
        try:
            outcome = await self._io.run(
                self._repository.accept_memory_governance,
                self._guard,
                prepared=prepared,
                decided_at=datetime.now(timezone.utc),
                deadline_monotonic=deadline_monotonic,
            )
        except ConversationKernelConflict:
            raise
        except Exception:
            confirmation = await self._io.run(
                self._repository.confirm_memory_governance_winner,
                prepared=prepared,
                deadline_monotonic=deadline_monotonic,
            )
            if confirmation is MemoryGovernanceConfirmation.FULL:
                return
            if confirmation is MemoryGovernanceConfirmation.CONFLICT:
                raise ConversationKernelConflict(
                    "memory governance names a different canonical winner"
                )
            # NONE is safe to retry with the same frozen semantic candidate.
            outcome = await self._io.run(
                self._repository.accept_memory_governance,
                self._guard,
                prepared=prepared,
                decided_at=datetime.now(timezone.utc),
                deadline_monotonic=deadline_monotonic,
            )
        for _ in range(3):
            if not isinstance(outcome, PreparedExistingSourceRelationSettlement):
                return
            settlement = outcome
            try:
                outcome = await self._io.run(
                    self._repository.settle_existing_source_memory_relation,
                    self._guard,
                    prepared=prepared,
                    settlement=settlement,
                    decided_at=datetime.now(timezone.utc),
                    deadline_monotonic=deadline_monotonic,
                )
            except ConversationKernelConflict:
                raise
            except Exception:
                confirmation = await self._io.run(
                    self._repository.confirm_memory_governance_winner,
                    prepared=prepared,
                    existing_settlement=settlement,
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmation is MemoryGovernanceConfirmation.FULL:
                    return
                if confirmation is MemoryGovernanceConfirmation.CONFLICT:
                    raise ConversationKernelConflict(
                        "memory relation settlement names a different winner"
                    )
                outcome = await self._io.run(
                    self._repository.settle_existing_source_memory_relation,
                    self._guard,
                    prepared=prepared,
                    settlement=settlement,
                    decided_at=datetime.now(timezone.utc),
                    deadline_monotonic=deadline_monotonic,
                )
        if isinstance(outcome, PreparedExistingSourceRelationSettlement):
            raise ConversationKernelConflict(
                "memory relation settlement did not reach a stable disposition"
            )

    async def _drain_reflections(self) -> None:
        while self._reflections and not self._closing:
            attempt = self._reflections.popleft()
            deadline = self._deadlines.deadline(
                KernelWatchdogOwner.MEMORY_HINT_REVIEW_ATTEMPT
            )
            try:
                await self._review_hints(attempt.handoff, deadline_monotonic=deadline)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Reflection is intentionally weaker than governance.
                continue

    async def _review_hints(
        self,
        handoff: PreparedCheapHintReflectionHandoff,
        *,
        deadline_monotonic: float,
    ) -> None:
        value = {
            "contract": "pulsara.cheap-memory-hint-review.v1",
            "instruction": (
                "Return zero to four single-atom advisory memory proposals. Copy "
                "the exact normalized statement from a cited human entry; do not "
                "infer from memory, rewrite, merge, or split one proposal."
            ),
            "entries": [
                {
                    "source": f"user:{ordinal}",
                    "human_text": item.public_text,
                    "adjacent_assistant_text": item.adjacent_assistant_text,
                    "hint_codes": tuple(hint.signal_code for hint in item.hints),
                }
                for ordinal, item in enumerate(handoff.eligible_entries, start=1)
            ],
            "final_assistant_text": handoff.final_assistant_text,
            "output": {"candidates": []},
        }
        prompt = canonical_json_bytes(value)
        if len(prompt) > MAXIMUM_HINT_REVIEW_INPUT_BYTES:
            return
        async with self._auxiliary_lane:
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                return
            policy = self._deadlines.policy.bounded_auxiliary_transport(remaining)
            call = self._model.prepare_json_call(
                purpose=ModelCallPurpose.MEMORY_HINT_REVIEW,
                messages=(LLMMessage.user(prompt.decode("utf-8")),),
                maximum_input_tokens=16_384,
                maximum_input_bytes=MAXIMUM_HINT_REVIEW_INPUT_BYTES,
                maximum_output_tokens=2_048,
                timeout_policy=policy,
                maximum_result_bytes=MAXIMUM_HINT_REVIEW_OUTPUT_BYTES,
            )
            output = await self._model.complete_prepared_json(call)
        batch = _prepare_reflection_batch(handoff, output)
        if not batch.candidates:
            return
        try:
            await self._io.run(
                self._repository.accept_reflection_memory_candidates,
                self._guard,
                candidates=batch.candidates,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception:
            confirmations = await asyncio.gather(
                *(
                    self._io.run(
                        self._repository.confirm_memory_candidate_intake,
                        candidate=candidate,
                        deadline_monotonic=deadline_monotonic,
                    )
                    for candidate in batch.candidates
                )
            )
            if not all(confirmations):
                return
        self._wake.set()

    async def _maintain_embeddings(self) -> None:
        if self._embedding_port is None or self._closing:
            return
        deadline = self._deadlines.deadline(
            KernelWatchdogOwner.MEMORY_FACT_EMBEDDING_BATCH
        )
        try:
            rows = await self._io.run(
                self._repository.list_unembedded_memory_facts,
                read_binding=self._read_binding,
                limit=MAXIMUM_EMBEDDING_SCAN,
                deadline_monotonic=deadline,
            )
        except Exception:
            return
        for offset in range(0, min(len(rows), 50), MAXIMUM_EMBEDDING_BATCH):
            if offset // MAXIMUM_EMBEDDING_BATCH >= MAXIMUM_EMBEDDING_CALLS:
                break
            batch = rows[offset : offset + MAXIMUM_EMBEDDING_BATCH]
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            vectors = await self._embedding_port.embed_memory_batch(
                tuple(item[2] for item in batch), timeout_seconds=remaining
            )
            if vectors is None or len(vectors) != len(batch):
                return
            for (fact_id, semantic_digest, _body), vector in zip(
                batch, vectors, strict=True
            ):
                try:
                    await self._io.run(
                        self._repository.upsert_memory_embedding,
                        read_binding=self._read_binding,
                        fact_id=fact_id,
                        fact_semantic_digest=semantic_digest,
                        vector=vector,
                        embedded_at=datetime.now(timezone.utc),
                        deadline_monotonic=deadline,
                    )
                except Exception:
                    continue


def _finalize_governance_source_envelope(
    evidence: FrozenMemoryGovernanceEvidence,
    *,
    historical: CanonicalModelInputSnapshot | None,
) -> FrozenMemoryGovernanceEvidence:
    """Keep every terminal-turn human source, then add a bounded causal tail."""

    raw_causal = tuple(
        item
        for source in (() if historical is None else historical.items)
        if (item := _causal_source_item(source)) is not None
    )
    causal = tuple(
        replace(item, anchor=True)
        if item.evidence_role is MemoryGovernanceEvidenceRole.HUMAN_ASSERTION
        and item.source_entry_id is not None
        and _causal_source_item_turn(historical, item.source_entry_id)
        == evidence.terminal_fence.source_turn_id
        else item
        for item in raw_causal
    )
    repository_items = (
        *evidence.producer_public_output,
        *evidence.post_proposal_turn_suffix,
    )
    required_repository = tuple(
        item
        for item in repository_items
        if item.evidence_role
        in {
            MemoryGovernanceEvidenceRole.HUMAN_ASSERTION,
            MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN,
        }
    )
    current_turn_human = tuple(
        item
        for item in causal
        if item.source_entry_id is not None
        and _causal_source_item_turn(historical, item.source_entry_id)
        == evidence.terminal_fence.source_turn_id
        and item.evidence_role is MemoryGovernanceEvidenceRole.HUMAN_ASSERTION
    )
    selected: set[int] = {id(item) for item in required_repository}
    retained: list[FrozenMemoryGovernanceSourceItem] = list(required_repository)
    omitted_causal = 0
    origin_human_complete = (
        evidence.source_coverage.origin_turn_human_source_complete
    )
    for item in current_turn_human:
        if id(item) in selected:
            continue
        if memory_governance_source_projection_bytes((*retained, item)) > (
            MAXIMUM_GOVERNANCE_PRODUCER_TURN_BYTES
        ):
            omitted_causal += 1
            origin_human_complete = False
            continue
        selected.add(id(item))
        retained.append(item)

    optional_repository = tuple(
        item for item in repository_items if id(item) not in selected
    )
    for item in optional_repository:
        if memory_governance_source_projection_bytes((*retained, item)) > (
            MAXIMUM_GOVERNANCE_PRODUCER_TURN_BYTES
        ):
            continue
        selected.add(id(item))
        retained.append(item)
    for item in reversed(causal):
        if id(item) in selected:
            continue
        if memory_governance_source_projection_bytes((*retained, item)) > (
            MAXIMUM_GOVERNANCE_PRODUCER_TURN_BYTES
        ):
            omitted_causal += 1
            continue
        selected.add(id(item))
        retained.append(item)

    producer_output = tuple(
        item for item in evidence.producer_public_output if id(item) in selected
    )
    suffix = tuple(
        item for item in evidence.post_proposal_turn_suffix if id(item) in selected
    )
    causal_output = tuple(item for item in causal if id(item) in selected)
    omitted_repository = sum(
        1 for item in repository_items if id(item) not in selected
    )
    retained_nonhuman_detail_gaps = sum(
        item.item_omitted_before
        + item.item_omitted_after
        + int(item.truncated)
        for item in (*producer_output, *suffix)
        if item.evidence_role
        not in {
            MemoryGovernanceEvidenceRole.HUMAN_ASSERTION,
            MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN,
        }
    )
    retained_source_complete = all(
        not item.truncated
        and item.item_omitted_before == 0
        and item.item_omitted_after == 0
        for item in (*causal_output, *producer_output, *suffix)
    )
    causal_complete = omitted_causal == 0 and len(causal_output) == len(causal)
    post_complete = (
        evidence.source_coverage.post_proposal_human_source_complete
    )
    omitted_assistant_or_tool = (
        evidence.source_coverage.omitted_assistant_or_tool_items
        + omitted_repository
        + retained_nonhuman_detail_gaps
    )
    coverage = FrozenMemoryGovernanceSourceCoverage(
        causal_context_complete=causal_complete,
        origin_turn_human_source_complete=origin_human_complete,
        post_proposal_human_source_complete=post_complete,
        omitted_causal_items=(
            evidence.source_coverage.omitted_causal_items + omitted_causal
        ),
        omitted_assistant_or_tool_items=omitted_assistant_or_tool,
        omitted_post_proposal_human_items=(
            evidence.source_coverage.omitted_post_proposal_human_items
        ),
        relation_authority=(
            causal_complete
            and origin_human_complete
            and post_complete
            and evidence.model_visible_complete
            and omitted_assistant_or_tool == 0
            and retained_source_complete
            and all(not item.truncated for item in evidence.tool_result_evidence)
        ),
    )
    return replace(
        evidence,
        producer_call_context=causal_output,
        producer_public_output=producer_output,
        post_proposal_turn_suffix=suffix,
        source_coverage=coverage,
    )


def _causal_source_item(
    item: FrozenProviderInputItem,
) -> FrozenMemoryGovernanceSourceItem | None:
    if not item.text:
        return None
    role = MemoryGovernanceEvidenceRole.NON_HUMAN_CONTEXT
    label = "主模型当时看到的非用户上下文"
    if item.item_kind is FrozenProviderInputItemKind.USER:
        if item.input_origin in {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.HUMAN_STEER,
        }:
            role = MemoryGovernanceEvidenceRole.HUMAN_ASSERTION
            label = "主模型当时看到的用户原话"
        else:
            label = "主模型当时看到的运行时输入"
    elif item.item_kind in {
        FrozenProviderInputItemKind.ASSISTANT,
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
    }:
        role = MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT
        label = "主模型当时看到的助手上下文"
    elif item.item_kind in {
        FrozenProviderInputItemKind.TOOL_RESULT,
        FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
        FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
    }:
        role = MemoryGovernanceEvidenceRole.TOOL_CONTEXT_ONLY
        label = "主模型当时看到的普通工具上下文"
    return FrozenMemoryGovernanceSourceItem(
        source_entry_id=item.source_entry_id,
        chronology=MemoryGovernanceChronology.BEFORE_PROPOSAL,
        source_product_label=label,
        evidence_role=role,
        public_kind=_provider_item_product_kind(item),
        blocks=(
            FrozenMemoryGovernanceSourceBlock(
                block_kind=MemoryGovernanceSourceBlockKind.TEXT,
                text=item.text,
            ),
        ),
    )


def _causal_source_item_turn(
    snapshot: CanonicalModelInputSnapshot | None,
    source_entry_id: str,
) -> str | None:
    if snapshot is None:
        return None
    for item in snapshot.items:
        if item.source_entry_id == source_entry_id:
            return item.source_turn_id
    return None


def _provider_item_product_kind(item: FrozenProviderInputItem) -> str:
    if item.input_origin is not None:
        return {
            CanonicalInputOriginKind.HUMAN_MESSAGE: "用户消息",
            CanonicalInputOriginKind.HUMAN_STEER: "用户补充",
            CanonicalInputOriginKind.SUBAGENT_OBJECTIVE: "子任务目标",
            CanonicalInputOriginKind.PLAN_CONTINUATION: "计划运行时续接",
            CanonicalInputOriginKind.INTER_AGENT_MESSAGE: "代理协作消息",
        }[item.input_origin]
    return {
        FrozenProviderInputItemKind.CONTEXT_SNAPSHOT: "已采用的上下文摘要",
        FrozenProviderInputItemKind.USER: "用户形态输入",
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION: "终止观察",
        FrozenProviderInputItemKind.ASSISTANT: "助手回复",
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST: "助手工具请求回复",
        FrozenProviderInputItemKind.TOOL_RESULT: "工具结果",
        FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE: "工具结果闭合说明",
        FrozenProviderInputItemKind.LATE_TOOL_OUTCOME: "延迟工具结果",
        FrozenProviderInputItemKind.PLAN_CONTINUATION: "计划运行时续接",
        FrozenProviderInputItemKind.INTER_AGENT_MESSAGE: "代理协作消息",
    }[item.item_kind]


def _governance_packet_variants(
    *,
    base: Mapping[str, object],
    source_items: tuple[FrozenMemoryGovernanceSourceItem, ...],
    citation_items: tuple[Mapping[str, object], ...],
    related_items: tuple[Mapping[str, object], ...],
    targets: Mapping[str, FrozenMemoryPublicFactProjection],
    legal_final_kinds: Sequence[str],
) -> tuple[_GovernancePacketVariant, ...]:
    """Enumerate every allowed shedding boundary in its product order."""

    working_sources = list(source_items)
    working_citations = [dict(item) for item in citation_items]
    working_related = list(related_items)
    working_targets: Mapping[str, FrozenMemoryPublicFactProjection] = targets
    original_coverage = base["source_coverage"]
    if not isinstance(original_coverage, Mapping):
        raise TypeError("governance source coverage is not a mapping")
    coverage = dict(original_coverage)
    variants: list[_GovernancePacketVariant] = []
    seen: set[str] = set()

    def append_variant() -> None:
        value = dict(base)
        value["source_coverage"] = dict(coverage)
        value["ordered_source_items"] = tuple(
            memory_governance_source_item_payload(item, ordinal=ordinal)
            for ordinal, item in enumerate(working_sources, start=1)
        )
        value["cited_tool_evidence"] = tuple(
            dict(item) for item in working_citations
        )
        value["allowed_relation_targets"] = tuple(working_related)
        value["output_schema"] = _governance_output_schema(
            legal_final_kinds=legal_final_kinds,
            allowed_target_ids=tuple(working_targets),
        )
        packet = canonical_json_bytes(value).decode("utf-8")
        if packet in seen:
            return
        seen.add(packet)
        variants.append(
            _GovernancePacketVariant(
                packet=packet,
                allowed_targets=working_targets,
            )
        )

    append_variant()

    # Relations are the first optional material and their authority travels
    # with their exact allowlist.
    if working_related:
        working_related.clear()
        working_targets = {}
        coverage["relation_authority"] = False
        append_variant()

    # The source projection is already in chronology order. Remove each oldest
    # non-anchor at a real item boundary; current-turn and post-proposal human
    # anchors are never candidates for shedding.
    for item in tuple(source_items):
        if item.anchor:
            continue
        working_sources = [
            retained for retained in working_sources if retained is not item
        ]
        if item.chronology is MemoryGovernanceChronology.BEFORE_PROPOSAL:
            coverage["omitted_causal_items"] = (
                int(coverage["omitted_causal_items"]) + 1
            )
            coverage["causal_context_complete"] = False
        else:
            coverage["omitted_assistant_or_tool_items"] = (
                int(coverage["omitted_assistant_or_tool_items"]) + 1
            )
        working_related.clear()
        working_targets = {}
        coverage["relation_authority"] = False
        append_variant()

    # Cited observations retain identity, kind, state, timing and an honest
    # truncation marker when their optional body is shed.
    citation_order = sorted(
        range(len(working_citations)),
        key=lambda index: (
            -len(str(working_citations[index].get("body", "")).encode("utf-8")),
            index,
        ),
    )
    for index in citation_order:
        item = working_citations[index]
        if not item.get("body"):
            continue
        item["body"] = ""
        item["truncated"] = True
        working_related.clear()
        working_targets = {}
        coverage["relation_authority"] = False
        append_variant()

    if not variants:
        raise RuntimeError("governance packet planner produced no variants")
    return tuple(variants)


def _governance_output_schema(
    *,
    legal_final_kinds: Sequence[str],
    allowed_target_ids: Sequence[str],
) -> Mapping[str, object]:
    """Describe one flat closed output object without example-shaped branches."""

    decisions = [MemoryDecisionKind.SKIP.value, MemoryDecisionKind.ACCEPT.value]
    if allowed_target_ids:
        decisions.extend(
            (
                MemoryDecisionKind.ACCEPT_AND_SUPERSEDE.value,
                MemoryDecisionKind.ACCEPT_AND_CONTRADICT.value,
            )
        )
    return {
        "type": "object",
        "shape": "one flat top-level object; never wrap it in a branch name",
        "additional_fields": False,
        "field_constraints": {
            "decision": {
                "type": "string",
                "allowed_values": tuple(decisions),
            },
            "reason_code": {
                "type": "string",
                "allowed_values": tuple(
                    sorted(item.value for item in MODEL_GOVERNANCE_SKIP_REASON_CODES)
                ),
            },
            "final_kind": {
                "type": "string",
                "allowed_values": tuple(legal_final_kinds),
            },
            "target_fact_id": {
                "type": "string",
                "allowed_values": tuple(allowed_target_ids),
            },
            "supersede_mode": {
                "type": "string",
                "allowed_values": (
                    MemorySupersedeMode.SAME_KIND_REPLACEMENT.value,
                    MemorySupersedeMode.TAXONOMY_CORRECTION.value,
                ),
            },
            "public_summary": {
                "type": "string",
                "contract": "target-independent formation summary",
            },
        },
        "required_fields_by_decision": {
            MemoryDecisionKind.SKIP.value: ("decision", "reason_code"),
            MemoryDecisionKind.ACCEPT.value: (
                "decision",
                "final_kind",
                "public_summary",
            ),
            **(
                {
                    MemoryDecisionKind.ACCEPT_AND_SUPERSEDE.value: (
                        "decision",
                        "final_kind",
                        "target_fact_id",
                        "supersede_mode",
                        "public_summary",
                    ),
                    MemoryDecisionKind.ACCEPT_AND_CONTRADICT.value: (
                        "decision",
                        "final_kind",
                        "target_fact_id",
                        "public_summary",
                    ),
                }
                if allowed_target_ids
                else {}
            ),
        },
        "optional_fields_by_decision": {
            MemoryDecisionKind.SKIP.value: ("public_summary",),
        },
    }


def _terminal_outcome_product_label(status: str) -> str:
    return (
        "The origin turn completed normally."
        if status == "COMPLETED"
        else "The origin turn ended before normal completion."
    )


def _fact_projection(item) -> dict[str, object]:
    return {
        "memory_id": item.fact_id,
        "scope_kind": item.scope_kind,
        "kind": item.fact_kind,
        "lifecycle": item.lifecycle,
        "statement": item.statement,
        "applies_when": item.applies_when,
        "do_not_apply_when": item.do_not_apply_when,
    }


def _frozen_fact_projection(item) -> FrozenMemoryPublicFactProjection:
    return FrozenMemoryPublicFactProjection(
        fact_id=item.fact_id,
        scope_kind=MemoryScopeKind(item.scope_kind),
        scope_id=item.scope_id,
        fact_kind=MemoryFactKind(item.fact_kind),
        lifecycle=item.lifecycle,
        statement=item.statement,
        applies_when=item.applies_when,
        do_not_apply_when=item.do_not_apply_when,
        fact_semantic_digest=item.fact_semantic_digest,
    )


def _parse_governance_decision(
    value: Mapping[str, object],
    allowed_targets: Mapping[str, FrozenMemoryPublicFactProjection],
    *,
    legal_final_kinds: Sequence[MemoryFactKind],
) -> FrozenMemoryGovernanceDecision:
    decision_text = _required_string(value.get("decision"), "governance decision")
    try:
        decision = MemoryDecisionKind(decision_text)
    except ValueError as exc:
        raise ValueError("governance decision is outside the closed union") from exc
    allowed_fields = {"decision", "public_summary"}
    if decision is MemoryDecisionKind.SKIP:
        allowed_fields.add("reason_code")
        if set(value) - allowed_fields:
            raise ValueError("SKIP output contains extra semantic fields")
        reason = _required_string(value.get("reason_code"), "governance reason")
        try:
            closed_reason = MemoryDecisionReasonCode(reason)
        except ValueError as exc:
            raise ValueError("governance SKIP reason is outside the closed union") from exc
        if closed_reason not in MODEL_GOVERNANCE_SKIP_REASON_CODES:
            raise ValueError("governance SKIP reason is invalid")
        public_summary = _optional_string(value.get("public_summary"))
        if public_summary is not None:
            _validate_governance_public_summary(public_summary)
        return FrozenMemoryGovernanceDecision(
            decision,
            reason_code=reason,
            public_summary=public_summary,
        )
    allowed_fields.add("final_kind")
    try:
        final_kind = MemoryFactKind(
            _required_string(value.get("final_kind"), "governance final kind")
        )
    except ValueError as exc:
        raise ValueError("governance final kind is outside the closed union") from exc
    if final_kind not in legal_final_kinds:
        raise ValueError("governance final kind is illegal for the frozen shape")
    target = None
    mode = None
    if decision is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE:
        allowed_fields.update({"target_fact_id", "supersede_mode"})
        target = _required_string(
            value.get("target_fact_id"), "governance target fact"
        )
        mode = MemorySupersedeMode(
            _required_string(value.get("supersede_mode"), "governance supersede mode")
        )
    elif decision is MemoryDecisionKind.ACCEPT_AND_CONTRADICT:
        allowed_fields.add("target_fact_id")
        target = _required_string(
            value.get("target_fact_id"), "governance target fact"
        )
    if set(value) - allowed_fields:
        raise ValueError("governance output contains extra semantic fields")
    if target is not None and target not in allowed_targets:
        raise ValueError("governance selected a target outside the frozen allowlist")
    public_summary = _required_string(
        value.get("public_summary"), "governance public summary"
    )
    _validate_governance_public_summary(public_summary)
    return FrozenMemoryGovernanceDecision(
        decision,
        final_kind=final_kind,
        public_summary=public_summary,
        related_target_fact_id=target,
        supersede_mode=mode,
    )


def _selected_governance_relation_targets(
    decision: FrozenMemoryGovernanceDecision,
    allowed_targets: Mapping[str, FrozenMemoryPublicFactProjection],
) -> tuple[FrozenMemoryPublicFactProjection, ...]:
    """Transfer only the exact target authority selected by the parsed decision."""

    target_id = decision.related_target_fact_id
    if target_id is None:
        return ()
    try:
        return (allowed_targets[target_id],)
    except KeyError as exc:
        raise ValueError("governance selected a target outside the frozen allowlist") from exc


def _prepare_reflection_batch(
    handoff: PreparedCheapHintReflectionHandoff,
    output: Mapping[str, object],
) -> PreparedCheapHintReflectionCandidateBatch:
    if set(output) != {"candidates"} or not isinstance(output["candidates"], list):
        raise ValueError("reflection output is outside its closed schema")
    rows = output["candidates"]
    if len(rows) > 4:
        raise ValueError("reflection output exceeds four candidates")
    eligible = {
        f"user:{ordinal}": item
        for ordinal, item in enumerate(handoff.eligible_entries, start=1)
    }
    encoded = canonical_json_bytes(output)
    output_digest = "sha256:" + sha256(encoded).hexdigest()
    candidates: list[PreparedMemoryCandidateAcceptance] = []
    for ordinal, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError("reflection candidate is not an object")
        allowed = {
            "source",
            "statement",
            "scope",
            "kind_hint",
            "applies_when",
            "do_not_apply_when",
        }
        if set(raw) - allowed:
            raise ValueError("reflection candidate contains extra fields")
        source_handle = _required_string(raw.get("source"), "reflection source")
        source = eligible.get(source_handle)
        if source is None:
            raise ValueError("reflection candidate source is not eligible")
        statement = _required_string(raw.get("statement"), "reflection statement")
        # Reflection may select a single verbatim normalized atom, but cannot
        # invent text absent from the exact human projection.
        normalized_source = normalize_memory_text(source.public_text)
        normalized_statement = normalize_memory_text(statement)
        if not normalized_statement or normalized_statement not in normalized_source:
            raise ValueError("reflection candidate rewrote its human source")
        scope_kind = MemoryScopeKind(
            _required_string(raw.get("scope", "USER"), "reflection scope")
        )
        if scope_kind is MemoryScopeKind.USER:
            scope_id = "ctx:user"
        else:
            scope_id = handoff.workspace_scope_id
            if scope_id is None:
                raise ValueError(
                    "reflection cannot propose WORKSPACE memory in a transient Host"
                )
        proposal = FrozenMemoryProposal(
            statement=normalized_statement,
            scope_kind=scope_kind,
            scope_id=scope_id,
            kind_hint=MemoryKindHint(
                _required_string(raw.get("kind_hint", "AUTO"), "reflection kind hint")
            ),
            applies_when=_optional_string(raw.get("applies_when")),
            do_not_apply_when=_strict_string_sequence(
                raw.get("do_not_apply_when"), "reflection exclusions"
            ),
        )
        candidate_id = "memory-candidate:" + sha256(
            canonical_json_bytes(
                (
                    cheap_hint_handoff_identity_digest(handoff),
                    output_digest,
                    ordinal,
                )
            )
        ).hexdigest()
        candidates.append(
            prepare_memory_candidate(
                candidate_id=candidate_id,
                memory_domain_id=handoff.memory_domain_id,
                origin_workspace_id=handoff.workspace_id,
                origin_session_id=handoff.session_id,
                producer_kind=MemoryProducerKind.CHEAP_HINT_REFLECTION,
                proposal=proposal,
                trigger_user_entry_id=source.entry_id,
                producer_candidate_ordinal=ordinal,
            )
        )
    return PreparedCheapHintReflectionCandidateBatch(
        candidates=tuple(candidates),
    )


def _validate_governance_public_summary(value: str) -> None:
    lowered = value.casefold()
    forbidden = (
        "memory-candidate:",
        "memory-fact:",
        "memory:",
        "memory-relation:",
        "session:",
        "turn:",
        "entry:",
        "event:",
        "source:",
        "context:",
        "producer:",
        "after:",
        "cited-observation:",
        "ctx:",
        "target_fact_id",
        "reason_code",
        "final_kind",
        "human_assertion",
        "post_proposal_human",
        "primary_observation",
        "memory_read_exposure",
        "assistant_context",
        "non_human_context",
        "tool_context_only",
        "sql",
        "prompt",
        "provider",
        "wire api",
        "replay fragment",
        "fingerprint",
        "embedding score",
        "rerank score",
        "permission snapshot",
        "system prompt",
        "tool schema",
        "context binding",
        "event sequence",
        "terminal event",
        "governance watchdog",
        "runtime owner",
        "database lane",
        "host process",
        "candidate claim",
        "verified",
        "permanent",
        "guaranteed",
        "replaced",
        "updated",
        "conflict",
        "已验证",
        "永久",
        "保证会",
        "替代了",
        "更新了",
        "冲突",
        "提示词",
        "供应商",
        "数据库通道",
        "候选领取",
    )
    if any(term in lowered for term in forbidden):
        raise ValueError("governance public summary exposes non-product semantics")
    internal_enums = (
        tuple(item.value for item in MemoryFactKind)
        + tuple(item.value for item in MemoryDecisionReasonCode)
        + tuple(item.value for item in MemoryProducerKind)
    )
    if any(term.casefold() in lowered for term in internal_enums):
        raise ValueError("governance public summary exposes an internal enum")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional model output text is not a string")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is not a non-empty string")
    return value


def _strict_string_sequence(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} is not a string array")
    return tuple(value)


__all__ = ["AdvisoryMemoryGovernor", "MemoryEmbeddingMaintenancePort"]
