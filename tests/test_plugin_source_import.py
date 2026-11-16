from tests.support.capability_management_targets import plugin_target


import json
from pathlib import Path
from time import monotonic

import pytest

from pulsara_agent.plugins.source_import import (
    PluginImportError,
    observe_plugin_import,
    preview_plugin_imports,
)
from pulsara_agent.plugins.contracts import (
    ValidateLocalPluginSourceRequest,
    NeverCancelPluginOperation,
)
from pulsara_agent.plugins.package_core import (
    PluginSourceObserver,
    revalidate_observation,
    PluginPackageRaced,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)


def fixture(tmp_path, manifest, source_format="codex"):
    source = tmp_path / "source"
    source.mkdir()
    path = source / f".{source_format}-plugin/plugin.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"name": "example", "version": "1.0.0", **manifest}))
    return source


def observe(source, source_format="codex", **kwargs):
    return observe_plugin_import(
        PluginSourceObserver(ProcessCredentialBoundary()),
        ValidateLocalPluginSourceRequest(
            source, monotonic() + 20, source_format=source_format, **kwargs
        ),
        scrub_set=ProcessCredentialScrubSet(),
    )


def discover(source):
    return preview_plugin_imports(
        source,
        deadline_monotonic=monotonic() + 20,
        cancellation=NeverCancelPluginOperation(),
        credential_boundary=ProcessCredentialBoundary(),
    )["candidates"]


@pytest.mark.parametrize("source_format", ["claude", "codex", "cursor"])
def test_discovery_identifies_single_distribution_without_user_format(
    tmp_path, source_format
):
    source = fixture(
        tmp_path, {"skills": [], "mcpServers": {}, "hooks": []}, source_format
    )
    candidates = discover(source)
    assert len(candidates) == 1
    assert candidates[0]["source_format"] == source_format
    assert candidates[0]["error"] is None
    assert candidates[0]["preview"]["mcp"] == []
    assert not (source / "plugin.json").exists()


def test_discovery_lists_distinct_distribution_components_without_union(tmp_path):
    source = fixture(tmp_path, {"skills": [], "mcpServers": {}, "hooks": []})
    (source / ".claude-plugin").mkdir()
    (source / ".claude-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "skills": [],
                "hooks": [],
                "mcpServers": {"search": {"url": "https://example.org/mcp"}},
            }
        )
    )
    candidates = {item["source_format"]: item for item in discover(source)}
    assert set(candidates) == {"claude", "codex"}
    assert candidates["codex"]["preview"]["mcp"] == []
    assert [item["server_id"] for item in candidates["claude"]["preview"]["mcp"]] == [
        "search"
    ]
    # Detection must not change publication's explicit selected-format semantics.
    with observe(source, "codex") as observed:
        assert observed.summary.mcp.mcp_servers == ()


@pytest.mark.parametrize("invalid_manifest", ['{"name":', '{"name": {}}'])
def test_discovery_keeps_invalid_distribution_visible_without_hiding_valid_sibling(
    tmp_path,
    invalid_manifest,
):
    source = fixture(tmp_path, {"skills": [], "mcpServers": {}, "hooks": []})
    (source / ".claude-plugin").mkdir()
    (source / ".claude-plugin/plugin.json").write_text(invalid_manifest)
    candidates = {item["source_format"]: item for item in discover(source)}
    assert candidates["claude"]["preview"] is None
    assert candidates["claude"]["error"]
    assert candidates["codex"]["preview"]["name"] == "example"


def test_discovery_native_uses_existing_component_observer(tmp_path):
    from tests.test_round9_3_agent_plugin_product import _package

    source = _package(tmp_path / "native")
    candidates = discover(source)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["source_format"] == "native"
    assert candidate["error"] is None
    assert candidate["preview"]["skills"] == ["fixture-skill"]
    assert candidate["preview"]["mcp"]


def test_discovery_missing_manifest_does_not_guess_from_skills(tmp_path):
    (tmp_path / "SKILL.md").write_text("Not a Plugin manifest")
    with pytest.raises(PluginImportError, match="未找到"):
        discover(tmp_path)


