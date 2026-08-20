from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.local_skills import (
    LocalSkillProvider,
    SkillDiscoveryDisposition,
)
from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider
from pulsara_agent.capability.types import (
    SkillCatalogUnavailableReason,
    SkillDiagnostic,
    SkillDiagnosticSeverity,
)
from pulsara_agent.conversation_kernel.capability import (
    KernelSkillProjectionComposer,
)
from pulsara_agent.conversation_kernel.context_sources import (
    _public_capability_diagnostics,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.tools.builtins.filesystem import (
    DEFAULT_READ_LINES,
    MAX_READ_LINES,
    ReadFileTool,
)


def _document(
    name: str,
    *,
    description: str = "Portable Agent Skill.",
    body: str = "# Instructions\n",
    extra: str = "",
) -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\n"
        f"{extra}---\n{body}"
    )


def _write_skill(
    workspace: Path,
    name: str,
    *,
    document: str | None = None,
) -> Path:
    path = workspace / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document or _document(name), encoding="utf-8")
    return path


def _policy(
    provider: LocalSkillProvider,
    workspace: Path,
    *,
    scope: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    task_id: str | None = None,
):
    return provider.prepare_root_policy(
        workspace,
        conversation_scope_kind=scope,
        scope_subagent_task_id=task_id,
    )


