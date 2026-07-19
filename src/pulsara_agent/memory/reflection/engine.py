"""Flash-powered best-effort durable-memory reflection."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    MemoryCandidate,
    MemoryReflectionCompletedEvent,
    MemoryReflectionFailedEvent,
)
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.event_log import EventLog
from pulsara_agent.graph import GraphStore
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.direct import (
    DirectModelCallResult,
    collect_direct_model_call_handle,
)
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.errors import (
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    CandidatePool,
    PooledMemoryCandidate,
    candidate_payload_fingerprint,
)
from pulsara_agent.memory.candidates.projection_outbox import (
    CandidateProjectionOutboxRow,
    MemoryCandidateProjectionCommitPort,
)
from pulsara_agent.memory.scope import format_scope_list
from pulsara_agent.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    CandidateProjectionOutboxItemFact,
    CandidateProjectionProducerKind,
    CandidateQuotedEvidenceLocatorFact,
    ReflectionCandidateAttributionFact,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession
    from pulsara_agent.runtime.state import LoopState


_CANDIDATE_ADAPTER = TypeAdapter(MemoryCandidate)
_REMEMBER_TOOL_NAMES = {
    "remember_claim",
    "remember_preference",
    "remember_observation",
    "remember_action_boundary",
    "remember_decision",
}
_EXPLICIT_MEMORY_SIGNALS = (
    "从现在开始",
    "我特别讨厌",
    "我真的不喜欢",
    "我真的讨厌",
    "我极其讨厌",
    "不要忘记",
    "从今以后",
    "我比较喜欢",
    "我不是这个意思",
    "make sure you",
    "just so you know",
    "going forward",
    "from now on",
    "from then on",
    "in the future",
    "make sure to",
    "stop doing",
    "stop saying",
    "don't forget",
    "keep in mind",
    "for the record",
    "like i said",
    "what i meant was",
    "i really dislike",
    "i don't like",
    "i never",
    "我更喜欢",
    "我不喜欢",
    "我通常",
    "我常常",
    "我一般",
    "我习惯",
    "我总",
    "我讨厌",
    "以后都",
    "不要再",
    "不是这个意思",
    "我的意思是",
    "千万不要",
    "千万别",
    "别忘了",
    "记下来",
    "你要记住",
    "你得记住",
    "你应该",
    "你需要",
    "你必须",
    "you always",
    "i told you",
    "i usually",
    "i always",
    "i prefer",
    "i'd rather",
    "i hate",
    "i dislike",
    "my favorite",
    "next time",
    "take note",
    "that's not what i meant",
    "please don't",
    "别再",
    "我更偏好",
    "我更偏爱",
    "我更爱",
    "决定",
    "remember",
    "prefer",
    "preference",
    "do not",
    "don't",
)


class MemoryToolAttempt(BaseModel):
    tool_call_id: str | None = None
    tool_name: str | None = None
    candidate_id: str | None = None
    candidate: dict[str, Any] | None = None
    memory_type: str | None = None
    status: str | None = None
    memory_id: str | None = None
    gate_reason: str | None = None
    error_message: str | None = None


class MemoryReflectionHint(BaseModel):
    source: str
    reason: str
    signal: str | None = None
    excerpt: str | None = None


class MemoryReflectionInput(BaseModel):
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    graph_id: str | None = None
    trigger_reasons: list[str] = Field(default_factory=list)
    cheap_hints: list[MemoryReflectionHint] = Field(default_factory=list)
    prior_reflections: list[dict[str, Any]] = Field(default_factory=list)
    user_message_summary: str
    assistant_reply_summary: str
    tool_traces: list[dict[str, Any]] = Field(default_factory=list)
    memory_projection_ids: list[str] = Field(default_factory=list)
    memory_tool_attempts: list[MemoryToolAttempt] = Field(default_factory=list)
    available_evidence_ids: list[str] = Field(default_factory=list)
    allowed_scopes: list[str] = Field(default_factory=list)


class MemoryReflectionOutput(BaseModel):
    should_reflect: bool = True
    reason: str = ""
    quoted_evidence: list[str] = Field(default_factory=list)
    candidate_kinds: list[str] = Field(default_factory=list)
    summary: str = ""
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    skipped_candidates: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryReflectionOptions:
    model_role: ModelRole = ModelRole.FLASH
    llm_options: LLMOptions = field(default_factory=LLMOptions)
    max_summary_chars: int = 4_000
    tool_call_threshold: int = 3
    turn_threshold: int = 5
    token_delta_threshold: int = 4_000
    min_runs_between_reflections: int = 1


@dataclass(slots=True)
class MemoryReflectionEngine:
    llm_runtime: LLMRuntime
    candidate_pool: CandidatePool
    graph: GraphStore
    runtime_session: "RuntimeSession | None" = None
    graph_id: str | None = None
    allowed_scopes: frozenset[str] | None = None
    options: MemoryReflectionOptions = field(default_factory=MemoryReflectionOptions)
    candidate_projection_commit_port: MemoryCandidateProjectionCommitPort | None = None

    async def reflect(
        self,
        *,
        state: "LoopState",
        event_store: EventLog,
        pending_events: list[AgentEvent] | None = None,
        trigger_reasons: list[str] | None = None,
        cheap_hints: list[MemoryReflectionHint] | None = None,
        safe_point: str = "manual",
    ) -> list[AgentEvent]:
        reflection_id = f"memory-reflection:{uuid4().hex}"
        pending_events = pending_events or []
        trigger_reasons = _unique_strings(trigger_reasons or [])
        cheap_hints = cheap_hints or []
        if not trigger_reasons:
            return []
        failure_stage = "input_build"
        resolved_call = None
        call_result: DirectModelCallResult | None = None
        try:
            source_events = event_store.iter(run_id=state.run_id) + pending_events
            reflection_input = self._build_input(
                state,
                source_events,
                trigger_reasons=trigger_reasons,
                cheap_hints=cheap_hints,
            )

            failure_stage = "target_resolution"
            target = self.llm_runtime.resolve_target(
                role=self.options.model_role,
                requested_options=self.options.llm_options,
            )
            failure_stage = "call_resolution"
            resolved_call = self.llm_runtime.resolve_call(
                target=target,
                purpose=ModelCallPurpose.MEMORY_REFLECTION,
            )
            failure_stage = "model_validation"
            call_result = await self._call_flash(
                reflection_id,
                reflection_input,
                call=resolved_call,
            )
            failure_stage = "model_stream"
            if call_result.outcome != "completed":
                raise RuntimeError(
                    call_result.error.message if call_result.error else "provider error"
                )
            failure_stage = "output_parse"
            output = _parse_reflection_output(call_result.text)
            skipped = list(output.skipped_candidates)
            raw_candidates = output.candidates if output.should_reflect else []
            pooled_candidates: list[PooledMemoryCandidate] = []
            candidate_attributions: list[ReflectionCandidateAttributionFact] = []

            if not output.should_reflect and output.candidates:
                skipped.extend(
                    {"reason": "reflection_decision_false", "candidate": candidate}
                    for candidate in output.candidates
                )

            failure_stage = "candidate_append"
            quote_indices = tuple(
                index for index, value in enumerate(output.quoted_evidence) if value
            )
            quote_index = quote_indices[0] if quote_indices else None
            quote = (
                output.quoted_evidence[quote_index] if quote_index is not None else None
            )
            for candidate_index, raw_candidate in enumerate(raw_candidates):
                normalized = _candidate_with_id(
                    raw_candidate,
                    reflection_id=reflection_id,
                    candidate_index=candidate_index,
                )
                payload = ValidCandidatePayload(candidate=normalized)
                payload_fingerprint = candidate_payload_fingerprint(payload)
                entry_id = _reflection_candidate_entry_id(
                    reflection_id=reflection_id,
                    candidate_index=candidate_index,
                    payload_fingerprint=payload_fingerprint,
                )
                locator = (
                    build_frozen_fact(
                        CandidateQuotedEvidenceLocatorFact,
                        schema_version="candidate_quoted_evidence_locator.v1",
                        locator_kind="reflection_quote_index",
                        source_message_id=None,
                        source_event_reference=None,
                        source_artifact_reference=None,
                        source_quote_index=quote_index,
                        start_char=None,
                        end_char=None,
                        quoted_text_sha256=hashlib.sha256(
                            (quote or "").encode("utf-8")
                        ).hexdigest(),
                    )
                    if quote_index is not None
                    else None
                )
                attribution = build_frozen_fact(
                    ReflectionCandidateAttributionFact,
                    schema_version="reflection_candidate_attribution.v1",
                    candidate_entry_id=entry_id,
                    candidate_index=candidate_index,
                    candidate_payload=payload,
                    candidate_payload_fingerprint=payload_fingerprint,
                    intent_fingerprint=None,
                    ordered_quoted_evidence_indices=quote_indices,
                )
                candidate_attributions.append(attribution)
                pooled_candidates.append(
                    PooledMemoryCandidate(
                        entry_id=entry_id,
                        payload=payload,
                        origin=CandidateOrigin.REFLECTION,
                        source_session_id=state.session_id,
                        source_run_id=state.run_id,
                        source_turn_id=state.turn_id,
                        source_reply_id=state.reply_id,
                        user_quote=quote,
                        quoted_evidence_locator=locator,
                        source_event_id=f"{reflection_id}:completed",
                        metadata={
                            "reflection_id": reflection_id,
                            "candidate_index": candidate_index,
                        },
                    )
                )

            completed = MemoryReflectionCompletedEvent(
                id=f"{reflection_id}:completed",
                **_event_context(state).event_fields(),
                reflection_id=reflection_id,
                trigger_reason=trigger_reasons[0],
                trigger_reasons=trigger_reasons,
                safe_point=safe_point,
                should_reflect=output.should_reflect,
                decision_reason=output.reason,
                quoted_evidence=output.quoted_evidence,
                candidate_kinds=[
                    item.candidate.kind
                    for item in (candidate.payload for candidate in pooled_candidates)
                ],
                proposed_count=len(pooled_candidates),
                skipped_count=len(skipped),
                written_count=0,
                failed_count=0,
                summary=output.summary,
                resolved_call=call_result.resolved_call,
                usage_status=call_result.usage_status,
                usage=call_result.usage,
                estimated_input_tokens=call_result.estimated_input_tokens,
                reported_model_id=call_result.reported_model_id,
                reflection_model_call_end_event_identity=(
                    call_result.model_call_end_event_identity
                ),
                reflection_model_result_semantic_fingerprint=context_fingerprint(
                    "reflection-model-result-semantic:v1",
                    {"text": call_result.text},
                ),
                reflection_policy_contract_fingerprint=(
                    _reflection_policy_contract_fingerprint(self.options)
                ),
                ordered_candidate_attributions=tuple(candidate_attributions),
                metadata={"skipped_candidates": skipped},
            )
            if self.candidate_projection_commit_port is None:
                raise RuntimeError(
                    "memory reflection requires candidate projection commit ownership"
                )
            producer_identity = stable_event_identity(
                completed,
                runtime_session_id=state.session_id,
            )
            outbox_rows = tuple(
                CandidateProjectionOutboxRow(
                    item=build_frozen_fact(
                        CandidateProjectionOutboxItemFact,
                        schema_version="candidate_projection_outbox_item.v1",
                        producer_kind=CandidateProjectionProducerKind.REFLECTION,
                        producer_event_identity=producer_identity,
                        candidate_entry_id=candidate.entry_id,
                        candidate_index=index,
                        candidate_payload=candidate.payload,
                        candidate_payload_fingerprint=(
                            candidate_payload_fingerprint(candidate.payload)
                        ),
                        candidate_attribution_fingerprint=(
                            candidate_attributions[index].attribution_fingerprint
                        ),
                    ),
                    candidate=candidate,
                )
                for index, candidate in enumerate(pooled_candidates)
            )
            await self.candidate_projection_commit_port.commit_producer_bundle(
                producer_event=completed,
                rows=outbox_rows,
            )
            return []
        except Exception as exc:
            if failure_stage == "model_stream" and isinstance(
                exc,
                (
                    ModelInputBudgetExceeded,
                    ModelInputEstimateMismatch,
                    ModelContextIdentityMismatch,
                    ModelTargetCapabilityMismatch,
                    ModelTargetBindingMismatch,
                ),
            ):
                failure_stage = "model_validation"
            estimate = getattr(exc, "estimate", None)
            return [
                MemoryReflectionFailedEvent(
                    **_event_context(state).event_fields(),
                    reflection_id=reflection_id,
                    trigger_reason=trigger_reasons[0],
                    trigger_reasons=trigger_reasons,
                    safe_point=safe_point,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    failure_stage=failure_stage,
                    resolved_call=(
                        call_result.resolved_call
                        if call_result is not None
                        else resolved_call.fact
                        if resolved_call is not None
                        else None
                    ),
                    usage_status=(
                        call_result.usage_status
                        if call_result is not None
                        else "missing"
                    ),
                    usage=call_result.usage if call_result is not None else None,
                    estimated_input_tokens=(
                        call_result.estimated_input_tokens
                        if call_result is not None
                        else estimate.total_input_tokens
                        if estimate is not None
                        else None
                    ),
                    reported_model_id=(
                        call_result.reported_model_id
                        if call_result is not None
                        else None
                    ),
                )
            ]

    def _build_input(
        self,
        state: "LoopState",
        events: list[AgentEvent],
        *,
        trigger_reasons: list[str],
        cheap_hints: list[MemoryReflectionHint],
    ) -> MemoryReflectionInput:
        attempts = _memory_tool_attempts(events)
        user_summary = _message_summary(
            [message for message in state.messages if message.role == "user"],
            self.options.max_summary_chars,
        )
        return MemoryReflectionInput(
            runtime_session_id=state.session_id,
            run_id=state.run_id,
            turn_id=state.turn_id,
            reply_id=state.reply_id,
            graph_id=self.graph_id,
            trigger_reasons=trigger_reasons,
            cheap_hints=cheap_hints,
            prior_reflections=_prior_reflections(events),
            user_message_summary=user_summary,
            assistant_reply_summary=_message_summary(
                [message for message in state.messages if message.role == "assistant"],
                self.options.max_summary_chars,
            ),
            tool_traces=_tool_traces(state),
            memory_projection_ids=_projection_ids(state.memory_projection),
            memory_tool_attempts=attempts,
            available_evidence_ids=_available_evidence_ids(self.graph, self.graph_id),
            allowed_scopes=sorted(self.allowed_scopes or ()),
        )

    async def _call_flash(
        self,
        reflection_id: str,
        reflection_input: MemoryReflectionInput,
        *,
        call,
    ) -> DirectModelCallResult:
        context = LLMContext(
            system_prompt=_reflection_system_prompt(self.allowed_scopes),
            messages=(
                LLMMessage.user(
                    "Reflect on this run and output JSON only:\n"
                    + json.dumps(
                        reflection_input.model_dump(mode="json"), ensure_ascii=False
                    )
                ),
            ),
            tools=(),
            context_id=f"context:reflection:{reflection_id}",
            resolved_model_call_id=call.fact.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            model_call_index=None,
        )
        event_context = EventContext(
            run_id=reflection_input.run_id,
            turn_id=reflection_input.turn_id,
            reply_id=f"{reflection_input.reply_id}:{reflection_id}",
        )
        if self.runtime_session is None:
            raise RuntimeError(
                "memory reflection model execution requires RuntimeSession ownership"
            )
        provider_input = await self.runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
            call=call,
            context=context,
            event_context=event_context,
            operation_kind="direct_model_call",
            operation_id=reflection_id,
        )
        try:
            context = provider_input.carrier.to_llm_context(context)
            start_bundle = prepare_model_lifecycle_start_bundle(
                call=call,
                context=context,
                event_context=event_context,
                runtime_session=self.runtime_session,
                lifecycle_kind="direct_internal_call",
                provider_input_start_bundle=provider_input,
            )
            handle = self.llm_runtime.start_stream(
                call=call,
                context=context,
                event_context=event_context,
                start_bundle=start_bundle,
                commit_port=RuntimeSessionModelStreamEventCommitPort(
                    runtime_session=self.runtime_session,
                    state=None,
                ),
                execution_registry=(
                    self.runtime_session.model_stream_execution_registry
                ),
            )
        except BaseException:
            await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                provider_input.prepared_candidate.preparation_ownership.preparation_id,
                reason="one_shot_failed_before_start",
            )
            raise
        return await collect_direct_model_call_handle(
            handle,
            expected_call=call,
            runtime_session_id=self.runtime_session.runtime_session_id,
        )


def _parse_reflection_output(text: str) -> MemoryReflectionOutput:
    payload = json.loads(_json_object_text(text))
    return MemoryReflectionOutput.model_validate(payload)


def cheap_memory_hints(text: str) -> list[MemoryReflectionHint]:
    lowered = text.lower()
    matches: list[tuple[int, MemoryReflectionHint]] = []
    matched_spans: list[tuple[int, int]] = []
    for signal in sorted(_EXPLICIT_MEMORY_SIGNALS, key=len, reverse=True):
        index = lowered.find(signal)
        if index < 0:
            continue
        end = index + len(signal)
        if any(
            index < matched_end and end > matched_start
            for matched_start, matched_end in matched_spans
        ):
            continue
        matched_spans.append((index, end))
        matches.append(
            (
                index,
                MemoryReflectionHint(
                    source="cheap_string_match",
                    reason="User text matched a cheap memory-trigger string. This may be a false positive.",
                    signal=signal,
                    excerpt=_excerpt_around(text, index),
                ),
            )
        )
    return [hint for _, hint in sorted(matches, key=lambda match: match[0])]


def _candidate_with_id(
    candidate: dict[str, Any],
    *,
    reflection_id: str,
    candidate_index: int,
) -> MemoryCandidate:
    payload = dict(candidate)
    identity = context_fingerprint(
        "reflection-candidate-id:v1",
        {
            "reflection_id": reflection_id,
            "candidate_index": candidate_index,
            "candidate": {
                key: value for key, value in payload.items() if key != "candidate_id"
            },
        },
    ).removeprefix("sha256:")
    payload["candidate_id"] = f"candidate:reflection:{identity}"
    return _CANDIDATE_ADAPTER.validate_python(payload)


def _reflection_candidate_entry_id(
    *,
    reflection_id: str,
    candidate_index: int,
    payload_fingerprint: str,
) -> str:
    digest = context_fingerprint(
        "reflection-candidate-entry:v1",
        (reflection_id, candidate_index, payload_fingerprint),
    ).removeprefix("sha256:")
    return f"pool:reflection:{digest}"


def _reflection_policy_contract_fingerprint(
    options: MemoryReflectionOptions,
) -> str:
    return context_fingerprint(
        "memory-reflection-policy-contract:v1",
        {
            "model_role": options.model_role.value,
            "max_summary_chars": options.max_summary_chars,
            "tool_call_threshold": options.tool_call_threshold,
            "turn_threshold": options.turn_threshold,
            "token_delta_threshold": options.token_delta_threshold,
            "min_runs_between_reflections": options.min_runs_between_reflections,
        },
    )


def _json_object_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Memory reflection response did not contain a JSON object")
    return stripped[start : end + 1]


def _candidate_kinds(candidates: list[dict[str, Any]]) -> list[str]:
    return _unique_strings(
        candidate.get("kind")
        for candidate in candidates
        if isinstance(candidate.get("kind"), str)
    )


def _prior_reflections(events: list[AgentEvent]) -> list[dict[str, Any]]:
    prior: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, MemoryReflectionCompletedEvent):
            prior.append(
                {
                    "event_type": "MemoryReflectionCompletedEvent",
                    "reflection_id": event.reflection_id,
                    "trigger_reasons": event.trigger_reasons or [event.trigger_reason],
                    "safe_point": event.safe_point,
                    "should_reflect": event.should_reflect,
                    "decision_reason": event.decision_reason,
                    "summary": event.summary,
                    "proposed_count": event.proposed_count,
                    "skipped_count": event.skipped_count,
                    "written_count": event.written_count,
                    "failed_count": event.failed_count,
                }
            )
        elif isinstance(event, MemoryReflectionFailedEvent):
            prior.append(
                {
                    "event_type": "MemoryReflectionFailedEvent",
                    "reflection_id": event.reflection_id,
                    "trigger_reasons": event.trigger_reasons or [event.trigger_reason],
                    "safe_point": event.safe_point,
                    "error_type": event.error_type,
                    "message": event.message,
                }
            )
    return prior


def _memory_tool_attempts(events: list[AgentEvent]) -> list[MemoryToolAttempt]:
    attempts_by_candidate: dict[str, MemoryToolAttempt] = {}
    attempts_by_tool_call: dict[str, MemoryToolAttempt] = {}
    result_text_by_call: dict[str, list[str]] = {}

    for event in events:
        event_type = getattr(event, "type", None)
        if (
            event_type == "TOOL_CALL_START"
            and getattr(event, "tool_call_name", None) in _REMEMBER_TOOL_NAMES
        ):
            attempts_by_tool_call[event.tool_call_id] = MemoryToolAttempt(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_call_name,
            )
        elif (
            event_type == "TOOL_RESULT_TEXT_DELTA"
            and getattr(event, "tool_call_id", None) in attempts_by_tool_call
        ):
            result_text_by_call.setdefault(event.tool_call_id, []).append(event.delta)
        elif (
            event_type == "TOOL_RESULT_END"
            and getattr(event, "tool_call_id", None) in attempts_by_tool_call
        ):
            attempt = attempts_by_tool_call[event.tool_call_id]
            attempt.status = str(event.state.value)
            output = "".join(result_text_by_call.get(event.tool_call_id, []))
            attempt.error_message = output if attempt.status != "success" else None
            candidate_id = _candidate_id_from_tool_output(output)
            if candidate_id is not None:
                attempt.candidate_id = candidate_id
                attempts_by_candidate[candidate_id] = attempt

    attempts = list(attempts_by_tool_call.values())
    for candidate_id, attempt in attempts_by_candidate.items():
        if not any(existing.candidate_id == candidate_id for existing in attempts):
            attempts.append(attempt)
    return attempts


def _candidate_id_from_tool_output(output: str) -> str | None:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    candidate_id = payload.get("candidate_id") if isinstance(payload, dict) else None
    return candidate_id if isinstance(candidate_id, str) else None


def _message_summary(messages: list[Msg], max_chars: int) -> str:
    text = "\n".join(_message_text(message) for message in messages)
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _message_text(message: Msg) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolCallBlock):
            parts.append(f"[tool_call:{block.name}:{block.id}] {block.input}")
        elif isinstance(block, ToolResultBlock):
            output = "\n".join(
                part.text for part in block.output if isinstance(part, TextBlock)
            )
            parts.append(
                f"[tool_result:{block.name}:{block.state.value}:{block.id}] {output}"
            )
    return "\n".join(parts)


def _tool_traces(state: "LoopState") -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    calls = {
        call.id: call
        for message in state.messages
        for call in message.content
        if isinstance(call, ToolCallBlock)
    }
    results = [
        result
        for message in state.messages
        for result in message.content
        if isinstance(result, ToolResultBlock)
    ]
    for result in results:
        call = calls.get(result.id)
        traces.append(
            {
                "tool_call_id": result.id,
                "tool_name": result.name,
                "arguments": call.input if call is not None else "",
                "status": result.state.value,
                "result_summary": "\n".join(
                    part.text for part in result.output if isinstance(part, TextBlock)
                )[:1_000],
            }
        )
    return traces


def _projection_ids(projection: dict[str, Any] | None) -> list[str]:
    if not projection:
        return []
    ids = projection.get("included_memory_ids")
    if isinstance(ids, list):
        return [item for item in ids if isinstance(item, str)]
    return []


def _available_evidence_ids(graph: GraphStore, graph_id: str | None) -> list[str]:
    try:
        records = graph.find_by_type(rt.EVIDENCE, graph_id=graph_id)
    except Exception:
        return []
    return [
        str(record["@id"]) for record in records if isinstance(record.get("@id"), str)
    ]


def _event_context(state: "LoopState") -> EventContext:
    return EventContext(
        run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id
    )


def _excerpt_around(text: str, index: int, radius: int = 80) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    return text[start:end].strip()


def _unique_strings(values) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


_REFLECTION_SYSTEM_PROMPT = """
You are Pulsara's memory reflection worker.

