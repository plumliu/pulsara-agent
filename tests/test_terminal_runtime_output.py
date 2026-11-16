"""Terminal business values carry complete observations and truthful controls."""

from pulsara_agent.conversation_kernel.tool_runtime_terminal import (
    _terminal_execution_result,
)
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.terminal_process.models import TerminalResult, TerminalStatus


def test_complete_observation_keeps_every_line_and_no_process_state_confuses_tool_state():
    text = '中文🙂\\"\n' * 20_000
    physical = TerminalResult(
        TerminalStatus.ERROR, text, 7, "/workspace", process_id=random_id("process")
    )
    result = _terminal_execution_result(
        ToolCall("provider-call", "exec_process"), physical
    )
    assert result.status is ToolResultState.SUCCESS
    assert result.business_value.content[0].value == text
    assert result.business_value.public_fields["meta"] == {
        "process_status": "EXITED",
        "exit_code": 7,
        "cwd": "/workspace",
    }


def test_running_process_has_unknown_exit_code_instead_of_a_sentinel():
    physical = TerminalResult(
        TerminalStatus.RUNNING, "", -1, "/workspace", process_id=random_id("process")
    )
    result = _terminal_execution_result(
        ToolCall("provider-call", "exec_process"), physical
    )
    assert result.business_value.public_fields["meta"] == {
        "process_status": "RUNNING",
        "exit_code": None,
        "cwd": "/workspace",
    }
    assert result.business_value.content == ()


def test_control_result_does_not_consume_a_log_observation():
    physical = TerminalResult(
        TerminalStatus.RUNNING,
        "prior output\n",
        -1,
        "/workspace",
        process_id=random_id("process"),
    )
    callbacks = []
    result = _terminal_execution_result(
        ToolCall("provider-call", "manage_process"),
        physical,
        action="close_stdin",
        control_text="已关闭进程输入。",
        acceptance_succeeded=lambda: callbacks.append(True),
    )
    assert result.business_value.content[0].value == "已关闭进程输入。"
    assert result.acceptance_succeeded is None and callbacks == []


def test_cancelled_start_is_cancelled_even_if_process_identity_and_exit_are_known():
    physical = TerminalResult(
        TerminalStatus.KILLED,
        "obtained log",
        -9,
        "/workspace",
        process_id=random_id("process"),
        cancelled=True,
    )
    result = _terminal_execution_result(
        ToolCall("provider-call", "exec_process"), physical
    )
    assert result.status is ToolResultState.INTERRUPTED
    assert result.business_value.content[0].value == "obtained log"
    assert "已取消" in result.business_value.content[-1].value
