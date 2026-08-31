from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionProducer,
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.local_skills import (
    FrozenLooseSkillDefinitions,
    LooseSkillDefinitionProducer,
    LooseSkillDefinitionsDisposition,
)
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    SkillCatalogResolver,
    UnavailableEffectiveSkillCatalogInspection,
)
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.capability.types import (
    ResolutionUnavailableCause,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillProducerUnavailableReason,
    SkillResolutionUnavailableReason,
)
from pulsara_agent.capability.user_skill_config import (
    set_user_skill_enabled,
    workspace_skill_config_path,
)
from pulsara_agent.conversation_kernel.capability import KernelSkillProjectionComposer
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
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


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


def _producer(tmp_path: Path, **kwargs: object) -> LooseSkillDefinitionProducer:
    return LooseSkillDefinitionProducer(
        user_product_skills_root=tmp_path / "test-user-product-skills",
        user_agents_skills_root=tmp_path / "test-user-agent-skills",
        **kwargs,
    )


def _policy(producer: LooseSkillDefinitionProducer, workspace: Path):
    return producer.prepare_root_policy(workspace)


def _diagnostics(result: FrozenLooseSkillDefinitions) -> tuple[SkillDiagnostic, ...]:
    if result.unavailable_cause is not None:
        return result.unavailable_cause.diagnostics
    return tuple(
        diagnostic
        for issue in result.invalid_issues
        for diagnostic in issue.diagnostics
    )


