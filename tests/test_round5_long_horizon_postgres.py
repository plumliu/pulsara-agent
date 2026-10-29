from __future__ import annotations

from pulsara_agent.identity import random_id

from tests.support.model_config import frozen_test_prompt
from pulsara_agent.llm.input import FrozenPromptContent

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event
from time import monotonic
from pathlib import Path

import pytest
from psycopg import OperationalError

from pulsara_agent.conversation_kernel.contracts import (
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.direct_model import KernelModelExecutionRequest
from pulsara_agent.conversation_kernel.io import KernelSessionIOClosed
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
    StaleHostWriter,
    ToolRemoteIdentityConfirmationKind,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
)
from pulsara_agent.conversation_kernel.repository_errors import PreparedRootInputHeadStale
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
    ProcessLocalEffectSettlementResult,
    ProcessLocalEffectSettlementToken,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.llm.input import LLMTextPart, MessageRole
from tests.support.postgres import verified_postgres_provider
from tests.support.model_config import (
    acquire_bound_test_writer,
    enqueue_test_prompt,
    test_model_resolution_snapshot,
)
from tests.support.round3 import (
    CallbackScriptedKernelModel,
    ScriptedKernelModel,
    StaticContextSourceCollector,
    StructuredToolPort,
    seal_test_direct_tool_port,
)


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return random_id(prefix)


def _text_stream(text: str, *, block_id: str) -> list[object]:
    return [
        TextStartPayload(block_id),
        TextDeltaPayload(block_id, text),
        TextEndPayload(
            block_id,
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        ),
    ]


def _tool_stream(
    index: int,
    *,
    tool_name: str = "test_tool",
    arguments: str = "{}",
) -> list[object]:
    tool_call_id = f"tool-call:{index}"
    block_id = tool_call_id
    return [
        ToolCallStartPayload(block_id, tool_call_id, tool_name),
        ToolCallDeltaPayload(block_id, tool_call_id, arguments),
        ToolCallEndPayload(
            block_identity=block_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_json=arguments,
            utf8_bytes=len(arguments),
            digest=live_digest(arguments),
        ),
    ]


class _KnownReadOnlyTool:
    def __init__(self) -> None:
        self.invocations = 0

    async def authorize(self, **_kwargs: object) -> KernelToolAuthorization:
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            "round5:test-policy",
        )

    async def request_confirmation(self, **_kwargs: object) -> KernelToolAuthorization:
        raise AssertionError("the Round 5 test tool never requests confirmation")

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        return KernelToolResult(state="SUCCESS", content=b"ok")


class _LostRootAdmissionAckRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = 0

    def accept_root_turn_intent(self, *args, **kwargs):
        self.calls += 1
        accepted = super().accept_root_turn_intent(*args, **kwargs)
        if self.calls == 1:
            raise OSError("injected lost ROOT admission acknowledgement")
        return accepted


class _RootNoneThenFullRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = 0

    def accept_root_turn_intent(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise OSError("injected pre-commit ROOT failure")
        return super().accept_root_turn_intent(*args, **kwargs)


class _RootConflictRepository(ConversationKernelRepository):
    def accept_root_turn_intent(self, *args, **kwargs):
        del args, kwargs
        raise OSError("injected ROOT admission conflict window")

    def confirm_root_turn_intent(self, **kwargs):
        del kwargs
        return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)


class _CancelledRootAdmissionRepository(ConversationKernelRepository):
    def __init__(self, provider, *, commit_before_block: bool) -> None:
        super().__init__(provider)
        self.commit_before_block = commit_before_block
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def accept_root_turn_intent(self, *args, **kwargs):
        self.calls += 1
        accepted = (
            super().accept_root_turn_intent(*args, **kwargs)
            if self.commit_before_block
            else None
        )
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release ROOT admission")
        if accepted is None:
            raise OSError("injected pre-commit ROOT admission failure")
        return accepted


class _FlakyCancelledRootConfirmationRepository(_CancelledRootAdmissionRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider, commit_before_block=True)
        self.confirmation_calls = 0

    def confirm_root_turn_intent(self, **kwargs):
        self.confirmation_calls += 1
        if self.confirmation_calls <= 2:
            raise OSError("injected transient ROOT confirmation failure")
        return super().confirm_root_turn_intent(**kwargs)












