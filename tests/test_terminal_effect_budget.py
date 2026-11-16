"""Known terminal effects are executed once and reported as explicit business values."""

import shlex
import sys
from time import monotonic
import pytest
from pulsara_agent.conversation_kernel.tool_runtime import (
    _DirectTerminalProcessTool,
    _DirectTerminalTool,
    _execute_terminal_tool_call,
)
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_process.environment import TerminalEnvConfig
from pulsara_agent.terminal_process.manager import TerminalManager
from pulsara_agent.terminal_process.models import TerminalProcessOrigin

OWNER, SESSION = random_id("host"), random_id("session")
ORIGIN = TerminalProcessOrigin(random_id("turn"), "ROOT", SESSION)


@pytest.fixture
def manager(tmp_path):
    value = TerminalManager(tmp_path)
    value.activate_owner(OWNER)
    value.environment_owner.config = TerminalEnvConfig(enable_shell_snapshot=False)
    try:
        yield value
    finally:
        value.release_owner(OWNER, timeout_seconds=5)


def program(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def invoke(manager, name, arguments):
    tool = (
        _DirectTerminalTool if name == "exec_process" else _DirectTerminalProcessTool
    )(manager, OWNER, session_id=SESSION)
    return _execute_terminal_tool_call(
        tool,
        ToolCall("provider-call", name, arguments),
        None,
        ORIGIN,
        random_id("terminal-decision"),
        PermissionMode.BYPASS_PERMISSIONS,
        deadline_monotonic=monotonic() + 5,
    )


def text(result):
    return "".join(p.value for p in result.business_value.content if p.type == "text")


def test_spawned_command_effect_and_complete_result_are_observed_once(
    manager, tmp_path
):
    marker = tmp_path / "effects.txt"
    result = invoke(
        manager,
        "exec_process",
        {
            "command": program(
                f"open({str(marker)!r},'a').write('once\\n'); print('ACTUAL')"
            ),
            "yield_time_ms": 1000,
        },
    )
    assert result.status is ToolResultState.SUCCESS
    assert text(result) == "ACTUAL\n" and marker.read_text() == "once\n"
    assert result.business_value.public_fields["meta"]["exit_code"] == 0


def test_exact_input_is_not_implicitly_submitted_or_repeated(manager, tmp_path):
    marker = tmp_path / "input.txt"
    started = invoke(
        manager,
        "exec_process",
        {
            "command": program(
                "import sys; print('READY',flush=True); line=sys.stdin.readline(); "
                + f"open({str(marker)!r},'a').write(line); print('REPLY',flush=True)"
            ),
            "yield_time_ms": 100,
        },
    )
    target = started.business_value.public_fields["target"]
    started.acceptance_succeeded()
    pending = invoke(
        manager,
        "manage_process",
        {"action": "write", "target": target, "data": "once", "yield_time_ms": 50},
    )
    assert pending.status is ToolResultState.SUCCESS and not marker.exists()
    result = invoke(
        manager,
        "manage_process",
        {"action": "write", "target": target, "data": "\n", "yield_time_ms": 1000},
    )
    assert result.status is ToolResultState.SUCCESS and marker.read_text() == "once\n"
    assert text(result) == "REPLY\n"


def test_closed_input_is_a_known_failure_and_does_not_restart_process(manager):
    started = invoke(manager, "exec_process", {"command": "true"})
    target = started.business_value.public_fields["target"]
    result = invoke(
        manager,
        "manage_process",
        {"action": "write", "target": target, "data": "never"},
    )
    assert result.status is ToolResultState.ERROR
    assert result.business_value.public_fields["target"] == target
    assert "closed" in text(result).lower()
