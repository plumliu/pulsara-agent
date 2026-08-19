from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.direct_model import KernelModelExecutionRequest
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    StaleHostWriter,
    ToolRemoteIdentityConfirmationKind,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
    build_prepared_subagent_turn_admission,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
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
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import (
    CallbackScriptedKernelModel,
    ScriptedKernelModel,
    StaticContextSourceCollector,
    StructuredToolPort,
)


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


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

    def accept_root_turn(self, *args, **kwargs):
        self.calls += 1
        accepted = super().accept_root_turn(*args, **kwargs)
        if self.calls == 1:
            raise OSError("injected lost ROOT admission acknowledgement")
        return accepted


class _RootNoneThenFullRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = 0

    def accept_root_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise OSError("injected pre-commit ROOT failure")
        return super().accept_root_turn(*args, **kwargs)


class _RootConflictRepository(ConversationKernelRepository):
    def accept_root_turn(self, *args, **kwargs):
        del args, kwargs
        raise OSError("injected ROOT admission conflict window")

    def confirm_root_turn_admission(self, **kwargs):
        del kwargs
        return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)


class _CancelledRootAdmissionRepository(ConversationKernelRepository):
    def __init__(self, provider, *, commit_before_block: bool) -> None:
        super().__init__(provider)
        self.commit_before_block = commit_before_block
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def accept_root_turn(self, *args, **kwargs):
        self.calls += 1
        accepted = (
            super().accept_root_turn(*args, **kwargs)
            if self.commit_before_block
            else None
        )
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release ROOT admission")
        if accepted is None:
            raise OSError("injected pre-commit ROOT admission failure")
        return accepted


class _FlakyCancelledRootConfirmationRepository(
    _CancelledRootAdmissionRepository
):
    def __init__(self, provider) -> None:
        super().__init__(provider, commit_before_block=True)
        self.confirmation_calls = 0

    def confirm_root_turn_admission(self, **kwargs):
        self.confirmation_calls += 1
        if self.confirmation_calls <= 2:
            raise OSError("injected transient ROOT confirmation failure")
        return super().confirm_root_turn_admission(**kwargs)


class _LostSubagentAdmissionAckRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = 0

    def accept_subagent_turn(self, *args, **kwargs):
        self.calls += 1
        accepted = super().accept_subagent_turn(*args, **kwargs)
        if self.calls == 1:
            raise OSError("injected lost subagent admission acknowledgement")
        return accepted


class _FlakyLostSubagentAdmissionAckRepository(
    _LostSubagentAdmissionAckRepository
):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.confirmation_calls = 0

    def confirm_subagent_turn_admission(self, **kwargs):
        self.confirmation_calls += 1
        if self.confirmation_calls <= 2:
            raise OSError("injected transient subagent confirmation failure")
        return super().confirm_subagent_turn_admission(**kwargs)


class _SubagentNoneThenFullRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = 0

    def accept_subagent_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise OSError("injected pre-commit subagent failure")
        return super().accept_subagent_turn(*args, **kwargs)


