import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent import cli
from pulsara_agent.capability import LocalSkillProvider, LocalSkillResolver
from pulsara_agent.capability.bundled_skills import (
    reset_bundled_skill as real_reset_bundled_skill,
    sync_bundled_skills as real_sync_bundled_skills,
)
from pulsara_agent.host import HostWorkspaceInput
from pulsara_agent.message import ToolCallBlock, ToolCallState
from pulsara_agent.runtime import PendingApproval, PendingPlanInteraction
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.runtime.plan import PlanWorkflowState
from pulsara_agent.runtime.state import LoopStatus
from tests.support.runtime_session import in_memory_runtime_session


class FakeResult:
    status = LoopStatus.FINISHED
    stop_reason = "final"
    final_text = "fake final"


class FakeSession:
    host_session_id = "host:fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.active_skill_names: list[frozenset[str] | None] = []
        self.plan_state = PlanWorkflowState()
        self.enter_plan_reasons: list[str] = []

    async def run_turn(self, prompt: str, *, active_skill_names=None):
        self.prompts.append(prompt)
        self.active_skill_names.append(active_skill_names)
        return FakeResult()

    def get_pending_approval(self):
        return None

    def get_pending_interaction(self):
        return None

    def enter_plan(self, *, reason: str = ""):
        self.enter_plan_reasons.append(reason)
        self.plan_state.begin(
            source="user",
            previous_mode=PermissionMode.BYPASS_PERMISSIONS,
            previous_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
            reason=reason,
            pending_entry_audit=True,
        )
        return preset_to_policy(PermissionMode.READ_ONLY)

    async def stop_current_turn(self):
        return None


class PendingFakeSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions = []
        self._pending = PendingApproval(
            approval_id="approval:test",
            host_session_id=self.host_session_id,
            runtime_session_id="runtime:test",
            run_id="run:test",
            turn_id="turn:test",
            reply_id="reply:test",
            tool_calls=(
                ToolCallBlock(id="call:danger", name="terminal", input="{}", state=ToolCallState.ASKING),
            ),
        )

    def get_pending_approval(self):
        return self._pending

    async def resolve_approval(self, resolution):
        self.resolutions.append(resolution)
        self._pending = None
        return FakeResult()

    async def stop_current_turn(self):
        self._pending = None
        result = FakeResult()
        result.status = LoopStatus.ABORTED
        result.stop_reason = "aborted"
        return result


class PendingPlanFakeSession(FakeSession):
    def __init__(self, *, kind: str = "question") -> None:
        super().__init__()
        self.resolutions = []
        self._pending = PendingPlanInteraction(
            interaction_id="plan_interaction:test",
            kind=kind,
            host_session_id=self.host_session_id,
            runtime_session_id="runtime:test",
            run_id="run:test",
            turn_id="turn:test",
            reply_id="reply:test",
            tool_call_id="call:plan",
            question_id="plan_question:test" if kind == "question" else None,
            question="Scope?" if kind == "question" else "",
            exit_request_id="plan_exit:test" if kind == "exit" else None,
            plan_text="draft" if kind == "exit" else "",
            summary="draft summary" if kind == "exit" else "",
        )

    def get_pending_interaction(self):
        return self._pending

    async def resolve_plan_interaction(self, resolution):
        self.resolutions.append(resolution)
        self._pending = None
        return FakeResult()


class FakeCore:
    instances: list["FakeCore"] = []

    def __init__(self, *, settings):
        self.settings = settings
        self.session = FakeSession()
        self.workspace_input: HostWorkspaceInput | None = None
        self.closed: list[str] = []
        self.shutdown_called = False
        self.__class__.instances.append(self)

    async def open_session(self, workspace_input, *, model_role, permission_policy=None):
        self.workspace_input = workspace_input
        self.model_role = model_role
        self.permission_policy = permission_policy
        return self.session

    async def close_session(self, host_session_id: str):
        self.closed.append(host_session_id)

    async def list_sessions(self):
        return []

    async def list_workspace_supervisors(self):
        return []

    async def shutdown(self):
        self.shutdown_called = True
        if self.session.host_session_id not in self.closed:
            await self.close_session(self.session.host_session_id)


