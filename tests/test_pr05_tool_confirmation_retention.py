"""PR05: exercise the production coordinator with explicit physical I/O barriers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from threading import Event

import pytest

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.interaction import (
    KernelInteractionCoordinator,
    ToolInteractionDecisionNotAccepted,
)
from pulsara_agent.conversation_kernel.interaction_arbiter import (
    InteractionAdmissionHooks,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.repository import (
    AcceptedInteractionDecision,
    ConversationKernelConflict,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)


class DecisionRepository:
    def __init__(self, *, block=False, failure=False, committed=False):
        self.started = Event()
        self.release = Event()
        if not block:
            self.release.set()
        self.failure = failure
        self.committed = committed
        self.writes = []
        self.reads = []
        self.accepted = None

    def accept_tool_interaction_decision(self, guard, **kwargs):
        self.writes.append(kwargs)
        self.started.set()
        assert self.release.wait(3), "test must release the physical writer"
        if self.committed or not self.failure:
            self.accepted = AcceptedInteractionDecision(
                kwargs["decision_id"],
                kwargs["command_id"],
                kwargs["decision"],
                kwargs["assistant_entry_id"],
                kwargs["tool_call_id"],
                kwargs["attempt_id"],
                kwargs["result_entry_id"],
                kwargs["permission_snapshot_fingerprint"],
                kwargs["result_id"],
                7 if kwargs["result_id"] else None,
                kwargs["occurred_at"] if kwargs["result_id"] else None,
            )
        if self.failure:
            raise OSError("injected lost writer response")
        return self.accepted

    def confirm_tool_interaction_decision(self, guard, **kwargs):
        self.reads.append(kwargs)
        return self.accepted


def make_owner(repository=None):
    repository = repository or DecisionRepository()
    live = SessionLiveControlOwner(session_id="session:pr05", owner_epoch=7)
    owner = KernelInteractionCoordinator(
        repository=repository,
        guard=HostWriterGuard("session:pr05", 7, "host:pr05"),
        live_control=live,
        live_bus=LiveAgentEventBus(),
        io_owner=KernelSessionIO(),
    )
    return owner, live, repository


def request(owner, ordinal=1, hooks=None):
    return asyncio.create_task(
        owner.request_tool_confirmation(
            turn_id=f"turn:{ordinal}",
            assistant_entry_id=f"entry:{ordinal}",
            tool_call_id=f"call:{ordinal}",
            tool_name="terminal",
            permission_snapshot=build_run_permission_snapshot(
                snapshot_id="permission:pr05",
                requested_mode=PermissionMode.ASK_PERMISSIONS,
                effective_mode=PermissionMode.ASK_PERMISSIONS,
                admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            ),
            admission_hooks=hooks,
        )
    )


def resolve(owner, live, *, actor="controller:1", decision="ALLOW"):
    snapshot = live.current_snapshot()
    return owner.resolve_tool_interaction(
        expected_writer_generation=7,
        expected_owner_epoch=snapshot.owner_epoch,
        expected_live_revision=snapshot.revision,
        interaction_id=snapshot.current_interaction.interaction_id,
        command_id=f"command:{decision}",
        decision=decision,
        actor_id=actor,
    )


def test_r01_visible_detach_retains_exact_future_and_deadline():
    async def exercise():
        owner, live, repository = make_owner()
        await owner.attach_controller("controller:1")
        waiter = request(owner)
        await asyncio.sleep(0)
        pending = owner._pending
        original = live.current_snapshot().current_interaction
        try:
            await owner.controller_detached("controller:1")
            assert owner._pending is pending
            assert not waiter.done() and not pending.future.done()
            assert repository.writes == []
            await owner.attach_controller("controller:2")
            assert live.current_snapshot().current_interaction == original
            await resolve(owner, live, actor="controller:2")
            assert (await waiter).attempt_id == pending.attempt_id
            assert len(repository.writes) == 1
        finally:
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


def test_r02_offline_candidate_waits_and_attach_promotes():
    async def exercise():
        owner, live, repository = make_owner()
        waiter = request(owner)
        await asyncio.sleep(0)
        try:
            assert not waiter.done()
            assert len(owner._dormant) == 1
            assert live.current_snapshot().current_interaction is None
            await owner.attach_controller("controller:1")
            assert live.current_snapshot().current_interaction is not None
            await resolve(owner, live)
            assert (await waiter).decision == "ALLOW"
            assert len(repository.writes) == 1
        finally:
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


def test_r03_admitted_unpublished_head_is_retained():
    async def exercise():
        owner, live, _ = make_owner()
        counts = []

        def admit():
            counts.append("admit")
            owner.detach_controller("controller:1")

        await owner.attach_controller("controller:1")
        waiter = request(
            owner,
            hooks=InteractionAdmissionHooks(admit, lambda: counts.append("discard")),
        )
        await asyncio.sleep(0)
        try:
            pending = owner._pending
            assert pending is not None and not pending.visible
            assert counts == ["admit"]
            await owner.attach_controller("controller:2")
            assert owner._pending is pending and pending.visible
            assert counts == ["admit"]
            await resolve(owner, live, actor="controller:2")
            assert (await waiter).decision == "ALLOW"
        finally:
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


def test_r08_old_controller_cannot_submit_after_exact_revoke():
    async def exercise():
        owner, live, repository = make_owner()
        await owner.attach_controller("controller:1")
        waiter = request(owner)
        await asyncio.sleep(0)
        owner.detach_controller("controller:1")
        await owner.attach_controller("controller:2")
        try:
            with pytest.raises(ConversationKernelConflict):
                await resolve(owner, live)
            assert repository.writes == []
        finally:
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


@pytest.mark.parametrize("decision", ["ALLOW", "DENY"])
def test_r14_writer_response_loss_recovers_full_facts_read_only(decision):
    async def exercise():
        owner, live, repository = make_owner(
            DecisionRepository(failure=True, committed=True)
        )
        await owner.attach_controller("controller:1")
        waiter = request(owner)
        await asyncio.sleep(0)
        try:
            accepted = await resolve(owner, live, decision=decision)
            result = await asyncio.wait_for(asyncio.shield(waiter), 1)
            assert result.decision == decision
            assert result.attempt_id == accepted.attempt_id
            assert result.result_entry_sequence == accepted.result_entry_sequence
            assert len(repository.writes) == len(repository.reads) == 1
            assert repository.reads[0]["actor_id"] == "controller:1"
        finally:
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


def test_r13_expiry_during_failed_submission_closes_future(monkeypatch):
    import pulsara_agent.conversation_kernel.interaction as interaction_module

    monkeypatch.setattr(interaction_module, "INTERACTION_TIMEOUT_SECONDS", 0.05)

    async def exercise():
        owner, live, repository = make_owner(
            DecisionRepository(block=True, failure=True)
        )
        await owner.attach_controller("controller:1")
        waiter = request(owner)
        await asyncio.sleep(0)
        pending = owner._pending
        submission = asyncio.create_task(resolve(owner, live))
        assert await asyncio.to_thread(repository.started.wait, 1)
        try:
            assert live.current_snapshot().current_interaction.decision_in_progress
            await asyncio.sleep(0.08)
            repository.release.set()
            with pytest.raises(ToolInteractionDecisionNotAccepted):
                await submission
            result = await asyncio.wait_for(asyncio.shield(waiter), 0.2)
            assert result.reference == "interaction:expired"
            assert pending.settlement_changed.is_set()
            assert len(repository.writes) == len(repository.reads) == 1
        finally:
            repository.release.set()
            await asyncio.gather(submission, return_exceptions=True)
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


def test_r05_deadline_is_frozen_at_enqueue(monkeypatch):
    import pulsara_agent.conversation_kernel.interaction as interaction_module

    monkeypatch.setattr(interaction_module, "INTERACTION_TIMEOUT_SECONDS", 0.1)

    async def exercise():
        owner, live, _ = make_owner()
        waiter = request(owner)
        await asyncio.sleep(0)
        try:
            pending = owner._dormant[0]
            original = pending.expires_at_utc
            await asyncio.sleep(0.02)
            await owner.attach_controller("controller:1")
            assert (
                live.current_snapshot().current_interaction.expires_at_utc == original
            )
            assert datetime.fromisoformat(original).tzinfo is not None
            assert (await waiter).reference == "interaction:expired"
        finally:
            await owner.aclose()
            await waiter

    asyncio.run(exercise())


@pytest.mark.parametrize("invalidation", ["expiry", "stop", "capability", "close"])
@pytest.mark.parametrize("committed", [False, True])
def test_r09_r13_r16_r18_submitted_winner_precedes_deferred_invalidation(
    invalidation, committed
):
    async def exercise():
        owner, live, repository = make_owner(
            DecisionRepository(block=True, failure=True, committed=committed)
        )
        await owner.attach_controller("controller:1")
        discarded = []
        waiter = request(
            owner,
            hooks=InteractionAdmissionHooks(
                lambda: None, lambda: discarded.append(True), "mcp:test"
            ),
        )
        await asyncio.sleep(0)
        pending = owner._pending
        submission = asyncio.create_task(resolve(owner, live))
        assert await asyncio.to_thread(repository.started.wait, 1)
        close = None
        if invalidation == "expiry":
            pending.deadline_monotonic = asyncio.get_running_loop().time() - 1
        elif invalidation == "stop":
            await owner._abort_candidate(
                interaction_id=pending.interaction_id,
                reference="interaction:turn-cancelled",
                public_message="stopped",
            )
        elif invalidation == "capability":
            await owner.cancel_tool_confirmations(
                owner_keys=frozenset({"mcp:test"}),
                reference="interaction:mcp-config-changed",
                public_message="changed",
            )
        else:
            close = asyncio.create_task(owner.aclose())
            await asyncio.sleep(0)
            assert not close.done()
        assert pending.resolving and not pending.future.done() and not discarded
        repository.release.set()
        if committed:
            await submission
            assert (await waiter).decision == "ALLOW"
            assert not discarded
        else:
            with pytest.raises(ToolInteractionDecisionNotAccepted):
                await submission
            result = await asyncio.wait_for(waiter, 1)
            assert (
                result.reference
                == {
                    "expiry": "interaction:expired",
                    "stop": "interaction:turn-cancelled",
                    "capability": "interaction:mcp-config-changed",
                    "close": "interaction:host-closing",
                }[invalidation]
            )
            assert discarded == [True]
        assert pending.settlement_changed.is_set()
        assert live.current_snapshot().current_interaction is None
        if close:
            await close
        await owner.aclose()

    asyncio.run(exercise())


def test_r04_r07_fifo_exact_child_cancellation_and_single_admission():
    async def exercise():
        owner, live, repository = make_owner()
        admitted, discarded = [], []
        waiters = [
            request(
                owner,
                n,
                InteractionAdmissionHooks(
                    lambda n=n: admitted.append(n), lambda n=n: discarded.append(n)
                ),
            )
            for n in range(1, 5)
        ]
        await asyncio.sleep(0)
        assert admitted == []
        original = tuple(owner._dormant)
        await owner.attach_controller("controller:1")
        assert admitted == [1]
        await owner._abort_candidate(
            interaction_id=original[2].interaction_id,
            reference="interaction:turn-cancelled",
            public_message="child cancelled",
        )
        assert discarded == [3] and len(owner._dormant) == 2
        for n in (1, 2, 4):
            assert owner._pending is original[n - 1]
            assert admitted[-1] == n
            await resolve(owner, live)
        results = await asyncio.gather(*waiters)
        assert [item.decision for item in results] == [
            "ALLOW",
            "ALLOW",
            "DENY",
            "ALLOW",
        ]
        assert admitted == [1, 2, 4]
        assert len(repository.writes) == 3
        await owner.aclose()

    asyncio.run(exercise())


def test_r05_first_promotion_is_inside_request_timeout(monkeypatch):
    import pulsara_agent.conversation_kernel.interaction as interaction_module

    monkeypatch.setattr(interaction_module, "INTERACTION_TIMEOUT_SECONDS", 0.03)

    async def exercise():
        owner, _, _ = make_owner()
        await owner.attach_controller("controller:1")
        entered = asyncio.Event()
        original = owner._promote_next

        async def barrier():
            if not entered.is_set():
                entered.set()
                await asyncio.Event().wait()
            await original()

        monkeypatch.setattr(owner, "_promote_next", barrier)
        discarded = []
        waiter = request(
            owner,
            hooks=InteractionAdmissionHooks(
                lambda: None, lambda: discarded.append(True)
            ),
        )
        await entered.wait()
        pending = owner._dormant[0]
        assert (await asyncio.wait_for(waiter, 1)).reference == "interaction:expired"
        assert pending.future.done() and discarded == [True]
        assert owner._pending is None and not owner._dormant
        await owner.aclose()

    asyncio.run(exercise())


def test_r06_takeover_never_renews_deadline_or_repeats_hook():
    async def exercise():
        owner, live, _ = make_owner()
        await owner.attach_controller("controller:1")
        calls = []
        waiter = request(
            owner,
            hooks=InteractionAdmissionHooks(
                lambda: calls.append("admit"), lambda: calls.append("discard")
            ),
        )
        await asyncio.sleep(0)
        pending = owner._pending
        original = live.current_snapshot().current_interaction
        for ordinal in range(1, 8):
            await owner.controller_detached(f"controller:{ordinal}")
            await owner.attach_controller(f"controller:{ordinal + 1}")
            await owner.controller_detached(f"controller:{ordinal}")
            assert owner.is_current_controller(f"controller:{ordinal + 1}")
            assert owner._pending is pending
            assert live.current_snapshot().current_interaction == original
        assert calls == ["admit"]
        await owner.aclose()
        assert (await waiter).reference == "interaction:host-closing"
        assert calls == ["admit", "discard"]

    asyncio.run(exercise())


def test_r11_r14_cancelled_socket_waiter_cannot_discard_full_winner():
    async def exercise():
        owner, live, repository = make_owner(
            DecisionRepository(block=True, failure=True, committed=True)
        )
        await owner.attach_controller("controller:1")
        waiter = request(owner)
        await asyncio.sleep(0)
        pending = owner._pending
        submission = asyncio.create_task(resolve(owner, live))
        assert await asyncio.to_thread(repository.started.wait, 1)
        submission.cancel()
        await owner.controller_detached("controller:1")
        await owner.attach_controller("controller:2")
        with pytest.raises(ConversationKernelConflict):
            await resolve(owner, live, actor="controller:2")
        repository.release.set()
        with pytest.raises(asyncio.CancelledError):
            await submission
        assert (await waiter).attempt_id == pending.attempt_id
        assert len(repository.writes) == len(repository.reads) == 1
        assert repository.reads[0]["actor_id"] == "controller:1"
        await owner.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [OSError, ConversationKernelConflict])
def test_r14_confirmation_failure_is_unknown_not_stale_at_gateway(tmp_path, failure):
    from types import SimpleNamespace
    from tests.test_pr05_confirmation_postgres import ProtocolHost
    from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
    from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire

    async def exercise():
        owner, live, repository = make_owner(DecisionRepository(failure=True, committed=True))

        def failed_read(guard, **kwargs):
            repository.reads.append(kwargs)
            raise failure("injected exact read failure/conflict")

        repository.confirm_tool_interaction_decision = failed_read
        await owner.attach_controller("controller:1")
        waiter = request(owner)
        await asyncio.sleep(0)
        snapshot = live.current_snapshot()
        host = ProtocolHost(owner, live, repository)
        server = TerminalKernelProtocolServer(socket_path=tmp_path / "s", session_provider=lambda _: host)
        state = SimpleNamespace(host_session=host, granted_role=wire.ATTACHMENT_ROLE_CONTROLLER,
            attachment_id="controller:1")
        response = await server._resolve_interaction(state, wire.ResolveInteractionRequest(
            request_id="request:unknown", command_id="command:unknown", expected_writer_generation=7,
            expected_owner_epoch=snapshot.owner_epoch, expected_live_revision=snapshot.revision,
            interaction_id=snapshot.current_interaction.interaction_id, decision=wire.INTERACTION_ALLOW))
        assert response.error.stable_code == "INTERACTION_OUTCOME_UNKNOWN"
        assert repository.accepted.decision == "ALLOW"
        assert len(repository.reads) == len(repository.writes) == 1
        with pytest.raises(ConversationKernelConflict, match="outcome is unknown"):
            await waiter
        assert owner._pending is None
        await owner.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["writing", "confirming", "expired-wait"])
@pytest.mark.parametrize("committed", [False, True])
def test_r07_r14_cancel_real_mcp_tool_waiter_joins_and_releases(
    tmp_path, monkeypatch, phase, committed
):
    from tests.test_round6_mcp_production import (
        McpHostSupervisor, DirectKernelToolPort, DefaultToolDispatchAuthorizationPolicy,
        ModelInputScopeKind, prepare_test_direct_tool_surface, _config,
        _seal_mcp_test_port, _enabled_memory_context,
    )
    import pulsara_agent.conversation_kernel.interaction as interaction_module

    if phase == "expired-wait":
        monkeypatch.setattr(interaction_module, "INTERACTION_TIMEOUT_SECONDS", 0.05)

    async def exercise():
        repository = DecisionRepository(block=phase != "confirming", failure=True, committed=committed)
        read_started, read_release = Event(), Event()
        reader = repository.confirm_tool_interaction_decision

        def held_read(*args, **kwargs):
            read_started.set()
            assert read_release.wait(3)
            return reader(*args, **kwargs)

        if phase == "confirming":
            monkeypatch.setattr(repository, "confirm_tool_interaction_decision", held_read)
        owner, live, _ = make_owner(repository)
        await owner.attach_controller("controller:1")
        supervisor = McpHostSupervisor(session_id="session:pr05", workspace_root=tmp_path, configs=(_config(tmp_path),))
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id="host:pr05",
            session_id="session:pr05", live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.bind_interaction_port(owner)
        port.bind_mcp_supervisor(supervisor)
        _seal_mcp_test_port(port)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None)
        effect = next(item for item in surface.model_surface.tool_specs if item.name.endswith("fixture_effect"))
        borrow = port.borrow_tool_surface(surface)
        permission = build_run_permission_snapshot(snapshot_id="permission:pr05",
            requested_mode=PermissionMode.ASK_PERMISSIONS, effective_mode=PermissionMode.ASK_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
        waiter = submission = None
        try:
            await port.authorize(tool_name=effect.name, arguments={"value": "must-not-execute"},
                tool_call_id="call:cancel", turn_id="turn:cancel", assistant_entry_id="entry:cancel",
                permission_snapshot=permission, surface_borrow=borrow, memory_context=_enabled_memory_context())
            prepared = port.prepare_permission_request(tool_call_id="call:cancel", turn_id="turn:cancel", surface_borrow=borrow)
            waiter = asyncio.create_task(port.request_confirmation(prepared_request=prepared,
                tool_name=effect.name, assistant_entry_id="entry:cancel", permission_snapshot=permission))
            await asyncio.sleep(0)
            pending = owner._pending
            permit = next(iter(port._mcp_dispatch_permits.values()))
            releases = []
            release = type(permit).release

            def counted_release(current):
                if current is permit:
                    releases.append(current.state.value)
                return release(current)

            monkeypatch.setattr(type(permit), "release", counted_release)
            submission = asyncio.create_task(resolve(owner, live))
            assert await asyncio.to_thread((read_started if phase == "confirming" else repository.started).wait, 1)
            if phase == "expired-wait":
                while pending.invalidation is None:
                    await asyncio.sleep(0.005)
                assert pending.invalidation[0] == "interaction:expired"
            waiter.cancel()
            await asyncio.sleep(0)
            waiter.cancel()  # A second owner cancellation must not break the join.
            await asyncio.sleep(0)
            joined = not waiter.done()
            assert permit.state.value == "ADMITTED"
            repository.release.set()
            read_release.set()
            await asyncio.gather(submission, return_exceptions=True)
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not port._mcp_dispatch_permits
            assert not port._mcp_confirmation_admissions
            assert releases == ["ADMITTED"]
            assert permit.state.value == "RELEASED"
            assert joined, "real tool waiter must join the original decision before cancellation exits"
            assert pending.future.done() and pending.settlement_changed.is_set()
            assert owner._pending is None
            assert len(repository.writes) == len(repository.reads) == 1
            assert (repository.accepted.decision if repository.accepted else None) == ("ALLOW" if committed else None)
        finally:
            repository.release.set()
            read_release.set()
            if submission:
                await asyncio.gather(submission, return_exceptions=True)
            if waiter and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            borrow.close()
            await owner.aclose()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    asyncio.run(exercise())
