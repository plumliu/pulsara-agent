"""Pure reducer for the durable MCP input-required lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Sequence

from pulsara_agent.event import (
    AgentEvent,
    McpInputRequiredBindingChangedEvent,
    McpInputRequiredExpiredEvent,
    McpInputRequiredInteractionClosedEvent,
    McpInputRequiredResolutionSubmittedEvent,
    McpInputRequiredResumeFailedEvent,
    RunEndEvent,
    RunStartEvent,
    ToolExecutionSuspendedEvent,
    ToolResultEndEvent,
)
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)


McpInputRequiredLifecycleStatus = Literal[
    "suspended",
    "resolution_submitted",
    "resume_failed",
    "terminal",
    "closed",
    "run_ended",
]


@dataclass(frozen=True, slots=True)
class McpInputRequiredLifecycleRecord:
    interaction_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    tool_call_id: str
    tool_name: str
    round_count: int
    status: McpInputRequiredLifecycleStatus
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension_fact_fingerprint: str
    latest_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    ) = None
    latest_resume_failed_event_reference: ContextEventReferenceFact | None = None
    terminal_tool_result_event_reference: ContextEventReferenceFact | None = None
    terminal_disposition_event_reference: ContextEventReferenceFact | None = None
    closure_event_reference: ContextEventReferenceFact | None = None
    run_end_event_reference: ContextEventReferenceFact | None = None

    @property
    def interaction_remains_open(self) -> bool:
        return self.status in {
            "suspended",
            "resolution_submitted",
            "resume_failed",
        }


class McpInputRequiredLifecycleStore:
    """Incrementally validates exact suspension/resolution/terminal joins."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        events: Sequence[AgentEvent] = (),
        through_sequence: int = 0,
        capture_terminal_snapshots: bool = False,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.through_sequence = 0
        self._capture_terminal_snapshots = capture_terminal_snapshots
        self._records: dict[str, McpInputRequiredLifecycleRecord] = {}
        self._terminal_snapshots: list[McpInputRequiredLifecycleRecord] = []
        self._events_by_id: dict[str, AgentEvent] = {}
        self._run_start_event_ids: dict[str, str] = {}
        self._run_closure_references: dict[str, ContextEventReferenceFact] = {}
        self._event_interaction_ids: dict[str, str] = {}
        if events:
            self.apply_committed(tuple(events))
        self.through_sequence = max(self.through_sequence, through_sequence)

    @classmethod
    def from_sparse_bootstrap(
        cls,
        events: Sequence[AgentEvent],
        *,
        runtime_session_id: str,
        through_sequence: int,
    ) -> "McpInputRequiredLifecycleStore":
        return cls(
            runtime_session_id=runtime_session_id,
            events=events,
            through_sequence=through_sequence,
        )

    def records(self) -> tuple[McpInputRequiredLifecycleRecord, ...]:
        return tuple(
            sorted(
                (*self._terminal_snapshots, *self._records.values()),
                key=lambda item: (
                    item.source_suspension_event_reference.sequence,
                    item.interaction_id,
                ),
            )
        )

    def record(self, interaction_id: str) -> McpInputRequiredLifecycleRecord | None:
        return self._records.get(interaction_id)

    def active_for_run(
        self,
        run_id: str,
    ) -> tuple[McpInputRequiredLifecycleRecord, ...]:
        return tuple(
            item
            for item in self.records()
            if item.run_id == run_id and item.interaction_remains_open
        )

    def validate_next_batch(self, events: Sequence[AgentEvent]) -> None:
        if not self._relevant_events(events):
            return
        clone = McpInputRequiredLifecycleStore(
            runtime_session_id=self.runtime_session_id,
            through_sequence=self.through_sequence,
            capture_terminal_snapshots=self._capture_terminal_snapshots,
        )
        clone._records = dict(self._records)
        clone._terminal_snapshots = list(self._terminal_snapshots)
        clone._events_by_id = dict(self._events_by_id)
        clone._run_start_event_ids = dict(self._run_start_event_ids)
        clone._run_closure_references = dict(self._run_closure_references)
        clone._event_interaction_ids = dict(self._event_interaction_ids)
        candidate_sequence = self.through_sequence
        sequenced: list[AgentEvent] = []
        for event in events:
            if event.sequence is None:
                candidate_sequence += 1
                sequenced.append(event.model_copy(update={"sequence": candidate_sequence}))
            else:
                candidate_sequence = max(candidate_sequence, event.sequence)
                sequenced.append(event)
        clone.apply_committed(tuple(sequenced))

    def rebuild(self, events: tuple[AgentEvent, ...]) -> None:
        self._records.clear()
        self._terminal_snapshots.clear()
        self._events_by_id.clear()
        self._run_start_event_ids.clear()
        self._run_closure_references.clear()
        self._event_interaction_ids.clear()
        self.through_sequence = 0
        self.apply_committed(events)

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        if not events:
            return
        relevant = self._relevant_events(events)
        if not relevant:
            self.through_sequence = max(
                self.through_sequence,
                max(event.sequence or 0 for event in events),
            )
            return
        batch_by_id = {event.id: event for event in relevant}
        if len(batch_by_id) != len(relevant):
            raise ValueError("MCP lifecycle batch contains duplicate event IDs")
        known = {**self._events_by_id, **batch_by_id}
        disposition_terminal_ids = {
            event.terminal_tool_result_event_identity.event_id
            for event in relevant
            if isinstance(
                event,
                (McpInputRequiredExpiredEvent, McpInputRequiredBindingChangedEvent),
            )
        }
        closure_terminal_ids = {
            event.terminal_tool_result_event_identity.event_id
            for event in relevant
            if isinstance(event, McpInputRequiredInteractionClosedEvent)
        }
        for event in relevant:
            if event.sequence is None:
                raise ValueError("MCP lifecycle reducer requires stored events")
            retire_interaction_id: str | None = None
            if isinstance(event, RunStartEvent):
                existing = self._run_start_event_ids.get(event.run_id)
                if existing is not None and existing != event.id:
                    raise ValueError("MCP lifecycle observed duplicate RunStart")
                self._run_start_event_ids[event.run_id] = event.id
            elif isinstance(event, ToolExecutionSuspendedEvent):
                self._apply_suspension(event, known)
            elif isinstance(event, McpInputRequiredResolutionSubmittedEvent):
                self._apply_resolution(event, known)
            elif isinstance(event, McpInputRequiredResumeFailedEvent):
                self._apply_resume_failed(event, known)
            elif isinstance(event, ToolResultEndEvent):
                retire_interaction_id = self._apply_tool_terminal(
                    event,
                    terminal_has_companion=(
                        event.id in disposition_terminal_ids
                        or event.id in closure_terminal_ids
                    ),
                    known=known,
                )
            elif isinstance(
                event,
                (McpInputRequiredExpiredEvent, McpInputRequiredBindingChangedEvent),
            ):
                retire_interaction_id = self._apply_terminal_disposition(event, known)
            elif isinstance(event, McpInputRequiredInteractionClosedEvent):
                retire_interaction_id = self._apply_closure(event, known)
            elif isinstance(event, RunEndEvent):
                self._apply_run_end(event)
                self._retire_run(event.run_id)
                continue
            self._events_by_id[event.id] = event.model_copy(deep=True)
            interaction_id = self._interaction_id_for_event(event, known)
            if interaction_id is not None:
                self._event_interaction_ids[event.id] = interaction_id
            if retire_interaction_id is not None:
                self._retire_interaction(retire_interaction_id)
        self.through_sequence = max(
            self.through_sequence,
            max(
                event.sequence or 0
                for event in events
            ),
        )

    def _relevant_events(
        self,
        events: Sequence[AgentEvent],
    ) -> tuple[AgentEvent, ...]:
        candidate_run_ids = {
            event.run_id for event in events if isinstance(event, RunStartEvent)
        }
        active_run_ids = (
            set(self._run_start_event_ids)
            | candidate_run_ids
            | {record.run_id for record in self._records.values()}
            | {record.run_id for record in self._terminal_snapshots}
        )
        return tuple(
            event
            for event in events
            if self._is_mcp_relevant(event)
            or isinstance(event, RunStartEvent)
            or (isinstance(event, RunEndEvent) and event.run_id in active_run_ids)
        )

    @staticmethod
    def _is_mcp_relevant(event: AgentEvent) -> bool:
        if isinstance(event, ToolResultEndEvent):
            return event.mcp_input_required_terminal_source is not None
        return isinstance(
            event,
            (
                ToolExecutionSuspendedEvent,
                McpInputRequiredResolutionSubmittedEvent,
                McpInputRequiredResumeFailedEvent,
                McpInputRequiredExpiredEvent,
                McpInputRequiredBindingChangedEvent,
                McpInputRequiredInteractionClosedEvent,
            ),
        )

    def _interaction_id_for_event(
        self,
        event: AgentEvent,
        known: dict[str, AgentEvent],
    ) -> str | None:
        if isinstance(event, ToolExecutionSuspendedEvent):
            return event.suspension.interaction.interaction_id
        if isinstance(event, McpInputRequiredResolutionSubmittedEvent):
            return event.source.interaction.interaction_id
        reference: ContextEventReferenceFact | None = None
        if isinstance(event, McpInputRequiredResumeFailedEvent):
            reference = event.resolution_submitted_event_reference
        elif isinstance(
            event,
            (McpInputRequiredExpiredEvent, McpInputRequiredBindingChangedEvent),
        ):
            reference = event.resolution_submitted_event_reference
        elif isinstance(event, ToolResultEndEvent):
            source = event.mcp_input_required_terminal_source
            reference = (
                source.source_suspension_event_reference
                if source is not None
                else None
            )
        elif isinstance(event, McpInputRequiredInteractionClosedEvent):
            reference = event.source_suspension_event_reference
        if reference is None:
            return None
        source = known.get(reference.event_id)
        if isinstance(source, McpInputRequiredResolutionSubmittedEvent):
            return source.source.interaction.interaction_id
        if isinstance(source, ToolExecutionSuspendedEvent):
            return source.suspension.interaction.interaction_id
        return None

    def _retire_interaction(self, interaction_id: str) -> None:
        record = self._records.pop(interaction_id, None)
        if self._capture_terminal_snapshots and record is not None:
            self._terminal_snapshots.append(record)
        event_ids = tuple(
            event_id
            for event_id, owner in self._event_interaction_ids.items()
            if owner == interaction_id
        )
        for event_id in event_ids:
            self._event_interaction_ids.pop(event_id, None)
            self._events_by_id.pop(event_id, None)

    def _retire_run(self, run_id: str) -> None:
        for interaction_id, record in tuple(self._records.items()):
            if record.run_id == run_id:
                self._retire_interaction(interaction_id)
        start_event_id = self._run_start_event_ids.pop(run_id, None)
        if start_event_id is not None:
            self._events_by_id.pop(start_event_id, None)
        self._run_closure_references.pop(run_id, None)

    def _reference(self, event: AgentEvent) -> ContextEventReferenceFact:
        return event_reference_from_stored(
            event,
            runtime_session_id=self.runtime_session_id,
        )

    def _require_exact(
        self,
        reference: ContextEventReferenceFact,
        *,
        expected_type: type[AgentEvent] | tuple[type[AgentEvent], ...],
        known: dict[str, AgentEvent],
    ) -> AgentEvent:
        event = known.get(reference.event_id)
        if event is None or not isinstance(event, expected_type):
            raise ValueError("MCP lifecycle reference cannot be resolved")
        if self._reference(event) != reference:
            raise ValueError("MCP lifecycle event reference is not exact")
        return event

    def _record_for_suspension(
        self,
        reference: ContextEventReferenceFact,
        *,
        known: dict[str, AgentEvent],
    ) -> McpInputRequiredLifecycleRecord:
        suspension = self._require_exact(
            reference,
            expected_type=ToolExecutionSuspendedEvent,
            known=known,
        )
        assert isinstance(suspension, ToolExecutionSuspendedEvent)
        interaction_id = suspension.suspension.interaction.interaction_id
        record = self._records.get(interaction_id)
        if (
            record is None
            or record.source_suspension_event_reference != reference
        ):
            raise ValueError("MCP lifecycle suspension owner is not active")
        return record

    def _apply_suspension(
        self,
        event: ToolExecutionSuspendedEvent,
        known: dict[str, AgentEvent],
    ) -> None:
        suspension = event.suspension
        interaction = suspension.interaction
        reference = self._reference(event)
        previous = self._records.get(interaction.interaction_id)
        predecessor = suspension.predecessor_resolution_submitted_event_reference
        if interaction.round_count == 1:
            if previous is not None or predecessor is not None:
                raise ValueError("first MCP suspension conflicts with prior lifecycle")
        else:
            if (
                previous is None
                or predecessor is None
                or previous.latest_resolution_submitted_event_reference != predecessor
                or previous.status != "resolution_submitted"
            ):
                raise ValueError("next MCP suspension lacks its exact predecessor")
            self._require_exact(
                predecessor,
                expected_type=McpInputRequiredResolutionSubmittedEvent,
                known=known,
            )
        self._records[interaction.interaction_id] = (
            McpInputRequiredLifecycleRecord(
                interaction_id=interaction.interaction_id,
                runtime_session_id=self.runtime_session_id,
                run_id=event.run_id,
                turn_id=event.turn_id,
                reply_id=event.reply_id,
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
                round_count=interaction.round_count,
                status="suspended",
                source_suspension_event_reference=reference,
                source_suspension_fact_fingerprint=(
                    suspension.suspension_fact_fingerprint
                ),
            )
        )

    def _apply_resolution(
        self,
        event: McpInputRequiredResolutionSubmittedEvent,
        known: dict[str, AgentEvent],
    ) -> None:
        source = event.source
        record = self._record_for_suspension(
            source.source_suspension_event_reference,
            known=known,
        )
        suspension = self._require_exact(
            source.source_suspension_event_reference,
            expected_type=ToolExecutionSuspendedEvent,
            known=known,
        )
        assert isinstance(suspension, ToolExecutionSuspendedEvent)
        if (
            source.source_suspension_fact_fingerprint
            != suspension.suspension.suspension_fact_fingerprint
            or source.interaction != suspension.suspension.interaction
            or source.binding_identity != suspension.suspension.binding_identity
            or source.pending_lease_reservation
            != suspension.suspension.pending_lease_reservation
            or source.request_envelope_semantic_fingerprint
            != suspension.suspension.request_envelope.request_envelope_semantic_fingerprint
            or source.rollout_reservation_id
            != suspension.suspension.rollout_reservation_id
            or source.rollout_reservation_fingerprint
            != suspension.suspension.rollout_reservation_fingerprint
            or source.source_mcp_installation_id
            != suspension.suspension.source_mcp_installation_id
            or source.durable_deadline_utc
            != suspension.suspension.durable_deadline_utc
            or source.deadline_policy_fingerprint
            != suspension.suspension.deadline_policy_fingerprint
            or source.predecessor_resolution_submitted_event_reference
            != suspension.suspension.predecessor_resolution_submitted_event_reference
            or event.run_id != record.run_id
        ):
            raise ValueError("MCP resolution source authority drifted")
        run_start = self._require_exact(
            source.original_run_start_event_reference,
            expected_type=RunStartEvent,
            known=known,
        )
        if run_start.run_id != event.run_id:
            raise ValueError("MCP resolution cites the wrong RunStart")
        attempt = event.attempt
        if record.status == "suspended":
            if (
                attempt.attempt_ordinal != 1
                or attempt.predecessor_resolution_submitted_event_reference
                is not None
                or attempt.predecessor_resume_failed_event_reference is not None
            ):
                raise ValueError("first MCP resolution attempt is not canonical")
        elif record.status == "resume_failed":
            if (
                record.latest_resolution_submitted_event_reference
                != attempt.predecessor_resolution_submitted_event_reference
                or record.latest_resume_failed_event_reference
                != attempt.predecessor_resume_failed_event_reference
            ):
                raise ValueError("MCP resolution retry predecessor drifted")
            previous = self._require_exact(
                attempt.predecessor_resolution_submitted_event_reference,
                expected_type=McpInputRequiredResolutionSubmittedEvent,
                known=known,
            )
            assert isinstance(previous, McpInputRequiredResolutionSubmittedEvent)
            if attempt.attempt_ordinal != previous.attempt.attempt_ordinal + 1:
                raise ValueError("MCP resolution retry ordinal is not contiguous")
        else:
            raise ValueError("MCP resolution submitted from an invalid lifecycle state")
        self._records[record.interaction_id] = replace(
            record,
            status="resolution_submitted",
            latest_resolution_submitted_event_reference=self._reference(event),
            latest_resume_failed_event_reference=None,
        )

    def _apply_resume_failed(
        self,
        event: McpInputRequiredResumeFailedEvent,
        known: dict[str, AgentEvent],
    ) -> None:
        resolution = self._require_exact(
            event.resolution_submitted_event_reference,
            expected_type=McpInputRequiredResolutionSubmittedEvent,
            known=known,
        )
        assert isinstance(resolution, McpInputRequiredResolutionSubmittedEvent)
        record = self._record_for_suspension(
            resolution.source.source_suspension_event_reference,
            known=known,
        )
        if (
            record.status != "resolution_submitted"
            or record.latest_resolution_submitted_event_reference
            != event.resolution_submitted_event_reference
        ):
            raise ValueError("MCP resume failure does not consume the active attempt")
        self._records[record.interaction_id] = replace(
            record,
            status="resume_failed",
            latest_resume_failed_event_reference=self._reference(event),
        )

    def _apply_tool_terminal(
        self,
        event: ToolResultEndEvent,
        *,
        terminal_has_companion: bool,
        known: dict[str, AgentEvent],
    ) -> str | None:
        source = event.mcp_input_required_terminal_source
        if source is None:
            return
        record = self._record_for_suspension(
            source.source_suspension_event_reference,
            known=known,
        )
        resolution = source.source_resolution_submitted_event_reference
        if resolution is not None:
            self._require_exact(
                resolution,
                expected_type=McpInputRequiredResolutionSubmittedEvent,
                known=known,
            )
            if record.latest_resolution_submitted_event_reference != resolution:
                raise ValueError("MCP terminal consumes a stale resolution")
        elif record.status not in {"suspended", "resume_failed"}:
            raise ValueError("MCP terminal without resolution has no active suspension")
        self._records[record.interaction_id] = replace(
            record,
            status="resolution_submitted" if terminal_has_companion else "terminal",
            terminal_tool_result_event_reference=self._reference(event),
        )
        return None if terminal_has_companion else record.interaction_id

    def _apply_terminal_disposition(
        self,
        event: McpInputRequiredExpiredEvent | McpInputRequiredBindingChangedEvent,
        known: dict[str, AgentEvent],
    ) -> str:
        resolution = self._require_exact(
            event.resolution_submitted_event_reference,
            expected_type=McpInputRequiredResolutionSubmittedEvent,
            known=known,
        )
        assert isinstance(resolution, McpInputRequiredResolutionSubmittedEvent)
        record = self._record_for_suspension(
            resolution.source.source_suspension_event_reference,
            known=known,
        )
        terminal = known.get(event.terminal_tool_result_event_identity.event_id)
        if not isinstance(terminal, ToolResultEndEvent) or stable_event_identity(
            terminal,
            runtime_session_id=self.runtime_session_id,
        ) != event.terminal_tool_result_event_identity:
            raise ValueError("MCP terminal disposition ToolResult identity drifted")
        if (
            record.latest_resolution_submitted_event_reference
            != event.resolution_submitted_event_reference
            or record.terminal_tool_result_event_reference != self._reference(terminal)
        ):
            raise ValueError("MCP terminal disposition join drifted")
        self._records[record.interaction_id] = replace(
            record,
            status="terminal",
            terminal_disposition_event_reference=self._reference(event),
        )
        return record.interaction_id

    def _apply_closure(
        self,
        event: McpInputRequiredInteractionClosedEvent,
        known: dict[str, AgentEvent],
    ) -> str:
        record = self._record_for_suspension(
            event.source_suspension_event_reference,
            known=known,
        )
        if (
            event.source_resolution_submitted_event_reference
            != record.latest_resolution_submitted_event_reference
            or event.source_resume_failed_event_reference
            != record.latest_resume_failed_event_reference
        ):
            raise ValueError("MCP closure predecessor chain drifted")
        terminal = known.get(event.terminal_tool_result_event_identity.event_id)
        if not isinstance(terminal, ToolResultEndEvent):
            raise ValueError("MCP closure lacks its terminal ToolResult")
        if stable_event_identity(
            terminal,
            runtime_session_id=self.runtime_session_id,
        ) != event.terminal_tool_result_event_identity:
            raise ValueError("MCP closure terminal ToolResult identity drifted")
        if record.terminal_tool_result_event_reference != self._reference(terminal):
            raise ValueError("MCP closure terminal ToolResult was not adopted")
        self._records[record.interaction_id] = replace(
            record,
            status="closed",
            closure_event_reference=self._reference(event),
        )
        closure_reference = self._reference(event)
        existing = self._run_closure_references.get(record.run_id)
        if existing is not None and existing != closure_reference:
            raise ValueError("Run cannot join multiple MCP closures")
        self._run_closure_references[record.run_id] = closure_reference
        return record.interaction_id

    def _apply_run_end(self, event: RunEndEvent) -> None:
        records = tuple(
            item for item in self._records.values() if item.run_id == event.run_id
        )
        active = tuple(item for item in records if item.interaction_remains_open)
        if active:
            raise ValueError("RunEnd bypasses an active MCP suspension")
        closed_references = tuple(
            item.closure_event_reference
            for item in records
            if item.status == "closed" and item.closure_event_reference is not None
        )
        if len(closed_references) > 1:
            raise ValueError("RunEnd cannot join multiple MCP closures")
        expected_closure = self._run_closure_references.get(event.run_id) or (
            closed_references[0] if closed_references else None
        )
        if event.mcp_input_required_closure_event_reference != expected_closure:
            raise ValueError("RunEnd does not exact-join MCP closure")
        if self._capture_terminal_snapshots:
            run_end_reference = self._reference(event)
            self._terminal_snapshots = [
                (
                    replace(
                        record,
                        status="run_ended",
                        run_end_event_reference=run_end_reference,
                    )
                    if record.run_id == event.run_id
                    else record
                )
                for record in self._terminal_snapshots
            ]


__all__ = [
    "McpInputRequiredLifecycleRecord",
    "McpInputRequiredLifecycleStatus",
    "McpInputRequiredLifecycleStore",
]
