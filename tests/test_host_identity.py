from pathlib import Path

import pytest

from pulsara_agent.host.identity import (
    HostWorkspaceInput,
    normalize_workspace_kind,
    resolve_workspace,
)
from pulsara_agent.memory.scope import CTX_USER, MemoryDomainContext, workspace_scope


def test_host_workspace_project_resolution_couples_memory_domain(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=repo,
            display_label="Repo",
            memory_domain_id="u_test",
        )
    )

    assert resolved.workspace_kind == "project"
    assert resolved.workspace_root == repo.resolve()
    assert resolved.display_label == "Repo"
    assert resolved.memory_domain == MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=repo.resolve().as_posix(),
        workspace_label="Repo",
    )
    assert resolved.workspace_scope == workspace_scope(repo.resolve().as_posix())
    assert resolved.workspace_scope in resolved.memory_domain.read_scopes
    assert resolved.memory_domain.graph_id == "graph:user/u_test"


def test_host_workspace_transient_resolution_uses_user_scope_only(tmp_path) -> None:
    scratch = tmp_path / "scratch"

    resolved = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="transient",
            workspace_root=scratch,
            memory_domain_id="u_test",
        )
    )

    assert resolved.workspace_kind == "transient"
    assert resolved.workspace_root == scratch.resolve()
    assert resolved.cleanup_workspace_root_on_close is False
    assert resolved.display_label == "Scratch"
    assert resolved.workspace_scope is None
    assert resolved.memory_domain.read_scopes == frozenset({CTX_USER})
    assert resolved.workspace_key.startswith("transient:")


def test_host_workspace_auto_transient_resolution_keeps_root_by_default(tmp_path) -> None:
    scratch_root = tmp_path / "scratch-root"

    resolved = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="transient",
            memory_domain_id="u_test",
        ),
        scratch_root=scratch_root,
    )

    assert resolved.workspace_kind == "transient"
    assert resolved.workspace_root.parent == scratch_root.resolve()
    assert resolved.workspace_root.exists()
    assert resolved.cleanup_workspace_root_on_close is False


def test_host_workspace_auto_transient_resolution_can_opt_into_cleanup(tmp_path) -> None:
    scratch_root = tmp_path / "scratch-root"

    resolved = resolve_workspace(
        HostWorkspaceInput(
            workspace_kind="transient",
            memory_domain_id="u_test",
            cleanup_workspace_root_on_close=True,
        ),
        scratch_root=scratch_root,
    )

    assert resolved.workspace_kind == "transient"
    assert resolved.workspace_root.parent == scratch_root.resolve()
    assert resolved.workspace_root.exists()
    assert resolved.cleanup_workspace_root_on_close is True


def test_host_workspace_kind_ephemeral_alias_is_removed() -> None:
    # `ephemeral` was a transient alias (legacy shim); it is now rejected everywhere.
    with pytest.raises(ValueError, match="workspace_kind"):
        normalize_workspace_kind("ephemeral")
    with pytest.raises(ValueError, match="workspace_kind"):
        MemoryDomainContext(memory_domain_id="u_test", workspace_kind="ephemeral")  # type: ignore[arg-type]


def test_host_workspace_project_requires_existing_directory(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires workspace_root"):
        resolve_workspace(HostWorkspaceInput(workspace_kind="project"))
    with pytest.raises(ValueError, match="does not exist"):
        resolve_workspace(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path / "missing"))
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        resolve_workspace(HostWorkspaceInput(workspace_kind="project", workspace_root=Path(file_path)))
