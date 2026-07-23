"""Process-owned, cancellation-safe PostgreSQL schema verification service."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Any

from pulsara_agent.storage.postgres_connection_provider import (
    BorrowedVerifiedPostgresConnectionProvider,
    PostgresPreflightIdentity,
    PostgresRuntimeConnectionFactory,
    VerifiedPostgresConnectionProvider,
)
from pulsara_agent.storage.postgres_endpoint import PostgresPhysicalOperationControl
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.schema_contract import VerifiedPostgresSchemaBinding


class PostgresVerificationLifecycle(StrEnum):
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(slots=True)
class _VerificationAttempt:
    factory: PostgresRuntimeConnectionFactory
    operation_control: PostgresPhysicalOperationControl
    future: Future[VerifiedPostgresConnectionProvider]
    lifecycle: PostgresVerificationLifecycle = PostgresVerificationLifecycle.VERIFYING
    provider: VerifiedPostgresConnectionProvider | None = None
    failure: BaseException | None = None


class VerifiedPostgresAccessLease:
    def __init__(
        self,
        provider: VerifiedPostgresConnectionProvider,
        *,
        close_provider_on_release: bool = False,
    ) -> None:
        self._provider = provider
        self._connection_provider = provider.borrow()
        self._released = False
        self._close_provider_on_release = close_provider_on_release

    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding:
        self._require_active()
        return self._provider.schema_binding

    @property
    def connection_provider(self) -> BorrowedVerifiedPostgresConnectionProvider:
        self._require_active()
        return self._connection_provider

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._connection_provider.release()
            if self._close_provider_on_release:
                self._provider.close()

    def _require_active(self) -> None:
        if self._released:
            raise RuntimeError("verified PostgreSQL access lease is released")

    def __enter__(self) -> "VerifiedPostgresAccessLease":
        self._require_active()
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self.release()

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedPostgresAccessLease is not serializable")

    def __copy__(self) -> Any:
        raise TypeError("VerifiedPostgresAccessLease cannot be copied")

    def __deepcopy__(self, memo: object) -> Any:
        del memo
        raise TypeError("VerifiedPostgresAccessLease cannot be copied")


class PostgresSchemaVerificationService:
    def __init__(self) -> None:
        self._lock = RLock()
        self._attempts: dict[tuple[object, ...], _VerificationAttempt] = {}
        self._preflights: dict[
            Future[PostgresPreflightIdentity],
            PostgresPhysicalOperationControl,
        ] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="pulsara-postgres-schema-verify",
        )
        self._closing = False

    async def acquire(
        self,
        runtime_dsn: str,
        *,
        deadline_monotonic: float,
    ) -> VerifiedPostgresAccessLease:
        with self._lock:
            if self._closing:
                raise RuntimeError("PostgreSQL schema verification service is closing")
        factory = PostgresRuntimeConnectionFactory(runtime_dsn)
        preflight = await self._resolve_preflight(
            factory,
            deadline_monotonic=deadline_monotonic,
        )
        from pulsara_agent.storage.migrations.registry import (
            POSTGRES_MIGRATION_REGISTRY,
        )

        key = (
            preflight.database_target_fingerprint,
            preflight.database_identity.database_oid,
            preflight.database_identity.runtime_role,
            POSTGRES_MIGRATION_REGISTRY.registry_fingerprint,
        )
        with self._lock:
            if self._closing:
                raise RuntimeError("PostgreSQL schema verification service is closing")
            attempt = self._attempts.get(key)
            if attempt is None:
                operation_control = PostgresPhysicalOperationControl(
                    deadline_monotonic=deadline_monotonic
                )
                operation_control.arm()
                future = self._executor.submit(
                    self._verify_factory,
                    factory,
                    preflight,
                    deadline_monotonic,
                    operation_control,
                )
                attempt = _VerificationAttempt(
                    factory=factory,
                    operation_control=operation_control,
                    future=future,
                )
                self._attempts[key] = attempt
                future.add_done_callback(
                    lambda completed, attempt_key=key: self._finish_attempt(
                        attempt_key, completed
                    )
                )
        try:
            wrapped_future = asyncio.wrap_future(attempt.future)
            wrapped_future.add_done_callback(_consume_wrapped_future_outcome)
            provider = await asyncio.wait_for(
                asyncio.shield(wrapped_future),
                timeout=_remaining(deadline_monotonic),
            )
        except TimeoutError as exc:
            if monotonic() >= attempt.operation_control.deadline_monotonic:
                attempt.operation_control.cancel()
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
                "PostgreSQL schema verification borrower deadline exceeded",
                retryable=True,
            ) from exc
        except asyncio.CancelledError:
            raise
        return VerifiedPostgresAccessLease(provider)

    async def _resolve_preflight(
        self,
        factory: PostgresRuntimeConnectionFactory,
        *,
        deadline_monotonic: float,
    ) -> PostgresPreflightIdentity:
        operation_control = PostgresPhysicalOperationControl(
            deadline_monotonic=deadline_monotonic
        )
        operation_control.arm()
        with self._lock:
            if self._closing:
                operation_control.finish()
                raise RuntimeError("PostgreSQL schema verification service is closing")
            future = self._executor.submit(
                self._preflight_factory,
                factory,
                deadline_monotonic,
                operation_control,
            )
            self._preflights[future] = operation_control
            future.add_done_callback(self._finish_preflight)
        try:
            wrapped_future = asyncio.wrap_future(future)
            wrapped_future.add_done_callback(_consume_wrapped_future_outcome)
            return await asyncio.wait_for(
                asyncio.shield(wrapped_future),
                timeout=_remaining(deadline_monotonic),
            )
        except TimeoutError as exc:
            operation_control.cancel()
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
                "PostgreSQL schema verification preflight deadline exceeded",
                retryable=True,
            ) from exc
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _preflight_factory(
        factory: PostgresRuntimeConnectionFactory,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl,
    ) -> PostgresPreflightIdentity:
        return factory.preflight(
            deadline_monotonic=deadline_monotonic,
            operation_control=operation_control,
        )

    @staticmethod
    def _verify_factory(
        factory: PostgresRuntimeConnectionFactory,
        preflight: PostgresPreflightIdentity,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl,
    ) -> VerifiedPostgresConnectionProvider:
        bundle = factory.verify(
            deadline_monotonic=deadline_monotonic,
            operation_control=operation_control,
        )
        binding = bundle.binding
        _require_binding_matches_preflight(
            factory=factory,
            preflight=preflight,
            binding=binding,
        )
        return VerifiedPostgresConnectionProvider(
            factory=factory,
            binding=binding,
        )

    def _finish_preflight(
        self,
        future: Future[PostgresPreflightIdentity],
    ) -> None:
        with self._lock:
            operation_control = self._preflights.pop(future, None)
        if operation_control is not None:
            operation_control.finish()
        if future.cancelled():
            return
        try:
            future.exception()
        except BaseException:
            pass

    def _finish_attempt(
        self,
        key: tuple[object, ...],
        future: Future[VerifiedPostgresConnectionProvider],
    ) -> None:
        with self._lock:
            attempt = self._attempts.get(key)
            if attempt is None or attempt.future is not future:
                return
            attempt.operation_control.finish()
            try:
                attempt.provider = future.result()
            except BaseException as exc:
                attempt.failure = exc
                attempt.lifecycle = PostgresVerificationLifecycle.FAILED
            else:
                attempt.lifecycle = PostgresVerificationLifecycle.VERIFIED

    async def close(self, *, deadline_monotonic: float | None = None) -> None:
        deadline = deadline_monotonic or (monotonic() + 30.0)
        with self._lock:
            self._closing = True
            attempts = tuple(self._attempts.values())
            preflights = tuple(self._preflights.items())
        owned_futures = tuple(
            (
                future,
                operation_control,
            )
            for future, operation_control in preflights
        ) + tuple((attempt.future, attempt.operation_control) for attempt in attempts)
        if owned_futures:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            asyncio.shield(asyncio.wrap_future(future))
                            for future, _operation_control in owned_futures
                        ),
                        return_exceptions=True,
                    ),
                    timeout=_remaining(deadline),
                )
            except TimeoutError:
                for future, operation_control in owned_futures:
                    if not future.done():
                        operation_control.cancel()
        for attempt in attempts:
            if attempt.future.done() and not attempt.future.cancelled():
                try:
                    attempt.future.result().close()
                except BaseException:
                    pass
        self._executor.shutdown(wait=False, cancel_futures=False)


def _remaining(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("PostgreSQL schema verification deadline exceeded")
    return remaining


def _consume_wrapped_future_outcome(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    future.exception()


def _require_binding_matches_preflight(
    *,
    factory: PostgresRuntimeConnectionFactory,
    preflight: PostgresPreflightIdentity,
    binding: VerifiedPostgresSchemaBinding,
) -> None:
    identity = preflight.database_identity
    if (
        preflight.database_target_fingerprint != factory.database_target_fingerprint
        or binding.database_target_fingerprint != preflight.database_target_fingerprint
        or binding.database_name != identity.database_name
        or binding.database_oid != identity.database_oid
        or binding.runtime_role != identity.runtime_role
        or binding.normalized_search_path != identity.normalized_search_path
        or binding.server_version_num != identity.server_version_num
    ):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
            "PostgreSQL identity changed between resolved-key preflight and verification",
        )


_PROCESS_SERVICE: PostgresSchemaVerificationService | None = None
_PROCESS_SERVICE_LOCK = RLock()


def process_postgres_schema_verification_service() -> PostgresSchemaVerificationService:
    global _PROCESS_SERVICE
    with _PROCESS_SERVICE_LOCK:
        if _PROCESS_SERVICE is None:
            _PROCESS_SERVICE = PostgresSchemaVerificationService()
        return _PROCESS_SERVICE


def acquire_verified_postgres_access_sync(
    runtime_dsn: str,
    *,
    deadline_monotonic: float,
) -> VerifiedPostgresAccessLease:
    """Verify one short-lived synchronous composition root such as CLI inspect."""

    factory = PostgresRuntimeConnectionFactory(runtime_dsn)
    preflight = factory.preflight(deadline_monotonic=deadline_monotonic)
    bundle = factory.verify(deadline_monotonic=deadline_monotonic)
    _require_binding_matches_preflight(
        factory=factory,
        preflight=preflight,
        binding=bundle.binding,
    )
    return VerifiedPostgresAccessLease(
        VerifiedPostgresConnectionProvider(factory=factory, binding=bundle.binding),
        close_provider_on_release=True,
    )


__all__ = [
    "PostgresSchemaVerificationService",
    "PostgresVerificationLifecycle",
    "VerifiedPostgresAccessLease",
    "acquire_verified_postgres_access_sync",
    "process_postgres_schema_verification_service",
]
