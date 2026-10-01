"""Unified source queries prove observable selection and existing owner boundaries."""

import asyncio
import json
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.capability.source_query import (
    CapabilityQueryError,
    list_input_schema,
    inspect_input_schema,
    parse_query,
    render_page,
    minimal_row,
)
from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from tests.test_capability_management_preparation import preparation, install_plugin
from tests.test_app_capability_boundary import skill_source, hook_document, invoke


async def query(
    service,
    arguments,
    *,
    inspect=False,
    runtime=None,
    plan=None,
    scope=ModelInputScopeKind.ROOT,
):
    values = parse_query(
        arguments, inspect=inspect, subagent=scope is ModelInputScopeKind.SUBAGENT_TASK
    )
    return await CapabilitySourceQuery(
        service, runtime=runtime, plan=plan, scope=scope
    ).execute(values, inspect=inspect)


@pytest.mark.parametrize(
    "arguments",
    [
        {"kind": "PLUGIN"},
        {"limit": True},
        {"offset": True},
        {"offset": -1},
        {"cursor": "old"},
        {"workspace_root": "/other"},
        {"parent": {"kind": "PLUGIN", "scope": "USER"}},
        {
            "kind": "MCP_TOOL",
            "parent": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "a"},
        },
        {
            "source_kind": "LOCAL",
            "parent": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "a"},
        },
        {"source_kind": "BUNDLED", "kind": "MCP_TOOL"},
    ],
)
def test_query_inputs_are_closed(arguments):
    with pytest.raises(CapabilityQueryError, match="."):
        parse_query(arguments)


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "MCP_SERVER", "runtime_server_id": "s", "scope": "USER"},
        {"kind": "PLUGIN", "scope": "USER", "plugin_id": "*", "search": "a"},
        {"kind": "SKILL", "skill_path": "SKILL.md"},
        {"kind": "MCP_TOOL", "server_id": "s", "tool_name": "t", "permit": True},
    ],
)
def test_inspect_targets_are_closed(target):
    with pytest.raises(CapabilityQueryError):
        parse_query({"target": target}, inspect=True)