@pytest.fixture
def inspect_wiring(monkeypatch):
    monkeypatch.setattr(
        cli.PulsaraSettings,
        "from_env",
        classmethod(lambda cls, prefix="PULSARA": object()),
    )

    def _build(_settings, workspace_root, **_kwargs):
        return SimpleNamespace(runtime_session=in_memory_runtime_session(workspace_root))

    monkeypatch.setattr(cli, "build_durable_runtime_wiring", _build)


def test_cli_host_run_uses_host_core_with_transient_workspace(monkeypatch, tmp_path) -> None:
    FakeCore.instances.clear()
    sync_calls = []
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: sync_calls.append("sync"))
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace-kind",
            "transient",
            "--workspace",
            str(tmp_path),
            "--model-role",
            "flash",
            "--skill",
            "review-pr",
            "say hi",
        ]
    )

    result = asyncio.run(cli._host_run(args))

    assert result.final_text == "fake final"
    core = FakeCore.instances[0]
    assert core.workspace_input is not None
    assert core.workspace_input.workspace_kind == "transient"
    assert core.workspace_input.workspace_root == tmp_path
    assert core.session.prompts == ["say hi"]
    assert core.session.active_skill_names == [frozenset({"review-pr"})]
    assert core.closed == ["host:fake"]
    assert core.shutdown_called is True
    assert sync_calls == ["sync"]


def test_cli_host_run_rejects_removed_ephemeral_workspace_kind() -> None:
    # The `ephemeral` alias is hard-cut: argparse no longer accepts it.
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["host", "run", "--workspace-kind", "ephemeral", "--workspace", ".", "say hi"]
        )


def test_cli_rejects_removed_demo_ledger_command() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["demo-ledger"])


def test_cli_host_run_uses_production_host_core_without_backend_switch(monkeypatch, tmp_path) -> None:
    FakeCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace",
            str(tmp_path),
            "--model-role",
            "flash",
            "say hi",
        ]
    )

    asyncio.run(cli._host_run(args))

    assert len(FakeCore.instances) == 1


@pytest.mark.parametrize("removed_flag", ["--in-memory", "--durable"])
def test_cli_host_run_rejects_removed_runtime_backend_flags(tmp_path, removed_flag) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "host",
                "run",
                removed_flag,
                "--workspace",
                str(tmp_path),
                "say hi",
            ]
        )


def test_cli_host_run_threads_explicit_permission_policy(monkeypatch, tmp_path) -> None:
    class PolicyCapturingCore(FakeCore):
        async def open_session(self, workspace_input, *, model_role, permission_policy=None):
            self.workspace_input = workspace_input
            self.model_role = model_role
            self.permission_policy = permission_policy
            return self.session

    PolicyCapturingCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", PolicyCapturingCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace",
            str(tmp_path),
            "--permission-profile",
            "workspace_guarded",
            "--approval-policy",
            "risky_only",
            "--terminal-access",
            "off",
            "say hi",
        ]
    )

    result = asyncio.run(cli._host_run(args))

    assert result.final_text == "fake final"
    core = PolicyCapturingCore.instances[0]
    assert core.permission_policy.profile.value == "workspace_guarded"
    assert core.permission_policy.approval.value == "risky_only"
    assert core.permission_policy.terminal.value == "off"


def test_cli_host_run_returns_pending_approval_summary_for_one_shot(monkeypatch, tmp_path) -> None:
    class PendingCore(FakeCore):
        def __init__(self, *, settings):
            super().__init__(settings=settings)
            self.session = PendingFakeSession()

    PendingCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", PendingCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace",
            str(tmp_path),
            "--permission-profile",
            "trusted_host",
            "--approval-policy",
            "on_request",
            "--terminal-access",
            "ask",
            "danger",
        ]
    )

    result = asyncio.run(cli._host_run(args))

    assert result["status"] == "waiting_user"
    assert result["pending_approval"]["approval_id"] == "approval:test"
    assert result["pending_approval"]["tool_calls"][0]["id"] == "call:danger"
    core = PendingCore.instances[0]
    assert core.permission_policy.profile.value == "trusted_host"
    assert core.permission_policy.approval.value == "on_request"
    assert core.permission_policy.terminal.value == "ask"
    assert core.closed == ["host:fake"]
    assert core.shutdown_called is True


