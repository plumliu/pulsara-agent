from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
import sys

import pytest

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
        ToolCall("call:skill", "read_file", {"target": {"kind": "PATH", "path": str(installer.path)}})
    )
    reference = installer.base_dir / "references" / "directory-contract.md"
    reference_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:reference", "read_file", {"target": {"kind": "PATH", "path": str(reference)}})
    )
    assert skill_read.status is ToolResultState.SUCCESS
    assert reference_read.status is ToolResultState.SUCCESS
    assert json.loads(skill_read.output)["target"]["path"] == str(installer.path)
    assert json.loads(reference_read.output)["target"]["path"] == str(reference)

    plugin_installer = next(
        item for item in result.candidates if item.name == "pulsara-plugin-installer"
    )
    plugin_skill_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:plugin-skill", "read_file", {"target": {"kind": "PATH", "path": str(plugin_installer.path)}})
    )
    assert plugin_skill_read.status is ToolResultState.SUCCESS
    assert json.loads(plugin_skill_read.output)["target"]["path"] == str(plugin_installer.path)
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
                {"target": {"kind": "PATH", "path": str(plugin_reference)}},
            )
        )
        assert plugin_reference_read.status is ToolResultState.SUCCESS
        assert json.loads(plugin_reference_read.output)["target"]["path"] == str(
            plugin_reference
        )

    mcp_installer = next(
        item for item in result.candidates if item.name == "pulsara-mcp-installer"
    )
    mcp_skill_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:mcp-skill", "read_file", {"target": {"kind": "PATH", "path": str(mcp_installer.path)}})
    )
    assert mcp_skill_read.status is ToolResultState.SUCCESS
    assert json.loads(mcp_skill_read.output)["target"]["path"] == str(mcp_installer.path)
    mcp_reference = mcp_installer.base_dir / "references" / "connection-configuration.md"
    mcp_reference_read = ReadFileTool(tmp_path).execute(
        ToolCall("call:mcp-config-reference", "read_file", {"target": {"kind": "PATH", "path": str(mcp_reference)}})
    )
    assert mcp_reference_read.status is ToolResultState.SUCCESS
    assert json.loads(mcp_reference_read.output)["target"]["path"] == str(mcp_reference)

    analysis = next(item for item in result.candidates if item.name == "pulsara-data-analysis")
    assert analysis.source is SkillSource.BUNDLED
    assert analysis.origin == BundledSkillOrigin("bundled_skills/pulsara-data-analysis")
    for relative_path in (
        "SKILL.md",
        "references/environment-data.md",
        "references/explore-transform.md",
        "references/analysis-methods.md",
        "references/visualization-render.md",
        "references/html-environment.md",
        "references/html-theme-css.md",
        "references/multi-view-analysis.md",
        "examples/analyze_csv.py",
    ):
        resource = analysis.base_dir / relative_path
        resource_read = ReadFileTool(tmp_path).execute(
            ToolCall(
                f"call:analysis-resource:{relative_path}",
                "read_file",
                {"target": {"kind": "PATH", "path": str(resource)}},
            )
        )
        assert resource_read.status is ToolResultState.SUCCESS
        assert json.loads(resource_read.output)["target"]["path"] == str(resource)

    docs = next(item for item in result.candidates if item.name == "pulsara-docs")
    assert docs.source is SkillSource.BUNDLED
    assert docs.origin == BundledSkillOrigin("bundled_skills/pulsara-docs")
    for relative_path in (
        "SKILL.md",
        "references/read-inspect.md",
        "references/create-template.md",
        "references/edit-review.md",
        "references/notes-math.md",
        "references/render-verify.md",
        "references/formats-passwords.md",
        "scripts/inspect_docx.py",
        "scripts/render_docx.py",
        "examples/create_report.py",
        "examples/replace_text.py",
        "examples/fill_template.py",
        "examples/decrypt_document.py",
    ):
        resource = docs.base_dir / relative_path
        resource_read = ReadFileTool(tmp_path).execute(
            ToolCall(
                f"call:docs-resource:{relative_path}",
                "read_file",
                {"target": {"kind": "PATH", "path": str(resource)}},
            )
        )
        assert resource_read.status is ToolResultState.SUCCESS
        assert json.loads(resource_read.output)["target"]["path"] == str(resource)

    pdf = next(item for item in result.candidates if item.name == "pulsara-pdf")
    assert pdf.source is SkillSource.BUNDLED
    assert pdf.origin == BundledSkillOrigin("bundled_skills/pulsara-pdf")
    for relative_path in (
        "SKILL.md",
        "references/read-extract.md",
        "references/create-verify.md",
    ):
        resource = pdf.base_dir / relative_path
        resource_read = ReadFileTool(tmp_path).execute(
            ToolCall(
                f"call:pdf-resource:{relative_path}",
                "read_file",
                {"target": {"kind": "PATH", "path": str(resource)}},
            )
        )
        assert resource_read.status is ToolResultState.SUCCESS
        assert json.loads(resource_read.output)["target"]["path"] == str(resource)

    sheets = next(item for item in result.candidates if item.name == "pulsara-sheets")
    assert sheets.source is SkillSource.BUNDLED
    assert sheets.origin == BundledSkillOrigin("bundled_skills/pulsara-sheets")
    for relative_path in (
        "SKILL.md",
        "references/read-inspect.md",
        "references/create-edit.md",
        "references/formulas-recalc.md",
        "references/charts-layout.md",
        "references/formats-passwords.md",
        "references/verify-deliver.md",
        "scripts/inspect_workbook.py",
        "examples/create_workbook.py",
    ):
        resource = sheets.base_dir / relative_path
        resource_read = ReadFileTool(tmp_path).execute(
            ToolCall(
                f"call:sheets-resource:{relative_path}",
                "read_file",
                {"target": {"kind": "PATH", "path": str(resource)}},
            )
        )
        assert resource_read.status is ToolResultState.SUCCESS
        assert json.loads(resource_read.output)["target"]["path"] == str(resource)



