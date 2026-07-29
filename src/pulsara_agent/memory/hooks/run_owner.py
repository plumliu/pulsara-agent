"""Process-local memory state owned independently from run execution state."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock


@dataclass(slots=True)
class MemoryProjectionLedgerOwner:
    generation: int = 0
    surfaced_ids: set[str] = field(default_factory=set)
    surfaced_fingerprints: set[str] = field(default_factory=set)


@dataclass(slots=True)
class DurableMemoryRecallProjectionCacheOwner:
    generation: int = 0
    query_text: str | None = None
    projection: dict[str, object] | None = None


@dataclass(slots=True)
class MemoryHookRunOwner:
    runtime_session_id: str
    run_id: str
    generation: int = 1
    active: bool = True
    projection_ledger: MemoryProjectionLedgerOwner = field(
        default_factory=MemoryProjectionLedgerOwner
    )
    recall_projection_cache: DurableMemoryRecallProjectionCacheOwner = field(
        default_factory=DurableMemoryRecallProjectionCacheOwner
    )
    working_context_refresh_attempted_model_step_key: str | None = None

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("memory hook run owner is retired")


class MemoryHookRunOwnerRegistry:
    """Single process-local owner for per-run memory caches and ledgers."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._owners: dict[tuple[str, str], MemoryHookRunOwner] = {}

    def acquire(
        self, *, runtime_session_id: str, run_id: str
    ) -> MemoryHookRunOwner:
        key = (runtime_session_id, run_id)
        with self._lock:
            owner = self._owners.get(key)
            if owner is None:
                owner = MemoryHookRunOwner(
                    runtime_session_id=runtime_session_id,
                    run_id=run_id,
                )
                self._owners[key] = owner
            owner.require_active()
            return owner

    def retire(self, *, runtime_session_id: str, run_id: str) -> None:
        key = (runtime_session_id, run_id)
        with self._lock:
            owner = self._owners.pop(key, None)
            if owner is None:
                return
            owner.active = False
            owner.generation += 1
            owner.projection_ledger.surfaced_ids.clear()
            owner.projection_ledger.surfaced_fingerprints.clear()
            owner.recall_projection_cache.projection = None

    def resident_count(self) -> int:
        with self._lock:
            return len(self._owners)


__all__ = [
    "DurableMemoryRecallProjectionCacheOwner",
    "MemoryHookRunOwner",
    "MemoryHookRunOwnerRegistry",
    "MemoryProjectionLedgerOwner",
]
