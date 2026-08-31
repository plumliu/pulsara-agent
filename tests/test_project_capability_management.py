from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import pulsara_agent.mcp_config as mcp_config_module

from pulsara_agent.capability.local_skills import LooseSkillDefinitionProducer
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
)
from pulsara_agent.capability.user_skill_config import (
    load_user_skill_config,
    workspace_skill_config_path,
)
from pulsara_agent.capability.workspace_mcp_trust import (
    approve_workspace_mcp_server_config,
    workspace_mcp_config_is_trusted,
    workspace_mcp_server_approvals,
    workspace_mcp_trust_path,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerState
from pulsara_agent.mcp_config import (
    MAXIMUM_MCP_CONFIGURED_SERVERS,
    McpConfiguredServerBoundExceeded,
    McpLocalConfigSourceKind,
    McpScopePolicy,
    StdioTransportConfig,
    create_workspace_mcp_server_config,
    load_mcp_server_configs,
    load_workspace_mcp_server_configs,
    mcp_server_workspace_approval_identity,
    write_mcp_server_config,
)
from pulsara_agent.web_app.session_controller import (
    HostSessionHandle,
    LocalSessionController,
    _capability_payload,
    _mcp_transport_payload,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


class _Session:
    def __init__(self, session_id: str, workspace_root: Path) -> None:
        self.session_id = session_id
        self.host_session_id = f"host:{session_id}"
        self.workspace = SimpleNamespace(
            workspace_root=workspace_root,
            workspace_kind="project",
        )
        self.refresh_requests = 0

    def inspect_capability_catalog(self):
        return SimpleNamespace(mcp_configured_servers=())

    async def request_project_capability_refresh(self) -> int:
        self.refresh_requests += 1
        return self.refresh_requests


def _controller_with_sessions(
    workspace_root: Path, other_workspace: Path
) -> tuple[LocalSessionController, HostSessionHandle, _Session, _Session, _Session]:
    primary = _Session("session:primary", workspace_root)
    sibling = _Session("session:sibling", workspace_root)
    other = _Session("session:other", other_workspace)
    handles = [
        HostSessionHandle(primary, SimpleNamespace()),
        HostSessionHandle(sibling, SimpleNamespace()),
        HostSessionHandle(other, SimpleNamespace()),
    ]
    controller = object.__new__(LocalSessionController)
    controller._lock = asyncio.Lock()
    controller._capability_mutation_lock = asyncio.Lock()
    controller._workspace_capability_revisions = {}
    controller._by_session = {item.session_id: item for item in handles}

    async def resume_session(session_id: str) -> HostSessionHandle:
        return controller._by_session[session_id]

    async def capability_payload(_handle: HostSessionHandle) -> dict[str, object]:
        return {"session_id": _handle.session_id}

    controller.resume_session = resume_session  # type: ignore[method-assign]
    controller._session_capability_payload = capability_payload  # type: ignore[method-assign]
    return controller, handles[0], primary, sibling, other


def test_project_skill_switch_derives_target_from_session_and_marks_same_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill = workspace / ".pulsara" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: Review this project.\n---\n# Instructions\n",
        encoding="utf-8",
    )
    controller, handle, primary, sibling, other = _controller_with_sessions(
        workspace, tmp_path / "other"
    )

    result = asyncio.run(
        controller.set_session_skill_enabled(
            handle.session_id,
            skill_id="pulsara:review",
            enabled=False,
        )
    )

    config = load_user_skill_config(config_path=workspace_skill_config_path(workspace))
    assert config.available
    assert config.enabled_for(skill) is False
    assert result["adoption"]["pending_sessions"] == 2  # type: ignore[index]
    assert primary.refresh_requests == sibling.refresh_requests == 1
    assert other.refresh_requests == 0


def test_project_mcp_write_is_session_scoped_and_approves_only_exact_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller, handle, primary, sibling, other = _controller_with_sessions(
        workspace, tmp_path / "other"
    )

    result = asyncio.run(
        controller.create_session_mcp_server(
            handle.session_id,
            server_id="project-docs",
            display_name="Project Docs",
            transport="http",
            endpoint="https://example.com/mcp",
            command=None,
            args=[],
            available_to_subagents=True,
        )
    )

    configs = load_workspace_mcp_server_configs(workspace)
    assert [item.server_id for item in configs] == ["project-docs"]
    assert configs[0].scope_policy.value == "ROOT_AND_SUBAGENTS"
    assert workspace_mcp_config_is_trusted(workspace)
    assert result["adoption"]["pending_sessions"] == 2  # type: ignore[index]
    assert primary.refresh_requests == sibling.refresh_requests == 1
    assert other.refresh_requests == 0

    path = workspace / ".pulsara" / "mcp.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "display_name: Project Docs",
            "display_name: Replaced Docs",
        ),
        encoding="utf-8",
    )
    assert not workspace_mcp_config_is_trusted(workspace)


