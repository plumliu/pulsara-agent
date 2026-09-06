import json
import asyncio
from dataclasses import replace
from time import monotonic

import pytest

from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService,
    McpSecretMutation,
    McpManagementConflict,
)
from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.mcp_config import BearerSecret, NoAuth
from pulsara_agent.mcp_credentials import (
    ManagedLocalCredentialReference,
    McpCredentialBinding,
)
from pulsara_agent.plugins.connection_management import (
    ReplacePluginMcpConnectionRequest,
)
from pulsara_agent.plugins.contracts import (
    InstallLocalPluginRequest,
    PluginScopeKind,
    NeverCancelPluginOperation,
    RemoveLocalPluginRequest,
    InspectLocalPluginsRequest,
    SetLocalPluginEnabledRequest,
    ExternalProcessAcceptance,
    PreparedPluginInstanceObservation,
)
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.plugins.mcp_adapter import (
    materialize_plugin_mcp_definition,
    bind_plugin_authorization,
)
from pulsara_agent.plugins.mcp_connection import (
    PluginMcpConnectionOverlay,
    overlay_to_dict,
    overlay_from_dict,
    plugin_connection_owner,
    connection_review,
    review_to_dict,
    review_from_dict,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.settings import LocalSettingsStore, LOCAL_SETTINGS_FILE_NAME


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "example",
                "version": "1.0.0",
                "description": "Fixture",
            }
        )
    )
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "docs": {
                        "type": "streamable-http",
                        "url": "https://example.org/mcp",
                    }
                },
            }
        )
    )
    home = tmp_path / "home"
    service = PluginManagementService(
        credential_boundary=ProcessCredentialBoundary(),
        pulsara_home_resolution=resolve_pulsara_home(str(home)),
    )
    connections = LocalMcpManagementService(
        LocalSettingsStore(home / LOCAL_SETTINGS_FILE_NAME),
        user_config_path=home / "mcp.yaml",
    )
    installed = await service.install_local_plugin(
        InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 30),
        connections=connections,
    )
    assert installed.disposition.value == "INSTALLED", installed
    owner = plugin_connection_owner(installed.identity, "docs")
    binding = McpCredentialBinding(owner, "bearer")
    overlay = PluginMcpConnectionOverlay(
        "docs",
        "streamable_http",
        auth=BearerSecret(ManagedLocalCredentialReference(binding)),
    )
    request = ReplacePluginMcpConnectionRequest(
        installed.identity,
        "docs",
        installed.package_install_id,
        None,
        overlay,
        monotonic() + 30,
        (McpSecretMutation(binding, "private-fixture-value"),),
    )
    return service, connections, request, source


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["remove", "disable", "connection", "replace"])
async def test_model_preparation_rejoins_existing_state_before_expanding_process_effects(
    tmp_path, action
):
    service, connections, edit, source = await fixture(tmp_path)
    store = service._store()
    layout = store.layout(edit.identity)
    original = store.read_state(layout)
    assert original is not None and not original.enabled
    prepared = PreparedPluginInstanceObservation(edit.identity, original)
    enabled = service.set_local_plugin_enabled(
        SetLocalPluginEnabledRequest(
            PluginScopeKind.USER,
            edit.identity.plugin_id,
            True,
            edit.expected_package_install_id,
            monotonic() + 10,
            connection_review=(),
            external_process_acceptance=ExternalProcessAcceptance.ACCEPTED,
        )
    )
    assert enabled.disposition.value == "ENABLED"
    current = store.read_state(layout)
    assert current.current_package_install_id == original.current_package_install_id
    if action == "remove":
        outcome = await service.remove_local_plugin(
            RemoveLocalPluginRequest(
                PluginScopeKind.USER,
                edit.identity.plugin_id,
                monotonic() + 10,
                edit.expected_package_install_id,
                prepared_current=prepared,
            ),
            connections=connections,
        )
        assert outcome.disposition.value == "STALE"
    elif action == "disable":
        outcome = service.set_local_plugin_enabled(
            SetLocalPluginEnabledRequest(
                PluginScopeKind.USER,
                edit.identity.plugin_id,
                False,
                edit.expected_package_install_id,
                monotonic() + 10,
                connection_review=(),
                prepared_current=prepared,
            )
        )
        assert outcome.disposition.value == "STALE"
    elif action == "connection":
        with pytest.raises(McpManagementConflict, match="model preparation"):
            await service.replace_plugin_mcp_connection_overlay(
                replace(edit, prepared_current=prepared),
                connections=connections,
            )
    else:
        with pytest.raises(McpManagementConflict, match="model preparation"):
            await service.install_local_plugin(
                InstallLocalPluginRequest(
                    source,
                    PluginScopeKind.USER,
                    monotonic() + 10,
                    replace=True,
                    prepared_current=prepared,
                ),
                connections=connections,
            )
    assert store.read_state(layout) == current
    assert (
        connections.settings.resolve_mcp_secret(edit.secret_changes[0].binding) is None
    )
    await connections.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["default_endpoint", "transport", "removed", "invalid"]
)
async def test_replace_revalidates_connections_and_cleans_only_obsolete_values(
    tmp_path, change
):
    service, connections, edit, source = await fixture(tmp_path)
    edit = replace(
        edit, overlay=replace(edit.overlay, endpoint="https://configured.example/mcp")
    )
    await service.replace_plugin_mcp_connection_overlay(edit, connections=connections)
    binding = edit.secret_changes[0].binding
    unrelated = McpCredentialBinding(
        replace(binding.owner, plugin_id="other"), "bearer"
    )
    await connections.settings.replace_mcp_secrets(
        unrelated.owner, ((unrelated, "other-value"),)
    )
    definitions = {
        "default_endpoint": {
            "docs": {
                "type": "streamable-http",
                "url": "https://new-default.example/mcp",
            }
        },
        "transport": {"docs": {"type": "stdio", "command": "never-executed"}},
        "removed": {},
        "invalid": {
            "docs": {"type": "unknown", "url": "https://new-default.example/mcp"}
        },
    }
    raw = json.loads((source / "mcp.json").read_text())
    raw["mcpServers"] = definitions[change]
    (source / "mcp.json").write_text(json.dumps(raw))
    result = await service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, monotonic() + 30, replace=True
        ),
        connections=connections,
    )
    assert result.disposition.value == "REPLACED", result
    assert not result.cleanup_attention and not result.enabled
    store = service._store()
    state = store.read_state(store.layout(edit.identity))
    assert not state.enabled
    assert state.current_package_install_id != edit.expected_package_install_id
    if change == "default_endpoint":
        assert state.mcp_connection_overlays == (edit.overlay,)
        assert (
            connections.settings.read().mcp_secret(binding) == "private-fixture-value"
        )
    else:
        assert state.mcp_connection_overlays == ()
        assert connections.settings.read().mcp_secret(binding) is None
        assert any(item.component == "example:docs" for item in result.diagnostics)
    assert connections.settings.read().mcp_secret(unrelated) == "other-value"
    assert (
        store.layout(edit.identity).plugin_package_parent
        / edit.expected_package_install_id
    ).exists()
    await connections.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("override", [False, True])