def test_explicit_empty_components_do_not_discover_neighbor_format(tmp_path):
    # Slack's Codex distribution declares {} even when Claude's .mcp.json exists.
    source = fixture(tmp_path, {"mcpServers": {}, "hooks": [], "skills": []})
    (source / ".mcp.json").write_text(
        '{"mcpServers":{"other":{"url":"https://example.org/mcp"}}}'
    )
    (source / "hooks").mkdir()
    (source / "hooks/hooks.json").write_text('{"hooks":{"Unsupported":[]}}')
    with observe(source) as observation:
        assert observation.summary.mcp.mcp_servers == ()
        assert observation.summary.hooks.hook_definitions == ()
        candidate = observation.source_path
    assert not candidate.exists()
    assert (source / ".mcp.json").exists()


@pytest.mark.parametrize("distribution", ["sentry-claude", "slack-codex"])
def test_pinned_market_manifests_preserve_selected_mcp_semantics(
    tmp_path, distribution
):
    market = Path(__file__).parent / "fixtures/market_plugins"
    source_format = distribution.rsplit("-", 1)[1]
    manifest = json.loads((market / f"{distribution}.json").read_text())
    source = fixture(tmp_path, manifest, source_format)
    (source / "LICENSE").write_bytes((market / "LICENSE").read_bytes())
    # A synthetic inert resource verifies full copying without vendoring or
    # executing either project's scripts. The manifest above is real upstream.
    skill = source / "skills/market-fixture"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: market-fixture\ndescription: Inspect the fixture resource\n---\n"
        "Read references/example.txt.\n"
    )
    (skill / "references").mkdir()
    (skill / "references/example.txt").write_bytes(b"market-resource\n")
    (source / ".mcp.json").write_text(
        '{"mcpServers":{"must-not-union":{"url":"https://example.org/mcp"}}}'
    )
    with observe(source, source_format) as observed:
        servers = observed.summary.mcp.mcp_servers
        if distribution == "sentry-claude":
            assert [
                (server.local_server_id, server.endpoint) for server in servers
            ] == [("sentry", "https://mcp.sentry.dev/mcp?utm_source=plugin")]
        else:
            assert servers == ()
        assert (
            observed.source_path / "skills/market-fixture/references/example.txt"
        ).read_bytes() == b"market-resource\n"
        assert (observed.source_path / "LICENSE").read_bytes() == (
            market / "LICENSE"
        ).read_bytes()


@pytest.mark.parametrize("source_format", ["claude", "codex", "cursor"])
def test_inline_mcp_and_complete_skill_resources_convert_once(tmp_path, source_format):
    source = fixture(
        tmp_path,
        {"mcpServers": {"docs": {"type": "http", "url": "https://example.org/mcp"}}},
        source_format,
    )
    skill = source / "skills/example"
    skill.mkdir(parents=True)
    text = b"---\nname: example\ndescription: Read the fixture\n---\nRead references/data.txt.\n"
    (skill / "SKILL.md").write_bytes(text)
    (skill / "references").mkdir()
    (skill / "references/data.txt").write_bytes(b"unaltered\x00resource")
    with observe(source, source_format) as observation:
        assert (
            observation.summary.mcp.mcp_servers[0].endpoint == "https://example.org/mcp"
        )
        assert (
            observation.source_path / "skills/example/SKILL.md"
        ).read_bytes() == text
        assert (
            observation.source_path / "skills/example/references/data.txt"
        ).read_bytes() == b"unaltered\x00resource"
        assert len(observation.summary.skills.skills) == 1


def test_private_template_is_missing_input_not_copied_secret(tmp_path):
    source = fixture(
        tmp_path,
        {
            "mcpServers": {
                "docs": {
                    "url": "https://example.org/mcp",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                }
            }
        },
    )
    with observe(source) as observation:
        server = observation.summary.mcp.mcp_servers[0]
        assert server.connection_inputs.inputs[0].private
        assert server.public_headers == ()
        assert (
            "Bearer ${TOKEN}" not in (observation.source_path / "mcp.json").read_text()
        )
    with observe(
        source, import_public_values=(("TOKEN", "must-not-store"),)
    ) as observed:
        assert observed.summary.mcp.mcp_servers == ()
        assert any("私有" in item.message for item in observed.diagnostics)
        assert "must-not-store" not in (observed.source_path / "mcp.json").read_text()