def test_query_catalog_hard_cut_and_local_availability():
    for name in ("list_capabilities", "inspect_capability"):
        entry = builtin_tool_catalog_entry(name)
        assert entry.availability_requirement.kind.value == "always"
        assert entry.descriptor.is_read_only
    for name in (
        "list_mcp_servers",
        "inspect_new_mcp_tool",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "list_mcp_prompts",
    ):
        with pytest.raises(KeyError):
            builtin_tool_catalog_entry(name)
    for schema in (list_input_schema(), inspect_input_schema()):
        Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    "arguments,inspect",
    [
        ({"scope": "USER"}, False),
        ({"source_kind": "PLUGIN"}, False),
        ({"kind": "SKILL"}, False),
        ({"target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "known"}}, True),
        (
            {
                "target": {
                    "kind": "MCP_SERVER",
                    "scope": "USER",
                    "source_kind": "LOCAL",
                    "server_id": "a",
                }
            },
            True,
        ),
    ],
)
def test_child_does_not_query_installation_sources(arguments, inspect):
    with pytest.raises(CapabilityQueryError) as error:
        parse_query(arguments, inspect=inspect, subagent=True)
    assert error.value.code == "ROOT_ONLY"


def test_offset_pages_are_complete_without_cursor_state():
    rows = [
        minimal_row(
            "MCP_SERVER",
            f"s{i}",
            {"kind": "MCP_SERVER", "runtime_server_id": f"s{i}"},
            None,
            "尚未发现目录。",
        )
        for i in range(3)
    ]
    first = render_page(rows, {"limit": 1})
    assert first["returned_count"] == 1 and first["next_offset"] == 1
    second = render_page(rows, {"limit": 1, "offset": first["next_offset"]})
    assert second["items"][0]["name"] == "s1"
    assert render_page(rows, {"offset": 99})["items"] == []
    assert render_page(rows, {"offset": 99})["next_offset"] is None
    assert (
        render_page(
            rows,
            {},
            diagnostics=[{"code": "UNAVAILABLE", "message": "actual failure"}],
            completeness="PARTIAL",
        )["completeness"]
        == "PARTIAL"
    )


def test_no_mcp_port_lists_local_and_bundled_without_full_configuration(
    tmp_path, monkeypatch
):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        installed = await invoke(
            service,
            action="INSTALL_LOOSE_SKILL",
            scope="WORKSPACE",
            source_path=str(source),
        )
        path = installed["identity"]["skill_path"]
        listed = await query(service, {"kind": "SKILL"})
        item = next(
            item for item in listed["items"] if item["target"]["skill_path"] == path
        )
        assert set(item) <= {
            "kind",
            "name",
            "description",
            "target",
            "source",
            "status",
        }
        assert item["source"] == {"kind": "LOCAL", "scope": "WORKSPACE"}
        detail = await query(service, {"target": item["target"]}, inspect=True)
        assert detail["skill_path"] == path and detail["skill_root"] == str(
            Path(path).parent
        )
        assert "root_kind" not in json.dumps(listed)
        assert any(item["source"] == {"kind": "BUNDLED"} for item in listed["items"])
        await service.mcp.aclose()

    asyncio.run(run())


def test_exact_plugin_isolated_from_unrelated_corrupt_state(tmp_path, monkeypatch):
    async def run():
        service = preparation(tmp_path)
        await install_plugin(service, tmp_path, hooks=hook_document())
        store = service.plugins._store()
        from pulsara_agent.plugins.contracts import PluginScopeKind

        layout = store.layout(
            store.identity(
                scope=PluginScopeKind.USER, plugin_id="broken", workspace_root=None
            )
        )
        layout.state_parent.mkdir(parents=True, exist_ok=True)
        layout.state_path.write_text("broken JSON")
        target = {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"}
        result = await query(service, {"target": target}, inspect=True)
        assert set(result) == {
            "target",
            "version",
            "description",
            "package_root",
            "enabled",
            "components",
            "diagnostics",
        }
        assert (
            result["enabled"] is False
            and result["components"]["mcp_servers"]["count"] == 1
        )
        assert Path(result["package_root"]).is_dir()
        children = await query(service, {"parent": target})
        assert children["items"] and all(
            item["source"]["target"] == target for item in children["items"]
        )
        assert all("来源已禁用" in item["status"] for item in children["items"])
        assert not (
            store.layout(
                store.identity(
                    scope=PluginScopeKind.USER, plugin_id="example", workspace_root=None
                )
            ).data_root
        ).exists()
        default = await query(service, {})
        assert default["completeness"] == "PARTIAL"
        assert "broken" not in json.dumps(default) and any(
            item["kind"] == "SKILL" for item in default["items"]
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_mcp_paths_and_advanced_configuration_are_not_list_payload(tmp_path):
    async def run():
        service = preparation(tmp_path)
        config = {
            "display_name": "Docs",
            "enabled": False,
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
                "stateless_http_asserted": True,
            },
            "supports_parallel_tool_calls": True,
        }
        result = await invoke(
            service,
            action="ADD_LOCAL_MCP",
            scope="USER",
            server_id="docs",
            config=config,
        )
        assert "config" not in result["current"]
        assert result["current"]["config_path"] == str(service.mcp.user_config_path)
        listing = await query(service, {"kind": "MCP_SERVER", "source_kind": "LOCAL"})
        row = listing["items"][0]
        assert "config_path" not in row and "scope" not in row["source"]
        details = await query(service, {"target": row["target"]}, inspect=True)
        assert details["config_path"] == result["current"]["config_path"]
        assert details["connection"] == {
            "type": "streamable_http",
            "endpoint": "https://example.org/mcp",
        }
        wire = json.dumps(details)
        for internal in (
            "stateless_http_asserted",
            "supports_parallel_tool_calls",
            "effect_policy",
            "exposure_policy",
            "auth",
            "fingerprint",
        ):
            assert internal not in wire
        page = await query(service, {"kind": "MCP_TOOL", "parent": row["target"]})
        assert page["items"] == [] and page["completeness"] == "PARTIAL"
        await service.mcp.aclose()

    asyncio.run(run())


def test_transient_project_scope_is_not_terminal_cwd(tmp_path):
    async def run():
        service = preparation(tmp_path)
        service.workspace_kind = "transient"
        with pytest.raises(CapabilityQueryError) as error:
            await query(service, {"scope": "WORKSPACE"})
        assert error.value.code == "WORKSPACE_UNAVAILABLE"
        assert not (service.workspace_root / ".pulsara").exists()
        await service.mcp.aclose()

    asyncio.run(run())


def test_current_project_selection_applies_to_user_rows(tmp_path):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        user = await invoke(
            service, action="INSTALL_LOOSE_SKILL", scope="USER", source_path=str(source)
        )
        project = await invoke(
            service,
            action="INSTALL_LOOSE_SKILL",
            scope="WORKSPACE",
            source_path=str(source),
        )
        listing = await query(service, {"kind": "SKILL", "source_kind": "LOCAL"})
        items = {item["target"]["skill_path"]: item for item in listing["items"]}
        user_detail = await query(
            service,
            {"target": items[user["identity"]["skill_path"]]["target"]},
            inspect=True,
        )
        project_detail = await query(
            service,
            {"target": items[project["identity"]["skill_path"]]["target"]},
            inspect=True,
        )
        assert user_detail["selected"] is False
        assert project_detail["selected"] is True
        assert "覆盖" in items[user["identity"]["skill_path"]]["status"]
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("invalid", [False, True])
def test_empty_and_rejected_plugin_components_do_not_form_plugin_list_rows(
    tmp_path, invalid
):
    async def run():
        from pulsara_agent.plugins.contracts import (
            InstallLocalPluginRequest,
            PluginScopeKind,
        )

        service = preparation(tmp_path)
        source = tmp_path / "empty-source"
        source.mkdir()
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "empty",
                }
            )
        )
        if invalid:
            skill = source / "skills" / "bad"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("no frontmatter")
        outcome = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 5),
            connections=service.mcp,
        )
        assert outcome.disposition.value == "INSTALLED"
        target = {"kind": "PLUGIN", "scope": "USER", "plugin_id": "empty"}
        detail = await query(service, {"target": target}, inspect=True)
        assert detail["version"] is None
        assert detail["components"]["skills"]["count"] == 0
        assert bool(detail["diagnostics"]) is invalid
        page = await query(service, {"parent": target})
        assert all(item["kind"] == "SKILL" for item in page["items"])
        assert len(page["items"]) == int(invalid)
        assert page["total_count"] == int(invalid)
        assert all(item["kind"] != "PLUGIN" for item in page["items"])
        assert not (
            service.plugins._store().layout(outcome.identity).data_root
        ).exists()
        await service.mcp.aclose()

    asyncio.run(run())


