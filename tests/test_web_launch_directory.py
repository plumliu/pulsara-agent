from __future__ import annotations

from pulsara_agent.capability.local_skill_management import LocalSkillManagementService

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.cli import build_parser
from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.local_skills import LooseSkillDefinitionProducer
from pulsara_agent.web_app.application import LocalWebApplication
from pulsara_agent.web_app import session_controller as module
from pulsara_agent.web_app.session_controller import LocalSessionController


@pytest.mark.parametrize("arguments", [
    ["--workspace", "/tmp/project"],
    ["--workspace-kind", "project"],
    ["--transient-display-label", "project"],
])
def test_app_rejects_removed_directory_arguments(arguments):
    with pytest.raises(SystemExit) as rejected:
        build_parser().parse_args(["app", *arguments])
    assert rejected.value.code == 2


def test_app_starts_without_a_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["app"])
    assert not hasattr(args, "workspace")
    assert not hasattr(args, "workspace_kind")
    core = SimpleNamespace(mcp_management=SimpleNamespace(lane=asyncio.Lock()))
    app = LocalWebApplication(
        core=core, settings=object(), catalog=object(), model_runtime=object(),
        permission_policy=None,
    )
    assert not hasattr(app, "workspace_input")
    assert not hasattr(app.sessions, "workspace_input")
    assert "workspace" not in app.sessions.bootstrap_payload()
    with pytest.raises(ValueError, match="workspace_path is required"):
        app.sessions._workspace_input_for_create(workspace_kind="project", workspace_path=None)
    selected = tmp_path / "selected"
    selected.mkdir()
    workspace = app.sessions._workspace_input_for_create(
        workspace_kind="project", workspace_path=str(selected),
    )
    assert workspace.workspace_root == selected


def test_user_inventory_does_not_scan_the_launch_project(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    launch.mkdir()
    # An invalid project root would make an ordinary full observation unavailable.
    (launch / ".pulsara").write_text("not a directory")
    monkeypatch.chdir(launch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PULSARA_HOME", str(home))
    user_root = home / "skills"
    agents_root = tmp_path / "agents-skills"
    skill = user_root / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example skill\n---\nBody\n")
    producer = LooseSkillDefinitionProducer(
        user_product_skills_root=user_root, user_agents_skills_root=agents_root,
    )
    policy = producer.prepare_root_policy()
    assert policy.includes_workspace is False
    assert {root.root_kind for root in policy.roots} == {
        LocalSkillRootKind.USER_PULSARA, LocalSkillRootKind.USER_AGENTS,
    }
    monkeypatch.setattr(module, "LocalSkillManagementService",
        lambda: LocalSkillManagementService(loose_producer=producer))
    inventory = module._inspect_user_skills()
    assert inventory["status"] == "ready"
    assert [item["name"] for item in inventory["items"]] == ["example"]
    assert all(str(launch) not in root["path"] for root in inventory["roots"])


def test_user_mcp_test_uses_its_management_home_without_a_session(tmp_path, monkeypatch):
    async def run():
        home = tmp_path / "pulsara-home"
        home.mkdir()
        monkeypatch.setenv("PULSARA_HOME", str(home))
        monkeypatch.chdir(tmp_path)
        test = AsyncMock(return_value=SimpleNamespace(
            status="ready", tools=0, resources=0, resource_templates=0, prompts=0,
        ))
        core = SimpleNamespace(mcp_management=SimpleNamespace(lane=asyncio.Lock(), test=test))
        controller = LocalSessionController(core=core, permission_policy=None, active_skill_names=frozenset())
        await controller.test_mcp_server(server_id="user-mcp", config={})
        assert test.await_args.args[0].workspace_root is None
        assert test.await_args.kwargs["workspace_root"] == home
        assert controller._by_session == {}

    asyncio.run(run())
