"""Pure rendering of normalized transcript and typed tool-result units.

No function in this module accepts ``Msg`` or infers execution semantics from
tool names / result JSON.  The durable ``ToolResultRenderUnit`` is the sole
authority for result state, variant, essential facts and terminal timing.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Protocol

from pulsara_agent.primitives.context import (
    ResolvedToolResultRenderPolicyFact,
    ToolResultRenderPolicyBasisFact,
    TranscriptCompileInput,
    TranscriptToolResultRefFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowProjectionState,
    ToolObservationRepresentation,
)
from pulsara_agent.primitives.tool_result import (
    ContextToolResultArtifactRefFact,
    PreparedToolResultRenderInput,
    ToolResultBodyCandidateSource,
    ToolResultBodyPolicy,
    ToolResultEnvelopePolicy,
    ToolResultLatestReserveReasonCode,
    ToolResultMinimumEnvelopeKind,
    ToolResultPayloadFormat,
    ToolResultRenderCacheHint,
    ToolResultRenderDecisionFact,
    ToolResultRenderDiagnosticCode,
    ToolResultRenderDiagnosticFact,
    ToolResultRenderOperationalFact,
    ToolResultRenderReasonCode,
    ToolResultRenderUnit,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.estimator import TokenEstimator
    from pulsara_agent.llm.resolution import ResolvedModelCall


class ToolResultRenderDecisionCachePort(Protocol):
    def get(self, cache_key: str) -> ToolResultRenderCacheHint | None: ...


class InMemoryToolResultRenderCache:
    """Bounded session-owned optimization cache; never a render authority."""

    def __init__(self, *, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("render cache max entries must be positive")
        self._max_entries = max_entries
        self._lock = RLock()
        self._values: OrderedDict[str, ToolResultRenderCacheHint] = OrderedDict()

    def get(self, cache_key: str) -> ToolResultRenderCacheHint | None:
        with self._lock:
            hint = self._values.get(cache_key)
            if hint is not None:
                self._values.move_to_end(cache_key)
            return hint

    def put(self, cache_key: str, hint: ToolResultRenderCacheHint) -> None:
        if hint.cache_key != cache_key:
            raise ValueError("render cache key/hint mismatch")
        with self._lock:
            self._values[cache_key] = hint
            self._values.move_to_end(cache_key)
            while len(self._values) > self._max_entries:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


@dataclass(frozen=True, slots=True)
class RenderedToolResultFragment:
    """One rendered result body; transcript lowering remains compiler-owned."""

    unit_id: str
    tool_call_id: str
    source_message_id: str
    source_message_index: int
    content_block_index: int
    segment: str
    text: str
    rendered_text_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolResultRenderCacheWriteCandidate:
    cache_key: str
    hint: ToolResultRenderCacheHint

    def __post_init__(self) -> None:
        if self.hint.cache_key != self.cache_key:
            raise ValueError("render cache candidate key mismatch")


@dataclass(frozen=True, slots=True)
class PreparedToolResultRenderOutput:
    fragments: tuple[RenderedToolResultFragment, ...]
    canonical_decisions: tuple[ToolResultRenderDecisionFact, ...]
    operational_facts: tuple[ToolResultRenderOperationalFact, ...]
    tool_result_render_decisions: tuple[dict[str, object], ...]
    tool_result_budget_report: dict[str, object]
    cache_write_candidates: tuple[ToolResultRenderCacheWriteCandidate, ...]


@dataclass(frozen=True, slots=True)
class _RenderedUnit:
    text: str
    decision: ToolResultRenderDecisionFact
    operational: ToolResultRenderOperationalFact


def prepare_tool_result_render_input(
    *,
    units: tuple[ToolResultRenderUnit, ...],
    transcript: TranscriptCompileInput,
    policy_basis: ToolResultRenderPolicyBasisFact,
    cache: ToolResultRenderDecisionCachePort | None = None,
) -> PreparedToolResultRenderInput:
    unit_ids = tuple(unit.unit_id for unit in units)
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("tool-result render input contains duplicate unit IDs")
    refs = tuple(
        (message_index, block_index, message, block)
        for message_index, message in enumerate(transcript.messages)
        for block_index, block in enumerate(message.blocks)
        if isinstance(block, TranscriptToolResultRefFact)
    )
    if tuple(ref.tool_result_unit_id for _, _, _, ref in refs) != unit_ids:
        raise ValueError("tool-result unit order differs from transcript refs")
    positions = {
        (message.message_id, block_index): position
        for position, (message, block_index) in enumerate(
            (message, block_index)
            for message in transcript.messages
            for block_index, _block in enumerate(message.blocks)
        )
    }
    pairs = {
        (pair.result_message_id, pair.tool_call_id): pair
        for pair in transcript.tool_pairs
    }
    for unit, (_message_index, block_index, message, ref) in zip(
        units, refs, strict=True
    ):
        pair = pairs.get((message.message_id, ref.tool_call_id))
        if pair is None:
            raise ValueError("tool-result unit lacks normalized interaction pair")
        if (
            ref.tool_call_id != unit.tool_call_id
            or pair.tool_call_id != unit.tool_call_id
            or pair.model_tool_name != unit.model_tool_name
            or pair.call_message_id != unit.call_message_id
            or pair.result_message_id != unit.result_message_id
            or pair.result_message_id != message.message_id
            or pair.result_block_index != block_index
            or unit.call_position
            != positions[(pair.call_message_id, pair.call_block_index)]
            or unit.result_position
            != positions[(pair.result_message_id, pair.result_block_index)]
        ):
            raise ValueError("tool-result ref/pair/unit identity mismatch")

    segment_by_unit = {
        block.tool_result_unit_id: message.segment
        for message in transcript.messages
        for block in message.blocks
        if isinstance(block, TranscriptToolResultRefFact)
    }
    protected_unit_ids = tuple(
        unit.unit_id
        for unit in units
        if segment_by_unit[unit.unit_id] in {"current_user", "current_run_tail"}
    )
    resolved_payload = {
        "basis": policy_basis,
        "ordered_unit_ids": unit_ids,
        "protected_unit_ids": protected_unit_ids,
        "unit_order_fingerprint": context_fingerprint(
            "tool-result-unit-order:v2",
            unit_ids,
        ),
        "protection_fingerprint": context_fingerprint(
            "tool-result-unit-protection:v2",
            protected_unit_ids,
        ),
    }
    resolved = ResolvedToolResultRenderPolicyFact(
        **resolved_payload,
        policy_fingerprint=context_fingerprint(
            "resolved-tool-result-render-policy:v2",
            resolved_payload,
        ),
    )
    render_payload = {
        "units": units,
        "resolved_policy": resolved,
        "transcript_fingerprint": transcript.transcript_fingerprint,
    }
    render_input_fingerprint = context_fingerprint(
        "prepared-tool-result-render-input:v1",
        render_payload,
    )
    cache_hints_list: list[ToolResultRenderCacheHint] = []
    cache_read_failed_unit_ids: list[str] = []
    if cache is not None:
        for unit in units:
            try:
                hint = cache.get(
                    _render_cache_key(
                        unit=unit,
                        segment=_render_segment(segment_by_unit[unit.unit_id]),
                        policy_basis_fingerprint=policy_basis.basis_fingerprint,
                    )
                )
            except Exception:
                cache_read_failed_unit_ids.append(unit.unit_id)
                continue
            if hint is not None:
                cache_hints_list.append(hint)
    cache_hints = tuple(cache_hints_list)
    hints_fp = context_fingerprint(
        "tool-result-render-cache-hints:v1",
        tuple(hint.hint_fingerprint for hint in cache_hints),
    )
    return PreparedToolResultRenderInput(
        units=units,
        resolved_policy=resolved,
        cache_configured=cache is not None,
        cache_hints=cache_hints,
        cache_read_failed_unit_ids=tuple(cache_read_failed_unit_ids),
        render_input_fingerprint=render_input_fingerprint,
        cache_hints_fingerprint=hints_fp,
    )


def render_prepared_tool_result_units(
    *,
    prepared: PreparedToolResultRenderInput,
    transcript: TranscriptCompileInput,
    token_estimator: TokenEstimator,
    projection_state: ContextWindowProjectionState | None = None,
) -> PreparedToolResultRenderOutput:
    refs = tuple(
        block.tool_result_unit_id
        for message in transcript.messages
        for block in message.blocks
        if isinstance(block, TranscriptToolResultRefFact)
    )
    if tuple(unit.unit_id for unit in prepared.units) != refs:
        raise ValueError("prepared tool-result units differ from transcript refs")
    if transcript.transcript_fingerprint == "":
        raise ValueError("normalized transcript fingerprint is required")

    allocator = _TypedToolResultAllocator(
        policy=prepared.resolved_policy,
        token_estimator=token_estimator,
        cache_hints={hint.unit_id: hint for hint in prepared.cache_hints},
        cache_configured=prepared.cache_configured,
        cache_read_failed_unit_ids=frozenset(prepared.cache_read_failed_unit_ids),
        cache_keys={
            unit.unit_id: _render_cache_key(
                unit=unit,
                segment=_render_segment(
                    next(
                        message.segment
                        for message in transcript.messages
                        for block in message.blocks
                        if isinstance(block, TranscriptToolResultRefFact)
                        and block.tool_result_unit_id == unit.unit_id
                    )
                ),
                policy_basis_fingerprint=(
                    prepared.resolved_policy.basis.basis_fingerprint
                ),
            )
            for unit in prepared.units
        },
    )
    units = {unit.unit_id: unit for unit in prepared.units}
    fragments: list[RenderedToolResultFragment] = []
    for message_index, message in enumerate(transcript.messages):
        segment = _render_segment(message.segment)
        for block_index, block in enumerate(message.blocks):
            if not isinstance(block, TranscriptToolResultRefFact):
                continue
            unit = units[block.tool_result_unit_id]
            text = allocator.render(
                unit=unit,
                segment=segment,
                source_message_id=message.message_id,
                source_message_index=message_index,
                content_block_index=block_index,
            )
            fragments.append(
                RenderedToolResultFragment(
                    unit_id=unit.unit_id,
                    tool_call_id=unit.tool_call_id,
                    source_message_id=message.message_id,
                    source_message_index=message_index,
                    content_block_index=block_index,
                    segment=segment,
                    text=text,
                    rendered_text_fingerprint=rendered_text_fingerprint(text),
                )
            )
    cache_writes = tuple(
        _render_cache_write_candidate(fragment, decision, operational)
        for fragment, decision, operational in zip(
            fragments,
            allocator.decisions,
            allocator.operational,
            strict=True,
        )
        if operational.cache_status in {"miss", "invalidated"}
        and _cache_admission_allowed(decision)
    )
    output = PreparedToolResultRenderOutput(
        fragments=tuple(fragments),
        canonical_decisions=tuple(allocator.decisions),
        operational_facts=tuple(allocator.operational),
        tool_result_render_decisions=tuple(
            _decision_event_value(item) for item in allocator.decisions
        ),
        tool_result_budget_report=allocator.report(),
        cache_write_candidates=cache_writes,
    )
    if projection_state is None:
        return output
    return apply_tool_observation_projection(
        units=prepared.units,
        rendered=output,
        projection_state=projection_state,
        policy=prepared.resolved_policy,
        token_estimator=token_estimator,
    )


def apply_tool_observation_projection(
    *,
    units: tuple[ToolResultRenderUnit, ...],
    rendered: PreparedToolResultRenderOutput,
    projection_state: ContextWindowProjectionState,
    policy: ResolvedToolResultRenderPolicyFact,
    token_estimator: TokenEstimator,
) -> PreparedToolResultRenderOutput:
    """Render the exact representation selected by the durable projection."""

    if not (len(units) == len(rendered.fragments) == len(rendered.canonical_decisions)):
        raise ValueError("projection render input cardinality mismatch")
    projections = {item.unit_id: item for item in projection_state.unit_projections}
    projected_fragments: list[RenderedToolResultFragment] = []
    projected_decisions: list[ToolResultRenderDecisionFact] = []
    for unit, fragment, decision in zip(
        units,
        rendered.fragments,
        rendered.canonical_decisions,
        strict=True,
    ):
        projection = projections.get(unit.unit_id)
        if projection is None:
            raise ValueError("durable projection is missing a transcript result unit")
        if (
            projection.tool_call_id != unit.tool_call_id
            or projection.tool_result_event_id != unit.source_event_ids[-1]
            or projection.tool_result_sequence != unit.source_sequence_end
        ):
            raise ValueError("durable projection source identity mismatch")
        if (
            projection.source_rollup_id is None
            and projection.rendered_fragment_fingerprint
            == fragment.rendered_text_fingerprint
            and projection.estimated_tokens
            == token_estimator.estimate_text(fragment.text)
        ):
            projected_fragment, projected_decision = fragment, decision
        else:
            projected_fragment, projected_decision = (
                render_tool_result_projection_variant(
                    unit=unit,
                    base_fragment=fragment,
                    base_decision=decision,
                    representation=projection.representation,
                    source_rollup_id=projection.source_rollup_id,
                    policy=policy,
                )
            )
        if (
            projected_fragment.rendered_text_fingerprint
            != projection.rendered_fragment_fingerprint
            or token_estimator.estimate_text(projected_fragment.text)
            != projection.estimated_tokens
        ):
            raise ValueError(
                "durable projection differs from deterministic rendered variant"
            )
        projected_fragments.append(projected_fragment)
        projected_decisions.append(projected_decision)
    report = _projection_render_report(
        decisions=tuple(projected_decisions),
        policy=policy,
        token_estimator=token_estimator,
        projection_state=projection_state,
    )
    return PreparedToolResultRenderOutput(
        fragments=tuple(projected_fragments),
        canonical_decisions=tuple(projected_decisions),
        operational_facts=rendered.operational_facts,
        tool_result_render_decisions=tuple(
            _decision_event_value(item) for item in projected_decisions
        ),
        tool_result_budget_report=report,
        # A downgraded projection is not a canonical high-fidelity render-cache
        # candidate.  The original render candidates remain semantically valid
        # only when every selected representation is unchanged.
        cache_write_candidates=(
            rendered.cache_write_candidates
            if all(
                projected.text == original.text
                for projected, original in zip(
                    projected_fragments, rendered.fragments, strict=True
                )
            )
            else ()
        ),
    )


def render_tool_result_projection_variant(
    *,
    unit: ToolResultRenderUnit,
    base_fragment: RenderedToolResultFragment,
    base_decision: ToolResultRenderDecisionFact,
    representation: ToolObservationRepresentation,
    source_rollup_id: str | None,
    policy: ResolvedToolResultRenderPolicyFact,
) -> tuple[RenderedToolResultFragment, ToolResultRenderDecisionFact]:
    """Render one monotonic, pairing-safe long-horizon representation."""

    if base_fragment.unit_id != unit.unit_id or base_decision.unit_id != unit.unit_id:
        raise ValueError("projection variant base render identity mismatch")
    if representation is ToolObservationRepresentation.FULL:
        if source_rollup_id is not None:
            raise ValueError("full projection cannot reference a rollup")
        return base_fragment, base_decision
    if (representation is ToolObservationRepresentation.ROLLUP_MEMBER) != (
        source_rollup_id is not None
    ):
        raise ValueError("rollup-member projection requires exact rollup identity")

    observation = _observation_payload(unit)
    header = _tool_result_header(
        unit.model_tool_name,
        unit.result_state.value,
        observation,
    )
    primary = _primary_text_artifact(unit.artifacts)
    body = _unit_body(unit)
    visible_body = 0
    body_policy = ToolResultBodyPolicy.OMITTED_NON_ARTIFACT
    envelope_policy = ToolResultEnvelopePolicy.COMPACT
    reason = ToolResultRenderReasonCode.BUDGET_EXHAUSTED

    if representation is ToolObservationRepresentation.PREVIEW:
        preview_cap = min(
            len(body),
            max(512, min(4_096, policy.basis.per_tool_cap_chars // 4)),
        )
        preview = (
            _clip_artifact_body(unit, body, preview_cap)
            if unit.artifacts
            else _clip_text(body, preview_cap)
        )
        if not preview or len(preview) >= len(body):
            raise ValueError("preview projection does not reduce the source body")
        payload: dict[str, object] = {
            "projection": "preview",
            "output_preview": preview,
            "output_truncated": True,
        }
        if unit.essential is not None:
            payload["essential_kind"] = unit.essential.kind
        if primary is not None:
            payload["primary_text_artifact"] = _artifact_payload(primary)
        if unit.terminal_payload_timing is not None:
            payload["timing"] = _terminal_timing_payload(unit)
        text = header + _json(payload)
        visible_body = len(preview)
        body_policy = (
            ToolResultBodyPolicy.ARTIFACT_PREVIEW
            if primary is not None
            else ToolResultBodyPolicy.CLIPPED
        )
        envelope_policy = ToolResultEnvelopePolicy.COMPACT
        reason = ToolResultRenderReasonCode.ARTIFACT_PREVIEW
    else:
        payload = _projection_envelope_payload(
            unit=unit,
            representation=representation,
            source_rollup_id=source_rollup_id,
            primary=primary,
        )
        available = policy.basis.per_envelope_cap_chars - len(header)
        if available < 1:
            raise ValueError("projection envelope cap cannot hold observation header")
        rendered_payload, envelope_policy, _, terminal_timing_included = _fit_envelope(
            payload,
            cap=available,
            state=unit.result_state.value,
            primary_artifact_id=primary.artifact_id if primary else None,
        )
        if unit.terminal_payload_timing is not None and not terminal_timing_included:
            # Terminal timing is part of the typed essential carrier.  A
            # projection that cannot preserve even its minimal form is invalid.
            minimal_timing = _terminal_timing_payload(unit)
            minimal_timing = {
                key: minimal_timing[key]
                for key in ("observed_at", "duration_seconds", "freshness")
                if key in minimal_timing
            }
            fallback_payload = {
                "projection": representation.value,
                "status": unit.result_state.value,
                "tool_call_id": unit.tool_call_id,
                "timing": minimal_timing,
            }
            if primary is not None:
                fallback_payload["primary_artifact_id"] = primary.artifact_id
            rendered_payload = _json(fallback_payload)
            if len(rendered_payload) > available:
                raise ValueError(
                    "projection envelope cap cannot preserve terminal timing"
                )
            envelope_policy = ToolResultEnvelopePolicy.MINIMAL
        text = header + rendered_payload
        body_policy = (
            ToolResultBodyPolicy.OMITTED_ARTIFACT
            if primary is not None
            else ToolResultBodyPolicy.OMITTED_NON_ARTIFACT
        )
        reason = (
            ToolResultRenderReasonCode.ESSENTIAL_PRESERVED
            if representation is ToolObservationRepresentation.ESSENTIAL
            else ToolResultRenderReasonCode.BUDGET_EXHAUSTED
        )

    rendered_envelope = len(text) - visible_body
    if rendered_envelope > policy.basis.per_envelope_cap_chars:
        raise ValueError("projection variant exceeds per-unit envelope safety cap")
    projected_decision = _projected_render_decision(
        base=base_decision,
        unit=unit,
        text=text,
        visible_body=visible_body,
        body_policy=body_policy,
        envelope_policy=envelope_policy,
        reason=reason,
        primary=primary,
    )
    return (
        RenderedToolResultFragment(
            unit_id=base_fragment.unit_id,
            tool_call_id=base_fragment.tool_call_id,
            source_message_id=base_fragment.source_message_id,
            source_message_index=base_fragment.source_message_index,
            content_block_index=base_fragment.content_block_index,
            segment=base_fragment.segment,
            text=text,
            rendered_text_fingerprint=rendered_text_fingerprint(text),
        ),
        projected_decision,
    )


def validate_prepared_tool_result_render_output(
    *,
    output: PreparedToolResultRenderOutput,
    resolved_call: ResolvedModelCall,
    context_id: str,
    model_call_index: int,
) -> None:
    from pulsara_agent.runtime.context_engine.types import (
        ContextBudgetExceeded,
        ContextBudgetReport,
        ContextDiagnostic,
    )

    report = output.tool_result_budget_report
    failures = tuple(
        item
        for item in report.get("diagnostics", ())
        if isinstance(item, dict)
        and item.get("code")
        in {
            "tool_observation_timing_missing",
        }
    )
    if not failures:
        return
    target = resolved_call.target
    raise ContextBudgetExceeded(
        "Tool-result render hard cap was exceeded before model call.",
        context_id=context_id,
        model_call_index=model_call_index,
        diagnostics=tuple(
            ContextDiagnostic(
                severity="error",
                code=str(item["code"]),
                message="Tool-result render hard cap was exceeded before model call.",
                section_id="transcript:tool_results",
                metadata=dict(item),
            )
            for item in failures
        ),
        tool_result_render_decisions=(output.tool_result_render_decisions),
        tool_result_budget_report=report,
        budget_report=ContextBudgetReport(
            target_fingerprint=target.fact.target_fingerprint,
            resolved_model_call_id=resolved_call.fact.resolved_model_call_id,
            measurement_stage="tool_result_render",
            total_context_tokens=target.limits.total_context_tokens,
            max_input_tokens=target.limits.max_input_tokens,
            max_output_tokens=target.limits.max_output_tokens,
            effective_output_tokens=target.context_budget.effective_output_tokens,
            safety_margin_tokens=target.context_budget.safety_margin_tokens,
            input_budget_tokens=target.context_budget.input_budget_tokens,
            sections_estimated_tokens=None,
            tools_estimated_tokens=None,
            envelope_estimated_tokens=None,
            allocation_estimated_tokens=None,
            final_payload_estimated_tokens=None,
            non_transcript_baseline_tokens=None,
            transcript_estimated_tokens=None,
            estimator=target.token_estimator.fact,
        ),
    )


class _TypedToolResultAllocator:
    def __init__(
        self,
        *,
        policy: ResolvedToolResultRenderPolicyFact,
        token_estimator: TokenEstimator,
        cache_hints: Mapping[str, ToolResultRenderCacheHint],
        cache_configured: bool,
        cache_read_failed_unit_ids: frozenset[str],
        cache_keys: Mapping[str, str],
    ) -> None:
        self.policy = policy
        self.basis = policy.basis
        self.token_estimator = token_estimator
        self.cache_hints = cache_hints
        self.cache_configured = cache_configured
        self.cache_read_failed_unit_ids = cache_read_failed_unit_ids
        self.cache_keys = cache_keys
        self.batch_remaining: dict[str, int] = {}
        self.decisions: list[ToolResultRenderDecisionFact] = []
        self.operational: list[ToolResultRenderOperationalFact] = []

    def render(
        self,
        *,
        unit: ToolResultRenderUnit,
        segment: str,
        source_message_id: str,
        source_message_index: int,
        content_block_index: int,
    ) -> str:
        del source_message_index, content_block_index
        body = _unit_body(unit)
        original_chars, body_source = _body_candidate(unit)
        basic_header = _tool_result_header(
            unit.model_tool_name,
            unit.result_state.value,
            None,
        )
        observation = _observation_payload(unit)
        timed_header = _tool_result_header(
            unit.model_tool_name,
            unit.result_state.value,
            observation,
        )
        batch_before = self.batch_remaining.setdefault(
            unit.call_message_id,
            self.basis.per_message_cap_chars,
        )
        body_allowed = min(
            batch_before,
            self.basis.per_tool_cap_chars,
        )
        # Reserve the stable identity header. The payload may carry the complete
        # observation itself; otherwise the timing header is used only when the
        # same per-unit envelope cap can still hold it.
        envelope_allowed = max(
            0,
            self.basis.per_envelope_cap_chars - len(basic_header),
        )
        (
            rendered_payload,
            visible_body,
            body_policy,
            envelope_policy,
            reason,
            payload_contains_observation,
            payload_contains_terminal_timing,
        ) = _render_unit_payload(
            unit=unit,
            body=body,
            body_allowed=body_allowed,
            envelope_allowed=envelope_allowed,
            observation=observation,
        )
        payload_preserved = (
            not unit.artifacts
            and visible_body == len(body)
            and body_policy is ToolResultBodyPolicy.FULL_VISIBLE
            and envelope_policy is ToolResultEnvelopePolicy.FULL
            and rendered_payload == body
        )
        payload_envelope_chars = len(rendered_payload) - visible_body
        observation_in_header = (
            not payload_contains_observation
            and len(timed_header) + payload_envelope_chars
            <= self.basis.per_envelope_cap_chars
        )
        header = (
            basic_header
            if payload_contains_observation or not observation_in_header
            else timed_header
        )
        text = header + rendered_payload
        rendered_total = len(text)
        rendered_envelope = rendered_total - visible_body
        if rendered_envelope > self.basis.per_envelope_cap_chars:
            raise ValueError("tool-result per-unit envelope safety cap was exceeded")
        if visible_body > self.basis.per_tool_cap_chars:
            raise ValueError("tool-result per-unit body safety cap was exceeded")
        self.batch_remaining[unit.call_message_id] = max(
            0,
            batch_before - visible_body,
        )

        rendered_observation = (
            unit.observation_timing
            if payload_contains_observation or observation_in_header
            else None
        )
        rendered_terminal_timing = (
            unit.terminal_payload_timing if payload_contains_terminal_timing else None
        )
        diagnostics: list[ToolResultRenderDiagnosticFact] = []
        if body_policy is not ToolResultBodyPolicy.FULL_VISIBLE:
            diagnostics.append(
                _diagnostic(
                    ToolResultRenderDiagnosticCode.BUDGET_DEGRADED,
                    {"reason": reason.value},
                )
            )
        if unit.essential is not None and envelope_policy in {
            ToolResultEnvelopePolicy.MINIMAL,
            ToolResultEnvelopePolicy.OMITTED,
        }:
            diagnostics.append(
                _diagnostic(
                    ToolResultRenderDiagnosticCode.ESSENTIAL_ENVELOPE_CLIPPED,
                    {"envelope_policy": envelope_policy.value},
                )
            )
        artifact_ids = tuple(item.artifact_id for item in unit.artifacts)
        primary = _primary_text_artifact(unit.artifacts)
        decision_payload = {
            "unit_id": unit.unit_id,
            "tool_call_id": unit.tool_call_id,
            "source_message_id": source_message_id,
            "source_assistant_message_id": unit.call_message_id,
            "segment": segment,
            "render_order": len(self.decisions) + 1,
            "state": unit.result_state.value,
            "render_source_fingerprint": unit.unit_fingerprint,
            "artifact_fingerprint": context_fingerprint(
                "tool-result-render-artifacts:v1",
                tuple(item.ref_fingerprint for item in unit.artifacts),
            ),
            "original_chars": original_chars,
            "body_candidate_chars": original_chars,
            "body_candidate_source": body_source,
            "minimum_envelope_kind": (
                ToolResultMinimumEnvelopeKind.ESSENTIAL
                if unit.essential is not None
                else ToolResultMinimumEnvelopeKind.ARTIFACT
                if unit.artifacts
                else ToolResultMinimumEnvelopeKind.NONE
            ),
            "latest_reserved_candidate": False,
            "latest_reserved_applied": False,
            "latest_reserved_reason": ToolResultLatestReserveReasonCode.NOT_LATEST,
            "visible_body_chars": visible_body,
            "rendered_tool_observation": (rendered_observation),
            "observation_timing_policy": (
                "full" if rendered_observation is not None else "omitted"
            ),
            "rendered_terminal_payload_timing": rendered_terminal_timing,
            "terminal_payload_timing_policy": (
                "not_applicable"
                if unit.terminal_payload_timing is None
                else "full"
                if rendered_terminal_timing is not None
                else "omitted"
            ),
            "rendered_header_chars": len(header),
            "rendered_envelope_chars": rendered_envelope,
            "rendered_total_chars": rendered_total,
            "framing": (
                "pulsara_tool_result_header"
                if payload_preserved
                else "pulsara_tool_result_envelope"
            ),
            "payload_preserved": payload_preserved,
            "payload_format": _classify_display_payload_format(body),
            "body_budget_remaining": max(
                0, self.basis.per_tool_cap_chars - visible_body
            ),
            "message_body_budget_remaining": self.batch_remaining[unit.call_message_id],
            "envelope_budget_remaining": max(
                0, self.basis.per_envelope_cap_chars - rendered_envelope
            ),
            "primary_artifact_id": primary.artifact_id if primary else None,
            "artifact_ids": artifact_ids,
            "body_policy": body_policy,
            "envelope_policy": envelope_policy,
            "reason_code": reason,
            "clipped_envelope_fields": (),
            "read_more": (
                primary.preview.read_more
                if primary is not None and primary.preview is not None
                else freeze_json(
                    {"tool": "artifact_read", "artifact_id": primary.artifact_id}
                )
                if primary is not None
                else None
            ),
            "diagnostics": tuple(diagnostics),
        }
        normalized_decision_payload = ToolResultRenderDecisionFact.model_construct(
            **decision_payload,
            decision_fingerprint="",
        ).model_dump(mode="json", exclude={"decision_fingerprint"})
        decision = ToolResultRenderDecisionFact(
            **normalized_decision_payload,
            decision_fingerprint=context_fingerprint(
                "tool-result-render-decision:v1",
                normalized_decision_payload,
            ),
        )
        hint = self.cache_hints.get(unit.unit_id)
        if hint is None:
            if self.cache_configured:
                read_failed = unit.unit_id in self.cache_read_failed_unit_ids
                operational = ToolResultRenderOperationalFact(
                    unit_id=unit.unit_id,
                    cache_status="miss",
                    cache_key=self.cache_keys[unit.unit_id],
                    diagnostics=(
                        _diagnostic(
                            ToolResultRenderDiagnosticCode.CACHE_READ_FAILED,
                            {"reason": "cache_get_failed"},
                        ),
                    )
                    if read_failed
                    else (),
                )
            else:
                operational = ToolResultRenderOperationalFact(
                    unit_id=unit.unit_id,
                    cache_status="not_configured",
                    cache_key=None,
                    diagnostics=(),
                )
        elif hint.rendered_text == text and hint.decision == decision:
            operational = ToolResultRenderOperationalFact(
                unit_id=unit.unit_id,
                cache_status="hit",
                cache_key=hint.cache_key,
                diagnostics=(),
            )
        else:
            operational = ToolResultRenderOperationalFact(
                unit_id=unit.unit_id,
                cache_status="invalidated",
                cache_key=hint.cache_key,
                diagnostics=(
                    _diagnostic(
                        ToolResultRenderDiagnosticCode.CACHE_INVALID,
                        {"reason": "fresh_render_differs_from_hint"},
                    ),
                ),
            )
        self.decisions.append(decision)
        self.operational.append(operational)
        return text

    def report(self) -> dict[str, object]:
        total = sum(item.rendered_total_chars for item in self.decisions)
        body = sum(item.visible_body_chars for item in self.decisions)
        envelope = sum(item.rendered_envelope_chars for item in self.decisions)
        return {
            "policy_version": self.basis.policy_version,
            "per_unit_safety_caps": self.basis.model_dump(mode="json"),
            "used": {"total": total, "body": body, "envelope": envelope},
            "estimated_tokens": {
                "total": self.token_estimator.estimate_text("x" * total),
                "body": self.token_estimator.estimate_text("x" * body),
                "envelope": self.token_estimator.estimate_text("x" * envelope),
            },
            "used_by_batch": {
                key: {"remaining": value} for key, value in self.batch_remaining.items()
            },
            "render_cache": {
                "hints": len(self.cache_hints),
                "hits": sum(item.cache_status == "hit" for item in self.operational),
                "invalidated": sum(
                    item.cache_status == "invalidated" for item in self.operational
                ),
            },
            "diagnostics": [],
        }


def _render_unit_payload(
    *,
    unit: ToolResultRenderUnit,
    body: str,
    body_allowed: int,
    envelope_allowed: int,
    observation: dict[str, object],
) -> tuple[
    str,
    int,
    ToolResultBodyPolicy,
    ToolResultEnvelopePolicy,
    ToolResultRenderReasonCode,
    bool,
    bool,
]:
    primary = _primary_text_artifact(unit.artifacts)
    primary_artifact_id = primary.artifact_id if primary is not None else None
    if not unit.artifacts and len(body) <= body_allowed:
        return (
            body,
            len(body),
            ToolResultBodyPolicy.FULL_VISIBLE,
            ToolResultEnvelopePolicy.FULL,
            ToolResultRenderReasonCode.WITHIN_BUDGET,
            False,
            False,
        )
    if unit.essential is not None:
        payload = _essential_envelope(unit, observation=observation)
        rendered, policy, observation_included, terminal_timing_included = (
            _fit_envelope(
                payload,
                cap=envelope_allowed,
                state=unit.result_state.value,
                primary_artifact_id=primary_artifact_id,
            )
        )
        return (
            rendered,
            0,
            ToolResultBodyPolicy.OMITTED_NON_ARTIFACT,
            policy,
            ToolResultRenderReasonCode.ESSENTIAL_PRESERVED,
            observation_included,
            terminal_timing_included,
        )
    if unit.artifacts:
        visible = _clip_artifact_body(unit, body, body_allowed)
        payload: dict[str, object] = {
            "output_preview": visible,
            "output_truncated": len(visible) < len(body),
            "artifacts": [_artifact_payload(item) for item in unit.artifacts],
            "pulsara_tool_observation": observation,
        }
        rendered, policy, observation_included, terminal_timing_included = (
            _fit_envelope(
                payload,
                # ``output_preview`` is body budget, not envelope budget.  The
                # envelope fitter receives the combined allowance so a large,
                # already-budgeted preview is not discarded merely because its
                # JSON carrier is larger than the envelope-only allowance.
                cap=envelope_allowed + len(visible),
                state=unit.result_state.value,
                primary_artifact_id=primary_artifact_id,
            )
        )
        if (
            policy
            in {
                ToolResultEnvelopePolicy.MINIMAL,
                ToolResultEnvelopePolicy.OMITTED,
            }
            and len(rendered) > envelope_allowed
        ):
            # Once the preview body has been dropped its former body allowance
            # cannot be reclassified as envelope budget.
            rendered, policy, observation_included, terminal_timing_included = (
                _fit_envelope(
                    payload,
                    cap=envelope_allowed,
                    state=unit.result_state.value,
                    primary_artifact_id=primary_artifact_id,
                )
            )
        visible_chars = (
            len(visible)
            if policy
            in {
                ToolResultEnvelopePolicy.FULL,
                ToolResultEnvelopePolicy.COMPACT,
            }
            else 0
        )
        return (
            rendered,
            visible_chars,
            (
                ToolResultBodyPolicy.ARTIFACT_PREVIEW
                if visible_chars
                else ToolResultBodyPolicy.OMITTED_ARTIFACT
            ),
            policy,
            ToolResultRenderReasonCode.ARTIFACT_PREVIEW,
            observation_included,
            terminal_timing_included,
        )
    visible = _clip_text(body, body_allowed)
    if visible:
        return (
            visible,
            len(visible),
            ToolResultBodyPolicy.CLIPPED,
            ToolResultEnvelopePolicy.FULL,
            ToolResultRenderReasonCode.BUDGET_EXHAUSTED,
            False,
            False,
        )
    payload = {
        "output_preview": "[omitted]",
        "output_truncated": True,
        "tool_result_body_omitted": True,
        "pulsara_tool_observation": observation,
    }
    rendered, policy, observation_included, terminal_timing_included = _fit_envelope(
        payload,
        cap=envelope_allowed,
        state=unit.result_state.value,
        primary_artifact_id=None,
    )
    return (
        rendered,
        0,
        ToolResultBodyPolicy.OMITTED_NON_ARTIFACT,
        policy,
        ToolResultRenderReasonCode.BUDGET_EXHAUSTED,
        observation_included,
        terminal_timing_included,
    )


def _essential_envelope(
    unit: ToolResultRenderUnit,
    *,
    observation: dict[str, object],
) -> dict[str, object]:
    essential = unit.essential
    assert essential is not None
    raw = essential.model_dump(
        mode="json",
        exclude={"capture_policy_fingerprint", "kind", "execution_started"},
    )
    error = raw.get("error")
    if isinstance(error, dict):
        raw["error"] = str(error.get("text") or "")
        raw["error_truncated"] = bool(error.get("truncated"))
        raw["error_original_chars"] = int(error.get("original_chars") or 0)
    action = raw.pop("action", None)
    if essential.kind == "terminal_monitor_error":
        action = raw.get("requested_action")
    if essential.kind.startswith("terminal_process") and action is not None:
        raw["terminal_process_action"] = action
    if essential.kind.startswith("terminal_monitor") and action is not None:
        raw["terminal_monitor_action"] = action
    if "output_truncated" in raw:
        raw["truncated"] = raw.pop("output_truncated")
    if "process_summaries" in raw:
        raw["processes_summary"] = raw.pop("process_summaries")
    if "monitor_summaries" in raw:
        raw["monitors_summary"] = raw.pop("monitor_summaries")
    if "summaries_truncated" in raw and essential.kind == "terminal_monitor_inventory":
        raw["monitors_summary_truncated"] = raw.pop("summaries_truncated")
    elif "summaries_truncated" in raw:
        raw["processes_summary_truncated"] = raw.pop("summaries_truncated")
    payload: dict[str, object] = {
        "output_preview": (
            "[TOOL RESULT BODY OMITTED: full output is available via artifact_read]"
            if unit.artifacts
            else "[TOOL RESULT BODY OMITTED: no retained artifact]"
        ),
        "output_truncated": True,
        "tool_result_body_omitted": True,
        "tool_result_body_omitted_reason": "tool_result_render_budget_exhausted",
        **{key: value for key, value in raw.items() if value is not None},
        "pulsara_tool_observation": observation,
    }
    if unit.terminal_payload_timing is not None:
        payload["timing"] = _terminal_timing_payload(unit)
    if unit.artifacts:
        payload["artifacts"] = [_artifact_payload(item) for item in unit.artifacts]
    return payload


def _projection_envelope_payload(
    *,
    unit: ToolResultRenderUnit,
    representation: ToolObservationRepresentation,
    source_rollup_id: str | None,
    primary: ContextToolResultArtifactRefFact | None,
) -> dict[str, object]:
    payload: dict[str, object]
    if representation is ToolObservationRepresentation.ESSENTIAL:
        if unit.essential is None:
            raise ValueError("essential projection requires typed essential facts")
        payload = _essential_envelope(
            unit,
            observation=_observation_payload(unit),
        )
        payload.pop("pulsara_tool_observation", None)
        payload["projection"] = "essential"
    elif representation is ToolObservationRepresentation.ARTIFACT_LOCATOR:
        if primary is None:
            raise ValueError(
                "artifact-locator projection requires primary text artifact"
            )
        payload = {
            "projection": "artifact_locator",
            "status": unit.result_state.value,
            "tool_call_id": unit.tool_call_id,
            "primary_text_artifact": _artifact_payload(primary),
        }
    elif representation is ToolObservationRepresentation.ROLLUP_MEMBER:
        if source_rollup_id is None:
            raise ValueError("rollup-member projection lacks rollup identity")
        payload = {
            "projection": "rollup_member",
            "status": unit.result_state.value,
            "tool_call_id": unit.tool_call_id,
            "source_rollup_id": source_rollup_id,
            "result_sequence": unit.source_sequence_end,
        }
        if primary is not None:
            payload["primary_text_artifact"] = _artifact_payload(primary)
        if unit.essential is not None:
            payload["essential_kind"] = unit.essential.kind
    elif representation is ToolObservationRepresentation.PAIR_STUB:
        payload = {
            "projection": "pair_stub",
            "status": unit.result_state.value,
            "tool_call_id": unit.tool_call_id,
            "result_sequence": unit.source_sequence_end,
        }
        if primary is not None:
            payload["primary_text_artifact"] = _artifact_payload(primary)
        if unit.essential is not None:
            payload["essential_kind"] = unit.essential.kind
    else:
        raise ValueError(f"unsupported projected representation: {representation}")
    if unit.terminal_payload_timing is not None:
        payload["timing"] = _terminal_timing_payload(unit)
    return payload


def _projected_render_decision(
    *,
    base: ToolResultRenderDecisionFact,
    unit: ToolResultRenderUnit,
    text: str,
    visible_body: int,
    body_policy: ToolResultBodyPolicy,
    envelope_policy: ToolResultEnvelopePolicy,
    reason: ToolResultRenderReasonCode,
    primary: ContextToolResultArtifactRefFact | None,
) -> ToolResultRenderDecisionFact:
    payload = base.model_dump(mode="json", exclude={"decision_fingerprint"})
    rendered_envelope = len(text) - visible_body
    payload.update(
        {
            "visible_body_chars": visible_body,
            "rendered_tool_observation": unit.observation_timing,
            "observation_timing_policy": "full",
            "rendered_terminal_payload_timing": unit.terminal_payload_timing,
            "terminal_payload_timing_policy": (
                "not_applicable" if unit.terminal_payload_timing is None else "full"
            ),
            "rendered_header_chars": len(
                _tool_result_header(
                    unit.model_tool_name,
                    unit.result_state.value,
                    _observation_payload(unit),
                )
            ),
            "rendered_envelope_chars": rendered_envelope,
            "rendered_total_chars": len(text),
            "framing": "pulsara_tool_result_envelope",
            "payload_preserved": False,
            "payload_format": ToolResultPayloadFormat.JSON,
            "body_budget_remaining": max(0, base.original_chars - visible_body),
            "message_body_budget_remaining": max(0, base.message_body_budget_remaining),
            "envelope_budget_remaining": max(
                0,
                base.rendered_envelope_chars
                + base.envelope_budget_remaining
                - rendered_envelope,
            ),
            "primary_artifact_id": primary.artifact_id if primary else None,
            "body_policy": body_policy,
            "envelope_policy": envelope_policy,
            "reason_code": reason,
            "read_more": (
                primary.preview.read_more
                if primary is not None and primary.preview is not None
                else freeze_json(
                    {"tool": "artifact_read", "artifact_id": primary.artifact_id}
                )
                if primary is not None
                else None
            ),
            "diagnostics": (
                *base.diagnostics,
                _diagnostic(
                    ToolResultRenderDiagnosticCode.BUDGET_DEGRADED,
                    {"reason": "long_horizon_projection"},
                ),
            ),
        }
    )
    return ToolResultRenderDecisionFact(
        **payload,
        decision_fingerprint=context_fingerprint(
            "tool-result-render-decision:v1", payload
        ),
    )


def _projection_render_report(
    *,
    decisions: tuple[ToolResultRenderDecisionFact, ...],
    policy: ResolvedToolResultRenderPolicyFact,
    token_estimator: TokenEstimator,
    projection_state: ContextWindowProjectionState,
) -> dict[str, object]:
    total = sum(item.rendered_total_chars for item in decisions)
    body = sum(item.visible_body_chars for item in decisions)
    envelope = sum(item.rendered_envelope_chars for item in decisions)
    return {
        "policy_version": policy.basis.policy_version,
        "per_unit_safety_caps": policy.basis.model_dump(mode="json"),
        "used": {"total": total, "body": body, "envelope": envelope},
        "estimated_tokens": {
            "total": token_estimator.estimate_text("x" * total),
            "body": token_estimator.estimate_text("x" * body),
            "envelope": token_estimator.estimate_text("x" * envelope),
        },
        "projection": {
            "window_id": projection_state.window_id,
            "generation": projection_state.projection_generation,
            "state_semantic_fingerprint": (projection_state.state_semantic_fingerprint),
            "representations": {
                representation.value: sum(
                    item.representation is representation
                    for item in projection_state.unit_projections
                )
                for representation in ToolObservationRepresentation
            },
        },
        "diagnostics": [],
    }


def _fit_envelope(
    payload: dict[str, object],
    *,
    cap: int,
    state: str,
    primary_artifact_id: str | None,
) -> tuple[str, ToolResultEnvelopePolicy, bool, bool]:
    rendered = _json(payload)
    if len(rendered) <= cap:
        return (
            rendered,
            ToolResultEnvelopePolicy.FULL,
            "pulsara_tool_observation" in payload,
            "timing" in payload,
        )
    compact = dict(payload)
    compact.pop("pulsara_tool_observation", None)
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        compact["artifacts"] = _compact_artifact_payloads(
            artifacts,
            primary_artifact_id=primary_artifact_id,
        )
    timing = compact.get("timing")
    if isinstance(timing, dict):
        compact["timing"] = {
            key: timing[key]
            for key in ("observed_at", "duration_seconds", "freshness")
            if key in timing
        }
    for key in ("error", "cwd", "command", "requested_command"):
        if isinstance(compact.get(key), str):
            compact[key] = _clip_string(str(compact[key]), 96)
    summaries = compact.get("processes_summary")
    if isinstance(summaries, list):
        compact["processes_summary"] = summaries[:3]
        compact["processes_summary_truncated"] = True
    rendered = _json(compact)
    if len(rendered) <= cap:
        return (
            rendered,
            ToolResultEnvelopePolicy.COMPACT,
            "pulsara_tool_observation" in compact,
            "timing" in compact,
        )
    minimal: dict[str, object] = {
        "output_preview": "[omitted]",
        "output_truncated": True,
        "tool_result_body_omitted": True,
        "status": payload.get("status", state),
    }
    for key in (
        "exit_code",
        "process_id",
        "monitor_id",
        "terminal_process_action",
        "terminal_monitor_action",
        "error",
    ):
        if key in payload:
            minimal[key] = _clip_value(payload[key], 72)
    if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
        selected = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict)
                and item.get("artifact_id") == primary_artifact_id
            ),
            artifacts[0],
        )
        if not isinstance(selected, dict):
            raise ValueError("artifact envelope entry must be an object")
        artifact_id = selected.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            minimal_artifact: dict[str, object] = {
                "artifact_id": artifact_id,
            }
            for key in ("role", "media_type", "stored_complete"):
                if key in selected:
                    minimal_artifact[key] = selected[key]
            preview = selected.get("preview")
            if (
                artifact_id == primary_artifact_id
                and isinstance(preview, dict)
                and isinstance(preview.get("read_more"), dict)
            ):
                minimal_artifact["read_more"] = preview["read_more"]
            minimal["artifacts"] = [minimal_artifact]
    rendered = _json(minimal)
    if len(rendered) <= cap:
        return (
            rendered,
            ToolResultEnvelopePolicy.MINIMAL,
            "pulsara_tool_observation" in minimal,
            "timing" in minimal,
        )
    fallback_payload: dict[str, object] = {
        "status": state,
        "tool_result_body_omitted": True,
    }
    if primary_artifact_id is not None:
        fallback_payload["primary_artifact_id"] = primary_artifact_id
    fallback = _json(fallback_payload)
    if len(fallback) > cap:
        fallback = _json({"status": state})
    if len(fallback) > cap:
        raise ValueError("per-unit envelope cap cannot hold parseable identity")
    return fallback, ToolResultEnvelopePolicy.OMITTED, False, False


def _compact_artifact_payloads(
    artifacts: list[object],
    *,
    primary_artifact_id: str | None,
) -> list[object]:
    normalized = [dict(item) for item in artifacts if isinstance(item, dict)]
    if len(normalized) != len(artifacts):
        raise ValueError("artifact envelope entries must be objects")
    if primary_artifact_id is not None:
        matches = [
            item
            for item in normalized
            if item.get("artifact_id") == primary_artifact_id
        ]
        if len(matches) != 1:
            raise ValueError("primary artifact is absent from envelope payload")
        primary = matches[0]
        normalized = [
            primary,
            *(item for item in normalized if item is not primary),
        ]
    for item in normalized:
        if item.get("artifact_id") == primary_artifact_id:
            continue
        preview = item.get("preview")
        if isinstance(preview, dict) and "read_more" in preview:
            item["preview"] = {
                key: value for key, value in preview.items() if key != "read_more"
            }
    return normalized


def _unit_body(unit: ToolResultRenderUnit) -> str:
    parts = [item.text for item in unit.content.text_blocks]
    parts.extend(
        (
            f"[data block omitted id={item.block_id}"
            f"{f' name={item.name}' if item.name else ''} "
            f"media_type={item.media_type} source={item.source_kind}]"
        )
        for item in unit.content.data_blocks
    )
    return "\n".join(parts)


def _body_candidate(
    unit: ToolResultRenderUnit,
) -> tuple[int, ToolResultBodyCandidateSource]:
    previews = tuple(
        artifact.preview for artifact in unit.artifacts if artifact.preview is not None
    )
    if previews:
        preview = max(previews, key=lambda item: item.original_chars)
        if preview.original_chars > preview.preview_chars:
            return (
                preview.original_chars,
                ToolResultBodyCandidateSource.ARTIFACT_PREVIEW,
            )
        return preview.preview_chars, ToolResultBodyCandidateSource.ARTIFACT_PREVIEW
    if unit.content.data_blocks and not unit.content.text_blocks:
        return len(_unit_body(unit)), ToolResultBodyCandidateSource.DATA_PLACEHOLDER
    return len(_unit_body(unit)), ToolResultBodyCandidateSource.INLINE


def _primary_text_artifact(
    artifacts: tuple[ContextToolResultArtifactRefFact, ...],
) -> ContextToolResultArtifactRefFact | None:
    candidates = tuple(
        item
        for item in artifacts
        if item.role not in {"diagnostics", "metadata"}
        and (
            item.media_type.startswith("text/")
            or "json" in item.media_type
            or "xml" in item.media_type
            or "yaml" in item.media_type
        )
    )
    return next((item for item in candidates if item.preview is not None), None) or (
        candidates[0] if candidates else None
    )


def _artifact_payload(item: ContextToolResultArtifactRefFact) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_id": item.artifact_id,
        "role": item.role,
        "media_type": item.media_type,
        "size_bytes": item.size_bytes,
        "stored_complete": item.stored_complete,
    }
    if item.loss_reason is not None:
        payload["loss_reason"] = item.loss_reason
    if item.preview is not None:
        payload["preview"] = {
            "preview_policy": item.preview.preview_policy,
            "preview_chars": item.preview.preview_chars,
            "original_chars": item.preview.original_chars,
            "omitted_middle_chars": item.preview.omitted_middle_chars,
            "read_more": thaw_json(item.preview.read_more),
        }
    return payload


def _observation_payload(unit: ToolResultRenderUnit) -> dict[str, object]:
    timing = unit.observation_timing
    return {
        key: value
        for key, value in {
            "observed_at": timing.observed_at_utc,
            "source_started_at": timing.source_started_at_utc,
            "source_ended_at": timing.source_ended_at_utc,
            "observation_duration_seconds": timing.observation_duration_seconds,
            "tool_reported_duration_seconds": (timing.tool_reported_duration_seconds),
            "freshness": timing.freshness,
            "clock_source": timing.clock_source,
            "tool_origin": timing.tool_origin,
            "tool_name": timing.tool_name or unit.model_tool_name,
            "tool_call_id": unit.tool_call_id,
            "suspended_at": timing.suspended_at_utc,
            "resumed_at": timing.resumed_at_utc,
        }.items()
        if value is not None
    }


def _terminal_timing_payload(unit: ToolResultRenderUnit) -> dict[str, object]:
    timing = unit.terminal_payload_timing
    assert timing is not None
    return {
        key: value
        for key, value in {
            "observed_at": timing.observed_at_utc,
            "duration_seconds": timing.duration_seconds,
            "freshness": timing.freshness,
            "clock_source": timing.clock_source,
            "command_started_at": timing.command_started_at_utc,
            "process_started_at": timing.process_started_at_utc,
            "last_output_at": timing.last_output_at_utc,
        }.items()
        if value is not None
    }


def _tool_result_header(
    model_tool_name: str,
    state: str,
    observation: dict[str, object] | None,
) -> str:
    if observation is None:
        return f"[tool_result:{model_tool_name}:{state}]\n"
    fields = [
        f"tool_result:{model_tool_name}:{state}",
        f"observed_at={observation['observed_at']}",
    ]
    duration = observation.get("observation_duration_seconds")
    if isinstance(duration, int | float):
        fields.append(f"observation_duration={float(duration):.3f}s")
    if observation.get("freshness"):
        fields.append(f"freshness={observation['freshness']}")
    if observation.get("tool_origin"):
        fields.append(f"origin={observation['tool_origin']}")
    return "[" + "; ".join(fields) + "]\n"


def _clip_text(text: str, cap: int) -> str:
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    marker = f"\n[TOOL RESULT BODY TRUNCATED: kept {cap} of {len(text)} chars]"
    if len(marker) >= cap:
        return ""
    return text[: cap - len(marker)] + marker


def _clip_artifact_body(
    unit: ToolResultRenderUnit,
    text: str,
    cap: int,
) -> str:
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    primary = _primary_text_artifact(unit.artifacts)
    preview = primary.preview if primary is not None else None
    if preview is None or preview.visible_tail_chars <= 0:
        return _clip_text(text, cap)
    marker = f"\n[TOOL RESULT BODY TRUNCATED: kept head/tail within {cap} of {len(text)} chars]\n"
    available = cap - len(marker)
    if available < 2:
        return ""
    source_total = max(1, preview.visible_head_chars + preview.visible_tail_chars)
    head = max(1, int(available * preview.visible_head_chars / source_total))
    tail = max(1, available - head)
    if head + tail > available:
        head = available - tail
    return text[:head] + marker + text[-tail:]


def _clip_string(value: str, cap: int) -> str:
    if len(value) <= cap:
        return value
    marker = f"...[clipped {len(value) - cap} chars]"
    return value[: max(0, cap - len(marker))] + marker


def _clip_value(value: object, cap: int) -> object:
    if isinstance(value, str):
        return _clip_string(value, cap)
    return value


def _json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _classify_display_payload_format(text: str) -> ToolResultPayloadFormat:
    """Classify display syntax only; never derive execution semantics."""
    stripped = text.strip()
    if not stripped:
        return ToolResultPayloadFormat.TEXT
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return ToolResultPayloadFormat.MIXED
        return ToolResultPayloadFormat.JSON
    return ToolResultPayloadFormat.TEXT


def _render_segment(segment: str) -> str:
    if segment in {"current_user", "current_run_tail"}:
        return segment
    return "prior_history"


def _diagnostic(
    code: ToolResultRenderDiagnosticCode,
    attributes: Mapping[str, object],
) -> ToolResultRenderDiagnosticFact:
    return ToolResultRenderDiagnosticFact(
        code=code,
        severity="warning",
        attributes=tuple(
            (str(key), freeze_json(value)) for key, value in sorted(attributes.items())
        ),
    )


def _decision_event_value(
    decision: ToolResultRenderDecisionFact,
) -> dict[str, object]:
    value = decision.model_dump(mode="json")
    return dict(value)


def rendered_text_fingerprint(text: str) -> str:
    return context_fingerprint("tool-result-rendered-text:v1", text)


def _render_cache_key(
    *,
    unit: ToolResultRenderUnit,
    segment: str,
    policy_basis_fingerprint: str,
) -> str:
    return context_fingerprint(
        "tool-result-render-cache-key:v1",
        {
            "unit_id": unit.unit_id,
            "unit_fingerprint": unit.unit_fingerprint,
            "segment": segment,
            "policy_basis_fingerprint": policy_basis_fingerprint,
        },
    )


def _render_cache_write_candidate(
    fragment: RenderedToolResultFragment,
    decision: ToolResultRenderDecisionFact,
    operational: ToolResultRenderOperationalFact,
) -> ToolResultRenderCacheWriteCandidate:
    cache_key = operational.cache_key
    if cache_key is None:
        raise ValueError("render cache write requires cache key")
    hint_payload = {
        "unit_id": fragment.unit_id,
        "cache_key": cache_key,
        "rendered_text": fragment.text,
        "rendered_text_fingerprint": fragment.rendered_text_fingerprint,
        "decision": decision,
    }
    hint = ToolResultRenderCacheHint(
        **hint_payload,
        hint_fingerprint=context_fingerprint(
            "tool-result-render-cache-hint:v1",
            hint_payload,
        ),
    )
    return ToolResultRenderCacheWriteCandidate(cache_key=cache_key, hint=hint)


def _cache_admission_allowed(decision: ToolResultRenderDecisionFact) -> bool:
    return (
        decision.body_policy is ToolResultBodyPolicy.FULL_VISIBLE
        and decision.envelope_policy is ToolResultEnvelopePolicy.FULL
        and decision.reason_code is ToolResultRenderReasonCode.WITHIN_BUDGET
        and decision.payload_preserved
    )


__all__ = [
    "apply_tool_observation_projection",
    "PreparedToolResultRenderOutput",
    "RenderedToolResultFragment",
    "ToolResultRenderCacheWriteCandidate",
    "InMemoryToolResultRenderCache",
    "ToolResultRenderDecisionCachePort",
    "prepare_tool_result_render_input",
    "render_prepared_tool_result_units",
    "render_tool_result_projection_variant",
    "rendered_text_fingerprint",
    "validate_prepared_tool_result_render_output",
]
