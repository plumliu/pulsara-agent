"""PR05 R12–R15: real PostgreSQL transactions and real Unix bridge takeover."""

from __future__ import annotations

from pulsara_agent.llm.input import FrozenPromptContent

import asyncio
from datetime import datetime, timezone
from threading import Event
from tempfile import TemporaryDirectory
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.interaction import (
    KernelInteractionCoordinator,
    ToolInteractionDecisionNotAccepted,
    ToolInteractionDecisionOutcomeUnknown,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    ConversationKernelConflict,
)
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    build_run_permission_snapshot,
    RunPermissionAdmissionSource,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from tests.test_stage2_conversation_kernel_postgres import _repository, _start_root_turn


def pg_owner(database):
    repository = _repository(database)
    identity = uuid4().hex
    session_id = "session:pr05:" + identity
    deadline = monotonic() + 30
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id="ctx:workspace/" + identity,
        writer_owner_id="host:" + identity,
        lease_seconds=30,
        deadline_monotonic=deadline,
    )
    turn_id, entry_id, call_id = (
        "turn:" + identity,
        "entry:assistant:" + identity,
        "call:" + identity,
    )
    permission = build_run_permission_snapshot(
        snapshot_id="permission:" + identity,
        requested_mode=PermissionMode.ASK_PERMISSIONS,
        effective_mode=PermissionMode.ASK_PERMISSIONS,
        admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
    )
    _start_root_turn(
        repository,
        lease.guard,
        command_id="command:prompt:" + identity,
        turn_id=turn_id,
        entry_id="entry:prompt:" + identity,
        context_binding_revision_id="revision:" + identity,
        permission_snapshot_id=permission.snapshot_id,
        requested_permission_mode=PermissionMode.ASK_PERMISSIONS,
        content=FrozenPromptContent.text('PR05 harmless operation'),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=deadline,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=deadline
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"tool request"),
        blocks=(
            AssistantToolCallBlock(
                block_id="block:" + identity,
                tool_call_id=call_id,
                tool_name="terminal",
                arguments=freeze_json({"command": "true"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:fixture",
        deadline_monotonic=deadline,
    )
    live = SessionLiveControlOwner(
        session_id=session_id, owner_epoch=lease.guard.writer_generation
    )
    io = KernelSessionIO()
    owner = KernelInteractionCoordinator(
        repository=repository,
        guard=lease.guard,
        live_control=live,
        live_bus=LiveAgentEventBus(),
        io_owner=io,
    )
    return (
        owner,
        live,
        repository,
        dict(
            turn_id=turn_id,
            assistant_entry_id=entry_id,
            tool_call_id=call_id,
            tool_name="terminal",
            permission_snapshot=permission,
        ),
    )


def decision_request(owner, live, decision="ALLOW", actor="controller:1"):
    snapshot = live.current_snapshot()
    return dict(
        expected_writer_generation=owner._guard.writer_generation,
        expected_owner_epoch=snapshot.owner_epoch,
        expected_live_revision=snapshot.revision,
        interaction_id=snapshot.current_interaction.interaction_id,
        command_id="command:decision:" + uuid4().hex,
        decision=decision,
        actor_id=actor,
    )


def counts(repository, session_id):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 10
    ) as connection:
        return tuple(
            connection.execute(
                f"SELECT count(*) FROM pulsara_v3.{table} WHERE session_id = %s",
                (session_id,),
            ).fetchone()[0]
            for table in (
                "interaction_decisions",
                "session_commands",
                "tool_execution_attempts",
                "tool_results",
                "agent_events",
            )
        )


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
@pytest.mark.parametrize("fault", ["commit-response-loss", "rollback", "read-failure"])
def test_r13_r15_real_commit_rollback_readonly_confirmation(
    stage2_migrated_postgres_database, monkeypatch, decision, fault
):
    owner, live, repository, request = pg_owner(stage2_migrated_postgres_database)
    writer = repository.accept_tool_interaction_decision
    reader = repository.confirm_tool_interaction_decision
    exact_reader = repository._read_tool_interaction_decision
    append = repository._append_events
    writes, reads, facts = [], [], []
    started, release = Event(), Event()

    def fail_append(*args, **kwargs):
        append(*args, **kwargs)
        raise OSError("injected rollback after SQL inserts")

    def write(guard, **kwargs):
        writes.append(kwargs)
        started.set()
        assert release.wait(3)
        facts.append(writer(guard, **kwargs))
        raise OSError("injected response loss after actual COMMIT")

    def read(guard, **kwargs):
        reads.append(kwargs)
        before = counts(repository, guard.session_id)
        if fault == "read-failure":
            raise OSError("injected unavailable read, not an empty result")
        result = reader(guard, **kwargs)
        assert counts(repository, guard.session_id) == before
        return result

    def exact(connection, guard, **kwargs):
        if reads:
            assert (
                connection.execute("SHOW transaction_read_only").fetchone()[
                    "transaction_read_only"
                ]
                == "on"
            )
        return exact_reader(connection, guard, **kwargs)

    monkeypatch.setattr(repository, "accept_tool_interaction_decision", write)
    monkeypatch.setattr(repository, "confirm_tool_interaction_decision", read)
    monkeypatch.setattr(repository, "_read_tool_interaction_decision", exact)
    if fault == "rollback":
        monkeypatch.setattr(repository, "_append_events", fail_append)

    async def exercise():
        await owner.attach_controller("controller:1")
        waiter = asyncio.create_task(owner.request_tool_confirmation(**request))
        await asyncio.sleep(0)
        pending = owner._pending
        command = decision_request(owner, live, decision)
        submission = asyncio.create_task(owner.resolve_tool_interaction(**command))
        assert await asyncio.to_thread(started.wait, 1)
        assert live.current_snapshot().current_interaction.decision_in_progress
        # Invalidation reaches the original owner while its real SQL call waits.
        await owner._abort_candidate(
            interaction_id=pending.interaction_id,
            reference="interaction:expired",
            public_message="original deadline elapsed",
        )
        release.set()
        if fault == "commit-response-loss":
            accepted = await submission
            result = await waiter
            assert accepted == facts[0]
            assert result.decision == decision
            assert result.attempt_id == accepted.attempt_id
            assert result.result_entry_sequence == accepted.result_entry_sequence
            assert result.result_observed_at == accepted.result_observed_at
        else:
            with pytest.raises(ToolInteractionDecisionNotAccepted if fault == "rollback" else ToolInteractionDecisionOutcomeUnknown):
                await submission
            if fault == "rollback":
                assert (await waiter).reference == "interaction:expired"
                assert facts == []
            else:
                with pytest.raises(ConversationKernelConflict, match="unknown"):
                    await waiter
                assert len(facts) == 1  # DB commit survived an unavailable read.
        assert len(writes) == len(reads) == 1
        assert writes[0]["actor_id"] == reads[0]["actor_id"] == "controller:1"
        assert pending.settlement_changed.is_set()
        assert live.current_snapshot().current_interaction is None
        await owner.aclose()

    asyncio.run(exercise())
    observed = counts(repository, owner._guard.session_id)
    assert observed[0] == (0 if fault == "rollback" else 1)
    assert observed[2] == int(fault != "rollback" and decision == "ALLOW")
    assert observed[3] == int(fault != "rollback" and decision == "DENY")


class ProtocolHost:
    """Production attachment/resolve methods; unrelated UI metadata is fixture data."""

    attach_controller = KernelHostSession.attach_controller
    has_controller_attachment = KernelHostSession.has_controller_attachment
    controller_detached = KernelHostSession.controller_detached
    resolve_tool_interaction = KernelHostSession.resolve_tool_interaction

    def __init__(self, owner, live, repository):
        self._interactions = owner
        self._presentation_notices = {}
        self.session_id = owner._guard.session_id
        self.host_session_id = owner._guard.writer_owner_id
        self.repository = repository
        self.live_control = live
        self.live_bus = owner._live_bus

    def _require_open(self):
        assert not self._interactions._closed

    def current_todo_snapshots(self):
        return ()

    def current_compaction_projection(self):
        return False, None, None, None

    def active_root_turn_id(self):
        return None

    def control_admission_deadline_ms(self):
        return 10000


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
def test_r15_real_rollback_returns_explicit_non_acceptance_and_allows_new_click(
    stage2_migrated_postgres_database, monkeypatch, decision
):
    owner, live, repository, request = pg_owner(stage2_migrated_postgres_database)
    host = ProtocolHost(owner, live, repository)
    append = repository._append_events
    writer = repository.accept_tool_interaction_decision
    reader = repository.confirm_tool_interaction_decision
    writes, reads = [], []

    def rollback(*args, **kwargs):
        append(*args, **kwargs)
        raise OSError("injected actual rollback")

    def write(*args, **kwargs):
        writes.append(kwargs["command_id"])
        return writer(*args, **kwargs)

    def read(*args, **kwargs):
        result = reader(*args, **kwargs)
        reads.append(result)
        return result

    monkeypatch.setattr(repository, "_append_events", rollback)
    monkeypatch.setattr(repository, "accept_tool_interaction_decision", write)
    monkeypatch.setattr(repository, "confirm_tool_interaction_decision", read)

    async def exercise(socket_dir):
        server = TerminalKernelProtocolServer(socket_path=Path(socket_dir) / "s", session_provider=lambda _: host)

        async def resume(_):
            return SimpleNamespace(session_id=host.session_id, host_session_id=host.host_session_id)

        bridge = LocalBrowserBridge(sessions=SimpleNamespace(resume_session=resume), protocol_server=server)
        await server.start()
        waiter = None
        try:
            connected = await bridge.connect(host.session_id, browser_instance_id="browser:rollback")
            waiter = asyncio.create_task(owner.request_tool_confirmation(**request))
            await asyncio.sleep(0)
            pending = owner._pending
            deadline = pending.deadline_monotonic
            command = decision_request(owner, live, decision)
            body = {key: value for key, value in command.items() if key != "actor_id"}
            body["decision"] = "INTERACTION_" + decision
            rejected = await bridge.resolve_interaction(connected["connection_id"], body)
            assert rejected["error"]["stable_code"] == "INTERACTION_NOT_ACCEPTED"
            assert reads == [None] and len(writes) == 1
            assert owner._pending is pending and not waiter.done()
            assert pending.deadline_monotonic == deadline
            assert not live.current_snapshot().current_interaction.decision_in_progress
            assert counts(repository, host.session_id)[0] == 0
            monkeypatch.setattr(repository, "_append_events", append)
            retry = decision_request(owner, live, decision)
            body.update(command_id=retry["command_id"], expected_live_revision=retry["expected_live_revision"])
            await bridge.resolve_interaction(connected["connection_id"], body)
            assert (await waiter).decision == decision
            assert writes == [command["command_id"], retry["command_id"]]
            assert counts(repository, host.session_id)[0] == 1
        finally:
            await bridge.aclose()
            await owner.aclose()
            await server.close()
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)

    with TemporaryDirectory(prefix="pr05-rollback-", dir="/tmp") as socket_dir:
        asyncio.run(exercise(socket_dir))


@pytest.mark.parametrize("phase", ["writing", "confirming"])
def test_r12_real_unix_bridge_takeover_while_original_decision_settles(
    stage2_migrated_postgres_database, monkeypatch, phase
):
    owner, live, repository, request = pg_owner(stage2_migrated_postgres_database)
    host = ProtocolHost(owner, live, repository)
    started, release = Event(), Event()
    writes = []
    writer = repository.accept_tool_interaction_decision
    reader = repository.confirm_tool_interaction_decision

    def write(guard, **kwargs):
        writes.append(kwargs)
        if phase == "writing":
            started.set()
            assert release.wait(3)
            return writer(guard, **kwargs)
        writer(guard, **kwargs)
        raise OSError("commit response lost")

    def read(guard, **kwargs):
        started.set()
        assert release.wait(3)
        return reader(guard, **kwargs)

    monkeypatch.setattr(repository, "accept_tool_interaction_decision", write)
    monkeypatch.setattr(repository, "confirm_tool_interaction_decision", read)

    async def exercise():
        server = TerminalKernelProtocolServer(
            socket_path=Path(socket_dir) / "s", session_provider=lambda _: host
        )

        async def resume(session_id):
            assert session_id == host.session_id
            return SimpleNamespace(
                session_id=host.session_id, host_session_id=host.host_session_id
            )

        bridge = LocalBrowserBridge(
            sessions=SimpleNamespace(resume_session=resume), protocol_server=server
        )
        await server.start()
        waiter = None
        submission = None
        try:
            first = await bridge.connect(
                host.session_id, browser_instance_id="browser:first"
            )
            connection = bridge._connections[first["connection_id"]]
            old_id = connection.controller.attachment_id
            waiter = asyncio.create_task(owner.request_tool_confirmation(**request))
            await asyncio.sleep(0)
            command = decision_request(owner, live, actor=old_id)
            body = {key: value for key, value in command.items() if key != "actor_id"}
            body["decision"] = "INTERACTION_ALLOW"
            submission = asyncio.create_task(
                bridge.resolve_interaction(first["connection_id"], body)
            )
            assert await asyncio.to_thread(started.wait, 1)
            second = await asyncio.wait_for(
                bridge.connect(
                    host.session_id, browser_instance_id="browser:second", takeover=True
                ),
                1,
            )
            assert second["role"] == "controller"
            assert owner.is_current_controller(
                bridge._connections[second["connection_id"]].controller.attachment_id
            )
            assert not owner.is_current_controller(old_id)
            assert live.current_snapshot().current_interaction.decision_in_progress
            assert second["live_control_snapshot"]["snapshot"]["current_interaction"][
                "decision_in_progress"
            ]
            await host.controller_detached(old_id)
            assert not waiter.done()
            release.set()
            await asyncio.gather(
                submission, return_exceptions=True
            )  # ACK transport was closed, not replayed.
            assert (await asyncio.wait_for(waiter, 1)).decision == "ALLOW"
            assert len(writes) == 1
            assert owner.is_current_controller(
                bridge._connections[second["connection_id"]].controller.attachment_id
            )
        finally:
            release.set()
            if submission:
                await asyncio.gather(submission, return_exceptions=True)
            await bridge.aclose()
            await owner.aclose()
            await server.close()
            if waiter:
                await asyncio.gather(waiter, return_exceptions=True)

    with TemporaryDirectory(prefix="pr05-", dir="/tmp") as socket_dir:
        asyncio.run(exercise())


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
def test_r14_exact_confirmation_rejects_partial_and_wrong_identity(
    stage2_migrated_postgres_database, monkeypatch, decision
):
    owner, live, repository, request = pg_owner(stage2_migrated_postgres_database)
    writer = repository.accept_tool_interaction_decision
    written = []

    def record(guard, **kwargs):
        written.append(kwargs)
        return writer(guard, **kwargs)

    monkeypatch.setattr(repository, "accept_tool_interaction_decision", record)

    async def exercise():
        await owner.attach_controller("controller:1")
        waiting = asyncio.create_task(owner.request_tool_confirmation(**request))
        await asyncio.sleep(0)
        accepted = await owner.resolve_tool_interaction(**decision_request(owner, live, decision))
        assert (await waiting).decision == decision
        await owner.aclose()
        return accepted

    accepted = asyncio.run(exercise())
    exact = dict(written[0], deadline_monotonic=monotonic() + 10)
    before = counts(repository, owner._guard.session_id)
    assert repository.confirm_tool_interaction_decision(owner._guard, **exact) == accepted
    mutations = [
        {"actor_id": "controller:other"},
        {"redacted_subject": "other tool"},
        {"permission_snapshot_fingerprint": "other permission"},
        {"tool_call_id": "call:other"},
        {"decision_id": "decision:other"},
        # Existing attempt/result with neither of the requested command/decision
        # rows is partial truth, never proof that no decision was committed.
        {"command_id": "command:absent", "decision_id": "decision:absent"},
    ]
    for mutation in mutations:
        with pytest.raises(ConversationKernelConflict):
            repository.confirm_tool_interaction_decision(owner._guard, **(exact | mutation))
        assert counts(repository, owner._guard.session_id) == before
    assert len(written) == 1