You inspect one completed run and decide whether to propose durable memory
candidates. You are not the main assistant. Do not answer the user.

Return JSON only, with this shape:
{
  "should_reflect": true,
  "reason": "why this run does or does not contain durable memory",
  "quoted_evidence": ["short user/tool quotes that justify the decision"],
  "candidate_kinds": ["Preference"],
  "summary": "short reflection summary",
  "candidates": [
    {
      "kind": "Preference",
      "statement": "...",
      "scope": "ctx:user",
      "source_authority": "explicit_user_instruction",
      "verification_status": "user_confirmed",
      "evidence_ids": []
    }
  ],
  "skipped_candidates": [
    {"reason": "duplicate or not durable", "statement": "..."}
  ]
}

Allowed kinds are Claim, Preference, Observation, ActionBoundary, and Decision.
ActionBoundary requires applies_when and do_not_apply_when. Decision may include
based_on_ids. Do not include candidate_id.

Rules:
- Prefer no candidate over weak or redundant memory.
- cheap hints are hints and may be false positives. Inspect the full input
  critically before proposing candidates.
- If there is no durable future-use memory, set should_reflect=false and
  candidates=[].
- Candidate scopes must be one of allowed_scopes when allowed_scopes is non-empty.
- Use ctx:user only for durable user-wide preferences or habits.
- Use the exact allowed ctx:workspace/<id> scope only for durable current-project facts or decisions.
- Do not propose durable memory for one-off task details.
- Do not propose a candidate if memory_tool_attempts already attempted or
  proposed the same memory; governance owns repair and final writing.
