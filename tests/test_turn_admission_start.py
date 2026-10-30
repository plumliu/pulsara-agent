"""Request start settlement is independent of first provider preparation."""
from __future__ import annotations

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.repository import TurnAdmissionConfirmationKind
from pulsara_agent.conversation_kernel.turn_admission import TurnAdmissionCoordinator
from pulsara_agent.model_input.contracts import ModelInputScopeKind


class _IO:
    async def run(self, operation, *args, **kwargs):
        result = operation(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


class _Repository:
    def __init__(self):
        self.writes = 0
        self.confirms = 0
        self.interruptions = []
        self.outcome = SimpleNamespace(
            accepted=SimpleNamespace(turn_id="turn", entry_id="entry"),
            started_input_group=object(),
        )
        self.error = None
        self.confirmation = SimpleNamespace(kind=TurnAdmissionConfirmationKind.NONE)

    def accept_root_turn_intent(self, guard, **kwargs):
        assert "provider_input_admission" not in kwargs
        self.writes += 1
        if self.error is not None:
            raise self.error
        return self.outcome

    def require_provider_safe_turn(self, guard, **kwargs):
        return None

    def confirm_root_turn_intent(self, **kwargs):
        self.confirms += 1
        return self.confirmation

    def interrupt_turn(self, guard, *, turn_id, reason, **kwargs):
        self.interruptions.append((turn_id, reason))
        return True


def _coordinator(repository, finalizer=None):
    return TurnAdmissionCoordinator(
        repository=repository,
        io_owner=_IO(),
        writer_lease=SimpleNamespace(guard=SimpleNamespace(session_id="session")),
        deadline_factory=SimpleNamespace(deadline=lambda _owner: monotonic() + 5),
        todo_finalizer=finalizer,
    )


def _request():
    return SimpleNamespace(
        session_id="session", command_id="command", turn_id="turn",
        entry_id="entry", context_binding_revision_id="binding",
    )


def _intent():
    return ActiveTurnCancellationIntent("turn", ModelInputScopeKind.ROOT)


def test_start_does_not_require_compiled_provider_input():
    async def scenario():
        repository = _Repository()
        activated = []
        async def finalize(prepared, accepted):
            activated.append(accepted.turn_id)
        outcome = await _coordinator(repository, finalize).accept_root_intent(
            _request(), model_resolution_snapshot=object(), cancellation_intent=_intent(),
        )
        assert outcome is repository.outcome
        assert repository.writes == 1
        assert activated == ["turn"]
    asyncio.run(scenario())


def test_deterministic_rejection_and_none_is_not_reissued():
    async def scenario():
        repository = _Repository()
        repository.error = ValueError("permission changed")
        with pytest.raises(ValueError, match="permission changed"):
            await _coordinator(repository).accept_root_intent(
                _request(), model_resolution_snapshot=object(), cancellation_intent=_intent(),
            )
        assert (repository.writes, repository.confirms) == (1, 1)
        assert repository.interruptions == []
    asyncio.run(scenario())


def test_unknown_ack_full_preserves_started_group_and_finalizes_once():
    async def scenario():
        repository = _Repository()
        repository.error = ConnectionError("commit acknowledgement lost")
        repository.confirmation = SimpleNamespace(
            kind=TurnAdmissionConfirmationKind.FULL,
            accepted=repository.outcome.accepted,
            started_input_group=repository.outcome.started_input_group,
        )
        # The immutable admission is valid even when current status/binding
        # already advanced; start settlement does not impose RUNNING/revision0.
        activated = []
        async def finalize(prepared, accepted):
            activated.append(accepted.turn_id)
        outcome = await _coordinator(repository, finalize).accept_root_intent(
            _request(), model_resolution_snapshot=object(), cancellation_intent=_intent(),
        )
        assert outcome.started_input_group is repository.outcome.started_input_group
        assert (repository.writes, repository.confirms) == (1, 1)
        assert activated == ["turn"]
    asyncio.run(scenario())


def test_stop_joins_started_write_then_interrupts_original_turn():
    async def scenario():
        repository = _Repository()
        started, release = asyncio.Event(), asyncio.Event()
        original = repository.accept_root_turn_intent
        async def blocked_write(*args, **kwargs):
            started.set()
            await release.wait()
            return original(*args, **kwargs)
        repository.accept_root_turn_intent = blocked_write
        intent = _intent()
        task = asyncio.create_task(_coordinator(repository).accept_root_intent(
            _request(), model_resolution_snapshot=object(), cancellation_intent=intent,
        ))
        await started.wait()
        intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repository.writes == 1
        assert repository.interruptions == [("turn", "USER_STOPPED")]
    asyncio.run(scenario())


def test_cancelled_none_does_not_restart_request():
    async def scenario():
        repository = _Repository()
        intent = _intent()
        def reject(*args, **kwargs):
            repository.writes += 1
            intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
            raise ConnectionError("not committed")
        repository.accept_root_turn_intent = reject
        with pytest.raises(asyncio.CancelledError):
            await _coordinator(repository).accept_root_intent(
                _request(), model_resolution_snapshot=object(), cancellation_intent=intent,
            )
        assert (repository.writes, repository.confirms) == (1, 1)
        assert repository.interruptions == []
    asyncio.run(scenario())


def test_todo_finalization_failure_settles_started_turn():
    async def scenario():
        repository = _Repository()
        async def finalize(prepared, accepted):
            raise RuntimeError("TODO activation failed")
        with pytest.raises(RuntimeError, match="TODO activation failed"):
            await _coordinator(repository, finalize).accept_root_intent(
                _request(), model_resolution_snapshot=object(), cancellation_intent=_intent(),
            )
        assert repository.interruptions == [("turn", "FOREGROUND_EXECUTION_INTERRUPTED")]
    asyncio.run(scenario())


def test_first_stop_cause_wins_over_concurrent_todo_failure():
    async def scenario():
        repository = _Repository()
        intent = _intent()
        async def finalize(prepared, accepted):
            intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
            raise RuntimeError("TODO activation failed while stopping")
        with pytest.raises(RuntimeError, match="while stopping"):
            await _coordinator(repository, finalize).accept_root_intent(
                _request(), model_resolution_snapshot=object(), cancellation_intent=intent,
            )
        assert repository.interruptions == [("turn", "USER_STOPPED")]
    asyncio.run(scenario())


@pytest.mark.postgres
def test_preparation_observer_failure_cannot_strand_started_turn(
    stage2_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from tests.support.postgres import verified_postgres_provider
    from tests.test_stage2_conversation_runner import (
        _AssertingTool,
        _ScriptedModel,
        _acquire_bound_host_writer,
        _name,
        _text_stream,
        frozen_test_prompt,
        StaticContextSourceCollector,
        StructuredToolPort,
        test_model_resolution_snapshot,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository, session_id=session_id, workspace_id=_name("workspace"),
        writer_owner_id=_name("host"), lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    async def failing_observer(turn_id, reason):
        raise RuntimeError("failed to publish preparation observation")
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository, writer_lease=lease, model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=4,
        provider_input_failure_observer=failing_observer,
    )
    with pytest.raises(RuntimeError, match="preparation observation"):
        asyncio.run(runner.run_turn(frozen_test_prompt("retain the original request")))
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.turns WHERE session_id=%s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED", "PROVIDER_INPUT_RESOURCE_EXHAUSTED")


def test_waiter_cancel_without_explicit_cause_prevents_none_reissue():
    async def scenario():
        repository = _Repository()
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked_write(*args, **kwargs):
            repository.writes += 1
            started.set()
            await release.wait()
            raise ConnectionError("write did not commit")
        repository.accept_root_turn_intent = blocked_write
        task = asyncio.create_task(_coordinator(repository).accept_root_intent(
            _request(), model_resolution_snapshot=object(), cancellation_intent=_intent(),
        ))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (repository.writes, repository.confirms) == (1, 1)
        assert repository.interruptions == []
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("winner", ["terminal", "takeover"])
def test_ack_unknown_terminal_or_takeover_never_republishes_active_todo(
    tmp_path, stage2_migrated_postgres_database, monkeypatch, winner,
):
    from datetime import datetime, timezone
    from psycopg.rows import dict_row
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelConflict, StaleHostWriter,
    )
    from pulsara_agent.identity import random_id
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from tests.support.postgres import verified_postgres_provider
    from tests.test_scheduled_tasks_host import setup
    from tests.test_stage2_kernel_host_dogfood import _SteerModelPort

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        model = _SteerModelPort()
        core, sessions, service, handle, root = await setup(
            tmp_path, stage2_migrated_postgres_database, monkeypatch, model,
        )
        session = handle.session
        repository = session.repository
        provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
        original_accept = repository.accept_root_turn_intent
        original_finalizer = session._runner._turn_admission._todo_finalizer
        activations = []
        captured = []
        async def observe_finalization(prepared, accepted):
            activations.append(accepted.turn_id)
            await original_finalizer(prepared, accepted)
        session._runner._turn_admission._todo_finalizer = observe_finalization
        command = random_id("command")
        def commit_then_lose_ack(guard, **kwargs):
            outcome = original_accept(guard, **kwargs)
            captured.append(outcome)
            if winner == "terminal":
                repository.interrupt_turn(
                    guard, turn_id=outcome.accepted.turn_id,
                    reason="FOREGROUND_EXECUTION_INTERRUPTED",
                    occurred_at=datetime.now(timezone.utc), actor_id="test",
                    deadline_monotonic=monotonic() + 10,
                )
            else:
                with provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                    deadline_monotonic=monotonic() + 10,
                ) as connection:
                    metadata = connection.execute(
                        "SELECT s.memory_domain_id, w.workspace_kind, w.workspace_root, w.workspace_label "
                        "FROM pulsara_v3.sessions s JOIN pulsara_v3.workspaces w "
                        "ON w.id=s.workspace_id AND w.memory_domain_id=s.memory_domain_id "
                        "WHERE s.id=%s", (session.session_id,),
                    ).fetchone()
                repository.acquire_host_writer(
                    intent="EXISTING", session_id=session.session_id,
                    workspace_id=session.workspace.workspace_key,
                    memory_domain_id=metadata["memory_domain_id"],
                    workspace_kind=metadata["workspace_kind"],
                    workspace_root=metadata["workspace_root"],
                    workspace_label=metadata["workspace_label"],
                    writer_owner_id=random_id("host"), lease_seconds=30,
                    deadline_monotonic=monotonic() + 10,
                )
            raise ConnectionError("the start commit acknowledgement was lost")
        monkeypatch.setattr(repository, "accept_root_turn_intent", commit_then_lose_ack)
        try:
            expected = StaleHostWriter if winner == "takeover" else ConversationKernelConflict
            with pytest.raises(expected):
                await session.run_turn(
                    PromptContent.text("keep the exact original accepted request"),
                    command_id=command,
                )
            assert len(captured) == 1
            assert activations == []
            assert session._tools.todo_owner.current_snapshots() == ()
            assert session.active_root_turn_id() is None
            assert not session._active_turn_started
            assert model.requests == []
            with provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                deadline_monotonic=monotonic() + 10,
            ) as connection:
                assert connection.execute(
                    "SELECT status, initial_entry_id FROM pulsara_v3.turns WHERE session_id=%s",
                    (session.session_id,),
                ).fetchone() == ("INTERRUPTED", captured[0].accepted.entry_id)
                assert connection.execute(
                    "SELECT target_turn_id FROM pulsara_v3.session_commands "
                    "WHERE session_id=%s AND command_id=%s",
                    (session.session_id, command),
                ).fetchone() == (captured[0].accepted.turn_id,)
        finally:
            model.release.set()
            await service.aclose()
            await sessions.aclose()
            await core.shutdown()
    asyncio.run(scenario())
