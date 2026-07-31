"""Runtime-owned MCP execution and pending-interaction lease boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpConfirmedContinuationDispatchReceipt,
    McpContinuationTransactionIntent,
    McpDispatchReservationCommitGuard,
    McpPendingExecutionHandle,
    McpPendingExecutionHandleIdentity,
    McpPendingHandleState,
    McpPendingHandleTransitionOutcome,
    McpPendingTerminalReason,
    McpPreparedSuspensionCommitView,
    McpStatelessRecoveryRebindReceipt,
    McpPreparedTerminalSettlement,
    McpToolCompletedOutcome,
    McpToolExecutionOutcome,
    McpToolExecutionRequest,
    McpToolRejectCode,
    McpToolRejectedOutcome,
    McpToolResumeRequest,
    McpToolSuspendedOutcome,
    PreparedMcpContinuationDispatch,
    PreparedMcpInputRequiredResolution,
)
from pulsara_agent.ports.tool_registry import McpToolBindingContract
from pulsara_agent.ports.mcp_elicitation import McpExternalBrowserPort
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
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.mcp import McpBindingIdentityFact, freeze_mcp_json_value
from pulsara_agent.primitives.mcp_continuation import (
    McpContinuationCompanionKind,
    McpContinuationCarrierState,
    McpContinuationDispatchReservationFact,
    McpContinuationExpiryFact,
    McpContinuationResolutionCarrierFact,
    McpInputRequiredResolutionSemanticFact,
    build_mcp_continuation_fact,
    mcp_continuation_charge_contract_fingerprint,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    McpInputRequiredInteractionSemanticFact,
    McpInputRequiredRequestEnvelopeFact,
    McpInputRequiredSuspensionFact,
    McpPendingLeaseReservationIdentityFact,
    build_mcp_interaction_semantic,
    build_mcp_user_visible_request,
    stable_runtime_event_id,
)
from pulsara_agent.primitives.tool_result import ToolResultRenderVariantCode
from pulsara_agent.runtime.mcp.supervisor import McpDrainError, McpServerSupervisor
from pulsara_agent.runtime.mcp.continuation_store import (
    McpContinuationAadContext,
    McpContinuationMutationKind,
    McpContinuationRepository,
    McpContinuationSecretCodec,
    McpContinuationStoredRecord,
    PreparedMcpAwaitingContinuation,
    PreparedMcpReplayContinuation,
    build_mcp_continuation_transaction_intent,
    prepare_mcp_awaiting_continuation,
    prepare_mcp_replay_continuation,
)
from pulsara_agent.primitives.mcp_continuation_storage import (
    McpContinuationCarrierControlFact,
    build_mcp_continuation_storage_fact,
)
from pulsara_agent.runtime.mcp.elicitation_batch import (
    McpElicitationBatchOwner,
    McpElicitationBatchState,
    build_mcp_elicitation_batch_owner,
    build_recovered_resolved_mcp_elicitation_batch_owner,
)
from pulsara_agent.runtime.mcp.protocol import McpClientInputRequired
from pulsara_agent.runtime.mcp.protocol import McpClientInputRequiredLeg
from pulsara_agent.runtime.mcp.types import (
    McpBindingIdentity,
    McpContentArtifact,
    McpToolResult,
    redact_mcp_error_message,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.mcp.recovery import (
        RecoveredMcpInputRequiredClosure,
    )
    from pulsara_agent.runtime.session import RuntimeSession


@dataclass(frozen=True, slots=True)
class _RecoveredCommittedContinuationIntent:
    """Read-only tombstone for a companion already proven FULL before reopen."""

    companion_kind: McpContinuationCompanionKind
    storage_mutation_plan_fingerprint: str
    charge_contract_fingerprint: str
    charged_payload_bytes: int = 0

    def bind_candidate_batch(self, candidates):
        del candidates
        raise RuntimeError("a recovered FULL companion cannot be committed again")


@dataclass(frozen=True, slots=True)
class _PreparedMcpSecureSuspension:
    suspension_event_id: str
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    continuation: PreparedMcpAwaitingContinuation = field(repr=False)
    transaction_companion: McpContinuationTransactionIntent = field(repr=False)
    elicitation_batch_owner: McpElicitationBatchOwner = field(repr=False)
    client_input_result: McpClientInputRequired = field(repr=False)
    deadline_monotonic: float
    tool_observation_timing_seed: FrozenJsonObjectFact | None
    recovery_rebind_receipt: McpStatelessRecoveryRebindReceipt | None
    prepared_suspension_fingerprint: str

    def __reduce__(self):
        raise TypeError("prepared MCP suspension is process-local")


class _RuntimeMcpPendingExecutionHandle:
    __slots__ = (
        "_identity",
        "_invocation_owner",
        "_prepared",
        "_state",
        "_candidate_owner",
        "_prepared_settlement",
        "_prepared_resolution",
        "_prepared_replay",
        "_prepared_dispatch",
        "_dispatch_record",
        "_dispatch_receipt",
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
        prepared: _PreparedMcpSecureSuspension,
        state: McpPendingHandleState,
        port_instance_id: str,
    ) -> None:
        self._identity = identity
        self._invocation_owner = invocation_owner
        self._prepared = prepared
        self._state = state
        self._candidate_owner: ToolExecutionStableCandidateOwnerIdentity | None = None
        self._prepared_settlement: McpPreparedTerminalSettlement | None = None
        self._prepared_resolution: PreparedMcpInputRequiredResolution | None = None
        self._prepared_replay: PreparedMcpReplayContinuation | None = None
        self._prepared_dispatch: PreparedMcpContinuationDispatch | None = None
        self._dispatch_record: McpContinuationStoredRecord | None = None
        self._dispatch_receipt: McpConfirmedContinuationDispatchReceipt | None = None
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
            "suspension_event_id": prepared.suspension_event_id,
            "interaction": prepared.interaction,
            "binding_identity": prepared.binding_identity,
            "pending_lease_reservation": prepared.pending_lease_reservation,
            "request_envelope": prepared.request_envelope,
            "durable_continuation": prepared.continuation.durable_fact,
            "transaction_companion": prepared.transaction_companion,
            "deadline_monotonic": prepared.deadline_monotonic,
            "tool_observation_timing_seed": prepared.tool_observation_timing_seed,
            "prepared_suspension_fingerprint": (
                prepared.prepared_suspension_fingerprint
            ),
        }
        fingerprint_payload = {
            key: value
            for key, value in payload.items()
            if key != "transaction_companion"
        }
        fingerprint_payload["storage_mutation_plan_fingerprint"] = (
            prepared.transaction_companion.storage_mutation_plan_fingerprint
        )
        return McpPreparedSuspensionCommitView(
            **payload,
            view_fingerprint=context_fingerprint(
                "mcp-prepared-suspension-commit-view:v2", fingerprint_payload
            ),
        )

    @property
    def elicitation_batch_owner(self) -> McpElicitationBatchOwner:
        return self._prepared.elicitation_batch_owner

    @property
    def recovery_rebind_receipt(self) -> McpStatelessRecoveryRebindReceipt | None:
        return self._prepared.recovery_rebind_receipt

    def __reduce__(self):
        raise TypeError("MCP pending execution handles are process-local")


@dataclass(frozen=True, slots=True)
class RecoveredMcpContinuationOwner:
    pending_handle: McpPendingExecutionHandle
    prepared_resolution: PreparedMcpInputRequiredResolution | None
    recovery_state: str

    def __post_init__(self) -> None:
        if self.recovery_state not in {"awaiting_client_input", "replay_ready"}:
            raise ValueError("invalid MCP continuation recovery state")
        if (self.prepared_resolution is None) != (
            self.recovery_state == "awaiting_client_input"
        ):
            raise ValueError("MCP recovery state/resolution matrix mismatch")


class RuntimeMcpToolExecutionPort:
    """Own live MCP managers, leases, raw protocol state, and settlement."""

    def __init__(
        self,
        supervisor: McpServerSupervisor,
        *,
        continuation_codec: McpContinuationSecretCodec | None = None,
        continuation_repository: McpContinuationRepository | None = None,
        external_browser_port: McpExternalBrowserPort | None = None,
    ) -> None:
        if (continuation_codec is None) != (continuation_repository is None):
            raise ValueError(
                "MCP continuation codec and repository must be installed together"
            )
        self._supervisor = supervisor
        self._continuation_codec = continuation_codec
        self._continuation_repository = continuation_repository
        self._external_browser_port = external_browser_port
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

    async def terminalize_reopened_input_required(
        self,
        runtime_session: "RuntimeSession",
        *,
        run_id: str,
        closure_reason: str,
        deadline_monotonic: float,
    ) -> "RecoveredMcpInputRequiredClosure":
        """Own exact secret-row deletion for a reopened MCP suspension."""

        from pulsara_agent.runtime.mcp.recovery import (
            terminalize_reopened_mcp_input_required,
        )

        self._begin_operation()
        try:
            return await terminalize_reopened_mcp_input_required(
                runtime_session,
                run_id=run_id,
                closure_reason=closure_reason,
                deadline_monotonic=deadline_monotonic,
                continuation_repository=self._require_continuation_repository(),
            )
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
                lease,
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
        if isinstance(result, McpClientInputRequired):
            assert lease is not None
            reservation = None
            try:
                reservation = self._supervisor.promote_lease_to_pending(
                    lease, result.interaction_id
                )
                prepared = _prepare_suspension(
                    codec=self._require_continuation_codec(),
                    repository=self._require_continuation_repository(),
                    issuer_id=self._instance_id,
                    result=result,
                    owner=request.owner,
                    exposed_tool_name=request.exposed_tool_name,
                    binding=request.binding.binding_identity,
                    binding_contract_fingerprint=(
                        request.binding.contract_fact_fingerprint
                    ),
                    reservation_id=reservation.reservation_id,
                    predecessor_resolution_reference=None,
                    inherited_expiry=None,
                    source_dispatch_record=None,
                    browser_port=self._external_browser_port,
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
            handle.elicitation_batch_owner.retire()
            with self._lock:
                self._handles.pop(handle.identity.handle_id, None)
        elif handle.state is McpPendingHandleState.PENDING_CONFIRMED:
            handle._candidate_owner = None
        return transition

    def prepare_resolution(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        source_suspension_event_reference: ContextEventReferenceFact,
        source_suspension: McpInputRequiredSuspensionFact,
        attempt_ordinal: int,
        submitted_at_utc: str,
    ) -> PreparedMcpInputRequiredResolution:
        handle = self._require_handle(pending_handle)
        prepared = handle._prepared
        if source_suspension_event_reference.event_id != prepared.suspension_event_id:
            raise ValueError("MCP resolution names another suspension event")
        if (
            source_suspension.interaction != prepared.interaction
            or source_suspension.binding_identity != prepared.binding_identity
            or source_suspension.pending_lease_reservation
            != prepared.pending_lease_reservation
            or source_suspension.request_envelope != prepared.request_envelope
            or source_suspension.durable_continuation
            != prepared.continuation.durable_fact
        ):
            raise ValueError("MCP resolution suspension authority drifted")
        with self._lock:
            if handle._prepared_resolution is not None:
                existing = handle._prepared_resolution
                if (
                    existing.source_suspension_event_reference
                    != source_suspension_event_reference
                ):
                    raise ValueError("MCP resolution retry changed source authority")
                if handle._state is McpPendingHandleState.PENDING_CONFIRMED:
                    frozen = handle.elicitation_batch_owner.begin_commit()
                    if frozen is not existing.sealed_responses:
                        handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                        raise RuntimeError(
                            "MCP resolution retry changed sealed responses"
                        )
                    handle._state = McpPendingHandleState.RESOLUTION_COMMIT_IN_FLIGHT
                elif (
                    handle._state
                    is not McpPendingHandleState.RESOLUTION_COMMIT_IN_FLIGHT
                ):
                    raise RuntimeError(
                        "MCP resolution candidate is no longer retryable"
                    )
                return existing
            if handle._state is not McpPendingHandleState.PENDING_CONFIRMED:
                raise RuntimeError("MCP pending handle is not resolution-ready")
        responses = handle.elicitation_batch_owner.begin_commit()
        resolution_event_id = "mcp_resolution:" + stable_runtime_event_id(
            "mcp-input-required-resolution-submitted-event:v2",
            source_suspension_event_reference.event_id,
            prepared.interaction.round_count,
            attempt_ordinal,
            responses.response_attribution_fingerprint,
        )
        replay = prepare_mcp_replay_continuation(
            codec=self._require_continuation_codec(),
            source=prepared.continuation,
            source_plaintext=prepared.continuation.plaintext,
            resolution_event_id=resolution_event_id,
            current_round_input_responses=responses,
            created_at_utc=submitted_at_utc,
            bounds=prepared.continuation.durable_fact.bounds,
        )
        semantic = build_mcp_continuation_fact(
            McpInputRequiredResolutionSemanticFact,
            schema_version="mcp_input_required_resolution.v2",
            request_set_fingerprint=responses.request_set_fingerprint,
            ordered_response_keys=responses.ordered_request_keys,
            commitment_key_id=responses.commitment_key_id,
            keyed_current_round_responses_commitment=(
                responses.keyed_current_round_responses_commitment
            ),
            response_attribution_fingerprint=(
                responses.response_attribution_fingerprint
            ),
        )
        durable = prepared.continuation.durable_fact
        carrier = build_mcp_continuation_fact(
            McpContinuationResolutionCarrierFact,
            schema_version="mcp_continuation_resolution_carrier.v1",
            source_continuation_carrier_id=durable.continuation_carrier_id,
            replay_continuation_carrier_id=(
                replay.stored_record.envelope.continuation_carrier_id
            ),
            source_suspension_event_reference=source_suspension_event_reference,
            source_carrier_plaintext_commitment=(durable.carrier_plaintext_commitment),
            source_stored_envelope_fingerprint=(durable.stored_envelope_fingerprint),
            replay_plaintext_commitment=(
                replay.stored_record.envelope.carrier_plaintext_commitment
            ),
            retryable_base_params_commitment=(replay.retryable_base_params_commitment),
            ordered_response_keys=responses.ordered_request_keys,
            keyed_current_round_responses_commitment=(
                responses.keyed_current_round_responses_commitment
            ),
            response_attribution_fingerprint=(
                responses.response_attribution_fingerprint
            ),
            replay_stored_envelope_fingerprint=(
                replay.stored_record.envelope.stored_envelope_fingerprint
            ),
            retryable_payload_kind=durable.retryable_payload_kind,
            source_method=durable.source_method,
            source_method_schema_fingerprint=(durable.source_method_schema_fingerprint),
            request_set_fingerprint=durable.request_set_fingerprint,
            commitment_key_id=durable.commitment_key_id,
            bounds_fingerprint=durable.bounds.bounds_fingerprint,
            protocol_semantic_fingerprint=durable.protocol_semantic_fingerprint,
            endpoint_attribution_fingerprint=(durable.endpoint_attribution_fingerprint),
            auth_attribution_fingerprint=durable.auth_attribution_fingerprint,
            binding_contract_fingerprint=durable.binding_contract_fingerprint,
            resolution_event_id=resolution_event_id,
            round_ordinal=durable.round_ordinal,
            operation_expires_at_utc=durable.expiry.operation_expires_at_utc,
            expiry_fingerprint=durable.expiry.expiry_fingerprint,
        )
        companion = build_mcp_continuation_transaction_intent(
            companion_kind=McpContinuationCompanionKind.RESOLUTION_REPLAY_READY,
            mutation_kind=(McpContinuationMutationKind.REPLACE_WITH_REPLAY_READY),
            runtime_session_id=prepared.continuation.stored_record.runtime_session_id,
            interaction_id=prepared.interaction.interaction_id,
            round_ordinal=prepared.interaction.round_count,
            source_event_id=resolution_event_id,
            repository=self._require_continuation_repository(),
            issuer_id=self._instance_id,
            issuer_generation=attempt_ordinal,
            charge_contract_fingerprint=(
                mcp_continuation_charge_contract_fingerprint(durable.bounds)
            ),
            source_carrier_id=durable.continuation_carrier_id,
            resulting_record=replay.stored_record,
            expected_control=prepared.continuation.stored_record.control,
        )
        fingerprint_payload = {
            "source_suspension_event_reference": (
                source_suspension_event_reference.model_dump(mode="json")
            ),
            "source_suspension_fact_fingerprint": (
                source_suspension.suspension_fact_fingerprint
            ),
            "interaction_id": prepared.interaction.interaction_id,
            "resolution_semantic_fingerprint": (
                semantic.resolution_semantic_fingerprint
            ),
            "resolution_carrier_fact_fingerprint": (
                carrier.resolution_carrier_fact_fingerprint
            ),
            "storage_mutation_plan_fingerprint": (
                companion.storage_mutation_plan_fingerprint
            ),
            "batch_owner_id": handle.elicitation_batch_owner.identity.owner_id,
        }
        resolution = PreparedMcpInputRequiredResolution(
            source_suspension_event_reference=source_suspension_event_reference,
            source_suspension_fact_fingerprint=(
                source_suspension.suspension_fact_fingerprint
            ),
            interaction_id=prepared.interaction.interaction_id,
            resolution_semantic=semantic,
            resolution_carrier=carrier,
            transaction_companion=companion,
            sealed_responses=responses,
            batch_owner_id=handle.elicitation_batch_owner.identity.owner_id,
            prepared_resolution_fingerprint=context_fingerprint(
                "prepared-mcp-input-required-resolution:v2",
                fingerprint_payload,
            ),
        )
        with self._lock:
            if handle._state is not McpPendingHandleState.PENDING_CONFIRMED:
                handle.elicitation_batch_owner.confirm_commit("unknown")
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP resolution installation lost its owner")
            handle._prepared_resolution = resolution
            handle._prepared_replay = replay
            handle._state = McpPendingHandleState.RESOLUTION_COMMIT_IN_FLIGHT
        return resolution

    def confirm_resolution_commit(
        self,
        *,
        prepared_resolution: PreparedMcpInputRequiredResolution,
        outcome: str,
    ) -> None:
        with self._lock:
            matches = tuple(
                item
                for item in self._handles.values()
                if item._prepared_resolution is prepared_resolution
            )
            if len(matches) != 1:
                raise RuntimeError("MCP resolution does not own one pending handle")
            handle = matches[0]
            if handle._state is not McpPendingHandleState.RESOLUTION_COMMIT_IN_FLIGHT:
                raise RuntimeError("MCP resolution has no commit in flight")
            if outcome == "full":
                handle._state = McpPendingHandleState.REPLAY_READY
            elif outcome == "none":
                handle._state = McpPendingHandleState.PENDING_CONFIRMED
            else:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
        handle.elicitation_batch_owner.confirm_commit(outcome)
        if outcome == "full":
            handle.elicitation_batch_owner.retire()

    def prepare_dispatch(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        prepared_resolution: PreparedMcpInputRequiredResolution,
        source_resolution_event_reference: ContextEventReferenceFact,
        commit_guard: McpDispatchReservationCommitGuard,
    ) -> PreparedMcpContinuationDispatch:
        handle = self._require_handle(pending_handle)
        with self._lock:
            if handle._prepared_dispatch is not None:
                existing = handle._prepared_dispatch
                if (
                    existing.commit_guard.guard_generation
                    > commit_guard.guard_generation
                ):
                    raise ValueError("MCP dispatch guard generation moved backwards")
                if (
                    existing.commit_guard.physical_reservation_fingerprint
                    != commit_guard.physical_reservation_fingerprint
                    or existing.dispatch_reservation.source_resolution_event_reference
                    != source_resolution_event_reference
                ):
                    raise ValueError("MCP dispatch retry changed stable authority")
                if handle._state is not McpPendingHandleState.REPLAY_READY:
                    raise RuntimeError("MCP dispatch retry is not replay-ready")
                retry = PreparedMcpContinuationDispatch(
                    dispatch_event_id=existing.dispatch_event_id,
                    dispatch_reservation=existing.dispatch_reservation,
                    transaction_companion=existing.transaction_companion,
                    replay_plaintext=existing.replay_plaintext,
                    commit_guard=commit_guard,
                    prepared_dispatch_fingerprint=context_fingerprint(
                        "prepared-mcp-continuation-dispatch:v1",
                        {
                            "dispatch_event_id": existing.dispatch_event_id,
                            "dispatch_reservation_fingerprint": (
                                existing.dispatch_reservation.dispatch_reservation_fingerprint
                            ),
                            "storage_mutation_plan_fingerprint": (
                                existing.transaction_companion.storage_mutation_plan_fingerprint
                            ),
                            "commit_guard_fingerprint": commit_guard.guard_fingerprint,
                        },
                    ),
                )
                handle._prepared_dispatch = retry
                handle._state = McpPendingHandleState.DISPATCH_COMMIT_IN_FLIGHT
                return retry
            if (
                handle._state is not McpPendingHandleState.REPLAY_READY
                or handle._prepared_resolution is not prepared_resolution
                or handle._prepared_replay is None
            ):
                raise RuntimeError("MCP handle is not replay-ready")
        replay = handle._prepared_replay
        replay_record = replay.stored_record
        expected = replay_record.control
        durable = handle._prepared.continuation.durable_fact
        if datetime.now(timezone.utc) >= _parse_utc_datetime(
            durable.expiry.operation_expires_at_utc
        ):
            raise RuntimeError("MCP continuation expired before dispatch")
        if expected.carrier_state is not McpContinuationCarrierState.REPLAY_READY:
            raise ValueError("MCP dispatch source is not replay-ready")
        if (
            source_resolution_event_reference.event_id
            != prepared_resolution.resolution_carrier.resolution_event_id
            or commit_guard.runtime_session_id != replay_record.runtime_session_id
            or commit_guard.interaction_id != replay_record.interaction_id
            or commit_guard.tool_call_id != handle._invocation_owner.tool_call_id
        ):
            raise ValueError("MCP dispatch source/physical guard mismatch")
        dispatch_event_id = "mcp_dispatch:" + stable_runtime_event_id(
            "mcp-continuation-dispatch-reserved-event:v1",
            replay_record.runtime_session_id,
            replay_record.envelope.continuation_carrier_id,
            source_resolution_event_reference.event_id,
            1,
            expected.control_revision,
        )
        sdk_generation = handle._prepared.client_input_result.sdk_client_generation_id
        dispatch_reservation_id = context_fingerprint(
            "mcp-continuation-dispatch-reservation:v1",
            {
                "dispatch_event_id": dispatch_event_id,
                "physical_operation_reservation_event_id": (
                    commit_guard.physical_reservation_event_reference.event_id
                ),
                "sdk_client_generation_id": sdk_generation,
            },
        )
        resulting_control = build_mcp_continuation_storage_fact(
            McpContinuationCarrierControlFact,
            schema_version="mcp_continuation_carrier_control.v1",
            continuation_carrier_id=expected.continuation_carrier_id,
            carrier_state=McpContinuationCarrierState.DISPATCH_RESERVED,
            control_revision=expected.control_revision + 1,
            source_event_id=dispatch_event_id,
            stored_envelope_fingerprint=expected.stored_envelope_fingerprint,
        )
        dispatch = build_mcp_continuation_fact(
            McpContinuationDispatchReservationFact,
            schema_version="mcp_continuation_dispatch_reservation.v1",
            dispatch_reservation_id=dispatch_reservation_id,
            runtime_session_id=replay_record.runtime_session_id,
            interaction_id=replay_record.interaction_id,
            physical_operation_id=commit_guard.physical_operation_id,
            replay_continuation_carrier_id=(
                replay_record.envelope.continuation_carrier_id
            ),
            source_resolution_event_reference=source_resolution_event_reference,
            source_physical_operation_reservation_event_reference=(
                commit_guard.physical_reservation_event_reference
            ),
            expected_control_revision=expected.control_revision,
            expected_control_fingerprint=expected.control_fingerprint,
            resulting_control_revision=resulting_control.control_revision,
            resulting_control_fingerprint=resulting_control.control_fingerprint,
            retryable_payload_kind=durable.retryable_payload_kind,
            source_method=durable.source_method,
            source_method_schema_fingerprint=(durable.source_method_schema_fingerprint),
            protocol_semantic_fingerprint=durable.protocol_semantic_fingerprint,
            endpoint_attribution_fingerprint=(durable.endpoint_attribution_fingerprint),
            auth_attribution_fingerprint=durable.auth_attribution_fingerprint,
            binding_contract_fingerprint=durable.binding_contract_fingerprint,
            sdk_client_generation_id=sdk_generation,
            dispatch_ordinal=1,
            operation_expires_at_utc=durable.expiry.operation_expires_at_utc,
            expiry_fingerprint=durable.expiry.expiry_fingerprint,
        )
        companion = build_mcp_continuation_transaction_intent(
            companion_kind=McpContinuationCompanionKind.DISPATCH_RESERVE,
            mutation_kind=McpContinuationMutationKind.RESERVE_DISPATCH,
            runtime_session_id=replay_record.runtime_session_id,
            interaction_id=replay_record.interaction_id,
            round_ordinal=replay_record.round_ordinal,
            source_event_id=dispatch_event_id,
            repository=self._require_continuation_repository(),
            issuer_id=self._instance_id,
            issuer_generation=commit_guard.guard_generation,
            charge_contract_fingerprint=(
                mcp_continuation_charge_contract_fingerprint(durable.bounds)
            ),
            source_carrier_id=replay_record.envelope.continuation_carrier_id,
            expected_control=expected,
            resulting_control=resulting_control,
        )
        fingerprint_payload = {
            "dispatch_event_id": dispatch_event_id,
            "dispatch_reservation_fingerprint": (
                dispatch.dispatch_reservation_fingerprint
            ),
            "storage_mutation_plan_fingerprint": (
                companion.storage_mutation_plan_fingerprint
            ),
            "commit_guard_fingerprint": commit_guard.guard_fingerprint,
        }
        prepared_dispatch = PreparedMcpContinuationDispatch(
            dispatch_event_id=dispatch_event_id,
            dispatch_reservation=dispatch,
            transaction_companion=companion,
            replay_plaintext=replay.plaintext,
            commit_guard=commit_guard,
            prepared_dispatch_fingerprint=context_fingerprint(
                "prepared-mcp-continuation-dispatch:v1",
                fingerprint_payload,
            ),
        )
        with self._lock:
            if handle._state is not McpPendingHandleState.REPLAY_READY:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP dispatch preparation lost its owner")
            handle._prepared_dispatch = prepared_dispatch
            handle._state = McpPendingHandleState.DISPATCH_COMMIT_IN_FLIGHT
        return prepared_dispatch

    def confirm_dispatch_commit(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        prepared_dispatch: PreparedMcpContinuationDispatch,
        outcome: str,
    ) -> McpConfirmedContinuationDispatchReceipt | None:
        handle = self._require_handle(pending_handle)
        with self._lock:
            if (
                handle._prepared_dispatch is not prepared_dispatch
                or handle._state is not McpPendingHandleState.DISPATCH_COMMIT_IN_FLIGHT
            ):
                raise RuntimeError("MCP dispatch has no matching commit in flight")
            if outcome == "full":
                fact = prepared_dispatch.dispatch_reservation
                receipt_payload = {
                    "dispatch_event_id": prepared_dispatch.dispatch_event_id,
                    "source_resolution_event_id": (
                        fact.source_resolution_event_reference.event_id
                    ),
                    "replay_continuation_carrier_id": (
                        fact.replay_continuation_carrier_id
                    ),
                    "runtime_session_id": fact.runtime_session_id,
                    "interaction_id": fact.interaction_id,
                    "round_ordinal": (handle._prepared.interaction.round_count),
                    "physical_operation_id": fact.physical_operation_id,
                    "resulting_control_revision": fact.resulting_control_revision,
                    "sdk_client_generation_id": fact.sdk_client_generation_id,
                    "operation_expires_at_utc": fact.operation_expires_at_utc,
                }
                receipt = McpConfirmedContinuationDispatchReceipt(
                    **receipt_payload,
                    receipt_fingerprint=context_fingerprint(
                        "mcp-confirmed-continuation-dispatch-receipt:v1",
                        receipt_payload,
                    ),
                )
                replay = handle._prepared_replay
                if replay is None:
                    raise RuntimeError("MCP dispatch lost its replay carrier")
                resulting_control = build_mcp_continuation_storage_fact(
                    McpContinuationCarrierControlFact,
                    schema_version="mcp_continuation_carrier_control.v1",
                    continuation_carrier_id=fact.replay_continuation_carrier_id,
                    carrier_state=McpContinuationCarrierState.DISPATCH_RESERVED,
                    control_revision=fact.resulting_control_revision,
                    source_event_id=prepared_dispatch.dispatch_event_id,
                    stored_envelope_fingerprint=(
                        replay.stored_record.envelope.stored_envelope_fingerprint
                    ),
                )
                if resulting_control.control_fingerprint != (
                    fact.resulting_control_fingerprint
                ):
                    raise RuntimeError("MCP dispatch resulting control drifted")
                handle._dispatch_record = McpContinuationStoredRecord(
                    runtime_session_id=replay.stored_record.runtime_session_id,
                    interaction_id=replay.stored_record.interaction_id,
                    source_event_id=receipt.dispatch_event_id,
                    round_ordinal=replay.stored_record.round_ordinal,
                    envelope=replay.stored_record.envelope,
                    control=resulting_control,
                )
                handle._dispatch_receipt = receipt
                handle._state = McpPendingHandleState.DISPATCH_RESERVED
                return receipt
            if outcome == "none":
                handle._state = McpPendingHandleState.REPLAY_READY
                return None
            handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
            return None

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
            if handle._state is not McpPendingHandleState.DISPATCH_RESERVED:
                return _rejected(
                    McpToolRejectCode.PENDING_LEASE_BORROW_FAILED,
                    RuntimeError("MCP continuation dispatch is not confirmed"),
                    retryable=False,
                )
        if (
            request.prepared_resolution.interaction_id != handle.identity.interaction_id
            or request.source_suspension.suspension_fact_fingerprint
            != request.prepared_resolution.source_suspension_fact_fingerprint
            or handle._prepared_resolution is not request.prepared_resolution
            or handle._dispatch_receipt != request.dispatch_receipt
            or handle._prepared_dispatch is None
        ):
            return _rejected(
                McpToolRejectCode.RESOLUTION_IDENTITY_MISMATCH,
                RuntimeError("MCP resolution authority mismatch"),
                retryable=False,
            )
        identity = _runtime_binding(request.binding.binding_identity)
        with self._lock:
            if handle._state is not McpPendingHandleState.DISPATCH_RESERVED:
                raise RuntimeError("MCP resume admission state changed concurrently")
            handle._state = McpPendingHandleState.RESUME_IN_FLIGHT
        try:
            lease = self._supervisor.borrow_pending_lease(
                handle.identity.interaction_id, identity
            )
        except Exception as exc:
            self._restore_dispatch_retry_state(handle)
            return _rejected(McpToolRejectCode.ADAPTER_ERROR, exc, retryable=True)
        except BaseException:
            self._restore_dispatch_retry_state(handle)
            raise
        try:
            manager = self._supervisor.manager_for_lease(lease)
            result = await manager.resume_suspended_request(
                binding_lease=lease,
                replay_plaintext=handle._prepared_dispatch.replay_plaintext,
                dispatch_receipt=request.dispatch_receipt,
                timeout_ms=request.timeout_ms,
            )
        except Exception as exc:
            self._return_resume_borrow(handle)
            with self._lock:
                if handle._state is not McpPendingHandleState.RESUME_IN_FLIGHT:
                    handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                    raise RuntimeError("MCP failed replay lost its in-flight owner")
                handle._state = McpPendingHandleState.RESUME_RESULT_RECEIVED
                handle._frozen_resume_request_fingerprint = request.request_fingerprint
            return self._freeze_resume_lowering_failure(handle, error=exc)
        except BaseException:
            self._return_resume_borrow(handle)
            self._mark_resume_reconciliation(handle)
            raise
        with self._lock:
            if handle._state is not McpPendingHandleState.RESUME_IN_FLIGHT:
                handle._state = McpPendingHandleState.RECONCILIATION_REQUIRED
                raise RuntimeError("MCP resume result lost its in-flight owner")
            handle._state = McpPendingHandleState.RESUME_RESULT_RECEIVED
            handle._frozen_resume_request_fingerprint = request.request_fingerprint
        self._return_resume_borrow(handle)
        try:
            if isinstance(result, McpClientInputRequired):
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
        terminal_event_id: str,
    ) -> McpPreparedTerminalSettlement:
        handle = self._require_handle(pending_handle)
        if (
            candidate_owner_identity.candidate_kind
            is not ToolExecutionStableCandidateKind.TERMINAL
            or candidate_owner_identity.physical_owner_identity_fingerprint
            != handle.identity.identity_fingerprint
            or candidate_owner_identity.tool_call_id
            != handle._invocation_owner.tool_call_id
            or terminal_event_id
            not in candidate_owner_identity.ordered_candidate_event_ids
        ):
            raise ValueError("MCP terminal candidate owner identity mismatch")
        source_record = self._terminal_source_record(handle)
        with self._lock:
            handle._candidate_owner = candidate_owner_identity
            handle._settlement_generation += 1
            handle._state = McpPendingHandleState.TERMINAL_CANDIDATE_FROZEN
            payload = {
                "pending_handle_identity": asdict(handle.identity),
                "reason": reason.value,
                "candidate_owner_identity": asdict(candidate_owner_identity),
                "terminal_event_id": terminal_event_id,
                "settlement_generation": handle._settlement_generation,
            }
            transaction_companion = build_mcp_continuation_transaction_intent(
                companion_kind=McpContinuationCompanionKind.TERMINAL_DELETE,
                mutation_kind=McpContinuationMutationKind.DELETE_TERMINAL,
                runtime_session_id=source_record.runtime_session_id,
                interaction_id=source_record.interaction_id,
                round_ordinal=source_record.round_ordinal,
                source_event_id=terminal_event_id,
                repository=self._require_continuation_repository(),
                issuer_id=self._instance_id,
                issuer_generation=handle._settlement_generation,
                charge_contract_fingerprint=(
                    mcp_continuation_charge_contract_fingerprint(
                        handle._prepared.continuation.durable_fact.bounds
                    )
                ),
                source_carrier_id=(source_record.envelope.continuation_carrier_id),
                expected_control=source_record.control,
            )
            payload["storage_mutation_plan_fingerprint"] = (
                transaction_companion.storage_mutation_plan_fingerprint
            )
            settlement = McpPreparedTerminalSettlement(
                pending_handle_identity=handle.identity,
                reason=reason,
                candidate_owner_identity=candidate_owner_identity,
                terminal_event_id=terminal_event_id,
                transaction_companion=transaction_companion,
                settlement_generation=handle._settlement_generation,
                settlement_fingerprint=context_fingerprint(
                    "mcp-prepared-terminal-settlement:v1", payload
                ),
            )
            handle._prepared_settlement = settlement
            return settlement

    @staticmethod
    def _terminal_source_record(
        handle: _RuntimeMcpPendingExecutionHandle,
    ) -> McpContinuationStoredRecord:
        if handle._dispatch_record is not None:
            return handle._dispatch_record
        if handle._prepared_replay is not None and handle._state in {
            McpPendingHandleState.REPLAY_READY,
            McpPendingHandleState.DISPATCH_COMMIT_IN_FLIGHT,
        }:
            return handle._prepared_replay.stored_record
        return handle._prepared.continuation.stored_record

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
            if (
                handle.elicitation_batch_owner.state
                is not McpElicitationBatchState.RETIRED
            ):
                handle.elicitation_batch_owner.retire()
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

    def recover_committed_continuation(
        self,
        *,
        owner: McpInvocationOwner,
        binding: McpToolBindingContract,
        source_suspension_event_reference: ContextEventReferenceFact,
        source_suspension: McpInputRequiredSuspensionFact,
        source_resolution_event_reference: ContextEventReferenceFact | None = None,
        resolution_semantic: McpInputRequiredResolutionSemanticFact | None = None,
        resolution_carrier: McpContinuationResolutionCarrierFact | None = None,
    ) -> RecoveredMcpContinuationOwner:
        """Rebuild one exact stateless continuation from durable authorities."""

        codec = self._require_continuation_codec()
        repository = self._require_continuation_repository()
        interaction = source_suspension.interaction
        durable = source_suspension.durable_continuation
        if (
            owner.runtime_session_id
            != source_suspension_event_reference.runtime_session_id
            or owner.tool_call_id != interaction.tool_call_id
            or binding.tool_name != interaction.tool_name
            or binding.binding_identity.server_id
            != source_suspension.binding_identity.server_id
            or binding.binding_identity.snapshot_id
            != source_suspension.binding_identity.snapshot_id
            or binding.binding_identity.discovery_generation
            != source_suspension.binding_identity.discovery_generation
            or source_suspension_event_reference.event_id
            != self._source_suspension_event_id(source_suspension_event_reference)
        ):
            raise RuntimeError("MCP continuation recovery binding/source mismatch")
        runtime_binding = _runtime_binding(binding.binding_identity)
        (
            protocol_revision,
            protocol_semantic_fingerprint,
            endpoint_attribution_fingerprint,
            auth_attribution_fingerprint,
            sdk_client_generation_id,
            effective_snapshot_semantic_fingerprint,
        ) = self._supervisor.recovery_rebind_authority(runtime_binding)
        if (
            protocol_revision != "2026-07-28"
            or source_suspension.request_envelope.protocol_revision != protocol_revision
            or durable.protocol_semantic_fingerprint != protocol_semantic_fingerprint
            or durable.endpoint_attribution_fingerprint
            != endpoint_attribution_fingerprint
            or durable.auth_attribution_fingerprint != auth_attribution_fingerprint
        ):
            raise RuntimeError("MCP continuation recovery target authority changed")
        rebind_payload = {
            "source_binding_identity": source_suspension.binding_identity.model_dump(
                mode="json"
            ),
            "effective_binding_identity": binding.binding_identity.model_dump(
                mode="json"
            ),
            "source_suspension_event_reference": (
                source_suspension_event_reference.model_dump(mode="json")
            ),
            "source_suspension_fact_fingerprint": (
                source_suspension.suspension_fact_fingerprint
            ),
            "source_binding_contract_fingerprint": (
                durable.binding_contract_fingerprint
            ),
            "effective_binding_contract_fingerprint": (
                binding.contract_fact_fingerprint
            ),
            "effective_snapshot_semantic_fingerprint": (
                effective_snapshot_semantic_fingerprint
            ),
            "protocol_semantic_fingerprint": protocol_semantic_fingerprint,
            "endpoint_attribution_fingerprint": endpoint_attribution_fingerprint,
            "auth_attribution_fingerprint": auth_attribution_fingerprint,
        }
        recovery_rebind_receipt = McpStatelessRecoveryRebindReceipt(
            source_binding_identity=source_suspension.binding_identity,
            effective_binding_identity=binding.binding_identity,
            source_suspension_event_reference=source_suspension_event_reference,
            source_suspension_fact_fingerprint=(
                source_suspension.suspension_fact_fingerprint
            ),
            source_binding_contract_fingerprint=durable.binding_contract_fingerprint,
            effective_binding_contract_fingerprint=(binding.contract_fact_fingerprint),
            effective_snapshot_semantic_fingerprint=(
                effective_snapshot_semantic_fingerprint
            ),
            protocol_semantic_fingerprint=protocol_semantic_fingerprint,
            endpoint_attribution_fingerprint=endpoint_attribution_fingerprint,
            auth_attribution_fingerprint=auth_attribution_fingerprint,
            receipt_fingerprint=context_fingerprint(
                "mcp-stateless-recovery-rebind-receipt:v1", rebind_payload
            ),
        )
        expiry = _parse_utc_datetime(durable.expiry.operation_expires_at_utc)
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("MCP continuation expired before recovery")

        if source_resolution_event_reference is None:
            if resolution_semantic is not None or resolution_carrier is not None:
                raise ValueError("awaiting MCP recovery received partial resolution")
            carrier_id = durable.continuation_carrier_id
            expected_commitment = durable.carrier_plaintext_commitment
            expected_state = McpContinuationCarrierState.AWAITING_CLIENT_INPUT
            aad_source_event_id = source_suspension_event_reference.event_id
        else:
            if resolution_semantic is None or resolution_carrier is None:
                raise ValueError("replay-ready MCP recovery lacks its resolution")
            if (
                resolution_carrier.source_suspension_event_reference
                != source_suspension_event_reference
                or resolution_carrier.resolution_event_id
                != source_resolution_event_reference.event_id
                or resolution_carrier.request_set_fingerprint
                != source_suspension.request_envelope.request_set_fingerprint
                or resolution_semantic.request_set_fingerprint
                != resolution_carrier.request_set_fingerprint
                or resolution_semantic.ordered_response_keys
                != resolution_carrier.ordered_response_keys
                or resolution_semantic.keyed_current_round_responses_commitment
                != resolution_carrier.keyed_current_round_responses_commitment
                or resolution_semantic.response_attribution_fingerprint
                != resolution_carrier.response_attribution_fingerprint
                or resolution_carrier.operation_expires_at_utc
                != durable.expiry.operation_expires_at_utc
                or resolution_carrier.expiry_fingerprint
                != durable.expiry.expiry_fingerprint
            ):
                raise RuntimeError("MCP replay-ready recovery resolution drifted")
            carrier_id = resolution_carrier.replay_continuation_carrier_id
            expected_commitment = resolution_carrier.replay_plaintext_commitment
            expected_state = McpContinuationCarrierState.REPLAY_READY
            aad_source_event_id = source_resolution_event_reference.event_id
        record = repository.read(carrier_id)
        if (
            record is None
            or record.runtime_session_id != owner.runtime_session_id
            or record.interaction_id != interaction.interaction_id
            or record.source_event_id != aad_source_event_id
            or record.round_ordinal != interaction.round_count
            or record.envelope.continuation_carrier_id != carrier_id
            or record.control.continuation_carrier_id != carrier_id
            or record.control.carrier_state is not expected_state
            or record.control.source_event_id != aad_source_event_id
            or record.control.stored_envelope_fingerprint
            != record.envelope.stored_envelope_fingerprint
        ):
            raise RuntimeError("MCP continuation recovery storage authority drifted")
        if source_resolution_event_reference is None:
            if (
                record.envelope.stored_envelope_fingerprint
                != durable.stored_envelope_fingerprint
            ):
                raise RuntimeError("MCP awaiting continuation envelope drifted")
        elif (
            record.envelope.stored_envelope_fingerprint
            != resolution_carrier.replay_stored_envelope_fingerprint
        ):
            raise RuntimeError("MCP replay continuation envelope drifted")
        plaintext = codec.decrypt_and_rebind(
            envelope=record.envelope,
            aad_context=McpContinuationAadContext(
                runtime_session_id=record.runtime_session_id,
                interaction_id=record.interaction_id,
                source_event_id=record.source_event_id,
                round_ordinal=record.round_ordinal,
                operation_expires_at_utc=durable.expiry.operation_expires_at_utc,
                expiry_fingerprint=durable.expiry.expiry_fingerprint,
            ),
            expected_plaintext_commitment=expected_commitment,
            bounds=durable.bounds,
            plaintext_suspension_event_id=(source_suspension_event_reference.event_id),
        )
        attribution = codec.recovery_attribution(plaintext)
        if attribution != {
            "runtime_session_id": owner.runtime_session_id,
            "interaction_id": interaction.interaction_id,
            "suspension_event_id": source_suspension_event_reference.event_id,
            "round_ordinal": interaction.round_count,
            "request_set_fingerprint": durable.request_set_fingerprint,
            "protocol_semantic_fingerprint": durable.protocol_semantic_fingerprint,
            "endpoint_attribution_fingerprint": durable.endpoint_attribution_fingerprint,
            "auth_attribution_fingerprint": durable.auth_attribution_fingerprint,
            "binding_contract_fingerprint": durable.binding_contract_fingerprint,
            "created_at_utc": record.envelope.created_at_utc,
            "operation_expires_at_utc": durable.expiry.operation_expires_at_utc,
            "expiry_fingerprint": durable.expiry.expiry_fingerprint,
        }:
            raise RuntimeError("MCP continuation plaintext attribution drifted")
        if expected_state is McpContinuationCarrierState.AWAITING_CLIENT_INPUT:
            retryable_payload, request_state, private_urls = (
                codec.awaiting_recovery_parts(plaintext)  # type: ignore[arg-type]
            )
        else:
            (
                retryable_payload,
                request_state,
                sealed_responses,
                response_attribution,
                plaintext_resolution_event_id,
            ) = codec.replay_recovery_parts(plaintext)  # type: ignore[arg-type]
            if (
                plaintext_resolution_event_id
                != source_resolution_event_reference.event_id
                or response_attribution
                != resolution_semantic.response_attribution_fingerprint
                or sealed_responses.keyed_current_round_responses_commitment
                != resolution_semantic.keyed_current_round_responses_commitment
            ):
                raise RuntimeError("MCP replay plaintext resolution drifted")
            private_urls = ()
        input_requests = tuple(
            item.request
            for item in source_suspension.request_envelope.ordered_user_visible_input_requests
        )
        leg_payload = {
            "leg_kind": "client_input_required",
            "input_requests": input_requests,
            "ordered_request_keys": tuple(item.key for item in input_requests),
            "request_set_fingerprint": durable.request_set_fingerprint,
            "request_state": request_state,
            "leg_ordinal": interaction.round_count,
            "retryable_payload_fingerprint": (
                retryable_payload.process_local_payload_fingerprint
            ),
            "operation_deadline_monotonic": monotonic() + remaining,
        }
        leg = McpClientInputRequiredLeg(
            **leg_payload,
            leg_fingerprint=context_fingerprint(
                "mcp-client-input-required-leg:v1", leg_payload
            ),
        )
        recovered_result = McpClientInputRequired(
            interaction_id=interaction.interaction_id,
            server_id=interaction.server_id,
            exact_protocol_revision=protocol_revision,
            protocol_semantic_fingerprint=protocol_semantic_fingerprint,
            endpoint_attribution_fingerprint=endpoint_attribution_fingerprint,
            auth_attribution_fingerprint=auth_attribution_fingerprint,
            sdk_client_generation_id=sdk_client_generation_id,
            leg=leg,
            retryable_request_payload=retryable_payload,
            private_url_payloads=private_urls,
            continuation_bounds=durable.bounds,
            first_input_required_observed_at_utc=(
                durable.expiry.first_input_required_observed_at_utc
            ),
        )
        reconstructed_insert = _RecoveredCommittedContinuationIntent(
            companion_kind=McpContinuationCompanionKind.SUSPENSION_INSERT,
            storage_mutation_plan_fingerprint=context_fingerprint(
                "recovered-full-mcp-suspension-companion:v1",
                {
                    "source_suspension_event_reference": (
                        source_suspension_event_reference.model_dump(mode="json")
                    ),
                    "continuation_fact_fingerprint": (
                        durable.continuation_fact_fingerprint
                    ),
                },
            ),
            charge_contract_fingerprint=(
                mcp_continuation_charge_contract_fingerprint(durable.bounds)
            ),
        )
        awaiting = PreparedMcpAwaitingContinuation(
            durable_fact=durable,
            stored_record=(
                record
                if expected_state is McpContinuationCarrierState.AWAITING_CLIENT_INPUT
                else McpContinuationStoredRecord(
                    runtime_session_id=owner.runtime_session_id,
                    interaction_id=interaction.interaction_id,
                    source_event_id=source_suspension_event_reference.event_id,
                    round_ordinal=interaction.round_count,
                    envelope=record.envelope,
                    control=record.control,
                )
            ),
            plaintext=plaintext,  # type: ignore[arg-type]
            retryable_base_params_commitment=durable.retryable_base_params_commitment,
            request_state_commitment=durable.request_state_commitment,
        )
        if expected_state is McpContinuationCarrierState.AWAITING_CLIENT_INPUT:
            batch_owner = build_mcp_elicitation_batch_owner(
                runtime_session_id=owner.runtime_session_id,
                interaction_id=interaction.interaction_id,
                round_ordinal=interaction.round_count,
                request_set_fingerprint=durable.request_set_fingerprint,
                requests=input_requests,
                private_url_payloads=private_urls,
                response_factory=codec.response_factory(bounds=durable.bounds),
                browser_port=self._external_browser_port,
            )
        else:
            batch_owner = build_recovered_resolved_mcp_elicitation_batch_owner(
                runtime_session_id=owner.runtime_session_id,
                interaction_id=interaction.interaction_id,
                round_ordinal=interaction.round_count,
                request_set_fingerprint=durable.request_set_fingerprint,
                requests=input_requests,
            )
        prepared_payload = {
            "source_suspension_event_reference": (
                source_suspension_event_reference.model_dump(mode="json")
            ),
            "source_suspension_fact_fingerprint": (
                source_suspension.suspension_fact_fingerprint
            ),
            "stored_envelope_fingerprint": record.envelope.stored_envelope_fingerprint,
            "control_fingerprint": record.control.control_fingerprint,
            "batch_owner_id": batch_owner.identity.owner_id,
            "sdk_client_generation_id": sdk_client_generation_id,
        }
        prepared = _PreparedMcpSecureSuspension(
            suspension_event_id=source_suspension_event_reference.event_id,
            interaction=interaction,
            binding_identity=source_suspension.binding_identity,
            pending_lease_reservation=source_suspension.pending_lease_reservation,
            request_envelope=source_suspension.request_envelope,
            continuation=awaiting,
            transaction_companion=reconstructed_insert,
            elicitation_batch_owner=batch_owner,
            client_input_result=recovered_result,
            deadline_monotonic=leg.operation_deadline_monotonic,
            tool_observation_timing_seed=None,
            recovery_rebind_receipt=recovery_rebind_receipt,
            prepared_suspension_fingerprint=context_fingerprint(
                "recovered-mcp-input-required-suspension:v1", prepared_payload
            ),
        )
        reservation = source_suspension.pending_lease_reservation
        self._supervisor.recover_confirmed_pending_lease(
            interaction_id=interaction.interaction_id,
            reservation_id=reservation.reservation_id,
            binding_identity=runtime_binding,
        )
        handle = self._build_handle(owner=owner, prepared=prepared, predecessor=None)
        handle._state = McpPendingHandleState.PENDING_CONFIRMED
        with self._lock:
            if self.handle_for_interaction(interaction.interaction_id) is not None:
                self._supervisor.abort_pending_lease(
                    interaction.interaction_id, reservation.reservation_id
                )
                batch_owner.retire()
                raise RuntimeError("MCP recovery found a resident interaction owner")
            self._handles[handle.identity.handle_id] = handle

        prepared_resolution = None
        if expected_state is McpContinuationCarrierState.REPLAY_READY:
            replay = PreparedMcpReplayContinuation(
                stored_record=record,
                plaintext=plaintext,  # type: ignore[arg-type]
                retryable_base_params_commitment=(
                    resolution_carrier.retryable_base_params_commitment
                ),
            )
            resolution_intent = _RecoveredCommittedContinuationIntent(
                companion_kind=(McpContinuationCompanionKind.RESOLUTION_REPLAY_READY),
                storage_mutation_plan_fingerprint=context_fingerprint(
                    "recovered-full-mcp-resolution-companion:v1",
                    {
                        "source_resolution_event_reference": (
                            source_resolution_event_reference.model_dump(mode="json")
                        ),
                        "resolution_carrier_fact_fingerprint": (
                            resolution_carrier.resolution_carrier_fact_fingerprint
                        ),
                        "stored_envelope_fingerprint": (
                            record.envelope.stored_envelope_fingerprint
                        ),
                        "control_fingerprint": record.control.control_fingerprint,
                    },
                ),
                charge_contract_fingerprint=(
                    mcp_continuation_charge_contract_fingerprint(durable.bounds)
                ),
            )
            resolution_payload = {
                "source_suspension_event_reference": (
                    source_suspension_event_reference.model_dump(mode="json")
                ),
                "source_suspension_fact_fingerprint": (
                    source_suspension.suspension_fact_fingerprint
                ),
                "interaction_id": interaction.interaction_id,
                "resolution_semantic_fingerprint": (
                    resolution_semantic.resolution_semantic_fingerprint
                ),
                "resolution_carrier_fact_fingerprint": (
                    resolution_carrier.resolution_carrier_fact_fingerprint
                ),
                "storage_mutation_plan_fingerprint": (
                    resolution_intent.storage_mutation_plan_fingerprint
                ),
                "batch_owner_id": batch_owner.identity.owner_id,
            }
            prepared_resolution = PreparedMcpInputRequiredResolution(
                source_suspension_event_reference=source_suspension_event_reference,
                source_suspension_fact_fingerprint=(
                    source_suspension.suspension_fact_fingerprint
                ),
                interaction_id=interaction.interaction_id,
                resolution_semantic=resolution_semantic,
                resolution_carrier=resolution_carrier,
                transaction_companion=resolution_intent,
                sealed_responses=sealed_responses,
                batch_owner_id=batch_owner.identity.owner_id,
                prepared_resolution_fingerprint=context_fingerprint(
                    "prepared-mcp-input-required-resolution:v2",
                    resolution_payload,
                ),
            )
            handle._prepared_resolution = prepared_resolution
            handle._prepared_replay = replay
            handle._state = McpPendingHandleState.REPLAY_READY
            batch_owner.retire()
        return RecoveredMcpContinuationOwner(
            pending_handle=handle,
            prepared_resolution=prepared_resolution,
            recovery_state=expected_state.value,
        )

    def discard_recovered_continuation(
        self,
        recovered: RecoveredMcpContinuationOwner,
    ) -> None:
        """Release a recovered physical owner before Host handoff completes.

        This operation is deliberately narrower than terminal settlement. It
        is legal only while the recovered handle has not acquired a stable
        event candidate. Durable carrier deletion remains the responsibility
        of the fail-closed reopen terminalizer.
        """

        pending_handle = recovered.pending_handle
        with self._lock:
            resident = self._handles.get(pending_handle.identity.handle_id)
            if resident is not pending_handle:
                return
            if resident._candidate_owner is not None:
                raise RuntimeError(
                    "recovered MCP continuation already owns a stable candidate"
                )
            if resident.state not in {
                McpPendingHandleState.PENDING_CONFIRMED,
                McpPendingHandleState.REPLAY_READY,
            }:
                raise RuntimeError(
                    "recovered MCP continuation cannot be discarded in its current state"
                )
            self._handles.pop(pending_handle.identity.handle_id, None)
        reservation = pending_handle.identity.pending_lease_reservation
        self._supervisor.abort_pending_lease(
            pending_handle.identity.interaction_id,
            reservation.reservation_id,
        )
        pending_handle.elicitation_batch_owner.retire()
        resident._state = McpPendingHandleState.ABORTED

    @staticmethod
    def _source_suspension_event_id(
        source: ContextEventReferenceFact,
    ) -> str:
        if source.event_type != "TOOL_EXECUTION_SUSPENDED":
            raise ValueError("MCP recovery source is not a suspension event")
        return source.event_id

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

    def _require_continuation_codec(self) -> McpContinuationSecretCodec:
        codec = self._continuation_codec
        if codec is None:
            raise RuntimeError(
                "MCP client input was returned without a secure continuation codec"
            )
        return codec

    def _require_continuation_repository(self) -> McpContinuationRepository:
        repository = self._continuation_repository
        if repository is None:
            raise RuntimeError(
                "MCP client input was returned without a continuation repository"
            )
        return repository

    def _end_operation(self) -> None:
        with self._lock:
            if self._active_operations < 1:
                raise RuntimeError("MCP execution operation accounting underflow")
            self._active_operations -= 1

    def _new_handle(
        self,
        *,
        owner: McpInvocationOwner,
        prepared: _PreparedMcpSecureSuspension,
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
        prepared: _PreparedMcpSecureSuspension,
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
            state=McpPendingHandleState.PREPARED_SUSPENSION,
            port_instance_id=self._instance_id,
        )
        return handle

    def _freeze_successor_result(
        self,
        *,
        predecessor: _RuntimeMcpPendingExecutionHandle,
        request: McpToolResumeRequest,
        result: McpClientInputRequired,
    ) -> McpToolSuspendedOutcome:
        if result.interaction_id != predecessor.identity.interaction_id:
            raise ValueError("MCP successor changed the pending interaction identity")
        if result.server_id != request.binding.binding_identity.server_id:
            raise ValueError("MCP successor changed the binding server identity")
        dispatch_record = predecessor._dispatch_record
        prepared_dispatch = predecessor._prepared_dispatch
        if dispatch_record is None or prepared_dispatch is None:
            raise RuntimeError("MCP successor lost its dispatch carrier owner")
        successor_prepared = _prepare_suspension(
            codec=self._require_continuation_codec(),
            repository=self._require_continuation_repository(),
            issuer_id=self._instance_id,
            result=result,
            owner=request.owner,
            exposed_tool_name=request.binding.tool_name,
            binding=request.binding.binding_identity,
            binding_contract_fingerprint=(request.binding.contract_fact_fingerprint),
            reservation_id=(
                predecessor.identity.pending_lease_reservation.reservation_id
            ),
            predecessor_resolution_reference=(
                prepared_dispatch.dispatch_reservation.source_resolution_event_reference
            ),
            inherited_expiry=(predecessor._prepared.continuation.durable_fact.expiry),
            source_dispatch_record=dispatch_record,
            browser_port=self._external_browser_port,
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
        if (
            predecessor.elicitation_batch_owner.state
            is not McpElicitationBatchState.RETIRED
        ):
            predecessor.elicitation_batch_owner.retire()
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

    def _restore_dispatch_retry_state(
        self,
        handle: _RuntimeMcpPendingExecutionHandle,
    ) -> None:
        with self._lock:
            if handle._state is McpPendingHandleState.RESUME_IN_FLIGHT:
                handle._state = McpPendingHandleState.DISPATCH_RESERVED
                return
            if handle._state is not McpPendingHandleState.DISPATCH_RESERVED:
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
    codec: McpContinuationSecretCodec,
    repository: McpContinuationRepository,
    issuer_id: str,
    result: McpClientInputRequired,
    owner: McpInvocationOwner,
    exposed_tool_name: str,
    binding: McpBindingIdentityFact,
    binding_contract_fingerprint: str,
    reservation_id: str,
    predecessor_resolution_reference: ContextEventReferenceFact | None,
    inherited_expiry: McpContinuationExpiryFact | None,
    source_dispatch_record: McpContinuationStoredRecord | None,
    browser_port: McpExternalBrowserPort | None,
) -> _PreparedMcpSecureSuspension:
    if (source_dispatch_record is None) != (predecessor_resolution_reference is None):
        raise ValueError("MCP successor source/predecessor matrix mismatch")
    if result.server_id != binding.server_id:
        raise ValueError("MCP client-input result changed server identity")
    interaction = build_mcp_interaction_semantic(
        interaction_id=result.interaction_id,
        tool_call_id=owner.tool_call_id,
        tool_name=exposed_tool_name,
        server_id=binding.server_id,
        round_count=result.leg.leg_ordinal,
    )
    pending_reservation = build_frozen_fact(
        McpPendingLeaseReservationIdentityFact,
        schema_version="mcp_pending_lease_reservation_identity.v1",
        reservation_id=reservation_id,
        interaction_id=result.interaction_id,
        binding_identity=binding,
    )
    user_requests = tuple(
        build_mcp_user_visible_request(request=request)
        for request in result.leg.input_requests
    )
    request_envelope = build_frozen_fact(
        McpInputRequiredRequestEnvelopeFact,
        schema_version="mcp_input_required_request_envelope.v2",
        protocol_revision=result.exact_protocol_revision,
        ordered_user_visible_input_requests=user_requests,
        request_set_fingerprint=result.leg.request_set_fingerprint,
    )
    suspension_event_id = "mcp_suspension:" + stable_runtime_event_id(
        "mcp-input-required-suspension-event:v2",
        owner.runtime_session_id,
        owner.run_id,
        owner.tool_call_id,
        result.interaction_id,
        result.leg.leg_ordinal,
        reservation_id,
        result.leg.leg_fingerprint,
        binding_contract_fingerprint,
    )
    bounds = result.continuation_bounds
    prepared_continuation = prepare_mcp_awaiting_continuation(
        codec=codec,
        runtime_session_id=owner.runtime_session_id,
        interaction_id=result.interaction_id,
        suspension_event_id=suspension_event_id,
        round_ordinal=result.leg.leg_ordinal,
        retryable_request_payload=result.retryable_request_payload,
        request_state=result.leg.request_state,
        request_set_fingerprint=result.leg.request_set_fingerprint,
        private_url_requests=result.private_url_payloads,
        protocol_semantic_fingerprint=result.protocol_semantic_fingerprint,
        endpoint_attribution_fingerprint=result.endpoint_attribution_fingerprint,
        auth_attribution_fingerprint=result.auth_attribution_fingerprint,
        binding_contract_fingerprint=binding_contract_fingerprint,
        first_input_required_observed_at_utc=(
            result.first_input_required_observed_at_utc
        ),
        created_at_utc=_utc_now(),
        bounds=bounds,
        inherited_expiry=inherited_expiry,
        predecessor_control_revision=(
            source_dispatch_record.control.control_revision
            if source_dispatch_record is not None
            else None
        ),
    )
    expiry_remaining = (
        _parse_utc_datetime(
            prepared_continuation.durable_fact.expiry.operation_expires_at_utc
        )
        - datetime.now(timezone.utc)
    ).total_seconds()
    if expiry_remaining <= 0:
        raise RuntimeError("MCP continuation expired during suspension preparation")
    deadline_monotonic = min(
        result.leg.operation_deadline_monotonic,
        monotonic() + expiry_remaining,
    )
    transaction_companion = build_mcp_continuation_transaction_intent(
        companion_kind=(
            McpContinuationCompanionKind.SUSPENSION_INSERT
            if source_dispatch_record is None
            else McpContinuationCompanionKind.SUCCESSOR_REPLACE
        ),
        mutation_kind=(
            McpContinuationMutationKind.INSERT_AWAITING
            if source_dispatch_record is None
            else McpContinuationMutationKind.REPLACE_WITH_SUCCESSOR
        ),
        runtime_session_id=owner.runtime_session_id,
        interaction_id=result.interaction_id,
        round_ordinal=result.leg.leg_ordinal,
        source_event_id=suspension_event_id,
        repository=repository,
        issuer_id=issuer_id,
        issuer_generation=result.leg.leg_ordinal,
        charge_contract_fingerprint=(
            mcp_continuation_charge_contract_fingerprint(bounds)
        ),
        source_carrier_id=(
            source_dispatch_record.envelope.continuation_carrier_id
            if source_dispatch_record is not None
            else None
        ),
        resulting_record=prepared_continuation.stored_record,
        expected_control=(
            source_dispatch_record.control
            if source_dispatch_record is not None
            else None
        ),
    )
    batch_owner = build_mcp_elicitation_batch_owner(
        runtime_session_id=owner.runtime_session_id,
        interaction_id=result.interaction_id,
        round_ordinal=result.leg.leg_ordinal,
        request_set_fingerprint=result.leg.request_set_fingerprint,
        requests=result.leg.input_requests,
        private_url_payloads=result.private_url_payloads,
        response_factory=codec.response_factory(bounds=bounds),
        browser_port=browser_port,
    )
    safe_payload = {
        "suspension_event_id": suspension_event_id,
        "interaction_semantic_fingerprint": (
            interaction.interaction_semantic_fingerprint
        ),
        "binding_identity": binding.model_dump(mode="json"),
        "pending_reservation_fingerprint": pending_reservation.reservation_fingerprint,
        "request_envelope_semantic_fingerprint": (
            request_envelope.request_envelope_semantic_fingerprint
        ),
        "continuation_fact_fingerprint": (
            prepared_continuation.durable_fact.continuation_fact_fingerprint
        ),
        "storage_mutation_plan_fingerprint": (
            transaction_companion.storage_mutation_plan_fingerprint
        ),
        "elicitation_batch_owner_id": batch_owner.identity.owner_id,
        "deadline_monotonic": deadline_monotonic,
        "predecessor_resolution_event_id": (
            predecessor_resolution_reference.event_id
            if predecessor_resolution_reference is not None
            else None
        ),
    }
    return _PreparedMcpSecureSuspension(
        suspension_event_id=suspension_event_id,
        interaction=interaction,
        binding_identity=binding,
        pending_lease_reservation=pending_reservation,
        request_envelope=request_envelope,
        continuation=prepared_continuation,
        transaction_companion=transaction_companion,
        elicitation_batch_owner=batch_owner,
        client_input_result=result,
        deadline_monotonic=deadline_monotonic,
        tool_observation_timing_seed=None,
        recovery_rebind_receipt=None,
        prepared_suspension_fingerprint=context_fingerprint(
            "prepared-mcp-input-required-suspension:v2",
            safe_payload,
        ),
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


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("MCP continuation timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["RuntimeMcpToolExecutionPort"]
