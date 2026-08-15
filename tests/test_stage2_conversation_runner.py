from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest

from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveObservationKind,
    LiveSettlementKind,
)
from pulsara_agent.conversation_kernel.context_sources import (
    build_memory_context_source,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    AutomaticMemoryTriggerDisposition,
    FrozenMemoryTriggerPolicy,
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.memory.reflection import (
    MemoryWriteOptOut,
    TurnMemoryUseOptOut,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolResult,
    _stable_id,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    PostgresToolArtifactReadPort,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import (
    ScriptedKernelModel,
    StaticContextSourceCollector,
    StructuredToolPort,
)


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class _ScriptedModel(ScriptedKernelModel):
    pass


class _ChangingRegistryCollector(StaticContextSourceCollector):
    def __init__(self) -> None:
        self._reads = 0

    @property
    def registry_fingerprint(self) -> str:
        self._reads += 1
        if self._reads == 1:
            return super().registry_fingerprint
        return "sha256:" + ("0" * 64)


class _FailingOperationalExtension:
    def offer_operational_nowait(self, _offer) -> None:
        raise RuntimeError("injected best-effort observer failure")


class _RevocableStructuredToolPort(StructuredToolPort):
    def __init__(self, delegate: object, *, tool_names: tuple[str, ...] = ()) -> None:
        super().__init__(delegate, tool_names=tool_names)
        self.revoked = False

    def borrow_tool_surface(self, prepared):
        if self.revoked:
            raise RuntimeError("injected tool surface revocation")
        borrow = super().borrow_tool_surface(prepared)
        original_validate = borrow._validate

        def validate(current, tool_name):
            if self.revoked:
                raise RuntimeError("injected tool surface revocation")
            return original_validate(current, tool_name)

        borrow._validate = validate
        return borrow


class _SurfaceRevokingCollector(StaticContextSourceCollector):
    def __init__(self, tools: _RevocableStructuredToolPort) -> None:
        self._tools = tools

    def complete_frozen_sources(self, frozen, **kwargs):
        result = super().complete_frozen_sources(frozen, **kwargs)
        self._tools.revoked = True
        return result


class _MeasuredRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.host_write_transactions = 0

    def _writer_transaction(self, guard, *, deadline_monotonic):
        self.host_write_transactions += 1
        return super()._writer_transaction(guard, deadline_monotonic=deadline_monotonic)


class _LostAssistantAckRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self._lost_once = False

    def commit_assistant_message(self, *args, **kwargs):
        accepted = super().commit_assistant_message(*args, **kwargs)
        if not self._lost_once:
            self._lost_once = True
            raise OSError("injected lost assistant commit acknowledgement")
        return accepted


class _CancellingFirstSteerRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.cancelled_once = False

    def consume_prepared_prompt_steer(self, guard, *, candidate, deadline_monotonic):
        if not self.cancelled_once:
            self.cancelled_once = True
            self.cancel_prompt(
                guard,
                queue_item_id=candidate.queue_item_id,
                occurred_at=datetime.now(timezone.utc),
                actor_id="test:concurrent-cancel",
                deadline_monotonic=deadline_monotonic,
            )
        return super().consume_prepared_prompt_steer(
            guard,
            candidate=candidate,
            deadline_monotonic=deadline_monotonic,
        )


class _ExpiredSteerCompiler(StructuredModelInputCompiler):
    def compile_append(self, request, **kwargs):
        if len(request.canonical_input.items) > 1:
            kwargs["deadline_monotonic"] = monotonic() - 1
        return super().compile_append(request, **kwargs)


class _OnlyOneSteerCompiler(StructuredModelInputCompiler):
    def __init__(self) -> None:
        super().__init__()
        self.failures: list[tuple[int, ModelInputCompileFailureKind]] = []

    def compile_append(self, request, **kwargs):
        steer_count = sum(
            item.input_origin is not None
            and item.input_origin.value == "HUMAN_STEER"
            for item in request.canonical_input.items
        )
        if steer_count > 1:
            self.failures.append(
                (
                    steer_count,
                    ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
                )
            )
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
            )
        try:
            return super().compile_append(request, **kwargs)
        except StructuredModelInputCompileError as exc:
            self.failures.append((steer_count, exc.kind))
            raise


class _AssertingTool:
    def __init__(self, provider, session_id: str) -> None:
        self._provider = provider
        self._session_id = session_id
        self.invocations: list[str] = []

    async def authorize(
        self, *, tool_name, arguments, tool_call_id, turn_id, assistant_entry_id
    ):
        del turn_id, assistant_entry_id
        del tool_name, arguments, tool_call_id
        return KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW, "test-policy")

    async def request_confirmation(self, **kwargs):
        del kwargs
        raise AssertionError("test policy never requests human confirmation")

    async def invoke(
        self,
        *,
        tool_name,
        arguments,
        tool_call_id,
        attempt_id,
        turn_id,
        assistant_entry_id,
        invocation_context,
        live_sink=None,
    ):
        del turn_id, assistant_entry_id, invocation_context, live_sink
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 10,
        ) as connection:
            row = connection.execute(
                """
                SELECT a.id, b.tool_name
                FROM pulsara_v3.tool_execution_attempts a
                JOIN pulsara_v3.assistant_message_blocks b
                  ON b.session_id = a.session_id
                 AND b.assistant_entry_id = a.assistant_entry_id
                 AND b.tool_call_id = a.tool_call_id
                WHERE a.session_id = %s AND a.id = %s
                """,
                (self._session_id, attempt_id),
            ).fetchone()
        assert row == (attempt_id, tool_name)
        assert tool_call_id
        self.invocations.append(attempt_id)
        return KernelToolResult(state="SUCCESS", content=b"tool-ok")


