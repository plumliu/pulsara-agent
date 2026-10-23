"""Explicit dogfood fault injection at the SDK payload seam, never saved config.

Run only while the dogfood's normal calls are quiescent: the scoped patches affect
one adapter module. The supplied probe keeps its real resolved target and ordinary
credential owner; only the actual outgoing model string is deliberately invalid.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import importlib
from copy import deepcopy
from unittest.mock import patch

from pulsara_agent.llm.connection_probe import ModelConnectionProbeFailure


async def run_unknown_model_failure_probe(
    *,
    wire_api: str,
    probe: Callable[[], Awaitable[None]],
) -> dict[str, object]:
    if wire_api == "openai_chat_completions":
        module_name, builder_name = "chat_completions", "build_chat_completions_payload"
    elif wire_api == "openai_responses":
        module_name, builder_name = "responses", "build_responses_payload"
    else:
        raise ValueError("dogfood failure probe requires a supported wire API")
    module = importlib.import_module(f"pulsara_agent.llm.adapters.openai.{module_name}")
    build_payload = getattr(module, builder_name)
    build_client = module.build_async_openai_client
    injected_model = "pulsara-dogfood-deliberately-nonexistent-model"
    status_codes: list[int] = []
    payload_builds = 0
    injected_payload: dict[str, object] | None = None

    def fault_payload(*, call, context):
        nonlocal payload_builds, injected_payload
        payload = build_payload(call=call, context=context)
        payload_builds += 1
        value = {**payload, "model": injected_model}
        if isinstance(value.get("extra_body"), dict) and "model" in value["extra_body"]:
            value["extra_body"] = {**value["extra_body"], "model": injected_model}
        injected_payload = deepcopy(value)
        return value

    async def observe_response(response):
        status_codes.append(response.status_code)

    def observed_client(**kwargs):
        kwargs["response_hooks"] = [
            *(kwargs.get("response_hooks") or ()),
            observe_response,
        ]
        return build_client(**kwargs)

    with (
        patch.object(module, builder_name, fault_payload),
        patch.object(module, "build_async_openai_client", observed_client),
    ):
        try:
            await probe()
        except ModelConnectionProbeFailure as error:
            # SDK response observer counts actual HTTP responses, including any
            # accidental retry, independently of the single prepared payload.
            if payload_builds != 1 or len(status_codes) != 1:
                raise AssertionError(
                    f"failure probe expected one payload/HTTP response: {payload_builds=}, {status_codes=}"
                ) from error
            if not 400 <= status_codes[0] < 500:
                raise AssertionError(
                    f"unknown-model probe returned unexpected status: {status_codes}"
                ) from error
            return {
                "wire_api": wire_api,
                "fault": "outgoing_model_override_only",
                "injected_model": injected_model,
                "injected_payload": injected_payload,
                "payload_builds": payload_builds,
                "http_statuses": status_codes,
                "provider_error_code": error.code,
                "provider_error": error.message,
            }
    raise AssertionError(
        "provider unexpectedly accepted the deliberately nonexistent model"
    )
