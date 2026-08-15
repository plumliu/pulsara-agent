"""Transport-neutral rerank provider contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple, Protocol, runtime_checkable


class RerankResult(NamedTuple):
    index: int
    score: float


@runtime_checkable
class RerankProvider(Protocol):
    model_id: str

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        instruction: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]: ...

    async def aclose(self) -> None: ...


__all__ = ["RerankProvider", "RerankResult"]