class _DenyingTool(_AssertingTool):
    async def authorize(
        self, *, tool_name, arguments, tool_call_id, turn_id, assistant_entry_id
    ):
        del turn_id, assistant_entry_id
        del tool_name, arguments, tool_call_id
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.PERMISSION_DENIED,
            "test-policy:deny",
            "denied before dispatch",
        )


class _ConfirmationWithoutControllerTool(_AssertingTool):
    async def authorize(self, **kwargs):
        del kwargs
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
            "test-policy:require-confirmation",
            "confirmation required",
        )

    async def request_confirmation(self, **kwargs):
        del kwargs
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.PERMISSION_DENIED,
            "interaction:no-controller",
            "no controller is attached",
        )


class _BlockingTool(_AssertingTool):
    def __init__(self, provider, session_id: str) -> None:
        super().__init__(provider, session_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().invoke(**kwargs)


class _LargeTool(_AssertingTool):
    async def invoke(self, **kwargs):
        await super().invoke(**kwargs)
        return KernelToolResult(state="SUCCESS", content=b"z" * (70 << 10))


class _BlockingSourceCollector(StaticContextSourceCollector):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.freeze_calls = 0
        self.complete_calls = 0

    def freeze_non_trigger_sources(self, **kwargs):
        self.freeze_calls += 1
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test source capture was not released")
        return super().freeze_non_trigger_sources(**kwargs)

    def complete_frozen_sources(self, frozen, **kwargs):
        self.complete_calls += 1
        return super().complete_frozen_sources(frozen, **kwargs)


class _PolicyMemoryProjection:
    def __init__(self) -> None:
        self._write = MemoryWriteOptOut()
        self._all = TurnMemoryUseOptOut()
        self.preference_calls = 0
        self.recall_calls = 0

    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy:
        if self._all.excludes(text):
            return FrozenMemoryTriggerPolicy(
                AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE,
                MemoryUsePolicy.ALL_DISABLED_BY_USER,
            )
        return FrozenMemoryTriggerPolicy(
            (
                AutomaticMemoryTriggerDisposition.SKIPPED_LOW_INFORMATION
                if len(" ".join(text.split())) < 8
                else AutomaticMemoryTriggerDisposition.ELIGIBLE
            ),
            (
                MemoryUsePolicy.WRITE_DISABLED_BY_USER
                if self._write.excludes(text)
                else MemoryUsePolicy.ENABLED
            ),
        )

    def classify_automatic_trigger(
        self, text: str
    ) -> AutomaticMemoryTriggerDisposition:
        return self.classify_memory_trigger(text).automatic_recall

    async def freeze_response_preference_source(self):
        self.preference_calls += 1
        return build_memory_context_source(
            kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
            texts=(' {"items":[]} ',),
        )

    async def freeze_automatic_recall_source(self, _query: str):
        self.recall_calls += 1
        return build_memory_context_source(
            kind=ContextSourceKind.MEMORY_RECALL,
            texts=(
                '{"items":[]}',
                '{"items":[]}',
                '{"items":[]}',
            ),
        )

    def offer_candidate_wake(self, _candidate_id: str) -> None:
        return None

    def prepare_and_adopt_reflection(self, **_kwargs: object) -> None:
        return None


class _DelayedPreparedExecution:
    def __init__(self, delegate, started: asyncio.Event, release: asyncio.Event) -> None:
        self._delegate = delegate
        self._started = started
        self._release = release
        self.execution_fingerprint = delegate.execution_fingerprint

    def discard(self) -> None:
        self._delegate.discard()

    async def open_once(self, permit):
        self._started.set()
        await self._release.wait()
        async for item in self._delegate.open_once(permit):
            yield item


class _BlockingFirstCallModel(_ScriptedModel):
    def __init__(self, calls: list[list[object]]) -> None:
        super().__init__(calls)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def preflight_execution(
        self,
        request,
        *,
        expected_append_candidate_fingerprint,
        install_authority,
    ):
        prepared = super().preflight_execution(
            request,
            expected_append_candidate_fingerprint=(
                expected_append_candidate_fingerprint
            ),
            install_authority=install_authority,
        )
        if len(self.requests) == 1:
            return _DelayedPreparedExecution(prepared, self.started, self.release)
        return prepared


class _FailingPostConsumptionReader:
    def __init__(self, delegate: CanonicalProviderInputReader) -> None:
        self._delegate = delegate
        self.calls = 0

    def read_frozen_compile_snapshot(self, cut, *, deadline_monotonic):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("injected post-consumption canonical mismatch")
        return self._delegate.read_frozen_compile_snapshot(
            cut, deadline_monotonic=deadline_monotonic
        )


def _text_stream(text: str, *, block: str = "text:1") -> list[object]:
    return [
        TextStartPayload(block),
        TextDeltaPayload(block, text),
        TextEndPayload(block, text, len(text.encode("utf-8")), live_digest(text)),
    ]


def _tool_stream() -> list[object]:
    arguments = '{"command":"true"}'
    return [
        ToolCallStartPayload("call:1", "call:1", "terminal"),
        ToolCallDeltaPayload("call:1", "call:1", arguments),
        ToolCallEndPayload(
            block_identity="call:1",
            tool_call_id="call:1",
            tool_name="terminal",
            arguments_json=arguments,
            utf8_bytes=len(arguments.encode("utf-8")),
            digest=live_digest(arguments),
        ),
    ]


def test_stage2_runner_text_turn_has_two_entry_transactions_and_no_segments(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _MeasuredRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("answer")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = asyncio.run(runner.run_turn("question"))
    assert result.final_text == "answer"
    assert result.model_call_count == 1
    assert repository.host_write_transactions == 2
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
    ]
    assert not any("segment" in key for row in rows for key in row)
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    assert tuple(row["event_type"] for row in events) == (
        "UserMessageAccepted",
        "AssistantMessageAccepted",
        "TurnCompleted",
    )


def test_round3_1_empty_epoch_absorbs_pre_first_call_steers_once(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _ScriptedModel([_text_stream("one call")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
    )

    async def exercise():
        task = asyncio.create_task(runner.run_turn("initial", command_id=command_id))
        assert await asyncio.to_thread(collector.started.wait, 5)
        for index, text in enumerate(("steer one", "steer two"), start=1):
            steer_command = _name(f"steer-command-{index}")
            repository.enqueue_prompt(
                lease.guard,
                command_id=steer_command,
                queue_item_id=_name(f"steer-queue-{index}"),
                client_submission_id=steer_command,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                content=InlineContent.from_bytes(text.encode("utf-8")),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
        collector.release.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "one call"
    assert len(model.requests) == 1
    assert collector.freeze_calls == 1
    assert collector.complete_calls == 1
    user_messages = [
        message.content[0]
        for message in model.requests[0].compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" not in message.content[0]
    ]
    assert user_messages == ["initial", "steer one", "steer two"]


def test_round8_memory_policy_aggregates_steers_and_resets_on_next_root_message(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    collector = _BlockingSourceCollector()
    projection = _PolicyMemoryProjection()
    model = _ScriptedModel(
        [
            _text_stream("first answer"),
            _text_stream("second answer"),
            _text_stream("third answer"),
        ]
    )
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(
            _AssertingTool(provider, session_id), tool_names=()
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        memory_projection=projection,
    )

    async def exercise() -> None:
        first = asyncio.create_task(runner.run_turn("normal initial root message"))
        assert await asyncio.to_thread(collector.started.wait, 5)
        collector.release.set()
        await first
        assert model.requests[0].memory_context.memory_use_policy is (
            MemoryUsePolicy.ENABLED
        )
        assert projection.preference_calls == 1
        assert projection.recall_calls == 1

        collector.started.clear()
        collector.release.clear()
        second_command = _name("command")
        second_turn = _stable_id("turn", session_id, second_command)
        second = asyncio.create_task(
            runner.run_turn(
                "don't use saved memory for this answer",
                command_id=second_command,
            )
        )
        assert await asyncio.to_thread(collector.started.wait, 5)
        steer_command = _name("steer-command")
        repository.enqueue_prompt(
            lease.guard,
            command_id=steer_command,
            queue_item_id=_name("steer-queue"),
            client_submission_id=steer_command,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=second_turn,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            content=InlineContent.from_bytes(b"continue normally"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        collector.release.set()
        await second
        assert model.requests[1].memory_context.memory_use_policy is (
            MemoryUsePolicy.ALL_DISABLED_BY_USER
        )
        assert projection.preference_calls == 1
        assert projection.recall_calls == 1

        await runner.run_turn("normal next root message")
        assert model.requests[2].memory_context.memory_use_policy is (
            MemoryUsePolicy.ENABLED
        )
        assert projection.preference_calls == 2
        assert projection.recall_calls == 2

    asyncio.run(exercise())
    first_input, second_input, third_input = (
        request.compiled_input for request in model.requests
    )
    assert first_input.system_prompt == second_input.system_prompt
    assert second_input.system_prompt == third_input.system_prompt
    assert first_input.tools == second_input.tools
    assert second_input.tools == third_input.tools
    assert second_input.messages[: len(first_input.messages)] == first_input.messages
    assert third_input.messages[: len(second_input.messages)] == second_input.messages
    second_suffix_observations = tuple(
        decode_runtime_observation(message)
        for message in second_input.messages[len(first_input.messages) :]
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in message.content[0]
    )
    cleared_memory_sources = {
        item.source_kind
        for item in second_suffix_observations
        if item.presence.value == "CLEARED"
    }
    assert {
        ContextSourceKind.MEMORY_RECALL,
        ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
    } <= cleared_memory_sources


def test_round3_1_planning_reaches_shorter_fifo_prefix_without_recharging_base(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _BlockingFirstCallModel([_text_stream("one call")])
    compiler = _OnlyOneSteerCompiler()
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compiler=compiler,
    )
    # Nested prefixes share the same 64 KiB canonical base.  The injected
    # target admits only one steer.  A 512 KiB planning bound admits the unique
    # base + suffix materialization, while charging the base for each of the
    # sixteen trials would fail before reaching the valid one-item prefix.
    initial = "x" * (64 << 10)
    body = b"12345678"
    queue_ids = tuple(_name(f"steer-queue-{index}") for index in range(1, 17))

    async def exercise():
        task = asyncio.create_task(runner.run_turn(initial, command_id=command_id))
        assert await asyncio.to_thread(collector.started.wait, 5)
        for index, queue_id in enumerate(queue_ids, start=1):
            steer_command = _name(f"steer-command-{index}")
            repository.enqueue_prompt(
                lease.guard,
                command_id=steer_command,
                queue_item_id=queue_id,
                client_submission_id=steer_command,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                content=InlineContent.from_bytes(body),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.runner.MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES",
            512 << 10,
        )
        collector.release.set()
        await asyncio.wait_for(model.started.wait(), timeout=5)
        user_messages = [
            message.content[0]
            for message in model.requests[0].compiled_input.messages
            if message.role is MessageRole.USER
            and message.content
            and "pulsara_runtime_observation" not in message.content[0]
        ]
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 10,
        ) as connection:
            rows = connection.execute(
                "SELECT id, status FROM pulsara_v3.prompt_queue_items "
                "WHERE session_id = %s AND id = ANY(%s) ORDER BY queue_sequence",
                (session_id, list(queue_ids)),
            ).fetchall()
        task.cancel()
        model.release.set()
        await asyncio.gather(task, return_exceptions=True)
        return user_messages, rows

    try:
        user_messages, rows = asyncio.run(exercise())
    except StructuredModelInputCompileError as exc:
        pytest.fail(f"shortest prefix was rejected: {exc.kind}; {compiler.failures}")
    assert user_messages == [initial, body.decode("utf-8")]
    assert rows[0] == (queue_ids[0], "CONSUMED")
    assert len(rows) == len(queue_ids)
    assert all(status == "PENDING" for _queue_id, status in rows[1:])


def test_round3_1_expired_steer_planning_consumes_nothing_and_io_closes(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    steer_command = _name("steer-command")
    steer_queue = _name("steer-queue")
    collector = _BlockingSourceCollector()
    io_owner = KernelSessionIO()
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compiler=_ExpiredSteerCompiler(),
        io_owner=io_owner,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run_turn("initial", command_id=command_id))
        assert await asyncio.to_thread(collector.started.wait, 5)
        repository.enqueue_prompt(
            lease.guard,
            command_id=steer_command,
            queue_item_id=steer_queue,
            client_submission_id=steer_command,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            content=InlineContent.from_bytes(b"must remain pending"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        collector.release.set()
        with pytest.raises(StructuredModelInputCompileError) as failure:
            await task
        assert failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED
        await io_owner.aclose(deadline_monotonic=monotonic() + 1)

    asyncio.run(exercise())
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status, consumed_entry_id FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue),
        ).fetchone() == ("PENDING", None)
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
            (session_id, turn_id),
        ).fetchone() == ("INTERRUPTED",)


def test_round3_1_future_lane_does_not_block_active_steer_batch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    future_command_id = _name("future-command")
    future_queue_item_id = _name("future-queue")
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _ScriptedModel([_text_stream("one call")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
    )

    async def exercise():
        task = asyncio.create_task(runner.run_turn("initial", command_id=command_id))
        assert await asyncio.to_thread(collector.started.wait, 5)
        repository.enqueue_prompt(
            lease.guard,
            command_id=future_command_id,
            queue_item_id=future_queue_item_id,
            client_submission_id=future_command_id,
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            permission_snapshot_id="permission:future",
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            content=InlineContent.from_bytes(b"future"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        repository.enqueue_prompt(
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            content=InlineContent.from_bytes(b"steer now"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        collector.release.set()
        return await task

    asyncio.run(exercise())
    assert collector.freeze_calls == 1
    assert collector.complete_calls == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        rows = connection.execute(
            "SELECT id, status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s ORDER BY queue_sequence",
            (session_id,),
        ).fetchall()
    assert rows == [
        (future_queue_item_id, "PENDING"),
        (steer_queue_item_id, "CONSUMED"),
    ]


def test_round3_1_installed_epoch_absorbs_two_steers_in_one_followup_call(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    model = _BlockingFirstCallModel(
        [_text_stream("first answer"), _text_stream("final answer", block="text:2")]
    )
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise():
        task = asyncio.create_task(runner.run_turn("initial", command_id=command_id))
        await asyncio.wait_for(model.started.wait(), timeout=5)
        for index, text in enumerate(("steer one", "steer two"), start=1):
            steer_command = _name(f"command-steer-{index}")
            steer_queue = _name(f"queue-steer-{index}")
            repository.enqueue_prompt(
                lease.guard,
                command_id=steer_command,
                queue_item_id=steer_queue,
                client_submission_id=steer_command,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                content=InlineContent.from_bytes(text.encode("utf-8")),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
        model.release.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "final answer"
    assert len(model.requests) == 2
    first = model.requests[0].compiled_input
    second = model.requests[1].compiled_input
    assert second.system_prompt == first.system_prompt
    assert second.tools == first.tools
    assert second.messages[: len(first.messages)] == first.messages
    appended_users = [
        message.content[0]
        for message in second.messages[len(first.messages) :]
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" not in message.content[0]
    ]
    assert appended_users == ["steer one", "steer two"]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s ORDER BY queue_sequence",
            (session_id,),
        ).fetchall() == [("CONSUMED",), ("CONSUMED",)]


def test_round3_1_post_consumption_read_failure_interrupts_without_open_or_recompile(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    reader = _FailingPostConsumptionReader(CanonicalProviderInputReader(provider))
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        input_reader=reader,
        context_source_collector=collector,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run_turn("initial", command_id=command_id))
        assert await asyncio.to_thread(collector.started.wait, 5)
        repository.enqueue_prompt(
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            content=InlineContent.from_bytes(b"accepted then mismatched"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        collector.release.set()
        with pytest.raises(
            RuntimeError, match="injected post-consumption canonical mismatch"
        ):
            await task

    asyncio.run(exercise())
    assert reader.calls == 2
    assert collector.freeze_calls == 1
    assert collector.complete_calls == 1
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue_item_id),
        ).fetchone() == ("CONSUMED",)
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.turns "
            "WHERE session_id = %s AND id = %s",
            (session_id, turn_id),
        ).fetchone() == ("INTERRUPTED", "PROVIDER_INPUT_PLAN_CONFLICT")


def test_round3_1_pre_consumption_stale_plan_discards_and_replans_without_steer_entry(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _CancellingFirstSteerRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _ScriptedModel([_text_stream("initial only")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
    )

    async def exercise():
        task = asyncio.create_task(runner.run_turn("initial", command_id=command_id))
        assert await asyncio.to_thread(collector.started.wait, 5)
        repository.enqueue_prompt(
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            content=InlineContent.from_bytes(b"cancel before consume"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        collector.release.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "initial only"
    assert repository.cancelled_once
    assert len(model.requests) == 1
    assert [
        message.content[0]
        for message in model.requests[0].compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" not in message.content[0]
    ] == ["initial"]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status, consumed_entry_id FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue_item_id),
        ).fetchone() == ("CANCELLED", None)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'USER_STEER'",
            (session_id,),
        ).fetchone() == (0,)


def test_round3_compile_failure_interrupts_after_user_acceptance_with_zero_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=4,
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(runner.run_turn("accepted before compile"))
    assert failure.value.kind is (
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
    )
    assert model.preparation_requests
    assert model.requests == []
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert [row["entry_kind"] for row in rows] == ["USER_MESSAGE"]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        status = connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert status == "INTERRUPTED"


def test_round3_compile_failure_observer_cannot_block_turn_interruption(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=4,
        extensions=_FailingOperationalExtension(),  # type: ignore[arg-type]
    )
    with pytest.raises(StructuredModelInputCompileError):
        asyncio.run(runner.run_turn("accepted before compile"))
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        status = connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert status == "INTERRUPTED"


def test_round3_source_registry_drift_interrupts_with_zero_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=_ChangingRegistryCollector(),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(runner.run_turn("accepted before registry drift"))
    assert failure.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
    assert model.requests == []


def test_round3_surface_revoked_before_borrow_has_zero_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    tools = _RevocableStructuredToolPort(
        _AssertingTool(provider, session_id), tool_names=("test_tool",)
    )
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=LiveAgentEventBus(),
        context_source_collector=_SurfaceRevokingCollector(tools),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(runner.run_turn("accepted before surface revocation"))
    assert failure.value.kind is ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
    assert model.requests == []


def test_stage2_runner_commits_tool_message_and_attempt_before_invoke(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _MeasuredRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")])
    tool = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = asyncio.run(runner.run_turn("run it"))
    assert result.final_text == "done"
    assert result.tool_call_count == 1
    assert len(tool.invocations) == 1
    assert repository.host_write_transactions == 5
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_TOOL_REQUEST",
        "TOOL_RESULT",
        "ASSISTANT_MESSAGE",
    ]
    assert (
        model.requests[
            1
        ].compiled_input.canonical_input_identity.provider_input_through_sequence
        == 3
    )
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    assert tuple(row["event_type"] for row in events) == (
        "UserMessageAccepted",
        "AssistantToolRequestAccepted",
        "CapabilityDecisionAccepted",
        "ToolAttemptAccepted",
        "ToolResultAccepted",
        "AssistantMessageAccepted",
        "TurnCompleted",
    )


def test_stage2_live_bus_overflow_is_nonblocking_and_returns_gap() -> None:
    bus = LiveAgentEventBus(maximum_events=2, maximum_payload_bytes=128)
    observer, generation, revision = bus.subscribe()
    assert generation == 1 and revision == 0
    for index in range(4):
        assert (
            bus.offer_nowait(
                event_type=LiveEventType.TEXT_DELTA,
                session_id="session",
                turn_id="turn",
                draft_identity="draft",
                payload=TextDeltaPayload(block_identity="block:1", delta=str(index)),
            )
            is not None
        )
    observed = bus.observe(observer, after_revision=0, maximum_events=2)
    assert observed.kind is LiveObservationKind.GAP
    assert observed.latest_revision == 4
    bus.close()
    assert (
        bus.offer_nowait(
            event_type=LiveEventType.TEXT_END,
            session_id="session",
            turn_id="turn",
            draft_identity="draft",
            payload=TextEndPayload(
                block_identity="block:1",
                final_text="",
                utf8_bytes=0,
                digest=live_digest(""),
            ),
        )
        is None
    )


def test_stage2_live_cursor_is_client_owned_and_lost_response_is_repeatable() -> None:
    bus = LiveAgentEventBus(maximum_events=8, maximum_payload_bytes=4096)
    observer, generation, revision = bus.subscribe()
    assert generation == 1 and revision == 0
    for index in range(3):
        bus.offer_nowait(
            event_type=LiveEventType.TEXT_DELTA,
            session_id="session",
            turn_id="turn",
            draft_identity="entry:future",
            payload=TextDeltaPayload(block_identity="block:1", delta=str(index)),
        )
    first = bus.observe(observer, after_revision=0, maximum_events=2)
    repeated = bus.observe(observer, after_revision=0, maximum_events=2)
    assert first == repeated
    assert first.latest_revision == 2
    tail = bus.observe(observer, after_revision=2, maximum_events=2)
    assert tuple(item.revision for item in tail.events) == (3,)
    settlement = bus.offer_settlement_nowait(
        kind=LiveSettlementKind.COMMITTED,
        session_id="session",
        turn_id="turn",
        draft_identity="entry:future",
        committed_entry_id="entry:future",
    )
    assert settlement is not None
    terminal = bus.observe(observer, after_revision=3, maximum_events=2)
    assert terminal.events == ()
    assert terminal.settlements == (settlement,)


def test_stage2_no_attempt_result_is_committed_before_any_physical_invoke(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")])
    tools = _DenyingTool(provider, session_id)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn("do not dispatch"))
    assert tools.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT attempt_id, result_state FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (None, "PERMISSION_DENIED")


def test_stage2_lost_assistant_commit_ack_exact_confirms_single_winner(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostAssistantAckRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("one winner")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    accepted = asyncio.run(runner.run_turn("confirm the winner"))
    assert accepted is not None
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'ASSISTANT_MESSAGE'",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status, final_entry_id FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("COMPLETED", accepted.final_entry_id)


def test_stage2_lost_tool_request_ack_confirms_before_single_dispatch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostAssistantAckRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")]),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn("dispatch exactly once"))
    assert len(tools.invocations) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'ASSISTANT_TOOL_REQUEST'",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)


def test_stage2_subagent_runner_produces_durable_message_child(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _name("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=parent_turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
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
        objective="produce one message",
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
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("child answer")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = asyncio.run(
        runner.run_subagent_turn(task_id=task_id, objective="produce one message")
    )
    repository.accept_subagent_child(
        lease.guard,
        child_id=_name("subagent-result"),
        task_id=task_id,
        child_kind="RESULT",
        child_ordinal=result.model_call_count,
        entry_id=result.final_entry_id,
        occurred_at=datetime.now(timezone.utc),
        actor_id=task_id,
        deadline_monotonic=monotonic() + 30,
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        children = connection.execute(
            "SELECT child_kind, child_ordinal, entry_id "
            "FROM pulsara_v3.subagent_task_children "
            "WHERE session_id = %s AND task_id = %s "
            "ORDER BY child_ordinal",
            (session_id, task_id),
        ).fetchall()
        assert children == [
            ("MESSAGE", 0, result.final_entry_id),
            ("RESULT", 1, result.final_entry_id),
        ]
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events "
            "WHERE session_id = %s AND event_type = 'SubagentMessageAccepted'",
            (session_id,),
        ).fetchone() == (1,)


def test_stage2_confirmation_without_controller_keeps_policy_and_result_closed(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _MeasuredRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _ConfirmationWithoutControllerTool(provider, session_id)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")]),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn("ask before dispatch"))
    assert tools.invocations == []
    assert repository.host_write_transactions == 5
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            """
            SELECT decision, actor_kind, command_id
            FROM pulsara_v3.interaction_decisions
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == ("REQUIRE_CONFIRMATION", "machine", None)
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.tool_execution_attempts
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT attempt_id, result_state FROM pulsara_v3.tool_results
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == (None, "PERMISSION_DENIED")


def test_stage2_large_assistant_content_uses_immutable_blob_reference(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    text = "x" * (70 << 10)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream(text)]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn("large answer"))
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        row = connection.execute(
            """
            SELECT b.blob_id, x.logical_size, octet_length(x.body)
            FROM pulsara_v3.assistant_message_blocks b
            JOIN pulsara_v3.blobs x ON x.id = b.blob_id
            WHERE b.session_id = %s AND b.block_kind = 'TEXT'
            """,
            (session_id,),
        ).fetchone()
    assert row is not None
    assert row[1:] == (len(text), len(text))


def test_stage2_cancellation_does_not_turn_a_live_tool_into_system_error(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _BlockingTool(provider, session_id)
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream()]),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run_turn("block in a tool"))
        await asyncio.wait_for(tools.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED",)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)


def test_round1_provider_rematerialization_uses_preview_and_scoped_artifact(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")])
    runner = ConversationKernelRunner(
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_LargeTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn("return a large result"))
    tool_messages = [
        item
        for item in model.requests[1].compiled_input.messages
        if item.role is MessageRole.TOOL_RESULT
    ]
    assert len(tool_messages) == 1
    preview = tool_messages[0].content[0]
    assert len(preview.encode("utf-8")) <= 65_536
    assert "OUTPUT TRUNCATED / PREVIEW" in preview
    assert "Use artifact_read" in preview
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        artifact_id = connection.execute(
            """
            SELECT output_artifact_id
            FROM pulsara_v3.tool_results
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone()[0]
    read_port = PostgresToolArtifactReadPort(
        provider,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    page = read_port.read_text(
        artifact_id,
        offset_chars=0,
        max_chars=32_000,
    )
    assert page.text == "z" * 32_000
    assert page.has_more
