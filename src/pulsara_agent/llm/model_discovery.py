"""User-level, memory-only model ID discovery through the OpenAI SDK."""

from __future__ import annotations

import asyncio

from pulsara_agent.llm.adapters.openai.client import (
    OpenAITransportTimeoutPolicy,
    bounded_json_response_hook,
    build_async_openai_client,
    openai_auth_request_options,
    prepare_openai_sdk,
)
from pulsara_agent.llm.adapters.openai.retrying import (
    expand_provider_diagnostic_scrubber, provider_diagnostic_await,
)
from pulsara_agent.llm.model_target import canonicalize_endpoint
from pulsara_agent.llm.stream_limits import MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

# One interactive discovery operation, matching connection_probe's network budget.
# See archived_docs/PULSARA_CUSTOM_MODEL_DISCOVERY_IMPLEMENTATION_SPEC.zh.md.
_DISCOVERY_TIMEOUT = OpenAITransportTimeoutPolicy(10, 10, 10, 30, 45)


class ModelDiscoveryFailure(RuntimeError):
    pass


async def discover_model_ids(*, base_url: str, api_key: str | None) -> list[str]:
    endpoint = canonicalize_endpoint(base_url)
    await prepare_openai_sdk()
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    boundary = ProcessCredentialBoundary(api_key or "")
    scrubber = expand_provider_diagnostic_scrubber(boundary.capture_scrub_set(), endpoint)
    try:
        async with build_async_openai_client(
            api_key=api_key,
            base_url=endpoint,
            timeout_policy=_DISCOVERY_TIMEOUT,
            credential_boundary=boundary,
            max_retries=0,
            response_hooks=[bounded_json_response_hook(MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES)],
        ) as client:
            async with asyncio.timeout(_DISCOVERY_TIMEOUT.total_seconds):
                page = await provider_diagnostic_await(client.models.list(
                    **openai_auth_request_options(requires_api_key=bool(api_key))
                ), scrubber)
            rows = getattr(page, "data", None)
            if not isinstance(rows, list):
                raise ValueError("模型列表响应缺少 data 数组。")
            ids: dict[str, None] = {}
            for row in rows:
                model_id = getattr(row, "id", None)
                if not isinstance(model_id, str) or not model_id.strip() or model_id != model_id.strip():
                    raise ValueError("模型列表包含无效的 Model ID。")
                if scrubber.contains(model_id):
                    raise ValueError("模型列表包含凭据内容，无法采用。")
                ids[model_id] = None
            return list(ids)
    except APIStatusError as exc:
        hint = {
            401: "服务要求有效的 API key，请填写或检查密钥。",
            403: "服务拒绝访问模型列表，请检查密钥或权限。",
            404: "该地址未提供模型列表，请检查 Base URL 或手动填写。",
            405: "该服务不支持获取模型列表，请手动填写。",
        }.get(exc.status_code, "服务未能返回模型列表。")
        raise ModelDiscoveryFailure(
            scrubber.scrub_text(f"{hint} HTTP {exc.status_code}：{exc.response.text}")
        ) from None
    except (TimeoutError, APITimeoutError):
        raise ModelDiscoveryFailure("获取模型列表超时，请重试或手动填写。") from None
    except APIConnectionError as exc:
        # The SDK wraps response-hook failures as connection errors.
        detail = str(exc.__cause__ or exc)
        raise ModelDiscoveryFailure(scrubber.scrub_text(f"无法获取模型列表：{detail}")) from None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ModelDiscoveryFailure(scrubber.scrub_text(f"无法识别模型列表：{exc}")) from None