def test_unsupported_component_and_private_literal_are_inactive(tmp_path):
    source = fixture(tmp_path, {"apps": {"service": "app-123"}, "mcpServers": {}})
    with observe(source) as observed:
        assert any(item.component == "apps" for item in observed.diagnostics)
        assert observed.summary.mcp.mcp_servers == ()
    assert any("apps" in notice for notice in discover(source)[0]["preview"]["notices"])
    (source / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "mcpServers": {
                    "docs": {
                        "url": "https://example.org/mcp",
                        "headers": {"Authorization": "private-literal"},
                    }
                },
            }
        )
    )
    with observe(source) as observed:
        assert observed.summary.mcp.mcp_servers == ()
        assert any("凭据字面量" in item.message for item in observed.diagnostics)
        assert all(
            "private-literal" not in item.message for item in observed.diagnostics
        )
        assert "private-literal" not in (observed.source_path / "mcp.json").read_text()


def test_source_drift_is_rejected_and_external_resource_is_not_copied(tmp_path):
    source = fixture(tmp_path, {"mcpServers": {}})
    with observe(source) as observation:
        (source / "new-file").write_text("drift")
        with pytest.raises(PluginPackageRaced):
            revalidate_observation(
                observation,
                deadline_monotonic=monotonic() + 20,
                cancellation=NeverCancelPluginOperation(),
            )
    (source / "link").symlink_to(Path("/etc/passwd"))
    with observe(source) as observed:
        assert not (observed.source_path / "link").exists()
        assert (observed.source_path / "new-file").read_text() == "drift"


def test_public_input_and_classification_required_before_conversion(tmp_path):
    source = fixture(
        tmp_path,
        {
            "mcpServers": {
                "docs": {"url": "https://${HOST}/mcp", "headers": {"X-Region": "west"}}
            }
        },
    )
    with observe(source) as observed:
        assert observed.summary.mcp.mcp_servers == ()
        assert any("HOST" in item.message for item in observed.diagnostics)
    with observe(source, import_public_values=(("HOST", "example.org"),)) as observed:
        assert observed.summary.mcp.mcp_servers == ()
        assert any("X-Region" in item.message for item in observed.diagnostics)
    with observe(
        source,
        import_public_values=(("HOST", "example.org"),),
        import_classifications=(("docs.header:X-Region", "public"),),
    ) as observation:
        assert (
            observation.summary.mcp.mcp_servers[0].endpoint == "https://example.org/mcp"
        )
        assert observation.summary.mcp.mcp_servers[0].public_headers == (
            ("X-Region", "west"),
        )


def test_external_validation_reports_original_skill_files_after_cleanup(tmp_path):
    from pulsara_agent.plugins.management import PluginManagementService

    source = fixture(tmp_path, {"skills": ["custom/alpha", "elsewhere/beta"]})
    for relative, name in (("custom/alpha", "alpha"), ("elsewhere/beta", "beta")):
        path = source / relative / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            f"---\nname: {name}\ndescription: Read {name}\n---\nBody {name}.\n"
        )
    service = PluginManagementService(credential_boundary=ProcessCredentialBoundary())
    result = service.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(
            source, monotonic() + 20, source_format="codex"
        )
    )
    assert result.disposition.value == "VALID"
    paths = {item.name: Path(item.skill_path) for item in result.summary.skills.skills}
    assert paths == {
        "alpha": source / "custom/alpha/SKILL.md",
        "beta": source / "elsewhere/beta/SKILL.md",
    }
    assert "Body alpha." in paths["alpha"].read_text()
    assert "Body beta." in paths["beta"].read_text()


