from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.model_catalog import (
    MINIMUM_SELECTABLE_CONTEXT_TOKENS,
    ModelTargetKey,
    ReasoningProviderDefault,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ModelConnectionAuthentication,
    ModelConnectionConfig,
    ModelConnectionId,
    ReasoningWireProfile,
    UserDeclaredModelTarget,
)
from pulsara_agent.settings import (
    LocalModelApiKey,
    LocalSettings,
    LocalSettingsStore,
    local_settings_from_dict,
    local_settings_to_dict,
    write_local_settings,
)
from pulsara_agent.web.config import (
    WebConnection,
    deepseek_candidate,
    resolve_web_connection,
    web_connection_from_dict,
    web_settings_public,
)


def _model(name: str, endpoint: str = "https://api.deepseek.com", *, custom=False):
    return ModelConnectionConfig(
        ModelConnectionId(derive_id("connection", "web-settings-test", name)),
        ModelTargetKey(
            "user_declared" if custom else "deepseek",
            WireApi.OPENAI_CHAT_COMPLETIONS,
            name,
        ),
        endpoint,
        ReasoningWireProfile.PROVIDER_DEFAULT
        if custom
        else ReasoningWireProfile.CATALOG_STANDARD,
        user_declared=UserDeclaredModelTarget(
            name,
            MINIMUM_SELECTABLE_CONTEXT_TOKENS,
            8192,
            True,
            ReasoningProviderDefault(),
            ModelConnectionAuthentication.BEARER_API_KEY,
        )
        if custom
        else None,
    )


def _settings(*models, web=None):
    return LocalSettings(
        model_connections=models,
        model_api_keys=tuple(
            LocalModelApiKey(model.id, f"private-{index}")
            for index, model in enumerate(models)
        ),
        web=web,
        input_suggestions_enabled=False,
        cache_warming_enabled=True,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1/",
        "https://api.deepseek.com:443/v1/chat/completions",
        "https://api.deepseek.com/proxy/v1",
        "https://api.deepseek.com/v1/chat/completions",
    ],
)
@pytest.mark.parametrize("custom", [False, True])
def test_official_deepseek_candidate_requires_explicit_selection(endpoint, custom):
    model = _model("saved", endpoint, custom=custom)
    settings = _settings(model)
    assert deepseek_candidate(settings) == model
    assert resolve_web_connection(settings) is None
    assert web_settings_public(settings)["mode"] == "free"
    chosen = replace(
        settings, web=WebConnection("deepseek", model_connection_id=model.id)
    )
    assert resolve_web_connection(chosen) == ("deepseek", "private-0")
    assert chosen.web.private() == {
        "provider": "deepseek",
        "model_connection_id": model.id.value,
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.deepseek.com.evil.test/v1",
        "https://evil.test/api.deepseek.com",
        "https://deepseek.com/v1",
        "https://api.deepseek.com@evil.test",
        "https://evil.test@api.deepseek.com",
        "https://api.deepseek.com:8443/v1",
        "http://api.deepseek.com",
        "https://api.deepseek.com/v1?upstream=other",
        "https://api.deepseek.com/v1#other",
        "https://api.deepseek.com/v1/../v1",
        "https://api.deepseek.com\n.evil.test/v1",
        "https://api.deepseek.com./v1",
    ],
)
@pytest.mark.parametrize("custom", [False, True])
def test_custom_and_catalog_candidates_reject_spoofed_or_unofficial_endpoint(
    endpoint, custom
):
    model = _model("saved", endpoint, custom=custom)
    settings = _settings(
        model, web=WebConnection("deepseek", model_connection_id=model.id)
    )
    assert deepseek_candidate(settings) is None
    assert resolve_web_connection(settings) is None
    assert web_settings_public(settings)["connection"]["has_api_key"] is False
    assert web_settings_public(settings)["mode"] == "free"


def test_candidate_order_missing_reference_and_public_secret_boundary():
    first, second = _model("first", custom=True), _model("second")
    settings = _settings(first, second)
    assert deepseek_candidate(settings) == first
    # A model label is user text; a saved credential must never escape through it.
    secret_label = replace(
        first,
        user_declared=replace(first.user_declared, configuration_name="private-0"),
    )
    settings = replace(
        settings,
        model_connections=(secret_label, second),
        web=WebConnection("exa", "web-private"),
    )
    assert "private-0" not in json.dumps(web_settings_public(settings))
    assert "web-private" not in repr(settings)
    assert "web-private" not in json.dumps(web_settings_public(settings))
    assert settings.credential_scrub_set().scrub_text("web-private") != "web-private"
    missing = replace(
        settings,
        web=WebConnection("deepseek", model_connection_id=_model("deleted").id),
    )
    assert resolve_web_connection(missing) is None
    assert web_settings_public(missing)["mode"] == "free"
    assert web_settings_public(missing)["connection"]["model_connection_label"] is None
    missing_key = replace(
        settings,
        model_api_keys=(
            LocalModelApiKey(first.id, " "),
            LocalModelApiKey(second.id, "private-1"),
        ),
    )
    assert deepseek_candidate(missing_key) == second


