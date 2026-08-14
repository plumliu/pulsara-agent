"""Same-Host pending interaction owner for the Stage 2 kernel.

Pending requests, epochs, futures, and secret-capable carriers never enter
PostgreSQL.  Only a controller's accepted decision is committed, together
with the exact tool attempt (allow) or no-attempt result (deny).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from uuid import uuid4

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


@dataclass(frozen=True, slots=True)
class ToolInteractionResolution:
    decision: str
    reference: str
    public_message: str
    attempt_id: str | None = None
    result_entry_id: str | None = None
    permission_snapshot_fingerprint: str | None = None


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
    admission_hooks: InteractionAdmissionHooks | None = None
    visible: bool = False
    discarded: bool = False
    resolving: bool = False
    settlement_changed: asyncio.Event | None = None


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

    def attach_controller(self, attachment_id: str) -> bool:
        if not attachment_id:
            return False
        with self._controller_lock:
            if self._closed or self._controller_id not in {None, attachment_id}:
                return False
            self._controller_id = attachment_id
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
        if not self.has_controller():
            if admission_hooks is not None:
                admission_hooks.discard()
            return ToolInteractionResolution(
                "DENY",
                "interaction:no-controller",
                "tool execution requires confirmation but no controller is attached",
            )
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed or not self.has_controller():
                if admission_hooks is not None:
                    admission_hooks.discard()
                return ToolInteractionResolution(
                    "DENY",
                    "interaction:no-controller",
                    "tool execution requires confirmation but no controller is attached",
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
                admission_hooks=admission_hooks,
                settlement_changed=asyncio.Event(),
            )
            self._dormant.append(pending)
        await self._promote_next()
        try:
            async with asyncio.timeout(INTERACTION_TIMEOUT_SECONDS):
                return await asyncio.shield(pending.future)
        except TimeoutError:
            await self._abort_candidate(
                interaction_id=interaction_id,
                reference="interaction:expired",
                public_message="tool confirmation expired",
            )
            return await pending.future
        except asyncio.CancelledError:
            await self._abort_candidate(
                interaction_id=interaction_id,
                reference="interaction:turn-cancelled",
                public_message="tool confirmation was cancelled",
            )
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
            if self._closed or expected_writer_generation != self._guard.writer_generation:
                raise ConversationKernelConflict("interaction writer generation is stale")
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
                raise ConversationKernelConflict("interaction resolution is already active")
            assert pending.settlement_changed is not None
            # Each physical resolution attempt owns a fresh unsettled edge.
            # A prior failed attempt set this event to wake detach/close; if it
            # remains set, _abort_all would spin instead of joining this retry.
            pending.settlement_changed.clear()
            pending.resolving = True
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
        try:
            accepted = await self._io.run(
                self._repository.accept_tool_interaction_decision,
                self._guard,
                **kwargs,
            )
        except BaseException:
            async with self._lock:
                if self._pending is pending:
                    pending.resolving = False
                    assert pending.settlement_changed is not None
                    pending.settlement_changed.set()
            raise
        async with self._lock:
            if self._pending is not pending:
                raise ConversationKernelConflict(
                    "interaction live owner changed during durable resolution"
                )
            close_event = self._live_control.close_interaction(
                expected_interaction_id=interaction_id
            )
            self._offer_interaction_event(
                close_event,
                turn_id=pending.turn_id,
                current=None,
                reason="RESOLVED",
            )
            self._pending = None
            resolution = ToolInteractionResolution(
                decision,
                f"interaction-decision:{accepted.decision_id}",
                "tool execution was allowed"
                if decision == "ALLOW"
                else "tool execution was denied",
                accepted.attempt_id,
                accepted.result_entry_id,
                accepted.permission_snapshot_fingerprint,
            )
            if not pending.future.done():
                pending.future.set_result(resolution)
            assert pending.settlement_changed is not None
            pending.settlement_changed.set()
        if decision == "DENY":
            self._discard_hooks(pending)
        await self._promote_next()
        return accepted

    async def controller_detached(self, attachment_id: str) -> None:
        if not self.detach_controller(attachment_id):
            return
        await self._abort_all(
            reference="interaction:controller-detached",
            public_message="tool confirmation ended because the controller detached",
        )

    async def cancel_tool_confirmations(
        self,
        *,
        owner_keys: frozenset[str],
        reference: str,
        public_message: str,
    ) -> None:
        """Cancel exact process-local candidates for one capability plane.

        A candidate whose durable resolution is already running remains owned by
        that resolution.  Config replacement must not erase a canonical winner.
        """

        if not owner_keys:
            return
        discarded: list[_PendingToolInteraction] = []
        async with self._lock:
            pending = self._pending
            if (
                pending is not None
                and not pending.resolving
                and pending.admission_hooks is not None
                and pending.admission_hooks.owner_key in owner_keys
            ):
                if pending.visible:
                    try:
                        close_event = self._live_control.close_interaction(
                            expected_interaction_id=pending.interaction_id
                        )
                        self._offer_interaction_event(
                            close_event,
                            turn_id=pending.turn_id,
                            current=None,
                            reason=reference,
                        )
                    except RuntimeError:
                        pass
                self._pending = None
                discarded.append(pending)
            for candidate in tuple(self._dormant):
                hooks = candidate.admission_hooks
                if hooks is None or hooks.owner_key not in owner_keys:
                    continue
                self._dormant.remove(candidate)
                discarded.append(candidate)
            for candidate in discarded:
                if not candidate.future.done():
                    candidate.future.set_result(
                        ToolInteractionResolution(
                            "DENY", reference, public_message
                        )
                    )
        for candidate in discarded:
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

    async def _abort_current_if_no_controller(self) -> None:
        if self.has_controller():
            return
        await self._abort_all(
            reference="interaction:controller-detached",
            public_message="tool confirmation ended because the controller detached",
        )

    async def _abort_candidate(
        self,
        *,
        interaction_id: str,
        reference: str,
        public_message: str,
    ) -> None:
        discarded: _PendingToolInteraction | None = None
        async with self._lock:
            pending = self._pending
            if pending is not None and pending.interaction_id == interaction_id:
                if pending.resolving:
                    return
                if pending.visible:
                    try:
                        close_event = self._live_control.close_interaction(
                            expected_interaction_id=pending.interaction_id
                        )
                        self._offer_interaction_event(
                            close_event,
                            turn_id=pending.turn_id,
                            current=None,
                            reason=reference,
                        )
                    except RuntimeError:
                        pass
                self._pending = None
                discarded = pending
            else:
                for candidate in tuple(self._dormant):
                    if candidate.interaction_id == interaction_id:
                        self._dormant.remove(candidate)
                        discarded = candidate
                        break
            if discarded is None:
                return
            pending = discarded
            if not pending.future.done():
                pending.future.set_result(
                    ToolInteractionResolution(
                        "DENY", reference, public_message
                    )
                )
        self._discard_hooks(discarded)
        await self._promote_next()

    async def _promote_next(self) -> None:
        """Publish exactly one FIFO head after its local admission succeeds."""

        while True:
            async with self._lock:
                if (
                    self._closed
                    or not self.has_controller()
                    or self._pending is not None
                    or not self._dormant
                ):
                    return
                candidate = self._dormant.popleft()
                self._pending = candidate
            try:
                if candidate.admission_hooks is not None:
                    candidate.admission_hooks.before_publish()
            except BaseException as exc:
                async with self._lock:
                    if self._pending is candidate:
                        self._pending = None
                    if not candidate.future.done():
                        candidate.future.set_result(
                            ToolInteractionResolution(
                                "DENY",
                                "interaction:admission-rejected",
                                "tool confirmation admission was rejected: "
                                f"{type(exc).__name__}",
                            )
                        )
                self._discard_hooks(candidate)
                continue
            discard = False
            async with self._lock:
                if (
                    self._pending is not candidate
                    or self._closed
                    or not self.has_controller()
                ):
                    if self._pending is candidate:
                        self._pending = None
                    if not candidate.future.done():
                        candidate.future.set_result(
                            ToolInteractionResolution(
                                "DENY",
                                "interaction:owner-unavailable",
                                "tool confirmation owner is unavailable",
                            )
                        )
                    discard = True
                else:
                    view = CurrentInteractionView(
                        interaction_id=candidate.interaction_id,
                        interaction_kind="TOOL_CONFIRMATION",
                        public_prompt=f"Allow {candidate.tool_name}?",
                        public_options=("ALLOW", "DENY"),
                        expires_at_utc=(
                            datetime.now(timezone.utc)
                            + timedelta(seconds=INTERACTION_TIMEOUT_SECONDS)
                        ).isoformat(),
                    )
                    event = self._live_control.install_interaction(view)
                    candidate.revision = event.revision
                    candidate.visible = True
                    self._offer_interaction_event(
                        event,
                        turn_id=candidate.turn_id,
                        current=view,
                        reason=None,
                    )
                    return
            if discard:
                self._discard_hooks(candidate)

    async def _abort_all(
        self,
        *,
        reference: str,
        public_message: str,
    ) -> None:
        # A controller detach or Host close may race a decision transaction
        # after it has started.  That transaction owns a possible canonical
        # winner and therefore cannot be erased or have its MCP admission
        # permit discarded.  Wait for its process-local settlement, then make
        # another pass; a failed transaction resets ``resolving`` and can be
        # cancelled normally, while a FULL winner removes itself.
        while True:
            settlement_changed: asyncio.Event | None = None
            async with self._lock:
                candidates = list(self._dormant)
                self._dormant.clear()
                pending = self._pending
                if pending is not None:
                    if pending.resolving:
                        settlement_changed = pending.settlement_changed
                    else:
                        candidates.insert(0, pending)
                        if pending.visible:
                            try:
                                close_event = self._live_control.close_interaction(
                                    expected_interaction_id=pending.interaction_id
                                )
                                self._offer_interaction_event(
                                    close_event,
                                    turn_id=pending.turn_id,
                                    current=None,
                                    reason=reference,
                                )
                            except RuntimeError:
                                pass
                        self._pending = None
                for candidate in candidates:
                    if not candidate.future.done():
                        candidate.future.set_result(
                            ToolInteractionResolution(
                                "DENY", reference, public_message
                            )
                        )
            for candidate in candidates:
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
    "ToolInteractionResolution",
]
