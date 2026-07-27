"""HostCore-owned durable projection admission and worker coordinator."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from threading import BoundedSemaphore
from time import monotonic
from typing import cast
from uuid import uuid4

from pulsara_agent.runtime.blocking_executor import (
    projection_maintenance_executor,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationSurface,
    DurableProjectionCommitConfirmation,
    DurableProjectionKind,
    LeasedDurableProjectionJob,
)
from pulsara_agent.runtime.projection_jobs.postgres_repository import (
    DurableProjectionSeedBlockedError,
    PostgresDurableProjectionRepository,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DURABLE_PROJECTION_TRIGGER_REGISTRY,
    DurableProjectionExecutableRegistry,
)
from pulsara_agent.runtime.projection_jobs.seeder import (
    build_seed_failure_commit_candidate,
    canonical_seed_state,
)
from pulsara_agent.runtime.projection_jobs.surface import (
    CanonicalMutationSurfaceHandler,
    CanonicalMutationSurfaceWorker,
    PostgresCanonicalMutationSurfaceRepository,
)
from pulsara_agent.runtime.projection_jobs.worker import (
    DurableProjectionPreparedResultFactory,
    DurableProjectionWorkerAttempt,
)
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


_PROCESS_SEED_CAPACITY = BoundedSemaphore(1)
_PROCESS_GENERIC_HANDLER_CAPACITY = BoundedSemaphore(4)
_PROCESS_SURFACE_HANDLER_CAPACITY = {
    surface: BoundedSemaphore(1) for surface in CanonicalMutationSurface
}
_ACTIVE_POLL_SECONDS = 1.0
_IDLE_POLL_SECONDS = 5.0
_DIRTY_AUTHORITY_HINT_LIMIT = 4096


class DurableProjectionServiceHealth(StrEnum):
    HEALTHY = "healthy"
    BACKLOGGED = "backlogged"
    RETRYING = "retrying"
    DEGRADED_DEAD_LETTER = "degraded_dead_letter"
    AUTHORITY_UNTRUSTED = "authority_untrusted"
    WORKER_UNAVAILABLE = "worker_unavailable"
    NON_DURABLE_TEST_MODE = "non_durable_test_mode"


class DurableProjectionServiceCloseBlocked(RuntimeError):
    """Physical projection work still owns its dependency leases."""


class DurableProjectionSeedAuthorityError(ValueError):
    """One session/kind authority is deterministically invalid."""

    def __init__(self, message: str, *, failure_kind: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


@dataclass(frozen=True, slots=True)
class DurableProjectionServiceSnapshot:
    service_id: str
    running: bool
    accepting: bool
    active_operation_count: int
    wake_count: int
    seed_page_count: int
    claimed_job_count: int
    settled_job_count: int
    claimed_surface_delivery_count: int
    settled_surface_delivery_count: int
    health: DurableProjectionServiceHealth
    recent_diagnostics: tuple[str, ...]


@dataclass(slots=True)
class DurableProjectionJobService:
    """Logical owner; physical work stays on the process singleton executor."""

    connection_provider: VerifiedPostgresConnectionProviderProtocol
    executable_registry: DurableProjectionExecutableRegistry
    surface_handlers: tuple[CanonicalMutationSurfaceHandler, ...] = ()
    repository: PostgresDurableProjectionRepository = field(init=False)
    service_id: str = field(
        default_factory=lambda: f"projection-service:{uuid4().hex}",
        init=False,
    )
    _wake_event: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
    )
    _runner: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _active_attempts: set[asyncio.Task[None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _accepting: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _wake_count: int = field(default=0, init=False, repr=False)
    _seed_page_count: int = field(default=0, init=False, repr=False)
    _seed_authority_cursor: tuple[str, str] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _seed_scan_continuation_pending: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _dirty_authority_hints: deque[tuple[str, DurableProjectionKind]] = field(
        default_factory=lambda: deque(maxlen=_DIRTY_AUTHORITY_HINT_LIMIT),
        init=False,
        repr=False,
    )
    _trigger_kinds_by_event_type: dict[str, tuple[DurableProjectionKind, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _claimed_job_count: int = field(default=0, init=False, repr=False)
    _settled_job_count: int = field(default=0, init=False, repr=False)
    _claimed_surface_delivery_count: int = field(
        default=0,
        init=False,
        repr=False,
    )
    _settled_surface_delivery_count: int = field(
        default=0,
        init=False,
        repr=False,
    )
    _health: DurableProjectionServiceHealth = field(
        default=DurableProjectionServiceHealth.HEALTHY,
        init=False,
        repr=False,
    )
    _diagnostics: deque[str] = field(
        default_factory=lambda: deque(maxlen=256),
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.repository = PostgresDurableProjectionRepository(self.connection_provider)
        surfaces = tuple(handler.surface for handler in self.surface_handlers)
        if len(surfaces) != len(set(surfaces)):
            raise ValueError("projection service has duplicate surface handlers")
        by_event_type: dict[str, list[DurableProjectionKind]] = {}
        for contract in DURABLE_PROJECTION_TRIGGER_REGISTRY.contracts():
            for binding in contract.ordered_trigger_bindings:
                by_event_type.setdefault(
                    binding.trigger_event_type,
                    [],
                ).append(contract.projection_kind)
        self._trigger_kinds_by_event_type = {
            event_type: tuple(kinds) for event_type, kinds in by_event_type.items()
        }

    @property
    def accepting(self) -> bool:
        return self._accepting

    async def start(self) -> None:
        if self._runner is not None:
            return
        loop = asyncio.get_running_loop()
        activations = await loop.run_in_executor(
            projection_maintenance_executor(),
            partial(
                self.repository.list_kind_activations,
                deadline_monotonic=monotonic() + 10.0,
            ),
        )
        for activation in activations:
            kind = activation.activation_semantic.projection_kind
            seed_contract = DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(kind)
            if activation.activation_semantic.seed_contract != seed_contract:
                raise ValueError(
                    "active projection contract differs from executable registry"
                )
            binding = self.executable_registry.resolve(
                kind,
                contract_fingerprint=(
                    seed_contract.handler_contract.contract_fingerprint
                ),
            )
            if binding.contract != seed_contract.handler_contract:
                raise ValueError(
                    "active projection handler contract differs from registry"
                )
        self._closing = False
        self._accepting = True
        self._runner = asyncio.create_task(
            self._run(),
            name=f"pulsara-projection-service:{self.service_id}",
        )
        self._wake_event.set()

    async def on_published_event(
        self,
        published: RuntimePublishedEvent,
    ) -> None:
        """O(1) latency hint: no event parsing and no storage access."""

        if not self._accepting:
            return
        event_type = published.event.type.value
        for projection_kind in self._trigger_kinds_by_event_type.get(
            event_type,
            (),
        ):
            self._dirty_authority_hints.append(
                (published.runtime_session_id, projection_kind)
            )
        self._wake_count += 1
        self._wake_event.set()

    def wake(self, runtime_session_id: str | None = None) -> None:
        del runtime_session_id
        if self._accepting:
            self._wake_count += 1
            self._wake_event.set()

    async def aclose(self, *, deadline_monotonic: float) -> None:
        if deadline_monotonic <= monotonic():
            raise TimeoutError("projection service close deadline exceeded")
        deadline = deadline_monotonic
        self._accepting = False
        self._closing = True
        self._wake_event.set()
        runner = self._runner
        if runner is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(runner),
                    max(0.0, deadline - monotonic()),
                )
            except TimeoutError:
                self._record_diagnostic("projection service close drain timed out")
                raise DurableProjectionServiceCloseBlocked(
                    "projection service still owns a physical runner"
                ) from None
        pending = tuple(self._active_attempts)
        if pending:
            done, still_pending = await asyncio.wait(
                pending,
                timeout=max(0.0, deadline - monotonic()),
            )
            for task in done:
                self._consume_attempt(task)
            if still_pending:
                self._record_diagnostic(
                    "projection service active attempt drain timed out"
                )
                raise DurableProjectionServiceCloseBlocked(
                    "projection service still owns physical job attempts"
                )
        self._runner = None

    def snapshot(self) -> DurableProjectionServiceSnapshot:
        return DurableProjectionServiceSnapshot(
            service_id=self.service_id,
            running=self._runner is not None and not self._runner.done(),
            accepting=self._accepting,
            active_operation_count=len(self._active_attempts),
            wake_count=self._wake_count,
            seed_page_count=self._seed_page_count,
            claimed_job_count=self._claimed_job_count,
            settled_job_count=self._settled_job_count,
            claimed_surface_delivery_count=(self._claimed_surface_delivery_count),
            settled_surface_delivery_count=(self._settled_surface_delivery_count),
            health=self._health,
            recent_diagnostics=tuple(self._diagnostics),
        )

    async def _run(self) -> None:
        idle = True
        while not self._closing:
            progressed = False
            try:
                progressed = await self._seed_cycle()
                progressed = await self._claim_cycle() or progressed
                if self.surface_handlers:
                    progressed = await self._surface_cycle() or progressed
            except BaseException as error:
                self._health = DurableProjectionServiceHealth.RETRYING
                self._record_diagnostic(error)
            if self._closing:
                break
            if self._seed_scan_continuation_pending:
                await asyncio.sleep(0)
                continue
            timeout = _ACTIVE_POLL_SECONDS if progressed else _IDLE_POLL_SECONDS
            idle = not progressed
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout)
            except TimeoutError:
                pass
            self._wake_event.clear()
        if idle and self._health is DurableProjectionServiceHealth.BACKLOGGED:
            self._health = DurableProjectionServiceHealth.HEALTHY

    async def _seed_cycle(self) -> bool:
        loop = asyncio.get_running_loop()

        def operation() -> bool:
            with _PROCESS_SEED_CAPACITY:
                return self._seed_cycle_blocking()

        return await loop.run_in_executor(
            projection_maintenance_executor(),
            operation,
        )

    def _seed_cycle_blocking(self) -> bool:
        progressed = False
        dirty_seen: set[tuple[str, DurableProjectionKind]] = set()
        while self._dirty_authority_hints and len(dirty_seen) < 256:
            authority_key = self._dirty_authority_hints.popleft()
            if authority_key in dirty_seen:
                continue
            dirty_seen.add(authority_key)
            authority = self.repository.read_active_seed_authority(
                runtime_session_id=authority_key[0],
                projection_kind=authority_key[1],
                deadline_monotonic=monotonic() + 10.0,
            )
            if authority is None:
                continue
            progressed = (
                self._seed_authority_with_failure_isolation(*authority) or progressed
            )

        cursor = self._seed_authority_cursor
        authorities = self.repository.list_active_seed_authorities(
            after_runtime_session_id=(cursor[0] if cursor is not None else None),
            after_projection_kind=(cursor[1] if cursor is not None else None),
            limit=256,
            deadline_monotonic=monotonic() + 10.0,
        )
        if not authorities:
            self._seed_authority_cursor = None
            self._seed_scan_continuation_pending = False
            return progressed
        if len(authorities) == 256:
            last_cutover = authorities[-1][1]
            self._seed_authority_cursor = (
                last_cutover.runtime_session_id,
                last_cutover.projection_kind.value,
            )
            self._seed_scan_continuation_pending = True
        else:
            self._seed_authority_cursor = None
            self._seed_scan_continuation_pending = False
        for activation, cutover in authorities:
            progressed = (
                self._seed_authority_with_failure_isolation(
                    activation,
                    cutover,
                )
                or progressed
            )
        return progressed

    def _seed_authority_with_failure_isolation(
        self,
        activation: object,
        cutover: object,
    ) -> bool:
        try:
            return self._seed_one_authority(activation, cutover)
        except DurableProjectionSeedAuthorityError as error:
            self._health = DurableProjectionServiceHealth.AUTHORITY_UNTRUSTED
            self._record_diagnostic(error)
            expected = self.repository.read_seed_state(
                cutover.runtime_session_id,
                cutover.projection_kind,
                deadline_monotonic=monotonic() + 10.0,
            )
            failure = build_seed_failure_commit_candidate(
                cutover=cutover,
                activation_fingerprint=activation.activation_fingerprint,
                expected_state=expected or canonical_seed_state(cutover),
                failure_kind=error.failure_kind,
                error=error,
            )
            outcome = self.repository.commit(
                candidate=failure,
                deadline_monotonic=monotonic() + 10.0,
            )
            if outcome.confirmation is not DurableProjectionCommitConfirmation.FULL:
                raise ValueError(
                    "durable projection seed failure could not be confirmed"
                )
            return True

    def _seed_one_authority(self, activation: object, cutover: object) -> bool:
        from pulsara_agent.projection_jobs.contracts import (
            DurableProjectionKindActivationFact,
            DurableProjectionSessionCutoverFact,
        )

        typed_activation = cast(DurableProjectionKindActivationFact, activation)
        typed_cutover = cast(DurableProjectionSessionCutoverFact, cutover)
        seed_contract = DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(
            typed_cutover.projection_kind
        )
        if (
            typed_activation.activation_semantic.seed_contract != seed_contract
            or typed_cutover.seed_contract_fingerprint
            != seed_contract.seed_contract_fingerprint
        ):
            raise DurableProjectionSeedAuthorityError(
                "durable projection activation differs from executable registry",
                failure_kind="trigger_contract_mismatch",
            )
        binding = self.executable_registry.resolve(
            typed_cutover.projection_kind,
            contract_fingerprint=(seed_contract.handler_contract.contract_fingerprint),
        )
        if binding.contract != seed_contract.handler_contract:
            raise DurableProjectionSeedAuthorityError(
                "active durable projection handler contract drifted",
                failure_kind="trigger_contract_mismatch",
            )
        try:
            candidate = self.repository.prepare_next_seed_candidate(
                runtime_session_id=typed_cutover.runtime_session_id,
                projection_kind=typed_cutover.projection_kind,
                deadline_monotonic=monotonic() + 10.0,
            )
        except DurableProjectionSeedBlockedError:
            return False
        except ValueError as error:
            raise DurableProjectionSeedAuthorityError(
                "durable projection source authority failed validation",
                failure_kind="source_authority_conflict",
            ) from error
        if candidate is None:
            return False
        outcome = self.repository.commit(
            candidate=candidate,
            deadline_monotonic=monotonic() + 10.0,
        )
        if outcome.confirmation is DurableProjectionCommitConfirmation.FULL:
            self._seed_page_count += 1
            return True
        if outcome.confirmation is DurableProjectionCommitConfirmation.NONE:
            return True
        self._health = DurableProjectionServiceHealth.AUTHORITY_UNTRUSTED
        raise RuntimeError("durable projection seed commit could not be confirmed")

    async def _claim_cycle(self) -> bool:
        loop = asyncio.get_running_loop()
        leases = await loop.run_in_executor(
            projection_maintenance_executor(),
            partial(
                self.repository.claim_due,
                owner_id=self.service_id,
                limit=4,
                deadline_monotonic=monotonic() + 10.0,
            ),
        )
        if not leases:
            return False
        self._claimed_job_count += len(leases)
        self._health = DurableProjectionServiceHealth.BACKLOGGED
        for lease in leases:
            task = asyncio.create_task(
                self._execute_lease(lease),
                name=f"pulsara-projection-job:{lease.job.job_id}",
            )
            self._active_attempts.add(task)
            task.add_done_callback(self._consume_attempt)
        return True

    async def _surface_cycle(self) -> bool:
        loop = asyncio.get_running_loop()
        repository = PostgresCanonicalMutationSurfaceRepository(
            self.connection_provider
        )

        def operation() -> tuple[int, int]:
            claimed = 0
            settled = 0
            for handler in self.surface_handlers:
                semaphore = _PROCESS_SURFACE_HANDLER_CAPACITY[handler.surface]
                with semaphore:
                    before = _pending_surface_count(
                        self.connection_provider,
                        surface=handler.surface,
                        deadline_monotonic=monotonic() + 10.0,
                    )
                    worker = CanonicalMutationSurfaceWorker(
                        repository=repository,
                        handler=handler,
                        owner_id=(f"{self.service_id}:surface:{handler.surface.value}"),
                    )
                    completed = worker.run_once(
                        limit=4,
                        deadline_monotonic=monotonic() + 120.0,
                    )
                    after = _pending_surface_count(
                        self.connection_provider,
                        surface=handler.surface,
                        deadline_monotonic=monotonic() + 10.0,
                    )
                claimed += max(completed, before - after)
                settled += completed
            return claimed, settled

        claimed, settled = await loop.run_in_executor(
            projection_maintenance_executor(),
            operation,
        )
        self._claimed_surface_delivery_count += claimed
        self._settled_surface_delivery_count += settled
        if claimed or settled:
            self._health = DurableProjectionServiceHealth.BACKLOGGED
        return bool(claimed or settled)

    async def _execute_lease(self, lease: LeasedDurableProjectionJob) -> None:
        try:
            binding = self.executable_registry.resolve(
                lease.job.projection_kind,
                contract_fingerprint=(lease.job.handler_contract.contract_fingerprint),
            )
        except ValueError as error:
            self._health = DurableProjectionServiceHealth.WORKER_UNAVAILABLE
            self._record_diagnostic(error)
            return
        attempt = DurableProjectionWorkerAttempt(
            repository=self.repository,
            prepared_result_factory=cast(
                DurableProjectionPreparedResultFactory,
                binding.executable,
            ),
        )
        loop = asyncio.get_running_loop()

        def operation() -> None:
            with _PROCESS_GENERIC_HANDLER_CAPACITY:
                outcome = attempt.execute(lease)
            if outcome.confirmation is DurableProjectionCommitConfirmation.FULL:
                self._settled_job_count += 1
            elif outcome.confirmation is DurableProjectionCommitConfirmation.NONE:
                self._health = DurableProjectionServiceHealth.RETRYING
                self._record_diagnostic(
                    outcome.failure
                    or "projection settlement returned NONE without diagnostic"
                )
            elif outcome.confirmation is DurableProjectionCommitConfirmation.CONFLICT:
                self._health = DurableProjectionServiceHealth.AUTHORITY_UNTRUSTED
            elif outcome.confirmation is DurableProjectionCommitConfirmation.UNRESOLVED:
                self._health = DurableProjectionServiceHealth.RETRYING

        await loop.run_in_executor(
            projection_maintenance_executor(),
            operation,
        )

    def _consume_attempt(self, task: asyncio.Task[None]) -> None:
        self._active_attempts.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            self._health = DurableProjectionServiceHealth.RETRYING
            self._record_diagnostic(error)
        self._wake_event.set()

    def _record_diagnostic(self, value: object) -> None:
        message = str(value)
        encoded = message.encode("utf-8", errors="replace")[:2048]
        self._diagnostics.append(encoded.decode("utf-8", errors="ignore"))


def _pending_surface_count(
    connection_provider: VerifiedPostgresConnectionProviderProtocol,
    *,
    surface: CanonicalMutationSurface,
    deadline_monotonic: float,
) -> int:
    with connection_provider.connection(
        lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
        deadline_monotonic=deadline_monotonic,
    ) as connection:
        row = connection.execute(
            """
            SELECT count(*)
            FROM canonical_mutation_surface_deliveries
            WHERE surface = %s
              AND status IN ('pending', 'leased', 'retry_wait')
            """,
            (surface.value,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


__all__ = [
    "DurableProjectionJobService",
    "DurableProjectionServiceHealth",
    "DurableProjectionServiceSnapshot",
]