def test_exact_corrupt_target_keeps_real_target_diagnostic(tmp_path):
    async def run():
        from pulsara_agent.plugins.contracts import PluginScopeKind

        service = preparation(tmp_path)
        store = service.plugins._store()
        layout = store.layout(
            store.identity(
                scope=PluginScopeKind.USER, plugin_id="bad", workspace_root=None
            )
        )
        layout.state_parent.mkdir(parents=True)
        layout.state_path.write_text("broken JSON")
        with pytest.raises(CapabilityQueryError) as error:
            await query(
                service,
                {"target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "bad"}},
                inspect=True,
            )
        assert error.value.code == "PLUGIN_UNAVAILABLE"
        assert error.value.diagnostics
        assert any(
            "JSON" in item["message"] or "state" in item["message"].lower()
            for item in error.value.diagnostics
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_subagent_runtime_detail_never_reads_host_config_or_source(
    tmp_path, monkeypatch
):
    import pulsara_agent.conversation_kernel.capability_query as owner

    target = {"kind": "MCP_SERVER", "runtime_server_id": "docs"}
    detail = {
        "target": target,
        "name": "Docs",
        "connection_status": "READY",
        "catalog_discovered": True,
        "diagnostics": [],
    }
    monkeypatch.setattr(
        owner,
        "runtime_rows",
        lambda *args: (
            [minimal_row("MCP_SERVER", "Docs", target, None, "已连接。")],
            {("MCP_SERVER", "docs"): detail},
            [],
        ),
    )
    monkeypatch.setattr(
        owner,
        "frozen_runtime_config",
        lambda *args: pytest.fail("child query read private slot config"),
    )
    result = asyncio.run(
        query(
            None,
            {"target": target},
            inspect=True,
            scope=ModelInputScopeKind.SUBAGENT_TASK,
        )
    )
    assert result["source"] is None
    assert set(result) == {
        "target",
        "name",
        "source",
        "connection_status",
        "catalog_discovered",
        "diagnostics",
    }


def test_root_runtime_summary_is_exact_candidate_configuration(monkeypatch):
    from pulsara_agent.conversation_kernel.mcp.directory import frozen_runtime_config

    config = SimpleNamespace(
        semantic_config_fingerprint="s",
        runtime_config_fingerprint="r",
        resolved_config_identity="i",
    )
    candidate = SimpleNamespace(
        slot_lease=SimpleNamespace(
            _slot=SimpleNamespace(client=SimpleNamespace(config=config))
        ),
        expected_semantic_config_fingerprint="s",
        expected_runtime_config_fingerprint="r",
        expected_resolved_config_identity="i",
    )
    runtime = SimpleNamespace(candidates={"docs": candidate})
    assert frozen_runtime_config(runtime, "docs") is config
    candidate.expected_runtime_config_fingerprint = "newer"
    assert frozen_runtime_config(runtime, "docs") is None
    assert frozen_runtime_config(runtime, "unknown") is None


def test_page_budget_advances_and_single_row_overbound_is_visible():
    from pulsara_agent.primitives.tool_observation import (
        MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
    )

    rows = [
        minimal_row(
            "SKILL",
            str(i),
            {"kind": "SKILL", "skill_path": f"/skills/{i}/SKILL.md"},
            None,
            "known",
            "x" * 8000,
        )
        for i in range(20)
    ]
    page = render_page(rows, {"limit": 20})
    assert 0 < page["returned_count"] < 20
    assert page["next_offset"] == page["returned_count"]
    second = render_page(rows, {"limit": 20, "offset": page["next_offset"]})
    assert not {json.dumps(item["target"]) for item in page["items"]} & {
        json.dumps(item["target"]) for item in second["items"]
    }
    rows[0]["description"] = "x" * MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    with pytest.raises(CapabilityQueryError) as error:
        render_page(rows, {"limit": 20})
    assert error.value.code == "CAPABILITY_ROW_OVERBOUND"


def test_repeated_query_cancellation_joins_native_observation_and_closes_anchor(
    tmp_path,
):
    from threading import Event
    from pulsara_agent.plugins.contracts import (
        PluginInspectionOutcome,
        PluginInspectionDisposition,
    )

    async def run():
        service = preparation(tmp_path)
        started, release, closed = Event(), Event(), Event()

        class Anchor:
            def close(self):
                closed.set()

        def observe():
            started.set()
            release.wait(2)
            return PluginInspectionOutcome(
                PluginInspectionDisposition.COMPLETE,
                (),
                (),
                (),
                physical_lifetime_anchors=(Anchor(),),
            )

        owner = CapabilitySourceQuery(
            service, runtime=None, plan=None, scope=ModelInputScopeKind.ROOT
        )
        work = asyncio.create_task(owner._native(observe))
        assert await asyncio.to_thread(started.wait, 2)
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done() and owner.cancellation.cancellation_requested()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert closed.is_set()
        await service.mcp.aclose()

    asyncio.run(run())


def test_credential_scrub_happens_before_page_budget(tmp_path, monkeypatch):
    import pulsara_agent.conversation_kernel.capability_query as owner
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
    from pulsara_agent.capability.source_query import fits_result

    service = preparation(tmp_path)
    scrub = ProcessCredentialScrubSet()
    scrub.observe("short-test-key")
    monkeypatch.setattr(service.plugins, "_capture_scrub_set", lambda request: scrub)
    rows = [
        minimal_row(
            "MCP_SERVER",
            f"server-{i}",
            {"kind": "MCP_SERVER", "runtime_server_id": f"server-{i}"},
            None,
            "known",
            "short-test-key " * 400,
        )
        for i in range(20)
    ]
    monkeypatch.setattr(
        owner,
        "runtime_rows",
        lambda *args: (
            rows,
            {
                ("MCP_SERVER", item["target"]["runtime_server_id"]): {
                    "target": item["target"],
                    "name": item["name"],
                    "connection_status": "READY",
                    "catalog_discovered": True,
                    "diagnostics": [],
                }
                for item in rows
            },
            [],
        ),
    )
    monkeypatch.setattr(owner, "frozen_runtime_config", lambda *args: None)

    async def run():
        result = await query(
            service, {"limit": 20}, scope=ModelInputScopeKind.ROOT, runtime=None
        )
        assert 0 < result["returned_count"] < 20
        assert result["next_offset"] == result["returned_count"] and fits_result(result)
        assert "short-test-key" not in json.dumps(result)
        assert "[REDACTED_CREDENTIAL]" in json.dumps(result)
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
def test_returned_plugin_skill_target_roundtrips_despite_unrelated_broken_package(
    tmp_path, scope
):
    async def run():
        from pulsara_agent.plugins.contracts import (
            InstallLocalPluginRequest,
            PluginScopeKind,
        )

        service = preparation(tmp_path)
        source = tmp_path / "healthy"
        source.mkdir()
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "healthy",
                }
            )
        )
        note = skill_source(source / "skills", "healthy-note")
        note.rename(source / "skills" / "healthy-note")
        note.parent.rmdir()
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(
                source,
                PluginScopeKind(scope),
                monotonic() + 10,
                workspace_root=service.workspace_root if scope == "WORKSPACE" else None,
            ),
            connections=service.mcp,
        )
        assert installed.disposition.value == "INSTALLED"
        store = service.plugins._store()
        bad = store.layout(
            store.identity(
                scope=PluginScopeKind.USER, plugin_id="bad", workspace_root=None
            )
        )
        bad.state_parent.mkdir(parents=True, exist_ok=True)
        bad.state_path.write_text("invalid JSON")
        rows = await query(
            service,
            {"parent": {"kind": "PLUGIN", "scope": scope, "plugin_id": "healthy"}},
        )
        (row,) = rows["items"]
        detail = await query(service, {"target": row["target"]}, inspect=True)
        assert detail["name"] == "healthy-note" and detail["source"] == row["source"]
        assert (
            detail["selected"] is False
        )  # Source disabled, independently of composition.
        assert detail["skill_path"] == row["target"]["skill_path"]
        await service.mcp.aclose()

    asyncio.run(run())