def test_external_install_uses_native_disabled_instance_and_preserves_source(tmp_path):
    import asyncio

    from pulsara_agent.capability.management_form import (
        AcceptedCapabilityFormSubmission,
    )
    from pulsara_agent.capability.render import render_catalog_prompt
    from pulsara_agent.capability.resolver import _catalog_entry
    from pulsara_agent.cli import _plugin_summary_payload
    from pulsara_agent.conversation_kernel.capability_management_execution import (
        CapabilityManagementCall,
    )
    from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
    from pulsara_agent.model_input.contracts import ModelInputScopeKind
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        InspectLocalPluginsRequest,
        PluginScopeKind,
    )
    from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
    from pulsara_agent.plugins.view import EnabledPluginViewOwner
    from pulsara_agent.ports.tool_execution import ToolCall, ToolResultState
    from pulsara_agent.primitives.context import thaw_json
    from pulsara_agent.tools.builtins.filesystem import ReadFileTool
    from tests.test_capability_management_preparation import preparation

    source = fixture(
        tmp_path,
        {"mcpServers": {"docs": {"type": "http", "url": "https://example.org/mcp"}}},
    )
    original = (source / ".codex-plugin/plugin.json").read_bytes()
    skill = source / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    document = "---\nname: example\ndescription: Read the fixture\n---\nFixture body.\n"
    skill.write_text(document)

    async def run():
        service = preparation(tmp_path)
        result = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(
                source, PluginScopeKind.USER, monotonic() + 20, source_format="codex"
            ),
            connections=service.mcp,
        )
        assert result.disposition.value == "INSTALLED", result
        installed_path = result.package_root / "skills/example/SKILL.md"
        assert result.summary.skills.skills[0].skill_path == str(installed_path)
        assert installed_path.read_text() == document
        summary = _plugin_summary_payload(result.summary)["skills"]["definitions"][0]
        assert summary == {
            "name": "example",
            "description": "Read the fixture",
            "skill_path": str(installed_path),
        }
        inspection = service.plugins.inspect_local_plugins(
            InspectLocalPluginsRequest(monotonic() + 20)
        )
        assert len(inspection.instances) == 1
        installed = inspection.instances[0]
        assert installed.summary.skills.skills[0].skill_path == str(installed_path)
        assert not installed.enabled
        assert (
            installed.summary.mcp.mcp_servers[0].endpoint == "https://example.org/mcp"
        )
        assert (
            json.loads((installed.package_root / "plugin.json").read_text())["$schema"]
            == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        )
        assert (source / ".codex-plugin/plugin.json").read_bytes() == original

        async def query(arguments, *, inspect=False):
            return await CapabilitySourceQuery(
                service,
                runtime=None,
                mcp_configs=(),
                scope=ModelInputScopeKind.ROOT,
                inspect_tool=None,
            ).execute(arguments)

        listed = await query({"kind": "SKILL"})
        row = next(
            item
            for item in [
                part["value"] for part in listed["content"] if part["type"] == "object"
            ]
            if item["name"] == "example"
        )
        assert row["target"] == {"kind": "SKILL", "skill_path": str(installed_path)}
        detail = await query({"kind": "SKILL", "target": row["target"]}, inspect=True)
        assert detail["content"][0]["value"]["target"]["skill_path"] == str(
            installed_path
        )
        enable = CapabilityManagementCall(
            service,
            await service.prepare(
                {
                    "action": "SET_PLUGIN_ENABLED",
                    "enabled": True,
                    "target": plugin_target(service, "example", scope="USER"),
                }
            ),
        )
        form = enable.form("review")
        shown = thaw_json(form.public_projection)["plugin"]["skills"][0]
        assert shown == summary
        enable.accept(
            AcceptedCapabilityFormSubmission(
                await form.prepare_submission({"enable_review_accepted": True})
            )
        )
        assert (await enable.execute()).state == "SUCCESS"
        view = EnabledPluginViewOwner(
            store=service.plugins._store(),
            credential_boundary=service.plugins._credential_boundary,
        ).observe(
            workspace_root=service.workspace_root,
            deadline_monotonic=monotonic() + 20,
            cancellation=NeverCancelPluginOperation(),
        )
        try:
            candidate = PluginSkillDefinitionProducer().observe(view).candidates[0]
            row = json.loads(render_catalog_prompt((_catalog_entry(candidate),)))[
                "skills"
            ][0]
            assert row["target"]["skill_path"] == str(installed_path)
            read = ReadFileTool(service.workspace_root).execute(
                ToolCall(
                    "read-plugin",
                    "read_file",
                    {"target": {"kind": "PATH", "path": row["target"]["skill_path"]}},
                )
            )
            assert read.status is ToolResultState.SUCCESS
            assert "Fixture body." in read.business_value.content[0].value
        finally:
            view.close()
        await service.mcp.aclose()

    asyncio.run(run())


