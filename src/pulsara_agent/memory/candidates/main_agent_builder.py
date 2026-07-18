"""Pure deterministic replay contract for main-agent memory tool candidates."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from pulsara_agent.event.candidates import (
    ActionBoundaryCandidate,
    CandidatePayload,
    ClaimCandidate,
    DecisionCandidate,
    InvalidAttemptPayload,
    MemoryCandidateBase,
    ObservationCandidate,
    PreferenceCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    MainAgentMemoryCandidateBuilderContractFact,
)


_MAIN_MEMORY_TOOL_TYPES: dict[str, tuple[type[MemoryCandidateBase], str]] = {
    "remember_claim": (ClaimCandidate, "Claim"),
    "remember_preference": (PreferenceCandidate, "Preference"),
    "remember_observation": (ObservationCandidate, "Observation"),
    "remember_action_boundary": (ActionBoundaryCandidate, "ActionBoundary"),
    "remember_decision": (DecisionCandidate, "Decision"),
}


def main_agent_memory_candidate_builder_contract(
) -> MainAgentMemoryCandidateBuilderContractFact:
    return build_frozen_fact(
        MainAgentMemoryCandidateBuilderContractFact,
        schema_version="main_agent_memory_candidate_builder_contract.v1",
        builder_id="pulsara.main_agent_memory_candidate",
        builder_version="1",
        input_schema_fingerprint=context_fingerprint(
            "main-agent-memory-builder-input-schema:v1",
            "runtime_session+tool_call_id+typed_arguments",
        ),
        output_schema_fingerprint=context_fingerprint(
            "main-agent-memory-builder-output-schema:v1",
            "CandidatePayload",
        ),
        candidate_id_policy="source_identity_sha256_v1",
        normalization_contract_fingerprint=context_fingerprint(
            "main-agent-memory-builder-normalization:v1",
            "pydantic-candidate-schema",
        ),
    )


def main_agent_memory_candidate_id(
    *, runtime_session_id: str, tool_call_id: str
) -> str:
    contract = main_agent_memory_candidate_builder_contract()
    digest = context_fingerprint(
        "main-agent-memory-candidate-id:v1",
        (runtime_session_id, tool_call_id, contract.contract_fingerprint),
    ).removeprefix("sha256:")
    return f"candidate:main_tool:{digest}"


def main_agent_memory_candidate_entry_id(
    *, runtime_session_id: str, tool_call_id: str
) -> str:
    return main_agent_memory_candidate_id(
        runtime_session_id=runtime_session_id,
        tool_call_id=tool_call_id,
    ).replace("candidate:", "pool:", 1)


def build_main_agent_memory_candidate_payload(
    *,
    runtime_session_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> CandidatePayload:
    try:
        candidate_type, attempted_kind = _MAIN_MEMORY_TOOL_TYPES[tool_name]
    except KeyError as exc:
        raise ValueError("unsupported main-agent memory tool") from exc
    payload = {
        **arguments,
        "candidate_id": main_agent_memory_candidate_id(
            runtime_session_id=runtime_session_id,
            tool_call_id=tool_call_id,
        ),
        "kind": attempted_kind,
    }
    try:
        candidate = candidate_type.model_validate(payload)
    except ValidationError as exc:
        return InvalidAttemptPayload(
            attempted_tool_name=tool_name,
            attempted_kind=attempted_kind,
            raw_arguments=dict(arguments),
            validation_error=str(exc),
        )
    return ValidCandidatePayload(candidate=candidate)


__all__ = [
    "build_main_agent_memory_candidate_payload",
    "main_agent_memory_candidate_builder_contract",
    "main_agent_memory_candidate_entry_id",
    "main_agent_memory_candidate_id",
]
