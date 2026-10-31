"""Public package IDs never replace the owner's exact management guard."""

import asyncio
import json

import pytest

from pulsara_agent.capability.management_form import AcceptedCapabilityFormSubmission
from pulsara_agent.capability.mcp_management import McpManagementConflict
from pulsara_agent.conversation_kernel.capability_management_execution import CapabilityManagementCall
from pulsara_agent.identity import random_id
from pulsara_agent.mcp_config import OAuthAuthorization
from pulsara_agent.plugins.mcp_connection import PluginMcpConnectionOverlay, overlay_to_dict
from pulsara_agent.primitives.context import thaw_json
from tests.test_capability_management_preparation import install_plugin, preparation


@pytest.mark.parametrize("withheld", [False, True])
def test_plugin_mcp_configuration_and_oauth_use_exact_prepared_owner(tmp_path, monkeypatch, withheld):
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        package = installed.package_install_id
        secret = package[10:20] if withheld else "unrelated-private-credential"
        service.plugins._credential_boundary.rotate_sync(secret)
        target = {"kind": "MCP_CONNECTION", "scope": "USER", "source_kind": "PLUGIN", "plugin_id": "example", "server_id": "docs"}
        overlay = overlay_to_dict(PluginMcpConnectionOverlay("docs", "streamable_http", auth=OAuthAuthorization()))
        args = {"action": "CONFIGURE_PLUGIN_MCP_CONNECTION", "target": target, "overlay": overlay}
        prepared = await service.prepare(args)
        assert thaw_json(prepared.expected_current)["expected_package_install_id"] == package
        assert (await CapabilityManagementCall(service, prepared).execute())["status"] == "APPLIED"
        with pytest.raises(McpManagementConflict):
            await service.prepare({**args, "expected_package_install_id": random_id("package")})
        await service.prepare({**args, "expected_package_install_id": package})

        async def exchange(*args, **kwargs):
            return None
        monkeypatch.setattr(service.mcp.oauth, "_exchange", exchange)
        authorize = {"action": "AUTHORIZE_MCP", "target": target}
        call = CapabilityManagementCall(service, await service.prepare(authorize))
        form = call.form("authorize")
        public = thaw_json(form.public_projection)
        expected = {"visibility": "WITHHELD"} if withheld else {"visibility": "VISIBLE", "value": package}
        assert public["plugin"]["package_install_id"] == expected
        assert public["expected_current"]["expected_package_install_id"] == expected
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({})))
        result = await call.execute()
        assert result["status"] == "APPLIED"
        assert result["current"]["authorized"] is True
        assert result["authorization"]["package_install_id"] == expected
        assert secret not in json.dumps(result)
        clear = CapabilityManagementCall(service, await service.prepare({"action": "CLEAR_MCP_AUTHORIZATION", "target": target}))
        cleared = await clear.execute()
        assert cleared["status"] == "APPLIED"
        assert cleared["current"]["authorized"] is False
        remove = {"action": "REMOVE_PLUGIN", "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"}}
        with pytest.raises(McpManagementConflict):
            await service.prepare({**remove, "expected_package_install_id": random_id("package")})
        await service.prepare({**remove, "expected_package_install_id": package})
        removed = await CapabilityManagementCall(service, await service.prepare(remove)).execute()
        assert removed["status"] == "APPLIED"
        assert removed["current"]["operation_status"] == "REMOVED"
        await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("secret", ["package", "visibility", "WITHHELD", "APPLIED", "status"])
def test_fixed_key_overlap_preserves_real_configuration_and_oauth_settlement(tmp_path, monkeypatch, secret):
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        package = installed.package_install_id
        service.plugins._credential_boundary.rotate_sync(secret)
        target = {"kind": "MCP_CONNECTION", "scope": "USER", "source_kind": "PLUGIN", "plugin_id": "example", "server_id": "docs"}
        overlay = overlay_to_dict(PluginMcpConnectionOverlay("docs", "streamable_http", auth=OAuthAuthorization(),
            public_headers=tuple(sorted(((secret, "external-value"), ("ordinary", "prefix-" + secret))))))
        result = await CapabilityManagementCall(service, await service.prepare({
            "action": "CONFIGURE_PLUGIN_MCP_CONNECTION", "target": target, "overlay": overlay,
        })).execute()
        assert result["status"] == "APPLIED"
        assert result["current"]["overlay"]["withheld_fields"] == ["public_headers"]
        assert "public_headers" not in result["current"]["overlay"]
        # The public omission must not alter the real committed overlay.
        config = (await service._plugin(None, "example")).mcp_connection_overlays[0]
        assert dict(config.public_headers)[secret] == "external-value"

        async def exchange(*args, **kwargs):
            return None
        monkeypatch.setattr(service.mcp.oauth, "_exchange", exchange)
        call = CapabilityManagementCall(service, await service.prepare({"action": "AUTHORIZE_MCP", "target": target}))
        assert thaw_json(call.prepared.expected_current)["expected_package_install_id"] == package
        form = call.form("prefix-" + secret)
        public = thaw_json(form.public_projection)
        assert public["action"] == "AUTHORIZE_MCP"
        assert public["scope"] == "USER"
        assert secret not in public["reason"]
        assert public["plugin"]["mcp"][0]["config"]["withheld_fields"] == ["public_headers"]
        expected = {"visibility": "WITHHELD"} if secret == "package" else {"visibility": "VISIBLE", "value": package}
        assert public["plugin"]["package_install_id"] == expected
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({})))
        authorized = await call.execute()
        assert authorized["status"] == "APPLIED"
        assert authorized["current"]["authorized"] is True
        assert authorized["authorization"]["package_install_id"] == expected
        await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("ack_unknown", [False, True])
