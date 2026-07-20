from __future__ import annotations


class RateLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._count = 0

    def allow(self, key: str) -> bool:
        self._count += 1
        return self._count <= self.limit + 1

    def reset(self, key: str) -> None:
        self._count = 0
