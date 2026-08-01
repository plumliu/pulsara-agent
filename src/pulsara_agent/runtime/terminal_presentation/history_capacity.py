"""Single-owner presentation history growth admission and reservation accounting."""

from __future__ import annotations

from threading import RLock

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_checkpoint_storage import (
    PresentationHistoryCapacityCheckpointFact,
    PresentationHistoryCapacityReservationCheckpointEntryFact,
)
from pulsara_agent.primitives.presentation_history import (
    AvailableHistoryCapacityFact,
    HistoryCapacityReconciliationRequiredFact,
    HistorySessionRotationRequiredFact,
    HistoryTreeCapacityExhaustedFact,
    PresentationHistoryCapacityAdmissionDecisionFact,
    PresentationHistoryCapacityStateFact,
    PresentationHistoryGrowthQuoteFact,
    PresentationHistoryGrowthQuotePolicyFact,
    PresentationHistoryGrowthReservationFact,
    PresentationHistoryMaterializationPolicyFact,
)
from pulsara_agent.primitives.storage_frozen import build_frozen_storage_fact


class PresentationHistoryCapacityError(RuntimeError):
    """A caller attempted to bypass the frozen history-capacity contract."""


class PresentationHistoryGrowthQuotePolicyRegistry:
    """Historical exact resolver for immutable quote-policy bindings."""

    def __init__(self) -> None:
        self._by_identity: dict[
            tuple[str, str, str], PresentationHistoryGrowthQuotePolicyFact
        ] = {}
        self._fingerprint_by_version: dict[tuple[str, str], str] = {}

    def register(self, policy: PresentationHistoryGrowthQuotePolicyFact) -> None:
        key = (policy.quote_policy_id, policy.quote_policy_version)
        existing_fingerprint = self._fingerprint_by_version.get(key)
        if (
            existing_fingerprint is not None
            and existing_fingerprint != policy.quote_policy_fingerprint
        ):
            raise PresentationHistoryCapacityError(
                "presentation history quote-policy configuration conflict"
            )
        identity = (*key, policy.quote_policy_fingerprint)
        existing = self._by_identity.get(identity)
        if existing is not None and existing != policy:
            raise PresentationHistoryCapacityError(
                "presentation history quote-policy payload conflict"
            )
        self._fingerprint_by_version[key] = policy.quote_policy_fingerprint
        self._by_identity[identity] = policy

    def resolve_exact(
        self, policy_id: str, policy_version: str, policy_fingerprint: str
    ) -> PresentationHistoryGrowthQuotePolicyFact:
        try:
            return self._by_identity[(policy_id, policy_version, policy_fingerprint)]
        except KeyError as exc:
            raise PresentationHistoryCapacityError(
                "presentation history quote-policy binding is unavailable"
            ) from exc