def test_round9_1_root_policy_is_exactly_two_or_four_and_scope_bound(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    first = _producer(tmp_path / "first")
    second = _producer(tmp_path / "second")
    assert tuple(item.root_kind for item in _policy(first, workspace).roots) == (
        LocalSkillRootKind.WORKSPACE_PULSARA,
        LocalSkillRootKind.WORKSPACE_AGENTS,
        LocalSkillRootKind.USER_PULSARA,
        LocalSkillRootKind.USER_AGENTS,
    )

    frozen = _policy(first, workspace)
    assert not hasattr(frozen, "conversation_scope_kind")
    assert not hasattr(frozen, "scope_subagent_task_id")
    with pytest.raises(ValueError, match="foreign loose Skill root policy"):
        second.observe(frozen)
    with pytest.raises(TypeError, match="_constructor"):
        replace(frozen.roots[0], path=tmp_path / "swapped")


def test_round9_1_root_policy_bounds_cannot_be_relaxed() -> None:
    for kwargs in (
        {"max_skill_file_bytes": 64 * 1024 + 1},
        {"maximum_direct_child_directories": 1_025},
        {"maximum_discovery_skill_bytes": 16 * 1024 * 1024 + 1},
    ):
        with pytest.raises(ValueError, match="closed maximum"):
            LooseSkillDefinitionProducer(**kwargs)


def test_round9_1_membership_change_before_final_revalidation_invalidates_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.capability.local_skills as local_skills

    workspace = tmp_path / "workspace"
    first = _write_skill(workspace, "alpha")
    producer = _producer(tmp_path)
    policy = _policy(producer, workspace)
    original = local_skills.read_observed_skill_document
    installed = False

    def read_and_install(
        child, *, maximum: int, deadline_monotonic, cancellation=None
    ) -> bytes:
        nonlocal installed
        if not installed:
            installed = True
            _write_skill(workspace, "beta")
        return original(
            child,
            maximum=maximum,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )

    monkeypatch.setattr(local_skills, "read_observed_skill_document", read_and_install)
    current = producer.observe(policy)
    assert first.exists()
    assert current.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert current.candidates == ()
    successor = producer.observe(policy)
    assert [item.name for item in successor.candidates] == ["alpha", "beta"]


def test_round9_1_enumerated_member_loss_invalidates_whole_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pulsara_agent.capability.local_skills as local_skills

    workspace = tmp_path / "workspace"
    _write_skill(workspace, "alpha")
    victim = _write_skill(workspace, "beta")
    producer = _producer(tmp_path)
    policy = _policy(producer, workspace)
    original = local_skills.read_observed_skill_document
    reads = 0

    def remove_before_second_read(
        child, *, maximum: int, deadline_monotonic, cancellation=None
    ) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            victim.unlink()
        return original(
            child,
            maximum=maximum,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )

    monkeypatch.setattr(
        local_skills, "read_observed_skill_document", remove_before_second_read
    )
    result = producer.observe(policy)
    assert result.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert result.unavailable_cause is not None
    assert (
        result.unavailable_cause.reason
        is SkillProducerUnavailableReason.LOOSE_DISCOVERY_RACED
    )
    assert result.candidates == ()


def test_round9_1_non_regular_skill_file_is_bounded_unavailable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".agents" / "skills" / "fifo-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    os.mkfifo(skill_path)
    producer = _producer(tmp_path)

    result = producer.observe(_policy(producer, workspace))

    assert result.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert result.unavailable_cause is not None
    assert (
        result.unavailable_cause.reason
        is SkillProducerUnavailableReason.LOOSE_DISCOVERY_RACED
    )
    assert result.candidates == ()


def test_round9_1_explicit_null_metadata_is_not_standard_valid(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(
        workspace,
        "null-metadata",
        document=_document("null-metadata", extra="metadata:\n"),
    )
    producer = _producer(tmp_path)

    result = producer.observe(_policy(producer, workspace))

    assert result.disposition is LooseSkillDefinitionsDisposition.COMPLETE
    assert result.candidates == ()
    assert [item.code for item in _diagnostics(result)] == [
        SkillDiagnosticCode.INVALID_METADATA
    ]


def test_round9_1_inert_skill_diagnostics_are_not_public_degradation() -> None:
    diagnostics = (
        SkillDiagnostic(
            severity=SkillDiagnosticSeverity.INFO,
            code=SkillDiagnosticCode.HOST_EXTENSION_IGNORED,
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
    direct_producer = _producer(tmp_path / "direct-user")
    direct = direct_producer.observe(_policy(direct_producer, direct_workspace))
    assert direct.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE
    assert direct.unavailable_cause is not None
    assert (
        direct.unavailable_cause.reason
        is SkillProducerUnavailableReason.LOOSE_DISCOVERY_OVERBOUND
    )
    assert direct.candidates == ()

    winner_workspace = tmp_path / "winners"
    for index in range(65):
        _write_skill(winner_workspace, f"skill-{index:02d}")
    winner_producer = _producer(tmp_path / "winner-user")
    loose = winner_producer.observe(_policy(winner_producer, winner_workspace))
    with BundledSkillDistributionBindingOwner() as binding:
        bundled = BundledSkillDefinitionProducer(binding).observe()
    inspection = SkillCatalogResolver().resolve(
        loose,
        FrozenPluginSkillDefinitions(PluginSkillDefinitionsDisposition.COMPLETE),
        bundled,
    )
    assert isinstance(inspection, UnavailableEffectiveSkillCatalogInspection)
    assert inspection.winners == ()
    assert len(inspection.unavailable_causes) == 1
    cause = inspection.unavailable_causes[0]
    assert isinstance(cause, ResolutionUnavailableCause)
    assert (
        cause.reason is SkillResolutionUnavailableReason.EFFECTIVE_WINNER_BOUND_EXCEEDED
    )


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
    _write_skill(workspace, "alpha", document=f"---\n{frontmatter}---\nbody\n")
    producer = _producer(tmp_path)
    result = producer.observe(_policy(producer, workspace))
    assert result.candidates == ()
    assert [item.code for item in _diagnostics(result)] == [
        SkillDiagnosticCode.INVALID_FRONTMATTER_YAML
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
    producer = _producer(tmp_path)
    binding = BundledSkillDistributionBindingOwner()
    composer = KernelSkillProjectionComposer(
        workspace_root=workspace,
        bundled_binding_owner=binding,
        plugin_definitions_provider=lambda: FrozenPluginSkillDefinitions(
            PluginSkillDefinitionsDisposition.COMPLETE
        ),
        loose_producer=producer,
    )
    try:
        owner_a = composer.freeze_owner_snapshot(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        frozen_a = composer.freeze_projection_input(owner_a)
        skill.write_text(_document("alpha", body="# Version B\n"), encoding="utf-8")

        assert isinstance(owner_a.inspection, CompleteEffectiveSkillCatalogInspection)
        alpha_a = next(
            item for item in owner_a.inspection.winners if item.name == "alpha"
        )
        assert alpha_a.body == "# Version A\n"
        assert composer.freeze_projection_input(owner_a) == frozen_a
        owner_b = composer.freeze_owner_snapshot(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        alpha_b = next(
            item for item in owner_b.inspection.winners if item.name == "alpha"
        )
        assert alpha_b.body == "# Version B\n"
        assert composer.freeze_projection_input(owner_b) != frozen_a
    finally:
        binding.close()


def test_round9_1_workspace_skill_switch_applies_on_the_next_owner_snapshot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill = _write_skill(workspace, "alpha")
    producer = _producer(tmp_path)
    binding = BundledSkillDistributionBindingOwner()
    composer = KernelSkillProjectionComposer(
        workspace_root=workspace,
        bundled_binding_owner=binding,
        plugin_definitions_provider=lambda: FrozenPluginSkillDefinitions(
            PluginSkillDefinitionsDisposition.COMPLETE
        ),
        loose_producer=producer,
    )
    try:
        enabled = composer.freeze_owner_snapshot(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        assert isinstance(enabled.inspection, CompleteEffectiveSkillCatalogInspection)
        assert "alpha" in {item.name for item in enabled.inspection.winners}

        set_user_skill_enabled(
            skill_path=skill,
            enabled=False,
            config_path=workspace_skill_config_path(workspace),
        )
        disabled = composer.freeze_owner_snapshot(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        assert isinstance(disabled.inspection, CompleteEffectiveSkillCatalogInspection)
        assert "alpha" not in {item.name for item in disabled.inspection.winners}
    finally:
        binding.close()


def test_round9_1_read_file_repeats_current_bytes_with_2000_line_window(
    tmp_path: Path,
) -> None:
    assert DEFAULT_READ_LINES == MAX_READ_LINES == 2_000
    path = tmp_path / "skill.md"
    path.write_text(
        "\n".join(f"old-{index}" for index in range(2_001)), encoding="utf-8"
    )
    tool = ReadFileTool(tmp_path)
    first = tool.execute(ToolCall("call:first", "read_file", {"path": "skill.md"}))
    first_payload = json.loads(first.output)
    assert first.status is ToolResultState.SUCCESS
    assert first_payload["limit"] == 2_000
    assert first_payload["truncated"] is True
    assert "2000|old-1999" in first_payload["content"]

    path.write_text("new-first\nsecond\n", encoding="utf-8")
    second = tool.execute(ToolCall("call:second", "read_file", {"path": "skill.md"}))
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
    producer = LooseSkillDefinitionProducer(
        user_agents_skills_root=tmp_path / "empty-agents" / "skills"
    )
    result = producer.observe(_policy(producer, workspace))
    home_skill = next(item for item in result.candidates if item.name == "home-skill")
    assert home_skill.location == "${PULSARA_HOME}/skills/home-skill/SKILL.md"

    read = ReadFileTool(workspace).execute(
        ToolCall(
            id="read-home-skill",
            name="read_file",
            arguments={"path": home_skill.location},
        )
    )
    assert read.status is ToolResultState.SUCCESS
    assert "home-skill" in read.output


def test_round9_1_production_has_no_fifth_root_or_skill_execution_authority() -> None:
    package_root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    production = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(package_root.rglob("*.py"))
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
