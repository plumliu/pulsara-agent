from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.runner import PostgresDatabaseIdentity
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresPreflightIdentity,
)
from pulsara_agent.storage.schema_contract import (
    build_verified_postgres_schema_binding,
)
from pulsara_agent.storage.schema_verification_service import (
    PostgresSchemaVerificationService,
)


@dataclass(slots=True)
class _FactoryControl:
    verify_started: Event
    release_verify: Event
    lock: Lock
    physical_cancelled: Event
    preflight_count: int = 0
    verify_count: int = 0
    fail: bool = False
    retryable_fail: bool = False


class _FakeFactory:
    control: _FactoryControl
    database_oid_overrides: dict[str, int] = {}

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.database_number = int(dsn.rsplit("-", 1)[-1])
        self.resolved_database_oid = self.database_oid_overrides.get(
            dsn, self.database_number
        )
        self.database_target_fingerprint = postgres_schema_fingerprint(
            "pulsara:test-schema-target:v1", dsn
        )
        self.runtime_role = "runtime"

    def preflight(
        self,
        *,
        deadline_monotonic: float,
        operation_control=None,
    ) -> PostgresPreflightIdentity:
        del operation_control
        assert deadline_monotonic > monotonic()
        with self.control.lock:
            self.control.preflight_count += 1
        return PostgresPreflightIdentity(
            database_target_fingerprint=self.database_target_fingerprint,
            database_identity=PostgresDatabaseIdentity(
                database_name=f"database_{self.resolved_database_oid}",
                database_oid=self.resolved_database_oid,
                runtime_role="runtime",
                normalized_search_path=("public",),
                server_version_num=160000,
            ),
        )

    def verify(self, *, deadline_monotonic: float, operation_control=None):
        with self.control.lock:
            self.control.verify_count += 1
        self.control.verify_started.set()
        while not self.control.release_verify.wait(timeout=0.01):
            if operation_control is not None and operation_control.cancelled:
                self.control.physical_cancelled.set()
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
                    "fake physical verification cancelled",
                    retryable=True,
                )
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("fake verification deadline")
        if self.control.fail:
            raise RuntimeError("stable verification failure")
        if self.control.retryable_fail:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.CONNECTION_FAILED,
                "retryable verification failure",
                retryable=True,
            )
        binding = build_verified_postgres_schema_binding(
            database_target_fingerprint=self.database_target_fingerprint,
            database_name=f"database_{self.resolved_database_oid}",
            database_oid=self.resolved_database_oid,
            normalized_search_path=("public",),
            runtime_role="runtime",
            server_version_num=160000,
            pgvector_extension_version="0.8.0",
            migration_head_version=4,
            durable_registry_prefix_fingerprint=postgres_schema_fingerprint(
                "pulsara:test-registry-prefix:v1", 4
            ),
            fast_executable_schema_fingerprint=postgres_schema_fingerprint(
                "pulsara:test-fast-schema:v1", self.resolved_database_oid
            ),
            verification_contract_fingerprint=postgres_schema_fingerprint(
                "pulsara:test-verification-contract:v1", 1
            ),
        )
        return SimpleNamespace(binding=binding)


def _install_factory(
    monkeypatch,
    *,
    blocked: bool = False,
    fail: bool = False,
    retryable_fail: bool = False,
):
    control = _FactoryControl(
        verify_started=Event(),
        release_verify=Event(),
        lock=Lock(),
        physical_cancelled=Event(),
        fail=fail,
        retryable_fail=retryable_fail,
    )
    if not blocked:
        control.release_verify.set()
    _FakeFactory.control = control
    _FakeFactory.database_oid_overrides = {}
    monkeypatch.setattr(
        "pulsara_agent.storage.schema_verification_service.PostgresRuntimeConnectionFactory",
        _FakeFactory,
    )
    return control


def test_process_service_shares_one_verification_attempt(monkeypatch) -> None:
    control = _install_factory(monkeypatch)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        first, second = await asyncio.gather(
            service.acquire("fake-101", deadline_monotonic=monotonic() + 5.0),
            service.acquire("fake-101", deadline_monotonic=monotonic() + 5.0),
        )
        assert first.schema_binding.binding_fingerprint == (
            second.schema_binding.binding_fingerprint
        )
        first.release()
        second.release()
        await service.close(deadline_monotonic=monotonic() + 5.0)

    asyncio.run(scenario())
    assert control.preflight_count == 2
    assert control.verify_count == 1