def test_cli_host_repl_approval_commands_show_and_resolve_pending(monkeypatch, tmp_path, capsys) -> None:
    class PendingCore(FakeCore):
        def __init__(self, *, settings):
            super().__init__(settings=settings)
            self.session = PendingFakeSession()

    PendingCore.instances.clear()
    inputs = iter([":approval", ":approve", "quit"])
    monkeypatch.setattr(cli, "HostCore", PendingCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path)])

    asyncio.run(cli._host_repl(args))

    core = PendingCore.instances[0]
    session = core.session
    assert session.resolutions[0].approval_id == "approval:test"
    assert session.resolutions[0].decisions[0].tool_call_id == "call:danger"
    assert session.resolutions[0].decisions[0].confirmed is True
    assert core.closed == ["host:fake"]
    assert core.shutdown_called is True
    out = capsys.readouterr().out
    assert '"approval_id": "approval:test"' in out
    assert "fake final" in out


def test_cli_host_repl_plan_command_enters_plan_without_running_prompt(monkeypatch, tmp_path, capsys) -> None:
    FakeCore.instances.clear()
    inputs = iter([":plan inspect first", "quit"])
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path)])

    asyncio.run(cli._host_repl(args))

    session = FakeCore.instances[0].session
    assert session.enter_plan_reasons == ["inspect first"]
    assert session.prompts == []
    out = capsys.readouterr().out
    assert '"pending_entry_audit": true' in out


def test_cli_host_repl_answers_pending_plan_question(monkeypatch, tmp_path, capsys) -> None:
    class PendingPlanCore(FakeCore):
        def __init__(self, *, settings):
            super().__init__(settings=settings)
            self.session = PendingPlanFakeSession(kind="question")

    PendingPlanCore.instances.clear()
    inputs = iter([":interaction", ":answer runtime", "quit"])
    monkeypatch.setattr(cli, "HostCore", PendingPlanCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path)])

    asyncio.run(cli._host_repl(args))

    session = PendingPlanCore.instances[0].session
    assert session.resolutions[0].interaction_id == "plan_interaction:test"
    assert session.resolutions[0].answer_text == "runtime"
    out = capsys.readouterr().out
    assert '"kind": "question"' in out
    assert "fake final" in out


def test_cli_host_repl_approves_pending_plan_exit(monkeypatch, tmp_path) -> None:
    class PendingPlanCore(FakeCore):
        def __init__(self, *, settings):
            super().__init__(settings=settings)
            self.session = PendingPlanFakeSession(kind="exit")

    PendingPlanCore.instances.clear()
    inputs = iter([":approve-plan", "quit"])
    monkeypatch.setattr(cli, "HostCore", PendingPlanCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path)])

    asyncio.run(cli._host_repl(args))

    resolution = PendingPlanCore.instances[0].session.resolutions[0]
    assert resolution.interaction_id == "plan_interaction:test"
    assert resolution.decision == "approve"


def test_cli_host_repl_stop_aborts_pending_approval(monkeypatch, tmp_path, capsys) -> None:
    class PendingCore(FakeCore):
        def __init__(self, *, settings):
            super().__init__(settings=settings)
            self.session = PendingFakeSession()

    PendingCore.instances.clear()
    inputs = iter([":stop", "quit"])
    monkeypatch.setattr(cli, "HostCore", PendingCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path)])

    asyncio.run(cli._host_repl(args))

    core = PendingCore.instances[0]
    assert core.session.get_pending_approval() is None
    assert core.closed == ["host:fake"]
    out = capsys.readouterr().out
    assert '"status": "aborted"' in out


def test_cli_host_repl_runs_bundled_sync_before_opening_session(monkeypatch, tmp_path) -> None:
    FakeCore.instances.clear()
    sync_calls = []
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: sync_calls.append("sync"))
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path), "--model-role", "flash"])

    asyncio.run(cli._host_repl(args))

    core = FakeCore.instances[0]
    assert sync_calls == ["sync"]
    assert core.workspace_input is not None
    assert core.workspace_input.workspace_root == tmp_path
    assert core.closed == ["host:fake"]