class _SubagentConflictRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = 0

    def accept_subagent_turn(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        raise OSError("injected subagent admission conflict window")

    def confirm_subagent_turn_admission(self, **kwargs):
        del kwargs
        return TurnAdmissionConfirmation(TurnAdmissionConfirmationKind.CONFLICT)


class _CancelledSubagentAdmissionRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def accept_subagent_turn(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release subagent admission")
        raise OSError("injected pre-commit subagent admission failure")


class _PhysicalFailureTool(_KnownReadOnlyTool):
    def __init__(self, effect_class: str) -> None:
        super().__init__()
        self.effect_class = effect_class

    async def invoke(self, **_kwargs: object) -> KernelToolResult:
        self.invocations += 1
        raise KernelToolPhysicalInvocationError(
            effect_class=self.effect_class,
            error=OSError("injected physical failure"),
            timing="LATE_AFTER_WATCHDOG",
            caller_cancelled=False,
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
                token_fingerprint="sha256:" + "4" * 64,
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
    return ConversationKernelRunner(
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
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    return session_id, workspace_id, lease


def _prepare_subagent_task(repository, lease) -> str:
    parent_turn_id = _name("parent-turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("parent-command"),
        turn_id=parent_turn_id,
        entry_id=_name("parent-entry"),
        context_binding_revision_id=_name("parent-revision"),
        permission_snapshot_id=_name("parent-permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(b"delegate"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    task_id = _name("subagent-task")
    repository.accept_subagent_task(
        lease.guard,
        task_id=task_id,
        parent_turn_id=parent_turn_id,
        objective="answer once",
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    repository.set_subagent_task_status(
        lease.guard,
        task_id=task_id,
        status="ACTIVE",
        reason=None,
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
        deadline_monotonic=monotonic() + 30,
    )
    return task_id


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

    result = asyncio.run(_runner(repository, lease, model, tool).run_turn("start"))

    assert result.model_call_count == 64
    assert result.tool_call_count == 63
    assert result.final_text == "final-64"
    assert tool.invocations == 63
    assert len(model.requests) == 64
    for previous, current in zip(model.requests, model.requests[1:], strict=False):
        assert current.compiled_input.system_prompt == previous.compiled_input.system_prompt
        assert current.compiled_input.tools == previous.compiled_input.tools
        assert current.compiled_input.messages[: len(previous.compiled_input.messages)] == (
            previous.compiled_input.messages
        )


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
        ).run_turn("start")
    )

    assert result.final_text == "done"
    assert deadlines.owners.count(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING) == 1
    assert deadlines.owners.count(KernelWatchdogOwner.FOREGROUND_CANONICAL) >= 4


def test_round5_more_than_sixty_four_tool_calls_in_one_turn_finalize(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    tool_batch: list[object] = []
    for index in range(65):
        tool_batch.extend(_tool_stream(index))
    model = ScriptedKernelModel(
        [tool_batch, _text_stream("after-65-tools", block_id="final:tools")]
    )
    tool = _KnownReadOnlyTool()

    result = asyncio.run(_runner(repository, lease, model, tool).run_turn("start"))

    assert result.model_call_count == 2
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
        ).run_turn("start")
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
            _runner(repository, lease, model, tool).run_turn("start")
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

    result = asyncio.run(_runner(repository, lease, model, tool).run_turn("start"))

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
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")

    def enqueue(request: KernelModelExecutionRequest) -> None:
        repository.enqueue_prompt(
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=request.turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            content=InlineContent.from_bytes(b"late steer"),
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
            "start", command_id=command_id
        )
    )

    assert result.model_call_count == 64
    assert result.tool_call_count == 63
    assert (
        deadlines.owners.count(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING)
        == 64
    )
    assert result.final_text == "after-steer"
    appended = model.requests[25].compiled_input.messages
    assert any(
        message.role is MessageRole.USER
        and message.content == ("late steer",)
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
    blocked = asyncio.Event()

    async def stream(request: KernelModelExecutionRequest):
        if request.model_call_index <= 24:
            for item in _tool_stream(request.model_call_index):
                yield item
            return
        blocked.set()
        await asyncio.Event().wait()

    model = CallbackScriptedKernelModel(stream)

    async def scenario() -> None:
        task = asyncio.create_task(_runner(repository, lease, model).run_turn("start"))
        await asyncio.wait_for(blocked.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

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
        result = asyncio.run(runner.run_turn("start", command_id=_name("command")))
        assert result.final_text == "done"
        assert len(model.requests) == 1
    else:
        with pytest.raises(Exception, match="conflicting winner"):
            asyncio.run(runner.run_turn("start", command_id=_name("command")))
        assert model.requests == []
    assert repository.calls == expected_calls if hasattr(repository, "calls") else True


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
            runner.run_turn("start", command_id=_name("cancelled-command"))
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
            runner.run_turn("start", command_id=_name("cancelled-command"))
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


def test_round5_subagent_turn_lost_ack_confirms_exact_winner_once(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostSubagentAdmissionAckRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    task_id = _prepare_subagent_task(repository, lease)
    streams = [_tool_stream(index) for index in range(63)]
    streams.append(_text_stream("child", block_id="child-answer"))
    model = ScriptedKernelModel(streams)

    result = asyncio.run(
        _runner(repository, lease, model).run_subagent_turn(
            task_id=task_id,
            objective="answer once",
        )
    )

    assert result.final_text == "child"
    assert result.model_call_count == 64
    assert result.tool_call_count == 63
    assert repository.calls == 1
    assert len(model.requests) == 64
    assert session_id == lease.guard.session_id


def test_round5_subagent_lost_ack_joins_transient_confirmation_failures(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _FlakyLostSubagentAdmissionAckRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    task_id = _prepare_subagent_task(repository, lease)
    model = ScriptedKernelModel([_text_stream("child", block_id="child-answer")])

    result = asyncio.run(
        _runner(repository, lease, model).run_subagent_turn(
            task_id=task_id,
            objective="answer once",
        )
    )

    assert result.final_text == "child"
    assert repository.calls == 1
    assert repository.confirmation_calls == 3
    assert len(model.requests) == 1


def test_round5_subagent_admission_exact_joins_immutable_objective(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    task_id = _prepare_subagent_task(repository, lease)
    occurred_at = datetime.now(timezone.utc)
    mismatched = build_prepared_subagent_turn_admission(
        session_id=session_id,
        task_id=task_id,
        turn_id=_name("mismatched-turn"),
        entry_id=_name("mismatched-entry"),
        context_binding_revision_id=_name("mismatched-revision"),
        permission_snapshot_id=_name("mismatched-permission"),
        content=InlineContent.from_bytes(b"different objective"),
        occurred_at=occurred_at,
        actor_id="subagent-manager",
    )
    assert (
        repository.confirm_subagent_turn_admission(
            candidate=mismatched,
            guard=lease.guard,
            deadline_monotonic=monotonic() + 10,
        ).kind
        is TurnAdmissionConfirmationKind.CONFLICT
    )
    with pytest.raises(Exception, match="immutable objective"):
        repository.accept_subagent_turn(
            lease.guard,
            candidate=mismatched,
            deadline_monotonic=monotonic() + 10,
        )

    accepted_candidate = build_prepared_subagent_turn_admission(
        session_id=session_id,
        task_id=task_id,
        turn_id=_name("accepted-turn"),
        entry_id=_name("accepted-entry"),
        context_binding_revision_id=_name("accepted-revision"),
        permission_snapshot_id=_name("accepted-permission"),
        content=InlineContent.from_bytes(b"answer once"),
        occurred_at=datetime.now(timezone.utc),
        actor_id="subagent-manager",
    )
    repository.accept_subagent_turn(
        lease.guard,
        candidate=accepted_candidate,
        deadline_monotonic=monotonic() + 10,
    )
    assert (
        repository.confirm_subagent_turn_admission(
            candidate=accepted_candidate,
            guard=lease.guard,
            deadline_monotonic=monotonic() + 10,
        ).kind
        is TurnAdmissionConfirmationKind.FULL
    )
    with provider.connection(
        lane=PostgresConnectionLane.HOST_CONTROL,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        connection.execute(
            "UPDATE pulsara_v3.subagent_tasks SET objective = %s "
            "WHERE session_id = %s AND id = %s",
            ("corrupted objective", session_id, task_id),
        )
    assert (
        repository.confirm_subagent_turn_admission(
            candidate=accepted_candidate,
            guard=lease.guard,
            deadline_monotonic=monotonic() + 10,
        ).kind
        is TurnAdmissionConfirmationKind.CONFLICT
    )


@pytest.mark.parametrize(
    ("repository_type", "expected_calls", "should_open"),
    (
        (_SubagentNoneThenFullRepository, 2, True),
        (_SubagentConflictRepository, 1, False),
    ),
)
def test_round5_subagent_turn_admission_none_conflict_matrix(
    stage2_migrated_postgres_database,
    repository_type,
    expected_calls: int,
    should_open: bool,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = repository_type(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    task_id = _prepare_subagent_task(repository, lease)
    model = ScriptedKernelModel([_text_stream("child", block_id="child-answer")])
    runner = _runner(repository, lease, model)

    if should_open:
        result = asyncio.run(
            runner.run_subagent_turn(task_id=task_id, objective="answer once")
        )
        assert result.final_text == "child"
        assert len(model.requests) == 1
    else:
        with pytest.raises(Exception, match="conflicting winner"):
            asyncio.run(
                runner.run_subagent_turn(task_id=task_id, objective="answer once")
            )
        assert model.requests == []
    assert repository.calls == expected_calls


def test_round5_cancelled_subagent_admission_none_never_reissues(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _CancelledSubagentAdmissionRepository(provider)
    _session_id, _workspace_id, lease = _lease(repository)
    task_id = _prepare_subagent_task(repository, lease)
    model = ScriptedKernelModel([_text_stream("must-not-open", block_id="answer")])
    runner = _runner(repository, lease, model)

    async def scenario() -> None:
        operation = asyncio.create_task(
            runner.run_subagent_turn(task_id=task_id, objective="answer once")
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


@pytest.mark.parametrize(
    ("effect_class", "tool_name", "arguments"),
    (
        ("read_only", "read_file", "{}"),
        ("TERMINAL_OBSERVATION", "terminal_process", '{"action":"poll"}'),
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
        ).run_turn("start")
    )

    assert result.final_text == "recovered"
    assert tool.invocations == 1
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
        ("TERMINAL_EFFECT", "terminal_process", '{"action":"write"}'),
    ),
)
def test_round5_effectful_physical_exception_keeps_attempt_without_result(
    stage2_migrated_postgres_database,
    effect_class: str,
    tool_name: str,
    arguments: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, _workspace_id, lease = _lease(repository)
    tool = _PhysicalFailureTool(effect_class)
    runner = _runner(
        repository,
        lease,
        ScriptedKernelModel(
            [_tool_stream(1, tool_name=tool_name, arguments=arguments)]
        ),
        tool,
        tool_names=(tool_name,),
    )

    with pytest.raises(KernelToolPhysicalInvocationError):
        asyncio.run(runner.run_turn("start"))

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
        task = asyncio.create_task(runner.run_turn("start"))
        await asyncio.wait_for(tool.started.wait(), timeout=5)
        await asyncio.to_thread(
            repository.acquire_host_writer,
            session_id=session_id,
            workspace_id=workspace_id,
            writer_owner_id=_name("replacement-host"),
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
