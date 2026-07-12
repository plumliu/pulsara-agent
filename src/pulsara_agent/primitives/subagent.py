"""Event-safe cross-ledger subagent terminal and result handoff facts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.model_call import (
    ModelTokenUsageFact,
    canonical_json_bytes,
    sha256_fingerprint,
)
from pulsara_agent.primitives.run_lifecycle import (
    RunStopReason,
    RunTerminalizationKind,
)


class ChildResultRenderPolicyFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    renderer_version: str = Field(min_length=1, max_length=128)
    max_summary_chars: int = Field(ge=0)
    max_artifact_refs: int = Field(ge=0)
    policy_fingerprint: str

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "ChildResultRenderPolicyFact":
        expected = child_result_render_policy_fingerprint(
            renderer_version=self.renderer_version,
            max_summary_chars=self.max_summary_chars,
            max_artifact_refs=self.max_artifact_refs,
        )
        if self.policy_fingerprint != expected:
            raise ValueError("child result render policy fingerprint mismatch")
        return self


def child_result_render_policy_fingerprint(
    *,
    renderer_version: str,
    max_summary_chars: int,
    max_artifact_refs: int,
) -> str:
    return sha256_fingerprint(
        "child-result-render-policy:v1",
        [renderer_version, max_summary_chars, max_artifact_refs],
    )


def build_child_result_render_policy(
    *,
    renderer_version: str,
    max_summary_chars: int,
    max_artifact_refs: int,
) -> ChildResultRenderPolicyFact:
    return ChildResultRenderPolicyFact(
        renderer_version=renderer_version,
        max_summary_chars=max_summary_chars,
        max_artifact_refs=max_artifact_refs,
        policy_fingerprint=child_result_render_policy_fingerprint(
            renderer_version=renderer_version,
            max_summary_chars=max_summary_chars,
            max_artifact_refs=max_artifact_refs,
        ),
    )


def validate_child_render_policy_against_budget(
    policy: ChildResultRenderPolicyFact,
    budget_snapshot: object,
) -> None:
    if policy.max_summary_chars != int(
        getattr(budget_snapshot, "max_result_summary_chars_per_child")
    ):
        raise ValueError("child result summary cap does not match parent budget")
    if policy.max_artifact_refs != int(
        getattr(budget_snapshot, "max_result_artifact_refs_per_child")
    ):
        raise ValueError("child result artifact cap does not match parent budget")


class ChildNativeTerminalReferenceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    child_runtime_session_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    terminal_event_id: str = Field(min_length=1)
    terminal_sequence: int = Field(ge=1)
    terminal_status: Literal["finished", "failed", "aborted"]
    terminalization_kind: RunTerminalizationKind
    stop_reason: RunStopReason

    @model_validator(mode="after")
    def _validate_terminal_matrix(self) -> "ChildNativeTerminalReferenceFact":
        kind = self.terminalization_kind
        if kind is RunTerminalizationKind.NORMAL:
            valid = (
                self.terminal_status == "finished"
                and self.stop_reason is RunStopReason.FINAL
            )
        elif kind in {
            RunTerminalizationKind.USER_STOP,
            RunTerminalizationKind.HOST_TEARDOWN,
            RunTerminalizationKind.RECOVERED_INTERRUPTED,
        }:
            valid = (
                self.terminal_status == "aborted"
                and self.stop_reason is RunStopReason.ABORTED
            )
        else:
            valid = (
                self.terminal_status == "failed"
                and self.stop_reason
                not in {
                    RunStopReason.FINAL,
                    RunStopReason.WAITING_USER,
                    RunStopReason.ABORTED,
                }
            )
        if not valid:
            raise ValueError("child terminal reference violates run terminal matrix")
        return self


class ChildExplicitResultEvidenceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_result_submitted_event_id: str = Field(min_length=1)
    source_result_submitted_event_sequence: int = Field(ge=1)
    child_runtime_session_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    source_tool_call_id: str = Field(min_length=1)
    tool_call_start_event_id: str = Field(min_length=1)
    tool_call_start_sequence: int = Field(ge=1)
    tool_result_end_event_id: str = Field(min_length=1)
    tool_result_end_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_order(self) -> "ChildExplicitResultEvidenceFact":
        if self.tool_result_end_sequence < self.tool_call_start_sequence:
            raise ValueError("explicit result tool result precedes tool call")
        return self


class ChildResultHandoffFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_kind: Literal["explicit", "inferred"]
    renderer_version: str = Field(min_length=1)
    render_policy_fingerprint: str = Field(min_length=1)
    child_terminal_reference: ChildNativeTerminalReferenceFact
    explicit_evidence: ChildExplicitResultEvidenceFact | None
    result_id: str = Field(min_length=1)
    summary: str
    result_artifact_id: str = Field(min_length=1)
    artifact_ids: tuple[str, ...]
    rendered_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_usage: ModelTokenUsageFact | None
    usage_status: Literal["complete", "partial", "missing"]
    tool_call_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_handoff(self) -> "ChildResultHandoffFact":
        if self.handoff_kind == "explicit":
            if self.explicit_evidence is None:
                raise ValueError("explicit handoff requires explicit evidence")
            if (
                self.explicit_evidence.child_runtime_session_id
                != self.child_terminal_reference.child_runtime_session_id
                or self.explicit_evidence.child_run_id
                != self.child_terminal_reference.child_run_id
            ):
                raise ValueError("explicit evidence child attribution mismatch")
            terminal_sequence = self.child_terminal_reference.terminal_sequence
            evidence_sequences = (
                self.explicit_evidence.tool_call_start_sequence,
                self.explicit_evidence.tool_result_end_sequence,
            )
            if any(sequence >= terminal_sequence for sequence in evidence_sequences):
                raise ValueError("explicit evidence must precede child terminal")
        elif self.explicit_evidence is not None:
            raise ValueError("inferred handoff cannot contain explicit evidence")

        if self.usage_status == "missing":
            if self.token_usage is not None:
                raise ValueError("missing usage cannot contain token usage")
        elif self.token_usage is None:
            raise ValueError("complete/partial usage requires token usage")

        if not self.artifact_ids:
            raise ValueError("handoff requires the primary result artifact")
        if self.artifact_ids != tuple(sorted(set(self.artifact_ids))):
            raise ValueError("handoff artifact_ids must be sorted and unique")
        if self.result_artifact_id not in self.artifact_ids:
            raise ValueError("result_artifact_id must be present in artifact_ids")
        return self


def build_child_result_handoff(
    *,
    handoff_kind: Literal["explicit", "inferred"],
    policy: ChildResultRenderPolicyFact,
    child_terminal_reference: ChildNativeTerminalReferenceFact,
    explicit_evidence: ChildExplicitResultEvidenceFact | None,
    result_id: str,
    summary: str,
    result_artifact_id: str,
    artifact_ids: tuple[str, ...],
    token_usage: ModelTokenUsageFact | None,
    usage_status: Literal["complete", "partial", "missing"],
    tool_call_count: int,
) -> ChildResultHandoffFact:
    if len(summary) > policy.max_summary_chars:
        raise ValueError("child result summary exceeds frozen render policy")
    canonical_artifact_ids = tuple(sorted(set(artifact_ids)))
    if len(canonical_artifact_ids) > policy.max_artifact_refs:
        raise ValueError("child result artifact refs exceed frozen render policy")
    payload = {
        "handoff_kind": handoff_kind,
        "renderer_version": policy.renderer_version,
        "render_policy_fingerprint": policy.policy_fingerprint,
        "child_terminal_reference": child_terminal_reference.model_dump(mode="json"),
        "explicit_evidence": (
            explicit_evidence.model_dump(mode="json")
            if explicit_evidence is not None
            else None
        ),
        "result_id": result_id,
        "summary": summary,
        "result_artifact_id": result_artifact_id,
        "artifact_ids": list(canonical_artifact_ids),
        "token_usage": (
            token_usage.model_dump(mode="json") if token_usage is not None else None
        ),
        "usage_status": usage_status,
        "tool_call_count": tool_call_count,
    }
    return ChildResultHandoffFact(
        **payload,
        rendered_payload_sha256=rendered_payload_sha256(payload),
    )


def deterministic_child_result_id(
    *, subagent_run_id: str, terminal_event_id: str, policy_fingerprint: str
) -> str:
    digest = sha256_fingerprint(
        "child-result-id:v1",
        [subagent_run_id, terminal_event_id, policy_fingerprint],
    ).removeprefix("sha256:")
    return f"subagent_result:{digest}"


def deterministic_child_result_artifact_id(
    *, subagent_run_id: str, terminal_event_id: str, policy_fingerprint: str
) -> str:
    digest = sha256_fingerprint(
        "child-result-artifact-id:v1",
        [subagent_run_id, terminal_event_id, policy_fingerprint],
    ).removeprefix("sha256:")
    return f"artifact:subagent_result:{digest}"


def deterministic_parent_subagent_terminal_event_id(
    *,
    parent_runtime_session_id: str,
    subagent_run_id: str,
    child_terminal_event_id: str,
    parent_terminal_event_type: str,
) -> str:
    """Derive the parent-ledger terminal identity from durable child truth."""

    digest = sha256_fingerprint(
        "parent-subagent-terminal-event-id:v1",
        [
            parent_runtime_session_id,
            subagent_run_id,
            child_terminal_event_id,
            parent_terminal_event_type,
        ],
    ).removeprefix("sha256:")
    return f"{parent_terminal_event_type}:{digest}"


def rendered_payload_sha256(payload: object) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = [
    "ChildExplicitResultEvidenceFact",
    "ChildNativeTerminalReferenceFact",
    "ChildResultHandoffFact",
    "ChildResultRenderPolicyFact",
    "build_child_result_render_policy",
    "build_child_result_handoff",
    "child_result_render_policy_fingerprint",
    "deterministic_child_result_artifact_id",
    "deterministic_child_result_id",
    "deterministic_parent_subagent_terminal_event_id",
    "rendered_payload_sha256",
    "validate_child_render_policy_against_budget",
]
