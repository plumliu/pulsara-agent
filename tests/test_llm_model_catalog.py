from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from pulsara_agent.llm.model_catalog import (
    ModelCatalogEntryKey,
    ModelCatalogInvalid,
    ModelCatalogOwner,
    ModelsDevCatalogClient,
    ReasoningProviderDefault,
    ReasoningSelectableControls,
    RouteWireDialect,
    parse_models_dev_catalog,
    selectable_catalog,
)


def catalog_fixture() -> dict[str, object]:
    return {
        "zhipuai": {
            "id": "zhipuai",
            "name": "Zhipu AI",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://open.bigmodel.cn/api/paas/v4",
            "models": {
                "glm-5.3": {
                    "id": "glm-5.3",
                    "name": "GLM-5.3",
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high", "max"]}
                    ],
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1_000_000, "output": 131_072},
                },
                "glm-small": {
                    "id": "glm-small",
                    "name": "Small",
                    "reasoning": False,
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 255_999, "output": 8_192},
                },
            },
        },
        "openrouter": {
            "id": "mismatching-inner-route",
            "name": "OpenRouter",
            "npm": "@openrouter/ai-sdk-provider",
            "api": "https://openrouter.ai/api/v1",
            "models": {
                "z-ai/glm-5.2": {
                    "id": "wrong-inner-model",
                    "name": "GLM-5.2",
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "toggle"},
                        {"type": "effort", "values": ["high", "xhigh"]},
                        {"type": "budget_tokens", "min": 1024, "max": 4096},
                    ],
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "provider": {"shape": "responses"},
                    "limit": {
                        "context": 1_048_576,
                        "input": 900_000,
                        "output": 131_072,
                    },
                },
                "anthropic/claude-sonnet": {
                    "reasoning": True,
                    "reasoning_options": [],
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 256_000, "output": 8_192},
                },
                "google/gemini-pro": {
                    "reasoning": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 256_000, "output": 8_192},
                },
                "~~claude-kept": {
                    "reasoning": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 256_000, "output": 8_192},
                },
                "broken-options": {
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "future-control"},
                        {"type": "effort", "values": ["low", "low"]},
                    ],
                    "tool_call": "yes",
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 256_000, "output": 8_192},
                },
            },
        },
        "filtered": {
            "name": "Claude-looking provider names do not filter ordinary models",
            "npm": "@ai-sdk/anthropic",
            "api": "https://example.test/v1",
            "models": {
                "ordinary-model": {
                    "name": "Gemini display label is not a filter",
                    "reasoning": False,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 256_000, "output": 8_192},
                }
            },
        },
        "deepseek": {
            "name": "DeepSeek",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.deepseek.com",
            "models": {
                "deepseek-v4-flash": {
                    "name": "DeepSeek V4 Flash",
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "toggle"},
                        {"type": "effort", "values": ["low", "high", "max"]},
                    ],
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 1_000_000, "output": 384_000},
                }
            },
        },
    }


def test_models_dev_parser_preserves_route_identity_reasoning_combinations_and_hints() -> None:
    snapshot = parse_models_dev_catalog(catalog_fixture())
    zhipu = snapshot.entries[ModelCatalogEntryKey("zhipuai", "glm-5.3")]
    router = snapshot.entries[
        ModelCatalogEntryKey("openrouter", "z-ai/glm-5.2")
    ]

    assert isinstance(zhipu.reasoning, ReasoningSelectableControls)
    assert zhipu.reasoning.effort is not None
    assert zhipu.reasoning.effort.values == ("low", "high", "max")
    assert zhipu.wire_dialect is RouteWireDialect.OPENAI_COMPATIBLE
    assert zhipu.wire_shape_hint is None
    assert isinstance(router.reasoning, ReasoningSelectableControls)
    assert router.reasoning.toggle is not None
    assert router.reasoning.effort is not None
    assert router.reasoning.budget is not None
    assert router.reasoning.budget.closed
    assert router.wire_shape_hint == "responses"
    assert router.wire_dialect is RouteWireDialect.OPENAI_COMPATIBLE
    assert router.limits is not None
    assert router.limits.max_input_tokens == 900_000
    assert {item.code for item in snapshot.diagnostics} == {
        "catalog_route_inner_id_mismatch"
    }
    assert "catalog_model_inner_id_mismatch" in {
        item.code for item in router.diagnostics
    }


