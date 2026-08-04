"""Durable prompt-queue reducer, transaction companion, and mutation service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import RLock
from typing import Any, Iterable, Sequence

from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    PromptQueueAcceptedEvent,
    PromptQueueCancelledEvent,
    PromptQueueCommittedToProviderInputEvent,
    PromptQueueCommittedToRunEvent,
    PromptQueueContentRetiredEvent,
    PromptQueueDeliveryRejectedEvent,
    PromptQueueReconciliationRequiredEvent,
    PromptQueueReservationInstalledEvent,
    PromptQueueReservationReleasedEvent,
    UserSteerCommittedEvent,
)
from pulsara_agent.event_log.protocol import (
    EventLogPreparedCandidateBatchIdentity,
    EventLogStoredCandidateBatchRebindReceipt,
    build_prepared_candidate_batch_identity,
)
from pulsara_agent.event_log.serialization import (
    freeze_event_write_candidate,
    stable_event_identity,
)
from pulsara_agent.ports.event_write import FrozenEventWriteCandidate
from pulsara_agent.ports.model_lifecycle import (
    ModelLifecycleTransactionCompanionIdentityFact,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.prompt_queue import (
    CLIENT_VISIBLE_ACTIVE_QUEUE_STATES,
    MAX_ACTIVE_PROMPT_QUEUE_ITEMS,
    PROMPT_QUEUE_COMPANION_CHARGE_CONTRACT_FINGERPRINT,
    PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
    PROMPT_QUEUE_INLINE_MAX_UTF8_BYTES,
    PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT,
    PreparedPromptQueueContentFact,
    ConfirmedArtifactQueueContentFact,
    InlineQueueContentFact,
    PromptQueueAccountProjectionFact,
    PromptQueueCompanionChargeFact,
    PromptQueueDomainCheckpointFact,
    PromptQueueDeliveryMode,
    PromptQueueResolvedDeliveryMode,
    PromptQueueDeliveryState,
    PromptQueueReservationFact,
    PromptQueueTransitionHeadFact,
    UserSteerSemanticFact,
    build_prompt_queue_account_projection,
    prepare_inline_prompt_queue_content,
    prompt_queue_transition_genesis_accumulator,
)
from pulsara_agent.runtime.terminal_application.artifact_hold import (
    PromptQueueArtifactStoragePort,
    prompt_queue_content_semantic_fingerprint,
)
from pulsara_agent.runtime.terminal_presentation.public_text import (
    bounded_terminal_safe_public_text,
)


QUEUE_EVENT_TYPES = (
    PromptQueueAcceptedEvent,
    PromptQueueReservationInstalledEvent,
    PromptQueueReservationReleasedEvent,
    PromptQueueDeliveryRejectedEvent,
    PromptQueueCancelledEvent,
    PromptQueueReconciliationRequiredEvent,
    PromptQueueContentRetiredEvent,
    PromptQueueCommittedToRunEvent,
    PromptQueueCommittedToProviderInputEvent,
)


@dataclass(frozen=True, slots=True)
class PromptQueueProjectedItem:
    queue_item_id: str
    accepted_ordinal: int
    delivery_state: PromptQueueDeliveryState
    content_retention_state: str
    item_revision: int
    account_revision: int
    head_event_id: str
    head_event_type: str
    head_event_sequence: int
    head_candidate_payload_fingerprint: str
    prepared_content: PreparedPromptQueueContentFact | None
    requested_delivery_mode: PromptQueueDeliveryMode
    resolved_delivery_mode: PromptQueueResolvedDeliveryMode
    reservation: PromptQueueReservationFact | None
    disposition_code: str | None
    row_fingerprint: str

    def __post_init__(self) -> None:
        if self.resolved_delivery_mode not in {"pending", "steer", "follow_up"}:
            raise ValueError("queue resolved delivery mode is invalid")
        if (
            self.delivery_state == "accepted_pending"
            and self.resolved_delivery_mode != "pending"
        ):
            raise ValueError("pending queue item cannot have a resolved placement")
        if (
            self.delivery_state == "steer_reserved"
            and self.resolved_delivery_mode != "steer"
        ):
            raise ValueError("steer reservation placement mismatch")
        if (
            self.delivery_state == "follow_up_reserved"
            and self.resolved_delivery_mode != "follow_up"
        ):
            raise ValueError("follow-up reservation placement mismatch")


@dataclass(frozen=True, slots=True)
class PromptQueueProjectionSnapshot:
    runtime_session_id: str
    ledger_through_sequence: int
    account_revision: int
    next_accepted_ordinal: int
    transition_count: int
    transition_accumulator: str
    queue_head_event_id: str | None
    queue_head_event_type: str | None
    queue_head_event_sequence: int
    queue_head_payload_fingerprint: str | None
    items: tuple[PromptQueueProjectedItem, ...]
    active_client_item_count: int
    active_client_item_accumulator: str
    row_set_accumulator: str
    pending_head_set_accumulator: str
    bounded_tail_first_sequence: int
    bounded_tail_last_sequence: int
    bounded_tail_count: int
    bounded_tail_accumulator: str


@dataclass(slots=True)
class PromptQueueProjectionStore:
    runtime_session_id: str
    through_sequence: int = 0
    account_revision: int = 0
    next_accepted_ordinal: int = 1
    transition_count: int = 0
    head_event_id: str | None = None
    head_event_type: str | None = None
    head_event_sequence: int = 0
    head_candidate_payload_fingerprint: str | None = None
    transition_accumulator: str = field(init=False)
    _items: dict[str, PromptQueueProjectedItem] = field(
        default_factory=dict, init=False, repr=False
    )
    _bounded_tail_entries: list[tuple[str, int, str]] = field(
        default_factory=list, init=False, repr=False
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.transition_accumulator = prompt_queue_transition_genesis_accumulator(
            self.runtime_session_id
        )

    def apply_committed(self, events: Sequence[AgentEvent]) -> None:
        with self._lock:
            for event in events:
                if event.sequence is None:
                    raise ValueError("queue reducer requires stored events")
                if event.sequence != self.through_sequence + 1:
                    raise ValueError("queue reducer input is not contiguous")
                if isinstance(event, QUEUE_EVENT_TYPES):
                    self._apply_transition(event)
                self.through_sequence = event.sequence

    def apply_sparse_bootstrap(
        self,
        events: Sequence[AgentEvent],
        *,
        through_sequence: int,
    ) -> None:
        """Restore queue-owned events while preserving the ledger high-water.

        The typed EventLog query proves one atomic high-water and returns every
        queue-domain event in that prefix. Non-queue events are deterministic
        no-ops and therefore do not need decoding here.
        """

        sequences = tuple(event.sequence for event in events)
        if any(sequence is None for sequence in sequences):
            raise ValueError("queue sparse bootstrap requires stored events")
        concrete = tuple(
            int(sequence) for sequence in sequences if sequence is not None
        )
        if concrete != tuple(sorted(concrete)) or len(concrete) != len(set(concrete)):
            raise ValueError("queue sparse bootstrap is not ordered and unique")
        if concrete and concrete[-1] > through_sequence:
            raise ValueError("queue sparse bootstrap exceeds its high-water")
        with self._lock:
            for event in events:
                if not isinstance(event, QUEUE_EVENT_TYPES):
                    raise ValueError("queue sparse bootstrap contains a foreign event")
                self._apply_transition(event)
            self.through_sequence = through_sequence

    def restore_checkpoint(
        self,
        checkpoint: PromptQueueDomainCheckpointFact,
        *,
        item_payloads: Sequence[dict[str, object]],
        head_event_type: str | None,
    ) -> None:
        if checkpoint.runtime_session_id != self.runtime_session_id:
            raise ValueError("prompt queue checkpoint crosses runtime sessions")
        items = tuple(_projected_item_from_payload(item) for item in item_payloads)
        if len(items) != len({item.queue_item_id for item in items}):
            raise ValueError("prompt queue checkpoint item identities are duplicated")
        if _row_set_accumulator(items) != checkpoint.queue_row_set_accumulator:
            raise ValueError("prompt queue checkpoint row-set accumulator mismatch")
        if (
            _active_client_item_count(items) != checkpoint.active_client_item_count
            or _active_client_item_accumulator(items)
            != checkpoint.active_client_item_accumulator
        ):
            raise ValueError("prompt queue checkpoint active projection mismatch")
        if (
            _pending_head_set_accumulator(items)
            != checkpoint.pending_item_head_set_accumulator
        ):
            raise ValueError(
                "prompt queue checkpoint pending-head accumulator mismatch"
            )
        if (checkpoint.resulting_queue_head_event_id is None) != (
            head_event_type is None
        ):
            raise ValueError("prompt queue checkpoint head type matrix mismatch")
        with self._lock:
            self.through_sequence = checkpoint.through_sequence
            self.account_revision = checkpoint.account_revision
            self.next_accepted_ordinal = checkpoint.next_accepted_ordinal
            self.transition_count = checkpoint.transition_count
            self.transition_accumulator = checkpoint.transition_accumulator
            self.head_event_id = checkpoint.resulting_queue_head_event_id
            self.head_event_type = head_event_type
            self.head_event_sequence = checkpoint.through_sequence
            self.head_candidate_payload_fingerprint = (
                checkpoint.resulting_queue_head_payload_fingerprint
            )
            self._items = {item.queue_item_id: item for item in items}
            self._bounded_tail_entries.clear()

    def validate_durable_projection(
        self,
        *,
        account: PromptQueueAccountProjectionFact,
        item_payloads: Sequence[dict[str, object]],
    ) -> None:
        durable_items = tuple(
            _projected_item_from_payload(item) for item in item_payloads
        )
        with self._lock:
            resident_items = tuple(
                sorted(self._items.values(), key=lambda item: item.queue_item_id)
            )
            if (
                tuple(sorted(durable_items, key=lambda item: item.queue_item_id))
                != resident_items
            ):
                raise ValueError("prompt queue item rows drifted from event authority")
            if (
                account.runtime_session_id != self.runtime_session_id
                or account.account_revision != self.account_revision
                or account.next_accepted_ordinal != self.next_accepted_ordinal
                or account.transition_count != self.transition_count
                or account.transition_accumulator != self.transition_accumulator
                or account.queue_chain_head_event_id != self.head_event_id
                or account.queue_chain_head_sequence != self.head_event_sequence
                or account.queue_chain_head_payload_fingerprint
                != self.head_candidate_payload_fingerprint
                or account.row_set_accumulator != _row_set_accumulator(resident_items)
                or account.pending_item_head_set_accumulator
                != _pending_head_set_accumulator(resident_items)
                or account.active_client_item_count
                != _active_client_item_count(resident_items)
                or account.active_client_item_accumulator
                != _active_client_item_accumulator(resident_items)
                or account.bounded_tail_count != len(self._bounded_tail_entries)
                or account.bounded_tail_first_sequence
                != self._bounded_tail_first_sequence()
                or account.bounded_tail_accumulator != self._bounded_tail_accumulator()
            ):
                raise ValueError("prompt queue account drifted from event authority")

    def rebuild(self, events: Iterable[AgentEvent]) -> None:
        with self._lock:
            self.through_sequence = 0
            self.account_revision = 0
            self.next_accepted_ordinal = 1
            self.transition_count = 0
            self.head_event_id = None
            self.head_event_type = None
            self.head_event_sequence = 0
            self.head_candidate_payload_fingerprint = None
            self.transition_accumulator = prompt_queue_transition_genesis_accumulator(
                self.runtime_session_id
            )
            self._items.clear()
            self._bounded_tail_entries.clear()
        self.apply_committed(tuple(events))

    def item(self, queue_item_id: str) -> PromptQueueProjectedItem | None:
        with self._lock:
            return self._items.get(queue_item_id)

    def pending_items(self, *, limit: int = 64) -> tuple[PromptQueueProjectedItem, ...]:
        if limit < 1 or limit > 256:
            raise ValueError("prompt queue list limit must be 1..256")
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._items.values()
                        if item.delivery_state
                        in {"accepted_pending", "steer_reserved", "follow_up_reserved"}
                    ),
                    key=lambda item: item.accepted_ordinal,
                )[:limit]
            )

    def active_client_items(self) -> tuple[PromptQueueProjectedItem, ...]:
        """Return the complete, bounded client-visible active set."""

        with self._lock:
            result = tuple(
                sorted(
                    (
                        item
                        for item in self._items.values()
                        if item.delivery_state in CLIENT_VISIBLE_ACTIVE_QUEUE_STATES
                        and item.content_retention_state == "active"
                    ),
                    key=lambda item: (item.accepted_ordinal, item.queue_item_id),
                )
            )
        if len(result) > MAX_ACTIVE_PROMPT_QUEUE_ITEMS:
            raise RuntimeError("active prompt queue projection exceeds 64 items")
        return result

    def all_items(self) -> tuple[PromptQueueProjectedItem, ...]:
        with self._lock:
            return tuple(
                sorted(self._items.values(), key=lambda item: item.accepted_ordinal)
            )

    def snapshot(self) -> PromptQueueProjectionSnapshot:
        """Freeze every checkpoint/account join under the reducer lock."""

        with self._lock:
            items = tuple(
                sorted(self._items.values(), key=lambda item: item.queue_item_id)
            )
            return PromptQueueProjectionSnapshot(
                runtime_session_id=self.runtime_session_id,
                ledger_through_sequence=self.through_sequence,
                account_revision=self.account_revision,
                next_accepted_ordinal=self.next_accepted_ordinal,
                transition_count=self.transition_count,
                transition_accumulator=self.transition_accumulator,
                queue_head_event_id=self.head_event_id,
                queue_head_event_type=self.head_event_type,
                queue_head_event_sequence=self.head_event_sequence,
                queue_head_payload_fingerprint=(
                    self.head_candidate_payload_fingerprint
                ),
                items=items,
                active_client_item_count=_active_client_item_count(items),
                active_client_item_accumulator=_active_client_item_accumulator(items),
                row_set_accumulator=_row_set_accumulator(items),
                pending_head_set_accumulator=_pending_head_set_accumulator(items),
                bounded_tail_first_sequence=self._bounded_tail_first_sequence(),
                bounded_tail_last_sequence=self._bounded_tail_last_sequence(),
                bounded_tail_count=len(self._bounded_tail_entries),
                bounded_tail_accumulator=self._bounded_tail_accumulator(),
            )

    def install_checkpoint_base(
        self, checkpoint: PromptQueueDomainCheckpointFact
    ) -> None:
        """Advance only the acceleration base while preserving a concurrent suffix."""

        with self._lock:
            if checkpoint.transition_count > self.transition_count:
                raise ValueError("prompt queue checkpoint exceeds live transition head")
            self._bounded_tail_entries = [
                entry
                for entry in self._bounded_tail_entries
                if entry[1] > checkpoint.through_sequence
            ]
            if len(self._bounded_tail_entries) != (
                self.transition_count - checkpoint.transition_count
            ):
                raise ValueError(
                    "prompt queue checkpoint cannot rebase its exact suffix"
                )

    def _bounded_tail_first_sequence(self) -> int:
        return self._bounded_tail_entries[0][1] if self._bounded_tail_entries else 0

    def _bounded_tail_last_sequence(self) -> int:
        return self._bounded_tail_entries[-1][1] if self._bounded_tail_entries else 0

    def _bounded_tail_accumulator(self) -> str:
        accumulator = context_fingerprint("prompt-queue-bounded-tail:v1", ())
        for (
            event_id,
            sequence,
            candidate_payload_fingerprint,
        ) in self._bounded_tail_entries:
            accumulator = context_fingerprint(
                "prompt-queue-bounded-tail-step:v1",
                {
                    "previous": accumulator,
                    "event_id": event_id,
                    "sequence": sequence,
                    "candidate_payload_fingerprint": candidate_payload_fingerprint,
                },
            )
        return accumulator

    def row_set_accumulator(self) -> str:
        with self._lock:
            return context_fingerprint(
                "prompt-queue-row-set:v1",
                tuple(
                    (item.queue_item_id, item.row_fingerprint)
                    for item in sorted(
                        self._items.values(), key=lambda value: value.queue_item_id
                    )
                ),
            )

    def pending_head_set_accumulator(self) -> str:
        with self._lock:
            return context_fingerprint(
                "prompt-queue-pending-head-set:v1",
                tuple(
                    (
                        item.queue_item_id,
                        item.head_event_id,
                        item.head_candidate_payload_fingerprint,
                    )
                    for item in sorted(
                        self._items.values(), key=lambda value: value.queue_item_id
                    )
                    if item.delivery_state
                    in {"accepted_pending", "steer_reserved", "follow_up_reserved"}
                ),
            )

    def _apply_transition(self, event) -> None:
        if len(self._bounded_tail_entries) >= 256:
            raise ValueError("prompt queue bounded tail requires checkpoint admission")
        transition = event.transition
        if transition.runtime_session_id != self.runtime_session_id:
            raise ValueError("queue event crosses runtime sessions")
        existing = self._items.get(transition.queue_item_id)
        if isinstance(event, PromptQueueAcceptedEvent):
            if existing is not None or transition.previous_delivery_state is not None:
                raise ValueError("queue acceptance conflicts with existing item")
            prepared = event.prepared_content
            requested = event.requested_delivery_mode
            resolved = event.resolved_delivery_mode
            reservation = None
            disposition = None
            if transition.accepted_ordinal != self.next_accepted_ordinal:
                raise ValueError("queue accepted ordinal is not contiguous")
            self.next_accepted_ordinal += 1
        else:
            if existing is None:
                raise ValueError("queue transition references missing item")
            if (
                transition.previous_delivery_state != existing.delivery_state
                or transition.expected_item_revision != existing.item_revision
                or transition.predecessor_event_reference is None
                or transition.predecessor_event_reference.event_id
                != existing.head_event_id
                or transition.predecessor_event_reference.sequence
                != existing.head_event_sequence
                or transition.predecessor_event_reference.event_type
                != existing.head_event_type
                or transition.predecessor_candidate_payload_fingerprint
                != existing.head_candidate_payload_fingerprint
            ):
                raise ValueError("queue transition predecessor drifted")
            prepared = existing.prepared_content
            requested = existing.requested_delivery_mode
            resolved = existing.resolved_delivery_mode
            reservation = existing.reservation
            disposition = existing.disposition_code
            if isinstance(event, PromptQueueReservationInstalledEvent):
                expected_state = (
                    "steer_reserved"
                    if event.reservation.reservation_kind == "steer"
                    else "follow_up_reserved"
                )
                if transition.resulting_delivery_state != expected_state:
                    raise ValueError("queue reservation state/kind mismatch")
                reservation = event.reservation
                resolved = event.reservation.reservation_kind
            elif isinstance(event, PromptQueueReservationReleasedEvent):
                if transition.resulting_delivery_state != "accepted_pending":
                    raise ValueError("queue release must return to pending")
                reservation = None
                resolved = "pending"
                disposition = event.release_reason
            elif isinstance(event, PromptQueueDeliveryRejectedEvent):
                if transition.resulting_delivery_state != "delivery_rejected":
                    raise ValueError("queue rejection state mismatch")
                reservation = None
                disposition = event.rejection_reason
            elif isinstance(event, PromptQueueCancelledEvent):
                if transition.resulting_delivery_state != "cancelled":
                    raise ValueError("queue cancellation state mismatch")
                reservation = None
                disposition = event.cancellation_reason
            elif isinstance(event, PromptQueueReconciliationRequiredEvent):
                if transition.resulting_delivery_state != "reconciliation_required":
                    raise ValueError("queue reconciliation state mismatch")
                disposition = event.stable_reason_code
            elif isinstance(event, PromptQueueContentRetiredEvent):
                if transition.resulting_content_retention_state != "retired":
                    raise ValueError("queue content retirement state mismatch")
                prepared = None
                disposition = event.retirement_reason
            elif isinstance(event, PromptQueueCommittedToRunEvent):
                if (
                    existing.delivery_state != "follow_up_reserved"
                    or transition.resulting_delivery_state != "committed_to_new_run"
                ):
                    raise ValueError("queue follow-up commit state mismatch")
                reservation = None
                disposition = "committed_to_run"
            elif isinstance(event, PromptQueueCommittedToProviderInputEvent):
                if (
                    existing.delivery_state != "steer_reserved"
                    or transition.resulting_delivery_state != "committed_to_active_run"
                ):
                    raise ValueError("queue steer commit state mismatch")
                reservation = None
                disposition = "committed_to_provider_input"
        if transition.expected_account_revision != self.account_revision:
            raise ValueError("queue account revision drifted")
        assert event.sequence is not None
        candidate_fingerprint = freeze_event_write_candidate(
            event.model_copy(update={"sequence": None})
        ).payload_fingerprint
        fingerprint_payload = {
            "queue_item_id": transition.queue_item_id,
            "accepted_ordinal": transition.accepted_ordinal,
            "delivery_state": transition.resulting_delivery_state,
            "content_retention_state": transition.resulting_content_retention_state,
            "item_revision": transition.resulting_item_revision,
            "account_revision": transition.resulting_account_revision,
            "head_event_id": event.id,
            "head_event_type": str(event.type),
            "head_event_sequence": event.sequence,
            "head_candidate_payload_fingerprint": candidate_fingerprint,
            "prepared_content_fact_fingerprint": (
                prepared.content_fact_fingerprint if prepared is not None else None
            ),
            "requested_delivery_mode": requested,
            "resolved_delivery_mode": resolved,
            "reservation_fingerprint": (
                reservation.reservation_fingerprint if reservation is not None else None
            ),
            "disposition_code": disposition,
        }
        self._items[transition.queue_item_id] = PromptQueueProjectedItem(
            queue_item_id=transition.queue_item_id,
            accepted_ordinal=transition.accepted_ordinal,
            delivery_state=transition.resulting_delivery_state,
            content_retention_state=(transition.resulting_content_retention_state),
            item_revision=transition.resulting_item_revision,
            account_revision=transition.resulting_account_revision,
            head_event_id=event.id,
            head_event_type=str(event.type),
            head_event_sequence=event.sequence,
            head_candidate_payload_fingerprint=candidate_fingerprint,
            prepared_content=prepared,
            requested_delivery_mode=requested,
            resolved_delivery_mode=resolved,
            reservation=reservation,
            disposition_code=disposition,
            row_fingerprint=context_fingerprint(
                "prompt-queue-item-row:v1", fingerprint_payload
            ),
        )
        self.account_revision = transition.resulting_account_revision
        self.transition_count += 1
        self.transition_accumulator = context_fingerprint(
            "prompt-queue-transition-step:v1",
            {
                "previous": self.transition_accumulator,
                "event_id": event.id,
                "sequence": event.sequence,
                "candidate_payload_fingerprint": candidate_fingerprint,
                "transition_fact_fingerprint": transition.transition_fact_fingerprint,
            },
        )
        self.head_event_id = event.id
        self.head_event_type = str(event.type)
        self.head_event_sequence = event.sequence
        self.head_candidate_payload_fingerprint = candidate_fingerprint
        self._bounded_tail_entries.append(
            (event.id, event.sequence, candidate_fingerprint)
        )


@dataclass(slots=True)
class PromptQueueTransactionCompanion:
    runtime_session_id: str
    prepared_candidate_batch_identity: EventLogPreparedCandidateBatchIdentity
    resulting_item: PromptQueueProjectedItem
    previous_item: PromptQueueProjectedItem | None
    transition_event_id: str
    expected_account_revision: int
    previous_transition_count: int
    previous_transition_accumulator: str
    resulting_next_accepted_ordinal: int
    resulting_items: tuple[PromptQueueProjectedItem, ...]
    charge: PromptQueueCompanionChargeFact
    projection_store: PromptQueueProjectionStore = field(repr=False)
    artifact_storage: PromptQueueArtifactStoragePort = field(repr=False)
    _stored_rebind_receipt: EventLogStoredCandidateBatchRebindReceipt | None = field(
        default=None, init=False, repr=False
    )
    _binding_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def charged_payload_bytes(self) -> int:
        return self.charge.conservative_charged_payload_bytes

    @property
    def charge_contract_fingerprint(self) -> str:
        return self.charge.charge_contract_fingerprint

    @property
    def storage_mutation_plan_fingerprint(self) -> str:
        return self.charge.storage_mutation_plan_fingerprint

    def accept_stored_candidate_rebind_receipt(
        self, receipt: EventLogStoredCandidateBatchRebindReceipt
    ) -> None:
        if (
            receipt.exact_ordered_batch_fingerprint
            != self.prepared_candidate_batch_identity.exact_ordered_batch_fingerprint
        ):
            raise ValueError("queue companion received a foreign stored batch")
        self._stored_rebind_receipt = receipt

    def bind_candidate_batch(
        self,
        candidates: Sequence[FrozenEventWriteCandidate],
    ) -> "PromptQueueTransactionCompanion":
        """Bind accounting events after the one-shot operation is fully frozen."""

        full = build_prepared_candidate_batch_identity(candidates)
        with self._binding_lock:
            current = self.prepared_candidate_batch_identity
            if current == full:
                return self
            current_ids = current.ordered_candidate_event_ids
            full_ids = full.ordered_candidate_event_ids
            positions = tuple(
                full_ids.index(event_id) if event_id in full_ids else -1
                for event_id in current_ids
            )
            if (
                any(index < 0 for index in positions)
                or positions != tuple(sorted(positions))
                or tuple(full.ordered_candidates[index] for index in positions)
                != current.ordered_candidates
                or self.transition_event_id not in current_ids
            ):
                raise ValueError(
                    "queue companion accounting batch changed its business prefix"
                )
            values = self.charge.model_dump(mode="python")
            values.pop("charge_fingerprint")
            values["exact_ordered_event_batch_fingerprint"] = (
                full.exact_ordered_batch_fingerprint
            )
            self.charge = build_frozen_fact(
                PromptQueueCompanionChargeFact,
                **values,
            )
            self.prepared_candidate_batch_identity = full
            return self

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        self._require_batch(stored_events)
        if self.projection_store.account_revision != self.expected_account_revision:
            raise ValueError("prompt queue in-memory account CAS failed")
        current = self.projection_store.item(self.resulting_item.queue_item_id)
        expected_item_revision = self.resulting_item.item_revision - 1
        if (0 if current is None else current.item_revision) != expected_item_revision:
            raise ValueError("prompt queue in-memory item CAS failed")
        if self.charge.companion_kind == "ACCEPT":
            content = self.resulting_item.prepared_content
            if content is None:
                raise ValueError("queue acceptance lost its prepared content")
            self.artifact_storage.apply_accept_in_memory(
                runtime_session_id=self.runtime_session_id,
                queue_item_id=self.resulting_item.queue_item_id,
                content=content,
            )
        elif self.charge.companion_kind == "CONTENT_RETIRE":
            previous = self.previous_item
            if previous is None or previous.prepared_content is None:
                raise ValueError("queue retirement lost its previous content")
            self.artifact_storage.apply_retire_in_memory(
                runtime_session_id=self.runtime_session_id,
                queue_item_id=self.resulting_item.queue_item_id,
                content=previous.prepared_content,
            )

    def apply_postgres(self, cursor: Any, stored_events: Sequence[AgentEvent]) -> None:
        self._require_batch(stored_events)
        stored_event = next(
            item for item in stored_events if item.id == self.transition_event_id
        )
        assert stored_event.sequence is not None
        item = _with_stored_sequence(self.resulting_item, stored_event.sequence)
        actual_items = tuple(
            item if value.queue_item_id == item.queue_item_id else value
            for value in self.resulting_items
        )
        transition_accumulator = context_fingerprint(
            "prompt-queue-transition-step:v1",
            {
                "previous": self.previous_transition_accumulator,
                "event_id": stored_event.id,
                "sequence": stored_event.sequence,
                "candidate_payload_fingerprint": (
                    item.head_candidate_payload_fingerprint
                ),
                "transition_fact_fingerprint": (
                    stored_event.transition.transition_fact_fingerprint
                ),
            },
        )
        pending_count = sum(
            value.delivery_state
            in {"accepted_pending", "steer_reserved", "follow_up_reserved"}
            for value in actual_items
        )
        reserved_count = sum(
            value.delivery_state in {"steer_reserved", "follow_up_reserved"}
            for value in actual_items
        )
        row_set_accumulator = _row_set_accumulator(actual_items)
        pending_head_set_accumulator = _pending_head_set_accumulator(actual_items)
        active_client_item_count = _active_client_item_count(actual_items)
        active_client_item_accumulator = _active_client_item_accumulator(actual_items)
        account_row = cursor.execute(
            """
            SELECT checkpoint_generation, checkpoint_through_sequence,
                   checkpoint_fingerprint, bounded_tail_first_sequence,
                   bounded_tail_count, bounded_tail_payload_bytes,
                   bounded_tail_accumulator, artifact_bytes,
                   transition_count, transition_accumulator,
                   reducer_contract_fingerprint, event_registry_fingerprint
            FROM prompt_queue_accounts
            WHERE session_id = %s AND account_revision = %s
            FOR UPDATE
            """,
            (self.runtime_session_id, self.expected_account_revision),
        ).fetchone()
        if account_row is None:
            raise ValueError("prompt queue account genesis/CAS authority is missing")
        values = (
            tuple(account_row.values())
            if isinstance(account_row, dict)
            else tuple(account_row)
        )
        (
            checkpoint_generation,
            checkpoint_through_sequence,
            checkpoint_fingerprint,
            previous_tail_first,
            previous_tail_count,
            previous_tail_bytes,
            previous_tail_accumulator,
            artifact_bytes,
            observed_transition_count,
            observed_transition_accumulator,
            reducer_contract_fingerprint,
            event_registry_fingerprint,
        ) = values
        if (
            int(observed_transition_count) != self.previous_transition_count
            or str(observed_transition_accumulator)
            != self.previous_transition_accumulator
            or str(reducer_contract_fingerprint)
            != PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT
            or str(event_registry_fingerprint)
            != PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT
        ):
            raise ValueError("prompt queue account transition authority drifted")
        resulting_tail_count = int(previous_tail_count) + 1
        resulting_tail_bytes = int(previous_tail_bytes) + self.charged_payload_bytes
        if resulting_tail_count > 256 or resulting_tail_bytes > 8 * 1024 * 1024:
            raise ValueError("prompt queue bounded tail requires checkpoint admission")
        resulting_tail_first = (
            item.head_event_sequence
            if int(previous_tail_count) == 0
            else int(previous_tail_first)
        )
        resulting_tail_accumulator = context_fingerprint(
            "prompt-queue-bounded-tail-step:v1",
            {
                "previous": str(previous_tail_accumulator),
                "event_id": item.head_event_id,
                "sequence": item.head_event_sequence,
                "candidate_payload_fingerprint": (
                    item.head_candidate_payload_fingerprint
                ),
            },
        )
        artifact_bytes_delta = _artifact_bytes_delta(
            companion_kind=self.charge.companion_kind,
            resulting_item=item,
            previous_item=self.previous_item,
        )
        resulting_artifact_bytes = int(artifact_bytes) + artifact_bytes_delta
        if resulting_artifact_bytes < 0:
            raise ValueError("prompt queue artifact byte accounting underflow")
        resulting_account = build_prompt_queue_account_projection(
            runtime_session_id=self.runtime_session_id,
            next_accepted_ordinal=self.resulting_next_accepted_ordinal,
            queue_chain_head_event_id=item.head_event_id,
            queue_chain_head_sequence=item.head_event_sequence,
            queue_chain_head_payload_fingerprint=(
                item.head_candidate_payload_fingerprint
            ),
            account_revision=item.account_revision,
            checkpoint_generation=int(checkpoint_generation),
            checkpoint_through_sequence=int(checkpoint_through_sequence),
            checkpoint_fingerprint=str(checkpoint_fingerprint),
            transition_count=self.previous_transition_count + 1,
            transition_accumulator=transition_accumulator,
            bounded_tail_first_sequence=resulting_tail_first,
            bounded_tail_count=resulting_tail_count,
            bounded_tail_payload_bytes=resulting_tail_bytes,
            bounded_tail_accumulator=resulting_tail_accumulator,
            pending_item_count=pending_count,
            reserved_item_count=reserved_count,
            artifact_bytes=resulting_artifact_bytes,
            pending_item_head_set_accumulator=pending_head_set_accumulator,
            active_client_item_count=active_client_item_count,
            active_client_item_accumulator=active_client_item_accumulator,
            row_set_accumulator=row_set_accumulator,
            reducer_contract_fingerprint=PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT,
            event_registry_fingerprint=PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
        )
        cursor.execute(
            """
            UPDATE prompt_queue_accounts
            SET next_accepted_ordinal = %s,
                queue_chain_head_event_id = %s,
                queue_chain_head_sequence = %s,
                queue_chain_head_payload_fingerprint = %s,
                account_revision = %s,
                transition_count = %s,
                transition_accumulator = %s,
                bounded_tail_first_sequence = %s,
                bounded_tail_count = %s,
                bounded_tail_payload_bytes = %s,
                bounded_tail_accumulator = %s,
                pending_item_count = %s,
                reserved_item_count = %s,
                artifact_bytes = %s,
                pending_item_head_set_accumulator = %s,
                active_client_item_count = %s,
                active_client_item_accumulator = %s,
                row_set_accumulator = %s,
                account_fingerprint = %s,
                updated_at = now()
            WHERE session_id = %s AND account_revision = %s
            """,
            (
                self.resulting_next_accepted_ordinal,
                item.head_event_id,
                item.head_event_sequence,
                item.head_candidate_payload_fingerprint,
                item.account_revision,
                self.previous_transition_count + 1,
                transition_accumulator,
                resulting_tail_first,
                resulting_tail_count,
                resulting_tail_bytes,
                resulting_tail_accumulator,
                pending_count,
                reserved_count,
                resulting_artifact_bytes,
                pending_head_set_accumulator,
                active_client_item_count,
                active_client_item_accumulator,
                row_set_accumulator,
                resulting_account.account_fingerprint,
                self.runtime_session_id,
                self.expected_account_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("prompt queue account CAS did not affect exactly one row")
        cursor.execute(
            """
            insert into prompt_queue_items (
                session_id, queue_item_id, accepted_ordinal, delivery_state,
                content_retention_state, row_revision, head_transition_event_id,
                head_transition_event_type, head_transition_sequence,
                head_candidate_payload_fingerprint,
                requested_delivery_mode, resolved_delivery_mode,
                state_payload, reducer_contract_fingerprint,
                event_registry_fingerprint, row_fingerprint, updated_at
            ) values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, now()
            )
            on conflict (session_id, queue_item_id) do update set
                delivery_state = excluded.delivery_state,
                content_retention_state = excluded.content_retention_state,
                row_revision = excluded.row_revision,
                head_transition_event_id = excluded.head_transition_event_id,
                head_transition_event_type = excluded.head_transition_event_type,
                head_transition_sequence = excluded.head_transition_sequence,
                head_candidate_payload_fingerprint = excluded.head_candidate_payload_fingerprint,
                requested_delivery_mode = excluded.requested_delivery_mode,
                resolved_delivery_mode = excluded.resolved_delivery_mode,
                state_payload = excluded.state_payload,
                reducer_contract_fingerprint = excluded.reducer_contract_fingerprint,
                event_registry_fingerprint = excluded.event_registry_fingerprint,
                row_fingerprint = excluded.row_fingerprint,
                updated_at = now()
            where prompt_queue_items.row_revision = excluded.row_revision - 1
            """,
            (
                self.runtime_session_id,
                item.queue_item_id,
                item.accepted_ordinal,
                item.delivery_state,
                item.content_retention_state,
                item.item_revision,
                item.head_event_id,
                item.head_event_type,
                stored_event.sequence,
                item.head_candidate_payload_fingerprint,
                item.requested_delivery_mode,
                item.resolved_delivery_mode,
                Jsonb(_projected_item_payload(item)),
                PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT,
                PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
                item.row_fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("prompt queue item CAS did not affect exactly one row")
        if self.charge.companion_kind == "ACCEPT":
            content = item.prepared_content
            if content is None:
                raise ValueError("queue acceptance lost its prepared content")
            self.artifact_storage.apply_accept_postgres(
                cursor,
                runtime_session_id=self.runtime_session_id,
                queue_item_id=item.queue_item_id,
                content=content,
            )
        elif self.charge.companion_kind == "CONTENT_RETIRE":
            previous = self.previous_item
            if previous is None or previous.prepared_content is None:
                raise ValueError("queue retirement lost its previous content")
            self.artifact_storage.apply_retire_postgres(
                cursor,
                runtime_session_id=self.runtime_session_id,
                queue_item_id=item.queue_item_id,
                content=previous.prepared_content,
            )

    def _require_batch(self, stored_events: Sequence[AgentEvent]) -> None:
        receipt = self._stored_rebind_receipt
        if receipt is None:
            raise RuntimeError("queue companion lacks stored batch rebind proof")
        if tuple(item.id for item in stored_events) != receipt.ordered_event_ids:
            raise ValueError("queue companion stored event order drifted")
        if self.transition_event_id not in receipt.ordered_event_ids:
            raise ValueError("queue companion transition event is absent")


@dataclass(slots=True)
class PromptQueueModelStartTransactionCompanion:
    """Purpose-neutral lifecycle adapter around the queue CAS companion."""

    queue_companion: PromptQueueTransactionCompanion
    identity: ModelLifecycleTransactionCompanionIdentityFact

    @classmethod
    def build(
        cls,
        *,
        queue_companion: PromptQueueTransactionCompanion,
        resolved_model_call_id: str,
        model_call_start_event_id: str,
    ) -> "PromptQueueModelStartTransactionCompanion":
        reservation = queue_companion.resulting_item.reservation
        payload = {
            "companion_kind": "prompt_queue_steer",
            "phase": "start",
            "purpose": ModelCallPurpose.AGENT_MODEL_LOOP,
            "resolved_model_call_id": resolved_model_call_id,
            "stable_primary_event_id": model_call_start_event_id,
            "external_owner_reference_fingerprint": (
                reservation.reservation_fingerprint
                if reservation is not None
                else queue_companion.resulting_item.row_fingerprint
            ),
            "stable_candidate_fingerprint": (
                queue_companion.prepared_candidate_batch_identity.exact_ordered_batch_fingerprint
            ),
        }
        return cls(
            queue_companion=queue_companion,
            identity=ModelLifecycleTransactionCompanionIdentityFact(
                **payload,
                companion_fingerprint=context_fingerprint(
                    "model-lifecycle-transaction-companion:v1", payload
                ),
            ),
        )

    @property
    def charged_payload_bytes(self) -> int:
        return self.queue_companion.charged_payload_bytes

    @property
    def charge_contract_fingerprint(self) -> str:
        return self.queue_companion.charge_contract_fingerprint

    @property
    def storage_mutation_plan_fingerprint(self) -> str:
        return self.queue_companion.storage_mutation_plan_fingerprint

    @property
    def prepared_candidate_batch_identity(
        self,
    ) -> EventLogPreparedCandidateBatchIdentity:
        return self.queue_companion.prepared_candidate_batch_identity

    def bind_candidate_batch(
        self,
        candidates: Sequence[FrozenEventWriteCandidate],
    ) -> "PromptQueueModelStartTransactionCompanion":
        self.queue_companion.bind_candidate_batch(candidates)
        return self

    def accept_stored_candidate_rebind_receipt(
        self, receipt: EventLogStoredCandidateBatchRebindReceipt
    ) -> None:
        self.queue_companion.accept_stored_candidate_rebind_receipt(receipt)

    def apply_in_memory(self, stored_events: Sequence[AgentEvent]) -> None:
        self.queue_companion.apply_in_memory(stored_events)

    def apply_postgres(self, cursor: Any, stored_events: Sequence[AgentEvent]) -> None:
        self.queue_companion.apply_postgres(cursor, stored_events)


@dataclass(frozen=True, slots=True)
class PromptQueueSubmitRequest:
    command_id: str
    client_instance_id: str
    client_submission_id: str
    text: str
    requested_delivery_mode: PromptQueueDeliveryMode
    event_context: EventContext


@dataclass(frozen=True, slots=True)
class PreparedPromptQueueDispatchBatch:
    """One immutable queue disposition joined to its durable authority batch."""

    queue_item_id: str
    reservation_fingerprint: str
    prepared_events: tuple[AgentEvent, ...]
    transition_event_id: str
    transaction_companion: PromptQueueTransactionCompanion

    def __post_init__(self) -> None:
        if not self.prepared_events:
            raise ValueError("queue dispatch batch cannot be empty")
        if self.transition_event_id not in {event.id for event in self.prepared_events}:
            raise ValueError("queue dispatch transition is absent from its batch")


@dataclass(slots=True)
class TerminalPromptQueueMutationService:
    runtime_session: Any
    store: PromptQueueProjectionStore
    artifact_storage: PromptQueueArtifactStoragePort
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _submit_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    async def submit(
        self, request: PromptQueueSubmitRequest
    ) -> PromptQueueProjectedItem:
        async with self._submit_lock:
            owner_identity = (
                f"{request.client_instance_id}:{request.client_submission_id}"
            )
            if len(request.text.encode("utf-8")) <= PROMPT_QUEUE_INLINE_MAX_UTF8_BYTES:
                content: PreparedPromptQueueContentFact = (
                    prepare_inline_prompt_queue_content(
                        text=request.text,
                        inline_admission_identity=owner_identity,
                    )
                )
            else:
                semantic = prompt_queue_content_semantic_fingerprint(request.text)
                candidate_item_id = _queue_item_id(
                    runtime_session_id=self.runtime_session.runtime_session_id,
                    client_instance_id=request.client_instance_id,
                    client_submission_id=request.client_submission_id,
                    content_semantic_fingerprint=semantic,
                )
                existing = self.store.item(candidate_item_id)
                if existing is not None:
                    if (
                        existing.prepared_content is not None
                        and existing.prepared_content.content_semantic_fingerprint
                        == semantic
                    ):
                        return existing
                    raise ValueError("stable queue item identity conflicts")
                preparation_deadline = (
                    self.runtime_session.event_write_service.new_deadline_monotonic()
                )
                content = await self.runtime_session.context_input_io_service.execute(
                    operation_name="prompt-queue-artifact-prepare",
                    operation=lambda: self.artifact_storage.prepare(
                        runtime_session_id=self.runtime_session.runtime_session_id,
                        owner_client_submission_identity=owner_identity,
                        text=request.text,
                        deadline_monotonic=preparation_deadline,
                    ),
                    deadline_monotonic=preparation_deadline,
                )
            queue_item_id = _queue_item_id(
                runtime_session_id=self.runtime_session.runtime_session_id,
                client_instance_id=request.client_instance_id,
                client_submission_id=request.client_submission_id,
                content_semantic_fingerprint=content.content_semantic_fingerprint,
            )
            write_deadline = (
                self.runtime_session.event_write_service.new_deadline_monotonic()
            )
            with self._lock:
                existing = self.store.item(queue_item_id)
                if existing is not None:
                    if (
                        existing.prepared_content is not None
                        and existing.prepared_content.content_semantic_fingerprint
                        == content.content_semantic_fingerprint
                    ):
                        return existing
                    raise ValueError("stable queue item identity conflicts")
                if len(self.store.active_client_items()) >= 64:
                    raise RuntimeError("active prompt queue capacity is exhausted")
                transition = _transition_head(
                    runtime_session_id=self.runtime_session.runtime_session_id,
                    queue_item_id=queue_item_id,
                    accepted_ordinal=self.store.next_accepted_ordinal,
                    transition_ordinal=0,
                    predecessor=None,
                    predecessor_payload_fingerprint=None,
                    previous_delivery_state=None,
                    resulting_delivery_state="accepted_pending",
                    previous_content_retention_state="active",
                    resulting_content_retention_state="active",
                    expected_item_revision=0,
                    expected_account_revision=self.store.account_revision,
                    semantic_payload={
                        "content_semantic_fingerprint": (
                            content.content_semantic_fingerprint
                        ),
                        "requested_delivery_mode": request.requested_delivery_mode,
                        "resolved_delivery_mode": "pending",
                    },
                    attribution_payload={
                        "client_instance_id": request.client_instance_id,
                        "client_submission_id": request.client_submission_id,
                        "content_attribution_fingerprint": (
                            content.content_attribution_fingerprint
                        ),
                    },
                )
                event_id = _transition_event_id(
                    queue_item_id=queue_item_id,
                    command_id=request.command_id,
                    transition_kind="accepted",
                    predecessor_event_id=None,
                    expected_item_revision=0,
                    reservation_generation=0,
                )
                event = PromptQueueAcceptedEvent(
                    id=event_id,
                    **request.event_context.event_fields(),
                    command_id=request.command_id,
                    client_instance_id=request.client_instance_id,
                    client_submission_id=request.client_submission_id,
                    requested_delivery_mode=request.requested_delivery_mode,
                    resolved_delivery_mode="pending",
                    prepared_content=content,
                    transition=transition,
                )
                prepared_event = self.runtime_session.prepare_event_for_write(event)
                predicted = _predicted_item(prepared_event)
                companion = PromptQueueTransactionCompanion(
                    **_companion_fields(
                        runtime_session_id=self.runtime_session.runtime_session_id,
                        store=self.store,
                        prepared_event=prepared_event,
                        resulting_item=predicted,
                        artifact_storage=self.artifact_storage,
                    )
                )
            await self.runtime_session.write_events_with_deadline(
                (event,),
                deadline_monotonic=write_deadline,
                transaction_companion=companion,
            )
            item = self.store.item(queue_item_id)
            if item is None:
                raise RuntimeError("committed queue acceptance did not reach reducer")
            return item

    async def materialize_content_text(
        self,
        queue_item_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> str:
        item = self.store.item(queue_item_id)
        if item is None or item.prepared_content is None:
            raise ValueError("prompt queue content is unavailable")
        content = item.prepared_content
        if isinstance(content, InlineQueueContentFact):
            return content.canonical_utf8_text
        deadline = deadline_monotonic or (
            self.runtime_session.event_write_service.new_deadline_monotonic()
        )
        text = await self.runtime_session.context_input_io_service.execute(
            operation_name="prompt-queue-artifact-hydrate",
            operation=lambda: self.runtime_session.archive.get_text(
                content.stable_content_addressed_artifact_id,
                session_id=self.runtime_session.runtime_session_id,
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
        )
        encoded = text.encode("utf-8")
        if (
            len(encoded) != content.canonical_byte_count
            or f"sha256:{sha256(encoded).hexdigest()}"
            != content.canonical_payload_sha256
            or self.store.item(queue_item_id) != item
        ):
            raise ValueError("prompt queue artifact hydration identity mismatch")
        return text

    async def reserve(
        self,
        *,
        queue_item_id: str,
        reservation_kind: str,
        target_run_id: str | None,
        target_safe_point: str,
        command_id: str,
        event_context: EventContext,
        lifetime_seconds: int = 24 * 60 * 60,
    ) -> PromptQueueProjectedItem:
        if reservation_kind not in {"steer", "follow_up"}:
            raise ValueError("queue reservation kind is invalid")
        if lifetime_seconds < 1 or lifetime_seconds > 24 * 60 * 60:
            raise ValueError("queue reservation lifetime is out of bounds")
        with self._lock:
            item = self._require_pending_item(queue_item_id)
            reservation_generation = item.item_revision + 1
            reservation_id = context_fingerprint(
                "prompt-queue-reservation-id:v1",
                {
                    "queue_item_id": item.queue_item_id,
                    "head_event_id": item.head_event_id,
                    "reservation_kind": reservation_kind,
                    "reservation_generation": reservation_generation,
                    "target_run_id": target_run_id,
                    "target_safe_point": target_safe_point,
                },
            ).replace("sha256:", "prompt-queue-reservation:")
            reservation = build_frozen_fact(
                PromptQueueReservationFact,
                schema_version="prompt_queue_reservation.v1",
                reservation_kind=reservation_kind,
                reservation_id=reservation_id,
                reservation_generation=reservation_generation,
                ordered_item_set_fingerprint=context_fingerprint(
                    "prompt-queue-reservation-ordered-item-set:v1",
                    (
                        (
                            item.queue_item_id,
                            item.head_event_id,
                            item.head_candidate_payload_fingerprint,
                        ),
                    ),
                ),
                target_run_id=target_run_id,
                target_safe_point=target_safe_point,
                absolute_deadline_utc=(
                    datetime.now(UTC) + timedelta(seconds=lifetime_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z"),
            )
            predecessor = _item_predecessor(
                runtime_session_id=self.runtime_session.runtime_session_id,
                item=item,
            )
            transition = _transition_head(
                runtime_session_id=self.runtime_session.runtime_session_id,
                queue_item_id=queue_item_id,
                accepted_ordinal=item.accepted_ordinal,
                transition_ordinal=item.item_revision,
                predecessor=predecessor,
                predecessor_payload_fingerprint=(
                    item.head_candidate_payload_fingerprint
                ),
                previous_delivery_state=item.delivery_state,
                resulting_delivery_state=(
                    "steer_reserved"
                    if reservation_kind == "steer"
                    else "follow_up_reserved"
                ),
                previous_content_retention_state=item.content_retention_state,
                resulting_content_retention_state=item.content_retention_state,
                expected_item_revision=item.item_revision,
                expected_account_revision=item.account_revision,
                semantic_payload={
                    "reservation_kind": reservation_kind,
                    "ordered_item_set_fingerprint": (
                        reservation.ordered_item_set_fingerprint
                    ),
                    "target_safe_point": target_safe_point,
                },
                attribution_payload={
                    "reservation_id": reservation.reservation_id,
                    "reservation_generation": reservation.reservation_generation,
                    "target_run_id": target_run_id,
                    "absolute_deadline_utc": reservation.absolute_deadline_utc,
                },
            )
            event = PromptQueueReservationInstalledEvent(
                id=_transition_event_id(
                    queue_item_id=queue_item_id,
                    command_id=command_id,
                    transition_kind="reservation_installed",
                    predecessor_event_id=item.head_event_id,
                    expected_item_revision=item.item_revision,
                    reservation_generation=reservation_generation,
                ),
                **event_context.event_fields(),
                command_id=command_id,
                reservation=reservation,
                transition=transition,
            )
            prepared_event, companion = self._prepare_single_transition(
                event=event,
                previous=item,
            )
        await self.runtime_session.write_events(
            (prepared_event,), transaction_companion=companion
        )
        result = self.store.item(queue_item_id)
        if result is None or result.reservation != reservation:
            raise RuntimeError("queue reservation did not reach its reducer")
        return result

    def prepare_commit_to_run(
        self,
        *,
        queue_item_id: str,
        reservation_fingerprint: str,
        command_id: str,
        event_context: EventContext,
        run_start_event: AgentEvent,
        candidate_prefix: Sequence[AgentEvent],
    ) -> PreparedPromptQueueDispatchBatch:
        """Freeze the queue disposition and RunStart in one physical batch."""

        with self._lock:
            item = self._require_reserved_item(
                queue_item_id,
                reservation_kind="follow_up",
                reservation_fingerprint=reservation_fingerprint,
            )
            prepared_prefix = tuple(
                self.runtime_session.prepare_event_for_write(event)
                for event in candidate_prefix
            )
            matching_starts = tuple(
                event for event in prepared_prefix if event.id == run_start_event.id
            )
            if len(matching_starts) != 1:
                raise ValueError("queue follow-up batch lacks its unique RunStart")
            run_start = matching_starts[0]
            if str(run_start.type) != "RUN_START":
                raise ValueError("queue follow-up target is not a RunStart")
            event = self._build_terminal_transition(
                item=item,
                command_id=command_id,
                event_context=event_context,
                resulting_delivery_state="committed_to_new_run",
                transition_kind="committed_to_run",
                semantic_payload={
                    "run_start_event_identity": stable_event_identity(
                        run_start,
                        runtime_session_id=self.runtime_session.runtime_session_id,
                    ).identity_fingerprint,
                },
                event_factory=lambda transition: PromptQueueCommittedToRunEvent(
                    id=_transition_event_id(
                        queue_item_id=queue_item_id,
                        command_id=command_id,
                        transition_kind="committed_to_run",
                        predecessor_event_id=item.head_event_id,
                        expected_item_revision=item.item_revision,
                        reservation_generation=(
                            item.reservation.reservation_generation
                            if item.reservation is not None
                            else 0
                        ),
                    ),
                    **event_context.event_fields(),
                    command_id=command_id,
                    source_reservation_fingerprint=reservation_fingerprint,
                    committed_run_start_event_identity=stable_event_identity(
                        run_start,
                        runtime_session_id=self.runtime_session.runtime_session_id,
                    ),
                    transition=transition,
                ),
            )
            prepared_event = self.runtime_session.prepare_event_for_write(event)
            prepared_events = (*prepared_prefix, prepared_event)
            predicted = _predicted_item(prepared_event, previous=item)
            companion = PromptQueueTransactionCompanion(
                **_companion_fields(
                    runtime_session_id=self.runtime_session.runtime_session_id,
                    store=self.store,
                    prepared_event=prepared_event,
                    resulting_item=predicted,
                    artifact_storage=self.artifact_storage,
                    prepared_batch=prepared_events,
                )
            )
            return PreparedPromptQueueDispatchBatch(
                queue_item_id=queue_item_id,
                reservation_fingerprint=reservation_fingerprint,
                prepared_events=prepared_events,
                transition_event_id=prepared_event.id,
                transaction_companion=companion,
            )

    def prepare_user_steer_event(
        self,
        *,
        queue_item_id: str,
        reservation_fingerprint: str,
        command_id: str,
        event_context: EventContext,
        provider_input_append_event: AgentEvent,
        canonical_text: str,
    ) -> UserSteerCommittedEvent:
        """Freeze the canonical user-intent fact for one same-batch steer."""

        with self._lock:
            item = self._require_reserved_item(
                queue_item_id,
                reservation_kind="steer",
                reservation_fingerprint=reservation_fingerprint,
            )
            content = item.prepared_content
            if content is None:
                raise ValueError("active-run steer content is unavailable")
            encoded = canonical_text.encode("utf-8")
            if (
                len(encoded) != content.canonical_byte_count
                or f"sha256:{sha256(encoded).hexdigest()}"
                != content.canonical_payload_sha256
            ):
                raise ValueError("active-run steer hydrated content identity drifted")
            prepared_append = self.runtime_session.prepare_event_for_write(
                provider_input_append_event
            )
            if str(prepared_append.type) != "PROVIDER_INPUT_APPEND_COMMITTED":
                raise ValueError("queue steer target is not a ProviderInput append")
            message_id = user_steer_message_id(
                queue_item_id=queue_item_id,
                reservation_fingerprint=reservation_fingerprint,
            )
            steer = build_frozen_fact(
                UserSteerSemanticFact,
                schema_version="user_steer_semantic.v1",
                message_id=message_id,
                canonical_utf8_text=canonical_text,
                canonical_payload_sha256=content.canonical_payload_sha256,
                canonical_byte_count=content.canonical_byte_count,
                observed_at_utc=datetime.now(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                source_queue_content_semantic_fingerprint=(
                    content.content_semantic_fingerprint
                ),
            )
            return UserSteerCommittedEvent(
                id=user_steer_event_id(
                    queue_item_id=queue_item_id,
                    reservation_fingerprint=reservation_fingerprint,
                ),
                **event_context.event_fields(),
                command_id=command_id,
                queue_item_id=queue_item_id,
                source_reservation_fingerprint=reservation_fingerprint,
                steer=steer,
                provider_input_append_event_identity=stable_event_identity(
                    prepared_append,
                    runtime_session_id=self.runtime_session.runtime_session_id,
                ),
            )

    def prepare_commit_to_provider_input(
        self,
        *,
        queue_item_id: str,
        reservation_fingerprint: str,
        command_id: str,
        event_context: EventContext,
        provider_input_append_event: AgentEvent,
        user_steer_event: UserSteerCommittedEvent,
        candidate_prefix: Sequence[AgentEvent],
    ) -> PreparedPromptQueueDispatchBatch:
        """Freeze queue CAS beside UserSteer, provider append, and ModelStart."""

        with self._lock:
            item = self._require_reserved_item(
                queue_item_id,
                reservation_kind="steer",
                reservation_fingerprint=reservation_fingerprint,
            )
            prepared_prefix = tuple(
                self.runtime_session.prepare_event_for_write(event)
                for event in candidate_prefix
            )
            appends = tuple(
                event
                for event in prepared_prefix
                if event.id == provider_input_append_event.id
            )
            steers = tuple(
                event for event in prepared_prefix if event.id == user_steer_event.id
            )
            if (
                len(appends) != 1
                or str(appends[0].type) != "PROVIDER_INPUT_APPEND_COMMITTED"
                or len(steers) != 1
                or not isinstance(steers[0], UserSteerCommittedEvent)
            ):
                raise ValueError(
                    "queue steer batch lacks its unique append/UserSteer facts"
                )
            append = appends[0]
            steer = steers[0]
            append_identity = stable_event_identity(
                append,
                runtime_session_id=self.runtime_session.runtime_session_id,
            )
            steer_identity = stable_event_identity(
                steer,
                runtime_session_id=self.runtime_session.runtime_session_id,
            )
            if (
                steer.queue_item_id != queue_item_id
                or steer.source_reservation_fingerprint != reservation_fingerprint
                or steer.provider_input_append_event_identity != append_identity
            ):
                raise ValueError("queue steer semantic/append authority drifted")
            event = self._build_terminal_transition(
                item=item,
                command_id=command_id,
                event_context=event_context,
                resulting_delivery_state="committed_to_active_run",
                transition_kind="committed_to_provider_input",
                semantic_payload={
                    "provider_input_append_event_identity": (
                        append_identity.identity_fingerprint
                    ),
                    "user_steer_event_identity": steer_identity.identity_fingerprint,
                },
                event_factory=lambda transition: (
                    PromptQueueCommittedToProviderInputEvent(
                        id=_transition_event_id(
                            queue_item_id=queue_item_id,
                            command_id=command_id,
                            transition_kind="committed_to_provider_input",
                            predecessor_event_id=item.head_event_id,
                            expected_item_revision=item.item_revision,
                            reservation_generation=(
                                item.reservation.reservation_generation
                                if item.reservation is not None
                                else 0
                            ),
                        ),
                        **event_context.event_fields(),
                        command_id=command_id,
                        source_reservation_fingerprint=reservation_fingerprint,
                        provider_input_append_event_identity=append_identity,
                        user_steer_event_identity=steer_identity,
                        transition=transition,
                    )
                ),
            )
            prepared_event = self.runtime_session.prepare_event_for_write(event)
            prepared_events = (*prepared_prefix, prepared_event)
            predicted = _predicted_item(prepared_event, previous=item)
            companion = PromptQueueTransactionCompanion(
                **_companion_fields(
                    runtime_session_id=self.runtime_session.runtime_session_id,
                    store=self.store,
                    prepared_event=prepared_event,
                    resulting_item=predicted,
                    artifact_storage=self.artifact_storage,
                    prepared_batch=prepared_events,
                )
            )
            return PreparedPromptQueueDispatchBatch(
                queue_item_id=queue_item_id,
                reservation_fingerprint=reservation_fingerprint,
                prepared_events=prepared_events,
                transition_event_id=prepared_event.id,
                transaction_companion=companion,
            )

    async def release_reservation(
        self,
        *,
        queue_item_id: str,
        reservation_fingerprint: str,
        command_id: str,
        event_context: EventContext,
        reason: str,
    ) -> PromptQueueProjectedItem:
        if reason not in {
            "preflight_retryable",
            "target_unavailable",
            "safe_point_missed_auto_requeue",
            "caller_cancelled_before_dispatch",
        }:
            raise ValueError("queue reservation release reason is invalid")
        with self._lock:
            item = self._require_reserved_item(
                queue_item_id,
                reservation_kind=None,
                reservation_fingerprint=reservation_fingerprint,
            )
            event = self._build_terminal_transition(
                item=item,
                command_id=command_id,
                event_context=event_context,
                resulting_delivery_state="accepted_pending",
                transition_kind="reservation_released",
                semantic_payload={"release_reason": reason},
                event_factory=lambda transition: PromptQueueReservationReleasedEvent(
                    id=_transition_event_id(
                        queue_item_id=queue_item_id,
                        command_id=command_id,
                        transition_kind="reservation_released",
                        predecessor_event_id=item.head_event_id,
                        expected_item_revision=item.item_revision,
                        reservation_generation=(
                            item.reservation.reservation_generation
                            if item.reservation is not None
                            else 0
                        ),
                    ),
                    **event_context.event_fields(),
                    command_id=command_id,
                    source_reservation_fingerprint=reservation_fingerprint,
                    release_reason=reason,
                    transition=transition,
                ),
            )
            prepared_event, companion = self._prepare_single_transition(
                event=event,
                previous=item,
            )
        await self.runtime_session.write_events(
            (prepared_event,), transaction_companion=companion
        )
        result = self.store.item(queue_item_id)
        if result is None:
            raise RuntimeError("queue reservation release did not reach reducer")
        return result

    async def reject_delivery(
        self,
        *,
        queue_item_id: str,
        command_id: str,
        event_context: EventContext,
        reason: str,
        reservation_fingerprint: str | None = None,
    ) -> PromptQueueProjectedItem:
        if reason not in {
            "explicit_steer_safe_point_missed",
            "invalid_target",
            "history_capacity_rejected",
            "content_unavailable",
            "session_closing",
        }:
            raise ValueError("queue delivery rejection reason is invalid")
        with self._lock:
            item = self.store.item(queue_item_id)
            if item is None:
                raise KeyError(queue_item_id)
            if item.delivery_state in {
                "committed_to_active_run",
                "committed_to_new_run",
                "cancelled",
                "delivery_rejected",
            }:
                return item
            if item.delivery_state == "reconciliation_required":
                raise RuntimeError("queue reconciliation item cannot be rejected")
            if item.reservation is not None:
                if reservation_fingerprint != item.reservation.reservation_fingerprint:
                    raise ValueError("queue rejection reservation identity drifted")
            elif reservation_fingerprint is not None:
                raise ValueError("unreserved queue rejection carries a reservation")
            event = self._build_terminal_transition(
                item=item,
                command_id=command_id,
                event_context=event_context,
                resulting_delivery_state="delivery_rejected",
                transition_kind="delivery_rejected",
                semantic_payload={"rejection_reason": reason},
                event_factory=lambda transition: PromptQueueDeliveryRejectedEvent(
                    id=_transition_event_id(
                        queue_item_id=queue_item_id,
                        command_id=command_id,
                        transition_kind="delivery_rejected",
                        predecessor_event_id=item.head_event_id,
                        expected_item_revision=item.item_revision,
                        reservation_generation=(
                            item.reservation.reservation_generation
                            if item.reservation is not None
                            else 0
                        ),
                    ),
                    **event_context.event_fields(),
                    command_id=command_id,
                    source_reservation_fingerprint=reservation_fingerprint,
                    rejection_reason=reason,
                    transition=transition,
                ),
            )
            prepared_event, companion = self._prepare_single_transition(
                event=event,
                previous=item,
            )
        await self.runtime_session.write_events(
            (prepared_event,), transaction_companion=companion
        )
        result = self.store.item(queue_item_id)
        if result is None:
            raise RuntimeError("queue delivery rejection did not reach reducer")
        return result

    async def cancel(
        self,
        *,
        queue_item_id: str,
        command_id: str,
        event_context: EventContext,
        reason: str = "client_cancel",
    ) -> PromptQueueProjectedItem:
        with self._lock:
            item = self.store.item(queue_item_id)
            if item is None:
                raise KeyError(queue_item_id)
            if item.delivery_state in {
                "cancelled",
                "delivery_rejected",
                "committed_to_active_run",
                "committed_to_new_run",
            }:
                return item
            if item.delivery_state != "accepted_pending":
                raise RuntimeError("only an unreserved queue item may be cancelled")
            predecessor = _item_predecessor(
                runtime_session_id=self.runtime_session.runtime_session_id,
                item=item,
            )
            transition = _transition_head(
                runtime_session_id=self.runtime_session.runtime_session_id,
                queue_item_id=queue_item_id,
                accepted_ordinal=item.accepted_ordinal,
                transition_ordinal=item.item_revision,
                predecessor=predecessor,
                predecessor_payload_fingerprint=item.head_candidate_payload_fingerprint,
                previous_delivery_state=item.delivery_state,
                resulting_delivery_state="cancelled",
                previous_content_retention_state=item.content_retention_state,
                resulting_content_retention_state=item.content_retention_state,
                expected_item_revision=item.item_revision,
                expected_account_revision=item.account_revision,
                semantic_payload={"cancellation_reason": reason},
                attribution_payload={"command_id": command_id},
            )
            event = PromptQueueCancelledEvent(
                id=_transition_event_id(
                    queue_item_id=queue_item_id,
                    command_id=command_id,
                    transition_kind="cancelled",
                    predecessor_event_id=item.head_event_id,
                    expected_item_revision=item.item_revision,
                    reservation_generation=0,
                ),
                **event_context.event_fields(),
                command_id=command_id,
                cancellation_reason=reason,
                transition=transition,
            )
            prepared_event = self.runtime_session.prepare_event_for_write(event)
            predicted = _predicted_item(prepared_event, previous=item)
            companion = PromptQueueTransactionCompanion(
                **_companion_fields(
                    runtime_session_id=self.runtime_session.runtime_session_id,
                    store=self.store,
                    prepared_event=prepared_event,
                    resulting_item=predicted,
                    artifact_storage=self.artifact_storage,
                )
            )
        await self.runtime_session.write_events(
            (event,), transaction_companion=companion
        )
        result = self.store.item(queue_item_id)
        if result is None:
            raise RuntimeError("queue cancellation did not reach reducer")
        return result

    async def retire_content(
        self,
        *,
        queue_item_id: str,
        command_id: str,
        event_context: EventContext,
        reason: str,
    ) -> PromptQueueProjectedItem:
        if reason not in {
            "terminal_delivery",
            "cancelled",
            "rejected",
            "operator_retirement",
        }:
            raise ValueError("queue content retirement reason is invalid")
        with self._lock:
            item = self.store.item(queue_item_id)
            if item is None:
                raise KeyError(queue_item_id)
            if item.content_retention_state == "retired":
                return item
            if item.delivery_state not in {
                "committed_to_active_run",
                "committed_to_new_run",
                "cancelled",
                "delivery_rejected",
            }:
                raise RuntimeError("queue content is not terminally retirable")
            content = item.prepared_content
            if content is None:
                raise RuntimeError("queue content retirement authority is missing")
            predecessor = _item_predecessor(
                runtime_session_id=self.runtime_session.runtime_session_id,
                item=item,
            )
            transition = _transition_head(
                runtime_session_id=self.runtime_session.runtime_session_id,
                queue_item_id=queue_item_id,
                accepted_ordinal=item.accepted_ordinal,
                transition_ordinal=item.item_revision,
                predecessor=predecessor,
                predecessor_payload_fingerprint=item.head_candidate_payload_fingerprint,
                previous_delivery_state=item.delivery_state,
                resulting_delivery_state=item.delivery_state,
                previous_content_retention_state="active",
                resulting_content_retention_state="retired",
                expected_item_revision=item.item_revision,
                expected_account_revision=item.account_revision,
                semantic_payload={
                    "content_semantic_fingerprint": (
                        content.content_semantic_fingerprint
                    ),
                    "retirement_reason": reason,
                },
                attribution_payload={
                    "command_id": command_id,
                    "content_fact_fingerprint": content.content_fact_fingerprint,
                },
            )
            event = PromptQueueContentRetiredEvent(
                id=_transition_event_id(
                    queue_item_id=queue_item_id,
                    command_id=command_id,
                    transition_kind="content_retired",
                    predecessor_event_id=item.head_event_id,
                    expected_item_revision=item.item_revision,
                    reservation_generation=0,
                ),
                **event_context.event_fields(),
                command_id=command_id,
                preparation_id=(
                    content.preparation_id
                    if isinstance(content, ConfirmedArtifactQueueContentFact)
                    else None
                ),
                artifact_identity_fingerprint=(
                    content.artifact_identity_fingerprint
                    if isinstance(content, ConfirmedArtifactQueueContentFact)
                    else None
                ),
                retention_policy_fingerprint=context_fingerprint(
                    "prompt-queue-content-retention-policy:v1",
                    {"retire_at": "session_close_or_explicit_maintenance"},
                ),
                retirement_reason=reason,
                transition=transition,
            )
            prepared_event, companion = self._prepare_single_transition(
                event=event,
                previous=item,
            )
        await self.runtime_session.write_events(
            (prepared_event,), transaction_companion=companion
        )
        result = self.store.item(queue_item_id)
        if result is None:
            raise RuntimeError("queue content retirement did not reach reducer")
        return result

    def source_event_context(self, queue_item_id: str) -> EventContext:
        item = self.store.item(queue_item_id)
        if item is None:
            raise KeyError(queue_item_id)
        source = self.runtime_session.event_log.get_by_id(item.head_event_id)
        if source is None:
            raise RuntimeError("queue item head event is unavailable")
        return EventContext(
            run_id=source.run_id,
            turn_id=source.turn_id,
            reply_id=source.reply_id,
        )

    def _prepare_single_transition(
        self,
        *,
        event: AgentEvent,
        previous: PromptQueueProjectedItem | None,
    ) -> tuple[AgentEvent, PromptQueueTransactionCompanion]:
        prepared_event = self.runtime_session.prepare_event_for_write(event)
        predicted = _predicted_item(prepared_event, previous=previous)
        companion = PromptQueueTransactionCompanion(
            **_companion_fields(
                runtime_session_id=self.runtime_session.runtime_session_id,
                store=self.store,
                prepared_event=prepared_event,
                resulting_item=predicted,
                artifact_storage=self.artifact_storage,
            )
        )
        return prepared_event, companion

    def _build_terminal_transition(
        self,
        *,
        item: PromptQueueProjectedItem,
        command_id: str,
        event_context: EventContext,
        resulting_delivery_state: str,
        transition_kind: str,
        semantic_payload: object,
        event_factory,
    ) -> AgentEvent:
        del event_context
        predecessor = _item_predecessor(
            runtime_session_id=self.runtime_session.runtime_session_id,
            item=item,
        )
        transition = _transition_head(
            runtime_session_id=self.runtime_session.runtime_session_id,
            queue_item_id=item.queue_item_id,
            accepted_ordinal=item.accepted_ordinal,
            transition_ordinal=item.item_revision,
            predecessor=predecessor,
            predecessor_payload_fingerprint=item.head_candidate_payload_fingerprint,
            previous_delivery_state=item.delivery_state,
            resulting_delivery_state=resulting_delivery_state,
            previous_content_retention_state=item.content_retention_state,
            resulting_content_retention_state=item.content_retention_state,
            expected_item_revision=item.item_revision,
            expected_account_revision=item.account_revision,
            semantic_payload=semantic_payload,
            attribution_payload={
                "command_id": command_id,
                "transition_kind": transition_kind,
                "source_reservation_fingerprint": (
                    item.reservation.reservation_fingerprint
                    if item.reservation is not None
                    else None
                ),
            },
        )
        return event_factory(transition)

    def _require_pending_item(self, queue_item_id: str) -> PromptQueueProjectedItem:
        item = self.store.item(queue_item_id)
        if item is None:
            raise KeyError(queue_item_id)
        if item.delivery_state != "accepted_pending" or item.reservation is not None:
            raise RuntimeError("queue item is not available for reservation")
        if item.prepared_content is None or item.content_retention_state != "active":
            raise RuntimeError("queue item content is unavailable")
        return item

    def _require_reserved_item(
        self,
        queue_item_id: str,
        *,
        reservation_kind: str | None,
        reservation_fingerprint: str,
    ) -> PromptQueueProjectedItem:
        item = self.store.item(queue_item_id)
        if item is None:
            raise KeyError(queue_item_id)
        reservation = item.reservation
        if (
            reservation is None
            or reservation.reservation_fingerprint != reservation_fingerprint
            or reservation_kind is not None
            and reservation.reservation_kind != reservation_kind
            or item.delivery_state
            != (
                "steer_reserved"
                if reservation.reservation_kind == "steer"
                else "follow_up_reserved"
            )
        ):
            raise RuntimeError("queue reservation authority is stale")
        return item


def _queue_item_id(
    *,
    runtime_session_id: str,
    client_instance_id: str,
    client_submission_id: str,
    content_semantic_fingerprint: str,
) -> str:
    fingerprint = context_fingerprint(
        "prompt-queue-item:v1",
        {
            "runtime_session_id": runtime_session_id,
            "client_instance_id": client_instance_id,
            "client_submission_id": client_submission_id,
            "content_semantic_fingerprint": content_semantic_fingerprint,
        },
    )
    return f"prompt-queue:{fingerprint.removeprefix('sha256:')}"


def _transition_event_id(
    *,
    queue_item_id: str,
    command_id: str,
    transition_kind: str,
    predecessor_event_id: str | None,
    expected_item_revision: int,
    reservation_generation: int,
) -> str:
    fingerprint = context_fingerprint(
        "prompt-queue-transition-event:v1",
        {
            "queue_item_id": queue_item_id,
            "command_id": command_id,
            "transition_kind": transition_kind,
            "predecessor_event_id": predecessor_event_id,
            "expected_item_revision": expected_item_revision,
            "reservation_generation": reservation_generation,
        },
    )
    return f"prompt-queue-event:{fingerprint.removeprefix('sha256:')}"


def user_steer_event_id(*, queue_item_id: str, reservation_fingerprint: str) -> str:
    fingerprint = context_fingerprint(
        "prompt-queue-user-steer-event:v1",
        (queue_item_id, reservation_fingerprint),
    )
    return f"user-steer:{fingerprint.removeprefix('sha256:')}"


def user_steer_message_id(*, queue_item_id: str, reservation_fingerprint: str) -> str:
    fingerprint = context_fingerprint(
        "prompt-queue-user-steer-message:v1",
        (queue_item_id, reservation_fingerprint),
    )
    return f"user-steer:{fingerprint.removeprefix('sha256:')}"


def _transition_head(
    *,
    runtime_session_id: str,
    queue_item_id: str,
    accepted_ordinal: int,
    transition_ordinal: int,
    predecessor: ContextEventReferenceFact | None,
    predecessor_payload_fingerprint: str | None,
    previous_delivery_state,
    resulting_delivery_state,
    previous_content_retention_state,
    resulting_content_retention_state,
    expected_item_revision: int,
    expected_account_revision: int,
    semantic_payload: object,
    attribution_payload: object,
) -> PromptQueueTransitionHeadFact:
    semantic = context_fingerprint(
        "prompt-queue-transition-semantic:v1",
        {
            "queue_item_id": queue_item_id,
            "previous_delivery_state": previous_delivery_state,
            "resulting_delivery_state": resulting_delivery_state,
            "previous_content_retention_state": previous_content_retention_state,
            "resulting_content_retention_state": resulting_content_retention_state,
            "semantic_payload": semantic_payload,
        },
    )
    attribution = context_fingerprint(
        "prompt-queue-transition-attribution:v1",
        {
            "runtime_session_id": runtime_session_id,
            "accepted_ordinal": accepted_ordinal,
            "transition_ordinal": transition_ordinal,
            "predecessor": predecessor,
            "attribution_payload": attribution_payload,
        },
    )
    return build_frozen_fact(
        PromptQueueTransitionHeadFact,
        schema_version="prompt_queue_transition_head.v1",
        runtime_session_id=runtime_session_id,
        queue_item_id=queue_item_id,
        accepted_ordinal=accepted_ordinal,
        transition_ordinal=transition_ordinal,
        predecessor_event_reference=predecessor,
        predecessor_candidate_payload_fingerprint=predecessor_payload_fingerprint,
        previous_delivery_state=previous_delivery_state,
        resulting_delivery_state=resulting_delivery_state,
        previous_content_retention_state=previous_content_retention_state,
        resulting_content_retention_state=resulting_content_retention_state,
        expected_item_revision=expected_item_revision,
        resulting_item_revision=expected_item_revision + 1,
        expected_account_revision=expected_account_revision,
        resulting_account_revision=expected_account_revision + 1,
        transition_semantic_fingerprint=semantic,
        transition_attribution_fingerprint=attribution,
    )


def _item_predecessor(
    *,
    runtime_session_id: str,
    item: PromptQueueProjectedItem,
) -> ContextEventReferenceFact:
    return ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=item.head_event_id,
        sequence=item.head_event_sequence,
        event_type=item.head_event_type,
        payload_fingerprint=item.head_candidate_payload_fingerprint,
    )


def _predicted_item(event, previous: PromptQueueProjectedItem | None = None):
    transition = event.transition
    if isinstance(event, PromptQueueAcceptedEvent):
        content = event.prepared_content
        requested = event.requested_delivery_mode
        resolved = event.resolved_delivery_mode
        reservation = None
        disposition = None
    else:
        assert previous is not None
        content = previous.prepared_content
        requested = previous.requested_delivery_mode
        resolved = previous.resolved_delivery_mode
        reservation = previous.reservation
        disposition = previous.disposition_code
        if isinstance(event, PromptQueueReservationInstalledEvent):
            reservation = event.reservation
            resolved = event.reservation.reservation_kind
        elif isinstance(event, PromptQueueReservationReleasedEvent):
            reservation = None
            resolved = "pending"
            disposition = event.release_reason
        elif isinstance(event, PromptQueueDeliveryRejectedEvent):
            reservation = None
            disposition = event.rejection_reason
        elif isinstance(event, PromptQueueCancelledEvent):
            reservation = None
            disposition = event.cancellation_reason
        elif isinstance(event, PromptQueueReconciliationRequiredEvent):
            disposition = event.stable_reason_code
        elif isinstance(event, PromptQueueContentRetiredEvent):
            content = None
            disposition = event.retirement_reason
        elif isinstance(event, PromptQueueCommittedToRunEvent):
            reservation = None
            disposition = "committed_to_run"
        elif isinstance(event, PromptQueueCommittedToProviderInputEvent):
            reservation = None
            disposition = "committed_to_provider_input"
    candidate = freeze_event_write_candidate(event).payload_fingerprint
    fingerprint_payload = {
        "queue_item_id": transition.queue_item_id,
        "accepted_ordinal": transition.accepted_ordinal,
        "delivery_state": transition.resulting_delivery_state,
        "content_retention_state": transition.resulting_content_retention_state,
        "item_revision": transition.resulting_item_revision,
        "account_revision": transition.resulting_account_revision,
        "head_event_id": event.id,
        "head_event_type": str(event.type),
        "head_event_sequence": 0,
        "head_candidate_payload_fingerprint": candidate,
        "prepared_content_fact_fingerprint": (
            content.content_fact_fingerprint if content is not None else None
        ),
        "requested_delivery_mode": requested,
        "resolved_delivery_mode": resolved,
        "reservation_fingerprint": (
            reservation.reservation_fingerprint if reservation is not None else None
        ),
        "disposition_code": disposition,
    }
    return PromptQueueProjectedItem(
        queue_item_id=transition.queue_item_id,
        accepted_ordinal=transition.accepted_ordinal,
        delivery_state=transition.resulting_delivery_state,
        content_retention_state=transition.resulting_content_retention_state,
        item_revision=transition.resulting_item_revision,
        account_revision=transition.resulting_account_revision,
        head_event_id=event.id,
        head_event_type=str(event.type),
        head_event_sequence=0,
        head_candidate_payload_fingerprint=candidate,
        prepared_content=content,
        requested_delivery_mode=requested,
        resolved_delivery_mode=resolved,
        reservation=reservation,
        disposition_code=disposition,
        row_fingerprint=context_fingerprint(
            "prompt-queue-item-row:v1", fingerprint_payload
        ),
    )


def _projected_item_payload(item: PromptQueueProjectedItem) -> dict[str, object]:
    return {
        "queue_item_id": item.queue_item_id,
        "accepted_ordinal": item.accepted_ordinal,
        "delivery_state": item.delivery_state,
        "content_retention_state": item.content_retention_state,
        "item_revision": item.item_revision,
        "account_revision": item.account_revision,
        "head_event_id": item.head_event_id,
        "head_event_type": item.head_event_type,
        "head_event_sequence": item.head_event_sequence,
        "head_candidate_payload_fingerprint": item.head_candidate_payload_fingerprint,
        "prepared_content": (
            item.prepared_content.model_dump(mode="json")
            if item.prepared_content is not None
            else None
        ),
        "requested_delivery_mode": item.requested_delivery_mode,
        "resolved_delivery_mode": item.resolved_delivery_mode,
        "reservation": (
            item.reservation.model_dump(mode="json")
            if item.reservation is not None
            else None
        ),
        "disposition_code": item.disposition_code,
        "row_fingerprint": item.row_fingerprint,
    }


def _projected_item_from_payload(
    payload: dict[str, object],
) -> PromptQueueProjectedItem:
    prepared_payload = payload.get("prepared_content")
    prepared_content: PreparedPromptQueueContentFact | None
    if prepared_payload is None:
        prepared_content = None
    elif not isinstance(prepared_payload, dict):
        raise ValueError("prompt queue prepared-content payload is malformed")
    elif prepared_payload.get("content_kind") == "inline":
        prepared_content = InlineQueueContentFact.model_validate(prepared_payload)
    elif prepared_payload.get("content_kind") == "confirmed_artifact":
        prepared_content = ConfirmedArtifactQueueContentFact.model_validate(
            prepared_payload
        )
    else:
        raise ValueError("prompt queue prepared-content kind is unknown")

    reservation_payload = payload.get("reservation")
    if reservation_payload is None:
        reservation = None
    elif isinstance(reservation_payload, dict):
        reservation = PromptQueueReservationFact.model_validate(reservation_payload)
    else:
        raise ValueError("prompt queue reservation payload is malformed")

    try:
        item = PromptQueueProjectedItem(
            queue_item_id=str(payload["queue_item_id"]),
            accepted_ordinal=int(payload["accepted_ordinal"]),
            delivery_state=str(payload["delivery_state"]),  # type: ignore[arg-type]
            content_retention_state=str(payload["content_retention_state"]),
            item_revision=int(payload["item_revision"]),
            account_revision=int(payload["account_revision"]),
            head_event_id=str(payload["head_event_id"]),
            head_event_type=str(payload["head_event_type"]),
            head_event_sequence=int(payload["head_event_sequence"]),
            head_candidate_payload_fingerprint=str(
                payload["head_candidate_payload_fingerprint"]
            ),
            prepared_content=prepared_content,
            requested_delivery_mode=str(payload["requested_delivery_mode"]),  # type: ignore[arg-type]
            resolved_delivery_mode=str(payload["resolved_delivery_mode"]),
            reservation=reservation,
            disposition_code=(
                str(payload["disposition_code"])
                if payload.get("disposition_code") is not None
                else None
            ),
            row_fingerprint=str(payload["row_fingerprint"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("prompt queue projected-item payload is malformed") from exc
    expected = context_fingerprint(
        "prompt-queue-item-row:v1", _item_fingerprint_payload(item)
    )
    if item.row_fingerprint != expected:
        raise ValueError("prompt queue projected-item row fingerprint mismatch")
    return item


def _item_fingerprint_payload(item: PromptQueueProjectedItem) -> dict[str, object]:
    return {
        "queue_item_id": item.queue_item_id,
        "accepted_ordinal": item.accepted_ordinal,
        "delivery_state": item.delivery_state,
        "content_retention_state": item.content_retention_state,
        "item_revision": item.item_revision,
        "account_revision": item.account_revision,
        "head_event_id": item.head_event_id,
        "head_event_type": item.head_event_type,
        "head_event_sequence": item.head_event_sequence,
        "head_candidate_payload_fingerprint": (item.head_candidate_payload_fingerprint),
        "prepared_content_fact_fingerprint": (
            item.prepared_content.content_fact_fingerprint
            if item.prepared_content is not None
            else None
        ),
        "requested_delivery_mode": item.requested_delivery_mode,
        "resolved_delivery_mode": item.resolved_delivery_mode,
        "reservation_fingerprint": (
            item.reservation.reservation_fingerprint
            if item.reservation is not None
            else None
        ),
        "disposition_code": item.disposition_code,
    }


def _with_stored_sequence(
    item: PromptQueueProjectedItem, sequence: int
) -> PromptQueueProjectedItem:
    payload = _item_fingerprint_payload(item)
    payload["head_event_sequence"] = sequence
    return replace(
        item,
        head_event_sequence=sequence,
        row_fingerprint=context_fingerprint("prompt-queue-item-row:v1", payload),
    )


def _row_set_accumulator(items: Sequence[PromptQueueProjectedItem]) -> str:
    return context_fingerprint(
        "prompt-queue-row-set:v1",
        tuple(
            (item.queue_item_id, item.row_fingerprint)
            for item in sorted(items, key=lambda value: value.queue_item_id)
        ),
    )


def _pending_head_set_accumulator(
    items: Sequence[PromptQueueProjectedItem],
) -> str:
    return context_fingerprint(
        "prompt-queue-pending-head-set:v1",
        tuple(
            (
                item.queue_item_id,
                item.head_event_id,
                item.head_candidate_payload_fingerprint,
            )
            for item in sorted(items, key=lambda value: value.queue_item_id)
            if item.delivery_state
            in {"accepted_pending", "steer_reserved", "follow_up_reserved"}
        ),
    )


def prompt_queue_item_public_view_payload(
    item: PromptQueueProjectedItem,
) -> dict[str, object]:
    """Return the one reducer-owned public item projection payload."""

    content = item.prepared_content
    if content is None:
        preview = ""
    elif getattr(content, "content_kind", None) == "inline":
        preview = bounded_terminal_safe_public_text(
            content.canonical_utf8_text,
            maximum_code_points=512,
            maximum_utf8_bytes=2_048,
        )
    else:
        preview = "[confirmed artifact content]"
    return {
        "queue_item_id": item.queue_item_id,
        "accepted_ordinal": item.accepted_ordinal,
        "delivery_state": item.delivery_state,
        "content_retention_state": item.content_retention_state,
        "requested_delivery_mode": item.requested_delivery_mode,
        "resolved_delivery_mode": item.resolved_delivery_mode,
        "public_preview": preview,
        "head_event_id": item.head_event_id,
        "item_revision": item.item_revision,
    }


def prompt_queue_item_public_view_fingerprint(
    item: PromptQueueProjectedItem,
) -> str:
    return context_fingerprint(
        "prompt-queue-item-view:v1", prompt_queue_item_public_view_payload(item)
    )


def _active_client_items(
    items: Sequence[PromptQueueProjectedItem],
) -> tuple[PromptQueueProjectedItem, ...]:
    result = tuple(
        sorted(
            (
                item
                for item in items
                if item.delivery_state in CLIENT_VISIBLE_ACTIVE_QUEUE_STATES
                and item.content_retention_state == "active"
            ),
            key=lambda item: (item.accepted_ordinal, item.queue_item_id),
        )
    )
    if len(result) > MAX_ACTIVE_PROMPT_QUEUE_ITEMS:
        raise ValueError("active prompt queue projection exceeds its durable bound")
    return result


def _active_client_item_count(items: Sequence[PromptQueueProjectedItem]) -> int:
    return len(_active_client_items(items))


def _active_client_item_accumulator(
    items: Sequence[PromptQueueProjectedItem],
) -> str:
    return context_fingerprint(
        "terminal-active-prompt-queue-items:v1",
        tuple(
            prompt_queue_item_public_view_fingerprint(item)
            for item in _active_client_items(items)
        ),
    )


def _artifact_bytes_delta(
    *,
    companion_kind: str,
    resulting_item: PromptQueueProjectedItem,
    previous_item: PromptQueueProjectedItem | None,
) -> int:
    if companion_kind == "ACCEPT" and isinstance(
        resulting_item.prepared_content, ConfirmedArtifactQueueContentFact
    ):
        return resulting_item.prepared_content.canonical_byte_count
    if (
        companion_kind == "CONTENT_RETIRE"
        and previous_item is not None
        and isinstance(
            previous_item.prepared_content, ConfirmedArtifactQueueContentFact
        )
    ):
        return -previous_item.prepared_content.canonical_byte_count
    return 0


def _companion_fields(
    *,
    runtime_session_id: str,
    store: PromptQueueProjectionStore,
    prepared_event: AgentEvent,
    resulting_item: PromptQueueProjectedItem,
    artifact_storage: PromptQueueArtifactStoragePort,
    prepared_batch: Sequence[AgentEvent] | None = None,
) -> dict[str, object]:
    batch = tuple(prepared_batch or (prepared_event,))
    if sum(event.id == prepared_event.id for event in batch) != 1:
        raise ValueError("queue transition must occur once in its physical batch")
    candidates = tuple(freeze_event_write_candidate(event) for event in batch)
    previous_item = store.item(resulting_item.queue_item_id)
    existing = {item.queue_item_id: item for item in store.all_items()}
    existing[resulting_item.queue_item_id] = resulting_item
    resulting_items = tuple(existing[key] for key in sorted(existing))
    companion_kind_by_type = {
        PromptQueueAcceptedEvent: "ACCEPT",
        PromptQueueReservationInstalledEvent: "RESERVE",
        PromptQueueReservationReleasedEvent: "RELEASE_RESERVATION",
        PromptQueueCommittedToProviderInputEvent: "COMMIT_TO_ACTIVE_RUN",
        PromptQueueCommittedToRunEvent: "COMMIT_TO_NEW_RUN",
        PromptQueueCancelledEvent: "CANCEL",
        PromptQueueDeliveryRejectedEvent: "DELIVERY_REJECT",
        PromptQueueReconciliationRequiredEvent: "RECONCILIATION_LATCH",
        PromptQueueContentRetiredEvent: "CONTENT_RETIRE",
    }
    companion_kind = companion_kind_by_type[type(prepared_event)]
    relevant_content = (
        previous_item.prepared_content
        if companion_kind == "CONTENT_RETIRE" and previous_item is not None
        else resulting_item.prepared_content
    )
    content_reference_mutation_count = int(
        companion_kind in {"ACCEPT", "CONTENT_RETIRE"}
    )
    artifact_hold_mutation_count = int(
        content_reference_mutation_count == 1
        and isinstance(relevant_content, ConfirmedArtifactQueueContentFact)
    )
    relations = [
        ("prompt_queue_accounts", "upsert", 1),
        ("prompt_queue_items", "upsert", 1),
    ]
    if content_reference_mutation_count:
        relations.append(
            (
                "prompt_queue_content_references",
                "insert" if companion_kind == "ACCEPT" else "delete",
                1,
            )
        )
    if artifact_hold_mutation_count:
        relations.append(
            (
                "prompt_queue_artifact_preparation_holds",
                "consume" if companion_kind == "ACCEPT" else "release",
                1,
            )
        )
    plan = {
        "runtime_session_id": runtime_session_id,
        "transition_event_id": prepared_event.id,
        "expected_account_revision": store.account_revision,
        "resulting_item_fingerprint": resulting_item.row_fingerprint,
        "resulting_item_count": len(resulting_items),
        "relations": tuple(relations),
    }
    base_bytes = len(canonical_json_bytes(plan)) + len(
        canonical_json_bytes(_projected_item_payload(resulting_item))
    )
    charged = base_bytes + 8 * 1024
    if charged > 64 * 1024:
        raise ValueError("prompt queue companion plan exceeds its charge contract")
    batch_identity = build_prepared_candidate_batch_identity(candidates)
    charge = build_frozen_fact(
        PromptQueueCompanionChargeFact,
        schema_version="prompt_queue_companion_charge.v1",
        companion_kind=companion_kind,
        runtime_session_id=runtime_session_id,
        exact_ordered_event_batch_fingerprint=(
            batch_identity.exact_ordered_batch_fingerprint
        ),
        item_row_mutation_count=1,
        account_row_mutation_count=1,
        content_reference_mutation_count=content_reference_mutation_count,
        artifact_hold_mutation_count=artifact_hold_mutation_count,
        total_auxiliary_row_mutations=(
            2 + content_reference_mutation_count + artifact_hold_mutation_count
        ),
        normalized_auxiliary_payload_base_bytes=base_bytes,
        sequence_wrapper_max_bytes=4 * 1024,
        revision_wrapper_max_bytes=4 * 1024,
        conservative_charged_payload_bytes=charged,
        charge_contract_fingerprint=(
            PROMPT_QUEUE_COMPANION_CHARGE_CONTRACT_FINGERPRINT
        ),
        storage_mutation_plan_fingerprint=context_fingerprint(
            "prompt-queue-storage-mutation-plan:v1", plan
        ),
    )
    return {
        "runtime_session_id": runtime_session_id,
        "prepared_candidate_batch_identity": (batch_identity),
        "resulting_item": resulting_item,
        "previous_item": previous_item,
        "transition_event_id": prepared_event.id,
        "expected_account_revision": store.account_revision,
        "previous_transition_count": store.transition_count,
        "previous_transition_accumulator": store.transition_accumulator,
        "resulting_next_accepted_ordinal": (
            store.next_accepted_ordinal
            + (1 if isinstance(prepared_event, PromptQueueAcceptedEvent) else 0)
        ),
        "resulting_items": resulting_items,
        "charge": charge,
        "projection_store": store,
        "artifact_storage": artifact_storage,
    }


__all__ = [
    "PROMPT_QUEUE_COMPANION_CHARGE_CONTRACT_FINGERPRINT",
    "PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT",
    "PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT",
    "PromptQueueProjectedItem",
    "PromptQueueModelStartTransactionCompanion",
    "PromptQueueProjectionStore",
    "PromptQueueSubmitRequest",
    "PromptQueueTransactionCompanion",
    "TerminalPromptQueueMutationService",
    "user_steer_event_id",
    "user_steer_message_id",
]
