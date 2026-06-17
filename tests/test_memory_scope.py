from __future__ import annotations

import pytest

from pulsara_agent.memory import (
    CTX_USER,
    MemoryDomainContext,
    is_valid_flat_id,
    is_valid_scope,
    parse_scope,
    workspace_scope,
)


def test_scope_vocab_accepts_only_user_and_flat_workspace_scopes() -> None:
    assert is_valid_scope(CTX_USER)
    assert is_valid_scope("ctx:workspace/test_project")
    assert is_valid_scope("ctx:workspace/repo_abc.123")

    for scope in ("", "ctx:workspace", "ctx:workspace/a/b", "ctx:project", "ctx:乱填"):
        assert not is_valid_scope(scope)


def test_memory_domain_context_validates_ids_and_resolves_graph_and_scopes() -> None:
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key="repo_test",
        workspace_label="repo",
    )

    assert domain.graph_id == "graph:user/u_test"
    assert domain.read_scopes == frozenset({"ctx:user", "ctx:workspace/repo_test"})
    assert domain.allowed_write_scopes == domain.read_scopes
    assert workspace_scope("repo_test") == "ctx:workspace/repo_test"
    assert parse_scope("ctx:workspace/repo_test") == ("workspace", "repo_test")


def test_memory_domain_context_rejects_backend_unsafe_ids() -> None:
    assert not is_valid_flat_id("Repo/Path")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="user/path", workspace_kind="transient")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="u_test", workspace_kind="project")
    with pytest.raises(ValueError):
        MemoryDomainContext(memory_domain_id="u_test", workspace_kind="transient", stable_project_key="repo")
