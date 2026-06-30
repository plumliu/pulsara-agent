"""Rerank provider protocol."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple, Protocol, runtime_checkable


class RerankResult(NamedTuple):
    index: int
    score: float


@runtime_checkable
class RerankProvider(Protocol):
    """Async rerank provider contract."""

    model_id: str

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        instruction: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        """Score and reorder documents against a query."""

    async def aclose(self) -> None:
        """Close provider-owned async resources."""
