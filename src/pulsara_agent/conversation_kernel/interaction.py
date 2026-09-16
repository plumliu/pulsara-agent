"""Same-Host pending interaction owner for the Stage 2 kernel.

Pending requests, epochs, futures, and secret-capable carriers never enter
PostgreSQL.  Only a controller's accepted decision is committed, together
with the exact tool attempt (allow) or no-attempt result (deny).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from uuid import uuid4

from pulsara_agent.capability.management_form import (
    AcceptedCapabilityFormSubmission,
    CapabilityFormValues,
    PendingCapabilityForm,
)
from pulsara_agent.primitives.context import FrozenJsonObjectFact

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard, InlineContent
from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlEvent,
    LiveControlEventKind,
    SessionLiveControlOwner,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import (
    InteractionClosedPayload,
    InteractionOpenedPayload,
    InteractionReplacedPayload,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.interaction_arbiter import (
    InteractionAdmissionHooks,
    MAXIMUM_DORMANT_INTERACTION_CANDIDATES,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedInteractionDecision,
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot


INTERACTION_TIMEOUT_SECONDS = 10 * 60


class ToolInteractionDecisionNotAccepted(RuntimeError):
    """The original write exited and exact read-only confirmation found no decision."""


class ToolInteractionDecisionOutcomeUnknown(RuntimeError):
    """An admitted write could not be confirmed; this is not a stale rejection."""


@dataclass(frozen=True, slots=True)
class ToolInteractionResolution:
    decision: str
    reference: str
    public_message: str
    attempt_id: str | None = None
    result_entry_id: str | None = None
    permission_snapshot_fingerprint: str | None = None
    result_id: str | None = None
    result_entry_sequence: int | None = None
    result_observed_at: datetime | None = None
    result_public_body: str | None = None
    capability_submission: AcceptedCapabilityFormSubmission | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.capability_submission is not None and (
            self.decision != "SUBMIT"
            or self.attempt_id is not None
            or self.result_entry_id is not None
        ):
            raise ValueError("capability submission is pre-admission user input")
        if (self.result_entry_id is not None) != all(
            value is not None
            for value in (
                self.result_id,
                self.result_entry_sequence,
                self.result_observed_at,
                self.result_public_body,
            )
        ):
            raise ValueError("interaction ToolResult settlement facts are incomplete")


@dataclass(slots=True)
class _PendingToolInteraction:
    interaction_id: str
    revision: int
    turn_id: str
    assistant_entry_id: str
    tool_call_id: str
    tool_name: str
    attempt_id: str
    result_id: str
    result_entry_id: str
    permission_snapshot_fingerprint: str
    future: asyncio.Future[ToolInteractionResolution]
    deadline_monotonic: float
    expires_at_utc: str
    admission_hooks: InteractionAdmissionHooks | None = None
    visible: bool = False
    discarded: bool = False
    resolving: bool = False
    settlement_changed: asyncio.Event | None = None
    capability_form: PendingCapabilityForm | None = field(default=None, repr=False)
    capability_cancelled: bool = False
    admission_completed: bool = False
    promoting: bool = False
    invalidation: tuple[str, str] | None = None
    settlement_task: asyncio.Task[AcceptedInteractionDecision] | None = None


class KernelInteractionCoordinator:
    """One current live request and one current controller per Host session."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        guard: HostWriterGuard,
        live_control: SessionLiveControlOwner,
        live_bus: LiveAgentEventBus,
        io_owner: KernelSessionIO,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
    ) -> None:
        self._repository = repository
        self._guard = guard
        self._live_control = live_control
        self._live_bus = live_bus
        self._io = io_owner
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._lock = asyncio.Lock()
        self._controller_lock = Lock()
        self._controller_id: str | None = None
        self._pending: _PendingToolInteraction | None = None
        self._dormant: deque[_PendingToolInteraction] = deque()
        self._closed = False

    async def attach_controller(self, attachment_id: str) -> bool:
        if not attachment_id:
            return False
        with self._controller_lock:
            if self._closed or self._controller_id not in {None, attachment_id}:
                return False
            self._controller_id = attachment_id
        await self._promote_next()
        return True

    def detach_controller(self, attachment_id: str) -> bool:
        with self._controller_lock:
            if self._controller_id != attachment_id:
                return False
            self._controller_id = None
            return True

    def has_controller(self) -> bool:
        with self._controller_lock:
            return not self._closed and self._controller_id is not None

    def is_current_controller(self, attachment_id: str) -> bool:
        """Return the narrow same-Host content/control capability join."""

        with self._controller_lock:
            return (
                not self._closed
                and bool(attachment_id)
                and self._controller_id == attachment_id
            )

    def current_controller_id(self) -> str | None:
        """Return the exact current process-local controller attachment."""

        with self._controller_lock:
            if self._closed:
                return None
            return self._controller_id

    async def request_tool_confirmation(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        tool_name: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        admission_hooks: InteractionAdmissionHooks | None = None,
    ) -> ToolInteractionResolution:
        return await self._request(
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            permission_snapshot=permission_snapshot,
            admission_hooks=admission_hooks,
        )

    async def request_capability_form(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        form: PendingCapabilityForm,
    ) -> ToolInteractionResolution:
        return await self._request(
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            tool_name="manage_capability",
            permission_snapshot=permission_snapshot,
            capability_form=form,
        )

    async def _request(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        tool_name: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        admission_hooks: InteractionAdmissionHooks | None = None,
        capability_form: PendingCapabilityForm | None = None,
    ) -> ToolInteractionResolution:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed or (
                capability_form is not None and not self.has_controller()
            ):
                if admission_hooks is not None:
                    admission_hooks.discard()
                return ToolInteractionResolution(
                    "DENY",
                    "interaction:host-closing"
                    if self._closed
                    else "interaction:no-controller",
                    "interaction owner is unavailable",
                )
            if len(self._dormant) + int(self._pending is not None) >= (
                MAXIMUM_DORMANT_INTERACTION_CANDIDATES
            ):
                if admission_hooks is not None:
                    admission_hooks.discard()
                return ToolInteractionResolution(
                    "DENY",
                    "interaction:capacity",
                    "tool confirmation capacity is full",
                )
            interaction_id = f"interaction:{uuid4().hex}"
            pending = _PendingToolInteraction(
                interaction_id=interaction_id,
                revision=0,
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                attempt_id=f"tool-attempt:{uuid4().hex}",
                result_id=f"tool-result:{uuid4().hex}",
                result_entry_id=f"entry:{uuid4().hex}",
                permission_snapshot_fingerprint=(
                    permission_snapshot.snapshot_fingerprint
                ),
                future=loop.create_future(),
                deadline_monotonic=loop.time() + INTERACTION_TIMEOUT_SECONDS,
                expires_at_utc=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=INTERACTION_TIMEOUT_SECONDS)
                ).isoformat(),
                admission_hooks=admission_hooks,
                settlement_changed=asyncio.Event(),
                capability_form=capability_form,
            )
            self._dormant.append(pending)
        try:
            try:
                async with asyncio.timeout_at(pending.deadline_monotonic):
                    await self._promote_next()
                    return await asyncio.shield(pending.future)
            except TimeoutError:
                await self._abort_candidate(
                    interaction_id=interaction_id,
                    reference="interaction:expired",
                    public_message="tool confirmation expired",
                )
                return await asyncio.shield(pending.future)
        except asyncio.CancelledError:
            while True:
                try:
                    await self._abort_candidate(
                        interaction_id=interaction_id,
                        reference="interaction:turn-cancelled",
                        public_message="tool confirmation was cancelled",
                    )
                    break
                except asyncio.CancelledError:
                    continue
            if pending.capability_form is None:
                # This is the actual tool waiter, not the socket submitting its
                # decision. Join the original bounded write/confirm settlement,
                # then release admission: a cancelled waiter cannot take over an
                # ALLOW permit. Preserve the committed winner in the future/DB.
                while not pending.future.done():
                    try:
                        await asyncio.shield(pending.future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not pending.future.cancelled():
                    pending.future.exception()  # Consume an unknown outcome too.
                self._discard_hooks(pending)
            raise

    async def resolve_tool_interaction(
        self,
        *,
        expected_writer_generation: int,
        expected_owner_epoch: int,
        expected_live_revision: int,
        interaction_id: str,
        command_id: str,
        decision: str,
        actor_id: str,
    ) -> AcceptedInteractionDecision:
        if decision not in {"ALLOW", "DENY"}:
            raise ValueError("tool interaction resolution is not closed")
        async with self._lock:
            if (
                self._closed
                or expected_writer_generation != self._guard.writer_generation
            ):
                raise ConversationKernelConflict(
                    "interaction writer generation is stale"
                )
            pending = self._pending
            snapshot = self._live_control.current_snapshot()
            if (
                pending is None
                or pending.interaction_id != interaction_id
                or pending.revision != expected_live_revision
                or snapshot.owner_epoch != expected_owner_epoch
                or snapshot.revision != expected_live_revision
                or snapshot.current_interaction is None
                or snapshot.current_interaction.interaction_id != interaction_id
            ):
                raise ConversationKernelConflict("interaction live authority is stale")
            if pending.resolving:
                raise ConversationKernelConflict(
                    "interaction resolution is already active"
                )
            if pending.capability_form is not None:
                raise ConversationKernelConflict(
                    "capability form requires SUBMIT or CANCEL"
                )
            assert pending.settlement_changed is not None
            # Each physical resolution attempt owns a fresh unsettled edge.
            # A prior failed attempt set this event to wake detach/close; if it
            # remains set, _abort_all would spin instead of joining this retry.
            with self._controller_lock:
                if self._controller_id != actor_id or self._closed:
                    raise ConversationKernelConflict("interaction controller is stale")
                if (
                    pending.invalidation
                    or asyncio.get_running_loop().time() >= pending.deadline_monotonic
                ):
                    raise ConversationKernelConflict("interaction is no longer valid")
                pending.settlement_changed.clear()
                pending.resolving = True
            self._publish_resolving(pending)
            kwargs = {
                "command_id": command_id,
                "decision_id": "interaction-decision:"
                + sha256(
                    f"{self._guard.session_id}\0{command_id}".encode("utf-8")
                ).hexdigest(),
                "assistant_entry_id": pending.assistant_entry_id,
                "tool_call_id": pending.tool_call_id,
                "decision": decision,
                "attempt_id": pending.attempt_id if decision == "ALLOW" else None,
                "result_id": pending.result_id if decision == "DENY" else None,
                "result_entry_id": (
                    pending.result_entry_id if decision == "DENY" else None
                ),
                "denial_content": (
                    InlineContent.from_bytes(b"tool execution denied by user")
                    if decision == "DENY"
                    else None
                ),
                "redacted_subject": f"tool:{pending.tool_name}",
                "actor_id": actor_id,
                "occurred_at": datetime.now(timezone.utc),
                "permission_snapshot_fingerprint": (
                    pending.permission_snapshot_fingerprint
                ),
                "deadline_monotonic": self._deadlines.deadline(
                    KernelWatchdogOwner.FOREGROUND_CANONICAL
                ),
            }
            # The original candidate owns settlement, independently of a socket
            # waiter. Host close joins its existing settlement_changed edge.
            task = asyncio.create_task(
                self._settle_decision(pending, kwargs),
                name=f"interaction-settlement:{interaction_id}",
            )
            pending.settlement_task = task
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except BaseException:
                break
        accepted = task.result()
        if cancellation is not None:
            raise cancellation
        return accepted

    def _publish_resolving(self, pending: _PendingToolInteraction) -> None:
        snapshot = self._live_control.current_snapshot()
        assert snapshot.current_interaction is not None
        view = replace(
            snapshot.current_interaction, decision_in_progress=pending.resolving
        )
        event = self._live_control.install_interaction(
            view, replace_expected_interaction_id=pending.interaction_id
        )
        pending.revision = event.revision
        self._offer_interaction_event(
            event, turn_id=pending.turn_id, current=view, reason=None
        )

    async def _settle_decision(
        self, pending: _PendingToolInteraction, kwargs: dict[str, object]
    ) -> AcceptedInteractionDecision:
        try:
            accepted = await self._io.run(
                self._repository.accept_tool_interaction_decision,
                self._guard,
                **kwargs,
            )
        except BaseException as write_error:
            # KernelSessionIO has joined the physical write before raising.
            # One bounded read attempt uses the original frozen identity and
            # actor. Reconnects neither start another owner nor renew its budget.
            try:
                accepted = await self._io.run(
                    self._repository.confirm_tool_interaction_decision,
                    self._guard,
                    **(
                        kwargs
                        | {
                            "deadline_monotonic": self._deadlines.deadline(
                                KernelWatchdogOwner.FOREGROUND_CANONICAL
                            )
                        }
                    ),
                )
            except BaseException as confirmation_error:
                async with self._lock:
                    self._close_candidate_locked(pending, "interaction:outcome-unknown")
                    pending.resolving = False
                    if not pending.future.done():
                        pending.future.set_exception(
                            ConversationKernelConflict(
                                "interaction decision outcome is unknown; original operation must be verified"
                            )
                        )
                    pending.settlement_changed.set()
                self._discard_hooks(pending)
                await self._promote_next()
                raise ToolInteractionDecisionOutcomeUnknown(
                    "original interaction decision outcome could not be confirmed"
                ) from confirmation_error
            if accepted is None:
                async with self._lock:
                    pending.resolving = False
                    invalidation = self._invalidation(pending)
                    if invalidation is None:
                        self._publish_resolving(pending)
                    else:
                        self._close_candidate_locked(pending, invalidation[0])
                        if not pending.future.done():
                            pending.future.set_result(
                                ToolInteractionResolution("DENY", *invalidation)
                            )
                    pending.settlement_changed.set()
                if invalidation is not None:
                    self._discard_hooks(pending)
                    await self._promote_next()
                raise ToolInteractionDecisionNotAccepted(
                    "original interaction decision was confirmed not accepted"
                ) from write_error
        decision = accepted.decision
        async with self._lock:
            if self._pending is not pending:
                raise ConversationKernelConflict(
                    "interaction live owner changed during durable resolution"
                )
            self._close_candidate_locked(pending, "RESOLVED")
            pending.resolving = False
            resolution = ToolInteractionResolution(
                decision,
                f"interaction-decision:{accepted.decision_id}",
                "tool execution was allowed"
                if decision == "ALLOW"
                else "tool execution was denied",
                accepted.attempt_id,
                accepted.result_entry_id,
                accepted.permission_snapshot_fingerprint,
                accepted.result_id,
                accepted.result_entry_sequence,
                accepted.result_observed_at,
                "tool execution denied by user" if decision == "DENY" else None,
            )
            if not pending.future.done():
                pending.future.set_result(resolution)
            assert pending.settlement_changed is not None
            pending.settlement_changed.set()
        if decision == "DENY":
            self._discard_hooks(pending)
        await self._promote_next()
        return accepted

    def current_capability_form(
        self,
        *,
        attachment_id: str,
        interaction_id: str,
        expected_owner_epoch: int,
        expected_live_revision: int,
    ) -> FrozenJsonObjectFact:
        # No await: the owner and snapshot are observed in one event-loop step.
        pending = self._require_capability_form(
            attachment_id=attachment_id,
            interaction_id=interaction_id,
            expected_owner_epoch=expected_owner_epoch,
            expected_live_revision=expected_live_revision,
        )
        assert pending.capability_form is not None
        return pending.capability_form.public_projection

    def _require_capability_form(
        self,
        *,
        attachment_id,
        interaction_id,
        expected_owner_epoch,
        expected_live_revision,
    ) -> _PendingToolInteraction:
        if not self.is_current_controller(attachment_id):
            raise ConversationKernelConflict("capability form controller is stale")
        pending = self._pending
        snapshot = self._live_control.current_snapshot()
        if (
            pending is None
            or pending.capability_form is None
            or pending.interaction_id != interaction_id
            or pending.revision != expected_live_revision
            or snapshot.owner_epoch != expected_owner_epoch
            or snapshot.revision != expected_live_revision
            or snapshot.current_interaction is None
            or snapshot.current_interaction.interaction_id != interaction_id
        ):
            raise ConversationKernelConflict("capability form live authority is stale")
        return pending

    async def resolve_capability_form(
        self,
        *,
        attachment_id: str,
        interaction_id: str,
        expected_owner_epoch: int,
        expected_live_revision: int,
        decision: str,
        submission: dict[str, object] | None = None,
    ) -> None:
        """Validate and transfer input; never mutate or create a ToolAttempt here."""
        if decision not in {"SUBMIT", "CANCEL"} or (
            (decision == "SUBMIT") != isinstance(submission, dict)
        ):
            raise ValueError("capability form resolution is invalid")
        async with self._lock:
            pending = self._require_capability_form(
                attachment_id=attachment_id,
                interaction_id=interaction_id,
                expected_owner_epoch=expected_owner_epoch,
                expected_live_revision=expected_live_revision,
            )
            if pending.resolving:
                raise ConversationKernelConflict("capability form is already resolving")
            assert pending.settlement_changed is not None
            pending.settlement_changed.clear()
            pending.resolving = True
        try:
            values = None
            if decision == "SUBMIT":
                assert pending.capability_form is not None and submission is not None
                # Parsing/reinspection is outside the slot lock. No mutation lane
                # or provider deadline is held while waiting for user input.
                values = await pending.capability_form.prepare_submission(submission)
                if not isinstance(values, CapabilityFormValues):
                    raise TypeError("capability form parser returned invalid input")
            async with self._lock:
                if self._pending is not pending:
                    raise ConversationKernelConflict("capability form owner changed")
                cancelled = (
                    decision == "CANCEL"
                    or pending.capability_cancelled
                    or not self.is_current_controller(attachment_id)
                )
                event = self._live_control.close_interaction(
                    expected_interaction_id=interaction_id
                )
                self._offer_interaction_event(
                    event,
                    turn_id=pending.turn_id,
                    current=None,
                    reason="CANCELLED" if cancelled else "SUBMITTED",
                )
                self._pending = None
                if not pending.future.done():
                    pending.future.set_result(
                        ToolInteractionResolution(
                            "CANCEL" if cancelled else "SUBMIT",
                            "capability-form:cancelled"
                            if cancelled
                            else "capability-form:user-submitted",
                            "Capability configuration was cancelled." if cancelled else "The user submitted the capability configuration.",
                            capability_submission=(
                                AcceptedCapabilityFormSubmission(values)
                                if not cancelled and values is not None
                                else None
                            ),
                        )
                    )
        finally:
            async with self._lock:
                pending.resolving = False
                pending.settlement_changed.set()
            # A validation failure keeps the editor open unless its execution
            # owner was cancelled while the read-only reinspection was running.
            if pending.capability_cancelled:
                await self._abort_candidate(
                    interaction_id=interaction_id,
                    reference="interaction:turn-cancelled",
                    public_message="capability form was cancelled",
                )
            await self._promote_next()

    async def controller_detached(self, attachment_id: str) -> None:
        if not self.detach_controller(attachment_id):
            return
        # Revocation is already effective. A parsing form retains its original
        # parser owner and observes cancellation before transferring input.
        async with self._lock:
            forms = tuple(
                candidate
                for candidate in (
                    *((self._pending,) if self._pending is not None else ()),
                    *self._dormant,
                )
                if candidate.capability_form is not None
            )
            for candidate in forms:
                self._invalidate_locked(
                    candidate,
                    (
                        "interaction:controller-detached",
                        "capability form ended because the controller detached",
                    ),
                )
        for candidate in forms:
            if not candidate.resolving:
                self._discard_hooks(candidate)

    async def cancel_tool_confirmations(
        self,
        *,
        owner_keys: frozenset[str],
        reference: str,
        public_message: str,
    ) -> None:
        if not owner_keys:
            return
        async with self._lock:
            candidates = tuple(
                candidate
                for candidate in (
                    *((self._pending,) if self._pending is not None else ()),
                    *self._dormant,
                )
                if candidate.admission_hooks is not None
                and candidate.admission_hooks.owner_key in owner_keys
            )
            for candidate in candidates:
                self._invalidate_locked(candidate, (reference, public_message))
        for candidate in candidates:
            if not candidate.resolving:
                self._discard_hooks(candidate)
        await self._promote_next()

    async def aclose(self) -> None:
        with self._controller_lock:
            self._closed = True
            self._controller_id = None
        await self._abort_all(
            reference="interaction:host-closing",
            public_message="tool confirmation ended with the Host",
        )

    def _invalidation(
        self, candidate: _PendingToolInteraction
    ) -> tuple[str, str] | None:
        if candidate.invalidation is not None:
            return candidate.invalidation
        if self._closed:
            return "interaction:host-closing", "tool confirmation ended with the Host"
        if asyncio.get_running_loop().time() >= candidate.deadline_monotonic:
            return "interaction:expired", "tool confirmation expired"
        if candidate.capability_form is not None and not self.has_controller():
            return (
                "interaction:controller-detached",
                "capability form controller detached",
            )
        return None

    def _close_candidate_locked(
        self, candidate: _PendingToolInteraction, reason: str
    ) -> None:
        if self._pending is candidate:
            if candidate.visible:
                event = self._live_control.close_interaction(
                    expected_interaction_id=candidate.interaction_id
                )
                self._offer_interaction_event(
                    event, turn_id=candidate.turn_id, current=None, reason=reason
                )
            self._pending = None
        elif candidate in self._dormant:
            self._dormant.remove(candidate)

    def _invalidate_locked(
        self, candidate: _PendingToolInteraction, reason: tuple[str, str]
    ) -> None:
        if candidate.invalidation is None:
            candidate.invalidation = reason
        if candidate.resolving:
            if candidate.capability_form is not None:
                candidate.capability_cancelled = True
            return
        self._close_candidate_locked(candidate, candidate.invalidation[0])
        if not candidate.future.done():
            candidate.future.set_result(
                ToolInteractionResolution("DENY", *candidate.invalidation)
            )
        candidate.settlement_changed.set()

    async def _abort_candidate(
        self,
        *,
        interaction_id: str,
        reference: str,
        public_message: str,
    ) -> None:
        async with self._lock:
            candidate = next(
                (
                    candidate
                    for candidate in (
                        *((self._pending,) if self._pending is not None else ()),
                        *self._dormant,
                    )
                    if candidate.interaction_id == interaction_id
                ),
                None,
            )
            if candidate is None:
                return
            self._invalidate_locked(candidate, (reference, public_message))
        if not candidate.resolving:
            self._discard_hooks(candidate)
        await self._promote_next()

    async def _promote_next(self) -> None:
        """Keep one exact FIFO head, including its completed MCP admission."""
        while True:
            async with self._lock:
                if self._closed or not self.has_controller():
                    return
                candidate = self._pending
                if candidate is None:
                    if not self._dormant:
                        return
                    candidate = self._dormant.popleft()
                    self._pending = candidate
                if candidate.visible or candidate.promoting:
                    return
                invalidation = self._invalidation(candidate)
                if invalidation is not None:
                    self._invalidate_locked(candidate, invalidation)
                    self._discard_hooks(candidate)
                    continue
                candidate.promoting = True
            try:
                if not candidate.admission_completed:
                    if candidate.admission_hooks is not None:
                        candidate.admission_hooks.before_publish()
                    candidate.admission_completed = True
                async with self._lock:
                    if self._pending is not candidate:
                        self._discard_hooks(candidate)
                        continue
                    invalidation = self._invalidation(candidate)
                    if invalidation is not None:
                        self._invalidate_locked(candidate, invalidation)
                        self._discard_hooks(candidate)
                        continue
                    if not self.has_controller():
                        return
                    view = CurrentInteractionView(
                        interaction_id=candidate.interaction_id,
                        interaction_kind="CAPABILITY_FORM"
                        if candidate.capability_form
                        else "TOOL_CONFIRMATION",
                        public_prompt=candidate.capability_form.public_prompt
                        if candidate.capability_form
                        else f"Allow {candidate.tool_name}?",
                        public_options=("SUBMIT", "CANCEL")
                        if candidate.capability_form
                        else ("ALLOW", "DENY"),
                        expires_at_utc=candidate.expires_at_utc,
                        decision_in_progress=False,
                    )
                    event = self._live_control.install_interaction(view)
                    candidate.revision = event.revision
                    candidate.visible = True
                    self._offer_interaction_event(
                        event, turn_id=candidate.turn_id, current=view, reason=None
                    )
                    return
            except asyncio.CancelledError:
                # The request's deadline/cancellation owns cleanup; an attach
                # cancellation leaves its still-live head for the next attach.
                raise
            except Exception as exc:
                async with self._lock:
                    self._invalidate_locked(
                        candidate,
                        (
                            "interaction:admission-rejected",
                            f"tool confirmation admission was rejected: {type(exc).__name__}",
                        ),
                    )
                self._discard_hooks(candidate)
            finally:
                candidate.promoting = False

    async def _abort_all(self, *, reference: str, public_message: str) -> None:
        while True:
            async with self._lock:
                candidates = tuple(
                    (
                        *((self._pending,) if self._pending is not None else ()),
                        *self._dormant,
                    )
                )
                settlement_changed = None
                for candidate in candidates:
                    self._invalidate_locked(candidate, (reference, public_message))
                    if candidate.resolving:
                        settlement_changed = candidate.settlement_changed
            for candidate in candidates:
                if not candidate.resolving:
                    self._discard_hooks(candidate)
            if settlement_changed is None:
                return
            await asyncio.shield(settlement_changed.wait())

    @staticmethod
    def _discard_hooks(candidate: _PendingToolInteraction) -> None:
        if candidate.discarded:
            return
        candidate.discarded = True
        if candidate.admission_hooks is not None:
            candidate.admission_hooks.discard()

    def _offer_interaction_event(
        self,
        event: LiveControlEvent,
        *,
        turn_id: str,
        current: CurrentInteractionView | None,
        reason: str | None,
    ) -> None:
        if event.kind is LiveControlEventKind.INTERACTION_OPENED:
            assert current is not None
            event_type = LiveEventType.INTERACTION_OPENED
            payload = InteractionOpenedPayload(
                current.interaction_id,
                current.interaction_kind,
                current.public_prompt,
                current.public_options,
                current.expires_at_utc,
            )
            identity = current.interaction_id
        elif event.kind is LiveControlEventKind.INTERACTION_REPLACED:
            assert current is not None and event.closed_interaction_id is not None
            event_type = LiveEventType.INTERACTION_REPLACED
            payload = InteractionReplacedPayload(
                event.closed_interaction_id,
                current.interaction_id,
                current.interaction_kind,
                current.public_prompt,
                current.public_options,
                current.expires_at_utc,
            )
            identity = current.interaction_id
        else:
            assert event.closed_interaction_id is not None and reason is not None
            event_type = LiveEventType.INTERACTION_CLOSED
            payload = InteractionClosedPayload(event.closed_interaction_id, reason)
            identity = event.closed_interaction_id
        self._live_bus.offer_nowait(
            event_type=event_type,
            session_id=self._guard.session_id,
            turn_id=turn_id,
            draft_identity=identity,
            payload=payload,
            channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
            generation_id=f"interaction:{identity}",
            block_id=identity,
            block_ordinal=0,
            block_kind=LiveBlockKind.OPERATIONAL,
        )


__all__ = [
    "INTERACTION_TIMEOUT_SECONDS",
    "KernelInteractionCoordinator",
    "ToolInteractionDecisionNotAccepted",
    "ToolInteractionDecisionOutcomeUnknown",
    "ToolInteractionResolution",
]