def test_project_mcp_approval_does_not_enable_an_unapproved_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_mcp_server_config(
        workspace_root=workspace,
        server_id="unreviewed",
        entry={
            "transport": "stdio",
            "command": "unknown-command",
            "enabled": True,
        },
    )
    controller, handle, *_ = _controller_with_sessions(workspace, tmp_path / "other")

    asyncio.run(
        controller.create_session_mcp_server(
            handle.session_id,
            server_id="reviewed",
            display_name="Reviewed Docs",
            transport="http",
            endpoint="https://example.com/mcp",
            command=None,
            args=[],
            available_to_subagents=False,
        )
    )

    approvals = workspace_mcp_server_approvals(workspace)
    assert set(approvals) == {"reviewed"}
    merged = load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=tmp_path / "missing-user.yaml",
        approved_workspace_server_identities=approvals,
    )
    assert {item.server_id: item.enabled for item in merged} == {
        "reviewed": True,
        "unreviewed": False,
    }


def test_disabling_project_mcp_removes_its_workspace_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller, handle, *_ = _controller_with_sessions(workspace, tmp_path / "other")
    asyncio.run(
        controller.create_session_mcp_server(
            handle.session_id,
            server_id="docs",
            display_name="Docs",
            transport="http",
            endpoint="https://example.com/mcp",
            command=None,
            args=[],
            available_to_subagents=False,
        )
    )
    inspected = load_workspace_mcp_server_configs(workspace)[0]
    assert set(workspace_mcp_server_approvals(workspace)) == {"docs"}

    asyncio.run(
        controller.set_session_mcp_enabled(
            handle.session_id,
            server_id="docs",
            enabled=False,
            expected_config_identity=mcp_server_workspace_approval_identity(
                inspected
            ),
        )
    )

    assert workspace_mcp_server_approvals(workspace) == {}


def test_disabling_project_mcp_repairs_bad_approval_and_still_marks_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller, handle, primary, sibling, _ = _controller_with_sessions(
        workspace, tmp_path / "other"
    )
    asyncio.run(
        controller.create_session_mcp_server(
            handle.session_id,
            server_id="docs",
            display_name="Docs",
            transport="http",
            endpoint="https://example.com/mcp",
            command=None,
            args=[],
            available_to_subagents=False,
        )
    )
    inspected = load_workspace_mcp_server_configs(workspace)[0]
    workspace_mcp_trust_path(workspace).write_text("not: [valid", encoding="utf-8")

    asyncio.run(
        controller.set_session_mcp_enabled(
            handle.session_id,
            server_id="docs",
            enabled=False,
            expected_config_identity=mcp_server_workspace_approval_identity(
                inspected
            ),
        )
    )

    assert primary.refresh_requests == sibling.refresh_requests == 2
    assert not workspace_mcp_trust_path(workspace).exists()
    assert load_workspace_mcp_server_configs(workspace)[0].enabled is False


