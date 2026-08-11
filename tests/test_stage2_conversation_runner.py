from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import monotonic
from typing import AsyncIterator
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveObservationKind,
    LiveSettlementKind,
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
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelModelRequest,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolResult,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    PostgresToolArtifactReadPort,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class _ScriptedModel:
    def __init__(self, calls: list[list[object]]) -> None:
        self._calls = calls
        self.requests: list[KernelModelRequest] = []

    async def stream(self, request: KernelModelRequest) -> AsyncIterator[object]:
        self.requests.append(request)
        for item in self._calls.pop(0):
            yield item


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
        tools=_AssertingTool(provider, session_id),
        live_bus=LiveAgentEventBus(),
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
        tools=tool,
        live_bus=LiveAgentEventBus(),
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
    assert model.requests[1].cut.provider_input_through_sequence == 3
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
        tools=tools,
        live_bus=LiveAgentEventBus(),
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
        tools=_AssertingTool(provider, session_id),
        live_bus=LiveAgentEventBus(),
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
        tools=tools,
        live_bus=LiveAgentEventBus(),
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
        tools=_AssertingTool(provider, session_id),
        live_bus=LiveAgentEventBus(),
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
        tools=tools,
        live_bus=LiveAgentEventBus(),
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
        tools=_AssertingTool(provider, session_id),
        live_bus=LiveAgentEventBus(),
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
        tools=tools,
        live_bus=LiveAgentEventBus(),
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
        tools=_LargeTool(provider, session_id),
        live_bus=LiveAgentEventBus(),
    )
    asyncio.run(runner.run_turn("return a large result"))
    tool_items = [
        item
        for item in model.requests[1].provider_input.items
        if item.item_kind.value == "TOOL_RESULT"
    ]
    assert len(tool_items) == 1
    preview = tool_items[0].text
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