def test_waiter_cancellation_detaches_without_cancelling_owner(monkeypatch) -> None:
    control = _install_factory(monkeypatch, blocked=True)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        cancelled = asyncio.create_task(
            service.acquire("fake-102", deadline_monotonic=monotonic() + 5.0)
        )
        await asyncio.to_thread(control.verify_started.wait, 2.0)
        survivor = asyncio.create_task(
            service.acquire("fake-102", deadline_monotonic=monotonic() + 5.0)
        )
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        control.release_verify.set()
        lease = await survivor
        lease.release()
        await service.close(deadline_monotonic=monotonic() + 5.0)

    asyncio.run(scenario())
    assert control.verify_count == 1


def test_verification_keys_are_partitioned_by_database(monkeypatch) -> None:
    control = _install_factory(monkeypatch)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        first, second = await asyncio.gather(
            service.acquire("fake-103", deadline_monotonic=monotonic() + 5.0),
            service.acquire("fake-104", deadline_monotonic=monotonic() + 5.0),
        )
        assert first.schema_binding.database_oid != second.schema_binding.database_oid
        first.release()
        second.release()
        await service.close(deadline_monotonic=monotonic() + 5.0)

    asyncio.run(scenario())
    assert control.verify_count == 2


def test_database_recreation_uses_new_resolved_verification_key(monkeypatch) -> None:
    control = _install_factory(monkeypatch)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        first = await service.acquire("fake-108", deadline_monotonic=monotonic() + 5.0)
        assert first.schema_binding.database_oid == 108
        first.release()

        _FakeFactory.database_oid_overrides["fake-108"] = 1108
        second = await service.acquire("fake-108", deadline_monotonic=monotonic() + 5.0)
        assert second.schema_binding.database_oid == 1108
        second.release()
        await service.close(deadline_monotonic=monotonic() + 5.0)

    asyncio.run(scenario())
    assert control.preflight_count == 2
    assert control.verify_count == 2


def test_stable_verification_failure_is_cached(monkeypatch) -> None:
    control = _install_factory(monkeypatch, fail=True)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="stable verification failure"):
                await service.acquire("fake-105", deadline_monotonic=monotonic() + 5.0)
        await service.close(deadline_monotonic=monotonic() + 5.0)

    asyncio.run(scenario())
    assert control.verify_count == 1


def test_retryable_keyed_verification_failure_is_cached(monkeypatch) -> None:
    control = _install_factory(monkeypatch, retryable_fail=True)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        for _ in range(2):
            with pytest.raises(PostgresSchemaError) as failure:
                await service.acquire("fake-109", deadline_monotonic=monotonic() + 5.0)
            assert failure.value.code is PostgresSchemaFailureCode.CONNECTION_FAILED
        await service.close(deadline_monotonic=monotonic() + 5.0)

    asyncio.run(scenario())
    assert control.preflight_count == 2
    assert control.verify_count == 1


def test_service_shutdown_drains_inflight_verifier(monkeypatch) -> None:
    control = _install_factory(monkeypatch, blocked=True)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        borrower = asyncio.create_task(
            service.acquire("fake-106", deadline_monotonic=monotonic() + 5.0)
        )
        await asyncio.to_thread(control.verify_started.wait, 2.0)
        closing = asyncio.create_task(
            service.close(deadline_monotonic=monotonic() + 5.0)
        )
        await asyncio.sleep(0)
        assert not closing.done()
        control.release_verify.set()
        lease = await borrower
        await closing
        lease.release()

    asyncio.run(scenario())
    assert control.verify_count == 1


def test_physical_verification_is_cancelled_at_operation_deadline(monkeypatch) -> None:
    control = _install_factory(monkeypatch, blocked=True)
    service = PostgresSchemaVerificationService()

    async def scenario() -> None:
        with pytest.raises(PostgresSchemaError) as failure:
            await service.acquire(
                "fake-107",
                deadline_monotonic=monotonic() + 0.15,
            )
        assert failure.value.code is PostgresSchemaFailureCode.DEADLINE_EXCEEDED
        assert await asyncio.to_thread(control.physical_cancelled.wait, 2.0)
        await service.close(deadline_monotonic=monotonic() + 2.0)

    asyncio.run(scenario())