def test_round9_1_root_policy_is_exactly_two_or_four_and_scope_bound(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    disabled = LocalSkillProvider(include_user_skills=False)
    enabled = LocalSkillProvider(
        user_product_skills_root=tmp_path / "user" / ".pulsara" / "skills",
        user_agents_skills_root=tmp_path / "user" / ".agents" / "skills",
    )
    assert tuple(item.root_kind for item in _policy(disabled, workspace).roots) == (
        LocalSkillRootKind.WORKSPACE_PULSARA,
        LocalSkillRootKind.WORKSPACE_AGENTS,
    )
    assert tuple(item.root_kind for item in _policy(enabled, workspace).roots) == tuple(
        LocalSkillRootKind
    )

    child = _policy(
        disabled,
        workspace,
        scope=ModelInputScopeKind.SUBAGENT_TASK,
        task_id="task:child-a",
    )
    assert child.scope_subagent_task_id == "task:child-a"
    with pytest.raises(ValueError, match="foreign Skill root policy"):
        enabled.discover(child)
    with pytest.raises(ValueError, match="fingerprint"):
        replace(child, scope_subagent_task_id="task:child-b")
    with pytest.raises(ValueError, match="fingerprint"):
        replace(child.roots[0], path=tmp_path / "swapped")


def test_round9_1_root_policy_bounds_cannot_be_relaxed() -> None:
    for kwargs in (
        {"maximum_direct_child_directories": 1_025},
        {"maximum_admitted_skills": 65},
        {"maximum_discovery_skill_bytes": 16 * 1024 * 1024 + 1},
    ):
        with pytest.raises(ValueError, match="closed maximum"):
            LocalSkillProvider(**kwargs)


def test_round9_1_enumeration_freezes_candidates_until_next_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.capability.local_skills as local_skills

    workspace = tmp_path / "workspace"
    first = _write_skill(workspace, "alpha")
    provider = LocalSkillProvider(include_user_skills=False)
    policy = _policy(provider, workspace)
    original = local_skills._read_bounded_bytes
    installed = False

    def read_and_install(path: Path, *, maximum: int) -> bytes:
        nonlocal installed
        if not installed:
            installed = True
            _write_skill(workspace, "beta")
        return original(path, maximum=maximum)

    monkeypatch.setattr(local_skills, "_read_bounded_bytes", read_and_install)
    current = provider.discover(policy)
    assert first.exists()
    assert [item.name for item in current.skills] == ["alpha"]
    successor = provider.discover(policy)
    assert [item.name for item in successor.skills] == ["alpha", "beta"]


def test_round9_1_enumerated_member_loss_invalidates_whole_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.capability.local_skills as local_skills

    workspace = tmp_path / "workspace"
    _write_skill(workspace, "alpha")
    victim = _write_skill(workspace, "beta")
    provider = LocalSkillProvider(include_user_skills=False)
    policy = _policy(provider, workspace)
    original = local_skills._read_bounded_bytes
    reads = 0

    def remove_before_second_read(path: Path, *, maximum: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            victim.unlink()
        return original(path, maximum=maximum)

    monkeypatch.setattr(
        local_skills, "_read_bounded_bytes", remove_before_second_read
    )
    discovery = provider.discover(policy)
    assert discovery.disposition is SkillDiscoveryDisposition.UNAVAILABLE
    assert discovery.unavailable_reason is SkillCatalogUnavailableReason.DISCOVERY_RACED
    assert discovery.skills == ()


def test_round9_1_non_regular_skill_file_is_bounded_unavailable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".agents" / "skills" / "fifo-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    os.mkfifo(skill_path)
    provider = LocalSkillProvider(include_user_skills=False)

    discovery = provider.discover(_policy(provider, workspace))

    assert discovery.disposition is SkillDiscoveryDisposition.UNAVAILABLE
    assert discovery.unavailable_reason is SkillCatalogUnavailableReason.DISCOVERY_RACED
    assert discovery.skills == ()


def test_round9_1_explicit_null_metadata_is_not_standard_valid(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(
        workspace,
        "null-metadata",
        document=_document("null-metadata", extra="metadata:\n"),
    )
    provider = LocalSkillProvider(include_user_skills=False)

    discovery = provider.discover(_policy(provider, workspace))

    assert discovery.disposition is SkillDiscoveryDisposition.COMPLETE
    assert discovery.skills == ()
    assert any(item.code == "skill_invalid_metadata" for item in discovery.diagnostics)


def test_round9_1_inert_skill_diagnostics_are_not_public_degradation() -> None:
    diagnostics = (
        SkillDiagnostic(
            severity=SkillDiagnosticSeverity.INFO,
            code="skill_host_extension_ignored",
            message="behaviorally inert",
        ),
    )

    assert _public_capability_diagnostics(diagnostics) == ()


def test_round9_1_1025_directories_and_65_winners_publish_no_partial_catalog(
    tmp_path: Path,
) -> None:
    direct_workspace = tmp_path / "direct"
    root = direct_workspace / ".agents" / "skills"
    for index in range(1_025):
        (root / f"dir-{index:04d}").mkdir(parents=True)
    direct_provider = LocalSkillProvider(include_user_skills=False)
    direct = direct_provider.discover(_policy(direct_provider, direct_workspace))
    assert direct.disposition is SkillDiscoveryDisposition.UNAVAILABLE
    assert direct.unavailable_reason is SkillCatalogUnavailableReason.DISCOVERY_OVERBOUND
    assert direct.skills == ()

    winner_workspace = tmp_path / "winners"
    for index in range(65):
        name = f"skill-{index:02d}"
        _write_skill(winner_workspace, name)
    winner_provider = LocalSkillProvider(include_user_skills=False)
    winners = winner_provider.discover(_policy(winner_provider, winner_workspace))
    assert winners.disposition is SkillDiscoveryDisposition.UNAVAILABLE
    assert winners.unavailable_reason is SkillCatalogUnavailableReason.CATALOG_OVERBOUND
    assert winners.skills == ()


@pytest.mark.parametrize(
    "frontmatter",
    (
        "name: alpha\nname: alpha\ndescription: duplicate\n",
        "name: &name alpha\ndescription: alias\nmetadata: {key: *name}\n",
        "name: alpha\ndescription: !custom tagged\n",
        "name: alpha\ndescription: first\n...\nname: alpha\n",
    ),
)
def test_round9_1_yaml_alias_tag_duplicate_and_multidoc_are_rejected(
    tmp_path: Path, frontmatter: str
) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(
        workspace,
        "alpha",
        document=f"---\n{frontmatter}---\nbody\n",
    )
    provider = LocalSkillProvider(include_user_skills=False)
    discovery = provider.discover(_policy(provider, workspace))
    assert discovery.skills == ()
    assert [item.code for item in discovery.diagnostics] == [
        "skill_invalid_frontmatter_yaml"
    ]


def test_round9_1_owner_snapshot_freezes_effective_head_until_next_scan(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill = _write_skill(
        workspace,
        "alpha",
        document=_document("alpha", body="# Version A\n"),
    )
    provider = LocalSkillProvider(include_user_skills=False)
    composer = KernelSkillProjectionComposer(
        workspace_root=workspace,
        provider=LocalSkillCapabilityProvider(provider=provider),
    )
    owner_a = composer.freeze_owner_snapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    frozen_a = composer.freeze_projection_input(owner_a)
    skill.write_text(_document("alpha", body="# Version B\n"), encoding="utf-8")

    assert owner_a.discovery.skills[0].body == "# Version A\n"
    assert composer.freeze_projection_input(owner_a) == frozen_a
    owner_b = composer.freeze_owner_snapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    assert owner_b.discovery.skills[0].body == "# Version B\n"
    assert composer.freeze_projection_input(owner_b) != frozen_a


def test_round9_1_read_file_repeats_current_bytes_with_2000_line_window(
    tmp_path: Path,
) -> None:
    assert DEFAULT_READ_LINES == MAX_READ_LINES == 2_000
    path = tmp_path / "skill.md"
    path.write_text("\n".join(f"old-{index}" for index in range(2_001)), encoding="utf-8")
    tool = ReadFileTool(tmp_path)
    first = tool.execute(
        ToolCall("call:first", "read_file", {"path": "skill.md"})
    )
    first_payload = json.loads(first.output)
    assert first.status is ToolResultState.SUCCESS
    assert first_payload["limit"] == 2_000
    assert first_payload["truncated"] is True
    assert "2000|old-1999" in first_payload["content"]

    path.write_text("new-first\nsecond\n", encoding="utf-8")
    second = tool.execute(
        ToolCall("call:second", "read_file", {"path": "skill.md"})
    )
    second_payload = json.loads(second.output)
    assert second.status is ToolResultState.SUCCESS
    assert second_payload["content"] == "1|new-first\n2|second"
    assert "content_returned" not in second_payload


def test_round9_1_pulsara_home_catalog_location_is_ordinary_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pulsara_home = tmp_path / "custom-pulsara-home"
    skill_path = pulsara_home / "skills" / "home-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(_document("home-skill"), encoding="utf-8")
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))

    workspace = tmp_path / "workspace"
    provider = LocalSkillProvider(
        user_agents_skills_root=tmp_path / "empty-agents" / "skills"
    )
    discovery = provider.discover(_policy(provider, workspace))
    assert discovery.skills[0].location == (
        "${PULSARA_HOME}/skills/home-skill/SKILL.md"
    )

    result = ReadFileTool(workspace).execute(
        ToolCall(
            id="read-home-skill",
            name="read_file",
            arguments={"path": discovery.skills[0].location},
        )
    )
    assert result.status is ToolResultState.SUCCESS
    assert "home-skill" in result.output


def test_round9_1_production_has_no_fifth_root_or_skill_execution_authority() -> None:
    package_root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package_root.rglob("*.py"))
    )
    assert ".claude/skills" not in production
    assert "ACTIVATE_SKILL" not in production
    assert "skill_health" not in production
    semantic_contracts = "\n".join(
        (package_root / "capability" / name).read_text(encoding="utf-8")
        for name in ("types.py", "provider.py", "resolver.py")
    )
    assert "allowed_tools" not in semantic_contracts
    assert "provides_tools" not in semantic_contracts
    assert "RenderedSkillPrompt" not in semantic_contracts
    assert "catalog_rendered" not in semantic_contracts
    assert "active_skill_rendered" not in semantic_contracts
