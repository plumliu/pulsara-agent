"""Process-local contracts for the memory extraction extension."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.compaction import (
    CompactionHumanEvidenceManifestReferenceFact,
)
from pulsara_agent.primitives.frozen import (
    FrozenRuntimeStateBase,
    StableEventIdentityFact,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    BoundedRuntimeFailureDiagnosticFact,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionResultReceiptReferenceFact,
)


def _validate_fingerprint(
    model: FrozenRuntimeStateBase,
    *,
    field_name: str,
    domain: str,
) -> None:
    expected = context_fingerprint(
        domain, model.model_dump(mode="json", exclude={field_name})
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


class CompactionHumanEvidenceManifestPreparationIdentity(FrozenRuntimeStateBase):
    preparation_id: str
    generation: int
    stable_manifest_reference_fingerprint: str
    operation_deadline_monotonic: float
    identity_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "CompactionHumanEvidenceManifestPreparationIdentity":
        if self.generation < 1 or self.operation_deadline_monotonic <= 0:
            raise ValueError("manifest preparation identity bounds are invalid")
        _validate_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="compaction-human-evidence-manifest-preparation-identity:v1",
        )
        return self


class CompactionHumanEvidenceManifestPreparationFailureSnapshot(
    FrozenRuntimeStateBase
):
    failure_stage: Literal[
        "page_content_write",
        "page_write",
        "root_write",
        "artifact_confirmation",
        "physical_cancel",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    failure_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(
        self,
    ) -> "CompactionHumanEvidenceManifestPreparationFailureSnapshot":
        _validate_fingerprint(
            self,
            field_name="failure_fingerprint",
            domain="compaction-human-evidence-manifest-preparation-failure:v1",
        )
        return self


class CompactionHumanEvidenceManifestPreparationSnapshot(FrozenRuntimeStateBase):
    preparation_identity_fingerprint: str
    logical_state: Literal["preparing", "full", "failed", "abandoned"]
    physical_state: Literal["queued", "running", "exited"]
    completion_consumed: bool
    failure: CompactionHumanEvidenceManifestPreparationFailureSnapshot | None
    snapshot_fingerprint: str

    @model_validator(mode="after")
    def _snapshot(self) -> "CompactionHumanEvidenceManifestPreparationSnapshot":
        if (self.logical_state == "failed") != (self.failure is not None):
            raise ValueError("manifest preparation failure state mismatch")
        _validate_fingerprint(
            self,
            field_name="snapshot_fingerprint",
            domain="compaction-human-evidence-manifest-preparation-snapshot:v1",
        )
        return self


class CompactionHumanEvidenceManifestConsumedFull(FrozenRuntimeStateBase):
    outcome_kind: Literal["full"] = "full"
    manifest_reference: CompactionHumanEvidenceManifestReferenceFact
    pin_transfer_identity_fingerprint: str
    outcome_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "CompactionHumanEvidenceManifestConsumedFull":
        _validate_fingerprint(
            self,
            field_name="outcome_fingerprint",
            domain="compaction-human-evidence-manifest-consumed-full:v1",
        )
        return self


class CompactionHumanEvidenceManifestConsumedAbandoned(FrozenRuntimeStateBase):
    outcome_kind: Literal["abandoned"] = "abandoned"
    failure_stage: Literal[
        "manifest_not_ready_at_completion",
        "manifest_prepare",
        "manifest_abandoned",
    ]
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    outcome_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "CompactionHumanEvidenceManifestConsumedAbandoned":
        _validate_fingerprint(
            self,
            field_name="outcome_fingerprint",
            domain="compaction-human-evidence-manifest-consumed-abandoned:v1",
        )
        return self


CompactionHumanEvidenceManifestConsumptionOutcome: TypeAlias = Annotated[
    CompactionHumanEvidenceManifestConsumedFull
    | CompactionHumanEvidenceManifestConsumedAbandoned,
    Field(discriminator="outcome_kind"),
]


class CompactionHumanEvidenceManifestPreparationHandle(Protocol):
    @property
    def identity(self) -> CompactionHumanEvidenceManifestPreparationIdentity: ...

    def snapshot_nowait(self) -> CompactionHumanEvidenceManifestPreparationSnapshot: ...

    def consume_full_or_abandon(
        self,
    ) -> CompactionHumanEvidenceManifestConsumptionOutcome: ...

    def request_physical_cancel(self) -> None: ...

    async def wait_physical_exit(self, *, deadline_monotonic: float) -> bool: ...


class CompactionMemoryPreferenceProposalFact(FrozenRuntimeStateBase):
    kind: Literal["Preference"] = "Preference"
    statement: str = Field(min_length=1)
    evidence_node_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _proposal(self) -> "CompactionMemoryPreferenceProposalFact":
        if len(self.statement.encode("utf-8")) > 1000:
            raise ValueError("extraction proposal statement is oversized")
        if len(self.evidence_node_ids) != len(set(self.evidence_node_ids)):
            raise ValueError("extraction proposal evidence IDs must be unique")
        return self


class CompactionMemoryExtractionOutputFact(FrozenRuntimeStateBase):
    schema_version: Literal["compaction_memory_extraction_output.v1"] = (
        "compaction_memory_extraction_output.v1"
    )
    candidates: tuple[CompactionMemoryPreferenceProposalFact, ...] = Field(
        max_length=3
    )


class CompactionMemoryExtractionSettlementWriteAttemptIdentity(
    FrozenRuntimeStateBase
):
    result_candidate_id: str
    result_candidate_fingerprint: str
    settlement_generation: int
    opened_at_utc: str
    deadline_monotonic: float
    identity_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(
        self,
    ) -> "CompactionMemoryExtractionSettlementWriteAttemptIdentity":
        if self.settlement_generation < 1 or self.deadline_monotonic <= 0:
            raise ValueError("extraction settlement attempt bounds are invalid")
        _validate_fingerprint(
            self,
            field_name="identity_fingerprint",
            domain="compaction-memory-extraction-settlement-attempt:v1",
        )
        return self


@dataclass(slots=True)
class CompactionMemoryExtractionSettlementWriteAttempt:
    identity: CompactionMemoryExtractionSettlementWriteAttemptIdentity
    active: bool = True

    def consume(self) -> None:
        if not self.active:
            raise RuntimeError("extraction settlement attempt was already consumed")
        self.active = False


class CompactionMemoryExtractionSettlementOutcome(FrozenRuntimeStateBase):
    confirmation: Literal["full", "none", "conflict", "unresolved"]
    result_candidate_id: str
    result_candidate_fingerprint: str
    settlement_generation: int
    producer_event_identity: StableEventIdentityFact
    result_receipt_reference: DurableProjectionResultReceiptReferenceFact | None
    target_head_revision: int | None
    publication_status: Literal[
        "not_applicable",
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ]
    runtime_session_ledger_reconciliation_required: bool
    outcome_fingerprint: str

    @model_validator(mode="after")
    def _outcome(self) -> "CompactionMemoryExtractionSettlementOutcome":
        if self.settlement_generation < 1:
            raise ValueError("extraction settlement generation must be positive")
        full = self.confirmation == "full"
        if full != (self.result_receipt_reference is not None):
            raise ValueError("extraction settlement receipt matrix mismatch")
        if full != (self.target_head_revision is not None):
            raise ValueError("extraction settlement head matrix mismatch")
        _validate_fingerprint(
            self,
            field_name="outcome_fingerprint",
            domain="compaction-memory-extraction-settlement-outcome:v1",
        )
        return self


class CompactionMemoryExtractionSettlementPort(Protocol):
    async def commit_result(
        self,
        *,
        result_candidate: object,
        write_attempt: CompactionMemoryExtractionSettlementWriteAttempt,
    ) -> CompactionMemoryExtractionSettlementOutcome: ...

    async def drain(self, *, deadline_monotonic: float) -> None: ...


class CompactionMemoryExtractionRepositoryPort(Protocol):
    """Narrow operational surface consumed by the memory-owned driver."""

    def release_session_model_lease_without_attempt(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def supersede_unstarted_compaction_memory_jobs(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def claim_compaction_memory_settlements(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def read_compaction_memory_result_candidate(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def read_background_budget_terminal_authority(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def defer_recovered_session_model_attempt(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def prepare_background_budget_reservation(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def defer_session_model_job_after_attempt(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def read_head(self, *args: object, **kwargs: object) -> object: ...

    def prepare_compaction_memory_result_installation_guard(
        self, *args: object, **kwargs: object
    ) -> object: ...

    def install_compaction_memory_result_candidate(
        self, *args: object, **kwargs: object
    ) -> object: ...


def build_settlement_write_attempt(
    *,
    result_candidate: object,
    settlement_generation: int,
    deadline_monotonic: float,
) -> CompactionMemoryExtractionSettlementWriteAttempt:
    result_candidate_id = getattr(result_candidate, "result_candidate_id")
    result_candidate_fingerprint = getattr(
        result_candidate,
        "result_candidate_fingerprint",
    )
    if not isinstance(result_candidate_id, str) or not isinstance(
        result_candidate_fingerprint, str
    ):
        raise TypeError("settlement candidate lacks its stable identity")
    opened_at = canonical_utc_timestamp(datetime.now(timezone.utc).isoformat())
    payload = {
        "result_candidate_id": result_candidate_id,
        "result_candidate_fingerprint": result_candidate_fingerprint,
        "settlement_generation": settlement_generation,
        "opened_at_utc": opened_at,
        "deadline_monotonic": deadline_monotonic,
    }
    return CompactionMemoryExtractionSettlementWriteAttempt(
        identity=CompactionMemoryExtractionSettlementWriteAttemptIdentity(
            **payload,
            identity_fingerprint=context_fingerprint(
                "compaction-memory-extraction-settlement-attempt:v1",
                payload,
            ),
        )
    )


__all__ = [name for name in globals() if name.startswith(("Background", "Compaction", "Driver"))]