def test_cli_host_repl_ctrl_c_clears_input_without_closing_session(monkeypatch, tmp_path, capsys) -> None:
    class FakePrompt:
        def __init__(self) -> None:
            self.responses = iter([KeyboardInterrupt(), ":help", "hello", "quit"])
            self.messages = []

        async def read_line(self, message: str) -> str:
            self.messages.append(message)
            response = next(self.responses)
            if isinstance(response, BaseException):
                raise response
            return response

    FakeCore.instances.clear()
    prompt = FakePrompt()
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: None)
    monkeypatch.setattr(cli, "build_repl_prompt", lambda **_kwargs: prompt)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "repl", "--workspace", str(tmp_path)])

    asyncio.run(cli._host_repl(args))

    core = FakeCore.instances[0]
    assert prompt.messages == ["pulsara> ", "pulsara> ", "pulsara> ", "pulsara> "]
    assert core.session.prompts == ["hello"]
    assert core.shutdown_called is True
    output = capsys.readouterr().out
    assert "^C" in output
    assert "Ctrl-R search" in output


def test_cli_host_run_continues_when_bundled_sync_fails(monkeypatch, tmp_path, capsys) -> None:
    FakeCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))

    def _fail_sync():
        raise RuntimeError("sync boom")

    monkeypatch.setattr(cli, "sync_bundled_skills", _fail_sync)
    parser = cli.build_parser()
    args = parser.parse_args(["host", "run", "--workspace", str(tmp_path), "--model-role", "flash", "say hi"])

    result = asyncio.run(cli._host_run(args))

    assert result.final_text == "fake final"
    assert "bundled skill sync failed: sync boom" in capsys.readouterr().err
    assert FakeCore.instances[0].closed == ["host:fake"]


def test_cli_host_inspect_prints_host_process_recovery_scope_and_skills(
    monkeypatch, tmp_path, inspect_wiring
) -> None:
    FakeCore.instances.clear()
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "pulsara-home"))
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(
        cli,
        "_best_effort_sync_bundled_skills",
        lambda: (_ for _ in ()).throw(AssertionError("inspect must not sync bundled skills")),
    )
    monkeypatch.setattr(
        cli,
        "LocalSkillResolver",
        lambda: LocalSkillResolver(provider=LocalSkillProvider(include_user_skills=False)),
    )
    skill_dir = tmp_path / ".agents" / "skills" / "review-pr"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: review-pr
description: Review pull requests.
provides_tools:
  - read_file
  - not_a_tool
