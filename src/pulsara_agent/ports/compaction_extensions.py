"""Low-level compaction post-completion extension boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import model_validator

from pulsara_agent.ports.event_write import FrozenEventWriteCandidate
from pulsara_agent.primitives._context_base import ContextEventReferenceFact, context_fingerprint
from pulsara_agent.primitives.compaction import (
    CompactionPostCompletionExtensionAdmissionFailedFact,
    CompactionPostCompletionExtensionContractFact,
    CompactionPostCompletionExtensionLinkFact,
    ExtensionAdmissionFailureStage,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.runtime_event_vocabulary import (
    BoundedRuntimeFailureDiagnosticFact,
)

if TYPE_CHECKING:
    from pulsara_agent.event.events import AgentEvent
    from pulsara_agent.event.events import ContextCompactionCompletedEvent, EventContext


def _runtime_fingerprint(model: FrozenRuntimeStateBase, field_name: str, domain: str) -> None:
    expected = context_fingerprint(
        domain,
        model.model_dump(mode="json", exclude={field_name}),
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


class CompactionPostCompletionExtensionPrivateHandleIdentity(FrozenRuntimeStateBase):
    extension_id: str
    handle_id: str
    generation: int
    manifest_preparation_identity_fingerprint: str
    identity_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "CompactionPostCompletionExtensionPrivateHandleIdentity":
        if self.generation < 1:
            raise ValueError("extension handle generation must be positive")
        _runtime_fingerprint(
            self,
            "identity_fingerprint",
            "compaction-post-completion-extension-private-handle-identity:v1",
        )
        return self


class CompactionPostCompletionExtensionPrivateHandle(Protocol):
    @property
    def identity(self) -> CompactionPostCompletionExtensionPrivateHandleIdentity: ...

    @property
    def active(self) -> bool: ...

    def confirm_request_batch_full(
        self,
        *,
        prepared_batch_fingerprint: str,
        stored_request_reference: ContextEventReferenceFact,
    ) -> None: ...

    def retain_request_batch_none(self, *, prepared_batch_fingerprint: str) -> None: ...

    def mark_request_batch_reconciliation_required(
        self, *, prepared_batch_fingerprint: str
    ) -> None: ...

    def abandon_before_write(self, *, reason: str) -> None: ...


class PreparedCompactionPostCompletionExtensionIntentIdentity(FrozenRuntimeStateBase):
    extension_contract_fingerprint: str
    completed_event_id: str
    request_event_id: str
    extension_link_id: str
    business_occurrence_fingerprint: str
    private_handle_identity_fingerprint: str
    intent_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "PreparedCompactionPostCompletionExtensionIntentIdentity":
        _runtime_fingerprint(
            self,
            "intent_fingerprint",
            "prepared-compaction-post-completion-extension-intent:v1",
        )
        return self


@dataclass(frozen=True, slots=True)
class PreparedCompactionPostCompletionExtensionIntent:
    identity: PreparedCompactionPostCompletionExtensionIntentIdentity
    extension_contract: CompactionPostCompletionExtensionContractFact
    private_handle: CompactionPostCompletionExtensionPrivateHandle = field(
        compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if (
            self.extension_contract.contract_fingerprint
            != self.identity.extension_contract_fingerprint
            or self.private_handle.identity.identity_fingerprint
            != self.identity.private_handle_identity_fingerprint
            or self.private_handle.identity.extension_id
            != self.extension_contract.extension_id
        ):
            raise ValueError("prepared extension intent identity join failed")
        if not self.private_handle.active:
            raise ValueError("prepared extension intent requires an active handle")

    def __reduce__(self) -> object:
        raise TypeError("live compaction extension intent is not serializable")


class PreparedCompactionPostCompletionExtensionAdmissionFailure(FrozenRuntimeStateBase):
    extension_contract_fingerprint: str
    failure_stage: ExtensionAdmissionFailureStage
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    preparation_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "PreparedCompactionPostCompletionExtensionAdmissionFailure":
        _runtime_fingerprint(
            self,
            "preparation_fingerprint",
            "prepared-compaction-post-completion-extension-admission-failure:v1",
        )
        return self


class PreparedCompactionPostCompletionExtensionBatchIdentity(FrozenRuntimeStateBase):
    extension_contract_fingerprint: str
    extension_link_id: str
    request_event_id: str
    request_event_type: str
    request_event_schema_fingerprint: str
    request_event_payload_fingerprint: str
    prepared_batch_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "PreparedCompactionPostCompletionExtensionBatchIdentity":
        _runtime_fingerprint(
            self,
            "prepared_batch_fingerprint",
            "prepared-compaction-post-completion-extension-batch:v1",
        )
        return self


@dataclass(frozen=True, slots=True)
class PreparedCompactionPostCompletionExtensionBatch:
    identity: PreparedCompactionPostCompletionExtensionBatchIdentity
    extension_link: CompactionPostCompletionExtensionLinkFact
    request_event_candidate: FrozenEventWriteCandidate

    def __post_init__(self) -> None:
        candidate = self.request_event_candidate
        if (
            self.extension_link.extension_link_id != self.identity.extension_link_id
            or candidate.event_id != self.identity.request_event_id
            or candidate.event_type != self.identity.request_event_type
            or candidate.event_schema_fingerprint
            != self.identity.request_event_schema_fingerprint
            or candidate.payload_fingerprint
            != self.identity.request_event_payload_fingerprint
        ):
            raise ValueError("prepared extension batch exact candidate join failed")


class CompactionPostCompletionExtensionPort(Protocol):
    def prepare_intent(
        self,
        *,
        runtime_session_id: str,
        event_context: "EventContext",
        compaction_id: str,
        completed_event_id: str,
        trigger: str,
        phase: str | None,
        previous_keep_after_sequence: int,
        current_keep_after_sequence: int,
        current_through_sequence: int,
        predecessor_completed_event_id: str | None,
        transcript_authority_snapshot: object,
        event_lookup: Callable[[str], "AgentEvent | None"],
    ) -> (
        PreparedCompactionPostCompletionExtensionIntent
        | PreparedCompactionPostCompletionExtensionAdmissionFailure
        | None
    ): ...

    def prepare_completion_disposition(
        self,
        *,
        preparation: (
            PreparedCompactionPostCompletionExtensionIntent
            | PreparedCompactionPostCompletionExtensionAdmissionFailure
        ),
        completed_event: "ContextCompactionCompletedEvent",
    ) -> (
        PreparedCompactionPostCompletionExtensionBatch
        | CompactionPostCompletionExtensionAdmissionFailedFact
    ): ...

    async def stop_admission_and_drain(
        self, *, deadline_monotonic: float
    ) -> None: ...


__all__ = [name for name in globals() if name.startswith(("Compaction", "Prepared"))]
