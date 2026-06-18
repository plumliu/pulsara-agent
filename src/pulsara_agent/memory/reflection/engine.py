"""Flash-powered best-effort durable-memory reflection."""

from __future__ import annotations

import json
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
    RunErrorEvent,
    TextBlockDeltaEvent,
)
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.event_log import EventLog
from pulsara_agent.graph import GraphStore
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.candidates.pool import CandidateOrigin, CandidatePool, CandidatePoolProposal
from pulsara_agent.memory.scope import format_scope_list
from pulsara_agent.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from pulsara_agent.ontology import runtime as rt

if TYPE_CHECKING:
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
    "记住",
    "以后",
    "总是",
    "不要",
    "偏好",
    "决定",
    "remember",
    "going forward",
    "from now on",
    "prefer",
    "preference",
    "always",
    "never",
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
    llm_options: LLMOptions = field(
        default_factory=lambda: LLMOptions(temperature=0, max_output_tokens=768)
    )
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
    graph_id: str | None = None
    allowed_scopes: frozenset[str] | None = None
    options: MemoryReflectionOptions = field(default_factory=MemoryReflectionOptions)

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
        try:
            source_events = event_store.iter(run_id=state.run_id) + pending_events
            reflection_input = self._build_input(
                state,
                source_events,
                trigger_reasons=trigger_reasons,
                cheap_hints=cheap_hints,
            )

            response = await self._call_flash(reflection_id, reflection_input)
            output = _parse_reflection_output(response)
            skipped = list(output.skipped_candidates)
            raw_candidates = output.candidates if output.should_reflect else []
            queued_count = 0

            if not output.should_reflect and output.candidates:
                skipped.extend(
                    {"reason": "reflection_decision_false", "candidate": candidate}
                    for candidate in output.candidates
                )

            for raw_candidate in raw_candidates:
                normalized = _candidate_with_id(raw_candidate)
                proposal = CandidatePoolProposal(
                    payload=ValidCandidatePayload(candidate=normalized),
                    origin=CandidateOrigin.REFLECTION,
                    user_quote=_first_quote(output.quoted_evidence),
                )
                self.candidate_pool.append_candidate(
                    proposal.to_pooled(
                        source_session_id=state.session_id,
                        source_run_id=state.run_id,
                        source_turn_id=state.turn_id,
                        source_reply_id=state.reply_id,
                    )
                )
                queued_count += 1

            completed = MemoryReflectionCompletedEvent(
                **_event_context(state).event_fields(),
                reflection_id=reflection_id,
                trigger_reason=trigger_reasons[0],
                trigger_reasons=trigger_reasons,
                safe_point=safe_point,
                should_reflect=output.should_reflect,
                decision_reason=output.reason,
                quoted_evidence=output.quoted_evidence,
                candidate_kinds=output.candidate_kinds or _candidate_kinds(raw_candidates),
                proposed_count=queued_count,
                skipped_count=len(skipped),
                written_count=0,
                failed_count=0,
                summary=output.summary,
                metadata={"skipped_candidates": skipped},
            )
            return [completed]
        except Exception as exc:
            return [
                MemoryReflectionFailedEvent(
                    **_event_context(state).event_fields(),
                    reflection_id=reflection_id,
                    trigger_reason=trigger_reasons[0],
                    trigger_reasons=trigger_reasons,
                    safe_point=safe_point,
                    error_type=type(exc).__name__,
                    message=str(exc),
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

    async def _call_flash(self, reflection_id: str, reflection_input: MemoryReflectionInput) -> str:
        text_parts: list[str] = []
        async for event in self.llm_runtime.stream(
            role=self.options.model_role,
            context=LLMContext(
                system_prompt=_reflection_system_prompt(self.allowed_scopes),
                messages=(
                    LLMMessage.user(
                        "Reflect on this run and output JSON only:\n"
                        + json.dumps(reflection_input.model_dump(mode="json"), ensure_ascii=False)
                    ),
                ),
                tools=(),
            ),
            event_context=EventContext(
                run_id=reflection_input.run_id,
                turn_id=reflection_input.turn_id,
                reply_id=f"{reflection_input.reply_id}:{reflection_id}",
            ),
            options=self.options.llm_options,
        ):
            if isinstance(event, TextBlockDeltaEvent):
                text_parts.append(event.delta)
            elif isinstance(event, RunErrorEvent):
                raise RuntimeError(event.message)
        return "".join(text_parts)


def _parse_reflection_output(text: str) -> MemoryReflectionOutput:
    payload = json.loads(_json_object_text(text))
    return MemoryReflectionOutput.model_validate(payload)


def cheap_memory_hints(text: str) -> list[MemoryReflectionHint]:
    lowered = text.lower()
    hints: list[MemoryReflectionHint] = []
    for signal in _EXPLICIT_MEMORY_SIGNALS:
        index = lowered.find(signal)
        if index < 0:
            continue
        hints.append(
            MemoryReflectionHint(
                source="cheap_string_match",
                reason="User text matched a cheap memory-trigger string. This may be a false positive.",
                signal=signal,
                excerpt=_excerpt_around(text, index),
            )
        )
    return hints


def _candidate_with_id(candidate: dict[str, Any]) -> MemoryCandidate:
    payload = dict(candidate)
    payload.setdefault("candidate_id", f"candidate:reflection:{uuid4().hex}")
    return _CANDIDATE_ADAPTER.validate_python(payload)


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
    return _unique_strings(candidate.get("kind") for candidate in candidates if isinstance(candidate.get("kind"), str))


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
        if event_type == "TOOL_CALL_START" and getattr(event, "tool_call_name", None) in _REMEMBER_TOOL_NAMES:
            attempts_by_tool_call[event.tool_call_id] = MemoryToolAttempt(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_call_name,
            )
        elif event_type == "TOOL_RESULT_TEXT_DELTA" and getattr(event, "tool_call_id", None) in attempts_by_tool_call:
            result_text_by_call.setdefault(event.tool_call_id, []).append(event.delta)
        elif event_type == "TOOL_RESULT_END" and getattr(event, "tool_call_id", None) in attempts_by_tool_call:
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
            output = "\n".join(part.text for part in block.output if isinstance(part, TextBlock))
            parts.append(f"[tool_result:{block.name}:{block.state.value}:{block.id}] {output}")
    return "\n".join(parts)


def _tool_traces(state: "LoopState") -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    calls = {call.id: call for message in state.messages for call in message.content if isinstance(call, ToolCallBlock)}
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
    return [str(record["@id"]) for record in records if isinstance(record.get("@id"), str)]


def _event_context(state: "LoopState") -> EventContext:
    return EventContext(run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id)


def _first_quote(quotes: list[str]) -> str | None:
    return next((quote for quote in quotes if quote), None)


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