def test_plugin_connection_detail_uses_input_defaults_and_saved_overlay(tmp_path):
    async def run():
        from pulsara_agent.plugins.contracts import (
            InstallLocalPluginRequest,
            PluginScopeKind,
        )

        service = preparation(tmp_path)
        await install_plugin(service, tmp_path)
        source = tmp_path / "source"
        inputs = source / "dev.pulsara" / "mcp" / "connection-inputs.json"
        inputs.parent.mkdir(parents=True)
        inputs.write_text(
            json.dumps(
                {
                    "servers": {
                        "docs": {
                            "inputs": [
                                {
                                    "name": "URL",
                                    "title": "Connection",
                                    "private": False,
                                    "required": True,
                                    "default": "https://default.example/mcp",
                                }
                            ],
                            "targets": [
                                {
                                    "kind": "endpoint",
                                    "name": "endpoint",
                                    "parts": [{"input": "URL"}],
                                }
                            ],
                        }
                    }
                }
            )
        )
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(
                source, PluginScopeKind.USER, monotonic() + 10, replace=True
            ),
            connections=service.mcp,
        )
        assert installed.disposition.value == "REPLACED"
        target = {
            "kind": "MCP_SERVER",
            "scope": "USER",
            "source_kind": "PLUGIN",
            "plugin_id": "example",
            "server_id": "docs",
        }
        detail = await query(service, {"target": target}, inspect=True)
        assert detail["connection"]["endpoint"] == "https://default.example/mcp"
        from pulsara_agent.plugins.mcp_connection import (
            PluginMcpConnectionOverlay,
            overlay_to_dict,
        )

        configured = await invoke(
            service,
            action="CONFIGURE_PLUGIN_MCP_CONNECTION",
            scope="USER",
            plugin_id="example",
            server_id="docs",
            overlay=overlay_to_dict(
                PluginMcpConnectionOverlay(
                    "docs", "streamable_http", endpoint="https://configured.example/mcp"
                )
            ),
        )
        assert configured["status"] == "APPLIED"
        detail = await query(service, {"target": target}, inspect=True)
        assert (
            detail["connection"]["endpoint"] == "https://configured.example/mcp"
            and detail["adopted"] is None
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_local_query_never_materializes_credentials_or_runtime_config(
    tmp_path, monkeypatch
):
    async def run():
        import pulsara_agent.mcp_config as native
        from pulsara_agent.mcp_credentials import (
            ManagedLocalCredentialReference,
            McpCredentialBinding,
            secret_to_dict,
        )
        from pulsara_agent.capability.mcp_management import LocalMcpTarget

        service = preparation(tmp_path)
        target = LocalMcpTarget("private-docs")
        reference = ManagedLocalCredentialReference(
            McpCredentialBinding(target.owner, "bearer")
        )
        native._write_mcp_raw(
            service.mcp.path(target),
            {
                "private-docs": {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://docs.example/mcp",
                    },
                    "auth": {"type": "bearer", "reference": secret_to_dict(reference)},
                }
            },
            workspace_root=None,
        )

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "read-only declaration query must not materialize secrets/config identity"
            )

        monkeypatch.setattr(native, "resolve_secret", forbidden)
        monkeypatch.setattr(native, "_derive_config_fingerprints", forbidden)
        monkeypatch.setattr(service.mcp.settings, "read", forbidden)
        (row,) = (
            await query(
                service, {"kind": "MCP_SERVER", "scope": "USER", "source_kind": "LOCAL"}
            )
        )["items"]
        detail = await query(service, {"target": row["target"]}, inspect=True)
        assert (
            detail["connection"]["endpoint"] == "https://docs.example/mcp"
            and detail["adopted"] is None
        )
        assert "bearer" not in json.dumps(detail)
        await service.mcp.aclose()

    asyncio.run(run())


