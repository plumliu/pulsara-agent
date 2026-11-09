"""Session start boundaries and delegated-session hook composition.

The runner retains activation ownership; these existing objects and operations
only consume its exact hook scopes, cancellation intent and boundary tokens.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dataclass_field, replace

from pulsara_agent.conversation_kernel.cancellation import ActiveTurnCancellationIntent
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    CompactionAttemptToken,
    PreparedCompactSessionStart,
    PreparedCompactSessionStartFacts,
    SessionStartCompactBoundaryPort,
    SessionStartCompactPort,
)
from pulsara_agent.hooks.context import HookContextOwner
from pulsara_agent.hooks.contracts import (
    ContinuationDecision,
    ContinuationOutcome,
    GateDecision,
    GateOutcome,
    HookDispatchEnvelope,
    HookDispatchScopeRef,
    SessionStartInput,
    SessionStartRef,
    StopInput,
    SubagentStartInput,
    SubagentStopInput,
    external_permission_mode,
)
from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
from pulsara_agent.hooks.matcher import event_matcher_subject


@dataclass(frozen=True, slots=True)
class _SessionStartAttemptToken:
    session_id: str
    source: str
    boundary_token: object | None = dataclass_field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class _PendingSessionStartBoundary:
    source: str
    boundary_token: object = dataclass_field(repr=False, compare=False)
    compact_attempt_token: object | None = dataclass_field(
        default=None, repr=False, compare=False
    )
    adopted_snapshot_id: str | None = None
    adopted_binding_revision_id: str | None = None


class _SessionStartColdBoundaryOwner(SessionStartCompactBoundaryPort):
    """One process-local pending start boundary for the next session cold open."""

    def __init__(self, initial_source: str) -> None:
        if initial_source not in {"startup", "resume"}:
            raise ValueError("initial SessionStart source is invalid")
        self._pending: _PendingSessionStartBoundary | None = (
            _PendingSessionStartBoundary(initial_source, object())
        )
        self._lock = asyncio.Lock()

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    async def arm_compact_boundary(
        self,
        *,
        attempt_token: CompactionAttemptToken,
        adopted_snapshot_id: str,
        adopted_binding_revision_id: str,
    ) -> None:
        if not adopted_snapshot_id or not adopted_binding_revision_id:
            raise ValueError("compact SessionStart boundary identity is incomplete")
        async with self._lock:
            self._pending = _PendingSessionStartBoundary(
                "compact",
                (
                    attempt_token,
                    adopted_snapshot_id,
                    adopted_binding_revision_id,
                ),
                attempt_token,
                adopted_snapshot_id,
                adopted_binding_revision_id,
            )

    async def consume_any(self) -> _PendingSessionStartBoundary | None:
        async with self._lock:
            pending = self._pending
            self._pending = None
            return pending

    async def consume_compact(
        self, facts: PreparedCompactSessionStartFacts
    ) -> _PendingSessionStartBoundary | None:
        async with self._lock:
            pending = self._pending
            if pending is None:
                return None
            if (
                pending.source != "compact"
                or pending.compact_attempt_token is not facts.attempt_token
                or pending.adopted_snapshot_id != facts.adopted_snapshot_id
                or pending.adopted_binding_revision_id
                != facts.adopted_binding_revision_id
            ):
                raise RuntimeError("compact SessionStart boundary token drifted")
            self._pending = None
            return pending


class SessionStartBlocked(RuntimeError):
    pass


class CompactionContinuationBlocked(RuntimeError):
    pass


async def _dispatch_session_start_hooks(
    dispatcher: KernelHookDispatcher,
    envelope: HookDispatchEnvelope[SessionStartInput],
    intent: ActiveTurnCancellationIntent,
) -> GateOutcome:
    """Add the delegated-session event at the same existing start boundary."""
    public = envelope.public_input
    outcome = await dispatcher.dispatch(
        envelope,
        matcher_subject=event_matcher_subject(public.event_type, source=public.source),
    )
    if intent.cause is not None:
        raise asyncio.CancelledError
    if public.session_kind != "SUBAGENT" or outcome.decision is GateDecision.BLOCK:
        return outcome
    child_input = SubagentStartInput(
        session_id=public.session_id,
        session_kind=public.session_kind,
        input_origin=public.input_origin,
        cwd=public.cwd,
        model=public.model,
        source=public.source,
        permission_mode=public.permission_mode,
    )
    child = await dispatcher.dispatch(
        replace(envelope, public_input=child_input),
        matcher_subject=event_matcher_subject(
            child_input.event_type, source=child_input.source
        ),
    )
    return replace(
        outcome,
        context_entries=(*outcome.context_entries, *child.context_entries),
        diagnostics=(*outcome.diagnostics, *child.diagnostics),
    )


async def _dispatch_session_stop_hooks(
    dispatcher: KernelHookDispatcher,
    envelope: HookDispatchEnvelope[StopInput],
    intent: ActiveTurnCancellationIntent,
) -> ContinuationOutcome:
    """Compose final-attempt controls without ending the reusable child session."""
    public = envelope.public_input
    outcome = await dispatcher.dispatch(
        envelope,
        matcher_subject=event_matcher_subject(public.event_type),
        continuation_already_used=public.stop_hook_active,
    )
    if intent.cause is not None:
        raise asyncio.CancelledError
    if public.session_kind != "SUBAGENT":
        return outcome
    child_input = SubagentStopInput(
        session_id=public.session_id,
        session_kind=public.session_kind,
        input_origin=public.input_origin,
        cwd=public.cwd,
        model=public.model,
        turn_id=public.turn_id,
        stop_hook_active=public.stop_hook_active,
        last_assistant_message=public.last_assistant_message,
        permission_mode=public.permission_mode,
    )
    child = await dispatcher.dispatch(
        replace(envelope, public_input=child_input),
        matcher_subject=event_matcher_subject(child_input.event_type),
        continuation_already_used=public.stop_hook_active,
    )
    if intent.cause is not None:
        raise asyncio.CancelledError
    outcomes = (outcome, child)
    winner = next((item for item in outcomes if item.explicit_terminalize), None)
    if winner is None:
        winner = next(
            (item for item in outcomes if item.decision is ContinuationDecision.CONTINUE_ONCE),
            outcome,
        )
    return replace(winner, diagnostics=(*outcome.diagnostics, *child.diagnostics))


@dataclass(frozen=True, slots=True)
class _RootSessionStartCompactPort(SessionStartCompactPort):
    dispatcher: KernelHookDispatcher | None = dataclass_field(repr=False, compare=False)
    context_owner: HookContextOwner | None = dataclass_field(repr=False, compare=False)
    scope: HookDispatchScopeRef | None = dataclass_field(repr=False, compare=False)
    boundary_owner: _SessionStartColdBoundaryOwner = dataclass_field(
        repr=False, compare=False
    )
    intent: ActiveTurnCancellationIntent = dataclass_field(repr=False, compare=False)
    session_id: str
    cwd: str

    async def __call__(
        self, facts: PreparedCompactSessionStartFacts
    ) -> PreparedCompactSessionStart:
        if (
            facts.scope.session_id != self.session_id
            or facts.turn_id != self.intent.turn_id
        ):
            raise RuntimeError("compact SessionStart port received foreign facts")
        boundary = await self.boundary_owner.consume_compact(facts)
        if boundary is None:
            return PreparedCompactSessionStart(True)
        if self.dispatcher is None or self.context_owner is None or self.scope is None:
            return PreparedCompactSessionStart(True)
        public_input = SessionStartInput(
            session_kind=facts.scope.scope_kind.value,
            input_origin=None,
            session_id=self.session_id,
            cwd=self.cwd,
            model=facts.model_id,
            source="compact",
            permission_mode=external_permission_mode(
                facts.permission_snapshot.effective_mode.value,
                active_plan_workflow=(
                    facts.permission_snapshot.plan_workflow_id is not None
                ),
            ),
        )
        causal_ref = SessionStartRef(
            _SessionStartAttemptToken(
                self.session_id,
                "compact",
                boundary.boundary_token,
            ),
            "compact",
        )
        outcome = await _dispatch_session_start_hooks(
            self.dispatcher,
            HookDispatchEnvelope(
                self.dispatcher.capture_view(),
                self.scope,
                public_input,
                causal_ref,
                facts.deadline_monotonic,
            ),
            self.intent,
        )
        if self.intent.cause is not None:
            raise asyncio.CancelledError
        if outcome.decision is GateDecision.BLOCK:
            return PreparedCompactSessionStart(
                False,
                outcome.reason or "compact SessionStart Hook blocked",
            )
        reservation = self.context_owner.prepare_sync(
            scope=self.scope,
            causal_ref=causal_ref,
            entries=outcome.context_entries,
        )
        if reservation is not None:
            reservation.commit()
        return PreparedCompactSessionStart(True, reservation=reservation)
