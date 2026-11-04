"""Exact source coverage subtraction; neither text nor model settings imply coverage."""

from __future__ import annotations

from .asr_cache import AsrCacheBlock


def missing_ranges(
    blocks: tuple[AsrCacheBlock, ...], start: float, end: float
) -> tuple[tuple[float, float], ...]:
    at = start
    gaps = []
    for block in sorted(
        blocks, key=lambda block: (block.from_seconds, block.to_seconds, block.id)
    ):
        if block.to_seconds <= at or block.from_seconds >= end:
            continue
        if block.from_seconds > at:
            gaps.append((at, min(end, block.from_seconds)))
        at = max(at, min(end, block.to_seconds))
    if at < end:
        gaps.append((at, end))
    return tuple(gaps)
