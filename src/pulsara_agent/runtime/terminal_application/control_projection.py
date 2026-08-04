"""Atomic, renderer-neutral control projection for terminal clients.

The history projection owns transcript placement.  This store owns the five
small control sections that must be installed with one revision.  It never
derives durable transcript semantics and it never exposes Host internals to a
client adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from threading import RLock
from time import monotonic
from typing import Literal

from pulsara_agent.ports.terminal_application import (
    PromptQueueItemView,
    TerminalInteractionRequestView,
    TerminalUiSessionSnapshot,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


TERMINAL_CONTROL_REGISTRY_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-control-transition-registry:v1",
    (
        "session_lifecycle",
        "run_control",
        "pending_interaction",
        "prompt_queue",
        "notifications",
    ),
)
TERMINAL_ACTIVE_QUEUE_PROJECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "terminal-active-prompt-queue-projection-contract:v1",
    {
        "maximum_active_items": 64,
        "ordering": "accepted_ordinal+queue_item_id",
        "states": (
            "accepted_pending",
            "steer_reserved",
            "follow_up_reserved",
            "reconciliation_required",
        ),
        "content_retention": "active",
    },
)
MAXIMUM_ACTIVE_QUEUE_ITEMS = 64
MAXIMUM_SERVER_NOTIFICATIONS = 16
MAXIMUM_CONTROL_TRANSITION_RECORDS = 256
MAXIMUM_CONTROL_TRANSITION_BYTES = 1024 * 1024
MAXIMUM_CONTROL_CAPTURE_BYTES = 8 * 1024 * 1024


ControlSectionKind = Literal[
    "session_lifecycle",
    "run_control",
    "pending_interaction",
    "prompt_queue",
    "notifications",
]
CONTROL_SECTION_ORDER: tuple[ControlSectionKind, ...] = (
    "session_lifecycle",
    "run_control",
    "pending_interaction",
    "prompt_queue",
    "notifications",
)


@dataclass(frozen=True, slots=True)
class TerminalControlSectionSourceVersion:
    section_kind: ControlSectionKind
    source_owner_id: str
    source_owner_generation: int
    source_owner_revision: int
    source_view_fingerprint: str
    source_version_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalControlSourceCaptureFenceReceipt:
    """Proof that one source view was read behind the common capture barrier."""

    section_kind: ControlSectionKind
    source_owner_id: str
    source_owner_generation: int
    snapshot_source_revision: int
    snapshot_source_view_fingerprint: str
    capture_through_revision: int
    capture_through_view_fingerprint: str
    capture_barrier_id: str
    capture_registration_id: str
    acknowledged_callback_count: int
    acknowledged_callback_accumulator: str
    fence_receipt_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalControlCaptureInput:
    session_snapshot: TerminalUiSessionSnapshot
    prompt_queue: PromptQueueClientProjection
    section_view_fingerprints: tuple[tuple[ControlSectionKind, str], ...]
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalControlCapturedBaseline:
    capture_input: TerminalControlCaptureInput
    capture_barrier_id: str
    capture_ordinal: int
    common_sequencer_fingerprint: str
    ordered_fence_receipts: tuple[TerminalControlSourceCaptureFenceReceipt, ...]
    captured_baseline_fingerprint: str


@dataclass(frozen=True, slots=True)
class PromptQueueProjectionHead:
    head_kind: Literal["empty_genesis", "committed"]
    checkpoint_generation: int
    checkpoint_through_sequence: int
    checkpoint_fingerprint: str
    checkpoint_transition_count: int
    checkpoint_transition_accumulator: str
    bounded_tail_first_sequence_or_zero: int
    bounded_tail_last_sequence_or_zero: int
    bounded_tail_count: int
    bounded_tail_accumulator: str
    head_event_id: str | None
    head_event_sequence: int
    head_event_payload_fingerprint: str | None
    head_receipt_fingerprint: str
    head_fingerprint: str


@dataclass(frozen=True, slots=True)
class PromptQueueClientProjection:
    queue_head: PromptQueueProjectionHead
    queue_account_revision: int
    ordered_active_items: tuple[PromptQueueItemView, ...]
    active_item_count: int
    active_item_accumulator: str
    projection_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalControlProjectionView:
    runtime_session_id: str
    lifecycle: Literal["open", "closing", "closed"]
    active_run_id: str | None
    suspended_run_id: str | None
    stopping_run_id: str | None
    pending_interaction: TerminalInteractionRequestView | None
    prompt_queue: PromptQueueClientProjection
    ordered_server_notifications: tuple[object, ...]
    section_versions: tuple[TerminalControlSectionSourceVersion, ...]
    control_view_fingerprint: str


@dataclass(frozen=True, slots=True)
class ControlProjectionCursor:
    control_generation: int
    control_revision: int
    control_projection_fingerprint: str
    transition_prefix_accumulator: str
    registry_contract_fingerprint: str
    cursor_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalControlProjectionSnapshot:
    view: TerminalControlProjectionView
    cursor: ControlProjectionCursor
    snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class TerminalProjectionSnapshotBundle:
    session_snapshot: TerminalUiSessionSnapshot
    control_snapshot: TerminalControlProjectionSnapshot


@dataclass(frozen=True, slots=True)
class ControlProjectionTransitionRecord:
    control_generation: int
    transition_ordinal: int
    base_control_projection_revision: int
    base_control_projection_fingerprint: str
    resulting_control_projection_revision: int
    resulting_control_projection_fingerprint: str
    changed_sections: tuple[ControlSectionKind, ...]
    transition_semantic_fingerprint: str
    previous_transition_prefix_accumulator: str
    resulting_transition_prefix_accumulator: str
    record_fingerprint: str


@dataclass(frozen=True, slots=True)
class ControlProjectionRead:
    status: Literal["same", "changed", "gap"]
    requested_cursor: ControlProjectionCursor
    latest_cursor: ControlProjectionCursor
    ordered_records: tuple[ControlProjectionTransitionRecord, ...]
    changed_sections: tuple[ControlSectionKind, ...]
    transition_range_accumulator: str
    gap_reason: (
        Literal[
            "generation_changed",
            "cursor_too_old",
            "transition_not_contiguous",
            "contract_changed",
        ]
        | None
    )


@dataclass(slots=True)
class TerminalControlSourceCaptureOwner:
    """Independent linearization owner for the five control source views.

    Host control sources are process-local and are all mutated on the Host event
    loop.  The owner invokes the supplied reader while holding its own common
    sequencer lock, then signs one fence per source.  Projection code receives
    only the resulting immutable baseline and cannot invent source revisions.
    """

    runtime_session_id: str
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _capture_ordinal: int = field(default=0, init=False, repr=False)
    _source_revisions: dict[ControlSectionKind, int] = field(
        default_factory=dict, init=False, repr=False
    )
    _source_fingerprints: dict[ControlSectionKind, str] = field(
        default_factory=dict, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.runtime_session_id:
            raise ValueError("terminal control capture requires a runtime session")

    def capture(
        self,
        reader: Callable[[], TerminalControlCaptureInput],
        *,
        deadline_monotonic: float | None = None,
    ) -> TerminalControlCapturedBaseline:
        with self._lock:
            if self._closed:
                raise RuntimeError("terminal control capture owner is closed")
            _require_capture_deadline(deadline_monotonic)
            captured_input = reader()
            _require_capture_deadline(deadline_monotonic)
            if (
                captured_input.session_snapshot.runtime_session_id
                != self.runtime_session_id
            ):
                raise ValueError("terminal control capture crosses sessions")
            logical = dict(captured_input.section_view_fingerprints)
            if tuple(logical) != CONTROL_SECTION_ORDER:
                raise ValueError("terminal control capture section order is incomplete")
            self._capture_ordinal += 1
            capture_ordinal = self._capture_ordinal
            barrier_id = (
                f"terminal-control-capture:{self.runtime_session_id}:{capture_ordinal}"
            )
            sequencer_fingerprint = context_fingerprint(
                "terminal-control-common-publication-sequencer:v1",
                {
                    "runtime_session_id": self.runtime_session_id,
                    "capture_ordinal": capture_ordinal,
                    "capture_barrier_id": barrier_id,
                    "captured_input_fingerprint": captured_input.input_fingerprint,
                },
            )
            empty_callbacks = context_fingerprint(
                "terminal-control-capture-callbacks:v1", ()
            )
            receipts: list[TerminalControlSourceCaptureFenceReceipt] = []
            for section_kind in CONTROL_SECTION_ORDER:
                view_fingerprint = logical[section_kind]
                previous = self._source_fingerprints.get(section_kind)
                if previous != view_fingerprint:
                    self._source_revisions[section_kind] = (
                        self._source_revisions.get(section_kind, 0) + 1
                    )
                    self._source_fingerprints[section_kind] = view_fingerprint
                revision = self._source_revisions[section_kind]
                source_owner_id = (
                    f"terminal-control-source:{self.runtime_session_id}:{section_kind}"
                )
                registration_id = (
                    f"terminal-control-registration:{capture_ordinal}:{section_kind}"
                )
                payload = {
                    "section_kind": section_kind,
                    "source_owner_id": source_owner_id,
                    "source_owner_generation": 1,
                    "snapshot_source_revision": revision,
                    "snapshot_source_view_fingerprint": view_fingerprint,
                    "capture_through_revision": revision,
                    "capture_through_view_fingerprint": view_fingerprint,
                    "capture_barrier_id": barrier_id,
                    "capture_registration_id": registration_id,
                    "acknowledged_callback_count": 0,
                    "acknowledged_callback_accumulator": empty_callbacks,
                }
                receipts.append(
                    TerminalControlSourceCaptureFenceReceipt(
                        **payload,
                        fence_receipt_fingerprint=context_fingerprint(
                            "terminal-control-source-capture-fence:v1", payload
                        ),
                    )
                )
            receipt_tuple = tuple(receipts)
            baseline_payload = {
                "captured_input_fingerprint": captured_input.input_fingerprint,
                "capture_barrier_id": barrier_id,
                "capture_ordinal": capture_ordinal,
                "common_sequencer_fingerprint": sequencer_fingerprint,
                "fence_receipt_fingerprints": tuple(
                    item.fence_receipt_fingerprint for item in receipt_tuple
                ),
            }
            if (
                len(canonical_json_bytes(baseline_payload))
                > MAXIMUM_CONTROL_CAPTURE_BYTES
            ):
                raise RuntimeError("terminal control capture exceeds its byte bound")
            return TerminalControlCapturedBaseline(
                capture_input=captured_input,
                capture_barrier_id=barrier_id,
                capture_ordinal=capture_ordinal,
                common_sequencer_fingerprint=sequencer_fingerprint,
                ordered_fence_receipts=receipt_tuple,
                captured_baseline_fingerprint=context_fingerprint(
                    "terminal-control-captured-baseline:v1", baseline_payload
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True


@dataclass(slots=True)
class TerminalControlProjectionStore:
    """Freeze all control sections behind one lock and one monotonic cursor."""

    runtime_session_id: str
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _revision: int = field(default=0, init=False, repr=False)
    _transition_accumulator: str = field(init=False, repr=False)
    _logical_fingerprints: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )
    _snapshot: TerminalControlProjectionSnapshot | None = field(
        default=None, init=False, repr=False
    )
    _transition_records: list[ControlProjectionTransitionRecord] = field(
        default_factory=list, init=False, repr=False
    )
    _transition_record_bytes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.runtime_session_id:
            raise ValueError("terminal control projection requires a runtime session")
        self._transition_accumulator = ""

    def install_captured(
        self, captured: TerminalControlCapturedBaseline
    ) -> TerminalControlProjectionSnapshot:
        _validate_captured_baseline(
            captured, runtime_session_id=self.runtime_session_id
        )
        session_snapshot = captured.capture_input.session_snapshot
        queue_projection = captured.capture_input.prompt_queue
        if session_snapshot.runtime_session_id != self.runtime_session_id:
            raise ValueError("terminal control projection crosses sessions")
        logical = dict(captured.capture_input.section_view_fingerprints)
        versions = tuple(
            _source_version_from_fence(item) for item in captured.ordered_fence_receipts
        )
        with self._lock:
            changed = tuple(
                key
                for key in CONTROL_SECTION_ORDER
                if self._logical_fingerprints.get(key) != logical[key]
            )
            if self._snapshot is not None and not changed:
                if self._snapshot.view.section_versions != versions:
                    raise ValueError(
                        "terminal control source version changed without its view"
                    )
                return self._snapshot
            previous_snapshot = self._snapshot
            previous_revision = self._revision
            previous_projection_fingerprint = (
                previous_snapshot.view.control_view_fingerprint
                if previous_snapshot is not None
                else None
            )
            self._revision += 1
            self._logical_fingerprints = dict(logical)
            view_payload = {
                "runtime_session_id": self.runtime_session_id,
                "lifecycle": session_snapshot.lifecycle,
                "active_run_id": session_snapshot.active_run_id,
                "suspended_run_id": session_snapshot.suspended_run_id,
                "stopping_run_id": session_snapshot.stopping_run_id,
                "pending_interaction_fingerprint": (
                    session_snapshot.pending_interaction.view_fingerprint
                    if session_snapshot.pending_interaction is not None
                    else None
                ),
                "prompt_queue_projection_fingerprint": (
                    queue_projection.projection_fingerprint
                ),
                "notification_fingerprints": (),
                "source_version_fingerprints": tuple(
                    item.source_version_fingerprint for item in versions
                ),
            }
            view_fingerprint = context_fingerprint(
                "terminal-control-projection-view:v1", view_payload
            )
            view = TerminalControlProjectionView(
                runtime_session_id=self.runtime_session_id,
                lifecycle=session_snapshot.lifecycle,
                active_run_id=session_snapshot.active_run_id,
                suspended_run_id=session_snapshot.suspended_run_id,
                stopping_run_id=session_snapshot.stopping_run_id,
                pending_interaction=session_snapshot.pending_interaction,
                prompt_queue=queue_projection,
                ordered_server_notifications=(),
                section_versions=versions,
                control_view_fingerprint=view_fingerprint,
            )
            if previous_snapshot is None:
                self._transition_accumulator = context_fingerprint(
                    "terminal-control-transition-genesis:v1",
                    {
                        "control_generation": 1,
                        "control_revision": self._revision,
                        "initial_projection_fingerprint": view_fingerprint,
                        "registry_contract_fingerprint": (
                            TERMINAL_CONTROL_REGISTRY_CONTRACT_FINGERPRINT
                        ),
                    },
                )
            else:
                assert previous_projection_fingerprint is not None
                transition_semantic_fingerprint = context_fingerprint(
                    "terminal-control-transition-semantic:v1",
                    {
                        "control_generation": 1,
                        "transition_ordinal": self._revision,
                        "base_control_projection_revision": previous_revision,
                        "base_control_projection_fingerprint": (
                            previous_projection_fingerprint
                        ),
                        "resulting_control_projection_revision": self._revision,
                        "resulting_control_projection_fingerprint": view_fingerprint,
                        "changed_sections": changed,
                        "registry_contract_fingerprint": (
                            TERMINAL_CONTROL_REGISTRY_CONTRACT_FINGERPRINT
                        ),
                    },
                )
                previous_accumulator = self._transition_accumulator
                resulting_accumulator = context_fingerprint(
                    "terminal-control-transition-step:v1",
                    {
                        "previous_transition_prefix_accumulator": (
                            previous_accumulator
                        ),
                        "transition_semantic_fingerprint": (
                            transition_semantic_fingerprint
                        ),
                    },
                )
                record_payload = {
                    "control_generation": 1,
                    "transition_ordinal": self._revision,
                    "base_control_projection_revision": previous_revision,
                    "base_control_projection_fingerprint": (
                        previous_projection_fingerprint
                    ),
                    "resulting_control_projection_revision": self._revision,
                    "resulting_control_projection_fingerprint": view_fingerprint,
                    "changed_sections": changed,
                    "transition_semantic_fingerprint": (
                        transition_semantic_fingerprint
                    ),
                    "previous_transition_prefix_accumulator": previous_accumulator,
                    "resulting_transition_prefix_accumulator": resulting_accumulator,
                }
                record = ControlProjectionTransitionRecord(
                    **record_payload,
                    record_fingerprint=context_fingerprint(
                        "terminal-control-transition-record:v1", record_payload
                    ),
                )
                self._transition_accumulator = resulting_accumulator
                self._append_transition_unlocked(record)
            cursor_payload = {
                "control_generation": 1,
                "control_revision": self._revision,
                "control_projection_fingerprint": view_fingerprint,
                "transition_prefix_accumulator": self._transition_accumulator,
                "registry_contract_fingerprint": (
                    TERMINAL_CONTROL_REGISTRY_CONTRACT_FINGERPRINT
                ),
            }
            cursor = ControlProjectionCursor(
                **cursor_payload,
                cursor_fingerprint=context_fingerprint(
                    "terminal-control-cursor:v1", cursor_payload
                ),
            )
            snapshot = TerminalControlProjectionSnapshot(
                view=view,
                cursor=cursor,
                snapshot_fingerprint=context_fingerprint(
                    "terminal-control-projection-snapshot:v1",
                    {
                        "view_fingerprint": view_fingerprint,
                        "cursor_fingerprint": cursor.cursor_fingerprint,
                    },
                ),
            )
            self._snapshot = snapshot
            return snapshot

    def snapshot(self) -> TerminalControlProjectionSnapshot:
        with self._lock:
            if self._snapshot is None:
                raise RuntimeError("terminal control projection is not initialized")
            return self._snapshot

    def read_after(
        self, requested_cursor: ControlProjectionCursor
    ) -> ControlProjectionRead:
        with self._lock:
            if self._snapshot is None:
                raise RuntimeError("terminal control projection is not initialized")
            latest = self._snapshot.cursor
            gap_reason = _control_cursor_gap_reason(requested_cursor, latest)
            if gap_reason is not None:
                return ControlProjectionRead(
                    status="gap",
                    requested_cursor=requested_cursor,
                    latest_cursor=latest,
                    ordered_records=(),
                    changed_sections=(),
                    transition_range_accumulator="",
                    gap_reason=gap_reason,
                )
            if requested_cursor.cursor_fingerprint == latest.cursor_fingerprint:
                return ControlProjectionRead(
                    status="same",
                    requested_cursor=requested_cursor,
                    latest_cursor=latest,
                    ordered_records=(),
                    changed_sections=(),
                    transition_range_accumulator=context_fingerprint(
                        "terminal-control-transition-range-genesis:v1",
                        requested_cursor.cursor_fingerprint,
                    ),
                    gap_reason=None,
                )
            records = tuple(
                record
                for record in self._transition_records
                if record.resulting_control_projection_revision
                > requested_cursor.control_revision
            )
            if (
                not records
                or records[0].base_control_projection_revision
                != requested_cursor.control_revision
                or records[0].base_control_projection_fingerprint
                != requested_cursor.control_projection_fingerprint
            ):
                return ControlProjectionRead(
                    status="gap",
                    requested_cursor=requested_cursor,
                    latest_cursor=latest,
                    ordered_records=(),
                    changed_sections=(),
                    transition_range_accumulator="",
                    gap_reason="cursor_too_old",
                )
            expected_revision = requested_cursor.control_revision
            expected_projection = requested_cursor.control_projection_fingerprint
            expected_prefix = requested_cursor.transition_prefix_accumulator
            range_accumulator = context_fingerprint(
                "terminal-control-transition-range-genesis:v1",
                requested_cursor.cursor_fingerprint,
            )
            changed_sections: set[ControlSectionKind] = set()
            for record in records:
                if (
                    record.base_control_projection_revision != expected_revision
                    or record.base_control_projection_fingerprint != expected_projection
                    or record.previous_transition_prefix_accumulator != expected_prefix
                ):
                    return ControlProjectionRead(
                        status="gap",
                        requested_cursor=requested_cursor,
                        latest_cursor=latest,
                        ordered_records=(),
                        changed_sections=(),
                        transition_range_accumulator="",
                        gap_reason="transition_not_contiguous",
                    )
                expected_revision = record.resulting_control_projection_revision
                expected_projection = record.resulting_control_projection_fingerprint
                expected_prefix = record.resulting_transition_prefix_accumulator
                changed_sections.update(record.changed_sections)
                range_accumulator = context_fingerprint(
                    "terminal-control-transition-range-step:v1",
                    {
                        "previous_range_accumulator": range_accumulator,
                        "record_fingerprint": record.record_fingerprint,
                    },
                )
            if (
                expected_revision != latest.control_revision
                or expected_projection != latest.control_projection_fingerprint
                or expected_prefix != latest.transition_prefix_accumulator
            ):
                return ControlProjectionRead(
                    status="gap",
                    requested_cursor=requested_cursor,
                    latest_cursor=latest,
                    ordered_records=(),
                    changed_sections=(),
                    transition_range_accumulator="",
                    gap_reason="transition_not_contiguous",
                )
            canonical_sections = tuple(
                item
                for item in (
                    "session_lifecycle",
                    "run_control",
                    "pending_interaction",
                    "prompt_queue",
                    "notifications",
                )
                if item in changed_sections
            )
            return ControlProjectionRead(
                status="changed",
                requested_cursor=requested_cursor,
                latest_cursor=latest,
                ordered_records=records,
                changed_sections=canonical_sections,  # type: ignore[arg-type]
                transition_range_accumulator=range_accumulator,
                gap_reason=None,
            )

    def _append_transition_unlocked(
        self, record: ControlProjectionTransitionRecord
    ) -> None:
        encoded_bytes = len(canonical_json_bytes(asdict(record)))
        self._transition_records.append(record)
        self._transition_record_bytes += encoded_bytes
        while self._transition_records and (
            len(self._transition_records) > MAXIMUM_CONTROL_TRANSITION_RECORDS
            or self._transition_record_bytes > MAXIMUM_CONTROL_TRANSITION_BYTES
        ):
            removed = self._transition_records.pop(0)
            self._transition_record_bytes -= len(canonical_json_bytes(asdict(removed)))


def _control_cursor_gap_reason(
    requested: ControlProjectionCursor,
    latest: ControlProjectionCursor,
) -> (
    Literal[
        "generation_changed",
        "cursor_too_old",
        "transition_not_contiguous",
        "contract_changed",
    ]
    | None
):
    expected_fingerprint = context_fingerprint(
        "terminal-control-cursor:v1",
        {
            "control_generation": requested.control_generation,
            "control_revision": requested.control_revision,
            "control_projection_fingerprint": (
                requested.control_projection_fingerprint
            ),
            "transition_prefix_accumulator": (requested.transition_prefix_accumulator),
            "registry_contract_fingerprint": (requested.registry_contract_fingerprint),
        },
    )
    if (
        requested.registry_contract_fingerprint
        != TERMINAL_CONTROL_REGISTRY_CONTRACT_FINGERPRINT
        or requested.cursor_fingerprint != expected_fingerprint
    ):
        return "contract_changed"
    if requested.control_generation != latest.control_generation:
        return "generation_changed"
    if requested.control_revision > latest.control_revision:
        return "transition_not_contiguous"
    if requested.control_revision == latest.control_revision:
        if (
            requested.control_projection_fingerprint
            != latest.control_projection_fingerprint
            or requested.transition_prefix_accumulator
            != latest.transition_prefix_accumulator
        ):
            return "transition_not_contiguous"
    return None


def _source_version_from_fence(
    receipt: TerminalControlSourceCaptureFenceReceipt,
) -> TerminalControlSectionSourceVersion:
    payload = {
        "section_kind": receipt.section_kind,
        "source_owner_id": receipt.source_owner_id,
        "source_owner_generation": receipt.source_owner_generation,
        "source_owner_revision": receipt.capture_through_revision,
        "source_view_fingerprint": receipt.capture_through_view_fingerprint,
    }
    return TerminalControlSectionSourceVersion(
        **payload,
        source_version_fingerprint=context_fingerprint(
            "terminal-control-section-source-version:v1", payload
        ),
    )


def build_terminal_control_capture_input(
    *,
    session_snapshot: TerminalUiSessionSnapshot,
    queue_checkpoint,
    queue_head_receipt,
    durable_active_item_count: int,
    durable_active_item_accumulator: str,
) -> TerminalControlCaptureInput:
    """Build the immutable five-section input before source fencing."""

    active_items = tuple(
        sorted(
            (
                item
                for item in session_snapshot.queue_items
                if item.delivery_state
                in {
                    "accepted_pending",
                    "steer_reserved",
                    "follow_up_reserved",
                    "reconciliation_required",
                }
                and item.content_retention_state == "active"
            ),
            key=lambda item: (item.accepted_ordinal, item.queue_item_id),
        )
    )
    if len(active_items) > MAXIMUM_ACTIVE_QUEUE_ITEMS:
        raise RuntimeError("terminal active prompt queue projection exceeds 64")
    if len({item.queue_item_id for item in active_items}) != len(active_items):
        raise ValueError("terminal active prompt queue identities are duplicated")
    queue_projection = build_prompt_queue_client_projection(
        checkpoint=queue_checkpoint,
        head_receipt=queue_head_receipt,
        queue_account_revision=session_snapshot.queue_account_revision,
        ordered_active_items=active_items,
        durable_active_item_count=durable_active_item_count,
        durable_active_item_accumulator=durable_active_item_accumulator,
    )
    logical = _control_section_logical_fingerprints(
        session_snapshot=session_snapshot,
        queue_projection=queue_projection,
    )
    payload = {
        "session_snapshot_fingerprint": session_snapshot.snapshot_fingerprint,
        "prompt_queue_projection_fingerprint": queue_projection.projection_fingerprint,
        "section_view_fingerprints": logical,
    }
    return TerminalControlCaptureInput(
        session_snapshot=session_snapshot,
        prompt_queue=queue_projection,
        section_view_fingerprints=logical,
        input_fingerprint=context_fingerprint(
            "terminal-control-capture-input:v1", payload
        ),
    )


def build_prompt_queue_client_projection(
    *,
    checkpoint,
    head_receipt,
    queue_account_revision: int,
    ordered_active_items: tuple[PromptQueueItemView, ...],
    durable_active_item_count: int,
    durable_active_item_accumulator: str,
) -> PromptQueueClientProjection:
    if (
        head_receipt.checkpoint_fingerprint != checkpoint.checkpoint_fingerprint
        or head_receipt.checkpoint_generation != checkpoint.checkpoint_generation
        or head_receipt.checkpoint_through_sequence != checkpoint.through_sequence
        or head_receipt.checkpoint_transition_count != checkpoint.transition_count
        or head_receipt.checkpoint_transition_accumulator
        != checkpoint.transition_accumulator
        or head_receipt.resulting_account_revision != queue_account_revision
        or head_receipt.resulting_active_client_item_count != durable_active_item_count
        or head_receipt.resulting_active_client_item_accumulator
        != durable_active_item_accumulator
    ):
        raise ValueError("prompt queue checkpoint/head/account proof drifted")
    tail_count = head_receipt.bounded_tail_count
    tail_first = head_receipt.bounded_tail_first_sequence
    tail_last = head_receipt.bounded_tail_last_sequence
    tail_accumulator = head_receipt.bounded_tail_accumulator
    queue_head_event_id = head_receipt.resulting_queue_head_event_id
    queue_head_event_sequence = tail_last or checkpoint.through_sequence
    queue_head_payload_fingerprint = (
        head_receipt.resulting_queue_head_payload_fingerprint
    )
    total = checkpoint.transition_count + tail_count
    if total == 0:
        payload = {
            "head_kind": "empty_genesis",
            "checkpoint_generation": 0,
            "checkpoint_through_sequence": 0,
            "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint,
            "checkpoint_transition_count": 0,
            "bounded_tail_count": 0,
            "head_receipt_fingerprint": head_receipt.receipt_fingerprint,
        }
        head = PromptQueueProjectionHead(
            **payload,
            checkpoint_transition_accumulator=checkpoint.transition_accumulator,
            bounded_tail_first_sequence_or_zero=0,
            bounded_tail_last_sequence_or_zero=0,
            bounded_tail_accumulator=tail_accumulator,
            head_event_id=None,
            head_event_sequence=0,
            head_event_payload_fingerprint=None,
            head_fingerprint=context_fingerprint(
                "terminal-active-prompt-queue-empty-head:v1", payload
            ),
        )
    else:
        if not queue_head_event_id or queue_head_event_sequence < 1:
            raise ValueError("committed prompt queue head lacks event identity")
        payload = {
            "head_kind": "committed",
            "checkpoint_generation": checkpoint.checkpoint_generation,
            "checkpoint_through_sequence": checkpoint.through_sequence,
            "checkpoint_fingerprint": checkpoint.checkpoint_fingerprint,
            "checkpoint_transition_count": checkpoint.transition_count,
            "checkpoint_transition_accumulator": checkpoint.transition_accumulator,
            "bounded_tail_first_sequence_or_zero": tail_first,
            "bounded_tail_last_sequence_or_zero": tail_last,
            "bounded_tail_count": tail_count,
            "bounded_tail_accumulator": tail_accumulator,
            "head_event_id": queue_head_event_id,
            "head_event_sequence": queue_head_event_sequence,
            "head_event_payload_fingerprint": queue_head_payload_fingerprint,
            "head_receipt_fingerprint": head_receipt.receipt_fingerprint,
        }
        head = PromptQueueProjectionHead(
            **payload,
            head_fingerprint=context_fingerprint(
                "terminal-active-prompt-queue-committed-head:v1", payload
            ),
        )
    active_accumulator = context_fingerprint(
        "terminal-active-prompt-queue-items:v1",
        tuple(item.view_fingerprint for item in ordered_active_items),
    )
    if (
        durable_active_item_count != len(ordered_active_items)
        or durable_active_item_accumulator != active_accumulator
    ):
        raise ValueError("prompt queue durable active projection proof drifted")
    projection_payload = {
        "contract_fingerprint": (TERMINAL_ACTIVE_QUEUE_PROJECTION_CONTRACT_FINGERPRINT),
        "head_fingerprint": head.head_fingerprint,
        "queue_account_revision": queue_account_revision,
        "active_item_count": len(ordered_active_items),
        "active_item_accumulator": active_accumulator,
    }
    return PromptQueueClientProjection(
        queue_head=head,
        queue_account_revision=queue_account_revision,
        ordered_active_items=ordered_active_items,
        active_item_count=len(ordered_active_items),
        active_item_accumulator=active_accumulator,
        projection_fingerprint=context_fingerprint(
            "terminal-active-prompt-queue-projection:v1", projection_payload
        ),
    )


def _control_section_logical_fingerprints(
    *,
    session_snapshot: TerminalUiSessionSnapshot,
    queue_projection: PromptQueueClientProjection,
) -> tuple[tuple[ControlSectionKind, str], ...]:
    return (
        (
            "session_lifecycle",
            context_fingerprint(
                "terminal-control-session-lifecycle:v1",
                session_snapshot.lifecycle,
            ),
        ),
        (
            "run_control",
            context_fingerprint(
                "terminal-control-run-control:v1",
                {
                    "active_run_id": session_snapshot.active_run_id,
                    "suspended_run_id": session_snapshot.suspended_run_id,
                    "stopping_run_id": session_snapshot.stopping_run_id,
                },
            ),
        ),
        (
            "pending_interaction",
            context_fingerprint(
                "terminal-control-pending-interaction:v1",
                (
                    session_snapshot.pending_interaction.view_fingerprint
                    if session_snapshot.pending_interaction is not None
                    else None
                ),
            ),
        ),
        ("prompt_queue", queue_projection.projection_fingerprint),
        (
            "notifications",
            context_fingerprint("terminal-control-server-notifications:v1", ()),
        ),
    )


def _validate_captured_baseline(
    captured: TerminalControlCapturedBaseline,
    *,
    runtime_session_id: str,
) -> None:
    captured_input = captured.capture_input
    logical = _control_section_logical_fingerprints(
        session_snapshot=captured_input.session_snapshot,
        queue_projection=captured_input.prompt_queue,
    )
    input_payload = {
        "session_snapshot_fingerprint": (
            captured_input.session_snapshot.snapshot_fingerprint
        ),
        "prompt_queue_projection_fingerprint": (
            captured_input.prompt_queue.projection_fingerprint
        ),
        "section_view_fingerprints": logical,
    }
    if (
        captured_input.session_snapshot.runtime_session_id != runtime_session_id
        or captured_input.section_view_fingerprints != logical
        or captured_input.input_fingerprint
        != context_fingerprint("terminal-control-capture-input:v1", input_payload)
        or len(captured.ordered_fence_receipts) != len(CONTROL_SECTION_ORDER)
        or captured.capture_ordinal < 1
    ):
        raise ValueError("terminal control captured baseline identity is invalid")
    expected_barrier = (
        f"terminal-control-capture:{runtime_session_id}:{captured.capture_ordinal}"
    )
    expected_sequencer = context_fingerprint(
        "terminal-control-common-publication-sequencer:v1",
        {
            "runtime_session_id": runtime_session_id,
            "capture_ordinal": captured.capture_ordinal,
            "capture_barrier_id": expected_barrier,
            "captured_input_fingerprint": captured_input.input_fingerprint,
        },
    )
    if (
        captured.capture_barrier_id != expected_barrier
        or captured.common_sequencer_fingerprint != expected_sequencer
    ):
        raise ValueError("terminal control capture sequencer proof is invalid")
    empty_callbacks = context_fingerprint("terminal-control-capture-callbacks:v1", ())
    logical_by_section = dict(logical)
    for section_kind, receipt in zip(
        CONTROL_SECTION_ORDER, captured.ordered_fence_receipts, strict=True
    ):
        expected_owner = f"terminal-control-source:{runtime_session_id}:{section_kind}"
        expected_registration = (
            f"terminal-control-registration:{captured.capture_ordinal}:{section_kind}"
        )
        payload = {
            "section_kind": section_kind,
            "source_owner_id": expected_owner,
            "source_owner_generation": 1,
            "snapshot_source_revision": receipt.snapshot_source_revision,
            "snapshot_source_view_fingerprint": logical_by_section[section_kind],
            "capture_through_revision": receipt.capture_through_revision,
            "capture_through_view_fingerprint": logical_by_section[section_kind],
            "capture_barrier_id": expected_barrier,
            "capture_registration_id": expected_registration,
            "acknowledged_callback_count": 0,
            "acknowledged_callback_accumulator": empty_callbacks,
        }
        if (
            receipt.section_kind != section_kind
            or receipt.source_owner_id != expected_owner
            or receipt.source_owner_generation != 1
            or receipt.snapshot_source_revision < 1
            or receipt.snapshot_source_revision != receipt.capture_through_revision
            or receipt.snapshot_source_view_fingerprint
            != logical_by_section[section_kind]
            or receipt.capture_through_view_fingerprint
            != logical_by_section[section_kind]
            or receipt.capture_barrier_id != expected_barrier
            or receipt.capture_registration_id != expected_registration
            or receipt.acknowledged_callback_count != 0
            or receipt.acknowledged_callback_accumulator != empty_callbacks
            or receipt.fence_receipt_fingerprint
            != context_fingerprint("terminal-control-source-capture-fence:v1", payload)
        ):
            raise ValueError("terminal control source fence receipt is invalid")
    baseline_payload = {
        "captured_input_fingerprint": captured_input.input_fingerprint,
        "capture_barrier_id": expected_barrier,
        "capture_ordinal": captured.capture_ordinal,
        "common_sequencer_fingerprint": expected_sequencer,
        "fence_receipt_fingerprints": tuple(
            item.fence_receipt_fingerprint for item in captured.ordered_fence_receipts
        ),
    }
    if captured.captured_baseline_fingerprint != context_fingerprint(
        "terminal-control-captured-baseline:v1", baseline_payload
    ):
        raise ValueError("terminal control captured baseline fingerprint is invalid")


def _require_capture_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("terminal control capture deadline expired")


__all__ = [
    "CONTROL_SECTION_ORDER",
    "ControlProjectionRead",
    "ControlProjectionCursor",
    "ControlProjectionTransitionRecord",
    "MAXIMUM_ACTIVE_QUEUE_ITEMS",
    "MAXIMUM_CONTROL_CAPTURE_BYTES",
    "MAXIMUM_SERVER_NOTIFICATIONS",
    "PromptQueueClientProjection",
    "PromptQueueProjectionHead",
    "TERMINAL_ACTIVE_QUEUE_PROJECTION_CONTRACT_FINGERPRINT",
    "TERMINAL_CONTROL_REGISTRY_CONTRACT_FINGERPRINT",
    "TerminalControlProjectionSnapshot",
    "TerminalControlProjectionStore",
    "TerminalControlSourceCaptureFenceReceipt",
    "TerminalControlSourceCaptureOwner",
    "TerminalProjectionSnapshotBundle",
    "build_prompt_queue_client_projection",
    "build_terminal_control_capture_input",
]
