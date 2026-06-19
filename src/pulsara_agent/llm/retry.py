"""Provider-neutral retry policy helpers for LLM transports."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMRetryConfig:
    """Retry configuration for a single provider request."""

    enabled: bool = True
    attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        attempts = max(int(self.attempts), 1)
        jitter_ratio = min(max(float(self.jitter_ratio), 0.0), 1.0)
        base_delay_seconds = float(self.base_delay_seconds)
        max_delay_seconds = float(self.max_delay_seconds)
        max_retry_after_seconds = float(self.max_retry_after_seconds)
        if base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds must be > 0")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if max_retry_after_seconds <= 0:
            raise ValueError("max_retry_after_seconds must be > 0")
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "jitter_ratio", jitter_ratio)
        object.__setattr__(self, "base_delay_seconds", base_delay_seconds)
        object.__setattr__(self, "max_delay_seconds", max_delay_seconds)
        object.__setattr__(self, "max_retry_after_seconds", max_retry_after_seconds)


class RetryDecisionKind(StrEnum):
    RETRY = "retry"
    DO_NOT_RETRY = "do_not_retry"


@dataclass(frozen=True, slots=True)
class LLMRetryDecision:
    kind: RetryDecisionKind
    reason: str
    status_code: int | None = None
    retry_after_seconds: float | None = None
    retry_after_exceeded: bool = False
    provider_code: str | None = None


@dataclass(frozen=True, slots=True)
class RetryAttemptTrace:
    attempt: int
    max_attempts: int
    error_type: str
    error_message: str
    reason: str
    delay_seconds: float | None
    status_code: int | None = None
    retry_after_seconds: float | None = None
    retry_after_exceeded: bool = False
    provider_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "reason": self.reason,
            "delay_seconds": self.delay_seconds,
            "status_code": self.status_code,
            "retry_after_seconds": self.retry_after_seconds,
            "retry_after_exceeded": self.retry_after_exceeded,
            "provider_code": self.provider_code,
        }


def compute_retry_delay(
    *,
    attempt_index: int,
    config: LLMRetryConfig,
    retry_after_seconds: float | None,
    rng: random.Random | None = None,
) -> float:
    """Return the sleep before the next attempt.

    ``attempt_index`` is one-based and names the failed attempt.
    """

    rng = rng or random
    if retry_after_seconds is not None:
        if retry_after_seconds > config.max_retry_after_seconds:
            raise ValueError("retry_after_seconds exceeds max_retry_after_seconds")
        jitter = retry_after_seconds * config.jitter_ratio * rng.random()
        return retry_after_seconds + jitter

    delay = config.base_delay_seconds * (2 ** max(attempt_index - 1, 0))
    delay = min(delay, config.max_delay_seconds)
    if config.jitter_ratio <= 0:
        return delay
    spread = delay * config.jitter_ratio
    return max(0.0, delay + rng.uniform(-spread, spread))


def apply_retry_after_cap(
    decision: LLMRetryDecision,
    *,
    config: LLMRetryConfig,
) -> LLMRetryDecision:
    if (
        decision.kind is RetryDecisionKind.RETRY
        and decision.retry_after_seconds is not None
        and decision.retry_after_seconds > config.max_retry_after_seconds
    ):
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY,
            reason="retry_after_exceeded",
            status_code=decision.status_code,
            retry_after_seconds=decision.retry_after_seconds,
            retry_after_exceeded=True,
            provider_code=decision.provider_code,
        )
    return decision


def retry_config_from_env(prefix: str = "PULSARA") -> LLMRetryConfig:
    import os

    return LLMRetryConfig(
        enabled=_bool_env(os.getenv(f"{prefix}_LLM_RETRY_ENABLED"), default=True),
        attempts=_int_env(os.getenv(f"{prefix}_LLM_RETRY_ATTEMPTS"), default=3),
        base_delay_seconds=_float_env(
            os.getenv(f"{prefix}_LLM_RETRY_BASE_DELAY_SECONDS"),
            default=0.5,
        ),
        max_delay_seconds=_float_env(
            os.getenv(f"{prefix}_LLM_RETRY_MAX_DELAY_SECONDS"),
            default=8.0,
        ),
        jitter_ratio=_float_env(os.getenv(f"{prefix}_LLM_RETRY_JITTER"), default=0.2),
        max_retry_after_seconds=_float_env(
            os.getenv(f"{prefix}_LLM_RETRY_MAX_RETRY_AFTER_SECONDS"),
            default=30.0,
        ),
    )


def _bool_env(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"Invalid boolean value: {raw}")


def _int_env(raw: str | None, *, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _float_env(raw: str | None, *, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())
