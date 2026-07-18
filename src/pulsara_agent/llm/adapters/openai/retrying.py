"""Shared retry helpers for OpenAI-compatible transports."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.retry import LLMRetryConfig, LLMRetryDecision, RetryAttemptTrace
from pulsara_agent.primitives.model_call import (
    ProviderRetryAttemptSummaryFact,
    ProviderRetrySummaryFact,
    sha256_fingerprint,
)


LOGGER = logging.getLogger(__name__)


def sdk_max_retries_for_transport(
    *,
    retry_config: LLMRetryConfig,
    explicit_max_retries: int | None,
) -> int | None:
    """Return SDK max_retries for a transport that has Pulsara safe retry."""

    if explicit_max_retries is not None:
        return explicit_max_retries
    return 0 if retry_config.enabled else None


def make_retry_trace(
    *,
    exc: BaseException,
    decision: LLMRetryDecision,
    attempt: int,
    max_attempts: int,
    delay_seconds: float | None,
) -> RetryAttemptTrace:
    return RetryAttemptTrace(
        attempt=attempt,
        max_attempts=max_attempts,
        error_type=type(exc).__name__,
        error_message=str(exc),
        reason=decision.reason,
        delay_seconds=delay_seconds,
        status_code=decision.status_code,
        retry_after_seconds=decision.retry_after_seconds,
        retry_after_exceeded=decision.retry_after_exceeded,
        provider_code=decision.provider_code,
    )


def log_retry_attempt(
    *,
    api: str,
    model: ModelProfile,
    trace: RetryAttemptTrace,
    has_semantic_output: bool,
) -> None:
    LOGGER.warning(
        "Retrying LLM provider request api=%s provider=%s model=%s base_url_host=%s "
        "attempt=%s/%s reason=%s delay_seconds=%s status_code=%s provider_code=%s "
        "error_type=%s has_semantic_output=%s",
        api,
        model.provider,
        model.id,
        _safe_host(model.base_url),
        trace.attempt,
        trace.max_attempts,
        trace.reason,
        trace.delay_seconds,
        trace.status_code,
        trace.provider_code,
        trace.error_type,
        has_semantic_output,
    )


def build_provider_retry_summary(
    *,
    config: LLMRetryConfig,
    traces: list[RetryAttemptTrace],
    final_decision: LLMRetryDecision | None,
    final_attempt: int,
    has_semantic_output: bool,
    exhausted: bool,
    skipped_reason: str | None,
) -> ProviderRetrySummaryFact:
    if final_decision is None:
        raise ValueError("provider retry summary requires a final decision")
    attempts = []
    for trace in traces:
        payload = {
            "attempt": trace.attempt,
            "max_attempts": trace.max_attempts,
            "reason": trace.reason[:96],
            "status_code": trace.status_code,
            "delay_millis": _seconds_to_millis(trace.delay_seconds),
            "retry_after_millis": _seconds_to_millis(
                trace.retry_after_seconds
            ),
            "retry_after_exceeded": trace.retry_after_exceeded,
        }
        provisional = ProviderRetryAttemptSummaryFact.model_construct(
            **payload, attempt_fingerprint="pending"
        )
        canonical = provisional.model_dump(
            mode="json", exclude={"attempt_fingerprint"}
        )
        attempts.append(
            ProviderRetryAttemptSummaryFact(
                **canonical,
                attempt_fingerprint=sha256_fingerprint(
                    "provider-retry-attempt-summary:v1", canonical
                ),
            )
        )
    contract_fingerprint = sha256_fingerprint(
        "provider-retry-summary-contract:v1",
        {
            "max_attempts": 32,
            "fields": (
                "attempt",
                "reason",
                "status_code",
                "delay_millis",
                "retry_after_millis",
                "retry_after_exceeded",
            ),
            "excluded": (
                "exception_message",
                "exception_repr",
                "provider_data",
                "url",
                "secret",
            ),
        },
    )
    payload = {
        "enabled": config.enabled,
        "final_attempt": final_attempt,
        "max_attempts": config.attempts if config.enabled else 1,
        "retry_count": len(attempts),
        "exhausted": exhausted,
        "has_semantic_output": has_semantic_output,
        "skipped_reason": skipped_reason[:96] if skipped_reason else None,
        "final_reason": final_decision.reason[:96],
        "final_status_code": final_decision.status_code,
        "retry_after_exceeded": final_decision.retry_after_exceeded,
        "attempts": tuple(attempts),
        "summary_contract_fingerprint": contract_fingerprint,
    }
    provisional = ProviderRetrySummaryFact.model_construct(
        **payload, summary_fingerprint="pending"
    )
    canonical = provisional.model_dump(mode="json", exclude={"summary_fingerprint"})
    return ProviderRetrySummaryFact(
        **canonical,
        summary_fingerprint=sha256_fingerprint(
            "provider-retry-summary:v1", canonical
        ),
    )


def provider_failure_code_hint(decision: LLMRetryDecision) -> str:
    if decision.status_code == 401:
        return "provider_authentication_401"
    if decision.status_code == 403:
        return "provider_permission_403"
    if decision.status_code == 429:
        return "provider_rate_limit_429"
    if decision.status_code == 408 or "timeout" in decision.reason.casefold():
        return "provider_timeout"
    if decision.status_code is not None and decision.status_code >= 500:
        return "provider_overloaded"
    if decision.status_code is not None and 400 <= decision.status_code < 500:
        return "provider_invalid_request"
    return "provider_transport_error"


def _seconds_to_millis(value: float | None) -> int | None:
    if value is None:
        return None
    return min(600_000, max(0, round(value * 1_000)))


def _safe_host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    return parsed.netloc or parsed.path or None
