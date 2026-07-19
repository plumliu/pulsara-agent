"""Origin-aware, bounded source evidence for memory governance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    EventType,
    MemoryReflectionCompletedEvent,
    ModelCallEndEvent,
    ModelCallControlDispositionResolvedEvent,
)
from pulsara_agent.event_log import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventLog,
    RawStoredEventEnvelope,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    PooledMemoryCandidate,
    candidate_payload_fingerprint,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.primitives.governance_evidence import (
    CandidateEvidenceRejectionReason,
    CompactionGovernanceSourceSemanticFact,
    GovernanceCandidateAttributionFact,
    GovernanceCandidatePayloadSemanticFact,
    GovernanceCandidatePromptPayloadFact,
    GovernanceEvidenceArtifactReferenceFact,
    GovernanceEvidenceBuildReason,
    GovernanceEvidenceBuildResult,
    GovernanceEvidenceBuildStatus,
    GovernanceEvidencePromptProjectionContractFact,
    GovernanceEvidencePromptProjectionFact,
    GovernancePromptEvidenceTextFact,
    GovernanceQuotedEvidenceAttributionFact,
    GovernanceQuotedEvidenceSemanticFact,
    GovernanceSourceEvidenceAttributionFact,
    GovernanceSourceEvidenceSemanticFact,
    GovernanceStoredEventReferenceFact,
    ImmutableGovernanceCandidateSnapshotFact,
    MainAgentToolGovernanceSourceSemanticFact,
    MemoryCandidateEvidenceRejectedRecord,
    ReflectionGovernanceSourceSemanticFact,
    TranscriptProjectionLeafEntryReferenceFact,
)
from pulsara_agent.primitives.terminal_projection import (
    ModelTerminalProjectionPayloadFact,
    ModelToolCallBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalInlineContentFact,
    TerminalProjectionDocumentFact,
    TerminalProjectionReferenceFact,
    ToolTerminalProjectionPayloadFact,
)
from pulsara_agent.primitives.transcript_projection import (
    InlineNormalizedMessageContentFact,
    NormalizedMessageContentArtifactFact,
    NormalizedMessageContentArtifactReferenceFact,
    TerminalProjectionMessageContentRefFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolResultLeafEntryFact,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    GovernanceTranscriptAuthoritySnapshot,
)
from pulsara_agent.memory.candidates.main_agent_builder import (
    build_main_agent_memory_candidate_payload,
    main_agent_memory_candidate_builder_contract,
    main_agent_memory_candidate_entry_id,
)


_MAX_EXACT_EVENT_REFS = 32
_PROMPT_FIELDS = (
    "verified_user_quote",
    "accepted_assistant_text",
    "selected_tool_arguments",
    "tool_result_essential",
    "reflection_report",
    "compaction_summary",
)


@dataclass(frozen=True, slots=True)
class GovernanceEvidencePreparation:
    result: GovernanceEvidenceBuildResult
    candidate_snapshot: ImmutableGovernanceCandidateSnapshotFact | None = None
    rejection: MemoryCandidateEvidenceRejectedRecord | None = None

    def __post_init__(self) -> None:
        if self.result.status is GovernanceEvidenceBuildStatus.FULL:
            if self.candidate_snapshot is None or self.rejection is not None:
                raise ValueError("full evidence requires one immutable snapshot")
        elif self.result.status is GovernanceEvidenceBuildStatus.CANDIDATE_SOURCE_INVALID:
            if self.rejection is None or self.candidate_snapshot is not None:
                raise ValueError("invalid evidence requires one rejection")
        elif self.candidate_snapshot is not None or self.rejection is not None:
            raise ValueError("non-full evidence cannot carry snapshot or rejection")


@dataclass(slots=True)
class GovernanceSourceEvidenceBuilder:
    runtime_session_id: str
    event_log: EventLog
    archive: ArtifactStore
    prompt_contract: GovernanceEvidencePromptProjectionContractFact = field(
        default_factory=lambda: default_governance_prompt_projection_contract()
    )

    def prepare(
        self,
        *,
        candidate: PooledMemoryCandidate,
        authority: GovernanceTranscriptAuthoritySnapshot,
    ) -> GovernanceEvidencePreparation:
        high_water = authority.ledger_through_sequence
        if candidate.source_session_id != self.runtime_session_id:
            return self._invalid(
                candidate,
                high_water=high_water,
                reason=GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                rejection=CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
            )
        if candidate.origin is CandidateOrigin.GOVERNANCE:
            return GovernanceEvidencePreparation(
                result=_build_result(
                    candidate.entry_id,
                    high_water,
                    GovernanceEvidenceBuildStatus.NOT_APPLICABLE,
                    GovernanceEvidenceBuildReason.NOT_APPLICABLE_AUDIT_ORIGIN,
                )
            )
        try:
            if candidate.origin is CandidateOrigin.MAIN_AGENT_TOOL:
                semantic, attribution = self._main_tool(candidate, authority)
            elif candidate.origin is CandidateOrigin.REFLECTION:
                semantic, attribution = self._reflection(candidate, authority)
            elif candidate.origin is CandidateOrigin.COMPACTION:
                semantic, attribution = self._compaction(candidate, high_water)
            else:
                return self._invalid(
                    candidate,
                    high_water=high_water,
                    reason=GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                    rejection=CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
                )
        except _EvidenceNotReady as exc:
            return GovernanceEvidencePreparation(
                result=_build_result(
                    candidate.entry_id,
                    high_water,
                    GovernanceEvidenceBuildStatus.NOT_READY,
                    exc.reason,
                    retry_after_seconds=1.0,
                )
            )
        except _CandidateSourceInvalid as exc:
            return self._invalid(
                candidate,
                high_water=high_water,
                reason=exc.reason,
                rejection=exc.rejection,
                observed=exc.observed,
            )
        except _AuthorityUntrusted as exc:
            return GovernanceEvidencePreparation(
                result=_build_result(
                    candidate.entry_id,
                    high_water,
                    GovernanceEvidenceBuildStatus.AUTHORITY_UNTRUSTED,
                    exc.reason,
                )
            )

        payload_semantic = _candidate_payload_semantic(candidate)
        source_event_reference = None
        if candidate.source_event_id is not None:
            matches = tuple(
                reference
                for reference in attribution.producer_event_references
                if reference.stable_identity.event_id == candidate.source_event_id
            )
            if len(matches) != 1:
                raise _AuthorityUntrusted(
                    GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
                )
            source_event_reference = matches[0]
        source_artifact_reference = None
        if candidate.source_artifact_id is not None:
            artifact_matches = tuple(
                reference
                for reference in attribution.source_artifact_references
                if reference.artifact_id == candidate.source_artifact_id
            )
            if len(artifact_matches) != 1:
                raise _AuthorityUntrusted(
                    GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
                )
            source_artifact_reference = artifact_matches[0]
        candidate_attribution = _candidate_attribution(
            candidate,
            source_event_reference=source_event_reference,
            source_artifact_reference=source_artifact_reference,
        )
        prompt = self._prompt_projection(
            candidate=candidate,
            semantic=semantic,
            attribution=attribution,
            authority=authority,
        )
        snapshot = build_frozen_fact(
            ImmutableGovernanceCandidateSnapshotFact,
            schema_version="immutable_governance_candidate_snapshot.v1",
            payload_semantic=payload_semantic,
            candidate_attribution=candidate_attribution,
            source_evidence_semantic=semantic,
            source_evidence_attribution=attribution,
            prompt_projection=prompt,
        )
        result = _build_result(
            candidate.entry_id,
            high_water,
            GovernanceEvidenceBuildStatus.FULL,
            {
                CandidateOrigin.MAIN_AGENT_TOOL: GovernanceEvidenceBuildReason.FULL_MAIN_TOOL_JOIN,
                CandidateOrigin.REFLECTION: GovernanceEvidenceBuildReason.FULL_REFLECTION_JOIN,
                CandidateOrigin.COMPACTION: GovernanceEvidenceBuildReason.FULL_COMPACTION_JOIN,
            }[candidate.origin],
            evidence_semantic=semantic,
            evidence_attribution=attribution,
        )
        return GovernanceEvidencePreparation(result=result, candidate_snapshot=snapshot)

    def _main_tool(
        self,
        candidate: PooledMemoryCandidate,
        authority: GovernanceTranscriptAuthoritySnapshot,
    ) -> tuple[
        MainAgentToolGovernanceSourceSemanticFact,
        GovernanceSourceEvidenceAttributionFact,
    ]:
        call_id = candidate.source_tool_call_id
        if call_id is None:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
            )
        entries = authority.reducer_evidence_snapshot.stable_entries
        assistant_matches: list[
            tuple[
                TranscriptMessageLeafEntryFact,
                TerminalProjectionReferenceFact,
                TerminalProjectionDocumentFact,
                ModelToolCallBlockSemanticFact,
            ]
        ] = []
        for entry in entries:
            if not isinstance(entry, TranscriptMessageLeafEntryFact):
                continue
            content = entry.content
            if not isinstance(content, TerminalProjectionMessageContentRefFact):
                continue
            document = authority.document_view.resolve(content.projection_reference)
            if not isinstance(document.payload, ModelTerminalProjectionPayloadFact):
                continue
            for item in document.payload.items:
                if (
                    isinstance(item.semantic_identity, ModelToolCallBlockSemanticFact)
                    and item.semantic_identity.tool_call_id == call_id
                    and entry.attribution.run_id == candidate.source_run_id
                    and entry.attribution.turn_id == candidate.source_turn_id
                    and entry.attribution.reply_id == candidate.source_reply_id
                ):
                    assistant_matches.append(
                        (
                            entry,
                            content.projection_reference,
                            document,
                            item.semantic_identity,
                        )
                    )
        result_matches = tuple(
            entry
            for entry in entries
            if isinstance(entry, TranscriptToolResultLeafEntryFact)
            and entry.semantic_identity.tool_call_id == call_id
        )
        pair_matches = tuple(
            entry
            for entry in entries
            if isinstance(entry, TranscriptToolPairLeafEntryFact)
            and entry.semantic_identity.assistant_tool_call_id == call_id
        )
        if not assistant_matches or not result_matches or not pair_matches:
            if self._source_run_terminal(candidate.source_run_id, authority.ledger_through_sequence):
                raise _CandidateSourceInvalid(
                    GovernanceEvidenceBuildReason.INVALID_TERMINAL_RUN_WITHOUT_PAIR,
                    CandidateEvidenceRejectionReason.TERMINAL_RUN_WITHOUT_PAIR,
                )
            raise _EvidenceNotReady(GovernanceEvidenceBuildReason.WAIT_REDUCER_BEHIND)
        if len(assistant_matches) != 1 or len(result_matches) != 1 or len(pair_matches) != 1:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        assistant_entry, model_ref, model_document, tool_call = assistant_matches[0]
        result_entry = result_matches[0]
        pair_entry = pair_matches[0]
        if (
            pair_entry.semantic_identity.assistant_message_semantic_fingerprint
            != assistant_entry.semantic_identity.semantic_fingerprint
            or pair_entry.semantic_identity.tool_result_semantic_fingerprint
            != result_entry.projection_reference.semantic_join.semantic_fingerprint
        ):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        if tool_call.arguments_status != "valid_object" or tool_call.parsed_arguments is None:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_CANDIDATE_PAYLOAD_MISMATCH,
                CandidateEvidenceRejectionReason.CANDIDATE_PAYLOAD_MISMATCH,
            )
        rebuilt = build_main_agent_memory_candidate_payload(
            runtime_session_id=self.runtime_session_id,
            tool_call_id=call_id,
            tool_name=tool_call.tool_name,
            arguments=thaw_json(tool_call.parsed_arguments),
        )
        if (
            main_agent_memory_candidate_entry_id(
                runtime_session_id=self.runtime_session_id,
                tool_call_id=call_id,
            )
            != candidate.entry_id
            or rebuilt != candidate.payload
        ):
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_CANDIDATE_PAYLOAD_MISMATCH,
                CandidateEvidenceRejectionReason.CANDIDATE_PAYLOAD_MISMATCH,
                observed=(candidate_payload_fingerprint(rebuilt),),
            )
        model_event_refs = self._entry_event_refs(
            assistant_entry,
            through_sequence=authority.ledger_through_sequence,
        )
        disposition_refs = tuple(
            ref
            for ref in model_event_refs
            if ref.stable_identity.event_type
            == EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED.value
        )
        projection_refs = tuple(
            ref
            for ref in model_event_refs
            if ref.stable_identity.event_type
            == EventType.MODEL_CALL_TERMINAL_PROJECTION_COMMITTED.value
        )
        if len(disposition_refs) != 1 or len(projection_refs) != 1:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        disposition_event = self._decode_exact(disposition_refs[0])
        if (
            not isinstance(disposition_event, ModelCallControlDispositionResolvedEvent)
            or disposition_event.disposition is not ModelCallControlDisposition.ACCEPTED
        ):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        tool_event_refs = self._entry_event_refs(
            result_entry,
            through_sequence=authority.ledger_through_sequence,
        )
        if len(tool_event_refs) != 1:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        tool_document = authority.document_view.resolve(result_entry.projection_reference)
        if not isinstance(tool_document.payload, ToolTerminalProjectionPayloadFact):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        quote_semantic, quote_attribution = self._canonical_quote(
            candidate,
            entries=entries,
            through_sequence=authority.ledger_through_sequence,
        )
        semantic = build_frozen_fact(
            MainAgentToolGovernanceSourceSemanticFact,
            schema_version="main_agent_tool_governance_source_semantic.v1",
            evidence_kind="main_agent_tool",
            candidate_payload_semantic_fingerprint=(
                _candidate_payload_semantic(candidate).payload_semantic_fingerprint
            ),
            model_control_acceptance="accepted",
            selected_tool_call_semantic=tool_call,
            tool_result_semantic=tool_document.semantic_identity,
            quoted_evidence_semantic=quote_semantic,
        )
        producer_refs = tuple(sorted((*projection_refs, *disposition_refs, *tool_event_refs), key=lambda item: item.sequence))
        for reference in producer_refs:
            producer_event = self._decode_exact(reference)
            if not _event_matches_candidate_source(producer_event, candidate):
                raise _AuthorityUntrusted(
                    GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
                )
        attribution = build_frozen_fact(
            GovernanceSourceEvidenceAttributionFact,
            schema_version="governance_source_evidence_attribution.v1",
            evidence_kind="main_agent_tool",
            evidence_semantic_fingerprint=semantic.semantic_fingerprint,
            runtime_session_id=self.runtime_session_id,
            authority_ledger_through_sequence=authority.ledger_through_sequence,
            candidate_entry_id=candidate.entry_id,
            producer_event_references=producer_refs,
            model_terminal_projection_reference=model_ref,
            model_disposition_event_reference=disposition_refs[0],
            tool_terminal_projection_reference=result_entry.projection_reference,
            quoted_evidence_attributions=(
                (quote_attribution,) if quote_attribution is not None else ()
            ),
            source_artifact_references=(),
            producer_contract_fingerprints=(
                main_agent_memory_candidate_builder_contract().contract_fingerprint,
            ),
        )
        del model_document
        return semantic, attribution

    def _reflection(
        self,
        candidate: PooledMemoryCandidate,
        authority: GovernanceTranscriptAuthoritySnapshot,
    ) -> tuple[
        ReflectionGovernanceSourceSemanticFact,
        GovernanceSourceEvidenceAttributionFact,
    ]:
        high_water = authority.ledger_through_sequence
        event, event_ref = self._required_source_event(
            candidate,
            expected_type=MemoryReflectionCompletedEvent,
            high_water=high_water,
        )
        model_end_raw = self._required_referenced_raw(
            event.reflection_model_call_end_event_identity.event_id,
            high_water=high_water,
        )
        model_end_ref = _stored_event_ref(model_end_raw)
        if model_end_ref.stable_identity != event.reflection_model_call_end_event_identity:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        model_end = model_end_raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if (
            not isinstance(model_end, ModelCallEndEvent)
            or model_end.outcome != "completed"
            or model_end.resolved_model_call_id
            != event.resolved_call.resolved_model_call_id
            or model_end.target_fingerprint
            != event.resolved_call.target.target_fingerprint
            or model_end.usage_status != event.usage_status
            or model_end.usage != event.usage
            or model_end.reported_model_id != event.reported_model_id
        ):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        matches = tuple(
            item
            for item in event.ordered_candidate_attributions
            if item.candidate_entry_id == candidate.entry_id
        )
        if len(matches) != 1:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_PRODUCER_OMITS_CANDIDATE,
                CandidateEvidenceRejectionReason.PRODUCER_OMITS_CANDIDATE,
            )
        item = matches[0]
        if item.candidate_payload != candidate.payload:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_CANDIDATE_PAYLOAD_MISMATCH,
                CandidateEvidenceRejectionReason.CANDIDATE_PAYLOAD_MISMATCH,
            )
        quote_semantics: list[GovernanceQuotedEvidenceSemanticFact] = []
        quote_attributions: list[GovernanceQuotedEvidenceAttributionFact] = []
        for index in item.ordered_quoted_evidence_indices:
            if index >= len(event.quoted_evidence):
                raise _CandidateSourceInvalid(
                    GovernanceEvidenceBuildReason.INVALID_PRODUCER_OMITS_CANDIDATE,
                    CandidateEvidenceRejectionReason.PRODUCER_OMITS_CANDIDATE,
                )
            quote_text = event.quoted_evidence[index]
            canonical = self._match_canonical_user_quote(
                quote_text,
                candidate=candidate,
                entries=authority.reducer_evidence_snapshot.stable_entries,
                through_sequence=high_water,
            )
            if canonical is None:
                quote = _quote_semantic(
                    text=quote_text,
                    quote_kind="reflection_reported",
                    verification_status="origin_reported",
                )
                quote_attribution = build_frozen_fact(
                    GovernanceQuotedEvidenceAttributionFact,
                    schema_version="governance_quoted_evidence_attribution.v1",
                    quote_semantic_fingerprint=quote.semantic_fingerprint,
                    source_entry_ref=None,
                    source_artifact_ref=None,
                    start_char=None,
                    end_char=None,
                    producer_event_reference=event_ref,
                )
            else:
                quote, quote_attribution = canonical
            quote_semantics.append(quote)
            quote_attributions.append(quote_attribution)
        semantic = build_frozen_fact(
            ReflectionGovernanceSourceSemanticFact,
            schema_version="reflection_governance_source_semantic.v1",
            evidence_kind="reflection",
            candidate_payload_semantic_fingerprint=(
                _candidate_payload_semantic(candidate).payload_semantic_fingerprint
            ),
            reflection_policy_id="pulsara.memory_reflection",
            reflection_policy_version="1",
            reflection_policy_contract_fingerprint=(
                event.reflection_policy_contract_fingerprint
            ),
            reflection_model_result_semantic_fingerprint=(
                event.reflection_model_result_semantic_fingerprint
            ),
            candidate_index=item.candidate_index,
            ordered_quoted_evidence_semantics=tuple(quote_semantics),
        )
        attribution = build_frozen_fact(
            GovernanceSourceEvidenceAttributionFact,
            schema_version="governance_source_evidence_attribution.v1",
            evidence_kind="reflection",
            evidence_semantic_fingerprint=semantic.semantic_fingerprint,
            runtime_session_id=self.runtime_session_id,
            authority_ledger_through_sequence=high_water,
            candidate_entry_id=candidate.entry_id,
            producer_event_references=(event_ref,),
            model_terminal_projection_reference=None,
            model_disposition_event_reference=None,
            tool_terminal_projection_reference=None,
            quoted_evidence_attributions=tuple(quote_attributions),
            source_artifact_references=(),
            producer_contract_fingerprints=(
                event.reflection_policy_contract_fingerprint,
            ),
        )
        return semantic, attribution

    def _compaction(
        self,
        candidate: PooledMemoryCandidate,
        high_water: int,
    ) -> tuple[
        CompactionGovernanceSourceSemanticFact,
        GovernanceSourceEvidenceAttributionFact,
    ]:
        event, proposed_ref = self._required_source_event(
            candidate,
            expected_type=ContextCompactionMemoryCandidatesProposedEvent,
            high_water=high_water,
        )
        matches = tuple(
            item
            for item in event.ordered_candidate_attributions
            if item.candidate_entry_id == candidate.entry_id
        )
        if len(matches) != 1:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_PRODUCER_OMITS_CANDIDATE,
                CandidateEvidenceRejectionReason.PRODUCER_OMITS_CANDIDATE,
            )
        item = matches[0]
        if item.candidate_payload != candidate.payload:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_CANDIDATE_PAYLOAD_MISMATCH,
                CandidateEvidenceRejectionReason.CANDIDATE_PAYLOAD_MISMATCH,
            )
        completed_raw = self._required_referenced_raw(
            event.completed_compaction_event_identity.event_id,
            high_water=high_water,
        )
        completed_ref = _stored_event_ref(completed_raw)
        if completed_ref.stable_identity != event.completed_compaction_event_identity:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        completed = completed_raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if (
            not isinstance(completed, ContextCompactionCompletedEvent)
            or completed.id != event.source_event_id
            or completed_raw.sequence != event.source_event_sequence
            or completed.compaction_id != event.compaction_id
            or completed.summary_artifact_id != event.summary_artifact_id
            or item.raw_candidate_index >= event.attempted_count
            or not _event_matches_candidate_source(event, candidate)
            or not _event_matches_candidate_source(completed, candidate)
        ):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        try:
            summary = self.archive.get_text(
                event.summary_artifact_id,
                session_id=self.runtime_session_id,
            )
            info = self.archive.get_info(
                event.summary_artifact_id,
                session_id=self.runtime_session_id,
            )
        except TimeoutError as exc:
            raise _EvidenceNotReady(
                GovernanceEvidenceBuildReason.WAIT_ARTIFACT_CONFIRMATION
            ) from exc
        except (KeyError, ValueError) as exc:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
            ) from exc
        encoded = summary.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != event.summary_content_sha256 or len(encoded) != event.summary_content_bytes:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
            )
        artifact_ref = build_frozen_fact(
            GovernanceEvidenceArtifactReferenceFact,
            schema_version="governance_evidence_artifact_reference.v1",
            artifact_kind="compaction_summary",
            artifact_id=event.summary_artifact_id,
            media_type=info.media_type,
            content_sha256=digest,
            content_bytes=len(encoded),
            artifact_contract_id="pulsara.compaction.summary",
            artifact_contract_version="1",
            artifact_contract_fingerprint=event.extractor_contract.contract_fingerprint,
        )
        payload_fp = _candidate_payload_semantic(candidate).payload_semantic_fingerprint
        semantic = build_frozen_fact(
            CompactionGovernanceSourceSemanticFact,
            schema_version="compaction_governance_source_semantic.v1",
            evidence_kind="compaction",
            candidate_payload_semantic_fingerprint=payload_fp,
            summary_content_sha256=digest,
            summary_content_semantic_fingerprint=context_fingerprint(
                "compaction-summary-content-semantic:v1", summary
            ),
            extractor_contract=event.extractor_contract,
            raw_candidate_index=item.raw_candidate_index,
            canonical_parsed_candidate_payload_fingerprint=payload_fp,
            intent_fingerprint=item.intent_fingerprint,
            quoted_evidence_semantic=None,
        )
        attribution = build_frozen_fact(
            GovernanceSourceEvidenceAttributionFact,
            schema_version="governance_source_evidence_attribution.v1",
            evidence_kind="compaction",
            evidence_semantic_fingerprint=semantic.semantic_fingerprint,
            runtime_session_id=self.runtime_session_id,
            authority_ledger_through_sequence=high_water,
            candidate_entry_id=candidate.entry_id,
            producer_event_references=tuple(
                sorted((completed_ref, proposed_ref), key=lambda ref: ref.sequence)
            ),
            model_terminal_projection_reference=None,
            model_disposition_event_reference=None,
            tool_terminal_projection_reference=None,
            quoted_evidence_attributions=(),
            source_artifact_references=(artifact_ref,),
            producer_contract_fingerprints=(
                event.extractor_contract.contract_fingerprint,
            ),
        )
        return semantic, attribution

    def _prompt_projection(
        self,
        *,
        candidate: PooledMemoryCandidate,
        semantic: GovernanceSourceEvidenceSemanticFact,
        attribution: GovernanceSourceEvidenceAttributionFact,
        authority: GovernanceTranscriptAuthoritySnapshot,
    ) -> GovernanceEvidencePromptProjectionFact:
        evidence: list[GovernancePromptEvidenceTextFact] = []
        truncations: list[str] = []
        artifacts = attribution.source_artifact_references[
            : self.prompt_contract.max_artifact_refs_per_candidate
        ]
        if isinstance(semantic, MainAgentToolGovernanceSourceSemanticFact):
            quote = semantic.quoted_evidence_semantic
            if quote is not None:
                projected_quote = _head_tail(
                    quote.text,
                    self.prompt_contract.max_quote_characters_per_candidate,
                )
                if projected_quote != quote.text:
                    truncations.append("verified_user_quote:character_limit")
                evidence.append(
                    _prompt_text(
                        "verified_user_quote",
                        projected_quote,
                        quote.semantic_fingerprint,
                        "canonical_match",
                    )
                )
            raw_arguments = semantic.selected_tool_call_semantic.raw_arguments_json
            projected_arguments = _head_tail(raw_arguments, 2_000)
            if projected_arguments != raw_arguments:
                truncations.append("selected_tool_arguments:character_limit")
            evidence.append(
                _prompt_text(
                    "selected_tool_arguments",
                    projected_arguments,
                    semantic.selected_tool_call_semantic.semantic_fingerprint,
                    "canonical_match",
                )
            )
            result_text = self._tool_result_prompt_text(
                attribution.tool_terminal_projection_reference,
                authority,
            )
            projected_result = _head_tail(
                result_text,
                self.prompt_contract.max_tool_result_characters_per_candidate,
            )
            if projected_result != result_text:
                truncations.append("tool_result_essential:character_limit")
            evidence.append(
                _prompt_text(
                    "tool_result_essential",
                    projected_result,
                    semantic.tool_result_semantic.semantic_fingerprint,
                    "canonical_match",
                )
            )
            tool_name = semantic.selected_tool_call_semantic.tool_name
            result_state = (
                semantic.tool_result_semantic.canonical_result_block_semantic.result_state.value
            )
            timing_fp = context_fingerprint(
                "governance-tool-observation-timing:v1",
                semantic.tool_result_semantic.observation_timing.model_dump(mode="json"),
            )
            accepted = True
        elif isinstance(semantic, ReflectionGovernanceSourceSemanticFact):
            for quote in semantic.ordered_quoted_evidence_semantics:
                projected_quote = _head_tail(
                    quote.text,
                    self.prompt_contract.max_quote_characters_per_candidate,
                )
                if projected_quote != quote.text:
                    truncations.append("reflection_report:character_limit")
                evidence.append(
                    _prompt_text(
                        "reflection_report",
                        projected_quote,
                        quote.semantic_fingerprint,
                        quote.verification_status,
                    )
                )
            tool_name = result_state = timing_fp = None
            accepted = False
        else:
            summary = self._verified_artifact_text(
                attribution.source_artifact_references[0]
            )
            projected_summary = _head_tail(
                summary,
                self.prompt_contract.max_assistant_text_characters_per_candidate,
            )
            if projected_summary != summary:
                truncations.append("compaction_summary:character_limit")
            evidence.append(
                _prompt_text(
                    "compaction_summary",
                    projected_summary,
                    semantic.summary_content_semantic_fingerprint,
                    "origin_reported",
                )
            )
            tool_name = result_state = timing_fp = None
            accepted = False
        payload_values = {
            "schema_version": "governance_candidate_prompt_payload.v1",
            "candidate_entry_id": candidate.entry_id,
            "candidate_payload_semantic_fingerprint": (
                _candidate_payload_semantic(candidate).payload_semantic_fingerprint
            ),
            "canonical_candidate_payload": candidate.payload,
            "evidence_kind": semantic.evidence_kind,
            "accepted": accepted,
            "ordered_evidence_texts": tuple(evidence[:16]),
            "tool_name": tool_name,
            "tool_result_state": result_state,
            "observation_timing_fingerprint": timing_fp,
            "artifact_references": tuple(artifacts),
        }
        payload_bytes = len(
            json.dumps(
                {
                    "candidate_entry_id": candidate.entry_id,
                    "candidate_payload_semantic_fingerprint": payload_values[
                        "candidate_payload_semantic_fingerprint"
                    ],
                    "canonical_candidate_payload": candidate.payload.model_dump(
                        mode="json"
                    ),
                    "evidence_kind": semantic.evidence_kind,
                    "accepted": accepted,
                    "ordered_evidence_texts": [
                        item.model_dump(mode="json") for item in evidence[:16]
                    ],
                    "tool_name": tool_name,
                    "tool_result_state": result_state,
                    "observation_timing_fingerprint": timing_fp,
                    "artifact_references": [
                        item.model_dump(mode="json") for item in artifacts
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        if payload_bytes > self.prompt_contract.max_candidate_projection_utf8_bytes:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        payload = build_frozen_fact(
            GovernanceCandidatePromptPayloadFact,
            **payload_values,
            payload_utf8_bytes=payload_bytes,
        )
        included = tuple(dict.fromkeys(item.field_code for item in evidence))
        omitted = tuple(code for code in _PROMPT_FIELDS if code not in included)
        return build_frozen_fact(
            GovernanceEvidencePromptProjectionFact,
            schema_version="governance_evidence_prompt_projection.v1",
            source_evidence_semantic_fingerprint=semantic.semantic_fingerprint,
            projection_contract_id=self.prompt_contract.policy_id,
            projection_contract_version=self.prompt_contract.policy_version,
            projection_contract_fingerprint=self.prompt_contract.contract_fingerprint,
            model_visible_payload=payload,
            included_field_codes=included,
            omitted_field_codes=omitted,
            truncation_reason_codes=tuple(dict.fromkeys(truncations)),
            projected_utf8_bytes=payload.payload_utf8_bytes,
        )

    def _tool_result_prompt_text(
        self,
        reference: TerminalProjectionReferenceFact | None,
        authority: GovernanceTranscriptAuthoritySnapshot,
    ) -> str:
        if reference is None:
            return ""
        document = authority.document_view.resolve(reference)
        if not isinstance(document.payload, ToolTerminalProjectionPayloadFact):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        parts: list[str] = []
        for block in document.payload.canonical_result_block.content_blocks:
            content = block.content
            if isinstance(content, TerminalInlineContentFact):
                parts.append(content.text)
            elif isinstance(content, TerminalArtifactContentReferenceFact):
                parts.append(
                    f"[artifact {content.artifact_id} {content.semantic_identity.media_type} "
                    f"{content.semantic_identity.utf8_bytes} bytes]"
                )
        if not parts:
            parts.append(
                "result_state="
                + document.semantic_identity.canonical_result_block_semantic.result_state.value
            )
        return "\n".join(parts)

    def _canonical_quote(
        self,
        candidate: PooledMemoryCandidate,
        *,
        entries: tuple[TranscriptProjectionLeafEntryFact, ...],
        through_sequence: int,
    ) -> tuple[
        GovernanceQuotedEvidenceSemanticFact | None,
        GovernanceQuotedEvidenceAttributionFact | None,
    ]:
        locator = candidate.quoted_evidence_locator
        if locator is None or locator.locator_kind != "canonical_user_message_span":
            return None, None
        matches = tuple(
            entry
            for entry in entries
            if isinstance(entry, TranscriptMessageLeafEntryFact)
            and entry.attribution.message_id == locator.source_message_id
            and entry.semantic_identity.message_provider_semantic_identity.role == "user"
        )
        if len(matches) != 1:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_SOURCE_CALL_MISSING,
                CandidateEvidenceRejectionReason.SOURCE_CALL_MISSING,
            )
        entry = matches[0]
        if (
            entry.attribution.run_id != candidate.source_run_id
            or entry.attribution.turn_id != candidate.source_turn_id
            or entry.attribution.reply_id != candidate.source_reply_id
        ):
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
            )
        text = self._message_text(entry)
        assert locator.start_char is not None and locator.end_char is not None
        if locator.end_char > len(text):
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_CANDIDATE_PAYLOAD_MISMATCH,
                CandidateEvidenceRejectionReason.CANDIDATE_PAYLOAD_MISMATCH,
            )
        quote_text = text[locator.start_char : locator.end_char]
        if (
            hashlib.sha256(quote_text.encode("utf-8")).hexdigest()
            != locator.quoted_text_sha256
            or quote_text != candidate.user_quote
        ):
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_CANDIDATE_PAYLOAD_MISMATCH,
                CandidateEvidenceRejectionReason.CANDIDATE_PAYLOAD_MISMATCH,
            )
        semantic = _quote_semantic(
            text=quote_text,
            quote_kind="canonical_user_span",
            verification_status="canonical_match",
        )
        entry_ref = _leaf_entry_ref(
            self.runtime_session_id,
            entry,
            self._entry_event_refs(entry, through_sequence=through_sequence),
        )
        attribution = build_frozen_fact(
            GovernanceQuotedEvidenceAttributionFact,
            schema_version="governance_quoted_evidence_attribution.v1",
            quote_semantic_fingerprint=semantic.semantic_fingerprint,
            source_entry_ref=entry_ref,
            source_artifact_ref=None,
            start_char=locator.start_char,
            end_char=locator.end_char,
            producer_event_reference=None,
        )
        return semantic, attribution

    def _match_canonical_user_quote(
        self,
        quote_text: str,
        *,
        candidate: PooledMemoryCandidate,
        entries: tuple[TranscriptProjectionLeafEntryFact, ...],
        through_sequence: int,
    ) -> tuple[
        GovernanceQuotedEvidenceSemanticFact,
        GovernanceQuotedEvidenceAttributionFact,
    ] | None:
        if not quote_text:
            return None
        matches: list[
            tuple[
                TranscriptMessageLeafEntryFact,
                int,
            ]
        ] = []
        for entry in entries:
            if (
                not isinstance(entry, TranscriptMessageLeafEntryFact)
                or entry.semantic_identity.message_provider_semantic_identity.role
                != "user"
                or entry.attribution.run_id != candidate.source_run_id
                or entry.attribution.turn_id != candidate.source_turn_id
                or entry.attribution.reply_id != candidate.source_reply_id
            ):
                continue
            text = self._message_text(entry)
            start = 0
            while True:
                start = text.find(quote_text, start)
                if start < 0:
                    break
                matches.append((entry, start))
                start += max(1, len(quote_text))
        # Reflection reports text rather than an exact source span.  Upgrade
        # it to replacement authority only when the canonical match is unique.
        if len(matches) != 1:
            return None
        entry, start = matches[0]
        semantic = _quote_semantic(
            text=quote_text,
            quote_kind="canonical_user_span",
            verification_status="canonical_match",
        )
        attribution = build_frozen_fact(
            GovernanceQuotedEvidenceAttributionFact,
            schema_version="governance_quoted_evidence_attribution.v1",
            quote_semantic_fingerprint=semantic.semantic_fingerprint,
            source_entry_ref=_leaf_entry_ref(
                self.runtime_session_id,
                entry,
                self._entry_event_refs(
                    entry,
                    through_sequence=through_sequence,
                ),
            ),
            source_artifact_ref=None,
            start_char=start,
            end_char=start + len(quote_text),
            producer_event_reference=None,
        )
        return semantic, attribution

    def _message_text(self, entry: TranscriptMessageLeafEntryFact) -> str:
        content = entry.content
        if isinstance(content, InlineNormalizedMessageContentFact):
            return "\n".join(
                block.provider_semantic_identity.text
                for block in content.blocks
                if block.provider_semantic_identity.block_kind == "text"
            )
        if isinstance(content, NormalizedMessageContentArtifactReferenceFact):
            try:
                raw = self.archive.get_text(
                    content.document_artifact_id,
                    session_id=self.runtime_session_id,
                )
                document = NormalizedMessageContentArtifactFact.model_validate_json(raw)
            except Exception as exc:
                raise _AuthorityUntrusted(
                    GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
                ) from exc
            encoded = raw.encode("utf-8")
            if (
                document.fact_fingerprint != content.document_fact_fingerprint
                or document.provider_semantic_identity
                != content.provider_semantic_identity
                or hashlib.sha256(encoded).hexdigest()
                != content.document_sha256.removeprefix("sha256:")
                or len(encoded) != content.document_byte_count
            ):
                raise _AuthorityUntrusted(
                    GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
                )
            return "\n".join(
                block.provider_semantic_identity.text
                for block in document.blocks
                if block.provider_semantic_identity.block_kind == "text"
            )
        raise _AuthorityUntrusted(
            GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
        )

    def _entry_event_refs(
        self,
        entry: TranscriptProjectionLeafEntryFact,
        *,
        through_sequence: int,
    ) -> tuple[GovernanceStoredEventReferenceFact, ...]:
        if len(entry.source_event_refs) > _MAX_EXACT_EVENT_REFS:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        raw = self.event_log.read_raw_events_by_id(
            tuple(ref.event_id for ref in entry.source_event_refs)
        )
        if len(raw) != len(entry.source_event_refs):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
            )
        by_id = {item.event_id: item for item in raw}
        result: list[GovernanceStoredEventReferenceFact] = []
        for expected in entry.source_event_refs:
            envelope = by_id[expected.event_id]
            if (
                envelope.sequence != expected.sequence
                or envelope.payload_fingerprint != expected.payload_fingerprint
                or envelope.event_type != expected.event_type
                or envelope.sequence > through_sequence
            ):
                raise _AuthorityUntrusted(
                    GovernanceEvidenceBuildReason.UNTRUSTED_REDUCER_EVENT_MISMATCH
                )
            result.append(_stored_event_ref(envelope))
        return tuple(result)

    def _required_source_event(
        self,
        candidate: PooledMemoryCandidate,
        *,
        expected_type,
        high_water: int,
    ):
        if candidate.source_event_id is None:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
            )
        rows = self.event_log.read_raw_events_by_id((candidate.source_event_id,))
        if not rows:
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_SOURCE_CALL_MISSING,
                CandidateEvidenceRejectionReason.SOURCE_CALL_MISSING,
            )
        raw = rows[0]
        if raw.sequence > high_water:
            raise _EvidenceNotReady(GovernanceEvidenceBuildReason.WAIT_REDUCER_BEHIND)
        event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(event, expected_type):
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
            )
        if not _event_matches_candidate_source(event, candidate):
            raise _CandidateSourceInvalid(
                GovernanceEvidenceBuildReason.INVALID_ORIGIN_FIELDS,
                CandidateEvidenceRejectionReason.ORIGIN_FIELDS_INVALID,
            )
        return event, _stored_event_ref(raw)

    def _decode_exact(
        self, reference: GovernanceStoredEventReferenceFact
    ) -> AgentEvent:
        raw = self._required_referenced_raw(
            reference.stable_identity.event_id,
            high_water=reference.sequence,
        )
        if _stored_event_ref(raw) != reference:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        return raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)

    def _required_referenced_raw(
        self,
        event_id: str,
        *,
        high_water: int,
    ) -> RawStoredEventEnvelope:
        rows = self.event_log.read_raw_events_by_id((event_id,))
        if not rows:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        row = rows[0]
        if row.sequence > high_water:
            raise _EvidenceNotReady(GovernanceEvidenceBuildReason.WAIT_REDUCER_BEHIND)
        return row

    def _verified_artifact_text(
        self,
        reference: GovernanceEvidenceArtifactReferenceFact,
    ) -> str:
        try:
            text = self.archive.get_text(
                reference.artifact_id,
                session_id=self.runtime_session_id,
            )
            info = self.archive.get_info(
                reference.artifact_id,
                session_id=self.runtime_session_id,
            )
        except Exception as exc:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
            ) from exc
        encoded = text.encode("utf-8")
        if (
            hashlib.sha256(encoded).hexdigest() != reference.content_sha256
            or len(encoded) != reference.content_bytes
            or info.media_type != reference.media_type
        ):
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ARTIFACT_HASH
            )
        return text

    def _source_run_terminal(self, run_id: str, high_water: int) -> bool:
        snapshot = self.event_log.read_raw_events_by_types(
            (EventType.RUN_END.value,),
            run_ids=(run_id,),
            through_sequence=high_water,
            max_events=2,
            max_payload_bytes=512 * 1024,
        )
        if len(snapshot.events) > 1:
            raise _AuthorityUntrusted(
                GovernanceEvidenceBuildReason.UNTRUSTED_ID_PAYLOAD_CONFLICT
            )
        return bool(snapshot.events)

    def _invalid(
        self,
        candidate: PooledMemoryCandidate,
        *,
        high_water: int,
        reason: GovernanceEvidenceBuildReason,
        rejection: CandidateEvidenceRejectionReason,
        observed: tuple[str, ...] = (),
    ) -> GovernanceEvidencePreparation:
        record = build_frozen_fact(
            MemoryCandidateEvidenceRejectedRecord,
            schema_version="memory_candidate_evidence_rejected.v1",
            candidate_entry_id=candidate.entry_id,
            source_high_water=high_water,
            stable_reason_code=rejection,
            observed_source_fingerprints=observed,
        )
        return GovernanceEvidencePreparation(
            result=_build_result(
                candidate.entry_id,
                high_water,
                GovernanceEvidenceBuildStatus.CANDIDATE_SOURCE_INVALID,
                reason,
            ),
            rejection=record,
        )


def default_governance_prompt_projection_contract(
) -> GovernanceEvidencePromptProjectionContractFact:
    return build_frozen_fact(
        GovernanceEvidencePromptProjectionContractFact,
        schema_version="governance_evidence_prompt_projection_contract.v1",
        policy_id="pulsara.governance_evidence_prompt",
        policy_version="1",
        max_quote_characters_per_candidate=2_000,
        max_assistant_text_characters_per_candidate=2_000,
        max_tool_result_characters_per_candidate=2_000,
        max_artifact_refs_per_candidate=8,
        max_candidates_per_batch=20,
        max_related_memories_per_candidate=5,
        max_candidate_projection_utf8_bytes=16 * 1024,
        max_batch_projection_utf8_bytes=128 * 1024,
        truncation_policy="typed_head_tail_v1",
        essential_envelope_contract_fingerprint=context_fingerprint(
            "governance-essential-envelope-contract:v1",
            ("typed-fields", "head-tail", "artifact-reference"),
        ),
    )


def _candidate_payload_semantic(
    candidate: PooledMemoryCandidate,
) -> GovernanceCandidatePayloadSemanticFact:
    payload_json = candidate.payload.model_dump(mode="json")
    payload_bytes = json.dumps(
        payload_json,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return build_frozen_fact(
        GovernanceCandidatePayloadSemanticFact,
        schema_version="governance_candidate_payload_semantic.v1",
        candidate_origin=candidate.origin.value,
        payload_kind=candidate.payload.payload_kind,
        canonical_candidate_payload=candidate.payload,
        canonical_payload_utf8_bytes=len(payload_bytes),
        intent_fingerprint=candidate.intent_fingerprint,
    )


def _candidate_attribution(
    candidate: PooledMemoryCandidate,
    *,
    source_event_reference: GovernanceStoredEventReferenceFact | None,
    source_artifact_reference: GovernanceEvidenceArtifactReferenceFact | None,
) -> GovernanceCandidateAttributionFact:
    return build_frozen_fact(
        GovernanceCandidateAttributionFact,
        schema_version="governance_candidate_attribution.v1",
        entry_id=candidate.entry_id,
        runtime_session_id=candidate.source_session_id,
        source_run_id=candidate.source_run_id,
        source_turn_id=candidate.source_turn_id,
        source_reply_id=candidate.source_reply_id,
        source_tool_call_id=candidate.source_tool_call_id,
        source_event_reference=source_event_reference,
        source_artifact_reference=source_artifact_reference,
        quoted_evidence_locator=candidate.quoted_evidence_locator,
        created_at_utc=candidate.created_at,
    )


def _event_matches_candidate_source(
    event: AgentEvent,
    candidate: PooledMemoryCandidate,
) -> bool:
    return (
        event.run_id == candidate.source_run_id
        and event.turn_id == candidate.source_turn_id
        and event.reply_id == candidate.source_reply_id
    )


def _stored_event_ref(
    envelope: RawStoredEventEnvelope,
) -> GovernanceStoredEventReferenceFact:
    event = envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    return build_frozen_fact(
        GovernanceStoredEventReferenceFact,
        schema_version="governance_stored_event_reference.v1",
        stable_identity=stable_event_identity(
            event,
            runtime_session_id=envelope.runtime_session_id,
        ),
        sequence=envelope.sequence,
        stored_envelope_fingerprint=envelope.envelope_fingerprint,
    )


def _leaf_entry_ref(
    runtime_session_id: str,
    entry: TranscriptProjectionLeafEntryFact,
    source_refs: tuple[GovernanceStoredEventReferenceFact, ...],
) -> TranscriptProjectionLeafEntryReferenceFact:
    context_refs = tuple(
        ContextEventReferenceFact(
            runtime_session_id=ref.stable_identity.runtime_session_id,
            event_id=ref.stable_identity.event_id,
            sequence=ref.sequence,
            event_type=ref.stable_identity.event_type,
            payload_fingerprint=ref.stable_identity.payload_fingerprint,
        )
        for ref in source_refs
    )
    return build_frozen_fact(
        TranscriptProjectionLeafEntryReferenceFact,
        schema_version="transcript_projection_leaf_entry_reference.v2",
        runtime_session_id=runtime_session_id,
        entry_kind=entry.entry_kind,
        ordinal=int(entry.ordinal.value_hex, 16),
        entry_semantic_fingerprint=entry.semantic_identity.semantic_fingerprint,
        entry_fact_fingerprint=entry.fact_fingerprint,
        source_event_references=context_refs,
    )


def _quote_semantic(
    *, text: str, quote_kind: str, verification_status: str
) -> GovernanceQuotedEvidenceSemanticFact:
    encoded = text.encode("utf-8")
    return build_frozen_fact(
        GovernanceQuotedEvidenceSemanticFact,
        schema_version="governance_quoted_evidence_semantic.v1",
        quote_kind=quote_kind,
        text=text,
        text_utf8_bytes=len(encoded),
        text_sha256=hashlib.sha256(encoded).hexdigest(),
        verification_status=verification_status,
    )


def _prompt_text(
    field_code: str,
    text: str,
    source_semantic_fingerprint: str,
    verification_status: str,
) -> GovernancePromptEvidenceTextFact:
    return build_frozen_fact(
        GovernancePromptEvidenceTextFact,
        schema_version="governance_prompt_evidence_text.v1",
        field_code=field_code,
        text=text,
        source_semantic_fingerprint=source_semantic_fingerprint,
        verification_status=verification_status,
    )


def _build_result(
    candidate_entry_id: str,
    high_water: int,
    status: GovernanceEvidenceBuildStatus,
    reason: GovernanceEvidenceBuildReason,
    *,
    evidence_semantic: GovernanceSourceEvidenceSemanticFact | None = None,
    evidence_attribution: GovernanceSourceEvidenceAttributionFact | None = None,
    retry_after_seconds: float | None = None,
) -> GovernanceEvidenceBuildResult:
    return build_frozen_fact(
        GovernanceEvidenceBuildResult,
        schema_version="governance_evidence_build_result.v1",
        status=status,
        candidate_entry_id=candidate_entry_id,
        source_high_water=high_water,
        evidence_semantic=evidence_semantic,
        evidence_attribution=evidence_attribution,
        stable_reason_code=reason,
        retry_after_seconds=retry_after_seconds,
    )


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 32:
        return text[:limit]
    head = (limit - 17) // 2
    tail = limit - 17 - head
    return text[:head] + "\n...[omitted]...\n" + text[-tail:]


class _EvidenceNotReady(RuntimeError):
    def __init__(self, reason: GovernanceEvidenceBuildReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class _CandidateSourceInvalid(RuntimeError):
    def __init__(
        self,
        reason: GovernanceEvidenceBuildReason,
        rejection: CandidateEvidenceRejectionReason,
        *,
        observed: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.rejection = rejection
        self.observed = observed
        super().__init__(reason.value)


class _AuthorityUntrusted(RuntimeError):
    def __init__(self, reason: GovernanceEvidenceBuildReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


__all__ = [
    "GovernanceEvidencePreparation",
    "GovernanceSourceEvidenceBuilder",
    "default_governance_prompt_projection_contract",
]
