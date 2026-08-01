"""Process-owned, generation-aware session driver registry."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import monotonic
from uuid import uuid4

from pulsara_agent.ports.model_lifecycle import (
    CompactionMemoryExtractionSessionDriverHandle,
    DriverBorrow,
    DriverBorrowIdentity,
    DriverRegistrationLease,
    DriverRegistrationLeaseIdentity,
)
from pulsara_agent.primitives._context_base import context_fingerprint


@dataclass(slots=True)
class _Registration:
    identity: DriverRegistrationLeaseIdentity
    driver: CompactionMemoryExtractionSessionDriverHandle
    active: bool = True
    borrow_generation: int = 0
    active_borrows: int = 0
    next_eligible_at: float | None = None
    dirty: bool = True


class _RegistrationLease(DriverRegistrationLease):
    def __init__(
        self,
        registry: "ProcessCompactionMemoryExtractionDriverRegistry",
        identity: DriverRegistrationLeaseIdentity,
    ) -> None:
        self._registry = registry
        self._identity = identity

    @property
    def identity(self) -> DriverRegistrationLeaseIdentity:
        return self._identity

    @property
    def active(self) -> bool:
        return self._registry._registration_is_active(self._identity)

    def revoke(self) -> None:
        self._registry._revoke(self._identity)


class _Borrow(DriverBorrow):
    def __init__(
        self,
        registry: "ProcessCompactionMemoryExtractionDriverRegistry",
        registration: _Registration,
        identity: DriverBorrowIdentity,
    ) -> None:
        self._registry = registry
        self._registration_identity = registration.identity
        self._driver = registration.driver
        self._identity = identity
        self._active = True

    @property
    def identity(self) -> DriverBorrowIdentity:
        return self._identity

    @property
    def active(self) -> bool:
        return self._active

    @property
    def driver(self) -> CompactionMemoryExtractionSessionDriverHandle:
        if not self._active:
            raise RuntimeError("compaction extraction driver borrow was released")
        return self._driver

    def release(self) -> None:
        if not self._active:
            return
        self._active = False
        self._registry._release_borrow(
            self._registration_identity,
            self._identity,
        )


class ProcessCompactionMemoryExtractionDriverRegistry:
    """One process registry; HostCore instances share bounded session drivers."""

    def __init__(self) -> None:
        self.registry_id = f"compaction-memory-driver-registry:{uuid4().hex}"
        self._lock = RLock()
        self._registrations: dict[str, _Registration] = {}
        self._generation_by_session: dict[str, int] = {}

    def next_driver_generation(self, runtime_session_id: str) -> int:
        with self._lock:
            current = self._registrations.get(runtime_session_id)
            if current is not None and current.active:
                raise ValueError("compaction extraction driver is already registered")
            return self._generation_by_session.get(runtime_session_id, 0) + 1

    def register(
        self,
        driver: CompactionMemoryExtractionSessionDriverHandle,
    ) -> DriverRegistrationLease:
        runtime_session_id = driver.runtime_session_id
        with self._lock:
            current = self._registrations.get(runtime_session_id)
            if current is not None and current.active:
                raise ValueError("compaction extraction driver is already registered")
            generation = self._generation_by_session.get(runtime_session_id, 0) + 1
            if driver.driver_generation != generation:
                raise ValueError("compaction extraction driver generation drifted")
            registration_id = (
                f"compaction-memory-driver:{runtime_session_id}:{generation}"
            )
            payload = {
                "registry_id": self.registry_id,
                "runtime_session_id": runtime_session_id,
                "driver_generation": generation,
                "binding_fingerprint": driver.binding_fingerprint,
                "registration_id": registration_id,
            }
            identity = DriverRegistrationLeaseIdentity(
                **payload,
                identity_fingerprint=context_fingerprint(
                    "compaction-memory-driver-registration-identity:v1",
                    payload,
                ),
            )
            self._registrations[runtime_session_id] = _Registration(
                identity=identity,
                driver=driver,
            )
            self._generation_by_session[runtime_session_id] = generation
            return _RegistrationLease(self, identity)

    def available_runtime_session_ids(
        self,
        *,
        now_monotonic: float,
    ) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    runtime_session_id
                    for runtime_session_id, item in self._registrations.items()
                    if item.active
                    and item.dirty
                    and (
                        item.next_eligible_at is None
                        or item.next_eligible_at <= now_monotonic
                    )
                )
            )

    def next_eligible_at_monotonic(self, runtime_session_id: str) -> float | None:
        with self._lock:
            item = self._registrations.get(runtime_session_id)
            return item.next_eligible_at if item is not None and item.active else None

    def mark_dirty(self, runtime_session_id: str) -> None:
        with self._lock:
            item = self._registrations.get(runtime_session_id)
            if item is not None and item.active:
                item.dirty = True

    def mark_clean(self, runtime_session_id: str) -> None:
        with self._lock:
            item = self._registrations.get(runtime_session_id)
            if item is not None:
                item.dirty = False

    def defer(
        self,
        runtime_session_id: str,
        *,
        not_before_monotonic: float,
    ) -> None:
        if not_before_monotonic <= monotonic():
            raise ValueError("driver deferral must point into the future")
        with self._lock:
            item = self._registrations.get(runtime_session_id)
            if item is not None and item.active:
                item.next_eligible_at = not_before_monotonic
                item.dirty = True

    def borrow(self, runtime_session_id: str) -> DriverBorrow | None:
        with self._lock:
            item = self._registrations.get(runtime_session_id)
            if item is None or not item.active:
                return None
            item.borrow_generation += 1
            item.active_borrows += 1
            item.dirty = False
            item.next_eligible_at = None
            borrow_id = (
                f"driver-borrow:{runtime_session_id}:{item.borrow_generation}:"
                f"{uuid4().hex}"
            )
            payload = {
                "registration_identity_fingerprint": (
                    item.identity.identity_fingerprint
                ),
                "borrow_id": borrow_id,
                "borrow_generation": item.borrow_generation,
            }
            identity = DriverBorrowIdentity(
                **payload,
                identity_fingerprint=context_fingerprint(
                    "compaction-memory-driver-borrow-identity:v1",
                    payload,
                ),
            )
            return _Borrow(self, item, identity)

    def active_borrow_count(self, runtime_session_id: str) -> int:
        with self._lock:
            item = self._registrations.get(runtime_session_id)
            return item.active_borrows if item is not None else 0

    def _registration_is_active(
        self,
        identity: DriverRegistrationLeaseIdentity,
    ) -> bool:
        with self._lock:
            item = self._registrations.get(identity.runtime_session_id)
            return bool(item is not None and item.active and item.identity == identity)

    def _revoke(self, identity: DriverRegistrationLeaseIdentity) -> None:
        with self._lock:
            item = self._registrations.get(identity.runtime_session_id)
            if item is None or item.identity != identity:
                raise RuntimeError("driver registration identity is stale")
            item.active = False
            item.dirty = False

    def _release_borrow(
        self,
        registration_identity: DriverRegistrationLeaseIdentity,
        borrow_identity: DriverBorrowIdentity,
    ) -> None:
        with self._lock:
            item = self._registrations.get(registration_identity.runtime_session_id)
            if item is None or item.identity != registration_identity:
                raise RuntimeError("driver borrow registration identity was lost")
            if (
                borrow_identity.registration_identity_fingerprint
                != item.identity.identity_fingerprint
                or item.active_borrows < 1
            ):
                raise RuntimeError("driver borrow accounting mismatch")
            item.active_borrows -= 1


PROCESS_COMPACTION_MEMORY_EXTRACTION_DRIVER_REGISTRY = (
    ProcessCompactionMemoryExtractionDriverRegistry()
)


__all__ = [
    "PROCESS_COMPACTION_MEMORY_EXTRACTION_DRIVER_REGISTRY",
    "ProcessCompactionMemoryExtractionDriverRegistry",
]
