"""Attachment startup overlaps independent reads and settles them on failure."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlObservationKind,
    SessionLiveControlOwner,
)
from pulsara_agent.desktop_app.protocol_client import (
    BrowserRuntimeConnection,
    ProtocolBridgeError,
    TerminalProtocolClient,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer


@pytest.mark.parametrize("outcome", ["success", "snapshot_failure", "live_failure", "cancel"])
def test_browser_startup_overlaps_baselines_and_joins_before_cleanup(monkeypatch, outcome):
    async def exercise():
        live_started = asyncio.Event()
        snapshot_started = asyncio.Event()
        live_settled = asyncio.Event()
        blocked = asyncio.Event()
        error = ProtocolBridgeError("TEST_STARTUP_FAILURE", "startup failed")

        class Attachment:
            def __init__(self, observer):
                self.observer = observer
                self.closed = False

            async def require(self, field, request, expected):
                if self.observer:
                    assert field == expected == "live_control_snapshot"
                    live_started.set()
                    try:
                        if outcome == "live_failure":
                            raise error
                        if outcome in {"snapshot_failure", "cancel"}:
                            await blocked.wait()
                        return wire.ServerFrame(live_control_snapshot=wire.LiveControlSnapshotResponse())
                    finally:
                        live_settled.set()
                assert field == expected == "snapshot"
                snapshot_started.set()
                # The live baseline must make progress while the canonical
                # read is still running; a sequential implementation times out.
                await live_started.wait()
                if outcome == "snapshot_failure":
                    raise error
                if outcome == "cancel":
                    await blocked.wait()
                return wire.ServerFrame(snapshot=wire.SnapshotResponse())

            async def aclose(self):
                assert live_settled.is_set()
                self.closed = True

        attachments = []

        async def connect(**kwargs):
            attachment = Attachment(observer=bool(attachments))
            attachments.append(attachment)
            return attachment, wire.HelloAccepted()

        monkeypatch.setattr(TerminalProtocolClient, "connect", staticmethod(connect))
        opening = asyncio.create_task(BrowserRuntimeConnection.open(
            server=object(), session_id="session:test", host_session_id="host:test", generation=1,
        ))
        await asyncio.wait_for(snapshot_started.wait(), 1)
        await asyncio.wait_for(live_started.wait(), 1)
        if outcome == "cancel":
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
        elif outcome == "success":
            connection = await asyncio.wait_for(opening, 1)
            assert connection.initial_snapshot.HasField("snapshot")
            assert connection.initial_live_control.HasField("live_control_snapshot")
            assert not any(attachment.closed for attachment in attachments)
            await connection.aclose()
        else:
            with pytest.raises(ProtocolBridgeError) as caught:
                await asyncio.wait_for(opening, 1)
            assert caught.value is error
        assert live_settled.is_set()
        assert all(attachment.closed for attachment in attachments)

    asyncio.run(exercise())


def test_live_control_changes_during_canonical_read_remain_in_the_same_observer_suffix(
    monkeypatch, tmp_path,
):
    async def exercise():
        owner = SessionLiveControlOwner(session_id="session:test")
        host = SimpleNamespace(
            live_control=owner, current_todo_snapshots=lambda: (),
            current_compaction_projection=lambda: (False, None, None, None),
            root_compaction_in_progress=lambda: False,
            host_session_id="host:test", active_root_turn_id=lambda: None,
            control_admission_deadline_ms=lambda: 0,
        )
        state = SimpleNamespace(host_session=host, live_control_subscriber_id="")
        server = TerminalKernelProtocolServer(
            socket_path=tmp_path / "unused.sock", session_provider=lambda _: host,
        )
        baseline_ready = asyncio.Event()
        opened = None

        class Attachment:
            def __init__(self, observer):
                self.observer = observer

            async def require(self, field, request, expected):
                nonlocal opened
                if self.observer:
                    frame = server._live_control_snapshot(state, request)
                    baseline_ready.set()
                    return frame
                await baseline_ready.wait()
                opened = owner.install_interaction(CurrentInteractionView(
                    "interaction:test", "TOOL_CONFIRMATION", "Allow?", ("allow", "deny"), "",
                ))
                return wire.ServerFrame(snapshot=wire.SnapshotResponse(
                    snapshot=wire.CanonicalSessionSnapshot(session_id="session:test", event_sequence_cut=10),
                ))

            async def aclose(self):
                if self.observer:
                    owner.detach(state.live_control_subscriber_id)

        attachments = []

        async def connect(**kwargs):
            attachment = Attachment(observer=bool(attachments))
            attachments.append(attachment)
            return attachment, wire.HelloAccepted()

        monkeypatch.setattr(TerminalProtocolClient, "connect", staticmethod(connect))
        connection = await asyncio.wait_for(BrowserRuntimeConnection.open(
            server=server, session_id="session:test", host_session_id="host:test", generation=1,
        ), 1)
        try:
            baseline = connection.initial_live_control.live_control_snapshot.snapshot
            assert baseline.live_revision == 0
            assert not baseline.HasField("current_interaction")
            suffix = owner.observe(
                state.live_control_subscriber_id, owner_epoch=baseline.owner_epoch,
                after_revision=baseline.live_revision, maximum_events=1,
            )
            assert suffix.kind is LiveControlObservationKind.EVENTS
            assert suffix.events == (opened,)
            assert suffix.through_revision == 1
            assert connection.initial_snapshot.snapshot.snapshot.event_sequence_cut == 10
        finally:
            await connection.aclose()

    asyncio.run(exercise())
