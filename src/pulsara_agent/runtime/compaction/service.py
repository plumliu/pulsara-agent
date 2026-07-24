"""LLM-backed context compaction service."""

from __future__ import annotations

import asyncio
import hashlib
import math
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import resources
from time import monotonic
from typing import Literal
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    CompactionCandidateDiagnosticEvent,
    ContextCompiledEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionMemoryCandidatesProposedEvent,
    ContextCompactionStartedEvent,
    EventContext,
    EventType,
    ModelCallStartEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    RunErrorEvent,
    RunEndEvent,
    RunStartEvent,
    ToolResultEndEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.direct import (
    DirectModelCallResult,
    collect_direct_model_call_handle,
)
from pulsara_agent.llm.commit import RuntimeSessionModelStreamEventCommitPort
from pulsara_agent.llm.lifecycle import prepare_model_lifecycle_start_bundle
from pulsara_agent.llm.errors import (
    CompactionSummarizerInputBudgetExceeded,
    CompactionTargetUnreachable,
    ModelContextIdentityMismatch,
    ModelInputBudgetExceeded,
    ModelInputEstimateMismatch,
    ModelTargetBindingMismatch,
    ModelTargetCapabilityMismatch,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.user_carrier import (
    derived_text_runtime_observation_payload,
    transcript_lifecycle_observation_payload,
)
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelTarget
from pulsara_agent.primitives.model_call import (
    CompactionObservedAfterMeasurementFact,
    CompactionTargetEstimateFact,
    ModelCallPurpose,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.message import (
    AssistantMsg,
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from pulsara_agent.message.assembler import BlockAssembler
from pulsara_agent.message.reducer import accepted_main_reply_ids
from pulsara_agent.runtime.compaction.planner import (
    SUMMARY_ARTIFACT_KIND,
    latest_completed_boundary,
    render_compaction_summary,
    strip_compaction_analysis,
)
from pulsara_agent.runtime.compaction.candidates import (
    CompactionCandidateAppendResult,
    CompactionCandidateDiagnostic,
    CompactionCandidateParseResult,
    CompactionMemoryCandidateSink,
    ContextCompactionMemoryCandidatePolicy,
    parse_compaction_memory_candidates,
    compaction_extractor_contract,
)
from pulsara_agent.memory.candidates.pool import candidate_payload_fingerprint
from pulsara_agent.memory.candidates.projection_outbox import (
    CandidateProjectionOutboxRow,
    MemoryCandidateProjectionCommitPort,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.primitives.governance_evidence import (
    CandidateProjectionOutboxItemFact,
    CandidateProjectionProducerKind,
)
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.runtime.compaction.commit import (
    CompactionCommitCancelledAfterCommit,
    CompactionCommitPendingAfterCancellation,
    CompactionEventCommitPort,
    CompactionEventCommitResult,
    CompactionPendingCommitNotDurable,
    PendingCompactionEventCommit,
    RuntimeSessionCompactionEventCommitPort,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    BoundedRuntimeFailureDiagnosticFact,
    CompactionCandidateProjectionReceipt,
    CompactionCandidateProjectionRequestIdentity,
    CompactionPublicationTerminalizationScope,
    PreparedCompactionCandidateProjectionInput,
    RuntimeEventOperationDeadlineBudget,
    build_bounded_runtime_failure_diagnostic,
    build_runtime_event_deadline_budget,
    ordered_fingerprint_accumulator,
)
from pulsara_agent.runtime.session import EventPublicationError, RuntimeSession
from pulsara_agent.runtime.state import LoopState

ContextCompactionTrigger = Literal["manual", "auto"]

_PRODUCTION_PROMPT_PACKAGE = "pulsara_agent.runtime.compaction.prompts"
_PRODUCTION_PROMPT_FILE = "context_compaction_prompt.md"
_COMPACTION_TEXT_CLIP_CHARS = 4_000
_COMPACTION_TOOL_INPUT_CLIP_CHARS = 2_000
_COMPACTION_TOOL_RESULT_CLIP_CHARS = 4_000
_MAX_COMPACTION_SOURCE_EVENTS = 16_384
_MAX_COMPACTION_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_COMPACTION_CHECKPOINT_CANDIDATES = 8
_MAX_COMPACTION_BASELINE_CANDIDATES = 32
_MAX_COMPACTION_LIFECYCLE_EVENTS = 16_384
_MAX_COMPACTION_LIFECYCLE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ContextCompactionAttemptResult:
    attempt_id: str
    compaction_id: str | None
    terminal_event_deadline_budget: RuntimeEventOperationDeadlineBudget | None
    publication_terminalization_scope: (
        CompactionPublicationTerminalizationScope | None
    )
    status: Literal["not_attempted", "completed", "failed"]
    not_attempted_reason: Literal[
        "disabled",
        "manual_disabled",
        "auto_disabled",
        "failure_circuit_open",
        "below_threshold",
        "empty_source",
        "no_plan",
    ] | None
    core_committed_events: tuple[AgentEvent, ...]
    terminal_event: (
        ContextCompactionCompletedEvent | ContextCompactionFailedEvent | None
    )
    committed_through_sequence: int | None
    publication_summary: Literal[
        "not_applicable",
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ]
    publication_errors: tuple[EventPublicationError, ...]
    candidate_projection_receipt: CompactionCandidateProjectionReceipt | None

    def __post_init__(self) -> None:
        has_terminal = self.terminal_event is not None
        if self.status == "not_attempted":
            if has_terminal or self.terminal_event_deadline_budget is not None:
                raise ValueError(
                    "not-attempted compaction cannot carry terminal authority"
                )
            return
        if not has_terminal or self.terminal_event_deadline_budget is None:
            raise ValueError(
                "terminal compaction result requires its event deadline budget"
            )
        if self.status == "completed" and not isinstance(
            self.terminal_event,
            ContextCompactionCompletedEvent,
        ):
            raise ValueError("completed compaction requires a completed terminal")
        if self.status == "failed" and not isinstance(
            self.terminal_event,
            ContextCompactionFailedEvent,
        ):
            raise ValueError("failed compaction requires a failed terminal")

    def __bool__(self) -> bool:
        return self.status == "completed"


class ContextCompactionInvocationFailed(RuntimeError):
    def __init__(self, result: ContextCompactionAttemptResult) -> None:
        self.result = result
        super().__init__("context compaction reached a durable failed terminal")


class ContextCompactionPublicationFailedAfterCommit(RuntimeError):
    def __init__(self, result: ContextCompactionAttemptResult) -> None:
        self.result = result
        super().__init__("context compaction publication failed after durable commit")


@dataclass(slots=True)
class _CompactionAttemptCollector:
    attempt_id: str
    scope: CompactionPublicationTerminalizationScope
    receipts: list[CompactionEventCommitResult] = field(default_factory=list)
    event_deadline_budgets: dict[str, RuntimeEventOperationDeadlineBudget] = field(
        default_factory=dict
    )
    candidate_projection_receipt: CompactionCandidateProjectionReceipt | None = None

    def admit_event_candidate(
        self,
        event_id: str,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> RuntimeEventOperationDeadlineBudget:
        existing = self.event_deadline_budgets.get(event_id)
        if existing is not None:
            if existing != deadline_budget:
                raise RuntimeError("compaction event candidate deadline was renewed")
            return existing
        self.event_deadline_budgets[event_id] = deadline_budget
        return deadline_budget

    def record(
        self,
        receipt: CompactionEventCommitResult,
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> None:
        event = receipt.committed_event
        if not isinstance(
            event,
            (
                ContextCompactionStartedEvent,
                ContextCompactionCompletedEvent,
                ContextCompactionFailedEvent,
            ),
        ):
            raise RuntimeError("compaction core collector received another event type")
        if receipt.candidate_event_id != event.id or event.sequence is None:
            raise RuntimeError("compaction core receipt identity mismatch")
        if receipt.candidate_deadline_budget != deadline_budget:
            raise RuntimeError("compaction core receipt deadline identity mismatch")
        if self.receipts and event.sequence <= self.receipts[-1].committed_through_sequence:
            raise RuntimeError("compaction core receipts are not strictly ordered")
        self.admit_event_candidate(event.id, deadline_budget)
        self.receipts.append(receipt)

    @property
    def last_receipt(self) -> CompactionEventCommitResult | None:
        return self.receipts[-1] if self.receipts else None

    @property
    def publication_failed(self) -> bool:
        return any(
            receipt.publication_status == "unavailable"
            or bool(receipt.publication_errors)
            for receipt in self.receipts
        )

    def freeze(
        self,
        *,
        not_attempted_reason: Literal[
            "disabled",
            "manual_disabled",
            "auto_disabled",
            "failure_circuit_open",
            "below_threshold",
            "empty_source",
            "no_plan",
        ]
        | None,
    ) -> ContextCompactionAttemptResult:
        events = tuple(receipt.committed_event for receipt in self.receipts)
        terminals = tuple(
            event
            for event in events
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
        )
        if len(terminals) > 1:
            raise RuntimeError("compaction attempt has conflicting terminal events")
        terminal = terminals[0] if terminals else None
        if isinstance(terminal, ContextCompactionCompletedEvent):
            status: Literal["not_attempted", "completed", "failed"] = "completed"
        elif isinstance(terminal, ContextCompactionFailedEvent):
            status = "failed"
        else:
            status = "not_attempted"
        if status == "not_attempted":
            events = ()
            terminal_deadline_budget = None
            publication_summary: Literal[
                "not_applicable",
                "completed",
                "enqueued",
                "unavailable",
                "failed_after_commit",
            ] = "not_applicable"
            committed_through = None
            compaction_id = None
            scope = None
        else:
            not_attempted_reason = None
            if any(receipt.publication_errors for receipt in self.receipts):
                publication_summary = "failed_after_commit"
            elif any(
                receipt.publication_status == "unavailable"
                for receipt in self.receipts
            ):
                publication_summary = "unavailable"
            elif any(
                receipt.publication_status == "enqueued"
                for receipt in self.receipts
            ):
                publication_summary = "enqueued"
            else:
                publication_summary = "completed"
            committed_through = max(
                receipt.committed_through_sequence for receipt in self.receipts
            )
            compaction_id = terminal.compaction_id
            scope = self.scope
            terminal_deadline_budget = next(
                receipt.candidate_deadline_budget
                for receipt in self.receipts
                if receipt.committed_event.id == terminal.id
            )
        return ContextCompactionAttemptResult(
            attempt_id=self.attempt_id,
            compaction_id=compaction_id,
            terminal_event_deadline_budget=terminal_deadline_budget,
            publication_terminalization_scope=scope,
            status=status,
            not_attempted_reason=not_attempted_reason,
            core_committed_events=events,
            terminal_event=terminal,
            committed_through_sequence=committed_through,
            publication_summary=publication_summary,
            publication_errors=tuple(
                error
                for receipt in self.receipts
                for error in receipt.publication_errors
            ),
            candidate_projection_receipt=self.candidate_projection_receipt,
        )


@dataclass(frozen=True, slots=True)
class _PreparedCandidateProjectionAdmission:
    request_identity: CompactionCandidateProjectionRequestIdentity
    prepared_input: PreparedCompactionCandidateProjectionInput | None
    preparation_failure: BoundedRuntimeFailureDiagnosticFact | None
    raw_summary: str
    summary: str
    phase: str | None


_CURRENT_COMPACTION_ATTEMPT: ContextVar[_CompactionAttemptCollector | None] = (
    ContextVar("pulsara_current_compaction_attempt", default=None)
)


@dataclass(frozen=True, slots=True)
class ContextCompactionPolicy:
    enabled: bool = True
    auto_enabled: bool = True
    manual_enabled: bool = True
    auto_trigger_ratio: float = 0.80
    post_compaction_target_ratio: float = 0.55
    min_events_after_last_compact: int = 20
    keep_recent_runs: int = 3
    max_summary_chars: int = 12_000
    max_consecutive_failures: int = 3
    summarizer_options: LLMOptions = field(default_factory=LLMOptions)
    memory_candidates: ContextCompactionMemoryCandidatePolicy = field(
        default_factory=ContextCompactionMemoryCandidatePolicy
    )

    def __post_init__(self) -> None:
        if not (0 < self.post_compaction_target_ratio < self.auto_trigger_ratio < 1):
            raise ValueError(
                "compaction ratios must satisfy 0 < post target < auto trigger < 1"
            )


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    through_sequence: int
    keep_after_sequence: int
    target_estimate: CompactionTargetEstimateFact
    threshold_tokens: int
    post_compaction_target_tokens: int
    retained_transcript_tokens: int
    protected_transcript_tokens: int
    included_run_ids: tuple[str, ...]
    included_artifact_ids: tuple[str, ...]
    compacted_events: tuple[AgentEvent, ...]
    tail_events: tuple[AgentEvent, ...]
    window_number: int
    window_id: str
    previous_summary_artifact_id: str | None = None
    previous_summary_text: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionSummaryReplayTemplate:
    summary_artifact_id: str
    compaction_id: str
    window_id: str
    through_sequence: int
    keep_after_sequence: int


@dataclass(slots=True)
class CompactionTerminalizationOwner:
    started_event: ContextCompactionStartedEvent
    terminal_event_id: str
    terminal_candidate: (
        ContextCompactionCompletedEvent | ContextCompactionFailedEvent | None
    )
    started_committed: bool = True
    pending_started_commit: PendingCompactionEventCommit | None = None
    deadline_budget: RuntimeEventOperationDeadlineBudget | None = None
    state: Literal[
        "started_commit_pending", "started", "candidate_frozen", "committing"
    ] = "started"


class PendingCompactionTerminalizationError(RuntimeError):
    pass


@dataclass(slots=True)
class ContextCompactionService:
    event_log: EventLog
    archive: ArtifactStore
    llm_runtime: LLMRuntime
    runtime_session_id: str
    runtime_session: RuntimeSession | None = None
    policy: ContextCompactionPolicy = ContextCompactionPolicy()
    model_role: ModelRole = ModelRole.FLASH
    candidate_sink: CompactionMemoryCandidateSink | None = None
    event_commit_port: CompactionEventCommitPort | None = None
    candidate_projection_commit_port: MemoryCandidateProjectionCommitPort | None = None
    _consecutive_failures: int = 0
    _pending_terminalizations: dict[str, CompactionTerminalizationOwner] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _candidate_projection_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _candidate_projection_receipts: dict[
        str, CompactionCandidateProjectionReceipt
    ] = field(default_factory=dict, init=False, repr=False)
    _candidate_projection_accepting: bool = field(
        default=True,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.event_commit_port,
            RuntimeSessionCompactionEventCommitPort,
        ):
            raise ValueError(
                "context compaction requires the RuntimeSession-owned commit port"
            )
        port_session = self.event_commit_port.runtime_session
        if self.runtime_session is None:
            self.runtime_session = port_session
        elif self.runtime_session is not port_session:
            raise ValueError("compaction RuntimeSession/commit port ownership drifted")
        if self.runtime_session.runtime_session_id != self.runtime_session_id:
            raise ValueError("compaction runtime-session identity mismatch")
        if self.runtime_session.event_log is not self.event_log:
            raise ValueError("compaction RuntimeSession/EventLog ownership drifted")
        self._recover_pending_terminalization_owners()

    @property
    def pending_terminalization_count(self) -> int:
        return len(self._pending_terminalizations)

    @property
    def pending_candidate_projection_count(self) -> int:
        return sum(
            not task.done() for task in self._candidate_projection_tasks.values()
        )

    def _recover_pending_terminalization_owners(self) -> None:
        deadline = monotonic() + 30.0
        snapshot = self.event_log.read_raw_events_by_types(
            (
                str(EventType.CONTEXT_COMPACTION_STARTED),
                str(EventType.CONTEXT_COMPACTION_COMPLETED),
                str(EventType.CONTEXT_COMPACTION_FAILED),
            ),
            max_events=_MAX_COMPACTION_LIFECYCLE_EVENTS,
            max_payload_bytes=_MAX_COMPACTION_LIFECYCLE_BYTES,
            deadline_monotonic=deadline,
        )
        events = tuple(
            event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for event in snapshot.events
        )
        terminal_started_ids = {
            event.started_event_id
            for event in events
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
            and event.started_event_id is not None
        }
        for event in events:
            if (
                isinstance(event, ContextCompactionStartedEvent)
                and event.id not in terminal_started_ids
            ):
                owner = CompactionTerminalizationOwner(
                    started_event=event,
                    terminal_event_id=event.terminal_event_id,
                    terminal_candidate=None,
                )
                owner.terminal_candidate = self._recovery_terminal_candidate(event)
                owner.state = "candidate_frozen"
                self._pending_terminalizations[event.id] = owner
        terminal_events = tuple(
            event
            for event in events
            if isinstance(
                event,
                (ContextCompactionCompletedEvent, ContextCompactionFailedEvent),
            )
        )
        failures = 0
        for event in reversed(terminal_events):
            if isinstance(event, ContextCompactionCompletedEvent):
                break
            failures += 1
        self._consecutive_failures = failures

    def _read_bounded_source_events(self) -> list[AgentEvent]:
        deadline = monotonic() + 30.0
        minimum_sequence = 1
        checkpoint_rows = self.event_log.read_raw_events_by_type(
            str(EventType.CONTEXT_COMPACTION_COMPLETED),
            limit=_MAX_COMPACTION_CHECKPOINT_CANDIDATES,
            deadline_monotonic=deadline,
        )
        if not checkpoint_rows and self.event_log.next_sequence() == 1:
            return []
        for raw in checkpoint_rows:
            event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if not isinstance(event, ContextCompactionCompletedEvent):
                raise ValueError(
                    "compaction checkpoint query returned another event type"
                )
            try:
                self.archive.get_text(
                    event.summary_artifact_id,
                    session_id=self.runtime_session_id,
                    deadline_monotonic=deadline,
                )
            except (KeyError, ValueError):
                continue
            minimum_sequence = event.keep_after_sequence + 1
            break
        snapshot = self.event_log.read_raw_range_snapshot(
            minimum_sequence=minimum_sequence,
            max_events=_MAX_COMPACTION_SOURCE_EVENTS,
            max_payload_bytes=_MAX_COMPACTION_SOURCE_BYTES,
            deadline_monotonic=deadline,
        )
        return [
            event.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for event in snapshot.events
        ]

    async def _read_bounded_source_events_async(self) -> list[AgentEvent]:
        deadline = monotonic() + 30.0
        io_service = getattr(self.runtime_session, "context_input_io_service", None)
        if io_service is None:
            return await asyncio.to_thread(self._read_bounded_source_events)
        return await io_service.execute(
            operation_name="context-compaction-source-read",
            operation=self._read_bounded_source_events,
            deadline_monotonic=deadline,
        )

    def _register_started_owner(
        self,
        started: ContextCompactionStartedEvent,
        *,
        committed: bool,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> None:
        self._pending_terminalizations[started.id] = CompactionTerminalizationOwner(
            started_event=started,
            terminal_event_id=started.terminal_event_id,
            terminal_candidate=None,
            started_committed=committed,
            deadline_budget=deadline_budget,
            state="started" if committed else "started_commit_pending",
        )

    def _acknowledge_started_commit(
        self,
        committed: ContextCompactionStartedEvent,
    ) -> None:
        owner = self._pending_terminalizations.get(committed.id)
        if owner is None:
            raise RuntimeError("committed compaction Started has no process owner")
        if committed.terminal_event_id != owner.terminal_event_id:
            raise RuntimeError("committed compaction Started identity mismatch")
        owner.started_event = committed
        owner.started_committed = True
        owner.pending_started_commit = None
        owner.state = "started"

    def _freeze_terminal_candidate(
        self,
        started_event_id: str,
        candidate: ContextCompactionCompletedEvent | ContextCompactionFailedEvent,
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
    ) -> None:
        owner = self._pending_terminalizations.get(started_event_id)
        if owner is None:
            raise RuntimeError("compaction terminal candidate has no Started owner")
        if candidate.id != owner.terminal_event_id:
            raise RuntimeError("compaction terminal candidate identity mismatch")
        if (
            owner.terminal_candidate is not None
            and owner.terminal_candidate != candidate
        ):
            stored = self.event_log.get_by_id(owner.terminal_event_id)
            if stored is not None:
                raise RuntimeError("compaction terminal candidate payload conflict")
        owner.terminal_candidate = candidate
        if (
            owner.state == "candidate_frozen"
            and owner.deadline_budget is not None
            and owner.deadline_budget != deadline_budget
        ):
            raise RuntimeError("compaction terminal candidate deadline was renewed")
        owner.deadline_budget = deadline_budget
        owner.state = "candidate_frozen"

    def _acknowledge_terminal_candidate(self, event: AgentEvent) -> None:
        started_event_id = getattr(event, "started_event_id", None)
        if not isinstance(started_event_id, str):
            return
        owner = self._pending_terminalizations.get(started_event_id)
        if owner is None:
            return
        if event.id != owner.terminal_event_id:
            raise RuntimeError("committed compaction terminal identity mismatch")
        self._pending_terminalizations.pop(started_event_id, None)

    def _recovery_terminal_candidate(
        self,
        started: ContextCompactionStartedEvent,
    ) -> ContextCompactionFailedEvent:
        return ContextCompactionFailedEvent(
            id=started.terminal_event_id,
            created_at=started.created_at,
            run_id=started.run_id,
            turn_id=started.turn_id,
            reply_id=started.reply_id,
            compaction_id=started.compaction_id,
            trigger=started.trigger,
            reason=started.reason,
            window_number=started.window_number,
            window_id=started.window_id,
            target_model_target=started.target_model_target,
            target_input_budget_tokens=started.target_input_budget_tokens,
            threshold_tokens=started.threshold_tokens,
            post_compaction_target_tokens=started.post_compaction_target_tokens,
            failure_stage="recovery_terminalization",
            target_estimate=started.target_estimate,
            summarizer_target=started.summarizer_call.target,
            summarizer_call=started.summarizer_call,
            summarizer_context_id=started.summarizer_context_id,
            summarizer_input_estimated_tokens=(
                started.summarizer_input_estimated_tokens
            ),
            summarizer_input_budget_tokens=started.summarizer_input_budget_tokens,
            summarizer_usage_status="missing",
            summarizer_usage=None,
            summarizer_estimated_input_tokens=(
                started.summarizer_input_estimated_tokens
            ),
            through_sequence=started.through_sequence,
            keep_after_sequence=started.keep_after_sequence,
            error_type="RecoveredInterruptedCompaction",
            message="compaction Started had no durable terminal fact",
            started_event_id=started.id,
            termination_kind="recovered_interrupted",
            host_boundary_id=started.host_boundary_id,
            host_boundary_kind=started.host_boundary_kind,
            metadata={**started.metadata, "recovery": True},
        )

    async def drain_pending_terminalizations(
        self,
        *,
        timeout_seconds: float,
    ) -> None:
        if not self._pending_terminalizations:
            return
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        errors: list[BaseException] = []
        for started_event_id, owner in tuple(self._pending_terminalizations.items()):
            if not owner.started_committed:
                pending = owner.pending_started_commit
                if pending is None:
                    self._pending_terminalizations.pop(started_event_id, None)
                    continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    started_result = await pending.resolve(timeout_seconds=remaining)
                except CompactionPendingCommitNotDurable:
                    self._pending_terminalizations.pop(started_event_id, None)
                    continue
                except BaseException as exc:
                    errors.append(exc)
                    continue
                committed_started = started_result.committed_event
                if not isinstance(committed_started, ContextCompactionStartedEvent):
                    errors.append(
                        RuntimeError(
                            "pending compaction Started returned wrong event type"
                        )
                    )
                    continue
                self._acknowledge_started_commit(committed_started)
            candidate = owner.terminal_candidate
            if candidate is None:
                candidate = self._recovery_terminal_candidate(owner.started_event)
            if owner.deadline_budget is None:
                self._freeze_terminal_candidate(
                    started_event_id,
                    candidate,
                    deadline_budget=self._new_event_write_deadline_budget(),
                )
            deadline_budget = owner.deadline_budget
            if deadline_budget is None:
                raise RuntimeError("compaction terminal owner lost its write deadline")
            owner.state = "committing"
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                committed = await asyncio.wait_for(
                    self._commit_event(
                        candidate,
                        deadline_budget=deadline_budget,
                    ),
                    timeout=remaining,
                )
            except CompactionCommitCancelledAfterCommit as exc:
                committed = exc.result.committed_event
            except BaseException as exc:
                owner.state = "candidate_frozen"
                errors.append(exc)
                continue
            self._acknowledge_terminal_candidate(committed)
        if self._pending_terminalizations:
            raise PendingCompactionTerminalizationError(
                "pending compaction terminal facts were not durably confirmed: "
                f"{len(self._pending_terminalizations)}"
            ) from (errors[-1] if errors else None)

    async def _commit_event(
        self,
        event: AgentEvent,
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget | None = None,
        use_terminal_deadline: bool = False,
        publication_terminal_maintenance_lease: object | None = None,
    ) -> AgentEvent:
        port = self.event_commit_port
        if port is None:  # pragma: no cover - guarded by __post_init__
            raise RuntimeError("compaction event commit port is unavailable")
        collector = _CURRENT_COMPACTION_ATTEMPT.get()
        if deadline_budget is None:
            deadline_budget = self._new_event_write_deadline_budget()
        if collector is not None:
            deadline_budget = collector.admit_event_candidate(
                event.id,
                deadline_budget,
            )
        result = await port.commit_event(
            event,
            deadline_budget=deadline_budget,
            use_terminal_deadline=use_terminal_deadline,
            publication_terminal_maintenance_lease=(
                publication_terminal_maintenance_lease
            ),
        )
        if collector is not None:
            collector.record(result, deadline_budget=deadline_budget)
        return result.committed_event

    @staticmethod
    def _new_event_write_deadline_budget() -> RuntimeEventOperationDeadlineBudget:
        return build_runtime_event_deadline_budget(
            admitted_at_monotonic=monotonic(),
            total_timeout_seconds=30.0,
            terminal_reserve_seconds=10.0,
        )

    def should_auto_compact(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
    ) -> bool:
        if not self.policy.enabled or not self.policy.auto_enabled:
            return False
        if self._consecutive_failures >= self.policy.max_consecutive_failures:
            return False
        events = self._read_bounded_source_events()
        try:
            plan = self._build_plan(
                events,
                compaction_id=f"context_compaction:{uuid4().hex}",
                target_model_target=target_model_target,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                model_visible_messages_before=model_visible_messages_before,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=keep_recent_runs_override,
            )
        except CompactionTargetUnreachable:
            return True
        if plan is None:
            return False
        return True

    async def compact_if_needed(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        reason: str = "context_threshold",
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
        event_metadata: dict[str, object] | None = None,
        host_boundary_id: str | None = None,
        host_boundary_kind: Literal["pre_run"] | None = None,
        runtime_state: LoopState | None = None,
    ) -> ContextCompactionAttemptResult:
        return await self.compact(
            target_model_target=target_model_target,
            trigger="auto",
            reason=reason,
            current_user_input_if_not_already_represented=(
                current_user_input_if_not_already_represented
            ),
            model_visible_messages_before=model_visible_messages_before,
            protected_model_visible_messages_after=(
                protected_model_visible_messages_after
            ),
            max_compactable_sequence=max_compactable_sequence,
            keep_recent_runs_override=keep_recent_runs_override,
            event_metadata=event_metadata,
            host_boundary_id=host_boundary_id,
            host_boundary_kind=host_boundary_kind,
            runtime_state=runtime_state,
        )

    async def _auto_threshold_reached_from_visible_context(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        model_visible_messages_before: list[Msg] | tuple[Msg, ...],
        current_user_input_if_not_already_represented: str,
        max_compactable_sequence: int | None,
    ) -> bool | None:
        """Check auto pressure before reading the raw compaction source."""

        deadline = monotonic() + 30.0

        def read_baselines():
            return self.event_log.read_raw_events_by_type(
                str(EventType.CONTEXT_COMPILED),
                limit=_MAX_COMPACTION_BASELINE_CANDIDATES,
                through_sequence=max_compactable_sequence,
                deadline_monotonic=deadline,
            )

        io_service = getattr(self.runtime_session, "context_input_io_service", None)
        if io_service is None:
            rows = await asyncio.to_thread(read_baselines)
        else:
            rows = await io_service.execute(
                operation_name="context-compaction-threshold-baseline-read",
                operation=read_baselines,
                deadline_monotonic=deadline,
            )
        basis = next(
            (
                event
                for raw in rows
                if isinstance(
                    event := raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY),
                    ContextCompiledEvent,
                )
                and event.resolved_call.target.target_fingerprint
                == target_model_target.fact.target_fingerprint
            ),
            None,
        )
        if basis is None:
            return None
        estimated_before = (
            _estimate_transcript_messages(
                model_visible_messages_before,
                current_user_input=current_user_input_if_not_already_represented,
                target_model_target=target_model_target,
                previous_summary_text=None,
            )
            + basis.budget.non_transcript_baseline_tokens
        )
        threshold_tokens = max(
            1,
            math.floor(
                target_model_target.fact.context_budget.input_budget_tokens
                * self.policy.auto_trigger_ratio
            ),
        )
        return estimated_before >= threshold_tokens

    async def compact(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        trigger: ContextCompactionTrigger,
        reason: str,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        force: bool = False,
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
        event_metadata: dict[str, object] | None = None,
        host_boundary_id: str | None = None,
        host_boundary_kind: Literal["pre_run"] | None = None,
        runtime_state: LoopState | None = None,
    ) -> ContextCompactionAttemptResult:
        not_attempted_reason: Literal[
            "disabled",
            "manual_disabled",
            "auto_disabled",
            "failure_circuit_open",
            "below_threshold",
            "empty_source",
            "no_plan",
        ] | None = None
        if not self.policy.enabled:
            not_attempted_reason = "disabled"
        elif trigger == "manual" and not self.policy.manual_enabled:
            not_attempted_reason = "manual_disabled"
        elif trigger == "auto" and not self.policy.auto_enabled:
            not_attempted_reason = "auto_disabled"
        elif (
            trigger == "auto"
            and self._consecutive_failures >= self.policy.max_consecutive_failures
        ):
            not_attempted_reason = "failure_circuit_open"
        scope = self._freeze_publication_terminalization_scope(
            trigger=trigger,
            event_metadata=event_metadata,
            runtime_state=runtime_state,
        )
        collector = _CompactionAttemptCollector(
            attempt_id=f"context_compaction_attempt:{uuid4().hex}",
            scope=scope,
        )
        if not_attempted_reason is not None:
            return collector.freeze(not_attempted_reason=not_attempted_reason)
        token = _CURRENT_COMPACTION_ATTEMPT.set(collector)
        caught: BaseException | None = None
        try:
            await self._compact_core(
                target_model_target=target_model_target,
                trigger=trigger,
                reason=reason,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                model_visible_messages_before=model_visible_messages_before,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                force=force,
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=keep_recent_runs_override,
                event_metadata=event_metadata,
                host_boundary_id=host_boundary_id,
                host_boundary_kind=host_boundary_kind,
            )
        except BaseException as exc:
            caught = exc
        finally:
            _CURRENT_COMPACTION_ATTEMPT.reset(token)
        result = collector.freeze(
            not_attempted_reason=(
                "empty_source"
                if not collector.receipts
                and self.event_log.next_sequence() == 1
                else "no_plan"
            )
        )
        if result.publication_summary in {"unavailable", "failed_after_commit"}:
            raise ContextCompactionPublicationFailedAfterCommit(result) from caught
        if caught is not None:
            if result.status == "failed" and isinstance(caught, Exception):
                if trigger == "manual":
                    raise ContextCompactionInvocationFailed(result) from caught
                return result
            raise caught
        return result

    def _freeze_publication_terminalization_scope(
        self,
        *,
        trigger: ContextCompactionTrigger,
        event_metadata: dict[str, object] | None,
        runtime_state: LoopState | None,
    ) -> CompactionPublicationTerminalizationScope:
        metadata = event_metadata or {}
        mid_turn = runtime_state is not None or metadata.get("phase") == "mid_turn"
        if mid_turn:
            if runtime_state is None or runtime_state.run_working_set is None:
                raise ValueError("mid-turn compaction requires its active RunWorkingSet")
            contract = runtime_state.run_working_set.long_horizon_contract
            payload = {
                "scope_kind": "mid_turn_active_run",
                "runtime_session_id": self.runtime_session_id,
                "active_run_id": runtime_state.run_id,
                "active_context_window_id": runtime_state.scratchpad.get(
                    "active_context_window_id",
                    contract.initial_window_id,
                ),
                "active_rollout_account_id": contract.rollout_account_id,
                "host_state_generation": int(
                    metadata.get("host_state_generation", runtime_state.turn_index)
                ),
            }
        else:
            payload = {
                "scope_kind": (
                    "pre_run_without_active_run"
                    if metadata.get("phase") == "pre_run"
                    else "manual_without_active_run"
                ),
                "runtime_session_id": self.runtime_session_id,
                "active_run_id": None,
                "active_context_window_id": None,
                "active_rollout_account_id": None,
                "host_state_generation": int(
                    metadata.get("host_state_generation", 0)
                ),
            }
        return CompactionPublicationTerminalizationScope(
            **payload,  # type: ignore[arg-type]
            scope_fingerprint=context_fingerprint(
                "compaction-publication-terminalization-scope:v1",
                payload,
            ),
        )

    async def _compact_core(
        self,
        *,
        target_model_target: ResolvedModelTarget,
        trigger: ContextCompactionTrigger,
        reason: str,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        force: bool = False,
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
        event_metadata: dict[str, object] | None = None,
        host_boundary_id: str | None = None,
        host_boundary_kind: Literal["pre_run"] | None = None,
    ) -> ContextCompactionCompletedEvent | None:
        if self._pending_terminalizations:
            await self.drain_pending_terminalizations(timeout_seconds=2.0)
        if not self.policy.enabled:
            return None
        if trigger == "manual" and not self.policy.manual_enabled:
            return None
        if trigger == "auto" and not self.policy.auto_enabled:
            return None
        if (
            trigger == "auto"
            and self._consecutive_failures >= self.policy.max_consecutive_failures
        ):
            return None
        if trigger == "auto" and model_visible_messages_before is not None:
            threshold_reached = await self._auto_threshold_reached_from_visible_context(
                target_model_target=target_model_target,
                model_visible_messages_before=model_visible_messages_before,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                max_compactable_sequence=max_compactable_sequence,
            )
            if threshold_reached is False:
                return None

        events = await self._read_bounded_source_events_async()
        if not events:
            return None
        compaction_id = f"context_compaction:{uuid4().hex}"
        started_event_id = f"context_compaction_started:{uuid4().hex}"
        terminal_event_id = f"context_compaction_terminal:{uuid4().hex}"
        context = _event_context_for_compaction(events)
        try:
            plan = self._build_plan(
                events,
                compaction_id=compaction_id,
                target_model_target=target_model_target,
                current_user_input_if_not_already_represented=(
                    current_user_input_if_not_already_represented
                ),
                model_visible_messages_before=model_visible_messages_before,
                protected_model_visible_messages_after=(
                    protected_model_visible_messages_after
                ),
                force=force,
                max_compactable_sequence=max_compactable_sequence,
                keep_recent_runs_override=keep_recent_runs_override,
            )
        except Exception as exc:
            self._consecutive_failures += 1
            window_number = _next_window_number(events)
            failed = ContextCompactionFailedEvent(
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=window_number,
                window_id=f"context_window:{window_number}:{uuid4().hex}",
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=max(
                    1,
                    math.floor(
                        target_model_target.fact.context_budget.input_budget_tokens
                        * self.policy.auto_trigger_ratio
                    ),
                ),
                post_compaction_target_tokens=max(
                    1,
                    math.floor(
                        target_model_target.fact.context_budget.input_budget_tokens
                        * self.policy.post_compaction_target_ratio
                    ),
                ),
                failure_stage="planning",
                error_type=type(exc).__name__,
                message=str(exc),
                started_event_id=None,
                termination_kind="failed",
                host_boundary_id=host_boundary_id,
                host_boundary_kind=host_boundary_kind,
                metadata=dict(event_metadata or {}),
            )
            await self._commit_event(failed)
            if trigger == "manual":
                raise
            return None
        if plan is None:
            return None
        if (
            not force
            and trigger == "auto"
            and plan.target_estimate.estimated_tokens_before < plan.threshold_tokens
        ):
            return None

        metadata = {
            "estimate_scope": plan.target_estimate.estimate_scope,
            "basis_context_id": plan.target_estimate.basis_context_id,
            **(event_metadata or {}),
        }
        phase = (
            str(metadata.get("phase")) if metadata.get("phase") is not None else None
        )
        failure_stage = "summarizer_resolution"
        summarizer_target = None
        summarizer_call = None
        summarizer_context: LLMContext | None = None
        summarizer_input_estimated_tokens: int | None = None
        summarizer_provider_input = None
        summarizer_model_event_context: EventContext | None = None
        call_result: DirectModelCallResult | None = None
        completed_target_estimate = plan.target_estimate
        observed_after_measurement: CompactionObservedAfterMeasurementFact | None = None
        started_committed: ContextCompactionStartedEvent | None = None
        terminal_committed = False
        projection_admission: _PreparedCandidateProjectionAdmission | None = None
        try:
            summarizer_target = self.llm_runtime.resolve_target(
                role=self.model_role,
                requested_options=self.policy.summarizer_options,
            )
            summarizer_call = self.llm_runtime.resolve_call(
                target=summarizer_target,
                purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            )
            failure_stage = "summarizer_input_build"
            summarizer_context, summarizer_input_estimated_tokens = (
                self._build_summarizer_context(
                    plan,
                    call=summarizer_call,
                    trigger=trigger,
                    phase=phase,
                )
            )
            if self.runtime_session is None:
                raise RuntimeError(
                    "context compaction model execution requires RuntimeSession ownership"
                )
            summarizer_model_event_context = EventContext(
                run_id=context.run_id,
                turn_id=context.turn_id,
                reply_id=f"{context.reply_id}:compaction-model",
            )
            failure_stage = "summarizer_provider_input_prepare"
            summarizer_provider_input = await self.runtime_session.provider_input_generation_coordinator.prepare_one_shot_call(
                call=summarizer_call,
                context=summarizer_context,
                event_context=summarizer_model_event_context,
                operation_kind="window_summarizer",
                operation_id=summarizer_context.context_id
                or summarizer_call.fact.resolved_model_call_id,
            )
            summarizer_context = summarizer_provider_input.carrier.to_llm_context(
                summarizer_context
            )
            summarizer_input_estimated_tokens = (
                summarizer_call.target.token_estimator.estimate_context(
                    summarizer_context
                ).total_input_tokens
            )
            started = ContextCompactionStartedEvent(
                id=started_event_id,
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=plan.threshold_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                target_estimate=plan.target_estimate,
                summarizer_call=summarizer_call.fact,
                summarizer_context_id=summarizer_context.context_id or "",
                summarizer_input_estimated_tokens=summarizer_input_estimated_tokens,
                summarizer_input_budget_tokens=(
                    summarizer_target.fact.context_budget.input_budget_tokens
                ),
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                force=force,
                terminal_event_id=terminal_event_id,
                host_boundary_id=host_boundary_id,
                host_boundary_kind=host_boundary_kind,
                metadata=metadata,
            )
            failure_stage = "started_append"
            collector = _CURRENT_COMPACTION_ATTEMPT.get()
            if collector is None:
                raise RuntimeError("compaction Started lacks its attempt owner")
            self._register_started_owner(
                started,
                committed=False,
                deadline_budget=(
                    started_deadline_budget := self._new_event_write_deadline_budget()
                ),
            )
            try:
                committed_started = await self._commit_event(
                    started,
                    deadline_budget=started_deadline_budget,
                )
            except CompactionCommitPendingAfterCancellation as pending_commit:
                owner = self._pending_terminalizations[started.id]
                owner.pending_started_commit = pending_commit.pending
                owner.state = "started_commit_pending"
                raise
            except CompactionCommitCancelledAfterCommit as cancelled_commit:
                committed_started = cancelled_commit.result.committed_event
                if not isinstance(committed_started, ContextCompactionStartedEvent):
                    raise RuntimeError(
                        "compaction Started cancellation confirmation type mismatch"
                    )
                started_committed = committed_started
                self._acknowledge_started_commit(committed_started)
                raise
            except BaseException:
                self._pending_terminalizations.pop(started.id, None)
                raise
            if not isinstance(committed_started, ContextCompactionStartedEvent):
                raise RuntimeError(
                    "compaction Started commit returned wrong event type"
                )
            started_committed = committed_started
            self._acknowledge_started_commit(committed_started)
            if collector.publication_failed:
                started_publication_failed = ContextCompactionFailedEvent(
                    id=terminal_event_id,
                    **context.event_fields(),
                    compaction_id=compaction_id,
                    trigger=trigger,
                    reason=reason,
                    window_number=plan.window_number,
                    window_id=plan.window_id,
                    target_model_target=target_model_target.fact,
                    target_input_budget_tokens=(
                        target_model_target.fact.context_budget.input_budget_tokens
                    ),
                    threshold_tokens=plan.threshold_tokens,
                    post_compaction_target_tokens=(
                        plan.post_compaction_target_tokens
                    ),
                    failure_stage="started_publication",
                    target_estimate=plan.target_estimate,
                    summarizer_target=None,
                    summarizer_call=None,
                    summarizer_context_id=None,
                    summarizer_input_estimated_tokens=None,
                    summarizer_input_budget_tokens=None,
                    summarizer_usage_status="missing",
                    summarizer_usage=None,
                    summarizer_estimated_input_tokens=None,
                    summarizer_reported_model_id=None,
                    through_sequence=plan.through_sequence,
                    keep_after_sequence=plan.keep_after_sequence,
                    error_type="CompactionStartedPublicationUnavailable",
                    message=(
                        "compaction Started committed but publication "
                        "was not confirmed"
                    ),
                    started_event_id=started_event_id,
                    termination_kind="failed",
                    host_boundary_id=host_boundary_id,
                    host_boundary_kind=host_boundary_kind,
                    metadata=metadata,
                )
                self._freeze_terminal_candidate(
                    started_event_id,
                    started_publication_failed,
                    deadline_budget=started_deadline_budget,
                )
                maintenance_lease = (
                    self.runtime_session.issue_publication_terminal_maintenance_lease(
                        owner_kind="compaction_started_publication_failed_bundle",
                        ordered_events=(started_publication_failed,),
                        transaction_companion=None,
                        deadline_budget=started_deadline_budget,
                    )
                )
                committed_failed = await self._commit_event(
                    started_publication_failed,
                    deadline_budget=started_deadline_budget,
                    use_terminal_deadline=True,
                    publication_terminal_maintenance_lease=maintenance_lease,
                )
                terminal_committed = True
                self._acknowledge_terminal_candidate(committed_failed)
                return None

            failure_stage = "model_stream"
            call_result = await self._summarize(
                call=summarizer_call,
                context=summarizer_context,
                event_context=summarizer_model_event_context,
                provider_input=summarizer_provider_input,
            )
            if call_result.outcome != "completed":
                raise RuntimeError(
                    call_result.error.message
                    if call_result.error
                    else "compact model error"
                )
            raw_summary = call_result.text
            failure_stage = "summary_validation"
            summary = strip_compaction_analysis(raw_summary)
            if not summary:
                raise RuntimeError("compact model returned an empty summary")
            if len(summary) > self.policy.max_summary_chars:
                raise RuntimeError("compact model summary exceeds max_summary_chars")
            artifact_id = _summary_artifact_id(compaction_id)
            replay_template = CompactionSummaryReplayTemplate(
                summary_artifact_id=artifact_id,
                compaction_id=compaction_id,
                window_id=plan.window_id,
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
            )
            summary_tokens_actual = estimate_compaction_summary_replay_tokens(
                replay_template=replay_template,
                summary_text=summary,
                target_model_target=target_model_target,
            )
            transcript_tokens_after = (
                plan.retained_transcript_tokens
                + plan.protected_transcript_tokens
                + summary_tokens_actual
            )
            baseline = plan.target_estimate.non_transcript_baseline_tokens
            estimated_tokens_after = transcript_tokens_after + (baseline or 0)
            predicted = (
                estimated_tokens_after <= plan.post_compaction_target_tokens
                if baseline is not None
                else None
            )
            if summary_tokens_actual > plan.target_estimate.summary_tokens_reserved:
                observed_after_measurement = CompactionObservedAfterMeasurementFact(
                    summary_tokens_actual=summary_tokens_actual,
                    retained_transcript_tokens=plan.retained_transcript_tokens,
                    protected_transcript_tokens=plan.protected_transcript_tokens,
                    transcript_tokens_after=transcript_tokens_after,
                    estimated_tokens_after=estimated_tokens_after,
                    predicted_post_target_reached=predicted,
                    violation_code="summary_tokens_exceed_reservation",
                )
                raise ValueError(
                    "actual summary tokens exceed the planning reservation"
                )
            completed_target_estimate = CompactionTargetEstimateFact(
                estimate_scope=plan.target_estimate.estimate_scope,
                basis_context_id=plan.target_estimate.basis_context_id,
                basis_context_compiled_sequence=(
                    plan.target_estimate.basis_context_compiled_sequence
                ),
                target_fingerprint=plan.target_estimate.target_fingerprint,
                non_transcript_baseline_tokens=baseline,
                transcript_tokens_before=plan.target_estimate.transcript_tokens_before,
                estimated_tokens_before=plan.target_estimate.estimated_tokens_before,
                summary_tokens_reserved=plan.target_estimate.summary_tokens_reserved,
                retained_transcript_tokens=(
                    plan.target_estimate.retained_transcript_tokens
                ),
                protected_transcript_tokens=(
                    plan.target_estimate.protected_transcript_tokens
                ),
                summary_tokens_actual=summary_tokens_actual,
                transcript_tokens_after=transcript_tokens_after,
                estimated_tokens_after=estimated_tokens_after,
                predicted_post_target_reached=predicted,
            )
            if predicted is False:
                raise CompactionTargetUnreachable(
                    "actual compacted context exceeds the resolved post-compaction target"
                )
            failure_stage = "artifact_write"
            await asyncio.to_thread(
                self.archive.put_text,
                artifact_id,
                summary,
                session_id=self.runtime_session_id,
                run_id=context.run_id,
                media_type="text/plain; charset=utf-8",
                metadata={
                    "kind": SUMMARY_ARTIFACT_KIND,
                    "do_not_write_back": True,
                    "compaction_id": compaction_id,
                    "trigger": trigger,
                    "reason": reason,
                    "window_number": plan.window_number,
                    "window_id": plan.window_id,
                    "through_sequence": plan.through_sequence,
                    "keep_after_sequence": plan.keep_after_sequence,
                    "included_run_ids": list(plan.included_run_ids),
                    "included_artifact_ids": list(plan.included_artifact_ids),
                    "target_estimate": completed_target_estimate.model_dump(
                        mode="json"
                    ),
                    **(event_metadata or {}),
                },
            )
            projection_admission = self._prepare_candidate_projection_admission(
                compaction_id=compaction_id,
                expected_completed_event_id=terminal_event_id,
                raw_summary=raw_summary,
                summary=summary,
                summary_artifact_id=artifact_id,
                trigger=trigger,
                phase=phase,
            )
            completed = ContextCompactionCompletedEvent(
                id=terminal_event_id,
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                summary_artifact_id=artifact_id,
                summary_chars=len(summary),
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=plan.threshold_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                target_estimate=completed_target_estimate,
                summarizer_call=call_result.resolved_call,
                summarizer_context_id=summarizer_context.context_id or "",
                summarizer_input_estimated_tokens=summarizer_input_estimated_tokens,
                summarizer_input_budget_tokens=(
                    summarizer_target.fact.context_budget.input_budget_tokens
                ),
                summarizer_usage_status=call_result.usage_status,
                summarizer_usage=call_result.usage,
                summarizer_estimated_input_tokens=call_result.estimated_input_tokens,
                summarizer_reported_model_id=call_result.reported_model_id,
                predicted_post_target_reached=predicted,
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                included_run_ids=list(plan.included_run_ids),
                included_artifact_ids=list(plan.included_artifact_ids),
                started_event_id=started_event_id,
                host_boundary_id=host_boundary_id,
                host_boundary_kind=host_boundary_kind,
                metadata=metadata,
            )
            failure_stage = "completed_append"
            completed_deadline_budget = self._new_event_write_deadline_budget()
            self._freeze_terminal_candidate(
                started_event_id,
                completed,
                deadline_budget=completed_deadline_budget,
            )
            try:
                stored_event = await self._commit_event(
                    completed,
                    deadline_budget=completed_deadline_budget,
                )
            except CompactionCommitCancelledAfterCommit as cancelled_commit:
                stored_event = cancelled_commit.result.committed_event
                if not isinstance(stored_event, ContextCompactionCompletedEvent):
                    raise RuntimeError(
                        "compaction Completed cancellation confirmation type mismatch"
                    )
                terminal_committed = True
                self._acknowledge_terminal_candidate(stored_event)
                raise
            if not isinstance(stored_event, ContextCompactionCompletedEvent):
                raise RuntimeError(
                    "compaction Completed commit returned wrong event type"
                )
            stored = stored_event
            terminal_committed = True
            self._acknowledge_terminal_candidate(stored)
            collector.candidate_projection_receipt = (
                self._install_candidate_projection_owner(
                    admission=projection_admission,
                    completed=stored,
                    publication_failed=collector.publication_failed,
                )
            )
            self._consecutive_failures = 0
            return stored
        except BaseException as exc:
            if summarizer_provider_input is not None and self.runtime_session is not None:
                await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                    summarizer_provider_input.prepared_candidate.preparation_ownership.preparation_id,
                    reason="compaction_failed_before_model_start",
                )
            if isinstance(exc, CompactionCommitPendingAfterCancellation):
                # The service now owns the still-running Started write.  Close or
                # the next safe point will resolve it and, if committed, append
                # the stable recovery terminal fact.
                raise
            if terminal_committed:
                raise
            terminal_owner = self._pending_terminalizations.get(started_event_id)
            if (
                started_committed is not None
                and terminal_owner is not None
                and terminal_owner.terminal_candidate is not None
            ):
                # The first terminal payload and its write budget are immutable.
                # The session-owned drain retries that exact candidate.
                raise
            self._consecutive_failures += 1
            if failure_stage == "model_stream" and isinstance(
                exc,
                (
                    ModelInputBudgetExceeded,
                    ModelInputEstimateMismatch,
                    ModelContextIdentityMismatch,
                    ModelTargetCapabilityMismatch,
                    ModelTargetBindingMismatch,
                ),
            ):
                failure_stage = "model_validation"
            estimate = getattr(exc, "estimate", None)
            failed = ContextCompactionFailedEvent(
                id=(
                    terminal_event_id if started_committed is not None else uuid4().hex
                ),
                **context.event_fields(),
                compaction_id=compaction_id,
                trigger=trigger,
                reason=reason,
                window_number=plan.window_number,
                window_id=plan.window_id,
                target_model_target=target_model_target.fact,
                target_input_budget_tokens=(
                    target_model_target.fact.context_budget.input_budget_tokens
                ),
                threshold_tokens=plan.threshold_tokens,
                post_compaction_target_tokens=plan.post_compaction_target_tokens,
                failure_stage=failure_stage,
                target_estimate=completed_target_estimate,
                observed_after_measurement=observed_after_measurement,
                summarizer_target=(
                    summarizer_target.fact if summarizer_target is not None else None
                ),
                summarizer_call=(
                    summarizer_call.fact if summarizer_call is not None else None
                ),
                summarizer_context_id=(
                    summarizer_context.context_id
                    if summarizer_context is not None
                    else None
                ),
                summarizer_input_estimated_tokens=summarizer_input_estimated_tokens,
                summarizer_input_budget_tokens=(
                    summarizer_target.fact.context_budget.input_budget_tokens
                    if summarizer_target is not None
                    else None
                ),
                summarizer_usage_status=(
                    call_result.usage_status if call_result is not None else "missing"
                ),
                summarizer_usage=call_result.usage if call_result is not None else None,
                summarizer_estimated_input_tokens=(
                    call_result.estimated_input_tokens
                    if call_result is not None
                    else estimate.total_input_tokens
                    if estimate is not None
                    else summarizer_input_estimated_tokens
                ),
                summarizer_reported_model_id=(
                    call_result.reported_model_id if call_result is not None else None
                ),
                through_sequence=plan.through_sequence,
                keep_after_sequence=plan.keep_after_sequence,
                error_type=type(exc).__name__,
                message=str(exc),
                started_event_id=(
                    started_event_id if started_committed is not None else None
                ),
                termination_kind=(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                ),
                host_boundary_id=host_boundary_id,
                host_boundary_kind=host_boundary_kind,
                metadata=metadata,
            )
            if started_committed is not None:
                failed_deadline_budget = self._new_event_write_deadline_budget()
                self._freeze_terminal_candidate(
                    started_event_id,
                    failed,
                    deadline_budget=failed_deadline_budget,
                )
            else:
                failed_deadline_budget = self._new_event_write_deadline_budget()
            try:
                committed_failed = await self._commit_event(
                    failed,
                    deadline_budget=failed_deadline_budget,
                )
            except CompactionCommitCancelledAfterCommit as cancelled_failed:
                # The stable terminal fact is durable; preserve the original
                # cancellation/architecture exception after ownership closes.
                self._acknowledge_terminal_candidate(
                    cancelled_failed.result.committed_event
                )
                raise exc
            if started_committed is not None:
                self._acknowledge_terminal_candidate(committed_failed)
            if not isinstance(exc, Exception):
                raise
            if trigger == "manual":
                raise
            return None

    def _build_summarizer_context(
        self,
        plan: CompactionPlan,
        *,
        call,
        trigger: ContextCompactionTrigger,
        phase: str | None,
    ) -> tuple[LLMContext, int]:
        prompt = production_compaction_prompt(
            memory_candidates_enabled=_memory_candidate_extraction_enabled(
                self.candidate_sink,
                trigger,
                phase=phase,
                policy=self.policy.memory_candidates,
            )
        )

        def build(input_text: str) -> LLMContext:
            return LLMContext(
                messages=(
                    LLMMessage.runtime_request(
                        input_text,
                        request_kind="compaction_request",
                        business_occurrence_semantic_fingerprint=context_fingerprint(
                            "compaction-runtime-request:v1",
                            (plan.window_id, input_text),
                        ),
                    ),
                ),
                system_prompt=prompt,
                tools=(),
                context_id=f"context:compaction:{plan.window_id}",
                resolved_model_call_id=call.fact.resolved_model_call_id,
                target_fingerprint=call.target.fact.target_fingerprint,
                model_call_index=None,
            )

        context = build(build_compaction_input(plan))
        estimate = call.target.token_estimator.estimate_context(context)
        budget = call.target.fact.context_budget.input_budget_tokens
        if estimate.total_input_tokens > budget:
            context = build(build_metadata_only_compaction_input(plan))
            estimate = call.target.token_estimator.estimate_context(context)
        if estimate.total_input_tokens > budget:
            exc = CompactionSummarizerInputBudgetExceeded(
                f"compaction summarizer input {estimate.total_input_tokens} exceeds budget {budget}"
            )
            exc.estimate = estimate  # type: ignore[attr-defined]
            raise exc
        return context, estimate.total_input_tokens

    async def _summarize(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
        provider_input,
    ) -> DirectModelCallResult:
        if self.runtime_session is None:
            raise RuntimeError(
                "context compaction model execution requires RuntimeSession ownership"
            )
        try:
            if provider_input.carrier.to_llm_context(context) != context:
                raise RuntimeError("compaction provider input changed after Started")
            start_bundle = prepare_model_lifecycle_start_bundle(
                call=call,
                context=context,
                event_context=event_context,
                runtime_session=self.runtime_session,
                lifecycle_kind="direct_internal_call",
                provider_input_start_bundle=provider_input,
            )
            handle = self.llm_runtime.start_stream(
                call=call,
                context=context,
                event_context=event_context,
                start_bundle=start_bundle,
                commit_port=RuntimeSessionModelStreamEventCommitPort(
                    runtime_session=self.runtime_session,
                    state=None,
                ),
                execution_registry=(
                    self.runtime_session.model_stream_execution_registry
                ),
            )
        except BaseException:
            await self.runtime_session.provider_input_generation_coordinator.abandon_uncommitted_preparation(
                provider_input.prepared_candidate.preparation_ownership.preparation_id,
                reason="one_shot_failed_before_start",
            )
            raise
        return await collect_direct_model_call_handle(
            handle,
            expected_call=call,
            runtime_session_id=self.runtime_session_id,
        )

    def _prepare_candidate_projection_admission(
        self,
        *,
        compaction_id: str,
        expected_completed_event_id: str,
        raw_summary: str,
        summary: str,
        summary_artifact_id: str,
        trigger: ContextCompactionTrigger,
        phase: str | None,
    ) -> _PreparedCandidateProjectionAdmission | None:
        sink = self.candidate_sink
        policy = self.policy.memory_candidates
        if not _memory_candidate_extraction_enabled(
            sink,
            trigger,
            phase=phase,
            policy=policy,
        ):
            return None
        contract = compaction_extractor_contract(policy)
        policy_fingerprint = context_fingerprint(
            "compaction-candidate-projection-policy:v1",
            {
                "enabled": policy.enabled,
                "extract_on_manual": policy.extract_on_manual,
                "extract_on_preflight": policy.extract_on_preflight,
                "extract_on_mid_turn": policy.extract_on_mid_turn,
                "missing_candidates_block_policy": (
                    policy.missing_candidates_block_policy
                ),
                "max_candidates_per_compaction": (
                    policy.max_candidates_per_compaction
                ),
                "max_summary_excerpt_chars": policy.max_summary_excerpt_chars,
                "max_provenance_ids": policy.max_provenance_ids,
                "extractor_version": policy.extractor_version,
            },
        )
        request_payload = {
            "request_id": (
                f"compaction-candidate-projection:{compaction_id}"
            ),
            "compaction_id": compaction_id,
            "expected_completed_event_id": expected_completed_event_id,
            "extractor_id": contract.extractor_id,
            "extractor_version": contract.extractor_version,
            "extractor_contract_fingerprint": contract.contract_fingerprint,
            "projection_policy_fingerprint": policy_fingerprint,
        }
        request = CompactionCandidateProjectionRequestIdentity(
            **request_payload,
            request_fingerprint=context_fingerprint(
                "compaction-candidate-projection-request:v1",
                request_payload,
            ),
        )
        try:
            summary_bytes = summary.encode("utf-8")
            prepared_payload = {
                "request_identity": request,
                "owner_id": (
                    "compaction-candidate-owner:"
                    + request.request_fingerprint.removeprefix("sha256:")
                ),
                "summary_artifact_id": summary_artifact_id,
                "summary_artifact_content_fingerprint": context_fingerprint(
                    "compaction-summary-artifact-content:v1",
                    summary,
                ),
                "owned_summary_canonical_utf8_bytes": bytes(summary_bytes),
            }
            prepared = PreparedCompactionCandidateProjectionInput(
                **prepared_payload,
                prepared_input_fingerprint=context_fingerprint(
                    "prepared-compaction-candidate-projection-input:v1",
                    {
                        "request_identity": request,
                        "owner_id": prepared_payload["owner_id"],
                        "summary_artifact_id": summary_artifact_id,
                        "summary_artifact_content_fingerprint": (
                            prepared_payload[
                                "summary_artifact_content_fingerprint"
                            ]
                        ),
                        "owned_summary_canonical_utf8": summary,
                    },
                ),
            )
        except BaseException as error:
            return _PreparedCandidateProjectionAdmission(
                request_identity=request,
                prepared_input=None,
                preparation_failure=build_bounded_runtime_failure_diagnostic(
                    error=error,
                    redaction_profile_id=(
                        "compaction_candidate_projection_preparation_error.v1"
                    ),
                ),
                raw_summary=str(raw_summary),
                summary=str(summary),
                phase=phase,
            )
        return _PreparedCandidateProjectionAdmission(
            request_identity=request,
            prepared_input=prepared,
            preparation_failure=None,
            raw_summary=str(raw_summary),
            summary=str(summary),
            phase=phase,
        )

    def _install_candidate_projection_owner(
        self,
        *,
        admission: _PreparedCandidateProjectionAdmission | None,
        completed: ContextCompactionCompletedEvent,
        publication_failed: bool,
    ) -> CompactionCandidateProjectionReceipt:
        completed_reference = event_reference_from_stored(
            completed,
            runtime_session_id=self.runtime_session_id,
        )
        empty = {
            "owner_id": None,
            "prepared_input_fingerprint": None,
            "failure_stage": None,
            "failure_diagnostic": None,
            "producer_event_id": None,
            "producer_payload_fingerprint": None,
            "producer_event_reference": None,
            "outbox_item_accumulator": None,
            "reconciliation_from_status": None,
        }
        if admission is None:
            return CompactionCandidateProjectionReceipt(
                completed_compaction_event_reference=completed_reference,
                request_identity=None,
                status="not_requested",
                **empty,
            )
        if admission.preparation_failure is not None:
            return CompactionCandidateProjectionReceipt(
                completed_compaction_event_reference=completed_reference,
                request_identity=admission.request_identity,
                status="preparation_failed",
                **{
                    **empty,
                    "failure_stage": "prepared_input_factory",
                    "failure_diagnostic": admission.preparation_failure,
                },
            )
        prepared = admission.prepared_input
        if prepared is None:
            raise RuntimeError("candidate projection admission lost prepared input")
        if publication_failed:
            return CompactionCandidateProjectionReceipt(
                completed_compaction_event_reference=completed_reference,
                request_identity=admission.request_identity,
                status="suppressed_by_publication_latch",
                **{
                    **empty,
                    "prepared_input_fingerprint": (
                        prepared.prepared_input_fingerprint
                    ),
                },
            )
        if (
            not self._candidate_projection_accepting
            or self.candidate_projection_commit_port is None
        ):
            diagnostic = build_bounded_runtime_failure_diagnostic(
                error=RuntimeError(
                    "compaction candidate projection owner admission is unavailable"
                ),
                redaction_profile_id=(
                    "compaction_candidate_projection_owner_installation_error.v1"
                ),
            )
            return CompactionCandidateProjectionReceipt(
                completed_compaction_event_reference=completed_reference,
                request_identity=admission.request_identity,
                status="owner_installation_failed",
                **{
                    **empty,
                    "prepared_input_fingerprint": (
                        prepared.prepared_input_fingerprint
                    ),
                    "failure_stage": "owner_installation",
                    "failure_diagnostic": diagnostic,
                },
            )
        owner_id = prepared.owner_id
        if owner_id in self._candidate_projection_tasks:
            raise RuntimeError("compaction candidate projection owner already exists")
        installed = CompactionCandidateProjectionReceipt(
            completed_compaction_event_reference=completed_reference,
            request_identity=admission.request_identity,
            status="owner_installed",
            **{
                **empty,
                "owner_id": owner_id,
                "prepared_input_fingerprint": prepared.prepared_input_fingerprint,
            },
        )
        self._candidate_projection_receipts[owner_id] = installed
        task = asyncio.create_task(
            self._drive_candidate_projection_owner(
                admission=admission,
                completed=completed.model_copy(deep=True),
                completed_reference=completed_reference,
            ),
            name=f"pulsara-compaction-candidate-projection:{owner_id}",
        )
        self._candidate_projection_tasks[owner_id] = task
        task.add_done_callback(
            lambda done, stable_owner_id=owner_id: (
                self._retire_candidate_projection_owner(
                    stable_owner_id,
                    done,
                )
            )
        )
        return installed

    async def _drive_candidate_projection_owner(
        self,
        *,
        admission: _PreparedCandidateProjectionAdmission,
        completed: ContextCompactionCompletedEvent,
        completed_reference: ContextEventReferenceFact,
    ) -> None:
        prepared = admission.prepared_input
        if prepared is None:
            raise RuntimeError("candidate projection owner has no prepared input")
        owner_id = prepared.owner_id
        try:
            proposal = await self._append_memory_candidate_proposals_if_enabled(
                raw_summary=admission.raw_summary,
                summary=admission.summary,
                completed=completed,
                summary_artifact_id=prepared.summary_artifact_id,
                phase=admission.phase,
            )
        except BaseException:
            current = self._candidate_projection_receipts[owner_id]
            self._candidate_projection_receipts[owner_id] = (
                CompactionCandidateProjectionReceipt(
                    completed_compaction_event_reference=completed_reference,
                    request_identity=admission.request_identity,
                    status="reconciliation_required",
                    owner_id=owner_id,
                    prepared_input_fingerprint=(
                        prepared.prepared_input_fingerprint
                    ),
                    failure_stage=None,
                    failure_diagnostic=None,
                    producer_event_id=current.producer_event_id,
                    producer_payload_fingerprint=(
                        current.producer_payload_fingerprint
                    ),
                    producer_event_reference=current.producer_event_reference,
                    outbox_item_accumulator=current.outbox_item_accumulator,
                    reconciliation_from_status=(
                        "candidate_frozen"
                        if current.producer_event_id is not None
                        else "owner_installed"
                    ),
                )
            )
            raise
        if proposal is None:
            return
        producer, rows = proposal
        producer_payload_fingerprint = stable_event_identity(
            producer,
            runtime_session_id=self.runtime_session_id,
        ).payload_fingerprint
        self._candidate_projection_receipts[owner_id] = (
            CompactionCandidateProjectionReceipt(
                completed_compaction_event_reference=completed_reference,
                request_identity=admission.request_identity,
                status="candidate_frozen",
                owner_id=owner_id,
                prepared_input_fingerprint=prepared.prepared_input_fingerprint,
                failure_stage=None,
                failure_diagnostic=None,
                producer_event_id=producer.id,
                producer_payload_fingerprint=producer_payload_fingerprint,
                producer_event_reference=None,
                outbox_item_accumulator=None,
                reconciliation_from_status=None,
            )
        )
        if self.candidate_projection_commit_port is None:
            raise RuntimeError(
                "compaction candidate producer requires projection commit ownership"
            )
        result = await self.candidate_projection_commit_port.commit_producer_bundle(
            producer_event=producer,
            rows=rows,
        )
        committed = next(
            event for event in result.committed_events if event.id == producer.id
        )
        producer_reference = event_reference_from_stored(
            committed,
            runtime_session_id=self.runtime_session_id,
        )
        self._candidate_projection_receipts[owner_id] = (
            CompactionCandidateProjectionReceipt(
                completed_compaction_event_reference=completed_reference,
                request_identity=admission.request_identity,
                status=(
                    "producer_bundle_full"
                    if self.candidate_projection_commit_port is not None
                    and self.candidate_projection_commit_port.dispatch_retry_required
                    else "projection_applied"
                ),
                owner_id=owner_id,
                prepared_input_fingerprint=prepared.prepared_input_fingerprint,
                failure_stage=None,
                failure_diagnostic=None,
                producer_event_id=producer.id,
                producer_payload_fingerprint=producer_payload_fingerprint,
                producer_event_reference=producer_reference,
                outbox_item_accumulator=ordered_fingerprint_accumulator(
                    "compaction-candidate-projection-outbox:v1",
                    tuple(row.item.item_fingerprint for row in rows),
                ),
                reconciliation_from_status=None,
            )
        )

    def _retire_candidate_projection_owner(
        self,
        owner_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._candidate_projection_tasks.get(owner_id) is task:
            self._candidate_projection_tasks.pop(owner_id, None)
        if not task.cancelled():
            task.exception()

    async def stop_candidate_projection_admission_and_drain(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        self._candidate_projection_accepting = False
        await self.drain_candidate_projection_owners(
            deadline_monotonic=deadline_monotonic
        )
        port = self.candidate_projection_commit_port
        if port is not None:
            await port.stop_admission_and_drain(
                deadline_monotonic=deadline_monotonic,
            )

    async def drain_candidate_projection_owners(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Wait for already-installed process owners without closing admission."""

        tasks = tuple(self._candidate_projection_tasks.values())
        if tasks:
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "compaction candidate projection drain deadline expired"
                )
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                raise TimeoutError(
                    "compaction candidate projection owners did not drain"
                )
            for task in done:
                task.result()

    async def _append_memory_candidate_proposals_if_enabled(
        self,
        *,
        raw_summary: str,
        summary: str,
        completed: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        phase: str | None,
    ):
        sink = self.candidate_sink
        policy = self.policy.memory_candidates
        if not _memory_candidate_extraction_enabled(
            sink,
            completed.trigger,
            phase=phase,
            policy=policy,
        ):
            return None
        assert sink is not None
        parse_result = parse_compaction_memory_candidates(
            raw_summary,
            workspace_scope=sink.workspace_scope,
            workspace_kind=sink.workspace_kind,
            policy=policy,
        )
        if not _extraction_attempted(parse_result):
            return None
        try:
            append_result = await asyncio.to_thread(
                sink.prepare_compaction_candidates,
                completed_event=completed,
                summary_artifact_id=summary_artifact_id,
                summary_text=summary,
                parse_result=parse_result,
                policy=policy,
            )
        except Exception as exc:
            diagnostic = CompactionCandidateDiagnostic(
                code="compaction_candidate_preparation_failed",
                message=type(exc).__name__,
                redacted=True,
            )
            append_result = CompactionCandidateAppendResult(
                source_event_id=completed.id,
                source_event_sequence=int(completed.sequence or 0),
                source_artifact_id=summary_artifact_id,
                entry_ids=(),
                diagnostics=(diagnostic,),
            )
        # Once the producer candidate is built, its ID and payload are stable.
        # Durable commit errors must propagate for same-candidate recovery; they
        # must never be rewritten as a zero-candidate preparation diagnostic.
        return self._prepare_memory_candidates_proposed_bundle(
            completed=completed,
            summary_artifact_id=summary_artifact_id,
            summary=summary,
            parse_result=parse_result,
            append_result=append_result,
            diagnostics=(),
        )

    def _prepare_memory_candidates_proposed_bundle(
        self,
        *,
        completed: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        summary: str,
        parse_result: CompactionCandidateParseResult,
        append_result: CompactionCandidateAppendResult,
        diagnostics: tuple[CompactionCandidateDiagnostic, ...],
    ):
        all_diagnostics = (
            *parse_result.diagnostics,
            *append_result.diagnostics,
            *_skipped_item_diagnostics(parse_result),
            *_skipped_item_diagnostics(append_result),
            *diagnostics,
        )
        skipped_count = len(parse_result.skipped) + len(append_result.skipped)
        error_count = sum(
            1
            for diagnostic in all_diagnostics
            if not diagnostic.code.startswith("compaction_candidate_skipped:")
            and ("failed" in diagnostic.code or "malformed" in diagnostic.code)
        )
        event = ContextCompactionMemoryCandidatesProposedEvent(
            id=f"context-compaction:{completed.compaction_id}:memory-candidates",
            **EventContext(
                run_id=completed.run_id,
                turn_id=completed.turn_id,
                reply_id=completed.reply_id,
            ).event_fields(),
            compaction_id=completed.compaction_id,
            source_event_id=append_result.source_event_id,
            source_event_sequence=append_result.source_event_sequence,
            summary_artifact_id=summary_artifact_id,
            candidate_entry_ids=list(append_result.entry_ids),
            attempted_count=parse_result.attempted_count,
            proposed_count=len(append_result.entry_ids),
            skipped_count=skipped_count,
            duplicate_count=append_result.duplicate_count,
            error_count=error_count,
            extractor_version=self.policy.memory_candidates.extractor_version,
            diagnostics=[
                _event_diagnostic(diagnostic) for diagnostic in all_diagnostics
            ],
            summary_content_sha256=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            summary_content_bytes=len(summary.encode("utf-8")),
            extractor_contract=compaction_extractor_contract(
                self.policy.memory_candidates
            ),
            ordered_candidate_attributions=append_result.attributions,
            completed_compaction_event_identity=stable_event_identity(
                completed,
                runtime_session_id=self.runtime_session_id,
            ),
        )
        producer_identity = stable_event_identity(
            event,
            runtime_session_id=self.runtime_session_id,
        )
        projected_candidates = tuple(
            candidate.model_copy(
                update={
                    "source_event_id": event.id,
                    "metadata": {
                        **candidate.metadata,
                        "source_event_id": event.id,
                        "compaction_completed_event_id": completed.id,
                    },
                }
            )
            for candidate in append_result.candidates
        )
        rows = tuple(
            CandidateProjectionOutboxRow(
                item=build_frozen_fact(
                    CandidateProjectionOutboxItemFact,
                    schema_version="candidate_projection_outbox_item.v1",
                    producer_kind=CandidateProjectionProducerKind.COMPACTION,
                    producer_event_identity=producer_identity,
                    candidate_entry_id=candidate.entry_id,
                    candidate_index=index,
                    candidate_payload=candidate.payload,
                    candidate_payload_fingerprint=candidate_payload_fingerprint(
                        candidate.payload
                    ),
                    candidate_attribution_fingerprint=(
                        append_result.attributions[index].attribution_fingerprint
                    ),
                ),
                candidate=candidate,
            )
            for index, candidate in enumerate(projected_candidates)
        )
        return event, rows

    def _build_plan(
        self,
        events: list[AgentEvent],
        *,
        compaction_id: str,
        target_model_target: ResolvedModelTarget,
        current_user_input_if_not_already_represented: str = "",
        model_visible_messages_before: list[Msg] | tuple[Msg, ...] | None = None,
        protected_model_visible_messages_after: tuple[LLMMessage, ...] = (),
        force: bool = False,
        max_compactable_sequence: int | None = None,
        keep_recent_runs_override: int | None = None,
    ) -> CompactionPlan | None:
        if not events:
            return None
        latest_boundary = latest_completed_boundary(
            events, archive=self.archive, session_id=self.runtime_session_id
        )
        last_keep_after = (
            latest_boundary.keep_after_sequence if latest_boundary is not None else 0
        )
        candidate_events = [
            event
            for event in events
            if event.sequence is not None
            and event.sequence > last_keep_after
            and (
                max_compactable_sequence is None
                or event.sequence <= max_compactable_sequence
            )
        ]
        if not candidate_events:
            return None
        transcript_messages = (
            list(model_visible_messages_before)
            if model_visible_messages_before is not None
            else model_visible_messages_from_events(candidate_events)
        )
        transcript_tokens_before = _estimate_transcript_messages(
            transcript_messages,
            current_user_input=current_user_input_if_not_already_represented,
            target_model_target=target_model_target,
            previous_summary_text=(
                latest_boundary.summary_text
                if latest_boundary is not None and model_visible_messages_before is None
                else None
            ),
        )
        basis = _latest_matching_context_compiled_event(
            events,
            target_fingerprint=target_model_target.fact.target_fingerprint,
            max_sequence=max_compactable_sequence,
        )
        baseline = (
            basis.budget.non_transcript_baseline_tokens if basis is not None else None
        )
        estimate_scope = (
            "compiled_context_baseline" if baseline is not None else "transcript_only"
        )
        estimated_before = transcript_tokens_before + (baseline or 0)
        threshold_tokens = max(
            1,
            math.floor(
                target_model_target.fact.context_budget.input_budget_tokens
                * self.policy.auto_trigger_ratio
            ),
        )
        post_target = max(
            1,
            math.floor(
                target_model_target.fact.context_budget.input_budget_tokens
                * self.policy.post_compaction_target_ratio
            ),
        )
        if not force and estimated_before < threshold_tokens:
            return None
        if (
            not force
            and len(candidate_events) < self.policy.min_events_after_last_compact
        ):
            return None
        keep_recent_runs = (
            keep_recent_runs_override
            if keep_recent_runs_override is not None
            else self.policy.keep_recent_runs
        )
        keep_after_sequence = _keep_after_sequence_for_recent_runs(
            candidate_events, keep_recent_runs
        )
        if keep_after_sequence <= last_keep_after:
            if keep_recent_runs_override is not None and not force:
                return None
            keep_after_sequence = max(event.sequence or 0 for event in candidate_events)
        compacted = tuple(
            event
            for event in candidate_events
            if (event.sequence or 0) <= keep_after_sequence
        )
        tail = tuple(
            event
            for event in candidate_events
            if (event.sequence or 0) > keep_after_sequence
        )
        if not compacted:
            return None
        through_sequence = max(event.sequence or 0 for event in compacted)
        retained_transcript_tokens = _estimate_transcript_messages(
            model_visible_messages_from_events(tail),
            current_user_input=current_user_input_if_not_already_represented,
            target_model_target=target_model_target,
            previous_summary_text=None,
        )
        protected_transcript_tokens = sum(
            target_model_target.token_estimator.estimate_message(message)
            for message in protected_model_visible_messages_after
        )
        next_window_number = _next_window_number(events)
        window_id = f"context_window:{next_window_number}:{uuid4().hex}"
        replay_template = CompactionSummaryReplayTemplate(
            summary_artifact_id=_summary_artifact_id(compaction_id),
            compaction_id=compaction_id,
            window_id=window_id,
            through_sequence=through_sequence,
            keep_after_sequence=through_sequence,
        )
        summary_tokens_reserved = estimate_compaction_summary_replay_tokens(
            replay_template=replay_template,
            summary_text="x" * self.policy.max_summary_chars,
            target_model_target=target_model_target,
        )
        if (
            baseline is not None
            and baseline
            + summary_tokens_reserved
            + retained_transcript_tokens
            + protected_transcript_tokens
            > post_target
        ):
            raise CompactionTargetUnreachable(
                "mandatory retained context cannot meet the post-compaction target"
            )
        target_estimate = CompactionTargetEstimateFact(
            estimate_scope=estimate_scope,
            basis_context_id=basis.context_id if basis is not None else None,
            basis_context_compiled_sequence=basis.sequence
            if basis is not None
            else None,
            target_fingerprint=target_model_target.fact.target_fingerprint,
            non_transcript_baseline_tokens=baseline,
            transcript_tokens_before=transcript_tokens_before,
            estimated_tokens_before=estimated_before,
            summary_tokens_reserved=summary_tokens_reserved,
            retained_transcript_tokens=retained_transcript_tokens,
            protected_transcript_tokens=protected_transcript_tokens,
            summary_tokens_actual=None,
            transcript_tokens_after=None,
            estimated_tokens_after=None,
            predicted_post_target_reached=None,
        )
        return CompactionPlan(
            through_sequence=through_sequence,
            keep_after_sequence=through_sequence,
            target_estimate=target_estimate,
            threshold_tokens=threshold_tokens,
            post_compaction_target_tokens=post_target,
            retained_transcript_tokens=retained_transcript_tokens,
            protected_transcript_tokens=protected_transcript_tokens,
            included_run_ids=tuple(dict.fromkeys(event.run_id for event in compacted)),
            included_artifact_ids=tuple(_artifact_ids(compacted)),
            compacted_events=compacted,
            tail_events=tail,
            window_number=next_window_number,
            window_id=window_id,
            previous_summary_artifact_id=(
                latest_boundary.event.summary_artifact_id
                if latest_boundary is not None
                else None
            ),
            previous_summary_text=latest_boundary.summary_text
            if latest_boundary is not None
            else None,
        )


def production_compaction_prompt(*, memory_candidates_enabled: bool = True) -> str:
    prompt = (
        resources.files(_PRODUCTION_PROMPT_PACKAGE)
        .joinpath(_PRODUCTION_PROMPT_FILE)
        .read_text(encoding="utf-8")
    )
    if memory_candidates_enabled:
        return prompt
    return _without_memory_candidate_instructions(prompt)


def _without_memory_candidate_instructions(prompt: str) -> str:
    prompt = prompt.replace(
        "- Your entire response must be plain text: an <analysis> block followed by a <summary> block, plus an optional <memory_candidates_json> block only when durable-memory candidate extraction is useful.",
        "- Your entire response must be plain text: an <analysis> block followed by a <summary> block.",
    )
    prompt = prompt.replace(
        "   - You may optionally propose durable-memory candidates in <memory_candidates_json>; those proposals are pending observations only and governance decides whether to persist them.\n",
        "",
    )
    optional_start = prompt.find("\nOptional memory-candidate block:")
    rules_start = prompt.find("\nRules:", optional_start)
    if optional_start != -1 and rules_start != -1:
        prompt = prompt[:optional_start] + prompt[rules_start:]
    return prompt


def _memory_candidate_extraction_enabled(
    sink: CompactionMemoryCandidateSink | None,
    trigger: ContextCompactionTrigger,
    *,
    phase: str | None,
    policy: ContextCompactionMemoryCandidatePolicy,
) -> bool:
    if sink is None:
        return False
    if sink.workspace_kind == "transient":
        return False
    if not sink.workspace_scope:
        return False
    return _should_extract_memory_candidates(trigger, phase=phase, policy=policy)


def _should_extract_memory_candidates(
    trigger: ContextCompactionTrigger,
    *,
    phase: str | None,
    policy: ContextCompactionMemoryCandidatePolicy,
) -> bool:
    if not policy.enabled:
        return False
    if phase == "mid_turn":
        return policy.extract_on_mid_turn
    if trigger == "manual":
        return policy.extract_on_manual
    return policy.extract_on_preflight


def _extraction_attempted(parse_result: CompactionCandidateParseResult) -> bool:
    return bool(
        parse_result.attempted_count
        or parse_result.candidates
        or parse_result.skipped
        or parse_result.diagnostics
    )


def _event_diagnostic(
    diagnostic: CompactionCandidateDiagnostic,
) -> CompactionCandidateDiagnosticEvent:
    return CompactionCandidateDiagnosticEvent(
        code=diagnostic.code,
        field=diagnostic.field,
        message=diagnostic.message,
        redacted=diagnostic.redacted,
    )


def _skipped_item_diagnostics(
    result: CompactionCandidateParseResult | CompactionCandidateAppendResult,
) -> tuple[CompactionCandidateDiagnostic, ...]:
    return tuple(
        CompactionCandidateDiagnostic(
            code=f"compaction_candidate_skipped:{item.code}",
            message=item.reason,
            redacted=item.redacted,
        )
        for item in result.skipped
    )


def build_compaction_input(plan: CompactionPlan) -> str:
    lines = [
        "# Pulsara compaction input",
        "",
        "The following canonical event-derived transcript prefix will be summarized.",
        f"through_sequence: {plan.through_sequence}",
        f"keep_after_sequence: {plan.keep_after_sequence}",
        f"included_run_ids: {', '.join(plan.included_run_ids)}",
        f"included_artifact_ids: {', '.join(plan.included_artifact_ids) or '(none)'}",
        f"estimated_model_visible_tokens_before: {plan.target_estimate.estimated_tokens_before}",
        f"estimate_scope: {plan.target_estimate.estimate_scope}",
        "",
        "## Event-derived messages and observations",
        "",
    ]
    if plan.previous_summary_text:
        lines.extend(
            [
                "## Previous compact summary to carry forward",
                "",
                (
                    "The next summary MUST preserve this previous handoff unless newer compacted events "
                    "explicitly supersede or correct it. Do not drop older context merely because only the "
                    "latest completed boundary is replayed at resume time."
                ),
                f"previous_summary_artifact_id: {plan.previous_summary_artifact_id or '(unknown)'}",
                "",
                plan.previous_summary_text.strip(),
                "",
            ]
        )
    lines.append(build_compaction_observation_text(plan.compacted_events))
    return "\n".join(lines)


def build_metadata_only_compaction_input(plan: CompactionPlan) -> str:
    """Deterministic degraded summarizer input without verbose event payloads."""

    return "\n".join(
        [
            "# Pulsara compaction metadata-only input",
            f"through_sequence: {plan.through_sequence}",
            f"keep_after_sequence: {plan.keep_after_sequence}",
            f"included_run_ids: {', '.join(plan.included_run_ids)}",
            f"included_artifact_ids: {', '.join(plan.included_artifact_ids) or '(none)'}",
            f"compacted_event_count: {len(plan.compacted_events)}",
            f"previous_summary_artifact_id: {plan.previous_summary_artifact_id or '(none)'}",
            "",
            "The detailed event representation exceeded the summarizer input budget. "
            "Summarize only the bounded metadata above and any previous summary below.",
            "",
            plan.previous_summary_text or "",
        ]
    )


def estimate_compaction_summary_replay_tokens(
    *,
    replay_template: CompactionSummaryReplayTemplate,
    summary_text: str,
    target_model_target: ResolvedModelTarget,
) -> int:
    rendered = render_compaction_summary(
        summary_text,
        summary_artifact_id=replay_template.summary_artifact_id,
        compaction_id=replay_template.compaction_id,
        window_id=replay_template.window_id,
        through_sequence=replay_template.through_sequence,
        keep_after_sequence=replay_template.keep_after_sequence,
    )
    return target_model_target.token_estimator.estimate_message(
        LLMMessage.runtime_observation(
            derived_text_runtime_observation_payload(
                derivation_kind="compaction_replacement_summary",
                model_visible_content=rendered,
                source_semantic_fingerprint=context_fingerprint(
                    "compaction-summary-replay-observation-source:v1",
                    (
                        replay_template.compaction_id,
                        replay_template.summary_artifact_id,
                        rendered,
                    ),
                ),
            ),
            observation_kind="compaction_replacement_summary",
            source_instance_id=f"compaction:{replay_template.compaction_id}",
            lifecycle_class="causal_append_once",
            authority_class="runtime_fact",
        )
    )


def _estimate_transcript_messages(
    messages: list[Msg] | tuple[Msg, ...],
    *,
    current_user_input: str,
    target_model_target: ResolvedModelTarget,
    previous_summary_text: str | None,
) -> int:
    estimator = target_model_target.token_estimator
    total = sum(
        estimator.estimate_message(_message_for_target_estimate(message))
        for message in messages
    )
    if current_user_input:
        total += estimator.estimate_message(LLMMessage.user(current_user_input))
    if previous_summary_text:
        total += estimator.estimate_message(
            LLMMessage.runtime_observation(
                derived_text_runtime_observation_payload(
                    derivation_kind="compaction_replacement_summary",
                    model_visible_content=previous_summary_text,
                    source_semantic_fingerprint=context_fingerprint(
                        "previous-compaction-summary-observation-source:v1",
                        previous_summary_text,
                    ),
                ),
                observation_kind="compaction_replacement_summary",
                source_instance_id="compaction:previous-summary",
                lifecycle_class="causal_append_once",
                authority_class="runtime_fact",
            )
        )
    return total


def _message_for_target_estimate(message: Msg) -> LLMMessage:
    text = _message_text_for_estimate(message)
    if message.role == "system":
        return LLMMessage.runtime_observation(
            transcript_lifecycle_observation_payload(
                lifecycle_segment="canonical_system_lifecycle",
                model_visible_content=text,
            ),
            observation_kind="lifecycle_observation",
            source_instance_id="transcript:canonical-system-lifecycle",
            lifecycle_class="causal_append_once",
            authority_class="runtime_fact",
        )
    if message.role == "assistant":
        return LLMMessage.assistant(text)
    if message.role == "tool_result":
        return LLMMessage.tool_result(text)
    return LLMMessage.user(text)


def _latest_matching_context_compiled_event(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
    *,
    target_fingerprint: str,
    max_sequence: int | None,
) -> ContextCompiledEvent | None:
    for event in reversed(events):
        if not isinstance(event, ContextCompiledEvent):
            continue
        if max_sequence is not None and (event.sequence or 0) > max_sequence:
            continue
        if (
            event.status != "compiled"
            or event.budget.measurement_stage != "final_payload"
        ):
            continue
        if event.resolved_call.target.target_fingerprint != target_fingerprint:
            continue
        if event.budget.non_transcript_baseline_tokens is None:
            continue
        return event
    return None


def _keep_after_sequence_for_recent_runs(
    events: list[AgentEvent], keep_recent_runs: int
) -> int:
    run_starts = [
        event
        for event in events
        if isinstance(event, RunStartEvent) and event.sequence is not None
    ]
    if len(run_starts) <= keep_recent_runs:
        return 0
    first_kept_run = run_starts[-keep_recent_runs]
    return (first_kept_run.sequence or 0) - 1


def _next_window_number(events: list[AgentEvent]) -> int:
    completed = [event for event in events if hasattr(event, "window_number")]
    numbers = [
        int(getattr(event, "window_number"))
        for event in completed
        if getattr(event, "window_number", None)
    ]
    return max(numbers, default=0) + 1


def _artifact_ids(events: tuple[AgentEvent, ...]) -> list[str]:
    artifact_ids: list[str] = []
    for event in events:
        if isinstance(event, ToolResultEndEvent):
            artifact_ids.extend(artifact.artifact_id for artifact in event.artifacts)
    return list(dict.fromkeys(artifact_ids))


def model_visible_messages_from_events(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
) -> list[Msg]:
    """Build a lightweight model-visible transcript estimate without Host imports."""

    accepted_reply_ids, controlled_reply_ids = _canonical_reply_control(events)
    messages: list[Msg] = []
    assistant_blocks_by_reply: dict[str, list[object]] = {}
    assembler = BlockAssembler()
    for event in events:
        if isinstance(event, RunStartEvent):
            messages.append(
                UserMsg(
                    name="user",
                    content=event.current_user_message.text,
                    id=event.current_user_message.message_id,
                    created_at=event.current_user_message.observed_at_utc,
                    metadata={"run_id": event.run_id},
                )
            )
            continue
        if (
            event.reply_id in controlled_reply_ids
            and event.reply_id not in accepted_reply_ids
        ):
            continue
        for completion in assembler.append(event).completed:
            block = completion.block
            if isinstance(block, ThinkingBlock):
                continue
            assistant_blocks_by_reply.setdefault(completion.reply_id, []).append(block)
        if hasattr(event, "type") and str(event.type) == "REPLY_END":
            blocks = assistant_blocks_by_reply.pop(event.reply_id, [])
            if blocks:
                messages.append(
                    AssistantMsg(
                        name="assistant",
                        content=blocks,
                        id=event.reply_id,
                        created_at=getattr(event, "created_at", None),
                    )
                )
    for reply_id, blocks in assistant_blocks_by_reply.items():
        if blocks:
            messages.append(AssistantMsg(name="assistant", content=blocks, id=reply_id))
    return messages


def build_compaction_observation_text(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
) -> str:
    accepted_reply_ids, controlled_reply_ids = _canonical_reply_control(events)
    lines: list[str] = []
    assembler = BlockAssembler()
    for event in events:
        if isinstance(event, RunStartEvent):
            rendered = event.current_user_message.text
            lines.append(
                f"[user run_id={event.run_id}] {_clip_text(rendered, _COMPACTION_TEXT_CLIP_CHARS)}"
            )
            continue
        if isinstance(event, RunEndEvent):
            if event.status != "finished" or event.abort_kind or event.error_message:
                lines.append(
                    "[run_end "
                    f"run_id={event.run_id} status={event.status} "
                    f"abort_kind={event.abort_kind or '(none)'} "
                    f"error={_clip_text(event.error_message or '', 500)}]"
                )
            continue
        if isinstance(event, RunErrorEvent):
            lines.append(
                f"[run_error run_id={event.run_id} code={event.code}] {_clip_text(event.message, 1000)}"
            )
            continue
        if isinstance(
            event,
            (
                PlanModeEnteredEvent,
                PlanQuestionAskedEvent,
                PlanQuestionAnsweredEvent,
                PlanExitRequestedEvent,
                PlanExitResolvedEvent,
                PlanModeExitedEvent,
            ),
        ):
            lines.append(_event_line(event))
            continue
        if (
            event.reply_id in controlled_reply_ids
            and event.reply_id not in accepted_reply_ids
        ):
            continue
        for completion in assembler.append(event).completed:
            rendered = _render_completed_block(completion.block)
            if rendered:
                lines.append(rendered)
    return "\n".join(lines)


def _canonical_reply_control(
    events: list[AgentEvent] | tuple[AgentEvent, ...],
) -> tuple[frozenset[str], frozenset[str]]:
    controlled = frozenset(
        event.reply_id
        for event in events
        if isinstance(event, ModelCallStartEvent)
        and event.recovery_plan.lifecycle_kind == "main_assistant_reply"
    )
    accepted = accepted_main_reply_ids(tuple(events))
    return accepted, controlled


def _render_completed_block(block: object) -> str:
    if isinstance(block, TextBlock):
        return f"[assistant] {_clip_text(block.text, _COMPACTION_TEXT_CLIP_CHARS)}"
    if isinstance(block, ThinkingBlock):
        return ""
    if isinstance(block, ToolCallBlock):
        return (
            f"[tool_call id={block.id} name={block.name} state={block.state}] "
            f"{_clip_text(block.input, _COMPACTION_TOOL_INPUT_CLIP_CHARS)}"
        )
    if isinstance(block, ToolResultBlock):
        artifact_text = ""
        if block.artifacts:
            refs = ", ".join(
                _artifact_ref_summary(artifact) for artifact in block.artifacts
            )
            artifact_text = f" artifacts=[{refs}]"
        output = "\n".join(_block_text(item) for item in block.output)
        return (
            f"[tool_result id={block.id} name={block.name} state={block.state}{artifact_text}] "
            f"{_clip_text(output, _COMPACTION_TOOL_RESULT_CLIP_CHARS)}"
        )
    if isinstance(block, HintBlock):
        return f"[hint source={block.source or '(unknown)'}] {_clip_text(_block_text(block), 1000)}"
    if isinstance(block, DataBlock):
        return _block_text(block)
    return ""


def _artifact_ref_summary(artifact) -> str:
    base = f"{artifact.artifact_id}({artifact.media_type}, {artifact.size_bytes} bytes)"
    preview = getattr(artifact, "preview", None)
    if preview is None:
        return base
    read_more = getattr(preview, "read_more", {}) or {}
    suggested_offset = read_more.get("suggested_offset_chars")
    return (
        f"{base}, preview_policy={preview.preview_policy}, "
        f"visible_head_chars={preview.visible_head_chars}, visible_tail_chars={preview.visible_tail_chars}, "
        f"suggested_offset_chars={suggested_offset}"
    )


def _block_text(block: object) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, ThinkingBlock):
        return block.thinking
    if isinstance(block, HintBlock):
        if isinstance(block.hint, str):
            return block.hint
        return "\n".join(_block_text(item) for item in block.hint)
    if isinstance(block, DataBlock):
        source = block.source
        media_type = getattr(source, "media_type", "application/octet-stream")
        if hasattr(source, "url"):
            return f"[data media_type={media_type} url={source.url}]"
        data = getattr(source, "data", "")
        return f"[data media_type={media_type} chars={len(data)}]"
    return str(block)


def _message_text_for_estimate(message: Msg) -> str:
    parts = [f"[{message.role} name={message.name} id={message.id}]"]
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            parts.append(block.thinking)
        elif isinstance(block, ToolCallBlock):
            parts.append(
                f"[tool_call id={block.id} name={block.name} state={block.state}] {block.input}"
            )
        elif isinstance(block, ToolResultBlock):
            artifacts = " ".join(artifact.artifact_id for artifact in block.artifacts)
            output = "\n".join(_block_text(item) for item in block.output)
            parts.append(
                f"[tool_result id={block.id} name={block.name} state={block.state} artifacts={artifacts}] {output}"
            )
        elif isinstance(block, HintBlock):
            parts.append(
                f"[hint source={block.source or '(unknown)'}] {_block_text(block)}"
            )
        elif isinstance(block, DataBlock):
            parts.append(_block_text(block))
        else:
            parts.append(str(block))
    return "\n".join(part for part in parts if part)


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n[CLIPPED: kept {limit} of {len(text)} chars]"
    return text[: max(0, limit - len(marker))] + marker


def _events_text_for_estimate(events: list[AgentEvent] | tuple[AgentEvent, ...]) -> str:
    return "\n".join(_event_line(event) for event in events)


def _event_line(event: AgentEvent) -> str:
    payload = event.model_dump(mode="json")
    compact: dict[str, object] = {
        "sequence": event.sequence,
        "type": str(event.type),
        "run_id": event.run_id,
    }
    for key in (
        "user_input_chars",
        "status",
        "stop_reason",
        "abort_kind",
        "error_message",
        "delta",
        "tool_call_name",
        "tool_call_id",
        "state",
        "artifacts",
        "question",
        "answer_text",
        "selected_option",
        "decision",
        "summary",
        "accepted_plan_summary",
        "accepted_plan_artifact_id",
    ):
        if key in payload:
            compact[key] = payload[key]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("user_input", "kind", "runtime_instruction"):
            if key in metadata:
                compact[f"metadata.{key}"] = metadata[key]
    return repr(compact)


def _event_context_for_compaction(events: list[AgentEvent]) -> EventContext:
    latest = events[-1]
    return EventContext(
        run_id=latest.run_id, turn_id=latest.turn_id, reply_id=latest.reply_id
    )


def _summary_artifact_id(compaction_id: str) -> str:
    return compaction_id.replace(":", "_") + ":summary"
