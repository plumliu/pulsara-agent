from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.runtime.provider_input.resident import (
    ProviderInputResidentBudgetManager,
    ProviderInputResidentCacheKey,
)


@dataclass(frozen=True)
class _Unit:
    content: str

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return {"content": self.content}


@dataclass(frozen=True)
class _Resident:
    units: tuple[_Unit, ...]


def _key(owner_id: str, *, runtime_session_id: str = "session"):
    return ProviderInputResidentCacheKey(
        runtime_session_id=runtime_session_id,
        owner_kind="generation",
        owner_id=owner_id,
    )


def test_provider_input_resident_budget_evicts_lru_without_affecting_authority() -> (
    None
):
    manager = ProviderInputResidentBudgetManager(
        max_resident_bytes=1024,
        max_resident_chunks=8,
        max_resident_generations=1,
    )
    first = _Resident((_Unit("first"),))
    second = _Resident((_Unit("second"),))

    assert manager.admit(_key("first"), first) is True
    assert manager.get(_key("first")) is first
    assert manager.admit(_key("second"), second) is True

    assert manager.get(_key("first")) is None
    assert manager.get(_key("second")) is second
    stats = manager.stats()
    assert stats.resident_generations == 1
    assert stats.eviction_count == 1
    assert stats.hit_count == 2
    assert stats.miss_count == 1


def test_provider_input_resident_admission_rejection_is_a_cache_miss() -> None:
    manager = ProviderInputResidentBudgetManager(
        max_resident_bytes=1,
        max_resident_chunks=1,
        max_resident_generations=1,
    )
    key = _key("too-large")

    assert manager.admit(key, _Resident((_Unit("payload"),))) is False
    assert manager.get(key) is None
    stats = manager.stats()
    assert stats.resident_bytes == 0
    assert stats.admission_rejected_count == 1
    assert stats.miss_count == 1


def test_provider_input_resident_budget_releases_one_runtime_session() -> None:
    manager = ProviderInputResidentBudgetManager(
        max_resident_bytes=4096,
        max_resident_chunks=8,
        max_resident_generations=4,
    )
    resident = _Resident((_Unit("payload"),))
    manager.admit(_key("a", runtime_session_id="session-a"), resident)
    manager.admit(_key("b", runtime_session_id="session-b"), resident)

    manager.discard_runtime_session("session-a")

    assert manager.get(_key("a", runtime_session_id="session-a")) is None
    assert manager.get(_key("b", runtime_session_id="session-b")) is resident
    assert manager.stats().resident_generations == 1
