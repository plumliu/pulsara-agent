from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from threading import Event
from threading import Thread
from time import monotonic
from time import sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import APITimeoutError
import pytest

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelExecutionWatchdogPolicy,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    build_prepared_root_turn_intent,
    build_prepared_subagent_turn_admission,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.memory_tools import (
    KernelMemoryToolPort,
    MEMORY_POINT_READ_TIMEOUT_SECONDS,
    MEMORY_SEARCH_TIMEOUT_SECONDS,
)
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.conversation_kernel.io import (
    KernelSessionIO,
    PhysicalToolInvocationDisposition,
    PhysicalToolInvocationTiming,
)
from pulsara_agent.conversation_kernel.tool_runtime import (
    _physical_effect_class,
    _production_executor_binding,
    _terminal_execution_result,
    production_builtin_executor_binding_identity_fingerprint,
)
from pulsara_agent.llm.adapters.openai import client as openai_client
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.terminal_process.models import TerminalResult, TerminalStatus


ROOT = Path(__file__).resolve().parents[1]


def test_round5_architecture_removes_turn_budget_and_preserves_oracles() -> None:
    parameters = inspect.signature(ConversationKernelRunner).parameters
    assert "maximum_model_calls_per_turn" not in parameters
    assert "operation_timeout_seconds" not in parameters
    assert not hasattr(STAGE2_LIMITS, "model_calls_per_turn_hard")
    assert not hasattr(STAGE2_LIMITS, "host_close_hard_ms")

    foreground_modules = (
        "runner.py",
        "provider_dispatch.py",
        "tool_execution.py",
        "turn_admission.py",
        "steer_consumption.py",
        "plan_runtime.py",
        "memory/dispatch.py",
        "compaction/coordinator.py",
    )
    foreground_source = "\n".join(
        (ROOT / "src/pulsara_agent/conversation_kernel" / relative).read_text(
            encoding="utf-8"
        )
        for relative in foreground_modules
    )
    assert "while model_call_count <" not in foreground_source
    assert "model-call limit exhausted" not in foreground_source
    assert "maximum_model_calls_per_turn" not in foreground_source
    assert "maximum_tool_calls_per_turn" not in foreground_source

    assert len(COMMITTED_EVENT_DESCRIPTORS) == 29
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 28


def test_round5_watchdog_policy_is_closed_and_has_no_turn_or_call_budget() -> None:
    policy = KernelExecutionWatchdogPolicy()

    assert policy.turn_total_seconds is None
    assert policy.model_calls_per_turn is None
    assert policy.tool_calls_per_turn is None
    assert policy.foreground_provider_total_seconds is None
    assert (
        policy.writer_renew_interval_seconds
        + policy.writer_renew_attempt_seconds
        + policy.writer_renew_safety_margin_seconds
        < policy.writer_lease_seconds
    )
    assert policy.terminal_foreground_decision_seconds > 30.0

    with pytest.raises(ValueError, match="does not fit inside its lease"):
        KernelExecutionWatchdogPolicy(
            writer_lease_seconds=30,
            writer_renew_interval_seconds=10,
            writer_renew_attempt_seconds=15,
            writer_renew_safety_margin_seconds=5,
        )


def test_round5_memory_keeps_its_existing_point_and_search_timeout_owner() -> None:
    assert MEMORY_POINT_READ_TIMEOUT_SECONDS == 10.0
    assert MEMORY_SEARCH_TIMEOUT_SECONDS == 20.0
    assert "deadline_factory" not in inspect.signature(KernelMemoryToolPort).parameters


def test_round5_deadline_factory_only_accepts_closed_owners_and_issues_fresh() -> None:
    ticks = iter((100.0, 150.0, 200.0))
    factory = KernelExecutionDeadlineFactory(clock=lambda: next(ticks))

    assert factory.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL) == 220.0
    assert factory.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL) == 270.0
    assert factory.deadline(KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING) == 320.0
    with pytest.raises(TypeError, match="closed vocabulary"):
        factory.deadline("FOREGROUND_CANONICAL")  # type: ignore[arg-type]


