import random

import pytest

from pulsara_agent.llm.adapters.openai.errors import classify_llm_error, parse_retry_after_seconds
from pulsara_agent.llm.adapters.openai import client as openai_client
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.llm.retry import (
    LLMRetryConfig,
    RetryDecisionKind,
    apply_retry_after_cap,
    compute_retry_delay,
    retry_config_from_env,
)


class FakeResponse:
    def __init__(self, *, status_code: int | None = None, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        headers=None,
        body=None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response = FakeResponse(status_code=status_code, headers=headers, body=body)
        self.body = body
        self.code = code


def test_retry_config_from_env_and_validation(monkeypatch) -> None:
    monkeypatch.setenv("PULSARA_LLM_RETRY_ENABLED", "false")
    monkeypatch.setenv("PULSARA_LLM_RETRY_ATTEMPTS", "0")
    monkeypatch.setenv("PULSARA_LLM_RETRY_BASE_DELAY_SECONDS", "0.25")
    monkeypatch.setenv("PULSARA_LLM_RETRY_MAX_DELAY_SECONDS", "2")
    monkeypatch.setenv("PULSARA_LLM_RETRY_JITTER", "1.5")
    monkeypatch.setenv("PULSARA_LLM_RETRY_MAX_RETRY_AFTER_SECONDS", "15")

    config = retry_config_from_env()

    assert config.enabled is False
    assert config.attempts == 1
    assert config.base_delay_seconds == 0.25
    assert config.max_delay_seconds == 2.0
    assert config.jitter_ratio == 1.0
    assert config.max_retry_after_seconds == 15.0


def test_llm_config_reads_retry_and_sdk_retry_env(monkeypatch) -> None:
    monkeypatch.setenv("PULSARA_API_KEY", "sk-test")
    monkeypatch.setenv("PULSARA_PRO_MODEL", "pro")
    monkeypatch.setenv("PULSARA_FLASH_MODEL", "flash")
    monkeypatch.setenv("PULSARA_LLM_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("PULSARA_OPENAI_SDK_MAX_RETRIES", "0")

    config = LLMConfig.from_env()

    assert config.retry.attempts == 4
    assert config.openai_sdk_max_retries == 0


def test_openai_client_max_retries_plumbing(monkeypatch) -> None:
    calls = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(openai_client, "AsyncOpenAI", FakeAsyncOpenAI)

    openai_client.build_async_openai_client(
        api_key="sk-test",
        base_url="https://example.test/v1/",
        timeout_seconds=7,
    )
    openai_client.build_async_openai_client(
        api_key="sk-test",
        base_url="https://example.test/v1/",
        timeout_seconds=7,
        max_retries=0,
    )

    assert "max_retries" not in calls[0]
    assert calls[0]["base_url"] == "https://example.test/v1"
    assert calls[1]["max_retries"] == 0


def test_retry_config_rejects_invalid_delay_values() -> None:
    with pytest.raises(ValueError, match="base_delay_seconds"):
        LLMRetryConfig(base_delay_seconds=0)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        LLMRetryConfig(max_delay_seconds=0)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        LLMRetryConfig(base_delay_seconds=2, max_delay_seconds=1)
    with pytest.raises(ValueError, match="max_retry_after_seconds"):
        LLMRetryConfig(max_retry_after_seconds=0)


def test_retry_classifier_treats_connection_error_cause_as_retryable() -> None:
    try:
        try:
            raise OSError("socket reset by peer")
        except OSError as exc:
            raise RuntimeError("Connection error.") from exc
    except RuntimeError as exc:
        decision = classify_llm_error(exc)

    assert decision.kind is RetryDecisionKind.RETRY
    assert decision.reason == "transport_error"


def test_retry_classifier_status_codes_and_bad_request_signals() -> None:
    retryable = classify_llm_error(FakeProviderError("rate limited", status_code=429))
    assert retryable.kind is RetryDecisionKind.RETRY
    assert retryable.reason == "http_429_retryable"

    deterministic_5xx = classify_llm_error(
        FakeProviderError(
            "gateway error",
            status_code=502,
            body={"error": {"code": "unsupported_parameter", "message": "unknown parameter foo"}},
        )
    )
    assert deterministic_5xx.kind is RetryDecisionKind.DO_NOT_RETRY
    assert deterministic_5xx.reason == "deterministic_bad_request"

    payload_too_large = classify_llm_error(FakeProviderError("too large", status_code=413))
    assert payload_too_large.kind is RetryDecisionKind.DO_NOT_RETRY

    overloaded = classify_llm_error(FakeProviderError("overloaded", status_code=529))
    assert overloaded.kind is RetryDecisionKind.RETRY


def test_retry_after_parses_headers_body_and_message() -> None:
    assert parse_retry_after_seconds(headers={"retry-after-ms": "500"}) == 0.5
    assert parse_retry_after_seconds(headers={"Retry-After": "2.5"}) == 2.5
    assert parse_retry_after_seconds(body={"error": {"retry_after": 3}}) == 3.0
    assert parse_retry_after_seconds(message="please try again in 750ms") == 0.75


def test_retry_after_cap_and_backoff_jitter() -> None:
    config = LLMRetryConfig(
        attempts=3,
        base_delay_seconds=1,
        max_delay_seconds=4,
        jitter_ratio=0.5,
        max_retry_after_seconds=5,
    )
    retry_after_decision = classify_llm_error(
        FakeProviderError("rate limited", status_code=429, headers={"retry-after": "10"})
    )
    capped = apply_retry_after_cap(retry_after_decision, config=config)
    assert capped.kind is RetryDecisionKind.DO_NOT_RETRY
    assert capped.reason == "retry_after_exceeded"
    assert capped.retry_after_exceeded is True

    delay = compute_retry_delay(
        attempt_index=2,
        config=config,
        retry_after_seconds=None,
        rng=random.Random(0),
    )
    assert 1.0 <= delay <= 3.0

    retry_after_delay = compute_retry_delay(
        attempt_index=1,
        config=config,
        retry_after_seconds=2,
        rng=random.Random(0),
    )
    assert 2.0 <= retry_after_delay <= 3.0