def test_official_endpoint_needs_valid_model_source_and_saved_authentication():
    model = _model("custom", custom=True)
    unauthenticated = replace(
        model,
        user_declared=replace(
            model.user_declared, authentication=ModelConnectionAuthentication.NONE
        ),
    )
    assert (
        deepseek_candidate(LocalSettings(model_connections=(unauthenticated,))) is None
    )
    other_provider = replace(
        _model("other"),
        target=ModelTargetKey("openai", WireApi.OPENAI_CHAT_COMPLETIONS, "other"),
    )
    assert deepseek_candidate(_settings(other_provider)) is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"provider": "other", "api_key": "key"},
        {"provider": "exa", "api_key": " "},
        {"provider": "exa", "api_key": None},
        {"provider": "exa", "model_connection_id": _model("ref").id.value},
        {"provider": "deepseek", "model_connection_id": "bad"},
        {"provider": "deepseek", "model_connection_id": []},
        {"provider": "exa", "api_key": "key", "extra": True},
        {
            "provider": "deepseek",
            "api_key": "key",
            "model_connection_id": _model("ref").id.value,
        },
    ],
)
def test_saved_web_connection_is_closed_and_validated(body):
    with pytest.raises(ValueError):
        web_connection_from_dict(body)


def test_web_settings_survive_model_mutations_and_roundtrip(tmp_path: Path):
    async def exercise():
        first, second = _model("first"), _model("second")
        initial = _settings(first, web=WebConnection("tavily", "web-private"))
        assert local_settings_from_dict(local_settings_to_dict(initial)) == initial
        store = LocalSettingsStore(tmp_path / "home" / "local-settings.yaml")
        write_local_settings(store.path, initial)
        added = await store.add_model_connection(
            connection=second, api_key="second-private"
        )
        assert added.web == initial.web
        updated = await store.update_model_connection(
            connection=replace(second, base_url="https://api.deepseek.com/v1"),
            api_key="changed-private",
        )
        assert updated.web == initial.web
        deleted, found = await store.delete_model_connection(second.id)
        assert found and deleted == initial
        kept = await store.save_web_connection(
            {"provider": "tavily", "key_action": "keep"}
        )
        assert kept == initial
        replaced = await store.save_web_connection(
            {"provider": "exa", "key_action": "replace", "api_key": "new-private"}
        )
        assert replace(replaced, web=initial.web) == initial
        selected = await store.use_saved_deepseek_web_connection()
        assert selected.web == WebConnection("deepseek", model_connection_id=first.id)
        rotated = await store.update_model_connection(
            connection=first, api_key="rotated-private"
        )
        assert resolve_web_connection(rotated) == ("deepseek", "rotated-private")
        changed = await store.update_model_connection(
            connection=replace(first, base_url="https://custom.example/v1"),
            api_key="custom-private",
        )
        assert changed.web == selected.web and resolve_web_connection(changed) is None
        removed, _ = await store.delete_model_connection(first.id)
        assert removed.web == selected.web
        assert resolve_web_connection(removed) is None
        cleared = await store.clear_web_connection()
        assert cleared.web is None
        assert (
            cleared.input_suggestions_enabled is False
            and cleared.cache_warming_enabled is True
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "exa", "key_action": "keep"},
        {"provider": "exa", "key_action": "replace"},
        {"provider": "exa", "key_action": "keep", "api_key": "key"},
        {"provider": "exa", "key_action": "clear"},
        {"provider": "exa", "key_action": "replace", "api_key": "key", "extra": True},
    ],
)
def test_write_only_key_actions_reject_inconsistent_mutations(body):
    with pytest.raises(ValueError):
        LocalSettingsStore.web_draft(LocalSettings(), body)


def test_web_routes_are_user_level_and_never_return_keys(tmp_path: Path):
    from pulsara_agent.desktop_app.http_server import LocalHttpServer
    from tests.test_desktop_http_surface import _Bridge, _model_server_dependencies

    async def exercise():
        (tmp_path / "index.html").write_text("Pulsara")
        store = LocalSettingsStore(tmp_path / "home" / "local-settings.yaml")
        deps = _model_server_dependencies()
        deps["settings"] = store
        # No session owner exists: these routes must use only the user settings owner.
        server = LocalHttpServer(
            sessions=SimpleNamespace(),
            bridge=_Bridge(),
            static_root=tmp_path,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            **deps,
        )
        await server.start()
        try:
            async with ClientSession(
                headers={
                    "X-Pulsara-Connection-Id": "connection-1",
                    "X-Pulsara-Connection-Generation": "1",
                }
            ) as client:
                base = server.origin + "/api/local-settings/web"
                async with client.get(base) as response:
                    assert response.status == 200
                    assert (await response.json())["mode"] == "free"
                async with client.post(base + "/use-deepseek", json={}) as response:
                    assert response.status == 400
                model = _model("saved")
                await store.add_model_connection(
                    connection=model, api_key="model-private"
                )
                async with client.get(base) as response:
                    value = await response.json()
                    assert (
                        value["deepseek_candidate"]["model_connection_id"]
                        == model.id.value
                    )
                    assert value["mode"] == "free"
                async with client.post(base + "/use-deepseek", json={}) as response:
                    assert response.status == 200
                    text = await response.text()
                    assert "model-private" not in text
                    assert json.loads(text)["mode"] == "account"
                async with client.put(
                    base,
                    json={
                        "provider": "exa",
                        "key_action": "replace",
                        "api_key": "web-private",
                    },
                ) as response:
                    assert response.status == 200
                    assert "web-private" not in await response.text()
                async with client.put(
                    base, json={"provider": "tavily", "key_action": "keep"}
                ) as response:
                    assert response.status == 400
                    assert "web-private" not in await response.text()
                async with client.delete(base) as response:
                    assert response.status == 200
                    assert (await response.json())["mode"] == "free"
        finally:
            await server.aclose()

    asyncio.run(exercise())
