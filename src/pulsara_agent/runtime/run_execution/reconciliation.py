"""Central run-reconciliation owner and closed repair reducer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.ports.run_authority import InstalledRunAuthorityRevision
from pulsara_agent.ports.run_execution import (
    LedgerHorizonFact,
    ReconciliationConfirmation,
    ReconciliationFullConfirmation,
    ReconciliationResolutionReceipt,
    RunReconciliationSnapshot,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.run_execution.commit_gateway import (
    confirm_event_batch,
    event_batch_candidate_identity,
    read_ledger_horizon,
)
from pulsara_agent.runtime.run_execution.owner import (
    ActiveRunActivation,
    ActiveRunSuspension,
    BoundRunResources,
    RunFinalizationOwner,
    RunOwner,
    RunReconciliationOwner,
)
from pulsara_agent.runtime.run_execution.registry import RunExecutionRegistry
from pulsara_agent.runtime.run_execution.snapshot import build_owner_state_identity
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.event import RunEndEvent


class RunReconciliationService:
    """Own bounded physical confirmations and the only repair-state reducer."""

    def __init__(self, *, registry: RunExecutionRegistry, event_log: EventLog) -> None:
        self._registry = registry
        self._event_log = event_log
        self._accepting = True

    def install_event_batch(
        self,
        *,
        run_id: str,
        attempt_kind: str,
        candidates: Sequence[AgentEvent],
        repair_mode: str = "live_resident",
        resident_owner_generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> RunReconciliationSnapshot:
        candidate_id, candidate_fingerprint = event_batch_candidate_identity(candidates)
        horizon = read_ledger_horizon(
            self._event_log,
            deadline_monotonic=deadline_monotonic,
        )
        return self.install(
            run_id=run_id,
            attempt_kind=attempt_kind,
            stable_candidate_id=candidate_id,
            stable_candidate_fingerprint=candidate_fingerprint,
            expected_ledger_horizon=horizon,
            repair_mode=repair_mode,
            resident_owner_generation=resident_owner_generation,
        )

    def current_ledger_horizon(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> LedgerHorizonFact:
        return read_ledger_horizon(
            self._event_log,
            deadline_monotonic=deadline_monotonic,
        )

    def install(
        self,
        *,
        run_id: str,
        attempt_kind: str,
        stable_candidate_id: str,
        stable_candidate_fingerprint: str,
        expected_ledger_horizon: LedgerHorizonFact,
        repair_mode: str,
        resident_owner_generation: int | None,
    ) -> RunReconciliationSnapshot:
        if not self._accepting:
            raise RuntimeError("run reconciliation service is closing")
        owner = self._registry.require(run_id)
        existing = owner.reconciliation_owner
        if existing is not None:
            requested = (
                attempt_kind,
                stable_candidate_id,
                stable_candidate_fingerprint,
                repair_mode,
                resident_owner_generation,
            )
            installed = (
                existing.snapshot.active_attempt_kind,
                existing.snapshot.stable_candidate_id,
                existing.snapshot.stable_candidate_fingerprint,
                existing.snapshot.repair_mode,
                existing.snapshot.resident_owner_generation,
            )
            if requested != installed:
                raise RuntimeError(
                    "run already owns a different reconciliation attempt"
                )
            return existing.snapshot
        prior_state = build_owner_state_identity(owner)
        payload = {
            "schema_version": 1,
            "repair_mode": repair_mode,
            "prior_state": prior_state,
            "active_attempt_kind": attempt_kind,
            "stable_candidate_id": stable_candidate_id,
            "stable_candidate_fingerprint": stable_candidate_fingerprint,
            "expected_ledger_horizon": expected_ledger_horizon,
            "resident_owner_generation": resident_owner_generation,
        }
        snapshot = RunReconciliationSnapshot(
            **payload,
            snapshot_fingerprint=context_fingerprint(
                "run-reconciliation-snapshot:v1",
                payload,
            ),
        )
        owner.reconciliation_owner = RunReconciliationOwner(snapshot=snapshot)
        owner.lifecycle = "reconciliation_required"
        if attempt_kind in {
            "run_end_commit",
            "final_output_materialization",
            "publication_terminal_maintenance",
        }:
            finalization = owner.finalization_slot.owner
            if isinstance(finalization, RunFinalizationOwner):
                finalization.state = "reconciliation_required"
                owner.finalization_slot.state = "reconciliation_required"
        return snapshot

    async def confirm_stable_candidate(
        self,
        *,
        run_id: str,
        candidates: Sequence[AgentEvent],
        deadline_monotonic: float,
    ) -> ReconciliationResolutionReceipt:
        owner = self._registry.require(run_id)
        reconciliation = self._require_owner(owner)
        if reconciliation.confirmation_task is not None:
            task = reconciliation.confirmation_task
        else:
            reconciliation.physical_attempt_generation += 1
            generation = reconciliation.physical_attempt_generation
            reconciliation.state = "confirming"
            task = asyncio.create_task(
                asyncio.to_thread(
                    confirm_event_batch,
                    self._event_log,
                    runtime_session_id=owner.identity.runtime_session_id,
                    candidates=tuple(candidates),
                    deadline_monotonic=deadline_monotonic,
                ),
                name=f"run-reconciliation:{run_id}:{generation}",
            )
            reconciliation.confirmation_task = task
            task.add_done_callback(_consume_task_exception)
        confirmation = await asyncio.shield(task)
        if reconciliation.confirmation_task is task:
            reconciliation.confirmation_task = None
        if (
            isinstance(confirmation, ReconciliationFullConfirmation)
            and reconciliation.snapshot.active_attempt_kind == "run_end_commit"
        ):
            run_ends = tuple(
                candidate
                for candidate in candidates
                if isinstance(candidate, RunEndEvent)
            )
            if len(run_ends) != 1 or run_ends[0].sequence is None:
                raise RuntimeError("run-end reconciliation lost its exact terminal")
            finalization = owner.finalization_slot.owner
            if not isinstance(finalization, RunFinalizationOwner):
                raise RuntimeError("run-end reconciliation lost finalization owner")
            finalization.confirmed_run_end_event_reference = (
                event_reference_from_stored(
                    run_ends[0],
                    runtime_session_id=owner.identity.runtime_session_id,
                )
            )
        return self.resolve(
            run_id=run_id,
            confirmation=confirmation,
            physical_attempt_generation=reconciliation.physical_attempt_generation,
        )

    def resolve(
        self,
        *,
        run_id: str,
        confirmation: ReconciliationConfirmation,
        physical_attempt_generation: int,
    ) -> ReconciliationResolutionReceipt:
        owner = self._registry.require(run_id)
        reconciliation = self._require_owner(owner)
        snapshot = reconciliation.snapshot
        if physical_attempt_generation != reconciliation.physical_attempt_generation:
            raise RuntimeError("stale reconciliation physical attempt generation")

        retry_owner_retained = True
        if isinstance(confirmation, ReconciliationFullConfirmation):
            if (
                confirmation.stored_candidate_id != snapshot.stable_candidate_id
                or confirmation.stored_candidate_fingerprint
                != snapshot.stable_candidate_fingerprint
            ):
                raise RuntimeError("FULL reconciliation candidate identity mismatch")
            retry_owner_retained = not self._apply_full(owner, snapshot)
        else:
            owner.lifecycle = "reconciliation_required"

        resulting_state = build_owner_state_identity(owner)
        payload = {
            "schema_version": 1,
            "snapshot_fingerprint": snapshot.snapshot_fingerprint,
            "physical_attempt_generation": physical_attempt_generation,
            "confirmation": confirmation,
            "resulting_state": resulting_state,
            "retry_owner_retained": retry_owner_retained,
        }
        receipt = ReconciliationResolutionReceipt(
            **payload,
            receipt_fingerprint=context_fingerprint(
                "run-reconciliation-resolution:v1",
                payload,
            ),
        )
        reconciliation.resolution_receipt = receipt
        if retry_owner_retained:
            reconciliation.state = {
                "none": "retry_wait",
                "conflict": "conflict",
                "unresolved": "unresolved",
                "full": "retry_wait",
            }[confirmation.disposition]
        else:
            reconciliation.state = "resolved"
            self._settle_reconciliation_carrier(owner, reconciliation)
            owner.reconciliation_resolution_history[snapshot.snapshot_fingerprint] = (
                receipt
            )
            owner.reconciliation_owner = None
        return receipt

    @staticmethod
    def _settle_reconciliation_carrier(
        owner: RunOwner,
        reconciliation: RunReconciliationOwner,
    ) -> None:
        carrier = reconciliation.state_carrier
        token = reconciliation.state_owner_token
        if carrier is None or token is None:
            return
        if owner.lifecycle == "initializing":
            target = (
                f"run-pending:{owner.identity.owner_fingerprint}:"
                f"reconciliation:{reconciliation.snapshot.snapshot_fingerprint}"
            )
            carrier.transfer(expected_owner_token=token, new_owner_token=target)
            owner.pending_activation_state = carrier
            owner.pending_activation_owner_token = target
        elif owner.lifecycle in {"terminal", "terminalizing"}:
            finalization = owner.finalization_slot.owner
            if not isinstance(finalization, RunFinalizationOwner):
                raise RuntimeError("reconciled terminal run lacks finalization owner")
            target = f"finalization:{owner.identity.owner_fingerprint}"
            carrier.transfer(expected_owner_token=token, new_owner_token=target)
            finalization.state_carrier = carrier
            finalization.state_owner_token = target
        else:
            raise RuntimeError("reconciliation carrier has no legal resulting owner")
        reconciliation.state_carrier = None
        reconciliation.state_owner_token = None

    @staticmethod
    def _apply_full(owner: RunOwner, snapshot: RunReconciliationSnapshot) -> bool:
        kind = snapshot.active_attempt_kind
        if kind == "run_end_commit":
            finalization = owner.finalization_slot.owner
            if not isinstance(finalization, RunFinalizationOwner):
                return False
            finalization.commit_state = "confirmed"
            finalization.state = "full_output_pending"
            finalization.run_end_candidate = None
            finalization.terminal_candidates = ()
            owner.finalization_slot.state = "run_end_full_pending_output"
            owner.lifecycle = "terminal"
            return True
        if kind == "final_output_materialization":
            finalization = owner.finalization_slot.owner
            if (
                not isinstance(finalization, RunFinalizationOwner)
                or finalization.terminal_receipt is None
            ):
                return False
            finalization.state = "completed"
            owner.finalization_slot.state = "completed"
            owner.finalization_slot.receipt = finalization.terminal_receipt
            owner.lifecycle = "terminal"
            return True
        if kind == "suspension_commit":
            if isinstance(owner.suspension_slot, ActiveRunSuspension):
                owner.lifecycle = "suspended"
                return True
            return False
        if kind in {
            "initial_authority_commit",
            "continuation_authority_commit",
            "interaction_resolution_commit",
        }:
            if not isinstance(owner.authority_head, InstalledRunAuthorityRevision):
                return False
            return RunReconciliationService._restore_dispatchable_or_initializing(
                owner, snapshot
            )
        if kind in {"activation_installation", "resource_rebind"}:
            return RunReconciliationService._restore_dispatchable_or_initializing(
                owner, snapshot
            )
        if kind == "publication_terminal_maintenance":
            owner.lifecycle = (
                "terminal"
                if owner.finalization_owner.commit_state == "confirmed"
                else "terminalizing"
            )
            return True
        return False

    @staticmethod
    def _restore_dispatchable_or_initializing(
        owner: RunOwner,
        snapshot: RunReconciliationSnapshot,
    ) -> bool:
        live_activation = isinstance(owner.activation_slot, ActiveRunActivation)
        bound = isinstance(owner.resource_slot, BoundRunResources)
        if (
            snapshot.repair_mode == "live_resident"
            and live_activation
            and bound
            and owner.active_segment is not None
            and owner.active_segment.segment_generation
            == snapshot.resident_owner_generation
        ):
            owner.lifecycle = "open"
        else:
            owner.lifecycle = "initializing"
        return True

    @staticmethod
    def _require_owner(owner: RunOwner) -> RunReconciliationOwner:
        reconciliation = owner.reconciliation_owner
        if reconciliation is None or owner.lifecycle != "reconciliation_required":
            raise RuntimeError("run has no active reconciliation owner")
        return reconciliation

    async def drain(self, *, deadline_monotonic: float) -> None:
        self._accepting = False
        tasks = tuple(
            owner.reconciliation_owner.confirmation_task
            for owner in self._registry.owners()
            if owner.reconciliation_owner is not None
            and owner.reconciliation_owner.confirmation_task is not None
            and not owner.reconciliation_owner.confirmation_task.done()
        )
        for task in dict.fromkeys(tasks):
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("run reconciliation drain deadline exceeded")
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


__all__ = ["RunReconciliationService"]
