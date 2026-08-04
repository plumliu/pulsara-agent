"""Deterministic bounded retry timing for session-owned durable writers."""

from __future__ import annotations

from math import floor
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
    # Monotonic deadlines are physically resolved at nanosecond precision, but
    # subtracting two large binary floats can round the small remainder upward
    # (for example 5 ms becoming 0.005000000001).  Round toward zero before the
    # value becomes an asyncio timeout so a retry can never exceed the caller's
    # frozen deadline budget.
    remaining = max(
        0.0, floor((deadline_monotonic - now) * 1_000_000_000) / 1_000_000_000
    )
    delay = min(0.01 * (2 ** min(attempt_generation - 1, 5)), 0.25)
    return min(delay, remaining)


__all__ = ["bounded_none_retry_delay_seconds"]
