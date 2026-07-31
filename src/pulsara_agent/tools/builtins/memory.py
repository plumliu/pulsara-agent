"""Built-in tools for proposing durable-memory write candidates.

These tools are type boundaries, not write boundaries. Each LLM-facing tool has
its own narrow schema and assembles exactly one typed ``MemoryCandidate``. A
schema-invalid call is a tool-argument error, while a schema-valid candidate is
deposited into ``MemoryProposalSink`` for an agent-loop-safe drain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar

from pulsara_agent.primitives.memory_candidate import (
    ActionBoundaryCandidate,
    ClaimCandidate,
    DecisionCandidate,
    InvalidAttemptPayload,
    MemoryCandidateBase,
    ObservationCandidate,
    PreferenceCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.memory.candidates.pool import CandidateOrigin, CandidatePoolProposal
from pulsara_agent.memory.candidates.proposal_sink import (
    MEMORY_INVALID_RETRY_LIMIT,
    MemoryProposalSink,
    MemoryRetryState,
)
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import json_text
from pulsara_agent.memory.candidates.main_agent_builder import (
    build_main_agent_memory_candidate_payload,
    main_agent_memory_candidate_entry_id,
)


@dataclass(slots=True)
class _RememberMemoryTool:
    sink: MemoryProposalSink
    runtime_session_id: str = "in-memory"

    name: ClassVar[str]
    candidate_type: ClassVar[type[MemoryCandidateBase]]
    kind: ClassVar[str]

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        intent_fingerprint = _intent_fingerprint(
            tool_name=call.name,
            attempted_kind=self.kind,
            raw_arguments=call.arguments,
        )
        candidate_payload = build_main_agent_memory_candidate_payload(
            runtime_session_id=self.runtime_session_id,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments=call.arguments,
        )
        if isinstance(candidate_payload, InvalidAttemptPayload):
            retry_state = self.sink.record_invalid(
                CandidatePoolProposal(
                    entry_id=_main_tool_candidate_entry_id(
                        runtime_session_id=self.runtime_session_id,
                        tool_call_id=call.id,
                    ),
                    payload=candidate_payload,
                    origin=CandidateOrigin.MAIN_AGENT_TOOL,
                    source_tool_call_id=call.id,
                ),
                intent_fingerprint,
            )
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=json_text(
                    _invalid_candidate_output(
                        candidate_payload.validation_error,
                        retry_state,
                    )
                ),
            )
        if not isinstance(candidate_payload, ValidCandidatePayload):
            raise TypeError(
                "main-agent memory candidate builder returned unknown payload"
            )
        candidate = candidate_payload.candidate
        self.sink.deposit_valid(
            CandidatePoolProposal(
                entry_id=_main_tool_candidate_entry_id(
                    runtime_session_id=self.runtime_session_id,
                    tool_call_id=call.id,
                ),
                payload=candidate_payload,
                origin=CandidateOrigin.MAIN_AGENT_TOOL,
                source_tool_call_id=call.id,
            ),
            intent_fingerprint,
        )
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=json_text(
                {
                    "candidate_id": candidate.candidate_id,
                    "kind": candidate.kind,
                    "status": "proposed",
                    "retry_cleared": True,
                }
            ),
        )


def _main_tool_candidate_entry_id(*, runtime_session_id: str, tool_call_id: str) -> str:
    return main_agent_memory_candidate_entry_id(
        runtime_session_id=runtime_session_id,
        tool_call_id=tool_call_id,
    )


def _intent_fingerprint(
    *, tool_name: str, attempted_kind: str, raw_arguments: dict[str, Any]
) -> str:
    statement = _normalize_intent_part(raw_arguments.get("statement"))
    scope = _normalize_intent_part(raw_arguments.get("scope"))
    if statement:
        payload = {
            "attempted_kind": attempted_kind,
            "scope": scope,
            "statement": statement,
            "tool_name": tool_name,
        }
    else:
        payload = {
            "attempted_kind": attempted_kind,
            "raw_arguments_hash": _stable_args_hash(raw_arguments),
            "tool_name": tool_name,
        }
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return (
        f"memory-intent:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:24]}"
    )


def _normalize_intent_part(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def _stable_args_hash(raw_arguments: dict[str, Any]) -> str:
    serialized = json.dumps(
        raw_arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _invalid_candidate_output(
    validation_error: str,
    retry_state: MemoryRetryState,
) -> dict[str, Any]:
    message = validation_error
    if not retry_state.retry_allowed:
        message = (
            f"{message}\nDo not retry this memory tool for the same memory intent in this run. "
            "Continue the user task; memory governance will review the final failed attempt."
        )
    return {
        "status": "invalid_candidate",
        "retry_allowed": retry_state.retry_allowed,
        "retry_count": retry_state.retry_count,
        "retry_limit": MEMORY_INVALID_RETRY_LIMIT,
        "remaining_retries": retry_state.remaining_retries,
        "message": message,
    }


class RememberClaimTool(_RememberMemoryTool):
    name = "remember_claim"
    candidate_type = ClaimCandidate
    kind = "Claim"


class RememberPreferenceTool(_RememberMemoryTool):
    name = "remember_preference"
    candidate_type = PreferenceCandidate
    kind = "Preference"


class RememberObservationTool(_RememberMemoryTool):
    name = "remember_observation"
    candidate_type = ObservationCandidate
    kind = "Observation"


class RememberActionBoundaryTool(_RememberMemoryTool):
    name = "remember_action_boundary"
    candidate_type = ActionBoundaryCandidate
    kind = "ActionBoundary"


class RememberDecisionTool(_RememberMemoryTool):
    name = "remember_decision"
    candidate_type = DecisionCandidate
    kind = "Decision"
