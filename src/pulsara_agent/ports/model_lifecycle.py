"""Purpose-neutral transaction companion contracts for model lifecycle commits."""

from __future__ import annotations

from typing import Any, Literal, Protocol, Sequence

from pydantic import model_validator

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.model_call import ModelCallPurpose


class ModelLifecycleContractError(RuntimeError):
    """Purpose-neutral base for deterministic model contract failures."""

    reason_code = "model_contract_error"


class ModelLifecycleTransactionCompanionIdentityFact(FrozenRuntimeStateBase):
    companion_kind: Literal["durable_derived_model_job"]
    phase: Literal["start", "terminal"]
    purpose: ModelCallPurpose
    resolved_model_call_id: str
    stable_primary_event_id: str
    external_owner_reference_fingerprint: str
    stable_candidate_fingerprint: str
    companion_fingerprint: str


class ModelLifecycleTransactionCompanion(Protocol):
    @property
    def identity(self) -> ModelLifecycleTransactionCompanionIdentityFact: ...

    def apply_postgres(
        self,
        cursor: Any,
        stored_events: Sequence[Any],
    ) -> None: ...

    def apply_in_memory(self, stored_events: Sequence[Any]) -> None: ...


class ModelLifecycleTransactionCompanionFactory(Protocol):
    def __call__(
        self,
        *,
        lease: object,
        reservation: object,
        admission_lease: object,
        model_call_start_event_id: str,
        model_call_end_event_id: str,
    ) -> tuple[
        ModelLifecycleTransactionCompanion,
        ModelLifecycleTransactionCompanion,
    ]: ...


class PreparedProviderInputStartBundlePort(Protocol):
    prepared_candidate: Any
    committed_reference: Any
    companion_events: tuple[Any, ...]
    append_semantic_fingerprint: str
    autonomy_delivery: Any

    @property
    def resulting_core_state(self) -> Any: ...

    @property
    def is_one_shot(self) -> bool: ...


class ModelLifecycleRuntimeGateway(Protocol):
    runtime_session_id: str
    allow_unbootstrapped_test_events: bool
    provider_input_generation_coordinator: Any
    long_horizon_state_store: Any
    event_log: Any

    def resolve_run_rollout_binding(self, *, run_id: str) -> Any: ...

    def plan_root_model_admission(
        self,
        *,
        account: Any,
        state: Any,
        quote: Any,
        purpose: Any,
    ) -> Any: ...

    def build_one_shot_generation_close_event(
        self,
        *,
        bundle: PreparedProviderInputStartBundlePort,
        event_context: Any,
    ) -> Any: ...


def _validate_identity(model: FrozenRuntimeStateBase, *, field: str, domain: str) -> None:
    expected = context_fingerprint(
        domain,
        model.model_dump(mode="json", exclude={field}),
    )
    if getattr(model, field) != expected:
        raise ValueError(f"{field} mismatch")


class DriverRegistrationLeaseIdentity(FrozenRuntimeStateBase):
    registry_id: str
    runtime_session_id: str
    driver_generation: int
    binding_fingerprint: str
    registration_id: str
    identity_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "DriverRegistrationLeaseIdentity":
        if self.driver_generation < 1:
            raise ValueError("driver registration generation must be positive")
        _validate_identity(
            self,
            field="identity_fingerprint",
            domain="compaction-memory-driver-registration-identity:v1",
        )
        return self


class DriverRegistrationLease(Protocol):
    @property
    def identity(self) -> DriverRegistrationLeaseIdentity: ...

    @property
    def active(self) -> bool: ...

    def revoke(self) -> None: ...


class DriverBorrowIdentity(FrozenRuntimeStateBase):
    registration_identity_fingerprint: str
    borrow_id: str
    borrow_generation: int
    identity_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "DriverBorrowIdentity":
        if self.borrow_generation < 1:
            raise ValueError("driver borrow generation must be positive")
        _validate_identity(
            self,
            field="identity_fingerprint",
            domain="compaction-memory-driver-borrow-identity:v1",
        )
        return self


class DriverBorrow(Protocol):
    @property
    def identity(self) -> DriverBorrowIdentity: ...

    @property
    def active(self) -> bool: ...

    @property
    def driver(self) -> "CompactionMemoryExtractionSessionDriverHandle": ...

    def release(self) -> None: ...