def test_round5_turn_admission_candidates_freeze_complete_event_drafts() -> None:
    occurred_at = datetime.now(timezone.utc)
    root = build_prepared_root_turn_intent(
        session_id="session:root",
        command_id="command:root",
        turn_id="turn:root",
        entry_id="entry:root",
        context_binding_revision_id="revision:root:0",
        permission_snapshot_id="permission:root",
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(b"root"),
        occurred_at=occurred_at,
    )
    child = build_prepared_subagent_turn_admission(
        session_id="session:child",
        task_id="task:child",
        turn_id="turn:child",
        entry_id="entry:child",
        context_binding_revision_id="revision:child:0",
        permission_snapshot_id="permission:child",
        task_start_event_id="event:task-start:child",
        expected_parent_permission_snapshot=build_run_permission_snapshot(
            snapshot_id="permission:parent",
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        ),
        content=InlineContent.from_bytes(b"child"),
        occurred_at=occurred_at,
    )

    with pytest.raises((AttributeError, TypeError)):
        root.actor_id = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        child.event.payload["mutated"] = True  # type: ignore[index]


def test_round5_close_owners_use_independent_policy_fields() -> None:
    policy = KernelExecutionWatchdogPolicy(
        host_session_close_join_seconds=41,
        blob_gc_close_seconds=47,
    )
    factory = KernelExecutionDeadlineFactory(policy, clock=lambda: 100.0)

    assert factory.deadline(KernelWatchdogOwner.HOST_SESSION_CLOSE) == 141.0
    assert factory.deadline(KernelWatchdogOwner.BLOB_GC_CLOSE) == 147.0


def test_round5_foreground_openai_timeout_is_typed_and_has_no_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            calls.append(dict(kwargs))

    monkeypatch.setattr(openai_client, "AsyncOpenAI", FakeAsyncOpenAI)
    policy = KernelExecutionWatchdogPolicy().foreground_transport
    openai_client.build_async_openai_client(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        timeout_policy=policy,
        credential_boundary=ProcessCredentialBoundary(),
        max_retries=0,
    )

    timeout = calls[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 120.0
    assert timeout.write == 120.0
    assert timeout.pool == 120.0
    assert timeout.read == 600.0
    assert policy.total_seconds is None


class _SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("content-length", "0"))
        self.rfile.read(size)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        idle = bool(getattr(self.server, "inject_idle_gap"))
        frames = (
            _responses_sse_frames()
            if self.path.endswith("/responses")
            else _chat_sse_frames()
        )
        for index, frame in enumerate(frames):
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if index + 1 < len(frames):
                sleep(0.09 if idle and index == 0 else 0.025)
        self.close_connection = True


def _chat_sse_frames() -> tuple[bytes, ...]:
    def frame(delta: str, finish_reason: str | None = None) -> bytes:
        payload = {
            "id": "chatcmpl_round5",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4.1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": delta},
                    "finish_reason": finish_reason,
                }
            ],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    return frame("a"), frame("b"), frame("", "stop"), b"data: [DONE]\n\n"


def _responses_sse_frames() -> tuple[bytes, ...]:
    response = {
        "id": "resp_round5",
        "created_at": 1.0,
        "model": "gpt-4.1",
        "object": "response",
        "output": [],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }

    def frame(event: str, payload: dict[str, object]) -> bytes:
        return (f"event: {event}\ndata: {json.dumps(payload)}\n\n").encode()

    return (
        frame(
            "response.created",
            {"type": "response.created", "sequence_number": 0, "response": response},
        ),
        frame(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "output_index": 0,
                "content_index": 0,
                "item_id": "item_round5",
                "delta": "a",
                "logprobs": [],
            },
        ),
        frame(
            "response.completed",
            {"type": "response.completed", "sequence_number": 2, "response": response},
        ),
    )


async def _consume_local_sse(*, api: str, base_url: str) -> int:
    client = openai_client.build_async_openai_client(
        api_key="sk-fixture-secret",
        base_url=base_url,
        timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 0.05, None),
        credential_boundary=ProcessCredentialBoundary(),
        max_retries=0,
    )
    try:
        if api == "chat":
            stream = await client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": "test"}],
                stream=True,
            )
        else:
            stream = await client.responses.create(
                model="gpt-4.1",
                input="test",
                stream=True,
            )
        count = 0
        async for _item in stream:
            count += 1
        return count
    finally:
        await client.close()