@pytest.mark.parametrize("available,decision,stopped_phase", [
    (False, False, None), (True, False, None), (True, True, None),
    (True, True, 1), (True, True, 2),
])
def test_code_mode_workflow_example_reuses_real_targets_and_guards_decision(
    available: bool, decision: bool, stopped_phase: int | None,
) -> None:
    from pulsara_agent.code_mode import CodeModeToolResult
    from pulsara_agent.identity import is_valid_id, random_id

    source = _code_mode_example("agent-orchestration/workflows.md", 0)
    calls = []
    batches = []
    readme = {"target": {"kind": "PATH", "path": "README.md"}, "content": "Project context"}
    fix_work = {"kind": "WORK", "work_id": random_id("queue")}

    async def invoke(call):
        calls.append(call)
        if call.tool_name == "read_file":
            assert call.arguments == {"target": readme["target"], "limit": 160}
            value = readme
        elif call.tool_name == "run_agent.batch":
            members = call.arguments["members"]
            value = {
                member["key"]: {
                    "agent": {"kind": "AGENT", "agent_id": member["agent_id"]},
                    "work": {"kind": "WORK", "work_id": member["work_id"]},
                }
                for member in members
            }
            assert all(is_valid_id(item["agent"]["agent_id"], "session") for item in value.values())
            assert all(is_valid_id(item["work"]["work_id"], "queue") for item in value.values())
            if batches:
                revision, implementation, review = members
                assert revision["parameters"]["target"] == batches[0]["design"]["agent"]
                assert revision["parameters"]["materials"] == [batches[0]["critic"]["work"]]
                assert review["parameters"]["target"] == batches[0]["critic"]["agent"]
                assert implementation["parameters"]["depends_on"] == [value["revision"]["work"]]
                assert review["parameters"]["depends_on"] == [
                    value["revision"]["work"], value["implementation"]["work"],
                ]
            batches.append(value)
        elif call.tool_name == "wait_agent.works":
            value = {"state": "settled", "works": [], "pending_targets": []}
        elif call.tool_name == "query_agents":
            stopped = stopped_phase == len(batches)
            value = {
                "target": call.arguments["target"],
                "status": "stopped" if stopped else "completed",
                "final": None if stopped else "Concrete review",
                "diagnostic": "Stopped by user" if stopped else None,
            }
        elif call.tool_name == "decide":
            assert available
            assert set(call.arguments) == {"condition", "inputs"}
            assert json.loads(call.arguments["inputs"][0]) == {
                "project_context": readme["content"], "review": "Concrete review",
            }
            value = {"decisions": [decision]}
        elif call.tool_name == "run_agent":
            assert available and decision
            assert call.arguments["target"] == batches[1]["implementation"]["agent"]
            assert call.arguments["materials"] == [batches[1]["review"]["work"]]
            value = {"agent": call.arguments["target"], "work": fix_work}
        else:
            raise AssertionError(call.tool_name)
        return CodeModeToolResult(value=value)

    output = asyncio.run(_execute_code_mode_example(source, invoke, available))
    names = [call.tool_name for call in calls]
    if stopped_phase is not None:
        assert len(batches) == stopped_phase
        assert names == ["read_file"] + [
            "run_agent.batch", "wait_agent.works", "query_agents",
        ] * stopped_phase
        stopped_review = output[-1]["proposalReview" if stopped_phase == 1 else "codeReview"]
        assert stopped_review == {
            "target": batches[-1]["critic" if stopped_phase == 1 else "review"]["work"],
            "status": "stopped", "final": None, "diagnostic": "Stopped by user",
        }
        return
    assert len(batches) == 2
    assert names == [
        "read_file", "run_agent.batch", "wait_agent.works", "query_agents",
        "run_agent.batch", "wait_agent.works", "query_agents",
        *(["decide"] if available else []),
        *(["run_agent", "wait_agent.works", "query_agents"] if available and decision else []),
    ]
    assert output[0]["phase1"] == batches[0]
    assert output[1]["phase2"] == batches[1]
    if not available:
        assert output[-1]["condition"]
        assert json.loads(output[-1]["inputs"][0])["review"] == "Concrete review"
        assert output[-1]["source"] == readme
        assert output[-1]["phase2"] == batches[1]
        assert output[-1]["codeReview"]["target"] == batches[1]["review"]["work"]
    elif decision:
        assert output[-1]["target"] == fix_work
    else:
        assert output[-1]["codeReview"]["target"] == batches[1]["review"]["work"]


