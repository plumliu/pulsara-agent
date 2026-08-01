"""Session-owned bounded checkpoint/reopen owner for the durable prompt queue."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import TYPE_CHECKING

from pulsara_agent.blocking_executor import auxiliary_io_executor
from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log.historical_decoder import (
    decode_raw_stored_event_envelope,
)
from pulsara_agent.primitives.stored_event import RawRuntimeProjectionCheckpoint
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.ports.prompt_queue import PromptQueueCheckpointCommitGuard
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.prompt_queue import (
    PROMPT_QUEUE_EVENT_TYPE_VALUES,
    PromptQueueDomainCheckpointFact,
    build_prompt_queue_domain_checkpoint,
)
from pulsara_agent.runtime.terminal_application.prompt_queue import (
    QUEUE_EVENT_TYPES,
    PromptQueueProjectionSnapshot,
    PromptQueueProjectionStore,
    _projected_item_payload,
)
from pulsara_agent.storage.prompt_queue_bootstrap import (
    PROMPT_QUEUE_PROJECTION_KIND,
    PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION,
    build_prompt_queue_genesis_raw_checkpoint,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


@dataclass(slots=True)
class PromptQueueCheckpointService:
    """Owns queue checkpoint I/O without entering the durable writer await path."""

    runtime_session: RuntimeSession
    store: PromptQueueProjectionStore
    _raw_checkpoint: RawRuntimeProjectionCheckpoint = field(init=False, repr=False)
    _checkpoint: PromptQueueDomainCheckpointFact = field(init=False, repr=False)
    _wakeup: asyncio.Event | None = field(default=None, init=False, repr=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _reconciliation_reason: str | None = field(default=None, init=False, repr=False)

    @property
    def reconciliation_reason(self) -> str | None:
        return self._reconciliation_reason

    @property
    def pending(self) -> bool:
        return self._worker is not None and not self._worker.done()

    def initialize(self, *, deadline_monotonic: float | None) -> None:
        event_log = self.runtime_session.event_log
        read_bundle = getattr(event_log, "read_prompt_queue_restore_bundle", None)
        if callable(read_bundle):
            bundle = read_bundle(
                max_delta_events=256,
                max_delta_payload_bytes=8 * 1024 * 1024,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.restore_checkpoint(
                bundle.checkpoint,
                item_payloads=bundle.checkpoint_item_payloads,
                head_event_type=bundle.checkpoint_head_event_type,
            )
            delta = tuple(
                decode_raw_stored_event_envelope(item, DEFAULT_EVENT_SCHEMA_REGISTRY)
                for item in bundle.bounded_delta_events
            )
            self.store.apply_sparse_bootstrap(
                delta,
                through_sequence=bundle.ledger_high_water,
            )
            self.store.validate_durable_projection(
                account=bundle.account,
                item_payloads=bundle.current_item_payloads,
            )
            self._raw_checkpoint = bundle.raw_checkpoint
            self._checkpoint = bundle.checkpoint
            return

        raw = event_log.read_runtime_projection_checkpoint(
            PROMPT_QUEUE_PROJECTION_KIND,
            deadline_monotonic=deadline_monotonic,
        )
        if raw is None:
            raw = build_prompt_queue_genesis_raw_checkpoint(
                self.runtime_session.runtime_session_id
            )
            event_log.write_runtime_projection_checkpoint(
                raw, deadline_monotonic=deadline_monotonic
            )
        checkpoint = PromptQueueDomainCheckpointFact.model_validate(
            raw.state_payload["checkpoint"]
        )
        self.store.restore_checkpoint(
            checkpoint,
            item_payloads=tuple(raw.state_payload.get("items", ())),
            head_event_type=(
                str(raw.state_payload["head_event_type"])
                if raw.state_payload.get("head_event_type") is not None
                else None
            ),
        )
        selection = event_log.read_raw_events_by_types(
            PROMPT_QUEUE_EVENT_TYPE_VALUES,
            active_runs_only=False,
            minimum_sequence=checkpoint.through_sequence + 1,
            max_events=256,
            max_payload_bytes=8 * 1024 * 1024,
            deadline_monotonic=deadline_monotonic,
        )
        delta = tuple(
            decode_raw_stored_event_envelope(item, DEFAULT_EVENT_SCHEMA_REGISTRY)
            for item in selection.events
        )
        self.store.apply_sparse_bootstrap(
            delta, through_sequence=selection.through_sequence
        )
        self._raw_checkpoint = raw
        self._checkpoint = checkpoint

    def start_background_if_possible(self) -> None:
        if self._closed or self._worker is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._wakeup = asyncio.Event()
        self._worker = loop.create_task(
            self._run(),
            name=f"prompt-queue-checkpoint:{self.runtime_session.runtime_session_id}",
        )

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        self.store.apply_committed(events)
        if any(isinstance(event, QUEUE_EVENT_TYPES) for event in events):
            self.wake()

    def wake(self) -> None:
        if self._worker is None:
            self.start_background_if_possible()
        event = self._wakeup
        if event is not None and not self._closed:
            event.set()

    async def _run(self) -> None:
        assert self._wakeup is not None
        try:
            while not self._closed:
                await self._wakeup.wait()
                self._wakeup.clear()
                while self._needs_checkpoint():
                    deadline = monotonic() + 10.0
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        auxiliary_io_executor(),
                        lambda: self._checkpoint_once(deadline_monotonic=deadline),
                    )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._reconciliation_reason = (
                f"PROMPT_QUEUE_CHECKPOINT_{type(exc).__name__.upper()}"
            )

    def _needs_checkpoint(self) -> bool:
        snapshot = self.store.snapshot()
        return snapshot.queue_head_event_sequence > self._checkpoint.through_sequence

    def _checkpoint_once(self, *, deadline_monotonic: float) -> None:
        snapshot = self.store.snapshot()
        predecessor = self._raw_checkpoint
        if snapshot.queue_head_event_sequence <= predecessor.through_sequence:
            return
        checkpoint = _build_checkpoint(
            snapshot,
            checkpoint_generation=self._checkpoint.checkpoint_generation + 1,
        )
        ledger_prefix = self.runtime_session.event_log.read_raw_ledger_prefix(
            through_sequence=checkpoint.through_sequence,
            deadline_monotonic=deadline_monotonic,
        )
        state_payload = {
            "checkpoint": checkpoint.model_dump(mode="json"),
            "items": [
                _projected_item_payload(item)
                for item in sorted(
                    snapshot.items, key=lambda value: value.accepted_ordinal
                )
            ],
            "head_event_type": snapshot.queue_head_event_type,
        }
        payload = {
            "projection_kind": PROMPT_QUEUE_PROJECTION_KIND,
            "through_sequence": checkpoint.through_sequence,
            "projection_schema_version": PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION,
            "ledger_prefix": asdict(ledger_prefix),
            "validation_base_through_sequence": predecessor.through_sequence,
            "validation_base_state_payload": predecessor.state_payload,
            "state_payload": state_payload,
        }
        candidate = RawRuntimeProjectionCheckpoint(
            projection_kind=PROMPT_QUEUE_PROJECTION_KIND,
            through_sequence=checkpoint.through_sequence,
            projection_schema_version=PROMPT_QUEUE_PROJECTION_SCHEMA_VERSION,
            ledger_prefix=ledger_prefix,
            validation_base_through_sequence=predecessor.through_sequence,
            validation_base_state_payload=predecessor.state_payload,
            state_payload=state_payload,
            payload_fingerprint=context_fingerprint(
                "prompt-queue-runtime-checkpoint-row:v1", payload
            ),
        )
        commit = getattr(
            self.runtime_session.event_log, "commit_prompt_queue_checkpoint", None
        )
        if not callable(commit):
            self.runtime_session.event_log.write_runtime_projection_checkpoint(
                candidate, deadline_monotonic=deadline_monotonic
            )
            self._raw_checkpoint = candidate
            self._checkpoint = checkpoint
            return
        guard = PromptQueueCheckpointCommitGuard(
            runtime_session_id=snapshot.runtime_session_id,
            expected_previous_through_sequence=predecessor.through_sequence,
            expected_previous_payload_fingerprint=predecessor.payload_fingerprint,
            expected_account_revision=snapshot.account_revision,
            expected_queue_head_event_id=snapshot.queue_head_event_id,
            expected_queue_head_payload_fingerprint=(
                snapshot.queue_head_payload_fingerprint
            ),
            expected_row_set_accumulator=snapshot.row_set_accumulator,
            expected_pending_item_head_set_accumulator=(
                snapshot.pending_head_set_accumulator
            ),
            guard_generation=checkpoint.checkpoint_generation,
        )
        outcome = commit(
            candidate=candidate,
            checkpoint=checkpoint,
            guard=guard,
            deadline_monotonic=deadline_monotonic,
        )
        if outcome.disposition == "none":
            return
        if outcome.disposition == "reconciliation_required":
            raise RuntimeError("prompt queue checkpoint requires reconciliation")
        bundle = self.runtime_session.event_log.read_prompt_queue_restore_bundle(
            max_delta_events=256,
            max_delta_payload_bytes=8 * 1024 * 1024,
            deadline_monotonic=deadline_monotonic,
        )
        self._raw_checkpoint = bundle.raw_checkpoint
        self._checkpoint = bundle.checkpoint

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        self.wake()
        while self._needs_checkpoint():
            if self._reconciliation_reason is not None:
                raise RuntimeError(self._reconciliation_reason)
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("prompt queue checkpoint drain timed out")
            if self._worker is None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    auxiliary_io_executor(),
                    lambda: self._checkpoint_once(
                        deadline_monotonic=deadline_monotonic
                    ),
                )
            else:
                await asyncio.sleep(0.01)

    def close_if_idle(self) -> None:
        if self._needs_checkpoint() or self._reconciliation_reason is not None:
            raise RuntimeError("prompt queue checkpoint owner is not drained")
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None


def _build_checkpoint(
    snapshot: PromptQueueProjectionSnapshot,
    *,
    checkpoint_generation: int,
) -> PromptQueueDomainCheckpointFact:
    if snapshot.queue_head_event_sequence < 1:
        raise ValueError("non-genesis queue checkpoint lacks a queue head")
    return build_prompt_queue_domain_checkpoint(
        runtime_session_id=snapshot.runtime_session_id,
        checkpoint_generation=checkpoint_generation,
        through_sequence=snapshot.queue_head_event_sequence,
        transition_count=snapshot.transition_count,
        transition_accumulator=snapshot.transition_accumulator,
        account_revision=snapshot.account_revision,
        next_accepted_ordinal=snapshot.next_accepted_ordinal,
        pending_item_head_set_accumulator=snapshot.pending_head_set_accumulator,
        queue_row_set_accumulator=snapshot.row_set_accumulator,
        resulting_queue_head_event_id=snapshot.queue_head_event_id,
        resulting_queue_head_payload_fingerprint=(
            snapshot.queue_head_payload_fingerprint
        ),
    )


__all__ = ["PromptQueueCheckpointService"]
