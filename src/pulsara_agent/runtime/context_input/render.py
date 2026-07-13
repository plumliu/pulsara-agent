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
    pairs = {pair.tool_call_id: pair for pair in transcript.tool_pairs}
    for unit, (_message_index, block_index, message, ref) in zip(
        units, refs, strict=True
    ):
        pair = pairs.get(ref.tool_call_id)
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
    current_tail = tuple(
        unit for unit in units if segment_by_unit[unit.unit_id] == "current_run_tail"
    )
    if current_tail:
        latest_call_message_id = current_tail[-1].call_message_id
        latest_tail = tuple(
            unit
            for unit in current_tail
            if unit.call_message_id == latest_call_message_id
        )
    else:
        latest_tail = ()
    latest_reserved = tuple(
        unit
        for unit in latest_tail
        if _body_candidate(unit)[0]
        <= policy_basis.latest_result_reserved_chars_per_unit
        and _body_candidate(unit)[1]
        is not ToolResultBodyCandidateSource.ARTIFACT_PREVIEW
    )
    latest_reserved_total = policy_basis.latest_result_reserved_chars_per_unit * len(
        latest_reserved
    )
    current_normal = max(
        0,
        policy_basis.current_run_tail_context_chars - latest_reserved_total,
    )
    resolved_payload = {
        "basis": policy_basis,
        "ordered_unit_ids": unit_ids,
        "latest_tail_unit_ids": tuple(unit.unit_id for unit in latest_tail),
        "latest_reserved_unit_ids": tuple(unit.unit_id for unit in latest_reserved),
        "latest_reserved_total_chars": latest_reserved_total,
        "current_tail_normal_context_chars": current_normal,
        "protected_current_tail_total_chars": (current_normal + latest_reserved_total),
        "initial_prior_remaining_chars": policy_basis.prior_history_context_chars,
        "initial_current_tail_remaining_chars": current_normal,
        "initial_current_user_remaining_chars": policy_basis.current_user_context_chars,
        "initial_legacy_remaining_chars": policy_basis.legacy_history_context_chars,
        "unit_order_fingerprint": context_fingerprint(
            "tool-result-unit-order:v1",
            unit_ids,
        ),
    }
    resolved = ResolvedToolResultRenderPolicyFact(
        **resolved_payload,
        policy_fingerprint=context_fingerprint(
            "resolved-tool-result-render-policy:v1",
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
    return PreparedToolResultRenderOutput(
        fragments=tuple(fragments),
        canonical_decisions=tuple(allocator.decisions),
        operational_facts=tuple(allocator.operational),
        tool_result_render_decisions=tuple(
            _decision_event_value(item) for item in allocator.decisions
        ),
        tool_result_budget_report=allocator.report(),
        cache_write_candidates=cache_writes,
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
            "tool_result_total_budget_unsatisfied",
            "max_tool_results_per_context_exceeded",
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
        self.segment_remaining = {
            "prior_history": policy.initial_prior_remaining_chars,
            "current_run_tail": policy.initial_current_tail_remaining_chars,
            "current_user": policy.initial_current_user_remaining_chars,
            "legacy_history": policy.initial_legacy_remaining_chars,
        }
        self.total_remaining = self.basis.total_context_chars
        self.body_remaining = self.basis.body_context_chars
        self.envelope_remaining = self.basis.envelope_context_chars
        self.latest_reserved_remaining = policy.latest_reserved_total_chars
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
        latest_candidate = unit.unit_id in self.policy.latest_tail_unit_ids
        latest_reserved_candidate = unit.unit_id in self.policy.latest_reserved_unit_ids
        batch_before = self.batch_remaining.setdefault(
            unit.call_message_id,
            self.basis.per_message_cap_chars,
        )
        segment_before = self.segment_remaining.get(segment, 0)
        use_reserved = (
            latest_reserved_candidate
            and self.latest_reserved_remaining > 0
            and batch_before
            >= min(
                original_chars,
                self.basis.latest_result_reserved_chars_per_unit,
            )
        )
        body_allowed = (
            self.basis.latest_result_reserved_chars_per_unit
            if use_reserved
            else segment_before
        )
        body_allowed = min(
            body_allowed,
            batch_before,
            self.basis.per_tool_cap_chars,
            self.body_remaining,
            max(0, self._total_allowed(segment) - len(timed_header)),
        )
        envelope_allowed = min(
            self.basis.per_envelope_cap_chars,
            max(0, self._total_allowed(segment) - len(basic_header)),
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
        # Raw/full or clipped bodies do not carry a structured observation
        # envelope, so the universal timing header must remain visible.  The
        # basic header is only valid when the rendered payload itself contains
        # the typed observation fact.
        header = basic_header if payload_contains_observation else timed_header
        text = header + rendered_payload
        rendered_total = len(text)
        rendered_envelope = rendered_total - visible_body
        if use_reserved and visible_body >= original_chars:
            self.latest_reserved_remaining = max(
                0,
                self.latest_reserved_remaining - visible_body,
            )
            latest_applied = True
            latest_reason = ToolResultLatestReserveReasonCode.APPLIED
        else:
            self.segment_remaining[segment] = max(
                0,
                segment_before - visible_body,
            )
            latest_applied = False
            latest_reason = (
                ToolResultLatestReserveReasonCode.NOT_LATEST
                if not latest_candidate
                else ToolResultLatestReserveReasonCode.BUDGET_UNSATISFIED
                if latest_reserved_candidate
                else ToolResultLatestReserveReasonCode.NOT_ELIGIBLE
            )
        self.batch_remaining[unit.call_message_id] = max(
            0,
            batch_before - visible_body,
        )
        self.total_remaining = max(0, self.total_remaining - rendered_total)
        self.body_remaining = max(0, self.body_remaining - visible_body)
        self.envelope_remaining = max(
            0,
            self.envelope_remaining - rendered_envelope,
        )

        rendered_observation = unit.observation_timing
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
            "latest_reserved_candidate": latest_candidate,
            "latest_reserved_applied": latest_applied,
            "latest_reserved_reason": latest_reason,
            "visible_body_chars": visible_body,
            "rendered_tool_observation": (
                unit.observation_timing if rendered_observation is not None else None
            ),
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
            "body_budget_remaining": (
                self.latest_reserved_remaining
                if latest_applied
                else self.segment_remaining[segment]
            ),
            "message_body_budget_remaining": self.batch_remaining[unit.call_message_id],
            "envelope_budget_remaining": self.envelope_remaining,
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

    def _total_allowed(self, segment: str) -> int:
        if segment != "prior_history":
            return self.total_remaining
        protected = min(
            self.total_remaining,
            self.policy.protected_current_tail_total_chars,
        )
        return max(0, self.total_remaining - protected)

    def report(self) -> dict[str, object]:
        total = sum(item.rendered_total_chars for item in self.decisions)
        body = sum(item.visible_body_chars for item in self.decisions)
        envelope = sum(item.rendered_envelope_chars for item in self.decisions)
        diagnostics: list[dict[str, object]] = []
        if total > self.basis.total_context_chars:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "tool_result_total_budget_unsatisfied",
                    "rendered_total_chars": total,
                    "cap": self.basis.total_context_chars,
                }
            )
        if body > self.basis.body_context_chars:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "tool_result_body_budget_unsatisfied",
                    "rendered_body_chars": body,
                    "cap": self.basis.body_context_chars,
                    "soft_target": True,
                }
            )
        if envelope > self.basis.envelope_context_chars:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "essential_envelope_budget_unsatisfied",
                    "rendered_envelope_chars": envelope,
                    "cap": self.basis.envelope_context_chars,
                    "soft_target": True,
                }
            )
        if len(self.decisions) > self.basis.max_tool_results_per_context:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "max_tool_results_per_context_exceeded",
                    "tool_result_count": len(self.decisions),
                    "cap": self.basis.max_tool_results_per_context,
                }
            )
        return {
            "caps": self.basis.model_dump(mode="json"),
            "used": {"total": total, "body": body, "envelope": envelope},
            "estimated_tokens": {
                "total": self.token_estimator.estimate_text("x" * total),
                "body": self.token_estimator.estimate_text("x" * body),
                "envelope": self.token_estimator.estimate_text("x" * envelope),
            },
            "remaining": {
                "total": self.total_remaining,
                "body": self.body_remaining,
                "envelope": self.envelope_remaining,
            },
            "used_by_scope": {
                key: {"remaining": value}
                for key, value in self.segment_remaining.items()
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
            "diagnostics": diagnostics,
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
    if essential.kind.startswith("terminal_process") and action is not None:
        raw["terminal_process_action"] = action
    if "output_truncated" in raw:
        raw["truncated"] = raw.pop("output_truncated")
    if "process_summaries" in raw:
        raw["processes_summary"] = raw.pop("process_summaries")
    if "summaries_truncated" in raw:
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
        "terminal_process_action",
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
    "PreparedToolResultRenderOutput",
    "RenderedToolResultFragment",
    "ToolResultRenderCacheWriteCandidate",
    "InMemoryToolResultRenderCache",
    "ToolResultRenderDecisionCachePort",
    "prepare_tool_result_render_input",
    "render_prepared_tool_result_units",
    "rendered_text_fingerprint",
    "validate_prepared_tool_result_render_output",
]
