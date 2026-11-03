"""Terminal receipt admission precedes effects; actual results survive a lost share."""

import json
import shlex
import sys
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.tool_runtime import (
    _DirectTerminalProcessTool, _DirectTerminalTool, _execute_terminal_tool_call,
)
from pulsara_agent.identity import derive_id, random_id, require_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.terminal_process.environment import TerminalEnvConfig
from pulsara_agent.terminal_process.manager import TerminalManager
from pulsara_agent.terminal_process.models import TerminalProcessOrigin


OWNER = derive_id("host", "terminal-effect-budget", "owner")
ORIGIN = TerminalProcessOrigin(derive_id("turn", "terminal-effect-budget", "turn"), "ROOT")
TARGET = {
    "kind": "SESSION_ENTRY",
    "session_id": derive_id("session", "terminal-effect-budget", "session"),
    "entry_id": derive_id("entry", "terminal-effect-budget", "result"),
}


@pytest.fixture
def manager(tmp_path):
    terminal = TerminalManager(tmp_path)
    terminal.activate_owner(OWNER)
    terminal.environment_owner.config = TerminalEnvConfig(enable_shell_snapshot=False)
    try:
        yield terminal
    finally:
        terminal.release_owner(OWNER, timeout_seconds=5)


def _program(script):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}"


def _invoke(manager, name, arguments, *, accepts=None):
    call = ToolCall(
        "provider:terminal-budget", name, arguments,
        page_budget=ToolResultPageBudget("provider:terminal-budget", TARGET, accepts),
    )
    tool = (_DirectTerminalTool if name == "exec_process" else _DirectTerminalProcessTool)(manager, OWNER)
    return _execute_terminal_tool_call(
        tool, call, None, ORIGIN, random_id("terminal-decision"),
        PermissionMode.BYPASS_PERMISSIONS, deadline_monotonic=monotonic() + 5,
    )


def test_too_small_receipt_has_real_identity_but_never_spawns(manager, monkeypatch):
    import pulsara_agent.terminal_process.manager as process_module

    observed, spawns = [], []
    original_popen = process_module.subprocess.Popen

    def deny(body, _provenance):
        observed.append(json.loads(body))
        return False

    def forbidden_spawn(*args, **kwargs):
        if "printf SHOULD_NOT_START" in args[0]:
            spawns.append((args, kwargs))
            raise AssertionError("insufficient minimum receipt must precede command Popen")
        # The existing environment owner validates the bundled rg executable
        # with --version; this is not the requested command's creation boundary.
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_spawn)
    result = _invoke(manager, "exec_process", {"command": "printf SHOULD_NOT_START"}, accepts=deny)
    assert result.status is ToolResultState.ERROR
    assert json.loads(result.output) == {
        "status": "resource_boundary", "error": "RESULT_BUDGET_EXCEEDED", "effect_applied": False,
    }
    require_id(observed[0]["target"]["process_id"], "process")
    assert "output_cursor" in observed[0] and "read" in observed[0]
    assert spawns == []
    assert manager.process_registry._states == {}
    assert manager.process_registry._decision_attempts == {}
    assert manager.process_registry._launching_by_owner == {}


@pytest.mark.parametrize("action", ("write", "submit", "close_stdin", "kill"))
def test_denied_control_does_not_write_close_or_kill(manager, tmp_path, action):
    marker = tmp_path / "input.txt"
    command = _program(
        "import sys; print('READY',flush=True); line=sys.stdin.readline(); "
        f"open({str(marker)!r},'a').write(line); print('DONE',flush=True)"
    )
    started = _invoke(manager, "exec_process", {"command": command, "yield_time_ms": 0})
    target = json.loads(started.output)["target"]
    arguments = {"action": action, "target": target}
    if action in {"write", "submit"}:
        arguments.update(data="forbidden\n", yield_time_ms=0)
    denied = _invoke(manager, "manage_process", arguments, accepts=lambda *_: False)
    assert json.loads(denied.output)["effect_applied"] is False
    state = manager.process_registry._states[target["process_id"]]
    assert state.process.poll() is None
    assert state.stdin_closed is False and not state.stdin.closed
    assert not marker.exists()

    actual = _invoke(manager, "manage_process", {
        "action": "submit", "target": target, "data": "accepted", "yield_time_ms": 1_000,
    })
    assert json.loads(actual.output)["status"] == "success"
    assert marker.read_text() == "accepted\n"


def test_spawned_command_settles_once_when_actual_receipt_exceeds_its_share(manager, tmp_path):
    marker = tmp_path / "effects.txt"
    seen = []

    def accepts(body, _ids):
        seen.append(json.loads(body))
        return not marker.exists()

    result = _invoke(manager, "exec_process", {
        "command": _program(f"open({str(marker)!r},'a').write('once\\n'); print('ACTUAL')"),
        "yield_time_ms": 1_000,
    }, accepts=accepts)
    value = json.loads(result.output)
    assert result.status is ToolResultState.SUCCESS
    assert value["status"] == "success" and value["exit_code"] == 0
    assert value["output"] == result.output_body_candidate.text == "ACTUAL\n"
    assert marker.read_text() == "once\n"
    assert "UNKNOWN" not in result.output
    prepared = [item for item in seen if item["exit_code"] == -(1 << 31)]
    assert {item["status"] for item in prepared} == {"running", "success", "error", "timeout", "killed"}
    assert all(item["target"] == value["target"] for item in prepared)


def test_submitted_input_settles_once_when_its_actual_reply_loses_the_share(manager, tmp_path):
    marker = tmp_path / "input-once.txt"
    started = _invoke(manager, "exec_process", {
        "command": _program(
            "import sys; print('READY',flush=True); line=sys.stdin.readline(); "
            f"open({str(marker)!r},'a').write(line); print('REPLY',flush=True)"
        ), "yield_time_ms": 0,
    })
    target = json.loads(started.output)["target"]
    state = manager.process_registry._states[target["process_id"]]
    state.output.wait_for_revision(0, 2)
    result = _invoke(manager, "manage_process", {
        "action": "submit", "target": target, "data": "once", "yield_time_ms": 1_000,
    }, accepts=lambda *_: not marker.exists())
    assert result.status is ToolResultState.SUCCESS
    assert json.loads(result.output)["status"] == "success"
    assert result.output_body_candidate.text == "REPLY\n"
    assert marker.read_text() == "once\n"


def test_closed_input_is_known_unapplied_not_an_unknown_effect(manager):
    started = _invoke(manager, "exec_process", {"command": "true"})
    target = json.loads(started.output)["target"]
    rejected = _invoke(manager, "manage_process", {
        "action": "write", "target": target, "data": "never",
    })
    assert json.loads(rejected.output) == {
        "status": "error", "error": "PROCESS_STDIN_CLOSED", "effect_applied": False,
    }
