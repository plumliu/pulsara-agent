"""Pure ledger materialization account state and deterministic charging."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterable, Sequence

from pulsara_agent.event import (
    AgentEvent,
    CheckpointDispatchBarrierInstalledEvent,
    CheckpointDispatchBarrierReleasedEvent,
    LedgerMaterializationAccountGenesisEvent,
    LedgerMaterializationConsumerHorizonAdvancedEvent,
    LedgerMaterializationConsumerRegisteredEvent,
    LedgerMaterializationConsumerRetiredEvent,
    LedgerMaterializationGenerationAdvancedEvent,
    PhysicalOperationChargeAppliedEvent,
    PhysicalOperationReservationCreatedEvent,
    PhysicalOperationReservationSettledEvent,
    PhysicalOperationReservationSuspendedEvent,
    RunStartEvent,
    SubagentGraphCheckpointCommittedEvent,
    EventContext,
)
from pulsara_agent.event_log.protocol import EventLog
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    canonical_event_payload_bytes,
)
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
)
from pulsara_agent.primitives.authority_materialization import (
    ActivePhysicalReservationStateFact,
    CheckpointDispatchBarrierFact,
    LedgerMaterializationAccountStateFact,
    LedgerMaterializationConsumerHorizonFact,
    LedgerMaterializationGenerationFact,
    PhysicalChargeContractFact,
    AuthorityMaterializationLimits,
    LedgerMaterializationAccountTransitionFact,
    LedgerMaterializationTransitionCauseIdentityFact,
    LedgerMaterializationAccountGenesisFact,
    LedgerMaterializationConsumerKind,
    LedgerGenesisConsumerCauseFact,
    RunSeedConsumerCauseFact,
    ConsumerRetirementCauseFact,
    CheckpointConsumerCauseFact,
    PhysicalBurstContractFact,
    PhysicalOperationKind,
    PhysicalOperationChargeAppliedFact,
    PhysicalOperationReservationFact,
    PhysicalOperationSuspensionTailFact,
    PhysicalOperationSettlementFact,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.llm.terminal_projection import stable_event_identity


class MaterializationAccountContractError(RuntimeError):
    """A committed transition does not match the canonical account reducer."""


class PhysicalHeadroomExceeded(RuntimeError):
    """The authoritative account cannot admit a finite operation burst."""


class CheckpointDispatchBarrierActive(RuntimeError):
    """Producer admission is closed while a checkpoint barrier owns the ledger."""


class MaterializationAccountReconciliationRequired(
    MaterializationAccountContractError
):
    """The atomic event/account outcome cannot be proven FULL or NONE."""


class MaterializationAccountCommitFailed(MaterializationAccountContractError):
    """The stable event/account candidate was proven not committed."""


class RunSeedSourceStale(MaterializationAccountCommitFailed):
    """A RunStart seed no longer matches the current ledger high-water."""


def _bounded_bookkeeping_event_id(kind: str, *identity_parts: str) -> str:
    digest = context_fingerprint(
        "ledger-materialization-bookkeeping-event-id:v1",
        {"kind": kind, "identity_parts": identity_parts},
    )
    return f"{kind}:{digest.removeprefix('sha256:')[:32]}"


@dataclass(frozen=True, slots=True)
class CommittedPhysicalReservation:
    reservation: PhysicalOperationReservationFact
    reservation_event: PhysicalOperationReservationCreatedEvent
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class PhysicalDispatchReservationRequest:
    reservation_id: str
    owner_id: str
    burst_contract: PhysicalBurstContractFact
    business_event_ids: tuple[str, ...]
    business_run_id: str | None = None
    business_window_id: str | None = None
    business_window_generation: int | None = None


@dataclass(frozen=True, slots=True)
class PhysicalOneShotReservationRequest:
    reservation_id: str
    owner_id: str
    burst_contract: PhysicalBurstContractFact
    business_event_ids: tuple[str, ...]
    terminal_outcome: str


@dataclass(frozen=True, slots=True)
class CommittedPhysicalReservationBatch:
    reservations: tuple[PhysicalOperationReservationFact, ...]
    reservation_events: tuple[PhysicalOperationReservationCreatedEvent, ...]
    one_shot_reservation: PhysicalOperationReservationFact | None
    one_shot_settlement_event: PhysicalOperationReservationSettledEvent | None
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedOneShotPhysicalOperation:
    reservation: PhysicalOperationReservationFact
    reservation_event: PhysicalOperationReservationCreatedEvent
    settlement_event: PhysicalOperationReservationSettledEvent
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedPhysicalCharge:
    charge_event: PhysicalOperationChargeAppliedEvent
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_reservation_state: ActivePhysicalReservationStateFact
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedPhysicalSettlement:
    settlement_event: PhysicalOperationReservationSettledEvent
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedPhysicalSuspension:
    suspension_event: PhysicalOperationReservationSuspendedEvent
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_reservation_state: ActivePhysicalReservationStateFact
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedLedgerGenesis:
    genesis_event: LedgerMaterializationAccountGenesisEvent
    consumer_events: tuple[LedgerMaterializationConsumerRegisteredEvent, ...]
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedGraphConsumerCheckpoint:
    checkpoint_event: SubagentGraphCheckpointCommittedEvent
    horizon_event: LedgerMaterializationConsumerHorizonAdvancedEvent
    generation_event: LedgerMaterializationGenerationAdvancedEvent | None
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class CommittedRunSeedConsumerRotation:
    reservation: PhysicalOperationReservationFact
    registration_event: LedgerMaterializationConsumerRegisteredEvent
    retirement_events: tuple[LedgerMaterializationConsumerRetiredEvent, ...]
    generation_event: LedgerMaterializationGenerationAdvancedEvent | None
    reservation_event: PhysicalOperationReservationCreatedEvent
    settlement_event: PhysicalOperationReservationSettledEvent
    business_events: tuple[AgentEvent, ...]
    stored_events: tuple[AgentEvent, ...]
    resulting_account_state: LedgerMaterializationAccountStateFact


@dataclass(frozen=True, slots=True)
class DeterministicLedgerCharge:
    event_count: int
    charged_payload_bytes: int


def deterministic_ledger_charge(
    events: Iterable[AgentEvent],
    *,
    contract: PhysicalChargeContractFact,
) -> DeterministicLedgerCharge:
    """Charge stable candidates without depending on database-assigned sequence."""

    event_tuple = tuple(events)
    bounds = {
        (item.event_type, item.event_schema_version): item
        for item in contract.bookkeeping_event_bounds
    }
    charged_bytes = 0
    for event in event_tuple:
        schema = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(event).schema_contract
        bound = bounds.get((str(event.type), schema.event_schema_version))
        if bound is not None:
            if str(event.type) == "PHYSICAL_OPERATION_CHARGE_APPLIED":
                dynamic_charge = (
                    event.charge.charge_applied_event_charge_payload_bytes
                )
                if dynamic_charge > bound.max_stored_envelope_bytes:
                    raise MaterializationAccountContractError(
                        "dynamic charge-applied quote exceeds its envelope bound"
                    )
                charged_bytes += dynamic_charge
            else:
                charged_bytes += bound.max_stored_envelope_bytes
        else:
            charged_bytes += len(
                canonical_event_payload_bytes(event.model_copy(update={"sequence": None}))
            ) + (
                contract.fixed_sequence_wrapper_charge_bytes_per_event
                + contract.fixed_schema_wrapper_charge_bytes_per_event
            )
    return DeterministicLedgerCharge(
        event_count=len(event_tuple),
        charged_payload_bytes=charged_bytes,
    )


def deterministic_bookkeeping_charge(
    event_type: str,
    *,
    contract: PhysicalChargeContractFact,
    business_event_count: int | None = None,
) -> DeterministicLedgerCharge:
    matching = tuple(
        item for item in contract.bookkeeping_event_bounds if item.event_type == event_type
    )
    if len(matching) != 1:
        raise MaterializationAccountContractError(
            f"bookkeeping event does not have one fixed charge: {event_type}"
        )
    charged_payload_bytes = matching[0].max_stored_envelope_bytes
    if event_type == "PHYSICAL_OPERATION_CHARGE_APPLIED":
        if business_event_count is None or business_event_count <= 0:
            raise MaterializationAccountContractError(
                "charge-applied bookkeeping requires a positive business event count"
            )
        charged_payload_bytes = (
            contract.charge_applied_bookkeeping_base_charge_bytes
            + business_event_count
            * contract.charge_applied_bookkeeping_per_business_event_charge_bytes
        )
        if charged_payload_bytes > matching[0].max_stored_envelope_bytes:
            raise MaterializationAccountContractError(
                "dynamic charge-applied quote exceeds the canonical envelope bound"
            )
    elif business_event_count is not None:
        raise MaterializationAccountContractError(
            "business event count is only valid for charge-applied bookkeeping"
        )
    return DeterministicLedgerCharge(
        event_count=1,
        charged_payload_bytes=charged_payload_bytes,
    )


def _candidate_charge_split(
    events: Sequence[AgentEvent],
    *,
    total_charge: DeterministicLedgerCharge,
) -> tuple[int, int]:
    candidate_bytes = sum(
        len(canonical_event_payload_bytes(event.model_copy(update={"sequence": None})))
        for event in events
    )
    wrapper_bytes = total_charge.charged_payload_bytes - candidate_bytes
    if wrapper_bytes < 0:
        raise MaterializationAccountContractError(
            "deterministic candidate charge is smaller than canonical payload"
        )
    return candidate_bytes, wrapper_bytes


def canonical_empty_generation(
    *,
    runtime_session_id: str,
    charge_contract_fingerprint: str,
) -> LedgerMaterializationGenerationFact:
    consumer_set = context_fingerprint(
        "ledger-materialization-consumer-set:v1", ()
    )
    return build_frozen_fact(
        LedgerMaterializationGenerationFact,
        schema_version="ledger_materialization_generation.v1",
        runtime_session_id=runtime_session_id,
        ledger_materialization_generation=0,
        consumer_horizon_revision=0,
        consumer_horizons=(),
        active_consumer_set_fingerprint=consumer_set,
        reclaimable_through_sequence=0,
        reclaimable_event_count_through=0,
        reclaimable_charged_payload_bytes_through=0,
        ledger_continuity_accumulator_through_reclaimable=(
            EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
        ),
        physical_charge_contract_fingerprint=charge_contract_fingerprint,
    )


def canonical_empty_account(
    *,
    runtime_session_id: str,
    charge_contract_fingerprint: str,
) -> LedgerMaterializationAccountStateFact:
    return build_account_state(
        runtime_session_id=runtime_session_id,
        generation=canonical_empty_generation(
            runtime_session_id=runtime_session_id,
            charge_contract_fingerprint=charge_contract_fingerprint,
        ),
        ledger_through_sequence=0,
        ledger_charged_payload_bytes_through=0,
        active_reservations=(),
        active_checkpoint_barrier=None,
        latest_transition_event_ids=(),
        reconciliation_required=False,
        reconciliation_reason_code=None,
    )


def build_generation(
    *,
    source: LedgerMaterializationGenerationFact,
    consumer_horizons: Sequence[LedgerMaterializationConsumerHorizonFact],
    materialization_generation: int | None = None,
    consumer_horizon_revision: int | None = None,
) -> LedgerMaterializationGenerationFact:
    horizons = tuple(
        sorted(
            consumer_horizons,
            key=lambda item: (item.consumer_kind.value, item.consumer_id),
        )
    )
    minimum = min(horizons, key=lambda item: item.through_sequence, default=None)
    return build_frozen_fact(
        LedgerMaterializationGenerationFact,
        schema_version="ledger_materialization_generation.v1",
        runtime_session_id=source.runtime_session_id,
        ledger_materialization_generation=(
            source.ledger_materialization_generation
            if materialization_generation is None
            else materialization_generation
        ),
        consumer_horizon_revision=(
            source.consumer_horizon_revision
            if consumer_horizon_revision is None
            else consumer_horizon_revision
        ),
        consumer_horizons=horizons,
        active_consumer_set_fingerprint=context_fingerprint(
            "ledger-materialization-consumer-set:v1",
            tuple(item.horizon_fingerprint for item in horizons),
        ),
        reclaimable_through_sequence=(minimum.through_sequence if minimum else 0),
        reclaimable_event_count_through=(
            minimum.ledger_event_count_through if minimum else 0
        ),
        reclaimable_charged_payload_bytes_through=(
            minimum.ledger_charged_payload_bytes_through if minimum else 0
        ),
        ledger_continuity_accumulator_through_reclaimable=(
            minimum.ledger_continuity_accumulator
            if minimum
            else EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
        ),
        physical_charge_contract_fingerprint=(
            source.physical_charge_contract_fingerprint
        ),
    )


def build_account_state(
    *,
    runtime_session_id: str,
    generation: LedgerMaterializationGenerationFact,
    ledger_through_sequence: int,
    ledger_charged_payload_bytes_through: int,
    active_reservations: Sequence[ActivePhysicalReservationStateFact],
    active_checkpoint_barrier: CheckpointDispatchBarrierFact | None,
    latest_transition_event_ids: Sequence[str],
    reconciliation_required: bool,
    reconciliation_reason_code: str | None,
) -> LedgerMaterializationAccountStateFact:
    reservations = tuple(sorted(active_reservations, key=lambda item: item.reservation_id))
    transition_ids = tuple(sorted(set(latest_transition_event_ids)))
    return build_frozen_fact(
        LedgerMaterializationAccountStateFact,
        schema_version="ledger_materialization_account_state.v1",
        runtime_session_id=runtime_session_id,
        generation=generation,
        ledger_through_sequence=ledger_through_sequence,
        ledger_event_count_through=ledger_through_sequence,
        ledger_charged_payload_bytes_through=(
            ledger_charged_payload_bytes_through
        ),
        used_since_reclaimable_events=(
            ledger_through_sequence - generation.reclaimable_event_count_through
        ),
        used_since_reclaimable_payload_bytes=(
            ledger_charged_payload_bytes_through
            - generation.reclaimable_charged_payload_bytes_through
        ),
        active_reservations=reservations,
        active_checkpoint_barrier=active_checkpoint_barrier,
        latest_transition_event_ids=transition_ids,
        reconciliation_required=reconciliation_required,
        reconciliation_reason_code=reconciliation_reason_code,
    )


def account_with_committed_usage(
    source: LedgerMaterializationAccountStateFact,
    *,
    events: Sequence[AgentEvent],
    charge_contract: PhysicalChargeContractFact,
    generation: LedgerMaterializationGenerationFact | None = None,
    active_reservations: Sequence[ActivePhysicalReservationStateFact] | None = None,
    active_checkpoint_barrier: CheckpointDispatchBarrierFact | None | object = ...,
    transition_event_ids: Sequence[str] = (),
) -> LedgerMaterializationAccountStateFact:
    charge = deterministic_ledger_charge(events, contract=charge_contract)
    barrier = (
        source.active_checkpoint_barrier
        if active_checkpoint_barrier is ...
        else active_checkpoint_barrier
    )
    return build_account_state(
        runtime_session_id=source.runtime_session_id,
        generation=generation or source.generation,
        ledger_through_sequence=source.ledger_through_sequence + charge.event_count,
        ledger_charged_payload_bytes_through=(
            source.ledger_charged_payload_bytes_through
            + charge.charged_payload_bytes
        ),
        active_reservations=(
            source.active_reservations
            if active_reservations is None
            else active_reservations
        ),
        active_checkpoint_barrier=barrier,  # type: ignore[arg-type]
        latest_transition_event_ids=transition_event_ids,
        reconciliation_required=source.reconciliation_required,
        reconciliation_reason_code=source.reconciliation_reason_code,
    )


class LedgerMaterializationAccountStore:
    """Incremental process projection backed by the atomic PostgreSQL row."""

    reducer_id = "ledger-materialization-account:ap3-v1"

    def __init__(
        self,
        *,
        state: LedgerMaterializationAccountStateFact | None,
        charge_contract: PhysicalChargeContractFact,
    ) -> None:
        self._lock = RLock()
        self._state = state
        self._charge_contract = charge_contract

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._state.ledger_through_sequence if self._state else 0

    def snapshot(self) -> LedgerMaterializationAccountStateFact | None:
        with self._lock:
            return self._state

    def install_confirmed_state(
        self, state: LedgerMaterializationAccountStateFact
    ) -> None:
        with self._lock:
            current = self._state
            if current is not None and (
                state.ledger_through_sequence < current.ledger_through_sequence
            ):
                raise MaterializationAccountContractError(
                    "materialization account cannot move backwards"
                )
            self._state = state

    def apply_committed(self, events: Sequence[AgentEvent]) -> None:
        """Verify account-bearing committed events against the atomic state row.

        The storage transaction owns the full resulting state. The reducer only
        accepts a genesis-carried state directly; subsequent batches are installed
        through ``install_confirmed_state`` after the same transaction returns.
        Non-account batches are ignored until AP4 makes every producer account-aware.
        """

        with self._lock:
            genesis = tuple(
                event
                for event in events
                if isinstance(event, LedgerMaterializationAccountGenesisEvent)
            )
            if genesis:
                if len(genesis) != 1 or self._state is not None:
                    raise MaterializationAccountContractError(
                        "ledger materialization genesis must be unique"
                    )
                candidate = genesis[0].resulting_account_state
                if candidate.ledger_through_sequence != max(
                    _stored_sequence(event) for event in events
                ):
                    raise MaterializationAccountContractError(
                        "genesis state does not cover its atomic batch"
                    )
                self._state = candidate


class LedgerMaterializationCoordinator:
    """The single linearized physical admission account for one event ledger."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        event_log: EventLog,
        store: LedgerMaterializationAccountStore,
        charge_contract: PhysicalChargeContractFact,
        limits: AuthorityMaterializationLimits,
        prepare_event: Callable[[AgentEvent], AgentEvent] | None = None,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.event_log = event_log
        self.store = store
        self.charge_contract = charge_contract
        self.limits = limits
        self._prepare_event = prepare_event or (lambda event: event)
        self._lock = RLock()

    def bootstrap_genesis(
        self,
        *,
        context: EventContext,
        business_events: Sequence[AgentEvent],
        genesis_profile: str,
        genesis_burst_contract: PhysicalBurstContractFact,
        register_transcript_consumer: bool,
        deadline_monotonic: float | None = None,
    ) -> CommittedLedgerGenesis:
        """Create the canonical account only alongside an empty ledger's first facts."""

        if genesis_profile not in {"host_first_run", "subagent_first_run"}:
            raise ValueError("unsupported ledger genesis profile")
        if genesis_burst_contract.operation_kind is not PhysicalOperationKind.LEDGER_GENESIS:
            raise ValueError("genesis requires the dedicated non-reservable contract")
        if not business_events:
            raise ValueError("ledger genesis requires first business facts")
        with self._lock:
            if self.store.snapshot() is not None:
                raise MaterializationAccountContractError(
                    "ledger materialization account is already initialized"
                )
            if self.event_log.next_sequence() != 1:
                raise MaterializationAccountContractError(
                    "non-empty ledger without materialization account is unsupported; "
                    "reset the database for the hard-cut schema"
                )
            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            empty = canonical_empty_account(
                runtime_session_id=self.runtime_session_id,
                charge_contract_fingerprint=self.charge_contract.contract_fingerprint,
            )
            horizon_specs = [
                (
                    LedgerMaterializationConsumerKind.SUBAGENT_GRAPH,
                    f"subagent_graph:{self.runtime_session_id}",
                    None,
                    None,
                    None,
                )
            ]
            if register_transcript_consumer:
                horizon_specs.append(
                    (
                        LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW,
                        f"transcript:{context.run_id}:seed",
                        context.run_id,
                        "seed",
                        0,
                    )
                )
            horizons = tuple(
                build_frozen_fact(
                    LedgerMaterializationConsumerHorizonFact,
                    schema_version="ledger_materialization_consumer_horizon.v1",
                    runtime_session_id=self.runtime_session_id,
                    consumer_kind=kind,
                    consumer_id=consumer_id,
                    business_run_id=run_id,
                    business_window_id=window_id,
                    business_window_generation=window_generation,
                    through_sequence=0,
                    ledger_event_count_through=0,
                    ledger_charged_payload_bytes_through=0,
                    ledger_continuity_accumulator=(
                        EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
                    ),
                    consumer_contract_fingerprint=context_fingerprint(
                        "ledger-materialization-consumer-contract:v1",
                        {
                            "kind": kind.value,
                            "consumer_id": consumer_id,
                        },
                    ),
                )
                for kind, consumer_id, run_id, window_id, window_generation in sorted(
                    horizon_specs, key=lambda item: (item[0].value, item[1])
                )
            )
            generation = build_generation(
                source=empty.generation,
                consumer_horizons=horizons,
                consumer_horizon_revision=len(horizons),
            )
            event_count = len(prepared_business) + len(horizons) + 1
            business_charge = deterministic_ledger_charge(
                prepared_business, contract=self.charge_contract
            )
            registration_charge = deterministic_bookkeeping_charge(
                "LEDGER_MATERIALIZATION_CONSUMER_REGISTERED",
                contract=self.charge_contract,
            )
            genesis_charge = deterministic_bookkeeping_charge(
                "LEDGER_MATERIALIZATION_ACCOUNT_GENESIS",
                contract=self.charge_contract,
            )
            charged_bytes = (
                business_charge.charged_payload_bytes
                + len(horizons) * registration_charge.charged_payload_bytes
                + genesis_charge.charged_payload_bytes
            )
            if (
                event_count > genesis_burst_contract.max_total_reserved_events
                or charged_bytes
                > genesis_burst_contract.max_total_reserved_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "ledger genesis batch exceeds its static burst contract"
                )
            genesis_event_id = _bounded_bookkeeping_event_id(
                "ledger_materialization_genesis",
                self.runtime_session_id,
            )
            registration_event_ids = tuple(
                _bounded_bookkeeping_event_id(
                    "ledger_consumer_registered",
                    item.consumer_id,
                )
                for item in horizons
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=generation,
                ledger_through_sequence=event_count,
                ledger_charged_payload_bytes_through=charged_bytes,
                active_reservations=(),
                active_checkpoint_barrier=None,
                latest_transition_event_ids=(
                    genesis_event_id,
                    *registration_event_ids,
                ),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            business_causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role="run_start",
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=empty,
                resulting=resulting,
                causes=business_causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-genesis-transition-contract:v1",
                    genesis_profile,
                ),
            )
            required_kinds = tuple(
                sorted((item.consumer_kind for item in horizons), key=lambda item: item.value)
            )
            genesis_fact = build_frozen_fact(
                LedgerMaterializationAccountGenesisFact,
                schema_version="ledger_materialization_account_genesis.v1",
                genesis_id=_bounded_bookkeeping_event_id(
                    "genesis",
                    self.runtime_session_id,
                ),
                runtime_session_id=self.runtime_session_id,
                empty_account=empty.generation,
                genesis_burst_contract_fingerprint=(
                    genesis_burst_contract.contract_fingerprint
                ),
                genesis_batch_contract_fingerprint=context_fingerprint(
                    "ledger-genesis-batch-contract:v1",
                    {
                        "profile": genesis_profile,
                        "consumer_kinds": tuple(
                            item.value for item in required_kinds
                        ),
                        "business_event_types": tuple(
                            sorted(str(item.type) for item in prepared_business)
                        ),
                    },
                ),
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                required_initial_consumer_kinds=required_kinds,
            )
            genesis_event = self._prepare_event(
                LedgerMaterializationAccountGenesisEvent(
                    id=genesis_event_id,
                    **context.event_fields(),
                    genesis=genesis_fact,
                    transition=transition,
                    resulting_account_state=resulting,
                )
            )
            genesis_identity = stable_event_identity(
                genesis_event,
                runtime_session_id=self.runtime_session_id,
            )
            registration_cause = build_frozen_fact(
                LedgerGenesisConsumerCauseFact,
                schema_version="ledger_genesis_consumer_cause.v1",
                cause_kind="ledger_genesis",
                genesis_event_identity=genesis_identity,
                genesis_contract_fingerprint=genesis_fact.genesis_fingerprint,
            )
            consumer_events = tuple(
                self._prepare_event(
                    LedgerMaterializationConsumerRegisteredEvent(
                        id=event_id,
                        **context.event_fields(),
                        consumer=horizon,
                        cause=registration_cause,
                        transition=transition,
                        resulting_account_state_fingerprint=(
                            resulting.account_state_fingerprint
                        ),
                    )
                )
                for event_id, horizon in zip(
                    registration_event_ids, horizons, strict=True
                )
            )
            candidate_batch = (
                *prepared_business,
                *consumer_events,
                genesis_event,
            )
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=None,
                resulting=resulting,
                expected_last_sequence=0,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedLedgerGenesis(
                genesis_event=genesis_event,
                consumer_events=consumer_events,
                business_events=prepared_business,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    def reserve_and_commit_dispatch(
        self,
        *,
        context: EventContext,
        business_events: Sequence[AgentEvent],
        reservation_id: str,
        owner_id: str,
        burst_contract: PhysicalBurstContractFact,
        business_run_id: str | None = None,
        business_window_id: str | None = None,
        business_window_generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> CommittedPhysicalReservation:
        """Atomically create a reservation and its dispatch proof batch."""

        if not business_events:
            raise ValueError("physical reservation requires a business dispatch fact")
        if burst_contract.operation_kind.value == "ledger_genesis":
            raise ValueError("ledger genesis is not an ordinary reservation owner")
        with self._lock:
            source = self._require_state()
            if source.reconciliation_required:
                raise MaterializationAccountContractError(
                    "materialization account requires reconciliation"
                )
            if source.active_checkpoint_barrier is not None:
                raise CheckpointDispatchBarrierActive(
                    "checkpoint barrier rejects new producer admission"
                )
            if any(
                item.reservation_id == reservation_id
                for item in source.active_reservations
            ):
                raise ValueError("physical reservation ID is already active")
            if len(source.active_reservations) >= (
                self.limits.max_active_physical_reservations
            ):
                raise PhysicalHeadroomExceeded(
                    "active physical reservation count is exhausted"
                )

            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            business_charge = deterministic_ledger_charge(
                prepared_business,
                contract=self.charge_contract,
            )
            reservation_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_CREATED",
                contract=self.charge_contract,
            )
            initial_charge_events = (
                business_charge.event_count + reservation_charge.event_count
            )
            initial_charge_bytes = (
                business_charge.charged_payload_bytes
                + reservation_charge.charged_payload_bytes
            )
            required_tail_events = burst_contract.terminal_tail_reserved_events
            required_tail_bytes = (
                burst_contract.terminal_tail_reserved_payload_bytes
            )
            if (
                burst_contract.max_total_reserved_events
                < initial_charge_events + required_tail_events
                or burst_contract.max_total_reserved_payload_bytes
                < initial_charge_bytes + required_tail_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "operation burst cannot cover dispatch proof and terminal tail"
                )
            active_reserved_events = sum(
                item.remaining_events for item in source.active_reservations
            )
            active_reserved_bytes = sum(
                item.remaining_payload_bytes for item in source.active_reservations
            )
            if (
                source.used_since_reclaimable_events
                + active_reserved_events
                + burst_contract.max_total_reserved_events
                > self.limits.max_unreclaimable_ledger_events
                or source.used_since_reclaimable_payload_bytes
                + active_reserved_bytes
                + burst_contract.max_total_reserved_payload_bytes
                > self.limits.max_unreclaimable_charged_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "physical event ledger headroom is exhausted"
                )

            reservation = build_frozen_fact(
                PhysicalOperationReservationFact,
                schema_version="physical_operation_reservation.v2",
                reservation_id=reservation_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=business_run_id,
                business_window_id=business_window_id,
                business_window_generation=business_window_generation,
                owner_kind=burst_contract.operation_kind,
                owner_id=owner_id,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                source_ledger_through_sequence=source.ledger_through_sequence,
                burst_contract_id=burst_contract.contract_id,
                burst_contract_version=burst_contract.contract_version,
                burst_contract_fingerprint=burst_contract.contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                reserved_events=burst_contract.max_total_reserved_events,
                reserved_payload_bytes=burst_contract.max_total_reserved_payload_bytes,
                terminal_tail_reserved_events=required_tail_events,
                terminal_tail_reserved_payload_bytes=required_tail_bytes,
            )
            reservation_event_id = _bounded_bookkeeping_event_id(
                "physical_reservation",
                reservation_id,
            )
            business_candidate_bytes, business_wrapper_bytes = _candidate_charge_split(
                prepared_business,
                total_charge=business_charge,
            )
            active = build_frozen_fact(
                ActivePhysicalReservationStateFact,
                schema_version="active_physical_reservation_state.v1",
                reservation_id=reservation_id,
                owner_kind=reservation.owner_kind,
                owner_id=owner_id,
                lifecycle_status="active",
                reservation_fingerprint=reservation.reservation_fingerprint,
                suspension_fingerprint=None,
                reserved_events_total=reservation.reserved_events,
                reserved_payload_bytes_total=reservation.reserved_payload_bytes,
                charged_candidate_events_lifetime=business_charge.event_count,
                charged_candidate_payload_bytes_lifetime=business_candidate_bytes,
                charged_wrapper_bytes_lifetime=business_wrapper_bytes,
                charged_bookkeeping_events_lifetime=reservation_charge.event_count,
                charged_bookkeeping_bytes_lifetime=(
                    reservation_charge.charged_payload_bytes
                ),
                charged_events_lifetime=initial_charge_events,
                charged_payload_bytes_lifetime=initial_charge_bytes,
                remaining_events=(
                    reservation.reserved_events - initial_charge_events
                ),
                remaining_payload_bytes=(
                    reservation.reserved_payload_bytes - initial_charge_bytes
                ),
                latest_reservation_event_id=reservation_event_id,
                latest_lifecycle_event_id=reservation_event_id,
                latest_charge_applied_event_id=None,
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=source.generation,
                ledger_through_sequence=(
                    source.ledger_through_sequence + initial_charge_events
                ),
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through
                    + initial_charge_bytes
                ),
                active_reservations=(*source.active_reservations, active),
                active_checkpoint_barrier=source.active_checkpoint_barrier,
                latest_transition_event_ids=(reservation_event_id,),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role="business_dispatch",
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-transition-contract:v1",
                    "atomic-event-batch+account-row-cas",
                ),
            )
            reservation_event = self._prepare_event(
                PhysicalOperationReservationCreatedEvent(
                    id=reservation_event_id,
                    **context.event_fields(),
                    reservation=reservation,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            # Dispatch occurs only after the whole transaction is confirmed.
            # Keep business fact sequence positions stable and append the
            # reservation proof in the same atomic batch.
            candidate_batch = (*prepared_business, reservation_event)
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedPhysicalReservation(
                reservation=reservation,
                reservation_event=reservation_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    def reserve_and_commit_dispatch_batch(
        self,
        *,
        context: EventContext,
        business_events: Sequence[AgentEvent],
        dispatch_requests: Sequence[PhysicalDispatchReservationRequest],
        one_shot_request: PhysicalOneShotReservationRequest | None = None,
        deadline_monotonic: float | None = None,
    ) -> CommittedPhysicalReservationBatch:
        """Atomically admit independent dispatch owners in one provider-order batch."""

        if not business_events or not dispatch_requests:
            raise ValueError("dispatch batch requires business facts and owners")
        with self._lock:
            source = self._require_state()
            if source.reconciliation_required:
                raise MaterializationAccountContractError(
                    "materialization account requires reconciliation"
                )
            if source.active_checkpoint_barrier is not None:
                raise CheckpointDispatchBarrierActive(
                    "checkpoint barrier rejects new producer admission"
                )
            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            by_id = {event.id: event for event in prepared_business}
            if len(by_id) != len(prepared_business):
                raise ValueError("dispatch batch business event IDs must be unique")
            requests = tuple(dispatch_requests)
            reservation_ids = tuple(item.reservation_id for item in requests)
            owner_keys = tuple(
                (item.burst_contract.operation_kind, item.owner_id) for item in requests
            )
            if (
                len(reservation_ids) != len(set(reservation_ids))
                or len(owner_keys) != len(set(owner_keys))
            ):
                raise ValueError("dispatch batch reservation identities must be unique")
            if any(
                item.burst_contract.operation_kind
                in {
                    PhysicalOperationKind.LEDGER_GENESIS,
                    PhysicalOperationKind.RUNTIME_INTERNAL_WRITE,
                }
                for item in requests
            ):
                raise ValueError("dispatch batch contains a non-dispatch contract")
            active_reservation_ids = {
                item.reservation_id for item in source.active_reservations
            }
            if active_reservation_ids.intersection(reservation_ids):
                raise ValueError("dispatch batch reservation ID is already active")
            if len(source.active_reservations) + len(requests) > (
                self.limits.max_active_materialization_consumers
            ):
                raise PhysicalHeadroomExceeded(
                    "active physical reservation count is exhausted"
                )

            assigned_ids: list[str] = []
            request_events: list[tuple[AgentEvent, ...]] = []
            for item in requests:
                if not item.business_event_ids:
                    raise ValueError("dispatch owner requires assigned business facts")
                try:
                    assigned = tuple(by_id[event_id] for event_id in item.business_event_ids)
                except KeyError as exc:
                    raise ValueError(
                        "dispatch owner references an unknown business event"
                    ) from exc
                assigned_ids.extend(item.business_event_ids)
                request_events.append(assigned)
            one_shot_events: tuple[AgentEvent, ...] = ()
            if one_shot_request is not None:
                if not one_shot_request.business_event_ids:
                    raise ValueError("one-shot batch owner requires business facts")
                if one_shot_request.burst_contract.operation_kind is not (
                    PhysicalOperationKind.RUNTIME_INTERNAL_WRITE
                ):
                    raise ValueError("one-shot dispatch companion must be runtime-internal")
                self._validate_fixed_batch_contract(
                    burst_contract=one_shot_request.burst_contract,
                    business_events=tuple(
                        by_id[event_id]
                        for event_id in one_shot_request.business_event_ids
                    ),
                )
                one_shot_events = tuple(
                    by_id[event_id]
                    for event_id in one_shot_request.business_event_ids
                )
                assigned_ids.extend(one_shot_request.business_event_ids)
            if (
                len(assigned_ids) != len(set(assigned_ids))
                or set(assigned_ids) != set(by_id)
            ):
                raise ValueError(
                    "dispatch batch business facts must be partitioned exactly once"
                )

            reservation_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_CREATED",
                contract=self.charge_contract,
            )
            settlement_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_SETTLED",
                contract=self.charge_contract,
            )
            reservations: list[PhysicalOperationReservationFact] = []
            active_states: list[ActivePhysicalReservationStateFact] = []
            reservation_event_ids: list[str] = []
            for item, assigned in zip(requests, request_events, strict=True):
                business_charge = deterministic_ledger_charge(
                    assigned,
                    contract=self.charge_contract,
                )
                initial_events = business_charge.event_count + reservation_charge.event_count
                initial_bytes = (
                    business_charge.charged_payload_bytes
                    + reservation_charge.charged_payload_bytes
                )
                required_tail_events = (
                    item.burst_contract.terminal_tail_reserved_events
                )
                required_tail_bytes = (
                    item.burst_contract.terminal_tail_reserved_payload_bytes
                )
                if (
                    initial_events + required_tail_events
                    > item.burst_contract.max_total_reserved_events
                    or initial_bytes + required_tail_bytes
                    > item.burst_contract.max_total_reserved_payload_bytes
                ):
                    raise PhysicalHeadroomExceeded(
                        "dispatch owner burst cannot cover proof and terminal tail"
                    )
                reservation = build_frozen_fact(
                    PhysicalOperationReservationFact,
                    schema_version="physical_operation_reservation.v2",
                    reservation_id=item.reservation_id,
                    runtime_session_id=self.runtime_session_id,
                    business_run_id=item.business_run_id,
                    business_window_id=item.business_window_id,
                    business_window_generation=item.business_window_generation,
                    owner_kind=item.burst_contract.operation_kind,
                    owner_id=item.owner_id,
                    ledger_materialization_generation=(
                        source.generation.ledger_materialization_generation
                    ),
                    consumer_horizon_revision=(
                        source.generation.consumer_horizon_revision
                    ),
                    source_ledger_through_sequence=source.ledger_through_sequence,
                    burst_contract_id=item.burst_contract.contract_id,
                    burst_contract_version=item.burst_contract.contract_version,
                    burst_contract_fingerprint=item.burst_contract.contract_fingerprint,
                    physical_charge_contract_fingerprint=(
                        self.charge_contract.contract_fingerprint
                    ),
                    reserved_events=item.burst_contract.max_total_reserved_events,
                    reserved_payload_bytes=(
                        item.burst_contract.max_total_reserved_payload_bytes
                    ),
                    terminal_tail_reserved_events=required_tail_events,
                    terminal_tail_reserved_payload_bytes=required_tail_bytes,
                )
                reservation_event_id = _bounded_bookkeeping_event_id(
                    "physical_reservation",
                    item.reservation_id,
                )
                candidate_bytes, wrapper_bytes = _candidate_charge_split(
                    assigned,
                    total_charge=business_charge,
                )
                active = build_frozen_fact(
                    ActivePhysicalReservationStateFact,
                    schema_version="active_physical_reservation_state.v1",
                    reservation_id=item.reservation_id,
                    owner_kind=reservation.owner_kind,
                    owner_id=item.owner_id,
                    lifecycle_status="active",
                    reservation_fingerprint=reservation.reservation_fingerprint,
                    suspension_fingerprint=None,
                    reserved_events_total=reservation.reserved_events,
                    reserved_payload_bytes_total=reservation.reserved_payload_bytes,
                    charged_candidate_events_lifetime=business_charge.event_count,
                    charged_candidate_payload_bytes_lifetime=candidate_bytes,
                    charged_wrapper_bytes_lifetime=wrapper_bytes,
                    charged_bookkeeping_events_lifetime=reservation_charge.event_count,
                    charged_bookkeeping_bytes_lifetime=(
                        reservation_charge.charged_payload_bytes
                    ),
                    charged_events_lifetime=initial_events,
                    charged_payload_bytes_lifetime=initial_bytes,
                    remaining_events=reservation.reserved_events - initial_events,
                    remaining_payload_bytes=(
                        reservation.reserved_payload_bytes - initial_bytes
                    ),
                    latest_reservation_event_id=reservation_event_id,
                    latest_lifecycle_event_id=reservation_event_id,
                    latest_charge_applied_event_id=None,
                )
                reservations.append(reservation)
                active_states.append(active)
                reservation_event_ids.append(reservation_event_id)

            one_shot_reservation: PhysicalOperationReservationFact | None = None
            one_shot_predecessor: ActivePhysicalReservationStateFact | None = None
            one_shot_settlement_id: str | None = None
            if one_shot_request is not None:
                one_shot_business_charge = deterministic_ledger_charge(
                    one_shot_events,
                    contract=self.charge_contract,
                )
                one_shot_total_events = (
                    one_shot_business_charge.event_count
                    + reservation_charge.event_count
                    + settlement_charge.event_count
                )
                one_shot_total_bytes = (
                    one_shot_business_charge.charged_payload_bytes
                    + reservation_charge.charged_payload_bytes
                    + settlement_charge.charged_payload_bytes
                )
                if (
                    one_shot_total_events
                    > one_shot_request.burst_contract.max_total_reserved_events
                    or one_shot_total_bytes
                    > one_shot_request.burst_contract.max_total_reserved_payload_bytes
                ):
                    raise PhysicalHeadroomExceeded(
                        "one-shot dispatch companion exceeds its contract"
                    )
                one_shot_reservation = build_frozen_fact(
                    PhysicalOperationReservationFact,
                    schema_version="physical_operation_reservation.v2",
                    reservation_id=one_shot_request.reservation_id,
                    runtime_session_id=self.runtime_session_id,
                    business_run_id=None,
                    business_window_id=None,
                    business_window_generation=None,
                    owner_kind=one_shot_request.burst_contract.operation_kind,
                    owner_id=one_shot_request.owner_id,
                    ledger_materialization_generation=(
                        source.generation.ledger_materialization_generation
                    ),
                    consumer_horizon_revision=(
                        source.generation.consumer_horizon_revision
                    ),
                    source_ledger_through_sequence=source.ledger_through_sequence,
                    burst_contract_id=one_shot_request.burst_contract.contract_id,
                    burst_contract_version=(
                        one_shot_request.burst_contract.contract_version
                    ),
                    burst_contract_fingerprint=(
                        one_shot_request.burst_contract.contract_fingerprint
                    ),
                    physical_charge_contract_fingerprint=(
                        self.charge_contract.contract_fingerprint
                    ),
                    reserved_events=one_shot_total_events,
                    reserved_payload_bytes=one_shot_total_bytes,
                    terminal_tail_reserved_events=settlement_charge.event_count,
                    terminal_tail_reserved_payload_bytes=(
                        settlement_charge.charged_payload_bytes
                    ),
                )
                one_shot_reservation_id = (
                    f"physical_reservation:{one_shot_request.reservation_id}"
                )
                candidate_bytes, wrapper_bytes = _candidate_charge_split(
                    one_shot_events,
                    total_charge=one_shot_business_charge,
                )
                one_shot_predecessor = build_frozen_fact(
                    ActivePhysicalReservationStateFact,
                    schema_version="active_physical_reservation_state.v1",
                    reservation_id=one_shot_request.reservation_id,
                    owner_kind=one_shot_reservation.owner_kind,
                    owner_id=one_shot_request.owner_id,
                    lifecycle_status="active",
                    reservation_fingerprint=(
                        one_shot_reservation.reservation_fingerprint
                    ),
                    suspension_fingerprint=None,
                    reserved_events_total=one_shot_reservation.reserved_events,
                    reserved_payload_bytes_total=(
                        one_shot_reservation.reserved_payload_bytes
                    ),
                    charged_candidate_events_lifetime=(
                        one_shot_business_charge.event_count
                    ),
                    charged_candidate_payload_bytes_lifetime=candidate_bytes,
                    charged_wrapper_bytes_lifetime=wrapper_bytes,
                    charged_bookkeeping_events_lifetime=(
                        reservation_charge.event_count
                    ),
                    charged_bookkeeping_bytes_lifetime=(
                        reservation_charge.charged_payload_bytes
                    ),
                    charged_events_lifetime=(
                        one_shot_business_charge.event_count
                        + reservation_charge.event_count
                    ),
                    charged_payload_bytes_lifetime=(
                        one_shot_business_charge.charged_payload_bytes
                        + reservation_charge.charged_payload_bytes
                    ),
                    remaining_events=settlement_charge.event_count,
                    remaining_payload_bytes=(
                        settlement_charge.charged_payload_bytes
                    ),
                    latest_reservation_event_id=one_shot_reservation_id,
                    latest_lifecycle_event_id=one_shot_reservation_id,
                    latest_charge_applied_event_id=None,
                )
                reservation_event_ids.append(one_shot_reservation_id)
                one_shot_settlement_id = (
                    f"physical_settlement:{one_shot_request.reservation_id}"
                )

            global_business_charge = deterministic_ledger_charge(
                prepared_business,
                contract=self.charge_contract,
            )
            bookkeeping_events = len(reservation_event_ids) + (
                1 if one_shot_settlement_id is not None else 0
            )
            bookkeeping_bytes = (
                len(reservation_event_ids) * reservation_charge.charged_payload_bytes
                + (
                    settlement_charge.charged_payload_bytes
                    if one_shot_settlement_id is not None
                    else 0
                )
            )
            actual_events = global_business_charge.event_count + bookkeeping_events
            actual_bytes = global_business_charge.charged_payload_bytes + bookkeeping_bytes
            new_reserved_events = sum(
                item.burst_contract.max_total_reserved_events for item in requests
            ) + (
                one_shot_reservation.reserved_events
                if one_shot_reservation is not None
                else 0
            )
            new_reserved_bytes = sum(
                item.burst_contract.max_total_reserved_payload_bytes
                for item in requests
            ) + (
                one_shot_reservation.reserved_payload_bytes
                if one_shot_reservation is not None
                else 0
            )
            active_reserved_events = sum(
                item.remaining_events for item in source.active_reservations
            )
            active_reserved_bytes = sum(
                item.remaining_payload_bytes for item in source.active_reservations
            )
            if (
                source.used_since_reclaimable_events
                + active_reserved_events
                + new_reserved_events
                > self.limits.max_unreclaimable_ledger_events
                or source.used_since_reclaimable_payload_bytes
                + active_reserved_bytes
                + new_reserved_bytes
                > self.limits.max_unreclaimable_charged_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "physical event ledger headroom is exhausted"
                )
            latest_ids = (
                *reservation_event_ids,
                *((one_shot_settlement_id,) if one_shot_settlement_id else ()),
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=source.generation,
                ledger_through_sequence=source.ledger_through_sequence + actual_events,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through + actual_bytes
                ),
                active_reservations=(*source.active_reservations, *active_states),
                active_checkpoint_barrier=source.active_checkpoint_barrier,
                latest_transition_event_ids=latest_ids,
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            one_shot_ids = set(
                one_shot_request.business_event_ids
                if one_shot_request is not None
                else ()
            )
            causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role=(
                        "business_terminal"
                        if event.id in one_shot_ids
                        else "business_dispatch"
                    ),
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-transition-contract:v1",
                    "atomic-event-batch+account-row-cas",
                ),
            )
            reservation_events = tuple(
                self._prepare_event(
                    PhysicalOperationReservationCreatedEvent(
                        id=event_id,
                        **context.event_fields(),
                        reservation=reservation,
                        transition=transition,
                        resulting_account_state_fingerprint=(
                            resulting.account_state_fingerprint
                        ),
                    )
                )
                for event_id, reservation in zip(
                    reservation_event_ids,
                    (*reservations, *((one_shot_reservation,) if one_shot_reservation else ())),
                    strict=True,
                )
            )
            one_shot_settlement_event = None
            if one_shot_request is not None:
                assert one_shot_reservation is not None
                assert one_shot_predecessor is not None
                assert one_shot_settlement_id is not None
                one_shot_charge = deterministic_ledger_charge(
                    one_shot_events,
                    contract=self.charge_contract,
                )
                candidate_bytes, wrapper_bytes = _candidate_charge_split(
                    one_shot_events,
                    total_charge=one_shot_charge,
                )
                settlement = build_frozen_fact(
                    PhysicalOperationSettlementFact,
                    schema_version="physical_operation_settlement.v2",
                    reservation_id=one_shot_request.reservation_id,
                    runtime_session_id=self.runtime_session_id,
                    business_run_id=None,
                    business_window_id=None,
                    business_window_generation=None,
                    ledger_materialization_generation=(
                        source.generation.ledger_materialization_generation
                    ),
                    consumer_horizon_revision=(
                        source.generation.consumer_horizon_revision
                    ),
                    owner_kind=one_shot_reservation.owner_kind,
                    owner_id=one_shot_request.owner_id,
                    reservation_fingerprint=(
                        one_shot_reservation.reservation_fingerprint
                    ),
                    predecessor_status="active",
                    predecessor_lifecycle_event_id=(
                        one_shot_predecessor.latest_lifecycle_event_id
                    ),
                    predecessor_reservation_state_fingerprint=(
                        one_shot_predecessor.state_fingerprint
                    ),
                    burst_contract_fingerprint=(
                        one_shot_reservation.burst_contract_fingerprint
                    ),
                    physical_charge_contract_fingerprint=(
                        self.charge_contract.contract_fingerprint
                    ),
                    predecessor_remaining_events=(
                        one_shot_predecessor.remaining_events
                    ),
                    predecessor_remaining_payload_bytes=(
                        one_shot_predecessor.remaining_payload_bytes
                    ),
                    terminal_batch_charge_before_settlement_events=0,
                    terminal_batch_charge_before_settlement_payload_bytes=0,
                    settlement_event_charge_events=settlement_charge.event_count,
                    settlement_event_charge_payload_bytes=(
                        settlement_charge.charged_payload_bytes
                    ),
                    charged_candidate_events=one_shot_charge.event_count,
                    charged_candidate_payload_bytes=candidate_bytes,
                    charged_wrapper_bytes=wrapper_bytes,
                    charged_bookkeeping_events=(
                        reservation_charge.event_count + settlement_charge.event_count
                    ),
                    charged_bookkeeping_bytes=(
                        reservation_charge.charged_payload_bytes
                        + settlement_charge.charged_payload_bytes
                    ),
                    total_charged_events=one_shot_reservation.reserved_events,
                    total_charged_payload_bytes=(
                        one_shot_reservation.reserved_payload_bytes
                    ),
                    terminal_outcome=one_shot_request.terminal_outcome,
                    released_on_suspension_events_lifetime=0,
                    released_on_suspension_payload_bytes_lifetime=0,
                    released_on_settlement_events=0,
                    released_on_settlement_payload_bytes=0,
                    resulting_reservation_state_fingerprint=context_fingerprint(
                        "settled-physical-reservation-state:v1",
                        one_shot_reservation.reservation_fingerprint,
                    ),
                )
                one_shot_settlement_event = self._prepare_event(
                    PhysicalOperationReservationSettledEvent(
                        id=one_shot_settlement_id,
                        **context.event_fields(),
                        settlement=settlement,
                        transition=transition,
                        resulting_account_state_fingerprint=(
                            resulting.account_state_fingerprint
                        ),
                    )
                )
            candidate_batch = (
                *prepared_business,
                *reservation_events,
                *((one_shot_settlement_event,) if one_shot_settlement_event else ()),
            )
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedPhysicalReservationBatch(
                reservations=tuple(reservations),
                reservation_events=reservation_events[: len(reservations)],
                one_shot_reservation=one_shot_reservation,
                one_shot_settlement_event=one_shot_settlement_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    def available_dispatch_capacity(
        self,
        *,
        burst_contract: PhysicalBurstContractFact,
    ) -> int:
        """Return the exact number of additional full reservations admissible now.

        This is advisory for batching only. The subsequent reservation commit
        repeats the same calculation under the coordinator lock and remains the
        linearization point.
        """

        with self._lock:
            source = self._require_state()
            if (
                source.reconciliation_required
                or source.active_checkpoint_barrier is not None
            ):
                return 0
            active_reserved_events = sum(
                item.remaining_events for item in source.active_reservations
            )
            active_reserved_bytes = sum(
                item.remaining_payload_bytes for item in source.active_reservations
            )
            event_headroom = max(
                0,
                self.limits.max_unreclaimable_ledger_events
                - source.used_since_reclaimable_events
                - active_reserved_events,
            )
            byte_headroom = max(
                0,
                self.limits.max_unreclaimable_charged_payload_bytes
                - source.used_since_reclaimable_payload_bytes
                - active_reserved_bytes,
            )
            owner_headroom = max(
                0,
                self.limits.max_active_physical_reservations
                - len(source.active_reservations),
            )
            return min(
                owner_headroom,
                event_headroom // burst_contract.max_total_reserved_events,
                byte_headroom // burst_contract.max_total_reserved_payload_bytes,
            )

    def commit_reserved_charge(
        self,
        *,
        context: EventContext,
        reservation: PhysicalOperationReservationFact,
        business_events: Sequence[AgentEvent],
        deadline_monotonic: float | None = None,
    ) -> CommittedPhysicalCharge:
        """Charge one finite non-terminal batch to an active reservation."""

        if not business_events:
            raise ValueError("reserved charge requires business facts")
        with self._lock:
            source = self._require_state()
            active = self._require_active_reservation(source, reservation)
            if active.lifecycle_status != "active":
                raise MaterializationAccountContractError(
                    "only an active reservation can accept business charges"
                )
            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            business_charge = deterministic_ledger_charge(
                prepared_business,
                contract=self.charge_contract,
            )
            charge_event_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_CHARGE_APPLIED",
                contract=self.charge_contract,
                business_event_count=len(prepared_business),
            )
            charged_events = business_charge.event_count + charge_event_charge.event_count
            charged_bytes = (
                business_charge.charged_payload_bytes
                + charge_event_charge.charged_payload_bytes
            )
            if (
                charged_events > active.remaining_events
                or charged_bytes > active.remaining_payload_bytes
                or active.remaining_events - charged_events
                < reservation.terminal_tail_reserved_events
                or active.remaining_payload_bytes - charged_bytes
                < reservation.terminal_tail_reserved_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "business charge would consume the reserved terminal tail"
                )
            candidate_bytes, wrapper_bytes = _candidate_charge_split(
                prepared_business,
                total_charge=business_charge,
            )
            charge_identity = context_fingerprint(
                "physical-operation-charge-event-id:v1",
                (
                    reservation.reservation_id,
                    active.latest_lifecycle_event_id,
                    tuple(event.id for event in prepared_business),
                ),
            ).removeprefix("sha256:")
            charge_event_id = _bounded_bookkeeping_event_id(
                "physical_charge",
                charge_identity,
            )
            resulting_active = build_frozen_fact(
                ActivePhysicalReservationStateFact,
                schema_version="active_physical_reservation_state.v1",
                reservation_id=active.reservation_id,
                owner_kind=active.owner_kind,
                owner_id=active.owner_id,
                lifecycle_status="active",
                reservation_fingerprint=active.reservation_fingerprint,
                suspension_fingerprint=None,
                reserved_events_total=active.reserved_events_total,
                reserved_payload_bytes_total=active.reserved_payload_bytes_total,
                charged_candidate_events_lifetime=(
                    active.charged_candidate_events_lifetime
                    + business_charge.event_count
                ),
                charged_candidate_payload_bytes_lifetime=(
                    active.charged_candidate_payload_bytes_lifetime
                    + candidate_bytes
                ),
                charged_wrapper_bytes_lifetime=(
                    active.charged_wrapper_bytes_lifetime + wrapper_bytes
                ),
                charged_bookkeeping_events_lifetime=(
                    active.charged_bookkeeping_events_lifetime
                    + charge_event_charge.event_count
                ),
                charged_bookkeeping_bytes_lifetime=(
                    active.charged_bookkeeping_bytes_lifetime
                    + charge_event_charge.charged_payload_bytes
                ),
                charged_events_lifetime=active.charged_events_lifetime + charged_events,
                charged_payload_bytes_lifetime=(
                    active.charged_payload_bytes_lifetime + charged_bytes
                ),
                remaining_events=active.remaining_events - charged_events,
                remaining_payload_bytes=active.remaining_payload_bytes - charged_bytes,
                latest_reservation_event_id=active.latest_reservation_event_id,
                latest_lifecycle_event_id=charge_event_id,
                latest_charge_applied_event_id=charge_event_id,
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=source.generation,
                ledger_through_sequence=source.ledger_through_sequence + charged_events,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through + charged_bytes
                ),
                active_reservations=(
                    *(
                        item
                        for item in source.active_reservations
                        if item.reservation_id != reservation.reservation_id
                    ),
                    resulting_active,
                ),
                active_checkpoint_barrier=source.active_checkpoint_barrier,
                latest_transition_event_ids=(charge_event_id,),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role="business_charge",
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-transition-contract:v1",
                    "atomic-event-batch+account-row-cas",
                ),
            )
            charge = build_frozen_fact(
                PhysicalOperationChargeAppliedFact,
                schema_version="physical_operation_charge_applied.v1",
                reservation_id=reservation.reservation_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                runtime_session_id=self.runtime_session_id,
                owner_kind=reservation.owner_kind,
                owner_id=reservation.owner_id,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                predecessor_reservation_state_fingerprint=active.state_fingerprint,
                charged_business_event_identities=tuple(
                    sorted(
                        (
                            stable_event_identity(
                                event,
                                runtime_session_id=self.runtime_session_id,
                            )
                            for event in prepared_business
                        ),
                        key=lambda item: (item.runtime_session_id, item.event_id),
                    )
                ),
                business_candidate_charge_events=business_charge.event_count,
                business_candidate_charge_payload_bytes=candidate_bytes,
                business_wrapper_charge_payload_bytes=wrapper_bytes,
                charge_applied_event_charge_events=charge_event_charge.event_count,
                charge_applied_event_charge_payload_bytes=(
                    charge_event_charge.charged_payload_bytes
                ),
                remaining_before_events=active.remaining_events,
                remaining_before_payload_bytes=active.remaining_payload_bytes,
                remaining_after_events=resulting_active.remaining_events,
                remaining_after_payload_bytes=resulting_active.remaining_payload_bytes,
                resulting_reservation_state_fingerprint=(
                    resulting_active.state_fingerprint
                ),
            )
            charge_event = self._prepare_event(
                PhysicalOperationChargeAppliedEvent(
                    id=charge_event_id,
                    **context.event_fields(),
                    charge=charge,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            candidate_batch = (*prepared_business, charge_event)
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedPhysicalCharge(
                charge_event=charge_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_reservation_state=resulting_active,
                resulting_account_state=resulting,
            )

    def commit_reserved_settlement(
        self,
        *,
        context: EventContext,
        reservation: PhysicalOperationReservationFact,
        business_events: Sequence[AgentEvent],
        terminal_outcome: str,
        model_stream_measurement_fingerprint: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> CommittedPhysicalSettlement:
        """Commit a stable terminal batch and close its exact reservation."""

        if not business_events:
            raise ValueError("physical settlement requires terminal business facts")
        with self._lock:
            source = self._require_state()
            active = self._require_active_reservation(source, reservation)
            if active.lifecycle_status not in {"active", "suspended_tail"}:
                raise MaterializationAccountContractError(
                    "reconciliation reservation cannot be settled live"
                )
            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            business_charge = deterministic_ledger_charge(
                prepared_business,
                contract=self.charge_contract,
            )
            settlement_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_SETTLED",
                contract=self.charge_contract,
            )
            terminal_events = business_charge.event_count + settlement_charge.event_count
            terminal_bytes = (
                business_charge.charged_payload_bytes
                + settlement_charge.charged_payload_bytes
            )
            if (
                terminal_events > active.remaining_events
                or terminal_bytes > active.remaining_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "terminal batch exceeds its retained physical reservation"
                )
            settlement_event_id = _bounded_bookkeeping_event_id(
                "physical_settlement",
                reservation.reservation_id,
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=source.generation,
                ledger_through_sequence=source.ledger_through_sequence + terminal_events,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through + terminal_bytes
                ),
                active_reservations=tuple(
                    item
                    for item in source.active_reservations
                    if item.reservation_id != reservation.reservation_id
                ),
                active_checkpoint_barrier=source.active_checkpoint_barrier,
                latest_transition_event_ids=(settlement_event_id,),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role="business_terminal",
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-transition-contract:v1",
                    "atomic-event-batch+account-row-cas",
                ),
            )
            candidate_bytes, wrapper_bytes = _candidate_charge_split(
                prepared_business,
                total_charge=business_charge,
            )
            charged_candidate_events = (
                active.charged_candidate_events_lifetime
                + business_charge.event_count
            )
            charged_candidate_bytes = (
                active.charged_candidate_payload_bytes_lifetime + candidate_bytes
            )
            charged_wrapper_bytes = active.charged_wrapper_bytes_lifetime + wrapper_bytes
            charged_bookkeeping_events = (
                active.charged_bookkeeping_events_lifetime
                + settlement_charge.event_count
            )
            charged_bookkeeping_bytes = (
                active.charged_bookkeeping_bytes_lifetime
                + settlement_charge.charged_payload_bytes
            )
            total_charged_events = charged_candidate_events + charged_bookkeeping_events
            total_charged_bytes = (
                charged_candidate_bytes
                + charged_wrapper_bytes
                + charged_bookkeeping_bytes
            )
            released_on_suspension_events = (
                reservation.reserved_events
                - active.charged_events_lifetime
                - active.remaining_events
            )
            released_on_suspension_bytes = (
                reservation.reserved_payload_bytes
                - active.charged_payload_bytes_lifetime
                - active.remaining_payload_bytes
            )
            settlement = build_frozen_fact(
                PhysicalOperationSettlementFact,
                schema_version="physical_operation_settlement.v2",
                reservation_id=reservation.reservation_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=reservation.business_run_id,
                business_window_id=reservation.business_window_id,
                business_window_generation=reservation.business_window_generation,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                owner_kind=reservation.owner_kind,
                owner_id=reservation.owner_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                predecessor_status=(
                    "suspended_tail"
                    if active.lifecycle_status == "suspended_tail"
                    else "active"
                ),
                predecessor_lifecycle_event_id=active.latest_lifecycle_event_id,
                predecessor_reservation_state_fingerprint=active.state_fingerprint,
                burst_contract_fingerprint=reservation.burst_contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                predecessor_remaining_events=active.remaining_events,
                predecessor_remaining_payload_bytes=active.remaining_payload_bytes,
                terminal_batch_charge_before_settlement_events=(
                    business_charge.event_count
                ),
                terminal_batch_charge_before_settlement_payload_bytes=(
                    business_charge.charged_payload_bytes
                ),
                settlement_event_charge_events=settlement_charge.event_count,
                settlement_event_charge_payload_bytes=(
                    settlement_charge.charged_payload_bytes
                ),
                charged_candidate_events=charged_candidate_events,
                charged_candidate_payload_bytes=charged_candidate_bytes,
                charged_wrapper_bytes=charged_wrapper_bytes,
                charged_bookkeeping_events=charged_bookkeeping_events,
                charged_bookkeeping_bytes=charged_bookkeeping_bytes,
                total_charged_events=total_charged_events,
                total_charged_payload_bytes=total_charged_bytes,
                terminal_outcome=terminal_outcome,
                model_stream_measurement_fingerprint=(
                    model_stream_measurement_fingerprint
                ),
                released_on_suspension_events_lifetime=(
                    released_on_suspension_events
                ),
                released_on_suspension_payload_bytes_lifetime=(
                    released_on_suspension_bytes
                ),
                released_on_settlement_events=(
                    active.remaining_events
                    - business_charge.event_count
                    - settlement_charge.event_count
                ),
                released_on_settlement_payload_bytes=(
                    active.remaining_payload_bytes
                    - business_charge.charged_payload_bytes
                    - settlement_charge.charged_payload_bytes
                ),
                resulting_reservation_state_fingerprint=context_fingerprint(
                    "settled-physical-reservation-state:v1",
                    reservation.reservation_fingerprint,
                ),
            )
            settlement_event = self._prepare_event(
                PhysicalOperationReservationSettledEvent(
                    id=settlement_event_id,
                    **context.event_fields(),
                    settlement=settlement,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            candidate_batch = (*prepared_business, settlement_event)
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedPhysicalSettlement(
                settlement_event=settlement_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    def commit_reserved_suspension(
        self,
        *,
        context: EventContext,
        reservation: PhysicalOperationReservationFact,
        business_events: Sequence[AgentEvent],
        suspension_id: str,
        binding_identity_fingerprint: str,
        deadline_monotonic: float | None = None,
    ) -> CommittedPhysicalSuspension:
        """Commit a suspension fact and retain only its exact recovery tail."""

        if not business_events:
            raise ValueError("physical suspension requires business facts")
        with self._lock:
            source = self._require_state()
            active = self._require_active_reservation(source, reservation)
            if active.lifecycle_status != "active":
                raise MaterializationAccountContractError(
                    "only an active physical reservation can suspend"
                )
            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            business_charge = deterministic_ledger_charge(
                prepared_business,
                contract=self.charge_contract,
            )
            suspension_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_SUSPENDED",
                contract=self.charge_contract,
            )
            retained_events = reservation.terminal_tail_reserved_events
            retained_bytes = reservation.terminal_tail_reserved_payload_bytes
            required_events = (
                business_charge.event_count
                + suspension_charge.event_count
                + retained_events
            )
            required_bytes = (
                business_charge.charged_payload_bytes
                + suspension_charge.charged_payload_bytes
                + retained_bytes
            )
            if (
                required_events > active.remaining_events
                or required_bytes > active.remaining_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "physical suspension cannot preserve its terminal tail"
                )
            suspension_identity = context_fingerprint(
                "physical-suspension-identity:v1",
                (reservation.reservation_id, suspension_id),
            ).removeprefix("sha256:")
            durable_suspension_id = _bounded_bookkeeping_event_id(
                "suspension",
                suspension_identity,
            )
            suspension_event_id = _bounded_bookkeeping_event_id(
                "physical_suspension",
                suspension_identity,
            )
            candidate_bytes, wrapper_bytes = _candidate_charge_split(
                prepared_business,
                total_charge=business_charge,
            )
            released_events = active.remaining_events - required_events
            released_bytes = active.remaining_payload_bytes - required_bytes
            charged_candidate_events = (
                active.charged_candidate_events_lifetime
                + business_charge.event_count
            )
            charged_candidate_bytes = (
                active.charged_candidate_payload_bytes_lifetime + candidate_bytes
            )
            charged_wrapper_bytes = active.charged_wrapper_bytes_lifetime + wrapper_bytes
            charged_bookkeeping_events = (
                active.charged_bookkeeping_events_lifetime
                + suspension_charge.event_count
            )
            charged_bookkeeping_bytes = (
                active.charged_bookkeeping_bytes_lifetime
                + suspension_charge.charged_payload_bytes
            )
            resulting_active_payload = {
                "schema_version": "active_physical_reservation_state.v1",
                "reservation_id": reservation.reservation_id,
                "owner_kind": reservation.owner_kind,
                "owner_id": reservation.owner_id,
                "lifecycle_status": "suspended_tail",
                "reservation_fingerprint": reservation.reservation_fingerprint,
                "reserved_events_total": active.reserved_events_total,
                "reserved_payload_bytes_total": active.reserved_payload_bytes_total,
                "charged_candidate_events_lifetime": charged_candidate_events,
                "charged_candidate_payload_bytes_lifetime": charged_candidate_bytes,
                "charged_wrapper_bytes_lifetime": charged_wrapper_bytes,
                "charged_bookkeeping_events_lifetime": charged_bookkeeping_events,
                "charged_bookkeeping_bytes_lifetime": charged_bookkeeping_bytes,
                "charged_events_lifetime": (
                    charged_candidate_events + charged_bookkeeping_events
                ),
                "charged_payload_bytes_lifetime": (
                    charged_candidate_bytes
                    + charged_wrapper_bytes
                    + charged_bookkeeping_bytes
                ),
                "remaining_events": retained_events,
                "remaining_payload_bytes": retained_bytes,
                "latest_reservation_event_id": active.latest_reservation_event_id,
                "latest_lifecycle_event_id": suspension_event_id,
                "latest_charge_applied_event_id": active.latest_charge_applied_event_id,
            }
            suspension_chain_fingerprint = context_fingerprint(
                "physical-operation-suspension-tail:v2",
                {
                    "reservation_fingerprint": reservation.reservation_fingerprint,
                    "durable_suspension_id": durable_suspension_id,
                    "binding_identity_fingerprint": binding_identity_fingerprint,
                    "predecessor_state_fingerprint": active.state_fingerprint,
                    "retained_events": retained_events,
                    "retained_payload_bytes": retained_bytes,
                },
            )
            resulting_active = build_frozen_fact(
                ActivePhysicalReservationStateFact,
                **resulting_active_payload,
                suspension_fingerprint=suspension_chain_fingerprint,
            )
            actual_events = business_charge.event_count + suspension_charge.event_count
            actual_bytes = (
                business_charge.charged_payload_bytes
                + suspension_charge.charged_payload_bytes
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=source.generation,
                ledger_through_sequence=source.ledger_through_sequence + actual_events,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through + actual_bytes
                ),
                active_reservations=tuple(
                    resulting_active
                    if item.reservation_id == reservation.reservation_id
                    else item
                    for item in source.active_reservations
                ),
                active_checkpoint_barrier=source.active_checkpoint_barrier,
                latest_transition_event_ids=(suspension_event_id,),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role="business_charge",
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-transition-contract:v1",
                    "atomic-event-batch+account-row-cas",
                ),
            )
            suspension = build_frozen_fact(
                PhysicalOperationSuspensionTailFact,
                schema_version="physical_operation_suspension_tail.v2",
                reservation_id=reservation.reservation_id,
                suspension_id=durable_suspension_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=reservation.business_run_id,
                business_window_id=reservation.business_window_id,
                business_window_generation=reservation.business_window_generation,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                owner_kind=reservation.owner_kind,
                owner_id=reservation.owner_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                burst_contract_fingerprint=reservation.burst_contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                predecessor_lifecycle_event_id=active.latest_lifecycle_event_id,
                predecessor_reservation_state_fingerprint=active.state_fingerprint,
                binding_identity_fingerprint=binding_identity_fingerprint,
                remaining_before_suspension_events=active.remaining_events,
                remaining_before_suspension_payload_bytes=active.remaining_payload_bytes,
                suspension_event_charge_events=(
                    business_charge.event_count + suspension_charge.event_count
                ),
                suspension_event_charge_payload_bytes=(
                    business_charge.charged_payload_bytes
                    + suspension_charge.charged_payload_bytes
                ),
                released_on_suspension_events=released_events,
                released_on_suspension_payload_bytes=released_bytes,
                retained_tail_after_suspension_events=retained_events,
                retained_tail_after_suspension_payload_bytes=retained_bytes,
                resulting_reservation_state_fingerprint=(
                    resulting_active.state_fingerprint
                ),
            )
            suspension_event = self._prepare_event(
                PhysicalOperationReservationSuspendedEvent(
                    id=suspension_event_id,
                    **context.event_fields(),
                    suspension=suspension,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            candidate_batch = (*prepared_business, suspension_event)
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedPhysicalSuspension(
                suspension_event=suspension_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_reservation_state=resulting_active,
                resulting_account_state=resulting,
            )

    def commit_run_seed_consumer_rotation(
        self,
        *,
        context: EventContext,
        business_events: Sequence[AgentEvent],
        run_start_event_id: str,
        seed_semantic_fingerprint: str,
        seed_reference_fingerprint: str,
        seed_source_through_sequence: int,
        seed_source_ledger_continuity_accumulator: str,
        reservation_id: str,
        owner_id: str,
        burst_contract: PhysicalBurstContractFact,
        deadline_monotonic: float | None = None,
    ) -> CommittedRunSeedConsumerRotation:
        """Atomically rotate the run-scoped transcript consumer with RunStart."""

        with self._lock:
            source = self._require_state()
            if source.reconciliation_required:
                raise MaterializationAccountContractError(
                    "materialization account requires reconciliation"
                )
            if source.active_checkpoint_barrier is not None:
                raise CheckpointDispatchBarrierActive(
                    "checkpoint barrier rejects run-seed consumer rotation"
                )
            if source.active_reservations:
                raise MaterializationAccountContractError(
                    "run-seed consumer rotation requires a drained ledger"
                )
            if seed_source_through_sequence != source.ledger_through_sequence:
                raise RunSeedSourceStale(
                    "run seed source does not match the account high-water"
                )
            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            run_starts = tuple(
                event
                for event in prepared_business
                if event.id == run_start_event_id
                and isinstance(event, RunStartEvent)
            )
            if len(run_starts) != 1:
                raise ValueError("run-seed rotation requires one exact RunStart")
            run_start = run_starts[0]
            self._validate_fixed_batch_contract(
                burst_contract=burst_contract,
                business_events=prepared_business,
            )

            existing_transcript = tuple(
                item
                for item in source.generation.consumer_horizons
                if item.consumer_kind
                is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
            )
            if len(existing_transcript) != 1:
                raise MaterializationAccountContractError(
                    "run-seed rotation requires one active transcript consumer"
                )
            successor_id = _bounded_bookkeeping_event_id(
                "transcript_seed_consumer",
                context.run_id,
            )
            if any(
                item.consumer_id == successor_id
                for item in source.generation.consumer_horizons
            ):
                raise ValueError("run transcript consumer already exists")
            successor = build_frozen_fact(
                LedgerMaterializationConsumerHorizonFact,
                schema_version="ledger_materialization_consumer_horizon.v1",
                runtime_session_id=self.runtime_session_id,
                consumer_kind=LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW,
                consumer_id=successor_id,
                business_run_id=context.run_id,
                business_window_id="seed",
                business_window_generation=0,
                through_sequence=seed_source_through_sequence,
                ledger_event_count_through=seed_source_through_sequence,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through
                ),
                ledger_continuity_accumulator=(
                    seed_source_ledger_continuity_accumulator
                ),
                consumer_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-consumer-contract:v1",
                    {
                        "kind": LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW.value,
                        "consumer_id": successor_id,
                    },
                ),
            )
            retained = tuple(
                item
                for item in source.generation.consumer_horizons
                if item.consumer_kind
                is not LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
            )
            horizons = tuple(
                sorted(
                    (*retained, successor),
                    key=lambda item: (item.consumer_kind.value, item.consumer_id),
                )
            )
            provisional_generation = build_generation(
                source=source.generation,
                consumer_horizons=horizons,
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision + 1
                ),
            )
            minimum_advanced = (
                provisional_generation.reclaimable_through_sequence
                > source.generation.reclaimable_through_sequence
            )
            resulting_generation = (
                build_generation(
                    source=source.generation,
                    consumer_horizons=horizons,
                    materialization_generation=(
                        source.generation.ledger_materialization_generation + 1
                    ),
                    consumer_horizon_revision=(
                        source.generation.consumer_horizon_revision + 1
                    ),
                )
                if minimum_advanced
                else provisional_generation
            )

            business_charge = deterministic_ledger_charge(
                prepared_business, contract=self.charge_contract
            )
            bookkeeping_types = [
                "LEDGER_MATERIALIZATION_CONSUMER_REGISTERED",
                *(
                    "LEDGER_MATERIALIZATION_CONSUMER_RETIRED"
                    for _ in existing_transcript
                ),
                "PHYSICAL_OPERATION_RESERVATION_CREATED",
                "PHYSICAL_OPERATION_RESERVATION_SETTLED",
            ]
            if minimum_advanced:
                bookkeeping_types.append(
                    "LEDGER_MATERIALIZATION_GENERATION_ADVANCED"
                )
            bookkeeping_charges = tuple(
                deterministic_bookkeeping_charge(
                    event_type, contract=self.charge_contract
                )
                for event_type in bookkeeping_types
            )
            total_events = business_charge.event_count + sum(
                item.event_count for item in bookkeeping_charges
            )
            total_bytes = business_charge.charged_payload_bytes + sum(
                item.charged_payload_bytes for item in bookkeeping_charges
            )
            if (
                total_events > burst_contract.max_total_reserved_events
                or total_bytes > burst_contract.max_total_reserved_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "run-seed consumer rotation exceeds Host boundary contract"
                )
            if (
                source.used_since_reclaimable_events + total_events
                > self.limits.max_unreclaimable_ledger_events
                or source.used_since_reclaimable_payload_bytes + total_bytes
                > self.limits.max_unreclaimable_charged_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "run-seed consumer rotation exhausts physical headroom"
                )

            registration_event_id = _bounded_bookkeeping_event_id(
                "ledger_consumer_registered",
                successor_id,
            )
            retirement_event_ids = tuple(
                _bounded_bookkeeping_event_id(
                    "ledger_consumer_retired",
                    item.consumer_id,
                    context.run_id,
                )
                for item in existing_transcript
            )
            generation_event_id = _bounded_bookkeeping_event_id(
                "ledger_generation_run_seed",
                context.run_id,
            )
            reservation_event_id = _bounded_bookkeeping_event_id(
                "physical_reservation",
                reservation_id,
            )
            settlement_event_id = _bounded_bookkeeping_event_id(
                "physical_settlement",
                reservation_id,
            )
            latest = [
                registration_event_id,
                *retirement_event_ids,
                reservation_event_id,
                settlement_event_id,
            ]
            if minimum_advanced:
                latest.append(generation_event_id)
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=resulting_generation,
                ledger_through_sequence=source.ledger_through_sequence + total_events,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through + total_bytes
                ),
                active_reservations=(),
                active_checkpoint_barrier=None,
                latest_transition_event_ids=tuple(latest),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=(
                    _transition_cause(
                        run_start,
                        runtime_session_id=self.runtime_session_id,
                        cause_role="run_start",
                    ),
                ),
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-run-seed-rotation-contract:v1",
                    "run-start+consumer-register+consumer-retire+one-shot",
                ),
            )
            run_start_identity = stable_event_identity(
                run_start, runtime_session_id=self.runtime_session_id
            )
            registration_cause = build_frozen_fact(
                RunSeedConsumerCauseFact,
                schema_version="run_seed_consumer_cause.v1",
                cause_kind="run_seed",
                run_start_event_identity=run_start_identity,
                seed_semantic_fingerprint=seed_semantic_fingerprint,
                seed_reference_fingerprint=seed_reference_fingerprint,
            )
            registration_event = self._prepare_event(
                LedgerMaterializationConsumerRegisteredEvent(
                    id=registration_event_id,
                    **context.event_fields(),
                    consumer=successor,
                    cause=registration_cause,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            retirement_events = tuple(
                self._prepare_event(
                    LedgerMaterializationConsumerRetiredEvent(
                        id=event_id,
                        **context.event_fields(),
                        retired_horizon=consumer,
                        cause=build_frozen_fact(
                            ConsumerRetirementCauseFact,
                            schema_version="consumer_retirement_cause.v1",
                            cause_kind="retirement",
                            successor_consumer_id=successor.consumer_id,
                            terminal_or_successor_event_identity=run_start_identity,
                        ),
                        transition=transition,
                        resulting_account_state_fingerprint=(
                            resulting.account_state_fingerprint
                        ),
                    )
                )
                for event_id, consumer in zip(
                    retirement_event_ids, existing_transcript, strict=True
                )
            )
            generation_event = (
                self._prepare_event(
                    LedgerMaterializationGenerationAdvancedEvent(
                        id=generation_event_id,
                        **context.event_fields(),
                        previous_generation=source.generation,
                        resulting_generation=resulting_generation,
                        transition=transition,
                        resulting_account_state_fingerprint=(
                            resulting.account_state_fingerprint
                        ),
                    )
                )
                if minimum_advanced
                else None
            )

            settlement_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_SETTLED",
                contract=self.charge_contract,
            )
            reservation = build_frozen_fact(
                PhysicalOperationReservationFact,
                schema_version="physical_operation_reservation.v2",
                reservation_id=reservation_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=context.run_id,
                business_window_id="seed",
                business_window_generation=0,
                owner_kind=burst_contract.operation_kind,
                owner_id=owner_id,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                source_ledger_through_sequence=source.ledger_through_sequence,
                burst_contract_id=burst_contract.contract_id,
                burst_contract_version=burst_contract.contract_version,
                burst_contract_fingerprint=burst_contract.contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                reserved_events=total_events,
                reserved_payload_bytes=total_bytes,
                terminal_tail_reserved_events=settlement_charge.event_count,
                terminal_tail_reserved_payload_bytes=(
                    settlement_charge.charged_payload_bytes
                ),
            )
            business_candidate_bytes, business_wrapper_bytes = _candidate_charge_split(
                prepared_business, total_charge=business_charge
            )
            before_settlement_events = total_events - settlement_charge.event_count
            before_settlement_bytes = (
                total_bytes - settlement_charge.charged_payload_bytes
            )
            predecessor = build_frozen_fact(
                ActivePhysicalReservationStateFact,
                schema_version="active_physical_reservation_state.v1",
                reservation_id=reservation_id,
                owner_kind=reservation.owner_kind,
                owner_id=owner_id,
                lifecycle_status="active",
                reservation_fingerprint=reservation.reservation_fingerprint,
                suspension_fingerprint=None,
                reserved_events_total=total_events,
                reserved_payload_bytes_total=total_bytes,
                charged_candidate_events_lifetime=business_charge.event_count,
                charged_candidate_payload_bytes_lifetime=business_candidate_bytes,
                charged_wrapper_bytes_lifetime=business_wrapper_bytes,
                charged_bookkeeping_events_lifetime=(
                    before_settlement_events - business_charge.event_count
                ),
                charged_bookkeeping_bytes_lifetime=(
                    before_settlement_bytes
                    - business_charge.charged_payload_bytes
                ),
                charged_events_lifetime=before_settlement_events,
                charged_payload_bytes_lifetime=before_settlement_bytes,
                remaining_events=settlement_charge.event_count,
                remaining_payload_bytes=settlement_charge.charged_payload_bytes,
                latest_reservation_event_id=reservation_event_id,
                latest_lifecycle_event_id=reservation_event_id,
                latest_charge_applied_event_id=None,
            )
            reservation_event = self._prepare_event(
                PhysicalOperationReservationCreatedEvent(
                    id=reservation_event_id,
                    **context.event_fields(),
                    reservation=reservation,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            settlement = build_frozen_fact(
                PhysicalOperationSettlementFact,
                schema_version="physical_operation_settlement.v2",
                reservation_id=reservation_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=context.run_id,
                business_window_id="seed",
                business_window_generation=0,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                owner_kind=reservation.owner_kind,
                owner_id=owner_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                predecessor_status="active",
                predecessor_lifecycle_event_id=reservation_event_id,
                predecessor_reservation_state_fingerprint=predecessor.state_fingerprint,
                burst_contract_fingerprint=reservation.burst_contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                predecessor_remaining_events=predecessor.remaining_events,
                predecessor_remaining_payload_bytes=predecessor.remaining_payload_bytes,
                terminal_batch_charge_before_settlement_events=0,
                terminal_batch_charge_before_settlement_payload_bytes=0,
                settlement_event_charge_events=settlement_charge.event_count,
                settlement_event_charge_payload_bytes=(
                    settlement_charge.charged_payload_bytes
                ),
                charged_candidate_events=business_charge.event_count,
                charged_candidate_payload_bytes=business_candidate_bytes,
                charged_wrapper_bytes=business_wrapper_bytes,
                charged_bookkeeping_events=sum(
                    item.event_count for item in bookkeeping_charges
                ),
                charged_bookkeeping_bytes=sum(
                    item.charged_payload_bytes for item in bookkeeping_charges
                ),
                total_charged_events=total_events,
                total_charged_payload_bytes=total_bytes,
                terminal_outcome="completed",
                released_on_suspension_events_lifetime=0,
                released_on_suspension_payload_bytes_lifetime=0,
                released_on_settlement_events=0,
                released_on_settlement_payload_bytes=0,
                resulting_reservation_state_fingerprint=context_fingerprint(
                    "settled-physical-reservation-state:v1",
                    reservation.reservation_fingerprint,
                ),
            )
            settlement_event = self._prepare_event(
                PhysicalOperationReservationSettledEvent(
                    id=settlement_event_id,
                    **context.event_fields(),
                    settlement=settlement,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            candidate_batch: tuple[AgentEvent, ...] = (
                *prepared_business,
                registration_event,
                *retirement_events,
                *((generation_event,) if generation_event is not None else ()),
                reservation_event,
                settlement_event,
            )
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedRunSeedConsumerRotation(
                reservation=reservation,
                registration_event=registration_event,
                retirement_events=retirement_events,
                generation_event=generation_event,
                reservation_event=reservation_event,
                settlement_event=settlement_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    @staticmethod
    def _require_active_reservation(
        source: LedgerMaterializationAccountStateFact,
        reservation: PhysicalOperationReservationFact,
    ) -> ActivePhysicalReservationStateFact:
        matches = tuple(
            item
            for item in source.active_reservations
            if item.reservation_id == reservation.reservation_id
        )
        if len(matches) != 1:
            raise MaterializationAccountContractError(
                "physical reservation is not uniquely active"
            )
        active = matches[0]
        if (
            active.reservation_fingerprint != reservation.reservation_fingerprint
            or active.owner_kind != reservation.owner_kind
            or active.owner_id != reservation.owner_id
            or active.reserved_events_total != reservation.reserved_events
            or active.reserved_payload_bytes_total != reservation.reserved_payload_bytes
        ):
            raise MaterializationAccountContractError(
                "active physical reservation identity drifted"
            )
        return active

    def commit_one_shot_operation(
        self,
        *,
        context: EventContext,
        business_events: Sequence[AgentEvent],
        reservation_id: str,
        owner_id: str,
        burst_contract: PhysicalBurstContractFact,
        terminal_outcome: str = "completed",
        business_run_id: str | None = None,
        business_window_id: str | None = None,
        business_window_generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> CommittedOneShotPhysicalOperation:
        """Atomically reserve, append one finite batch, and settle it.

        This path is only for operations whose entire durable effect is already
        frozen. Side-effecting model/tool operations use a retained reservation
        created before dispatch and settle in a later batch.
        """

        if not business_events:
            raise ValueError("one-shot physical operation requires business facts")
        if burst_contract.operation_kind in {
            PhysicalOperationKind.LEDGER_GENESIS,
            PhysicalOperationKind.MODEL_CALL,
            PhysicalOperationKind.TOOL_CALL,
        }:
            raise ValueError(
                "one-shot operation requires a fixed non-streaming burst contract"
            )
        if terminal_outcome not in {
            "completed",
            "denied",
            "cancelled",
            "provider_error",
            "runtime_error",
            "host_teardown",
            "recovered_interrupted",
        }:
            raise ValueError("one-shot operation terminal outcome is invalid")

        with self._lock:
            source = self._require_state()
            if source.reconciliation_required:
                raise MaterializationAccountContractError(
                    "materialization account requires reconciliation"
                )
            if source.active_checkpoint_barrier is not None:
                raise CheckpointDispatchBarrierActive(
                    "checkpoint barrier rejects new producer admission"
                )
            if any(
                item.reservation_id == reservation_id
                for item in source.active_reservations
            ):
                raise ValueError("physical reservation ID is already active")

            prepared_business = tuple(
                self._prepare_event(event) for event in business_events
            )
            self._validate_fixed_batch_contract(
                burst_contract=burst_contract,
                business_events=prepared_business,
            )
            business_charge = deterministic_ledger_charge(
                prepared_business,
                contract=self.charge_contract,
            )
            reservation_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_CREATED",
                contract=self.charge_contract,
            )
            settlement_charge = deterministic_bookkeeping_charge(
                "PHYSICAL_OPERATION_RESERVATION_SETTLED",
                contract=self.charge_contract,
            )
            total_events = (
                business_charge.event_count
                + reservation_charge.event_count
                + settlement_charge.event_count
            )
            total_bytes = (
                business_charge.charged_payload_bytes
                + reservation_charge.charged_payload_bytes
                + settlement_charge.charged_payload_bytes
            )
            if (
                total_events > burst_contract.max_total_reserved_events
                or total_bytes > burst_contract.max_total_reserved_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "one-shot operation exceeds its frozen burst contract"
                )
            active_reserved_events = sum(
                item.remaining_events for item in source.active_reservations
            )
            active_reserved_bytes = sum(
                item.remaining_payload_bytes for item in source.active_reservations
            )
            if (
                source.used_since_reclaimable_events
                + active_reserved_events
                + total_events
                > self.limits.max_unreclaimable_ledger_events
                or source.used_since_reclaimable_payload_bytes
                + active_reserved_bytes
                + total_bytes
                > self.limits.max_unreclaimable_charged_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "physical event ledger headroom is exhausted"
                )

            reservation = build_frozen_fact(
                PhysicalOperationReservationFact,
                schema_version="physical_operation_reservation.v2",
                reservation_id=reservation_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=business_run_id,
                business_window_id=business_window_id,
                business_window_generation=business_window_generation,
                owner_kind=burst_contract.operation_kind,
                owner_id=owner_id,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                source_ledger_through_sequence=source.ledger_through_sequence,
                burst_contract_id=burst_contract.contract_id,
                burst_contract_version=burst_contract.contract_version,
                burst_contract_fingerprint=burst_contract.contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                reserved_events=total_events,
                reserved_payload_bytes=total_bytes,
                terminal_tail_reserved_events=settlement_charge.event_count,
                terminal_tail_reserved_payload_bytes=(
                    settlement_charge.charged_payload_bytes
                ),
            )
            reservation_event_id = _bounded_bookkeeping_event_id(
                "physical_reservation",
                reservation_id,
            )
            settlement_event_id = _bounded_bookkeeping_event_id(
                "physical_settlement",
                reservation_id,
            )
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=source.generation,
                ledger_through_sequence=source.ledger_through_sequence + total_events,
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through + total_bytes
                ),
                active_reservations=source.active_reservations,
                active_checkpoint_barrier=source.active_checkpoint_barrier,
                latest_transition_event_ids=(
                    reservation_event_id,
                    settlement_event_id,
                ),
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            causes = tuple(
                _transition_cause(
                    event,
                    runtime_session_id=self.runtime_session_id,
                    cause_role="business_terminal",
                )
                for event in sorted(prepared_business, key=lambda item: item.id)
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=causes,
                transition_contract_fingerprint=context_fingerprint(
                    "ledger-materialization-transition-contract:v1",
                    "atomic-event-batch+account-row-cas",
                ),
            )
            reservation_event = self._prepare_event(
                PhysicalOperationReservationCreatedEvent(
                    id=reservation_event_id,
                    **context.event_fields(),
                    reservation=reservation,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            initial_events = business_charge.event_count + reservation_charge.event_count
            initial_bytes = (
                business_charge.charged_payload_bytes
                + reservation_charge.charged_payload_bytes
            )
            business_candidate_payload_bytes, business_wrapper_bytes = (
                _candidate_charge_split(
                    prepared_business,
                    total_charge=business_charge,
                )
            )
            predecessor = build_frozen_fact(
                ActivePhysicalReservationStateFact,
                schema_version="active_physical_reservation_state.v1",
                reservation_id=reservation_id,
                owner_kind=reservation.owner_kind,
                owner_id=owner_id,
                lifecycle_status="active",
                reservation_fingerprint=reservation.reservation_fingerprint,
                suspension_fingerprint=None,
                reserved_events_total=reservation.reserved_events,
                reserved_payload_bytes_total=reservation.reserved_payload_bytes,
                charged_candidate_events_lifetime=business_charge.event_count,
                charged_candidate_payload_bytes_lifetime=(
                    business_candidate_payload_bytes
                ),
                charged_wrapper_bytes_lifetime=business_wrapper_bytes,
                charged_bookkeeping_events_lifetime=reservation_charge.event_count,
                charged_bookkeeping_bytes_lifetime=(
                    reservation_charge.charged_payload_bytes
                ),
                charged_events_lifetime=initial_events,
                charged_payload_bytes_lifetime=initial_bytes,
                remaining_events=reservation.reserved_events - initial_events,
                remaining_payload_bytes=(
                    reservation.reserved_payload_bytes - initial_bytes
                ),
                latest_reservation_event_id=reservation_event_id,
                latest_lifecycle_event_id=reservation_event_id,
                latest_charge_applied_event_id=None,
            )
            settlement = build_frozen_fact(
                PhysicalOperationSettlementFact,
                schema_version="physical_operation_settlement.v2",
                reservation_id=reservation_id,
                runtime_session_id=self.runtime_session_id,
                business_run_id=business_run_id,
                business_window_id=business_window_id,
                business_window_generation=business_window_generation,
                ledger_materialization_generation=(
                    source.generation.ledger_materialization_generation
                ),
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision
                ),
                owner_kind=reservation.owner_kind,
                owner_id=owner_id,
                reservation_fingerprint=reservation.reservation_fingerprint,
                predecessor_status="active",
                predecessor_lifecycle_event_id=reservation_event_id,
                predecessor_reservation_state_fingerprint=(
                    predecessor.state_fingerprint
                ),
                burst_contract_fingerprint=reservation.burst_contract_fingerprint,
                physical_charge_contract_fingerprint=(
                    self.charge_contract.contract_fingerprint
                ),
                predecessor_remaining_events=predecessor.remaining_events,
                predecessor_remaining_payload_bytes=predecessor.remaining_payload_bytes,
                terminal_batch_charge_before_settlement_events=0,
                terminal_batch_charge_before_settlement_payload_bytes=0,
                settlement_event_charge_events=settlement_charge.event_count,
                settlement_event_charge_payload_bytes=(
                    settlement_charge.charged_payload_bytes
                ),
                charged_candidate_events=business_charge.event_count,
                charged_candidate_payload_bytes=business_candidate_payload_bytes,
                charged_wrapper_bytes=business_wrapper_bytes,
                charged_bookkeeping_events=(
                    reservation_charge.event_count + settlement_charge.event_count
                ),
                charged_bookkeeping_bytes=(
                    reservation_charge.charged_payload_bytes
                    + settlement_charge.charged_payload_bytes
                ),
                total_charged_events=total_events,
                total_charged_payload_bytes=total_bytes,
                terminal_outcome=terminal_outcome,
                released_on_suspension_events_lifetime=0,
                released_on_suspension_payload_bytes_lifetime=0,
                released_on_settlement_events=(
                    predecessor.remaining_events - settlement_charge.event_count
                ),
                released_on_settlement_payload_bytes=(
                    predecessor.remaining_payload_bytes
                    - settlement_charge.charged_payload_bytes
                ),
                resulting_reservation_state_fingerprint=context_fingerprint(
                    "settled-physical-reservation-state:v1",
                    reservation.reservation_fingerprint,
                ),
            )
            settlement_event = self._prepare_event(
                PhysicalOperationReservationSettledEvent(
                    id=settlement_event_id,
                    **context.event_fields(),
                    settlement=settlement,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            # A one-shot operation has no external dispatch between these
            # facts. Keep the already-prepared business sequence positions
            # stable, then append reservation/settlement bookkeeping in the
            # same transaction. Retained side-effecting reservations still put
            # their reservation event before dispatch.
            candidate_batch = (
                *prepared_business,
                reservation_event,
                settlement_event,
            )
            stored = self._commit_atomic(
                candidate_batch,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return CommittedOneShotPhysicalOperation(
                reservation=reservation,
                reservation_event=reservation_event,
                settlement_event=settlement_event,
                business_events=prepared_business,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    @staticmethod
    def _validate_fixed_batch_contract(
        *,
        burst_contract: PhysicalBurstContractFact,
        business_events: Sequence[AgentEvent],
    ) -> None:
        from pulsara_agent.primitives.authority_materialization import (
            FixedBatchBurstContractFact,
        )

        if not isinstance(burst_contract, FixedBatchBurstContractFact):
            raise ValueError("one-shot operation requires a fixed-batch contract")
        if len(business_events) > burst_contract.max_business_events:
            raise PhysicalHeadroomExceeded("fixed batch event count is exceeded")
        contracts = {
            (item.event_type, item.event_schema_version): item
            for item in burst_contract.batch_event_contracts
        }
        counts: dict[tuple[str, str], int] = {}
        total_payload = 0
        for event in business_events:
            schema = DEFAULT_EVENT_SCHEMA_REGISTRY.resolve_for_event(
                event
            ).schema_contract
            key = (str(event.type), schema.event_schema_version)
            try:
                event_contract = contracts[key]
            except KeyError as exc:
                raise ValueError(
                    "fixed batch contains an event outside its contract"
                ) from exc
            payload_bytes = len(canonical_event_payload_bytes(event))
            if payload_bytes > event_contract.max_candidate_payload_bytes_per_occurrence:
                raise PhysicalHeadroomExceeded(
                    "fixed batch event payload exceeds its contract"
                )
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > event_contract.maximum_occurrences:
                raise PhysicalHeadroomExceeded(
                    "fixed batch event occurrence bound is exceeded"
                )
            total_payload += payload_bytes
        if total_payload > burst_contract.max_business_candidate_payload_bytes:
            raise PhysicalHeadroomExceeded(
                "fixed batch candidate payload bound is exceeded"
            )

    def commit_graph_checkpoint_consumer_advance(
        self,
        *,
        checkpoint_event: SubagentGraphCheckpointCommittedEvent,
        ledger_charged_payload_bytes_through_checkpoint: int,
        ledger_continuity_accumulator_through_checkpoint: str,
        deadline_monotonic: float | None = None,
    ) -> CommittedGraphConsumerCheckpoint:
        """Commit one graph checkpoint and advance only its consumer horizon."""

        with self._lock:
            source = self._require_state()
            if source.reconciliation_required:
                raise MaterializationAccountContractError(
                    "materialization account requires reconciliation"
                )
            if source.active_checkpoint_barrier is not None:
                raise CheckpointDispatchBarrierActive(
                    "graph checkpoint cannot cross an active checkpoint barrier"
                )
            prepared_checkpoint = self._prepare_event(checkpoint_event)
            checkpoint = prepared_checkpoint.checkpoint
            if checkpoint.parent_runtime_session_id != self.runtime_session_id:
                raise ValueError("graph checkpoint runtime-session attribution drifted")
            graph_consumers = tuple(
                item
                for item in source.generation.consumer_horizons
                if item.consumer_kind is LedgerMaterializationConsumerKind.SUBAGENT_GRAPH
            )
            if len(graph_consumers) != 1:
                raise ValueError("ledger has no unique subagent-graph consumer")
            old_horizon = graph_consumers[0]
            if checkpoint.through_sequence <= old_horizon.through_sequence:
                raise ValueError("graph checkpoint does not advance its consumer horizon")
            if checkpoint.through_sequence > source.ledger_through_sequence:
                raise ValueError("graph checkpoint exceeds the committed ledger")
            if not (
                old_horizon.ledger_charged_payload_bytes_through
                <= ledger_charged_payload_bytes_through_checkpoint
                <= source.ledger_charged_payload_bytes_through
            ):
                raise ValueError("graph checkpoint charged prefix is invalid")

            new_horizon = build_frozen_fact(
                LedgerMaterializationConsumerHorizonFact,
                schema_version="ledger_materialization_consumer_horizon.v1",
                runtime_session_id=old_horizon.runtime_session_id,
                consumer_kind=old_horizon.consumer_kind,
                consumer_id=old_horizon.consumer_id,
                business_run_id=None,
                business_window_id=None,
                business_window_generation=None,
                through_sequence=checkpoint.through_sequence,
                ledger_event_count_through=checkpoint.through_sequence,
                ledger_charged_payload_bytes_through=(
                    ledger_charged_payload_bytes_through_checkpoint
                ),
                ledger_continuity_accumulator=(
                    ledger_continuity_accumulator_through_checkpoint
                ),
                consumer_contract_fingerprint=(
                    old_horizon.consumer_contract_fingerprint
                ),
            )
            horizons = tuple(
                new_horizon if item.consumer_id == old_horizon.consumer_id else item
                for item in source.generation.consumer_horizons
            )
            provisional_generation = build_generation(
                source=source.generation,
                consumer_horizons=horizons,
                consumer_horizon_revision=(
                    source.generation.consumer_horizon_revision + 1
                ),
            )
            minimum_advanced = (
                provisional_generation.reclaimable_through_sequence
                > source.generation.reclaimable_through_sequence
            )
            resulting_generation = (
                build_generation(
                    source=source.generation,
                    consumer_horizons=horizons,
                    materialization_generation=(
                        source.generation.ledger_materialization_generation + 1
                    ),
                    consumer_horizon_revision=(
                        source.generation.consumer_horizon_revision + 1
                    ),
                )
                if minimum_advanced
                else provisional_generation
            )
            checkpoint_identity = stable_event_identity(
                prepared_checkpoint,
                runtime_session_id=self.runtime_session_id,
            )
            candidate_fingerprint = context_fingerprint(
                "subagent-graph-checkpoint-candidate:v1",
                checkpoint.model_dump(mode="json"),
            )
            cause = build_frozen_fact(
                CheckpointConsumerCauseFact,
                schema_version="checkpoint_consumer_cause.v1",
                cause_kind="checkpoint",
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_committed_event_identity=checkpoint_identity,
                checkpoint_candidate_fingerprint=candidate_fingerprint,
            )
            horizon_event_id = _bounded_bookkeeping_event_id(
                "ledger_consumer_horizon",
                checkpoint.checkpoint_id,
            )
            generation_event_id = _bounded_bookkeeping_event_id(
                "ledger_generation",
                checkpoint.checkpoint_id,
            )
            checkpoint_charge = deterministic_ledger_charge(
                (prepared_checkpoint,),
                contract=self.charge_contract,
            )
            horizon_charge = deterministic_bookkeeping_charge(
                "LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED",
                contract=self.charge_contract,
            )
            generation_charge = (
                deterministic_bookkeeping_charge(
                    "LEDGER_MATERIALIZATION_GENERATION_ADVANCED",
                    contract=self.charge_contract,
                )
                if minimum_advanced
                else DeterministicLedgerCharge(0, 0)
            )
            charge = DeterministicLedgerCharge(
                event_count=(
                    checkpoint_charge.event_count
                    + horizon_charge.event_count
                    + generation_charge.event_count
                ),
                charged_payload_bytes=(
                    checkpoint_charge.charged_payload_bytes
                    + horizon_charge.charged_payload_bytes
                    + generation_charge.charged_payload_bytes
                ),
            )
            if (
                source.used_since_reclaimable_events + charge.event_count
                > self.limits.max_unreclaimable_ledger_events
                or source.used_since_reclaimable_payload_bytes
                + charge.charged_payload_bytes
                > self.limits.max_unreclaimable_charged_payload_bytes
            ):
                raise PhysicalHeadroomExceeded(
                    "graph checkpoint terminal batch exhausts maintenance headroom"
                )
            transition_ids = [horizon_event_id]
            if minimum_advanced:
                transition_ids.append(generation_event_id)
            resulting = build_account_state(
                runtime_session_id=self.runtime_session_id,
                generation=resulting_generation,
                ledger_through_sequence=(source.ledger_through_sequence + charge.event_count),
                ledger_charged_payload_bytes_through=(
                    source.ledger_charged_payload_bytes_through
                    + charge.charged_payload_bytes
                ),
                active_reservations=source.active_reservations,
                active_checkpoint_barrier=None,
                latest_transition_event_ids=transition_ids,
                reconciliation_required=False,
                reconciliation_reason_code=None,
            )
            transition = _transition(
                source=source,
                resulting=resulting,
                causes=(
                    _transition_cause(
                        prepared_checkpoint,
                        runtime_session_id=self.runtime_session_id,
                        cause_role="checkpoint_committed",
                    ),
                ),
                transition_contract_fingerprint=context_fingerprint(
                    "subagent-graph-consumer-horizon-transition-contract:v1",
                    checkpoint.graph_reducer_contract_fingerprint,
                ),
            )
            horizon_event = self._prepare_event(
                LedgerMaterializationConsumerHorizonAdvancedEvent(
                    id=horizon_event_id,
                    run_id=prepared_checkpoint.run_id,
                    turn_id=prepared_checkpoint.turn_id,
                    reply_id=prepared_checkpoint.reply_id,
                    created_at=prepared_checkpoint.created_at,
                    previous_horizon=old_horizon,
                    resulting_horizon=new_horizon,
                    cause=cause,
                    transition=transition,
                    resulting_account_state_fingerprint=(
                        resulting.account_state_fingerprint
                    ),
                )
            )
            generation_event = (
                self._prepare_event(
                    LedgerMaterializationGenerationAdvancedEvent(
                        id=generation_event_id,
                        run_id=prepared_checkpoint.run_id,
                        turn_id=prepared_checkpoint.turn_id,
                        reply_id=prepared_checkpoint.reply_id,
                        created_at=prepared_checkpoint.created_at,
                        previous_generation=source.generation,
                        resulting_generation=resulting_generation,
                        transition=transition,
                        resulting_account_state_fingerprint=(
                            resulting.account_state_fingerprint
                        ),
                    )
                )
                if minimum_advanced
                else None
            )
            events: tuple[AgentEvent, ...] = (
                prepared_checkpoint,
                horizon_event,
                *((generation_event,) if generation_event is not None else ()),
            )
            stored = self.commit_transition_batch(
                source=source,
                events=events,
                resulting=resulting,
                deadline_monotonic=deadline_monotonic,
            )
            by_id = {event.id: event for event in stored}
            stored_checkpoint = by_id[prepared_checkpoint.id]
            stored_horizon = by_id[horizon_event.id]
            stored_generation = (
                by_id[generation_event.id] if generation_event is not None else None
            )
            if not isinstance(stored_checkpoint, SubagentGraphCheckpointCommittedEvent):
                raise MaterializationAccountContractError(
                    "stored graph checkpoint event type drifted"
                )
            if not isinstance(
                stored_horizon, LedgerMaterializationConsumerHorizonAdvancedEvent
            ):
                raise MaterializationAccountContractError(
                    "stored graph horizon event type drifted"
                )
            if stored_generation is not None and not isinstance(
                stored_generation, LedgerMaterializationGenerationAdvancedEvent
            ):
                raise MaterializationAccountContractError(
                    "stored graph generation event type drifted"
                )
            return CommittedGraphConsumerCheckpoint(
                checkpoint_event=stored_checkpoint,
                horizon_event=stored_horizon,
                generation_event=stored_generation,
                stored_events=stored,
                resulting_account_state=resulting,
            )

    def commit_transition_batch(
        self,
        *,
        source: LedgerMaterializationAccountStateFact,
        events: Sequence[AgentEvent],
        resulting: LedgerMaterializationAccountStateFact,
        deadline_monotonic: float | None = None,
    ) -> tuple[AgentEvent, ...]:
        """Commit one prebuilt account transition after independent validation."""

        if not events:
            raise ValueError("account transition batch cannot be empty")
        with self._lock:
            current = self._require_state()
            if current.account_state_fingerprint != source.account_state_fingerprint:
                raise MaterializationAccountContractError(
                    "account transition source is stale"
                )
            prepared = tuple(self._prepare_event(event) for event in events)
            charge = deterministic_ledger_charge(
                prepared, contract=self.charge_contract
            )
            if (
                resulting.ledger_through_sequence
                != source.ledger_through_sequence + charge.event_count
                or resulting.ledger_charged_payload_bytes_through
                != source.ledger_charged_payload_bytes_through
                + charge.charged_payload_bytes
            ):
                raise MaterializationAccountContractError(
                    "account transition charged prefix mismatch"
                )
            for event in prepared:
                transition = getattr(event, "transition", None)
                resulting_fingerprint = getattr(
                    event, "resulting_account_state_fingerprint", None
                )
                if transition is not None and (
                    transition.before_account_state_fingerprint
                    != source.account_state_fingerprint
                    or transition.after_account_state_fingerprint
                    != resulting.account_state_fingerprint
                ):
                    raise MaterializationAccountContractError(
                        "account event transition fingerprint mismatch"
                    )
                if resulting_fingerprint is not None and (
                    resulting_fingerprint != resulting.account_state_fingerprint
                ):
                    raise MaterializationAccountContractError(
                        "account event resulting-state fingerprint mismatch"
                    )
            stored = self._commit_atomic(
                prepared,
                source_state_fingerprint=source.account_state_fingerprint,
                resulting=resulting,
                expected_last_sequence=source.ledger_through_sequence,
                deadline_monotonic=deadline_monotonic,
            )
            self.store.install_confirmed_state(resulting)
            return stored

    def _commit_atomic(
        self,
        events: Sequence[AgentEvent],
        *,
        source_state_fingerprint: str | None,
        resulting: LedgerMaterializationAccountStateFact,
        expected_last_sequence: int,
        deadline_monotonic: float | None,
    ) -> tuple[AgentEvent, ...]:
        """Commit or prove the exact stable event/account candidate FULL or NONE."""

        candidates = tuple(events)
        try:
            return tuple(
                self.event_log.extend_with_materialization_state(
                    candidates,
                    expected_account_state_fingerprint=source_state_fingerprint,
                    resulting_account_state=resulting,
                    physical_charge_contract=self.charge_contract,
                    expected_last_sequence=expected_last_sequence,
                    deadline_monotonic=deadline_monotonic,
                )
            )
        except BaseException as original:
            try:
                confirmation = self.event_log.confirm_batch(
                    candidates,
                    deadline_monotonic=deadline_monotonic,
                )
                account = self.event_log.read_materialization_account_state(
                    deadline_monotonic=deadline_monotonic,
                )
            except BaseException as confirmation_error:
                raise MaterializationAccountReconciliationRequired(
                    "materialization batch confirmation is unavailable"
                ) from confirmation_error
            if (
                not confirmation.missing_event_ids
                and len(confirmation.committed_events) == len(candidates)
                and account is not None
                and account.account_state_fingerprint
                == resulting.account_state_fingerprint
            ):
                return confirmation.committed_events
            if (
                not confirmation.committed_events
                and confirmation.missing_event_ids
                == tuple(item.id for item in candidates)
                and (
                    (account is None and source_state_fingerprint is None)
                    or (
                        account is not None
                        and account.account_state_fingerprint
                        == source_state_fingerprint
                    )
                )
            ):
                raise MaterializationAccountCommitFailed(
                    "materialization event/account batch was not committed"
                ) from original
            raise MaterializationAccountReconciliationRequired(
                "materialization batch/account confirmation is partial or conflicting"
            ) from original

    def _require_state(self) -> LedgerMaterializationAccountStateFact:
        state = self.store.snapshot()
        if state is None:
            raise MaterializationAccountContractError(
                "ledger materialization account has not been bootstrapped"
            )
        if state.runtime_session_id != self.runtime_session_id:
            raise MaterializationAccountContractError(
                "ledger materialization account session identity drifted"
            )
        return state


def _stored_sequence(event: AgentEvent) -> int:
    if event.sequence is None or event.sequence < 1:
        raise MaterializationAccountContractError(
            "account reducer requires committed events"
        )
    return event.sequence


def _transition_cause(
    event: AgentEvent,
    *,
    runtime_session_id: str,
    cause_role: str,
) -> LedgerMaterializationTransitionCauseIdentityFact:
    identity = stable_event_identity(event, runtime_session_id=runtime_session_id)
    return build_frozen_fact(
        LedgerMaterializationTransitionCauseIdentityFact,
        schema_version="ledger_materialization_transition_cause_identity.v1",
        cause_role=cause_role,
        event_identity=identity,
    )


def _transition(
    *,
    source: LedgerMaterializationAccountStateFact,
    resulting: LedgerMaterializationAccountStateFact,
    causes: tuple[LedgerMaterializationTransitionCauseIdentityFact, ...],
    transition_contract_fingerprint: str,
) -> LedgerMaterializationAccountTransitionFact:
    return build_frozen_fact(
        LedgerMaterializationAccountTransitionFact,
        schema_version="ledger_materialization_account_transition.v2",
        runtime_session_id=source.runtime_session_id,
        source_generation=source.generation.ledger_materialization_generation,
        source_consumer_horizon_revision=(
            source.generation.consumer_horizon_revision
        ),
        result_generation=resulting.generation.ledger_materialization_generation,
        result_consumer_horizon_revision=(
            resulting.generation.consumer_horizon_revision
        ),
        before_account_state_fingerprint=source.account_state_fingerprint,
        after_account_state_fingerprint=resulting.account_state_fingerprint,
        cause_event_identities=tuple(
            sorted(
                causes,
                key=lambda item: (
                    item.event_identity.runtime_session_id,
                    item.event_identity.event_id,
                    item.cause_role,
                ),
            )
        ),
        transition_contract_fingerprint=transition_contract_fingerprint,
    )


ACCOUNT_TRANSITION_EVENT_TYPES = (
    LedgerMaterializationConsumerRegisteredEvent,
    LedgerMaterializationConsumerHorizonAdvancedEvent,
    LedgerMaterializationConsumerRetiredEvent,
    LedgerMaterializationGenerationAdvancedEvent,
    PhysicalOperationReservationCreatedEvent,
    PhysicalOperationChargeAppliedEvent,
    PhysicalOperationReservationSuspendedEvent,
    PhysicalOperationReservationSettledEvent,
    CheckpointDispatchBarrierInstalledEvent,
    CheckpointDispatchBarrierReleasedEvent,
)


__all__ = [
    "ACCOUNT_TRANSITION_EVENT_TYPES",
    "DeterministicLedgerCharge",
    "CommittedPhysicalReservation",
    "CommittedPhysicalCharge",
    "CommittedPhysicalSettlement",
    "CommittedOneShotPhysicalOperation",
    "CommittedRunSeedConsumerRotation",
    "CommittedLedgerGenesis",
    "CheckpointDispatchBarrierActive",
    "LedgerMaterializationCoordinator",
    "LedgerMaterializationAccountStore",
    "MaterializationAccountContractError",
    "MaterializationAccountCommitFailed",
    "MaterializationAccountReconciliationRequired",
    "PhysicalHeadroomExceeded",
    "RunSeedSourceStale",
    "account_with_committed_usage",
    "build_account_state",
    "build_generation",
    "canonical_empty_account",
    "canonical_empty_generation",
    "deterministic_ledger_charge",
    "deterministic_bookkeeping_charge",
]
