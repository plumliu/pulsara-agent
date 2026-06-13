"""Built-in tool for proposing a durable-memory write candidate.

The tool is a *type boundary*, not a write boundary. It assembles a typed
``MemoryCandidate`` from the model's arguments and validates the schema eagerly
with the same ``TypeAdapter`` the write service uses. A schema-invalid call is a
tool-argument error -> ``ToolResultState.ERROR`` so the model sees it in the
result and can retry; nothing is deposited and no candidate event is produced.

A schema-valid candidate is deposited into the shared ``MemoryProposalSink``. The
authoritative write checks (evidence/basedOn existence, gate status) happen later
when a ``MemoryHooks`` drain point routes the candidate through
``MemoryWriteService.submit`` -- those produce the candidate/result/failed events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from pulsara_agent.event.candidates import MemoryCandidate
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import memory
from pulsara_agent.runtime.proposal_sink import MemoryProposalSink
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import json_text, object_schema


_CANDIDATE_ADAPTER = TypeAdapter(MemoryCandidate)
_KINDS = ["Claim", "Preference", "Observation", "ActionBoundary", "Decision"]
_SOURCE_AUTHORITIES = [item.value for item in memory.SourceAuthority]
_VERIFICATION_STATUSES = [item.value for item in memory.VerificationStatus]
_COMMON_FIELDS = {
    "kind",
    "statement",
    "scope",
    "source_authority",
    "verification_status",
    "evidence_ids",
}
_KIND_SPECIFIC_FIELDS = {
    "ActionBoundary": {"applies_when", "do_not_apply_when"},
    "Decision": {"based_on_ids"},
}


def _propose_memory_parameters() -> dict[str, Any]:
    return object_schema(
        properties={
            "kind": {
                "type": "string",
                "enum": _KINDS,
                "description": "Memory type. ActionBoundary requires applies_when/do_not_apply_when; Decision may carry based_on_ids.",
            },
            "statement": {"type": "string", "description": "The memory content as a single declarative statement."},
            "scope": {"type": "string", "description": "Scope this memory applies to, e.g. ctx:project or session."},
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
            "applies_when": {"type": "string", "description": "ActionBoundary only: condition the boundary applies under."},
            "do_not_apply_when": {"type": "string", "description": "ActionBoundary only: condition the boundary does not apply under."},
            "based_on_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Decision only: prior memory ids this decision builds on.",
            },
        },
        required=["kind", "statement", "scope", "source_authority", "verification_status"],
    )


@dataclass(slots=True)
class ProposeMemoryTool:
    sink: MemoryProposalSink
    name: str = "propose_memory"
    description: str = (
        "Propose a durable memory (Claim, Preference, Observation, ActionBoundary, or Decision). "
        "Only include fields valid for the selected kind; omit other kind-specific fields entirely. "
        "The proposal is validated and written at a safe point in the agent loop; the write may be "
        "accepted, flagged for review, or rejected by the memory gate."
    )
    parameters: dict[str, Any] = field(default_factory=_propose_memory_parameters)
    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        payload = _candidate_payload(call.arguments)
        payload["candidate_id"] = f"candidate:{uuid4().hex}"
        try:
            candidate = _CANDIDATE_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.ERROR,
                output=f"[INVALID_CANDIDATE] {exc.error_count()} validation error(s): {exc}",
            )
        self.sink.deposit(candidate)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output=json_text(
                {
                    "candidate_id": candidate.candidate_id,
                    "kind": candidate.kind,
                    "status": "proposed",
                }
            ),
        )


def _candidate_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    kind = arguments.get("kind")
    allowed_fields = _COMMON_FIELDS | _KIND_SPECIFIC_FIELDS.get(kind, set())
    payload: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        if key not in allowed_fields and _is_empty_default(value):
            continue
        payload[key] = value
    return payload


def _is_empty_default(value: Any) -> bool:
    return value == "" or value == []
