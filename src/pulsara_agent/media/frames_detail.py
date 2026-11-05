"""Short, explicit detail ranges (detail revision specification §1/§3)."""

from __future__ import annotations

from .source import MediaError, exact_seconds


# Product boundary: a local start/middle/end inspection, not a time scan.
DETAIL_MAX_SECONDS = 2


def validate_detail_range(start, end) -> None:
    if start is None or end is None:
        raise MediaError(
            "INVALID_RANGE",
            "detail requires explicit from and to with 0 < to - from <= 2 seconds. "
            "Choose a local range to inspect; use overview to locate content or motion to examine a sequence.",
        )
    a, b = exact_seconds(start), exact_seconds(end)
    if a < 0 or b <= a:
        raise MediaError("INVALID_RANGE", "detail requires 0 <= from < to.")
    if b - a > DETAIL_MAX_SECONDS:
        raise MediaError(
            "INVALID_RANGE",
            f"detail accepts at most 2 seconds; requested [{start}, {end}) spans {float(b - a):g} seconds. "
            "Choose a local range around the content to inspect. "
            "For a longer passage, use overview to locate content or motion to examine changes and order.",
        )
