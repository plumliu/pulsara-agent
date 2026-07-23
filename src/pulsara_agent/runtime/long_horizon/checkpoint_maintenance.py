"""Privileged checkpoint maintenance lock and quiescence boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition, Lock
from time import monotonic
from typing import ContextManager, Protocol

from psycopg.rows import dict_row

from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


class CheckpointMaintenanceSessionNotQuiescent(RuntimeError):
    pass


class CheckpointMaintenanceLockUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckpointMaintenancePermit:
    runtime_session_id: str
    authority_kind: str
    exclusive: bool


class CheckpointMaintenanceAuthority(Protocol):
    def acquire_shared(
        self, runtime_session_id: str
    ) -> ContextManager[CheckpointMaintenancePermit]: ...

    def acquire_exclusive(
        self, runtime_session_id: str
    ) -> ContextManager[CheckpointMaintenancePermit]: ...


@dataclass(slots=True)
class _InMemoryReadWriteLock:
    condition: Condition = field(default_factory=lambda: Condition(Lock()))
    reader_count: int = 0
    writer_active: bool = False

    def try_acquire_shared(self) -> bool:
        with self.condition:
            if self.writer_active:
                return False
            self.reader_count += 1
            return True

    def release_shared(self) -> None:
        with self.condition:
            if self.reader_count < 1:
                raise RuntimeError("checkpoint shared lock is not held")
            self.reader_count -= 1
            self.condition.notify_all()

    def try_acquire_exclusive(self) -> bool:
        with self.condition:
            if self.writer_active or self.reader_count:
                return False
            self.writer_active = True
            return True

    def release_exclusive(self) -> None:
        with self.condition:
            if not self.writer_active:
                raise RuntimeError("checkpoint exclusive lock is not held")
            self.writer_active = False
            self.condition.notify_all()


@dataclass(slots=True)
class InMemoryCheckpointMaintenanceAuthority:
    """Test-only lock with an explicit quiescence fact source."""

    is_quiescent: Callable[[str], bool]
    _locks: dict[str, _InMemoryReadWriteLock] = field(
        default_factory=dict, init=False, repr=False
    )
    _catalog_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def _lock_for(self, runtime_session_id: str) -> _InMemoryReadWriteLock:
        if not runtime_session_id:
            raise ValueError("checkpoint maintenance runtime session is required")
        with self._catalog_lock:
            return self._locks.setdefault(
                runtime_session_id, _InMemoryReadWriteLock()
            )

    @contextmanager
    def acquire_shared(
        self, runtime_session_id: str
    ) -> Iterator[CheckpointMaintenancePermit]:
        lock = self._lock_for(runtime_session_id)
        if not lock.try_acquire_shared():
            raise CheckpointMaintenanceLockUnavailable(
                "checkpoint_maintenance_lock_unavailable"
            )
        try:
            yield CheckpointMaintenancePermit(
                runtime_session_id=runtime_session_id,
                authority_kind="in_memory_test_double",
                exclusive=False,
            )
        finally:
            lock.release_shared()

    @contextmanager
    def acquire_exclusive(
        self, runtime_session_id: str
    ) -> Iterator[CheckpointMaintenancePermit]:
        lock = self._lock_for(runtime_session_id)
        if not lock.try_acquire_exclusive():
            raise CheckpointMaintenanceLockUnavailable(
                "checkpoint_maintenance_lock_unavailable"
            )
        try:
            if not self.is_quiescent(runtime_session_id):
                raise CheckpointMaintenanceSessionNotQuiescent(
                    "checkpoint_maintenance_session_not_quiescent"
                )
            yield CheckpointMaintenancePermit(
                runtime_session_id=runtime_session_id,
                authority_kind="in_memory_test_double",
                exclusive=True,
            )
        finally:
            lock.release_exclusive()


@dataclass(frozen=True, slots=True)
class PostgresCheckpointMaintenanceAuthority:
    """Connection-owned advisory lock for closed durable sessions."""

    connection_provider: VerifiedPostgresConnectionProviderProtocol

    @contextmanager
    def acquire_shared(
        self, runtime_session_id: str
    ) -> Iterator[CheckpointMaintenancePermit]:
        with self._acquire(
            runtime_session_id,
            exclusive=False,
            require_quiescent=False,
        ) as permit:
            yield permit

    @contextmanager
    def acquire_exclusive(
        self, runtime_session_id: str
    ) -> Iterator[CheckpointMaintenancePermit]:
        with self._acquire(
            runtime_session_id,
            exclusive=True,
            require_quiescent=True,
        ) as permit:
            yield permit

    @contextmanager
    def _acquire(
        self,
        runtime_session_id: str,
        *,
        exclusive: bool,
        require_quiescent: bool,
    ) -> Iterator[CheckpointMaintenancePermit]:
        if not runtime_session_id:
            raise ValueError("checkpoint maintenance runtime session is required")
        lock_key = f"pulsara:checkpoint-maintenance:{runtime_session_id}"
        acquired = False
        lock_function = (
            "pg_try_advisory_lock"
            if exclusive
            else "pg_try_advisory_lock_shared"
        )
        unlock_function = (
            "pg_advisory_unlock"
            if exclusive
            else "pg_advisory_unlock_shared"
        )
        with self.connection_provider.connection(
            lane=PostgresConnectionLane.CHECKPOINT_MAINTENANCE,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 30.0,
        ) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"select {lock_function}(hashtextextended(%s, 0)) as acquired",
                        (lock_key,),
                    )
                    acquired = bool(cursor.fetchone()["acquired"])
                    if not acquired:
                        raise CheckpointMaintenanceLockUnavailable(
                            "checkpoint_maintenance_lock_unavailable"
                        )
                    if require_quiescent:
                        cursor.execute(
                            """
                            select metadata #>> '{lifecycle,closed_at}' as closed_at
                            from sessions
                            where id = %s
                            """,
                            (runtime_session_id,),
                        )
                        session = cursor.fetchone()
                        cursor.execute(
                            """
                            select count(*) as active_count
                            from runs
                            where session_id = %s and status = 'running'
                            """,
                            (runtime_session_id,),
                        )
                        active_count = int(cursor.fetchone()["active_count"])
                        if (
                            session is None
                            or session["closed_at"] is None
                            or active_count != 0
                        ):
                            raise CheckpointMaintenanceSessionNotQuiescent(
                                "checkpoint_maintenance_session_not_quiescent"
                            )
                yield CheckpointMaintenancePermit(
                    runtime_session_id=runtime_session_id,
                    authority_kind="postgres_advisory_lock",
                    exclusive=exclusive,
                )
            finally:
                if acquired:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"select {unlock_function}(hashtextextended(%s, 0))",
                            (lock_key,),
                        )


def checkpoint_maintenance_authority_for_event_log(
    event_log: object,
) -> CheckpointMaintenanceAuthority | None:
    """Bind production Postgres readers to the checkpoint maintenance domain."""

    from pulsara_agent.event_log.postgres import PostgresEventLog

    if isinstance(event_log, PostgresEventLog):
        return PostgresCheckpointMaintenanceAuthority(
            event_log.connection_provider
        )
    return None
