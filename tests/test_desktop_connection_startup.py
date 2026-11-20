"""Attachment startup overlaps independent reads and settles them on failure."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlObservationKind,
    SessionLiveControlOwner,
)
from pulsara_agent.identity import random_id
from pulsara_agent.desktop_app.protocol_client import (
    BrowserRuntimeConnection,
    ProtocolBridgeError,
    TerminalProtocolClient,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer


def test_command_attachment_can_read_without_subscribing_to_live_observation():
    async def exercise(socket_path):
        active_controller = None

        async def attach(attachment_id):
            nonlocal active_controller
            assert active_controller is None
            active_controller = attachment_id
            return True

        async def detach(attachment_id):
            nonlocal active_controller
            if active_controller == attachment_id:
                active_controller = None

        host = SimpleNamespace(
            session_id=random_id("session"), host_session_id=random_id("host"),
            repository=SimpleNamespace(connection_provider=None), root_session_id=None,
            has_controller_attachment=lambda value: active_controller == value,
            attach_controller=attach, controller_detached=detach,
            live_bus=LiveAgentEventBus(),
        )
        server = TerminalKernelProtocolServer(socket_path=socket_path, session_provider=lambda _: host)
        await server.start()
        try:
            binding = dict(server=server, session_id=host.session_id, host_session_id=host.host_session_id)
            command, hello = await TerminalProtocolClient.connect(
                **binding, role=wire.ATTACHMENT_ROLE_CONTROLLER, client_instance_id=random_id("transport"),
                without_live_observation=True,
            )
            assert hello.granted_role == wire.ATTACHMENT_ROLE_CONTROLLER
            assert active_controller == hello.attachment_id
            assert not hello.HasField("live_snapshot")
            heartbeat = await command.require("heartbeat", wire.HeartbeatRequest(), "heartbeat")
            assert heartbeat.heartbeat.active
            for operation, request in (
                ("observe", wire.ObserveRequest()),
                ("live_control_snapshot", wire.LiveControlSnapshotRequest()),
            ):
                frame = await command.request(operation, request)
                assert frame.error.stable_code == "INVALID_REQUEST"
            observer, live_hello = await TerminalProtocolClient.connect(
                **binding, role=wire.ATTACHMENT_ROLE_OBSERVER, client_instance_id=random_id("transport"),
            )
            assert live_hello.HasField("live_snapshot")
            assert live_hello.live_snapshot.owner_epoch == live_hello.live_owner_epoch
            assert server.has_session_attachments(host)
            await observer.aclose()
            await command.aclose()
            assert active_controller is None
        finally:
            await server.close()

    with TemporaryDirectory(prefix="pulsara-hello-", dir="/tmp") as directory:
        asyncio.run(exercise(Path(directory) / "kernel.sock"))


@pytest.mark.parametrize("initial_reading_page", [True, False])
def test_browser_requests_one_canonical_snapshot(monkeypatch, initial_reading_page):
    async def exercise():
        requests = []
        tail = wire.CanonicalSessionSnapshot(
            entry_sequence_cut=82, event_sequence_cut=100,
            control=wire.CanonicalControl(active_turn_total_count=1),
            older_history_cursor=wire.HistoryCursor(
                cut_sequence=82, event_sequence_cut=100, entry_sequence=59,
            ),
        )

        class Attachment:
            async def require(self, field, request, expected):
                assert field == expected
                if field == "live_control_snapshot":
                    return wire.ServerFrame(live_control_snapshot=wire.LiveControlSnapshotResponse())
                assert field == "snapshot"
                requests.append((request.maximum_entries, request.preferred_history_entries))
                return wire.ServerFrame(snapshot=wire.SnapshotResponse(
                    snapshot=tail,
                ))

            async def aclose(self):
                pass

        async def connect(**kwargs):
            return Attachment(), wire.HelloAccepted()

        monkeypatch.setattr(TerminalProtocolClient, "connect", staticmethod(connect))
        connection = await BrowserRuntimeConnection.open(
            server=object(), session_id="session:test", host_session_id="host:test",
            generation=1, initial_reading_page=initial_reading_page,
        )
        try:
            assert requests == [(256, 24 if initial_reading_page else 0)]
            assert connection.initial_snapshot.snapshot.snapshot == tail
        finally:
            await connection.aclose()

    asyncio.run(exercise())


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
            assert kwargs.get("without_live_observation", False) == (not attachment.observer)
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
            live_control=owner, current_work_snapshot=lambda: SimpleNamespace(todos=(), media_ranges=()),
            current_compaction_projection=lambda: (False, None, None, None),
            root_compaction_in_progress=lambda: False,
            host_session_id="host:test", active_root_turn_id=lambda: None,
            active_manual_compaction_command_id=lambda: None,
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
                    try:
                        frame = server._live_control_snapshot(state, request)
                    finally:
                        # Let startup surface the original baseline failure,
                        # rather than leaving the canonical side blocked here.
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