def test_project_mcp_approval_survives_process_secret_commitment_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROJECT_DOCS_TOKEN", "rotatable-secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_mcp_server_config(
        workspace_root=workspace,
        server_id="project-docs",
        entry={
            "transport": "streamable_http",
            "url": "https://example.com/mcp",
            "auth": {
                "type": "bearer_environment_ref",
                "environment_variable": "PROJECT_DOCS_TOKEN",
            },
            "enabled": True,
        },
    )

    monkeypatch.setattr(
        mcp_config_module, "_PROCESS_SECRET_COMMITMENT_KEY", b"a" * 32
    )
    first = load_workspace_mcp_server_configs(workspace)[0]
    approve_workspace_mcp_server_config(workspace, first)
    first_approval = mcp_server_workspace_approval_identity(first)

    monkeypatch.setattr(
        mcp_config_module, "_PROCESS_SECRET_COMMITMENT_KEY", b"b" * 32
    )
    second = load_workspace_mcp_server_configs(workspace)[0]
    assert second.resolved_config_identity != first.resolved_config_identity
    assert mcp_server_workspace_approval_identity(second) == first_approval
    assert workspace_mcp_server_approvals(workspace) == {
        "project-docs": first_approval
    }

    (loaded,) = load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=tmp_path / "missing-user.yaml",
        approved_workspace_server_identities=workspace_mcp_server_approvals(
            workspace
        ),
    )
    assert loaded.enabled is True


def test_project_mcp_approval_identity_does_not_depend_on_process_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PATH", "/first/bin")
    write_mcp_server_config(
        workspace_root=workspace,
        server_id="local",
        entry={"transport": "stdio", "command": "example-mcp", "enabled": True},
    )
    first = load_workspace_mcp_server_configs(workspace)[0]

    monkeypatch.setenv("PATH", "/second/bin")
    second = load_workspace_mcp_server_configs(workspace)[0]

    assert first.resolved_config_identity != second.resolved_config_identity
    assert mcp_server_workspace_approval_identity(first) == (
        mcp_server_workspace_approval_identity(second)
    )


def test_project_mcp_writer_rejects_the_next_entry_without_corrupting_the_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(MAXIMUM_MCP_CONFIGURED_SERVERS):
        write_mcp_server_config(
            workspace_root=workspace,
            server_id=f"server-{index:02d}",
            entry={
                "transport": "streamable_http",
                "url": f"https://example.com/{index}/mcp",
            },
        )

    with pytest.raises(McpConfiguredServerBoundExceeded):
        create_workspace_mcp_server_config(
            workspace_root=workspace,
            server_id="one-too-many",
            entry={
                "transport": "streamable_http",
                "url": "https://example.com/overflow/mcp",
            },
        )

    assert len(load_workspace_mcp_server_configs(workspace)) == (
        MAXIMUM_MCP_CONFIGURED_SERVERS
    )


