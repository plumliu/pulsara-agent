"""Errors owned by the retained PostgreSQL embedding boundary."""

from __future__ import annotations


class EmbeddingServiceError(RuntimeError):
    """The configured embedding provider failed or returned invalid output."""