async def test_replace_oauth_preserves_only_exact_effective_binding(tmp_path, override):
    from pulsara_agent.mcp_config import OAuthAuthorization
    from pulsara_agent.mcp_credentials import LocalMcpOAuthRecord
    from pulsara_agent.capability.mcp_management import auth_to_entry

    service, connections, edit, source = await fixture(tmp_path)
    auth = OAuthAuthorization()
    endpoint = "https://explicit.example/mcp" if override else "https://example.org/mcp"
    edit = replace(
        edit,
        overlay=replace(
            edit.overlay, auth=auth, endpoint=endpoint if override else None
        ),
        secret_changes=(),
    )
    await service.replace_plugin_mcp_connection_overlay(edit, connections=connections)
    owner = plugin_connection_owner(edit.identity, "docs")
    record = LocalMcpOAuthRecord(
        owner,
        endpoint,
        "https://issuer.example",
        "client",
        "read",
        '{"access_token":"private-grant","token_type":"Bearer"}',
        '{"client_id":"client"}',
        auth_json=json.dumps(auth_to_entry(auth)),
    )
    await connections.settings.replace_mcp_authorization(owner, record, expected=None)
    raw = json.loads((source / "mcp.json").read_text())
    raw["mcpServers"]["docs"]["url"] = "https://changed.example/mcp"
    (source / "mcp.json").write_text(json.dumps(raw))
    result = await service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, monotonic() + 30, replace=True
        ),
        connections=connections,
    )
    assert result.disposition.value == "REPLACED" and not result.enabled
    assert connections.settings.read().mcp_authorization(owner) == (
        record if override else None
    )
    await connections.aclose()


