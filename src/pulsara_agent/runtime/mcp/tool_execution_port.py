"""Runtime-owned MCP execution and pending-interaction lease boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from threading import RLock
from time import monotonic
from typing import Any
from uuid import uuid4

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpPendingExecutionHandle,
    McpPendingExecutionHandleIdentity,
    McpPendingHandleState,
    McpPendingHandleTransitionOutcome,
    McpPendingTerminalReason,
    McpPreparedSuspensionCommitView,
    McpPreparedTerminalSettlement,
    McpToolCompletedOutcome,
    McpToolExecutionOutcome,
    McpToolExecutionRequest,
    McpToolRejectCode,
    McpToolRejectedOutcome,
    McpToolResumeRequest,
    McpToolSuspendedOutcome,
)
from pulsara_agent.ports.tool_execution import (
    ToolExecutionCandidateConfirmationKind,
    ToolExecutionPhysicalOwnerHandoffReceipt,
    ToolExecutionStableCandidateCommitReceipt,
    ToolExecutionStableCandidateKind,
    ToolExecutionStableCandidateOwnerIdentity,
    ToolResultArtifactCandidate,
)
from pulsara_agent.ports.tool_result_semantics import (
    FrozenToolResultSemanticsRuntimeInput,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.mcp import McpBindingIdentityFact, freeze_mcp_json_value
from pulsara_agent.primitives.runtime_event_vocabulary import (
    PreparedMcpInputRequiredSuspension,
    prepare_mcp_input_required_suspension,
)
from pulsara_agent.primitives.tool_result import ToolResultRenderVariantCode
from pulsara_agent.runtime.mcp.supervisor import McpDrainError, McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpContentArtifact,
    McpInputRequired,
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpToolResult,
    redact_mcp_error_message,
)


class _RuntimeMcpPendingExecutionHandle:
    __slots__ = (
        "_identity",
        "_invocation_owner",
        "_prepared",
        "_state",
        "_candidate_owner",
        "_prepared_settlement",
        "_frozen_resume_outcome",
        "_frozen_resume_request_fingerprint",
        "_settlement_generation",
        "_port_instance_id",
    )

    def __init__(
        self,
        *,
        identity: McpPendingExecutionHandleIdentity,
        invocation_owner: McpInvocationOwner,
        prepared: PreparedMcpInputRequiredSuspension,
        state: McpPendingHandleState,
        port_instance_id: str,
    ) -> None:
        self._identity = identity
        self._invocation_owner = invocation_owner
        self._prepared = prepared
        self._state = state
        self._candidate_owner: ToolExecutionStableCandidateOwnerIdentity | None = None
        self._prepared_settlement: McpPreparedTerminalSettlement | None = None
        self._frozen_resume_outcome: McpToolExecutionOutcome | None = None
        self._frozen_resume_request_fingerprint: str | None = None
        self._settlement_generation = 0
        self._port_instance_id = port_instance_id

    @property
    def identity(self) -> McpPendingExecutionHandleIdentity:
        return self._identity

    @property
    def state(self) -> McpPendingHandleState:
        return self._state

    @property
    def suspension_commit_view(self) -> McpPreparedSuspensionCommitView:
        prepared = self._prepared
        payload = {
            "interaction": prepared.interaction,
            "binding_identity": prepared.binding_identity,
            "pending_lease_reservation": prepared.pending_lease_reservation,
            "request_envelope": prepared.request_envelope,
            "deadline_monotonic": prepared.deadline_monotonic,
            "tool_observation_timing_seed": prepared.tool_observation_timing_seed,
            "prepared_suspension_fingerprint": (
                prepared.prepared_suspension_fingerprint
            ),
        }
        return McpPreparedSuspensionCommitView(
            **payload,
            view_fingerprint=context_fingerprint(
                "mcp-prepared-suspension-commit-view:v1", payload
            ),
        )

    def __reduce__(self):
        raise TypeError("MCP pending execution handles are process-local")


class RuntimeMcpToolExecutionPort:
    """Own live MCP managers, leases, raw protocol state, and settlement."""

    def __init__(self, supervisor: McpServerSupervisor) -> None:
        self._supervisor = supervisor
        self._instance_id = f"mcp_tool_execution_port:{uuid4().hex}"
        self._lock = RLock()
        self._handles: dict[str, _RuntimeMcpPendingExecutionHandle] = {}
        self._accepting = True
        self._active_operations = 0

    async def execute(
        self, request: McpToolExecutionRequest
    ) -> McpToolExecutionOutcome:
        self._begin_operation()
        try:
            return await self._execute_owned(request)
        finally:
            self._end_operation()

    async def _execute_owned(
        self, request: McpToolExecutionRequest
    ) -> McpToolExecutionOutcome:
        identity = _runtime_binding(request.binding.binding_identity)
        lease = None
        try:
            lease = self._supervisor.acquire_binding_lease(identity)
            manager = self._supervisor.manager_for_lease(lease)
            result = await manager.call_tool(
                request.binding.binding_identity.server_id,
                request.original_tool_name,
                _thaw(request.frozen_arguments),
                timeout_ms=request.timeout_ms,
            )
        except Exception as exc:
            if lease is not None:
                self._supervisor.release_lease(lease)
            return _rejected(
                McpToolRejectCode.LEASE_ACQUIRE_FAILED
                if lease is None
                else McpToolRejectCode.ADAPTER_ERROR,
                exc,
                retryable=True,
            )
        except BaseException:
            if lease is not None:
                self._supervisor.release_lease(lease)
            raise
        if isinstance(result, McpInputRequired):
            assert lease is not None
            reservation = None
            try:
                reservation = self._supervisor.promote_lease_to_pending(
                    lease, result.interaction_id
                )
                prepared = _prepare_suspension(
                    result=result,
                    owner=request.owner,
                    exposed_tool_name=request.exposed_tool_name,
                    binding=request.binding.binding_identity,
                    reservation_id=reservation.reservation_id,
                )
                handle = self._new_handle(
                    owner=request.owner,
                    prepared=prepared,
                    predecessor=None,
                )
                return _suspended(handle)
            except Exception as exc:
                try:
                    if reservation is None:
                        self._supervisor.release_lease(lease)
                    else:
                        self._supervisor.abort_pending_lease(
                            result.interaction_id,
                            reservation.reservation_id,
                        )
                except Exception:
                    if reservation is None:
                        self._supervisor.release_lease(lease)
                return _rejected(McpToolRejectCode.PROTOCOL_ERROR, exc, retryable=False)
            except BaseException:
                _abort_initial_pending_promotion(
                    supervisor=self._supervisor,
                    lease=lease,
                    interaction_id=result.interaction_id,
                    reservation_id=(
                        reservation.reservation_id if reservation is not None else None
                    ),
                )
                raise
        assert lease is not None
        self._supervisor.release_lease(lease)
        return _completed(
            result,
            server_id=request.binding.binding_identity.server_id,
            original_tool_name=request.original_tool_name,
            interaction_id=None,
        )

    def bind_suspension_candidate(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
    ) -> None:
        handle = self._require_handle(pending_handle)
        identity = handle.identity
        if (
            candidate_owner_identity.candidate_kind
            is not ToolExecutionStableCandidateKind.SUSPENSION
            or candidate_owner_identity.tool_call_id
            != handle._invocation_owner.tool_call_id
            or candidate_owner_identity.run_id != handle._invocation_owner.run_id
            or candidate_owner_identity.physical_owner_kind != "mcp_pending"
            or candidate_owner_identity.physical_owner_identity_fingerprint
            != identity.identity_fingerprint
        ):
            raise ValueError("MCP suspension candidate owner identity mismatch")
        with self._lock:
            if handle._state not in {
                McpPendingHandleState.PREPARED_SUSPENSION,
                McpPendingHandleState.PENDING_CONFIRMED,
            }:
                raise RuntimeError("MCP handle cannot bind a suspension candidate")
            handle._candidate_owner = candidate_owner_identity
            handle._state = McpPendingHandleState.SUSPENSION_COMMIT_IN_FLIGHT

    def confirm_suspension_commit(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome:
        handle = self._require_handle(pending_handle)
        self._require_receipt(handle, commit_receipt)
        confirmation = commit_receipt.confirmation_kind
        reservation = handle.identity.pending_lease_reservation
        if confirmation is ToolExecutionCandidateConfirmationKind.FULL:
            self._supervisor.confirm_pending_lease(
                reservation.interaction_id, reservation.reservation_id
            )
            handle._state = (
                McpPendingHandleState.RECONCILIATION_REQUIRED
                if commit_receipt.reconciliation_required
                else McpPendingHandleState.PENDING_CONFIRMED
            )
            disposition = "confirmed"
            retry = False
            reconciliation = commit_receipt.reconciliation_required
        elif confirmation is ToolExecutionCandidateConfirmationKind.NONE:
            if commit_receipt.owner_identity.none_policy.value == "abandon_on_none":
                self._supervisor.abort_pending_lease(
                    reservation.interaction_id, reservation.reservation_id
                )
                handle._state = McpPendingHandleState.ABORTED
                disposition = "released"
                retry = False
                reconciliation = False
            else:
                handle._state = McpPendingHandleState.SUCCESSOR_SUSPENSION_FROZEN
                disposition = "retained"
                retry = True
                reconciliation = False
        else:
            handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
            disposition = "retained"
            retry = False
            reconciliation = True
        transition = self._transition(
            handle,
            commit_receipt=commit_receipt,
            disposition=disposition,
            exact_retry_required=retry,
            reconciliation_required=reconciliation,
        )
        if handle.state is McpPendingHandleState.ABORTED:
            with self._lock:
                self._handles.pop(handle.identity.handle_id, None)
        elif handle.state is McpPendingHandleState.PENDING_CONFIRMED:
            handle._candidate_owner = None
        return transition

    async def resume(self, request: McpToolResumeRequest) -> McpToolExecutionOutcome:
        self._begin_operation()
        try:
            return await self._resume_owned(request)
        finally:
            self._end_operation()

    async def _resume_owned(
        self, request: McpToolResumeRequest
    ) -> McpToolExecutionOutcome:
        handle = self._require_handle(request.pending_handle)
        with self._lock:
            if handle._state is McpPendingHandleState.TERMINAL_RESULT_FROZEN:
                if (
                    handle._frozen_resume_request_fingerprint
                    != request.request_fingerprint
                ):
                    return _rejected(
                        McpToolRejectCode.RESOLUTION_IDENTITY_MISMATCH,
                        RuntimeError("MCP frozen result belongs to another resolution"),
                        retryable=False,
                    )
                frozen = handle._frozen_resume_outcome
                if frozen is None:
                    raise RuntimeError("MCP frozen terminal result carrier is missing")
                return frozen
            if handle._state is not McpPendingHandleState.PENDING_CONFIRMED:
                return _rejected(
                    McpToolRejectCode.PENDING_LEASE_BORROW_FAILED,
                    RuntimeError("MCP pending interaction is not resumable"),
                    retryable=False,
                )
        prepared = handle._prepared
        if (
            request.prepared_resolution.interaction_id != handle.identity.interaction_id
            or request.source_suspension.suspension_fact_fingerprint
            != request.prepared_resolution.source_suspension_fact_fingerprint
        ):
            return _rejected(
                McpToolRejectCode.RESOLUTION_IDENTITY_MISMATCH,
                RuntimeError("MCP resolution authority mismatch"),
                retryable=False,
            )
        identity = _runtime_binding(request.binding.binding_identity)
        with self._lock:
            if handle._state is not McpPendingHandleState.PENDING_CONFIRMED:
                raise RuntimeError("MCP resume admission state changed concurrently")
            handle._state = McpPendingHandleState.RESUME_IN_FLIGHT
        try:
            lease = self._supervisor.borrow_pending_lease(
                handle.identity.interaction_id, identity
            )
        except Exception as exc:
            self._restore_resume_retry_state(handle)
            return _rejected(McpToolRejectCode.ADAPTER_ERROR, exc, retryable=True)
        except BaseException:
            self._restore_resume_retry_state(handle)
            raise
        try:
            manager = self._supervisor.manager_for_lease(lease)
            result = await manager.resume_suspended_request(
                server_id=identity.server_id,
                original_request=_original_request(prepared.thaw_original_request()),
                request_state=prepared.thaw_request_state(),
                resolution=McpInputRequiredResolution(
                    interaction_id=handle.identity.interaction_id,
                    responses={
                        key: dict(value)
                        for key, value in request.prepared_resolution.thaw_responses().items()
                    },
                    cancelled=request.prepared_resolution.cancelled,
                    tool_call_id=request.owner.tool_call_id,
                    round_count=prepared.interaction.round_count,
                    deadline_monotonic=prepared.deadline_monotonic,
                ),
                timeout_ms=request.timeout_ms,
            )
        except Exception as exc:
            self._return_resume_borrow(handle)
            self._restore_resume_retry_state(handle)
            return _rejected(McpToolRejectCode.ADAPTER_ERROR, exc, retryable=True)
        except BaseException:
            self._return_resume_borrow(handle)
            self._restore_resume_retry_state(handle)
            raise
        with self._lock:
            if handle._state is not McpPendingHandleState.RESUME_IN_FLIGHT:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP resume result lost its in-flight owner")
            handle._state = McpPendingHandleState.RESUME_RESULT_RECEIVED
            handle._frozen_resume_request_fingerprint = request.request_fingerprint
        self._return_resume_borrow(handle)
        try:
            if isinstance(result, McpInputRequired):
                return self._freeze_successor_result(
                    predecessor=handle,
                    request=request,
                    result=result,
                )
            outcome = _completed(
                result,
                server_id=identity.server_id,
                original_tool_name=request.binding.original_tool_name,
                interaction_id=handle.identity.interaction_id,
            )
        except Exception as exc:
            return self._freeze_resume_lowering_failure(handle, error=exc)
        except BaseException:
            self._mark_resume_reconciliation(handle)
            raise
        with self._lock:
            if handle._state is not McpPendingHandleState.RESUME_RESULT_RECEIVED:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP terminal result freeze lost its owner")
            handle._frozen_resume_outcome = outcome
            handle._state = McpPendingHandleState.TERMINAL_RESULT_FROZEN
        return outcome

    def prepare_terminal_settlement(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        reason: McpPendingTerminalReason,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
    ) -> McpPreparedTerminalSettlement:
        handle = self._require_handle(pending_handle)
        if (
            candidate_owner_identity.candidate_kind
            is not ToolExecutionStableCandidateKind.TERMINAL
            or candidate_owner_identity.physical_owner_identity_fingerprint
            != handle.identity.identity_fingerprint
            or candidate_owner_identity.tool_call_id
            != handle._invocation_owner.tool_call_id
        ):
            raise ValueError("MCP terminal candidate owner identity mismatch")
        with self._lock:
            handle._candidate_owner = candidate_owner_identity
            handle._settlement_generation += 1
            handle._state = McpPendingHandleState.TERMINAL_CANDIDATE_FROZEN
            payload = {
                "pending_handle_identity": asdict(handle.identity),
                "reason": reason.value,
                "candidate_owner_identity": asdict(candidate_owner_identity),
                "settlement_generation": handle._settlement_generation,
            }
            settlement = McpPreparedTerminalSettlement(
                pending_handle_identity=handle.identity,
                reason=reason,
                candidate_owner_identity=candidate_owner_identity,
                settlement_generation=handle._settlement_generation,
                settlement_fingerprint=context_fingerprint(
                    "mcp-prepared-terminal-settlement:v1", payload
                ),
            )
            handle._prepared_settlement = settlement
            return settlement

    def confirm_terminal_commit(
        self,
        *,
        settlement: McpPreparedTerminalSettlement,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome:
        handle = self._require_handle_id(settlement.pending_handle_identity.handle_id)
        if (
            handle.identity != settlement.pending_handle_identity
            or handle._candidate_owner != settlement.candidate_owner_identity
            or commit_receipt.owner_identity != settlement.candidate_owner_identity
        ):
            raise ValueError("MCP terminal settlement/receipt mismatch")
        confirmation = commit_receipt.confirmation_kind
        if confirmation is ToolExecutionCandidateConfirmationKind.FULL:
            self._supervisor.complete_pending_lease(handle.identity.interaction_id)
            handle._state = (
                McpPendingHandleState.RECONCILIATION_REQUIRED
                if commit_receipt.reconciliation_required
                else McpPendingHandleState.COMPLETED
            )
            disposition = "released"
            retry = False
            reconciliation = commit_receipt.reconciliation_required
        elif confirmation is ToolExecutionCandidateConfirmationKind.NONE:
            handle._state = McpPendingHandleState.TERMINAL_CANDIDATE_FROZEN
            disposition = "retained"
            retry = True
            reconciliation = False
        else:
            handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
            disposition = "retained"
            retry = False
            reconciliation = True
        transition = self._transition(
            handle,
            commit_receipt=commit_receipt,
            disposition=disposition,
            exact_retry_required=retry,
            reconciliation_required=reconciliation,
        )
        if handle.state is McpPendingHandleState.COMPLETED:
            with self._lock:
                self._handles.pop(handle.identity.handle_id, None)
        return transition

    def confirm_owned_candidate_commit(
        self,
        *,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome:
        with self._lock:
            matches = tuple(
                handle
                for handle in self._handles.values()
                if handle._candidate_owner == candidate_owner_identity
            )
        if len(matches) != 1:
            raise RuntimeError(
                "MCP candidate owner does not resolve to one physical handle"
            )
        handle = matches[0]
        if (
            candidate_owner_identity.candidate_kind
            is ToolExecutionStableCandidateKind.SUSPENSION
        ):
            return self.confirm_suspension_commit(
                pending_handle=handle,
                commit_receipt=commit_receipt,
            )
        settlement = handle._prepared_settlement
        if settlement is None:
            raise RuntimeError("MCP terminal candidate lost its prepared settlement")
        return self.confirm_terminal_commit(
            settlement=settlement,
            commit_receipt=commit_receipt,
        )

    def handle_for_interaction(
        self, interaction_id: str
    ) -> McpPendingExecutionHandle | None:
        with self._lock:
            for handle in self._handles.values():
                if (
                    handle.identity.interaction_id == interaction_id
                    and handle.state
                    not in {
                        McpPendingHandleState.ABORTED,
                        McpPendingHandleState.COMPLETED,
                    }
                ):
                    return handle
        return None

    async def stop_admission_and_drain(
        self,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Revoke new calls and prove all physical MCP owners were settled."""

        with self._lock:
            self._accepting = False
        while True:
            with self._lock:
                active_operations = self._active_operations
                resident_handles = tuple(self._handles.values())
            if active_operations == 0 and not resident_handles:
                return
            if monotonic() >= deadline_monotonic:
                handle_states = ",".join(
                    sorted(
                        f"{item.identity.handle_id}:{item.state.value}"
                        for item in resident_handles
                    )
                )
                raise McpDrainError(
                    "MCP execution port did not drain before close deadline "
                    f"(active_operations={active_operations}, handles={handle_states})"
                )
            await asyncio.sleep(min(0.01, max(0.0, deadline_monotonic - monotonic())))

    def _begin_operation(self) -> None:
        with self._lock:
            if not self._accepting:
                raise RuntimeError("MCP execution port admission is closed")
            self._active_operations += 1

    def _end_operation(self) -> None:
        with self._lock:
            if self._active_operations < 1:
                raise RuntimeError("MCP execution operation accounting underflow")
            self._active_operations -= 1

    def _new_handle(
        self,
        *,
        owner: McpInvocationOwner,
        prepared: PreparedMcpInputRequiredSuspension,
        predecessor: _RuntimeMcpPendingExecutionHandle | None,
    ) -> _RuntimeMcpPendingExecutionHandle:
        handle = self._build_handle(
            owner=owner,
            prepared=prepared,
            predecessor=predecessor,
        )
        with self._lock:
            if handle.identity.handle_id in self._handles:
                raise RuntimeError("MCP pending handle identity collision")
            self._handles[handle.identity.handle_id] = handle
        return handle

    def _build_handle(
        self,
        *,
        owner: McpInvocationOwner,
        prepared: PreparedMcpInputRequiredSuspension,
        predecessor: _RuntimeMcpPendingExecutionHandle | None,
    ) -> _RuntimeMcpPendingExecutionHandle:
        handle_id = f"mcp_pending_handle:{uuid4().hex}"
        generation = (
            1 if predecessor is None else predecessor.identity.handle_generation + 1
        )
        payload = {
            "handle_id": handle_id,
            "interaction_id": prepared.interaction.interaction_id,
            "binding_identity": prepared.binding_identity.model_dump(mode="json"),
            "pending_lease_reservation": prepared.pending_lease_reservation.model_dump(
                mode="json"
            ),
            "prepared_suspension_fingerprint": prepared.prepared_suspension_fingerprint,
            "predecessor_handle_id": (
                predecessor.identity.handle_id if predecessor is not None else None
            ),
            "handle_generation": generation,
        }
        identity = McpPendingExecutionHandleIdentity(
            handle_id=handle_id,
            interaction_id=prepared.interaction.interaction_id,
            binding_identity=prepared.binding_identity,
            pending_lease_reservation=prepared.pending_lease_reservation,
            prepared_suspension_fingerprint=prepared.prepared_suspension_fingerprint,
            predecessor_handle_id=payload["predecessor_handle_id"],
            handle_generation=generation,
            identity_fingerprint=context_fingerprint(
                "mcp-pending-execution-handle-identity:v1", payload
            ),
        )
        handle = _RuntimeMcpPendingExecutionHandle(
            identity=identity,
            invocation_owner=owner,
            prepared=prepared,
            state=(
                McpPendingHandleState.PREPARED_SUSPENSION
                if predecessor is None
                else McpPendingHandleState.PENDING_CONFIRMED
            ),
            port_instance_id=self._instance_id,
        )
        return handle

    def _freeze_successor_result(
        self,
        *,
        predecessor: _RuntimeMcpPendingExecutionHandle,
        request: McpToolResumeRequest,
        result: McpInputRequired,
    ) -> McpToolSuspendedOutcome:
        if result.interaction_id != predecessor.identity.interaction_id:
            raise ValueError("MCP successor changed the pending interaction identity")
        if result.server_id != request.binding.binding_identity.server_id:
            raise ValueError("MCP successor changed the binding server identity")
        successor_prepared = _prepare_suspension(
            result=result,
            owner=request.owner,
            exposed_tool_name=request.binding.tool_name,
            binding=request.binding.binding_identity,
            reservation_id=(
                predecessor.identity.pending_lease_reservation.reservation_id
            ),
        )
        successor = self._build_handle(
            owner=request.owner,
            prepared=successor_prepared,
            predecessor=predecessor,
        )
        outcome = _suspended(successor)
        with self._lock:
            resident = self._handles.get(predecessor.identity.handle_id)
            if (
                resident is not predecessor
                or predecessor._state
                is not McpPendingHandleState.RESUME_RESULT_RECEIVED
                or successor.identity.handle_id in self._handles
            ):
                predecessor._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP successor installation authority changed")
            self._handles[successor.identity.handle_id] = successor
            predecessor._frozen_resume_outcome = outcome
            predecessor._frozen_resume_request_fingerprint = request.request_fingerprint
            predecessor._state = McpPendingHandleState.COMPLETED
            self._handles.pop(predecessor.identity.handle_id)
        return outcome

    def _freeze_resume_lowering_failure(
        self,
        handle: _RuntimeMcpPendingExecutionHandle,
        *,
        error: Exception,
    ) -> McpToolRejectedOutcome:
        try:
            outcome = _rejected(
                McpToolRejectCode.RESULT_LOWERING_FAILED,
                RuntimeError(
                    "MCP provider result failed closed lowering "
                    f"({type(error).__name__})"
                ),
                retryable=False,
            )
        except BaseException:
            self._mark_resume_reconciliation(handle)
            raise
        with self._lock:
            if handle._state is not McpPendingHandleState.RESUME_RESULT_RECEIVED:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP lowering failure lost its result owner")
            handle._frozen_resume_outcome = outcome
            handle._state = McpPendingHandleState.TERMINAL_RESULT_FROZEN
        return outcome

    def _return_resume_borrow(
        self,
        handle: _RuntimeMcpPendingExecutionHandle,
    ) -> None:
        try:
            self._supervisor.return_pending_borrow(handle.identity.interaction_id)
        except BaseException:
            self._mark_resume_reconciliation(handle)
            raise

    def _restore_resume_retry_state(
        self,
        handle: _RuntimeMcpPendingExecutionHandle,
    ) -> None:
        with self._lock:
            if handle._state is McpPendingHandleState.RESUME_IN_FLIGHT:
                handle._state = McpPendingHandleState.PENDING_CONFIRMED
                return
            if handle._state is not McpPendingHandleState.PENDING_CONFIRMED:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP failed resume lost its retry owner")

    def _mark_resume_reconciliation(
        self,
        handle: _RuntimeMcpPendingExecutionHandle,
    ) -> None:
        with self._lock:
            if self._handles.get(handle.identity.handle_id) is handle:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED

    def _require_handle(
        self, value: McpPendingExecutionHandle
    ) -> _RuntimeMcpPendingExecutionHandle:
        if not isinstance(value, _RuntimeMcpPendingExecutionHandle):
            raise TypeError("MCP pending handle was not issued by the runtime port")
        if value._port_instance_id != self._instance_id:
            raise ValueError("MCP pending handle belongs to another execution port")
        return self._require_handle_id(value.identity.handle_id)

    def _require_handle_id(self, handle_id: str) -> _RuntimeMcpPendingExecutionHandle:
        with self._lock:
            handle = self._handles.get(handle_id)
        if handle is None:
            raise RuntimeError("MCP pending handle is no longer resident")
        return handle

    @staticmethod
    def _require_receipt(
        handle: _RuntimeMcpPendingExecutionHandle,
        receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> None:
        if handle._candidate_owner != receipt.owner_identity:
            raise ValueError("MCP handle candidate receipt identity mismatch")

    @staticmethod
    def _transition(
        handle: _RuntimeMcpPendingExecutionHandle,
        *,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
        disposition: str,
        exact_retry_required: bool,
        reconciliation_required: bool,
    ) -> McpPendingHandleTransitionOutcome:
        owner = commit_receipt.owner_identity
        handoff_payload = {
            "candidate_owner_identity": asdict(owner),
            "source_commit_receipt_fingerprint": commit_receipt.receipt_fingerprint,
            "physical_owner_kind": "mcp_pending",
            "physical_owner_identity_fingerprint": handle.identity.identity_fingerprint,
            "handoff_generation": commit_receipt.write_attempt_generation,
            "physical_disposition": disposition,
            "exact_retry_required": exact_retry_required,
            "reconciliation_required": reconciliation_required,
        }
        handoff = ToolExecutionPhysicalOwnerHandoffReceipt(
            candidate_owner_identity=owner,
            source_commit_receipt_fingerprint=commit_receipt.receipt_fingerprint,
            physical_owner_kind="mcp_pending",
            physical_owner_identity_fingerprint=handle.identity.identity_fingerprint,
            handoff_generation=commit_receipt.write_attempt_generation,
            physical_disposition=disposition,  # type: ignore[arg-type]
            exact_retry_required=exact_retry_required,
            reconciliation_required=reconciliation_required,
            receipt_fingerprint=context_fingerprint(
                "tool-execution-physical-owner-handoff:v1", handoff_payload
            ),
        )
        outcome_payload = {
            "resulting_state": handle.state.value,
            "handoff_receipt_fingerprint": handoff.receipt_fingerprint,
        }
        return McpPendingHandleTransitionOutcome(
            resulting_state=handle.state,
            handoff_receipt=handoff,
            outcome_fingerprint=context_fingerprint(
                "mcp-pending-handle-transition-outcome:v1", outcome_payload
            ),
        )


def _prepare_suspension(
    *,
    result: McpInputRequired,
    owner: McpInvocationOwner,
    exposed_tool_name: str,
    binding: McpBindingIdentityFact,
    reservation_id: str,
) -> PreparedMcpInputRequiredSuspension:
    return prepare_mcp_input_required_suspension(
        interaction_id=result.interaction_id,
        tool_call_id=owner.tool_call_id,
        tool_name=exposed_tool_name,
        server_id=binding.server_id,
        round_count=result.round_count,
        binding_identity=binding,
        pending_lease_reservation_id=reservation_id,
        protocol_version=result.protocol_version,
        input_requests=tuple(item.to_dict() for item in result.input_requests),
        original_request=result.original_request.to_dict(),
        request_state=result.request_state,
        deadline_monotonic=result.deadline_monotonic,
    )


def _abort_initial_pending_promotion(
    *,
    supervisor: McpServerSupervisor,
    lease,
    interaction_id: str,
    reservation_id: str | None,
) -> None:
    """Best-effort rollback that never masks caller cancellation."""

    try:
        if reservation_id is None:
            supervisor.release_lease(lease)
        else:
            supervisor.abort_pending_lease(interaction_id, reservation_id)
    except Exception:
        if reservation_id is None:
            try:
                supervisor.release_lease(lease)
            except Exception:
                pass


def _runtime_binding(value: McpBindingIdentityFact) -> McpBindingIdentity:
    return McpBindingIdentity(
        server_id=value.server_id,
        slot_id=value.slot_id,
        snapshot_id=value.snapshot_id,
        discovery_generation=value.discovery_generation,
    )


def _completed(
    raw: Any,
    *,
    server_id: str,
    original_tool_name: str,
    interaction_id: str | None,
) -> McpToolCompletedOutcome:
    normalized = _normalize_result(raw)
    artifact_candidates = tuple(
        _artifact_candidate(item) for item in normalized.artifacts
    )
    metadata = {
        "provider_kind": "mcp",
        "server_id": server_id,
        "original_tool_name": original_tool_name,
        **(
            {"mcp_input_required_interaction_id": interaction_id}
            if interaction_id is not None
            else {}
        ),
        **normalized.metadata,
    }
    frozen_metadata = freeze_mcp_json_value(metadata)
    payload = {
        "result_state": "error" if normalized.is_error else "success",
        "normalized_is_error": normalized.is_error,
        "normalized_output": normalized.output,
        "normalized_metadata": frozen_metadata,
        "artifact_candidates": tuple(asdict(item) for item in artifact_candidates),
    }
    return McpToolCompletedOutcome(
        outcome_kind="completed",
        result_state=(
            ToolResultState.ERROR if normalized.is_error else ToolResultState.SUCCESS
        ),
        normalized_is_error=normalized.is_error,
        normalized_output=normalized.output,
        frozen_display_payload=None,
        normalized_metadata=frozen_metadata,
        artifact_candidates=artifact_candidates,
        semantics_input=FrozenToolResultSemanticsRuntimeInput(
            semantics_input_kind=ToolResultRenderVariantCode.GENERIC_RESULT,
            domain_submission=None,
        ),
        outcome_fingerprint=context_fingerprint(
            "mcp-tool-completed-outcome:v1", payload
        ),
    )


def _suspended(handle: McpPendingExecutionHandle) -> McpToolSuspendedOutcome:
    return McpToolSuspendedOutcome(
        outcome_kind="suspended",
        pending_handle=handle,
        outcome_fingerprint=context_fingerprint(
            "mcp-tool-suspended-outcome:v1", handle.identity.identity_fingerprint
        ),
    )


def _rejected(
    code: McpToolRejectCode,
    error: Exception,
    *,
    retryable: bool,
) -> McpToolRejectedOutcome:
    message = redact_mcp_error_message(error)
    payload = {
        "error_code": code.value,
        "sanitized_message": message,
        "retryable_in_same_live_owner": retryable,
    }
    return McpToolRejectedOutcome(
        outcome_kind="rejected",
        error_code=code,
        sanitized_message=message,
        retryable_in_same_live_owner=retryable,
        outcome_fingerprint=context_fingerprint(
            "mcp-tool-rejected-outcome:v1", payload
        ),
    )


def _normalize_result(value: Any) -> McpToolResult:
    if isinstance(value, McpToolResult):
        return value
    if isinstance(value, str):
        return McpToolResult(output=value)
    try:
        return McpToolResult(
            output=json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        )
    except TypeError:
        return McpToolResult(output=str(value))


def _artifact_candidate(value: McpContentArtifact) -> ToolResultArtifactCandidate:
    return ToolResultArtifactCandidate(
        role=value.role,
        media_type=value.media_type,
        text=value.text,
        data=value.data,
        metadata=value.metadata,
    )


def _original_request(payload: dict[str, Any]) -> McpOriginalRequest:
    source = McpRequestSourceMethod(str(payload["source_method"]))
    arguments = payload.get("arguments")
    prompt_arguments = payload.get("prompt_arguments")
    return McpOriginalRequest(
        source_method=source,
        tool_name=(
            str(payload["tool_name"]) if payload.get("tool_name") is not None else None
        ),
        arguments=dict(arguments) if isinstance(arguments, dict) else None,
        resource_uri=(
            str(payload["resource_uri"])
            if payload.get("resource_uri") is not None
            else None
        ),
        prompt_name=(
            str(payload["prompt_name"])
            if payload.get("prompt_name") is not None
            else None
        ),
        prompt_arguments=(
            dict(prompt_arguments) if isinstance(prompt_arguments, dict) else None
        ),
    )


def _thaw(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("MCP request arguments must be an object")

    def visit(item: object) -> object:
        if isinstance(item, dict):
            return {str(key): visit(nested) for key, nested in item.items()}
        if isinstance(item, tuple):
            return [visit(nested) for nested in item]
        return item

    return {str(key): visit(item) for key, item in value.items()}


__all__ = ["RuntimeMcpToolExecutionPort"]
