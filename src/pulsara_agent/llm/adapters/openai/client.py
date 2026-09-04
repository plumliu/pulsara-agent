"""Shared OpenAI SDK client helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from typing import Any

import httpx
from openai import AsyncOpenAI, Omit

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundAsyncClient,
    admit_process_credential_http_operation,
)


OPENAI_RESPONSES_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"


@dataclass(frozen=True, slots=True)
class OpenAITransportTimeoutPolicy:
    """Closed wire-timeout shape for one OpenAI-compatible transport."""

    connect_seconds: float
    write_seconds: float
    pool_seconds: float
    read_idle_seconds: float
    total_seconds: float | None

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.connect_seconds,
                self.write_seconds,
                self.pool_seconds,
                self.read_idle_seconds,
            )
        ):
            raise ValueError("OpenAI transport timeout fields must be positive")
        if self.total_seconds is not None and self.total_seconds <= 0:
            raise ValueError(
                "OpenAI transport total timeout must be absent or positive"
            )

    @property
    def policy_fingerprint(self) -> str:
        return context_fingerprint(
            "openai-transport-timeout-policy:v1",
            {
                "connect_seconds": self.connect_seconds,
                "write_seconds": self.write_seconds,
                "pool_seconds": self.pool_seconds,
                "read_idle_seconds": self.read_idle_seconds,
                "total_seconds": self.total_seconds,
            },
        )

    def bounded_by(self, remaining_seconds: float) -> "OpenAITransportTimeoutPolicy":
        if remaining_seconds <= 0:
            raise TimeoutError("provider attempt deadline already expired")
        return OpenAITransportTimeoutPolicy(
            connect_seconds=min(self.connect_seconds, remaining_seconds),
            write_seconds=min(self.write_seconds, remaining_seconds),
            pool_seconds=min(self.pool_seconds, remaining_seconds),
            read_idle_seconds=min(self.read_idle_seconds, remaining_seconds),
            total_seconds=remaining_seconds,
        )


def build_async_openai_client(
    *,
    api_key: str | None,
    base_url: str,
    timeout_policy: OpenAITransportTimeoutPolicy,
    credential_boundary: ProcessCredentialBoundary,
    max_retries: int | None = None,
) -> AsyncOpenAI:
    """Create an AsyncOpenAI client for a model profile."""

    timeout = httpx.Timeout(
        timeout_policy.total_seconds,
        connect=timeout_policy.connect_seconds,
        write=timeout_policy.write_seconds,
        pool=timeout_policy.pool_seconds,
        read=timeout_policy.read_idle_seconds,
    )
    kwargs: dict[str, Any] = {
        # An explicit empty string prevents the SDK from consulting
        # OPENAI_API_KEY for a connection whose contract says no auth.
        "api_key": api_key if api_key is not None else "",
        "_enforce_credentials": api_key is not None,
        "base_url": base_url.rstrip("/"),
        "timeout": timeout,
        "http_client": ProcessCredentialBoundAsyncClient(
            credential_boundary=credential_boundary,
            credential_header_names=frozenset({b"authorization"}),
            timeout=timeout,
            follow_redirects=True,
        ),
    }
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return AsyncOpenAI(
        **kwargs,
    )


def openai_auth_request_options(*, requires_api_key: bool) -> dict[str, Any]:
    """Return the per-request SDK option that explicitly omits bearer auth."""

    if requires_api_key:
        return {}
    # The SDK validates omissions against request-local headers, not client
    # defaults. This also prevents an ambient OPENAI_API_KEY from leaking onto
    # a connection whose declared authentication contract is NONE.
    return {"extra_headers": {"Authorization": Omit()}}


async def admit_provider_request(
    *,
    credential_boundary: ProcessCredentialBoundary,
    payload: object,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Validate and enqueue one frozen provider request under the shared gate."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return await admit_process_credential_http_operation(
        credential_boundary=credential_boundary,
        guarded_values=(encoded,),
        operation=operation,
    )


__all__ = [
    "OPENAI_CHAT_COMPLETIONS_API",
    "OPENAI_RESPONSES_API",
    "OpenAITransportTimeoutPolicy",
    "admit_provider_request",
    "build_async_openai_client",
    "openai_auth_request_options",
]
