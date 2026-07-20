"""Stable transcript placement and provider-message projection.

Compile time remains invocation audit metadata.  It is never rendered into an
existing transcript section and therefore cannot invalidate a provider prefix.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Iterable

from pulsara_agent.llm.estimator import TokenEstimator
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.context import (
    ContextFactSnapshotFact,
    ContextSourceTimingFact,
    TranscriptCompileInput,
)
from pulsara_agent.primitives.frozen import (
    PreparedRuntimeValueBase,
    build_frozen_fact,
)
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProviderInvocationRenderingContractFact,
    TranscriptProviderLoweringOrderContractFact,
    TranscriptProviderLoweringOrderRuleFact,
    TranscriptProviderProjectionFact,
    TranscriptProviderProjectionSemanticFact,
    TranscriptProviderSectionProjectionFact,
    TranscriptProviderSectionSemanticFact,
    TranscriptProviderSectionTimingSemanticFact,
    TranscriptTimingOverlayContractFact,
    TranscriptTimingOverlayRuleFact,
)
from pulsara_agent.runtime.context_engine.types import AllocatedContextSection


@dataclass(frozen=True, slots=True)
class PreparedTranscriptProviderProjectionFact(PreparedRuntimeValueBase):
    projection_fact: TranscriptProviderProjectionFact
    lowered_provider_messages: tuple[LLMMessage, ...]
    rendered_transcript_sections: tuple[AllocatedContextSection, ...]

    def __post_init__(self) -> None:
        expected = provider_messages_fingerprint(self.lowered_provider_messages)
        if (
            self.projection_fact.semantic_identity.lowered_provider_messages_fingerprint
            != expected
        ):
            raise ValueError("prepared transcript provider messages mismatch")
        projected_ids = tuple(item.section_id for item in self.projection_fact.sections)
        rendered_ids = tuple(item.id for item in self.rendered_transcript_sections)
        if rendered_ids != projected_ids:
            raise ValueError("prepared transcript provider sections mismatch")
        for rendered, projected in zip(
            self.rendered_transcript_sections,
            self.projection_fact.sections,
            strict=True,
        ):
            timing = projected.semantic_identity.timing_semantic
            metadata = rendered.metadata
            if metadata.get("timing_header_text") != timing.rendered_timing_header:
                raise ValueError("prepared transcript timing header mismatch")
            _validate_rendered_timing_metadata(metadata, timing)


def build_default_transcript_invocation_rendering_contract() -> (
    TranscriptProviderInvocationRenderingContractFact
):
    lowering_rules = tuple(
        build_frozen_fact(
            TranscriptProviderLoweringOrderRuleFact,
            schema_version="transcript_provider_lowering_order_rule.v1",
            normalized_lane=lane,
            lowering_scope=scope,
            section_order=order,
            section_grouping="contiguous_same_lane_and_scope",
            within_section_order="stable_transcript_traversal",
        )
        for order, (lane, scope) in enumerate(
            (
                ("prior_history", "transcript_prior"),
                ("current_user", "leading_user"),
                ("current_run_tail", "transcript_current_run"),
                ("runtime_system", "system_runtime"),
            )
        )
    )
    lowering = build_frozen_fact(
        TranscriptProviderLoweringOrderContractFact,
        schema_version="transcript_provider_lowering_order_contract.v1",
        contract_id="pulsara.transcript-provider-lowering-order",
        contract_version="1",
        rules=lowering_rules,
        base_system_prompt_position="outside_transcript_projection",
    )
    timing_rules = tuple(
        build_frozen_fact(
            TranscriptTimingOverlayRuleFact,
            schema_version="transcript_timing_overlay_rule.v1",
            timing_overlay_kind=kind,
            header_policy="none",
            source_range_aggregation="section_min_start_max_end",
            age_basis="not_applicable",
        )
        for kind in (
            "compacted_history",
            "current_run_observation",
            "current_user",
            "historical_replay",
            "runtime_observation",
        )
    )
    timing = build_frozen_fact(
        TranscriptTimingOverlayContractFact,
        schema_version="transcript_timing_overlay_contract.v1",
        contract_id="pulsara.transcript-timing-overlay",
        contract_version="2",
        rules=timing_rules,
        compiled_at_source="context_compile_request_compiled_at_utc",
        local_date_source="session_timezone_from_compile_request",
        age_rounding="floor_non_negative_seconds",
        header_format_version="context-timing:none:v2",
        max_rendered_header_characters=2_048,
        max_rendered_header_utf8_bytes=8_192,
    )
    return build_frozen_fact(
        TranscriptProviderInvocationRenderingContractFact,
        schema_version="transcript_provider_invocation_rendering_contract.v1",
        contract_id="pulsara.transcript-provider-invocation-rendering",
        contract_version="2",
        lowering_order_contract=lowering,
        timing_overlay_contract=timing,
    )


def prepare_transcript_provider_projection(
    *,
    snapshot: ContextFactSnapshotFact,
    transcript: TranscriptCompileInput,
    prior_history_messages: tuple[LLMMessage, ...],
    current_user_messages: tuple[LLMMessage, ...],
    current_run_tail_messages: tuple[LLMMessage, ...],
    chronological_messages: tuple[LLMMessage, ...],
    sections: tuple[AllocatedContextSection, ...],
    estimator: TokenEstimator,
    rendering_contract: TranscriptProviderInvocationRenderingContractFact | None = None,
) -> PreparedTranscriptProviderProjectionFact:
    contract = (
        rendering_contract or build_default_transcript_invocation_rendering_contract()
    )
    lane_inputs = _lane_inputs(
        prior_history_messages=prior_history_messages,
        current_user_messages=current_user_messages,
        current_run_tail_messages=current_run_tail_messages,
    )
    source_by_lane = {
        lane: tuple(
            message
            for message in transcript.messages
            if _normalized_lane(message.segment) == lane
        )
        for lane, *_rest in lane_inputs
    }
    projected_sections: list[TranscriptProviderSectionProjectionFact] = []
    rendered_sections: list[AllocatedContextSection] = []
    for lane, scope, section_id, messages in lane_inputs:
        if not messages:
            continue
        section = next(
            (item for item in sections if item.id == section_id and item.included),
            None,
        )
        if section is None:
            raise ValueError("transcript provider projection section is missing")
        source = ContextSourceTimingFact.model_validate(
            section.metadata.get("source_timing")
        )
        timing_kind = _timing_kind(lane, source_by_lane[lane])
        timing_contract = contract.timing_overlay_contract
        rule = _timing_rule(timing_contract, timing_kind)
        if rule.header_policy != "none" or rule.age_basis != "not_applicable":
            raise ValueError("transcript dynamic timing overlay is forbidden")
        header = None
        _validate_header_bounds(header, timing_contract)
        stable_observed_at = (
            source.source_ended_at_utc
            or source.observed_at_utc
            or source.source_started_at_utc
            or "1970-01-01T00:00:00Z"
        )
        timing_semantic = build_frozen_fact(
            TranscriptProviderSectionTimingSemanticFact,
            schema_version="transcript_provider_section_timing_semantic.v1",
            timing_overlay_kind=timing_kind,
            compiled_at_utc=stable_observed_at,
            session_timezone=None,
            compiled_local_date=None,
            source_started_at_utc=source.source_started_at_utc,
            source_ended_at_utc=source.source_ended_at_utc,
            source_observed_at_utc=source.observed_at_utc,
            age_seconds=None,
            rendered_timing_header=header,
            timing_overlay_contract_id=timing_contract.contract_id,
            timing_overlay_contract_version=timing_contract.contract_version,
            timing_overlay_contract_fingerprint=timing_contract.contract_fingerprint,
        )
        message_semantics = tuple(
            provider_message_semantic_fingerprint(item) for item in messages
        )
        section_semantic = build_frozen_fact(
            TranscriptProviderSectionSemanticFact,
            schema_version="transcript_provider_section_semantic.v1",
            normalized_lane=lane,
            lowering_scope=scope,
            ordered_message_semantic_fingerprints=message_semantics,
            timing_semantic=timing_semantic,
        )
        source_attribution = tuple(
            context_fingerprint(
                "transcript-provider-message-attribution:v1",
                {
                    "section_id": section_id,
                    "message_index": index,
                    "source_message_fingerprints": tuple(
                        item.message_fingerprint for item in source_by_lane[lane]
                    ),
                },
            )
            for index in range(len(messages))
        )
        projected_sections.append(
            build_frozen_fact(
                TranscriptProviderSectionProjectionFact,
                schema_version="transcript_provider_section_projection.v1",
                section_id=section_id,
                section_index=len(projected_sections),
                semantic_identity=section_semantic,
                ordered_message_attribution_fingerprints=source_attribution,
            )
        )
        rendered_sections.append(
            _render_transcript_section(
                section=section,
                timing=timing_semantic,
                estimator=estimator,
                messages=messages,
            )
        )
        if header is not None:
            raise ValueError("dynamic transcript timing headers require ordered lowering")
    _validate_chronological_message_coverage(
        chronological_messages=chronological_messages,
        lane_inputs=lane_inputs,
    )
    projection_semantic = build_frozen_fact(
        TranscriptProviderProjectionSemanticFact,
        schema_version="transcript_provider_projection_semantic.v1",
        stable_normalized_transcript_fingerprint=transcript.transcript_fingerprint,
        ordered_section_semantic_fingerprints=tuple(
            item.semantic_identity.semantic_fingerprint for item in projected_sections
        ),
        lowered_provider_messages_fingerprint=provider_messages_fingerprint(
            chronological_messages
        ),
    )
    projection = build_frozen_fact(
        TranscriptProviderProjectionFact,
        schema_version="transcript_provider_projection.v1",
        context_id=snapshot.identity.context_id,
        model_call_index=snapshot.identity.model_call_index,
        compile_attempt_index=snapshot.identity.compile_attempt_index,
        semantic_identity=projection_semantic,
        sections=tuple(projected_sections),
        rendering_contract=contract,
    )
    return PreparedTranscriptProviderProjectionFact(
        projection_fact=projection,
        lowered_provider_messages=chronological_messages,
        rendered_transcript_sections=tuple(rendered_sections),
    )


def materialize_transcript_provider_projection(
    *,
    snapshot: ContextFactSnapshotFact,
    projection_fact: TranscriptProviderProjectionFact,
    transcript: TranscriptCompileInput,
    prior_history_messages: tuple[LLMMessage, ...],
    current_user_messages: tuple[LLMMessage, ...],
    current_run_tail_messages: tuple[LLMMessage, ...],
    chronological_messages: tuple[LLMMessage, ...],
    sections: tuple[AllocatedContextSection, ...],
    estimator: TokenEstimator,
) -> PreparedTranscriptProviderProjectionFact:
    """Hydrate one frozen invocation projection without recomputing timing."""

    if (
        projection_fact.context_id != snapshot.identity.context_id
        or projection_fact.model_call_index != snapshot.identity.model_call_index
        or projection_fact.compile_attempt_index
        != snapshot.identity.compile_attempt_index
    ):
        raise ValueError("frozen provider projection invocation identity mismatch")
    if (
        projection_fact.semantic_identity.stable_normalized_transcript_fingerprint
        != transcript.transcript_fingerprint
    ):
        raise ValueError("frozen provider projection transcript mismatch")
    lane_messages = {
        "prior_history": prior_history_messages,
        "current_user": current_user_messages,
        "current_run_tail": current_run_tail_messages,
    }
    expected_sections = {
        lane: (scope, section_id, messages)
        for lane, scope, section_id, messages in _lane_inputs(
            prior_history_messages=prior_history_messages,
            current_user_messages=current_user_messages,
            current_run_tail_messages=current_run_tail_messages,
        )
        if lane_messages[lane]
    }
    source_sections = {item.id: item for item in sections}
    if len(source_sections) != len(sections):
        raise ValueError("transcript source section IDs are not unique")
    if len(projection_fact.sections) != len(expected_sections):
        raise ValueError("frozen provider projection section count mismatch")
    rendered_sections: list[AllocatedContextSection] = []
    seen_lanes: set[str] = set()
    for section in projection_fact.sections:
        semantic = section.semantic_identity
        lane = semantic.normalized_lane
        expected = expected_sections.get(lane)
        if expected is None or lane in seen_lanes:
            raise ValueError("frozen provider projection lane mismatch")
        seen_lanes.add(lane)
        expected_scope, expected_section_id, _expected_messages = expected
        if (
            semantic.lowering_scope != expected_scope
            or section.section_id != expected_section_id
        ):
            raise ValueError("frozen provider projection placement mismatch")
        messages = lane_messages[lane]
        message_fingerprints = tuple(
            provider_message_semantic_fingerprint(item) for item in messages
        )
        if (
            semantic.ordered_message_semantic_fingerprints != message_fingerprints
            or len(section.ordered_message_attribution_fingerprints) != len(messages)
        ):
            raise ValueError("frozen provider projection message mismatch")
        timing = semantic.timing_semantic
        contract = projection_fact.rendering_contract.timing_overlay_contract
        if (
            timing.timing_overlay_contract_id != contract.contract_id
            or timing.timing_overlay_contract_version != contract.contract_version
            or timing.timing_overlay_contract_fingerprint
            != contract.contract_fingerprint
        ):
            raise ValueError("frozen provider projection timing contract mismatch")
        source_section = source_sections.get(expected_section_id)
        if source_section is None:
            raise ValueError("frozen provider projection source section is missing")
        _validate_frozen_source_timing(source_section, timing)
        rule = _timing_rule(contract, timing.timing_overlay_kind)
        header = timing.rendered_timing_header
        if (rule.header_policy == "none") != (header is None):
            raise ValueError("frozen provider projection timing header mismatch")
        _validate_header_bounds(header, contract)
        rendered_sections.append(
            _render_transcript_section(
                section=source_section,
                timing=timing,
                estimator=estimator,
                messages=messages,
            )
        )
        if header is not None:
            raise ValueError("dynamic transcript timing headers require ordered lowering")
    if seen_lanes != set(expected_sections):
        raise ValueError("frozen provider projection omitted a transcript lane")
    _validate_chronological_message_coverage(
        chronological_messages=chronological_messages,
        lane_inputs=_lane_inputs(
            prior_history_messages=prior_history_messages,
            current_user_messages=current_user_messages,
            current_run_tail_messages=current_run_tail_messages,
        ),
    )
    prepared = PreparedTranscriptProviderProjectionFact(
        projection_fact=projection_fact,
        lowered_provider_messages=chronological_messages,
        rendered_transcript_sections=tuple(rendered_sections),
    )
    if (
        tuple(
            item.semantic_identity.semantic_fingerprint
            for item in projection_fact.sections
        )
        != projection_fact.semantic_identity.ordered_section_semantic_fingerprints
    ):
        raise ValueError("frozen provider projection section identity mismatch")
    return prepared


def _validate_chronological_message_coverage(
    *,
    chronological_messages: tuple[LLMMessage, ...],
    lane_inputs: tuple[tuple[str, str, str, tuple[LLMMessage, ...]], ...],
) -> None:
    lane_fingerprints = sorted(
        provider_message_semantic_fingerprint(message)
        for _lane, _scope, _section_id, messages in lane_inputs
        for message in messages
    )
    chronological_fingerprints = sorted(
        provider_message_semantic_fingerprint(message)
        for message in chronological_messages
    )
    if chronological_fingerprints != lane_fingerprints:
        raise ValueError("chronological transcript lowering coverage drifted")


def provider_message_semantic_fingerprint(message: LLMMessage) -> str:
    return context_fingerprint(
        "provider-neutral-transcript-message:v1",
        _canonical_message(message),
    )


def provider_messages_fingerprint(messages: tuple[LLMMessage, ...]) -> str:
    return context_fingerprint(
        "provider-neutral-transcript-messages:v1",
        tuple(_canonical_message(item) for item in messages),
    )


def _canonical_message(message: LLMMessage) -> dict[str, object]:
    payload = asdict(message)
    payload["role"] = str(message.role)
    return payload


def _lane_inputs(
    *,
    prior_history_messages: tuple[LLMMessage, ...],
    current_user_messages: tuple[LLMMessage, ...],
    current_run_tail_messages: tuple[LLMMessage, ...],
) -> tuple[tuple[str, str, str, tuple[LLMMessage, ...]], ...]:
    return (
        (
            "prior_history",
            "transcript_prior",
            "transcript:prior_history",
            prior_history_messages,
        ),
        (
            "current_user",
            "leading_user",
            "transcript:current_user",
            current_user_messages,
        ),
        (
            "current_run_tail",
            "transcript_current_run",
            "transcript:current_run_tail",
            current_run_tail_messages,
        ),
    )


def _normalized_lane(segment: str) -> str:
    if segment == "current_user":
        return "current_user"
    if segment == "current_run_tail":
        return "current_run_tail"
    return "prior_history"


def _timing_kind(lane: str, source_messages: Iterable[object]) -> str:
    if lane == "current_user":
        return "current_user"
    if lane == "current_run_tail":
        return "current_run_observation"
    if any(
        getattr(item, "segment", None) == "compaction_summary"
        for item in source_messages
    ):
        return "compacted_history"
    return "historical_replay"


def _timing_rule(contract: TranscriptTimingOverlayContractFact, kind: str):
    matches = tuple(item for item in contract.rules if item.timing_overlay_kind == kind)
    if len(matches) != 1:
        raise ValueError("transcript timing overlay rule is unavailable")
    return matches[0]


def _timing_metadata(
    timing: TranscriptProviderSectionTimingSemanticFact,
) -> dict[str, object]:
    source = {
        "observed_at_utc": timing.source_observed_at_utc,
        "source_started_at_utc": timing.source_started_at_utc,
        "source_ended_at_utc": timing.source_ended_at_utc,
    }
    return {
        "compiled_at_utc": timing.compiled_at_utc,
        "session_timezone": timing.session_timezone,
        "compiled_local_date": timing.compiled_local_date,
        "age_seconds": timing.age_seconds,
        "source": source,
    }


def _render_transcript_section(
    *,
    section: AllocatedContextSection,
    timing: TranscriptProviderSectionTimingSemanticFact,
    estimator: TokenEstimator,
    messages: tuple[LLMMessage, ...],
) -> AllocatedContextSection:
    source = ContextSourceTimingFact.model_validate(
        section.metadata.get("source_timing")
    )
    timing_metadata = _timing_metadata(timing)
    timing_metadata["source"] = source.model_dump(mode="json")
    header = timing.rendered_timing_header
    metadata = {
        **section.metadata,
        "source_timing": source.model_dump(mode="json"),
        "timing": timing_metadata,
    }
    header_tokens = 0
    if header is not None:
        header_tokens = estimator.estimate_text(header)
        metadata["timing_header_text"] = header
        metadata["rendered_timing_header_tokens"] = header_tokens
        metadata["rendered_timing_header_chars"] = len(header)
    message_tokens = sum(estimator.estimate_message(item) for item in messages)
    return replace(
        section,
        metadata=metadata,
        estimated_tokens=(
            message_tokens
            + header_tokens
        ),
    )


def _validate_frozen_source_timing(
    section: AllocatedContextSection,
    timing: TranscriptProviderSectionTimingSemanticFact,
) -> None:
    source = ContextSourceTimingFact.model_validate(
        section.metadata.get("source_timing")
    )
    if (
        timing.source_started_at_utc != source.source_started_at_utc
        or timing.source_ended_at_utc != source.source_ended_at_utc
        or timing.source_observed_at_utc != source.observed_at_utc
    ):
        raise ValueError("frozen provider projection source timing mismatch")


def _validate_rendered_timing_metadata(
    metadata: dict[str, object],
    timing: TranscriptProviderSectionTimingSemanticFact,
) -> None:
    payload = metadata.get("timing")
    if not isinstance(payload, dict):
        raise ValueError("prepared transcript timing metadata is missing")
    if any(
        payload.get(key) != expected
        for key, expected in (
            ("compiled_at_utc", timing.compiled_at_utc),
            ("session_timezone", timing.session_timezone),
            ("compiled_local_date", timing.compiled_local_date),
            ("age_seconds", timing.age_seconds),
        )
    ):
        raise ValueError("prepared transcript timing metadata mismatch")
    source = payload.get("source")
    if not isinstance(source, dict) or any(
        source.get(key) != expected
        for key, expected in (
            ("source_started_at_utc", timing.source_started_at_utc),
            ("source_ended_at_utc", timing.source_ended_at_utc),
            ("observed_at_utc", timing.source_observed_at_utc),
        )
    ):
        raise ValueError("prepared transcript source timing metadata mismatch")


def _validate_header_bounds(
    header: str | None,
    contract: TranscriptTimingOverlayContractFact,
) -> None:
    if header is None:
        return
    if (
        len(header) > contract.max_rendered_header_characters
        or len(header.encode("utf-8")) > contract.max_rendered_header_utf8_bytes
    ):
        raise ValueError("frozen provider projection timing header exceeds cap")


__all__ = [
    "PreparedTranscriptProviderProjectionFact",
    "build_default_transcript_invocation_rendering_contract",
    "materialize_transcript_provider_projection",
    "prepare_transcript_provider_projection",
    "provider_message_semantic_fingerprint",
    "provider_messages_fingerprint",
]
