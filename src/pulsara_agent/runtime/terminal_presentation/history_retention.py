"""Process-local immutable presentation-root retention and cursor leases."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import monotonic
from uuid import uuid4

from pulsara_agent.primitives.presentation_history import (
    PresentationHistoryProjectionRootFact,
    PresentationHistoryRootIdentityFact,
)


@dataclass(frozen=True, slots=True)
class PresentationHistoryCursorRootLease:
    lease_id: str
    attachment_id: str
    root_identity_fingerprint: str
    lease_generation: int
    expires_at_monotonic: float


@dataclass(slots=True)
class _RetainedRoot:
    identity: PresentationHistoryRootIdentityFact
    root: PresentationHistoryProjectionRootFact
    registered_at_monotonic: float


class PresentationHistoryRootRetentionOwner:
    """Current root plus bounded old-root leases; never a durable authority."""

    def __init__(
        self,
        *,
        max_retained_root_generations: int,
        root_retention_ttl_seconds: int,
        max_active_leases: int = 64,
    ) -> None:
        if (
            min(
                max_retained_root_generations,
                root_retention_ttl_seconds,
                max_active_leases,
            )
            <= 0
        ):
            raise ValueError("presentation root retention bounds must be positive")
        self.max_retained_root_generations = max_retained_root_generations
        self.root_retention_ttl_seconds = root_retention_ttl_seconds
        self.max_active_leases = max_active_leases
        self._lock = RLock()
        self._roots: dict[str, _RetainedRoot] = {}
        self._latest_fingerprint: str | None = None
        self._leases: dict[str, PresentationHistoryCursorRootLease] = {}
        self._lease_generation_by_attachment: dict[str, int] = {}

    def install(
        self,
        identity: PresentationHistoryRootIdentityFact,
        root: PresentationHistoryProjectionRootFact,
    ) -> None:
        if (
            identity.projection_root_fingerprint != root.projection_root_fingerprint
            or identity.runtime_session_id != root.runtime_session_id
        ):
            raise ValueError("presentation retained root identity mismatch")
        with self._lock:
            existing = self._roots.get(identity.root_identity_fingerprint)
            candidate = _RetainedRoot(identity, root, monotonic())
            if existing is not None and (
                existing.identity != identity or existing.root != root
            ):
                raise ValueError("presentation retained root conflict")
            self._roots.setdefault(identity.root_identity_fingerprint, candidate)
            if self._latest_fingerprint is None:
                self._latest_fingerprint = identity.root_identity_fingerprint
            else:
                latest = self._roots[self._latest_fingerprint]
                if (
                    identity.checkpoint_generation
                    >= latest.identity.checkpoint_generation
                ):
                    self._latest_fingerprint = identity.root_identity_fingerprint
            self._retire_unleased_unlocked(monotonic())

    def latest(
        self,
    ) -> tuple[
        PresentationHistoryRootIdentityFact, PresentationHistoryProjectionRootFact
    ]:
        with self._lock:
            if self._latest_fingerprint is None:
                raise RuntimeError("presentation root retention has no current root")
            item = self._roots[self._latest_fingerprint]
            return item.identity, item.root

    def resolve(
        self, root_identity_fingerprint: str
    ) -> (
        tuple[
            PresentationHistoryRootIdentityFact, PresentationHistoryProjectionRootFact
        ]
        | None
    ):
        with self._lock:
            self._retire_unleased_unlocked(monotonic())
            item = self._roots.get(root_identity_fingerprint)
            if item is None:
                return None
            return item.identity, item.root

    def borrow(
        self,
        *,
        attachment_id: str,
        root_identity_fingerprint: str,
        ttl_seconds: float,
    ) -> PresentationHistoryCursorRootLease:
        if ttl_seconds <= 0:
            raise ValueError("presentation root lease TTL must be positive")
        with self._lock:
            self._retire_expired_leases_unlocked(monotonic())
            if root_identity_fingerprint not in self._roots:
                raise KeyError(root_identity_fingerprint)
            if len(self._leases) >= self.max_active_leases:
                raise RuntimeError("presentation root lease capacity exhausted")
            generation = self._lease_generation_by_attachment.get(attachment_id, 0) + 1
            self._lease_generation_by_attachment[attachment_id] = generation
            lease = PresentationHistoryCursorRootLease(
                lease_id=f"presentation-root-lease:{uuid4().hex}",
                attachment_id=attachment_id,
                root_identity_fingerprint=root_identity_fingerprint,
                lease_generation=generation,
                expires_at_monotonic=monotonic() + ttl_seconds,
            )
            self._leases[lease.lease_id] = lease
            return lease

    def release(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)
            self._retire_unleased_unlocked(monotonic())

    def renew(self, lease_id: str, *, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("presentation root lease TTL must be positive")
        now = monotonic()
        with self._lock:
            self._retire_expired_leases_unlocked(now)
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            if lease.root_identity_fingerprint not in self._roots:
                self._leases.pop(lease_id, None)
                return False
            self._leases[lease_id] = PresentationHistoryCursorRootLease(
                lease_id=lease.lease_id,
                attachment_id=lease.attachment_id,
                root_identity_fingerprint=lease.root_identity_fingerprint,
                lease_generation=lease.lease_generation,
                expires_at_monotonic=now + ttl_seconds,
            )
            return True

    def release_attachment(self, attachment_id: str) -> None:
        with self._lock:
            for lease_id in tuple(
                key
                for key, lease in self._leases.items()
                if lease.attachment_id == attachment_id
            ):
                self._leases.pop(lease_id, None)
            self._retire_unleased_unlocked(monotonic())

    def clear(self) -> None:
        with self._lock:
            self._leases.clear()
            self._roots.clear()
            self._latest_fingerprint = None

    def _retire_expired_leases_unlocked(self, now: float) -> None:
        for lease_id in tuple(
            key
            for key, lease in self._leases.items()
            if lease.expires_at_monotonic <= now
        ):
            self._leases.pop(lease_id, None)

    def _retire_unleased_unlocked(self, now: float) -> None:
        self._retire_expired_leases_unlocked(now)
        if self._latest_fingerprint is None:
            return
        latest_generation = self._roots[
            self._latest_fingerprint
        ].identity.checkpoint_generation
        leased = {item.root_identity_fingerprint for item in self._leases.values()}
        for fingerprint, retained in tuple(self._roots.items()):
            if fingerprint == self._latest_fingerprint or fingerprint in leased:
                continue
            generation_retained = (
                latest_generation - retained.identity.checkpoint_generation
                < self.max_retained_root_generations
            )
            ttl_retained = (
                now - retained.registered_at_monotonic
                <= self.root_retention_ttl_seconds
            )
            if not generation_retained or not ttl_retained:
                self._roots.pop(fingerprint, None)


__all__ = [
    "PresentationHistoryCursorRootLease",
    "PresentationHistoryRootRetentionOwner",
]