def test_exact_invalid_plugin_hook_retains_error_without_a_default_list_row(tmp_path):
    async def run():
        service = preparation(tmp_path)
        await install_plugin(
            service,
            tmp_path,
            hooks={
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Grep",
                            "hooks": [{"type": "command", "command": ""}],
                        }
                    ]
                }
            },
        )
        parent = {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"}
        listed = await query(service, {"parent": parent})
        assert all(item["kind"] != "HOOK_SOURCE" for item in listed["items"])
        detail = await query(
            service,
            {
                "target": {
                    "kind": "HOOK_SOURCE",
                    "scope": "USER",
                    "source_kind": "PLUGIN",
                    "plugin_id": "example",
                }
            },
            inspect=True,
        )
        assert detail["definitions"] == []
        assert any(
            item["code"] == "HOOK_CONFIG_INVALID_HANDLER"
            for item in detail["diagnostics"]
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_large_hook_details_use_existing_output_artifact_owner(tmp_path):
    async def run():
        from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
        from pulsara_agent.primitives.tool_result_projection import (
            classify_tool_result_delivery,
            BEST_AVAILABLE_TOOL_RESULT_DELIVERY,
        )
        from tests.test_round1_tool_output_artifact import (
            _RecordingPublisher,
            _processor,
        )
        from pulsara_agent.ports.artifact import (
            ToolOutputArtifactDisposition,
            ToolResultDisplayKind,
        )

        service = preparation(tmp_path)
        hooks = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Grep",
                        "hooks": [
                            {"type": "command", "command": "printf " + "x" * 7500}
                            for _ in range(8)
                        ],
                    }
                ]
            }
        }
        await install_plugin(service, tmp_path, hooks=hooks)
        # Drive the actual provider builtin result branch, then its production OUTPUT projection.
        port = object.__new__(DirectKernelToolPort)
        port._capability_management = service
        port._mcp_runtime_by_surface_generation = {}
        prepared = SimpleNamespace(
            access=SimpleNamespace(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                surface_generation="fresh",
            ),
            capability_exposure_plan=None,
        )
        arguments = {
            "target": {
                "kind": "HOOK_SOURCE",
                "scope": "USER",
                "source_kind": "PLUGIN",
                "plugin_id": "example",
            }
        }
        result = await port._query_capabilities_result(
            tool_name="inspect_capability",
            arguments=arguments,
            invocation_context=SimpleNamespace(
                surface_borrow=SimpleNamespace(prepared=prepared)
            ),
        )
        assert result.state == "SUCCESS" and len(result.content) > 40000
        assert len(json.loads(result.content)["definitions"]) == 8
        assert (
            classify_tool_result_delivery(
                tool_name="inspect_capability",
                result_state=result.state,
                public_arguments=arguments,
            )
            == BEST_AVAILABLE_TOOL_RESULT_DELIVERY
        )
        publisher = _RecordingPublisher()
        output = _processor(publisher).prepare(
            workspace_id="workspace",
            result_entry_id="hook-result",
            public_output=result.content.decode(),
            candidate=result.output_artifact_candidate,
            artifact_inline_result=False,
            deadline_monotonic=monotonic() + 10,
        )
        assert output.artifact_disposition is ToolOutputArtifactDisposition.AVAILABLE
        assert output.display_kind is ToolResultDisplayKind.HEAD_TAIL
        assert publisher.calls == [result.content] and output.artifact_id
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.postgres
def test_large_hook_query_artifact_roundtrips_through_canonical_postgres_owner(
    tmp_path, stage2_migrated_postgres_database
):
    from datetime import datetime, timezone
    from tests.test_round1_tool_output_artifact import _install_tool_call, _name
    from tests.support.postgres import verified_postgres_provider
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
        build_prepared_tool_result_acceptance,
    )
    from pulsara_agent.conversation_kernel.tool_artifacts import (
        ToolOutputArtifactProcessor,
        PostgresToolArtifactReadPort,
        ARTIFACT_READ_HARD_CHARS,
    )
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.primitives.context import canonical_json_bytes

    async def observe():
        service = preparation(tmp_path)
        await install_plugin(
            service,
            tmp_path,
            hooks={
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Grep",
                            "hooks": [
                                {"type": "command", "command": "printf " + "x" * 7500}
                                for _ in range(8)
                            ],
                        }
                    ]
                }
            },
        )
        try:
            return await query(
                service,
                {
                    "target": {
                        "kind": "HOOK_SOURCE",
                        "scope": "USER",
                        "source_kind": "PLUGIN",
                        "plugin_id": "example",
                    }
                },
                inspect=True,
            )
        finally:
            await service.mcp.aclose()

    body = canonical_json_bytes(asyncio.run(observe())).decode()
    assert len(body.encode()) > 40000
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace_id = _name("workspace")
    lease, turn_id, assistant_id, call_id, attempt_id = _install_tool_call(
        repository, workspace_id
    )
    entry_id = _name("entry")
    output = ToolOutputArtifactProcessor(provider).prepare(
        workspace_id=workspace_id,
        result_entry_id=entry_id,
        public_output=body,
        candidate=None,
        artifact_inline_result=False,
        deadline_monotonic=monotonic() + 30,
    )
    accepted = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_name("result"),
        result_entry_id=entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_id,
        tool_call_id=call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=output.canonical_preview,
        artifact_disposition=output.artifact_disposition,
        artifact_id=output.artifact_id,
        artifact_blob_descriptor=output.artifact_blob,
        source_coverage=output.source_coverage,
        display_kind=output.display_kind,
        source_coverage_reason=output.source_coverage_reason,
        artifact_unavailability_reason=output.artifact_unavailability_reason,
        actor_id="query",
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
    )
    repository.accept_tool_result(
        lease.guard, candidate=accepted, deadline_monotonic=monotonic() + 30
    )
    reader = PostgresToolArtifactReadPort(
        provider, session_id=lease.guard.session_id, workspace_id=workspace_id
    )
    pieces, offset = [], 0
    while True:
        page = reader.read_text(
            output.artifact_id, offset_chars=offset, max_chars=ARTIFACT_READ_HARD_CHARS
        )
        pieces.append(page.text)
        if not page.has_more:
            break
        offset = page.next_offset_chars
    assert (
        "".join(pieces) == body and len(json.loads("".join(pieces))["definitions"]) == 8
    )