class _PhysicalFailureTool(_KnownReadOnlyTool):
    def __init__(
        self,
        effect_class: str,
        *,
        error: BaseException | None = None,
        raw: bool = False,
        caller_cancelled: bool = False,
    ) -> None:
        super().__init__()
        self.effect_class = effect_class
        self.error = error or OSError("injected physical failure")
        self.raw = raw
        self.caller_cancelled = caller_cancelled

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        if self.effect_class == "admission" or self.raw:
            raise self.error
        raise KernelToolPhysicalInvocationError(
            effect_class=self.effect_class,
            error=self.error,
            timing="LATE_AFTER_WATCHDOG",
            caller_cancelled=self.caller_cancelled,
        )


class _TakeoverDuringTool(_KnownReadOnlyTool):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        self.started.set()
        await self.release.wait()
        return KernelToolResult(
            state="SUCCESS",
            content=b"late exact result",
            physical_timing="LATE_AFTER_WATCHDOG",
        )


class _BlockingToolResultAcceptanceRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.acceptance_started = Event()
        self.release_acceptance = Event()
        self.acceptance_calls = 0

    def accept_tool_result(self, *args, **kwargs):
        self.acceptance_calls += 1
        self.acceptance_started.set()
        if not self.release_acceptance.wait(5):
            raise TimeoutError("test did not release ToolResult acceptance")
        return super().accept_tool_result(*args, **kwargs)


class _SettlementTokenTool(_KnownReadOnlyTool):
    def __init__(self) -> None:
        super().__init__()
        self.settlements: list[ProcessLocalEffectSettlementDisposition] = []

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        return KernelToolResult(
            state="SUCCESS",
            content=b"known exact result",
            process_local_settlement=ProcessLocalEffectSettlementToken(
                token_id="terminal-monitor-token:test",
                prepared=self,
            ),
        )

    async def settle_process_local_effect(
        self,
        _token: ProcessLocalEffectSettlementToken,
        disposition: ProcessLocalEffectSettlementDisposition,
    ) -> ProcessLocalEffectSettlementResult:
        self.settlements.append(disposition)
        return ProcessLocalEffectSettlementResult(
            ProcessLocalEffectSettlementOutcome.INSTALLED
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED
            else ProcessLocalEffectSettlementOutcome.DISCARDED
        )


class _RemoteIdentityTool(_KnownReadOnlyTool):
    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        return KernelToolResult(
            state="SUCCESS",
            content=b"known exact result",
            remote_identity="terminal-process:test-exact",
        )


class _LostRemoteIdentityAckRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.publication_calls = 0
        self.confirmation_calls = 0
        self.candidate = None

    def publish_tool_remote_identity(self, *args, **kwargs):
        self.publication_calls += 1
        self.candidate = kwargs["candidate"]
        installed = super().publish_tool_remote_identity(*args, **kwargs)
        if self.publication_calls == 1:
            raise OSError("injected lost remote-identity acknowledgement")
        return installed

    def confirm_tool_remote_identity(self, *args, **kwargs):
        self.confirmation_calls += 1
        if self.confirmation_calls == 1:
            raise OSError("injected transient remote-identity confirmation failure")
        return super().confirm_tool_remote_identity(*args, **kwargs)


class _DeadlineMarkerTool(_KnownReadOnlyTool):
    def __init__(self, factory: "_RecordingDeadlineFactory") -> None:
        super().__init__()
        self._factory = factory
        self.owner_count_at_physical_return: int | None = None

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        self.owner_count_at_physical_return = len(self._factory.owners)
        return KernelToolResult(state="SUCCESS", content=b"long exact result")


class _RecordingDeadlineFactory(KernelExecutionDeadlineFactory):
    def __init__(self) -> None:
        self.owners: list[KernelWatchdogOwner] = []
        self._tick = monotonic()
        super().__init__(clock=self._clock)

    def _clock(self) -> float:
        # Simulate more than the removed 120-second turn budget elapsing
        # between independently-owned operations without sleeping.
        self._tick += 121.0
        return self._tick

    def deadline(self, owner: KernelWatchdogOwner) -> float:
        self.owners.append(owner)
        return super().deadline(owner)


