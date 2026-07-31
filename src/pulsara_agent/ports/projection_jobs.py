"""Low-level execution ports for durable projection and mutation ownership."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, final

from psycopg import Connection

from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationBundleAppendReceiptFact,
    PreparedCanonicalMutationBundleFact,
    RuntimeSessionBootstrapCommitOutcomeFact,
    RuntimeSessionOwnerBootstrapCandidateFact,
    DurableProjectionKind,
    RuntimeWriteAdmissionEpochFact,
)
from pulsara_agent.primitives.context import context_fingerprint


@dataclass(frozen=True, slots=True)
class CanonicalMutationTransactionIdentity:
    schema_binding_fingerprint: str
    connection_provider_borrower_id: str
    transaction_owner_id: str
    transaction_generation: int
    backend_pid: int
    admission_epoch_fingerprint: str
    admission_guard_lock_identity_fingerprint: str
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if self.transaction_generation < 1 or self.backend_pid < 1:
            raise ValueError("canonical mutation transaction identity is invalid")
        payload = asdict(self)
        actual = payload.pop("identity_fingerprint")
        expected = context_fingerprint(
            "canonical-mutation-transaction-identity:v1", payload
        )
        if actual != expected:
            raise ValueError("canonical mutation transaction identity drifted")


class CanonicalMutationCommitPort(Protocol):
    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity: ...

    def append_bundle(
        self, *, bundle: PreparedCanonicalMutationBundleFact
    ) -> CanonicalMutationBundleAppendReceiptFact: ...


class CanonicalMutationWriterPort(Protocol):
    def append_bundle(
        self,
        *,
        bundle: PreparedCanonicalMutationBundleFact,
        deadline_monotonic: float,
    ) -> CanonicalMutationBundleAppendReceiptFact: ...


@final
class MemoryUowScopeFactoryAuthority:
    __slots__ = ("_nonce",)

    def __init__(self, nonce: object) -> None:
        if nonce is not _AUTHORITY_ISSUER:
            raise TypeError("memory UOW scope authority is composition-owned")
        self._nonce = object()

    def __reduce__(self) -> object:
        raise TypeError("memory UOW scope authority is not serializable")


@final
class CanonicalMutationDriverAuthority:
    __slots__ = ("_nonce",)

    def __init__(self, nonce: object) -> None:
        if nonce is not _AUTHORITY_ISSUER:
            raise TypeError("mutation driver authority is composition-owned")
        self._nonce = object()

    def __reduce__(self) -> object:
        raise TypeError("mutation driver authority is not serializable")


_AUTHORITY_ISSUER = object()


def issue_memory_uow_scope_factory_authority() -> MemoryUowScopeFactoryAuthority:
    return MemoryUowScopeFactoryAuthority(_AUTHORITY_ISSUER)


def issue_canonical_mutation_driver_authority() -> CanonicalMutationDriverAuthority:
    return CanonicalMutationDriverAuthority(_AUTHORITY_ISSUER)


def build_canonical_mutation_transaction_identity(
    *,
    schema_binding_fingerprint: str,
    connection_provider_borrower_id: str,
    transaction_owner_id: str,
    transaction_generation: int,
    backend_pid: int,
    admission_epoch_fingerprint: str,
    admission_guard_lock_identity_fingerprint: str,
) -> CanonicalMutationTransactionIdentity:
    payload = {
        "schema_binding_fingerprint": schema_binding_fingerprint,
        "connection_provider_borrower_id": connection_provider_borrower_id,
        "transaction_owner_id": transaction_owner_id,
        "transaction_generation": transaction_generation,
        "backend_pid": backend_pid,
        "admission_epoch_fingerprint": admission_epoch_fingerprint,
        "admission_guard_lock_identity_fingerprint": (
            admission_guard_lock_identity_fingerprint
        ),
    }
    return CanonicalMutationTransactionIdentity(
        **payload,
        identity_fingerprint=context_fingerprint(
            "canonical-mutation-transaction-identity:v1", payload
        ),
    )


@dataclass(frozen=True, slots=True)
class MemoryUowPhysicalTransactionRequest:
    transaction_owner_id: str
    transaction_generation: int
    deadline_monotonic: float
    scope_request_fingerprint: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        if self.transaction_generation < 1 or self.deadline_monotonic <= 0:
            raise ValueError("memory UOW physical request bounds are invalid")
        payload = asdict(self)
        actual = payload.pop("request_fingerprint")
        if actual != context_fingerprint(
            "memory-uow-physical-transaction-request:v1", payload
        ):
            raise ValueError("memory UOW physical request fingerprint drifted")


def build_memory_uow_physical_transaction_request(
    *,
    transaction_owner_id: str,
    transaction_generation: int,
    deadline_monotonic: float,
    scope_request_fingerprint: str,
) -> MemoryUowPhysicalTransactionRequest:
    payload = {
        "transaction_owner_id": transaction_owner_id,
        "transaction_generation": transaction_generation,
        "deadline_monotonic": deadline_monotonic,
        "scope_request_fingerprint": scope_request_fingerprint,
    }
    return MemoryUowPhysicalTransactionRequest(
        **payload,
        request_fingerprint=context_fingerprint(
            "memory-uow-physical-transaction-request:v1", payload
        ),
    )


class MemoryUowPhysicalTransactionCapability(Protocol):
    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity: ...

    @property
    def active(self) -> bool: ...

    def borrow_for_scope_factory(
        self, *, authority: MemoryUowScopeFactoryAuthority
    ) -> AbstractContextManager[Connection]: ...

    def borrow_for_mutation_driver(
        self, *, authority: CanonicalMutationDriverAuthority
    ) -> AbstractContextManager[Connection]: ...

    def issue_canonical_mutation_commit_port(
        self, *, authority: MemoryUowScopeFactoryAuthority
    ) -> CanonicalMutationCommitPort: ...


class PostgresCanonicalMutationTransactionDriverPort(Protocol):
    @property
    def driver_authority(self) -> CanonicalMutationDriverAuthority: ...

    def append_on_transaction(
        self,
        *,
        transaction: MemoryUowPhysicalTransactionCapability,
        bundle: PreparedCanonicalMutationBundleFact,
    ) -> CanonicalMutationBundleAppendReceiptFact: ...


class RuntimeSessionOwnerBootstrapPort(Protocol):
    def candidate(
        self,
        *,
        runtime_session_id: str,
        workspace_root: str | None,
    ) -> RuntimeSessionOwnerBootstrapCandidateFact: ...

    def bootstrap(
        self,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
        deadline_monotonic: float,
    ) -> RuntimeSessionBootstrapCommitOutcomeFact: ...


@dataclass(frozen=True, slots=True)
class ProjectionMigrationTransactionIdentity:
    database_target_fingerprint: str
    database_oid: int
    backend_pid: int
    current_head_version: int
    current_registry_prefix_fingerprint: str
    maintenance_operation_id: str
    maintenance_epoch_fingerprint: str
    transaction_generation: int
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.database_oid < 1
            or self.backend_pid < 1
            or self.current_head_version < 0
            or self.transaction_generation < 1
        ):
            raise ValueError("projection migration transaction identity is invalid")
        _validate_dataclass_fingerprint(
            self,
            "identity_fingerprint",
            "projection-migration-transaction-identity:v1",
        )


@final
class ProjectionMigrationPortAuthority:
    __slots__ = ("_nonce",)

    def __init__(self, nonce: object) -> None:
        if nonce is not _AUTHORITY_ISSUER:
            raise TypeError("projection migration authority is port-owned")
        self._nonce = object()

    def __reduce__(self) -> object:
        raise TypeError("projection migration authority is not serializable")


def issue_projection_migration_port_authority() -> ProjectionMigrationPortAuthority:
    return ProjectionMigrationPortAuthority(_AUTHORITY_ISSUER)


class ProjectionMigrationTransactionCapability(Protocol):
    @property
    def transaction_identity(self) -> ProjectionMigrationTransactionIdentity: ...

    def assert_active(self) -> None: ...

    def borrow_for_port(
        self, *, authority: ProjectionMigrationPortAuthority
    ) -> AbstractContextManager[Connection]: ...


@dataclass(frozen=True, slots=True)
class ProjectionMigrationReadinessView:
    legacy_surface_binding_plan_ready: bool
    timeline_coverage_ready: bool
    evidence_coverage_ready: bool
    authority_fingerprint: str

    def __post_init__(self) -> None:
        _validate_dataclass_fingerprint(
            self,
            "authority_fingerprint",
            "projection-migration-readiness-view:v1",
        )


@dataclass(frozen=True, slots=True)
class ProjectionMigrationPreparationReportView:
    preparation_kind: Literal[
        "legacy_surface_binding_plan.v1",
        "run_timeline_pre_activation_coverage.v1",
        "tool_result_evidence_pre_activation_coverage.v1",
    ]
    target_migration_version: int
    maintenance_operation_id: str
    maintenance_epoch_fingerprint: str
    durable_authority_fingerprint: str
    item_count: int


class ProjectionMigrationPreparationPort(Protocol):
    @property
    def port_authority(self) -> ProjectionMigrationPortAuthority: ...

    def readiness(
        self,
        *,
        transaction: ProjectionMigrationTransactionCapability,
        current_head_version: int,
        database_target_fingerprint: str,
    ) -> ProjectionMigrationReadinessView: ...

    def apply_transform(
        self,
        *,
        transaction: ProjectionMigrationTransactionCapability,
        version: int,
        maintenance_epoch: RuntimeWriteAdmissionEpochFact,
        resulting_registry_prefix_fingerprint: str,
    ) -> None: ...

    def protected_relation_resource_for_version(self, version: int) -> str: ...

    def prepare_legacy_surface_bindings(
        self, *, deadline_monotonic: float
    ) -> ProjectionMigrationPreparationReportView: ...

    def drain_pre_activation(
        self,
        *,
        kind: DurableProjectionKind,
        deadline_monotonic: float,
    ) -> ProjectionMigrationPreparationReportView: ...


def build_projection_migration_transaction_identity(
    **values: object,
) -> ProjectionMigrationTransactionIdentity:
    payload = dict(values)
    return ProjectionMigrationTransactionIdentity(
        **payload,  # type: ignore[arg-type]
        identity_fingerprint=context_fingerprint(
            "projection-migration-transaction-identity:v1", payload
        ),
    )


def build_projection_migration_readiness_view(
    *,
    legacy_surface_binding_plan_ready: bool,
    timeline_coverage_ready: bool,
    evidence_coverage_ready: bool,
) -> ProjectionMigrationReadinessView:
    payload = {
        "legacy_surface_binding_plan_ready": legacy_surface_binding_plan_ready,
        "timeline_coverage_ready": timeline_coverage_ready,
        "evidence_coverage_ready": evidence_coverage_ready,
    }
    return ProjectionMigrationReadinessView(
        **payload,
        authority_fingerprint=context_fingerprint(
            "projection-migration-readiness-view:v1", payload
        ),
    )


def _validate_dataclass_fingerprint(
    value: object, field_name: str, namespace: str
) -> None:
    payload = asdict(value)
    actual = payload.pop(field_name)
    if actual != context_fingerprint(namespace, payload):
        raise ValueError(f"{field_name} mismatch")


__all__ = [
    "CanonicalMutationCommitPort",
    "CanonicalMutationDriverAuthority",
    "CanonicalMutationTransactionIdentity",
    "CanonicalMutationWriterPort",
    "MemoryUowPhysicalTransactionCapability",
    "MemoryUowPhysicalTransactionRequest",
    "MemoryUowScopeFactoryAuthority",
    "PostgresCanonicalMutationTransactionDriverPort",
    "ProjectionMigrationPortAuthority",
    "ProjectionMigrationPreparationPort",
    "ProjectionMigrationPreparationReportView",
    "ProjectionMigrationReadinessView",
    "ProjectionMigrationTransactionCapability",
    "ProjectionMigrationTransactionIdentity",
    "RuntimeSessionOwnerBootstrapPort",
    "build_canonical_mutation_transaction_identity",
    "build_memory_uow_physical_transaction_request",
    "build_projection_migration_readiness_view",
    "build_projection_migration_transaction_identity",
    "issue_canonical_mutation_driver_authority",
    "issue_memory_uow_scope_factory_authority",
    "issue_projection_migration_port_authority",
]