def test_model_install_projects_secret_id_and_preserves_unknown_cut(tmp_path, monkeypatch, ack_unknown):
    from pulsara_agent.plugins import package_store
    from pulsara_agent.plugins.package_store import ManagedPluginStore
    from tests.test_round9_3_agent_plugin_product import _package

    async def run():
        service = preparation(tmp_path)
        package = random_id("package")
        monkeypatch.setattr(package_store, "random_id", lambda kind: package if kind == "package" else random_id(kind))
        service.plugins._credential_boundary.rotate_sync(package)
        source = _package(tmp_path / "source")
        if ack_unknown:
            read = ManagedPluginStore.read_state
            def uncertain(store, layout):
                if layout.state_path.exists():
                    raise OSError("confirmation unavailable")
                return read(store, layout)
            monkeypatch.setattr(ManagedPluginStore, "read_state", uncertain)
        call = CapabilityManagementCall(service, await service.prepare({
            "action": "INSTALL_PLUGIN", "scope": "USER", "source_path": str(source),
        }))
        result = await call.execute()
        assert package not in json.dumps(result)
        if ack_unknown:
            assert result["status"] == "PARTIAL"
            assert result["current"]["attempted_package_install_id"] == {"visibility": "WITHHELD"}
            assert result["current"]["last_known_cut"] == "STATE_RENAME_FULL_CONFIRMATION_UNAVAILABLE"
        else:
            assert result["status"] == "APPLIED"
            assert result["current"]["package_install_id"] == {"visibility": "WITHHELD"}
            assert result["current"]["operation_status"] == "INSTALLED"
        await service.mcp.aclose()
    asyncio.run(run())


def test_remove_form_scrubs_original_and_rotated_credentials(tmp_path):
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        package = installed.package_install_id
        secret = package[10:20]
        service.plugins._credential_boundary.rotate_sync(secret)
        args = {"action": "REMOVE_PLUGIN", "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "example"}}
        call = CapabilityManagementCall(service, await service.prepare(args))
        service.plugins._credential_boundary.rotate_sync("new-credential")
        form = call.form("remove")
        public = thaw_json(form.public_projection)
        assert public["expected_current"]["expected_package_install_id"] == {"visibility": "WITHHELD"}
        assert secret not in json.dumps(public)
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({})))
        result = await call.execute()
        assert result["status"] == "APPLIED"
        assert result["current"]["prior_package_install_id"] == {"visibility": "WITHHELD"}
        assert secret not in json.dumps(result)
        await service.mcp.aclose()
    asyncio.run(run())


def test_local_hook_review_preserves_preparation_scrub_across_rotation(tmp_path):
    from tests.test_app_capability_boundary import hook_document

    async def run():
        service = preparation(tmp_path)
        path = service.hooks.user_config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hook_document("echo old-private-credential visibility retained-command")))
        service.plugins._credential_boundary.rotate_sync("old-private-credential")
        call = CapabilityManagementCall(service, await service.prepare({
            "action": "TRUST_HOOK_SOURCE",
            "target": {"kind": "HOOK_SOURCE", "scope": "USER", "source_kind": "LOCAL"},
        }))
        digest = call.prepared.hook.trust.current_definition_digest
        service.plugins._credential_boundary.rotate_sync("visibility")
        form = call.form("review visibility")
        public = thaw_json(form.public_projection)
        command = public["prefill"]["hook_source"]["definitions"][0]["command"]
        assert "old-private-credential" not in command
        assert "visibility" not in command
        assert "retained-command" in command
        assert public["prefill"]["hook_source"]["definitions"][0]["event"] == "PreToolUse"
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission({"hook_review_accepted": True})))
        assert (await call.execute())["status"] == "APPLIED"
        assert service.hooks.trust_store.read(call.prepared.hook.provenance.trust_subject).trusted_definition_digest == digest
        await service.mcp.aclose()
    asyncio.run(run())


