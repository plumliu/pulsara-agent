"""Direct USER Hook editing/review uses the existing source and trust owners."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.capability.change_observation import CapabilityUiChange
from pulsara_agent.capability.pulsara_home import resolve_pulsara_home
from pulsara_agent.hooks.config_parser import (
    HookConfigParseError,
    MAXIMUM_HOOK_CONFIG_BYTES,
)
from pulsara_agent.hooks.contracts import HookSourceKind
from pulsara_agent.hooks.source import (
    HookSourceConflict,
    LocalHookConfigText,
    LocalHookSourceProvider,
)
from pulsara_agent.plugins.management import PluginManagementService
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.web_app.session_controller import LocalSessionController

CONFIG = json.dumps(
    {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hello"}]}]
        }
    }
)


@pytest.fixture
def owners(tmp_path, monkeypatch):
    from pulsara_agent.settings import LocalSettingsStore
    home = tmp_path / "home"
    monkeypatch.setenv("PULSARA_HOME", str(home))
    boundary = ProcessCredentialBoundary("private-test-credential")
    plugins = PluginManagementService(
        credential_boundary=boundary,
        pulsara_home_resolution=resolve_pulsara_home(str(home)),
    )
    core = SimpleNamespace(
        mcp_management=SimpleNamespace(lane=asyncio.Lock(), settings=LocalSettingsStore(home / "local-settings.yaml")),
        _credential_boundary=boundary,
        _plugin_management=lambda: plugins,
        _canonical_deadline=lambda: monotonic() + 5,
        mark_capability_change=AsyncMock(return_value=2),
    )
    controller = LocalSessionController(
        core=core,
        permission_policy=preset_to_policy("read-only"),
        active_skill_names=frozenset(),
    )
    return controller, controller._user_hook_provider(), plugins


def accept(controller, review):
    return controller.trust_user_hook(
        plugin_id=review.get("plugin_id"),
        definition_digest=review["definition_digest"],
        trust={
            k: review[k] for k in ("enabled", "trusted_definition_digest", "trusted_at")
        },
        accepted=True,
    )


def test_missing_creation_warnings_crlf_and_no_automatic_trust(owners):
    controller, provider, _ = owners

    async def run():
        local = (await controller.read_user_hooks())["sources"][0]
        assert local["exists"] is False and not local["can_trust"]
        draft = CONFIG.replace("{", "{\r\n", 1)[:-1] + ',"unknown":true}'
        initial = await controller.read_user_hook_config()
        outcome = await controller.save_user_hook_config(
            exists=initial["exists"], expected_text=initial["text"], text=draft
        )
        assert outcome == {"status": "APPLIED", "message": None, "pending_sessions": 2}
        assert provider.user_config_path.read_bytes() == draft.encode()
        local = (await controller.read_user_hooks())["sources"][0]
        assert local["definition_count"] == 1 and local["runnable_handler_count"] == 0
        assert local["trust_state"] == "UNTRUSTED" and local["can_trust"]
        assert local["diagnostics"][0]["code"] == "HOOK_CONFIG_UNKNOWN_ROOT_FIELD"
        review = await controller.read_user_hook_review()
        assert review["definitions"][0]["command"] == "echo hello"
        assert review["diagnostics"] == local["diagnostics"]
        assert (await controller.read_user_hook_config())["text"] == draft

    asyncio.run(run())


def test_raw_bad_json_editable_parse_error_keeps_file_and_stale_save_conflicts(owners):
    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_bytes(b"{broken\r\n")

    async def run():
        local = (await controller.read_user_hooks())["sources"][0]
        assert local["source_disposition"] == "UNAVAILABLE"
        assert local.get("editable") is not False and not local["can_trust"]
        initial = await controller.read_user_hook_config()
        with pytest.raises(HookConfigParseError):
            await controller.save_user_hook_config(
                exists=True, expected_text=initial["text"], text="{still broken"
            )
        assert provider.user_config_path.read_bytes() == b"{broken\r\n"
        provider.user_config_path.write_text(CONFIG)
        with pytest.raises(HookSourceConflict):
            await controller.save_user_hook_config(
                exists=True, expected_text=initial["text"], text=CONFIG
            )
        controller.core.mark_capability_change.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["utf8", "symlink", "large", "directory"])
def test_unreadable_sources_never_become_empty_editable_text(owners, tmp_path, kind):
    controller, provider, _ = owners
    path = provider.user_config_path
    path.parent.mkdir()
    if kind == "utf8":
        path.write_bytes(b"\xff")
    elif kind == "large":
        path.write_bytes(b" " * (MAXIMUM_HOOK_CONFIG_BYTES + 1))
    elif kind == "directory":
        path.mkdir()
    else:
        outside = tmp_path / "outside.json"
        outside.write_text(CONFIG)
        path.symlink_to(outside)

    async def run():
        local = (await controller.read_user_hooks())["sources"][0]
        assert local["editable"] is False and not local["can_trust"]
        with pytest.raises((OSError, ValueError)):
            await controller.read_user_hook_config()
        with pytest.raises((OSError, ValueError)):
            await controller.save_user_hook_config(
                exists=False, expected_text="", text=CONFIG
            )
        controller.core.mark_capability_change.assert_not_awaited()

    asyncio.run(run())


def test_trust_preserves_disabled_source_and_whitespace_validity(owners):
    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_text(CONFIG)
    subject = provider.source_subject(HookSourceKind.USER_FILE)
    provider.trust_store.set_enabled(subject, enabled=False)

    async def run():
        review = await controller.read_user_hook_review()
        assert review["enabled"] is False and review["definition_count"] == 1
        provider.user_config_path.write_text(json.dumps(json.loads(CONFIG), indent=2))
        assert (await accept(controller, review))["status"] == "APPLIED"
        local = (await controller.read_user_hooks())["sources"][0]
        assert local["trust_state"] == "TRUSTED" and local["enabled"] is False
        assert local["runnable_handler_count"] == 0 and local["definition_count"] == 1
        original = provider.trust_store.read(subject)
        baseline = await controller.read_user_hook_config()
        await controller.save_user_hook_config(
            exists=True, expected_text=baseline["text"], text=CONFIG
        )
        assert provider.trust_store.read(subject) == original

    asyncio.run(run())


@pytest.mark.parametrize("change", ["definition", "trust"])
def test_review_race_is_checked_inside_original_source_lock(
    owners, monkeypatch, change
):
    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_text(CONFIG)
    subject = provider.source_subject(HookSourceKind.USER_FILE)
    store_class = type(provider.trust_store)
    lock = store_class.source_lock
    entered = []

    async def run():
        review = await controller.read_user_hook_review()

        @contextmanager
        def racing_lock(self, actual_subject, **kwargs):
            # Another cooperative writer commits before the submitter acquires flock.
            if not entered:
                entered.append(True)
                if change == "definition":
                    provider.user_config_path.write_text(
                        CONFIG.replace("hello", "changed")
                    )
                else:
                    provider.trust_store.set_enabled(subject, enabled=False)
            with lock(self, actual_subject, **kwargs):
                yield

        monkeypatch.setattr(store_class, "source_lock", racing_lock)
        with pytest.raises(HookSourceConflict):
            await accept(controller, review)
        assert entered
        assert provider.trust_store.read(subject).trusted_definition_digest is None
        controller.core.mark_capability_change.assert_not_awaited()

    asyncio.run(run())


def test_source_lock_deadline_bounds_wait_without_writing(owners):
    _, provider, _ = owners
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with provider.trust_store.source_lock(
            provider.source_subject(HookSourceKind.USER_FILE)
        ):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(TimeoutError):
            provider.save_user_config(
                expected=LocalHookConfigText(False, ""),
                text=CONFIG,
                deadline_monotonic=monotonic() + 0.05,
            )
        assert not provider.user_config_path.exists()
    finally:
        release.set()
        worker.join()


@pytest.mark.parametrize("failure", ["fsync", "notify"])
def test_published_mutations_report_attention_and_still_mark_refresh(
    owners, monkeypatch, failure
):
    controller, provider, _ = owners
    if failure == "fsync":
        fsync = os.fsync

        def fail_directory(fd):
            import stat

            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync failed")
            fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_directory)
    else:
        controller.core.mark_capability_change.side_effect = RuntimeError(
            "notification failed"
        )

    async def run():
        outcome = await controller.save_user_hook_config(
            exists=False, expected_text="", text=CONFIG
        )
        assert outcome["status"] == "ATTENTION" and "已写入" in outcome["message"]
        assert provider.user_config_path.read_text() == CONFIG
        controller.core.mark_capability_change.assert_awaited_once_with(
            None, change=CapabilityUiChange.settled(
                "SAVE_HOOK_CONFIG", "USER", {"kind":"HOOK_SOURCE", "source_kind":"LOCAL"}, "CLEANUP_ATTENTION" if failure == "fsync" else "APPLIED"
            )
        )

    asyncio.run(run())


def test_started_write_settles_after_cancel_but_queued_cancel_does_not_write(
    owners, monkeypatch
):
    controller, provider, _ = owners
    entered = threading.Event()
    release = threading.Event()
    save = LocalHookSourceProvider.save_user_config

    def delayed(self, **kwargs):
        entered.set()
        assert release.wait(5)
        save(self, **kwargs)

    monkeypatch.setattr(LocalHookSourceProvider, "save_user_config", delayed)

    async def run():
        async with controller._capability_mutation_lock:
            queued = asyncio.create_task(
                controller.save_user_hook_config(
                    exists=False, expected_text="", text=CONFIG
                )
            )
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        assert not entered.is_set() and not provider.user_config_path.exists()
        task = asyncio.create_task(
            controller.save_user_hook_config(
                exists=False, expected_text="", text=CONFIG
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        assert (await task)["status"] == "APPLIED"
        assert provider.user_config_path.read_text() == CONFIG
        controller.core.mark_capability_change.assert_awaited_once_with(
            None, change=CapabilityUiChange.settled(
                "SAVE_HOOK_CONFIG", "USER", {"kind":"HOOK_SOURCE", "source_kind":"LOCAL"}, "APPLIED"
            )
        )

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_user_plugin_review_rejects_disabled_and_replaced_packages_and_closes_anchors(
    owners, tmp_path, monkeypatch
):
    from pulsara_agent.capability.mcp_management import LocalMcpManagementService
    from pulsara_agent.settings import LocalSettingsStore
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        SetLocalPluginEnabledRequest,
        PluginScopeKind,
        ExternalProcessAcceptance,
    )
    from pulsara_agent.plugins.view import EnabledPluginViewOwner

    controller, _, plugins = owners
    source = tmp_path / "native"
    (source / "dev.pulsara/hooks").mkdir(parents=True)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "test-hooks",
                "version": "1.0.0",
                "description": "inert fixture",
            }
        )
    )
    (source / "dev.pulsara/hooks/hooks.json").write_text(CONFIG)
    connections = LocalMcpManagementService(
        LocalSettingsStore(tmp_path / "home/local-settings.yaml"),
        user_config_path=tmp_path / "home/mcp.yaml",
    )
    captured = []
    observe = EnabledPluginViewOwner.observe

    def watching(self, **kwargs):
        view = observe(self, **kwargs)
        captured.extend(view.instances)
        return view

    monkeypatch.setattr(EnabledPluginViewOwner, "observe", watching)

    async def run():
        installed = await plugins.install_local_plugin(
            InstallLocalPluginRequest(source, PluginScopeKind.USER, monotonic() + 5),
            connections=connections,
        )
        assert installed.disposition.value == "INSTALLED", installed

        def enabled(value, package=installed.package_install_id):
            result = plugins.set_local_plugin_enabled(
                SetLocalPluginEnabledRequest(
                    PluginScopeKind.USER,
                    "test-hooks",
                    value,
                    package,
                    monotonic() + 5,
                    connection_review=(),
                    external_process_acceptance=ExternalProcessAcceptance.ACCEPTED
                    if value
                    else None,
                )
            )
            assert result.disposition.value in {"ENABLED", "DISABLED"}, result

        assert len((await controller.read_user_hooks())["sources"]) == 1
        enabled(True)
        catalog = await controller.read_user_hooks()
        plugin = next(item for item in catalog["sources"] if item.get("plugin_id"))
        assert plugin["trust_state"] == "UNTRUSTED"
        assert plugin["definitions"][0]["command"] == "echo hello"
        assert plugin["runnable_handler_count"] == 0
        review = await controller.read_user_hook_review("test-hooks")
        assert review["definition_count"] == 1
        for instance in captured:
            assert instance.physical_lifetime_anchor._closed
            assert instance.hooks.parsed.provenance.physical_lifetime_anchor._closed
        enabled(False)
        with pytest.raises(HookSourceConflict):
            await accept(controller, review)
        enabled(True)
        (source / "plugin.json").write_text(
            (source / "plugin.json")
            .read_text()
            .replace('1.0.0", "description', '2.0.0", "description')
        )
        newer = await plugins.install_local_plugin(
            InstallLocalPluginRequest(
                source, PluginScopeKind.USER, monotonic() + 5, replace=True
            ),
            connections=connections,
        )
        assert newer.disposition.value == "REPLACED", newer
        enabled(True, newer.package_install_id)
        with pytest.raises(HookSourceConflict):
            await accept(controller, review)

    asyncio.run(run())


def test_review_and_diagnostics_scrub_actual_credentials_without_hiding_definitions(
    owners,
):
    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_text(
        CONFIG.replace("echo hello", "echo private-test-credential visible-command")
    )

    async def run():
        review = await controller.read_user_hook_review()
        catalog = await controller.read_user_hooks()
        assert "private-test-credential" not in json.dumps(catalog)
        assert "visible-command" in catalog["sources"][0]["definitions"][0]["command"]
        assert "private-test-credential" not in json.dumps(review)
        assert "visible-command" in review["definitions"][0]["command"]
        assert (
            "private-test-credential"
            in (await controller.read_user_hook_config())["text"]
        )

    asyncio.run(run())


def test_unavailable_plugin_inventory_keeps_local_source_and_reports_failure(
    owners, monkeypatch
):
    controller, _, plugins = owners
    monkeypatch.setattr(plugins, "_store", lambda: None)

    async def run():
        catalog = await controller.read_user_hooks()
        assert len(catalog["sources"]) == 1 and catalog["details"]
        assert catalog["sources"][0]["exists"] is False

    asyncio.run(run())


def test_http_direct_hook_roundtrip_no_session_and_closed_selectors(owners, tmp_path):
    from aiohttp import ClientSession
    from tests.test_local_web_http_surface import _Bridge, _model_server_dependencies
    from pulsara_agent.web_app.http_server import LocalHttpServer

    controller, provider, _ = owners
    (tmp_path / "index.html").write_text("Pulsara")

    async def run():
        server = LocalHttpServer(
            sessions=controller,
            bridge=_Bridge(),
            static_root=tmp_path,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **_model_server_dependencies(),
        )
        await server.start()
        try:
            async with ClientSession() as client:
                origin = server.origin
                async with client.get(
                    origin + "/api/capabilities/hooks/local"
                ) as reply:
                    assert reply.status == 200
                    baseline = await reply.json()
                    assert baseline["exists"] is False
                async with client.put(
                    origin + "/api/capabilities/hooks/local",
                    json={"exists": False, "expected_text": "", "text": CONFIG},
                ) as reply:
                    assert (
                        reply.status == 200
                        and (await reply.json())["status"] == "APPLIED"
                    )
                async with client.get(
                    origin + "/api/capabilities/hooks/review"
                ) as reply:
                    assert reply.status == 200
                    review = await reply.json()
                payload = {
                    "plugin_id": None,
                    "definition_digest": review["definition_digest"],
                    "trust": {
                        k: review[k]
                        for k in ("enabled", "trusted_definition_digest", "trusted_at")
                    },
                    "accepted": True,
                }
                async with client.post(
                    origin + "/api/capabilities/hooks/trust", json=payload
                ) as reply:
                    assert reply.status == 200
                async with client.post(
                    origin + "/api/capabilities/hooks/trust", json=payload
                ) as reply:
                    assert reply.status == 409
                async with client.get(origin + "/api/capabilities/hooks") as reply:
                    assert reply.status == 200
                    assert (await reply.json())["sources"][0][
                        "trust_state"
                    ] == "TRUSTED"
                for path, method, body in [
                    (
                        "/api/capabilities/hooks/local",
                        "put",
                        {
                            "exists": True,
                            "expected_text": CONFIG,
                            "text": CONFIG,
                            "path": "/arbitrary",
                        },
                    ),
                    (
                        "/api/capabilities/hooks/trust",
                        "post",
                        {**payload, "scope": "WORKSPACE"},
                    ),
                    (
                        "/api/capabilities/hooks/trust",
                        "post",
                        {**payload, "accepted": False},
                    ),
                    (
                        "/api/capabilities/hooks/trust",
                        "post",
                        {**payload, "definitions": []},
                    ),
                ]:
                    async with getattr(client, method)(
                        origin + path, json=body
                    ) as reply:
                        assert reply.status == 400
                async with client.get(
                    origin + "/api/capabilities/hooks/review?scope=WORKSPACE"
                ) as reply:
                    assert reply.status == 400
                async with client.put(
                    origin + "/api/capabilities/hooks/local",
                    headers={"Origin": "https://foreign.example"},
                    json={"exists": True, "expected_text": CONFIG, "text": CONFIG},
                ) as reply:
                    assert reply.status == 403
                assert provider.user_config_path.read_text() == CONFIG
                assert not controller._by_session
        finally:
            await server.aclose()

    asyncio.run(run())


def test_trust_replace_fsync_failure_reports_written_and_keeps_adoption(
    owners, monkeypatch
):
    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_text(CONFIG)

    async def run():
        review = await controller.read_user_hook_review()
        fsync = os.fsync

        def fail_directory(fd):
            import stat

            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync failed")
            fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_directory)
        outcome = await accept(controller, review)
        assert outcome["status"] == "ATTENTION"
        assert (
            provider.trust_store.read(
                provider.source_subject(HookSourceKind.USER_FILE)
            ).trusted_definition_digest
            == review["definition_digest"]
        )
        controller.core.mark_capability_change.assert_awaited_once_with(
            None, change=CapabilityUiChange.settled(
                "TRUST_HOOK_SOURCE", "USER", {"kind":"HOOK_SOURCE", "source_kind":"LOCAL"}, "CLEANUP_ATTENTION"
            )
        )

    asyncio.run(run())


def test_unavailable_trust_stays_visible_and_cannot_be_directly_approved(owners):
    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_text(CONFIG)
    subject = provider.source_subject(HookSourceKind.USER_FILE)
    provider.trust_store.set_enabled(subject, enabled=True)
    state_path = provider.trust_store._state_path(subject)
    state_path.unlink()
    state_path.mkdir()

    async def run():
        source = (await controller.read_user_hooks())["sources"][0]
        assert source["definition_count"] == 1
        assert source["trust_state"] == "UNAVAILABLE" and not source["can_trust"]
        with pytest.raises(HookSourceConflict):
            await controller.read_user_hook_review()
        message = await controller.hook_error_message(
            ValueError("visible private-test-credential detail")
        )
        assert (
            "private-test-credential" not in message
            and "visible" in message
            and "detail" in message
        )

    asyncio.run(run())


def test_plugin_timeout_keeps_local_list_and_does_not_block_local_approval(
    owners, monkeypatch
):
    from time import sleep
    from pulsara_agent.plugins.package_core import PluginPackageTimedOut
    from pulsara_agent.plugins.view import EnabledPluginViewOwner

    controller, provider, _ = owners
    provider.user_config_path.parent.mkdir()
    provider.user_config_path.write_text(CONFIG)
    controller.core._canonical_deadline = lambda: monotonic() + 0.1
    calls = []

    def timeout(self, *, deadline_monotonic, **kwargs):
        calls.append(True)
        sleep(max(0, deadline_monotonic - monotonic()) + 0.01)
        raise PluginPackageTimedOut

    monkeypatch.setattr(EnabledPluginViewOwner, "observe", timeout)

    async def run():
        catalog = await controller.read_user_hooks()
        assert catalog["details"] and catalog["sources"][0]["exists"] is True
        assert (
            catalog["sources"][0]["definition_count"] == 1
            and catalog["sources"][0]["can_trust"]
        )
        assert catalog["sources"][0].get("editable") is not False
        review = await controller.read_user_hook_review()
        assert (await accept(controller, review))["status"] == "APPLIED"
        assert len(calls) == 1

    asyncio.run(run())


def test_hook_form_event_options_match_the_native_parser():
    from pulsara_agent.hooks.contracts import HookEventType
    from pulsara_agent.hooks.config_parser import (
        _CONTEXT_EVENTS,
        _MATCHER_IGNORED_EVENTS,
    )

    options = json.loads(
        (
            Path(__file__).resolve().parents[1] / "frontend/lib/hook-form-events.json"
        ).read_text()
    )
    assert {item["value"] for item in options} == {
        event.external_name for event in HookEventType
    }
    assert len(options) == len(HookEventType)
    for item in options:
        event = HookEventType.from_external_name(item["value"])
        assert item["matcher"] is (event not in _MATCHER_IGNORED_EVENTS)
        assert item["context"] is (event in _CONTEXT_EVENTS)