@pytest.mark.parametrize("available", [False, True])
def test_code_mode_history_example_preserves_sources_and_positional_judgments(
    available: bool,
) -> None:
    from pulsara_agent.code_mode import CodeModeToolResult
    from pulsara_agent.identity import random_id

    from pulsara_agent.ports.session_content import session_entry_target

    candidates = []
    for text in ("Keep Skill guidance concrete", "Install this Skill", "Keep Skill guidance concrete"):
        target = session_entry_target(random_id("session"), random_id("entry"))
        candidates.append({
            "target": target, "title": "Skill discussion", "score": 1.0,
            "messages": [{
                "role": "user", "label": "user", "at": "2026-11-07T10:00:00Z", "text": text,
                "target": target,
                "read": {"tool": "read_session_content", "arguments": {"target": target, "limit": 1}},
            }],
        })
    source = "const page = " + json.dumps({"items": candidates}) + ";\n"
    source += _code_mode_example("tool-calling/examples.md", 3)
    calls = []

    async def invoke(call):
        calls.append(call)
        assert available and call.tool_name == "decide"
        assert set(call.arguments) == {"condition", "inputs"}
        inputs = [json.loads(item) for item in call.arguments["inputs"]]
        assert inputs == [
            [{key: message[key] for key in ("role", "at", "text")} for message in item["messages"]]
            for item in candidates
        ]
        return CodeModeToolResult(value={"decisions": [True, False, True]})

    output = asyncio.run(_execute_code_mode_example(source, invoke, available))
    assert len(output) == 1
    if available:
        assert len(calls) == 1
        assert output == [{"matches": [candidates[0], candidates[2]]}]
    else:
        assert calls == []
        assert output[0]["condition"]
        assert len(output[0]["inputs"]) == 3
        assert output[0]["candidates"] == candidates


def _code_mode_example(reference: str, index: int) -> str:
    with BundledSkillDistributionBindingOwner() as owner:
        inventory = BundledSkillDefinitionProducer(owner).observe()
        skill = next(item for item in inventory.candidates if item.name == "pulsara-code-mode")
        text = (skill.base_dir / "references" / reference).read_text(encoding="utf-8")
    return re.findall(r"```js\n(.*?)\n```", text, re.DOTALL)[index]


async def _execute_code_mode_example(source, invoke, available):
    from pulsara_agent.code_mode import CodeModeHost
    from pulsara_agent.identity import random_id

    host = CodeModeHost()
    output = []
    try:
        cell_id = await host.start(
            source=source, source_exec_attempt_id=random_id("attempt"),
            turn_id=random_id("turn"), snapshot=object(),
            tool_catalog={name: {} for name in ("read_file", "run_agent", "wait_agent", "query_agents", "decide")},
            invoke=invoke, decision_available=available,
        )
        cell = host._cell(cell_id)
        async with asyncio.timeout(15):
            while True:
                lease = await host.lease_output(cell_id) if cell_id in host._cells else None
                if lease is not None:
                    assert lease.record["kind"] == "log", lease.record
                    output.append(json.loads(lease.record["text"]))
                    await host.acknowledge_output(lease)
                elif cell.state in {"completed", "failed", "terminated"} and not cell.pending and not cell.output:
                    assert cell.state == "completed", cell.error
                    return output
                else:
                    await asyncio.sleep(0.001)
    finally:
        await host.aclose()

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