def test_model_cleanup_keeps_prior_cut_and_scrubs_both_diagnostics(tmp_path, monkeypatch):
    from pulsara_agent.plugins.contracts import (
        CleanupUnavailablePluginInstallOutcome, FailedPluginInstallOutcome,
        PluginCleanupLocationStatus, PluginDiagnostic, PluginDiagnosticCode,
        PluginInstallDisposition,
    )
    from tests.test_round9_3_agent_plugin_product import _package

    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        package = installed.package_install_id
        secret = package[10:20]
        service.plugins._credential_boundary.rotate_sync(secret)
        source = _package(tmp_path / "replacement")
        prepared = await service.prepare({"action": "INSTALL_PLUGIN", "scope": "USER", "source_path": str(source)})
        scrub = service.plugins._credential_boundary.capture_scrub_set()
        prior = FailedPluginInstallOutcome(
            PluginInstallDisposition.ACK_UNKNOWN, source,
            attempted_package_install_id=package,
            diagnostics=(PluginDiagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE, f"prior {secret}"),),
            intended_enabled=False, last_known_cut="CUT_UNKNOWN", scrub_set=scrub,
        )
        wrapped = CleanupUnavailablePluginInstallOutcome(
            prior, source, PluginCleanupLocationStatus.UNKNOWN,
            PluginDiagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE, f"cleanup {secret}"), scrub_set=scrub,
        )
        async def settled(*args, **kwargs):
            return wrapped
        monkeypatch.setattr(service.plugins, "install_local_plugin", settled)
        result = await CapabilityManagementCall(service, prepared).execute()
        assert result["current"]["operation_status"] == "CLEANUP_UNAVAILABLE"
        assert result["current"]["prior_operation_status"] == "ACK_UNKNOWN"
        assert result["current"]["last_known_cut"] == "CUT_UNKNOWN"
        assert result["current"]["attempted_package_install_id"] == {"visibility": "WITHHELD"}
        assert len(result["diagnostics"]) == 2
        assert [entry["message"].split()[0] for entry in result["diagnostics"]] == ["prior", "cleanup"]
        assert secret not in json.dumps(result)
        await service.mcp.aclose()
    asyncio.run(run())


def test_model_form_and_config_result_preserve_package_named_dynamic_fields(tmp_path):
    from time import monotonic
    from pulsara_agent.plugins.contracts import InstallLocalPluginRequest, PluginScopeKind
    from tests.test_round9_3_plugin_physical_semantics import _make_package

    async def run():
        service = preparation(tmp_path)
        source = _make_package(tmp_path / "source", mcp_kind="stdio")
        path = source / "mcp.json"
        manifest = json.loads(path.read_text())
        manifest["mcpServers"]["fixture"]["env"]["package_install_id"] = "ordinary-env"
        manifest["mcpServers"]["web"] = {
            "type": "streamable-http", "url": "https://example.invalid/mcp",
            "headers": {"package_install_id": "ordinary-header"},
        }
        path.write_text(json.dumps(manifest))
        installed = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30), connections=service.mcp)
        assert installed.disposition.value == "INSTALLED"
        target = {"kind": "PLUGIN", "scope": "USER", "plugin_id": "physical-plugin"}
        call = CapabilityManagementCall(service, await service.prepare({"action": "SET_PLUGIN_ENABLED", "target": target, "enabled": True}))
        public = thaw_json(call.form("review").public_projection)
        assert public["plugin"]["package_install_id"] == {"visibility": "VISIBLE", "value": installed.package_install_id}
        servers = {entry["server_id"]: entry for entry in public["plugin"]["mcp"]}
        assert servers["fixture"]["config"]["transport"]["env"]["package_install_id"] == "ordinary-env"
        assert servers["web"]["config"]["public_headers"]["package_install_id"] == "ordinary-header"
        overlay = overlay_to_dict(PluginMcpConnectionOverlay(
            "web", "streamable_http", public_headers=(("package_install_id", "updated-header"),)))
        connection = {"kind": "MCP_CONNECTION", "scope": "USER", "source_kind": "PLUGIN", "plugin_id": "physical-plugin", "server_id": "web"}
        result = await CapabilityManagementCall(service, await service.prepare({
            "action": "CONFIGURE_PLUGIN_MCP_CONNECTION", "target": connection, "overlay": overlay,
        })).execute()
        assert result["status"] == "APPLIED"
        assert result["current"]["overlay"]["public_headers"]["package_install_id"] == "updated-header"
        await service.mcp.aclose()
    asyncio.run(run())


