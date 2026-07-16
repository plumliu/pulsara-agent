"""Privileged full-source verification for transcript materialization state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    LedgerMaterializationAccountGenesisEvent,
    RunStartEvent,
    TranscriptProjectionCheckpointCommittedEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY, EventLog
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    advance_ledger_continuity_accumulator,
    classify_transcript_event_type,
)
from pulsara_agent.llm.terminal_projection import hydrate_terminal_projection_text
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives.authority_materialization import (
    LedgerMaterializationConsumerKind,
    LedgerMaterializationAccountStateFact,
    PhysicalOperationKind,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.transcript_projection import (
    TranscriptProjectionScopeFact,
)
from pulsara_agent.runtime.authority_materialization.account import (
    LedgerMaterializationAccountStore,
    LedgerMaterializationCoordinator,
    canonical_empty_account,
    deterministic_ledger_charge,
)
from pulsara_agent.runtime.authority_materialization.checkpoint import (
    build_default_checkpoint_terminal_contract,
    commit_checkpoint_failure,
    commit_checkpoint_success,
    install_checkpoint_barrier,
    prepare_transcript_checkpoint_candidate,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    AuthorityMaterializationContractBundle,
)
from pulsara_agent.runtime.authority_materialization.transcript_hydrator import (
    hydrate_run_transcript_seed,
    hydrate_transcript_projection_materialization,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
    projection_references,
)
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    TranscriptProjectionMaterializationContracts,
    persist_prepared_transcript_projection_materialization,
    prepare_authority_artifact_write_reservation,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    CheckpointMaintenanceAuthority,
)
from pulsara_agent.primitives.transcript_checkpoint import (
    CheckpointFailureReasonCode,
)


class TranscriptProjectionDoctorOutcome(StrEnum):
    VERIFIED = "verified"
    REBUILT = "rebuilt"
    CHECKPOINT_MISSING = "checkpoint_missing"


class TranscriptProjectionDoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_session_id: str = Field(min_length=1)
    outcome: TranscriptProjectionDoctorOutcome
    scanned_through_sequence: int = Field(ge=1)
    scanned_event_count: int = Field(ge=1)
    scanned_payload_bytes: int = Field(ge=0)
    transcript_semantic_event_count: int = Field(ge=0)
    transcript_semantic_accumulator: str = Field(min_length=1)
    ledger_continuity_accumulator: str = Field(min_length=1)
    stable_state_semantic_fingerprint: str = Field(min_length=1)
    normalized_transcript_fingerprint: str = Field(min_length=1)
    checkpoint_count: int = Field(ge=0)
    verified_checkpoint_ids: tuple[str, ...]
    rebuilt_checkpoint_id: str | None = None
    account_state_fingerprint_before: str = Field(min_length=1)
    account_state_fingerprint_after: str = Field(min_length=1)
    report_fingerprint: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _FullSourceVerification:
    account: LedgerMaterializationAccountStateFact
    decoded_events: tuple[AgentEvent, ...]
    store: TranscriptProjectionStateStore
    checkpoint_events: tuple[TranscriptProjectionCheckpointCommittedEvent, ...]
    verified_checkpoint_ids: tuple[str, ...]
    reachable_by_checkpoint_id: dict[str, frozenset[str]]
    latest_run_start: RunStartEvent
    scanned_payload_bytes: int


def verify_or_rebuild_transcript_projection_checkpoint(
    *,
    runtime_session_id: str,
    mode: Literal["verify", "rebuild"],
    event_log: EventLog,
    archive: ArtifactStore,
    maintenance_authority: CheckpointMaintenanceAuthority,
    authority_contracts: AuthorityMaterializationContractBundle,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    max_events: int = 1_000_000,
    max_payload_bytes: int = 1024 * 1024 * 1024,
    operation_timeout_seconds: float = 120.0,
) -> TranscriptProjectionDoctorReport:
    """Verify current-schema authority or rebuild only its checkpoint memoization."""

    if mode not in {"verify", "rebuild"}:
        raise ValueError("transcript projection doctor mode is invalid")
    if max_events < 1 or max_payload_bytes < 1:
        raise ValueError("transcript projection doctor bounds must be positive")
    if operation_timeout_seconds <= 0:
        raise ValueError("transcript projection doctor timeout must be positive")
    deadline = monotonic() + operation_timeout_seconds
    with maintenance_authority.acquire_exclusive(runtime_session_id) as permit:
        if not permit.exclusive or permit.runtime_session_id != runtime_session_id:
            raise RuntimeError("transcript projection maintenance permit mismatch")
        verified = _verify_full_source(
            runtime_session_id=runtime_session_id,
            event_log=event_log,
            archive=archive,
            authority_contracts=authority_contracts,
            materialization_contracts=materialization_contracts,
            max_events=max_events,
            max_payload_bytes=max_payload_bytes,
            deadline_monotonic=deadline,
        )
        account_before = verified.account
        rebuilt_checkpoint_id: str | None = None
        account_after = account_before
        if mode == "rebuild":
            rebuilt_checkpoint_id, account_after = _rebuild_checkpoint(
                runtime_session_id=runtime_session_id,
                event_log=event_log,
                archive=archive,
                authority_contracts=authority_contracts,
                materialization_contracts=materialization_contracts,
                verified=verified,
                deadline_monotonic=deadline,
            )
        outcome = (
            TranscriptProjectionDoctorOutcome.REBUILT
            if rebuilt_checkpoint_id is not None
            else TranscriptProjectionDoctorOutcome.VERIFIED
            if verified.checkpoint_events
            else TranscriptProjectionDoctorOutcome.CHECKPOINT_MISSING
        )
        live = verified.store.snapshot()
        payload = {
            "runtime_session_id": runtime_session_id,
            "outcome": outcome,
            "scanned_through_sequence": account_before.ledger_through_sequence,
            "scanned_event_count": len(verified.decoded_events),
            "scanned_payload_bytes": verified.scanned_payload_bytes,
            "transcript_semantic_event_count": live.transcript_semantic_event_count,
            "transcript_semantic_accumulator": live.transcript_semantic_accumulator,
            "ledger_continuity_accumulator": live.ledger_continuity_accumulator,
            "stable_state_semantic_fingerprint": (
                live.stable_semantic_state.state_semantic_fingerprint
            ),
            "normalized_transcript_fingerprint": (
                live.stable_semantic_state.normalized_transcript_fingerprint
            ),
            "checkpoint_count": len(verified.checkpoint_events),
            "verified_checkpoint_ids": verified.verified_checkpoint_ids,
            "rebuilt_checkpoint_id": rebuilt_checkpoint_id,
            "account_state_fingerprint_before": (
                account_before.account_state_fingerprint
            ),
            "account_state_fingerprint_after": account_after.account_state_fingerprint,
        }
        return TranscriptProjectionDoctorReport(
            **payload,
            report_fingerprint=context_fingerprint(
                "transcript-projection-doctor-report:v1", payload
            ),
        )


def _verify_full_source(
    *,
    runtime_session_id: str,
    event_log: EventLog,
    archive: ArtifactStore,
    authority_contracts: AuthorityMaterializationContractBundle,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    max_events: int,
    max_payload_bytes: int,
    deadline_monotonic: float,
) -> _FullSourceVerification:
    usage = event_log.read_ledger_usage_snapshot(
        deadline_monotonic=deadline_monotonic
    )
    if usage.through_sequence < 1:
        raise ValueError("transcript projection doctor requires a non-empty ledger")
    account = event_log.read_materialization_account_state(
        deadline_monotonic=deadline_monotonic
    )
    if account is None:
        raise ValueError(
            "current-schema materialization account is missing; reset the database"
        )
    if account.runtime_session_id != runtime_session_id:
        raise ValueError("materialization account session attribution drifted")
    if account.ledger_through_sequence != usage.through_sequence:
        raise ValueError("materialization account does not cover ledger high-water")
    if account.reconciliation_required:
        raise ValueError("materialization account is latched for reconciliation")
    if account.active_checkpoint_barrier is not None or account.active_reservations:
        raise ValueError("transcript projection doctor requires a drained account")

    raw = event_log.read_raw_range_snapshot(
        minimum_sequence=1,
        through_sequence=usage.through_sequence,
        max_events=max_events,
        max_payload_bytes=max_payload_bytes,
        deadline_monotonic=deadline_monotonic,
    )
    if len(raw.events) != usage.through_sequence:
        raise ValueError("full-source ledger range is incomplete")
    decoded = tuple(
        envelope.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        for envelope in raw.events
    )
    _verify_materialization_account_chain(
        runtime_session_id=runtime_session_id,
        decoded_events=decoded,
        raw_events=raw.events,
        account=account,
        authority_contracts=authority_contracts,
    )

    semantic_delta = event_log.read_transcript_domain_delta(
        after_sequence=0,
        through_sequence=usage.through_sequence,
        max_events=max_events,
        max_payload_bytes=max_payload_bytes,
        registry_contract_fingerprint=(
            authority_contracts.event_domain.contract.registry_contract_fingerprint
        ),
        deadline_monotonic=deadline_monotonic,
    )
    expected_semantic_envelopes = tuple(
        envelope
        for envelope in raw.events
        if classify_transcript_event_type(envelope.event_type)
        == "transcript_semantic"
    )
    if tuple(
        item.envelope_fingerprint for item in semantic_delta.semantic_events
    ) != tuple(item.envelope_fingerprint for item in expected_semantic_envelopes):
        raise ValueError("transcript sparse prefix omitted a semantic event")

    documents = TranscriptProjectionDocumentRegistry()
    for reference in projection_references(decoded):
        text = archive.get_text(
            reference.document_artifact_id,
            session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        documents.register(
            reference,
            hydrate_terminal_projection_text(reference, text),
        )
    store = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=documents,
    )
    checkpoints = tuple(
        event
        for event in decoded
        if isinstance(event, TranscriptProjectionCheckpointCommittedEvent)
    )
    checkpoints_by_source: dict[
        int, list[TranscriptProjectionCheckpointCommittedEvent]
    ] = {}
    for event in checkpoints:
        checkpoints_by_source.setdefault(
            event.checkpoint.candidate_ledger_through_sequence, []
        ).append(event)
    run_starts: list[RunStartEvent] = []
    verified_checkpoint_ids: list[str] = []
    reachable_by_checkpoint_id: dict[str, frozenset[str]] = {}
    for event in decoded:
        if isinstance(event, RunStartEvent):
            _verify_run_seed(
                runtime_session_id=runtime_session_id,
                run_start=event,
                store=store,
                archive=archive,
                materialization_contracts=materialization_contracts,
                deadline_monotonic=deadline_monotonic,
            )
            run_starts.append(event)
        store.apply_committed((event,))
        for checkpoint in checkpoints_by_source.get(event.sequence or -1, ()):
            reachable_by_checkpoint_id[checkpoint.checkpoint_id] = (
                _verify_checkpoint_against_store(
                    runtime_session_id=runtime_session_id,
                    checkpoint=checkpoint,
                    store=store,
                    archive=archive,
                    materialization_contracts=materialization_contracts,
                    deadline_monotonic=deadline_monotonic,
                    run_starts=tuple(run_starts),
                )
            )
            verified_checkpoint_ids.append(checkpoint.checkpoint_id)
    if not run_starts:
        raise ValueError(
            "current-schema ledger has no required RunStart seed; reset the database"
        )
    live = store.snapshot()
    after = semantic_delta.after
    scanned_payload_bytes = sum(
        len(item.canonical_payload_bytes) for item in raw.events
    )
    if (
        after.through_sequence != live.ledger_through_sequence
        or after.ledger_payload_bytes != scanned_payload_bytes
        or after.semantic_event_count != live.transcript_semantic_event_count
        or after.semantic_accumulator != live.transcript_semantic_accumulator
        or after.ledger_continuity_accumulator
        != live.ledger_continuity_accumulator
    ):
        raise ValueError("transcript prefix projection drifted from full source")
    if set(verified_checkpoint_ids) != {
        item.checkpoint_id for item in checkpoints
    }:
        raise ValueError("checkpoint source lies outside the full-source ledger")
    return _FullSourceVerification(
        account=account,
        decoded_events=decoded,
        store=store,
        checkpoint_events=checkpoints,
        verified_checkpoint_ids=tuple(verified_checkpoint_ids),
        reachable_by_checkpoint_id=reachable_by_checkpoint_id,
        latest_run_start=run_starts[-1],
        scanned_payload_bytes=scanned_payload_bytes,
    )


def _verify_run_seed(
    *,
    runtime_session_id: str,
    run_start: RunStartEvent,
    store: TranscriptProjectionStateStore,
    archive: ArtifactStore,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    deadline_monotonic: float,
) -> None:
    before = store.snapshot()
    if not before.checkpointable:
        raise ValueError("RunStart crossed a non-checkpointable transcript assembly")
    if (
        run_start.run_transcript_seed_semantic.prior_stable_semantic_state
        != before.stable_semantic_state
    ):
        raise ValueError("RunStart transcript seed does not match full-source state")
    hydrated = hydrate_run_transcript_seed(
        archive=archive,
        runtime_session_id=runtime_session_id,
        seed_semantic=run_start.run_transcript_seed_semantic,
        seed_reference=run_start.run_transcript_seed_reference,
        contracts=materialization_contracts,
        deadline_monotonic=deadline_monotonic,
    )
    if _entry_semantics(hydrated.entries) != _entry_semantics(store.stable_entries()):
        raise ValueError("RunStart seed tree does not match full-source transcript")


def _verify_checkpoint_against_store(
    *,
    runtime_session_id: str,
    checkpoint: TranscriptProjectionCheckpointCommittedEvent,
    store: TranscriptProjectionStateStore,
    archive: ArtifactStore,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    deadline_monotonic: float,
    run_starts: tuple[RunStartEvent, ...],
) -> frozenset[str]:
    candidate = checkpoint.checkpoint
    live = store.snapshot()
    if not live.checkpointable:
        raise ValueError("durable checkpoint captured live transcript assembly")
    if (
        candidate.stable_semantic_state != live.stable_semantic_state
        or candidate.semantic_source.semantic_source_event_count
        != live.transcript_semantic_event_count
        or candidate.semantic_source.semantic_source_accumulator
        != live.transcript_semantic_accumulator
        or candidate.semantic_source.resulting_state_fingerprint
        != live.stable_semantic_state.state_semantic_fingerprint
        or candidate.candidate_ledger_continuity_accumulator
        != live.ledger_continuity_accumulator
    ):
        raise ValueError("checkpoint semantic source drifted from full-source fold")
    if not any(
        item.run_transcript_seed_semantic == candidate.run_seed_semantic
        and item.run_transcript_seed_reference == candidate.run_seed_reference
        for item in run_starts
    ):
        raise ValueError("checkpoint does not join one durable RunStart seed")
    hydrated = hydrate_transcript_projection_materialization(
        archive=archive,
        runtime_session_id=runtime_session_id,
        root_reference=candidate.materialization.root_manifest_ref,
        contracts=materialization_contracts,
        deadline_monotonic=deadline_monotonic,
    )
    if _entry_semantics(hydrated.entries) != _entry_semantics(store.stable_entries()):
        raise ValueError("checkpoint tree does not match full-source transcript")
    return hydrated.reachable_artifact_ids


def _verify_materialization_account_chain(
    *,
    runtime_session_id: str,
    decoded_events: tuple[AgentEvent, ...],
    raw_events,
    account: LedgerMaterializationAccountStateFact,
    authority_contracts: AuthorityMaterializationContractBundle,
) -> None:
    empty = canonical_empty_account(
        runtime_session_id=runtime_session_id,
        charge_contract_fingerprint=(
            authority_contracts.charge_contract.contract_fingerprint
        ),
    )
    current_fingerprint = empty.account_state_fingerprint
    genesis_count = 0
    continuity = EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
    continuity_by_sequence: dict[int, str] = {0: continuity}
    charged_bytes = 0
    charged_by_sequence: dict[int, int] = {0: 0}
    for event, raw in zip(decoded_events, raw_events, strict=True):
        continuity = advance_ledger_continuity_accumulator(
            continuity,
            envelope_fingerprint=raw.envelope_fingerprint,
        )
        continuity_by_sequence[raw.sequence] = continuity
        charged_bytes += deterministic_ledger_charge(
            (event,), contract=authority_contracts.charge_contract
        ).charged_payload_bytes
        charged_by_sequence[raw.sequence] = charged_bytes
        transition = getattr(event, "transition", None)
        if transition is None:
            continue
        if transition.before_account_state_fingerprint == current_fingerprint:
            current_fingerprint = transition.after_account_state_fingerprint
        elif transition.after_account_state_fingerprint != current_fingerprint:
            raise ValueError("materialization account transition chain is discontinuous")
        resulting = getattr(event, "resulting_account_state_fingerprint", None)
        if resulting is not None and resulting != transition.after_account_state_fingerprint:
            raise ValueError("account transition event reports another resulting state")
        if isinstance(event, LedgerMaterializationAccountGenesisEvent):
            genesis_count += 1
            if (
                event.resulting_account_state.account_state_fingerprint
                != transition.after_account_state_fingerprint
            ):
                raise ValueError("account genesis state does not match its transition")
    if genesis_count != 1:
        raise ValueError(
            "current-schema ledger requires one canonical account genesis; reset the database"
        )
    if current_fingerprint != account.account_state_fingerprint:
        raise ValueError("materialization account row drifted from transition chain")
    if account.ledger_charged_payload_bytes_through != charged_bytes:
        raise ValueError("materialization account charged prefix drifted")
    for horizon in account.generation.consumer_horizons:
        through = horizon.through_sequence
        if (
            horizon.ledger_event_count_through != through
            or charged_by_sequence.get(through)
            != horizon.ledger_charged_payload_bytes_through
            or continuity_by_sequence.get(through)
            != horizon.ledger_continuity_accumulator
        ):
            raise ValueError("materialization consumer horizon prefix drifted")
    by_id = {event.id: event for event in decoded_events}
    for event_id in account.latest_transition_event_ids:
        event = by_id.get(event_id)
        transition = None if event is None else getattr(event, "transition", None)
        if (
            transition is None
            or transition.after_account_state_fingerprint
            != account.account_state_fingerprint
        ):
            raise ValueError("account latest transition reference is invalid")


def _rebuild_checkpoint(
    *,
    runtime_session_id: str,
    event_log: EventLog,
    archive: ArtifactStore,
    authority_contracts: AuthorityMaterializationContractBundle,
    materialization_contracts: TranscriptProjectionMaterializationContracts,
    verified: _FullSourceVerification,
    deadline_monotonic: float,
) -> tuple[str, LedgerMaterializationAccountStateFact]:
    account = verified.account
    consumers = tuple(
        item
        for item in account.generation.consumer_horizons
        if item.consumer_kind is LedgerMaterializationConsumerKind.TRANSCRIPT_WINDOW
    )
    if len(consumers) != 1:
        raise ValueError("checkpoint rebuild requires one active transcript consumer")
    consumer = consumers[0]
    run_start = verified.latest_run_start
    if (
        consumer.business_run_id != run_start.run_id
        or consumer.business_window_id is None
        or consumer.business_window_generation is None
    ):
        raise ValueError("checkpoint rebuild consumer/run attribution drifted")
    digest = context_fingerprint(
        "transcript-checkpoint-doctor-id:v1",
        {
            "runtime_session_id": runtime_session_id,
            "consumer_id": consumer.consumer_id,
            "through_sequence": account.ledger_through_sequence,
            "stable_state": verified.store.snapshot().stable_semantic_state,
        },
    ).removeprefix("sha256:")
    checkpoint_id = f"transcript_checkpoint:doctor:{digest[:33]}"
    previous = max(
        verified.checkpoint_events,
        key=lambda item: item.sequence or 0,
        default=None,
    )
    previous_checkpoint_id = None if previous is None else previous.checkpoint_id
    previously_reachable = (
        frozenset()
        if previous_checkpoint_id is None
        else verified.reachable_by_checkpoint_id[previous_checkpoint_id]
    )
    prepared = prepare_transcript_checkpoint_candidate(
        checkpoint_id=checkpoint_id,
        scope=build_frozen_fact(
            TranscriptProjectionScopeFact,
            schema_version="transcript_projection_scope.v1",
            runtime_session_id=runtime_session_id,
            run_id=run_start.run_id,
            window_id=consumer.business_window_id,
            window_generation=consumer.business_window_generation,
        ),
        run_seed_semantic=run_start.run_transcript_seed_semantic,
        run_seed_reference=run_start.run_transcript_seed_reference,
        materialization_consumer=consumer,
        account_state=account,
        transcript_store=verified.store,
        transcript_semantic_domain_contract_fingerprint=(
            authority_contracts.event_domain.contract.registry_contract_fingerprint
        ),
        contracts=materialization_contracts,
        limits=authority_contracts.limits,
        previous_checkpoint_id=previous_checkpoint_id,
        previously_reachable_artifact_ids=previously_reachable,
    )
    store = LedgerMaterializationAccountStore(
        state=account,
        charge_contract=authority_contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        store=store,
        charge_contract=authority_contracts.charge_contract,
        limits=authority_contracts.limits,
    )
    terminal_contract = build_default_checkpoint_terminal_contract()
    context = EventContext(
        run_id=run_start.run_id,
        turn_id=run_start.turn_id,
        reply_id=run_start.reply_id,
    )
    installed = install_checkpoint_barrier(
        coordinator=coordinator,
        context=context,
        prepared=prepared,
        checkpoint_burst_contract=(
            authority_contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.CHECKPOINT_COMMIT
            ).contract
        ),
        terminal_contract=terminal_contract,
        deadline_monotonic=deadline_monotonic,
    )
    try:
        write_reservation = prepare_authority_artifact_write_reservation(
            operation_id=checkpoint_id,
            owner_kind="checkpoint_materialization",
            artifacts=prepared.materialization.artifacts,
            limits=authority_contracts.limits,
            absolute_deadline_monotonic=deadline_monotonic,
        )
        persist_prepared_transcript_projection_materialization(
            prepared.materialization,
            write_reservation=write_reservation,
            limits=authority_contracts.limits,
            archive=archive,
            runtime_session_id=runtime_session_id,
            run_id=run_start.run_id,
            deadline_monotonic=deadline_monotonic,
        )
    except BaseException:
        commit_checkpoint_failure(
            coordinator=coordinator,
            context=context,
            installed=installed,
            terminal_contract=terminal_contract,
            reason_code=CheckpointFailureReasonCode.ARTIFACT_WRITE_FAILED,
            deadline_monotonic=deadline_monotonic,
        )
        raise
    committed = commit_checkpoint_success(
        coordinator=coordinator,
        context=context,
        installed=installed,
        terminal_contract=terminal_contract,
        deadline_monotonic=deadline_monotonic,
    )
    return checkpoint_id, committed.resulting_account_state


def _entry_semantics(entries) -> tuple[str, ...]:
    return tuple(item.semantic_identity.semantic_fingerprint for item in entries)


__all__ = [
    "TranscriptProjectionDoctorOutcome",
    "TranscriptProjectionDoctorReport",
    "verify_or_rebuild_transcript_projection_checkpoint",
]
