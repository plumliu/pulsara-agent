"""Observable hard-cut contract for independent commands and process follow-up."""

from __future__ import annotations

import json
import asyncio
from dataclasses import replace
import shlex
import sys
from time import monotonic

import pytest
from pydantic import ValidationError

from pulsara_agent.conversation_kernel.tool_runtime import (
    DirectKernelToolPort,
    _DirectTerminalProcessTool,
    _DirectTerminalTool,
)
from pulsara_agent.conversation_kernel.io import (
    KernelSessionIO,
    PhysicalToolInvocationDisposition,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    model_tool_surface_fingerprint,
)
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.terminal_process.monitor import TerminalMonitorPolicy
from tests.support.round3 import prepare_test_direct_tool_surface
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.terminal import (
    parse_terminal_input,
    parse_terminal_process_input,
)
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_process.environment import TerminalEnvConfig
from pulsara_agent.terminal_process.manager import TerminalManager
from pulsara_agent.terminal_process.models import TerminalProcessOrigin


@pytest.fixture
def terminal(tmp_path):
    manager = TerminalManager(tmp_path)
    manager.activate_owner("host:cognitive")
    manager.environment_owner.config = TerminalEnvConfig(enable_shell_snapshot=False)
    try:
        yield manager
    finally:
        manager.release_owner("host:cognitive", timeout_seconds=5)


def start(manager, command, **kwargs):
    return _DirectTerminalTool(manager, "host:cognitive").execute(
        ToolCall("call:start", "terminal", {"command": command, **kwargs}),
        origin=TerminalProcessOrigin("turn:cognitive", "ROOT"),
        decision_attempt_id=f"decision:{monotonic()}",
        decision_deadline_monotonic=monotonic() + 5,
        effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )


def follow(manager, action, process_id, **kwargs):
    return _DirectTerminalProcessTool(manager, "host:cognitive").execute(
        ToolCall(
            "call:follow",
            "terminal_process",
            {
                "action": action,
                "process_id": process_id,
                **kwargs,
            },
        )
    )


