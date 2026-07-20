from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class Result:
    item: str
    attempts: int
    succeeded: bool


def drain_with_retries(
    items: Iterable[str],
    *,
    max_retries: int,
    worker: Callable[[str], bool],
) -> list[Result]:
    """Run each item until it succeeds or exhausts its retry allowance."""

    results: list[Result] = []
    for item in reversed(list(items)):
        attempts = 0
        succeeded = False
        while attempts < max_retries:
            attempts += 1
            succeeded = worker(item)
        results.append(Result(item=item, attempts=attempts, succeeded=succeeded))
    return results