@pytest.mark.parametrize(
    "model_id",
    (
        "claude-x",
        "gemini-x",
        "anthropic/claude-x",
        "google/gemini-x",
        "~anthropic/claude-x",
        "~Claude-X",
        "provider/~GeMiNi-X",
    ),
)
def test_selectable_catalog_keeps_claude_and_gemini_model_ids(model_id: str) -> None:
    fixture = catalog_fixture()
    models = fixture["zhipuai"]["models"]  # type: ignore[index]
    models[model_id] = {  # type: ignore[index]
        "reasoning": False,
        "limit": {"context": 256_000, "output": 8_192},
    }
    selected = selectable_catalog(parse_models_dev_catalog(fixture))
    assert ModelCatalogEntryKey("zhipuai", model_id) in selected.entries


def test_selectable_catalog_uses_context_boundary_and_no_other_heuristic() -> None:
    snapshot = parse_models_dev_catalog(catalog_fixture())
    selected = selectable_catalog(snapshot)

    assert ModelCatalogEntryKey("zhipuai", "glm-small") not in selected.entries
    assert ModelCatalogEntryKey("zhipuai", "glm-5.3") in selected.entries
    assert ModelCatalogEntryKey("openrouter", "~~claude-kept") in selected.entries
    assert ModelCatalogEntryKey("filtered", "ordinary-model") in selected.entries
    for model_id in ("anthropic/claude-sonnet", "google/gemini-pro"):
        key = ModelCatalogEntryKey("openrouter", model_id)
        assert selected.entries[key] is snapshot.entries[key]
        assert selected.entries[key] in next(
            route.entries for route in selected.routes if route.route_id == "openrouter"
        )
    assert {route.route_id for route in selected.routes} == {
        "zhipuai",
        "openrouter",
        "filtered",
        "deepseek",
    }


def test_bad_reasoning_family_degrades_only_that_row() -> None:
    snapshot = parse_models_dev_catalog(catalog_fixture())
    broken = snapshot.entries[
        ModelCatalogEntryKey("openrouter", "broken-options")
    ]
    assert isinstance(broken.reasoning, ReasoningProviderDefault)
    assert broken.tool_call is None
    assert {item.code for item in broken.diagnostics} >= {
        "catalog_reasoning_option_kind_unknown",
        "catalog_reasoning_effort_invalid",
        "catalog_tool_call_invalid",
    }


def test_invalid_reasoning_family_keeps_valid_sibling_control() -> None:
    fixture = catalog_fixture()
    fixture["openrouter"]["models"]["z-ai/glm-5.2"][  # type: ignore[index]
        "reasoning_options"
    ] = [
        {"type": "toggle"},
        {"type": "effort", "values": ["low", "low"]},
        {"type": "future-control"},
    ]
    entry = parse_models_dev_catalog(fixture).entries[
        ModelCatalogEntryKey("openrouter", "z-ai/glm-5.2")
    ]
    assert isinstance(entry.reasoning, ReasoningSelectableControls)
    assert entry.reasoning.toggle is not None
    assert entry.reasoning.effort is None
    assert {item.code for item in entry.diagnostics} >= {
        "catalog_reasoning_effort_invalid",
        "catalog_reasoning_option_kind_unknown",
    }


