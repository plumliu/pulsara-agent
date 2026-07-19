"""Canonical transcript ordering and provider-prefix continuity contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from pulsara_agent.llm.input import LLMMessage, MessageRole
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.context import (
    CompactedWindowReferenceFact,
    TranscriptCompileInput,
    TranscriptMessageFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    CompactionReplacementSummarySemanticSourceFact,
    CompactionReplacementSummarySourceAttributionFact,
    DerivedToolResultMessageSemanticSourceFact,
    DerivedToolResultMessageSourceAttributionFact,
    DirectStableMessageSemanticSourceFact,
    DirectStableMessageSourceAttributionFact,
    LifecycleNoteSemanticSourceFact,
    LifecycleNoteSourceAttributionFact,
    ProviderCausalPlacementSemanticFact,
    ProviderCompactionRewriteAuthorityReferenceFact,
    ProviderInputCausalValidationResult,
    ProviderInputCausalValidationFailureReason,
    ProviderInputPhysicalPolicyFailureReason,
    ProviderInvocationClassificationAttributionFact,
    ProviderOrderedTranscriptProjectionFact,
    ProviderOrderedTranscriptProjectionIdentityFact,
    ProviderOrderedTranscriptUnitFact,
    ProviderProjectionPositionFact,
    ProviderTranscriptNodeIdentityFact,
    ProviderTranscriptSourceSelectionContractFact,
    ProviderTranscriptSourceSelectionRuleFact,
    ProviderWireMessageSemanticFact,
    ResolvedProviderInputCausalAndPhysicalPolicyFact,
)
from pulsara_agent.primitives.transcript_projection import (
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionLeafEntryReferenceFact,
    TranscriptToolPairLeafEntryFact,
    TranscriptToolResultLeafEntryFact,
)
from pulsara_agent.runtime.provider_input.materialization import (
    freeze_provider_message_fragment,
)
from pulsara_agent.runtime.provider_input.vector import VECTOR_CONTRACT_FINGERPRINT


WIRE_FRAMING_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-wire-message-framing-contract:v1",
    {
        "role": "frozen-from-provider-message",
        "content": "ordered-typed-blocks",
        "classification": "excluded",
    },
)
POSITION_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-projection-position-contract:v1",
    {
        "index": "zero-based",
        "predecessor": "previous-node-only",
        "successor": "derived-not-stored",
    },
)
CAUSAL_VALIDATION_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-causal-validation-contract:v2",
    {
        "order": "canonical-transcript-traversal",
        "tool": "call-before-result-before-consuming-assistant",
        "current_user": "single-placement",
        "successor": "derived-only",
    },
)
CONTINUATION_JOIN_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-accepted-continuation-projection-join-contract:v1",
    "exact-call-reply-terminal-disposition-to-one-projection-unit",
)


class ProviderOrderedProjectionError(ValueError):
    """The canonical transcript cannot produce one trusted provider projection."""


class ProviderInputPhysicalPolicyError(ProviderOrderedProjectionError):
    def __init__(
        self,
        reason: ProviderInputPhysicalPolicyFailureReason,
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LoweredProviderTranscriptMessage:
    """One provider message and the canonical transcript item that produced it."""

    message: LLMMessage
    source_message: TranscriptMessageFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    lowered_part_index: int
    source_kind: Literal["canonical_message", "rollup_observation"] = (
        "canonical_message"
    )


@dataclass(frozen=True, slots=True)
class PreparedOrderedProviderTranscriptProjection:
    projection: ProviderOrderedTranscriptProjectionFact
    identity: ProviderOrderedTranscriptProjectionIdentityFact
    lowered_messages: tuple[LLMMessage, ...]
    source_event_refs_by_message: tuple[tuple[ContextEventReferenceFact, ...], ...]


def build_default_provider_transcript_source_selection_contract(
) -> ProviderTranscriptSourceSelectionContractFact:
    rules = (
        build_frozen_fact(
            ProviderTranscriptSourceSelectionRuleFact,
            schema_version="provider_transcript_source_selection_rule.v1",
            canonical_entry_kind="message",
            eligible_message_segments=(
                "prior_history",
                "current_user",
                "current_run_tail",
            ),
            selection_outcome="emit_provider_unit",
            selected_source_kind="direct_stable_message",
            required_companion_entry_kinds=(),
        ),
        build_frozen_fact(
            ProviderTranscriptSourceSelectionRuleFact,
            schema_version="provider_transcript_source_selection_rule.v1",
            canonical_entry_kind="message",
            eligible_message_segments=("compaction_summary",),
            selection_outcome="emit_provider_unit",
            selected_source_kind="compaction_replacement_summary",
            required_companion_entry_kinds=(),
        ),
        build_frozen_fact(
            ProviderTranscriptSourceSelectionRuleFact,
            schema_version="provider_transcript_source_selection_rule.v1",
            canonical_entry_kind="message",
            eligible_message_segments=(
                "recovery_note",
                "terminal_lifecycle_note",
            ),
            selection_outcome="emit_provider_unit",
            selected_source_kind="lifecycle_note",
            required_companion_entry_kinds=(),
        ),
        build_frozen_fact(
            ProviderTranscriptSourceSelectionRuleFact,
            schema_version="provider_transcript_source_selection_rule.v1",
            canonical_entry_kind="tool_result_projection_ref",
            eligible_message_segments=(),
            selection_outcome="emit_provider_unit",
            selected_source_kind="derived_tool_result_message",
            required_companion_entry_kinds=("tool_pair",),
        ),
        build_frozen_fact(
            ProviderTranscriptSourceSelectionRuleFact,
            schema_version="provider_transcript_source_selection_rule.v1",
            canonical_entry_kind="tool_pair",
            eligible_message_segments=(),
            selection_outcome="companion_only",
            selected_source_kind=None,
            required_companion_entry_kinds=("tool_result_projection_ref",),
        ),
    )
    return build_frozen_fact(
        ProviderTranscriptSourceSelectionContractFact,
        schema_version="provider_transcript_source_selection_contract.v1",
        rules=rules,
    )


def build_default_resolved_causal_physical_policy(
    *,
    max_projection_canonical_bytes: int = 16 * 1024 * 1024,
    context_manifest_physical_policy_fingerprint: str | None = None,
) -> ResolvedProviderInputCausalAndPhysicalPolicyFact:
    """Build the V1 process contract shared by projection, planner and doctor."""

    return build_frozen_fact(
        ResolvedProviderInputCausalAndPhysicalPolicyFact,
        schema_version="resolved_provider_input_causal_physical_policy.v1",
        max_parallel_tool_calls_per_model_call=128,
        max_non_tool_transcript_units_per_operation=256,
        max_visible_causal_predecessors_per_unit=130,
        max_projection_units_per_manifest=16_384,
        max_projection_canonical_bytes_per_manifest=(
            max_projection_canonical_bytes
        ),
        max_generation_root_units=128,
        max_initial_generation_units=16_640,
        max_transcript_delta_units_per_append=384,
        max_context_frame_units_per_append=128,
        max_append_units=512,
        max_append_candidate_canonical_bytes=4 * 1024 * 1024,
        allow_multi_append_before_model_start=False,
        provider_input_vector_contract_fingerprint=VECTOR_CONTRACT_FINGERPRINT,
        terminal_projection_contract_fingerprint=context_fingerprint(
            "terminal-projection-contract-binding:v1", "terminal-projection.v1"
        ),
        context_manifest_physical_policy_fingerprint=(
            context_manifest_physical_policy_fingerprint
            or context_fingerprint(
                "context-manifest-physical-policy:v1",
                {"max_projection_canonical_bytes": max_projection_canonical_bytes},
            )
        ),
    )


def build_ordered_provider_transcript_projection(
    *,
    runtime_session_id: str,
    context_id: str,
    transcript: TranscriptCompileInput,
    lowered_messages: tuple[LoweredProviderTranscriptMessage, ...],
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    rendering_contract_fingerprint: str,
    policy: ResolvedProviderInputCausalAndPhysicalPolicyFact,
    source_selection_contract: ProviderTranscriptSourceSelectionContractFact
    | None = None,
) -> PreparedOrderedProviderTranscriptProjection:
    """Build the sole chronological provider-facing transcript projection."""

    contract = (
        source_selection_contract
        or build_default_provider_transcript_source_selection_contract()
    )
    if len(lowered_messages) > policy.max_projection_units_per_manifest:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_PROJECTION_UNIT_BOUND_EXCEEDED,
            "provider transcript projection exceeds unit bound",
        )
    message_entries, pair_entries, result_entries = _stable_entry_indexes(
        stable_entries
    )
    compacted = {item.summary_message_id: item for item in transcript.compacted_windows}
    normalized_by_id = {item.message_id: item for item in transcript.messages}
    if len(normalized_by_id) != len(transcript.messages):
        raise ProviderOrderedProjectionError("canonical transcript message IDs repeat")

    units: list[ProviderOrderedTranscriptUnitFact] = []
    call_node_by_id: dict[str, str] = {}
    unresolved_result_nodes: list[str] = []
    seen_current_user_ids: set[str] = set()
    for index, lowered in enumerate(lowered_messages):
        source_message = normalized_by_id.get(lowered.source_message.message_id)
        if source_message != lowered.source_message:
            raise ProviderOrderedProjectionError(
                "lowered provider message lacks exact canonical source"
            )
        source_semantic, source_attribution = _source_identity(
            runtime_session_id=runtime_session_id,
            source_message=source_message,
            provider_message=lowered.message,
            source_event_refs=lowered.source_event_refs,
            lowered_source_kind=lowered.source_kind,
            message_entries=message_entries,
            pair_entries=pair_entries,
            result_entries=result_entries,
            compacted=compacted,
            stable_entries=stable_entries,
            transcript=transcript,
        )
        _validate_selected_source_rule(
            contract=contract,
            source_message=source_message,
            provider_message=lowered.message,
            lowered_source_kind=lowered.source_kind,
            selected_source_kind=source_semantic.source_kind,
        )
        fragment = freeze_provider_message_fragment(lowered.message)
        wire = build_frozen_fact(
            ProviderWireMessageSemanticFact,
            schema_version="provider_wire_message_semantic.v1",
            provider_message=fragment,
            wire_framing_contract_fingerprint=WIRE_FRAMING_CONTRACT_FINGERPRINT,
        )
        node = build_frozen_fact(
            ProviderTranscriptNodeIdentityFact,
            schema_version="provider_transcript_node_identity.v1",
            source_identity_fingerprint=source_semantic.source_semantic_fingerprint,
            wire_semantic_fingerprint=wire.wire_semantic_fingerprint,
        )
        predecessor = (
            units[-1].causal_placement.node_identity.node_identity_fingerprint
            if units
            else None
        )
        position = build_frozen_fact(
            ProviderProjectionPositionFact,
            schema_version="provider_projection_position.v1",
            projection_index=index,
            predecessor_node_identity_fingerprint=predecessor,
            position_contract_fingerprint=POSITION_CONTRACT_FINGERPRINT,
        )
        visible_predecessors: list[str] = []
        if lowered.message.role is MessageRole.TOOL_RESULT:
            call_id = lowered.message.tool_call_id
            call_node = call_node_by_id.pop(call_id or "", None)
            if call_node is None:
                raise ProviderOrderedProjectionError(
                    "provider tool result precedes or lacks its tool call"
                )
            visible_predecessors.append(call_node)
            unresolved_result_nodes.append(node.node_identity_fingerprint)
        elif lowered.message.role is MessageRole.ASSISTANT:
            visible_predecessors.extend(unresolved_result_nodes)
            unresolved_result_nodes.clear()
        causal = build_frozen_fact(
            ProviderCausalPlacementSemanticFact,
            schema_version="provider_causal_placement_semantic.v1",
            source=source_semantic,
            node_identity=node,
            position=position,
            visible_causal_predecessor_node_identity_fingerprints=tuple(
                dict.fromkeys(visible_predecessors)
            ),
        )
        classification = (
            "lifecycle_note"
            if lowered.source_kind == "rollup_observation"
            else _classification(source_message.segment)
        )
        invocation = build_frozen_fact(
            ProviderInvocationClassificationAttributionFact,
            schema_version="provider_invocation_classification_attribution.v1",
            invocation_classification=classification,
            compile_context_id=context_id,
            section_id=f"transcript:{classification}",
        )
        unit_causal_semantic_fingerprint = context_fingerprint(
            "provider-ordered-transcript-unit-causal-semantic:v2",
            (wire.wire_semantic_fingerprint, causal.causal_semantic_fingerprint),
        )
        unit = build_frozen_fact(
            ProviderOrderedTranscriptUnitFact,
            schema_version="provider_ordered_transcript_unit.v2",
            wire_semantic=wire,
            causal_placement=causal,
            source_attribution=source_attribution,
            invocation_attribution=invocation,
            unit_causal_semantic_fingerprint=unit_causal_semantic_fingerprint,
        )
        if (
            classification == "current_user"
            and lowered.source_kind == "canonical_message"
        ):
            message_id = source_message.message_id
            if message_id in seen_current_user_ids:
                raise ProviderOrderedProjectionError(
                    "current user has more than one provider placement"
                )
            seen_current_user_ids.add(message_id)
        for call in lowered.message.tool_calls:
            if call.id in call_node_by_id:
                raise ProviderOrderedProjectionError(
                    "provider projection repeats a tool-call identity"
                )
            call_node_by_id[call.id] = node.node_identity_fingerprint
        units.append(unit)
    if unresolved_result_nodes:
        # A terminal tool-result tail is legal; it will be consumed by the next
        # assistant call.  The result still has its direct call edge.
        pass
    if len(seen_current_user_ids) != 1:
        raise ProviderOrderedProjectionError(
            "provider projection requires one exact current-user message"
        )
    if any(
        len(item.causal_placement.visible_causal_predecessor_node_identity_fingerprints)
        > policy.max_visible_causal_predecessors_per_unit
        for item in units
    ):
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_TOOL_CALL_FAN_IN_EXCEEDED,
            "provider causal edge fan-in exceeds resolved policy",
        )
    wire_values = tuple(
        item.wire_semantic.wire_semantic_fingerprint for item in units
    )
    causal_values = tuple(item.unit_causal_semantic_fingerprint for item in units)
    wire_accumulator = _ordered_accumulator(
        "provider-ordered-transcript-wire:v2", wire_values
    )
    causal_accumulator = _ordered_accumulator(
        "provider-ordered-transcript-causal:v2", causal_values
    )
    causal_proof = context_fingerprint(
        "provider-ordered-transcript-causal-order-proof:v2",
        tuple(
            (
                item.causal_placement.node_identity.node_identity_fingerprint,
                item.causal_placement.position.position_fingerprint,
                item.causal_placement.visible_causal_predecessor_node_identity_fingerprints,
            )
            for item in units
        ),
    )
    projection_semantic_fingerprint = context_fingerprint(
        "provider-ordered-transcript-projection-semantic:v2",
        {
            "rendering_contract_fingerprint": rendering_contract_fingerprint,
            "source_selection_contract_fingerprint": contract.contract_fingerprint,
            "resolved_causal_physical_policy_fingerprint": policy.policy_fingerprint,
            "stable_transcript_semantic_fingerprint": transcript.transcript_fingerprint,
            "unit_count": len(units),
            "ordered_wire_semantic_accumulator": wire_accumulator,
            "ordered_causal_semantic_accumulator": causal_accumulator,
            "causal_order_proof_fingerprint": causal_proof,
        },
    )
    projection = build_frozen_fact(
        ProviderOrderedTranscriptProjectionFact,
        schema_version="provider_ordered_transcript_projection.v2",
        rendering_contract_fingerprint=rendering_contract_fingerprint,
        source_selection_contract_fingerprint=contract.contract_fingerprint,
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
        stable_transcript_semantic_fingerprint=transcript.transcript_fingerprint,
        ordered_units=tuple(units),
        ordered_wire_semantic_accumulator=wire_accumulator,
        ordered_causal_semantic_accumulator=causal_accumulator,
        causal_order_proof_fingerprint=causal_proof,
        projection_semantic_fingerprint=projection_semantic_fingerprint,
    )
    encoded = canonical_json_bytes(projection.model_dump(mode="json"))
    if len(encoded) > policy.max_projection_canonical_bytes_per_manifest:
        raise ProviderInputPhysicalPolicyError(
            ProviderInputPhysicalPolicyFailureReason.PROVIDER_INPUT_PROJECTION_BYTE_BOUND_EXCEEDED,
            "provider transcript projection exceeds canonical byte bound",
        )
    identity = projection_identity(projection)
    return PreparedOrderedProviderTranscriptProjection(
        projection=projection,
        identity=identity,
        lowered_messages=tuple(item.message for item in lowered_messages),
        source_event_refs_by_message=tuple(
            item.source_event_refs for item in lowered_messages
        ),
    )


def projection_identity(
    projection: ProviderOrderedTranscriptProjectionFact,
) -> ProviderOrderedTranscriptProjectionIdentityFact:
    return build_frozen_fact(
        ProviderOrderedTranscriptProjectionIdentityFact,
        schema_version="provider_ordered_transcript_projection_identity.v1",
        projection_semantic_fingerprint=projection.projection_semantic_fingerprint,
        unit_count=len(projection.ordered_units),
        ordered_wire_semantic_accumulator=(
            projection.ordered_wire_semantic_accumulator
        ),
        ordered_causal_semantic_accumulator=(
            projection.ordered_causal_semantic_accumulator
        ),
    )


def validate_projection(
    *,
    projection: ProviderOrderedTranscriptProjectionFact,
    identity: ProviderOrderedTranscriptProjectionIdentityFact,
    policy: ResolvedProviderInputCausalAndPhysicalPolicyFact,
) -> ProviderInputCausalValidationResult:
    if projection_identity(projection) != identity:
        return _invalid_validation(
            identity=identity,
            policy=policy,
            reason=ProviderInputCausalValidationFailureReason.PROJECTION_SOURCE_JOIN_MISMATCH,
            indices=(),
        )
    edge_count = sum(
        len(item.causal_placement.visible_causal_predecessor_node_identity_fingerprints)
        for item in projection.ordered_units
    )
    return build_frozen_fact(
        ProviderInputCausalValidationResult,
        schema_version="provider_input_causal_validation_result.v2",
        status="valid",
        projection_identity_fingerprint=identity.identity_fingerprint,
        checked_visible_edge_count=edge_count,
        violation_reason=None,
        violating_projection_indices=(),
        validation_contract_fingerprint=CAUSAL_VALIDATION_CONTRACT_FINGERPRINT,
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )


def _invalid_validation(
    *,
    identity: ProviderOrderedTranscriptProjectionIdentityFact,
    policy: ResolvedProviderInputCausalAndPhysicalPolicyFact,
    reason: ProviderInputCausalValidationFailureReason,
    indices: tuple[int, ...],
) -> ProviderInputCausalValidationResult:
    return build_frozen_fact(
        ProviderInputCausalValidationResult,
        schema_version="provider_input_causal_validation_result.v2",
        status="invalid",
        projection_identity_fingerprint=identity.identity_fingerprint,
        checked_visible_edge_count=0,
        violation_reason=reason,
        violating_projection_indices=indices,
        validation_contract_fingerprint=CAUSAL_VALIDATION_CONTRACT_FINGERPRINT,
        resolved_causal_physical_policy_fingerprint=policy.policy_fingerprint,
    )


def _stable_entry_indexes(stable_entries):
    messages: dict[str, TranscriptMessageLeafEntryFact] = {}
    pairs: dict[str, TranscriptToolPairLeafEntryFact] = {}
    results: dict[str, TranscriptToolResultLeafEntryFact] = {}
    for entry in stable_entries:
        if isinstance(entry, TranscriptMessageLeafEntryFact):
            key = entry.attribution.message_id
            target = messages
        elif isinstance(entry, TranscriptToolPairLeafEntryFact):
            key = (
                entry.semantic_identity.call_block_position,
                entry.semantic_identity.assistant_tool_call_id,
            )
            target = pairs
        else:
            key = entry.ordinal.value
            target = results
        if key in target:
            raise ProviderOrderedProjectionError("stable transcript entry identity repeats")
        target[key] = entry
    return messages, pairs, results


def _source_identity(
    *,
    runtime_session_id: str,
    source_message: TranscriptMessageFact,
    provider_message: LLMMessage,
    source_event_refs: tuple[ContextEventReferenceFact, ...],
    lowered_source_kind: Literal["canonical_message", "rollup_observation"],
    message_entries,
    pair_entries,
    result_entries,
    compacted: dict[str, CompactedWindowReferenceFact],
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    transcript: TranscriptCompileInput,
):
    if lowered_source_kind == "rollup_observation":
        if not source_event_refs:
            raise ProviderOrderedProjectionError("rollup observation lacks source events")
        note_semantic = context_fingerprint(
            "provider-rollup-observation-semantic:v1",
            freeze_provider_message_fragment(provider_message).semantic_fingerprint,
        )
        semantic = build_frozen_fact(
            LifecycleNoteSemanticSourceFact,
            schema_version="lifecycle_note_semantic_source.v1",
            note_semantic_fingerprint=note_semantic,
            cause_semantic_fingerprint=source_event_refs[0].payload_fingerprint,
            lifecycle_note_contract_fingerprint=context_fingerprint(
                "provider-lifecycle-note-contract:v1", "rollup_observation"
            ),
        )
        leaf_ref = _synthetic_leaf_reference(
            runtime_session_id=runtime_session_id,
            entry_kind="message",
            semantic_fingerprint=note_semantic,
            fact_fingerprint=context_fingerprint(
                "provider-rollup-observation-fact:v1",
                (
                    source_message.message_id,
                    tuple(ref.payload_fingerprint for ref in source_event_refs),
                ),
            ),
            source_event_refs=source_event_refs,
        )
        attribution = build_frozen_fact(
            LifecycleNoteSourceAttributionFact,
            schema_version="lifecycle_note_source_attribution.v1",
            note_leaf_reference=leaf_ref,
            note_event_reference=source_event_refs[-1],
            cause_event_reference=source_event_refs[0],
            source_semantic_fingerprint=semantic.source_semantic_fingerprint,
        )
        return semantic, attribution
    if provider_message.role is MessageRole.TOOL_RESULT:
        call_id = provider_message.tool_call_id
        normalized_pairs = tuple(
            item
            for item in transcript.tool_pairs
            if item.result_message_id == source_message.message_id
            and item.tool_call_id == call_id
        )
        if len(normalized_pairs) != 1:
            raise ProviderOrderedProjectionError(
                "tool result does not resolve to one normalized interaction"
            )
        normalized_pair = normalized_pairs[0]
        call_entry = message_entries.get(normalized_pair.call_message_id)
        pair = (
            pair_entries.get((call_entry.ordinal.value, call_id))
            if call_entry is not None
            else None
        )
        result = (
            result_entries.get(pair.semantic_identity.result_block_position)
            if pair is not None
            else None
        )
        if result is None or pair is None:
            raise ProviderOrderedProjectionError(
                "tool result lacks exact result/pair stable leaves"
            )
        terminal = result.projection_reference
        semantic = build_frozen_fact(
            DerivedToolResultMessageSemanticSourceFact,
            schema_version="derived_tool_result_message_semantic_source.v1",
            tool_result_leaf_semantic_fingerprint=(
                result.semantic_identity.semantic_fingerprint
            ),
            tool_pair_semantic_fingerprint=pair.semantic_identity.semantic_fingerprint,
            terminal_projection_semantic_fingerprint=(
                terminal.semantic_join.semantic_fingerprint
            ),
        )
        attribution = build_frozen_fact(
            DerivedToolResultMessageSourceAttributionFact,
            schema_version="derived_tool_result_message_source_attribution.v1",
            tool_result_leaf_reference=_leaf_reference(runtime_session_id, result),
            tool_pair_leaf_reference=_leaf_reference(runtime_session_id, pair),
            terminal_projection_reference=terminal,
            source_semantic_fingerprint=semantic.source_semantic_fingerprint,
        )
        _validate_tool_source_join(call_id, result, pair)
        return semantic, attribution

    if source_message.segment == "compaction_summary":
        compacted_ref = compacted.get(source_message.message_id)
        if compacted_ref is None:
            raise ProviderOrderedProjectionError(
                "compaction summary lacks confirmed rewrite attribution"
            )
        rewritten = tuple(
            item
            for item in stable_entries
            if max(ref.sequence for ref in item.source_event_refs)
            <= compacted_ref.compacted_through_sequence
        )
        if not rewritten:
            raise ProviderOrderedProjectionError(
                "compaction rewrite authority has an empty member range"
            )
        ordinals = tuple(item.ordinal.value for item in rewritten)
        member_accumulator = _ordered_accumulator(
            "provider-compaction-rewrite-members:v1",
            tuple(item.semantic_identity.semantic_fingerprint for item in rewritten),
        )
        summary_semantic = context_fingerprint(
            "provider-compaction-summary-message:v1",
            source_message.message_fingerprint,
        )
        rewrite_contract = context_fingerprint(
            "provider-compaction-rewrite-contract:v1",
            "confirmed-summary-replaces-stable-prefix",
        )
        rewrite_ref = build_frozen_fact(
            ProviderCompactionRewriteAuthorityReferenceFact,
            schema_version="provider_compaction_rewrite_authority_reference.v1",
            compaction_completed_event_reference=compacted_ref.source_event,
            source_document_fingerprint=context_fingerprint(
                "provider-compaction-source-document:v1",
                (
                    compacted_ref.summary_artifact_id,
                    compacted_ref.compacted_through_sequence,
                ),
            ),
            summary_semantic_fingerprint=summary_semantic,
            replaced_first_stable_ordinal=min(ordinals),
            replaced_last_stable_ordinal=max(ordinals),
            replaced_member_count=len(rewritten),
            replaced_member_semantic_accumulator=member_accumulator,
            resulting_stable_transcript_semantic_fingerprint=(
                transcript.transcript_fingerprint
            ),
            rewrite_contract_fingerprint=rewrite_contract,
        )
        range_fingerprint = context_fingerprint(
            "provider-compaction-replaced-source-range:v1",
            (
                min(ordinals),
                max(ordinals),
                len(rewritten),
                member_accumulator,
                rewrite_contract,
            ),
        )
        semantic = build_frozen_fact(
            CompactionReplacementSummarySemanticSourceFact,
            schema_version="compaction_replacement_summary_semantic_source.v1",
            summary_semantic_fingerprint=summary_semantic,
            replaced_source_range_fingerprint=range_fingerprint,
            resulting_stable_transcript_semantic_fingerprint=(
                transcript.transcript_fingerprint
            ),
            rewrite_contract_fingerprint=rewrite_contract,
        )
        summary_leaf = _synthetic_leaf_reference(
            runtime_session_id=runtime_session_id,
            entry_kind="message",
            semantic_fingerprint=summary_semantic,
            fact_fingerprint=source_message.message_fingerprint,
            source_event_refs=(compacted_ref.source_event,),
        )
        attribution = build_frozen_fact(
            CompactionReplacementSummarySourceAttributionFact,
            schema_version="compaction_replacement_summary_source_attribution.v1",
            summary_leaf_reference=summary_leaf,
            rewrite_authority_reference=rewrite_ref,
            source_semantic_fingerprint=semantic.source_semantic_fingerprint,
        )
        return semantic, attribution

    entry = message_entries.get(source_message.message_id)
    if entry is None:
        raise ProviderOrderedProjectionError(
            "provider message lacks exact stable message leaf"
        )
    leaf_ref = _leaf_reference(runtime_session_id, entry)
    if source_message.segment in {"recovery_note", "terminal_lifecycle_note"}:
        if not source_event_refs:
            raise ProviderOrderedProjectionError("lifecycle note lacks source event")
        note_semantic = entry.semantic_identity.semantic_fingerprint
        cause = source_event_refs[0]
        note = source_event_refs[-1]
        semantic = build_frozen_fact(
            LifecycleNoteSemanticSourceFact,
            schema_version="lifecycle_note_semantic_source.v1",
            note_semantic_fingerprint=note_semantic,
            cause_semantic_fingerprint=cause.payload_fingerprint,
            lifecycle_note_contract_fingerprint=context_fingerprint(
                "provider-lifecycle-note-contract:v1", source_message.segment
            ),
        )
        attribution = build_frozen_fact(
            LifecycleNoteSourceAttributionFact,
            schema_version="lifecycle_note_source_attribution.v1",
            note_leaf_reference=leaf_ref,
            note_event_reference=note,
            cause_event_reference=cause,
            source_semantic_fingerprint=semantic.source_semantic_fingerprint,
        )
        return semantic, attribution
    semantic = build_frozen_fact(
        DirectStableMessageSemanticSourceFact,
        schema_version="direct_stable_message_semantic_source.v1",
        canonical_message_id=source_message.message_id,
        stable_entry_semantic_fingerprint=entry.semantic_identity.semantic_fingerprint,
    )
    attribution = build_frozen_fact(
        DirectStableMessageSourceAttributionFact,
        schema_version="direct_stable_message_source_attribution.v1",
        stable_leaf_reference=leaf_ref,
        source_semantic_fingerprint=semantic.source_semantic_fingerprint,
    )
    return semantic, attribution


def _validate_tool_source_join(call_id, result, pair) -> None:
    pair_semantic = pair.semantic_identity
    result_semantic = result.semantic_identity
    terminal_semantic = result.projection_reference.semantic_join
    if (
        pair_semantic.assistant_tool_call_id != call_id
        or result_semantic.tool_call_id != call_id
        or terminal_semantic.tool_call_id != call_id
        or pair_semantic.tool_name != result_semantic.tool_name
        or terminal_semantic.model_tool_name != result_semantic.tool_name
        or pair_semantic.result_block_position != result.ordinal.value
    ):
        raise ProviderOrderedProjectionError("tool result/pair/terminal join drifted")


def _validate_selected_source_rule(
    *,
    contract: ProviderTranscriptSourceSelectionContractFact,
    source_message: TranscriptMessageFact,
    provider_message: LLMMessage,
    lowered_source_kind: Literal["canonical_message", "rollup_observation"],
    selected_source_kind: str,
) -> None:
    if provider_message.role is MessageRole.TOOL_RESULT:
        canonical_entry_kind = "tool_result_projection_ref"
        segment = None
        required_companions = ("tool_pair",)
    else:
        canonical_entry_kind = "message"
        segment = (
            "terminal_lifecycle_note"
            if lowered_source_kind == "rollup_observation"
            else source_message.segment
        )
        required_companions = ()
    matches = tuple(
        rule
        for rule in contract.rules
        if rule.canonical_entry_kind == canonical_entry_kind
        and (
            segment in rule.eligible_message_segments
            if segment is not None
            else not rule.eligible_message_segments
        )
    )
    if len(matches) != 1:
        raise ProviderOrderedProjectionError(
            "provider source-selection contract does not select one rule"
        )
    rule = matches[0]
    if (
        rule.selection_outcome != "emit_provider_unit"
        or rule.selected_source_kind != selected_source_kind
        or rule.required_companion_entry_kinds != required_companions
    ):
        raise ProviderOrderedProjectionError(
            "provider source-selection binding disagrees with selected source"
        )


def _leaf_reference(runtime_session_id, entry):
    return build_frozen_fact(
        TranscriptProjectionLeafEntryReferenceFact,
        schema_version="transcript_projection_leaf_entry_reference.v2",
        runtime_session_id=runtime_session_id,
        entry_kind=entry.entry_kind,
        ordinal=entry.ordinal.value,
        entry_semantic_fingerprint=entry.semantic_identity.semantic_fingerprint,
        entry_fact_fingerprint=entry.fact_fingerprint,
        source_event_references=entry.source_event_refs,
    )


def _synthetic_leaf_reference(
    *,
    runtime_session_id: str,
    entry_kind: str,
    semantic_fingerprint: str,
    fact_fingerprint: str,
    source_event_refs: tuple[ContextEventReferenceFact, ...],
):
    return build_frozen_fact(
        TranscriptProjectionLeafEntryReferenceFact,
        schema_version="transcript_projection_leaf_entry_reference.v2",
        runtime_session_id=runtime_session_id,
        entry_kind=entry_kind,
        ordinal=0,
        entry_semantic_fingerprint=semantic_fingerprint,
        entry_fact_fingerprint=fact_fingerprint,
        source_event_references=source_event_refs,
    )


def _classification(segment: str):
    if segment in {"current_user", "current_run_tail", "compaction_summary"}:
        return segment
    if segment in {"recovery_note", "terminal_lifecycle_note"}:
        return "lifecycle_note"
    return "prior_history"


def _ordered_accumulator(domain: str, values: Iterable[str]) -> str:
    accumulator = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        accumulator = context_fingerprint(f"{domain}:step", (accumulator, value))
    return accumulator


__all__ = [
    "CAUSAL_VALIDATION_CONTRACT_FINGERPRINT",
    "CONTINUATION_JOIN_CONTRACT_FINGERPRINT",
    "LoweredProviderTranscriptMessage",
    "PreparedOrderedProviderTranscriptProjection",
    "ProviderInputPhysicalPolicyError",
    "ProviderOrderedProjectionError",
    "build_default_provider_transcript_source_selection_contract",
    "build_default_resolved_causal_physical_policy",
    "build_ordered_provider_transcript_projection",
    "projection_identity",
    "validate_projection",
]
