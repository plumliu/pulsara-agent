"""Real terminal independence, exact writes, cancellation and prefix continuity."""

from tests.support.model_config import scheduled_task_service
from pulsara_agent.identity import random_id
import asyncio
from dataclasses import replace
import json
import shlex
import sys
from time import monotonic
import pytest
from pulsara_agent.conversation_kernel.tool_runtime import (
    DirectKernelToolPort,
    _DirectTerminalProcessTool,
    _DirectTerminalTool,
    _DirectTerminalQueryTool,
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
from tests.support.round3 import prepare_test_direct_tool_surface
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_process.environment import TerminalEnvConfig
from pulsara_agent.terminal_process.manager import TerminalManager
from pulsara_agent.terminal_process.models import TerminalProcessOrigin

OWNER = "host:45aj6sixiaarr5ta455q3trmwi"
SESSION = "session:liqqsnm2qenssqznkop23s5kji"


@pytest.fixture
def terminal(tmp_path):
    manager = TerminalManager(tmp_path)
    manager.activate_owner(OWNER)
    manager.environment_owner.config = TerminalEnvConfig(enable_shell_snapshot=False)
    try:
        yield manager
    finally:
        manager.release_owner(OWNER, timeout_seconds=5)


def start(manager, command, **kwargs):
    return _DirectTerminalTool(manager, OWNER, session_id=SESSION).execute(
        ToolCall("provider-start", "exec_process", {"command": command, **kwargs}),
        origin=TerminalProcessOrigin(random_id("turn"), "ROOT", SESSION),
        decision_attempt_id=random_id("terminal-decision"),
        decision_deadline_monotonic=monotonic() + 5,
        effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
    )


def follow(manager, action, process_id, **kwargs):
    return _DirectTerminalProcessTool(manager, OWNER, session_id=SESSION).execute(
        ToolCall(
            "provider-follow",
            "manage_process",
            {
                "action": action,
                "target": {"kind": "PROCESS", "process_id": process_id},
                **kwargs,
            },
        )
    )


def text(result):
    return "".join(p.value for p in result.business_value.content if p.type == "text")


def program(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def test_commands_use_workspace_relative_workdirs_and_never_remember_cd(
    terminal, tmp_path
):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = start(
        terminal, "cd ../two; export COGNITIVE_FLAG=inside; pwd", workdir="one"
    )
    assert first.business_value.public_fields["meta"]["cwd"] == str(tmp_path / "one")
    assert text(first).strip() == str(tmp_path / "two")
    second = start(terminal, 'pwd; printf "%s" "${COGNITIVE_FLAG-unset}"')
    assert second.business_value.public_fields["meta"]["cwd"] == str(tmp_path)
    assert text(second).splitlines() == [str(tmp_path), "unset"]
    assert text(start(terminal, "trap")) == ""
    assert list(tmp_path.glob(".pulsara-cwd-*")) == []
    for name in ("one", "two", "one", "two", "one"):
        assert start(terminal, "pwd", workdir=name).status is ToolResultState.SUCCESS


@pytest.mark.parametrize("tty", (False, True))
def test_input_observation_waits_past_echo_and_preserves_exact_newlines(terminal, tty):
    initial = start(
        terminal,
        program(
            "import sys,time\nprint('READY',flush=True)\nfor line in sys.stdin:\n if line.strip()=='quit':break\n time.sleep(.15)\n print('ANSWER:'+line.strip(),flush=True)\n"
        ),
        tty=tty,
        yield_time_ms=100,
    )
    pid = initial.business_value.public_fields["target"]["process_id"]
    initial.acceptance_succeeded()
    written = follow(terminal, "write", pid, data="hé", yield_time_ms=0)
    assert "ANSWER:" not in text(written)
    before = monotonic()
    result = follow(terminal, "write", pid, data="llo\n", yield_time_ms=300)
    assert monotonic() - before >= 0.27
    assert result.business_value.public_fields["meta"]["process_status"] == "RUNNING"
    assert "ANSWER:héllo\n" in text(result) and "READY\n" not in text(result)
    result.acceptance_succeeded()
    before = monotonic()
    ended = follow(terminal, "write", pid, data="quit\n", yield_time_ms=1000)
    assert monotonic() - before < 0.8
    assert ended.business_value.public_fields["meta"]["process_status"] == "EXITED"


def test_cancellation_after_input_preserves_exact_result_and_does_not_resend(terminal):
    async def scenario():
        initial = start(
            terminal,
            program(
                "import sys\nfor line in sys.stdin:print('ONCE:'+line.strip(),flush=True)"
            ),
            yield_time_ms=0,
        )
        pid = initial.business_value.public_fields["target"]["process_id"]
        io = KernelSessionIO()

        def write_once(*, deadline_monotonic):
            return follow(terminal, "write", pid, data="input\n", yield_time_ms=300)

        pending = asyncio.create_task(
            io.run_tool_invocation(write_once, deadline_monotonic=monotonic() + 5)
        )
        try:
            deadline = monotonic() + 2
            while "ONCE:input" not in text(
                follow(terminal, "wait", pid, timeout_seconds=0)
            ):
                assert monotonic() < deadline
                await asyncio.sleep(0.005)
            pending.cancel()
            settled = await pending
            assert (
                settled.caller_cancelled
                and settled.disposition
                is PhysicalToolInvocationDisposition.RETURNED_EXACT
            )
            assert text(settled.value) == "ONCE:input\n"
            assert (
                text(follow(terminal, "wait", pid, timeout_seconds=0)) == "ONCE:input\n"
            )
        finally:
            await io.aclose(deadline_monotonic=monotonic() + 3)

    asyncio.run(scenario())


def test_process_target_is_reused_for_observation_input_and_termination(terminal):
    initial = start(
        terminal, program("import sys;print(sys.stdin.read(),end='')"), yield_time_ms=0
    )
    target = initial.business_value.public_fields["target"]
    pid = target["process_id"]
    inventory = _DirectTerminalQueryTool(terminal, OWNER, session_id=SESSION).execute(
        ToolCall("provider-query", "query_processes", {})
    )
    assert inventory.business_value.content[0].value["target"] == target
    for data in ("alpha", "beta\n"):
        assert (
            follow(
                terminal, "write", pid, data=data, yield_time_ms=0
            ).business_value.public_fields["target"]
            == target
        )
    follow(terminal, "close_stdin", pid)
    completed = follow(terminal, "wait", pid, timeout_seconds=5)
    assert completed.business_value.public_fields["meta"]["process_status"] == "EXITED"
    assert text(completed) == "alphabeta\n"
    assert follow(terminal, "kill", pid).status is ToolResultState.SUCCESS


def test_stale_descriptor_is_refused_and_surface_handoff_keeps_live_owners(
    tmp_path, monkeypatch
):
    async def scenario():
        port = DirectKernelToolPort(
            scheduled_tasks=scheduled_task_service(),
            workspace_root=tmp_path,
            host_owner_id="host:45aj6sixiaarr5ta455q3trmwi",
            session_id="session:liqqsnm2qenssqznkop23s5kji",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port._terminal.environment_owner.config = TerminalEnvConfig(
            enable_shell_snapshot=False
        )

        class EmptySubagents:
            tool_names = ()

            async def freeze_compaction_handoff(self, *, cursor=None):
                return ()

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
            result = start(
                port._terminal, "printf HANDOFF; sleep 30", yield_time_ms=100
            ).business_value.public_fields
            pid = result["target"]["process_id"]
            plan = captured["plan"]
            # A changed descriptor cannot mutate an already installed surface.
            old_specs = tuple(
                replace(
                    spec,
                    description="A changed descriptor.",
                    parameters=freeze_json(
                        {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string"},
                                "terminal_session_id": {"type": "string"},
                            },
                        }
                    ),
                    descriptor_fingerprint="changed-descriptor",
                )
                if spec.name == "exec_process"
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
                session_id=port._session_id,
                work_turn_id="turn:gc7de6qlnst5d77yis7utsj2xu",
            )
            payload = json.loads(handoff.text.split("\n", 1)[0])
            assert payload["terminal_processes"][0]["target"]["process_id"] == pid
            assert "terminal_session_id" not in handoff.text
            successor = prepare_test_direct_tool_surface(port)
            borrow = port.borrow_tool_surface(successor)
            try:
                observed = follow(port._terminal, "wait", pid, timeout_seconds=0)
                assert (
                    observed.business_value.public_fields["meta"]["process_status"]
                    == "RUNNING"
                )
            finally:
                borrow.close()
            (tmp_path / "child").mkdir()
            start(port._terminal, "cd child; pwd")
            assert port.snapshot_workspace_root() == tmp_path.resolve()
        finally:
            await port.aclose()

    asyncio.run(scenario())
