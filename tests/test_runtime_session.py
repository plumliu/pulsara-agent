import json

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime import RuntimeSession
from pulsara_agent.tools import ToolCall, build_core_tool_registry


CTX = EventContext(run_id="run:runtime", turn_id="turn:runtime", reply_id="reply:runtime")


def test_runtime_session_create_tool_executor_uses_shared_event_log(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "printf ok"}),
        event_context=CTX,
    )

    assert result.status is ToolResultState.SUCCESS
    assert [event.sequence for event in runtime.event_log.iter(reply_id="reply:runtime")] == [1, 2, 3]


def test_runtime_session_keeps_named_terminal_sessions_separate(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()

    code_result = executor.execute(
        ToolCall(id="call:code", name="terminal", arguments={"command": "cd src && pwd", "session_id": "code"}),
        event_context=CTX,
    )
    default_result = executor.execute(
        ToolCall(id="call:default", name="terminal", arguments={"command": "pwd"}),
        event_context=CTX,
    )

    code_payload = json.loads(code_result.output)
    default_payload = json.loads(default_result.output)
    assert code_payload["terminal_session_id"] == "code"
    assert code_payload["cwd"] == str(tmp_path / "src")
    assert default_payload["terminal_session_id"] == "default"
    assert default_payload["cwd"] == str(tmp_path)


def test_runtime_session_terminal_session_limit_and_validation(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)
    executor = runtime.create_tool_executor()

    invalid = executor.execute(
        ToolCall(id="call:bad", name="terminal", arguments={"command": "pwd", "session_id": "../bad"}),
        event_context=CTX,
    )
    assert invalid.status is ToolResultState.ERROR
    assert "terminal session_id must be" in json.loads(invalid.output)["error"]

    for name in ["a", "b", "c", "d"]:
        result = executor.execute(
            ToolCall(id=f"call:{name}", name="terminal", arguments={"command": "pwd", "session_id": name}),
            event_context=CTX,
        )
        assert result.status is ToolResultState.SUCCESS

    too_many = executor.execute(
        ToolCall(id="call:e", name="terminal", arguments={"command": "pwd", "session_id": "e"}),
        event_context=CTX,
    )
    assert too_many.status is ToolResultState.ERROR
    assert "terminal session limit reached" in json.loads(too_many.output)["error"]


def test_build_core_tool_registry_requires_runtime_session(tmp_path) -> None:
    runtime = RuntimeSession(tmp_path)

    registry = build_core_tool_registry(runtime)

    assert "terminal" in registry.names()
    with pytest.raises(TypeError, match="requires a RuntimeSession"):
        build_core_tool_registry(tmp_path)
