from __future__ import annotations

from hashlib import sha256

import pytest

from pulsara_agent.memory import (
    CTX_USER,
    MemoryDomainContext,
    canonical_project_key,
    is_valid_flat_id,
    is_valid_scope,
    parse_scope,
    workspace_scope_key,
    workspace_scope,
)


def test_scope_vocab_accepts_only_user_and_flat_workspace_scopes() -> None:
    assert is_valid_scope(CTX_USER)
    assert is_valid_scope("ctx:workspace/test_project")
    assert is_valid_scope("ctx:workspace/repo_abc.123")

    for scope in ("", "ctx:workspace", "ctx:workspace/a/b", "ctx:project", "ctx:乱填"):
        assert not is_valid_scope(scope)


def test_memory_domain_context_canonicalizes_project_path_and_resolves_hashed_scopes(tmp_path) -> None:
    project_root = tmp_path / "Repo Test"
    equivalent_project_root = project_root / ".." / project_root.name
    expected_project_key = project_root.resolve().as_posix()
    expected_scope_key = sha256(expected_project_key.encode("utf-8")).hexdigest()[:16]
    expected_scope = f"ctx:workspace/{expected_scope_key}"
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=str(equivalent_project_root),
        workspace_label="repo",
    )

    assert domain.stable_project_key == expected_project_key
    assert domain.graph_id == "graph:user/u_test"
    assert domain.read_scopes == frozenset({"ctx:user", expected_scope})
    assert domain.allowed_write_scopes == domain.read_scopes
    assert canonical_project_key(str(equivalent_project_root)) == expected_project_key
    assert workspace_scope_key(str(equivalent_project_root)) == expected_scope_key
    assert workspace_scope(str(equivalent_project_root)) == expected_scope
    assert parse_scope(expected_scope) == ("workspace", expected_scope_key)


def test_memory_domain_context_rejects_backend_unsafe_ids() -> None:
    assert not is_valid_flat_id("Repo/Path")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="user/path", workspace_kind="transient")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="u_test", workspace_kind="project")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="u_test", workspace_kind="project", stable_project_key=" ")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="u_test", workspace_kind="transient", stable_project_key="repo")