def test_partial_import_keeps_resources_and_reports_unsupported_hooks(tmp_path):
    hooks = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo supported"},
                        {"type": "http", "url": "https://example.org/hook"},
                    ]
                }
            ],
            "ExternalHostEvent": [
                {"hooks": [{"type": "command", "command": "echo inert"}]}
            ],
        }
    }
    source = fixture(tmp_path, {"apps": {"app": "host-only"}, "hooks": hooks})
    (source / "commands").mkdir()
    (source / "commands/foreign.toml").write_text('prompt = "Gemini only"')
    preview = discover(source)[0]["preview"]
    assert preview["hooks"] == ["SessionStart"]
    assert any("apps" in value for value in preview["notices"])
    with observe(source) as observed:
        assert len(observed.summary.hooks.hook_definitions) == 1
        codes = {item.code for item in observed.diagnostics}
        assert "HOOK_CONFIG_UNKNOWN_EVENT" in codes
        assert "HOOK_CONFIG_KNOWN_UNSUPPORTED_HANDLER" in codes
        assert (
            observed.source_path / "commands/foreign.toml"
        ).read_text() == 'prompt = "Gemini only"'
        assert (
            json.loads(
                (observed.source_path / "dev.pulsara/hooks/hooks.json").read_text()
            )
            == hooks
        )


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "command", "command": 42},
        {"type": "command", "command": "echo yes", "unrecognizedExecutionFlag": True},
    ],
)
def test_bad_hook_handler_preserves_valid_sibling_and_skill(tmp_path, handler):
    source = fixture(
        tmp_path,
        {
            "apps": {"app": "host-only"},
            "hooks": {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                handler,
                                {"type": "command", "command": "echo valid"},
                            ]
                        }
                    ]
                }
            },
        },
    )
    skill = source / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: Use this Skill\n---\nBody.\n")
    with observe(source) as observed:
        assert [item.command for item in observed.summary.hooks.hook_definitions] == [
            "echo valid"
        ]
        assert [item.name for item in observed.summary.skills.skills] == ["example"]
        assert any("HANDLER" in str(item.code) for item in observed.diagnostics)


def test_unreferenced_gemini_commands_are_resources_in_claude_import(tmp_path):
    source = fixture(tmp_path, {}, "claude")
    (source / "commands").mkdir()
    (source / "commands/mode.toml").write_text('prompt = "mode"')
    with observe(source, "claude") as observed:
        assert not any(item.component == "commands" for item in observed.diagnostics)
        assert (observed.source_path / "commands/mode.toml").is_file()
    (source / "commands/host-command.md").write_text("Claude command")
    with observe(source, "claude") as observed:
        assert any(item.component == "commands" for item in observed.diagnostics)


def test_import_prevalidation_uses_declarations_without_hook_binding(
    tmp_path, monkeypatch
):
    from pulsara_agent.plugins import package_core

    source = fixture(
        tmp_path,
        {
            "hooks": {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo imported"}]}
                    ]
                }
            }
        },
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("prevalidation must not bind executable Hook provenance")

    monkeypatch.setattr(package_core, "parse_hook_config", forbidden)
    with observe(source) as observation:
        assert observation.hook_config is None
        assert observation.summary.hooks.hook_definitions[0].command == "echo imported"


def test_hook_array_keeps_order_and_ignores_only_broken_files(tmp_path):
    def hook(command):
        return {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": command}]}]
            }
        }

    source = fixture(
        tmp_path,
        {
            "hooks": [
                "./hooks/first.json",
                "./hooks/missing.json",
                "./hooks/broken.json",
                {**hook("echo second"), "modules": ["./ui.tsx"]},
                "./hooks/third.json",
            ]
        },
    )
    (source / "hooks").mkdir()
    (source / "hooks/first.json").write_text(json.dumps(hook("echo first")))
    (source / "hooks/third.json").write_text(json.dumps(hook("echo third")))
    (source / "hooks/broken.json").write_text('{"hooks":')
    script = source / "hooks/resource.js"
    script.write_bytes(b"throw Error('must not execute during import');\n")
    with observe(source) as observed:
        assert [item.command for item in observed.summary.hooks.hook_definitions] == [
            "echo first",
            "echo second",
            "echo third",
        ]
        text = "\n".join(item.message for item in observed.diagnostics)
        assert "missing.json" in text and "broken.json" in text and "modules" in text
        assert (
            observed.source_path / "hooks/resource.js"
        ).read_bytes() == script.read_bytes()
    notices = discover(source)[0]["preview"]["notices"]
    assert any("missing.json" in notice for notice in notices)


