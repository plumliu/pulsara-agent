"""Complete source queries preserve real source, scope and read-only boundaries."""

import asyncio
import json
from threading import Event

import pytest
from jsonschema import Draft202012Validator
from pulsara_agent.capability.source_query import parse_query, query_output_schema
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.contracts import (
    PluginInspectionOutcome,
    PluginInspectionDisposition,
    PluginScopeKind,
)
from tests.support.capability_management_targets import (
    plugin_target,
    hook_target,
    mcp_target,
)
from tests.test_capability_management_preparation import preparation, install_plugin
from tests.test_app_capability_boundary import skill_source, hook_document, invoke


async def query(service, arguments, *, scope=ModelInputScopeKind.ROOT):
    value = await CapabilitySourceQuery(
        service, runtime=None, mcp_configs=(), scope=scope, inspect_tool=None
    ).execute(arguments)
    Draft202012Validator(query_output_schema()).validate(value)
    return value


def records(value):
    return [part["value"] for part in value["content"] if part["type"] == "object"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"kind": "UNKNOWN"},
        {"kind": "MCP", "offset": 0},
        {"kind": "SKILL", "source_kind": "LOCAL"},
        {"kind": "MCP", "target": None},
        {"kind": "PLUGIN", "target": {"kind": "PLUGIN", "plugin_path": "relative"}},
        {
            "kind": "MCP",
            "target": {"kind": "MCP_TOOL", "server_id": "s", "remote_tool_name": "t"},
            "query": "t",
        },
    ],
)
def test_query_inputs_are_closed(arguments):
    with pytest.raises(ValueError):
        parse_query(arguments)


@pytest.mark.parametrize("kind", ["SKILL", "HOOK", "PLUGIN"])
def test_subagents_only_query_their_mcp_scope(kind):
    with pytest.raises(ValueError):
        parse_query({"kind": kind}, subagent=True)


def test_root_lists_local_and_bundled_definitions_as_minimal_records(tmp_path):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        installed = await invoke(
            service,
            action="INSTALL_LOOSE_SKILL",
            scope="WORKSPACE",
            target={"kind": "PATH", "path": str(source)},
        )
        target = installed.business_value.public_fields["target"]
        listing = await query(service, {"kind": "SKILL"})
        assert listing["content"][0] == {
            "type": "text",
            "value": "技能列表按来源列出定义，范围包括已禁用和当前未选择的副本。\n",
        }
        row = next(item for item in records(listing) if item["target"] == target)
        assert set(row) == {"target", "name", "description", "source", "status"}
        assert row["source"] == {"kind": "LOCAL", "scope": "WORKSPACE"}
        assert (
            records(await query(service, {"kind": "SKILL", "target": target}))[0] == row
        )
        assert any(item["source"] == {"kind": "BUNDLED"} for item in records(listing))
        await service.mcp.aclose()

    asyncio.run(run())


def test_complete_source_inventory_has_no_producer_row_limit(tmp_path):
    async def run():
        service = preparation(tmp_path)
        for index in range(55):
            skill_source(service.workspace_root / ".pulsara/skills", f"note-{index}")
        # The fixture creates download/<name>; put each real source in its actual managed root.
        nested = service.workspace_root / ".pulsara/skills/download"
        for directory in tuple(nested.iterdir()):
            directory.rename(nested.parent / directory.name)
        nested.rmdir()
        value = await query(service, {"kind": "SKILL", "scope": "WORKSPACE"})
        assert value["meta"]["status"] == "SUCCESS"
        assert len(records(value)) == 55
        await service.mcp.aclose()

    asyncio.run(run())


def test_exact_plugin_and_children_do_not_scan_unrelated_corrupt_state(tmp_path):
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path, hooks=hook_document())
        store = service.plugins._store()
        broken = store.layout(
            store.identity(
                scope=PluginScopeKind.USER, plugin_id="broken", workspace_root=None
            )
        )
        broken.state_parent.mkdir(parents=True, exist_ok=True)
        broken.state_path.write_text("broken JSON")
        target = plugin_target(service, "example")
        exact = await query(service, {"kind": "PLUGIN", "target": target})
        assert exact["meta"]["status"] == "SUCCESS"
        assert any(
            part["type"] == "target" and part["value"]["kind"] == "MCP_CONNECTION"
            for part in exact["content"]
        )
        children = await query(service, {"kind": "HOOK", "parent": target})
        assert children["meta"]["status"] == "SUCCESS" and records(children)[0][
            "target"
        ] == hook_target(service, plugin="example")
        assert not (await service._plugin(None, "example")).enabled
        listed = await query(service, {"kind": "PLUGIN"})
        assert listed["meta"]["status"] == "ERROR"
        assert installed.package_root.is_dir()
        await service.mcp.aclose()

    asyncio.run(run())