@pytest.mark.parametrize("api", ("chat", "responses"))
def test_round5_local_sse_activity_can_outlive_read_idle_total(api: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    server.inject_idle_gap = False  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        started = monotonic()
        count = asyncio.run(
            _consume_local_sse(
                api=api,
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
        )
        assert monotonic() - started > 0.05
        assert count >= 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("api", ("chat", "responses"))
def test_round5_local_sse_silent_gap_hits_read_idle(api: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    server.inject_idle_gap = True  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises((APITimeoutError, httpx.ReadTimeout)):
            asyncio.run(
                _consume_local_sse(
                    api=api,
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_round5_tool_watchdog_preserves_late_exact_return() -> None:
    entered = Event()
    release = Event()
    calls = 0

    def physical(*, deadline_monotonic: float) -> str:
        nonlocal calls
        del deadline_monotonic
        calls += 1
        entered.set()
        assert release.wait(2)
        return "exact-result"

    async def scenario() -> None:
        owner = KernelSessionIO(maximum_concurrency=1)
        task = asyncio.create_task(
            owner.run_tool_invocation(
                physical,
                deadline_monotonic=monotonic() + 0.02,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.04)
        release.set()
        outcome = await task
        assert outcome.disposition is PhysicalToolInvocationDisposition.RETURNED_EXACT
        assert outcome.timing is PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG
        assert outcome.value == "exact-result"
        assert outcome.error is None
        assert calls == 1
        await owner.aclose(deadline_monotonic=monotonic() + 1)

    asyncio.run(scenario())


def test_round5_tool_cancellation_callback_releases_physical_owner() -> None:
    entered = Event()
    release = Event()
    callback_calls = 0

    def physical(*, deadline_monotonic: float) -> str:
        del deadline_monotonic
        entered.set()
        assert release.wait(2)
        return "cancelled-exact-result"

    def cancel_physical() -> None:
        nonlocal callback_calls
        callback_calls += 1
        release.set()

    async def scenario() -> None:
        owner = KernelSessionIO(maximum_concurrency=1)
        task = asyncio.create_task(
            owner.run_tool_invocation(
                physical,
                deadline_monotonic=monotonic() + 2,
                on_caller_cancelled=cancel_physical,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        outcome = await asyncio.wait_for(task, timeout=1)
        assert outcome.disposition is PhysicalToolInvocationDisposition.RETURNED_EXACT
        assert outcome.timing is PhysicalToolInvocationTiming.LATE_AFTER_WATCHDOG
        assert outcome.caller_cancelled
        assert outcome.value == "cancelled-exact-result"
        assert callback_calls == 1
        await owner.aclose(deadline_monotonic=monotonic() + 1)

    asyncio.run(scenario())


def test_round5_io_close_reports_timeout_only_after_physical_thread_exit() -> None:
    entered = Event()
    release = Event()

    def physical(*, deadline_monotonic: float) -> str:
        del deadline_monotonic
        entered.set()
        assert release.wait(2)
        return "physically-terminal"

    async def scenario() -> None:
        owner = KernelSessionIO(maximum_concurrency=1)
        operation = asyncio.create_task(
            owner.run(physical, deadline_monotonic=monotonic() + 1)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        close = asyncio.create_task(owner.aclose(deadline_monotonic=monotonic() + 0.02))
        await asyncio.sleep(0.05)
        assert not close.done()
        release.set()
        assert await operation == "physically-terminal"
        with pytest.raises(TimeoutError, match="after close deadline"):
            await close

    asyncio.run(scenario())


def _bare_host_core_with_blocked_blob_gc(
    *, close_seconds: float
) -> tuple[KernelHostCore, asyncio.Event, asyncio.Event]:
    core = object.__new__(KernelHostCore)
    core.mcp_management = SimpleNamespace(aclose=AsyncMock())
    core._deadlines = KernelExecutionDeadlineFactory(  # noqa: SLF001
        KernelExecutionWatchdogPolicy(blob_gc_close_seconds=close_seconds)
    )
    core._sessions = {}  # noqa: SLF001
    core._open_attempts = set()  # noqa: SLF001
    core._close_attempts = {}  # noqa: SLF001
    core._extension_routes = {}  # noqa: SLF001
    core._jobs = None  # noqa: SLF001
    core._lock = asyncio.Lock()  # noqa: SLF001
    core._closing = False  # noqa: SLF001
    core._closed = False  # noqa: SLF001
    core._shutdown_task = None  # noqa: SLF001
    core._blob_gc_io = KernelSessionIO(maximum_concurrency=1)  # noqa: SLF001
    core._blob_store = object()  # noqa: SLF001
    core._access = None  # noqa: SLF001
    core._repository = None  # noqa: SLF001
    core._event_loop = asyncio.get_running_loop()  # noqa: SLF001
    core._bundled_skill_binding = (  # noqa: SLF001
        BundledSkillDistributionBindingOwner()
    )
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def blocked_gc() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    core._blob_gc_task = asyncio.create_task(blocked_gc())  # noqa: SLF001
    return core, cancelled, release


def test_round5_blob_gc_timeout_reports_only_after_exact_task_and_io_join() -> None:
    async def scenario() -> None:
        core, cancelled, release = _bare_host_core_with_blocked_blob_gc(
            close_seconds=0.02
        )
        gc_task = core._blob_gc_task  # noqa: SLF001
        shutdown = asyncio.create_task(core.shutdown())
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await asyncio.sleep(0.04)
        assert not shutdown.done()
        assert gc_task is not None and not gc_task.done()
        release.set()
        with pytest.raises(TimeoutError, match="after close deadline"):
            await shutdown
        assert gc_task.done()
        assert core._blob_gc_task is None  # noqa: SLF001
        assert core._blob_gc_io is None  # noqa: SLF001

    asyncio.run(scenario())


def test_round5_blob_gc_shutdown_cancellation_detaches_only_after_join() -> None:
    async def scenario() -> None:
        core, cancelled, release = _bare_host_core_with_blocked_blob_gc(
            close_seconds=1.0
        )
        gc_task = core._blob_gc_task  # noqa: SLF001
        shutdown = asyncio.create_task(core.shutdown())
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert gc_task is not None and gc_task.done()
        assert core._blob_gc_task is None  # noqa: SLF001
        assert core._blob_gc_io is None  # noqa: SLF001

    asyncio.run(scenario())


def test_round5_terminal_process_actions_have_closed_effect_semantics() -> None:
    binding = _production_executor_binding(
        "terminal_process", "round5:test-terminal-process-executor"
    )
    assert binding.catalog_entry.name == "terminal_process"
    assert production_builtin_executor_binding_identity_fingerprint(binding).startswith(
        "sha256:"
    )
    for action in ("list", "log", "poll", "wait"):
        assert (
            _physical_effect_class("terminal_process", {"action": action})
            == "TERMINAL_OBSERVATION"
        )
    for action in ("write", "submit", "close_stdin", "kill"):
        assert (
            _physical_effect_class("terminal_process", {"action": action})
            == "TERMINAL_EFFECT"
        )
    assert _physical_effect_class("terminal", {}) == "TERMINAL_EFFECT"


def _exec_sleeping_terminal(
    registry: ProcessRegistry,
    tmp_path: Path,
    *,
    attempt_id: str,
    decision_deadline_monotonic: float,
    yield_time_ms: int,
):
    return registry.exec_with_yield(
        terminal_session_id="default",
        command="sleep",
        cwd=tmp_path,
        yield_time_ms=yield_time_ms,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id="round5:host",
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        env=dict(os.environ),
        decision_attempt_id=attempt_id,
        decision_deadline_monotonic=decision_deadline_monotonic,
    )


def test_round5_terminal_decision_watchdog_during_preparing_kills_only_exact_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessRegistry(max_live_processes=4)
    sibling, yielded, _ = _exec_sleeping_terminal(
        registry,
        tmp_path,
        attempt_id="decision:sibling",
        decision_deadline_monotonic=monotonic() + 2,
        yield_time_ms=0,
    )
    assert yielded is True
    registry.settle_foreground_decision("decision:sibling")

    entered = Event()
    release = Event()
    original_spawn = registry._spawn  # noqa: SLF001

    def delayed_spawn(**kwargs):
        entered.set()
        assert release.wait(2)
        return original_spawn(**kwargs)

    monkeypatch.setattr(registry, "_spawn", delayed_spawn)
    outcome: list[object] = []

    def invoke() -> None:
        try:
            outcome.append(
                _exec_sleeping_terminal(
                    registry,
                    tmp_path,
                    attempt_id="decision:preparing",
                    decision_deadline_monotonic=monotonic() + 0.03,
                    yield_time_ms=1_000,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            outcome.append(exc)

    worker = Thread(target=invoke)
    worker.start()
    assert entered.wait(1)
    sleep(0.06)
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], BaseException)
    state, yielded, _cwd = outcome[0]  # type: ignore[misc]
    assert yielded is False
    assert state.killed is True
    assert state.physical_completion.is_set()
    assert (
        registry.poll(
            sibling.process_id,
            max_output_chars=32,
            owner_host_session_id="round5:host",
        ).status.value
        == "running"
    )
    registry.settle_foreground_decision("decision:preparing")
    registry.release_owner("round5:host", timeout_seconds=2)


def test_round5_terminal_decision_process_installed_and_result_ready_races(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry(max_live_processes=4)
    timed, yielded, _ = _exec_sleeping_terminal(
        registry,
        tmp_path,
        attempt_id="decision:installed",
        decision_deadline_monotonic=monotonic() + 0.03,
        yield_time_ms=1_000,
    )
    assert yielded is False
    assert timed.killed is True
    assert timed.physical_completion.is_set()
    assert registry.foreground_decision_state("decision:installed") == "RESULT_READY"
    registry.settle_foreground_decision("decision:installed")

    completed, yielded, _ = registry.exec_with_yield(
        terminal_session_id="default",
        command="true",
        cwd=tmp_path,
        yield_time_ms=1_000,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id="round5:host",
        shell_argv=(sys.executable, "-c", "pass"),
        env=dict(os.environ),
        decision_attempt_id="decision:ready",
        decision_deadline_monotonic=monotonic() + 0.2,
    )
    assert yielded is False
    assert completed.killed is False
    assert completed.physical_completion.is_set()
    sleep(0.25)
    assert registry.foreground_decision_state("decision:ready") == "RESULT_READY"
    registry.settle_foreground_decision("decision:ready")
    registry.release_owner("round5:host", timeout_seconds=2)


def test_round5_terminal_foreground_decision_can_be_aborted_by_exact_attempt(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry(max_live_processes=2)
    outcome: list[object] = []

    def invoke() -> None:
        try:
            outcome.append(
                _exec_sleeping_terminal(
                    registry,
                    tmp_path,
                    attempt_id="decision:user-stop",
                    decision_deadline_monotonic=monotonic() + 5,
                    yield_time_ms=4_000,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            outcome.append(exc)

    worker = Thread(target=invoke)
    worker.start()
    deadline = monotonic() + 2
    while registry.foreground_decision_state("decision:user-stop") is None:
        assert monotonic() < deadline
        sleep(0.01)
    assert registry.abort_foreground_decision("decision:user-stop")
    worker.join(3)
    assert not worker.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], BaseException)
    state, yielded, _cwd = outcome[0]  # type: ignore[misc]
    assert yielded is False
    assert state.killed is True
    assert state.physical_completion.is_set()
    registry.settle_foreground_decision("decision:user-stop")
    registry.release_owner("round5:host", timeout_seconds=2)


def test_round5_killed_terminal_result_is_an_interrupted_tool_result() -> None:
    result = _terminal_execution_result(
        ToolCall("call:user-stop", "terminal"),
        TerminalResult(
            status=TerminalStatus.KILLED,
            output="partial output",
            exit_code=-15,
            cwd="/tmp",
        ),
    )

    assert result.status is ToolResultState.INTERRUPTED
