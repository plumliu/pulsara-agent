"""Opaque publication-latch terminal-maintenance capabilities.

The durable event ledger remains the authority.  These handles only prove that
one already-latched RuntimeSession admitted one exact terminal batch while the
ordinary mutation gate was closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, NoReturn, Sequence, TypeAlias
from uuid import uuid4

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log.protocol import EventLogTransactionCompanion
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase


PublicationTerminalMaintenanceOwnerKind: TypeAlias = Literal[
    "mcp_interaction_closure_bundle",
    "mcp_publication_latched_run_termination_bundle",
    "mandatory_audit_publication_latched_run_termination_bundle",
    "compaction_started_publication_failed_bundle",
    "compaction_publication_latched_run_termination_bundle",
]

_OWNER_KINDS: frozenset[str] = frozenset(
    {
        "mcp_interaction_closure_bundle",
        "mcp_publication_latched_run_termination_bundle",
        "mandatory_audit_publication_latched_run_termination_bundle",
        "compaction_started_publication_failed_bundle",
        "compaction_publication_latched_run_termination_bundle",
    }
)


class PublicationTerminalMaintenanceLeaseIdentity(FrozenRuntimeStateBase):
    lease_id: str
    runtime_session_id: str
    publication_latch_generation: int
    owner_kind: PublicationTerminalMaintenanceOwnerKind
    ordered_candidate_event_ids: tuple[str, ...]
    ordered_candidate_payload_fingerprints: tuple[str, ...]
    transaction_companion_fingerprint: str | None
    exact_ordered_batch_fingerprint: str
    terminal_deadline_monotonic: float


class PublicationTerminalMaintenanceLease:
    """Uncopyable borrower handle issued only by a RuntimeSession coordinator."""

    __slots__ = (
        "__identity",
        "__issued_attempt_generation",
        "__issuer_token",
        "__valid",
    )

    def __init__(
        self,
        *,
        _issuer_token: object,
        identity: PublicationTerminalMaintenanceLeaseIdentity,
    ) -> None:
        if _issuer_token is None:
            raise TypeError("publication maintenance lease requires its issuer")
        self.__identity = identity
        self.__issued_attempt_generation = 0
        self.__issuer_token = _issuer_token
        self.__valid = True

    @property
    def identity(self) -> PublicationTerminalMaintenanceLeaseIdentity:
        return self.__identity

    @property
    def issued_attempt_generation(self) -> int:
        return self.__issued_attempt_generation

    @property
    def is_valid(self) -> bool:
        return self.__valid

    def _advance(self, *, issuer_token: object, generation: int) -> None:
        if issuer_token is not self.__issuer_token:
            raise PermissionError("publication maintenance lease issuer mismatch")
        self.__issued_attempt_generation = generation

    def _invalidate(self, *, issuer_token: object) -> None:
        if issuer_token is not self.__issuer_token:
            raise PermissionError("publication maintenance lease issuer mismatch")
        self.__valid = False

    def __copy__(self) -> NoReturn:
        raise TypeError("publication maintenance lease is not copyable")

    def __deepcopy__(self, memo: object) -> NoReturn:
        del memo
        raise TypeError("publication maintenance lease is not copyable")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del protocol
        raise TypeError("publication maintenance lease is not serializable")


@dataclass(frozen=True, slots=True)
class PublicationTerminalMaintenanceAttempt:
    lease_id: str
    attempt_generation: int


@dataclass(slots=True)
class _LeaseRecord:
    handle: PublicationTerminalMaintenanceLease
    state: Literal[
        "issued",
        "in_flight",
        "consumed",
        "reconciliation_required",
        "invalidated",
    ]
    attempt_generation: int


def transaction_companion_fingerprint(
    companion: EventLogTransactionCompanion | None,
) -> str | None:
    if companion is None:
        return None
    declared = getattr(companion, "transaction_companion_fingerprint", None)
    if isinstance(declared, str) and declared.startswith("sha256:"):
        return declared
    return context_fingerprint(
        "event-log-transaction-companion-process-identity:v1",
        {
            "module": type(companion).__module__,
            "qualname": type(companion).__qualname__,
            "object_identity": id(companion),
        },
    )


def _batch_identity(
    events: Sequence[AgentEvent],
    *,
    transaction_companion: EventLogTransactionCompanion | None,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None, str]:
    candidates = tuple(
        freeze_event_write_candidate(event.model_copy(update={"sequence": None}))
        for event in events
    )
    ids = tuple(item.event_id for item in candidates)
    payloads = tuple(item.payload_fingerprint for item in candidates)
    companion_fingerprint = transaction_companion_fingerprint(transaction_companion)
    fingerprint = context_fingerprint(
        "publication-terminal-maintenance-exact-batch:v1",
        {
            "ordered_candidate_event_ids": ids,
            "ordered_candidate_payload_fingerprints": payloads,
            "transaction_companion_fingerprint": companion_fingerprint,
        },
    )
    return ids, payloads, companion_fingerprint, fingerprint


def _validate_owner_batch(
    owner_kind: PublicationTerminalMaintenanceOwnerKind,
    events: Sequence[AgentEvent],
) -> None:
    from pulsara_agent.event import (
        ContextCompactionFailedEvent,
        ContextWindowClosedEvent,
        McpInputRequiredInteractionClosedEvent,
        RolloutBudgetAccountClosedEvent,
        RunEndEvent,
        ToolResultEndEvent,
    )

    if owner_kind == "compaction_started_publication_failed_bundle":
        if len(events) != 1 or not isinstance(
            events[0], ContextCompactionFailedEvent
        ):
            raise ValueError(
                "started-publication maintenance requires one compaction Failed"
            )
        failed = events[0]
        if (
            failed.failure_stage != "started_publication"
            or failed.started_event_id is None
            or failed.summarizer_call is not None
            or failed.summarizer_context_id is not None
            or failed.summarizer_usage is not None
        ):
            raise ValueError("started-publication maintenance payload drifted")
        return
    if owner_kind == "mcp_interaction_closure_bundle":
        closures = tuple(
            event
            for event in events
            if isinstance(event, McpInputRequiredInteractionClosedEvent)
        )
        tool_terminals = tuple(
            event for event in events if isinstance(event, ToolResultEndEvent)
        )
        if len(closures) != 1 or len(tool_terminals) != 1:
            raise ValueError(
                "MCP closure maintenance requires one closure and ToolResult terminal"
            )
        if (
            closures[0].terminal_tool_result_event_identity.event_id
            != tool_terminals[0].id
        ):
            raise ValueError("MCP closure maintenance terminal identity drifted")
        return
    run_ends = tuple(event for event in events if isinstance(event, RunEndEvent))
    window_closes = tuple(
        event for event in events if isinstance(event, ContextWindowClosedEvent)
    )
    account_closes = tuple(
        event
        for event in events
        if isinstance(event, RolloutBudgetAccountClosedEvent)
    )
    if len(run_ends) != 1 or len(window_closes) != 1 or len(account_closes) != 1:
        raise ValueError(
            "publication-latched RunEnd maintenance requires exact close facts"
        )
    termination = run_ends[0].publication_latched_termination
    if termination is None:
        raise ValueError("publication-latched RunEnd maintenance lacks authority")
    expected_reason = {
        "compaction_publication_latched_run_termination_bundle": (
            "compaction_publication_unavailable"
        ),
        "mandatory_audit_publication_latched_run_termination_bundle": (
            "mandatory_runtime_audit_publication_unavailable"
        ),
    }.get(owner_kind)
    if expected_reason is not None and termination.reason != expected_reason:
        raise ValueError("publication-latched RunEnd owner/reason mismatch")
    if (
        owner_kind == "mcp_publication_latched_run_termination_bundle"
        and not termination.reason.startswith("mcp_")
    ):
        raise ValueError("MCP RunEnd maintenance requires MCP publication authority")


def validate_publication_latched_run_termination_authority(
    run_end: AgentEvent,
    *,
    runtime_session_id: str,
    resolve_event: Callable[[str], AgentEvent | None],
) -> None:
    """Exact-rebind one publication-latched RunEnd to its durable source facts."""

    from pulsara_agent.event import (
        ContextCompactionCompletedEvent,
        ContextCompactionFailedEvent,
        ContextCompactionRequestedEvent,
        ContextCompactionStartedEvent,
        McpInputRequiredBindingChangedEvent,
        McpInputRequiredExpiredEvent,
        McpInputRequiredInteractionClosedEvent,
        McpInputRequiredResolutionSubmittedEvent,
        McpInputRequiredResumeFailedEvent,
        MidTurnContextCompactionSkippedEvent,
        RunEndEvent,
        ToolExecutionSuspendedEvent,
        ToolResultEndEvent,
        ToolResultEvidenceProjectionFailedEvent,
    )
    from pulsara_agent.runtime.context_input.event_slice import (
        event_reference_from_stored,
    )
    from pulsara_agent.llm.terminal_projection import stable_event_identity

    if not isinstance(run_end, RunEndEvent):
        raise TypeError("publication termination authority requires RunEnd")
    termination = run_end.publication_latched_termination
    if termination is None:
        return

    def require_exact_reference(
        reference: object,
        *,
        expected_type: type[AgentEvent] | tuple[type[AgentEvent], ...],
    ) -> AgentEvent:
        if not hasattr(reference, "runtime_session_id"):
            raise ValueError("publication termination nested reference is invalid")
        if reference.runtime_session_id != runtime_session_id:
            raise ValueError("publication termination source crosses runtime ledger")
        stored = resolve_event(reference.event_id)
        if (
            stored is None
            or stored.sequence is None
            or not isinstance(stored, expected_type)
            or event_reference_from_stored(
                stored,
                runtime_session_id=runtime_session_id,
            )
            != reference
        ):
            raise ValueError("publication termination source reference is not exact")
        if stored.run_id != run_end.run_id:
            raise ValueError("publication termination source belongs to another run")
        return stored

    def require_terminal_identity(
        terminal: ToolResultEndEvent,
        identity: object,
    ) -> None:
        if stable_event_identity(
            terminal,
            runtime_session_id=runtime_session_id,
        ) != identity:
            raise ValueError("MCP publication terminal identity is not exact")

    def validate_terminal_source(terminal: ToolResultEndEvent) -> None:
        source = terminal.mcp_input_required_terminal_source
        if source is None:
            raise ValueError("MCP publication ToolResult lacks terminal source")
        require_exact_reference(
            source.source_suspension_event_reference,
            expected_type=ToolExecutionSuspendedEvent,
        )
        resolution_reference = source.source_resolution_submitted_event_reference
        if resolution_reference is not None:
            resolution = require_exact_reference(
                resolution_reference,
                expected_type=McpInputRequiredResolutionSubmittedEvent,
            )
            assert isinstance(resolution, McpInputRequiredResolutionSubmittedEvent)
            if (
                resolution.source.source_suspension_event_reference
                != source.source_suspension_event_reference
            ):
                raise ValueError("MCP publication terminal source chain drifted")

    sources: list[AgentEvent] = []
    for reference in termination.source_event_references:
        sources.append(
            require_exact_reference(reference, expected_type=AgentEvent)
        )

    reason = termination.reason
    if reason == "mandatory_runtime_audit_publication_unavailable":
        allowed = (
            ContextCompactionRequestedEvent,
            MidTurnContextCompactionSkippedEvent,
            ToolResultEvidenceProjectionFailedEvent,
        )
        if len(sources) != 1 or not isinstance(sources[0], allowed):
            raise ValueError("mandatory audit publication authority is invalid")
        return
    if reason == "compaction_publication_unavailable":
        starts = tuple(
            event for event in sources if isinstance(event, ContextCompactionStartedEvent)
        )
        terminals = tuple(
            event
            for event in sources
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
        )
        if len(terminals) != 1 or len(starts) > 1 or len(sources) not in {1, 2}:
            raise ValueError("compaction publication authority has invalid cardinality")
        terminal = terminals[0]
        if starts:
            started = starts[0]
            if (
                len(sources) != 2
                or terminal.started_event_id != started.id
                or terminal.id != started.terminal_event_id
                or terminal.compaction_id != started.compaction_id
            ):
                raise ValueError("compaction publication authority pairing drifted")
        elif len(sources) != 1 or terminal.started_event_id is not None:
            raise ValueError(
                "pre-Started compaction publication authority is not canonical"
            )
        return

    mcp_allowed = (
        ToolExecutionSuspendedEvent,
        McpInputRequiredResumeFailedEvent,
        McpInputRequiredExpiredEvent,
        McpInputRequiredBindingChangedEvent,
        McpInputRequiredInteractionClosedEvent,
        ToolResultEndEvent,
    )
    if not sources or any(not isinstance(event, mcp_allowed) for event in sources):
        raise ValueError("MCP publication authority contains another event domain")
    terminals = tuple(
        event for event in sources if isinstance(event, ToolResultEndEvent)
    )
    for terminal in terminals:
        validate_terminal_source(terminal)
    resume_failures = tuple(
        event for event in sources if isinstance(event, McpInputRequiredResumeFailedEvent)
    )
    for failure in resume_failures:
        require_exact_reference(
            failure.resolution_submitted_event_reference,
            expected_type=McpInputRequiredResolutionSubmittedEvent,
        )
    if reason == "mcp_active_interaction_publication_unavailable":
        active = tuple(
            event
            for event in sources
            if isinstance(
                event,
                (ToolExecutionSuspendedEvent, McpInputRequiredResumeFailedEvent),
            )
        )
        closures = tuple(
            event
            for event in sources
            if isinstance(event, McpInputRequiredInteractionClosedEvent)
        )
        if not active or len(closures) != 1 or len(terminals) != 1:
            raise ValueError("active MCP publication authority is incomplete")
        closure = closures[0]
        terminal = terminals[0]
        terminal_source = terminal.mcp_input_required_terminal_source
        assert terminal_source is not None
        require_terminal_identity(
            terminal,
            closure.terminal_tool_result_event_identity,
        )
        if (
            closure.source_suspension_event_reference
            != terminal_source.source_suspension_event_reference
            or closure.source_resolution_submitted_event_reference
            != terminal_source.source_resolution_submitted_event_reference
        ):
            raise ValueError("active MCP closure/ToolResult source chain drifted")
        require_exact_reference(
            closure.source_suspension_event_reference,
            expected_type=ToolExecutionSuspendedEvent,
        )
        if closure.source_resolution_submitted_event_reference is not None:
            require_exact_reference(
                closure.source_resolution_submitted_event_reference,
                expected_type=McpInputRequiredResolutionSubmittedEvent,
            )
        if closure.source_resume_failed_event_reference is not None:
            require_exact_reference(
                closure.source_resume_failed_event_reference,
                expected_type=McpInputRequiredResumeFailedEvent,
            )
        return
    if reason == "mcp_terminal_disposition_publication_unavailable":
        dispositions = tuple(
            event
            for event in sources
            if isinstance(
                event,
                (McpInputRequiredExpiredEvent, McpInputRequiredBindingChangedEvent),
            )
        )
        if len(sources) != 2 or len(dispositions) != 1 or len(terminals) != 1:
            raise ValueError("MCP disposition publication authority is incomplete")
        disposition = dispositions[0]
        terminal = terminals[0]
        terminal_source = terminal.mcp_input_required_terminal_source
        assert terminal_source is not None
        require_terminal_identity(
            terminal,
            disposition.terminal_tool_result_event_identity,
        )
        if (
            terminal_source.source_resolution_submitted_event_reference
            != disposition.resolution_submitted_event_reference
        ):
            raise ValueError("MCP disposition/ToolResult resolution drifted")
        require_exact_reference(
            disposition.resolution_submitted_event_reference,
            expected_type=McpInputRequiredResolutionSubmittedEvent,
        )
        return
    if reason == "mcp_closure_publication_unavailable":
        closures = tuple(
            event
            for event in sources
            if isinstance(event, McpInputRequiredInteractionClosedEvent)
        )
        if len(sources) != 2 or len(closures) != 1 or len(terminals) != 1:
            raise ValueError("MCP closure publication authority is incomplete")
        closure = closures[0]
        terminal = terminals[0]
        terminal_source = terminal.mcp_input_required_terminal_source
        assert terminal_source is not None
        require_terminal_identity(
            terminal,
            closure.terminal_tool_result_event_identity,
        )
        if (
            closure.source_suspension_event_reference
            != terminal_source.source_suspension_event_reference
            or closure.source_resolution_submitted_event_reference
            != terminal_source.source_resolution_submitted_event_reference
        ):
            raise ValueError("MCP closure/ToolResult source chain drifted")
        require_exact_reference(
            closure.source_suspension_event_reference,
            expected_type=ToolExecutionSuspendedEvent,
        )
        if closure.source_resolution_submitted_event_reference is not None:
            require_exact_reference(
                closure.source_resolution_submitted_event_reference,
                expected_type=McpInputRequiredResolutionSubmittedEvent,
            )
        if closure.source_resume_failed_event_reference is not None:
            require_exact_reference(
                closure.source_resume_failed_event_reference,
                expected_type=McpInputRequiredResumeFailedEvent,
            )
        return
    raise ValueError("unknown publication-latched RunEnd authority reason")


class PublicationTerminalMaintenanceCoordinator:
    """RuntimeSession-owned exact-batch lease state machine."""

    def __init__(self, *, runtime_session_id: str) -> None:
        self._runtime_session_id = runtime_session_id
        self._issuer_token = object()
        self._records: dict[str, _LeaseRecord] = {}

    def issue(
        self,
        *,
        publication_latch_generation: int,
        owner_kind: PublicationTerminalMaintenanceOwnerKind,
        ordered_events: Sequence[AgentEvent],
        transaction_companion: EventLogTransactionCompanion | None,
        terminal_deadline_monotonic: float,
    ) -> PublicationTerminalMaintenanceLease:
        if owner_kind not in _OWNER_KINDS:
            raise ValueError("unknown publication terminal-maintenance owner kind")
        _validate_owner_batch(owner_kind, ordered_events)
        ids, payloads, companion, batch = _batch_identity(
            ordered_events,
            transaction_companion=transaction_companion,
        )
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("terminal-maintenance batch IDs must be non-empty and unique")
        identity = PublicationTerminalMaintenanceLeaseIdentity(
            lease_id=f"publication_maintenance:{uuid4().hex}",
            runtime_session_id=self._runtime_session_id,
            publication_latch_generation=publication_latch_generation,
            owner_kind=owner_kind,
            ordered_candidate_event_ids=ids,
            ordered_candidate_payload_fingerprints=payloads,
            transaction_companion_fingerprint=companion,
            exact_ordered_batch_fingerprint=batch,
            terminal_deadline_monotonic=terminal_deadline_monotonic,
        )
        handle = PublicationTerminalMaintenanceLease(
            _issuer_token=self._issuer_token,
            identity=identity,
        )
        self._records[identity.lease_id] = _LeaseRecord(
            handle=handle,
            state="issued",
            attempt_generation=0,
        )
        return handle

    def admit(
        self,
        *,
        handle: object,
        publication_latch_generation: int,
        events: Sequence[AgentEvent],
        transaction_companion: EventLogTransactionCompanion | None,
        deadline_monotonic: float,
    ) -> PublicationTerminalMaintenanceAttempt:
        record = self.preflight(
            handle=handle,
            publication_latch_generation=publication_latch_generation,
            events=events,
            transaction_companion=transaction_companion,
            deadline_monotonic=deadline_monotonic,
        )
        identity = record.handle.identity
        record.state = "in_flight"
        return PublicationTerminalMaintenanceAttempt(
            lease_id=identity.lease_id,
            attempt_generation=record.attempt_generation,
        )

    def preflight(
        self,
        *,
        handle: object,
        publication_latch_generation: int,
        events: Sequence[AgentEvent],
        transaction_companion: EventLogTransactionCompanion | None,
        deadline_monotonic: float,
    ) -> _LeaseRecord:
        """Validate one exact issued batch without advancing lease ownership."""

        if not isinstance(handle, PublicationTerminalMaintenanceLease):
            raise PermissionError("terminal maintenance requires an opaque lease handle")
        identity = handle.identity
        record = self._records.get(identity.lease_id)
        if (
            record is None
            or record.handle is not handle
            or not handle.is_valid
            or record.state != "issued"
            or record.attempt_generation != handle.issued_attempt_generation
        ):
            raise PermissionError("publication terminal-maintenance lease is stale")
        if (
            identity.runtime_session_id != self._runtime_session_id
            or identity.publication_latch_generation
            != publication_latch_generation
            or deadline_monotonic > identity.terminal_deadline_monotonic
        ):
            raise PermissionError("publication terminal-maintenance authority drifted")
        _validate_owner_batch(identity.owner_kind, events)
        ids, payloads, companion, batch = _batch_identity(
            events,
            transaction_companion=transaction_companion,
        )
        if (
            ids != identity.ordered_candidate_event_ids
            or payloads != identity.ordered_candidate_payload_fingerprints
            or companion != identity.transaction_companion_fingerprint
            or batch != identity.exact_ordered_batch_fingerprint
        ):
            raise PermissionError("publication terminal-maintenance batch drifted")
        return record

    def resolve(
        self,
        attempt: PublicationTerminalMaintenanceAttempt,
        *,
        status: Literal["full", "none", "unknown", "partial"],
    ) -> None:
        record = self._records.get(attempt.lease_id)
        if (
            record is None
            or record.state != "in_flight"
            or record.attempt_generation != attempt.attempt_generation
        ):
            raise RuntimeError("terminal-maintenance attempt ownership drifted")
        if status == "full":
            record.state = "consumed"
            record.handle._invalidate(issuer_token=self._issuer_token)
        elif status == "none":
            record.attempt_generation += 1
            record.state = "issued"
            record.handle._advance(
                issuer_token=self._issuer_token,
                generation=record.attempt_generation,
            )
        else:
            record.state = "reconciliation_required"
            record.handle._invalidate(issuer_token=self._issuer_token)

    def invalidate_issued(self) -> None:
        for record in self._records.values():
            if record.state == "issued":
                record.state = "invalidated"
                record.handle._invalidate(issuer_token=self._issuer_token)


__all__ = [
    "PublicationTerminalMaintenanceAttempt",
    "PublicationTerminalMaintenanceCoordinator",
    "PublicationTerminalMaintenanceLease",
    "PublicationTerminalMaintenanceLeaseIdentity",
    "PublicationTerminalMaintenanceOwnerKind",
    "validate_publication_latched_run_termination_authority",
]
