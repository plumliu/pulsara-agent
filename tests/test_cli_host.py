import asyncio

from pulsara_agent import cli
from pulsara_agent.host import HostWorkspaceInput
from pulsara_agent.runtime.state import LoopStatus


class FakeResult:
    status = LoopStatus.FINISHED
    stop_reason = "final"
    final_text = "fake final"


class FakeSession:
    host_session_id = "host:fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.active_skill_names: list[frozenset[str] | None] = []

    async def run_turn(self, prompt: str, *, active_skill_names=None):
        self.prompts.append(prompt)
        self.active_skill_names.append(active_skill_names)
        return FakeResult()


class FakeCore:
    instances: list["FakeCore"] = []

    def __init__(self, *, settings, durable: bool = False):
        self.settings = settings
        self.durable = durable
        self.session = FakeSession()
        self.workspace_input: HostWorkspaceInput | None = None
        self.closed: list[str] = []
        self.__class__.instances.append(self)

    async def open_session(self, workspace_input, *, model_role):
        self.workspace_input = workspace_input
        self.model_role = model_role
        return self.session

    async def close_session(self, host_session_id: str):
        self.closed.append(host_session_id)

    async def list_sessions(self):
        return []

    async def list_workspace_supervisors(self):
        return []

    async def shutdown(self):
        self.shutdown_called = True


def test_cli_host_run_uses_host_core_and_normalizes_ephemeral(monkeypatch, tmp_path, capsys) -> None:
    FakeCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(lambda cls, prefix="PULSARA": object()))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "host",
            "run",
            "--workspace-kind",
            "ephemeral",
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


def test_cli_host_inspect_prints_host_process_recovery_scope_and_skills(monkeypatch, tmp_path) -> None:
    FakeCore.instances.clear()
    monkeypatch.setattr(cli, "HostCore", FakeCore)
    skill_dir = tmp_path / ".pulsara" / "skills" / "review-pr"
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

    def _fail_from_env(cls, prefix="PULSARA"):
        raise AssertionError("inspect should not load settings")

    monkeypatch.setattr(cli.PulsaraSettings, "from_env", classmethod(_fail_from_env))
    parser = cli.build_parser()
    args = parser.parse_args(["host", "inspect", "--workspace", str(tmp_path)])

    snapshot = asyncio.run(cli._host_inspect(args))

    assert snapshot["sessions"] == []
    assert snapshot["workspace_supervisors"] == []
    assert snapshot["recovery_scope"] == "host_process"
    assert snapshot["workspace"]["workspace_kind"] == "project"
    assert snapshot["workspace"]["workspace_root"] == str(tmp_path)
    assert snapshot["skills"] == [
        {
            "name": "review-pr",
            "description": "Review pull requests.",
            "when_to_use": None,
            "location": ".pulsara/skills/review-pr/SKILL.md",
            "provides_tools": ["read_file"],
        }
    ]
    assert "read_file" in snapshot["tools"]
    assert [diagnostic["code"] for diagnostic in snapshot["capability_diagnostics"]] == [
        "skill_unknown_tool_reference"
    ]
    assert FakeCore.instances == []