---
# Review PR
""",
        encoding="utf-8",
    )

    parser = cli.build_parser()
    args = parser.parse_args(["host", "inspect", "--workspace", str(tmp_path)])

    snapshot = asyncio.run(cli._host_inspect(args))

    assert snapshot["inspect_kind"] == "static_workspace_capability"
    assert "sessions" not in snapshot
    assert "workspace_supervisors" not in snapshot
    assert snapshot["recovery_scope"] == "host_process"
    assert snapshot["workspace"]["workspace_kind"] == "project"
    assert snapshot["workspace"]["workspace_root"] == str(tmp_path)
    assert snapshot["skills"] == [
        {
            "name": "review-pr",
            "description": "Review pull requests.",
            "when_to_use": None,
            "location": ".agents/skills/review-pr/SKILL.md",
            "provides_tools": ["read_file"],
        }
    ]
    assert "read_file" in snapshot["tools"]
    # Visible-but-blocked: gate is the sole authority, so even under the
    # read-only inspect mode the tools stay registered/visible.
    assert "write_file" in snapshot["tools"]
    assert "terminal" in snapshot["tools"]
    assert snapshot["current_mode"] == "read-only"
    assert snapshot["memory"] == {
        "graph_id": "graph:user/u_local",
        "tools_enabled": [],
        "read_scopes": snapshot["workspace"]["read_scopes"],
        "allowed_write_scopes": snapshot["workspace"]["allowed_write_scopes"],
    }
    assert snapshot["permissions"] == {
        "profile": "read_only",
        "approval_policy": "on_request",
        "terminal_access": "off",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "file_tools": "workspace_only",
            "terminal": "off",
        },
    }
    assert [diagnostic["code"] for diagnostic in snapshot["capability_diagnostics"]] == [
        "skill_unknown_tool_reference"
    ]
    assert "bundled_skills" in snapshot
    assert not (tmp_path / "pulsara-home" / "skills").exists()
    assert FakeCore.instances == []


def test_cli_host_inspect_can_report_explicit_trusted_host_policy(
    monkeypatch, tmp_path, inspect_wiring
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "pulsara-home"))
    monkeypatch.setattr(
        cli,
        "LocalSkillResolver",
        lambda: LocalSkillResolver(provider=LocalSkillProvider(include_user_skills=False)),
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "inspect",
            "--workspace",
            str(tmp_path),
            "--permission-profile",
            "trusted_host",
            "--approval-policy",
            "never",
            "--terminal-access",
            "allow",
        ]
    )

    snapshot = asyncio.run(cli._host_inspect(args))

    assert {"edit_file", "write_file", "terminal", "terminal_process"}.issubset(snapshot["tools"])
    assert snapshot["permissions"]["profile"] == "trusted_host"
    assert snapshot["permissions"]["approval_policy"] == "never"
    assert snapshot["permissions"]["terminal_access"] == "allow"
    assert snapshot["permissions"]["filesystem"]["terminal"] == "host_shell"


def test_cli_host_inspect_can_report_terminal_ask_policy(
    monkeypatch, tmp_path, inspect_wiring
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "pulsara-home"))
    monkeypatch.setattr(
        cli,
        "LocalSkillResolver",
        lambda: LocalSkillResolver(provider=LocalSkillProvider(include_user_skills=False)),
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "inspect",
            "--workspace",
            str(tmp_path),
            "--permission-profile",
            "trusted_host",
            "--approval-policy",
            "never",
            "--terminal-access",
            "ask",
        ]
    )

    snapshot = asyncio.run(cli._host_inspect(args))

    assert {"edit_file", "write_file", "terminal", "terminal_process"}.issubset(snapshot["tools"])
    assert snapshot["permissions"]["profile"] == "trusted_host"
    assert snapshot["permissions"]["approval_policy"] == "never"
    assert snapshot["permissions"]["terminal_access"] == "ask"
    assert snapshot["permissions"]["filesystem"]["terminal"] == "host_shell"


def test_cli_host_inspect_can_report_on_request_with_terminal_allow(
    monkeypatch, tmp_path, inspect_wiring
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "pulsara-home"))
    monkeypatch.setattr(
        cli,
        "LocalSkillResolver",
        lambda: LocalSkillResolver(provider=LocalSkillProvider(include_user_skills=False)),
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "inspect",
            "--workspace",
            str(tmp_path),
            "--permission-profile",
            "trusted_host",
            "--approval-policy",
            "on_request",
            "--terminal-access",
            "allow",
        ]
    )

    snapshot = asyncio.run(cli._host_inspect(args))

    assert {"edit_file", "write_file", "terminal", "terminal_process"}.issubset(snapshot["tools"])
    assert snapshot["permissions"]["profile"] == "trusted_host"
    assert snapshot["permissions"]["approval_policy"] == "on_request"
    assert snapshot["permissions"]["terminal_access"] == "allow"


def test_cli_host_run_accepts_resumable_on_request_policy(monkeypatch, tmp_path) -> None:
    sync_calls = []
    FakeCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli, "_best_effort_sync_bundled_skills", lambda: sync_calls.append("sync"))
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace",
            str(tmp_path),
            "--permission-profile",
            "trusted_host",
            "--approval-policy",
            "on_request",
            "--terminal-access",
            "ask",
            "say hi",
        ]
    )

    result = asyncio.run(cli._host_run(args))

    assert result.final_text == "fake final"
    assert sync_calls == ["sync"]
    core = FakeCore.instances[0]
    assert core.permission_policy.profile.value == "trusted_host"
    assert core.permission_policy.approval.value == "on_request"
    assert core.permission_policy.terminal.value == "ask"


def test_cli_skills_sync_bundled_uses_pulsara_home(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source"
    skill_dir = source / "cli-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: cli-skill
description: CLI skill.
---
# CLI
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "pulsara-home"))
    monkeypatch.setattr(
        cli,
        "sync_bundled_skills",
        lambda **kwargs: real_sync_bundled_skills(source_root=source, **kwargs),
    )
    parser = cli.build_parser()
    args = parser.parse_args(["skills", "sync-bundled"])

    result = cli._skills_sync_bundled(args)

    assert result.items[0].action == "installed"


def test_cli_skills_status_is_read_only(monkeypatch, tmp_path) -> None:
    pulsara_home = tmp_path / "pulsara-home"
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))
    parser = cli.build_parser()
    args = parser.parse_args(["skills", "status"])

    result = cli._skills_status(args)

    assert {status.name for status in result.statuses} >= {"pulsara-skill-creator", "pulsara-skill-installer"}
    assert not (pulsara_home / "skills").exists()


def test_cli_skills_reset_uses_requested_name(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source"
    skill_dir = source / "cli-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: cli-skill
description: CLI skill.
---
# CLI bundled
""",
        encoding="utf-8",
    )
    pulsara_home = tmp_path / "pulsara-home"
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))
    real_sync_bundled_skills(pulsara_home=pulsara_home, source_root=source)
    (pulsara_home / "skills" / "cli-skill" / "note.txt").write_text("modified\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "reset_bundled_skill",
        lambda name: real_reset_bundled_skill(name, source_root=source),
    )
    parser = cli.build_parser()
    args = parser.parse_args(["skills", "reset", "cli-skill"])

    result = cli._skills_reset(args)

    assert result.name == "cli-skill"
    assert result.action == "reset"
    assert result.backup_path is not None