@pytest.mark.anyio
async def test_replace_cleanup_failure_keeps_confirmed_cut_and_reports_attention(
    tmp_path, monkeypatch
):
    service, connections, edit, source = await fixture(tmp_path)
    await service.replace_plugin_mcp_connection_overlay(edit, connections=connections)
    raw = json.loads((source / "mcp.json").read_text())
    raw["mcpServers"] = {}
    (source / "mcp.json").write_text(json.dumps(raw))

    async def fail(*args, **kwargs):
        raise OSError("fixture private storage failure")

    monkeypatch.setattr(connections.settings, "replace_mcp_secrets", fail)
    result = await service.install_local_plugin(
        InstallLocalPluginRequest(
            source, PluginScopeKind.USER, monotonic() + 30, replace=True
        ),
        connections=connections,
    )
    assert result.disposition.value == "REPLACED" and result.cleanup_attention
    state = service._store().read_state(service._store().layout(edit.identity))
    assert state.current_package_install_id == result.package_install_id
    assert not state.enabled and not state.mcp_connection_overlays
    await connections.aclose()


@pytest.mark.anyio
async def test_enable_rechecks_overlay_and_presence_but_not_secret_rotation(tmp_path):
    service, connections, edit, _ = await fixture(tmp_path)
    await service.replace_plugin_mcp_connection_overlay(edit, connections=connections)
    store = service._store()
    state = store.read_state(store.layout(edit.identity))
    reviewed = connection_review(
        state.mcp_connection_overlays, connections.settings.read().mcp_secret,
        servers=service.inspect_local_plugins(InspectLocalPluginsRequest(monotonic() + 30)).instances[0].summary.mcp.mcp_servers,
        identity=edit.identity,
    )
    assert review_from_dict(review_to_dict(reviewed)) == reviewed
    request = SetLocalPluginEnabledRequest(
        PluginScopeKind.USER,
        edit.identity.plugin_id,
        True,
        edit.expected_package_install_id,
        monotonic() + 30,
        connection_review=reviewed,
        external_process_acceptance=ExternalProcessAcceptance.ACCEPTED,
    )
    binding = edit.secret_changes[0].binding
    await connections.settings.replace_mcp_secrets(binding.owner, ((binding, None),))
    result = service.set_local_plugin_enabled(request)
    assert result.disposition.value == "STALE"
    assert not store.read_state(store.layout(edit.identity)).enabled
    # Pure rotation preserves the reviewed presence. No secret revision is added.
    await connections.settings.replace_mcp_secrets(
        binding.owner, ((binding, "rotated-private-value"),)
    )
    assert service.set_local_plugin_enabled(request).disposition.value == "ENABLED"
    await service.replace_plugin_mcp_connection_overlay(
        replace(
            edit,
            expected_overlay=edit.overlay,
            overlay=replace(edit.overlay, endpoint="https://other.example/mcp"),
            secret_changes=(),
            retain_credentials_confirmed=True,
        ),
        connections=connections,
    )
    # Even already-enabled state does not turn stale review into acceptance.
    assert service.set_local_plugin_enabled(request).disposition.value == "STALE"
    assert "rotated-private-value" not in json.dumps(review_to_dict(reviewed))


