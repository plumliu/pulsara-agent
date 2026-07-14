"""Session-owned bounded checkpoint read/write service."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import monotonic
from typing import TYPE_CHECKING, Protocol

from pulsara_agent.event import EventType, SubagentGraphCheckpointCommittedEvent
from pulsara_agent.event_log import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventLog,
    InMemoryEventLog,
)
from pulsara_agent.memory.foundation.records import ArtifactContentConflict
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.long_horizon import (
    SubagentGraphCheckpointPolicyFact,
    SubagentGraphReducerContractFact,
)
from pulsara_agent.runtime.long_horizon.checkpoint import (
    PreparedSubagentGraphCheckpoint,
    SubagentGraphCheckpointDeltaSnapshot,
    SubagentGraphCheckpointReadResult,
    SubagentGraphCheckpointReadUnavailable,
    prepare_subagent_graph_checkpoint,
    prepare_subagent_graph_checkpoint_from_restore,
    restore_subagent_graph_from_checkpoint,
)
from pulsara_agent.runtime.subagent.facts import SubagentGraphState
from pulsara_agent.runtime.long_horizon.reducer_contract import (
    SubagentGraphReducerBinding,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
    checkpoint_maintenance_authority_for_event_log,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


class SubagentGraphCheckpointBootstrapBoundExceeded(RuntimeError):
    pass


class SubagentGraphCheckpointRebaseUnavailable(RuntimeError):
    pass


class SubagentGraphCheckpointDeltaBoundExceeded(RuntimeError):
    pass


class SubagentGraphCheckpointWriteBlocked(RuntimeError):
    pass


class SubagentGraphCheckpointWriteState(StrEnum):
    PENDING = "pending"
    WRITING_ARTIFACT = "writing_artifact"
    WRITING_EVENT = "writing_event"
    CONFIRMING = "confirming"
    COMMITTED = "committed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class SubagentGraphCheckpointReadPort(Protocol):
    def read_checkpoint_and_delta_snapshot(
        self,
        *,
        requested_through_sequence: int,
        reducer_contract: SubagentGraphReducerContractFact,
        preferred_checkpoint_id: str | None,
        max_delta_events: int,
        max_delta_bytes: int,
        max_checkpoint_candidates: int,
    ) -> SubagentGraphCheckpointReadResult: ...


@dataclass(frozen=True, slots=True)
class EventLogSubagentGraphCheckpointReadPort:
    event_log: EventLog
    archive: ArtifactStore
    runtime_session_id: str
    read_timeout_seconds: float = 30.0
    maintenance_authority: CheckpointMaintenanceAuthority | None = None

    def read_checkpoint_and_delta_snapshot(
        self,
        *,
        requested_through_sequence: int,
        reducer_contract: SubagentGraphReducerContractFact,
        preferred_checkpoint_id: str | None,
        max_delta_events: int,
        max_delta_bytes: int,
        max_checkpoint_candidates: int,
    ) -> SubagentGraphCheckpointReadResult:
        authority = self.maintenance_authority
        if authority is None:
            authority = checkpoint_maintenance_authority_for_event_log(
                self.event_log
            )
        guard = (
            authority.acquire_shared(self.runtime_session_id)
            if authority is not None
            else nullcontext()
        )
        with guard:
            return self._read_checkpoint_and_delta_snapshot_locked(
                requested_through_sequence=requested_through_sequence,
                reducer_contract=reducer_contract,
                preferred_checkpoint_id=preferred_checkpoint_id,
                max_delta_events=max_delta_events,
                max_delta_bytes=max_delta_bytes,
                max_checkpoint_candidates=max_checkpoint_candidates,
            )

    def _read_checkpoint_and_delta_snapshot_locked(
        self,
        *,
        requested_through_sequence: int,
        reducer_contract: SubagentGraphReducerContractFact,
        preferred_checkpoint_id: str | None,
        max_delta_events: int,
        max_delta_bytes: int,
        max_checkpoint_candidates: int,
    ) -> SubagentGraphCheckpointReadResult:
        deadline = monotonic() + self.read_timeout_seconds
        catalog_snapshot = self.event_log.read_raw_checkpoint_ledger_snapshot(
            checkpoint_event_type=str(
                EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
            ),
            requested_through_sequence=requested_through_sequence,
            graph_reducer_id=reducer_contract.graph_reducer_id,
            graph_reducer_version=reducer_contract.graph_reducer_version,
            graph_reducer_contract_fingerprint=(
                reducer_contract.graph_reducer_contract_fingerprint
            ),
            preferred_checkpoint_id=preferred_checkpoint_id,
            # Phase one is catalog-only.  Selecting a checkpoint artifact before
            # loading the ledger suffix prevents overlapping delta reads.
            max_delta_events=0,
            max_delta_bytes=0,
            max_checkpoint_candidates=max_checkpoint_candidates,
            deadline_monotonic=deadline,
        )
        readable_count = 0
        saw_delta_bound = False
        selected_checkpoint_id: str | None = None
        selected_payload_text: str | None = None
        for candidate in sorted(
            catalog_snapshot.candidates,
            key=lambda item: item.checkpoint_through_sequence,
            reverse=True,
        ):
            if candidate.delta_event_count > max_delta_events:
                saw_delta_bound = True
                continue
            raw = candidate.checkpoint_event
            event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(event, SubagentGraphCheckpointCommittedEvent):
                raise RuntimeError(
                    "checkpoint catalog decoder returned the wrong event type"
                )
            checkpoint = event.checkpoint
            if (
                checkpoint.checkpoint_id != candidate.checkpoint_id
                or checkpoint.through_sequence
                != candidate.checkpoint_through_sequence
                or checkpoint.graph_reducer_id
                != reducer_contract.graph_reducer_id
                or checkpoint.graph_reducer_version
                != reducer_contract.graph_reducer_version
                or checkpoint.graph_reducer_contract_fingerprint
                != reducer_contract.graph_reducer_contract_fingerprint
            ):
                raise RuntimeError("checkpoint ledger catalog identity drifted")
            try:
                payload_text = self.archive.get_text(
                    event.artifact.artifact_id,
                    session_id=self.runtime_session_id,
                    deadline_monotonic=deadline,
                )
            except (KeyError, ValueError):
                continue
            readable_count += 1
            selected_checkpoint_id = checkpoint.checkpoint_id
            selected_payload_text = payload_text
            break
        if selected_checkpoint_id is not None and selected_payload_text is not None:
            ledger_snapshot = self.event_log.read_raw_checkpoint_ledger_snapshot(
                checkpoint_event_type=str(
                    EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
                ),
                requested_through_sequence=requested_through_sequence,
                graph_reducer_id=reducer_contract.graph_reducer_id,
                graph_reducer_version=reducer_contract.graph_reducer_version,
                graph_reducer_contract_fingerprint=(
                    reducer_contract.graph_reducer_contract_fingerprint
                ),
                preferred_checkpoint_id=selected_checkpoint_id,
                max_delta_events=max_delta_events,
                max_delta_bytes=max_delta_bytes,
                max_checkpoint_candidates=1,
                deadline_monotonic=deadline,
            )
            candidate = next(iter(ledger_snapshot.candidates), None)
            if candidate is not None and (
                candidate.checkpoint_id == selected_checkpoint_id
                and candidate.event_bound_satisfied
                and candidate.byte_bound_satisfied
            ):
                return SubagentGraphCheckpointDeltaSnapshot(
                    requested_through_sequence=requested_through_sequence,
                    checkpoint_event=candidate.checkpoint_event,
                    checkpoint_payload_bytes=selected_payload_text.encode("utf-8"),
                    checkpoint_materialization_sequence=(
                        candidate.checkpoint_event.sequence
                    ),
                    delta_events=candidate.delta_events,
                    ledger_high_water_observed=(
                        ledger_snapshot.ledger_high_water_observed
                    ),
                    preferred_checkpoint_id=preferred_checkpoint_id,
                    selected_checkpoint_id=selected_checkpoint_id,
                    rebased=(
                        preferred_checkpoint_id is not None
                        and preferred_checkpoint_id != selected_checkpoint_id
                    ),
                )
            saw_delta_bound = True
        reason = "no_compatible_artifact"
        if catalog_snapshot.confirmed_checkpoint_count == 0:
            reason = "no_confirmed_checkpoint"
        elif catalog_snapshot.contract_compatible_checkpoint_count == 0:
            reason = "reducer_contract_mismatch"
        elif saw_delta_bound:
            reason = "delta_bound_exceeded"
        return SubagentGraphCheckpointReadUnavailable(
            runtime_session_id=self.runtime_session_id,
            requested_through_sequence=requested_through_sequence,
            reason_code=reason,
            confirmed_checkpoint_count=catalog_snapshot.confirmed_checkpoint_count,
            contract_compatible_checkpoint_count=(
                catalog_snapshot.contract_compatible_checkpoint_count
            ),
            readable_artifact_count=readable_count,
            nearest_compatible_checkpoint_id=(
                catalog_snapshot.nearest_compatible_checkpoint_id
            ),
            nearest_compatible_checkpoint_through_sequence=(
                catalog_snapshot.nearest_compatible_checkpoint_through_sequence
            ),
        )


@dataclass(slots=True)
class SubagentGraphCheckpointService:
    runtime_session: RuntimeSession
    reducer_binding: SubagentGraphReducerBinding
    policy: SubagentGraphCheckpointPolicyFact
    read_timeout_seconds: float = 30.0
    _owners: dict[int, asyncio.Task[SubagentGraphCheckpointDeltaSnapshot]] = field(
        default_factory=dict, init=False, repr=False
    )
    _write_states: dict[int, SubagentGraphCheckpointWriteState] = field(
        default_factory=dict, init=False, repr=False
    )
    _physical_operations: set[asyncio.Task[object]] = field(
        default_factory=set, init=False, repr=False
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _bootstrap_eligible: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._bootstrap_eligible = (
            self.runtime_session.event_log.next_sequence() - 1 == 0
        )

    def restore_for_live_store(
        self,
        *,
        requested_through_sequence: int,
        preferred_checkpoint_id: str | None = None,
    ) -> SubagentGraphState:
        """Restore the process-local graph memoization without a prefix fold."""

        if requested_through_sequence < 0:
            raise ValueError("live subagent graph high-water cannot be negative")
        if requested_through_sequence == 0:
            return SubagentGraphState.empty()
        result = self._read_checkpoint(
            requested_through_sequence,
            preferred_checkpoint_id,
        )
        if isinstance(result, SubagentGraphCheckpointDeltaSnapshot):
            state, _, _ = restore_subagent_graph_from_checkpoint(
                snapshot=result,
                reducer_binding=self.reducer_binding,
            )
            return state
        if result.reason_code != "no_confirmed_checkpoint":
            raise SubagentGraphCheckpointRebaseUnavailable(
                "subagent_checkpoint_rebase_unavailable"
            )

        if isinstance(self.runtime_session.event_log, InMemoryEventLog):
            if requested_through_sequence > self.policy.bootstrap_max_events:
                raise SubagentGraphCheckpointBootstrapBoundExceeded(
                    "subagent_checkpoint_bootstrap_bound_exceeded"
                )
            raw = self.runtime_session.event_log.read_raw_range_snapshot(
                minimum_sequence=1,
                through_sequence=requested_through_sequence,
            )
            if (
                sum(len(item.canonical_payload_bytes) for item in raw.events)
                > self.policy.bootstrap_max_bytes
            ):
                raise SubagentGraphCheckpointBootstrapBoundExceeded(
                    "subagent_checkpoint_bootstrap_bound_exceeded"
                )
            return prepare_subagent_graph_checkpoint(
                runtime_session_id=self.runtime_session.runtime_session_id,
                prefix_events=raw.events,
                reducer_binding=self.reducer_binding,
            ).state

        deadline = monotonic() + self.read_timeout_seconds
        graph_types = tuple(sorted({
            contract.event_type
            for contract in self.reducer_binding.contract.supported_graph_events
        }))
        try:
            graph_snapshot = self.runtime_session.event_log.read_raw_events_by_types(
                graph_types,
                max_events=1,
                max_payload_bytes=1024 * 1024,
                deadline_monotonic=deadline,
            )
        except ValueError as exc:
            raise SubagentGraphCheckpointRebaseUnavailable(
                "subagent_checkpoint_required_for_live_graph"
            ) from exc
        if graph_snapshot.events:
            raise SubagentGraphCheckpointRebaseUnavailable(
                "subagent_checkpoint_required_for_live_graph"
            )
        return replace(
            SubagentGraphState.empty(),
            through_sequence=requested_through_sequence,
        )

    async def restore_for_selection(
        self,
        *,
        requested_through_sequence: int,
        preferred_checkpoint_id: str | None = None,
    ) -> SubagentGraphCheckpointDeltaSnapshot:
        if requested_through_sequence < 1:
            raise ValueError("subagent graph selection requires a positive high-water")
        result = await asyncio.to_thread(
            self._read_checkpoint,
            requested_through_sequence,
            preferred_checkpoint_id,
        )
        if isinstance(result, SubagentGraphCheckpointDeltaSnapshot):
            await self._schedule_advancement_if_needed(result)
            return result
        if result.reason_code == "no_confirmed_checkpoint":
            if not self._bootstrap_eligible:
                raise SubagentGraphCheckpointRebaseUnavailable(
                    "subagent_checkpoint_rebase_unavailable"
                )
            return await self._bootstrap(requested_through_sequence)
        if result.reason_code == "delta_bound_exceeded":
            if await self._await_relevant_writer(requested_through_sequence):
                retry = await asyncio.to_thread(
                    self._read_checkpoint,
                    requested_through_sequence,
                    preferred_checkpoint_id,
                )
                if isinstance(retry, SubagentGraphCheckpointDeltaSnapshot):
                    await self._schedule_advancement_if_needed(retry)
                    return retry
            raise SubagentGraphCheckpointDeltaBoundExceeded(
                "subagent_checkpoint_delta_bound_exceeded"
            )
        raise SubagentGraphCheckpointRebaseUnavailable(
            "subagent_checkpoint_rebase_unavailable"
        )

    def _read_checkpoint(
        self,
        requested_through_sequence: int,
        preferred_checkpoint_id: str | None,
    ) -> SubagentGraphCheckpointReadResult:
        return EventLogSubagentGraphCheckpointReadPort(
            event_log=self.runtime_session.event_log,
            archive=self.runtime_session.archive,
            runtime_session_id=self.runtime_session.runtime_session_id,
            read_timeout_seconds=self.read_timeout_seconds,
        ).read_checkpoint_and_delta_snapshot(
            requested_through_sequence=requested_through_sequence,
            reducer_contract=self.reducer_binding.contract,
            preferred_checkpoint_id=preferred_checkpoint_id,
            max_delta_events=self.policy.checkpoint_max_delta_events,
            max_delta_bytes=self.policy.checkpoint_max_delta_bytes,
            max_checkpoint_candidates=self.policy.rebase_max_checkpoint_candidates,
        )

    def _contract_matches(self, checkpoint) -> bool:
        contract = self.reducer_binding.contract
        return (
            checkpoint.graph_reducer_id == contract.graph_reducer_id
            and checkpoint.graph_reducer_version == contract.graph_reducer_version
            and checkpoint.graph_reducer_contract_fingerprint
            == contract.graph_reducer_contract_fingerprint
        )

    async def _bootstrap(
        self, requested_through_sequence: int
    ) -> SubagentGraphCheckpointDeltaSnapshot:
        async with self._lock:
            owner = self._owners.get(requested_through_sequence)
            if owner is None:
                owner = asyncio.create_task(
                    self._bootstrap_owner(requested_through_sequence)
                )
                self._write_states[requested_through_sequence] = (
                    SubagentGraphCheckpointWriteState.PENDING
                )
                self._owners[requested_through_sequence] = owner
                owner.add_done_callback(
                    lambda completed, sequence=requested_through_sequence: (
                        self._owners.pop(sequence, None)
                        if self._owners.get(sequence) is completed
                        else None
                    )
                )
        return await asyncio.shield(owner)

    async def _bootstrap_owner(
        self, requested_through_sequence: int
    ) -> SubagentGraphCheckpointDeltaSnapshot:
        if requested_through_sequence > self.policy.bootstrap_max_events:
            raise SubagentGraphCheckpointBootstrapBoundExceeded(
                "subagent_checkpoint_bootstrap_bound_exceeded"
            )
        deadline = monotonic() + self.read_timeout_seconds
        raw = await asyncio.to_thread(
            self.runtime_session.event_log.read_raw_range_snapshot,
            minimum_sequence=1,
            through_sequence=requested_through_sequence,
            deadline_monotonic=deadline,
        )
        byte_count = sum(len(item.canonical_payload_bytes) for item in raw.events)
        if byte_count > self.policy.bootstrap_max_bytes:
            raise SubagentGraphCheckpointBootstrapBoundExceeded(
                "subagent_checkpoint_bootstrap_bound_exceeded"
            )
        prepared = prepare_subagent_graph_checkpoint(
            runtime_session_id=self.runtime_session.runtime_session_id,
            prefix_events=raw.events,
            reducer_binding=self.reducer_binding,
        )
        return await self._write_prepared_checkpoint(
            prepared,
            deadline_monotonic=deadline,
        )

    async def _schedule_advancement_if_needed(
        self,
        snapshot: SubagentGraphCheckpointDeltaSnapshot,
    ) -> None:
        if not snapshot.delta_events:
            return
        delta_bytes = sum(
            len(event.canonical_payload_bytes) for event in snapshot.delta_events
        )
        byte_schedule_threshold = max(1, self.policy.checkpoint_max_delta_bytes // 2)
        if (
            len(snapshot.delta_events) < self.policy.checkpoint_every_events
            and delta_bytes < byte_schedule_threshold
        ):
            return
        sequence = snapshot.requested_through_sequence
        async with self._lock:
            if sequence in self._owners:
                return
            state, semantic_source, acceleration = (
                restore_subagent_graph_from_checkpoint(
                    snapshot=snapshot,
                    reducer_binding=self.reducer_binding,
                )
            )
            prepared = prepare_subagent_graph_checkpoint_from_restore(
                runtime_session_id=self.runtime_session.runtime_session_id,
                state=state,
                semantic_source=semantic_source,
                acceleration=acceleration,
                through_event=snapshot.delta_events[-1],
                reducer_binding=self.reducer_binding,
            )
            self._write_states[sequence] = SubagentGraphCheckpointWriteState.PENDING
            owner = asyncio.create_task(
                self._write_prepared_checkpoint(
                    prepared,
                    deadline_monotonic=monotonic() + self.read_timeout_seconds,
                )
            )
            self._owners[sequence] = owner
            owner.add_done_callback(
                lambda completed, through=sequence: (
                    self._owners.pop(through, None)
                    if self._owners.get(through) is completed
                    else None
                )
            )

    async def _await_relevant_writer(self, requested_through_sequence: int) -> bool:
        async with self._lock:
            owners = tuple(
                owner
                for through, owner in self._owners.items()
                if through <= requested_through_sequence
            )
        if not owners:
            return False
        await asyncio.gather(*(asyncio.shield(owner) for owner in owners))
        return True

    async def _write_prepared_checkpoint(
        self,
        prepared: PreparedSubagentGraphCheckpoint,
        *,
        deadline_monotonic: float,
    ) -> SubagentGraphCheckpointDeltaSnapshot:
        sequence = prepared.checkpoint.through_sequence
        self._write_states[sequence] = (
            SubagentGraphCheckpointWriteState.WRITING_ARTIFACT
        )
        physical = asyncio.create_task(
            asyncio.to_thread(self._write_artifact, prepared, deadline_monotonic)
        )
        self._physical_operations.add(physical)
        physical.add_done_callback(self._physical_operations.discard)
        try:
            await asyncio.shield(physical)
        except ArtifactContentConflict:
            self._write_states[sequence] = SubagentGraphCheckpointWriteState.CONFLICT
            self.runtime_session.latch_context_input_reconciliation_required()
            raise
        self._write_states[sequence] = SubagentGraphCheckpointWriteState.WRITING_EVENT
        stored = None
        write_error: BaseException | None = None
        try:
            result = await self.runtime_session.write_event(prepared.event)
            stored = next(
                (
                    event
                    for event in result.committed_events
                    if event.id == prepared.event.id
                ),
                None,
            )
        except BaseException as exc:
            write_error = exc
        if stored is None or stored.sequence is None:
            self._write_states[sequence] = SubagentGraphCheckpointWriteState.CONFIRMING
            if write_error is None:
                self._write_states[sequence] = SubagentGraphCheckpointWriteState.UNKNOWN
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise RuntimeError("checkpoint event write lost its terminal outcome")
            outcome = self.runtime_session.resolved_event_write_outcome(write_error)
            if outcome.status == "unknown":
                self._write_states[sequence] = SubagentGraphCheckpointWriteState.UNKNOWN
                raise write_error
            if outcome.status == "none":
                self._write_states[sequence] = SubagentGraphCheckpointWriteState.PENDING
                raise write_error
            stored = next(
                (
                    event
                    for event in outcome.committed_events
                    if event.id == prepared.event.id
                ),
                None,
            )
            if stored is None:
                self._write_states[sequence] = SubagentGraphCheckpointWriteState.UNKNOWN
                self.runtime_session.latch_event_commit_outcome_unknown()
                raise RuntimeError("checkpoint event commit outcome was not exact")
        raw_event = self.runtime_session.event_log.read_raw_events_by_id(
            (stored.id,), deadline_monotonic=deadline_monotonic
        )
        if len(raw_event) != 1:
            self._write_states[sequence] = SubagentGraphCheckpointWriteState.UNKNOWN
            self.runtime_session.latch_event_commit_outcome_unknown()
            raise RuntimeError("committed checkpoint raw envelope is unavailable")
        self._write_states[sequence] = SubagentGraphCheckpointWriteState.COMMITTED
        self._bootstrap_eligible = False
        return SubagentGraphCheckpointDeltaSnapshot(
            requested_through_sequence=sequence,
            checkpoint_event=raw_event[0],
            checkpoint_payload_bytes=prepared.artifact_payload_bytes,
            checkpoint_materialization_sequence=stored.sequence,
            delta_events=(),
            ledger_high_water_observed=stored.sequence,
            preferred_checkpoint_id=None,
            selected_checkpoint_id=prepared.checkpoint.checkpoint_id,
            rebased=False,
        )

    def _write_artifact(
        self,
        prepared: PreparedSubagentGraphCheckpoint,
        deadline_monotonic: float,
    ) -> None:
        artifact = prepared.artifact
        self.runtime_session.archive.put_text_if_absent_or_confirm_identical(
            artifact.artifact_id,
            prepared.artifact_payload_bytes.decode("utf-8"),
            session_id=self.runtime_session.runtime_session_id,
            run_id=None,
            media_type=artifact.media_type,
            semantic_metadata={
                "artifact_kind": "subagent_graph_checkpoint",
                "checkpoint_id": prepared.checkpoint.checkpoint_id,
                "content_sha256": artifact.content_sha256,
                "semantic_metadata_fingerprint": (
                    artifact.semantic_metadata_fingerprint
                ),
            },
            deadline_monotonic=deadline_monotonic,
        )

    async def drain_pending(self, *, deadline_monotonic: float) -> None:
        while True:
            owners = tuple(self._owners.values())
            physical = tuple(self._physical_operations)
            pending = (*owners, *physical)
            if not pending:
                blocked = tuple(
                    state
                    for state in self._write_states.values()
                    if state
                    in {
                        SubagentGraphCheckpointWriteState.CONFLICT,
                        SubagentGraphCheckpointWriteState.UNKNOWN,
                    }
                )
                if blocked:
                    raise SubagentGraphCheckpointWriteBlocked(
                        "subagent checkpoint writer requires reconciliation"
                    )
                return
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("subagent graph checkpoint drain timed out")
            done, _pending = await asyncio.wait(pending, timeout=remaining)
            if not done:
                raise TimeoutError("subagent graph checkpoint drain timed out")
            for task in done:
                task.result()

    def close_if_idle(self) -> None:
        if self._owners or self._physical_operations:
            raise RuntimeError("subagent graph checkpoint owners are still active")
        if any(
            state
            in {
                SubagentGraphCheckpointWriteState.CONFLICT,
                SubagentGraphCheckpointWriteState.UNKNOWN,
            }
            for state in self._write_states.values()
        ):
            raise SubagentGraphCheckpointWriteBlocked(
                "subagent graph checkpoint writer requires reconciliation"
            )

    def write_states(self) -> dict[int, str]:
        return {
            sequence: state.value
            for sequence, state in sorted(self._write_states.items())
        }


def reducer_contract_of(
    service: SubagentGraphCheckpointService,
) -> SubagentGraphReducerContractFact:
    return service.reducer_binding.contract