def program(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def test_removed_inputs_are_rejected_and_windows_have_only_per_call_bounds():
    with pytest.raises(ValidationError):
        parse_terminal_input({"command": "true", "terminal_session_id": "default"})
    with pytest.raises(ValidationError):
        parse_terminal_process_input({"action": "log", "process_id": "proc_old"})
    for action in ("write", "submit"):
        assert (
            parse_terminal_process_input(
                {"action": action, "process_id": "p", "data": "x"}
            ).yield_time_ms
            == 1000
        )
        for value in (-1, 30001):
            with pytest.raises(ValidationError):
                parse_terminal_process_input(
                    {
                        "action": action,
                        "process_id": "p",
                        "data": "x",
                        "yield_time_ms": value,
                    }
                )


def test_commands_use_workspace_relative_workdirs_and_never_remember_cd(
    terminal, tmp_path
):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = json.loads(
        start(
            terminal, "cd ../two; export COGNITIVE_FLAG=inside; pwd", workdir="one"
        ).output
    )
    assert first["cwd"] == str(tmp_path / "one")
    assert first["output"].strip() == str(tmp_path / "two")
    second = json.loads(
        start(terminal, 'pwd; printf "%s" "${COGNITIVE_FLAG-unset}"').output
    )
    assert second["cwd"] == str(tmp_path)
    assert second["output"].splitlines() == [str(tmp_path), "unset"]
    # No shell trap is installed, and a subdirectory may be removed independently.
    assert json.loads(start(terminal, "trap").output)["output"] == ""
    assert list(tmp_path.glob(".pulsara-cwd-*")) == []
    for name in ("one", "two", "one", "two", "one"):
        assert start(terminal, "pwd", workdir=name).status is ToolResultState.SUCCESS


@pytest.mark.parametrize("tty", (False, True))
def test_input_observation_waits_past_echo_and_preserves_text_and_newlines(
    terminal, tty
):
    result = start(
        terminal,
        program(
            "import sys,time\nprint('READY',flush=True)\n"
            "for line in sys.stdin:\n"
            " if line.strip()=='quit': break\n"
            " time.sleep(.15)\n print('ANSWER:'+line.strip(),flush=True)\n"
        ),
        tty=tty,
        yield_time_ms=100,
    )
    pid = json.loads(result.output)["process_id"]
    before = monotonic()
    written = follow(terminal, "write", pid, data="hé", yield_time_ms=0)
    assert monotonic() - before < 0.8
    assert written.status is ToolResultState.SUCCESS
    assert "ANSWER:" not in json.loads(written.output)["output"]
    before = monotonic()
    submitted = follow(terminal, "submit", pid, data="llo", yield_time_ms=300)
    assert monotonic() - before >= 0.27  # PTY echo must not complete this window.
    payload = json.loads(submitted.output)
    assert submitted.status is ToolResultState.SUCCESS
    assert payload["status"] == "running"
    assert "ANSWER:héllo\n" in payload["output"]
    assert "READY\n" in payload["output"]  # Retained snapshot, not a new reply claim.
    before = monotonic()
    ended = follow(terminal, "submit", pid, data="quit", yield_time_ms=1000)
    assert monotonic() - before < 0.8
    assert json.loads(ended.output)["status"] == "success"


@pytest.mark.parametrize("action", ("poll", "wait"))
def test_reading_failed_process_is_a_successful_observation(terminal, action):
    initial = start(terminal, "printf FAILURE_OUTPUT; exit 7")
    assert initial.status is ToolResultState.ERROR
    pid = json.loads(initial.output)["process_id"]
    observed = follow(terminal, action, pid)
    data = json.loads(observed.output)
    assert observed.status is ToolResultState.SUCCESS
    assert data["status"] == "error" and data["exit_code"] == 7
    assert data["output"] == "FAILURE_OUTPUT"
    assert data["error"] is None
    assert observed.output_artifact_candidate.text == "FAILURE_OUTPUT"


def test_capacity_failure_is_unstarted_and_kill_is_successful_control(terminal):
    # Eight is the existing Host process-group/reader/retained-output admission
    # boundary, preserved by section 3.4 of the active cognitive-subtraction spec.
    pids = [
        json.loads(start(terminal, "sleep 30", yield_time_ms=0).output)["process_id"]
        for _ in range(8)
    ]
    rejected = json.loads(start(terminal, "printf MUST_NOT_RUN").output)
    assert rejected["status"] == "blocked"
    assert rejected["reason"] == "PROCESS_CAPACITY_EXHAUSTED"
    assert rejected["process_id"] is None and rejected["output"] == ""
    killed = follow(terminal, "kill", pids[0])
    assert killed.status is ToolResultState.SUCCESS
    assert json.loads(killed.output)["status"] == "killed"
    assert start(terminal, "printf CAPACITY_RELEASED").status is ToolResultState.SUCCESS


def test_cancellation_after_input_preserves_exact_result_and_does_not_resend(terminal):
    async def scenario():
        initial = start(
            terminal,
            program(
                "import sys,time\n"
                "for line in sys.stdin:\n print('ONCE:'+line.strip(),flush=True)\n"
            ),
            yield_time_ms=0,
        )
        pid = json.loads(initial.output)["process_id"]
        io = KernelSessionIO()

        def submit_once(*, deadline_monotonic):
            return follow(terminal, "submit", pid, data="input", yield_time_ms=300)

        pending = asyncio.create_task(
            io.run_tool_invocation(
                submit_once,
                deadline_monotonic=monotonic() + 5,
            )
        )
        try:
            # Observe the actual process receipt before cancelling the caller.
            deadline = monotonic() + 2
            while (
                "ONCE:input"
                not in json.loads(follow(terminal, "poll", pid).output)["output"]
            ):
                assert monotonic() < deadline
                await asyncio.sleep(0.005)
            pending.cancel()
            settled = await pending
            assert settled.caller_cancelled
            assert (
                settled.disposition is PhysicalToolInvocationDisposition.RETURNED_EXACT
            )
            assert settled.value.status is ToolResultState.SUCCESS
            output = json.loads(settled.value.output)
            assert output["output"] == "ONCE:input\n"
            assert output["status"] == "running"
            assert (
                json.loads(follow(terminal, "poll", pid).output)["output"]
                == "ONCE:input\n"
            )
        finally:
            await io.aclose(deadline_monotonic=monotonic() + 3)

    asyncio.run(scenario())


def test_stale_descriptor_is_refused_and_surface_handoff_keeps_live_owners(
    tmp_path, monkeypatch
):
    async def scenario():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:cognitive",
            session_id="session:cognitive",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(
            enable_shell_snapshot=False
        )

        class EmptySubagents:
            tool_names = ()

            async def freeze_compaction_handoff(self):
                return (), (
                    ("ACTIVE", 0),
                    ("PENDING_START", 0),
                    ("WAITING_DEPENDENCY", 0),
                )

        port.bind_subagent_port(EmptySubagents())
        captured = {}
        prepare = port.prepare_planned_tool_surface

        def capture(*, plan, builtin):
            captured.update(plan=plan, builtin=builtin)
            return prepare(plan=plan, builtin=builtin)

        monkeypatch.setattr(port, "prepare_planned_tool_surface", capture)
        try:
            first = prepare_test_direct_tool_surface(port)
            initial_bytes = tuple(
                spec.canonical_bytes for spec in first.model_surface.tool_specs
            )
            result = json.loads(
                start(
                    port._terminal, "printf HANDOFF; sleep 30", yield_time_ms=100
                ).output
            )
            pid = result["process_id"]
            coordinator = port.terminal_monitor_coordinator
            registration = coordinator.prepare_registration(
                process_id=pid,
                origin_turn_id="turn:cognitive",
                origin_attempt_id="attempt:monitor",
                origin_result_entry_id="entry:monitor",
                writer_generation=1,
                authorization_reference="policy:cognitive",
                policy=TerminalMonitorPolicy(),
            )
            coordinator.settle_registration(registration, committed=True)
            plan = captured["plan"]
            # Reproduce the removed, semantically different descriptor, including
            # calls whose arguments happen to be valid under both contracts.
            old_specs = tuple(
                replace(
                    spec,
                    description="Run a command using remembered session cwd.",
                    parameters=freeze_json(
                        {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string"},
                                "terminal_session_id": {"type": "string"},
                            },
                        }
                    ),
                    descriptor_fingerprint="old-terminal-cwd-contract",
                )
                if spec.name == "terminal"
                else spec
                for spec in plan.direct_tool_surface.tool_specs
            )
            stale_surface = replace(
                plan.direct_tool_surface,
                tool_specs=old_specs,
                surface_fingerprint=model_tool_surface_fingerprint(
                    ModelInputScopeKind.ROOT, old_specs
                ),
            )
            stale_plan = replace(
                plan,
                selection=replace(plan.selection, direct_tool_surface=stale_surface),
            )
            with pytest.raises(
                RuntimeError, match="planned builtin descriptor drifted"
            ):
                prepare(plan=stale_plan, builtin=captured["builtin"])
            assert (
                tuple(spec.canonical_bytes for spec in first.model_surface.tool_specs)
                == initial_bytes
            )
            handoff = await port.freeze_compaction_runtime_handoff(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                maximum_utf8_bytes=8192,
            )
            payload = json.loads(handoff.full_text)
            assert payload["terminal_processes"][0]["process_id"] == pid
            assert "terminal_session_id" not in handoff.full_text
            assert (
                payload["terminal_monitors"][0]["monitor_id"] == registration.monitor_id
            )
            successor = prepare_test_direct_tool_surface(port)
            borrow = port.borrow_tool_surface(successor)
            try:
                observed = follow(
                    port._terminal, "poll", pid, since_cursor=result["output_cursor"]
                )
                assert json.loads(observed.output)["status"] == "running"
                assert (
                    coordinator.list_current()[0]["monitor_id"]
                    == registration.monitor_id
                )
            finally:
                borrow.close()
            (tmp_path / "child").mkdir()
            start(port._terminal, "cd child; pwd")
            assert port.snapshot_workspace_root() == tmp_path.resolve()
        finally:
            await port.aclose()

    asyncio.run(scenario())
