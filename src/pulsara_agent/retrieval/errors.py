"""Shared retrieval-service errors."""

from __future__ import annotations


class RetrievalServiceError(RuntimeError):
    """Base error for retrieval-side model services."""


class EmbeddingServiceError(RetrievalServiceError):
    """Embedding provider failure."""


class RerankServiceError(RetrievalServiceError):
    """Rerank provider failure."""