def test_project_mcp_addition_rejects_cross_source_capacity_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_config = tmp_path / "user-mcp.yaml"
    monkeypatch.setattr(mcp_config_module, "DEFAULT_USER_MCP_CONFIG", user_config)
    for index in range(MAXIMUM_MCP_CONFIGURED_SERVERS):
        write_mcp_server_config(
            user_config_path=user_config,
            server_id=f"user-{index:02d}",
            entry={
                "transport": "streamable_http",
                "url": f"https://example.com/{index}/mcp",
            },
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller, handle, primary, sibling, _ = _controller_with_sessions(
        workspace, tmp_path / "other"
    )

    with pytest.raises(McpConfiguredServerBoundExceeded):
        asyncio.run(
            controller.create_session_mcp_server(
                handle.session_id,
                server_id="project-extra",
                display_name="Project Extra",
                transport="http",
                endpoint="https://example.com/project/mcp",
                command=None,
                args=[],
                available_to_subagents=False,
            )
        )

    assert not (workspace / ".pulsara" / "mcp.yaml").exists()
    assert primary.refresh_requests == sibling.refresh_requests == 0


def test_project_mcp_edit_rejects_a_stale_inspected_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller, handle, *_ = _controller_with_sessions(workspace, tmp_path / "other")
    asyncio.run(
        controller.create_session_mcp_server(
            handle.session_id,
            server_id="docs",
            display_name="Docs",
            transport="http",
            endpoint="https://example.com/mcp",
            command=None,
            args=[],
            available_to_subagents=False,
        )
    )
    inspected = load_workspace_mcp_server_configs(workspace)[0]
    path = workspace / ".pulsara" / "mcp.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "display_name: Docs", "display_name: Changed outside Pulsara"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="changed"):
        asyncio.run(
            controller.set_session_mcp_enabled(
                handle.session_id,
                server_id="docs",
                enabled=False,
                expected_config_identity=mcp_server_workspace_approval_identity(
                    inspected
                ),
            )
        )


def test_project_mcp_write_rejects_a_symlinked_project_capability_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "user-owned"
    workspace.mkdir()
    external.mkdir()
    (workspace / ".pulsara").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        create_workspace_mcp_server_config(
            workspace_root=workspace,
            server_id="docs",
            entry={
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
            },
        )
    assert not (external / "mcp.yaml").exists()


def test_project_skill_switch_rejects_a_symlinked_project_capability_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "user-owned"
    workspace.mkdir()
    external.mkdir()
    (workspace / ".pulsara").symlink_to(external, target_is_directory=True)
    controller, handle, *_ = _controller_with_sessions(workspace, tmp_path / "other")

    with pytest.raises(ValueError, match="symlink"):
        asyncio.run(
            controller.set_session_skill_enabled(
                handle.session_id,
                skill_id="pulsara:review",
                enabled=False,
            )
        )
    assert not (external / "skills.yaml").exists()


def test_session_created_during_a_project_change_still_marks_lazy_refresh(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        entered = asyncio.Event()
        release = asyncio.Event()
        session = _Session("session:new", workspace)
        session.workspace = SimpleNamespace(
            workspace_root=workspace.resolve(),
            workspace_kind="project",
        )

        async def open_session(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return session

        controller = object.__new__(LocalSessionController)
        controller.core = SimpleNamespace(
            open_session=open_session,
            close_session=AsyncMock(),
        )
        controller.workspace_input = HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=workspace,
        )
        controller.model_role = SimpleNamespace()
        controller.permission_policy = SimpleNamespace()
        controller.active_skill_names = frozenset()
        controller._by_session = {}
        controller._by_host = {}
        controller._resumes = {}
        controller._lock = asyncio.Lock()
        controller._capability_mutation_lock = asyncio.Lock()
        controller._workspace_capability_revisions = {}
        controller._closing = False
        controller._close_task = None

        creating = asyncio.create_task(
            controller.create_session(
                workspace_kind="project",
                workspace_path=str(workspace),
            )
        )
        await entered.wait()
        assert await controller._mark_workspace_capability_refresh(workspace) == 0
        release.set()
        await creating
        assert session.refresh_requests == 1

    asyncio.run(exercise())


def test_session_resumed_from_a_string_workspace_path_keeps_raced_refresh(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        entered = asyncio.Event()
        release = asyncio.Event()
        session = _Session("session:resumed", workspace.resolve())

        async def read_resumable_session(*_args, **_kwargs):
            return SimpleNamespace(
                workspace_kind="project",
                workspace_root=str(workspace),
                workspace_label="workspace",
                memory_domain_id="u_local",
            )

        async def resume_session(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return session

        controller = object.__new__(LocalSessionController)
        controller.core = SimpleNamespace(
            read_resumable_session=read_resumable_session,
            resume_session=resume_session,
            close_session=AsyncMock(),
        )
        controller.workspace_input = HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=workspace,
        )
        controller.model_role = SimpleNamespace()
        controller.permission_policy = SimpleNamespace()
        controller.active_skill_names = frozenset()
        controller._by_session = {}
        controller._by_host = {}
        controller._resumes = {}
        controller._lock = asyncio.Lock()
        controller._capability_mutation_lock = asyncio.Lock()
        controller._workspace_capability_revisions = {}
        controller._closing = False
        controller._close_task = None

        resuming = asyncio.create_task(controller.resume_session("session:resumed"))
        await entered.wait()
        assert await controller._mark_workspace_capability_refresh(workspace) == 0
        release.set()
        handle = await resuming
        assert handle.session_id == "session:resumed"
        assert session.refresh_requests == 1

    asyncio.run(exercise())


def test_project_refresh_ignores_a_session_that_closes_during_marking(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        entered = asyncio.Event()
        release = asyncio.Event()
        session = _Session("session:closing", workspace)

        async def close_during_refresh() -> int:
            entered.set()
            await release.wait()
            raise RuntimeError("kernel Host session is closing")

        session.request_project_capability_refresh = close_during_refresh  # type: ignore[method-assign]
        handle = HostSessionHandle(session, SimpleNamespace())
        controller = object.__new__(LocalSessionController)
        controller._lock = asyncio.Lock()
        controller._workspace_capability_revisions = {}
        controller._by_session = {handle.session_id: handle}

        marking = asyncio.create_task(
            controller._mark_workspace_capability_refresh(workspace)
        )
        await entered.wait()
        async with controller._lock:
            controller._by_session.pop(handle.session_id)
        release.set()
        assert await marking == 0

    asyncio.run(exercise())


def test_live_session_refresh_is_lazy_until_the_next_turn_safe_point() -> None:
    async def exercise() -> None:
        session = SimpleNamespace(
            _lock=asyncio.Lock(),
            _queue_wake=asyncio.Event(),
            _project_capability_refresh_requested_revision=0,
            _project_capability_refresh_applied_revision=0,
            _project_capability_refresh_attention=None,
            _require_open=lambda: None,
            reload_plugins=AsyncMock(return_value={"mcp": "RELOADED"}),
        )

        await KernelHostSession.request_project_capability_refresh(session)
        session.reload_plugins.assert_not_awaited()
        assert KernelHostSession.project_capability_refresh_pending.fget(session)

        adopted = await KernelHostSession._adopt_project_capabilities_if_requested(
            session
        )
        assert adopted is True
        session.reload_plugins.assert_awaited_once_with(deadline_monotonic=None)
        assert not KernelHostSession.project_capability_refresh_pending.fget(session)
        assert session._project_capability_refresh_attention is None

    asyncio.run(exercise())


def test_failed_project_capability_adoption_does_not_starve_the_queued_turn() -> None:
    async def exercise() -> None:
        session = SimpleNamespace(
            _lock=asyncio.Lock(),
            _queue_wake=asyncio.Event(),
            _project_capability_refresh_requested_revision=0,
            _project_capability_refresh_applied_revision=0,
            _project_capability_refresh_attention=None,
            _require_open=lambda: None,
            reload_plugins=AsyncMock(side_effect=ValueError("bad project MCP")),
        )

        await KernelHostSession.request_project_capability_refresh(session)
        assert await KernelHostSession._adopt_project_capabilities_if_requested(
            session
        )
        assert not KernelHostSession.project_capability_refresh_pending.fget(session)
        assert session._project_capability_refresh_attention == (
            "PROJECT_CAPABILITY_ADOPTION_FAILED"
        )

        assert await KernelHostSession._adopt_project_capabilities_if_requested(
            session
        )
        session.reload_plugins.assert_awaited_once_with(deadline_monotonic=None)

    asyncio.run(exercise())


def test_project_mcp_transport_projection_keeps_the_complete_command() -> None:
    config = SimpleNamespace(
        transport=StdioTransportConfig(
            command="npx",
            args=("-y", "@modelcontextprotocol/server-everything"),
        )
    )

    assert _mcp_transport_payload(config) == {
        "kind": "stdio",
        "summary": "npx",
        "detail": "npx -y @modelcontextprotocol/server-everything",
    }


def test_project_mcp_transition_keeps_one_editable_row(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    producer = LooseSkillDefinitionProducer(
        user_product_skills_root=tmp_path / "user-product-skills",
        user_agents_skills_root=tmp_path / "user-agent-skills",
    )
    skill_catalog = CompleteEffectiveSkillCatalogInspection(
        root_policy=producer.prepare_root_policy(workspace),
        winners=(),
        candidate_issues=(),
    )
    write_mcp_server_config(
        workspace_root=workspace,
        server_id="time",
        entry={
            "display_name": "Time",
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-time"],
            "enabled": True,
            "scope_policy": McpScopePolicy.ROOT_ONLY.value,
        },
    )
    desired = load_workspace_mcp_server_configs(workspace)[0]
    stale_live = SimpleNamespace(
        server_id="time",
        display_name="Time",
        status=McpServerState.DISABLED,
        required=False,
        exposed_tool_count=0,
        discovered_tool_count=0,
        resource_count=0,
        resource_template_count=0,
        prompt_count=0,
        sanitized_instructions="",
        stable_failure_category=None,
        scope_subagents=False,
    )
    stale_config = SimpleNamespace(
        server_id="time",
        resolved_config_identity="old-config",
        source_kind=McpLocalConfigSourceKind.WORKSPACE.value,
    )
    inspection = SimpleNamespace(
        mcp_tools=(),
        mcp_catalog=SimpleNamespace(
            servers=(stale_live,), provider_name_collision_facts=()
        ),
        mcp_configured_servers=(stale_config,),
        skill_catalog=skill_catalog,
        configured_active_skill_names=frozenset(),
    )

    payload = _capability_payload(
        session_id="session:test",
        workspace_root=workspace,
        workspace_kind="project",
        workspace_skills={"items": []},
        workspace_mcp_configs=(desired,),
        trust_all_workspace_mcp=True,
        workspace_mcp_approvals={},
        refresh_pending=True,
        refresh_attention=None,
        inspection=inspection,
    )

    rows = payload["mcp"]["servers"]  # type: ignore[index]
    assert len(rows) == 1
    assert rows[0]["id"] == "time"
    assert rows[0]["editable"] is True
    assert rows[0]["status"] == "CONFIGURED"


def test_disabled_workspace_mcp_does_not_hide_the_effective_user_server(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    producer = LooseSkillDefinitionProducer(
        user_product_skills_root=tmp_path / "user-product-skills",
        user_agents_skills_root=tmp_path / "user-agent-skills",
    )
    skill_catalog = CompleteEffectiveSkillCatalogInspection(
        root_policy=producer.prepare_root_policy(workspace),
        winners=(),
        candidate_issues=(),
    )
    write_mcp_server_config(
        workspace_root=workspace,
        server_id="docs",
        entry={
            "display_name": "Project Docs",
            "transport": "streamable_http",
            "url": "https://project.example.com/mcp",
            "enabled": False,
        },
    )
    project = load_workspace_mcp_server_configs(workspace)[0]
    user_live = SimpleNamespace(
        server_id="docs",
        display_name="User Docs",
        status=McpServerState.READY,
        required=False,
        exposed_tool_count=2,
        discovered_tool_count=2,
        resource_count=0,
        resource_template_count=0,
        prompt_count=0,
        sanitized_instructions="",
        stable_failure_category=None,
        scope_subagents=False,
    )
    user_config = SimpleNamespace(
        server_id="docs",
        resolved_config_identity="user-config",
        source_kind=McpLocalConfigSourceKind.USER.value,
    )
    inspection = SimpleNamespace(
        mcp_tools=(),
        mcp_catalog=SimpleNamespace(
            servers=(user_live,), provider_name_collision_facts=()
        ),
        mcp_configured_servers=(user_config,),
        skill_catalog=skill_catalog,
        configured_active_skill_names=frozenset(),
    )

    payload = _capability_payload(
        session_id="session:test",
        workspace_root=workspace,
        workspace_kind="project",
        workspace_skills={"items": []},
        workspace_mcp_configs=(project,),
        trust_all_workspace_mcp=False,
        workspace_mcp_approvals={},
        refresh_pending=False,
        refresh_attention=None,
        inspection=inspection,
    )

    rows = payload["mcp"]["servers"]  # type: ignore[index]
    assert [(row["source"], row["enabled"], row["tool_count"]) for row in rows] == [
        ("workspace", False, 0),
        ("user", True, 2),
    ]
