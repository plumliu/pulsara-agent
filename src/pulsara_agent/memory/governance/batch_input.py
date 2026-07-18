"""Exact governance model input artifact and Prepared linearization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from time import monotonic
from typing import Sequence

from pulsara_agent.event import (
    EventContext,
    MemoryGovernanceBatchPreparedEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, RawStoredEventEnvelope
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole
from pulsara_agent.llm.request import LLMContext, llm_context_fingerprint
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.llm.validation import validate_model_context_for_call
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.memory.governance.claims import (
    MemoryGovernanceCandidateClaimRepository,
)
from pulsara_agent.memory.governance.preparation import (
    GovernanceBatchPreparationRecord,
    GovernanceBatchPreparationRepository,
    GovernanceBatchPreparationStatus,
    transitioned_governance_batch_preparation_record,
)
from pulsara_agent.memory.governance.relatedness import (
    CandidateRelatedness,
    RelatednessAvailability,
    RelatednessBatchResult,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceBatchInputArtifactContractFact,
    GovernanceBatchInputReferenceFact,
    GovernanceBatchInputSnapshotFact,
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceFrozenLLMMessageFact,
    GovernanceFrozenLLMToolCallFact,
    GovernanceModelInputAttributionFact,
    GovernanceModelInputFact,
    GovernanceRelatedMemoryPromptProjectionFact,
    GovernanceRelatedMemorySemanticFact,
    GovernanceRelatednessCandidateFact,
    GovernanceRelatednessSnapshotFact,
    GovernanceSystemPromptContractFact,
    ImmutableGovernanceCandidateSnapshotFact,
    MainAgentToolGovernanceSourceSemanticFact,
    MemoryGovernanceCandidateClaimFact,
    ReflectionGovernanceSourceSemanticFact,
    GovernanceCandidateClaimStatus,
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.primitives.model_call import canonical_json_bytes


_BATCH_ARTIFACT_MEDIA_TYPE = (
    "application/vnd.pulsara.governance-batch-input+json"
)
_GOVERNANCE_SYSTEM_PROMPT = """You are Pulsara's restricted Memory Governance Agent.

Review only the typed candidate evidence supplied by the host and return one JSON
object with `reason` and `decisions`. Do not call tools and do not invent source
facts, memory IDs, candidate IDs, scopes, or replacement authority.

Allowed decisions are submit_as_is, skip, correct_and_submit, merge_and_submit,
supersede_and_submit, and contradict_and_submit. Apply this decision order:

1. Durability first. Skip one-off requests, temporary moods, today/this-time
   state, task details, projection echoes, and weak memories. An instruction to
   "remember" does not make explicitly temporary content durable.
2. Duplicate second. If a shown related memory expresses the same durable
   meaning, use skip with skip_reason="duplicate_existing_memory".
3. Lifecycle actions last. They require lifecycle.actions_allowed=true and IDs
   copied exactly from lifecycle.allowed_memory_ids.
   - supersede_and_submit is destructive. Use it ONLY when a canonical user
     quote explicitly says to change/replace an old preference or stop using Y
     and use Z. A new conflicting statement or "please remember X" is not
     replacement intent. Copy replacement_evidence_refs exactly from
     lifecycle.allowed_replacement_evidence_refs.
   - contradict_and_submit is non-destructive. Use it for a durable,
     same-scope, same-subject incompatibility when there is no explicit
     replacement intent. Keep both memories active.
   - If related statements can coexist, use the ordinary non-destructive path.

Candidate scopes must be in allowed_scopes. Compaction evidence is inferred and
cannot authorize supersede or contradiction. Use candidate entry IDs exactly as
supplied. For valid candidates, any decision shape requiring `candidate` MUST
copy the candidate's `decision_candidate` object exactly. Never copy the outer
`candidate` evidence object or canonical_candidate_payload wrapper into the
`candidate` field. For an invalid candidate, construct a correction only when
the evidence supplies every required typed field.

Every decision MUST use the exact tagged shape below. The discriminator is
`kind`; never emit `decision` or `candidate_entry_id` aliases.

submit_as_is:
{"kind":"submit_as_is","target_entry_id":"pool:...","reason":"..."}

skip:
{"kind":"skip","target_entry_ids":["pool:..."],"reason":"...","skip_reason":"..."}