def test_invalid_limits_reject_only_the_affected_row() -> None:
    fixture = catalog_fixture()
    fixture["zhipuai"]["models"]["glm-5.3"]["limit"] = {  # type: ignore[index]
        "context": 256_000,
        "input": 300_000,
        "output": 8_192,
    }
    snapshot = parse_models_dev_catalog(fixture)
    broken = snapshot.entries[ModelCatalogEntryKey("zhipuai", "glm-5.3")]
    healthy = snapshot.entries[
        ModelCatalogEntryKey("openrouter", "z-ai/glm-5.2")
    ]
    assert broken.total_context_tokens == 256_000
    assert broken.limits is None
    assert {item.code for item in broken.diagnostics} == {
        "catalog_limits_contradictory"
    }
    assert healthy.limits is not None


def test_catalog_snapshot_is_immutable() -> None:
    snapshot = parse_models_dev_catalog(catalog_fixture())
    with pytest.raises(TypeError):
        snapshot.entries[ModelCatalogEntryKey("zhipuai", "new")] = snapshot.entries[  # type: ignore[index]
            ModelCatalogEntryKey("zhipuai", "glm-5.3")
        ]
    with pytest.raises(FrozenInstanceError):
        snapshot.routes[0].display_name = "changed"  # type: ignore[misc]


def test_top_level_invalid_catalog_is_not_silently_replaced() -> None:
    with pytest.raises(ModelCatalogInvalid):
        parse_models_dev_catalog([])


def test_catalog_owner_retains_last_successful_snapshot_after_refresh_failure() -> None:
    calls = 0

    async def fetch() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return catalog_fixture()
        raise OSError("offline")

    async def exercise() -> None:
        owner = ModelCatalogOwner(ModelsDevCatalogClient(fetch_override=fetch))
        first = await owner.refresh()
        with pytest.raises(Exception, match="unavailable"):
            await owner.refresh()
        assert owner.snapshot is first

    asyncio.run(exercise())


def test_catalog_client_distinguishes_invalid_fetch_payload_from_unavailability() -> None:
    async def invalid_json() -> object:
        raise ValueError("invalid JSON")

    async def exercise() -> None:
        client = ModelsDevCatalogClient(fetch_override=invalid_json)
        with pytest.raises(ModelCatalogInvalid, match="invalid"):
            await client.fetch()

    asyncio.run(exercise())


def test_input_modalities_preserve_known_unknown_and_missing_facts() -> None:
    fixture = catalog_fixture()
    models = fixture["zhipuai"]["models"]  # type: ignore[index]
    models["glm-5.3"]["modalities"] = {  # type: ignore[index]
        "input": ["text", "image", "future-modality", "image"]
    }
    models["glm-small"].pop("modalities")  # type: ignore[index]
    snapshot = parse_models_dev_catalog(fixture)
    known = snapshot.entries[ModelCatalogEntryKey("zhipuai", "glm-5.3")]
    missing = snapshot.entries[ModelCatalogEntryKey("zhipuai", "glm-small")]

    assert known.input_modalities == (
        "text",
        "image",
        "future-modality",
        "image",
    )
    assert "catalog_input_modalities_invalid" not in {
        item.code for item in known.diagnostics
    }
    assert missing.input_modalities is None
    assert "catalog_input_modalities_missing" in {
        item.code for item in missing.diagnostics
    }


@pytest.mark.parametrize(
    "modalities",
    (
        ["text", "image"],
        {"input": "text"},
        {"input": ["text", 7]},
        {"input": ["text", " image"]},
    ),
)
def test_invalid_input_modality_shape_remains_unknown_with_local_diagnostic(
    modalities: object,
) -> None:
    fixture = catalog_fixture()
    model = fixture["zhipuai"]["models"]["glm-5.3"]  # type: ignore[index]
    model["modalities"] = modalities  # type: ignore[index]
    entry = parse_models_dev_catalog(fixture).entries[
        ModelCatalogEntryKey("zhipuai", "glm-5.3")
    ]

    assert entry.input_modalities is None
    assert "catalog_input_modalities_invalid" in {
        item.code for item in entry.diagnostics
    }


