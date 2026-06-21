import asyncio
import pytest

from pulsara_agent import cli
from pulsara_agent.capability import LocalSkillProvider, LocalSkillResolver
from pulsara_agent.capability.bundled_skills import (
    reset_bundled_skill as real_reset_bundled_skill,
    sync_bundled_skills as real_sync_bundled_skills,
)
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


def test_cli_host_run_uses_host_core_and_normalizes_ephemeral(monkeypatch, tmp_path) -> None:
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
    assert sync_calls == ["sync"]


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


def test_cli_host_inspect_prints_host_process_recovery_scope_and_skills(monkeypatch, tmp_path) -> None:
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
            "location": ".agents/skills/review-pr/SKILL.md",
            "provides_tools": ["read_file"],
        }
    ]
    assert "read_file" in snapshot["tools"]
    assert [diagnostic["code"] for diagnostic in snapshot["capability_diagnostics"]] == [
        "skill_unknown_tool_reference"
    ]
    assert "bundled_skills" in snapshot
    assert not (tmp_path / "pulsara-home" / "skills").exists()
    assert FakeCore.instances == []


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
