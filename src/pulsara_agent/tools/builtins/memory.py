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
from uuid import uuid4

from pydantic import ValidationError

from pulsara_agent.event.candidates import (
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
from pulsara_agent.ontology import memory
from pulsara_agent.memory.candidate_pool import CandidateOrigin, CandidatePoolProposal
from pulsara_agent.runtime.proposal_sink import MEMORY_INVALID_RETRY_LIMIT, MemoryProposalSink, MemoryRetryState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import json_text, object_schema


_SOURCE_AUTHORITIES = [item.value for item in memory.SourceAuthority]
_VERIFICATION_STATUSES = [item.value for item in memory.VerificationStatus]


def _common_properties() -> dict[str, Any]:
    return {
        "statement": {
            "type": "string",
            "description": "The durable memory content as a single declarative statement.",
        },
        "scope": {
            "type": "string",
            "description": "Scope this memory applies to, e.g. ctx:user, ctx:project, or ctx:workspace.",
        },
        "source_authority": {
            "type": "string",
            "enum": _SOURCE_AUTHORITIES,
            "description": "Where the authority for this memory comes from.",
        },
        "verification_status": {
            "type": "string",
            "enum": _VERIFICATION_STATUSES,
            "description": "How well this memory is verified.",
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence node ids that support this memory.",
        },
    }


def _memory_parameters(
    *,
    extra_properties: dict[str, Any] | None = None,
    extra_required: list[str] | None = None,
) -> dict[str, Any]:
    properties = _common_properties()
    properties.update(extra_properties or {})
    return object_schema(
        properties=properties,
        required=[
            "statement",
            "scope",
            "source_authority",
            "verification_status",
            *(extra_required or []),
        ],
    )


_COMMON_PARAMETERS = _memory_parameters()
_ACTION_BOUNDARY_PARAMETERS = _memory_parameters(
    extra_properties={
        "applies_when": {
            "type": "string",
            "description": "Condition under which this action boundary applies.",
        },
        "do_not_apply_when": {
            "type": "string",
            "description": "Condition under which this action boundary does not apply.",
        },
    },
    extra_required=["applies_when", "do_not_apply_when"],
)
_DECISION_PARAMETERS = _memory_parameters(
    extra_properties={
        "based_on_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Prior memory ids this decision builds on.",
        },
    }
)


@dataclass(slots=True)
class _RememberMemoryTool:
    sink: MemoryProposalSink

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]
    candidate_type: ClassVar[type[MemoryCandidateBase]]
    kind: ClassVar[str]
    is_read_only: ClassVar[bool] = False
    is_concurrency_safe: ClassVar[bool] = False

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        intent_fingerprint = _intent_fingerprint(
            tool_name=call.name,
            attempted_kind=self.kind,
            raw_arguments=call.arguments,
        )
        payload = {
            **call.arguments,
            "candidate_id": f"candidate:{uuid4().hex}",
            "kind": self.kind,
        }
        try:
            candidate = self.candidate_type.model_validate(payload)
        except ValidationError as exc:
            retry_state = self.sink.record_invalid(
                CandidatePoolProposal(
                    payload=InvalidAttemptPayload(
                        attempted_tool_name=call.name,
                        attempted_kind=self.kind,
                        raw_arguments=dict(call.arguments),
                        validation_error=str(exc),
                    ),
                    origin=CandidateOrigin.MAIN_AGENT_TOOL,
                    source_tool_call_id=call.id,
                ),
                intent_fingerprint,
            )
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=json_text(_invalid_candidate_output(exc, retry_state)),
            )
        self.sink.deposit_valid(
            CandidatePoolProposal(
                payload=ValidCandidatePayload(candidate=candidate),
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


def _intent_fingerprint(*, tool_name: str, attempted_kind: str, raw_arguments: dict[str, Any]) -> str:
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
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"memory-intent:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:24]}"


def _normalize_intent_part(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def _stable_args_hash(raw_arguments: dict[str, Any]) -> str:
    serialized = json.dumps(raw_arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _invalid_candidate_output(exc: ValidationError, retry_state: MemoryRetryState) -> dict[str, Any]:
    message = f"{exc.error_count()} validation error(s): {exc}"
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
    description = "Remember a durable factual claim with optional evidence."
    parameters = _COMMON_PARAMETERS
    candidate_type = ClaimCandidate
    kind = "Claim"


class RememberPreferenceTool(_RememberMemoryTool):
    name = "remember_preference"
    description = "Remember a durable user or workspace preference."
    parameters = _COMMON_PARAMETERS
    candidate_type = PreferenceCandidate
    kind = "Preference"


class RememberObservationTool(_RememberMemoryTool):
    name = "remember_observation"
    description = "Remember a durable observation grounded in conversation, tool output, or another source."
    parameters = _COMMON_PARAMETERS
    candidate_type = ObservationCandidate
    kind = "Observation"


class RememberActionBoundaryTool(_RememberMemoryTool):
    name = "remember_action_boundary"
    description = "Remember a durable action boundary with explicit apply and non-apply conditions."
    parameters = _ACTION_BOUNDARY_PARAMETERS
    candidate_type = ActionBoundaryCandidate
    kind = "ActionBoundary"


class RememberDecisionTool(_RememberMemoryTool):
    name = "remember_decision"
    description = "Remember a durable decision, optionally linked to prior memory ids it is based on."
    parameters = _DECISION_PARAMETERS
    candidate_type = DecisionCandidate
    kind = "Decision"
