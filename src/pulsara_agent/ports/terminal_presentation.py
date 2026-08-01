"""Narrow renderer-neutral terminal presentation read ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from pulsara_agent.primitives.presentation_history import (
    PresentationHistoryPageCursorFact,
    PresentationHistoryRankedEntryView,
    PresentationHistoryRootIdentityFact,
)
from pulsara_agent.primitives.presentation_view import (
    PresentationHistoryViewportSnapshotFact,
)


PresentationHistoryPageDirection = Literal["before", "after"]


@dataclass(frozen=True, slots=True)
class PresentationHistoryPageReadLimits:
    maximum_entries: int
    maximum_canonical_bytes: int
    maximum_rendered_bytes: int
    maximum_node_reads: int
    maximum_tree_height: int

    def __post_init__(self) -> None:
        if (
            min(
                self.maximum_entries,
                self.maximum_canonical_bytes,
                self.maximum_rendered_bytes,
                self.maximum_node_reads,
                self.maximum_tree_height,
            )
            <= 0
        ):
            raise ValueError("presentation history page limits must be positive")


@dataclass(frozen=True, slots=True)
class PresentationHistoryPageData:
    disposition: Literal["page"]
    validated_input_cursor_fingerprint: str
    validated_request_direction: PresentationHistoryPageDirection
    validated_root_identity: PresentationHistoryRootIdentityFact
    ordered_history_entries: tuple[PresentationHistoryRankedEntryView, ...]
    ordered_history_entry_accumulator: str
    continuity_proof_fingerprint: str
    before_cursor: PresentationHistoryPageCursorFact | None
    after_cursor: PresentationHistoryPageCursorFact | None
    has_more_before: bool
    has_more_after: bool
    response_fingerprint: str


@dataclass(frozen=True, slots=True)
class PresentationHistoryCursorStale:
    disposition: Literal["cursor_stale"]
    requested_cursor_fingerprint: str
    latest_root_identity: PresentationHistoryRootIdentityFact
    replacement_cursor: PresentationHistoryPageCursorFact | None
    replacement_cursor_anchor_proof_fingerprint: str | None
    response_fingerprint: str

    def __post_init__(self) -> None:
        if (self.replacement_cursor is None) != (
            self.replacement_cursor_anchor_proof_fingerprint is None
        ):
            raise ValueError("stale cursor replacement proof is partial")


@dataclass(frozen=True, slots=True)
class PresentationHistoryRebaseRequired:
    disposition: Literal["rebase_required"]
    requested_cursor_fingerprint: str
    latest_root_identity: PresentationHistoryRootIdentityFact
    bounded_snapshot_or_rebase_token: str
    response_fingerprint: str


@dataclass(frozen=True, slots=True)
class PresentationHistoryReconciliationRequired:
    disposition: Literal["reconciliation_required"]
    requested_cursor_fingerprint: str
    fault_code: str
    reconciliation_owner_identity: str
    retry_after_ms: int | None
    trusted_latest_root_identity_hint: PresentationHistoryRootIdentityFact | None
    response_fingerprint: str


PresentationHistoryPageReadOutcome: TypeAlias = (
    PresentationHistoryPageData
    | PresentationHistoryCursorStale
    | PresentationHistoryRebaseRequired
    | PresentationHistoryReconciliationRequired
)


class PresentationHistoryPagePort(Protocol):
    def read_page(
        self,
        *,
        cursor: PresentationHistoryPageCursorFact,
        direction: PresentationHistoryPageDirection,
        limits: PresentationHistoryPageReadLimits,
        absolute_deadline: float | None,
    ) -> PresentationHistoryPageReadOutcome: ...


class TerminalPresentationSnapshotPort(Protocol):
    def snapshot(self) -> PresentationHistoryViewportSnapshotFact: ...


__all__ = [
    "PresentationHistoryCursorStale",
    "PresentationHistoryPageData",
    "PresentationHistoryPageDirection",
    "PresentationHistoryPagePort",
    "PresentationHistoryPageReadLimits",
    "PresentationHistoryPageReadOutcome",
    "PresentationHistoryRebaseRequired",
    "PresentationHistoryReconciliationRequired",
    "TerminalPresentationSnapshotPort",
]