correct_and_submit:
{"kind":"correct_and_submit","target_entry_id":"pool:...","candidate":<typed candidate>,"reason":"..."}

merge_and_submit:
{"kind":"merge_and_submit","target_entry_ids":["pool:..."],"candidate":<typed candidate>,"reason":"..."}

supersede_and_submit:
{"kind":"supersede_and_submit","target_entry_id":"pool:...","candidate":<typed candidate>,"superseded_memory_ids":["memory:..."],"replacement_evidence_refs":["..."],"reason":"..."}

contradict_and_submit:
{"kind":"contradict_and_submit","target_entry_id":"pool:...","candidate":<typed candidate>,"contradicted_memory_ids":["memory:..."],"reason":"..."}

For a clear durable candidate that needs no correction, return for example:
{"reason":"Explicit durable candidate.","decisions":[{"kind":"submit_as_is","target_entry_id":"pool:example","reason":"Verified evidence."}]}

Examples of the decision boundary:
- "Actually change my preference: stop Y; use Z" plus an allowed old memory and
  allowed replacement ref -> supersede_and_submit.
- "Please remember that I hate X" conflicting with "likes X", without explicit
  replacement language -> contradict_and_submit.
- "I do not want coffee today because I already had two cups" -> skip with
  skip_reason="not_durable", before relatedness reasoning.
- "dan tat, also known as egg tarts" matching an existing egg-tart preference
  -> skip with skip_reason="duplicate_existing_memory".