def test_attachment_does_not_grant_or_remove_image_input_authority() -> None:
    fixture = catalog_fixture()
    models = fixture["zhipuai"]["models"]  # type: ignore[index]
    models["glm-5.3"]["attachment"] = False  # type: ignore[index]
    models["glm-5.3"]["modalities"] = {"input": ["text", "image"]}  # type: ignore[index]
    models["glm-small"]["attachment"] = True  # type: ignore[index]
    models["glm-small"]["modalities"] = {"input": ["text"]}  # type: ignore[index]
    snapshot = parse_models_dev_catalog(fixture)

    assert snapshot.entries[
        ModelCatalogEntryKey("zhipuai", "glm-5.3")
    ].input_modalities == ("text", "image")
    assert snapshot.entries[
        ModelCatalogEntryKey("zhipuai", "glm-small")
    ].input_modalities == ("text",)


catalog_fixture.__test__ = False


@pytest.mark.parametrize("output", (["text", "image", "audio", "future-output"], None, "image", ["text", 7]))
def test_output_modalities_are_display_facts_independent_of_input(output: object) -> None:
    fixture = catalog_fixture()
    model = fixture["zhipuai"]["models"]["glm-5.3"]
    model["modalities"] = {"input": ["text"]}
    if output is not None:
        model["modalities"]["output"] = output
    entry = parse_models_dev_catalog(fixture).entries[
        ModelCatalogEntryKey("zhipuai", "glm-5.3")
    ]
    assert entry.input_modalities == ("text",)
    assert entry.output_modalities == (
        ("text", "image", "audio", "future-output")
        if output == ["text", "image", "audio", "future-output"] else None
    )


def test_concurrent_refresh_joins_one_fetch_despite_cancelled_waiter() -> None:
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        snapshot = parse_models_dev_catalog(catalog_fixture())

        class Client:
            async def fetch(self):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return snapshot

        owner = ModelCatalogOwner(Client())
        first = asyncio.create_task(owner.refresh())
        await entered.wait()
        second = asyncio.create_task(owner.refresh())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert calls == 1
        release.set()
        assert await second is snapshot
        assert owner.snapshot is snapshot
        await owner.aclose()

    asyncio.run(exercise())


def test_catalog_diagnostics_classify_without_disclosing_exception_text() -> None:
    import httpx
    import socket
    import ssl
    from pulsara_agent.llm.model_catalog import catalog_error_diagnostic

    secret = "proxy-password-must-not-appear"
    cases = [
        (httpx.ConnectTimeout(secret), "connect_timeout"),
        (httpx.ReadTimeout(secret), "timeout"),
        (httpx.ProxyError(secret), "proxy"),
        (httpx.ConnectError(secret), "connection"),
        (socket.gaierror(-2, secret), "dns"),
        (ssl.SSLError(secret), "tls"),
        (ValueError(secret), "invalid"),
        (RuntimeError(secret), "unknown"),
    ]
    for error, expected in cases:
        diagnostic = catalog_error_diagnostic(error)
        assert diagnostic["kind"] == expected
        assert secret not in str(diagnostic)
    response = httpx.Response(503, request=httpx.Request("GET", "https://models.dev/api.json"))
    diagnostic = catalog_error_diagnostic(httpx.HTTPStatusError(secret, request=response.request, response=response))
    assert diagnostic["kind"] == "http_status"
    assert diagnostic["status_code"] == 503


def test_refresh_waits_for_cancelled_fetch_cleanup_before_new_fetch() -> None:
    async def exercise():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        snapshot = parse_models_dev_catalog(catalog_fixture())

        class Client:
            calls = 0

            async def fetch(self):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaning.set()
                        await release.wait()
                return snapshot

        client = Client()
        owner = ModelCatalogOwner(client)
        first = asyncio.create_task(owner.refresh())
        await entered.wait()
        first.cancel()
        await cleaning.wait()
        second = asyncio.create_task(owner.refresh())
        await asyncio.sleep(0)
        assert client.calls == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second is snapshot
        assert client.calls == 2
        await owner.aclose()

    asyncio.run(exercise())
