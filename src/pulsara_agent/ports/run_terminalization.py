"""Stable run terminalization and final-output contracts."""

from __future__ import annotations

from typing import Literal, Protocol, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.run_lifecycle import (
    FAILURE_STOP_REASONS,
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    PublicationLatchedRunTerminationFact,
)
from pulsara_agent.ports.run_execution import (
    RunFinalOutputView,
    RunOwnerIdentity,
)


class RunFinalizationOwnerIdentity(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    terminal_event_id: str = Field(min_length=1)
    terminal_candidate_fingerprint: str = Field(min_length=1)
    finalization_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "RunFinalizationOwnerIdentity":
        expected = context_fingerprint(
            "run-finalization-owner:v1",
            self.model_dump(mode="json", exclude={"finalization_fingerprint"}),
        )
        if self.finalization_fingerprint != expected:
            raise ValueError("run finalization owner fingerprint mismatch")
        return self


class RunTerminalizationRequest(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    expected_authority_head_fingerprint: str = Field(min_length=1)
    expected_termination_revision: int = Field(ge=0)
    terminal_run_end_event_id: str = Field(min_length=1)
    status: Literal["finished", "failed", "aborted"]
    stop_reason: RunStopReason
    terminalization_kind: RunTerminalizationKind
    abort_kind: Literal["user_stop", "host_teardown"] | None
    redacted_error_message: str | None = Field(default=None, max_length=4096)
    mcp_closure_event_reference: ContextEventReferenceFact | None
    publication_latched_termination: PublicationLatchedRunTerminationFact | None
    request_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "RunTerminalizationRequest":
        if self.terminalization_kind is RunTerminalizationKind.NORMAL:
            valid = (
                self.status == "finished"
                and self.stop_reason is RunStopReason.FINAL
                and self.abort_kind is None
                and self.redacted_error_message is None
            )
        elif self.terminalization_kind is RunTerminalizationKind.USER_STOP:
            valid = (
                self.status == "aborted"
                and self.stop_reason is RunStopReason.ABORTED
                and self.abort_kind == "user_stop"
                and self.redacted_error_message is None
            )
        elif self.terminalization_kind in {
            RunTerminalizationKind.HOST_TEARDOWN,
            RunTerminalizationKind.RECOVERED_INTERRUPTED,
        }:
            valid = (
                self.status == "aborted"
                and self.stop_reason is RunStopReason.ABORTED
                and self.abort_kind == "host_teardown"
                and self.redacted_error_message is None
            )
        else:
            valid = (
                self.status == "failed"
                and self.stop_reason in FAILURE_STOP_REASONS
                and self.abort_kind is None
                and bool(self.redacted_error_message)
            )
        if not valid:
            raise ValueError("run terminalization request matrix mismatch")
        expected = context_fingerprint(
            "run-terminalization-request:v1",
            self.model_dump(mode="json", exclude={"request_fingerprint"}),
        )
        if self.request_fingerprint != expected:
            raise ValueError("run terminalization request fingerprint mismatch")
        return self


class PreparedRunTerminalCandidate(Protocol):
    @property
    def candidate_id(self) -> str: ...

    @property
    def candidate_fingerprint(self) -> str: ...


class RunTerminalizationFull(FrozenRuntimeStateBase):
    disposition: Literal["full"] = "full"
    run_end_event_reference: ContextEventReferenceFact


class RunTerminalizationNone(FrozenRuntimeStateBase):
    disposition: Literal["none"] = "none"
    candidate_id: str
    candidate_fingerprint: str


class RunTerminalizationUntrusted(FrozenRuntimeStateBase):
    disposition: Literal["unknown", "conflict"]
    candidate_id: str
    candidate_fingerprint: str


RunTerminalizationCommitOutcome: TypeAlias = (
    RunTerminalizationFull
    | RunTerminalizationNone
    | RunTerminalizationUntrusted
)


class RunTerminalizationPort(Protocol):
    def freeze_candidate(
        self, request: RunTerminalizationRequest
    ) -> PreparedRunTerminalCandidate: ...

    async def commit(
        self,
        candidate: PreparedRunTerminalCandidate,
        *,
        attempt_generation: int,
        deadline_monotonic: float,
    ) -> RunTerminalizationCommitOutcome: ...


class RunFinalOutputMaterializationOwnerIdentity(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    run_end_event_reference: ContextEventReferenceFact
    materializer_contract_fingerprint: str = Field(min_length=1)
    owner_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "RunFinalOutputMaterializationOwnerIdentity":
        expected = context_fingerprint(
            "run-final-output-materialization-owner:v1",
            self.model_dump(mode="json", exclude={"owner_fingerprint"}),
        )
        if self.owner_fingerprint != expected:
            raise ValueError("run final-output owner fingerprint mismatch")
        return self


class TerminalRunReceipt(FrozenRuntimeStateBase):
    schema_version: Literal[1] = 1
    owner_identity: RunOwnerIdentity
    run_end_event_reference: ContextEventReferenceFact
    finalization_receipt_fingerprint: str = Field(min_length=1)
    output: RunFinalOutputView
    receipt_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fingerprint(self) -> "TerminalRunReceipt":
        if (
            self.output.status == "finished"
            and self.output.stop_reason is not RunStopReason.FINAL
        ):
            raise ValueError("finished terminal receipt requires final stop reason")
        expected = context_fingerprint(
            "terminal-run-receipt:v1",
            self.model_dump(mode="json", exclude={"receipt_fingerprint"}),
        )
        if self.receipt_fingerprint != expected:
            raise ValueError("terminal run receipt fingerprint mismatch")
        return self


class RunFinalOutputMaterializationFull(FrozenRuntimeStateBase):
    disposition: Literal["full"] = "full"
    owner: RunFinalOutputMaterializationOwnerIdentity
    receipt: TerminalRunReceipt


class RunFinalOutputMaterializationRetryableUnavailable(FrozenRuntimeStateBase):
    disposition: Literal["retryable_unavailable"] = "retryable_unavailable"
    owner: RunFinalOutputMaterializationOwnerIdentity
    diagnostic_code: Literal[
        "deadline_exceeded",
        "source_temporarily_unavailable",
    ]


class RunFinalOutputMaterializationReconciliationRequired(FrozenRuntimeStateBase):
    disposition: Literal["reconciliation_required"] = "reconciliation_required"
    owner: RunFinalOutputMaterializationOwnerIdentity
    diagnostic_code: Literal[
        "run_end_authority_conflict",
        "transcript_authority_conflict",
        "usage_authority_conflict",
    ]
    conflicting_event_references: tuple[ContextEventReferenceFact, ...] = ()


RunFinalOutputMaterializationOutcome: TypeAlias = (
    RunFinalOutputMaterializationFull
    | RunFinalOutputMaterializationRetryableUnavailable
    | RunFinalOutputMaterializationReconciliationRequired
)


class RunFinalOutputMaterializerPort(Protocol):
    async def materialize(
        self,
        *,
        owner_identity: RunOwnerIdentity,
        run_end_event_reference: ContextEventReferenceFact,
        deadline_monotonic: float,
    ) -> RunFinalOutputMaterializationOutcome: ...


__all__ = [
    "PreparedRunTerminalCandidate",
    "RunFinalOutputMaterializationOwnerIdentity",
    "RunFinalOutputMaterializationFull",
    "RunFinalOutputMaterializationOutcome",
    "RunFinalOutputMaterializationReconciliationRequired",
    "RunFinalOutputMaterializationRetryableUnavailable",
    "RunFinalOutputMaterializerPort",
    "RunFinalizationOwnerIdentity",
    "RunTerminalizationCommitOutcome",
    "RunTerminalizationPort",
    "RunTerminalizationRequest",
    "TerminalRunReceipt",
]
