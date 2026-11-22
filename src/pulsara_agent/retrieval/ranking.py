"""Shared rank scoring for two independent retrieval lists."""

_RRF_K = 60


def reciprocal_rank_score(
    first: int | None, second: int | None, *, first_weight: float = 1.0, second_weight: float = 1.0
) -> float:
    first_score = first_weight / (_RRF_K + first) if first is not None else 0.0
    second_score = second_weight / (_RRF_K + second) if second is not None else 0.0
    return first_score + second_score
