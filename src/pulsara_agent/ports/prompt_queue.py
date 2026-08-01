"""Renderer-neutral prompt-queue storage and checkpoint ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pulsara_agent.primitives.prompt_queue import (
    PromptQueueAccountProjectionFact,
    PromptQueueDomainCheckpointFact,
    PromptQueueHeadReceiptFact,
)
from pulsara_agent.primitives.stored_event import (
    RawRuntimeProjectionCheckpoint,
    RawStoredEventEnvelope,
    RawTranscriptDomainPrefixFact,
)


@dataclass(frozen=True, slots=True)
class PromptQueueCheckpointCommitGuard:
    runtime_session_id: str
    expected_previous_through_sequence: int
    expected_previous_payload_fingerprint: str
    expected_account_revision: int
    expected_queue_head_event_id: str | None
    expected_queue_head_payload_fingerprint: str | None
    expected_row_set_accumulator: str
    expected_pending_item_head_set_accumulator: str
    guard_generation: int

    def __post_init__(self) -> None:
        if (
            not self.runtime_session_id
            or self.expected_previous_through_sequence < 0
            or self.expected_account_revision < 0
            or self.guard_generation < 1
        ):
            raise ValueError("prompt queue checkpoint guard is malformed")


@dataclass(frozen=True, slots=True)
class PromptQueueRestoreBundle:
    runtime_session_id: str
    ledger_high_water: int
    ledger_prefix: RawTranscriptDomainPrefixFact
    raw_checkpoint: RawRuntimeProjectionCheckpoint
    checkpoint: PromptQueueDomainCheckpointFact
    account: PromptQueueAccountProjectionFact
    checkpoint_item_payloads: tuple[dict[str, object], ...]
    checkpoint_head_event_type: str | None
    current_item_payloads: tuple[dict[str, object], ...]
    bounded_delta_events: tuple[RawStoredEventEnvelope, ...]
    bundle_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.ledger_high_water < self.checkpoint.through_sequence
            or self.ledger_prefix.through_sequence != self.ledger_high_water
            or self.raw_checkpoint.through_sequence != self.checkpoint.through_sequence
            or self.account.checkpoint_generation
            != self.checkpoint.checkpoint_generation
            or self.account.checkpoint_through_sequence
            != self.checkpoint.through_sequence
            or self.account.checkpoint_fingerprint
            != self.checkpoint.checkpoint_fingerprint
        ):
            raise ValueError("prompt queue restore bundle join mismatch")
        sequences = tuple(item.sequence for item in self.bounded_delta_events)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(
            set(sequences)
        ):
            raise ValueError("prompt queue restore delta is not ordered and unique")
        if sequences and (
            sequences[0] <= self.checkpoint.through_sequence
            or sequences[-1] > self.ledger_high_water
        ):
            raise ValueError("prompt queue restore delta exceeds its frozen range")


@dataclass(frozen=True, slots=True)
class PromptQueueCheckpointCommitOutcome:
    disposition: Literal[
        "full",
        "none",
        "superseded_by_compatible_winner",
        "reconciliation_required",
    ]
    candidate_fingerprint: str
    installed_checkpoint: PromptQueueDomainCheckpointFact | None
    head_receipt: PromptQueueHeadReceiptFact | None
    outcome_fingerprint: str


class PromptQueueCheckpointStoragePort(Protocol):
    def read_prompt_queue_restore_bundle(
        self,
        *,
        max_delta_events: int,
        max_delta_payload_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> PromptQueueRestoreBundle: ...

    def commit_prompt_queue_checkpoint(
        self,
        *,
        candidate: RawRuntimeProjectionCheckpoint,
        checkpoint: PromptQueueDomainCheckpointFact,
        guard: PromptQueueCheckpointCommitGuard,
        deadline_monotonic: float | None = None,
    ) -> PromptQueueCheckpointCommitOutcome: ...


__all__ = [
    "PromptQueueCheckpointCommitGuard",
    "PromptQueueCheckpointCommitOutcome",
    "PromptQueueCheckpointStoragePort",
    "PromptQueueRestoreBundle",
]
