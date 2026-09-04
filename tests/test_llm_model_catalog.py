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
                    "limit": {"context": 1_000_000, "output": 131_072},
                },
                "glm-small": {
                    "id": "glm-small",
                    "name": "Small",
                    "reasoning": False,
                    "tool_call": True,
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
                    "limit": {"context": 256_000, "output": 8_192},
                },
                "google/gemini-pro": {
                    "reasoning": True,
                    "limit": {"context": 256_000, "output": 8_192},
                },
                "~~claude-kept": {
                    "reasoning": True,
                    "limit": {"context": 256_000, "output": 8_192},
                },
                "broken-options": {
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "future-control"},
                        {"type": "effort", "values": ["low", "low"]},
                    ],
                    "tool_call": "yes",
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
def test_selectable_catalog_filters_exact_model_leafs(model_id: str) -> None:
    fixture = catalog_fixture()
    models = fixture["zhipuai"]["models"]  # type: ignore[index]
    models[model_id] = {  # type: ignore[index]
        "reasoning": False,
        "limit": {"context": 256_000, "output": 8_192},
    }
    selected = selectable_catalog(parse_models_dev_catalog(fixture))
    assert ModelCatalogEntryKey("zhipuai", model_id) not in selected.entries


def test_selectable_catalog_uses_context_boundary_and_no_other_heuristic() -> None:
    selected = selectable_catalog(parse_models_dev_catalog(catalog_fixture()))

    assert ModelCatalogEntryKey("zhipuai", "glm-small") not in selected.entries
    assert ModelCatalogEntryKey("zhipuai", "glm-5.3") in selected.entries
    assert ModelCatalogEntryKey("openrouter", "~~claude-kept") in selected.entries
    assert ModelCatalogEntryKey("filtered", "ordinary-model") in selected.entries
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


catalog_fixture.__test__ = False
