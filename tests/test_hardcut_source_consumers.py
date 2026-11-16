"""Storage consumers keep complete sources and known effects on failure."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel import tool_execution
from pulsara_agent.conversation_kernel.html_resources import freeze_html_resources
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.conversation_kernel.visualization import read_visualization_text
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_output import ToolOutputUnavailableReason, ToolResultSaveError


def test_visualization_source_beyond_removed_blob_bound_keeps_bytes(tmp_path):
    body = b"<html>\r\n" + b"x" * (17 * 1024 * 1024) + b"\n</html>"
    path = tmp_path / "large.html"
    path.write_bytes(body)
    assert read_visualization_text(path, kind="HTML", allow_empty=False) == body


def test_html_resource_serialization_does_not_reintroduce_whole_blob_cap(tmp_path):
    body = b"<html><body>" + b"x" * (17 * 1024 * 1024) + b"</body></html>"

    class ImmediateIO:
        async def run(self, operation, *args, deadline_monotonic):
            return operation(*args, deadline_monotonic=deadline_monotonic)

    result = asyncio.run(freeze_html_resources(
        body, tmp_path / "source.html", io=ImmediateIO(), validator=None,
        deadline_monotonic=float("inf"),
    ))
    assert result == body


def invoke_code_mode_failure(monkeypatch, error):
    class Snapshot(SimpleNamespace):
        pass

    monkeypatch.setattr(tool_execution, "CodeModeExecutionSnapshot", Snapshot)
    borrow = SimpleNamespace(closed=False)

    def close():
        borrow.closed = True

    borrow.close = close
    executor = object.__new__(ToolBatchExecutor)
    executor._tools = SimpleNamespace(borrow_code_mode_surface=lambda *_args, **_kwargs: borrow)
    calls = []
    async def execute(**kwargs):
        calls.append(kwargs)
        raise error

    executor.execute = execute
    invocation = SimpleNamespace(
        snapshot=Snapshot(turn_id=random_id("turn"), tool_directory=object(),
            canonical_facts=object(), canonical_identity=object(),
            request=SimpleNamespace(session_id=random_id("session")),
            agent_invocation_snapshot=None, continuity_scope=object(), hook_scope=None),
        arguments={"path": "/actual/path"}, tool_name="write_file", sequence=1,
        source_exec_attempt_id=random_id("attempt"), cell_id=random_id("cell"),
    )
    result = asyncio.run(executor.invoke_code_mode(invocation))
    assert len(calls) == 1
    assert borrow.closed
    assert tool_execution.current_tool_invocation.get() is None
    return result


@pytest.mark.parametrize("reason", list(ToolOutputUnavailableReason))
def test_code_mode_save_failure_preserves_known_value_without_result_identity(monkeypatch, reason):
    known = {"meta": {"status": "SUCCESS"}, "structuredContent": None,
             "content": [{"type": "text", "value": "The write completed.\n"}]}
    cause = ConnectionError("write acknowledgement unavailable")
    error = ToolResultSaveError(reason=reason, known_value=known, result_state="SUCCESS", cause=cause)
    result = invoke_code_mode_failure(monkeypatch, error)
    assert result.value == known
    assert result.error_code == reason.value
    assert "不应重做业务操作" in result.error_message
    assert result.has_result
    assert result.result_id is None and result.result_entry_id is None