@pytest.mark.anyio
async def test_plugin_oauth_uses_shared_login_runtime_and_removal_owner(tmp_path):
    from tests.test_mcp_oauth import oauth_server
    from pulsara_agent.conversation_kernel.mcp.oauth import McpOAuthNeedsLogin

    service, connections, edit, _ = await fixture(tmp_path)
    async with oauth_server(tmp_path / "oauth") as (manager, _, local_config, *rest):
        connections.oauth = manager
        connections.settings = manager.settings
        connections.lane = manager.lane
        await service.replace_plugin_mcp_connection_overlay(
            replace(
                edit,
                overlay=replace(
                    edit.overlay,
                    endpoint=local_config.transport.endpoint,
                    auth=local_config.auth,
                ),
                secret_changes=(),
            ),
            connections=connections,
        )
        flow = await service.authorize_plugin_mcp(
            identity=edit.identity,
            local_server_id="docs",
            expected_package_install_id=edit.expected_package_install_id,
            deadline_monotonic=monotonic() + 30,
            connections=connections,
        )
        record = await asyncio.wait_for(flow.task, 5)
        assert record.owner == plugin_connection_owner(edit.identity, "docs")
        store = service._store()
        layout = store.layout(edit.identity)
        state = store.read_state(layout)
        assert not state.enabled  # Login never grants enablement.
        config = bind_plugin_authorization(
            flow.config,
            edit.identity,
            state,
            local_server_id="docs",
            current_state=lambda identity: store.read_state(store.layout(identity)),
            oauth_manager=manager,
        )
        assert await config.authorization_provider() == (
            "Bearer initial-private-token",
            ("initial-private-token",),
        )
        outcome = await service.remove_local_plugin(
            RemoveLocalPluginRequest(
                PluginScopeKind.USER,
                edit.identity.plugin_id,
                monotonic() + 30,
                edit.expected_package_install_id,
            ),
            connections=connections,
        )
        assert outcome.disposition.value == "REMOVED"
        assert connections.settings.read().mcp_authorization(record.owner) is None
        with pytest.raises(McpOAuthNeedsLogin):
            await config.authorization_provider()


@pytest.mark.anyio
async def test_disabled_plugin_connection_edit_is_private_exact_and_does_not_mutate_package(
    tmp_path,
):
    service, connections, request, source = await fixture(tmp_path)
    source_before = (source / "mcp.json").read_bytes()
    result = await service.replace_plugin_mcp_connection_overlay(
        request, connections=connections
    )
    assert result.applied and not result.cleanup_attention
    store = service._store()
    layout = store.layout(request.identity)
    state = store.read_state(layout)
    assert not state.enabled
    assert state.mcp_connection_overlays == (request.overlay,)
    assert b"private-fixture-value" not in layout.state_path.read_bytes()
    assert (source / "mcp.json").read_bytes() == source_before
    assert (
        layout.plugin_package_parent / request.expected_package_install_id / "mcp.json"
    ).read_bytes() == source_before
    private = connections.settings.read()
    summary = store.read_package_summary(
        layout,
        state.current_package_install_id,
        deadline_monotonic=monotonic() + 30,
        cancellation=NeverCancelPluginOperation(),
        scrub_set=service._capture_scrub_set(request),
    )
    config = materialize_plugin_mcp_definition(
        identity=request.identity,
        state=state,
        server=summary.mcp.mcp_servers[0],
        package_root=layout.plugin_package_parent / state.current_package_install_id,
        data_root=layout.data_root,
        secret_resolver=private.mcp_secret,
    )
    assert config.resolved_headers()["Authorization"] == "Bearer private-fixture-value"
    assert config.physical_lifetime_anchor is None
    with pytest.raises(McpManagementConflict):
        await service.replace_plugin_mcp_connection_overlay(
            request, connections=connections
        )
    await connections.aclose()


@pytest.mark.anyio
async def test_plugin_restore_defaults_removes_only_its_private_binding(tmp_path):
    service, connections, request, _ = await fixture(tmp_path)
    await service.replace_plugin_mcp_connection_overlay(
        request, connections=connections
    )
    result = await service.replace_plugin_mcp_connection_overlay(
        replace(
            request, expected_overlay=request.overlay, overlay=None, secret_changes=()
        ),
        connections=connections,
    )
    assert result.applied
    assert (
        connections.settings.read().mcp_secret(request.secret_changes[0].binding)
        is None
    )
    store = service._store()
    assert (
        store.read_state(store.layout(request.identity)).mcp_connection_overlays == ()
    )
    await connections.aclose()