class BackgroundModelCallAdmissionLeaseIdentity(FrozenRuntimeStateBase):
    lease_id: str
    lease_generation: int
    runtime_session_id: str
    operation_id: str
    admission_proof_fingerprint: str
    identity_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "BackgroundModelCallAdmissionLeaseIdentity":
        if self.lease_generation < 1:
            raise ValueError("background admission generation must be positive")
        _validate_identity(
            self,
            field="identity_fingerprint",
            domain="background-model-call-admission-lease-identity:v1",
        )
        return self


class BackgroundModelCallAdmissionProof(FrozenRuntimeStateBase):
    lease_id: str
    lease_generation: int
    runtime_session_id: str
    operation_id: str
    host_state_generation: int
    active_run_frontier_fingerprint: str
    permission_policy_revision: int
    permission_policy_fingerprint: str
    stop_intent_revision: int
    close_intent_revision: int
    expected_provider_input_generation_revision: int
    expires_at_monotonic: float
    proof_fingerprint: str

    @model_validator(mode="after")
    def _fingerprint(self) -> "BackgroundModelCallAdmissionProof":
        if self.lease_generation < 1 or self.expires_at_monotonic <= 0:
            raise ValueError("background admission proof bounds are invalid")
        _validate_identity(
            self,
            field="proof_fingerprint",
            domain="background-model-call-admission-proof:v1",
        )
        return self


class BackgroundModelCallAdmissionLease(Protocol):
    @property
    def identity(self) -> BackgroundModelCallAdmissionLeaseIdentity: ...

    @property
    def state(self) -> Literal[
        "issued", "in_flight", "consumed", "released", "reconciliation_required"
    ]: ...

    def begin_model_start(self) -> None: ...

    def validate_model_start(self, *, resolved_model_call_id: str) -> None: ...

    def confirm_model_start_full(self) -> None: ...

    def mark_reconciliation_required(self) -> None: ...

    def release(self) -> None: ...


class CompactionMemoryExtractionSessionDriverHandle(Protocol):
    @property
    def runtime_session_id(self) -> str: ...

    @property
    def driver_generation(self) -> int: ...

    @property
    def binding_fingerprint(self) -> str: ...

    async def acquire_model_safe_point(
        self, *, operation_id: str, deadline_monotonic: float
    ) -> BackgroundModelCallAdmissionLease | None: ...

    async def execute_leased_job(
        self, job: object, *, deadline_monotonic: float
    ) -> None: ...

    async def settle_result_candidate(
        self,
        result_candidate: object,
        *,
        settlement_generation: int,
        deadline_monotonic: float,
    ) -> None: ...

    def stop_admission(self) -> None: ...

    async def close(self, *, deadline_monotonic: float) -> None: ...


class CompactionMemoryExtractionDriverRegistry(Protocol):
    def next_driver_generation(self, runtime_session_id: str) -> int: ...

    def register(
        self, driver: CompactionMemoryExtractionSessionDriverHandle
    ) -> DriverRegistrationLease: ...

    def available_runtime_session_ids(
        self, *, now_monotonic: float
    ) -> tuple[str, ...]: ...

    def next_eligible_at_monotonic(self, runtime_session_id: str) -> float | None: ...

    def mark_dirty(self, runtime_session_id: str) -> None: ...

    def borrow(self, runtime_session_id: str) -> DriverBorrow | None: ...

    def active_borrow_count(self, runtime_session_id: str) -> int: ...


class BackgroundModelCallAdmissionPort(Protocol):
    async def acquire(
        self,
        *,
        runtime_session_id: str,
        operation_kind: Literal["compaction_memory_extraction"],
        operation_id: str,
        deadline_monotonic: float,
    ) -> BackgroundModelCallAdmissionLease | None: ...


__all__ = [
    "ModelLifecycleTransactionCompanion",
    "ModelLifecycleTransactionCompanionFactory",
    "ModelLifecycleTransactionCompanionIdentityFact",
    "BackgroundModelCallAdmissionLease",
    "BackgroundModelCallAdmissionLeaseIdentity",
    "BackgroundModelCallAdmissionPort",
    "BackgroundModelCallAdmissionProof",
    "CompactionMemoryExtractionDriverRegistry",
    "CompactionMemoryExtractionSessionDriverHandle",
    "DriverBorrow",
    "DriverBorrowIdentity",
    "DriverRegistrationLease",
    "DriverRegistrationLeaseIdentity",
    "ModelLifecycleContractError",
]