class _EnqueueSteerAtCallModel(ScriptedKernelModel):
    def __init__(
        self,
        calls: list[list[object]],
        *,
        at_call: int,
        enqueue: Callable[[KernelModelExecutionRequest], None],
    ) -> None:
        super().__init__(calls)
        self._at_call = at_call
        self._enqueue = enqueue
        self._enqueued = False

    def preflight_execution(
        self, request: KernelModelExecutionRequest, **kwargs: object
    ):
        if request.model_call_index == self._at_call and not self._enqueued:
            self._enqueued = True
            self._enqueue(request)
        return super().preflight_execution(request, **kwargs)


def _runner(
    repository: ConversationKernelRepository,
    lease,
    model,
    tool=None,
    *,
    tool_names: tuple[str, ...] = ("test_tool",),
    deadline_factory: KernelExecutionDeadlineFactory | None = None,
):
    # These fixtures run a bare Runner, without KernelHostSession's renewal
    # task. Keep the real lease alive at each model boundary so machine load
    # cannot turn it into an accidental total-turn time cap. Renewal still
    # checks the exact guard and cannot revive an expired or replaced writer.
    preflight = model.preflight_execution

    def renew_before_preflight(request, **kwargs):
        repository.renew_host_writer(
            lease.guard, lease_seconds=30, deadline_monotonic=monotonic() + 30
        )
        return preflight(request, **kwargs)

    model.preflight_execution = renew_before_preflight
    return ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(
            tool or _KnownReadOnlyTool(),
            tool_names=tool_names,
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        deadline_factory=deadline_factory,
    )


def _lease(repository: ConversationKernelRepository) -> tuple[str, str, object]:
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = acquire_bound_test_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    return session_id, workspace_id, lease






