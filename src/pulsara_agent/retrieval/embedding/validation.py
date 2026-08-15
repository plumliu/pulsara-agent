"""Single physical validator for the sealed Round 8 vector space."""

from __future__ import annotations

from collections.abc import Sequence
import math


MEMORY_EMBEDDING_DIMENSIONS = 1024


def freeze_v1_embedding_vector(values: Sequence[float]) -> tuple[float, ...]:
    """Freeze one finite, non-zero float64 vector or fail before SQL/use."""

    if len(values) != MEMORY_EMBEDDING_DIMENSIONS:
        raise ValueError("memory embedding dimension is invalid")
    try:
        frozen = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("memory embedding contains a non-number") from exc
    if any(not math.isfinite(value) for value in frozen):
        raise ValueError("memory embedding contains a non-finite component")
    try:
        squared_norm = math.fsum(value * value for value in frozen)
        norm = math.sqrt(squared_norm)
    except (OverflowError, ValueError) as exc:
        raise ValueError("memory embedding norm is invalid") from exc
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("memory embedding norm is not finite and positive")
    return frozen


__all__ = ["MEMORY_EMBEDDING_DIMENSIONS", "freeze_v1_embedding_vector"]
