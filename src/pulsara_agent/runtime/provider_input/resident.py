"""Process-owned bounded resident cache for provider-input materialization."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock

from pulsara_agent.primitives.context import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class ProviderInputResidentCacheKey:
    runtime_session_id: str
    owner_kind: str
    owner_id: str


@dataclass(frozen=True, slots=True)
class ProviderInputResidentCacheStats:
    resident_bytes: int
    resident_chunks: int
    resident_generations: int
    admission_count: int
    admission_rejected_count: int
    eviction_count: int
    hit_count: int
    miss_count: int


@dataclass(slots=True)
class _ResidentRecord:
    value: object
    resident_bytes: int
    resident_chunks: int


class ProviderInputResidentBudgetManager:
    """Bound process memory without becoming provider-input authority."""

    def __init__(
        self,
        *,
        max_resident_bytes: int = 128 * 1024 * 1024,
        max_resident_chunks: int = 8_192,
        max_resident_generations: int = 128,
    ) -> None:
        if (
            min(
                max_resident_bytes,
                max_resident_chunks,
                max_resident_generations,
            )
            <= 0
        ):
            raise ValueError("provider resident budget must be positive")
        self._max_resident_bytes = max_resident_bytes
        self._max_resident_chunks = max_resident_chunks
        self._max_resident_generations = max_resident_generations
        self._lock = RLock()
        self._records: OrderedDict[ProviderInputResidentCacheKey, _ResidentRecord] = (
            OrderedDict()
        )
        self._resident_bytes = 0
        self._resident_chunks = 0
        self._admission_count = 0
        self._admission_rejected_count = 0
        self._eviction_count = 0
        self._hit_count = 0
        self._miss_count = 0

    def admit(self, key: ProviderInputResidentCacheKey, value: object) -> bool:
        resident_bytes, resident_chunks = _measure_resident(value)
        with self._lock:
            self._admission_count += 1
            existing = self._records.pop(key, None)
            if existing is not None:
                self._resident_bytes -= existing.resident_bytes
                self._resident_chunks -= existing.resident_chunks
            if (
                resident_bytes > self._max_resident_bytes
                or resident_chunks > self._max_resident_chunks
            ):
                self._admission_rejected_count += 1
                return False
            while self._records and (
                self._resident_bytes + resident_bytes > self._max_resident_bytes
                or self._resident_chunks + resident_chunks > self._max_resident_chunks
                or len(self._records) + 1 > self._max_resident_generations
            ):
                _evicted_key, evicted = self._records.popitem(last=False)
                self._resident_bytes -= evicted.resident_bytes
                self._resident_chunks -= evicted.resident_chunks
                self._eviction_count += 1
            if (
                self._resident_bytes + resident_bytes > self._max_resident_bytes
                or self._resident_chunks + resident_chunks > self._max_resident_chunks
                or len(self._records) + 1 > self._max_resident_generations
            ):
                self._admission_rejected_count += 1
                return False
            self._records[key] = _ResidentRecord(
                value=value,
                resident_bytes=resident_bytes,
                resident_chunks=resident_chunks,
            )
            self._resident_bytes += resident_bytes
            self._resident_chunks += resident_chunks
            return True

    def get(self, key: ProviderInputResidentCacheKey):
        with self._lock:
            record = self._records.get(key)
            if record is None:
                self._miss_count += 1
                return None
            self._hit_count += 1
            self._records.move_to_end(key)
            return record.value

    def discard(self, key: ProviderInputResidentCacheKey) -> None:
        with self._lock:
            record = self._records.pop(key, None)
            if record is None:
                return
            self._resident_bytes -= record.resident_bytes
            self._resident_chunks -= record.resident_chunks

    def discard_runtime_session(self, runtime_session_id: str) -> None:
        with self._lock:
            keys = tuple(
                key
                for key in self._records
                if key.runtime_session_id == runtime_session_id
            )
            for key in keys:
                record = self._records.pop(key)
                self._resident_bytes -= record.resident_bytes
                self._resident_chunks -= record.resident_chunks

    def stats(self) -> ProviderInputResidentCacheStats:
        with self._lock:
            return ProviderInputResidentCacheStats(
                resident_bytes=self._resident_bytes,
                resident_chunks=self._resident_chunks,
                resident_generations=len(self._records),
                admission_count=self._admission_count,
                admission_rejected_count=self._admission_rejected_count,
                eviction_count=self._eviction_count,
                hit_count=self._hit_count,
                miss_count=self._miss_count,
            )


def _measure_resident(value: object) -> tuple[int, int]:
    units = tuple(getattr(value, "units", ()))
    encoded = canonical_json_bytes(
        tuple(item.model_dump(mode="json") for item in units)
    )
    # The hydrated carrier duplicates some Python strings from the typed units.
    # Charging twice the canonical unit bytes is deliberately conservative.
    resident_bytes = max(1, len(encoded) * 2)
    resident_chunks = max(1, (len(units) + 127) // 128)
    return resident_bytes, resident_chunks


DEFAULT_PROVIDER_INPUT_RESIDENT_MANAGER = ProviderInputResidentBudgetManager()


__all__ = [
    "DEFAULT_PROVIDER_INPUT_RESIDENT_MANAGER",
    "ProviderInputResidentBudgetManager",
    "ProviderInputResidentCacheKey",
    "ProviderInputResidentCacheStats",
]
