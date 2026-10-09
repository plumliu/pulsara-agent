"""Transport-neutral rerank provider contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple, Protocol, runtime_checkable


RerankPurpose = Literal["recall", "related_memory"]


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
        candidate_ids: Sequence[str] | None = None,
        purpose: RerankPurpose = "recall",
    ) -> list[RerankResult]: ...

    async def aclose(self) -> None: ...


__all__ = ["RerankProvider", "RerankResult", "RerankPurpose"]
