"""Host-lifetime replacement coverage for the removed durable monitor owner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
import sys
from threading import Event
from uuid import uuid4

import pytest

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.runner import KernelToolAuthorizationKind
from pulsara_agent.conversation_kernel.tool_runtime import (
    DirectKernelToolPort,
    _truncate_tool_result_utf8,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tool_permission import default_permission_policy


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


@pytest.mark.parametrize("maximum", [1, 4, 5, 10, 64])
def test_tool_result_truncation_is_utf8_safe_and_obeys_final_hard_cap(
    maximum: int,
) -> None:
    result = _truncate_tool_result_utf8("🙂" * 100, maximum_bytes=maximum)
    assert len(result) <= maximum
    result.decode("utf-8")


async def _start_background_process(
    port: DirectKernelToolPort,
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
    authorization = await port.authorize(
        tool_name="terminal",
        arguments=arguments,
        tool_call_id=call_id,
        turn_id=turn_id,
        assistant_entry_id=entry_id,
    )
    assert authorization.kind is KernelToolAuthorizationKind.ALLOW
    result = await port.invoke(
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
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=owner,
            session_id=_name("session"),
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(
                default_permission_policy()
            ),
        )
        process_id, turn_id, entry_id = await _start_background_process(port)
        poll = await port.invoke(
            tool_name="terminal_process",
            arguments={"action": "poll", "process_id": process_id},
            tool_call_id=_name("call"),
            attempt_id=_name("attempt"),
            turn_id=turn_id,
            assistant_entry_id=entry_id,
        )
        assert json.loads(poll.content)["process_id"] == process_id
        assert port._terminal.live_process_count(  # noqa: SLF001
            owner_host_session_id=owner
        ) == 1
        await port.aclose(timeout_seconds=5.0)
        assert port._terminal.live_process_count(  # noqa: SLF001
            owner_host_session_id=owner
        ) == 0

    asyncio.run(scenario())


def test_stage2_terminal_new_host_does_not_adopt_or_relaunch_old_process(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        old_owner = _name("host")
        new_owner = _name("host")
        old = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=old_owner,
            session_id=_name("session"),
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(
                default_permission_policy()
            ),
        )
        new = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=new_owner,
            session_id=_name("session"),
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(
                default_permission_policy()
            ),
        )
        process_id, turn_id, entry_id = await _start_background_process(old)
        with pytest.raises(KeyError):
            await new.invoke(
                tool_name="terminal_process",
                arguments={"action": "poll", "process_id": process_id},
                tool_call_id=_name("call"),
                attempt_id=_name("attempt"),
                turn_id=turn_id,
                assistant_entry_id=entry_id,
            )
        assert new._terminal.live_process_count(  # noqa: SLF001
            owner_host_session_id=new_owner
        ) == 0
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

        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=_name("host"),
            session_id=_name("session"),
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(
                default_permission_policy()
            ),
        )
        port._tools["read_file"] = BlockingTool()  # type: ignore[assignment]  # noqa: SLF001
        operation = asyncio.create_task(
            port.invoke(
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
        with pytest.raises(TimeoutError):
            await port.aclose(timeout_seconds=0.05)
        assert not physically_exited.is_set()
        assert not port._physically_closed  # noqa: SLF001

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await port.aclose(timeout_seconds=1)
        assert physically_exited.is_set()
        assert port._physically_closed  # noqa: SLF001

    asyncio.run(scenario())


def test_host_core_retains_close_blocker_until_same_session_physically_exits() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        class BlockingSession:
            session_id = "session:blocking"

            async def aclose(self, *, close_conversation: bool) -> None:
                del close_conversation
                if not release.is_set():
                    raise TimeoutError("physical owner is still running")

        core = object.__new__(KernelHostCore)
        core._lock = asyncio.Lock()  # type: ignore[attr-defined]
        session = BlockingSession()
        core._sessions = {"host:blocking": session}  # type: ignore[attr-defined]
        core._extension_routes = {}  # type: ignore[attr-defined]

        with pytest.raises(TimeoutError):
            await core.close_session("host:blocking", close_conversation=False)
        assert core._sessions == {"host:blocking": session}  # type: ignore[attr-defined]  # noqa: SLF001

        release.set()
        await core.close_session("host:blocking", close_conversation=False)
        assert core._sessions == {}  # type: ignore[attr-defined]  # noqa: SLF001

    asyncio.run(scenario())