def test_public_package_id_rejects_noncanonical_values():
    from pulsara_agent.plugins.contracts import public_package_id
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet

    scrub = ProcessCredentialScrubSet()
    with pytest.raises(ValueError, match="INVALID_ID"):
        public_package_id("ordinary-environment-value", scrub)
    package = random_id("package")
    assert public_package_id(package, scrub) == {"visibility": "VISIBLE", "value": package}
    scrub.observe(package[10:20])
    assert public_package_id(package, scrub) == {"visibility": "WITHHELD"}


@pytest.mark.parametrize("withheld", [False, True])
@pytest.mark.parametrize("action", ["TRUST_HOOK_SOURCE", "REVOKE_HOOK_TRUST", "SET_HOOK_SOURCE_ENABLED"])
def test_plugin_hook_actions_keep_real_guard_and_scrub_public_result(tmp_path, action, withheld):
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hook"}]}]}}

    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path, hooks=hooks)
        package = installed.package_install_id
        secret = package[10:20] if withheld else "not-present-in-output"
        service.plugins._credential_boundary.rotate_sync(secret)
        target = {"kind": "HOOK_SOURCE", "scope": "USER", "source_kind": "PLUGIN", "plugin_id": "example"}
        args = {"action": action, "target": target}
        if action == "SET_HOOK_SOURCE_ENABLED":
            args["enabled"] = False
        prepared = await service.prepare(args)
        snapshot = prepared.hook
        assert snapshot.provenance.identity.package_install_id == package
        subject = snapshot.provenance.trust_subject
        digest = snapshot.trust.current_definition_digest
        if action == "REVOKE_HOOK_TRUST":
            service.hooks.trust_store.trust_after_revalidation(subject, expected_digest=digest, current_digest_reader=lambda: digest)
            prepared = await service.prepare(args)
            assert prepared.hook.trust.disposition.value == "TRUSTED"
        call = CapabilityManagementCall(service, prepared)
        form = call.form("review")
        public = thaw_json(form.public_projection)
        expected = {"visibility": "WITHHELD"} if withheld else {"visibility": "VISIBLE", "value": package}
        assert public["prefill"]["hook_source"]["package_install_id"] == expected
        assert public["plugin"]["package_install_id"] == expected
        assert secret not in json.dumps(public)
        submission = {"hook_review_accepted": True} if action == "TRUST_HOOK_SOURCE" else {}
        call.accept(AcceptedCapabilityFormSubmission(await form.prepare_submission(submission)))
        result = await call.execute()
        assert result["status"] == "APPLIED"
        assert secret not in json.dumps(result)
        observed = service.hooks.trust_store.assess(subject, digest)
        if action == "TRUST_HOOK_SOURCE":
            assert observed.disposition.value == "TRUSTED"
        elif action == "REVOKE_HOOK_TRUST":
            assert observed.disposition.value == "UNTRUSTED"
            assert result["current"]["trust_revoked"] is True
        else:
            assert observed.enabled is False
            assert result["current"]["enabled"] is False
        await service.mcp.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["TRUST_HOOK_SOURCE", "REVOKE_HOOK_TRUST", "SET_HOOK_SOURCE_ENABLED"])
@pytest.mark.parametrize("withheld", [False, True])
def test_plugin_hook_form_rejects_replaced_package_and_wrong_scope(tmp_path, action, withheld):
    from time import monotonic
    from pulsara_agent.plugins.contracts import InstallLocalPluginRequest, PluginScopeKind

    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hook"}]}]}}
    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path, hooks=hooks)
        package = installed.package_install_id
        secret = package[10:20] if withheld else "credential-not-present"
        service.plugins._credential_boundary.rotate_sync(secret)
        target = {"kind": "HOOK_SOURCE", "scope": "USER", "source_kind": "PLUGIN", "plugin_id": "example"}
        args = {"action": action, "target": target}
        if action == "SET_HOOK_SOURCE_ENABLED":
            args["enabled"] = False
        with pytest.raises(McpManagementConflict):
            await service.prepare({**args, "target": {**target, "scope": "WORKSPACE"}})
        prepared = await service.prepare(args)
        call = CapabilityManagementCall(service, prepared)
        form = call.form("review")
        replacement = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(tmp_path / "source", PluginScopeKind.USER, monotonic() + 30, replace=True),
            connections=service.mcp,
        )
        assert replacement.disposition.value == "REPLACED"
        assert replacement.package_install_id != package
        submission = {"hook_review_accepted": True} if action == "TRUST_HOOK_SOURCE" else {}
        with pytest.raises(McpManagementConflict):
            await form.prepare_submission(submission)
        await service.mcp.aclose()
    asyncio.run(run())
