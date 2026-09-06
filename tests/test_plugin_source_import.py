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
    with pytest.raises(PluginImportError, match="私有"):
        observe(source, import_public_values=(("TOKEN", "must-not-store"),))


def test_unknown_active_component_and_private_literal_reject_whole_bundle(tmp_path):
    source = fixture(tmp_path, {"apps": {"service": "app-123"}, "mcpServers": {}})
    with pytest.raises(PluginImportError, match="apps"):
        observe(source)
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
    with pytest.raises(PluginImportError, match="凭据字面量") as error:
        observe(source)
    assert "private-literal" not in str(error.value)


def test_source_drift_and_symlink_do_not_bypass_native_admission(tmp_path):
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
    with pytest.raises(ValueError):
        observe(source)


def test_public_input_and_classification_required_before_conversion(tmp_path):
    source = fixture(
        tmp_path,
        {
            "mcpServers": {
                "docs": {"url": "https://${HOST}/mcp", "headers": {"X-Region": "west"}}
            }
        },
    )
    with pytest.raises(PluginImportError, match="HOST"):
        observe(source)
    with pytest.raises(PluginImportError, match="X-Region"):
        observe(source, import_public_values=(("HOST", "example.org"),))
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


def test_external_install_uses_native_disabled_instance_and_preserves_source(tmp_path):
    import asyncio
    from tests.test_capability_management_preparation import preparation
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        InspectLocalPluginsRequest,
        PluginScopeKind,
    )

    source = fixture(
        tmp_path,
        {"mcpServers": {"docs": {"type": "http", "url": "https://example.org/mcp"}}},
    )
    original = (source / ".codex-plugin/plugin.json").read_bytes()

    async def run():
        service = preparation(tmp_path)
        result = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(
                source, PluginScopeKind.USER, monotonic() + 20, source_format="codex"
            ),
            connections=service.mcp,
        )
        assert result.disposition.value == "INSTALLED", result
        inspection = service.plugins.inspect_local_plugins(
            InspectLocalPluginsRequest(monotonic() + 20)
        )
        assert len(inspection.instances) == 1
        installed = inspection.instances[0]
        assert not installed.enabled
        assert (
            installed.summary.mcp.mcp_servers[0].endpoint == "https://example.org/mcp"
        )
        assert (
            json.loads((installed.package_root / "plugin.json").read_text())["$schema"]
            == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        )
        assert (source / ".codex-plugin/plugin.json").read_bytes() == original
        await service.mcp.aclose()

    asyncio.run(run())