def test_transient_workspace_query_never_uses_a_terminal_directory(tmp_path):
    async def run():
        service = preparation(tmp_path)
        service.workspace_kind = "transient"
        value = await query(service, {"kind": "SKILL", "scope": "WORKSPACE"})
        assert (
            value["meta"]["status"] == "ERROR"
            and not (service.workspace_root / ".pulsara").exists()
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_current_project_selection_applies_to_the_user_and_workspace_copies(tmp_path):
    async def run():
        service = preparation(tmp_path)
        source = skill_source(tmp_path)
        user = await invoke(
            service,
            action="INSTALL_LOOSE_SKILL",
            scope="USER",
            target={"kind": "PATH", "path": str(source)},
        )
        project = await invoke(
            service,
            action="INSTALL_LOOSE_SKILL",
            scope="WORKSPACE",
            target={"kind": "PATH", "path": str(source)},
        )
        listing = await query(service, {"kind": "SKILL"})
        by_target = {row["target"]["skill_path"]: row for row in records(listing)}
        assert (
            "当前未选择"
            in by_target[user.business_value.public_fields["target"]["skill_path"]][
                "status"
            ]
        )
        assert (
            "当前已选择"
            in by_target[project.business_value.public_fields["target"]["skill_path"]][
                "status"
            ]
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_repeated_query_cancellation_joins_native_observation_and_closes_anchor(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        started, release, closed = Event(), Event(), Event()

        class Anchor:
            def close(self):
                closed.set()

        def observe():
            started.set()
            assert release.wait(2)
            return PluginInspectionOutcome(
                PluginInspectionDisposition.COMPLETE,
                (),
                (),
                (),
                physical_lifetime_anchors=(Anchor(),),
            )

        owner = CapabilitySourceQuery(
            service, runtime=None, mcp_configs=(), scope=ModelInputScopeKind.ROOT, inspect_tool=None
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


def test_local_declaration_list_does_not_materialize_credentials_or_runtime_config(
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
        native._write_mcp_raw(
            service.mcp.path(target),
            {
                "private-docs": {
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": "https://docs.example/mcp",
                    },
                    "auth": {
                        "type": "bearer",
                        "reference": secret_to_dict(
                            ManagedLocalCredentialReference(
                                McpCredentialBinding(target.owner, "bearer")
                            )
                        ),
                    },
                }
            },
            workspace_root=None,
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("query cannot resolve credentials or runtime config")

        monkeypatch.setattr(native, "resolve_secret", forbidden)
        monkeypatch.setattr(native, "_derive_config_fingerprints", forbidden)
        monkeypatch.setattr(service.mcp.settings, "read", forbidden)
        value = await query(service, {"kind": "MCP", "scope": "USER"})
        assert value["meta"]["status"] == "SUCCESS"
        assert records(value)[0]["target"] == mcp_target(service, "private-docs")
        assert "bearer" not in json.dumps(
            value
        ) and "https://docs.example/mcp" not in json.dumps(value)
        unknown = await query(
            service, {"kind": "MCP", "target": mcp_target(service, "private-docs")}
        )
        assert (
            unknown["meta"]["status"] == "ERROR"
            and "目录未知" in unknown["content"][0]["value"]
        )
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["MCP", "HOOK"])
def test_exact_current_plugin_configuration_reports_corrupt_state(tmp_path, kind):
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path, hooks=hook_document())
        target = (
            mcp_target(service, "docs", plugin="example")
            if kind == "MCP"
            else hook_target(service, plugin="example")
        )
        store = service.plugins._store()
        store.layout(installed.identity).state_path.write_text("broken JSON")
        value = await query(service, {"kind": kind, "target": target})
        assert (
            value["meta"]["status"] == "ERROR" and value["content"][0]["type"] == "text"
        )
        assert (
            "JSON" in value["content"][0]["value"]
            or "Expecting" in value["content"][0]["value"]
        )
        await service.mcp.aclose()

    asyncio.run(run())


def test_hook_definition_content_stays_complete_and_scrubs_only_actual_credentials(
    tmp_path,
):
    async def run():
        service = preparation(tmp_path)
        path = service.hooks.user_config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Each command respects the Hook parser's 8 KiB command boundary;
        # the complete source still exceeds one native delivery page.
        suffix = "中文 full command " * 200
        command = "prefix actual-private-fixture " + suffix
        document = hook_document(command)
        document["hooks"]["PreToolUse"] *= 8
        path.write_text(json.dumps(document))
        service.plugins._credential_boundary.rotate_sync("actual-private-fixture")
        value = await query(service, {"kind": "HOOK", "target": hook_target(service)})
        assert value["meta"]["status"] == "SUCCESS"
        definitions = records(value)[0]["definitions"]
        assert len(definitions) == 8
        assert all(
            "actual-private-fixture" not in item["command"]
            and item["command"].endswith(suffix)
            for item in definitions
        )
        await service.mcp.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_plugin_mcp_declarations_respect_subagent_scope_after_overlay_changes(tmp_path, enabled):
    from dataclasses import replace
    from time import monotonic
    from pulsara_agent.mcp_config import McpScopePolicy
    from pulsara_agent.plugins.connection_management import ReplacePluginMcpConnectionRequest
    from pulsara_agent.plugins.contracts import SetLocalPluginEnabledRequest, ExternalProcessAcceptance
    from pulsara_agent.plugins.mcp_connection import PluginMcpConnectionOverlay, connection_review

    async def run():
        service = preparation(tmp_path)
        try:
            installed = await install_plugin(service, tmp_path)
            target = mcp_target(service, "docs", plugin="example")
            subagent = ModelInputScopeKind.SUBAGENT
            # No overlay means the same default scope as the materialized connection.
            assert records(await query(service, {"kind": "MCP"}, scope=subagent))[0]["target"] == target
            restricted = PluginMcpConnectionOverlay("docs", "streamable_http", scope_policy=McpScopePolicy.ROOT_ONLY)
            edit = ReplacePluginMcpConnectionRequest(installed.identity, "docs", installed.package_install_id,
                None, restricted, monotonic() + 30)
            await service.plugins.replace_plugin_mcp_connection_overlay(edit, connections=service.mcp)
            if enabled:
                inspection = await service._plugin(None, "example")
                review = connection_review(inspection.mcp_connection_overlays, service.mcp.settings.read().mcp_secret,
                    servers=inspection.summary.mcp.mcp_servers, identity=installed.identity)
                outcome = service.plugins.set_local_plugin_enabled(SetLocalPluginEnabledRequest(
                    PluginScopeKind.USER, "example", True, installed.package_install_id, monotonic() + 30,
                    connection_review=review, external_process_acceptance=ExternalProcessAcceptance.ACCEPTED))
                assert outcome.disposition.value == "ENABLED"
            assert records(await query(service, {"kind": "MCP"}))[0]["target"] == target
            for arguments in ({"kind": "MCP"}, {"kind": "MCP", "query": "docs"},
                              {"kind": "MCP", "parent": plugin_target(service, "example")}):
                observed = await query(service, arguments, scope=subagent)
                assert observed["meta"]["status"] == "SUCCESS" and not records(observed)
            exact = await query(service, {"kind": "MCP", "target": target}, scope=subagent)
            assert exact["meta"]["status"] == "ERROR" and not records(exact)
            permitted = replace(restricted, scope_policy=McpScopePolicy.ROOT_AND_SUBAGENTS)
            await service.plugins.replace_plugin_mcp_connection_overlay(
                replace(edit, expected_overlay=restricted, overlay=permitted, deadline_monotonic=monotonic() + 30),
                connections=service.mcp)
            assert records(await query(service, {"kind": "MCP"}, scope=subagent))[0]["target"] == target
        finally:
            await service.mcp.aclose()

    asyncio.run(run())