def test_bad_mcp_and_skill_entries_preserve_independent_content(tmp_path):
    source = fixture(
        tmp_path,
        {
            "skills": ["missing", "skills/good", "skills/bad"],
            "mcpServers": {
                "good": {"url": "https://example.org/mcp"},
                "unsupported": {"type": "websocket", "url": "wss://example.org/mcp"},
                "disabled": {"url": "https://example.org/off", "enabled": False},
                "needs_input": {"url": "https://${HOST}/mcp"},
                "needs_classification": {"command": "node", "env": {"REGION": "west"}},
            },
            "hooks": {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo active"}]}
                    ]
                }
            },
        },
    )
    good = source / "skills/good/SKILL.md"
    good.parent.mkdir(parents=True)
    good.write_text("---\nname: good\ndescription: Read this Skill\n---\nGood body.\n")
    bad = source / "skills/bad/SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("No frontmatter")
    with observe(source) as observed:
        assert [item.name for item in observed.summary.skills.skills] == ["good"]
        assert [item.local_server_id for item in observed.summary.mcp.mcp_servers] == [
            "good"
        ]
        assert [item.command for item in observed.summary.hooks.hook_definitions] == [
            "echo active"
        ]
        text = "\n".join(item.message for item in observed.diagnostics)
        for label in (
            "missing",
            "unsupported",
            "disabled",
            "needs_input",
            "needs_classification",
        ):
            assert label in text


def test_optional_foreign_metadata_does_not_invalidate_component(tmp_path):
    source = fixture(
        tmp_path,
        {
            "author": "Package Author",
            "description": 42,
            "homepage": {"url": "host metadata"},
            "keywords": ["good", 1],
            "mcpServers": {"docs": {"url": "https://example.org/mcp"}},
        },
    )
    with observe(source) as observed:
        assert observed.summary.manifest.author.name == "Package Author"
        assert observed.summary.manifest.description is None
        assert observed.summary.mcp.mcp_servers[0].local_server_id == "docs"
        assert any(item.component == "description" for item in observed.diagnostics)


def test_invalid_optional_variable_only_disables_dependent_server(tmp_path):
    source = fixture(
        tmp_path,
        {
            "variables": {
                "type": "object",
                "properties": {"HOST": {"type": "boolean", "default": True}},
            },
            "mcpServers": {
                "good": {"url": "https://example.org/mcp"},
                "dependent": {"url": "https://${HOST}/mcp"},
            },
        },
    )
    with observe(source) as observed:
        assert [item.local_server_id for item in observed.summary.mcp.mcp_servers] == [
            "good"
        ]
        assert any("HOST" in item.message for item in observed.diagnostics)


def test_expected_manifest_import_error_keeps_reason_in_validation_and_install(
    tmp_path,
):
    import asyncio
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        PluginScopeKind,
    )
    from tests.test_capability_management_preparation import preparation

    source = fixture(tmp_path, {})
    (source / ".codex-plugin/plugin.json").write_text('{"name":')
    service = preparation(tmp_path)
    validation = service.plugins.validate_local_plugin_source(
        ValidateLocalPluginSourceRequest(
            source, monotonic() + 20, source_format="codex"
        )
    )
    assert validation.disposition.value == "INVALID"
    assert ".codex-plugin/plugin.json" in validation.diagnostics[0].message

    async def run():
        try:
            result = await service.plugins.install_local_plugin(
                InstallLocalPluginRequest(
                    source,
                    PluginScopeKind.USER,
                    monotonic() + 20,
                    source_format="codex",
                ),
                connections=service.mcp,
            )
            assert result.disposition.value == "INVALID"
            assert ".codex-plugin/plugin.json" in result.diagnostics[0].message
        finally:
            await service.mcp.aclose()

    asyncio.run(run())


def test_bad_private_connection_target_does_not_poison_other_servers(tmp_path):
    source = fixture(
        tmp_path,
        {
            "mcpServers": {
                "good": {"url": "https://example.org/mcp"},
                "bad": {
                    "url": "https://example.org/private",
                    "headers": {
                        "Authorization": "Bearer ${TOKEN}",
                        "authorization": "Bearer ${OTHER_TOKEN}",
                    },
                },
            }
        },
    )
    with observe(source) as observed:
        assert [item.local_server_id for item in observed.summary.mcp.mcp_servers] == [
            "good"
        ]
        assert any(item.component == "bad" for item in observed.diagnostics)


def test_empty_endpoint_public_input_uses_preview_variable(tmp_path):
    source = fixture(tmp_path, {"mcpServers": {"docs": {"type": "http"}}})
    preview = discover(source)[0]["preview"]
    name = preview["mcp"][0]["fields"][0]["variables"][0]["name"]
    with observe(
        source, import_public_values=((name, "https://example.org/mcp"),)
    ) as observed:
        assert observed.summary.mcp.mcp_servers[0].endpoint == "https://example.org/mcp"


