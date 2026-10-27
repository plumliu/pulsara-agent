"""Identity collisions must not disturb an existing live owner."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from threading import Lock
from time import monotonic
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from pulsara_agent.identity import derive_id, random_id


def test_terminal_manager_retries_unpublished_decision_without_settling_sibling(
    tmp_path, monkeypatch
):
    from pulsara_agent.terminal_process import manager as module
    from pulsara_agent.terminal_process.models import TerminalRequest, TerminalStatus

    manager = module.TerminalManager(tmp_path)
    owner = random_id("host")
    occupied, fresh = random_id("terminal-decision"), random_id("terminal-decision")
    timer = Mock()
    sibling = module._TerminalForegroundDecisionAttempt(
        module.TerminalForegroundDecisionAttemptHandle(occupied, owner),
        module.TerminalForegroundDecisionState.PREPARING,
        timer=timer,
    )
    manager.process_registry._decision_attempts[occupied] = sibling
    manager.environment_owner = NS(
        build=lambda **kw: NS(
            shell=NS(command_argv=lambda command: ("/bin/sh", "-c", command)),
            values={},
            diagnostic=None,
        )
    )
    draws = iter((occupied, fresh))
    monkeypatch.setattr(
        module,
        "random_id",
        lambda kind: next(draws) if kind == "terminal-decision" else random_id(kind),
    )
    try:
        result = manager.execute(
            TerminalRequest(command="printf collision-survivor", yield_time_ms=1000),
            owner_host_session_id=owner,
            decision_deadline_monotonic=monotonic() + 5,
        )
        assert result.status is TerminalStatus.SUCCESS
        assert result.output == "collision-survivor"
        assert manager.process_registry._decision_attempts == {occupied: sibling}
        assert sibling.state is module.TerminalForegroundDecisionState.PREPARING
        timer.cancel.assert_not_called()
    finally:
        manager.process_registry.shutdown()


@pytest.mark.parametrize("capacity_exhausted", [False, True])
def test_terminal_rejected_explicit_decision_never_settles_existing_owner(
    tmp_path, monkeypatch, capacity_exhausted
):
    from pulsara_agent.terminal_process import manager as module
    from pulsara_agent.terminal_process.models import TerminalRequest, TerminalStatus

    manager = module.TerminalManager(tmp_path)
    registry = manager.process_registry
    owner, occupied = random_id("host"), random_id("attempt")
    timer = Mock()
    sibling = module._TerminalForegroundDecisionAttempt(
        module.TerminalForegroundDecisionAttemptHandle(occupied, owner),
        module.TerminalForegroundDecisionState.PREPARING,
        timer=timer,
    )
    registry._decision_attempts[occupied] = sibling
    manager.environment_owner = NS(
        build=lambda **kw: NS(
            shell=NS(command_argv=lambda command: ("/bin/sh", "-c", command)),
            values={},
            diagnostic=None,
        )
    )
    spawn = Mock(side_effect=AssertionError("rejected request must not spawn"))
    monkeypatch.setattr(registry, "_spawn", spawn)
    if capacity_exhausted:
        registry._launching_by_owner[owner] = registry.max_live_processes
    try:
        kwargs = dict(
            owner_host_session_id=owner,
            decision_attempt_id=occupied,
            decision_deadline_monotonic=monotonic() + 5,
        )
        if capacity_exhausted:
            assert (
                manager.execute(TerminalRequest(command="never-run"), **kwargs).status
                is TerminalStatus.BLOCKED
            )
        else:
            with pytest.raises(
                module.ProcessDecisionIdentityConflict, match="IDENTITY_CONFLICT"
            ):
                manager.execute(TerminalRequest(command="never-run"), **kwargs)
        assert registry._decision_attempts == {occupied: sibling}
        assert sibling.state is module.TerminalForegroundDecisionState.PREPARING
        timer.cancel.assert_not_called()
        spawn.assert_not_called()
    finally:
        registry._launching_by_owner.clear()
        registry.shutdown()


def test_gateway_retains_revoked_attachment_until_old_stream_cleanup(
    tmp_path, monkeypatch
):
    from pulsara_agent.conversation_kernel.interaction import (
        KernelInteractionCoordinator,
    )
    from pulsara_agent.terminal_protocol import v3_gateway as gateway
    from pulsara_agent.terminal_protocol.generated_v3 import (
        terminal_kernel_v3_pb2 as wire,
    )

    async def scenario():
        owner = object.__new__(KernelInteractionCoordinator)
        owner._controller_lock, owner._controller_id, owner._closed = (
            Lock(),
            None,
            False,
        )

        async def promote():
            pass

        owner._promote_next = promote

        async def detach(identity):
            owner.detach_controller(identity)

        host = NS(
            session_id=random_id("session"),
            host_session_id=random_id("host"),
            repository=NS(connection_provider=None),
            attach_controller=owner.attach_controller,
            has_controller_attachment=owner.is_current_controller,
            controller_detached=detach,
            live_bus=NS(
                subscribe_with_snapshot=lambda: (
                    random_id("observer"),
                    NS(generation=1, through_revision=0),
                ),
                detach=lambda _: None,
            ),
        )
        server = gateway.TerminalKernelProtocolServer(
            socket_path=tmp_path / "unused.sock", session_provider=lambda _: host
        )
        first, replacement = random_id("attachment"), random_id("attachment")
        draws = iter((first, first, replacement))
        monkeypatch.setattr(
            gateway,
            "random_id",
            lambda kind: next(draws) if kind == "attachment" else random_id(kind),
        )
        monkeypatch.setattr(
            gateway, "_live_snapshot_to_wire", lambda _: wire.LiveSnapshotProjection()
        )

        async def read(queue):
            item = await queue.get()
            if item is None:
                raise asyncio.IncompleteReadError(b"", 4)
            return item

        async def write(writer, frame):
            await writer.responses.put(frame)

        monkeypatch.setattr(server, "_read_frame", read)
        monkeypatch.setattr(server, "_write_frame", write)

        async def closed():
            pass

        writers = [
            NS(responses=asyncio.Queue(), close=lambda: None, wait_closed=closed)
            for _ in range(2)
        ]
        readers = [asyncio.Queue(), asyncio.Queue()]
        tasks = [
            asyncio.create_task(server._accept(r, w)) for r, w in zip(readers, writers)
        ]

        def hello():
            return wire.ClientFrame(
                hello=wire.HelloRequest(
                    protocol=gateway.protocol_identity(),
                    launch_id=server._launch_id,
                    launch_capability=server._launch_capability,
                    session_id=host.session_id,
                    host_session_id=host.host_session_id,
                    requested_role=wire.ATTACHMENT_ROLE_CONTROLLER,
                )
            )

        try:
            await readers[0].put(hello())
            a = await asyncio.wait_for(writers[0].responses.get(), 2)
            assert a.hello.attachment_id == first
            await server.controller_attachment_closed(
                session_id=host.session_id,
                host_session_id=host.host_session_id,
                attachment_id=first,
            )
            old_state = server._connections[tasks[0]]
            assert not server._has_controller_capability(old_state)
            await readers[1].put(hello())
            b = await asyncio.wait_for(writers[1].responses.get(), 2)
            assert b.hello.attachment_id == replacement
            assert not server._has_controller_capability(old_state)
            assert server._has_controller_capability(server._connections[tasks[1]])
            # Run the real old stream finally, not merely the detach helper.
            await readers[0].put(None)
            await asyncio.wait_for(tasks[0], 2)
            assert tasks[0] not in server._connections
            assert host.has_controller_attachment(replacement)
        finally:
            for reader in readers:
                await reader.put(None)
            await asyncio.gather(*tasks)
            await server.close()
        assert not server._connections
        assert owner.current_controller_id() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("collision_kind", ["entry", "event"])
@pytest.mark.parametrize("same_recipient", [True, False])
def test_mailbox_collision_rejects_only_new_candidate_and_preserves_fifo(
    monkeypatch, collision_kind, same_recipient
):
    from pulsara_agent.conversation_kernel import subagent as module
    from pulsara_agent.conversation_kernel.subagents import contracts
    from pulsara_agent.conversation_kernel.repository_errors import (
        ConversationKernelConflict,
    )

    async def scenario():
        manager = object.__new__(module.KernelSubagentManager)
        session, turn = random_id("session"), random_id("turn")
        first, second = random_id("task"), random_id("task")
        manager._guard, manager._closed = NS(session_id=session), False
        manager._state_changed, manager._state_revision = asyncio.Condition(), 0
        manager._completing = set()
        manager._tasks = {
            t: NS(status="ACTIVE", cancellation_intent=NS(turn_id=turn))
            for t in (first, second)
        }
        manager._mailboxes, manager._mailbox_ordinals = {first: [], second: []}, {}
        fixed = random_id(collision_kind)

        def force(kind, role, *parts):
            return fixed if kind == collision_kind else derive_id(kind, role, *parts)

        monkeypatch.setattr(module, "derive_id", force)
        monkeypatch.setattr(contracts, "derive_id", force)
        context = NS(
            session_id=session,
            turn_id=turn,
            attempt_id=random_id("attempt"),
            tool_call_id="native-1",
        )
        request = {"task_id": first, "message": "keep this message"}
        assert (await manager._send_message(request, context)).state == "SUCCESS"
        original = manager._mailboxes[first][0]
        # Same accepted attempt remains idempotent, without consuming an ordinal.
        assert (await manager._send_message(request, context)).state == "SUCCESS"
        ordinal_before = dict(manager._mailbox_ordinals)
        revision_before = manager._state_revision
        target = first if same_recipient else second
        other = NS(
            session_id=session,
            turn_id=turn,
            attempt_id=random_id("attempt"),
            tool_call_id="native-2",
        )
        with pytest.raises(ConversationKernelConflict, match="IDENTITY_CONFLICT"):
            await manager._send_message(
                {"task_id": target, "message": "reject collision"}, other
            )
        assert manager._mailbox_ordinals == ordinal_before
        assert manager._state_revision == revision_before
        assert manager._mailboxes == {first: [original], second: []}
        assert manager._tasks[first].status == "ACTIVE"
        batch = contracts.build_inter_agent_mailbox_batch(
            items=tuple(manager._mailboxes[first]),
            actor_id="test",
            occurred_at=datetime.now(timezone.utc),
        )
        assert batch.items == (original,)
        # A later valid send still receives the next ordinal.
        monkeypatch.setattr(module, "derive_id", derive_id)
        monkeypatch.setattr(contracts, "derive_id", derive_id)
        assert (
            await manager._send_message(
                {"task_id": first, "message": "next valid"}, other
            )
        ).state == "SUCCESS"
        assert [item.ordinal for item in manager._mailboxes[first]] == [0, 1]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "endpoint,field,kind",
    [
        ("list", "cursor", "schedule"),
        ("list", "session_id", "session"),
        ("list_sessions", "cursor", "session"),
        ("get", "task_id", "schedule"),
    ],
)
@pytest.mark.parametrize(
    "bad",
    ["scheduled:" + "0" * 32, "entry:" + "a" * 26, "schedule:" + "a" * 25 + "b", ""],
)
def test_scheduled_http_rejects_invalid_ids_before_service(endpoint, field, kind, bad):
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController
    from pulsara_agent.scheduling.contracts import ScheduledTaskError

    async def scenario():
        service = NS(
            **{
                endpoint: Mock(
                    side_effect=AssertionError("invalid identity reached service")
                )
            }
        )
        controller = ScheduledTaskController(service)
        request = NS(query={field: bad}, match_info={field: bad})
        with pytest.raises(ScheduledTaskError) as error:
            await getattr(controller, endpoint)(request)
        assert error.value.code == "INVALID_ID"
        getattr(service, endpoint).assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "endpoint,field,kind",
    [
        ("list", "cursor", "schedule"),
        ("list", "session_id", "session"),
        ("list_sessions", "cursor", "session"),
        ("get", "task_id", "schedule"),
    ],
)
def test_scheduled_http_forwards_exact_new_id(endpoint, field, kind):
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController
    from unittest.mock import AsyncMock

    async def scenario():
        value = random_id(kind)
        method = AsyncMock(return_value={"observed": value})
        response = await getattr(
            ScheduledTaskController(NS(**{endpoint: method})), endpoint
        )(NS(query={field: value}, match_info={field: value}))
        assert response.status == 200
        assert json.loads(response.body) == {"observed": value}
        if endpoint == "get":
            method.assert_awaited_once_with(value)
        else:
            assert method.await_args.kwargs[field] == value

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "query", [{"action": "list"}, {"action": "get"}, {"unknown": "x"}]
)
def test_scheduled_http_list_rejects_unknown_query_fields(query):
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController
    from pulsara_agent.scheduling.contracts import ScheduledTaskError

    async def scenario():
        service = NS(list=Mock())
        with pytest.raises(ScheduledTaskError) as error:
            await ScheduledTaskController(service).list(NS(query=query))
        assert error.value.code == "INVALID_REQUEST"
        service.list.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["update", "pause", "resume", "delete", "switch"])
@pytest.mark.parametrize("bad", ["scheduled:" + "0" * 32, "entry:" + "a" * 26])
def test_scheduled_http_mutation_rejects_invalid_id_before_service(action, bad):
    from unittest.mock import AsyncMock
    from pulsara_agent.desktop_app.scheduled_controller import ScheduledTaskController
    from pulsara_agent.scheduling.contracts import ScheduledTaskError

    async def scenario():
        service = NS(mutate=Mock(), switch=Mock())
        body = {"expected_revision": 1}
        if action == "update":
            body["values"] = {}
        if action == "switch":
            body["replacement"] = {"kind": "create", "values": {}}
        request = NS(
            match_info={"task_id": bad, "action": action},
            json=AsyncMock(return_value=body),
        )
        controller = ScheduledTaskController(service)
        with pytest.raises(ScheduledTaskError) as error:
            await (
                controller.update(request)
                if action == "update"
                else controller.action(request)
            )
        assert error.value.code == "INVALID_ID"
        service.mutate.assert_not_called()
        service.switch.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["client_command_id", "session_id", "task_id"])
@pytest.mark.parametrize("bad", ["scheduled:" + "0" * 32, "entry:" + "a" * 26, ""])
def test_scheduled_run_now_rejects_invalid_id_before_confirmation(field, bad):
    from pulsara_agent.scheduling.service import ScheduledTaskService
    from pulsara_agent.scheduling.contracts import ScheduledTaskError

    async def scenario():
        service = object.__new__(ScheduledTaskService)
        service._read = Mock(
            side_effect=AssertionError("invalid identity reached repository")
        )
        request = {
            "client_command_id": random_id("command"),
            "session_id": random_id("session"),
            "task_id": random_id("schedule"),
            "expected_revision": 1,
            "request_at_utc": "2026-10-14T00:00:00Z",
        }
        request[field] = bad
        with pytest.raises(ScheduledTaskError) as error:
            await service.run_now(request)
        assert error.value.code == "INVALID_ID"
        service._read.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "identity",
    [
        "terminal-monitor:" + "0" * 32,
        "process:" + "a" * 26,
        "monitor:" + "a" * 25 + "b",
    ],
)
def test_monitor_cancel_invalid_id_rejected_by_parser_and_owner(identity):
    from pulsara_agent.ports.terminal import parse_terminal_monitor_input
    from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator

    with pytest.raises(ValueError, match="INVALID_ID"):
        parse_terminal_monitor_input({"action": "cancel", "target": {"kind": "MONITOR", "monitor_id": identity}})
    # Validation must precede even looking up the live collection.
    owner = object.__new__(TerminalMonitorCoordinator)
    with pytest.raises(ValueError, match="INVALID_ID"):
        owner.cancel(identity)