class PresentationHistoryCapacityOwner:
    """Serializes quote, admission, settlement, and hard-fence transitions."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        materialization_policy: PresentationHistoryMaterializationPolicyFact,
        quote_registry: PresentationHistoryGrowthQuotePolicyRegistry | None = None,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.policy = materialization_policy
        self.quote_registry = (
            quote_registry or PresentationHistoryGrowthQuotePolicyRegistry()
        )
        self.quote_registry.register(materialization_policy.growth_quote_policy)
        self._lock = RLock()
        self._reservations: dict[str, PresentationHistoryGrowthReservationFact] = {}
        self._source_winner: dict[str, str] = {}
        self._run_start_source_by_reservation: dict[str, ContextEventReferenceFact] = {}
        self._fault: (
            HistoryCapacityReconciliationRequiredFact
            | HistoryTreeCapacityExhaustedFact
            | None
        ) = None

    def derive_quote(
        self,
        *,
        admission_kind: str,
        source_authority_fingerprint: str,
    ) -> PresentationHistoryGrowthQuoteFact:
        policy = self.quote_registry.resolve_exact(
            self.policy.growth_quote_policy.quote_policy_id,
            self.policy.growth_quote_policy.quote_policy_version,
            self.policy.growth_quote_policy.quote_policy_fingerprint,
        )
        matches = tuple(
            item
            for item in policy.ordered_kind_bounds
            if item.admission_kind == admission_kind
        )
        if len(matches) != 1:
            raise PresentationHistoryCapacityError(
                "presentation history admission kind is not registered"
            )
        maximum = matches[0].maximum_new_history_entries
        quote_id = context_fingerprint(
            "presentation-history-growth-quote-id:v1",
            {
                "runtime_session_id": self.runtime_session_id,
                "admission_kind": admission_kind,
                "source_authority_fingerprint": source_authority_fingerprint,
                "quote_policy_id": policy.quote_policy_id,
                "quote_policy_version": policy.quote_policy_version,
                "quote_policy_fingerprint": policy.quote_policy_fingerprint,
            },
        )
        return build_frozen_fact(
            PresentationHistoryGrowthQuoteFact,
            schema_version="presentation_history_growth_quote.v1",
            growth_quote_id=f"history-quote:{quote_id.removeprefix('sha256:')}",
            runtime_session_id=self.runtime_session_id,
            admission_kind=admission_kind,
            source_authority_fingerprint=source_authority_fingerprint,
            quote_policy_id=policy.quote_policy_id,
            quote_policy_version=policy.quote_policy_version,
            quote_policy_fingerprint=policy.quote_policy_fingerprint,
            maximum_new_history_entries=maximum,
        )

    def decide(
        self,
        *,
        quote: PresentationHistoryGrowthQuoteFact,
        confirmed_entry_count: int,
        current_tail_worst_case_entry_count: int,
    ) -> PresentationHistoryCapacityAdmissionDecisionFact:
        with self._lock:
            remaining = self._active_remaining_unlocked()
            projected = (
                confirmed_entry_count
                + current_tail_worst_case_entry_count
                + remaining
                + quote.maximum_new_history_entries
            )
            capacity = self._capacity_state_unlocked(
                confirmed_entry_count=confirmed_entry_count,
                current_tail_worst_case_entry_count=current_tail_worst_case_entry_count,
            )
            if self._fault is not None:
                disposition = self._fault.capacity_kind
                resulting = self._fault
            elif projected > self.policy.tree_contract.maximum_representable_entries:
                disposition = "tree_capacity_exhausted"
                resulting = build_frozen_fact(
                    HistoryTreeCapacityExhaustedFact,
                    schema_version="presentation_history_tree_capacity_exhausted.v1",
                    capacity_kind="tree_capacity_exhausted",
                    observed_entry_count=projected,
                    maximum_representable_entries=(
                        self.policy.tree_contract.maximum_representable_entries
                    ),
                    stable_fault_code="HISTORY_TREE_CAPACITY_EXHAUSTED",
                )
            elif projected > self.policy.capacity_soft_rotation_threshold_entries:
                disposition = "session_rotation_required"
                resulting = build_frozen_fact(
                    HistorySessionRotationRequiredFact,
                    schema_version="presentation_history_session_rotation_required.v1",
                    capacity_kind="session_rotation_required",
                    confirmed_entry_count=confirmed_entry_count,
                    current_tail_worst_case_entry_count=(
                        current_tail_worst_case_entry_count
                    ),
                    active_growth_reservation_remaining_entry_count=remaining,
                    projected_ordinary_entry_count=(
                        confirmed_entry_count
                        + current_tail_worst_case_entry_count
                        + remaining
                    ),
                    soft_rotation_threshold_entries=(
                        self.policy.capacity_soft_rotation_threshold_entries
                    ),
                    stable_reason="soft_threshold_reached",
                )
            else:
                disposition = "available"
                resulting = capacity
            return build_frozen_fact(
                PresentationHistoryCapacityAdmissionDecisionFact,
                schema_version="presentation_history_capacity_admission_decision.v1",
                runtime_session_id=self.runtime_session_id,
                source_active_head_fingerprint=quote.source_authority_fingerprint,
                requested_growth_quote_fingerprint=quote.quote_fingerprint,
                confirmed_entry_count=confirmed_entry_count,
                current_tail_worst_case_entry_count=(
                    current_tail_worst_case_entry_count
                ),
                active_growth_reservation_remaining_entry_count=remaining,
                requested_admission_growth_quote_entry_count=(
                    quote.maximum_new_history_entries
                ),
                projected_ordinary_entries=projected,
                soft_rotation_threshold_entries=(
                    self.policy.capacity_soft_rotation_threshold_entries
                ),
                terminalization_maintenance_reserve_entries=(
                    self.policy.terminalization_maintenance_reserve_entries
                ),
                maximum_representable_entries=(
                    self.policy.tree_contract.maximum_representable_entries
                ),
                disposition=disposition,
                resulting_capacity_state=resulting,
            )

    def reserve(
        self,
        *,
        quote: PresentationHistoryGrowthQuoteFact,
        decision: PresentationHistoryCapacityAdmissionDecisionFact,
        owner_kind: str,
        owner_id: str,
        owner_generation: int,
    ) -> PresentationHistoryGrowthReservationFact:
        if decision.disposition != "available":
            raise PresentationHistoryCapacityError(
                "history growth cannot reserve a rejected admission"
            )
        if (
            decision.requested_growth_quote_fingerprint != quote.quote_fingerprint
            or decision.source_active_head_fingerprint
            != quote.source_authority_fingerprint
        ):
            raise PresentationHistoryCapacityError(
                "history quote/decision join mismatch"
            )
        reservation_id = _growth_reservation_id(
            quote=quote,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )
        with self._lock:
            existing_source = self._source_winner.get(
                quote.source_authority_fingerprint
            )
            if existing_source is not None and existing_source != reservation_id:
                self._install_reconciliation_fault_unlocked(
                    "RESERVATION_AUTHORITY_CONFLICT"
                )
                raise PresentationHistoryCapacityError(
                    "history source authority has another reservation winner"
                )
            existing = self._reservations.get(reservation_id)
            if existing is not None:
                if (
                    existing.quote != quote
                    or existing.owner_kind != owner_kind
                    or existing.owner_id != owner_id
                ):
                    raise PresentationHistoryCapacityError(
                        "history reservation identity conflict"
                    )
                return existing
            if (
                sum(
                    item.reservation_state in {"reserved", "reconciliation_required"}
                    for item in self._reservations.values()
                )
                >= self.policy.growth_quote_policy.maximum_nonterminal_growth_reservations_per_session
            ):
                raise PresentationHistoryCapacityError(
                    "history reservation capacity exhausted"
                )
            reservation = build_frozen_fact(
                PresentationHistoryGrowthReservationFact,
                schema_version="presentation_history_growth_reservation.v1",
                growth_reservation_id=reservation_id,
                quote=quote,
                owner_kind=owner_kind,
                owner_id=owner_id,
                owner_generation=owner_generation,
                reservation_revision=0,
                previous_reservation_fingerprint=None,
                settled_materialized_entry_count=0,
                remaining_unmaterialized_entry_count=(
                    quote.maximum_new_history_entries
                ),
                reservation_state="reserved",
            )
            self._reservations[reservation_id] = reservation
            self._source_winner[quote.source_authority_fingerprint] = reservation_id
            return reservation

    def ensure_recovered_host_run_reservation(
        self,
        *,
        run_id: str,
        source_run_start_event_reference: ContextEventReferenceFact,
    ) -> PresentationHistoryGrowthReservationFact:
        """Restore the pre-commit admission when its RunStart is durable.

        This is not a fresh capacity decision.  Reopen starts from a checkpointed
        reservation state or from a prefix immediately before the exact RunStart;
        the restored tail then settles every positive mutation in sequence order.
        """

        source_fingerprint = presentation_run_growth_source_fingerprint(
            runtime_session_id=self.runtime_session_id,
            run_id=run_id,
        )
        quote = self.derive_quote(
            admission_kind="run_activation",
            source_authority_fingerprint=source_fingerprint,
        )
        reservation_id = _growth_reservation_id(
            quote=quote,
            owner_kind="host_run",
            owner_id=run_id,
        )
        with self._lock:
            existing = self._reservations.get(reservation_id)
            if existing is None:
                existing = build_frozen_fact(
                    PresentationHistoryGrowthReservationFact,
                    schema_version="presentation_history_growth_reservation.v1",
                    growth_reservation_id=reservation_id,
                    quote=quote,
                    owner_kind="host_run",
                    owner_id=run_id,
                    owner_generation=1,
                    reservation_revision=0,
                    previous_reservation_fingerprint=None,
                    settled_materialized_entry_count=0,
                    remaining_unmaterialized_entry_count=(
                        quote.maximum_new_history_entries
                    ),
                    reservation_state="reserved",
                )
                self._reservations[reservation_id] = existing
                self._source_winner[source_fingerprint] = reservation_id
            elif (
                existing.quote != quote
                or existing.owner_kind != "host_run"
                or existing.owner_id != run_id
                or existing.reservation_state
                not in {"reserved", "reconciliation_required"}
            ):
                raise PresentationHistoryCapacityError(
                    "recovered history reservation conflicts with RunStart"
                )
            self.bind_run_start_source(
                reservation_id,
                source_run_start_event_reference=source_run_start_event_reference,
            )
            return existing

    def bind_run_start_source(
        self,
        reservation_id: str,
        *,
        source_run_start_event_reference: ContextEventReferenceFact,
    ) -> None:
        if (
            source_run_start_event_reference.runtime_session_id
            != self.runtime_session_id
            or source_run_start_event_reference.event_type != "RUN_START"
        ):
            raise PresentationHistoryCapacityError(
                "history reservation RunStart source is invalid"
            )
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation.owner_kind != "host_run":
                raise PresentationHistoryCapacityError(
                    "history reservation RunStart owner is unavailable"
                )
            existing = self._run_start_source_by_reservation.get(reservation_id)
            if existing is not None and existing != source_run_start_event_reference:
                self._install_reconciliation_fault_unlocked(
                    "RESERVATION_RUN_START_SOURCE_CONFLICT"
                )
                raise PresentationHistoryCapacityError(
                    "history reservation RunStart source conflicts"
                )
            self._run_start_source_by_reservation[reservation_id] = (
                source_run_start_event_reference
            )

    def active_reservation_for_owner(
        self, *, owner_kind: str, owner_id: str
    ) -> PresentationHistoryGrowthReservationFact | None:
        with self._lock:
            matches = tuple(
                item
                for item in self._reservations.values()
                if item.owner_kind == owner_kind
                and item.owner_id == owner_id
                and item.reservation_state in {"reserved", "reconciliation_required"}
            )
            if len(matches) > 1:
                raise PresentationHistoryCapacityError(
                    "history owner has multiple active reservations"
                )
            return matches[0] if matches else None

    def rebind_active_owner(
        self, *, owner_kind: str, owner_id: str
    ) -> PresentationHistoryGrowthReservationFact:
        """Install a new process generation on one checkpoint-restored owner."""

        with self._lock:
            previous = self.active_reservation_for_owner(
                owner_kind=owner_kind,
                owner_id=owner_id,
            )
            if previous is None:
                raise PresentationHistoryCapacityError(
                    "recovered history owner lacks its active reservation"
                )
            current = build_frozen_fact(
                PresentationHistoryGrowthReservationFact,
                schema_version="presentation_history_growth_reservation.v1",
                growth_reservation_id=previous.growth_reservation_id,
                quote=previous.quote,
                owner_kind=previous.owner_kind,
                owner_id=previous.owner_id,
                owner_generation=previous.owner_generation + 1,
                reservation_revision=previous.reservation_revision + 1,
                previous_reservation_fingerprint=previous.reservation_fingerprint,
                settled_materialized_entry_count=(
                    previous.settled_materialized_entry_count
                ),
                remaining_unmaterialized_entry_count=(
                    previous.remaining_unmaterialized_entry_count
                ),
                reservation_state=previous.reservation_state,
            )
            self._reservations[previous.growth_reservation_id] = current
            return current

    def settle_growth(
        self, reservation_id: str, *, positive_entry_growth: int
    ) -> PresentationHistoryGrowthReservationFact:
        if positive_entry_growth < 0:
            raise ValueError("history positive growth cannot be negative")
        with self._lock:
            previous = self._reservations[reservation_id]
            if previous.reservation_state != "reserved":
                raise PresentationHistoryCapacityError(
                    "history reservation is not materializable"
                )
            if positive_entry_growth > previous.remaining_unmaterialized_entry_count:
                self._install_reconciliation_fault_unlocked(
                    "HISTORY_GROWTH_QUOTE_EXCEEDED"
                )
                next_state = "reconciliation_required"
                settled = previous.settled_materialized_entry_count
                remaining = previous.remaining_unmaterialized_entry_count
            else:
                next_state = "reserved"
                settled = (
                    previous.settled_materialized_entry_count + positive_entry_growth
                )
                remaining = (
                    previous.remaining_unmaterialized_entry_count
                    - positive_entry_growth
                )
            current = build_frozen_fact(
                PresentationHistoryGrowthReservationFact,
                schema_version="presentation_history_growth_reservation.v1",
                growth_reservation_id=previous.growth_reservation_id,
                quote=previous.quote,
                owner_kind=previous.owner_kind,
                owner_id=previous.owner_id,
                owner_generation=previous.owner_generation,
                reservation_revision=previous.reservation_revision + 1,
                previous_reservation_fingerprint=previous.reservation_fingerprint,
                settled_materialized_entry_count=settled,
                remaining_unmaterialized_entry_count=remaining,
                reservation_state=next_state,
            )
            self._reservations[reservation_id] = current
            return current

    def terminalize(
        self,
        reservation_id: str,
        *,
        outcome: str,
    ) -> PresentationHistoryGrowthReservationFact:
        if outcome not in {"settled", "released", "reconciliation_required"}:
            raise ValueError("unknown history reservation terminal outcome")
        with self._lock:
            previous = self._reservations[reservation_id]
            current = build_frozen_fact(
                PresentationHistoryGrowthReservationFact,
                schema_version="presentation_history_growth_reservation.v1",
                growth_reservation_id=previous.growth_reservation_id,
                quote=previous.quote,
                owner_kind=previous.owner_kind,
                owner_id=previous.owner_id,
                owner_generation=previous.owner_generation,
                reservation_revision=previous.reservation_revision + 1,
                previous_reservation_fingerprint=previous.reservation_fingerprint,
                settled_materialized_entry_count=(
                    previous.settled_materialized_entry_count
                ),
                remaining_unmaterialized_entry_count=(
                    0
                    if outcome in {"settled", "released"}
                    else previous.remaining_unmaterialized_entry_count
                ),
                reservation_state=outcome,
            )
            self._reservations[reservation_id] = current
            if outcome in {"settled", "released"}:
                self._source_winner.pop(
                    previous.quote.source_authority_fingerprint, None
                )
                self._run_start_source_by_reservation.pop(reservation_id, None)
            return current

    def settle_committed_growth(
        self, *, positive_entry_growth: int
    ) -> tuple[PresentationHistoryGrowthReservationFact, ...]:
        """Charge committed positive growth to active reservations in stable order.

        Presentation is downstream of durable truth, so this method never rejects
        the fold.  It consumes the oldest live reservation first and installs the
        existing reconciliation fence if admitted capacity was underestimated.
        Growth with no ordinary reservation is terminal/recovery maintenance and
        is reflected by the confirmed root rather than an invented reservation.
        """

        if positive_entry_growth < 0:
            raise ValueError("history positive growth cannot be negative")
        if positive_entry_growth == 0:
            return ()
        with self._lock:
            remaining_growth = positive_entry_growth
            changed: list[PresentationHistoryGrowthReservationFact] = []
            for reservation_id, reservation in tuple(self._reservations.items()):
                if reservation.reservation_state != "reserved":
                    continue
                charge = min(
                    remaining_growth,
                    reservation.remaining_unmaterialized_entry_count,
                )
                if charge:
                    changed.append(
                        self.settle_growth(
                            reservation_id,
                            positive_entry_growth=charge,
                        )
                    )
                    remaining_growth -= charge
                if remaining_growth == 0:
                    break
            return tuple(changed)

    def reservation(
        self, reservation_id: str
    ) -> PresentationHistoryGrowthReservationFact | None:
        with self._lock:
            return self._reservations.get(reservation_id)

    def checkpoint_snapshot(
        self, *, through_authority_sequence: int
    ) -> PresentationHistoryCapacityCheckpointFact:
        if through_authority_sequence < 0:
            raise ValueError("capacity checkpoint high-water cannot be negative")
        with self._lock:
            entries = []
            for reservation_id, reservation in self._reservations.items():
                if reservation.reservation_state not in {
                    "reserved",
                    "reconciliation_required",
                }:
                    continue
                source = self._run_start_source_by_reservation.get(reservation_id)
                # A pre-commit reservation is intentionally process-local.  It
                # becomes checkpointable only after its exact RunStart is folded.
                if source is None:
                    continue
                if source.sequence > through_authority_sequence:
                    raise PresentationHistoryCapacityError(
                        "capacity checkpoint moved before reservation source"
                    )
                entries.append(
                    build_frozen_storage_fact(
                        PresentationHistoryCapacityReservationCheckpointEntryFact,
                        schema_version=(
                            "presentation_history_capacity_reservation_checkpoint_entry.v1"
                        ),
                        reservation=reservation,
                        source_run_start_event_reference=source,
                    )
                )
            entries.sort(
                key=lambda item: (
                    item.source_run_start_event_reference.sequence,
                    item.reservation.growth_reservation_id,
                )
            )
            return build_frozen_storage_fact(
                PresentationHistoryCapacityCheckpointFact,
                schema_version="presentation_history_capacity_checkpoint.v1",
                runtime_session_id=self.runtime_session_id,
                through_authority_sequence=through_authority_sequence,
                quote_policy_fingerprint=(
                    self.policy.growth_quote_policy.quote_policy_fingerprint
                ),
                ordered_active_reservations=tuple(entries),
                fault=self._fault,
            )

    def restore_checkpoint(
        self, checkpoint: PresentationHistoryCapacityCheckpointFact
    ) -> None:
        if (
            checkpoint.runtime_session_id != self.runtime_session_id
            or checkpoint.quote_policy_fingerprint
            != self.policy.growth_quote_policy.quote_policy_fingerprint
        ):
            raise PresentationHistoryCapacityError(
                "capacity checkpoint binding mismatch"
            )
        with self._lock:
            reservations = {
                item.reservation.growth_reservation_id: item.reservation
                for item in checkpoint.ordered_active_reservations
            }
            source_winner = {
                item.reservation.quote.source_authority_fingerprint: (
                    item.reservation.growth_reservation_id
                )
                for item in checkpoint.ordered_active_reservations
            }
            sources = {
                item.reservation.growth_reservation_id: (
                    item.source_run_start_event_reference
                )
                for item in checkpoint.ordered_active_reservations
            }
            self._reservations = reservations
            self._source_winner = source_winner
            self._run_start_source_by_reservation = sources
            self._fault = checkpoint.fault

    def capacity_state(
        self,
        *,
        confirmed_entry_count: int,
        current_tail_worst_case_entry_count: int,
    ) -> PresentationHistoryCapacityStateFact:
        with self._lock:
            return self._capacity_state_unlocked(
                confirmed_entry_count=confirmed_entry_count,
                current_tail_worst_case_entry_count=current_tail_worst_case_entry_count,
            )

    def _active_remaining_unlocked(self) -> int:
        return sum(
            item.remaining_unmaterialized_entry_count
            for item in self._reservations.values()
            if item.reservation_state in {"reserved", "reconciliation_required"}
        )

    def _capacity_state_unlocked(
        self,
        *,
        confirmed_entry_count: int,
        current_tail_worst_case_entry_count: int,
    ) -> PresentationHistoryCapacityStateFact:
        if self._fault is not None:
            return self._fault
        remaining = self._active_remaining_unlocked()
        projected = (
            confirmed_entry_count + current_tail_worst_case_entry_count + remaining
        )
        if projected > self.policy.tree_contract.maximum_representable_entries:
            self._fault = build_frozen_fact(
                HistoryTreeCapacityExhaustedFact,
                schema_version="presentation_history_tree_capacity_exhausted.v1",
                capacity_kind="tree_capacity_exhausted",
                observed_entry_count=projected,
                maximum_representable_entries=(
                    self.policy.tree_contract.maximum_representable_entries
                ),
                stable_fault_code="HISTORY_TREE_CAPACITY_EXHAUSTED",
            )
            return self._fault
        if (
            projected + self.policy.minimum_ordinary_growth_quote_entries
            > self.policy.capacity_soft_rotation_threshold_entries
        ):
            return build_frozen_fact(
                HistorySessionRotationRequiredFact,
                schema_version="presentation_history_session_rotation_required.v1",
                capacity_kind="session_rotation_required",
                confirmed_entry_count=confirmed_entry_count,
                current_tail_worst_case_entry_count=(
                    current_tail_worst_case_entry_count
                ),
                active_growth_reservation_remaining_entry_count=remaining,
                projected_ordinary_entry_count=projected,
                soft_rotation_threshold_entries=(
                    self.policy.capacity_soft_rotation_threshold_entries
                ),
                stable_reason="minimum_quote_unavailable",
            )
        return build_frozen_fact(
            AvailableHistoryCapacityFact,
            schema_version="presentation_history_capacity_available.v1",
            capacity_kind="available",
            confirmed_entry_count=confirmed_entry_count,
            current_tail_worst_case_entry_count=current_tail_worst_case_entry_count,
            active_growth_reservation_remaining_entry_count=remaining,
            projected_ordinary_entry_count=projected,
            soft_rotation_threshold_entries=(
                self.policy.capacity_soft_rotation_threshold_entries
            ),
            minimum_ordinary_growth_quote_entries=(
                self.policy.minimum_ordinary_growth_quote_entries
            ),
        )

    def _install_reconciliation_fault_unlocked(self, fault_code: str) -> None:
        self._fault = build_frozen_fact(
            HistoryCapacityReconciliationRequiredFact,
            schema_version="presentation_history_capacity_reconciliation_required.v1",
            capacity_kind="capacity_reconciliation_required",
            stable_fault_code=fault_code,
            trusted_active_head_fingerprint=None,
        )


def presentation_run_growth_source_fingerprint(
    *, runtime_session_id: str, run_id: str
) -> str:
    """Stable pre-commit source candidate, revalidated against durable RunStart."""

    return context_fingerprint(
        "presentation-history-run-activation-source:v1",
        {
            "runtime_session_id": runtime_session_id,
            "run_id": run_id,
        },
    )


def _growth_reservation_id(
    *, quote: PresentationHistoryGrowthQuoteFact, owner_kind: str, owner_id: str
) -> str:
    digest = context_fingerprint(
        "presentation-history-growth-reservation-id:v1",
        {
            "growth_quote_id": quote.growth_quote_id,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
        },
    )
    return f"history-reservation:{digest.removeprefix('sha256:')}"


__all__ = [
    "PresentationHistoryCapacityError",
    "PresentationHistoryCapacityOwner",
    "PresentationHistoryGrowthQuotePolicyRegistry",
    "presentation_run_growth_source_fingerprint",
]
