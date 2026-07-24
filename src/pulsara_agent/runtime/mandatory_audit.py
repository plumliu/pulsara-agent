"""Session-owned bounded writer for mandatory runtime audit facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypeAlias
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
    ContextCompactionRequestedEvent,
    McpInputRequiredResumeFailedEvent,
    MidTurnContextCompactionSkippedEvent,
    ToolResultEvidenceProjectionFailedEvent,
)
from pulsara_agent.event_log.serialization import freeze_event_write_candidate
from pulsara_agent.primitives.context import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.runtime_event_vocabulary import (
    RuntimeEventOperationDeadlineBudget,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime._retry import bounded_none_retry_delay_seconds

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


MandatoryRuntimeAuditKind: TypeAlias = Literal[
    "mcp_resume_failure",
    "compaction_request",
    "mid_turn_compaction_skip",
    "tool_result_projection_failure",
]


class MandatoryRuntimeAuditReceipt(FrozenRuntimeStateBase):
    owner_id: str
    audit_kind: MandatoryRuntimeAuditKind
    candidate_event_id: str
    candidate_payload_fingerprint: str
    attempt_generation: int
    status: Literal["full", "reconciliation_required"]
    committed_event_reference: ContextEventReferenceFact | None
    publication_summary: Literal[
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ] | None
    publication_errors: tuple[object, ...]


@dataclass(slots=True)
class _MandatoryAuditAttempt:
    owner_id: str
    audit_kind: MandatoryRuntimeAuditKind
    candidate: AgentEvent
    candidate_payload_fingerprint: str
    deadline_budget: RuntimeEventOperationDeadlineBudget
    task: asyncio.Task[MandatoryRuntimeAuditReceipt]


def _audit_kind(event: AgentEvent) -> MandatoryRuntimeAuditKind:
    if isinstance(event, McpInputRequiredResumeFailedEvent):
        return "mcp_resume_failure"
    if isinstance(event, ContextCompactionRequestedEvent):
        return "compaction_request"
    if isinstance(event, MidTurnContextCompactionSkippedEvent):
        return "mid_turn_compaction_skip"
    if isinstance(event, ToolResultEvidenceProjectionFailedEvent):
        return "tool_result_projection_failure"
    raise TypeError("event is not a mandatory runtime audit fact")


class RuntimeSessionMandatoryAuditOwner:
    """Own exact stable candidates through FULL or reconciliation."""

    def __init__(self, runtime_session: "RuntimeSession") -> None:
        self._runtime_session = runtime_session
        self._attempts: dict[
            tuple[str, MandatoryRuntimeAuditKind, str],
            _MandatoryAuditAttempt,
        ] = {}
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        return sum(not attempt.task.done() for attempt in self._attempts.values())

    async def commit(
        self,
        event: AgentEvent,
        *,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
        state: object | None = None,
    ) -> MandatoryRuntimeAuditReceipt:
        if self._closed:
            raise RuntimeError("mandatory runtime audit owner is closed")
        kind = _audit_kind(event)
        frozen = freeze_event_write_candidate(
            event.model_copy(update={"sequence": None})
        )
        key = (self._runtime_session.runtime_session_id, kind, event.id)
        async with self._lock:
            existing = self._attempts.get(key)
            if existing is not None:
                if existing.candidate_payload_fingerprint != frozen.payload_fingerprint:
                    raise RuntimeError(
                        "mandatory audit stable event ID has a different payload"
                    )
                task = existing.task
            else:
                owner_id = f"mandatory_audit:{uuid4().hex}"
                task = asyncio.create_task(
                    self._drive(
                        owner_id=owner_id,
                        kind=kind,
                        candidate=event.model_copy(deep=True),
                        payload_fingerprint=frozen.payload_fingerprint,
                        deadline_budget=deadline_budget,
                        state=state,
                    ),
                    name=f"pulsara-mandatory-audit:{kind}:{event.id}",
                )
                self._attempts[key] = _MandatoryAuditAttempt(
                    owner_id=owner_id,
                    audit_kind=kind,
                    candidate=event.model_copy(deep=True),
                    candidate_payload_fingerprint=frozen.payload_fingerprint,
                    deadline_budget=deadline_budget,
                    task=task,
                )
                task.add_done_callback(
                    lambda completed, attempt_key=key: self._retire_completed_attempt(
                        attempt_key,
                        completed,
                    )
                )
        return await asyncio.shield(task)

    def _retire_completed_attempt(
        self,
        key: tuple[str, MandatoryRuntimeAuditKind, str],
        task: asyncio.Task[MandatoryRuntimeAuditReceipt],
    ) -> None:
        current = self._attempts.get(key)
        if current is not None and current.task is task:
            self._attempts.pop(key, None)

    async def _drive(
        self,
        *,
        owner_id: str,
        kind: MandatoryRuntimeAuditKind,
        candidate: AgentEvent,
        payload_fingerprint: str,
        deadline_budget: RuntimeEventOperationDeadlineBudget,
        state: object | None,
    ) -> MandatoryRuntimeAuditReceipt:
        attempt_generation = 0
        while monotonic() < deadline_budget.ordinary_deadline_monotonic:
            try:
                result = await self._runtime_session.write_event_with_deadline(
                    candidate,
                    deadline_monotonic=(
                        deadline_budget.ordinary_deadline_monotonic
                    ),
                    state=state,  # type: ignore[arg-type]
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                outcome = self._runtime_session.resolved_event_write_outcome(exc)
                if outcome.status == "none":
                    attempt_generation += 1
                    delay = bounded_none_retry_delay_seconds(
                        attempt_generation,
                        deadline_monotonic=(
                            deadline_budget.ordinary_deadline_monotonic
                        ),
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                self._runtime_session.latch_mandatory_audit_reconciliation_required()
                return MandatoryRuntimeAuditReceipt(
                    owner_id=owner_id,
                    audit_kind=kind,
                    candidate_event_id=candidate.id,
                    candidate_payload_fingerprint=payload_fingerprint,
                    attempt_generation=attempt_generation,
                    status="reconciliation_required",
                    committed_event_reference=None,
                    publication_summary=None,
                    publication_errors=(),
                )
            stored = next(
                event
                for event in result.committed_events
                if event.id == candidate.id
            )
            summary: Literal[
                "completed",
                "enqueued",
                "unavailable",
                "failed_after_commit",
            ] = (
                "failed_after_commit"
                if result.publication_errors
                else result.publication_status
            )
            return MandatoryRuntimeAuditReceipt(
                owner_id=owner_id,
                audit_kind=kind,
                candidate_event_id=candidate.id,
                candidate_payload_fingerprint=payload_fingerprint,
                attempt_generation=attempt_generation,
                status="full",
                committed_event_reference=event_reference_from_stored(
                    stored,
                    runtime_session_id=self._runtime_session.runtime_session_id,
                ),
                publication_summary=summary,
                publication_errors=tuple(result.publication_errors),
            )
        self._runtime_session.latch_mandatory_audit_reconciliation_required()
        return MandatoryRuntimeAuditReceipt(
            owner_id=owner_id,
            audit_kind=kind,
            candidate_event_id=candidate.id,
            candidate_payload_fingerprint=payload_fingerprint,
            attempt_generation=attempt_generation,
            status="reconciliation_required",
            committed_event_reference=None,
            publication_summary=None,
            publication_errors=(),
        )

    async def drain(self, *, deadline_monotonic: float) -> None:
        self._closed = True
        pending = tuple(
            attempt.task
            for attempt in self._attempts.values()
            if not attempt.task.done()
        )
        if not pending:
            return
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("mandatory runtime audit drain deadline expired")
        await asyncio.wait_for(
            asyncio.gather(*(asyncio.shield(task) for task in pending)),
            timeout=remaining,
        )

    def close_if_idle(self) -> None:
        self._closed = True
        if self.pending_count:
            raise RuntimeError("mandatory runtime audit owner still has pending writes")


__all__ = [
    "MandatoryRuntimeAuditKind",
    "MandatoryRuntimeAuditReceipt",
    "RuntimeSessionMandatoryAuditOwner",
]
