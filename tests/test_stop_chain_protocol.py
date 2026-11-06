"""Stop preparation uses existing snapshot/read owners without moving authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.cancellation import ActiveTurnCancellationIntent
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlObservationKind,
    SessionLiveControlOwner,
)
from pulsara_agent.conversation_kernel.user_control import (
    CONTROL_ADMISSION_WINDOW_MS,
    CONTROL_RESULT_RETENTION_MS,
    ControlQueryStatus,
    UserControlOperation,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.test_pr03_user_controls import _Clock, _host
from tests.test_stage2_protocol_v3 import _server, _state


def _id(kind: str, label: str) -> str:
    return derive_id(kind, "stop-chain-protocol-test", label)


def _install_observation_owners(state, clock, *, ring_events=8):
    host = state.host_session
    host.live_bus = LiveAgentEventBus()
    state.live_observer_id, state.live_epoch, state.live_revision = host.live_bus.subscribe()
    host.live_control = SessionLiveControlOwner(
        session_id=host.session_id, maximum_events=ring_events,
    )
    host.current_work_snapshot = lambda: SimpleNamespace(todos=(), media_ranges=())
    host.current_compaction_projection = lambda: (False, None, None, None)
    host.root_compaction_in_progress = lambda: False
    host.active_root_turn_id = lambda: None
    host.active_manual_compaction_command_id = lambda: None
    if not hasattr(host, "control_admission_deadline_ms"):
        host.control_admission_deadline_ms = lambda: int(clock() * 1000) + CONTROL_ADMISSION_WINDOW_MS
    host.provider_retry_observation = lambda: None
    host.take_presentation_notices = lambda _: ()
    # Canonical reads are independent here; the subject under test is the real
    # gateway's live-control cursor and controller-authority handling.
    state.protocol_reader = SimpleNamespace(observe_committed=lambda **_: SimpleNamespace(
        projections=(), through_event_sequence=0, gap_reason=None,
    ))


async def _dispatch(server, state, operation, request):
    request.request_id = _id("request", operation)
    request.attachment_id = state.attachment_id
    request.attachment_generation = state.attachment_generation
    return await server._dispatch(state, wire.ClientFrame(**{operation: request}))


async def _snapshot(server, state):
    response = await _dispatch(server, state, "live_control_snapshot", wire.LiveControlSnapshotRequest())
    assert response.WhichOneof("response") == "live_control_snapshot"
    return response.live_control_snapshot.snapshot


async def _observe(server, state, *, after, epoch=1):
    return (await _dispatch(server, state, "observe", wire.ObserveRequest(
        wait_ms=0, maximum_events=1, maximum_bytes=4096,
        live_owner_epoch=state.live_epoch, after_live_revision=state.live_revision,
        live_control_owner_epoch=epoch, after_live_control_revision=after,
    ))).observation


def _interaction(label):
    return CurrentInteractionView(
        _id("interaction", label), "TOOL_CONFIRMATION", f"Allow {label}?", ("allow", "deny"), "",
    )


def test_metadata_snapshot_resubscription_replays_both_sides_of_old_cursor():
    async def scenario():
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        clock = _Clock()
        _install_observation_owners(state, clock)
        server = _server()
        baseline = await _snapshot(server, state)
        assert baseline.live_revision == 0
        old_subscriber = state.live_control_subscriber_id
        owner = state.host_session.live_control
        first, second = _interaction("first"), _interaction("second")
        owner.install_interaction(first)
        clock.seconds += CONTROL_ADMISSION_WINDOW_MS / 1000 + 1

        prepared = await _snapshot(server, state)
        assert prepared.live_revision == 1
        assert prepared.current_interaction.interaction_id == first.interaction_id
        assert prepared.control_admission_deadline_ms > baseline.control_admission_deadline_ms
        assert state.live_control_subscriber_id != old_subscriber
        assert owner.observe(old_subscriber, owner_epoch=1, after_revision=0, maximum_events=1).kind is LiveControlObservationKind.DETACHED
        owner.close_interaction(expected_interaction_id=first.interaction_id)
        owner.install_interaction(second)

        # Metadata-only preparation did not install revision 1 on the client.
        # The new subscriber must honor the old client cursor and replay it.
        page = await _observe(server, state, after=baseline.live_revision)
        assert not page.HasField("gap")
        assert page.through_live_control_revision == 1
        assert [(event.kind, event.interaction.interaction_id) for event in page.live_control] == [
            (wire.LIVE_INTERACTION_OPENED, first.interaction_id),
        ]
        repeated = await _observe(server, state, after=baseline.live_revision)
        assert repeated.live_control == page.live_control
        closed = await _observe(server, state, after=page.through_live_control_revision)
        assert closed.through_live_control_revision == 2
        assert [(event.kind, event.closed_interaction_id) for event in closed.live_control] == [
            (wire.LIVE_INTERACTION_CLOSED, first.interaction_id),
        ]
        opened = await _observe(server, state, after=closed.through_live_control_revision)
        assert opened.through_live_control_revision == 3
        assert [(event.kind, event.interaction.interaction_id) for event in opened.live_control] == [
            (wire.LIVE_INTERACTION_OPENED, second.interaction_id),
        ]

    asyncio.run(scenario())


def test_metadata_snapshot_old_cursor_overflow_requires_explicit_gap_baseline():
    async def scenario():
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        _install_observation_owners(state, _Clock(), ring_events=2)
        server = _server()
        initial = await _snapshot(server, state)
        owner = state.host_session.live_control
        first, second = _interaction("overflow-first"), _interaction("overflow-second")
        owner.install_interaction(first)
        await _snapshot(server, state)
        owner.close_interaction(expected_interaction_id=first.interaction_id)
        owner.install_interaction(second)

        for _ in range(2):
            gap = await _observe(server, state, after=initial.live_revision)
            assert gap.gap.kind == wire.LIVE_CONTROL_GAP
            assert gap.gap.reason == "LIVE_CONTROL_RING_GAP"
            assert gap.gap.latest_sequence == 3
            assert list(gap.live_control) == []

        # Only explicit installation of the recovery snapshot advances the
        # client's baseline; a failed old read must not mutate server delivery.
        recovered = await _snapshot(server, state)
        assert recovered.live_revision == 3
        assert recovered.current_interaction.interaction_id == second.interaction_id
        owner.close_interaction(expected_interaction_id=second.interaction_id)
        suffix = await _observe(server, state, after=recovered.live_revision)
        assert not suffix.HasField("gap")
        assert suffix.through_live_control_revision == 4
        assert [(event.kind, event.closed_interaction_id) for event in suffix.live_control] == [
            (wire.LIVE_INTERACTION_CLOSED, second.interaction_id),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(("kind", "field", "target_kind", "records"), [
    (wire.STOP_ACTIVE_TURN, "target_turn_id", "turn", "stop_requests"),
    (wire.STOP_CONTEXT_COMPACTION, "target_compaction_command_id", "command", "compaction_stop_requests"),
    (wire.TERMINATE_BACKGROUND_PROCESS, "target_process_id", "process", "terminate_requests"),
    (wire.STOP_CODE_MODE_CELL, "target_cell_id", "cell", "cell_stop_requests"),
])
def test_successful_metadata_snapshot_cannot_authorize_revoked_controller(kind, field, target_kind, records):
    async def scenario():
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        _install_observation_owners(state, _Clock())
        server = _server()
        prepared = await _snapshot(server, state)
        host = state.host_session
        host.controller_id = _id("attachment", "replacement-controller")
        response = await _dispatch(server, state, "command", wire.CommandRequest(
            command_id=_id("command", f"revoked-{kind}"), command_kind=kind,
            expected_session_id=prepared.session_id,
            expected_host_session_id=prepared.host_session_id,
            control_admission_deadline_ms=prepared.control_admission_deadline_ms,
            **{field: _id(target_kind, "frozen-target")},
        ))
        assert response.error.stable_code == "CONTROLLER_REQUIRED"
        assert getattr(host, records) == []
        assert host.controller_id != state.attachment_id

    asyncio.run(scenario())


def test_new_deadline_and_retained_exact_root_stop_have_distinct_lifetimes():
    async def scenario():
        clock = _Clock()
        host = _host(clock)
        state = _state(role=wire.ATTACHMENT_ROLE_CONTROLLER)
        host.workspace_gate = state.host_session.workspace_gate
        host.controller_id = state.attachment_id
        host.has_controller_attachment = lambda attachment_id: attachment_id == host.controller_id
        state.host_session = host
        _install_observation_owners(state, clock)
        server = _server()
        target = _id("turn", "long-connection-root")
        started, cancelling, physical_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellations = []
        settled = []

        async def root():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append(target)
                cancelling.set()
                await physical_release.wait()
                host.repository.turns[target] = {"status": "INTERRUPTED", "terminal_reason": "USER_STOPPED"}
                raise

        async def settle(task):
            assert task is root_task and task.done()
            settled.append(target)

        root_task = asyncio.create_task(root())
        host._active_task, host._active_turn_id = root_task, target
        host._active_cancellation_intent = ActiveTurnCancellationIntent(target, ModelInputScopeKind.ROOT)
        host._settle_active_root_task = settle
        await started.wait()
        initial = await _snapshot(server, state)
        stale_deadline = initial.control_admission_deadline_ms
        clock.seconds += CONTROL_ADMISSION_WINDOW_MS / 1000 + 1

        async def stop(request):
            return await host.request_stop_turn(
                command_id=request.command_id, expected_session_id=request.session_id,
                expected_host_session_id=request.host_session_id,
                target_turn_id=request.target.target_id, admission_deadline_ms=request.admission_deadline_ms,
            )

        stale = UserControlRequest(
            UserControlOperation.STOP_ACTIVE_TURN, _id("command", "stale-unsent"),
            host.session_id, host.host_session_id,
            UserControlTarget(UserControlTargetKind.ROOT_TURN, target), stale_deadline,
        )
        try:
            rejected = await stop(stale)
            assert rejected.public_code == "CONTROL_REQUEST_EXPIRED"
            assert rejected.status == "REJECTED"
            assert root_task.cancelling() == 0 and cancellations == []
            assert host._user_control_attempts == {}
            prepared = await _snapshot(server, state)
            assert prepared.host_session_id == host.host_session_id
            request = replace(stale, command_id=_id("command", "fresh-stop"),
                              admission_deadline_ms=prepared.control_admission_deadline_ms)
            assert request.admission_deadline_ms > stale.admission_deadline_ms
            unknown = await host.query_control_command(request)
            assert unknown.status is ControlQueryStatus.RESULT_UNAVAILABLE and unknown.outcome is None
            first = await stop(request)
            assert first.status == "PENDING"
            await cancelling.wait()
            attempt = host._user_control_attempts[request.command_id]
            clock.seconds += CONTROL_ADMISSION_WINDOW_MS / 1000 + 1
            assert await stop(request) is first
            assert cancellations == [target] and not root_task.done()
            pending = await host.query_control_command(request)
            assert pending.status is ControlQueryStatus.FOUND and pending.outcome is first
            # The initial response may be lost. A new controller attachment to
            # the same Host queries the original frozen command after expiry;
            # it neither refreshes that command's deadline nor dispatches Stop.
            reconnected = replace(state, attachment_id=_id("attachment", "same-host-reconnect"))
            host.controller_id = reconnected.attachment_id
            query = wire.QueryCommandRequest(
                command_id=request.command_id,
                expected_control=wire.ExpectedUserControl(
                    operation=wire.USER_CONTROL_STOP_ACTIVE_TURN,
                    session_id=request.session_id, host_session_id=request.host_session_id,
                    admission_deadline_ms=request.admission_deadline_ms,
                    target=wire.UserControlTarget(kind=wire.USER_CONTROL_ROOT_TURN, target_id=target),
                ),
            )
            queried = (await _dispatch(server, reconnected, "query_command", query)).query_command
            assert queried.control_query_status == wire.CONTROL_QUERY_FOUND
            assert queried.outcome.status == wire.PENDING
            assert queried.outcome.command_id == request.command_id
            assert cancellations == [target]
            changed = await stop(replace(request, admission_deadline_ms=host.control_admission_deadline_ms()))
            assert changed.public_code == "CONTROL_COMMAND_CONFLICT"
            assert host._user_control_attempts[request.command_id] is attempt
            physical_release.set()
            await asyncio.wait_for(attempt.task, 1)
            finished = await host.query_control_command(request)
            assert finished.status is ControlQueryStatus.FOUND
            assert finished.outcome.public_code == "ROOT_STOPPED"
            assert finished.outcome.user_control.root.status == "INTERRUPTED"
            assert await stop(request) is finished.outcome
            assert cancellations == settled == [target]
            queried = (await _dispatch(server, reconnected, "query_command", query)).query_command
            assert queried.control_query_status == wire.CONTROL_QUERY_FOUND
            assert queried.outcome.public_code == "ROOT_STOPPED"
            assert attempt.retain_until_monotonic_ms == int(clock() * 1000) + CONTROL_RESULT_RETENTION_MS
            other_owner = await host.query_control_command(replace(request, host_session_id=_id("host", "new-owner")))
            assert other_owner.status is ControlQueryStatus.OWNER_UNAVAILABLE and other_owner.outcome is None
            clock.seconds = (attempt.retain_until_monotonic_ms - 1) / 1000
            assert (await host.query_control_command(request)).outcome is finished.outcome
            clock.seconds = attempt.retain_until_monotonic_ms / 1000
            retired = await host.query_control_command(request)
            assert retired.status is ControlQueryStatus.RESULT_UNAVAILABLE and retired.outcome is None
            queried = (await _dispatch(server, reconnected, "query_command", query)).query_command
            assert queried.control_query_status == wire.CONTROL_QUERY_RESULT_UNAVAILABLE
            assert not queried.found and not queried.HasField("outcome")
            assert host._user_control_attempts == {}
            assert (await stop(request)).public_code == "CONTROL_REQUEST_EXPIRED"
            assert cancellations == settled == [target]
        finally:
            physical_release.set()
            if not root_task.done():
                root_task.cancel()
            await asyncio.gather(root_task, *(attempt.task for attempt in host._user_control_attempts.values() if attempt.task), return_exceptions=True)

    asyncio.run(scenario())