def test_cli_skills_reset_prints_clean_error_for_invalid_name(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "reset_bundled_skill", lambda name: (_ for _ in ()).throw(ValueError(f"Invalid bundled skill name: {name!r}")))
    parser = cli.build_parser()
    args = parser.parse_args(["skills", "reset", "../bad"])

    with pytest.raises(SystemExit) as excinfo:
        cli._skills_reset(args)

    assert excinfo.value.code == 2
    assert "ERROR: Invalid bundled skill name" in capsys.readouterr().err


def test_permission_mode_preset_resolves_to_policy(tmp_path) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["host", "run", "--workspace", str(tmp_path), "--permission-mode", "read-only", "say hi"]
    )

    policy = cli._permission_policy_from_host_args(args, intent="run")

    assert policy.profile.value == "read_only"
    assert policy.approval.value == "on_request"
    assert policy.terminal.value == "off"


def test_permission_mode_default_run_is_bypass(tmp_path) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["host", "run", "--workspace", str(tmp_path), "say hi"])

    policy = cli._permission_policy_from_host_args(args, intent="run")

    assert policy.profile.value == "trusted_host"
    assert policy.approval.value == "never"
    assert policy.terminal.value == "allow"


def test_permission_mode_inspect_default_is_read_only(tmp_path) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["host", "inspect", "--workspace", str(tmp_path)])

    policy = cli._permission_policy_from_host_args(args, intent="inspect")

    assert policy.profile.value == "read_only"
    assert policy.terminal.value == "off"


def test_permission_mode_is_mutually_exclusive_with_raw_axes(tmp_path, capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace",
            str(tmp_path),
            "--permission-mode",
            "bypass-permissions",
            "--approval-policy",
            "never",
            "say hi",
        ]
    )

    with pytest.raises(SystemExit) as excinfo:
        cli._permission_policy_from_host_args(args, intent="run")

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--permission-mode cannot be combined" in err
    assert "--approval-policy" in err


def test_permission_mode_from_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSARA_PERMISSION_MODE", "accept-edits")
    parser = cli.build_parser()
    args = parser.parse_args(["host", "run", "--workspace", str(tmp_path), "say hi"])

    policy = cli._permission_policy_from_host_args(args, intent="run")

    assert policy.profile.value == "trusted_host"
    assert policy.approval.value == "never"
    assert policy.terminal.value == "ask"
