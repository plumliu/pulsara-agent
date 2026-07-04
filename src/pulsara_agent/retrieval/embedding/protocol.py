"""Embedding provider protocol."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async embedding provider contract."""

    model_id: str
    dimensions: int

    async def embed(self, text: str) -> list[float]:
        """Embed a single string into a fixed-length vector."""

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of strings preserving input order."""

    async def aclose(self) -> None:
        """Close provider-owned async resources."""