- Do not write recalled memory_projection ids back as new memory.
- Use evidence_ids only from available_evidence_ids; otherwise use [].
- Only durable future-use information belongs in candidates.
- Explicit user instructions may use source_authority explicit_user_instruction
  and verification_status user_confirmed.

Few-shot examples:

Example A: explicit durable preference
Input facts:
- cheap_hints matched "remember"
- user_message_summary: "Please remember that I prefer concise summaries."
Output:
{
  "should_reflect": true,
  "reason": "The user explicitly asked to remember a future-use preference.",
  "quoted_evidence": ["Please remember that I prefer concise summaries."],
  "candidate_kinds": ["Preference"],
  "summary": "Durable user preference found.",
  "candidates": [
    {
      "kind": "Preference",
      "statement": "The user prefers concise summaries.",
      "scope": "ctx:user",
      "source_authority": "explicit_user_instruction",
      "verification_status": "user_confirmed",
      "evidence_ids": []
    }
  ],
  "skipped_candidates": []
}

Example B: cheap hint false positive
Input facts:
- cheap_hints matched "以后"
- user_message_summary: "以后再说吧，先看当前测试结果。"
Output:
{
  "should_reflect": false,
  "reason": "The cheap hint is a temporal phrase, not a durable instruction or preference.",
  "quoted_evidence": ["以后再说吧，先看当前测试结果。"],
  "candidate_kinds": [],
  "summary": "No durable memory candidate.",
  "candidates": [],
  "skipped_candidates": [
    {"reason": "cheap_hint_false_positive", "statement": "以后再说吧"}
  ]
}

