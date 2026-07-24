"""Deterministic bounded retry timing for session-owned durable writers."""

from __future__ import annotations

from time import monotonic


def bounded_none_retry_delay_seconds(
    attempt_generation: int,
    *,
    deadline_monotonic: float,
    now_monotonic: float | None = None,
) -> float:
    """Return a small exponential delay without extending the frozen deadline."""

    if attempt_generation < 1:
        raise ValueError("retry attempt generation must be positive")
    now = monotonic() if now_monotonic is None else now_monotonic
    remaining = max(0.0, deadline_monotonic - now)
    delay = min(0.01 * (2 ** min(attempt_generation - 1, 5)), 0.25)
    return min(delay, remaining)


__all__ = ["bounded_none_retry_delay_seconds"]