def test_round5_sixty_four_model_calls_finalize_without_a_turn_cap(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    streams = [_tool_stream(index) for index in range(63)]
    streams.append(_text_stream("final-64", block_id="final:64"))
    model = ScriptedKernelModel(streams)
    tool = _KnownReadOnlyTool()

    result = asyncio.run(_runner(repository, lease, model, tool).run_turn(frozen_test_prompt("start")))

    assert result.model_call_count == 64
    assert result.tool_call_count == 63
    assert result.final_text == "final-64"
    assert tool.invocations == 63
    assert len(model.requests) == 64
    for previous, current in zip(model.requests, model.requests[1:], strict=False):
        assert (
            current.compiled_input.system_prompt
            == previous.compiled_input.system_prompt
        )
        assert current.compiled_input.tools == previous.compiled_input.tools
        assert current.compiled_input.messages[
            : len(previous.compiled_input.messages)
        ] == (previous.compiled_input.messages)


def test_round5_each_operation_gets_a_fresh_owner_deadline_without_turn_budget(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    deadlines = _RecordingDeadlineFactory()
    model = ScriptedKernelModel([_text_stream("done", block_id="answer")])

    result = asyncio.run(
        _runner(
            repository,
            lease,
            model,
            tool_names=(),
            deadline_factory=deadlines,
        ).run_turn(frozen_test_prompt("start"))
    )

    assert result.final_text == "done"
    # The first ROOT call has three additional K3 planning owners before the
    # ordinary per-call owner: prospective freeze, Hook sibling, and activation.
    assert deadlines.owners.count(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING) == 4
    assert deadlines.owners.count(KernelWatchdogOwner.FOREGROUND_CANONICAL) >= 4


def test_round5_more_than_sixty_four_tool_calls_in_one_turn_finalize(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    # Keep the long-horizon claim while making every response batch satisfy the
    # K3 effect-before-settlement upper bound.  This remains 65 tool effects in
    # one turn; it does not introduce a turn, call, or tool lifetime cap.
    model = ScriptedKernelModel(
        [
            *(_tool_stream(index) for index in range(65)),
            _text_stream("after-65-tools", block_id="final:tools"),
        ]
    )
    tool = _KnownReadOnlyTool()

    result = asyncio.run(_runner(repository, lease, model, tool).run_turn(frozen_test_prompt("start")))

    assert result.model_call_count == 66
    assert result.tool_call_count == 65
    assert result.final_text == "after-65-tools"
    assert tool.invocations == 65


def test_round5_tool_result_settlement_gets_fresh_canonical_watchdogs(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    deadlines = _RecordingDeadlineFactory()
    tool = _DeadlineMarkerTool(deadlines)
    model = ScriptedKernelModel(
        [
            _tool_stream(1),
            _text_stream("done", block_id="answer"),
        ]
    )

    result = asyncio.run(
        _runner(
            repository,
            lease,
            model,
            tool,
            deadline_factory=deadlines,
        ).run_turn(frozen_test_prompt("start"))
    )

    assert result.final_text == "done"
    assert tool.owner_count_at_physical_return is not None
    owners_after_return = deadlines.owners[tool.owner_count_at_physical_return :]
    assert owners_after_return
    assert owners_after_return[0] is KernelWatchdogOwner.FOREGROUND_CANONICAL
    assert owners_after_return.count(KernelWatchdogOwner.FOREGROUND_CANONICAL) >= 3


def test_round5_exact_tool_return_is_shielded_through_canonical_settlement(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _BlockingToolResultAcceptanceRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    tool = _SettlementTokenTool()
    model = ScriptedKernelModel([_tool_stream(1)])

    async def scenario() -> None:
        operation = asyncio.create_task(
            _runner(repository, lease, model, tool).run_turn(frozen_test_prompt("start"))
        )
        assert await asyncio.to_thread(repository.acceptance_started.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        repository.release_acceptance.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(scenario())


    assert repository.acceptance_calls == 1
    assert tool.invocations == 1
    assert tool.settlements == [ProcessLocalEffectSettlementDisposition.COMMITTED]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED",)


def test_root_stale_head_cancellation_during_confirmation_never_readmits(
    stage2_migrated_postgres_database,
) -> None:
    class BlockedConfirmationRepository(ConversationKernelRepository):
        calls = 0

        def accept_root_turn_intent(self, *args, **kwargs):
            self.calls += 1
            raise PreparedRootInputHeadStale("ROOT provider input head changed")

        def confirm_root_turn_intent(self, **kwargs):
            started.set()
            assert release.wait(5), "test did not release exact confirmation"
            return super().confirm_root_turn_intent(**kwargs)

    started, release = Event(), Event()
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = BlockedConfirmationRepository(provider)
    session_id, _, lease = _lease(repository)
    model = ScriptedKernelModel([])
    runner = _runner(repository, lease, model)

    async def scenario():
        from pulsara_agent.conversation_kernel.steer import build_direct_root_turn_identity
        command = _name("command")
        identity = build_direct_root_turn_identity(session_id, command)
        intent = ActiveTurnCancellationIntent(identity.turn_id, ModelInputScopeKind.ROOT)
        task = asyncio.create_task(runner.run_turn(
            frozen_test_prompt("cancel before canonical admission"),
            command_id=command, cancellation_intent=intent,
        ))
        assert await asyncio.to_thread(started.wait, 5)
        intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert repository.calls == 1
    assert model.requests == []
    with provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                             deadline_monotonic=monotonic() + 10) as connection:
        assert connection.execute("SELECT id FROM pulsara_v3.turns WHERE session_id=%s", (session_id,)).fetchall() == []



def test_round5_remote_identity_lost_ack_cannot_discard_exact_tool_result(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostRemoteIdentityAckRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    tool = _RemoteIdentityTool()
    model = ScriptedKernelModel(
        [
            _tool_stream(1),
            _text_stream("done", block_id="answer"),
        ]
    )

    result = asyncio.run(_runner(repository, lease, model, tool).run_turn(frozen_test_prompt("start")))

    assert result.final_text == "done"
    assert tool.invocations == 1
    assert repository.publication_calls == 1
    assert repository.confirmation_calls == 2
    assert repository.candidate is not None
    assert (
        repository.confirm_tool_remote_identity(
            lease.guard,
            candidate=repository.candidate,
            deadline_monotonic=monotonic() + 10,
        )
        is ToolRemoteIdentityConfirmationKind.FULL
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events "
            "WHERE session_id = %s "
            "AND event_type = 'ToolRemoteIdentityPublished'",
            (session_id,),
        ).fetchone() == (1,)


def test_round5_busy_steer_after_call_twenty_four_is_absorbed_as_a_suffix(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    command_id = _name("command")
    steer_command_id = _name("command")
    steer_queue_item_id = _name("queue")

    def enqueue(request: KernelModelExecutionRequest) -> None:
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=request.turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text('late steer'),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )

    streams = [_tool_stream(index) for index in range(63)]
    streams.append(_text_stream("after-steer", block_id="answer"))
    model = _EnqueueSteerAtCallModel(streams, at_call=25, enqueue=enqueue)
    deadlines = _RecordingDeadlineFactory()
    result = asyncio.run(
        _runner(repository, lease, model, deadline_factory=deadlines).run_turn(
            frozen_test_prompt("start"), command_id=command_id
        )
    )

    assert result.model_call_count == 64
    assert result.tool_call_count == 63
    # Sixty-four ordinary call owners plus the first ROOT prospective freeze,
    # optional Hook sibling, and activation owners.
    assert deadlines.owners.count(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING) == 67
    assert result.final_text == "after-steer"
    appended = model.requests[25].compiled_input.messages
    assert any(
        message.role is MessageRole.USER
        and message.content == (LLMTextPart("late steer"),)
        for message in appended
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue_item_id),
        ).fetchone() == ("CONSUMED",)


def test_round5_user_cancel_after_twenty_four_calls_interrupts_the_turn(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    calls_started: asyncio.Queue[int] = asyncio.Queue()

    async def stream(request: KernelModelExecutionRequest):
        calls_started.put_nowait(request.model_call_index)
        if request.model_call_index <= 24:
            for item in _tool_stream(request.model_call_index):
                yield item
            return
        await asyncio.Event().wait()

    model = CallbackScriptedKernelModel(stream)

    async def scenario() -> None:
        task = asyncio.create_task(_runner(repository, lease, model).run_turn(frozen_test_prompt("start")))
        try:
            # Exercise cancellation after 24 completed calls, without imposing
            # a total preparation-time cap on a slower or contended CI runner.
            # Each next call must still make progress within the same 10 seconds.
            for expected_call in range(1, 26):
                assert await asyncio.wait_for(calls_started.get(), timeout=10) == expected_call
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert len(model.requests) == 25
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED",)


@pytest.mark.parametrize(
    ("repository_type", "expected_calls", "should_open"),
    (
        (_LostRootAdmissionAckRepository, 1, True),
        (_RootNoneThenFullRepository, 2, True),
        (_RootConflictRepository, 1, False),
    ),
)
def test_round5_root_turn_admission_full_none_conflict_matrix(
    stage2_migrated_postgres_database,
    repository_type,
    expected_calls: int,
    should_open: bool,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = repository_type(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    model = ScriptedKernelModel([_text_stream("done", block_id="answer")])
    runner = _runner(repository, lease, model)

    if should_open:
        result = asyncio.run(runner.run_turn(frozen_test_prompt("start"), command_id=_name("command")))
        assert result.final_text == "done"
        assert len(model.requests) == 1
    else:
        with pytest.raises(Exception, match="conflicting winner"):
            asyncio.run(runner.run_turn(frozen_test_prompt("start"), command_id=_name("command")))
        assert model.requests == []
    assert repository.calls == expected_calls if hasattr(repository, "calls") else True


@pytest.mark.parametrize("after_unknown", (False, True))
def test_root_stale_head_reprepares_same_intent_without_rebuilding_prefix(
    stage2_migrated_postgres_database,
    after_unknown: bool,
) -> None:
    class StaleHeadRepository(ConversationKernelRepository):
        stale_attempts = []
        armed = False
        unknown_raised = False

        def accept_root_turn_intent(self, *args, **kwargs):
            if self.armed:
                self.stale_attempts.append(kwargs["intent"])
                if after_unknown and not self.unknown_raised:
                    self.unknown_raised = True
                    raise OSError("lost pre-commit acknowledgement")
                self.armed = False
                raise PreparedRootInputHeadStale("ROOT provider input head changed")
            return super().accept_root_turn_intent(*args, **kwargs)

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = StaleHeadRepository(provider)
    _, _, lease = _lease(repository)
    model = ScriptedKernelModel([
        _text_stream("first", block_id="first"),
        _text_stream("second", block_id="second"),
    ])
    runner = _runner(repository, lease, model)
    prepare = runner.prepare_prospective_root_input
    preparations = []

    async def tracked_prepare(*args, **kwargs):
        if preparations:
            assert not preparations[-1].owns_resources
        prepared = await prepare(*args, **kwargs)
        preparations.append(prepared)
        return prepared

    runner.prepare_prospective_root_input = tracked_prepare

    async def scenario():
        await runner.run_turn(frozen_test_prompt("first"))
        original = model.requests[0].compiled_input
        repository.armed = True
        command = _name("command")
        result = await runner.run_turn(frozen_test_prompt("second"), command_id=command)
        assert result.final_text == "second"
        assert len(preparations) == 3
        assert all(item.command_id == command for item in repository.stale_attempts)
        assert all(
            item is repository.stale_attempts[0] for item in repository.stale_attempts
        )
        assert preparations[1].admission.candidate.exact_turn_id == result.turn_id
        assert preparations[2].admission.candidate.exact_turn_id == result.turn_id
        current = model.requests[1].compiled_input
        assert current.system_prompt == original.system_prompt
        assert current.tools == original.tools
        assert current.messages[: len(original.messages)] == original.messages
        assert all(not item.owns_resources for item in preparations)

    asyncio.run(scenario())




@pytest.mark.parametrize("commit_before_block", (False, True))
def test_round5_cancelled_root_admission_never_reissues_and_full_is_interrupted(
    stage2_migrated_postgres_database,
    commit_before_block: bool,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _CancelledRootAdmissionRepository(
        provider,
        commit_before_block=commit_before_block,
    )
    session_id, _workspace_id, lease = _lease(repository)
    model = ScriptedKernelModel([_text_stream("must-not-open", block_id="answer")])
    runner = _runner(repository, lease, model)

    async def scenario() -> None:
        operation = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("start"), command_id=_name("command"))
        )
        assert await asyncio.to_thread(repository.started.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        repository.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(scenario())

    assert repository.calls == 1
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        rows = connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchall()
    assert rows == ([("INTERRUPTED",)] if commit_before_block else [])


def test_round5_cancelled_root_admission_joins_transient_confirmation_failures(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _FlakyCancelledRootConfirmationRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    model = ScriptedKernelModel([_text_stream("must-not-open", block_id="answer")])
    runner = _runner(repository, lease, model)

    async def scenario() -> None:
        operation = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("start"), command_id=_name("command"))
        )
        assert await asyncio.to_thread(repository.started.wait, 5)
        operation.cancel()
        repository.release.set()
        await asyncio.sleep(0.02)
        assert not operation.done()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(scenario())

    assert repository.calls == 1
    assert repository.confirmation_calls == 3
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED",)














@pytest.mark.parametrize(
    ("effect_class", "tool_name", "arguments"),
    (
        ("read_only", "read_file", "{}"),
        ("TERMINAL_OBSERVATION", "manage_process", '{"action":"poll"}'),
        ("admission", "read_file", "{}"),
    ),
)
def test_round5_observation_physical_exception_becomes_one_known_failure_result(
    stage2_migrated_postgres_database,
    effect_class: str,
    tool_name: str,
    arguments: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    tool = _PhysicalFailureTool(effect_class)
    model = ScriptedKernelModel(
        [
            _tool_stream(1, tool_name=tool_name, arguments=arguments),
            _text_stream("recovered", block_id="answer"),
        ]
    )

    result = asyncio.run(
        _runner(
            repository,
            lease,
            model,
            tool,
            tool_names=(tool_name,),
        ).run_turn(frozen_test_prompt("start"))
    )

    assert result.final_text == "recovered"
    assert tool.invocations == 1
    bodies = [
        json.loads(part.text)["pulsara_tool_result"]["body"]
        for message in model.requests[-1].compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT
        for part in message.content
        if isinstance(part, LLMTextPart)
    ]
    assert len(bodies) == 1
    assert "reliable result" in bodies[0]
    assert json.loads(bodies[0])["error"] == "OSError"
    assert json.loads(bodies[0])["message"] == "injected physical failure"
    assert "not started" not in bodies[0]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        rows = connection.execute(
            "SELECT result_state FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchall()
    assert rows == [("SYSTEM_ERROR",)]


@pytest.mark.parametrize(
    ("effect_class", "tool_name", "arguments"),
    (
        ("bounded_write", "write_file", "{}"),
        ("unknown_effect", "test_tool", "{}"),
        ("TERMINAL_EFFECT", "manage_process", '{"action":"write"}'),
    ),
)
@pytest.mark.parametrize("caller_cancelled", (False, True))
def test_effectful_tool_failure_preserves_unknown_result_and_continues(
    stage2_migrated_postgres_database,
    effect_class: str,
    tool_name: str,
    arguments: str,
    caller_cancelled: bool,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    tool = _PhysicalFailureTool(effect_class, caller_cancelled=caller_cancelled)
    model = ScriptedKernelModel(
        [
            _tool_stream(1, tool_name=tool_name, arguments=arguments),
            _text_stream("inspect before retrying", block_id="answer"),
        ]
    )
    runner = _runner(
        repository,
        lease,
        model,
        tool,
        tool_names=(tool_name,),
    )

    if caller_cancelled:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner.run_turn(frozen_test_prompt("start")))
        assert len(model.requests) == 1
    else:
        result = asyncio.run(runner.run_turn(frozen_test_prompt("start")))
        assert result.final_text == "inspect before retrying"
        bodies = [
            json.loads(part.text)["pulsara_tool_result"]["body"]
            for message in model.requests[-1].compiled_input.messages
            if message.role is MessageRole.TOOL_RESULT
            for part in message.content
            if isinstance(part, LLMTextPart)
        ]
        assert len(bodies) == 1
        body = json.loads(bodies[0])
        assert body["message"] == "injected physical failure"
        assert body["physical_outcome"] == "UNKNOWN"
        assert body["retry_performed"] is False

    assert tool.invocations == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        attempt_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
        result_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
        turn_status = connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert attempt_count == 1
    assert result_count == 1
    assert turn_status == ("INTERRUPTED" if caller_cancelled else "COMPLETED")


@pytest.mark.parametrize(
    "tool_name,arguments",
    (
        ("write_file", "{}"),
        ("manage_process", '{"action":"write"}'),
    ),
)
def test_write_tool_io_error_is_model_visible(
    stage2_migrated_postgres_database,
    tool_name: str,
    arguments: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    _, _, lease = _lease(repository)
    tool = _PhysicalFailureTool("bounded_write", raw=True)
    model = ScriptedKernelModel(
        [
            _tool_stream(1, tool_name=tool_name, arguments=arguments),
            _text_stream("can continue", block_id="answer"),
        ]
    )
    result = asyncio.run(
        _runner(repository, lease, model, tool, tool_names=(tool_name,)).run_turn(
            frozen_test_prompt("start")
        )
    )
    assert result.final_text == "can continue"
    assert tool.invocations == 1
    bodies = [
        json.loads(part.text)["pulsara_tool_result"]["body"]
        for message in model.requests[-1].compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT
        for part in message.content
        if isinstance(part, LLMTextPart)
    ]
    assert len(bodies) == 1
    assert json.loads(bodies[0])["message"] == "injected physical failure"
    assert json.loads(bodies[0])["physical_outcome"] == "UNKNOWN"


@pytest.mark.parametrize("raw", (False, True))
@pytest.mark.parametrize(
    "error_type",
    (
        StaleHostWriter,
        ConversationKernelConflict,
        KernelSessionIOClosed,
        OperationalError,
        MemoryError,
        asyncio.CancelledError,
    ),
)
def test_tool_failure_never_swallows_kernel_or_stop_boundaries(
    stage2_migrated_postgres_database,
    raw: bool,
    error_type: type[BaseException],
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _, lease = _lease(repository)
    model = ScriptedKernelModel([_tool_stream(1, tool_name="read_file")])
    tool = _PhysicalFailureTool(
        "read_only", error=error_type("boundary failure"), raw=raw
    )
    with pytest.raises(error_type):
        asyncio.run(
            _runner(repository, lease, model, tool, tool_names=("read_file",)).run_turn(
                frozen_test_prompt("start")
            )
        )
    assert len(model.requests) == 1
    assert tool.invocations == 1
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert not any(row["entry_kind"] == "TOOL_RESULT" for row in rows)


@pytest.mark.parametrize("after_write", (False, True))
def test_real_file_write_failure_can_be_inspected_without_replaying_write(
    stage2_migrated_postgres_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_write: bool,
) -> None:
    from pulsara_agent.tools.builtins import filesystem

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _, lease = _lease(repository)
    actual_create = filesystem._atomic_create_bytes
    calls = 0

    def failing_create(path, content):
        nonlocal calls
        calls += 1
        if after_write:
            actual_create(path, content)
            raise RuntimeError("post-write inspection failed")
        raise PermissionError("target filesystem denied creation")

    monkeypatch.setattr(filesystem, "_atomic_create_bytes", failing_create)
    model = ScriptedKernelModel(
        [
            _tool_stream(
                1,
                tool_name="write_file",
                arguments='{"target":{"kind":"PATH","path":"result.txt"},"content":"written before failure"}',
            ),
            _tool_stream(2, tool_name="read_file", arguments='{"target":{"kind":"PATH","path":"result.txt"}}'),
            _text_stream("actual file state checked", block_id="answer"),
        ]
    )

    async def exercise():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id='host:zvmka3c6ohpt4g7m4peilmratm',
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        seal_test_direct_tool_port(port)
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=port,
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
        )
        try:
            result = await runner.run_turn(
                frozen_test_prompt("create result.txt and inspect any failure")
            )
            assert result.final_text == "actual file state checked"
        finally:
            await port.aclose()

    asyncio.run(exercise())
    assert calls == 1
    assert (tmp_path / "result.txt").exists() is after_write
    bodies = [
        json.loads(part.text)["pulsara_tool_result"]["body"]
        for message in model.requests[-1].compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT
        for part in message.content
        if isinstance(part, LLMTextPart)
    ]
    assert len(bodies) == 2
    failure = json.loads(bodies[0])
    assert failure["physical_outcome"] == "UNKNOWN"
    assert failure["message"] == (
        "post-write inspection failed"
        if after_write
        else "target filesystem denied creation"
    )
    if after_write:
        assert "written before failure" in bodies[1]
    for before, after in zip(model.requests, model.requests[1:]):
        assert after.compiled_input.tools == before.compiled_input.tools
        assert after.compiled_input.system_prompt == before.compiled_input.system_prompt
        assert (
            after.compiled_input.messages[: len(before.compiled_input.messages)]
            == before.compiled_input.messages
        )


def test_round5_stale_writer_never_accepts_or_hands_off_late_tool_result(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, workspace_id, lease = _lease(repository)
    tool = _TakeoverDuringTool()
    runner = _runner(
        repository,
        lease,
        ScriptedKernelModel([_tool_stream(1, tool_name="read_file")]),
        tool,
        tool_names=("read_file",),
    )

    async def scenario() -> None:
        task = asyncio.create_task(runner.run_turn(frozen_test_prompt("start")))
        await asyncio.wait_for(tool.started.wait(), timeout=5)
        await asyncio.to_thread(
            repository.acquire_host_writer,
            intent="EXISTING",
            session_id=session_id,
            workspace_id=workspace_id,
            writer_owner_id=_name("host"),
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
        tool.release.set()
        with pytest.raises(StaleHostWriter):
            await task

    asyncio.run(scenario())

    assert tool.invocations == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        attempt_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
        result_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
        turn_status = connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert attempt_count == 1
    assert result_count == 0
    assert turn_status == "INTERRUPTED"
