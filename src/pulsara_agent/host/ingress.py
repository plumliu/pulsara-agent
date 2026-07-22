"""Unified Host ingress arbitration for human, resume, and runtime requests."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from typing import Any, Awaitable, Callable, Literal

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.host_ingress import (
    ActiveRunMonitorSafePointCommitGuardFact,
    HostActiveRunMonitorDeliveryFact,
    HostIngressAdmissionProofFact,
    HostIngressCoordinatorStateFact,
    HostRuntimeNotificationAttachmentFact,
)
from pulsara_agent.primitives.terminal_observation import TerminalAutonomousDeliveryFact


IngressKind = Literal["human", "resume", "runtime"]
IngressOwnerState = Literal[
    "queued",
    "preparing",
    "committed",
    "withdrawn",
    "replan_required",
    "reconciliation_required",
    "finished",
]


class HostIngressCapacityError(RuntimeError):
    pass


class HostIngressClosedError(RuntimeError):
    pass


class HostIngressWaitingUserError(RuntimeError):
    pass


class HostIngressAdmissionStale(RuntimeError):
    pass


@dataclass(slots=True)
class HostIngressAttemptOwner:
    ingress_id: str
    kind: IngressKind
    payload: Any
    runner: Callable[["HostIngressAttemptOwner"], Awaitable[Any]]
    result: asyncio.Future[Any]
    runner_task: asyncio.Task[Any] | None = None
    owner_state: IngressOwnerState = "queued"
    accepted_ingress_ordinal: int | None = None
    admission_generation: int | None = None
    caller_attached: bool = True
    selected_notification_head_fingerprints: tuple[str, ...] = ()
    selected_notifications: tuple[Any, ...] = ()
    expected_autonomy_chain_state_fingerprint: str | None = None
    proposed_automatic_delivery_ordinal: int | None = None
    selection_wake_chain_id: str | None = None
    resume_match_key: str | None = None
    replan_cleanup: Callable[["HostIngressAttemptOwner"], Awaitable[None]] | None = None
    replan_count: int = 0
    selection_queue_revision: int | None = None


@dataclass(frozen=True, slots=True)
class ActiveRunMonitorSafePointLease:
    """Process-local owner retained until one ModelStart is confirmed FULL."""

    lease_id: str
    runtime_session_id: str
    run_id: str
    next_model_call_index: int
    source_events: tuple[Any, ...]
    attachments: tuple[HostRuntimeNotificationAttachmentFact, ...]
    selected_notification_head_fingerprints: tuple[str, ...]
    notification_state_fingerprint: str
    wake_chain_id: str
    expected_autonomy_chain_state_fingerprint: str
    proposed_automatic_delivery_ordinal: int
    chain_policy_fingerprint: str
    host_state_generation: int
    permission_policy_revision: int
    permission_policy_fingerprint: str
    close_intent_revision: int
    stop_intent_revision: int
    termination_intent_revision: int
    active_segment_id: str
    active_segment_generation: int
    llm_lifecycle_generation: int
    run_start_event_reference: Any
    previous_model_call_end_event_reference: Any
    prior_model_control_disposition_reference: Any
    pending_interaction_frontier_fingerprint: str
    open_tool_pair_frontier_fingerprint: str


def build_active_run_monitor_delivery(
    *,
    lease: ActiveRunMonitorSafePointLease,
    provider_input_start_bundle: Any,
) -> HostActiveRunMonitorDeliveryFact:
    """Bind one borrowed notification set to the exact prepared append."""

    from pulsara_agent.primitives.provider_input import (
        ExistingAppendCommitGuardFact,
        RolloverGenerationCommitGuardFact,
    )

    provider_guard = (
        provider_input_start_bundle.prepared_candidate.generation_commit_guard
    )
    if isinstance(provider_guard, ExistingAppendCommitGuardFact):
        generation_id = provider_guard.generation_id
        revision = provider_guard.expected_revision
        core_fingerprint = provider_guard.expected_committed_core_state_fingerprint
    elif isinstance(provider_guard, RolloverGenerationCommitGuardFact):
        generation_id = provider_guard.old_generation_id
        revision = provider_guard.expected_old_revision
        core_fingerprint = provider_guard.expected_old_core_state_fingerprint
    else:
        raise HostIngressAdmissionStale(
            "active-run monitor delivery requires an existing provider generation"
        )
    guard = build_frozen_fact(
        ActiveRunMonitorSafePointCommitGuardFact,
        schema_version="active_run_monitor_safe_point_commit_guard.v1",
        runtime_session_id=lease.runtime_session_id,
        run_start_event_reference=lease.run_start_event_reference,
        active_segment_id=lease.active_segment_id,
        active_segment_generation=lease.active_segment_generation,
        expected_host_state_generation=lease.host_state_generation,
        expected_next_model_call_index=lease.next_model_call_index,
        expected_llm_lifecycle_generation=lease.llm_lifecycle_generation,
        expected_termination_intent_revision=lease.termination_intent_revision,
        expected_stop_intent_revision=lease.stop_intent_revision,
        expected_close_intent_revision=lease.close_intent_revision,
        expected_permission_policy_revision=lease.permission_policy_revision,
        expected_permission_policy_fingerprint=(lease.permission_policy_fingerprint),
        prior_model_control_disposition_reference=(
            lease.prior_model_control_disposition_reference
        ),
        previous_model_call_end_event_reference=(
            lease.previous_model_call_end_event_reference
        ),
        expected_provider_input_generation_id=generation_id,
        expected_provider_input_generation_revision=revision,
        expected_provider_input_committed_state_fingerprint=core_fingerprint,
        expected_pending_interaction_frontier_fingerprint=(
            lease.pending_interaction_frontier_fingerprint
        ),
        expected_open_tool_pair_frontier_fingerprint=(
            lease.open_tool_pair_frontier_fingerprint
        ),
        expected_notification_state_fingerprint=(lease.notification_state_fingerprint),
        expected_selected_notification_head_fingerprints=(
            lease.selected_notification_head_fingerprints
        ),
        expected_autonomy_chain_state_fingerprint=(
            lease.expected_autonomy_chain_state_fingerprint
        ),
        prepared_provider_input_append_fingerprint=(
            provider_input_start_bundle.prepared_candidate.candidate_fingerprint
        ),
    )
    attachment_fingerprints = tuple(
        item.attachment_fingerprint for item in lease.attachments
    )
    autonomy = build_frozen_fact(
        TerminalAutonomousDeliveryFact,
        schema_version="terminal_autonomous_delivery.v1",
        wake_chain_id=lease.wake_chain_id,
        ordered_source_attachment_fingerprints=attachment_fingerprints,
        delivery_kind="active_run_safe_point",
        automatic_delivery_ordinal=lease.proposed_automatic_delivery_ordinal,
        chain_policy_fingerprint=lease.chain_policy_fingerprint,
    )
    return build_frozen_fact(
        HostActiveRunMonitorDeliveryFact,
        schema_version="host_active_run_monitor_delivery.v1",
        commit_guard=guard,
        ordered_attachment_fingerprints=attachment_fingerprints,
        autonomy_delivery=autonomy,
    )


class HostIngressCoordinator:
    """One linearization point for every Host run boundary."""

    def __init__(
        self,
        *,
        host_session_id: str,
        maximum_queued_ingress: int = 32,
        permission_policy_fingerprint: str,
        selection_barrier: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.host_session_id = host_session_id
        self.maximum_queued_ingress = maximum_queued_ingress
        self._permission_policy_fingerprint = permission_policy_fingerprint
        self._permission_policy_revision = 0
        self._close_intent_revision = 0
        self._state_generation = 0
        self._queue_revision = 0
        self._admission_generation = 0
        self._accepted_ordinal = 0
        self._lifecycle_state: Literal[
            "open_idle",
            "preparing",
            "active",
            "waiting_user",
            "stopping",
            "closing",
            "closed",
            "latched",
        ] = "open_idle"
        self._active: HostIngressAttemptOwner | None = None
        self._queues: dict[IngressKind, list[HostIngressAttemptOwner]] = {
            "human": [],
            "resume": [],
            "runtime": [],
        }
        self._condition = asyncio.Condition()
        # Runtime event validation executes on the critical writer thread.
        # This lock is the no-await linearization domain shared by that CAS and
        # event-loop-owned ingress state transitions.
        self._state_lock = RLock()
        self._worker: asyncio.Task[None] | None = None
        self._selection_barrier = selection_barrier
        self._waiting_resume_match_key: str | None = None

    async def submit(
        self,
        *,
        kind: IngressKind,
        payload: Any,
        runner: Callable[[HostIngressAttemptOwner], Awaitable[Any]],
        ingress_id: str | None = None,
        selected_notification_head_fingerprints: tuple[str, ...] = (),
        selected_notifications: tuple[Any, ...] = (),
        expected_autonomy_chain_state_fingerprint: str | None = None,
        proposed_automatic_delivery_ordinal: int | None = None,
        selection_wake_chain_id: str | None = None,
        resume_match_key: str | None = None,
        replan_cleanup: Callable[[HostIngressAttemptOwner], Awaitable[None]]
        | None = None,
        reject_if_busy: bool = False,
    ) -> Any:
        loop = asyncio.get_running_loop()
        owner = HostIngressAttemptOwner(
            ingress_id=ingress_id or _stable_ingress_id(kind, id(payload), loop.time()),
            kind=kind,
            payload=payload,
            runner=runner,
            result=loop.create_future(),
            selected_notification_head_fingerprints=(
                selected_notification_head_fingerprints
            ),
            selected_notifications=selected_notifications,
            expected_autonomy_chain_state_fingerprint=(
                expected_autonomy_chain_state_fingerprint
            ),
            proposed_automatic_delivery_ordinal=(proposed_automatic_delivery_ordinal),
            selection_wake_chain_id=selection_wake_chain_id,
            resume_match_key=resume_match_key,
            replan_cleanup=replan_cleanup,
        )
        async with self._condition:
            with self._state_lock:
                if self._lifecycle_state in {"closing", "closed", "latched"}:
                    raise HostIngressClosedError("Host ingress is not accepting work")
                if self._lifecycle_state == "waiting_user" and (
                    kind != "resume"
                    or resume_match_key != self._waiting_resume_match_key
                ):
                    raise HostIngressWaitingUserError(
                        "Host ingress is waiting for its matching interaction resume"
                    )
                if reject_if_busy and (
                    self._active is not None or self._queued_count_locked() > 0
                ):
                    raise HostIngressCapacityError(
                        "Host ingress already has active work"
                    )
                if self._queued_count_locked() >= self.maximum_queued_ingress:
                    raise HostIngressCapacityError("Host ingress queue is full")
                self._queues[kind].append(owner)
                self._queue_revision += 1
                if self._worker is None or self._worker.done():
                    self._worker = asyncio.create_task(self._run(), name="host-ingress")
            self._condition.notify_all()
        try:
            return await asyncio.shield(owner.result)
        except asyncio.CancelledError:
            async with self._condition:
                with self._state_lock:
                    if owner.owner_state == "queued":
                        self._queues[kind].remove(owner)
                        owner.owner_state = "withdrawn"
                        self._queue_revision += 1
                        if not owner.result.done():
                            owner.result.cancel()
                    elif owner.owner_state == "preparing":
                        owner.caller_attached = False
            raise

    def admission_proof(
        self,
        owner: HostIngressAttemptOwner,
        *,
        ingress_fact_fingerprint: str,
    ) -> HostIngressAdmissionProofFact:
        with self._state_lock:
            return self._admission_proof_locked(
                owner,
                ingress_fact_fingerprint=ingress_fact_fingerprint,
            )

    def _admission_proof_locked(
        self,
        owner: HostIngressAttemptOwner,
        *,
        ingress_fact_fingerprint: str,
    ) -> HostIngressAdmissionProofFact:
        if (
            owner.owner_state != "preparing"
            or owner.accepted_ingress_ordinal is None
            or owner.admission_generation is None
            or self._active is not owner
        ):
            raise HostIngressAdmissionStale("Host ingress owner is not preparing")
        return build_frozen_fact(
            HostIngressAdmissionProofFact,
            schema_version="host_ingress_admission_proof.v1",
            admission_id=owner.ingress_id,
            admission_generation=owner.admission_generation,
            ingress_fact_fingerprint=ingress_fact_fingerprint,
            selected_ingress_item_ids=(owner.ingress_id,),
            selected_notification_head_fingerprints=(
                owner.selected_notification_head_fingerprints
            ),
            expected_host_state_generation=self._state_generation,
            expected_permission_policy_revision=self._permission_policy_revision,
            expected_permission_policy_fingerprint=(
                self._permission_policy_fingerprint
            ),
            expected_close_intent_revision=self._close_intent_revision,
            expected_autonomy_chain_state_fingerprint=(
                owner.expected_autonomy_chain_state_fingerprint
            ),
            proposed_automatic_delivery_ordinal=(
                owner.proposed_automatic_delivery_ordinal
            ),
        )

    def validate_precommit(
        self,
        owner: HostIngressAttemptOwner,
        proof: HostIngressAdmissionProofFact,
    ) -> None:
        with self._state_lock:
            expected = self._admission_proof_locked(
                owner,
                ingress_fact_fingerprint=proof.ingress_fact_fingerprint,
            )
            if expected != proof:
                owner.owner_state = "replan_required"
                raise HostIngressAdmissionStale("Host ingress admission proof is stale")

    def validate_run_start_event(self, event: Any) -> None:
        with self._state_lock:
            self._validate_run_start_event_locked(event)

    @contextmanager
    def authority_guard(self):
        """Hold the no-await Host admission authority across a physical commit."""

        with self._state_lock:
            yield

    @contextmanager
    def run_start_commit_guard(self, event: Any):
        """Hold the Host admission CAS through the physical RunStart commit."""

        with self._state_lock:
            self._validate_run_start_event_locked(event)
            yield

    def _validate_run_start_event_locked(self, event: Any) -> None:
        owner = self._active
        proof = getattr(event, "host_ingress_admission_proof", None)
        ingress = getattr(event, "host_run_ingress", None)
        if owner is None or proof is None or ingress is None:
            raise HostIngressAdmissionStale(
                "Host RunStart lacks its live admission owner"
            )
        expected = self._admission_proof_locked(
            owner,
            ingress_fact_fingerprint=proof.ingress_fact_fingerprint,
        )
        if expected != proof:
            owner.owner_state = "replan_required"
            raise HostIngressAdmissionStale("Host ingress admission proof is stale")
        if proof.ingress_fact_fingerprint != ingress.fact_fingerprint:
            raise HostIngressAdmissionStale("Host RunStart ingress identity drifted")

    async def mark_committed(
        self,
        owner: HostIngressAttemptOwner,
        *,
        run_start_event_id: str,
    ) -> None:
        async with self._condition:
            with self._state_lock:
                if self._active is not owner or owner.owner_state != "preparing":
                    raise HostIngressAdmissionStale("Host ingress commit owner changed")
                owner.owner_state = "committed"
                self._lifecycle_state = "active"
                self._state_generation += 1
                self._active_run_start_event_id = run_start_event_id

    async def cancel_active_preparation(self) -> bool:
        """Cancel the coordinator-owned runner, not a detached submit waiter."""

        async with self._condition:
            with self._state_lock:
                owner = self._active
                if owner is None or owner.owner_state != "preparing":
                    return False
                task = owner.runner_task
                if task is None or task.done():
                    return False
                task.cancel()
                return True

    async def mark_waiting_user(self, *, resume_match_key: str) -> None:
        async with self._condition:
            with self._state_lock:
                self._lifecycle_state = "waiting_user"
                self._waiting_resume_match_key = resume_match_key
                self._state_generation += 1

    async def clear_waiting_user(self) -> None:
        """Return a terminalized suspended run to the idle selection state."""

        async with self._condition:
            with self._state_lock:
                if self._lifecycle_state != "waiting_user":
                    return
                self._waiting_resume_match_key = None
                self._lifecycle_state = "open_idle"
                self._state_generation += 1
            self._condition.notify_all()

    def can_borrow_active_run_notifications(self) -> bool:
        """Snapshot the process-local safe-point admission predicates."""

        with self._state_lock:
            return (
                self._lifecycle_state == "active"
                and self._active is not None
                and not self._queues["human"]
                and not self._queues["resume"]
            )

    async def update_permission_policy(self, fingerprint: str) -> None:
        async with self._condition:
            with self._state_lock:
                if fingerprint != self._permission_policy_fingerprint:
                    self._permission_policy_fingerprint = fingerprint
                    self._permission_policy_revision += 1
                    self._state_generation += 1

    async def begin_close(self) -> None:
        async with self._condition:
            with self._state_lock:
                if self._lifecycle_state == "closed":
                    return
                self._close_intent_revision += 1
                self._state_generation += 1
                self._lifecycle_state = "closing"
                for queue in self._queues.values():
                    for owner in queue:
                        owner.owner_state = "withdrawn"
                        if not owner.result.done():
                            owner.result.set_exception(
                                HostIngressClosedError("Host ingress closed")
                            )
                    queue.clear()
            self._condition.notify_all()

    async def finish_close(self) -> None:
        await self.begin_close()
        worker = self._worker
        if worker is not None and worker is not asyncio.current_task():
            await asyncio.gather(worker, return_exceptions=True)
        async with self._condition:
            with self._state_lock:
                self._lifecycle_state = "closed"
                self._state_generation += 1

    def state_fact(self) -> HostIngressCoordinatorStateFact:
        with self._state_lock:
            active = self._active
            return build_frozen_fact(
                HostIngressCoordinatorStateFact,
                schema_version="host_ingress_coordinator_state.v1",
                host_session_id=self.host_session_id,
                state_generation=self._state_generation,
                lifecycle_state=self._lifecycle_state,
                active_admission_id=(
                    active.ingress_id
                    if active is not None
                    and active.owner_state in {"preparing", "committed"}
                    else None
                ),
                active_admission_generation=(
                    active.admission_generation
                    if active is not None
                    and active.owner_state in {"preparing", "committed"}
                    else None
                ),
                active_run_start_event_id=(
                    getattr(self, "_active_run_start_event_id", None)
                    if self._lifecycle_state == "active"
                    else None
                ),
                permission_policy_revision=self._permission_policy_revision,
                permission_policy_fingerprint=self._permission_policy_fingerprint,
                close_intent_revision=self._close_intent_revision,
            )

    async def _run(self) -> None:
        # Let producers that became runnable in the same loop turn enter the
        # common queue before selection. This is the linearization point tested
        # by the two-producer barrier gate.
        await asyncio.sleep(0)
        while True:
            if self._selection_barrier is not None:
                await self._selection_barrier()
            async with self._condition:
                with self._state_lock:
                    owner = self._select_locked()
                    if owner is None:
                        return
                    self._admission_generation += 1
                    if owner.accepted_ingress_ordinal is None:
                        self._accepted_ordinal += 1
                        owner.accepted_ingress_ordinal = self._accepted_ordinal
                    owner.admission_generation = self._admission_generation
                    owner.selection_queue_revision = self._queue_revision
                    owner.owner_state = "preparing"
                    self._active = owner
                    self._lifecycle_state = "preparing"
                    self._waiting_resume_match_key = None
                    self._state_generation += 1
            runner_task = asyncio.create_task(
                owner.runner(owner),
                name=f"host-ingress-owner:{owner.ingress_id}",
            )
            owner.runner_task = runner_task
            try:
                result = await runner_task
            except HostIngressAdmissionStale as exc:
                if owner.replan_cleanup is not None:
                    try:
                        await owner.replan_cleanup(owner)
                    except BaseException as cleanup_error:
                        await self._finish_failed_owner(owner, cleanup_error)
                        continue
                async with self._condition:
                    with self._state_lock:
                        if self._active is not owner:
                            self._finish_failed_owner_locked(
                                owner,
                                HostIngressAdmissionStale(
                                    "Host ingress stale owner changed during replan"
                                ),
                            )
                            continue
                        self._active = None
                        owner.replan_count += 1
                        if owner.replan_count > 8:
                            self._finish_failed_owner_locked(owner, exc)
                            continue
                        if self._lifecycle_state in {
                            "closing",
                            "closed",
                            "latched",
                        }:
                            self._finish_failed_owner_locked(
                                owner, HostIngressClosedError("Host ingress closed")
                            )
                            continue
                        owner.owner_state = "queued"
                        owner.admission_generation = None
                        owner.selection_queue_revision = None
                        owner.selected_notification_head_fingerprints = ()
                        owner.selected_notifications = ()
                        owner.expected_autonomy_chain_state_fingerprint = None
                        owner.proposed_automatic_delivery_ordinal = None
                        self._queues[owner.kind].insert(0, owner)
                        self._queue_revision += 1
                        self._lifecycle_state = "open_idle"
                        self._state_generation += 1
                        self._condition.notify_all()
                continue
            except BaseException as exc:
                await self._finish_failed_owner(owner, exc)
                continue
            finally:
                with self._state_lock:
                    if owner.runner_task is runner_task:
                        owner.runner_task = None
            async with self._condition:
                with self._state_lock:
                    owner.owner_state = "finished"
                    if not owner.result.done():
                        owner.result.set_result(result)
                    self._active = None
                    if self._lifecycle_state not in {
                        "waiting_user",
                        "closing",
                        "closed",
                        "latched",
                    }:
                        self._lifecycle_state = "open_idle"
                    self._state_generation += 1

    async def _finish_failed_owner(
        self, owner: HostIngressAttemptOwner, exc: BaseException
    ) -> None:
        async with self._condition:
            with self._state_lock:
                self._finish_failed_owner_locked(owner, exc)

    def _finish_failed_owner_locked(
        self, owner: HostIngressAttemptOwner, exc: BaseException
    ) -> None:
        if owner.owner_state not in {"reconciliation_required", "withdrawn"}:
            owner.owner_state = "finished"
        if not owner.result.done():
            owner.result.set_exception(exc)
        if self._active is owner:
            self._active = None
        if self._lifecycle_state not in {"closing", "closed", "latched"}:
            self._lifecycle_state = "open_idle"
        self._state_generation += 1

    def _select_locked(self) -> HostIngressAttemptOwner | None:
        if self._lifecycle_state in {"closing", "closed", "latched"}:
            return None
        if self._lifecycle_state == "waiting_user":
            expected = self._waiting_resume_match_key
            for index, owner in enumerate(self._queues["resume"]):
                if owner.resume_match_key == expected:
                    return self._queues["resume"].pop(index)
            return None
        for kind in ("human", "resume", "runtime"):
            if self._queues[kind]:
                return self._queues[kind].pop(0)
        return None

    def _queued_count_locked(self) -> int:
        return sum(len(items) for items in self._queues.values())


def _stable_ingress_id(kind: str, payload_identity: int, sampled: float) -> str:
    digest = sha256(f"{kind}\x00{payload_identity}\x00{sampled}".encode()).hexdigest()
    return f"host_ingress:{digest}"


def default_permission_policy_fingerprint() -> str:
    return context_fingerprint(
        "host-ingress-permission-policy:v1",
        {"source": "host-session-effective-policy"},
    )


__all__ = [
    "ActiveRunMonitorSafePointLease",
    "HostIngressAdmissionStale",
    "HostIngressAttemptOwner",
    "HostIngressCapacityError",
    "HostIngressClosedError",
    "HostIngressCoordinator",
    "HostIngressWaitingUserError",
    "build_active_run_monitor_delivery",
    "default_permission_policy_fingerprint",
]