def test_malformed_stdio_args_cannot_be_coerced_and_preserves_other_server(tmp_path):
    source = fixture(
        tmp_path,
        {
            "mcpServers": {
                "bad": {"command": "node", "args": "server.js"},
                "good": {"url": "https://example.org/mcp"},
            }
        },
    )
    with observe(source) as observed:
        assert [item.local_server_id for item in observed.summary.mcp.mcp_servers] == [
            "good"
        ]
        assert any("args" in item.message for item in observed.diagnostics)


def test_selected_empty_hooks_preserve_vendor_resources_without_activation(tmp_path):
    source = fixture(tmp_path, {"hooks": [], "skills": [], "mcpServers": {}}, "claude")
    (source / ".codex-plugin").mkdir()
    codex = {
        "name": "neighbor",
        "hooks": {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "echo must-not-activate"},
                        ]
                    }
                ]
            }
        },
    }
    (source / ".codex-plugin/plugin.json").write_text(json.dumps(codex))
    (source / ".mcp.json").write_text(
        '{"mcpServers":{"neighbor":{"url":"https://example.org/mcp"}}}'
    )
    with observe(source, "claude") as observed:
        assert observed.summary.hooks.hook_definitions == ()
        assert observed.summary.mcp.mcp_servers == ()
        assert (
            json.loads((observed.source_path / ".codex-plugin/plugin.json").read_text())
            == codex
        )
        assert (observed.source_path / ".mcp.json").read_bytes() == (
            source / ".mcp.json"
        ).read_bytes()
        assert json.loads(
            (observed.source_path / "dev.pulsara/hooks/hooks.json").read_text()
        ) == {"hooks": {}}


def test_external_symlinks_and_custom_skill_keep_node_resource_layout(tmp_path):
    import subprocess

    source = fixture(tmp_path, {"skills": ["custom/demo"], "hooks": []})
    skill = source / "custom/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Use the demo\n---\nRead scripts/run.cjs.\n"
    )
    scripts = skill / "scripts"
    scripts.mkdir()
    # A script's own parent and a link target's own parent both matter to Node.
    program = b"const fs=require('fs'),p=require('path');console.log(fs.readFileSync(p.join(__dirname,'data.txt'),'utf8'));\n"
    (scripts / "run.cjs").write_bytes(program)
    (scripts / "data.txt").write_text("skill-relative")
    shared = source / "shared"
    shared.mkdir()
    (shared / "entry.cjs").write_bytes(program)
    (shared / "data.txt").write_text("target-relative")
    (scripts / "linked.cjs").symlink_to("../../../shared/entry.cjs")

    def assert_node_layout(root):
        canonical = root / "skills/demo"
        assert canonical.is_symlink()
        assert canonical.resolve() == root / "custom/demo"
        assert (canonical / "scripts/linked.cjs").is_symlink()
        assert (canonical / "scripts/run.cjs").read_bytes() == program
        for filename, expected in (
            ("run.cjs", "skill-relative"),
            ("linked.cjs", "target-relative"),
        ):
            result = subprocess.run(
                ["node", str(canonical / "scripts" / filename)],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == expected

    with observe(source) as observed:
        assert_node_layout(observed.source_path)

    import asyncio
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        InspectLocalPluginsRequest,
        PluginScopeKind,
    )
    from tests.test_capability_management_preparation import preparation

    async def run():
        owner = preparation(tmp_path)
        try:
            installed = await owner.plugins.install_local_plugin(
                InstallLocalPluginRequest(
                    source,
                    PluginScopeKind.USER,
                    monotonic() + 20,
                    source_format="codex",
                ),
                connections=owner.mcp,
            )
            assert installed.disposition.value == "INSTALLED", installed
            assert_node_layout(installed.package_root)
            inspection = owner.plugins.inspect_local_plugins(
                InspectLocalPluginsRequest(monotonic() + 20)
            )
            assert not inspection.instances[0].enabled
            assert [
                item.name for item in inspection.instances[0].summary.skills.skills
            ] == ["demo"]
        finally:
            await owner.mcp.aclose()

    asyncio.run(run())