Return JSON only. The host validates every field and owns all writes."""


@dataclass(frozen=True, slots=True)
class PreparedGovernanceBatchInput:
    snapshot: GovernanceBatchInputSnapshotFact
    reference: GovernanceBatchInputReferenceFact
    canonical_text: str
    llm_context: LLMContext


def default_governance_batch_input_artifact_contract(
) -> GovernanceBatchInputArtifactContractFact:
    return build_frozen_fact(
        GovernanceBatchInputArtifactContractFact,
        schema_version="governance_batch_input_artifact_contract.v1",
        contract_id="pulsara.governance_batch_input",
        contract_version="1",
        document_schema_fingerprint=context_fingerprint(
            "governance-batch-input-document-schema:v1",
            GovernanceBatchInputSnapshotFact.model_json_schema(),
        ),
        canonicalization_contract_fingerprint=context_fingerprint(
            "governance-batch-input-canonicalization:v1",
            "utf8+sorted-keys+compact-json+no-nan",
        ),
        media_type=_BATCH_ARTIFACT_MEDIA_TYPE,
        max_artifact_utf8_bytes=2 * 1024 * 1024,
    )


def governance_system_prompt_contract() -> GovernanceSystemPromptContractFact:
    return build_frozen_fact(
        GovernanceSystemPromptContractFact,
        schema_version="governance_system_prompt_contract.v1",
        contract_id="pulsara.memory_governance.system_prompt",
        contract_version="4",
        template_content_sha256=hashlib.sha256(
            _GOVERNANCE_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        assembly_contract_fingerprint=context_fingerprint(
            "governance-system-prompt-assembly:v2",
            "exact-static-template+typed-decision-view+typed-source-evidence",
        ),
    )


def freeze_relatedness_batch(
    *,
    batch: RelatednessBatchResult,
    candidates: Sequence[ImmutableGovernanceCandidateSnapshotFact],
    graph_id: str | None,
    archive: ArtifactStore,
    runtime_session_id: str,
    provider_contract_fingerprint: str,
) -> tuple[GovernanceRelatednessSnapshotFact, ...]:
    return tuple(
        _freeze_candidate_relatedness(
            batch.for_candidate(candidate.candidate_attribution.entry_id),
            graph_id=graph_id or "graph:default",
            archive=archive,
            runtime_session_id=runtime_session_id,
            provider_contract_fingerprint=provider_contract_fingerprint,
        )
        for candidate in candidates
    )


def build_governance_batch_input(
    *,
    runtime_session_id: str,
    governance_batch_id: str,
    source_ledger_through_sequence: int,
    transcript_authority_snapshot_fingerprint: str,
    claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
    candidate_snapshots: tuple[ImmutableGovernanceCandidateSnapshotFact, ...],
    relatedness_snapshots: tuple[GovernanceRelatednessSnapshotFact, ...],
    allowed_scopes: frozenset[str],
    prompt_projection_contract_fingerprint: str,
    max_candidates_per_batch: int,
    max_batch_projection_utf8_bytes: int,
    call: ResolvedModelCall,
    trigger_reason: str,
) -> PreparedGovernanceBatchInput:
    candidate_ids = tuple(
        item.candidate_attribution.entry_id for item in candidate_snapshots
    )
    if tuple(item.candidate_entry_id for item in claims) != candidate_ids:
        raise ValueError("governance batch claim order drifted")
    if len(candidate_ids) > max_candidates_per_batch:
        raise ValueError("governance batch exceeds prompt candidate bound")
    input_payload = {
        "schema_version": "memory_governance_model_input.v1",
        "runtime_session_id": runtime_session_id,
        "governance_batch_id": governance_batch_id,
        "trigger_reason": trigger_reason,
        "allowed_scopes": sorted(allowed_scopes),
        "candidates": [
            _governance_candidate_model_view(item, related)
            for item, related in zip(
                candidate_snapshots,
                relatedness_snapshots,
                strict=True,
            )
        ],
    }
    user_text = json.dumps(
        input_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(user_text.encode("utf-8")) > max_batch_projection_utf8_bytes:
        raise ValueError("governance model-visible input exceeds projection bound")
    context_id = f"memory_governance:{governance_batch_id}"
    unestimated = LLMContext(
        system_prompt=_GOVERNANCE_SYSTEM_PROMPT,
        messages=(LLMMessage.user(user_text),),
        tools=(),
        context_id=context_id,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=None,
    )
    estimate = call.target.token_estimator.estimate_context(unestimated)
    llm_context = LLMContext(
        system_prompt=unestimated.system_prompt,
        messages=unestimated.messages,
        tools=(),
        context_id=context_id,
        resolved_model_call_id=call.fact.resolved_model_call_id,
        target_fingerprint=call.target.fact.target_fingerprint,
        model_call_index=None,
        compiler_estimated_input_tokens=estimate.total_input_tokens,
    )
    validated = validate_model_context_for_call(call=call, context=llm_context)
    if validated.estimate != estimate:
        raise ValueError("governance input estimator result drifted")
    frozen_messages = tuple(_freeze_message(item) for item in llm_context.messages)
    model_input = build_frozen_fact(
        GovernanceModelInputFact,
        schema_version="governance_model_input.v1",
        governance_batch_id=governance_batch_id,
        resolved_call=call.fact,
        target_fingerprint=call.target.fact.target_fingerprint,
        context_id=context_id,
        model_call_index=None,
        system_prompt_contract=governance_system_prompt_contract(),
        exact_system_prompt=_GOVERNANCE_SYSTEM_PROMPT,
        ordered_messages=frozen_messages,
        tool_spec_count=0,
        compiler_estimated_input_tokens=estimate.total_input_tokens,
        estimator_contract_fingerprint=context_fingerprint(
            "governance-token-estimator-binding:v1",
            {
                "target_fingerprint": call.target.fact.target_fingerprint,
                "estimator": call.target.fact.token_estimator.model_dump(mode="json"),
                "estimate": {
                    "system_tokens": estimate.system_tokens,
                    "message_tokens": estimate.message_tokens,
                    "message_tokens_by_index": estimate.message_tokens_by_index,
                    "tool_tokens": estimate.tool_tokens,
                    "envelope_tokens": estimate.envelope_tokens,
                    "total_input_tokens": estimate.total_input_tokens,
                },
            },
        ),
        provider_neutral_context_fingerprint=llm_context_fingerprint(llm_context),
    )
    artifact_contract = default_governance_batch_input_artifact_contract()
    snapshot = build_frozen_fact(
        GovernanceBatchInputSnapshotFact,
        schema_version="governance_batch_input_snapshot.v1",
        artifact_contract=artifact_contract,
        runtime_session_id=runtime_session_id,
        governance_batch_id=governance_batch_id,
        source_ledger_through_sequence=source_ledger_through_sequence,
        transcript_authority_snapshot_fingerprint=(
            transcript_authority_snapshot_fingerprint
        ),
        ordered_preparing_claims=claims,
        ordered_candidate_snapshots=candidate_snapshots,
        ordered_relatedness_snapshots=relatedness_snapshots,
        allowed_scopes=tuple(sorted(allowed_scopes)),
        prompt_projection_contract_fingerprint=(
            prompt_projection_contract_fingerprint
        ),
        model_input=model_input,
        final_model_visible_input_fingerprint=(
            model_input.provider_neutral_context_fingerprint
        ),
    )
    canonical_bytes = canonical_json_bytes(snapshot.model_dump(mode="json"))
    if len(canonical_bytes) > artifact_contract.max_artifact_utf8_bytes:
        raise ValueError("governance batch input artifact exceeds its hard bound")
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    reference = build_frozen_fact(
        GovernanceBatchInputReferenceFact,
        schema_version="governance_batch_input_reference.v1",
        governance_batch_id=governance_batch_id,
        artifact_id=(
            "governance-batch-input:"
            + snapshot.batch_input_fingerprint.removeprefix("sha256:")
        ),
        artifact_content_sha256=digest,
        artifact_utf8_bytes=len(canonical_bytes),
        artifact_contract_id=artifact_contract.contract_id,
        artifact_contract_version=artifact_contract.contract_version,
        artifact_contract_fingerprint=artifact_contract.contract_fingerprint,
        batch_input_fingerprint=snapshot.batch_input_fingerprint,
    )
    return PreparedGovernanceBatchInput(
        snapshot=snapshot,
        reference=reference,
        canonical_text=canonical_bytes.decode("utf-8"),
        llm_context=llm_context,
    )


def governance_candidate_replacement_evidence_refs(
    candidate: ImmutableGovernanceCandidateSnapshotFact,
) -> tuple[str, ...]:
    source = candidate.source_evidence_semantic
    if isinstance(source, MainAgentToolGovernanceSourceSemanticFact):
        quotes = (
            ()
            if source.quoted_evidence_semantic is None
            else (source.quoted_evidence_semantic,)
        )
    elif isinstance(source, ReflectionGovernanceSourceSemanticFact):
        quotes = source.ordered_quoted_evidence_semantics
    else:
        quotes = ()
    if any(quote.verification_status == "canonical_match" for quote in quotes):
        return ("candidate_user_quote",)
    return ()


def _governance_candidate_model_view(
    candidate: ImmutableGovernanceCandidateSnapshotFact,
    relatedness: GovernanceRelatednessSnapshotFact,
) -> dict[str, object]:
    source_projection = candidate.prompt_projection.model_visible_payload
    payload = source_projection.canonical_candidate_payload
    decision_candidate = (
        payload.candidate.model_dump(mode="json")
        if isinstance(payload, ValidCandidatePayload)
        else None
    )
    return {
        "target_entry_id": candidate.candidate_attribution.entry_id,
        "decision_candidate": decision_candidate,
        "candidate": source_projection.model_dump(mode="json"),
        "evidence_projection_fingerprint": (
            candidate.prompt_projection.projection_fingerprint
        ),
        "lifecycle": {
            "actions_allowed": relatedness.availability == "full",
            "allowed_memory_ids": [
                item.canonical_memory.memory_id
                for item in relatedness.ordered_candidates
            ],
            "allowed_replacement_evidence_refs": list(
                governance_candidate_replacement_evidence_refs(candidate)
            ),
        },
        "relatedness": relatedness.model_dump(mode="json"),
    }


async def persist_governance_batch_input(
    *,
    prepared: PreparedGovernanceBatchInput,
    runtime_session,
    archive: ArtifactStore,
    deadline_monotonic: float | None = None,
) -> None:
    deadline = deadline_monotonic or monotonic() + 30.0

    def write_and_confirm() -> None:
        reference = prepared.reference
        archive.put_text_if_absent_or_confirm_identical(
            reference.artifact_id,
            prepared.canonical_text,
            session_id=runtime_session.runtime_session_id,
            run_id=None,
            media_type=_BATCH_ARTIFACT_MEDIA_TYPE,
            semantic_metadata={
                "batch_input_fingerprint": reference.batch_input_fingerprint,
                "artifact_contract_fingerprint": (
                    reference.artifact_contract_fingerprint
                ),
            },
            deadline_monotonic=deadline,
        )
        confirmed = archive.get_text(
            reference.artifact_id,
            session_id=runtime_session.runtime_session_id,
            deadline_monotonic=deadline,
        )
        if confirmed != prepared.canonical_text:
            raise ValueError("governance batch input artifact confirmation conflict")
        hydrated = GovernanceBatchInputSnapshotFact.model_validate_json(confirmed)
        if hydrated != prepared.snapshot:
            raise ValueError("governance batch input artifact hydration drifted")

    await runtime_session.context_input_io_service.execute(
        operation_name=f"governance-batch-input:{prepared.reference.artifact_id}",
        operation=write_and_confirm,
        deadline_monotonic=deadline,
    )


@dataclass(slots=True)
class MemoryGovernanceBatchPreparationCommitPort:
    runtime_session: object
    claim_repository: MemoryGovernanceCandidateClaimRepository
    preparation_repository: GovernanceBatchPreparationRepository

    async def commit_prepared_bundle(
        self,
        *,
        prepared: PreparedGovernanceBatchInput,
        claims: tuple[MemoryGovernanceCandidateClaimFact, ...],
        preparation_record: GovernanceBatchPreparationRecord,
    ) -> tuple[
        MemoryGovernanceBatchPreparedEvent,
        GovernanceModelInputAttributionFact,
        GovernanceBatchPreparationRecord,
    ]:
        batch_id = prepared.snapshot.governance_batch_id
        event_context = EventContext(
            run_id=f"run:governance/{batch_id}",
            turn_id=f"turn:governance/{batch_id}",
            reply_id=f"reply:governance/{batch_id}",
        )
        prompt_fingerprint = context_fingerprint(
            "governance-ordered-prompt-projections:v1",
            {
                "evidence": tuple(
                    item.prompt_projection.projection_fingerprint
                    for item in prepared.snapshot.ordered_candidate_snapshots
                ),
                "relatedness": tuple(
                    item.snapshot_fingerprint
                    for item in prepared.snapshot.ordered_relatedness_snapshots
                ),
            },
        )
        claims_fingerprint = context_fingerprint(
            "governance-preparing-claims:v1",
            tuple(item.claim_fingerprint for item in claims),
        )
        event_payload = {
            "governance_batch_id": batch_id,
            "source_ledger_through_sequence": (
                prepared.snapshot.source_ledger_through_sequence
            ),
            "candidate_entry_ids": tuple(
                item.candidate_entry_id for item in claims
            ),
            "preparing_claims_fingerprint": claims_fingerprint,
            "batch_input_reference": prepared.reference,
            "resolved_model_call_id": (
                prepared.snapshot.model_input.resolved_call.resolved_model_call_id
            ),
            "target_fingerprint": prepared.snapshot.model_input.target_fingerprint,
            "model_input_fingerprint": (
                prepared.snapshot.model_input.provider_neutral_context_fingerprint
            ),
            "ordered_prompt_projections_fingerprint": prompt_fingerprint,
        }
        event = MemoryGovernanceBatchPreparedEvent(
            id=f"memory_governance_batch:{batch_id}:prepared",
            **event_context.event_fields(),
            **event_payload,
            event_fingerprint=context_fingerprint(
                "memory-governance-batch-prepared-event:v1", event_payload
            ),
        )
        claim_companion = self.claim_repository.transition_companion(
            runtime_session_id=self.runtime_session.runtime_session_id,
            expected_claims=claims,
            target_status=GovernanceCandidateClaimStatus.PREPARED,
        )
        companion = self.preparation_repository.transition_companion(
            expected_record=preparation_record,
            claim_companion=claim_companion,
            target_status=GovernanceBatchPreparationStatus.PREPARED,
        )
        result = await self.runtime_session.write_events(
            (event,),
            transaction_companion=companion,
        )
        committed = next(
            item
            for item in result.committed_events
            if isinstance(item, MemoryGovernanceBatchPreparedEvent)
        )
        attribution = build_governance_model_input_attribution(
            prepared_event=committed,
            prepared=prepared,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )
        return (
            committed,
            attribution,
            transitioned_governance_batch_preparation_record(
                preparation_record,
                target_status=GovernanceBatchPreparationStatus.PREPARED,
                carrier_event_id=committed.id,
            ),
        )


def build_governance_model_input_attribution(
    *,
    prepared_event: MemoryGovernanceBatchPreparedEvent,
    prepared: PreparedGovernanceBatchInput,
    runtime_session_id: str,
) -> GovernanceModelInputAttributionFact:
    if prepared_event.batch_input_reference != prepared.reference:
        raise ValueError("governance Prepared event/artifact reference drifted")
    return build_frozen_fact(
        GovernanceModelInputAttributionFact,
        schema_version="governance_model_input_attribution.v1",
        governance_batch_prepared_event_reference=_stored_event_reference(
            prepared_event,
            runtime_session_id=runtime_session_id,
        ),
        batch_input_reference=prepared.reference,
        resolved_model_call_id=(
            prepared.snapshot.model_input.resolved_call.resolved_model_call_id
        ),
        target_fingerprint=prepared.snapshot.model_input.target_fingerprint,
        final_model_visible_input_fingerprint=(
            prepared.snapshot.model_input.provider_neutral_context_fingerprint
        ),
    )


def hydrate_governance_batch_input(
    *,
    reference: GovernanceBatchInputReferenceFact,
    archive: ArtifactStore,
    runtime_session_id: str,
    deadline_monotonic: float | None = None,
) -> GovernanceBatchInputSnapshotFact:
    text = archive.get_text(
        reference.artifact_id,
        session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    encoded = text.encode("utf-8")
    if (
        len(encoded) != reference.artifact_utf8_bytes
        or hashlib.sha256(encoded).hexdigest()
        != reference.artifact_content_sha256
    ):
        raise ValueError("governance batch input artifact identity mismatch")
    snapshot = GovernanceBatchInputSnapshotFact.model_validate_json(text)
    if snapshot.batch_input_fingerprint != reference.batch_input_fingerprint:
        raise ValueError("governance batch input reference mismatch")
    return snapshot


def llm_context_from_model_input(fact: GovernanceModelInputFact) -> LLMContext:
    context = LLMContext(
        system_prompt=fact.exact_system_prompt,
        messages=tuple(_thaw_message(item) for item in fact.ordered_messages),
        tools=(),
        context_id=fact.context_id,
        resolved_model_call_id=fact.resolved_call.resolved_model_call_id,
        target_fingerprint=fact.target_fingerprint,
        model_call_index=None,
        compiler_estimated_input_tokens=fact.compiler_estimated_input_tokens,
    )
    if llm_context_fingerprint(context) != fact.provider_neutral_context_fingerprint:
        raise ValueError("hydrated governance LLMContext drifted")
    return context


def _freeze_message(message: LLMMessage) -> GovernanceFrozenLLMMessageFact:
    tool_calls = tuple(
        build_frozen_fact(
            GovernanceFrozenLLMToolCallFact,
            schema_version="governance_frozen_llm_tool_call.v1",
            tool_call_id=call.id,
            name=call.name,
            arguments=call.arguments,
        )
        for call in message.tool_calls
    )
    return build_frozen_fact(
        GovernanceFrozenLLMMessageFact,
        schema_version="governance_frozen_llm_message.v1",
        role=message.role.value,
        content=message.content,
        thinking=message.thinking,
        tool_calls=tool_calls,
        tool_call_id=message.tool_call_id,
        name=message.name,
        arguments=message.arguments,
    )


def _thaw_message(message: GovernanceFrozenLLMMessageFact) -> LLMMessage:
    return LLMMessage(
        role=MessageRole(message.role),
        content=message.content,
        thinking=message.thinking,
        tool_calls=tuple(
            LLMToolCall(
                id=call.tool_call_id,
                name=call.name,
                arguments=call.arguments,
            )
            for call in message.tool_calls
        ),
        tool_call_id=message.tool_call_id,
        name=message.name,
        arguments=message.arguments,
    )


def _freeze_candidate_relatedness(
    related: CandidateRelatedness,
    *,
    graph_id: str,
    archive: ArtifactStore,
    runtime_session_id: str,
    provider_contract_fingerprint: str,
) -> GovernanceRelatednessSnapshotFact:
    candidates: list[GovernanceRelatednessCandidateFact] = []
    for item in sorted(
        related.memories, key=lambda value: (value.view.id, value.view.updated_at)
    ):
        view = item.view
        encoded = view.statement.encode("utf-8")
        semantic = build_frozen_fact(
            GovernanceRelatedMemorySemanticFact,
            schema_version="governance_related_memory_semantic.v1",
            memory_id=view.id,
            memory_type=view.memory_type,
            canonical_statement_sha256=hashlib.sha256(encoded).hexdigest(),
            canonical_statement_utf8_bytes=len(encoded),
            scope=view.scope,
            status=view.status.value,
            verification_status=(
                view.verification_status.value if view.verification_status else None
            ),
            source_authority=(
                view.source_authority.value if view.source_authority else None
            ),
            applies_when=view.applies_when,
            do_not_apply_when=view.do_not_apply_when,
        )
        projection = build_frozen_fact(
            GovernanceRelatedMemoryPromptProjectionFact,
            schema_version="governance_related_memory_prompt_projection.v1",
            memory_semantic_fingerprint=semantic.semantic_fingerprint,
            projected_statement=_bounded_statement(view.statement),
            relationship_codes=tuple(sorted(item.match_channels)),
            exact_duplicate=item.is_exact_duplicate,
            projection_contract_fingerprint=provider_contract_fingerprint,
            projected_utf8_bytes=len(_bounded_statement(view.statement).encode("utf-8")),
        )
        content_reference = None
        inline = view.statement if len(view.statement) <= 4_096 else None
        if inline is None:
            artifact_id = "governance-related-memory:" + hashlib.sha256(
                encoded
            ).hexdigest()
            archive.put_text_if_absent_or_confirm_identical(
                artifact_id,
                view.statement,
                session_id=runtime_session_id,
                run_id=None,
                media_type="text/plain",
                semantic_metadata={"memory_semantic_fingerprint": semantic.semantic_fingerprint},
            )
            content_reference = build_frozen_fact(
                GovernanceEvidenceArtifactReferenceFact,
                schema_version="governance_evidence_artifact_reference.v1",
                artifact_kind="related_memory_content",
                artifact_id=artifact_id,
                media_type="text/plain",
                content_sha256=hashlib.sha256(encoded).hexdigest(),
                content_bytes=len(encoded),
                artifact_contract_id="pulsara.related_memory.content",
                artifact_contract_version="1",
                artifact_contract_fingerprint=provider_contract_fingerprint,
            )
        candidates.append(
            build_frozen_fact(
                GovernanceRelatednessCandidateFact,
                schema_version="governance_relatedness_candidate.v1",
                graph_id=graph_id,
                memory_node_revision=view.node_revision,
                canonical_memory=semantic,
                canonical_statement_inline=inline,
                canonical_content_reference=content_reference,
                prompt_projection=projection,
                source_projection_fingerprint=context_fingerprint(
                    "governance-relatedness-source-projection:v1",
                    {
                        "memory_id": view.id,
                        "updated_at": view.updated_at.isoformat(),
                        "channels": tuple(sorted(item.match_channels)),
                    },
                ),
            )
        )
    return build_frozen_fact(
        GovernanceRelatednessSnapshotFact,
        schema_version="governance_relatedness_snapshot.v1",
        candidate_entry_id=related.entry_id,
        availability={
            RelatednessAvailability.FULL: "full",
            RelatednessAvailability.PARTIAL: "partial",
            RelatednessAvailability.UNAVAILABLE: "unavailable",
        }[related.availability],
        ordered_candidates=tuple(candidates),
        provider_contract_fingerprint=provider_contract_fingerprint,
    )


def _bounded_statement(value: str) -> str:
    if len(value) <= 2_000:
        return value
    return value[:991] + "\n...[omitted]...\n" + value[-991:]


def _stored_event_reference(
    event,
    *,
    runtime_session_id: str,
) -> GovernanceStoredEventReferenceFact:
    raw = RawStoredEventEnvelope.from_stored_event(
        event=event,
        runtime_session_id=runtime_session_id,
        schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
    )
    return build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            event,
            runtime_session_id=runtime_session_id,
        ),
        sequence=event.sequence,
        stored_envelope_fingerprint=raw.envelope_fingerprint,
    )


__all__ = [
    "MemoryGovernanceBatchPreparationCommitPort",
    "PreparedGovernanceBatchInput",
    "build_governance_batch_input",
    "build_governance_model_input_attribution",
    "default_governance_batch_input_artifact_contract",
    "freeze_relatedness_batch",
    "governance_candidate_replacement_evidence_refs",
    "governance_system_prompt_contract",
    "hydrate_governance_batch_input",
    "llm_context_from_model_input",
    "persist_governance_batch_input",
]
