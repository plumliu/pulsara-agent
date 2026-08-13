"""Host-lifetime replacement coverage for the removed durable monitor owner."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import shlex
import sys
from threading import Event
from time import monotonic
from typing import Awaitable, Callable
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.host import (
    HostSessionCloseDecisionFrozen,
    HostSessionCloseState,
    KernelHostCore,
    KernelHostSession,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelExecutionWatchdogPolicy,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard, WriterLease
from pulsara_agent.conversation_kernel.runner import KernelToolAuthorizationKind
from pulsara_agent.conversation_kernel.tool_runtime import (
    DirectKernelToolPort,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
    ToolOutputArtifactProcessor,
)
from pulsara_agent.conversation_kernel.contracts import BlobContent
from pulsara_agent.ports.artifact import ToolResultDisplayKind
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
)
from tests.support.round3 import authorize_direct_tool, invoke_direct_tool


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class _RecordingDeadlineFactory(KernelExecutionDeadlineFactory):
    def __init__(
        self, policy: KernelExecutionWatchdogPolicy | None = None
    ) -> None:
        super().__init__(policy)
        self.owners: list[KernelWatchdogOwner] = []

    def deadline(self, owner: KernelWatchdogOwner) -> float:
        self.owners.append(owner)
        return super().deadline(owner)


def test_tool_result_preview_is_utf8_safe_and_obeys_final_hard_cap() -> None:
    class Publisher:
        def publish(self, **kwargs: object) -> BlobContent:
            content = bytes(kwargs["content"])
            return BlobContent(
                "blob:test",
                "sha256:" + __import__("hashlib").sha256(content).hexdigest(),
                len(content),
                "text/plain",
                "utf-8",
            )

    processor = ToolOutputArtifactProcessor(object(), publisher=Publisher())  # type: ignore[arg-type]
    text = "🙂" * 20_000
    prepared = processor.prepare(
        workspace_id="workspace:test",
        result_entry_id="entry:test",
        public_output=text,
        candidate=ToolOutputArtifactCandidate(
            role="OUTPUT",
            text=text,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            original_utf8_bytes=len(text.encode("utf-8")),
        ),
        artifact_source_read=False,
        deadline_monotonic=float("inf"),
    )
    assert prepared.display_kind is ToolResultDisplayKind.HEAD_TAIL
    assert prepared.canonical_preview.size <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
    prepared.canonical_preview.canonical_bytes.decode("utf-8")


async def _start_background_process(
    port: DirectKernelToolPort,
    *,
    session_id: str,
) -> tuple[str, str, str]:
    call_id = _name("call")
    turn_id = _name("turn")
    entry_id = _name("entry")
    arguments = {
        "command": (
            f"{shlex.quote(sys.executable)} -c "
            "'import time; print(\"READY\", flush=True); time.sleep(30)'"
        ),
        "yield_time_ms": 0,
    }
    authorization = await authorize_direct_tool(
        port,
        session_id=session_id,
        tool_name="terminal",
        arguments=arguments,
        tool_call_id=call_id,
        turn_id=turn_id,
        assistant_entry_id=entry_id,
    )
    assert authorization.kind is KernelToolAuthorizationKind.ALLOW
    result = await invoke_direct_tool(
        port,
        session_id=session_id,
        tool_name="terminal",
        arguments=arguments,
        tool_call_id=call_id,
        attempt_id=_name("attempt"),
        turn_id=turn_id,
        assistant_entry_id=entry_id,
    )
    payload = json.loads(result.content)
    assert payload["status"] == "running"
    assert payload["yielded_to_background"] is True
    return str(payload["process_id"]), turn_id, entry_id


def test_stage2_terminal_handle_is_same_host_only_and_close_kills_and_joins(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        owner = _name("host")
        session_id = _name("session")
        deadlines = _RecordingDeadlineFactory()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=owner,
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            deadline_factory=deadlines,
        )
        process_id, turn_id, entry_id = await _start_background_process(
            port, session_id=session_id
        )
        assert deadlines.owners == [
            KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
        ]
        poll = await invoke_direct_tool(
            port,
            session_id=session_id,
            tool_name="terminal_process",
            arguments={"action": "poll", "process_id": process_id},
            tool_call_id=_name("call"),
            attempt_id=_name("attempt"),
            turn_id=turn_id,
            assistant_entry_id=entry_id,
        )
        assert json.loads(poll.content)["process_id"] == process_id
        assert deadlines.owners[-1] is KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
        assert (
            port._terminal.live_process_count(  # noqa: SLF001
                owner_host_session_id=owner
            )
            == 1
        )
        await port.aclose(timeout_seconds=5.0)
        assert (
            port._terminal.live_process_count(  # noqa: SLF001
                owner_host_session_id=owner
            )
            == 0
        )

    asyncio.run(scenario())


def test_stage2_terminal_new_host_does_not_adopt_or_relaunch_old_process(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        old_owner = _name("host")
        new_owner = _name("host")
        old_session_id = _name("session")
        new_session_id = _name("session")
        old = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=old_owner,
            session_id=old_session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        new = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=new_owner,
            session_id=new_session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        process_id, turn_id, entry_id = await _start_background_process(
            old, session_id=old_session_id
        )
        with pytest.raises(KeyError):
            await invoke_direct_tool(
                new,
                session_id=new_session_id,
                tool_name="terminal_process",
                arguments={"action": "poll", "process_id": process_id},
                tool_call_id=_name("call"),
                attempt_id=_name("attempt"),
                turn_id=turn_id,
                assistant_entry_id=entry_id,
            )
        assert (
            new._terminal.live_process_count(  # noqa: SLF001
                owner_host_session_id=new_owner
            )
            == 0
        )
        await old.aclose(timeout_seconds=5.0)
        await new.aclose(timeout_seconds=5.0)

    asyncio.run(scenario())


def test_tool_close_blocks_until_cancelled_physical_thread_exits(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = Event()
        release = Event()
        physically_exited = Event()

        class BlockingTool:
            name = "read_file"

            def execute(self, call: ToolCall) -> ToolExecutionResult:
                started.set()
                release.wait()
                physically_exited.set()
                return ToolExecutionResult(
                    call.id, self.name, ToolResultState.SUCCESS, "done"
                )

        session_id = _name("session")
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=_name("host"),
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._tools["read_file"] = BlockingTool()  # type: ignore[assignment]  # noqa: SLF001
        operation = asyncio.create_task(
            invoke_direct_tool(
                port,
                session_id=session_id,
                tool_name="read_file",
                arguments={"path": "ignored"},
                tool_call_id=_name("call"),
                attempt_id=_name("attempt"),
                turn_id=_name("turn"),
                assistant_entry_id=_name("entry"),
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        operation.cancel()
        close = asyncio.create_task(port.aclose(timeout_seconds=0.05))
        await asyncio.sleep(0.08)
        assert not close.done()
        assert not physically_exited.is_set()
        assert not port._physically_closed  # noqa: SLF001

        release.set()
        # Round 5A keeps the caller attached until the physical worker exits.
        # Once that worker returns an exact value, cancellation cannot erase
        # the known tool outcome; the runner will accept it before interrupting
        # the enclosing turn.
        result = await operation
        assert result.content == b"done"
        assert result.physical_timing == "LATE_AFTER_WATCHDOG"
        assert result.caller_cancelled_while_running
        with pytest.raises(TimeoutError, match="after close deadline"):
            await close
        assert physically_exited.is_set()
        assert port._physically_closed  # noqa: SLF001
        await port.aclose(timeout_seconds=1)

    asyncio.run(scenario())


def test_host_core_close_waiter_cancellation_joins_one_physical_owner() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0
        close_conversation_requests = 0

        class BlockingSession:
            session_id = "session:blocking"

            def request_close_conversation(self) -> None:
                nonlocal close_conversation_requests
                close_conversation_requests += 1

            async def aclose(
                self,
                *,
                close_conversation: bool,
                deadline_monotonic: float,
                freeze_close_conversation_decision: Callable[[], Awaitable[bool]],
            ) -> None:
                nonlocal calls
                del close_conversation, deadline_monotonic
                calls += 1
                started.set()
                await release.wait()
                await freeze_close_conversation_decision()

        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()  # type: ignore[attr-defined]
        core._deadlines = KernelExecutionDeadlineFactory()  # type: ignore[attr-defined]
        core._close_attempts = {}  # type: ignore[attr-defined]
        session = BlockingSession()
        core._sessions = {"host:blocking": session}  # type: ignore[attr-defined]
        core._extension_routes = {}  # type: ignore[attr-defined]

        first = asyncio.create_task(
            core.close_session("host:blocking", close_conversation=False)
        )
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert core._sessions == {"host:blocking": session}  # type: ignore[attr-defined]  # noqa: SLF001
        assert calls == 1

        second = asyncio.create_task(
            core.close_session("host:blocking", close_conversation=True)
        )
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await second
        assert core._sessions == {}  # type: ignore[attr-defined]  # noqa: SLF001
        assert calls == 1
        assert close_conversation_requests == 1
        assert core._close_attempts == {}  # type: ignore[attr-defined]  # noqa: SLF001

    asyncio.run(scenario())


def test_round5_host_close_freezes_one_fresh_deadline_for_all_waiters() -> None:
    async def scenario() -> None:
        now = [10_000.0]
        deadline_factory_calls = 0

        def clock() -> float:
            nonlocal deadline_factory_calls
            deadline_factory_calls += 1
            return now[0]

        policy = KernelExecutionWatchdogPolicy(
            host_session_close_join_seconds=37.0
        )
        release = asyncio.Event()
        started = asyncio.Event()
        received_deadlines: list[float] = []

        class BlockingSession:
            session_id = "session:deadline"

            def request_close_conversation(self) -> None:
                return

            async def aclose(
                self,
                *,
                close_conversation: bool,
                deadline_monotonic: float,
                freeze_close_conversation_decision: Callable[[], Awaitable[bool]],
            ) -> None:
                assert close_conversation is False
                received_deadlines.append(deadline_monotonic)
                started.set()
                await release.wait()
                await freeze_close_conversation_decision()

        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()  # type: ignore[attr-defined]
        core._deadlines = KernelExecutionDeadlineFactory(  # type: ignore[attr-defined]
            policy, clock=clock
        )
        core._close_attempts = {}  # type: ignore[attr-defined]
        session = BlockingSession()
        core._sessions = {"host:deadline": session}  # type: ignore[attr-defined]
        core._extension_routes = {}  # type: ignore[attr-defined]

        first = asyncio.create_task(
            core.close_session("host:deadline", close_conversation=False)
        )
        await started.wait()
        assert received_deadlines == [10_037.0]
        # Simulate a turn that had already been alive for much longer than the
        # removed 120-second budget, then attach another close waiter.  The
        # waiter must not mint or extend the installed close deadline.
        now[0] += 10_000.0
        second = asyncio.create_task(
            core.close_session("host:deadline", close_conversation=False)
        )
        await asyncio.sleep(0)
        assert deadline_factory_calls == 1
        installed_attempt = core._close_attempts["host:deadline"]  # type: ignore[attr-defined]  # noqa: SLF001
        assert installed_attempt.deadline_monotonic == 10_037.0
        release.set()
        await asyncio.gather(first, second)
        assert installed_attempt.state is HostSessionCloseState.CLOSED
        assert core._close_attempts == {}  # type: ignore[attr-defined]  # noqa: SLF001

    asyncio.run(scenario())


def test_round5_canonical_close_upgrade_after_decision_fence_is_rejected() -> None:
    async def scenario() -> None:
        decision_frozen = asyncio.Event()
        release = asyncio.Event()

        class FencedSession:
            session_id = "session:fenced"
            canonical_closed = False

            def request_close_conversation(self) -> None:
                return

            async def aclose(
                self,
                *,
                close_conversation: bool,
                deadline_monotonic: float,
                freeze_close_conversation_decision: Callable[[], Awaitable[bool]],
            ) -> None:
                del close_conversation, deadline_monotonic
                self.canonical_closed = await freeze_close_conversation_decision()
                decision_frozen.set()
                await release.wait()

        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()  # type: ignore[attr-defined]
        core._deadlines = KernelExecutionDeadlineFactory()  # type: ignore[attr-defined]
        core._close_attempts = {}  # type: ignore[attr-defined]
        session = FencedSession()
        core._sessions = {"host:fenced": session}  # type: ignore[attr-defined]
        core._extension_routes = {}  # type: ignore[attr-defined]

        first = asyncio.create_task(
            core.close_session("host:fenced", close_conversation=False)
        )
        await decision_frozen.wait()
        with pytest.raises(
            HostSessionCloseDecisionFrozen,
            match="already frozen",
        ):
            await core.close_session("host:fenced", close_conversation=True)
        release.set()
        await first
        assert not session.canonical_closed
        assert core._close_attempts == {}  # type: ignore[attr-defined]  # noqa: SLF001
        with pytest.raises(
            HostSessionCloseDecisionFrozen,
            match="owner was retired",
        ):
            await core.close_session("host:fenced", close_conversation=True)
        # Repeated detach remains idempotent without retaining a tombstone.
        await core.close_session("host:fenced", close_conversation=False)

    asyncio.run(scenario())


def test_round5_writer_renewal_uses_its_short_owner_deadline_during_long_turn() -> None:
    async def scenario() -> None:
        policy = KernelExecutionWatchdogPolicy(
            writer_lease_seconds=0.2,
            writer_renew_interval_seconds=0.02,
            writer_renew_attempt_seconds=0.03,
            writer_renew_safety_margin_seconds=0.02,
        )
        deadlines = KernelExecutionDeadlineFactory(policy)
        guard = HostWriterGuard(
            session_id="session:renew",
            writer_generation=1,
            writer_owner_id="host:renew",
        )
        initial = WriterLease(guard=guard, expires_at=datetime.now(timezone.utc))
        calls: list[tuple[float, float]] = []

        class RecordingRepository:
            def renew_host_writer(
                self,
                actual_guard: HostWriterGuard,
                *,
                lease_seconds: float,
                deadline_monotonic: float,
            ) -> WriterLease:
                assert actual_guard == guard
                calls.append((lease_seconds, deadline_monotonic - monotonic()))
                return WriterLease(
                    guard=guard,
                    expires_at=datetime.now(timezone.utc),
                )

        class InlineIO:
            async def run(self, operation, /, *args: object, **kwargs: object):
                return operation(*args, **kwargs)

        session = object.__new__(KernelHostSession)
        session._deadlines = deadlines  # type: ignore[attr-defined]
        session._lease = initial  # type: ignore[attr-defined]
        session._io = InlineIO()  # type: ignore[attr-defined]
        session.repository = RecordingRepository()  # type: ignore[attr-defined]
        renewal = asyncio.create_task(session._renew_writer())  # noqa: SLF001
        try:
            async with asyncio.timeout(1):
                while len(calls) < 3:
                    await asyncio.sleep(0.005)
        finally:
            renewal.cancel()
            with pytest.raises(asyncio.CancelledError):
                await renewal

        assert len(calls) >= 3
        assert all(lease_seconds == 0.2 for lease_seconds, _ in calls)
        assert all(0 < remaining <= 0.031 for _, remaining in calls)
        assert (
            policy.writer_renew_interval_seconds
            + policy.writer_renew_attempt_seconds
            + policy.writer_renew_safety_margin_seconds
            < policy.writer_lease_seconds
        )
        assert inspect.signature(session._renew_writer).parameters == {}  # noqa: SLF001

    asyncio.run(scenario())


def test_host_core_close_failure_quarantines_the_unique_attempt() -> None:
    async def scenario() -> None:
        class FailingSession:
            session_id = "session:failing"
            physically_terminal = False

            def request_close_conversation(self) -> None:
                return

            async def aclose(
                self,
                *,
                close_conversation: bool,
                deadline_monotonic: float,
                freeze_close_conversation_decision: Callable[[], Awaitable[bool]],
            ) -> None:
                del close_conversation, deadline_monotonic
                await freeze_close_conversation_decision()
                self.physically_terminal = True
                raise TimeoutError("physical owner exited after close deadline")

        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()  # type: ignore[attr-defined]
        core._deadlines = KernelExecutionDeadlineFactory()  # type: ignore[attr-defined]
        core._close_attempts = {}  # type: ignore[attr-defined]
        session = FailingSession()
        core._sessions = {"host:failing": session}  # type: ignore[attr-defined]
        core._extension_routes = {}  # type: ignore[attr-defined]

        with pytest.raises(TimeoutError, match="physical owner"):
            await core.close_session("host:failing", close_conversation=False)
        attempt = core._close_attempts["host:failing"]  # type: ignore[attr-defined]  # noqa: SLF001
        assert attempt.state is HostSessionCloseState.CLOSE_FAILED_QUARANTINED
        assert session.physically_terminal
        assert core._sessions == {"host:failing": session}  # type: ignore[attr-defined]  # noqa: SLF001
        with pytest.raises(TimeoutError, match="physical owner"):
            await core.close_session("host:failing", close_conversation=False)

    asyncio.run(scenario())
