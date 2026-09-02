from __future__ import annotations

import json
from pathlib import Path
import sys

from pulsara_agent.capability.bundled_skills import (
    EXPECTED_BUNDLED_SKILL_NAMES,
    BundledInventoryEntry,
    BundledSkillDefinitionProducer,
    BundledSkillDefinitionsDisposition,
    BundledSkillDistributionBindingOwner,
    classify_bundled_skill_inventory,
)
from pulsara_agent.capability.local_skills import LooseSkillDefinitionProducer
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    SkillCatalogResolver,
)
from pulsara_agent.capability.types import (
    BundledSkillOrigin,
    LooseSkillOrigin,
    ShadowedSkillCandidateIssue,
    SkillProducerUnavailableReason,
    SkillSource,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.tools.builtins.filesystem import ReadFileTool


def test_bundled_definition_producer_reads_package_without_user_writes(
    tmp_path: Path,
) -> None:
    pulsara_home = tmp_path / "pulsara-home"
    with BundledSkillDistributionBindingOwner() as owner:
        observed = BundledSkillDefinitionProducer(owner).observe()

    assert observed.disposition is BundledSkillDefinitionsDisposition.COMPLETE
    assert tuple(item.name for item in observed.candidates) == (
        EXPECTED_BUNDLED_SKILL_NAMES
    )
    assert all(
        isinstance(item.origin, BundledSkillOrigin) for item in observed.candidates
    )
    assert all(item.source is SkillSource.BUNDLED for item in observed.candidates)
    assert not pulsara_home.exists()


def test_loose_marker_cannot_claim_bundled_origin(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user-skills"
    skill = _write_source_skill(
        user_root,
        "pulsara-skill-creator",
        description="User override.",
    )
    (skill / ".pulsara-skill-source.json").write_text(
        '{"source":"bundled"}\n', encoding="utf-8"
    )

    inspection = _inspect(workspace, user_root=user_root)

    winner = next(
        item for item in inspection.winners if item.name == "pulsara-skill-creator"
    )
    assert winner.source is SkillSource.USER
    assert isinstance(winner.origin, LooseSkillOrigin)
    assert any(
        isinstance(item, ShadowedSkillCandidateIssue)
        and item.name == "pulsara-skill-creator"
        and isinstance(item.origin, BundledSkillOrigin)
        for item in inspection.candidate_issues
    )


def test_process_binding_reuses_descriptor_and_rejects_rebound_root(
    tmp_path: Path,
) -> None:
    resource_root = _write_bundled_root(tmp_path / "bundled")
    with BundledSkillDistributionBindingOwner(
        _test_resource_root=resource_root
    ) as owner:
        producer = BundledSkillDefinitionProducer(owner)
        first = producer.observe()
        second = producer.observe()

        displaced = tmp_path / "displaced-bundled"
        resource_root.rename(displaced)
        replacement = _write_bundled_root(resource_root)
        (replacement / "pulsara-skill-creator" / "SKILL.md").write_text(
            "---\nname: pulsara-skill-creator\n"
            "description: Rebound bytes must not be adopted.\n---\nnew\n",
            encoding="utf-8",
        )
        rebound = producer.observe()

    assert first == second
    assert first.disposition is BundledSkillDefinitionsDisposition.COMPLETE
    assert rebound.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert rebound.unavailable_cause is not None
    assert rebound.unavailable_cause.reason is (
        SkillProducerUnavailableReason.BUNDLED_DISCOVERY_RACED
    )

    if sys.platform == "darwin":
        system_alias_root = _write_bundled_root(tmp_path / "system-alias-bundled")
        physical = system_alias_root.resolve()
        private_var = Path("/private/var")
        try:
            suffix = physical.relative_to(private_var)
        except ValueError:
            pass
        else:
            alias = Path("/var") / suffix
            with BundledSkillDistributionBindingOwner(
                _test_resource_root=alias
            ) as alias_owner:
                aliased = BundledSkillDefinitionProducer(alias_owner).observe()
            assert aliased.disposition is BundledSkillDefinitionsDisposition.COMPLETE


def test_workspace_loose_definition_shadows_bundled_default(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    override = _write_source_skill(
        workspace / ".pulsara" / "skills",
        "pulsara-skill-installer",
        description="Workspace modified definition.",
        body="# Workspace body\n",
    )

    inspection = _inspect(workspace, user_root=tmp_path / "user-skills")

    winner = next(
        item for item in inspection.winners if item.name == "pulsara-skill-installer"
    )
    assert winner.path == override / "SKILL.md"
    assert winner.body == "# Workspace body\n"
    with BundledSkillDistributionBindingOwner() as owner:
        bundled = BundledSkillDefinitionProducer(owner).observe()
    bundled_item = next(
        item for item in bundled.candidates if item.name == "pulsara-skill-installer"
    )
    assert bundled_item.body != winner.body


def test_delete_loose_override_falls_back_to_bundled_default(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user-skills"
    default = _inspect(workspace, user_root=user_root)
    default_winner = next(
        item for item in default.winners if item.name == "pulsara-skill-creator"
    )
    assert default_winner.source is SkillSource.BUNDLED

    override = _write_source_skill(user_root, "pulsara-skill-creator")
    overridden = _inspect(workspace, user_root=user_root)
    override_winner = next(
        item for item in overridden.winners if item.name == "pulsara-skill-creator"
    )
    assert override_winner.source is SkillSource.USER
    assert override_winner.path == override / "SKILL.md"

    (override / "SKILL.md").unlink()
    override.rmdir()

    fallback = _inspect(workspace, user_root=user_root)

    winner = next(
        item for item in fallback.winners if item.name == "pulsara-skill-creator"
    )
    assert winner.source is SkillSource.BUNDLED
    assert winner.origin == default_winner.origin
    assert not override.exists()


def test_bundled_inventory_defects_are_whole_batch_unavailable(
    tmp_path: Path,
) -> None:
    resource_root = _write_bundled_root(tmp_path / "bundled")
    missing = resource_root / EXPECTED_BUNDLED_SKILL_NAMES[0]
    (missing / "SKILL.md").unlink()
    missing.rmdir()

    with BundledSkillDistributionBindingOwner(
        _test_resource_root=resource_root
    ) as owner:
        result = BundledSkillDefinitionProducer(owner).observe()

    assert result.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert result.candidates == ()
    assert result.unavailable_cause is not None
    assert (
        result.unavailable_cause.reason
        is SkillProducerUnavailableReason.BUNDLED_INVENTORY_MISMATCH
    )

    extra_root = _write_bundled_root(tmp_path / "extra-bundled")
    (extra_root / "README").write_text("extra", encoding="utf-8")
    with BundledSkillDistributionBindingOwner(
        _test_resource_root=extra_root
    ) as owner:
        extra = BundledSkillDefinitionProducer(owner).observe()
    assert extra.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert extra.unavailable_cause is not None
    assert extra.unavailable_cause.reason is (
        SkillProducerUnavailableReason.BUNDLED_INVENTORY_MISMATCH
    )

    hidden_root = _write_bundled_root(tmp_path / "hidden-bundled")
    (hidden_root / ".build-metadata").mkdir()
    with BundledSkillDistributionBindingOwner(
        _test_resource_root=hidden_root
    ) as owner:
        hidden = BundledSkillDefinitionProducer(owner).observe()
    assert hidden.disposition is BundledSkillDefinitionsDisposition.COMPLETE

    nonregular_root = _write_bundled_root(tmp_path / "nonregular-bundled")
    nonregular_document = (
        nonregular_root / EXPECTED_BUNDLED_SKILL_NAMES[0] / "SKILL.md"
    )
    nonregular_document.unlink()
    nonregular_document.mkdir()
    with BundledSkillDistributionBindingOwner(
        _test_resource_root=nonregular_root
    ) as owner:
        nonregular = BundledSkillDefinitionProducer(owner).observe()
    assert nonregular.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert nonregular.unavailable_cause is not None
    assert nonregular.unavailable_cause.reason is (
        SkillProducerUnavailableReason.BUNDLED_INVENTORY_MISMATCH
    )

    invalid_root = _write_bundled_root(tmp_path / "invalid-bundled")
    (invalid_root / EXPECTED_BUNDLED_SKILL_NAMES[0] / "SKILL.md").write_text(
        "not frontmatter\n", encoding="utf-8"
    )
    with BundledSkillDistributionBindingOwner(
        _test_resource_root=invalid_root
    ) as owner:
        invalid = BundledSkillDefinitionProducer(owner).observe()
    assert invalid.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert invalid.unavailable_cause is not None
    assert invalid.unavailable_cause.reason is (
        SkillProducerUnavailableReason.BUNDLED_DEFINITION_INVALID
    )


def test_bundled_definition_observation_ignores_old_opt_out_marker(
    tmp_path: Path,
) -> None:
    pulsara_home = tmp_path / "pulsara-home"
    pulsara_home.mkdir()
    marker = pulsara_home / ".pulsara-skip-bundled-skills"
    marker.write_text("", encoding="utf-8")

    with BundledSkillDistributionBindingOwner() as owner:
        result = BundledSkillDefinitionProducer(owner).observe()

    assert result.disposition is BundledSkillDefinitionsDisposition.COMPLETE
    assert marker.is_file()
    assert not (pulsara_home / "skills").exists()


def test_user_loose_definition_shadows_bundled_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user-skills"
    existing = _write_source_skill(
        user_root,
        "pulsara-skill-installer",
        description="Unmanaged user definition.",
    )

    inspection = _inspect(workspace, user_root=user_root)

    winner = next(
        item for item in inspection.winners if item.name == "pulsara-skill-installer"
    )
    assert winner.path == existing / "SKILL.md"
    assert winner.source is SkillSource.USER
    assert "Unmanaged user definition" in winner.description


def test_skills_cli_has_only_four_current_commands(
    tmp_path: Path,
) -> None:
    import pulsara_agent.capability.bundled_skills as bundled_skills
    import pulsara_agent.cli as cli

    assert not hasattr(bundled_skills, "reset_bundled_skill")
    parser = cli.build_parser()
    skills = next(
        action for action in parser._actions if action.dest == "command"
    ).choices["skills"]
    subcommands = next(
        action for action in skills._actions if action.dest == "skills_command"
    ).choices
    assert set(subcommands) == {"validate", "install", "list", "doctor"}
    assert not (tmp_path / "backup").exists()


def test_unavailable_distribution_binding_does_not_reopen(
    tmp_path: Path,
) -> None:
    pulsara_home = tmp_path / "pulsara-home"
    missing_root = tmp_path / "missing-distribution"
    with BundledSkillDistributionBindingOwner(
        _test_resource_root=missing_root
    ) as owner:
        first = BundledSkillDefinitionProducer(owner).observe()
        _write_bundled_root(missing_root)
        second = BundledSkillDefinitionProducer(owner).observe()

    assert first == second
    assert first.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert first.unavailable_cause is not None
    assert first.unavailable_cause.reason is (
        SkillProducerUnavailableReason.BUNDLED_RESOURCE_UNAVAILABLE
    )
    assert not pulsara_home.exists()


def test_closed_distribution_binding_observes_typed_unavailable() -> None:
    owner = BundledSkillDistributionBindingOwner()
    owner.close()

    observed = BundledSkillDefinitionProducer(owner).observe()

    assert observed.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE
    assert observed.unavailable_cause is not None
    assert observed.unavailable_cause.reason is (
        SkillProducerUnavailableReason.BUNDLED_DISCOVERY_RACED
    )


def test_installed_bundled_inventory_is_exact_and_ordinary_readable(
    tmp_path: Path,
) -> None:
    assert EXPECTED_BUNDLED_SKILL_NAMES == (
        "pulsara-mcp-installer",
        "pulsara-plugin-installer",
        "pulsara-skill-creator",
        "pulsara-skill-installer",
    )
    with BundledSkillDistributionBindingOwner() as owner:
        result = BundledSkillDefinitionProducer(owner).observe()

    assert result.disposition is BundledSkillDefinitionsDisposition.COMPLETE
    assert tuple(item.name for item in result.candidates) == EXPECTED_BUNDLED_SKILL_NAMES
    classification = classify_bundled_skill_inventory(
        BundledInventoryEntry(name, True, True)
        for name in EXPECTED_BUNDLED_SKILL_NAMES
    )
    assert classification.valid
    assert classification.diagnostics == ()
    assert not classify_bundled_skill_inventory(
        (
            *(BundledInventoryEntry(name, True, True) for name in EXPECTED_BUNDLED_SKILL_NAMES),
            BundledInventoryEntry("README", False, False),
        )
    ).valid

    installer = next(
        item for item in result.candidates if item.name == "pulsara-skill-installer"
    )
    skill_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:skill", "read_file", {"path": str(installer.path)})
    )
    reference = installer.base_dir / "references" / "directory-contract.md"
    reference_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:reference", "read_file", {"path": str(reference)})
    )
    assert skill_read.status is ToolResultState.SUCCESS
    assert reference_read.status is ToolResultState.SUCCESS
    assert json.loads(skill_read.output)["path"] == str(installer.path)
    assert json.loads(reference_read.output)["path"] == str(reference)

    plugin_installer = next(
        item for item in result.candidates if item.name == "pulsara-plugin-installer"
    )
    plugin_skill_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:plugin-skill", "read_file", {"path": str(plugin_installer.path)})
    )
    assert plugin_skill_read.status is ToolResultState.SUCCESS
    assert json.loads(plugin_skill_read.output)["path"] == str(plugin_installer.path)
    for reference_name in (
        "conversion-contract.md",
        "codex-compatible.md",
        "pulsara-hook-extension.md",
    ):
        plugin_reference = plugin_installer.base_dir / "references" / reference_name
        plugin_reference_read = ReadFileTool(tmp_path).execute(
            ToolCall(
                f"call:plugin-reference:{reference_name}",
                "read_file",
                {"path": str(plugin_reference)},
            )
        )
        assert plugin_reference_read.status is ToolResultState.SUCCESS
        assert json.loads(plugin_reference_read.output)["path"] == str(
            plugin_reference
        )

    mcp_installer = next(
        item for item in result.candidates if item.name == "pulsara-mcp-installer"
    )
    mcp_skill_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:mcp-skill", "read_file", {"path": str(mcp_installer.path)})
    )
    assert mcp_skill_read.status is ToolResultState.SUCCESS
    assert json.loads(mcp_skill_read.output)["path"] == str(mcp_installer.path)


def _inspect(
    workspace: Path,
    *,
    user_root: Path,
) -> CompleteEffectiveSkillCatalogInspection:
    producer = LooseSkillDefinitionProducer(
        user_product_skills_root=user_root,
        user_agents_skills_root=workspace / ".test-user-agents",
    )
    loose = producer.observe(producer.prepare_root_policy(workspace))
    with BundledSkillDistributionBindingOwner() as owner:
        bundled = BundledSkillDefinitionProducer(owner).observe()
    inspection = SkillCatalogResolver().resolve(
        loose,
        FrozenPluginSkillDefinitions(PluginSkillDefinitionsDisposition.COMPLETE),
        bundled,
    )
    assert isinstance(inspection, CompleteEffectiveSkillCatalogInspection)
    return inspection


def _write_bundled_root(root: Path) -> Path:
    for name in EXPECTED_BUNDLED_SKILL_NAMES:
        _write_source_skill(root, name)
    return root


def _write_source_skill(
    root: Path,
    name: str,
    *,
    description: str = "A bundled skill.",
    body: str = "# Bundled Skill\n",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
---
{body}""",
        encoding="utf-8",
    )
    return skill_dir
