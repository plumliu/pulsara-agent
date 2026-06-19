"""Shared retry helpers for OpenAI-compatible transports."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from pulsara_agent.event import CustomEvent, EventContext
from pulsara_agent.llm.models import ModelProfile
from pulsara_agent.llm.retry import LLMRetryConfig, LLMRetryDecision, RetryAttemptTrace


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


def retry_event(
    *,
    api: str,
    model: ModelProfile,
    event_context: EventContext,
    trace: RetryAttemptTrace,
    has_semantic_output: bool,
) -> CustomEvent:
    return CustomEvent(
        **event_context.event_fields(),
        name="llm.retry",
        value={
            "api": api,
            "provider": model.provider,
            "model": model.id,
            "base_url_host": _safe_host(model.base_url),
            "attempt": trace.attempt,
            "max_attempts": trace.max_attempts,
            "reason": trace.reason,
            "delay_seconds": trace.delay_seconds,
            "status_code": trace.status_code,
            "retry_after_seconds": trace.retry_after_seconds,
            "retry_after_exceeded": trace.retry_after_exceeded,
            "provider_code": trace.provider_code,
            "error_type": trace.error_type,
            "has_semantic_output": has_semantic_output,
        },
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


def provider_data_with_retry(
    provider_data: dict[str, Any],
    *,
    config: LLMRetryConfig,
    traces: list[RetryAttemptTrace],
    final_decision: LLMRetryDecision | None,
    final_attempt: int,
    has_semantic_output: bool,
    exhausted: bool,
    skipped_reason: str | None,
) -> dict[str, Any]:
    data = dict(provider_data)
    data["retry"] = {
        "enabled": config.enabled,
        "attempts": final_attempt,
        "max_attempts": config.attempts if config.enabled else 1,
        "exhausted": exhausted,
        "has_semantic_output": has_semantic_output,
        "skipped_reason": skipped_reason,
        "final_reason": final_decision.reason if final_decision is not None else None,
        "final_status_code": final_decision.status_code if final_decision is not None else None,
        "retry_after_exceeded": (
            final_decision.retry_after_exceeded if final_decision is not None else False
        ),
        "traces": [trace.to_dict() for trace in traces],
    }
    return data


def _safe_host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    return parsed.netloc or parsed.path or None