def test_unpreservable_skill_link_does_not_remove_independent_skill(tmp_path):
    source = fixture(tmp_path, {"skills": ["skills/good", "skills/bad"]})
    for name in ("good", "bad"):
        skill = source / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Read this Skill\n---\nBody.\n"
        )
    unselected = source / "skills/unselected"
    unselected.mkdir()
    (unselected / "resource.txt").write_text("not selected")
    (source / "skills/bad/resource.txt").symlink_to("../unselected/resource.txt")
    with observe(source) as observed:
        assert [item.name for item in observed.summary.skills.skills] == ["good"]
        assert any(
            "bad" in item.message and "链接" in item.message
            for item in observed.diagnostics
        )


def test_foreign_skill_frontmatter_name_drives_preview_install_and_reads(tmp_path):
    import asyncio
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        PluginScopeKind,
    )
    from pulsara_agent.identity import random_id
    from pulsara_agent.ports.tool_execution import ToolCall, ToolResultState
    from pulsara_agent.tools.builtins.filesystem import ReadFileTool
    from tests.test_capability_management_preparation import preparation

    source = fixture(
        tmp_path,
        {"skills": ["skills/writing-rules", "skills/invalid", "skills/duplicate"]},
        "claude",
    )
    original = b"---\nname: writing-hookify-rules\ndescription: Write hook rules.\n---\nRead references/example.txt before writing rules.\n"
    for directory, content in (
        ("writing-rules", original),
        ("invalid", b"No frontmatter"),
        ("duplicate", original),
    ):
        skill = source / "skills" / directory
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(content)
    (source / "skills/writing-rules/references").mkdir()
    (source / "skills/writing-rules/references/example.txt").write_text(
        "authored resource"
    )
    preview = discover(source)[0]["preview"]
    assert preview["skills"] == ["writing-hookify-rules"]
    assert any("skills/invalid/SKILL.md" in notice for notice in preview["notices"])
    assert any(
        "skills/duplicate/SKILL.md" in notice and "冲突" in notice
        for notice in preview["notices"]
    )

    async def run():
        owner = preparation(tmp_path)
        try:
            result = await owner.plugins.install_local_plugin(
                InstallLocalPluginRequest(
                    source,
                    PluginScopeKind.USER,
                    monotonic() + 20,
                    source_format="claude",
                ),
                connections=owner.mcp,
            )
            assert result.disposition.value == "INSTALLED", result
            (skill,) = result.summary.skills.skills
            assert skill.name == "writing-hookify-rules"
            assert not any(
                str(getattr(item, "code", "")) == "skill_directory_name_mismatch"
                for item in result.diagnostics
            )
            assert (
                Path(skill.skill_path)
                == result.package_root / "skills/writing-rules/SKILL.md"
            )
            alias = result.package_root / "skills/writing-hookify-rules"
            assert alias.is_symlink()
            assert (alias / "SKILL.md").read_bytes() == original
            assert (alias / "references/example.txt").read_text() == "authored resource"
            reader = ReadFileTool(owner.workspace_root)
            # Both initial reading and later re-reading use the real saved Skill locator.
            for _ in range(2):
                read = reader.execute(
                    ToolCall(
                        random_id("call"),
                        "read_file",
                        {
                            "target": {"kind": "PATH", "path": skill.skill_path},
                        },
                    )
                )
                assert read.status is ToolResultState.SUCCESS
                assert (
                    "Read references/example.txt"
                    in read.business_value.content[0].value
                )
            assert (source / "skills/writing-rules/SKILL.md").read_bytes() == original
        finally:
            await owner.mcp.aclose()

    asyncio.run(run())


def test_foreign_mcp_presentation_note_is_ignored_but_auth_behavior_is_not(tmp_path):
    source = fixture(
        tmp_path,
        {
            "mcpServers": {
                "docs": {
                    "type": "http",
                    "url": "https://example.org/mcp",
                    "note": "Use this service for docs.",
                },
                "unknown_auth": {
                    "url": "https://example.org/auth",
                    "oauth_resource": "https://example.org/resource",
                },
            }
        },
    )
    preview = discover(source)[0]["preview"]
    assert [item["server_id"] for item in preview["mcp"]] == ["docs"]
    assert any("docs" in notice and "note" in notice for notice in preview["notices"])
    with observe(source) as observed:
        assert [item.local_server_id for item in observed.summary.mcp.mcp_servers] == [
            "docs"
        ]
        assert any(
            item.component == "unknown_auth" and "oauth_resource" in item.message
            for item in observed.diagnostics
        )
