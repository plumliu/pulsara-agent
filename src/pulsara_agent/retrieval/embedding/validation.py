"""Physical validation shared by embedding callers and the provider."""

from __future__ import annotations

from collections.abc import Sequence
import math

from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.retrieval.errors import EmbeddingServiceError


MEMORY_EMBEDDING_DIMENSIONS = 1024


def validate_embedding_inputs(texts: Sequence[str]) -> None:
    if not 1 <= len(texts) <= 10:
        raise EmbeddingServiceError("embedding batch is outside 1..10 items")
    estimator = PulsaraHeuristicTokenEstimatorV3()
    estimates = [estimator.estimate_text(value) for value in texts]
    if any(value < 1 or value > 8192 for value in estimates) or sum(estimates) > 81920:
        raise EmbeddingServiceError("embedding input exceeds the local token boundary")


def freeze_v1_embedding_vector(values: Sequence[float]) -> tuple[float, ...]:
    """Freeze one finite, non-zero float64 vector or fail before SQL/use."""

    if len(values) != MEMORY_EMBEDDING_DIMENSIONS:
        raise ValueError("memory embedding dimension is invalid")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise ValueError("memory embedding contains a non-number")
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


__all__ = [
    "MEMORY_EMBEDDING_DIMENSIONS",
    "freeze_v1_embedding_vector",
    "validate_embedding_inputs",
]