@pytest.mark.anyio
async def test_plugin_destination_change_and_stale_package_never_reuse_secret_silently(
    tmp_path,
):
    service, connections, request, _ = await fixture(tmp_path)
    await service.replace_plugin_mcp_connection_overlay(
        request, connections=connections
    )
    updated = replace(
        request,
        expected_overlay=request.overlay,
        overlay=replace(request.overlay, endpoint="https://new.example.org/mcp"),
        secret_changes=(),
    )
    with pytest.raises(ValueError, match="确认"):
        await service.replace_plugin_mcp_connection_overlay(
            updated, connections=connections
        )
    with pytest.raises(McpManagementConflict):
        await service.replace_plugin_mcp_connection_overlay(
            replace(updated, expected_package_install_id="pkg_" + "0" * 32),
            connections=connections,
        )
    result = await service.replace_plugin_mcp_connection_overlay(
        replace(updated, retain_credentials_confirmed=True), connections=connections
    )
    assert result.applied
    await connections.aclose()


def test_overlay_closed_codec_and_transport_ownership():
    overlay = PluginMcpConnectionOverlay(
        "local", "stdio", environment=(("REGION", "east"),)
    )
    assert overlay_from_dict(overlay_to_dict(overlay)) == overlay
    with pytest.raises(ValueError):
        overlay_from_dict({**overlay_to_dict(overlay), "command": "other-program"})
    with pytest.raises(ValueError):
        PluginMcpConnectionOverlay(
            "local", "stdio", environment=(("PLUGIN_ROOT", "/tmp/other"),)
        )
    with pytest.raises(ValueError):
        PluginMcpConnectionOverlay(
            "local", "streamable_http", environment=(("REGION", "east"),), auth=NoAuth()
        )


@pytest.mark.anyio
async def test_plugin_delete_cleans_private_values_but_not_data_and_stale_delete_is_inert(
    tmp_path,
):
    service, connections, request, source = await fixture(tmp_path)
    await service.replace_plugin_mcp_connection_overlay(
        request, connections=connections
    )
    store = service._store()
    layout = store.layout(request.identity)
    layout.data_root.mkdir(parents=True)
    (layout.data_root / "user-data.txt").write_text("keep data")
    removal = RemoveLocalPluginRequest(
        PluginScopeKind.USER, "example", monotonic() + 30, "pkg_" + "0" * 32
    )
    outcome = await service.remove_local_plugin(removal, connections=connections)
    assert outcome.disposition.value == "STALE"
    binding = request.secret_changes[0].binding
    assert connections.settings.read().mcp_secret(binding) == "private-fixture-value"
    assert store.read_state(layout) is not None
    outcome = await service.remove_local_plugin(
        replace(
            removal,
            expected_current_package_install_id=request.expected_package_install_id,
        ),
        connections=connections,
    )
    assert outcome.disposition.value == "REMOVED" and not outcome.cleanup_attention
    assert connections.settings.read().mcp_secret(binding) is None
    assert store.read_state(layout) is None
    assert (layout.data_root / "user-data.txt").read_text() == "keep data"
    assert source.exists()
    await connections.aclose()


@pytest.mark.anyio
async def test_disabled_plugin_inspection_keeps_overlay_for_one_shared_editor(tmp_path):
    from pulsara_agent.web_app.session_controller import _user_plugins_payload

    service, connections, request, _ = await fixture(tmp_path)
    await service.replace_plugin_mcp_connection_overlay(
        request, connections=connections
    )
    inspection = service.inspect_local_plugins(
        InspectLocalPluginsRequest(monotonic() + 30)
    )
    try:
        payload = _user_plugins_payload(
            inspection, connections.settings.read().mcp_secret
        )
        item = payload["items"][0]
        assert not item["enabled"]
        assert item["mcp_connections"][0]["overlay"] == overlay_to_dict(request.overlay)
        assert item["mcp_connections"][0]["config"]["auth"]["type"] == "bearer"
        assert "private-fixture-value" not in json.dumps(payload)
    finally:
        inspection.close()
        await connections.aclose()
