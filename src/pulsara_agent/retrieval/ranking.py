"""Shared rank scoring for two independent retrieval lists."""

_RRF_K = 60


def reciprocal_rank_score(first: int | None, second: int | None) -> float:
    return sum((1 / (_RRF_K + rank) for rank in (first, second) if rank is not None), 0.0)