@pytest.mark.parametrize("kind", ["HOOK_SOURCE", "MCP_SERVER"])
def test_exact_plugin_child_unavailable_preserves_real_source_failure(tmp_path, kind):
    async def run():
        from pulsara_agent.plugins.contracts import PluginScopeKind

        service = preparation(tmp_path)
        store = service.plugins._store()
        layout = store.layout(
            store.identity(
                scope=PluginScopeKind.USER, plugin_id="broken", workspace_root=None
            )
        )
        layout.state_parent.mkdir(parents=True, exist_ok=True)
        layout.state_path.write_text("broken JSON")
        target = {
            "kind": kind,
            "scope": "USER",
            "source_kind": "PLUGIN",
            "plugin_id": "broken",
        }
        if kind == "MCP_SERVER":
            target["server_id"] = "docs"
        with pytest.raises(CapabilityQueryError) as error:
            await query(service, {"target": target}, inspect=True)
        assert (
            error.value.code == "SOURCE_OBSERVATION_UNAVAILABLE"
            and error.value.diagnostics
        )
        assert any(
            "JSON" in d["message"] or "Expecting" in d["message"]
            for d in error.value.diagnostics
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_local_mcp_unavailable_is_not_absent_but_complete_missing_target_is_absent(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        target = {
            "kind": "MCP_SERVER",
            "scope": "USER",
            "source_kind": "LOCAL",
            "server_id": "docs",
        }
        service.mcp.user_config_path.write_text("servers: [invalid]")
        with pytest.raises(CapabilityQueryError) as error:
            await query(service, {"target": target}, inspect=True)
        assert error.value.code == "SOURCE_OBSERVATION_UNAVAILABLE"
        assert error.value.diagnostics[0]["code"] == "MCP_CONFIG_UNAVAILABLE"
        service.mcp.user_config_path.write_text("servers: {}")
        # An unrelated broken Plugin cannot turn a known complete LOCAL miss into unavailable.
        from pulsara_agent.plugins.contracts import PluginScopeKind

        store = service.plugins._store()
        broken = store.layout(
            store.identity(
                scope=PluginScopeKind.USER, plugin_id="broken", workspace_root=None
            )
        )
        broken.state_parent.mkdir(parents=True, exist_ok=True)
        broken.state_path.write_text("broken JSON")
        with pytest.raises(CapabilityQueryError) as error:
            await query(service, {"target": target}, inspect=True)
        assert error.value.code == "CAPABILITY_NOT_FOUND"
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "MCP_SERVER", "runtime_server_id": "docs"},
        {"kind": "MCP_RESOURCE", "server_id": "docs", "uri": "fixture://note"},
    ],
)
def test_runtime_inspect_without_catalog_retains_unknown_and_diagnostic(target):
    with pytest.raises(CapabilityQueryError) as error:
        asyncio.run(
            query(
                None,
                {"target": target},
                inspect=True,
                scope=ModelInputScopeKind.SUBAGENT_TASK,
            )
        )
    assert error.value.code == "SOURCE_OBSERVATION_UNAVAILABLE"
    assert error.value.diagnostics[0]["code"] == "MCP_CATALOG_UNAVAILABLE"