Example C: main agent already wrote the memory
Input facts:
- memory_tool_attempts contains a successful remember_preference for
  "The user prefers concise summaries."
Output:
{
  "should_reflect": true,
  "reason": "The main agent already wrote the relevant memory; do not duplicate it.",
  "quoted_evidence": ["The user prefers concise summaries."],
  "candidate_kinds": [],
  "summary": "Skipped duplicate main-agent memory write.",
  "candidates": [],
  "skipped_candidates": [
    {"reason": "already_written_by_main_agent", "statement": "The user prefers concise summaries."}
  ]
}

Example D: main-agent memory attempt means reflection should stand down
Input facts:
- memory_tool_attempts contains remember_action_boundary for
  "Never commit code unless I explicitly ask."
Output:
{
  "should_reflect": false,
  "reason": "The main agent already attempted the memory path. Any validation repair belongs to candidate-pool governance, not run-end reflection.",
  "quoted_evidence": ["Never commit code unless I explicitly ask."],
  "candidate_kinds": [],
  "summary": "No reflection candidate; governance will handle the tool attempt.",
  "candidates": [],
  "skipped_candidates": [
    {"reason": "main_agent_memory_attempt_present", "statement": "Never commit code unless I explicitly ask."}
  ]
}

Example E: projection echo
Input facts:
- memory_projection_ids contains "memory:pref-existing"
- user_message_summary has no new preference.
Output:
{
  "should_reflect": false,
  "reason": "The apparent memory comes only from recalled projection, not new run evidence.",
  "quoted_evidence": [],
  "candidate_kinds": [],
  "summary": "Rejected projection echo.",
  "candidates": [],
  "skipped_candidates": [
    {"reason": "projection_echo", "memory_id": "memory:pref-existing"}
  ]
}

Example F: tool evidence supports observation
Input facts:
- available_evidence_ids contains "evidence:tool-result-1"
- tool_traces shows read_file found "PULSARA_CONFIG_OK".
Output:
{
  "should_reflect": true,
  "reason": "A tool result verified a workspace observation that may matter later.",
  "quoted_evidence": ["PULSARA_CONFIG_OK"],
  "candidate_kinds": ["Observation"],
  "summary": "Tool-backed observation found.",
  "candidates": [
    {
      "kind": "Observation",
      "statement": "The current workspace contains PULSARA_CONFIG_OK in the inspected file.",
      "scope": "ctx:workspace/test_workspace",
      "source_authority": "tool_result",
      "verification_status": "tool_verified",
      "evidence_ids": ["evidence:tool-result-1"]
    }
  ],
  "skipped_candidates": []
}
""".strip()


def _reflection_system_prompt(allowed_scopes: frozenset[str] | None) -> str:
    if not allowed_scopes:
        return _REFLECTION_SYSTEM_PROMPT
    return "\n".join(
        [
            _REFLECTION_SYSTEM_PROMPT,
            "",
            "Allowed scopes for this run: " + format_scope_list(allowed_scopes) + ".",
        ]
    )