@pytest.mark.parametrize(
    "discovered,visible,expected",
    [
        (False, True, "SOURCE_OBSERVATION_UNAVAILABLE"),
        (True, True, "CAPABILITY_NOT_FOUND"),
        (False, False, "CAPABILITY_NOT_FOUND"),
    ],
)
def test_remote_metadata_miss_only_proves_absence_in_complete_visible_catalog(
    monkeypatch, discovered, visible, expected
):
    import pulsara_agent.conversation_kernel.capability_query as owner

    target = {"kind": "MCP_RESOURCE", "server_id": "docs", "uri": "fixture://missing"}
    details = (
        {("MCP_SERVER", "docs"): {"catalog_discovered": discovered}} if visible else {}
    )
    monkeypatch.setattr(owner, "runtime_rows", lambda *args: ([], details, []))
    with pytest.raises(CapabilityQueryError) as error:
        asyncio.run(
            query(
                None,
                {"target": target},
                inspect=True,
                scope=ModelInputScopeKind.SUBAGENT_TASK,
            )
        )
    assert error.value.code == expected
    if expected == "SOURCE_OBSERVATION_UNAVAILABLE":
        assert error.value.diagnostics[0]["code"] == "MCP_CATALOG_NOT_DISCOVERED"
    else:
        assert error.value.diagnostics == ()
