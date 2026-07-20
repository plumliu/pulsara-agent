"""Agent runtime events for Pulsara."""

from __future__ import annotations

import math
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pulsara_agent.event.candidates import (
    InvalidAttemptPayload,
    MemoryCandidate,
    ValidCandidatePayload,
)
from pulsara_agent.message.blocks import (
    ToolCallBlock,
    ToolResultArtifactRef,
    ToolResultState,
)
from pulsara_agent.ontology import memory
from pulsara_agent.primitives.model_call import (
    CompactionObservedAfterMeasurementFact,
    CompactionTargetEstimateFact,
    ContextBudgetReportEvent,
    ModelCallDiagnosticFact,
    ModelCallPurpose,
    ModelTokenUsageFact,
    ModelCallControlDisposition,
    ModelStreamDurableSemanticKind,
    ModelStreamSemanticAttributionFact,
    ProviderSanitizedErrorFact,
    RunTerminationIntentAttributionFact,
    ResolvedModelCallFact,
    ResolvedModelTargetFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.transcript_projection import (
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
    TranscriptProjectionScopeFact,
)
from pulsara_agent.primitives.authority_materialization import (
    CheckpointConsumerCauseFact,
    CheckpointDispatchBarrierFact,
    ConsumerRetirementCauseFact,
    LedgerMaterializationAccountGenesisFact,
    LedgerMaterializationAccountStateFact,
    LedgerMaterializationAccountTransitionFact,
    LedgerMaterializationConsumerHorizonFact,
    LedgerMaterializationConsumerRegistrationCauseFact,
    LedgerMaterializationGenerationFact,
    PhysicalOperationChargeAppliedFact,
    PhysicalOperationReservationFact,
    PhysicalOperationSettlementFact,
    PhysicalOperationSuspensionTailFact,
)
from pulsara_agent.primitives.transcript_checkpoint import (
    CheckpointCancellationReasonCode,
    CheckpointCancellationSource,
    CheckpointDispatchBarrierReleaseFact,
    CheckpointFailureReasonCode,
    CheckpointTerminalContractFact,
    CheckpointTerminalDiagnosticFact,
    TranscriptProjectionCheckpointCandidateFact,
)
from pulsara_agent.primitives.long_horizon import (
    ChildRolloutReservationPolicyFact,
    ChildRolloutSettlementAggregateFact,
    ChildRolloutSubaccountFact,
    ChildRolloutUsageHandoffFact,
    ContextWindowCloseReason,
    ContextWindowCompactionPlanFact,
    ContextWindowFact,
    LongHorizonContextBudgetDecisionFact,
    LongHorizonProjectionPressureShadowFact,
    ObservationRollupFact,
    ProjectionRewriteReason,
    RolloutBudgetAccountFact,
    RolloutPhase,
    RolloutReservationFact,
    RolloutTransitionReason,
    RolloutUsageChargeFact,
    ResolvedChildRolloutBudgetFact,
    RunLongHorizonContractFact,
    SubagentGraphReducerContractFact,
    SubagentGraphCheckpointArtifactFact,
    SubagentGraphCheckpointStateFact,
    ToolObservationProjectionRewriteEntryFact,
    ToolActionClassificationFact,
)
from pulsara_agent.primitives.mcp import (
    MAX_MCP_DIAGNOSTICS_PER_FACT,
    McpDiagnosticFact,
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpReconcileTriggerValue,
)
from pulsara_agent.primitives.permission import (
    parse_permission_mode,
    preset_permission_payload,
)
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.context import (
    ContextCompileInputAuditFact,
    ContextCompileFailureStage,
    ContextCompileInputFailureFact,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.run_boundary import (
    InteractionResumeBoundaryFact,
    ModelStreamRecoveryPlanFact,
    NewRunBoundaryFact,
    RunExecutionActivationFact,
)
from pulsara_agent.primitives.run_entry import (
    CurrentUserMessageFact,
    RunEntryKind,
    SubagentRunEntryFact,
    canonical_utc_timestamp,
    validate_host_current_user_attribution,
    validate_subagent_current_user_attribution,
)
from pulsara_agent.primitives.tool_result import (
    ExternalToolCallRequirementFact,
    ExternalToolResultIngressFact,
    TerminalPayloadTimingFact,
    ToolResultEssentialCapturePolicyFact,
    ToolResultEssentialFact,
    ToolResultExecutionSemanticsFact,
    ToolResultRenderProfileFact,
    ToolResultRenderDecisionFact,
    ToolResultRenderOperationalFact,
    ToolResultRollupSemanticsFact,
    ToolResultStateFact,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.terminal_projection import (
    ModelCallTerminalProjectionEndReferenceFact,
    TerminalProjectionReferenceFact,
    ToolResultTerminalProjectionEndReferenceFact,
)
from pulsara_agent.primitives.frozen import StableEventIdentityFact
from pulsara_agent.primitives.governance_evidence import (
    CompactionCandidateAttributionFact,
    CompactionMemoryCandidateExtractorContractFact,
    GovernanceBatchInputReferenceFact,
    GovernanceModelInputAttributionFact,
    MemoryCandidateEvidenceRejectedRecord,
    ReflectionCandidateAttributionFact,
)
from pulsara_agent.primitives.provider_input import (
    CommittedProviderInputGenerationCoreStateFact,
    CommittedProviderInputReferenceFact,
    ContextInputManifestProjectionReferenceFact,
    PreparedProviderInputAppendCandidateFact,
    ProviderInputAppendBatchReferenceFact,
    ProviderInputCausalValidationResult,
    ProviderInputContinuationConsumptionProofFact,
    ProviderInputContinuationMaterializationProofFact,
    ProviderInputContinuationRewriteCoverageProofFact,
    ProviderInputGenerationFact,
    ProviderInputGenerationRootReferenceFact,
    ProviderInvocationContextFramePlacementFact,
    ProviderInputRolloverRequestFact,
    ProviderTranscriptDeltaCommitProofFact,
    ProviderInputUnitVectorRootReferenceFact,
)
from pulsara_agent.primitives.runtime_observation import (
    PreparedRuntimeObservationProviderUnitFact,
    RuntimeObservationProjectionRewriteFact,
)
from pulsara_agent.primitives.context_source import (
    ContextSourceDispositionFact,
    LedgerAuthorityHorizonFact,
    LedgerAuthorityHorizonSetReferenceFact,
)
from pulsara_agent.primitives.run_lifecycle import (
    FAILURE_STOP_REASONS,
    RunStopReason,
    RunTerminalizationKind,
)
from pulsara_agent.primitives.subagent import (
    ChildNativeTerminalReferenceFact,
    ChildResultHandoffFact,
)

_RUN_PERMISSION_SNAPSHOT_SOURCES = frozenset(
    {"session_default", "plan_mode", "child_profile"}
)


def _validate_model_usage(
    usage_status: Literal["reported", "missing"],
    usage: ModelTokenUsageFact | None,
) -> None:
    if usage_status == "reported" and usage is None:
        raise ValueError("reported model usage requires a usage fact")
    if usage_status == "missing" and usage is not None:
        raise ValueError("missing model usage cannot contain a usage fact")


def _validate_reported_model_id(value: str | None) -> None:
    if value is not None and (not value or value != value.strip()):
        raise ValueError("reported model id must be a non-empty trimmed string")


def _validate_preset_permission_payload(
    *,
    mode: str,
    policy: dict[str, Any],
    context: str,
) -> None:
    try:
        parsed = parse_permission_mode(mode)
    except ValueError as exc:
        raise ValueError(f"{context} permission mode is invalid") from exc
    expected = preset_permission_payload(parsed)
    if dict(policy) != expected:
        raise ValueError(f"{context} permission policy must match preset mode {mode!r}")


class EventType(StrEnum):
    RUN_START = "RUN_START"
    RUN_END = "RUN_END"
    REPLY_START = "REPLY_START"
    REPLY_END = "REPLY_END"
    RUN_ERROR = "RUN_ERROR"

    MODEL_CALL_START = "MODEL_CALL_START"
    MODEL_CALL_END = "MODEL_CALL_END"
    MODEL_CALL_TERMINAL_PROJECTION_COMMITTED = (
        "MODEL_CALL_TERMINAL_PROJECTION_COMMITTED"
    )
    MODEL_CALL_REJECTED = "MODEL_CALL_REJECTED"
    PROVIDER_MODEL_STREAM_ERROR = "PROVIDER_MODEL_STREAM_ERROR"
    MODEL_CALL_CONTROL_DISPOSITION_RESOLVED = "MODEL_CALL_CONTROL_DISPOSITION_RESOLVED"
    PROVIDER_INPUT_GENERATION_STARTED = "PROVIDER_INPUT_GENERATION_STARTED"
    PROVIDER_INPUT_APPEND_COMMITTED = "PROVIDER_INPUT_APPEND_COMMITTED"
    PROVIDER_INPUT_EXISTING_PREPARATION_ABANDONED = (
        "PROVIDER_INPUT_EXISTING_PREPARATION_ABANDONED"
    )
    PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED = (
        "PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED"
    )
    PROVIDER_INPUT_GENERATION_ROLLOVER_RESOLVED = (
        "PROVIDER_INPUT_GENERATION_ROLLOVER_RESOLVED"
    )
    PROVIDER_INPUT_GENERATION_CLOSED = "PROVIDER_INPUT_GENERATION_CLOSED"
    CONTEXT_COMPILED = "CONTEXT_COMPILED"
    CAPABILITY_GATE_DECISION = "CAPABILITY_GATE_DECISION"
    CAPABILITY_EXPOSURE_RESOLVED = "CAPABILITY_EXPOSURE_RESOLVED"
    RUN_INTERACTION_RESUME_BOUNDARY = "RUN_INTERACTION_RESUME_BOUNDARY"
    MCP_CAPABILITY_SNAPSHOT_INSTALLED = "MCP_CAPABILITY_SNAPSHOT_INSTALLED"

    TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT = "TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT"
    TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED = (
        "TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED"
    )
    TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED = "TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED"
    TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED = (
        "TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED"
    )
    TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED = (
        "TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED"
    )
    LEDGER_MATERIALIZATION_ACCOUNT_GENESIS = "LEDGER_MATERIALIZATION_ACCOUNT_GENESIS"
    LEDGER_MATERIALIZATION_CONSUMER_REGISTERED = (
        "LEDGER_MATERIALIZATION_CONSUMER_REGISTERED"
    )
    LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED = (
        "LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED"
    )
    LEDGER_MATERIALIZATION_CONSUMER_RETIRED = "LEDGER_MATERIALIZATION_CONSUMER_RETIRED"
    LEDGER_MATERIALIZATION_GENERATION_ADVANCED = (
        "LEDGER_MATERIALIZATION_GENERATION_ADVANCED"
    )
    PHYSICAL_OPERATION_RESERVATION_CREATED = "PHYSICAL_OPERATION_RESERVATION_CREATED"
    PHYSICAL_OPERATION_CHARGE_APPLIED = "PHYSICAL_OPERATION_CHARGE_APPLIED"
    PHYSICAL_OPERATION_RESERVATION_SUSPENDED = (
        "PHYSICAL_OPERATION_RESERVATION_SUSPENDED"
    )
    PHYSICAL_OPERATION_RESERVATION_SETTLED = "PHYSICAL_OPERATION_RESERVATION_SETTLED"
    CHECKPOINT_DISPATCH_BARRIER_INSTALLED = "CHECKPOINT_DISPATCH_BARRIER_INSTALLED"
    CHECKPOINT_DISPATCH_BARRIER_RELEASED = "CHECKPOINT_DISPATCH_BARRIER_RELEASED"

    TEXT_BLOCK_START = "TEXT_BLOCK_START"
    TEXT_BLOCK_SEGMENT = "TEXT_BLOCK_SEGMENT"
    TEXT_BLOCK_END = "TEXT_BLOCK_END"

    DATA_BLOCK_START = "DATA_BLOCK_START"
    DATA_BLOCK_SEGMENT = "DATA_BLOCK_SEGMENT"
    DATA_BLOCK_END = "DATA_BLOCK_END"

    THINKING_BLOCK_START = "THINKING_BLOCK_START"
    THINKING_BLOCK_SEGMENT = "THINKING_BLOCK_SEGMENT"
    THINKING_BLOCK_END = "THINKING_BLOCK_END"

    HINT_BLOCK = "HINT_BLOCK"

    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGUMENTS_SEGMENT = "TOOL_CALL_ARGUMENTS_SEGMENT"
    TOOL_CALL_END = "TOOL_CALL_END"

    TOOL_RESULT_START = "TOOL_RESULT_START"
    TOOL_RESULT_TEXT_DELTA = "TOOL_RESULT_TEXT_DELTA"
    TOOL_RESULT_DATA_DELTA = "TOOL_RESULT_DATA_DELTA"
    TOOL_RESULT_END = "TOOL_RESULT_END"
    TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED = (
        "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED"
    )
    TOOL_EXECUTION_SUSPENDED = "TOOL_EXECUTION_SUSPENDED"

    REQUIRE_USER_CONFIRM = "REQUIRE_USER_CONFIRM"
    USER_CONFIRM_RESULT = "USER_CONFIRM_RESULT"
    REQUIRE_EXTERNAL_EXECUTION = "REQUIRE_EXTERNAL_EXECUTION"
    EXTERNAL_EXECUTION_RESULT = "EXTERNAL_EXECUTION_RESULT"
    TERMINAL_PROCESS_COMPLETED = "TERMINAL_PROCESS_COMPLETED"
    PLAN_MODE_ENTERED = "PLAN_MODE_ENTERED"
    PLAN_QUESTION_ASKED = "PLAN_QUESTION_ASKED"
    PLAN_QUESTION_ANSWERED = "PLAN_QUESTION_ANSWERED"
    PLAN_EXIT_REQUESTED = "PLAN_EXIT_REQUESTED"
    PLAN_EXIT_RESOLVED = "PLAN_EXIT_RESOLVED"
    PLAN_MODE_EXITED = "PLAN_MODE_EXITED"

    MEMORY_CANDIDATE_PROPOSED = "MEMORY_CANDIDATE_PROPOSED"
    MEMORY_WRITE_RESULT = "MEMORY_WRITE_RESULT"
    MEMORY_WRITE_FAILED = "MEMORY_WRITE_FAILED"
    MEMORY_REFLECTION_COMPLETED = "MEMORY_REFLECTION_COMPLETED"
    MEMORY_REFLECTION_FAILED = "MEMORY_REFLECTION_FAILED"
    MEMORY_SUPERSEDED = "MEMORY_SUPERSEDED"
    MEMORY_CONTRADICTION_LINKED = "MEMORY_CONTRADICTION_LINKED"
    MEMORY_MARKED_STALE = "MEMORY_MARKED_STALE"
    MEMORY_MAINTENANCE_PROPOSED = "MEMORY_MAINTENANCE_PROPOSED"
    MEMORY_MAINTENANCE_APPLIED = "MEMORY_MAINTENANCE_APPLIED"
    MEMORY_MAINTENANCE_REJECTED = "MEMORY_MAINTENANCE_REJECTED"
    MEMORY_GOVERNANCE_BATCH_PREPARED = "MEMORY_GOVERNANCE_BATCH_PREPARED"
    MEMORY_GOVERNANCE_BATCH_COMPLETED = "MEMORY_GOVERNANCE_BATCH_COMPLETED"
    MEMORY_GOVERNANCE_BATCH_FAILED = "MEMORY_GOVERNANCE_BATCH_FAILED"
    MEMORY_GOVERNANCE_BATCH_BLOCKED = "MEMORY_GOVERNANCE_BATCH_BLOCKED"
    MEMORY_CANDIDATE_EVIDENCE_REJECTED = "MEMORY_CANDIDATE_EVIDENCE_REJECTED"

    PROJECTION_REQUESTED = "PROJECTION_REQUESTED"
    PROJECTION_READY = "PROJECTION_READY"
    PROJECTION_FAILED = "PROJECTION_FAILED"

    CONTEXT_COMPACTION_STARTED = "CONTEXT_COMPACTION_STARTED"
    CONTEXT_COMPACTION_COMPLETED = "CONTEXT_COMPACTION_COMPLETED"
    CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED = (
        "CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED"
    )
    CONTEXT_COMPACTION_FAILED = "CONTEXT_COMPACTION_FAILED"

    SUBAGENT_RUN_STARTED = "SUBAGENT_RUN_STARTED"
    SUBAGENT_MESSAGE_SENT = "SUBAGENT_MESSAGE_SENT"
    SUBAGENT_RUN_SUSPENDED = "SUBAGENT_RUN_SUSPENDED"
    SUBAGENT_RUN_COMPLETED = "SUBAGENT_RUN_COMPLETED"
    SUBAGENT_RUN_FAILED = "SUBAGENT_RUN_FAILED"
    SUBAGENT_RUN_CANCELLED = "SUBAGENT_RUN_CANCELLED"
    SUBAGENT_EDGE_RECORDED = "SUBAGENT_EDGE_RECORDED"
    SUBAGENT_RESULT_DELIVERED = "SUBAGENT_RESULT_DELIVERED"
    SUBAGENT_TASK_CREATED = "SUBAGENT_TASK_CREATED"
    SUBAGENT_TASK_SCHEDULED = "SUBAGENT_TASK_SCHEDULED"
    SUBAGENT_TASK_STARTED = "SUBAGENT_TASK_STARTED"
    SUBAGENT_TASK_BLOCKED = "SUBAGENT_TASK_BLOCKED"
    SUBAGENT_TASK_COMPLETED = "SUBAGENT_TASK_COMPLETED"
    SUBAGENT_TASK_FAILED = "SUBAGENT_TASK_FAILED"
    SUBAGENT_TASK_CANCELLED = "SUBAGENT_TASK_CANCELLED"
    SUBAGENT_PHASE_REPORTED = "SUBAGENT_PHASE_REPORTED"
    SUBAGENT_RESULT_SUBMITTED = "SUBAGENT_RESULT_SUBMITTED"
    SUBAGENT_RESULT_CONSUMED = "SUBAGENT_RESULT_CONSUMED"
    SUBAGENT_GRAPH_CHECKPOINT_COMMITTED = "SUBAGENT_GRAPH_CHECKPOINT_COMMITTED"

    CONTEXT_WINDOW_OPENED = "CONTEXT_WINDOW_OPENED"
    CONTEXT_WINDOW_CLOSED = "CONTEXT_WINDOW_CLOSED"
    CONTEXT_WINDOW_COMPACTION_STARTED = "CONTEXT_WINDOW_COMPACTION_STARTED"
    CONTEXT_WINDOW_COMPACTION_COMPLETED = "CONTEXT_WINDOW_COMPACTION_COMPLETED"
    CONTEXT_WINDOW_COMPACTION_FAILED = "CONTEXT_WINDOW_COMPACTION_FAILED"
    CONTEXT_PROJECTION_REWRITE_PAGE = "CONTEXT_PROJECTION_REWRITE_PAGE"
    ROLLOUT_BUDGET_ACCOUNT_OPENED = "ROLLOUT_BUDGET_ACCOUNT_OPENED"
    ROLLOUT_BUDGET_ACCOUNT_CLOSED = "ROLLOUT_BUDGET_ACCOUNT_CLOSED"
    CHILD_ROLLOUT_SUBACCOUNT_CLOSED = "CHILD_ROLLOUT_SUBACCOUNT_CLOSED"
    ROLLOUT_BUDGET_RESERVATION_CREATED = "ROLLOUT_BUDGET_RESERVATION_CREATED"
    ROLLOUT_BUDGET_RESERVATION_SETTLED = "ROLLOUT_BUDGET_RESERVATION_SETTLED"
    ROLLOUT_PHASE_TRANSITIONED = "ROLLOUT_PHASE_TRANSITIONED"
    SUBAGENT_ROLLOUT_BUDGET_RESOLVED = "SUBAGENT_ROLLOUT_BUDGET_RESOLVED"

    CUSTOM = "CUSTOM"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EventContext:
    run_id: str
    turn_id: str
    reply_id: str

    def event_fields(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "reply_id": self.reply_id,
        }


class EventBase(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=utc_now)
    run_id: str
    turn_id: str
    reply_id: str
    sequence: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunStartEvent(EventBase):
    type: Literal[EventType.RUN_START] = EventType.RUN_START
    user_input_chars: int
    permission_snapshot_id: str
    permission_mode: Literal[
        "read-only", "ask-permissions", "accept-edits", "bypass-permissions"
    ]
    permission_policy: dict[str, Any]
    permission_snapshot_source: Literal["session_default", "plan_mode", "child_profile"]
    model_target: ResolvedModelTargetFact
    subagent_graph_reducer_contract: SubagentGraphReducerContractFact
    long_horizon: RunLongHorizonContractFact
    child_rollout_subaccount: ChildRolloutSubaccountFact | None
    mcp_installation_id: str
    mcp_installation_owner_runtime_session_id: str
    run_entry_kind: RunEntryKind
    current_user_message: CurrentUserMessageFact
    run_transcript_seed_semantic: RunTranscriptSeedSemanticFact
    run_transcript_seed_reference: RunTranscriptSeedReferenceFact
    terminal_run_end_event_id: str = Field(min_length=1)
    new_run_boundary: NewRunBoundaryFact | None
    subagent_run_entry: SubagentRunEntryFact | None

    @field_validator("created_at")
    @classmethod
    def _canonical_created_at(cls, value: str) -> str:
        return canonical_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_permission_snapshot(self) -> "RunStartEvent":
        if self.permission_snapshot_source not in _RUN_PERMISSION_SNAPSHOT_SOURCES:
            raise ValueError("RunStartEvent permission_snapshot_source is invalid")
        _validate_preset_permission_payload(
            mode=self.permission_mode,
            policy=self.permission_policy,
            context="RunStartEvent",
        )
        if self.long_horizon.subagent_graph_reducer_contract != (
            self.subagent_graph_reducer_contract
        ):
            raise ValueError("RunStart long-horizon graph reducer contract mismatch")
        if (
            self.long_horizon.rollout_account_owner_runtime_session_id
            != self.mcp_installation_owner_runtime_session_id
            and self.run_entry_kind is RunEntryKind.HOST
        ):
            raise ValueError("host RunStart long-horizon account owner mismatch")
        if self.user_input_chars != len(self.current_user_message.text):
            raise ValueError("RunStartEvent user_input_chars mismatch")
        if self.run_transcript_seed_reference.seed_semantic_fingerprint != (
            self.run_transcript_seed_semantic.seed_semantic_fingerprint
        ):
            raise ValueError("RunStartEvent transcript seed identity mismatch")
        if (
            self.run_entry_kind is RunEntryKind.HOST
            and self.run_transcript_seed_reference.source_runtime_session_id
            != self.mcp_installation_owner_runtime_session_id
        ):
            raise ValueError("RunStartEvent transcript seed ledger mismatch")
        if self.sequence is not None and (
            self.run_transcript_seed_reference.source_ledger_through_sequence
            >= self.sequence
        ):
            raise ValueError("RunStartEvent transcript seed must predate RunStart")
        created_at = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        observed_at = datetime.fromisoformat(
            self.current_user_message.observed_at_utc.replace("Z", "+00:00")
        )
        if created_at < observed_at:
            raise ValueError("RunStartEvent cannot predate current user observation")
        if self.run_entry_kind is RunEntryKind.HOST:
            if self.child_rollout_subaccount is not None:
                raise ValueError("host RunStart cannot carry child rollout subaccount")
            if self.new_run_boundary is None or self.subagent_run_entry is not None:
                raise ValueError("host RunStart requires only new_run_boundary")
            boundary = self.new_run_boundary
            identity = boundary.identity
            if (
                identity.run_id != self.run_id
                or identity.turn_id != self.turn_id
                or identity.reply_id != self.reply_id
                or identity.runtime_session_id
                != self.mcp_installation_owner_runtime_session_id
            ):
                raise ValueError("host RunStart boundary identity mismatch")
            validate_host_current_user_attribution(
                boundary=identity,
                current_user=self.current_user_message,
            )
            if (
                boundary.model_target_fingerprint
                != self.model_target.target_fingerprint
                or boundary.permission_snapshot_id != self.permission_snapshot_id
                or boundary.mcp_installation_id != self.mcp_installation_id
            ):
                raise ValueError("host RunStart contract identity mismatch")
        elif self.subagent_run_entry is None or self.new_run_boundary is not None:
            raise ValueError("child RunStart requires only subagent_run_entry")
        else:
            entry = self.subagent_run_entry
            inherited = self.long_horizon.inherited_rollout_reservation
            if self.child_rollout_subaccount is None or inherited is None:
                raise ValueError("child RunStart requires child rollout subaccount")
            if (
                self.child_rollout_subaccount.child_run_id != self.run_id
                or self.child_rollout_subaccount.root_account_id
                != self.long_horizon.rollout_account_id
                or self.child_rollout_subaccount.parent_reservation != inherited
            ):
                raise ValueError("child RunStart rollout subaccount mismatch")
            validate_subagent_current_user_attribution(
                entry=entry,
                current_user=self.current_user_message,
            )
            if (
                entry.permission_snapshot_id != self.permission_snapshot_id
                or entry.model_target_fingerprint
                != self.model_target.target_fingerprint
                or entry.mcp_installation_id != self.mcp_installation_id
                or entry.mcp_installation_owner_runtime_session_id
                != self.mcp_installation_owner_runtime_session_id
            ):
                raise ValueError("child RunStart contract identity mismatch")
        return self


class McpCapabilitySnapshotInstalledEvent(EventBase):
    type: Literal[EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED] = (
        EventType.MCP_CAPABILITY_SNAPSHOT_INSTALLED
    )
    installation_id: str
    previous_installation_id: str | None = None
    config_epoch: int
    event_safe_config_set_fingerprint: str
    installation_triggers: tuple[McpReconcileTriggerValue, ...]
    coalesced_installation_count: int = 0
    coalesced_attempt_summaries: tuple[McpReconcileAttemptSummaryFact, ...] = ()
    coalesced_attempt_summaries_omitted: int = 0
    server_snapshots: tuple[McpInstalledServerSnapshotFact, ...]
    total_installed_tool_count: int
    added_tool_count: int
    revoked_tool_count: int
    changed_tool_names_bounded: tuple[str, ...] = ()
    changed_tool_names_omitted: int = 0
    diagnostics: tuple[McpDiagnosticFact, ...] = ()

    @model_validator(mode="after")
    def _installation_contract(self) -> "McpCapabilitySnapshotInstalledEvent":
        if self.installation_id == self.previous_installation_id:
            raise ValueError("MCP installation cannot point to itself as previous")
        counts = (
            self.config_epoch,
            self.coalesced_installation_count,
            self.coalesced_attempt_summaries_omitted,
            self.total_installed_tool_count,
            self.added_tool_count,
            self.revoked_tool_count,
            self.changed_tool_names_omitted,
        )
        if any(value < 0 for value in counts):
            raise ValueError("MCP installation counts must be non-negative")
        if len(self.changed_tool_names_bounded) > 64:
            raise ValueError("MCP changed tool names exceed bounded cap")
        if len(self.coalesced_attempt_summaries) > 64:
            raise ValueError("MCP coalesced attempt summaries exceed bounded cap")
        if len(self.server_snapshots) > 64:
            raise ValueError("MCP installed server snapshots exceed bounded cap")
        if len(self.diagnostics) > MAX_MCP_DIAGNOSTICS_PER_FACT:
            raise ValueError("MCP installation diagnostics exceed bounded cap")
        triggers = {
            snapshot.attempt.reconcile_trigger
            for snapshot in self.server_snapshots
            if snapshot.changed_in_this_installation
        }
        triggers.update(
            summary.reconcile_trigger for summary in self.coalesced_attempt_summaries
        )
        if not triggers or tuple(sorted(triggers)) != tuple(self.installation_triggers):
            raise ValueError(
                "MCP installation triggers must match changed attempt facts"
            )
        return self


class RunInteractionResumeBoundaryEvent(EventBase):
    type: Literal[EventType.RUN_INTERACTION_RESUME_BOUNDARY] = (
        EventType.RUN_INTERACTION_RESUME_BOUNDARY
    )
    boundary: InteractionResumeBoundaryFact

    @model_validator(mode="after")
    def _validate_context(self) -> "RunInteractionResumeBoundaryEvent":
        identity = self.boundary.identity
        if (
            identity.run_id != self.run_id
            or identity.turn_id != self.turn_id
            or identity.reply_id != self.reply_id
        ):
            raise ValueError("resume boundary event context mismatch")
        return self


class CapabilityExposureResolvedEvent(EventBase):
    type: Literal[EventType.CAPABILITY_EXPOSURE_RESOLVED] = (
        EventType.CAPABILITY_EXPOSURE_RESOLVED
    )
    exposure: CapabilityExposureSnapshotFact
    exposure_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_context(self) -> "CapabilityExposureResolvedEvent":
        owner = self.exposure.owner
        if owner.run_id != self.run_id:
            raise ValueError("capability exposure event run mismatch")
        if self.exposure.resolution_kind == "initial":
            if self.exposure_revision != 1:
                raise ValueError("initial capability exposure revision must be 1")
        elif self.exposure_revision < 2:
            raise ValueError("continuation capability exposure revision must be >= 2")
        return self


class RunEndEvent(EventBase):
    type: Literal[EventType.RUN_END] = EventType.RUN_END
    status: Literal["finished", "failed", "aborted"]
    stop_reason: RunStopReason
    terminalization_kind: RunTerminalizationKind
    abort_kind: Literal["user_stop", "host_teardown"] | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_matrix(self) -> "RunEndEvent":
        kind = self.terminalization_kind
        if kind is RunTerminalizationKind.NORMAL:
            valid = (
                self.status == "finished"
                and self.stop_reason is RunStopReason.FINAL
                and self.abort_kind is None
                and self.error_message is None
            )
        elif kind is RunTerminalizationKind.USER_STOP:
            valid = (
                self.status == "aborted"
                and self.stop_reason is RunStopReason.ABORTED
                and self.abort_kind == "user_stop"
                and self.error_message is None
            )
        elif kind in {
            RunTerminalizationKind.HOST_TEARDOWN,
            RunTerminalizationKind.RECOVERED_INTERRUPTED,
        }:
            valid = (
                self.status == "aborted"
                and self.stop_reason is RunStopReason.ABORTED
                and self.abort_kind == "host_teardown"
                and self.error_message is None
            )
        else:
            valid = (
                self.status == "failed"
                and self.stop_reason in FAILURE_STOP_REASONS
                and self.abort_kind is None
                and isinstance(self.error_message, str)
                and bool(self.error_message.strip())
                and len(self.error_message) <= 4096
            )
        if not valid:
            raise ValueError("RunEndEvent violates terminalization matrix")
        return self


class ContextWindowOpenedEvent(EventBase):
    type: Literal[EventType.CONTEXT_WINDOW_OPENED] = EventType.CONTEXT_WINDOW_OPENED
    window: ContextWindowFact
    opening_batch_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _window_context(self) -> "ContextWindowOpenedEvent":
        if self.window.run_id != self.run_id:
            raise ValueError("context window open run mismatch")
        return self


class ContextWindowClosedEvent(EventBase):
    type: Literal[EventType.CONTEXT_WINDOW_CLOSED] = EventType.CONTEXT_WINDOW_CLOSED
    window_id: str = Field(min_length=1)
    window_generation: int = Field(ge=1)
    close_reason: ContextWindowCloseReason
    final_projection_generation: int = Field(ge=0)
    final_projection_state_fingerprint: str = Field(min_length=1)
    source_through_sequence: int = Field(ge=0)
    next_window_id: str | None
    compaction_terminal_event_id: str | None

    @model_validator(mode="after")
    def _close_shape(self) -> "ContextWindowClosedEvent":
        compaction = self.close_reason is ContextWindowCloseReason.LLM_COMPACTION
        if compaction != (
            self.next_window_id is not None
            and self.compaction_terminal_event_id is not None
        ):
            raise ValueError("window close compaction attribution mismatch")
        return self


class ContextWindowCompactionStartedEvent(EventBase):
    type: Literal[EventType.CONTEXT_WINDOW_COMPACTION_STARTED] = (
        EventType.CONTEXT_WINDOW_COMPACTION_STARTED
    )
    plan: ContextWindowCompactionPlanFact

    @model_validator(mode="after")
    def _started(self) -> "ContextWindowCompactionStartedEvent":
        if self.id != self.plan.stable_started_event_id:
            raise ValueError("window compaction Started stable ID mismatch")
        if self.run_id != self.plan.run_id:
            raise ValueError("window compaction Started run mismatch")
        return self


class ContextWindowCompactionCompletedEvent(EventBase):
    type: Literal[EventType.CONTEXT_WINDOW_COMPACTION_COMPLETED] = (
        EventType.CONTEXT_WINDOW_COMPACTION_COMPLETED
    )
    compaction_id: str = Field(min_length=1)
    started_event_id: str = Field(min_length=1)
    plan_fingerprint: str = Field(min_length=1)
    summary_artifact_id: str = Field(min_length=1)
    summary_content_sha256: str = Field(min_length=1)
    summary_fact_fingerprint: str = Field(min_length=1)
    summary_estimated_tokens: int = Field(ge=1)
    actual_post_compaction_estimated_tokens: int = Field(ge=0)
    post_compaction_target_tokens: int = Field(ge=1)
    target_reached: Literal[True] = True
    summarizer_call: ResolvedModelCallFact
    summarizer_usage: ModelTokenUsageFact | None
    usage_status: Literal["reported", "missing"]
    rollout_settlement_event_id: str = Field(min_length=1)
    source_window_close_event_id: str = Field(min_length=1)
    target_window_open_event_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _completed(self) -> "ContextWindowCompactionCompletedEvent":
        _validate_model_usage(self.usage_status, self.summarizer_usage)
        if (
            self.summarizer_call.purpose
            is not ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
        ):
            raise ValueError("window compaction completed call purpose mismatch")
        if (
            self.actual_post_compaction_estimated_tokens
            > self.post_compaction_target_tokens
        ):
            raise ValueError("completed window compaction did not reach its target")
        ids = (
            self.id,
            self.started_event_id,
            self.source_window_close_event_id,
            self.target_window_open_event_id,
        )
        if len(set(ids)) != len(ids):
            raise ValueError("window compaction completed event IDs overlap")
        return self


class ContextWindowCompactionFailedEvent(EventBase):
    type: Literal[EventType.CONTEXT_WINDOW_COMPACTION_FAILED] = (
        EventType.CONTEXT_WINDOW_COMPACTION_FAILED
    )
    compaction_id: str = Field(min_length=1)
    compaction_attempt_index: int = Field(ge=1)
    source_window_id: str = Field(min_length=1)
    source_window_generation: int = Field(ge=1)
    started_event_id: str | None
    plan_fingerprint: str | None
    failure_stage: Literal[
        "planning",
        "summarizer_resolution",
        "input_manifest",
        "model_validation",
        "model_stream",
        "summary_validation",
        "summary_artifact",
        "terminal_batch",
        "recovery",
    ]
    reason_code: str = Field(min_length=1)
    summarizer_call: ResolvedModelCallFact | None
    rollout_settlement_event_id: str | None
    observed_summary_tokens: int | None = Field(default=None, ge=0)
    observed_post_compaction_tokens: int | None = Field(default=None, ge=0)
    retryable: bool

    @model_validator(mode="after")
    def _failed(self) -> "ContextWindowCompactionFailedEvent":
        before_start = self.failure_stage in {
            "planning",
            "summarizer_resolution",
            "input_manifest",
            "model_validation",
        }
        if before_start:
            if self.started_event_id is not None:
                raise ValueError("pre-Started window failure cannot reference Started")
            if self.rollout_settlement_event_id is not None:
                raise ValueError("pre-Started window failure cannot settle reservation")
        elif self.started_event_id is None or self.rollout_settlement_event_id is None:
            raise ValueError(
                "post-Started window failure requires terminal attribution"
            )
        if self.failure_stage in {"planning", "summarizer_resolution"}:
            if self.plan_fingerprint is not None:
                raise ValueError("early window failure cannot claim a completed plan")
        elif self.plan_fingerprint is None:
            raise ValueError("planned window failure requires plan fingerprint")
        if self.failure_stage == "planning" and self.summarizer_call is not None:
            raise ValueError("planning failure cannot claim a summarizer call")
        if self.summarizer_call is not None and (
            self.summarizer_call.purpose
            is not ModelCallPurpose.CONTEXT_WINDOW_COMPACTION_SUMMARY
        ):
            raise ValueError("window compaction failed call purpose mismatch")
        return self


class ContextProjectionRewritePageEvent(EventBase):
    type: Literal[EventType.CONTEXT_PROJECTION_REWRITE_PAGE] = (
        EventType.CONTEXT_PROJECTION_REWRITE_PAGE
    )
    rewrite_id: str = Field(min_length=1)
    window_id: str = Field(min_length=1)
    from_projection_generation: int = Field(ge=0)
    to_projection_generation: int = Field(ge=1)
    source_through_sequence: int = Field(ge=0)
    page_index: int = Field(ge=0)
    page_count: int = Field(ge=1)
    entries: tuple[ToolObservationProjectionRewriteEntryFact, ...]
    rollups: tuple[ObservationRollupFact, ...]
    plan_fingerprint: str = Field(min_length=1)
    final_state_fingerprint: str = Field(min_length=1)
    reason_code: ProjectionRewriteReason

    @model_validator(mode="after")
    def _page(self) -> "ContextProjectionRewritePageEvent":
        if self.to_projection_generation != self.from_projection_generation + 1:
            raise ValueError("projection rewrite generation must advance by one")
        if self.page_index >= self.page_count:
            raise ValueError("projection rewrite page index is out of range")
        unit_ids = tuple(entry.unit_id for entry in self.entries)
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("projection rewrite page contains duplicate units")
        if any(
            entry.to_projection.window_id != self.window_id
            or entry.to_projection.projection_generation
            != self.to_projection_generation
            for entry in self.entries
        ):
            raise ValueError("projection rewrite entry attribution mismatch")
        return self


class RolloutBudgetAccountOpenedEvent(EventBase):
    type: Literal[EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED] = (
        EventType.ROLLOUT_BUDGET_ACCOUNT_OPENED
    )
    account: RolloutBudgetAccountFact

    @model_validator(mode="after")
    def _account_context(self) -> "RolloutBudgetAccountOpenedEvent":
        if self.account.root_run_id != self.run_id:
            raise ValueError("rollout account root run mismatch")
        return self


class RolloutBudgetAccountClosedEvent(EventBase):
    type: Literal[EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED] = (
        EventType.ROLLOUT_BUDGET_ACCOUNT_CLOSED
    )
    account_id: str = Field(min_length=1)
    final_state_fingerprint: str = Field(min_length=1)
    charged_milliunits: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    active_reservation_count: Literal[0]
    run_end_event_id: str = Field(min_length=1)


class ChildRolloutSubaccountClosedEvent(EventBase):
    type: Literal[EventType.CHILD_ROLLOUT_SUBACCOUNT_CLOSED] = (
        EventType.CHILD_ROLLOUT_SUBACCOUNT_CLOSED
    )
    subaccount_fingerprint: str = Field(min_length=1)
    settlement_aggregate: ChildRolloutSettlementAggregateFact
    run_end_event_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _subaccount_identity(self) -> "ChildRolloutSubaccountClosedEvent":
        if (
            self.subaccount_fingerprint
            != self.settlement_aggregate.subaccount_fingerprint
        ):
            raise ValueError("child rollout close subaccount mismatch")
        return self


class RolloutBudgetReservationCreatedEvent(EventBase):
    type: Literal[EventType.ROLLOUT_BUDGET_RESERVATION_CREATED] = (
        EventType.ROLLOUT_BUDGET_RESERVATION_CREATED
    )
    reservation: RolloutReservationFact


class RolloutBudgetReservationSettledEvent(EventBase):
    type: Literal[EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED] = (
        EventType.ROLLOUT_BUDGET_RESERVATION_SETTLED
    )
    reservation_id: str = Field(min_length=1)
    charged_milliunits: int = Field(ge=0)
    usage_status: Literal[
        "provider_reported_usage",
        "not_started_zero",
        "reserved_missing_usage",
        "cancelled_reserved",
        "tool_terminal",
        "child_terminal_handoff",
        "child_not_started_zero",
    ]
    usage_charge: RolloutUsageChargeFact | None
    source_model_call_end_event_id: str | None
    source_tool_result_event_id: str | None
    child_usage_handoff: ChildRolloutUsageHandoffFact | None

    @model_validator(mode="after")
    def _settlement(self) -> "RolloutBudgetReservationSettledEvent":
        is_model = self.usage_status in {
            "provider_reported_usage",
            "not_started_zero",
            "reserved_missing_usage",
            "cancelled_reserved",
        }
        if is_model:
            if self.usage_charge is None:
                raise ValueError("model settlement requires usage charge")
            if self.source_model_call_end_event_id is None:
                raise ValueError("model settlement requires model end event")
            if self.source_tool_result_event_id is not None:
                raise ValueError("model settlement cannot reference tool result")
            if self.charged_milliunits != self.usage_charge.charged_milliunits:
                raise ValueError("model settlement charge mismatch")
            if self.usage_status != self.usage_charge.accounting_basis:
                raise ValueError("model settlement usage basis mismatch")
            if self.child_usage_handoff is not None:
                raise ValueError("model settlement cannot carry child handoff")
        elif self.usage_status == "tool_terminal":
            if self.usage_charge is not None:
                raise ValueError("tool settlement cannot carry model usage")
            if self.source_model_call_end_event_id is not None:
                raise ValueError("tool settlement cannot reference model end")
            if self.source_tool_result_event_id is None:
                raise ValueError("tool settlement requires tool result event")
            if self.child_usage_handoff is not None:
                raise ValueError("tool settlement cannot carry child handoff")
        elif self.usage_status == "child_terminal_handoff":
            if self.usage_charge is not None:
                raise ValueError("child settlement cannot carry model usage")
            if (
                self.source_model_call_end_event_id is not None
                or self.source_tool_result_event_id is not None
            ):
                raise ValueError("child settlement cannot reference model/tool end")
            if self.child_usage_handoff is None:
                raise ValueError("child terminal settlement requires usage handoff")
            if (
                self.charged_milliunits
                != self.child_usage_handoff.settlement_aggregate.charged_milliunits
            ):
                raise ValueError("child settlement charge mismatch")
        else:
            if self.usage_charge is not None:
                raise ValueError("child start settlement cannot carry model usage")
            if (
                self.source_model_call_end_event_id is not None
                or self.source_tool_result_event_id is not None
                or self.child_usage_handoff is not None
            ):
                raise ValueError("child start settlement cannot carry source facts")
            if self.charged_milliunits != 0:
                raise ValueError("unstarted child settlement must be zero")
        return self


class RolloutPhaseTransitionedEvent(EventBase):
    type: Literal[EventType.ROLLOUT_PHASE_TRANSITIONED] = (
        EventType.ROLLOUT_PHASE_TRANSITIONED
    )
    account_id: str = Field(min_length=1)
    from_phase: RolloutPhase
    to_phase: RolloutPhase
    source_through_sequence: int = Field(ge=0)
    state_before_fingerprint: str = Field(min_length=1)
    state_after_fingerprint: str = Field(min_length=1)
    reason_code: RolloutTransitionReason

    @model_validator(mode="after")
    def _monotonic(self) -> "RolloutPhaseTransitionedEvent":
        from_index = tuple(RolloutPhase).index(self.from_phase)
        to_index = tuple(RolloutPhase).index(self.to_phase)
        if to_index <= from_index:
            raise ValueError("rollout phase transition must advance")
        return self


class SubagentRolloutBudgetResolvedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_ROLLOUT_BUDGET_RESOLVED] = (
        EventType.SUBAGENT_ROLLOUT_BUDGET_RESOLVED
    )
    subagent_run_id: str = Field(min_length=1)
    subagent_task_id: str | None
    budget_snapshot_event_id: str = Field(min_length=1)
    resolved_budget: ResolvedChildRolloutBudgetFact


class ReplyStartEvent(EventBase):
    type: Literal[EventType.REPLY_START] = EventType.REPLY_START
    name: str
    role: Literal["assistant"] = "assistant"


class ReplyEndEvent(EventBase):
    type: Literal[EventType.REPLY_END] = EventType.REPLY_END
    model_terminal_outcome: Literal[
        "completed", "provider_error", "cancelled", "runtime_error"
    ]


class RunErrorEvent(EventBase):
    type: Literal[EventType.RUN_ERROR] = EventType.RUN_ERROR
    message: str
    code: str = "runtime_error"


class ModelCallStartEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_START] = EventType.MODEL_CALL_START
    resolved_call: ResolvedModelCallFact
    context_id: str
    model_call_index: int | None = None
    recovery_plan: ModelStreamRecoveryPlanFact
    provider_input_reference: CommittedProviderInputReferenceFact
    governance_input_attribution: GovernanceModelInputAttributionFact | None = None

    @model_validator(mode="after")
    def _validate_call_context(self) -> "ModelCallStartEvent":
        if not self.context_id:
            raise ValueError("ModelCallStartEvent context_id is required")
        if (
            self.resolved_call.context_mode == "compiled"
            and self.model_call_index is None
        ):
            raise ValueError("compiled model call start requires model_call_index")
        if (
            self.resolved_call.context_mode == "direct"
            and self.model_call_index is not None
        ):
            raise ValueError("direct model call start cannot carry model_call_index")
        if self.model_call_index is not None and self.model_call_index < 0:
            raise ValueError("model_call_index must be non-negative")
        if (
            self.recovery_plan.model_call_start_event_id != self.id
            or self.recovery_plan.lifecycle_kind == "main_assistant_reply"
            and self.model_call_index is None
            or self.recovery_plan.lifecycle_kind != "main_assistant_reply"
            and self.model_call_index is not None
        ):
            raise ValueError("model call start recovery plan lifecycle mismatch")
        is_governance = self.resolved_call.purpose is ModelCallPurpose.MEMORY_GOVERNANCE
        if is_governance != (self.governance_input_attribution is not None):
            raise ValueError(
                "memory governance model Start requires exact batch input attribution"
            )
        if self.governance_input_attribution is not None:
            attribution = self.governance_input_attribution
            if (
                attribution.resolved_model_call_id
                != self.resolved_call.resolved_model_call_id
                or attribution.target_fingerprint
                != self.resolved_call.target.target_fingerprint
            ):
                raise ValueError("governance model Start attribution drifted")
        return self


class ContextCompiledEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPILED] = EventType.CONTEXT_COMPILED
    status: Literal["compiled", "pressure", "failed"] = "compiled"
    failure_stage: ContextCompileFailureStage | None = None
    context_id: str
    model_call_index: int
    compile_attempt_index: int
    context_retry_index: int
    resolved_call: ResolvedModelCallFact
    budget: ContextBudgetReportEvent
    sections: list[dict[str, Any]] = Field(default_factory=list)
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    lifecycle_decisions: list[dict[str, Any]] = Field(default_factory=list)
    tool_result_render_decisions: list[dict[str, Any]] = Field(default_factory=list)
    tool_result_budget_report: dict[str, Any] = Field(default_factory=dict)
    input_audit: ContextCompileInputAuditFact | None = None
    input_failure: ContextCompileInputFailureFact | None = None
    provider_neutral_payload_fingerprint: str | None = None
    canonical_render_decisions_fingerprint: str | None = None
    tool_result_render_decision_facts: tuple[ToolResultRenderDecisionFact, ...] = ()
    tool_result_render_operational_facts: tuple[
        ToolResultRenderOperationalFact, ...
    ] = ()
    long_horizon_context_budget_decision: (
        LongHorizonContextBudgetDecisionFact | None
    ) = None
    long_horizon_projection_pressure_shadow: (
        LongHorizonProjectionPressureShadowFact | None
    ) = None
    prepared_provider_input: PreparedProviderInputAppendCandidateFact | None = None
    manifest_projection_reference: (
        ContextInputManifestProjectionReferenceFact | None
    ) = None
    prepared_provider_input_plan_fingerprint: str | None = None
    prepared_provider_input_candidate_fingerprint: str | None = None

    @model_validator(mode="after")
    def _validate_budget_stage(self) -> "ContextCompiledEvent":
        if (self.input_audit is None) == (self.input_failure is None):
            raise ValueError("context compile event requires exactly one input carrier")
        if self.status == "failed":
            if self.failure_stage is None:
                raise ValueError("failed context compile requires failure stage")
        elif self.status == "pressure" and self.input_failure is not None:
            if self.failure_stage is None:
                raise ValueError("pre-manifest context pressure requires failure stage")
        elif self.failure_stage is not None:
            raise ValueError("non-failed context compile cannot carry failure stage")
        if self.status == "compiled" and self.input_audit is None:
            raise ValueError("compiled context requires full input audit")
        exact_fingerprints = (
            self.provider_neutral_payload_fingerprint,
            self.canonical_render_decisions_fingerprint,
        )
        if self.status == "compiled":
            if any(item is None for item in exact_fingerprints):
                raise ValueError("compiled context requires exact replay fingerprints")
            if self.prepared_provider_input is None:
                raise ValueError("compiled context requires prepared provider input")
            prepared = self.prepared_provider_input
            if (
                prepared.candidate_kind != "compiled_manifest"
                or self.manifest_projection_reference is None
                or self.manifest_projection_reference.context_id != self.context_id
                or self.input_audit.input_manifest_artifact_id
                != self.manifest_projection_reference.input_manifest_artifact_id
                or prepared.manifest_projection_reference
                != self.manifest_projection_reference
                or prepared.prepared_plan is None
                or self.prepared_provider_input_plan_fingerprint
                != prepared.prepared_plan.plan_fingerprint
                or self.prepared_provider_input_candidate_fingerprint
                != prepared.candidate_fingerprint
            ):
                raise ValueError("compiled context provider manifest join mismatch")
        elif any(item is not None for item in exact_fingerprints):
            raise ValueError(
                "non-compiled context cannot carry exact replay fingerprints"
            )
        elif self.prepared_provider_input is not None:
            raise ValueError(
                "non-compiled context cannot carry prepared provider input"
            )
        elif any(
            item is not None
            for item in (
                self.manifest_projection_reference,
                self.prepared_provider_input_plan_fingerprint,
                self.prepared_provider_input_candidate_fingerprint,
            )
        ):
            raise ValueError(
                "non-compiled context cannot carry provider manifest joins"
            )
        if self.input_audit is not None:
            audit = self.input_audit
            if (
                audit.resolved_model_call_id
                != self.resolved_call.resolved_model_call_id
                or audit.model_call_index != self.model_call_index
                or audit.compile_attempt_index != self.compile_attempt_index
                or audit.context_retry_index != self.context_retry_index
            ):
                raise ValueError("context input audit outer identity mismatch")
        if self.input_failure is not None:
            failure = self.input_failure
            if (
                failure.failure_stage != self.failure_stage
                or failure.context_id != self.context_id
                or failure.resolved_model_call_id
                != self.resolved_call.resolved_model_call_id
                or failure.model_call_index != self.model_call_index
                or failure.compile_attempt_index != self.compile_attempt_index
                or failure.context_retry_index != self.context_retry_index
            ):
                raise ValueError("context input failure outer identity mismatch")
        decision_ids = tuple(
            item.unit_id for item in self.tool_result_render_decision_facts
        )
        operational_ids = tuple(
            item.unit_id for item in self.tool_result_render_operational_facts
        )
        if decision_ids != operational_ids:
            raise ValueError("context render decision/operational unit mismatch")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("context render decision unit IDs are not unique")
        budget_decision = self.long_horizon_context_budget_decision
        pressure_shadow = self.long_horizon_projection_pressure_shadow
        if (budget_decision is None) != (pressure_shadow is None):
            raise ValueError(
                "long-horizon context budget decision/shadow must be paired"
            )
        if budget_decision is not None and pressure_shadow is not None:
            if (
                budget_decision.window_id != pressure_shadow.window_id
                or budget_decision.source_through_sequence
                != pressure_shadow.source_through_sequence
                or budget_decision.active_projection_unit_count
                != pressure_shadow.active_projection_unit_count
                or budget_decision.max_projection_units_per_window
                != pressure_shadow.max_projection_units_per_window
                or budget_decision.unit_count_limit_exceeded
                != pressure_shadow.unit_count_limit_exceeded
            ):
                raise ValueError("long-horizon budget/shadow attribution mismatch")
            if (
                self.input_audit is not None
                and self.input_audit.long_horizon_attribution_fingerprint == ""
            ):
                raise ValueError("long-horizon input attribution is required")
        if (
            self.status == "compiled"
            and self.budget.measurement_stage != "final_payload"
        ):
            raise ValueError("compiled context requires a final_payload budget report")
        if self.context_id == "":
            raise ValueError("context_id is required")
        if self.resolved_call.context_mode != "compiled":
            raise ValueError("ContextCompiledEvent requires a compiled resolved call")
        if self.model_call_index < 0:
            raise ValueError("model_call_index must be non-negative")
        target = self.resolved_call.target
        if (
            self.budget.resolved_model_call_id
            != self.resolved_call.resolved_model_call_id
        ):
            raise ValueError("compiled budget resolved call identity mismatch")
        if self.budget.target_fingerprint != target.target_fingerprint:
            raise ValueError("compiled budget target fingerprint mismatch")
        if (
            self.budget.total_context_tokens != target.limits.total_context_tokens
            or self.budget.max_input_tokens != target.limits.max_input_tokens
            or self.budget.max_output_tokens != target.limits.max_output_tokens
            or self.budget.effective_output_tokens
            != target.context_budget.effective_output_tokens
            or self.budget.safety_margin_tokens
            != target.context_budget.safety_margin_tokens
            or self.budget.input_budget_tokens
            != target.context_budget.input_budget_tokens
            or self.budget.estimator != target.token_estimator
        ):
            raise ValueError("compiled budget does not match resolved model target")
        return self


class ProviderInputGenerationStartedEvent(EventBase):
    type: Literal[EventType.PROVIDER_INPUT_GENERATION_STARTED] = (
        EventType.PROVIDER_INPUT_GENERATION_STARTED
    )
    schema_version: Literal["provider_input_generation_started_event.v2"] = (
        "provider_input_generation_started_event.v2"
    )
    generation: ProviderInputGenerationFact
    root_reference: ProviderInputGenerationRootReferenceFact
    initial_vector_root: ProviderInputUnitVectorRootReferenceFact
    initial_prefix_fingerprint: str = Field(min_length=1)
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    expected_initial_append_event_id: str = Field(min_length=1)
    expected_model_start_event_id: str = Field(min_length=1)
    genesis_core_state: CommittedProviderInputGenerationCoreStateFact

    @model_validator(mode="after")
    def _generation_join(self) -> "ProviderInputGenerationStartedEvent":
        if (
            self.root_reference.generation != self.generation
            or self.initial_vector_root != self.root_reference.initial_unit_vector_root
            or self.authority_horizon_set != self.root_reference.authority_horizon_set
            or self.genesis_core_state.generation != self.generation
            or self.genesis_core_state.root_reference != self.root_reference
            or self.genesis_core_state.revision != 0
            or self.genesis_core_state.committed_prefix_fingerprint
            != self.initial_prefix_fingerprint
        ):
            raise ValueError("provider generation Start identity drifted")
        return self


class ProviderInputAppendCommittedEvent(EventBase):
    type: Literal[EventType.PROVIDER_INPUT_APPEND_COMMITTED] = (
        EventType.PROVIDER_INPUT_APPEND_COMMITTED
    )
    schema_version: Literal["provider_input_append_committed_event.v8"] = (
        "provider_input_append_committed_event.v8"
    )
    append_kind: Literal["compiled_manifest", "one_shot"]
    generation_id: str = Field(min_length=1)
    generation_fingerprint: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    resulting_revision: int = Field(ge=1)
    append_batch_reference: ProviderInputAppendBatchReferenceFact
    consumed_preparation_id: str = Field(min_length=1)
    consumed_preparation_ownership_fingerprint: str = Field(min_length=1)
    consumed_pending_continuation_fingerprint: str | None
    continuation_consumption_proof: ProviderInputContinuationConsumptionProofFact | None
    manifest_projection_reference: ContextInputManifestProjectionReferenceFact | None
    causal_validation: ProviderInputCausalValidationResult | None
    frame_placement: ProviderInvocationContextFramePlacementFact | None
    transcript_delta_proof: ProviderTranscriptDeltaCommitProofFact | None
    runtime_observation_units: tuple[PreparedRuntimeObservationProviderUnitFact, ...]
    runtime_observation_semantic_noop_count: int = Field(ge=0)
    source_dispositions: tuple[ContextSourceDispositionFact, ...]
    prepared_provider_input_candidate_fingerprint: str | None
    predecessor_core_state_fingerprint: str
    resulting_core_state: CommittedProviderInputGenerationCoreStateFact
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    resolved_model_call_id: str = Field(min_length=1)
    expected_model_start_event_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _append_join(self) -> "ProviderInputAppendCommittedEvent":
        batch = self.append_batch_reference
        state = self.resulting_core_state
        if (
            self.generation_id != batch.generation.generation_id
            or self.generation_id != state.generation.generation_id
            or self.generation_fingerprint != batch.generation.generation_fingerprint
            or self.generation_fingerprint != state.generation.generation_fingerprint
            or self.resulting_revision != self.expected_revision + 1
            or self.resulting_revision != batch.append_index
            or self.resulting_revision != state.revision
            or self.expected_revision != batch.expected_generation_revision
            or state.committed_prefix_fingerprint != batch.resulting_prefix_fingerprint
            or state.unit_vector_root != batch.resulting_unit_vector_root
            or state.committed_authority_horizon_set
            != batch.resulting_authority_horizon_set
            or self.authority_horizons != batch.authority_horizons
        ):
            raise ValueError("provider input append transition drifted")
        disposition_keys = tuple(
            (item.source_id.value, item.source_instance_id)
            for item in self.source_dispositions
        )
        if disposition_keys != tuple(sorted(set(disposition_keys))):
            raise ValueError(
                "provider append source dispositions are not ordered/unique"
            )
        proof = self.continuation_consumption_proof
        if (self.consumed_pending_continuation_fingerprint is None) != (proof is None):
            raise ValueError("provider append continuation proof matrix mismatch")
        if proof is not None:
            if (
                proof.pending_continuation_fingerprint
                != self.consumed_pending_continuation_fingerprint
                or proof.resulting_transcript_frontier_fingerprint
                != state.transcript_frontier.provider_semantic_frontier_fingerprint
            ):
                raise ValueError("provider append continuation proof join mismatch")
            append_semantics = batch.append_semantic.ordered_unit_semantic_fingerprints
            predecessor_unit_count = state.unit_count - len(append_semantics)
            if predecessor_unit_count < 0:
                raise ValueError("provider append predecessor unit count is invalid")
            if isinstance(proof, ProviderInputContinuationMaterializationProofFact):
                offsets = tuple(
                    ordinal - predecessor_unit_count
                    for ordinal in proof.appended_unit_ordinals
                )
                if any(
                    offset < 0 or offset >= len(append_semantics) for offset in offsets
                ):
                    raise ValueError(
                        "provider continuation proof ordinal exceeds append"
                    )
                if tuple(append_semantics[offset] for offset in offsets) != (
                    proof.ordered_appended_unit_semantic_fingerprints
                ):
                    raise ValueError(
                        "provider continuation proof semantic range drifted"
                    )
            elif isinstance(proof, ProviderInputContinuationRewriteCoverageProofFact):
                offset = proof.replacement_summary_unit_ordinal - predecessor_unit_count
                if (
                    self.expected_revision != 0
                    or self.causal_validation is None
                    or proof.ordered_projection_identity_fingerprint
                    != self.causal_validation.projection_identity_fingerprint
                    or offset < 0
                    or offset >= len(append_semantics)
                    or append_semantics[offset]
                    != proof.replacement_summary_unit_semantic_fingerprint
                    or append_semantics.count(
                        proof.replacement_summary_unit_semantic_fingerprint
                    )
                    != 1
                ):
                    raise ValueError("provider continuation rewrite proof drifted")
            else:  # pragma: no cover - discriminated union is closed
                raise ValueError("unknown provider continuation proof kind")
        remaining_semantics = list(
            batch.append_semantic.ordered_unit_semantic_fingerprints
        )
        observation_ids: set[str] = set()
        for observation in self.runtime_observation_units:
            try:
                remaining_semantics.remove(
                    observation.provider_unit_semantic_fingerprint
                )
            except ValueError as exc:
                raise ValueError(
                    "runtime observation is not carried by the committed append"
                ) from exc
            semantic_id = observation.wire_semantic.observation_semantic_id
            if semantic_id in observation_ids:
                raise ValueError("runtime observation semantic identity is duplicated")
            observation_ids.add(semantic_id)
        if self.append_kind == "compiled_manifest":
            if (
                self.manifest_projection_reference is None
                or self.causal_validation is None
                or self.transcript_delta_proof is None
                or self.prepared_provider_input_candidate_fingerprint is None
            ):
                raise ValueError("compiled append requires manifest causal proof")
            if (
                self.manifest_projection_reference.projection_identity.identity_fingerprint
                != self.causal_validation.projection_identity_fingerprint
                or self.manifest_projection_reference.projection_identity.identity_fingerprint
                != self.transcript_delta_proof.projection_identity_fingerprint
                or self.transcript_delta_proof.resulting_frontier
                != state.transcript_frontier
            ):
                raise ValueError("compiled append projection proof mismatch")
        elif any(
            item is not None
            for item in (
                self.manifest_projection_reference,
                self.causal_validation,
                self.frame_placement,
                self.transcript_delta_proof,
                self.prepared_provider_input_candidate_fingerprint,
            )
        ):
            raise ValueError("one-shot append cannot carry compiled manifest proof")
        return self

    @property
    def resulting_core_state_fingerprint(self) -> str:
        return self.resulting_core_state.core_state_fingerprint


class ExistingGenerationPreparationAbandonedEvent(EventBase):
    type: Literal[EventType.PROVIDER_INPUT_EXISTING_PREPARATION_ABANDONED] = (
        EventType.PROVIDER_INPUT_EXISTING_PREPARATION_ABANDONED
    )
    schema_version: Literal["existing_generation_preparation_abandoned_event.v1"] = (
        "existing_generation_preparation_abandoned_event.v1"
    )
    abandonment_kind: Literal["existing_append"] = "existing_append"
    generation_id: str = Field(min_length=1)
    preparation_id: str = Field(min_length=1)
    preparation_ownership_fingerprint: str = Field(min_length=1)
    context_compiled_event_ref: ContextEventReferenceFact
    resolved_model_call_id: str = Field(min_length=1)
    expected_committed_core_state_fingerprint: str = Field(min_length=1)
    abandonment_reason: Literal[
        "caller_cancelled_before_start",
        "run_terminated_before_start",
        "prepared_candidate_stale",
        "resolved_target_invalidated_before_start",
        "recovery_confirmed_not_started",
    ]
    predecessor_preparation_attribution_fingerprint: str = Field(min_length=1)
    predecessor_scope_binding_fingerprint: str = Field(min_length=1)
    resulting_scope_binding_fingerprint: str = Field(min_length=1)


class ScopedGenerationPreparationAbandonedEvent(EventBase):
    type: Literal[EventType.PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED] = (
        EventType.PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED
    )
    schema_version: Literal["scoped_generation_preparation_abandoned_event.v1"] = (
        "scoped_generation_preparation_abandoned_event.v1"
    )
    abandonment_kind: Literal["initial_start", "rollover_start"]
    scope_fingerprint: str = Field(min_length=1)
    proposed_generation_id: str = Field(min_length=1)
    preparation_id: str = Field(min_length=1)
    old_generation_id: str | None
    expected_old_core_state_fingerprint: str | None
    preparation_ownership_fingerprint: str = Field(min_length=1)
    context_compiled_event_ref: ContextEventReferenceFact
    resolved_model_call_id: str = Field(min_length=1)
    abandonment_reason: Literal[
        "caller_cancelled_before_start",
        "run_terminated_before_start",
        "prepared_candidate_stale",
        "resolved_target_invalidated_before_start",
        "recovery_confirmed_not_started",
    ]
    predecessor_preparation_attribution_fingerprint: str = Field(min_length=1)
    predecessor_scope_binding_fingerprint: str = Field(min_length=1)
    resulting_scope_binding_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _scope_kind(self) -> "ScopedGenerationPreparationAbandonedEvent":
        old_present = self.old_generation_id is not None
        if old_present != (self.expected_old_core_state_fingerprint is not None):
            raise ValueError("scoped abandonment old-generation matrix mismatch")
        if (self.abandonment_kind == "initial_start") == old_present:
            raise ValueError("scoped abandonment kind/old-generation mismatch")
        return self


class ProviderInputGenerationRolloverResolvedEvent(EventBase):
    type: Literal[EventType.PROVIDER_INPUT_GENERATION_ROLLOVER_RESOLVED] = (
        EventType.PROVIDER_INPUT_GENERATION_ROLLOVER_RESOLVED
    )
    schema_version: Literal["provider_input_generation_rollover_resolved_event.v3"] = (
        "provider_input_generation_rollover_resolved_event.v3"
    )
    old_generation_id: str = Field(min_length=1)
    old_generation_fingerprint: str = Field(min_length=1)
    old_final_core_state_fingerprint: str = Field(min_length=1)
    new_generation: ProviderInputGenerationFact
    new_root_reference: ProviderInputGenerationRootReferenceFact
    rollover_request: ProviderInputRolloverRequestFact
    runtime_observation_rewrite: RuntimeObservationProjectionRewriteFact | None
    authority_horizon_set: LedgerAuthorityHorizonSetReferenceFact
    expected_old_close_event_id: str = Field(min_length=1)
    expected_new_start_event_id: str = Field(min_length=1)
    expected_initial_append_event_id: str = Field(min_length=1)
    expected_model_start_event_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _rollover_join(self) -> "ProviderInputGenerationRolloverResolvedEvent":
        if (
            self.new_generation.predecessor_generation_id != self.old_generation_id
            or self.new_generation.predecessor_generation_fingerprint
            != self.old_generation_fingerprint
            or self.new_generation.rollover_reason
            != self.rollover_request.intent.reason
            or self.rollover_request.intent.predecessor_generation_id
            != self.old_generation_id
            or self.new_root_reference.generation != self.new_generation
            or self.new_root_reference.authority_horizon_set
            != self.authority_horizon_set
        ):
            raise ValueError("provider input rollover identity drifted")
        long_horizon = self.rollover_request.intent.reason.value == (
            "explicit_long_horizon_rewrite"
        )
        if long_horizon != (self.runtime_observation_rewrite is not None):
            raise ValueError("provider rollover observation-rewrite matrix mismatch")
        if self.runtime_observation_rewrite is not None:
            authority = self.rollover_request.intent.authority
            parent = self.runtime_observation_rewrite.parent_long_horizon_rewrite_event_reference
            if (
                getattr(authority, "authority_kind", None) != "long_horizon_rewrite"
                or parent
                != authority.rewrite_authority_reference.compaction_completed_event_reference
            ):
                raise ValueError(
                    "provider rollover observation rewrite lacks authority"
                )
        return self


class ProviderInputGenerationClosedEvent(EventBase):
    type: Literal[EventType.PROVIDER_INPUT_GENERATION_CLOSED] = (
        EventType.PROVIDER_INPUT_GENERATION_CLOSED
    )
    schema_version: Literal["provider_input_generation_closed_event.v2"] = (
        "provider_input_generation_closed_event.v2"
    )
    generation_id: str = Field(min_length=1)
    generation_fingerprint: str = Field(min_length=1)
    final_revision: int = Field(ge=0)
    final_prefix_fingerprint: str = Field(min_length=1)
    final_vector_root: ProviderInputUnitVectorRootReferenceFact
    close_reason: Literal["rollover", "session_close", "one_shot_terminal"]
    successor_generation_id: str | None
    unconsumed_continuation_fingerprint: str | None
    predecessor_core_state_fingerprint: str = Field(min_length=1)
    resulting_closed_core_state: CommittedProviderInputGenerationCoreStateFact

    @model_validator(mode="after")
    def _closed(self) -> "ProviderInputGenerationClosedEvent":
        if (self.close_reason == "rollover") != (
            self.successor_generation_id is not None
        ):
            raise ValueError("provider generation close successor matrix mismatch")
        state = self.resulting_closed_core_state
        if (
            state.status != "closed"
            or state.generation.generation_id != self.generation_id
            or state.revision != self.final_revision
            or state.committed_prefix_fingerprint != self.final_prefix_fingerprint
            or state.unit_vector_root != self.final_vector_root
        ):
            raise ValueError("provider generation closed state drifted")
        return self


ProviderInputPreparationAbandonedEvent: TypeAlias = (
    ExistingGenerationPreparationAbandonedEvent
    | ScopedGenerationPreparationAbandonedEvent
)


class CapabilityGateDecisionEvent(EventBase):
    type: Literal[EventType.CAPABILITY_GATE_DECISION] = (
        EventType.CAPABILITY_GATE_DECISION
    )
    tool_call_id: str
    tool_name: str
    descriptor_id: str | None = None
    decision: Literal["allow", "deny", "wait_for_user"]
    reason_code: str | None = None
    reason_message: str | None = None
    suggested_rules: list[dict[str, Any]] = Field(default_factory=list)
    result_state: ToolResultState | None = None
    policy_mode: str | None = None
    permission_policy: dict[str, Any] = Field(default_factory=dict)
    exposure_generation: int | None = None
    availability: str | None = None
    permission_category: str | None = None
    effective_permission_category: str | None = None
    effective_read_only: bool | None = None
    capability_context: dict[str, Any] = Field(default_factory=dict)
    action_classification: ToolActionClassificationFact | None = None

    @model_validator(mode="after")
    def _action_classification_identity(self) -> "CapabilityGateDecisionEvent":
        classification = self.action_classification
        if self.descriptor_id is None:
            if classification is not None:
                raise ValueError(
                    "descriptor-missing gate decision cannot carry action classification"
                )
        elif classification is None:
            raise ValueError(
                "known descriptor gate decision requires action classification"
            )
        elif (
            classification.tool_call_id != self.tool_call_id
            or classification.descriptor_id != self.descriptor_id
        ):
            raise ValueError("gate decision action classification identity mismatch")
        return self


class ModelCallEndEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_END] = EventType.MODEL_CALL_END
    resolved_model_call_id: str
    target_fingerprint: str
    reported_model_id: str | None
    outcome: Literal["completed", "provider_error", "cancelled", "runtime_error"]
    provider_dispatch_status: Literal["not_started", "dispatched"]
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    estimated_input_tokens: int = Field(ge=0)
    diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()
    terminal_projection: ModelCallTerminalProjectionEndReferenceFact

    @model_validator(mode="after")
    def _validate_usage(self) -> "ModelCallEndEvent":
        if self.usage_status == "reported" and self.usage is None:
            raise ValueError("reported model usage requires a usage fact")
        if self.usage_status == "missing" and self.usage is not None:
            raise ValueError("missing model usage cannot contain a usage fact")
        if (
            self.usage_status == "reported"
            and self.provider_dispatch_status != "dispatched"
        ):
            raise ValueError("reported model usage requires provider dispatch")
        if self.outcome in {"completed", "provider_error"} and (
            self.provider_dispatch_status != "dispatched"
        ):
            raise ValueError("completed/provider-error outcome requires dispatch")
        _validate_reported_model_id(self.reported_model_id)
        semantic_join = self.terminal_projection.projection_reference.semantic_join
        if (
            semantic_join.projection_kind != "model_call"
            or semantic_join.terminal_outcome != self.outcome
        ):
            raise ValueError("ModelCallEnd terminal projection outcome mismatch")
        return self


class ModelCallTerminalProjectionCommittedEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_TERMINAL_PROJECTION_COMMITTED] = (
        EventType.MODEL_CALL_TERMINAL_PROJECTION_COMMITTED
    )
    resolved_model_call_id: str = Field(min_length=1)
    model_call_start_event_identity: StableEventIdentityFact
    projection_reference: TerminalProjectionReferenceFact

    @model_validator(mode="after")
    def _projection_identity(self) -> "ModelCallTerminalProjectionCommittedEvent":
        if self.projection_reference.projection_kind != "model_call":
            raise ValueError(
                "model projection event requires model projection reference"
            )
        if (
            self.model_call_start_event_identity.event_type
            != EventType.MODEL_CALL_START.value
        ):
            raise ValueError("model projection source must be ModelCallStart")
        return self


class ProviderModelStreamErrorEvent(EventBase):
    type: Literal[EventType.PROVIDER_MODEL_STREAM_ERROR] = (
        EventType.PROVIDER_MODEL_STREAM_ERROR
    )
    model_stream_attribution: ModelStreamSemanticAttributionFact
    error: ProviderSanitizedErrorFact

    @model_validator(mode="after")
    def _validate_attribution(self) -> "ProviderModelStreamErrorEvent":
        if (
            self.model_stream_attribution.durable_kind
            is not ModelStreamDurableSemanticKind.PROVIDER_ERROR
        ):
            raise ValueError(
                "provider stream error requires provider_error attribution"
            )
        return self


class ModelCallControlDispositionResolvedEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED] = (
        EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED
    )
    resolved_model_call_id: str = Field(min_length=1)
    model_call_start_event_id: str = Field(min_length=1)
    model_call_end_event_id: str = Field(min_length=1)
    model_call_index: int = Field(ge=1)
    source_result_fingerprint: str = Field(min_length=1)
    run_execution_activation: RunExecutionActivationFact
    disposition: ModelCallControlDisposition
    termination_intent: RunTerminationIntentAttributionFact | None
    recovery_reason_code: Literal["process_restarted_before_control_resolution"] | None
    event_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_disposition(self) -> "ModelCallControlDispositionResolvedEvent":
        if self.disposition is ModelCallControlDisposition.ACCEPTED:
            if (
                self.termination_intent is not None
                or self.recovery_reason_code is not None
            ):
                raise ValueError(
                    "accepted disposition cannot carry suppression attribution"
                )
        elif self.disposition is ModelCallControlDisposition.SUPPRESSED_BY_TERMINATION:
            if self.termination_intent is None or self.recovery_reason_code is not None:
                raise ValueError(
                    "termination suppression requires termination attribution"
                )
            if (
                self.termination_intent.target_run_execution_activation_fingerprint
                != self.run_execution_activation.activation_fingerprint
            ):
                raise ValueError("termination suppression activation mismatch")
        elif (
            self.termination_intent is not None
            or self.recovery_reason_code
            != "process_restarted_before_control_resolution"
        ):
            raise ValueError("recovery suppression requires its stable recovery reason")
        expected = sha256_fingerprint(
            "model-call-control-disposition-event:v1",
            self.model_dump(mode="json", exclude={"event_fingerprint", "sequence"}),
        )
        if self.event_fingerprint != expected:
            raise ValueError(
                "model call control disposition event fingerprint mismatch"
            )
        return self


class ModelCallRejectedEvent(EventBase):
    type: Literal[EventType.MODEL_CALL_REJECTED] = EventType.MODEL_CALL_REJECTED
    resolved_call: ResolvedModelCallFact
    context_id: str
    model_call_index: int
    reason_code: Literal[
        "model_input_budget_exceeded",
        "model_input_estimate_mismatch",
        "model_context_identity_mismatch",
        "model_target_capability_mismatch",
        "model_target_binding_mismatch",
    ]
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    input_budget_tokens: int = Field(ge=1)
    diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()

    @model_validator(mode="after")
    def _validate_compiled_call(self) -> "ModelCallRejectedEvent":
        if self.resolved_call.context_mode != "compiled":
            raise ValueError("ModelCallRejectedEvent only supports compiled calls")
        if not self.context_id or self.model_call_index < 0:
            raise ValueError(
                "ModelCallRejectedEvent requires valid context attribution"
            )
        if (
            self.input_budget_tokens
            != self.resolved_call.target.context_budget.input_budget_tokens
        ):
            raise ValueError("rejected input budget does not match resolved target")
        if (
            self.reason_code
            in {
                "model_input_budget_exceeded",
                "model_input_estimate_mismatch",
            }
            and self.estimated_input_tokens is None
        ):
            raise ValueError(f"{self.reason_code} requires estimated_input_tokens")
        return self


def _validate_model_stream_segment_content(
    *,
    content: str,
    content_utf8_bytes: int,
    content_sha256: str,
    estimated_tokens_v1: int | None,
) -> None:
    encoded = content.encode("utf-8")
    if content_utf8_bytes != len(encoded):
        raise ValueError("model stream segment UTF-8 byte count mismatch")
    if content_sha256 != f"sha256:{sha256(encoded).hexdigest()}":
        raise ValueError("model stream segment content SHA mismatch")
    if estimated_tokens_v1 is not None and estimated_tokens_v1 != max(
        1, (len(content) + 3) // 4
    ):
        raise ValueError("model stream segment V1 token estimate mismatch")


def _require_model_stream_durable_kind(
    attribution: ModelStreamSemanticAttributionFact,
    expected: ModelStreamDurableSemanticKind,
) -> None:
    if attribution.durable_kind is not expected:
        raise ValueError("model stream singleton attribution kind mismatch")


class TextBlockStartEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_START] = EventType.TEXT_BLOCK_START
    block_id: str = Field(min_length=1, max_length=128)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "TextBlockStartEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.TEXT_BLOCK_START,
        )
        return self


class TextBlockSegmentEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_SEGMENT] = EventType.TEXT_BLOCK_SEGMENT
    block_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    content_utf8_bytes: int = Field(ge=1)
    content_sha256: str = Field(min_length=1)
    estimated_tokens_v1: int = Field(ge=1)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _validate_content(self) -> "TextBlockSegmentEvent":
        _validate_model_stream_segment_content(
            content=self.text,
            content_utf8_bytes=self.content_utf8_bytes,
            content_sha256=self.content_sha256,
            estimated_tokens_v1=self.estimated_tokens_v1,
        )
        if (
            self.model_stream_attribution.durable_kind
            is not ModelStreamDurableSemanticKind.TEXT_BLOCK_SEGMENT
        ):
            raise ValueError("text segment attribution kind mismatch")
        return self


class TextBlockEndEvent(EventBase):
    type: Literal[EventType.TEXT_BLOCK_END] = EventType.TEXT_BLOCK_END
    block_id: str = Field(min_length=1, max_length=128)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "TextBlockEndEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.TEXT_BLOCK_END,
        )
        return self


class DataBlockStartEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_START] = EventType.DATA_BLOCK_START
    block_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=256)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "DataBlockStartEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.DATA_BLOCK_START,
        )
        return self


class DataBlockSegmentEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_SEGMENT] = EventType.DATA_BLOCK_SEGMENT
    block_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=256)
    data: str = Field(min_length=1)
    content_utf8_bytes: int = Field(ge=1)
    content_sha256: str = Field(min_length=1)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _validate_content(self) -> "DataBlockSegmentEvent":
        _validate_model_stream_segment_content(
            content=self.data,
            content_utf8_bytes=self.content_utf8_bytes,
            content_sha256=self.content_sha256,
            estimated_tokens_v1=None,
        )
        if (
            self.model_stream_attribution.durable_kind
            is not ModelStreamDurableSemanticKind.DATA_BLOCK_SEGMENT
        ):
            raise ValueError("data segment attribution kind mismatch")
        return self


class DataBlockEndEvent(EventBase):
    type: Literal[EventType.DATA_BLOCK_END] = EventType.DATA_BLOCK_END
    block_id: str = Field(min_length=1, max_length=128)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "DataBlockEndEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.DATA_BLOCK_END,
        )
        return self


class ThinkingBlockStartEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_START] = EventType.THINKING_BLOCK_START
    block_id: str = Field(min_length=1, max_length=128)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "ThinkingBlockStartEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.THINKING_BLOCK_START,
        )
        return self


class ThinkingBlockSegmentEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_SEGMENT] = EventType.THINKING_BLOCK_SEGMENT
    block_id: str = Field(min_length=1, max_length=128)
    thinking: str = Field(min_length=1)
    content_utf8_bytes: int = Field(ge=1)
    content_sha256: str = Field(min_length=1)
    estimated_tokens_v1: int = Field(ge=1)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _validate_content(self) -> "ThinkingBlockSegmentEvent":
        _validate_model_stream_segment_content(
            content=self.thinking,
            content_utf8_bytes=self.content_utf8_bytes,
            content_sha256=self.content_sha256,
            estimated_tokens_v1=self.estimated_tokens_v1,
        )
        if (
            self.model_stream_attribution.durable_kind
            is not ModelStreamDurableSemanticKind.THINKING_BLOCK_SEGMENT
        ):
            raise ValueError("thinking segment attribution kind mismatch")
        return self


class ThinkingBlockEndEvent(EventBase):
    type: Literal[EventType.THINKING_BLOCK_END] = EventType.THINKING_BLOCK_END
    block_id: str = Field(min_length=1, max_length=128)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "ThinkingBlockEndEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.THINKING_BLOCK_END,
        )
        return self


class HintBlockEvent(EventBase):
    type: Literal[EventType.HINT_BLOCK] = EventType.HINT_BLOCK
    block_id: str
    hint: str
    source: str | None = None


class ToolCallStartEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_START] = EventType.TOOL_CALL_START
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_call_name: str = Field(min_length=1, max_length=256)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "ToolCallStartEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.TOOL_CALL_START,
        )
        return self


class ToolCallArgumentsSegmentEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_ARGUMENTS_SEGMENT] = (
        EventType.TOOL_CALL_ARGUMENTS_SEGMENT
    )
    tool_call_id: str = Field(min_length=1, max_length=128)
    arguments_json_fragment: str = Field(min_length=1)
    content_utf8_bytes: int = Field(ge=1)
    content_sha256: str = Field(min_length=1)
    estimated_tokens_v1: int = Field(ge=1)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _validate_content(self) -> "ToolCallArgumentsSegmentEvent":
        _validate_model_stream_segment_content(
            content=self.arguments_json_fragment,
            content_utf8_bytes=self.content_utf8_bytes,
            content_sha256=self.content_sha256,
            estimated_tokens_v1=self.estimated_tokens_v1,
        )
        if (
            self.model_stream_attribution.durable_kind
            is not ModelStreamDurableSemanticKind.TOOL_CALL_ARGUMENTS_SEGMENT
        ):
            raise ValueError("tool-call segment attribution kind mismatch")
        return self


class ToolCallEndEvent(EventBase):
    type: Literal[EventType.TOOL_CALL_END] = EventType.TOOL_CALL_END
    tool_call_id: str = Field(min_length=1, max_length=128)
    model_stream_attribution: ModelStreamSemanticAttributionFact

    @model_validator(mode="after")
    def _kind(self) -> "ToolCallEndEvent":
        _require_model_stream_durable_kind(
            self.model_stream_attribution,
            ModelStreamDurableSemanticKind.TOOL_CALL_END,
        )
        return self


class ToolResultStartEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_START] = EventType.TOOL_RESULT_START
    tool_call_id: str
    tool_call_name: str


class ToolResultTextDeltaEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_TEXT_DELTA] = EventType.TOOL_RESULT_TEXT_DELTA
    tool_call_id: str
    delta: str


class ToolResultDataDeltaEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_DATA_DELTA] = EventType.TOOL_RESULT_DATA_DELTA
    tool_call_id: str
    block_id: str = Field(default_factory=lambda: uuid4().hex)
    media_type: str
    data: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "ToolResultDataDeltaEvent":
        if self.data is None and self.url is None:
            raise ValueError("ToolResultDataDeltaEvent needs data or url")
        if self.data is not None and self.url is not None:
            raise ValueError(
                "ToolResultDataDeltaEvent data and url are mutually exclusive"
            )
        return self


class ToolResultEndEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_END] = EventType.TOOL_RESULT_END
    tool_call_id: str
    state: ToolResultState
    artifacts: list[ToolResultArtifactRef] = Field(default_factory=list)
    observation_timing: ToolObservationTimingFact
    render_profile: ToolResultRenderProfileFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None = None
    essential_result: ToolResultEssentialFact | None = None
    terminal_payload_timing: TerminalPayloadTimingFact | None = None
    rollup_semantics: ToolResultRollupSemanticsFact | None
    terminal_projection: ToolResultTerminalProjectionEndReferenceFact

    @model_validator(mode="after")
    def _validate_tool_observation_timing(self) -> "ToolResultEndEvent":
        embedded_tool_call_id = self.observation_timing.tool_call_id
        if (
            embedded_tool_call_id is not None
            and embedded_tool_call_id != self.tool_call_id
        ):
            raise ValueError("ToolResultEndEvent timing tool_call_id mismatch")
        ToolResultExecutionSemanticsFact(
            render_profile=self.render_profile,
            result_state=ToolResultStateFact(self.state.value),
            essential_capture_policy=self.essential_capture_policy,
            essential_result=self.essential_result,
            terminal_payload_timing=self.terminal_payload_timing,
            rollup_semantics=self.rollup_semantics,
        )
        semantic_join = self.terminal_projection.projection_reference.semantic_join
        if (
            semantic_join.projection_kind != "tool_result"
            or semantic_join.tool_call_id != self.tool_call_id
            or semantic_join.result_state.value != self.state.value
        ):
            raise ValueError("ToolResultEnd terminal projection mismatch")
        return self


class ToolResultTerminalProjectionCommittedEvent(EventBase):
    type: Literal[EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED] = (
        EventType.TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED
    )
    tool_call_id: str = Field(min_length=1)
    source_kind: Literal["tool_result_stream", "external_requirement"]
    source_event_identity: StableEventIdentityFact
    projection_reference: TerminalProjectionReferenceFact

    @model_validator(mode="after")
    def _projection_identity(self) -> "ToolResultTerminalProjectionCommittedEvent":
        if self.projection_reference.projection_kind != "tool_result":
            raise ValueError("tool projection event requires tool projection reference")
        expected_event_type = {
            "tool_result_stream": EventType.TOOL_RESULT_START.value,
            "external_requirement": EventType.REQUIRE_EXTERNAL_EXECUTION.value,
        }[self.source_kind]
        if self.source_event_identity.event_type != expected_event_type:
            raise ValueError("tool projection source event type mismatch")
        if self.projection_reference.semantic_join.tool_call_id != self.tool_call_id:
            raise ValueError("tool projection reference call identity mismatch")
        return self


class ToolExecutionSuspendedEvent(EventBase):
    """Canonical fact that a tool call entered a pending interaction."""

    type: Literal[EventType.TOOL_EXECUTION_SUSPENDED] = (
        EventType.TOOL_EXECUTION_SUSPENDED
    )
    interaction_kind: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _validate_payload_identity(self) -> "ToolExecutionSuspendedEvent":
        if self.payload.get("tool_call_id") != self.tool_call_id:
            raise ValueError("tool suspension payload tool_call_id mismatch")
        if self.payload.get("tool_name") != self.tool_name:
            raise ValueError("tool suspension payload tool_name mismatch")
        return self


class RequireUserConfirmEvent(EventBase):
    type: Literal[EventType.REQUIRE_USER_CONFIRM] = EventType.REQUIRE_USER_CONFIRM
    tool_calls: list[ToolCallBlock]


class ConfirmResult(BaseModel):
    confirmed: bool
    tool_call: ToolCallBlock
    rules: list[dict] | None = None


class UserConfirmResultEvent(EventBase):
    type: Literal[EventType.USER_CONFIRM_RESULT] = EventType.USER_CONFIRM_RESULT
    confirm_results: list[ConfirmResult]


class RequireExternalExecutionEvent(EventBase):
    type: Literal[EventType.REQUIRE_EXTERNAL_EXECUTION] = (
        EventType.REQUIRE_EXTERNAL_EXECUTION
    )
    external_tool_calls: tuple[ExternalToolCallRequirementFact, ...]

    @model_validator(mode="after")
    def _requirements(self) -> "RequireExternalExecutionEvent":
        ids = tuple(item.tool_call_id for item in self.external_tool_calls)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(
                "external execution requirements must be non-empty and unique"
            )
        return self


class ExternalExecutionResultEvent(EventBase):
    type: Literal[EventType.EXTERNAL_EXECUTION_RESULT] = (
        EventType.EXTERNAL_EXECUTION_RESULT
    )
    external_results: tuple[ExternalToolResultIngressFact, ...]
    terminal_projections: tuple[ToolResultTerminalProjectionEndReferenceFact, ...]

    @model_validator(mode="after")
    def _validate_external_results(self) -> "ExternalExecutionResultEvent":
        result_ids = [
            result.result_block.tool_call_id for result in self.external_results
        ]
        duplicate_ids = sorted(
            {result_id for result_id in result_ids if result_ids.count(result_id) > 1}
        )
        if duplicate_ids:
            raise ValueError(
                "ExternalExecutionResultEvent external_results contain duplicate ids: "
                + ", ".join(duplicate_ids)
            )
        if not result_ids:
            raise ValueError("ExternalExecutionResultEvent requires external results")
        projection_ids = tuple(
            item.projection_reference.semantic_join.tool_call_id
            for item in self.terminal_projections
        )
        if projection_ids != tuple(result_ids):
            raise ValueError(
                "ExternalExecutionResultEvent terminal projections differ from results"
            )
        return self


class TerminalProcessCompletedEvent(EventBase):
    type: Literal[EventType.TERMINAL_PROCESS_COMPLETED] = (
        EventType.TERMINAL_PROCESS_COMPLETED
    )
    process_id: str
    terminal_session_id: str
    command: str
    status: str
    exit_code: int
    cwd: str
    timed_out: bool = False
    duration_seconds: float
    output_preview: str = ""
    output_truncated: bool = False
    backend_type: str = "local"
    io_mode: str = "pipe"
    tool_call_id: str | None = None
    completion_reason: str | None = None


class PlanModeEnteredEvent(EventBase):
    type: Literal[EventType.PLAN_MODE_ENTERED] = EventType.PLAN_MODE_ENTERED
    source: Literal["user", "agent"]
    previous_permission_mode: Literal[
        "read-only", "ask-permissions", "accept-edits", "bypass-permissions"
    ]
    previous_permission_policy: dict[str, Any]
    reason: str = ""

    @model_validator(mode="after")
    def _validate_previous_permission(self) -> "PlanModeEnteredEvent":
        _validate_preset_permission_payload(
            mode=self.previous_permission_mode,
            policy=self.previous_permission_policy,
            context="PlanModeEnteredEvent.previous",
        )
        return self


class PlanQuestionOption(BaseModel):
    label: str
    description: str = ""
    recommended: bool = False

    @field_validator("label")
    @classmethod
    def _label_must_not_be_empty(cls, value: str) -> str:
        label = value.strip()
        if not label:
            raise ValueError("plan question option label must not be empty")
        return label


class PlanQuestionAskedEvent(EventBase):
    type: Literal[EventType.PLAN_QUESTION_ASKED] = EventType.PLAN_QUESTION_ASKED
    question_id: str
    tool_call_id: str
    question: str
    options: list[PlanQuestionOption] = Field(default_factory=list)
    allow_free_text: bool = True
    reason: str = ""

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str):
                normalized.append({"label": item})
            else:
                normalized.append(item)
        return normalized


class PlanQuestionAnsweredEvent(EventBase):
    type: Literal[EventType.PLAN_QUESTION_ANSWERED] = EventType.PLAN_QUESTION_ANSWERED
    question_id: str
    answer_text: str
    selected_option: str | None = None


class PlanExitRequestedEvent(EventBase):
    type: Literal[EventType.PLAN_EXIT_REQUESTED] = EventType.PLAN_EXIT_REQUESTED
    exit_request_id: str
    tool_call_id: str
    plan_text: str = ""
    plan_artifact_id: str | None = None
    summary: str = ""


class PlanExitResolvedEvent(EventBase):
    type: Literal[EventType.PLAN_EXIT_RESOLVED] = EventType.PLAN_EXIT_RESOLVED
    exit_request_id: str
    tool_call_id: str
    decision: Literal["approve", "revise", "cancel"]
    user_feedback: str = ""


class PlanModeExitedEvent(EventBase):
    type: Literal[EventType.PLAN_MODE_EXITED] = EventType.PLAN_MODE_EXITED
    source: Literal["approved_exit_plan", "user_cancel", "user_force_exit"]
    exit_request_id: str | None = None
    restored_permission_mode: Literal[
        "read-only", "ask-permissions", "accept-edits", "bypass-permissions"
    ]
    restored_permission_policy: dict[str, Any]
    accepted_plan_summary: str = ""
    accepted_plan_artifact_id: str | None = None
    transition_owner: Literal["agent_run", "host_workflow"]
    host_workflow_operation_id: str | None = None

    @model_validator(mode="after")
    def _validate_restored_permission(self) -> "PlanModeExitedEvent":
        _validate_preset_permission_payload(
            mode=self.restored_permission_mode,
            policy=self.restored_permission_policy,
            context="PlanModeExitedEvent.restored",
        )
        if self.transition_owner == "host_workflow":
            if not self.host_workflow_operation_id:
                raise ValueError("host workflow plan exit requires operation id")
        elif self.host_workflow_operation_id is not None:
            raise ValueError(
                "agent-run plan exit cannot carry host workflow operation id"
            )
        return self


class MemoryEventBase(EventBase):
    scope: str
    memory_type: str
    statement: str | None = None
    summary: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    source_authority: memory.SourceAuthority | None = None
    verification_status: memory.VerificationStatus | None = None
    gate_reason: str | None = None


class MemoryCandidateProposedEvent(EventBase):
    type: Literal[EventType.MEMORY_CANDIDATE_PROPOSED] = (
        EventType.MEMORY_CANDIDATE_PROPOSED
    )
    candidate: MemoryCandidate


class MemoryWriteResultEvent(EventBase):
    type: Literal[EventType.MEMORY_WRITE_RESULT] = EventType.MEMORY_WRITE_RESULT
    candidate_id: str
    memory_id: str
    memory_type: str
    status: memory.NodeStatus
    confidence_level: memory.ConfidenceLevel
    verification_status: memory.VerificationStatus
    gate_reason: str


class MemoryWriteFailedEvent(EventBase):
    type: Literal[EventType.MEMORY_WRITE_FAILED] = EventType.MEMORY_WRITE_FAILED
    candidate_id: str | None = None
    memory_type: str | None = None
    error_type: str
    message: str


class MemoryReflectionCompletedEvent(EventBase):
    type: Literal[EventType.MEMORY_REFLECTION_COMPLETED] = (
        EventType.MEMORY_REFLECTION_COMPLETED
    )
    reflection_id: str
    trigger_reason: str
    trigger_reasons: list[str] = Field(default_factory=list)
    safe_point: str = ""
    should_reflect: bool = True
    decision_reason: str = ""
    quoted_evidence: list[str] = Field(default_factory=list)
    candidate_kinds: list[str] = Field(default_factory=list)
    proposed_count: int
    skipped_count: int
    written_count: int
    failed_count: int
    summary: str = ""
    resolved_call: ResolvedModelCallFact
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    estimated_input_tokens: int = Field(ge=0)
    reported_model_id: str | None
    reflection_model_call_end_event_identity: StableEventIdentityFact
    reflection_model_result_semantic_fingerprint: str = Field(min_length=1)
    reflection_policy_contract_fingerprint: str = Field(min_length=1)
    ordered_candidate_attributions: tuple[ReflectionCandidateAttributionFact, ...] = (
        Field(max_length=256)
    )

    @model_validator(mode="after")
    def _validate_usage(self) -> "MemoryReflectionCompletedEvent":
        _validate_model_usage(self.usage_status, self.usage)
        _validate_reported_model_id(self.reported_model_id)
        if self.proposed_count != len(self.ordered_candidate_attributions):
            raise ValueError("reflection proposed count/attribution mismatch")
        indices = tuple(
            item.candidate_index for item in self.ordered_candidate_attributions
        )
        if indices != tuple(range(len(indices))):
            raise ValueError("reflection candidate indices must be contiguous")
        expected_kinds = tuple(
            item.candidate_payload.candidate.kind
            if isinstance(item.candidate_payload, ValidCandidatePayload)
            else item.candidate_payload.attempted_kind or "invalid"
            if isinstance(item.candidate_payload, InvalidAttemptPayload)
            else "invalid"
            for item in self.ordered_candidate_attributions
        )
        if tuple(self.candidate_kinds) != expected_kinds:
            raise ValueError("reflection candidate kinds/attributions mismatch")
        return self


class MemoryReflectionFailedEvent(EventBase):
    type: Literal[EventType.MEMORY_REFLECTION_FAILED] = (
        EventType.MEMORY_REFLECTION_FAILED
    )
    reflection_id: str
    trigger_reason: str
    trigger_reasons: list[str] = Field(default_factory=list)
    safe_point: str = ""
    error_type: str
    message: str
    failure_stage: Literal[
        "input_build",
        "target_resolution",
        "call_resolution",
        "model_validation",
        "model_stream",
        "output_parse",
        "candidate_append",
    ]
    resolved_call: ResolvedModelCallFact | None = None
    usage_status: Literal["reported", "missing"] = "missing"
    usage: ModelTokenUsageFact | None = None
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    reported_model_id: str | None = None

    @model_validator(mode="after")
    def _validate_model_fact_stage(self) -> "MemoryReflectionFailedEvent":
        _validate_model_usage(self.usage_status, self.usage)
        _validate_reported_model_id(self.reported_model_id)
        if (
            self.failure_stage
            in {
                "model_validation",
                "model_stream",
                "output_parse",
                "candidate_append",
            }
            and self.resolved_call is None
        ):
            raise ValueError("reflection failure stage requires resolved_call")
        if (
            self.failure_stage
            in {
                "model_stream",
                "output_parse",
                "candidate_append",
            }
            and self.estimated_input_tokens is None
        ):
            raise ValueError("reflection post-start failure requires input estimate")
        return self


class MemorySupersededEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_SUPERSEDED] = EventType.MEMORY_SUPERSEDED
    memory_id: str
    superseded_by: str


class MemoryContradictionLinkedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_CONTRADICTION_LINKED] = (
        EventType.MEMORY_CONTRADICTION_LINKED
    )
    memory_id: str
    contradicts: str


class MemoryMarkedStaleEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MARKED_STALE] = EventType.MEMORY_MARKED_STALE
    memory_id: str


class MemoryMaintenanceProposedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_PROPOSED] = (
        EventType.MEMORY_MAINTENANCE_PROPOSED
    )
    proposal_id: str
    target_memory_id: str
    action: str


class MemoryMaintenanceAppliedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_APPLIED] = (
        EventType.MEMORY_MAINTENANCE_APPLIED
    )
    proposal_id: str
    target_memory_id: str
    action: str


class MemoryMaintenanceRejectedEvent(MemoryEventBase):
    type: Literal[EventType.MEMORY_MAINTENANCE_REJECTED] = (
        EventType.MEMORY_MAINTENANCE_REJECTED
    )
    proposal_id: str
    target_memory_id: str
    action: str


class ProjectionEventBase(EventBase):
    projection_id: str
    role: str
    scope: str
    token_budget: int | None = None


class ProjectionRequestedEvent(ProjectionEventBase):
    type: Literal[EventType.PROJECTION_REQUESTED] = EventType.PROJECTION_REQUESTED


class RecalledMemoryProjectionEntryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_index: int = Field(ge=0)
    memory_ids: tuple[str, ...]
    model_visible_text: str
    text_utf8_sha256: str
    entry_semantic_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "RecalledMemoryProjectionEntryFact":
        if self.memory_ids != tuple(sorted(set(self.memory_ids))):
            raise ValueError("recalled memory entry IDs must be ordered/unique")
        encoded = self.model_visible_text.encode("utf-8")
        if self.text_utf8_sha256 != f"sha256:{sha256(encoded).hexdigest()}":
            raise ValueError("recalled memory entry text digest mismatch")
        expected = sha256_fingerprint(
            "recalled-memory-projection-entry:v1",
            {
                "entry_index": self.entry_index,
                "memory_ids": self.memory_ids,
                "model_visible_text": self.model_visible_text,
                "text_utf8_sha256": self.text_utf8_sha256,
            },
        )
        if self.entry_semantic_fingerprint != expected:
            raise ValueError("recalled memory entry semantic fingerprint mismatch")
        return self


class ProjectionReadyEvent(ProjectionEventBase):
    type: Literal[EventType.PROJECTION_READY] = EventType.PROJECTION_READY
    projection_kind: Literal["memory"] = "memory"
    included_memory_ids: list[str] = Field(default_factory=list)
    filtered_memory_ids: list[str] = Field(default_factory=list)
    recalled_memory_entries: tuple[RecalledMemoryProjectionEntryFact, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _typed_projection(self) -> "ProjectionReadyEvent":
        indices = tuple(item.entry_index for item in self.recalled_memory_entries)
        if indices != tuple(range(len(indices))):
            raise ValueError("recalled memory entry indices must be contiguous")
        ids = tuple(sorted(set(self.included_memory_ids)))
        entry_ids = tuple(
            sorted(
                {
                    memory_id
                    for entry in self.recalled_memory_entries
                    for memory_id in entry.memory_ids
                }
            )
        )
        if ids != entry_ids:
            raise ValueError("recalled memory IDs do not match typed entries")
        expected_summary = _render_recalled_memory_projection_summary(
            self.recalled_memory_entries
        )
        if self.summary != expected_summary:
            raise ValueError(
                "recalled memory summary is not derived from typed entries"
            )
        return self


def _render_recalled_memory_projection_summary(
    entries: tuple[RecalledMemoryProjectionEntryFact, ...],
) -> str:
    if not entries:
        return ""
    return "\n".join(
        (
            '<recalled-memory-projection do_not_write_back="true">',
            *(f"- {entry.model_visible_text}" for entry in entries),
            "</recalled-memory-projection>",
        )
    )


class ProjectionFailedEvent(ProjectionEventBase):
    type: Literal[EventType.PROJECTION_FAILED] = EventType.PROJECTION_FAILED
    error: str


def _validate_compaction_target_contract(
    *,
    target: ResolvedModelTargetFact,
    target_input_budget_tokens: int,
    target_estimate: CompactionTargetEstimateFact,
) -> None:
    if target_input_budget_tokens != target.context_budget.input_budget_tokens:
        raise ValueError("compaction target input budget mismatch")
    if target_estimate.target_fingerprint != target.target_fingerprint:
        raise ValueError("compaction target estimate fingerprint mismatch")


def _validate_compaction_summarizer_contract(
    *,
    call: ResolvedModelCallFact,
    context_id: str,
    estimated_input_tokens: int,
    input_budget_tokens: int,
) -> None:
    if call.purpose != ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
        raise ValueError("compaction requires a summarizer call")
    if not context_id:
        raise ValueError("summarizer context id is required")
    if input_budget_tokens != call.target.context_budget.input_budget_tokens:
        raise ValueError("summarizer input budget does not match call target")
    if estimated_input_tokens > input_budget_tokens:
        raise ValueError("started summarizer input exceeds resolved budget")


def _validate_compaction_boundary_attribution(
    host_boundary_id: str | None,
    host_boundary_kind: Literal["pre_run"] | None,
) -> None:
    if (host_boundary_id is None) != (host_boundary_kind is None):
        raise ValueError("compaction host boundary attribution is all-or-none")
    if host_boundary_id is not None and not host_boundary_id:
        raise ValueError("compaction host boundary id cannot be empty")


class ContextCompactionStartedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_STARTED] = (
        EventType.CONTEXT_COMPACTION_STARTED
    )
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    target_model_target: ResolvedModelTargetFact
    target_input_budget_tokens: int = Field(ge=1)
    threshold_tokens: int
    post_compaction_target_tokens: int = Field(ge=1)
    target_estimate: CompactionTargetEstimateFact
    summarizer_call: ResolvedModelCallFact
    summarizer_context_id: str
    summarizer_input_estimated_tokens: int = Field(ge=0)
    summarizer_input_budget_tokens: int = Field(ge=1)
    through_sequence: int
    keep_after_sequence: int
    force: bool = False
    terminal_event_id: str = Field(min_length=1)
    host_boundary_id: str | None = None
    host_boundary_kind: Literal["pre_run"] | None = None

    @model_validator(mode="after")
    def _validate_compaction_contract(self) -> "ContextCompactionStartedEvent":
        _validate_compaction_boundary_attribution(
            self.host_boundary_id, self.host_boundary_kind
        )
        _validate_compaction_target_contract(
            target=self.target_model_target,
            target_input_budget_tokens=self.target_input_budget_tokens,
            target_estimate=self.target_estimate,
        )
        if (
            self.target_estimate.target_fingerprint
            != self.target_model_target.target_fingerprint
        ):
            raise ValueError("compaction target estimate fingerprint mismatch")
        if self.target_estimate.summary_tokens_actual is not None:
            raise ValueError(
                "started compaction cannot contain actual summary measurements"
            )
        if self.summarizer_call.purpose != ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            raise ValueError("compaction started requires a summarizer call")
        _validate_compaction_summarizer_contract(
            call=self.summarizer_call,
            context_id=self.summarizer_context_id,
            estimated_input_tokens=self.summarizer_input_estimated_tokens,
            input_budget_tokens=self.summarizer_input_budget_tokens,
        )
        return self


class ContextCompactionCompletedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_COMPLETED] = (
        EventType.CONTEXT_COMPACTION_COMPLETED
    )
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    summary_artifact_id: str
    summary_chars: int
    target_model_target: ResolvedModelTargetFact
    target_input_budget_tokens: int = Field(ge=1)
    threshold_tokens: int
    post_compaction_target_tokens: int = Field(ge=1)
    target_estimate: CompactionTargetEstimateFact
    summarizer_call: ResolvedModelCallFact
    summarizer_context_id: str
    summarizer_input_estimated_tokens: int = Field(ge=0)
    summarizer_input_budget_tokens: int = Field(ge=1)
    summarizer_usage_status: Literal["reported", "missing"]
    summarizer_usage: ModelTokenUsageFact | None
    summarizer_estimated_input_tokens: int = Field(ge=0)
    summarizer_reported_model_id: str | None
    predicted_post_target_reached: bool | None
    through_sequence: int
    keep_after_sequence: int
    included_run_ids: list[str] = Field(default_factory=list)
    included_artifact_ids: list[str] = Field(default_factory=list)
    started_event_id: str = Field(min_length=1)
    host_boundary_id: str | None = None
    host_boundary_kind: Literal["pre_run"] | None = None

    @model_validator(mode="after")
    def _validate_compaction_contract(self) -> "ContextCompactionCompletedEvent":
        _validate_compaction_boundary_attribution(
            self.host_boundary_id, self.host_boundary_kind
        )
        _validate_model_usage(self.summarizer_usage_status, self.summarizer_usage)
        _validate_reported_model_id(self.summarizer_reported_model_id)
        if (
            self.target_estimate.target_fingerprint
            != self.target_model_target.target_fingerprint
        ):
            raise ValueError("compaction target estimate fingerprint mismatch")
        if self.target_estimate.summary_tokens_actual is None:
            raise ValueError(
                "completed compaction requires actual summary measurements"
            )
        if (
            self.predicted_post_target_reached
            != self.target_estimate.predicted_post_target_reached
        ):
            raise ValueError("predicted target projection must match target estimate")
        if self.target_estimate.estimate_scope == "compiled_context_baseline":
            expected_prediction = (
                self.target_estimate.estimated_tokens_after
                <= self.post_compaction_target_tokens
            )
            if self.predicted_post_target_reached is not expected_prediction:
                raise ValueError(
                    "compiled target prediction does not match the post-compaction target"
                )
        elif self.predicted_post_target_reached is not None:
            raise ValueError(
                "transcript-only completion cannot claim full target success"
            )
        if self.summarizer_call.purpose != ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            raise ValueError("compaction completed requires a summarizer call")
        _validate_compaction_target_contract(
            target=self.target_model_target,
            target_input_budget_tokens=self.target_input_budget_tokens,
            target_estimate=self.target_estimate,
        )
        _validate_compaction_summarizer_contract(
            call=self.summarizer_call,
            context_id=self.summarizer_context_id,
            estimated_input_tokens=self.summarizer_input_estimated_tokens,
            input_budget_tokens=self.summarizer_input_budget_tokens,
        )
        if (
            self.summarizer_estimated_input_tokens
            != self.summarizer_input_estimated_tokens
        ):
            raise ValueError(
                "summarizer terminal estimate does not match started estimate"
            )
        return self


class CompactionCandidateDiagnosticEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    field: str | None = None
    message: str = ""
    redacted: bool = False


class ContextCompactionMemoryCandidatesProposedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED] = (
        EventType.CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED
    )
    compaction_id: str
    source_event_id: str
    source_event_sequence: int
    summary_artifact_id: str
    candidate_entry_ids: list[str] = Field(default_factory=list)
    attempted_count: int = 0
    proposed_count: int
    skipped_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    extractor_version: str = "compaction-memory-candidates:v1"
    diagnostics: list[CompactionCandidateDiagnosticEvent] = Field(default_factory=list)
    summary_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_content_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    extractor_contract: CompactionMemoryCandidateExtractorContractFact
    ordered_candidate_attributions: tuple[CompactionCandidateAttributionFact, ...] = (
        Field(max_length=256)
    )
    completed_compaction_event_identity: StableEventIdentityFact

    @model_validator(mode="after")
    def _candidate_attribution_join(
        self,
    ) -> "ContextCompactionMemoryCandidatesProposedEvent":
        if self.proposed_count != len(self.ordered_candidate_attributions):
            raise ValueError("compaction proposed count/attribution mismatch")
        if tuple(self.candidate_entry_ids) != tuple(
            item.candidate_entry_id for item in self.ordered_candidate_attributions
        ):
            raise ValueError("compaction candidate IDs/attributions mismatch")
        if self.extractor_version != self.extractor_contract.extractor_version:
            raise ValueError("compaction extractor version drifted")
        return self


class MemoryCandidateEvidenceRejectedEvent(EventBase):
    type: Literal[EventType.MEMORY_CANDIDATE_EVIDENCE_REJECTED] = (
        EventType.MEMORY_CANDIDATE_EVIDENCE_REJECTED
    )
    governance_batch_id: str = Field(min_length=1, max_length=256)
    rejection: MemoryCandidateEvidenceRejectedRecord


class MemoryGovernanceBatchPreparedEvent(EventBase):
    type: Literal[EventType.MEMORY_GOVERNANCE_BATCH_PREPARED] = (
        EventType.MEMORY_GOVERNANCE_BATCH_PREPARED
    )
    governance_batch_id: str = Field(min_length=1, max_length=256)
    source_ledger_through_sequence: int = Field(ge=0)
    candidate_entry_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    preparing_claims_fingerprint: str = Field(min_length=1)
    batch_input_reference: GovernanceBatchInputReferenceFact
    resolved_model_call_id: str = Field(min_length=1, max_length=256)
    target_fingerprint: str = Field(min_length=1)
    model_input_fingerprint: str = Field(min_length=1)
    ordered_prompt_projections_fingerprint: str = Field(min_length=1)
    event_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _prepared_join(self) -> "MemoryGovernanceBatchPreparedEvent":
        if self.batch_input_reference.governance_batch_id != self.governance_batch_id:
            raise ValueError("prepared governance batch reference drifted")
        if len(self.candidate_entry_ids) != len(set(self.candidate_entry_ids)):
            raise ValueError("prepared governance candidate IDs must be unique")
        return self


class MemoryGovernanceBatchCompletedEvent(EventBase):
    type: Literal[EventType.MEMORY_GOVERNANCE_BATCH_COMPLETED] = (
        EventType.MEMORY_GOVERNANCE_BATCH_COMPLETED
    )
    governance_batch_id: str
    prepared_event_id: str
    batch_input_fingerprint: str
    governance_model_call_id: str
    decision_ids: tuple[str, ...] = Field(max_length=32)
    terminal_reason: Literal["decisions_applied", "no_decisions"]
    diagnostics: tuple[str, ...] = Field(max_length=8)
    terminal_event_fingerprint: str


class MemoryGovernanceBatchFailedEvent(EventBase):
    type: Literal[EventType.MEMORY_GOVERNANCE_BATCH_FAILED] = (
        EventType.MEMORY_GOVERNANCE_BATCH_FAILED
    )
    governance_batch_id: str
    prepared_event_id: str
    batch_input_fingerprint: str
    governance_model_call_id: str | None
    decision_ids: tuple[str, ...] = Field(max_length=32)
    terminal_reason: Literal[
        "model_failed", "output_invalid", "decision_apply_failed", "cancelled"
    ]
    diagnostics: tuple[str, ...] = Field(max_length=8)
    terminal_event_fingerprint: str


class MemoryGovernanceBatchBlockedEvent(EventBase):
    type: Literal[EventType.MEMORY_GOVERNANCE_BATCH_BLOCKED] = (
        EventType.MEMORY_GOVERNANCE_BATCH_BLOCKED
    )
    governance_batch_id: str
    prepared_event_id: str
    batch_input_fingerprint: str
    governance_model_call_id: str | None
    decision_ids: tuple[str, ...] = Field(max_length=32)
    terminal_reason: Literal[
        "authority_untrusted", "artifact_untrusted", "historical_binding_missing"
    ]
    diagnostics: tuple[str, ...] = Field(max_length=8)
    terminal_event_fingerprint: str


class ContextCompactionFailedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_FAILED] = (
        EventType.CONTEXT_COMPACTION_FAILED
    )
    compaction_id: str
    trigger: Literal["manual", "auto"]
    reason: str
    window_number: int
    window_id: str
    target_model_target: ResolvedModelTargetFact
    target_input_budget_tokens: int = Field(ge=1)
    threshold_tokens: int
    post_compaction_target_tokens: int = Field(ge=1)
    failure_stage: Literal[
        "planning",
        "summarizer_resolution",
        "summarizer_input_build",
        "started_append",
        "model_validation",
        "model_stream",
        "summary_validation",
        "artifact_write",
        "completed_append",
        "recovery_terminalization",
    ]
    target_estimate: CompactionTargetEstimateFact | None = None
    observed_after_measurement: CompactionObservedAfterMeasurementFact | None = None
    summarizer_target: ResolvedModelTargetFact | None = None
    summarizer_call: ResolvedModelCallFact | None = None
    summarizer_context_id: str | None = None
    summarizer_input_estimated_tokens: int | None = Field(default=None, ge=0)
    summarizer_input_budget_tokens: int | None = Field(default=None, ge=1)
    summarizer_usage_status: Literal["reported", "missing"] = "missing"
    summarizer_usage: ModelTokenUsageFact | None = None
    summarizer_estimated_input_tokens: int | None = Field(default=None, ge=0)
    summarizer_reported_model_id: str | None = None
    through_sequence: int | None = None
    keep_after_sequence: int | None = None
    error_type: str
    message: str
    started_event_id: str | None = None
    termination_kind: Literal["failed", "cancelled", "recovered_interrupted"]
    host_boundary_id: str | None = None
    host_boundary_kind: Literal["pre_run"] | None = None

    @model_validator(mode="after")
    def _validate_failure_stage(self) -> "ContextCompactionFailedEvent":
        _validate_compaction_boundary_attribution(
            self.host_boundary_id, self.host_boundary_kind
        )
        if self.termination_kind in {"cancelled", "recovered_interrupted"}:
            if self.started_event_id is None:
                raise ValueError(
                    "cancelled/recovered compaction requires started event attribution"
                )
        elif self.started_event_id is None and self.failure_stage not in {
            "planning",
            "summarizer_resolution",
            "summarizer_input_build",
            "started_append",
        }:
            raise ValueError("post-start compaction failure requires started event id")
        _validate_model_usage(self.summarizer_usage_status, self.summarizer_usage)
        _validate_reported_model_id(self.summarizer_reported_model_id)
        if (
            self.target_input_budget_tokens
            != self.target_model_target.context_budget.input_budget_tokens
        ):
            raise ValueError("compaction target input budget mismatch")
        stages = [
            "planning",
            "summarizer_resolution",
            "summarizer_input_build",
            "started_append",
            "model_validation",
            "model_stream",
            "summary_validation",
            "artifact_write",
            "completed_append",
            "recovery_terminalization",
        ]
        stage_index = stages.index(self.failure_stage)
        if (
            stage_index >= stages.index("summarizer_resolution")
            and self.target_estimate is None
        ):
            raise ValueError(
                "post-planning compaction failure requires target estimate"
            )
        if (
            stage_index >= stages.index("summarizer_input_build")
            and self.summarizer_call is None
        ):
            raise ValueError("summarizer input failure requires resolved call")
        if stage_index >= stages.index("model_validation"):
            if (
                self.summarizer_context_id is None
                or self.summarizer_input_estimated_tokens is None
                or self.summarizer_input_budget_tokens is None
            ):
                raise ValueError(
                    "model-stage compaction failure requires summarizer context"
                )
        if (
            stage_index >= stages.index("model_stream")
            and self.summarizer_estimated_input_tokens is None
        ):
            raise ValueError("post-start compaction failure requires input estimate")
        if self.target_estimate is not None:
            _validate_compaction_target_contract(
                target=self.target_model_target,
                target_input_budget_tokens=self.target_input_budget_tokens,
                target_estimate=self.target_estimate,
            )
            has_actual_after = self.target_estimate.estimated_tokens_after is not None
            if (
                self.failure_stage in {"artifact_write", "completed_append"}
                and not has_actual_after
            ):
                raise ValueError(
                    "post-summary persistence failure requires actual after measurements"
                )
            if has_actual_after:
                if self.target_estimate.estimate_scope == "compiled_context_baseline":
                    expected_prediction = (
                        self.target_estimate.estimated_tokens_after
                        <= self.post_compaction_target_tokens
                    )
                    if (
                        self.target_estimate.predicted_post_target_reached
                        is not expected_prediction
                    ):
                        raise ValueError(
                            "compiled target prediction does not match the post-compaction target"
                        )
                elif self.target_estimate.predicted_post_target_reached is not None:
                    raise ValueError(
                        "transcript-only failure cannot claim full target success"
                    )
        if self.observed_after_measurement is not None:
            if self.failure_stage != "summary_validation":
                raise ValueError(
                    "observed after measurement is only valid for summary validation"
                )
            if self.target_estimate is None:
                raise ValueError(
                    "observed after measurement requires a planning estimate"
                )
            if any(
                value is not None
                for value in (
                    self.target_estimate.summary_tokens_actual,
                    self.target_estimate.transcript_tokens_after,
                    self.target_estimate.estimated_tokens_after,
                    self.target_estimate.predicted_post_target_reached,
                )
            ):
                raise ValueError(
                    "observed after measurement requires a planning-only target estimate"
                )
            observed = self.observed_after_measurement
            if (
                observed.retained_transcript_tokens
                != self.target_estimate.retained_transcript_tokens
                or observed.protected_transcript_tokens
                != self.target_estimate.protected_transcript_tokens
            ):
                raise ValueError(
                    "observed retained/protected transcript does not match planning"
                )
            if (
                observed.summary_tokens_actual
                <= self.target_estimate.summary_tokens_reserved
            ):
                raise ValueError(
                    "summary reservation violation requires observed tokens above reservation"
                )
            baseline = self.target_estimate.non_transcript_baseline_tokens
            if baseline is None:
                if observed.estimated_tokens_after != observed.transcript_tokens_after:
                    raise ValueError(
                        "transcript-only observed estimate is inconsistent"
                    )
                if observed.predicted_post_target_reached is not None:
                    raise ValueError(
                        "transcript-only observed estimate cannot claim target success"
                    )
            else:
                if observed.estimated_tokens_after != (
                    baseline + observed.transcript_tokens_after
                ):
                    raise ValueError("compiled observed estimate is inconsistent")
                if observed.predicted_post_target_reached is None:
                    raise ValueError(
                        "compiled observed estimate requires a target prediction"
                    )
                expected_prediction = (
                    observed.estimated_tokens_after
                    <= self.post_compaction_target_tokens
                )
                if observed.predicted_post_target_reached is not expected_prediction:
                    raise ValueError(
                        "compiled observed prediction does not match the post-compaction target"
                    )
        if self.summarizer_call is not None:
            if (
                self.summarizer_target is not None
                and self.summarizer_target != self.summarizer_call.target
            ):
                raise ValueError("summarizer target and call mismatch")
            if self.summarizer_input_budget_tokens is not None and (
                self.summarizer_input_budget_tokens
                != self.summarizer_call.target.context_budget.input_budget_tokens
            ):
                raise ValueError("summarizer input budget does not match call target")
            if (
                self.summarizer_context_id is not None
                and not self.summarizer_context_id
            ):
                raise ValueError("summarizer context id must be non-empty")
        return self


SubagentPermissionMode = Literal[
    "read-only",
    "ask-permissions",
    "accept-edits",
    "bypass-permissions",
]
SubagentCapabilityProfileName = Literal[
    "general_worker",
    "research_worker",
    "review_worker",
    "verification_worker",
    "synthesizer",
    "orchestrator",
]


class SubagentContextPolicySnapshotEvent(BaseModel):
    """Immutable event-visible child context policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["isolated", "fork"]
    include_parent_summary: bool
    include_parent_current_task: bool
    include_parent_memory_projection: bool
    include_parent_artifact_refs: bool
    max_parent_context_chars: int | None
    fork_source_context_id: str | None

    @model_validator(mode="after")
    def _validate_context_policy(self) -> SubagentContextPolicySnapshotEvent:
        if (
            self.max_parent_context_chars is not None
            and self.max_parent_context_chars <= 0
        ):
            raise ValueError("max_parent_context_chars must be positive when provided")
        if self.mode == "isolated" and self.fork_source_context_id is not None:
            raise ValueError(
                "isolated context policy cannot set fork_source_context_id"
            )
        if self.mode == "fork" and not self.fork_source_context_id:
            raise ValueError("fork context policy requires fork_source_context_id")
        return self


class SubagentCapabilityProfileSnapshotEvent(BaseModel):
    """Immutable event-visible child capability contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_name: SubagentCapabilityProfileName
    inherited_from_parent_context_id: str | None
    permission_mode: SubagentPermissionMode
    permission_policy: dict[str, Any]
    allowed_tool_names: tuple[str, ...]
    allowed_descriptor_ids: tuple[str, ...]
    allowed_skill_names: tuple[str, ...]
    allowed_mcp_server_ids: tuple[str, ...]
    can_spawn_subagents: bool
    max_spawn_depth_from_root: int
    memory_enabled: bool
    computed_from_parent_exposure_generation: int | None
    diagnostics: tuple[dict[str, Any], ...]

    @model_validator(mode="after")
    def _validate_capability_profile(self) -> SubagentCapabilityProfileSnapshotEvent:
        _validate_preset_permission_payload(
            mode=self.permission_mode,
            policy=self.permission_policy,
            context="SubagentCapabilityProfileSnapshotEvent",
        )
        if self.can_spawn_subagents:
            raise ValueError("V1 child capability profile cannot spawn subagents")
        if self.max_spawn_depth_from_root != 0:
            raise ValueError(
                "V1 child capability profile max_spawn_depth_from_root must be 0"
            )
        if self.memory_enabled:
            raise ValueError("V1 child capability profile cannot enable memory")
        for field_name in (
            "allowed_tool_names",
            "allowed_descriptor_ids",
            "allowed_skill_names",
            "allowed_mcp_server_ids",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be unique and canonically sorted")
        return self


class SubagentBudgetSnapshotEvent(BaseModel):
    """Immutable event-visible limits for one child runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_children_per_parent_run: int
    max_concurrent_children_per_host_session: int
    max_spawn_depth_from_root: int
    child_timeout_seconds: float | None
    max_total_child_runs_per_parent_run: int
    max_result_summary_chars_per_child: int
    max_result_artifact_refs_per_child: int
    max_subagent_results_per_parent_compile: int
    child_rollout_policy: ChildRolloutReservationPolicyFact

    @model_validator(mode="after")
    def _validate_budget(self) -> SubagentBudgetSnapshotEvent:
        positive_fields = (
            "max_concurrent_children_per_parent_run",
            "max_concurrent_children_per_host_session",
            "max_total_child_runs_per_parent_run",
            "max_subagent_results_per_parent_compile",
        )
        for field_name in positive_fields:
            if getattr(self, field_name) < 1:
                raise ValueError(f"{field_name} must be >= 1")
        if self.max_spawn_depth_from_root != 0:
            raise ValueError("V1 subagent budget max_spawn_depth_from_root must be 0")
        if (
            self.max_result_summary_chars_per_child < 0
            or self.max_result_artifact_refs_per_child < 0
        ):
            raise ValueError("subagent result summary/artifact caps must be >= 0")
        if self.child_timeout_seconds is not None:
            if (
                not math.isfinite(self.child_timeout_seconds)
                or self.child_timeout_seconds <= 0
            ):
                raise ValueError(
                    "child_timeout_seconds must be finite and > 0 when provided"
                )
        return self


class SubagentRunStartedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_STARTED] = EventType.SUBAGENT_RUN_STARTED
    subagent_run_id: str
    task_id: str | None = None
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    run_index: int | None = None
    edge_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    parent_context_id: str | None = None
    parent_model_call_index: int | None = None
    spawning_tool_name: str | None = None
    spawn_initiator_kind: (
        Literal["tool_call", "scheduler", "dependency_satisfied"] | None
    ) = None
    spawn_initiator_id: str | None = None
    child_runtime_session_id: str
    label: str | None = None
    role: str
    profile_id: str | None = None
    task_preview: str
    context_policy: SubagentContextPolicySnapshotEvent
    capability_profile: SubagentCapabilityProfileSnapshotEvent
    budget_snapshot: SubagentBudgetSnapshotEvent

    @model_validator(mode="after")
    def _validate_spawn_initiator(self) -> SubagentRunStartedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if self.task_id is None:
            if self.run_index is not None:
                raise ValueError("primitive subagent run cannot set run_index")
        elif self.run_index != 1:
            raise ValueError("V1 task-backed subagent run requires run_index=1")
        if self.spawn_initiator_kind == "tool_call":
            if not self.spawn_initiator_id:
                raise ValueError(
                    "tool_call spawn initiator requires spawn_initiator_id"
                )
        elif self.spawn_initiator_kind is not None and not self.spawn_initiator_id:
            raise ValueError("non-tool spawn initiator requires spawn_initiator_id")
        elif self.spawning_tool_name is not None:
            raise ValueError(
                "spawning_tool_name is only valid for tool_call initiators"
            )
        return self


class SubagentMessageSentEvent(EventBase):
    type: Literal[EventType.SUBAGENT_MESSAGE_SENT] = EventType.SUBAGENT_MESSAGE_SENT
    edge_id: str
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    child_runtime_session_id: str
    message_artifact_id: str
    message_preview: str
    delivery_kind: Literal["spawn_task", "send", "followup"]


class SubagentRunSuspendedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_SUSPENDED] = EventType.SUBAGENT_RUN_SUSPENDED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str
    pending_kind: str
    reason_code: str
    reason_message: str | None = None
    resumable: bool = False


class SubagentRunCompletedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_COMPLETED] = EventType.SUBAGENT_RUN_COMPLETED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str
    child_run_id: str
    result_id: str
    summary: str
    result_artifact_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    tool_call_count: int | None = None
    result_handoff: ChildResultHandoffFact

    @model_validator(mode="after")
    def _validate_result_artifact(self) -> SubagentRunCompletedEvent:
        if self.result_artifact_id not in self.artifact_ids:
            raise ValueError("result_artifact_id must be present in artifact_ids")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("artifact_ids must be unique")
        handoff = self.result_handoff
        terminal = handoff.child_terminal_reference
        event_usage = (
            ModelTokenUsageFact.model_validate(self.token_usage)
            if self.token_usage is not None
            else None
        )
        if (
            terminal.child_runtime_session_id != self.child_runtime_session_id
            or terminal.child_run_id != self.child_run_id
            or handoff.result_id != self.result_id
            or handoff.summary != self.summary
            or handoff.result_artifact_id != self.result_artifact_id
            or handoff.artifact_ids != tuple(self.artifact_ids)
            or handoff.token_usage != event_usage
            or handoff.tool_call_count != (self.tool_call_count or 0)
        ):
            raise ValueError("SubagentRunCompletedEvent result handoff mismatch")
        return self


class SubagentRunFailedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_FAILED] = EventType.SUBAGENT_RUN_FAILED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None
    child_terminal_reference: ChildNativeTerminalReferenceFact | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentRunFailedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if (
            self.child_terminal_reference is not None
            and self.child_terminal_reference.child_runtime_session_id
            != self.child_runtime_session_id
        ):
            raise ValueError("failed child terminal reference attribution mismatch")
        return self


class SubagentRunCancelledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RUN_CANCELLED] = EventType.SUBAGENT_RUN_CANCELLED
    subagent_run_id: str
    parent_runtime_session_id: str
    child_runtime_session_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    cancelled_by: Literal["user", "parent_agent", "runtime", "host_shutdown"]
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None
    child_terminal_reference: ChildNativeTerminalReferenceFact | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentRunCancelledEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if (
            self.child_terminal_reference is not None
            and self.child_terminal_reference.child_runtime_session_id
            != self.child_runtime_session_id
        ):
            raise ValueError("cancelled child terminal reference attribution mismatch")
        return self


class SubagentEdgeRecordedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_EDGE_RECORDED] = EventType.SUBAGENT_EDGE_RECORDED
    edge_id: str
    edge_kind: Literal[
        "spawn", "send", "followup", "wait", "cancel", "result", "suspend", "resume"
    ]
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    subagent_run_id: str
    child_runtime_session_id: str
    child_run_id: str | None = None
    source_context_id: str | None = None
    source_model_call_index: int | None = None
    source_tool_call_id: str | None = None
    source_tool_name: str | None = None
    target_context_id: str | None = None
    payload_artifact_id: str | None = None
    result_id: str | None = None
    result_artifact_id: str | None = None
    returned_to_tool_call_id: str | None = None


class SubagentResultDeliveredEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_DELIVERED] = (
        EventType.SUBAGENT_RESULT_DELIVERED
    )
    subagent_run_id: str
    parent_runtime_session_id: str
    parent_run_id: str
    parent_turn_id: str | None = None
    parent_reply_id: str | None = None
    context_id: str
    model_call_index: int
    section_id: str
    delivery_kind: Literal["internal_section"] = "internal_section"
    result_id: str
    result_artifact_id: str
    summary: str

    @model_validator(mode="after")
    def _validate_delivery_join(self) -> SubagentResultDeliveredEvent:
        if self.model_call_index < 0:
            raise ValueError("model_call_index must be >= 0")
        if not self.context_id or not self.section_id or not self.result_artifact_id:
            raise ValueError("delivery join fields must be non-empty")
        return self


class SubagentTaskCreatedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_CREATED] = EventType.SUBAGENT_TASK_CREATED
    task_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    task_key: str | None = None
    label: str | None = None
    profile_id: str
    display_role: str | None = None
    objective_preview: str
    objective_artifact_id: str
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_task_creation(self) -> SubagentTaskCreatedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if self.task_id in self.depends_on:
            raise ValueError("task cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on must be unique")
        return self


class SubagentTaskScheduledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_SCHEDULED] = EventType.SUBAGENT_TASK_SCHEDULED
    task_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    schedule_reason: Literal["immediate", "dependency_satisfied", "manual"] = (
        "immediate"
    )

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentTaskScheduledEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentTaskStartedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_STARTED] = EventType.SUBAGENT_TASK_STARTED
    task_id: str
    subagent_run_id: str
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    run_index: int = 1
    spawn_initiator_kind: Literal["tool_call", "scheduler", "dependency_satisfied"]
    spawn_initiator_id: str

    @model_validator(mode="after")
    def _validate_task_start(self) -> SubagentTaskStartedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        if self.run_index != 1:
            raise ValueError("V1 task-backed run_index must be 1")
        if not self.spawn_initiator_id:
            raise ValueError("spawn_initiator_id must be non-empty")
        return self


class SubagentTaskBlockedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_BLOCKED] = EventType.SUBAGENT_TASK_BLOCKED
    task_id: str
    status: Literal["waiting_dependency", "blocked_dependency_failed"]
    blocked_reason: Literal["waiting_dependency", "dependency_failed"]
    blocked_by_task_ids: list[str] = Field(default_factory=list)
    dependency_status_snapshot: dict[str, str] = Field(default_factory=dict)
    dependency_terminal_event_ids: dict[str, str] = Field(default_factory=dict)
    dependency_generation: int | None = None

    @model_validator(mode="after")
    def _validate_dependency_facts(self) -> SubagentTaskBlockedEvent:
        blocked_ids = set(self.blocked_by_task_ids)
        if len(blocked_ids) != len(self.blocked_by_task_ids):
            raise ValueError("blocked_by_task_ids must be unique")
        if set(self.dependency_status_snapshot) != blocked_ids:
            raise ValueError(
                "dependency_status_snapshot keys must match blocked_by_task_ids"
            )
        terminal = self.status == "blocked_dependency_failed"
        if terminal:
            if self.blocked_reason != "dependency_failed" or not blocked_ids:
                raise ValueError(
                    "terminal dependency block requires failed dependency facts"
                )
            if set(self.dependency_terminal_event_ids) != blocked_ids:
                raise ValueError(
                    "dependency_terminal_event_ids keys must match blocked_by_task_ids"
                )
            if self.dependency_generation is None or self.dependency_generation < 0:
                raise ValueError(
                    "terminal dependency block requires dependency_generation"
                )
        elif (
            self.blocked_reason != "waiting_dependency"
            or self.dependency_terminal_event_ids
            or self.dependency_generation is not None
        ):
            raise ValueError(
                "waiting dependency block cannot carry terminal dependency facts"
            )
        return self


class SubagentTaskCompletedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_COMPLETED] = EventType.SUBAGENT_TASK_COMPLETED
    task_id: str
    subagent_run_id: str
    result_id: str
    primary_result_artifact_id: str
    result_source: Literal["explicit", "inferred"] = "inferred"


class SubagentTaskFailedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_FAILED] = EventType.SUBAGENT_TASK_FAILED
    task_id: str
    subagent_run_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentTaskFailedEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentTaskCancelledEvent(EventBase):
    type: Literal[EventType.SUBAGENT_TASK_CANCELLED] = EventType.SUBAGENT_TASK_CANCELLED
    task_id: str
    subagent_run_id: str | None = None
    reason_code: str
    reason_message: str | None = None
    cancelled_by: Literal["user", "parent_agent", "runtime", "host_shutdown"]
    batch_id: str | None = None
    create_tool_call_id: str | None = None
    repair_id: str | None = None

    @model_validator(mode="after")
    def _validate_creation_attribution(self) -> SubagentTaskCancelledEvent:
        if (self.batch_id is None) != (self.create_tool_call_id is None):
            raise ValueError(
                "batch_id and create_tool_call_id must be provided together"
            )
        return self


class SubagentPhaseReportedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_PHASE_REPORTED] = EventType.SUBAGENT_PHASE_REPORTED
    subagent_run_id: str
    task_id: str | None = None
    phase: str
    message: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    source_tool_call_id: str | None = None


class SubagentResultSubmittedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_SUBMITTED] = (
        EventType.SUBAGENT_RESULT_SUBMITTED
    )
    subagent_run_id: str
    task_id: str | None = None
    result_id: str
    summary: str
    output_preview: str | None = None
    result_artifact_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    result_source: Literal["explicit"] = "explicit"
    source_tool_call_id: str = Field(min_length=1)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_result_artifact(self) -> SubagentResultSubmittedEvent:
        if self.result_artifact_id not in self.artifact_ids:
            raise ValueError("result_artifact_id must be present in artifact_ids")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("artifact_ids must be unique")
        return self


class SubagentResultConsumedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_RESULT_CONSUMED] = (
        EventType.SUBAGENT_RESULT_CONSUMED
    )
    consumption_id: str
    consumer_tool_call_id: str
    kind: Literal["wait_run", "wait_task"]
    task_id: str | None = None
    subagent_run_id: str | None = None
    result_id: str | None = None
    consumed_status: Literal[
        "completed", "failed", "cancelled", "blocked_dependency_failed"
    ]
    terminal_event_id: str | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_consumption(self) -> SubagentResultConsumedEvent:
        if self.task_id is None and self.subagent_run_id is None:
            raise ValueError("consumption requires task_id or subagent_run_id")
        if self.kind == "wait_run" and self.subagent_run_id is None:
            raise ValueError("wait_run consumption requires subagent_run_id")
        if self.kind == "wait_task" and self.task_id is None:
            raise ValueError("wait_task consumption requires task_id")
        if self.result_id is None and self.terminal_event_id is None:
            raise ValueError("consumption without result_id requires terminal_event_id")
        if self.consumed_status == "completed" and self.result_id is None:
            raise ValueError("completed consumption requires result_id")
        return self


class SubagentGraphCheckpointCommittedEvent(EventBase):
    type: Literal[EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED] = (
        EventType.SUBAGENT_GRAPH_CHECKPOINT_COMMITTED
    )
    checkpoint: SubagentGraphCheckpointStateFact
    artifact: SubagentGraphCheckpointArtifactFact

    @model_validator(mode="after")
    def _checkpoint_identity(self) -> "SubagentGraphCheckpointCommittedEvent":
        if self.artifact.checkpoint_state != self.checkpoint:
            raise ValueError("checkpoint artifact state differs from event state")
        return self


class TranscriptProjectionCheckpointIntentEvent(EventBase):
    type: Literal[EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT] = (
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT
    )
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scope: TranscriptProjectionScopeFact
    source_ledger_materialization_generation: int = Field(ge=0)
    source_consumer_horizon_revision: int = Field(ge=0)
    frozen_ledger_through_sequence: int = Field(ge=0)
    frozen_ledger_continuity_accumulator: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    maintenance_reservation_id: str = Field(min_length=1, max_length=128)
    intent_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    terminal_contract: CheckpointTerminalContractFact

    @model_validator(mode="after")
    def _scope_identity(self) -> "TranscriptProjectionCheckpointIntentEvent":
        if self.scope.run_id != self.run_id:
            raise ValueError("checkpoint intent scope run mismatch")
        return self


class TranscriptProjectionCheckpointCommittedEvent(EventBase):
    type: Literal[EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED] = (
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED
    )
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_intent_event_identity: StableEventIdentityFact
    barrier_installed_event_identity: StableEventIdentityFact
    checkpoint: TranscriptProjectionCheckpointCandidateFact
    terminal_contract_id: str = Field(min_length=1, max_length=128)
    terminal_contract_version: str = Field(min_length=1, max_length=64)
    terminal_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _candidate_identity(
        self,
    ) -> "TranscriptProjectionCheckpointCommittedEvent":
        if (
            self.checkpoint.checkpoint_id != self.checkpoint_id
            or self.checkpoint.candidate_fingerprint
            != self.checkpoint_candidate_fingerprint
            or self.checkpoint.scope.run_id != self.run_id
        ):
            raise ValueError("checkpoint committed candidate identity mismatch")
        return self


class TranscriptProjectionCheckpointFailedEvent(EventBase):
    type: Literal[EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED] = (
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED
    )
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_intent_event_identity: StableEventIdentityFact
    barrier_installed_event_identity: StableEventIdentityFact
    terminal_contract_id: str = Field(min_length=1, max_length=128)
    terminal_contract_version: str = Field(min_length=1, max_length=64)
    terminal_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stable_reason_code: CheckpointFailureReasonCode
    diagnostics: tuple[CheckpointTerminalDiagnosticFact, ...] = Field(max_length=8)


class TranscriptProjectionCheckpointCancelledEvent(EventBase):
    type: Literal[EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED] = (
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED
    )
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_intent_event_identity: StableEventIdentityFact
    barrier_installed_event_identity: StableEventIdentityFact
    terminal_contract_id: str = Field(min_length=1, max_length=128)
    terminal_contract_version: str = Field(min_length=1, max_length=64)
    terminal_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cancellation_source: CheckpointCancellationSource
    stable_reason_code: CheckpointCancellationReasonCode


class TranscriptProjectionCheckpointRecoveredInterruptedEvent(EventBase):
    type: Literal[EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED] = (
        EventType.TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED
    )
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_intent_event_identity: StableEventIdentityFact
    barrier_installed_event_identity: StableEventIdentityFact
    terminal_contract_id: str = Field(min_length=1, max_length=128)
    terminal_contract_version: str = Field(min_length=1, max_length=64)
    terminal_contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reopen_ledger_high_water: int = Field(ge=0)
    stable_reason_code: Literal["checkpoint_recovered_interrupted"] = (
        "checkpoint_recovered_interrupted"
    )


class LedgerMaterializationAccountGenesisEvent(EventBase):
    type: Literal[EventType.LEDGER_MATERIALIZATION_ACCOUNT_GENESIS] = (
        EventType.LEDGER_MATERIALIZATION_ACCOUNT_GENESIS
    )
    genesis: LedgerMaterializationAccountGenesisFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state: LedgerMaterializationAccountStateFact

    @model_validator(mode="after")
    def _result_join(self) -> "LedgerMaterializationAccountGenesisEvent":
        if (
            self.genesis.runtime_session_id
            != self.resulting_account_state.runtime_session_id
            or self.transition.after_account_state_fingerprint
            != self.resulting_account_state.account_state_fingerprint
        ):
            raise ValueError("ledger genesis resulting account mismatch")
        return self


class LedgerMaterializationConsumerRegisteredEvent(EventBase):
    type: Literal[EventType.LEDGER_MATERIALIZATION_CONSUMER_REGISTERED] = (
        EventType.LEDGER_MATERIALIZATION_CONSUMER_REGISTERED
    )
    consumer: LedgerMaterializationConsumerHorizonFact
    cause: LedgerMaterializationConsumerRegistrationCauseFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class LedgerMaterializationConsumerHorizonAdvancedEvent(EventBase):
    type: Literal[EventType.LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED] = (
        EventType.LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED
    )
    previous_horizon: LedgerMaterializationConsumerHorizonFact
    resulting_horizon: LedgerMaterializationConsumerHorizonFact
    cause: CheckpointConsumerCauseFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _horizon_identity(
        self,
    ) -> "LedgerMaterializationConsumerHorizonAdvancedEvent":
        before = self.previous_horizon
        after = self.resulting_horizon
        if (
            before.runtime_session_id != after.runtime_session_id
            or before.consumer_kind != after.consumer_kind
            or before.consumer_id != after.consumer_id
            or after.through_sequence <= before.through_sequence
        ):
            raise ValueError("consumer horizon transition is invalid")
        return self


class LedgerMaterializationConsumerRetiredEvent(EventBase):
    type: Literal[EventType.LEDGER_MATERIALIZATION_CONSUMER_RETIRED] = (
        EventType.LEDGER_MATERIALIZATION_CONSUMER_RETIRED
    )
    retired_horizon: LedgerMaterializationConsumerHorizonFact
    cause: ConsumerRetirementCauseFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class LedgerMaterializationGenerationAdvancedEvent(EventBase):
    type: Literal[EventType.LEDGER_MATERIALIZATION_GENERATION_ADVANCED] = (
        EventType.LEDGER_MATERIALIZATION_GENERATION_ADVANCED
    )
    previous_generation: LedgerMaterializationGenerationFact
    resulting_generation: LedgerMaterializationGenerationFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PhysicalOperationReservationCreatedEvent(EventBase):
    type: Literal[EventType.PHYSICAL_OPERATION_RESERVATION_CREATED] = (
        EventType.PHYSICAL_OPERATION_RESERVATION_CREATED
    )
    reservation: PhysicalOperationReservationFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PhysicalOperationChargeAppliedEvent(EventBase):
    type: Literal[EventType.PHYSICAL_OPERATION_CHARGE_APPLIED] = (
        EventType.PHYSICAL_OPERATION_CHARGE_APPLIED
    )
    charge: PhysicalOperationChargeAppliedFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PhysicalOperationReservationSuspendedEvent(EventBase):
    type: Literal[EventType.PHYSICAL_OPERATION_RESERVATION_SUSPENDED] = (
        EventType.PHYSICAL_OPERATION_RESERVATION_SUSPENDED
    )
    suspension: PhysicalOperationSuspensionTailFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PhysicalOperationReservationSettledEvent(EventBase):
    type: Literal[EventType.PHYSICAL_OPERATION_RESERVATION_SETTLED] = (
        EventType.PHYSICAL_OPERATION_RESERVATION_SETTLED
    )
    settlement: PhysicalOperationSettlementFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CheckpointDispatchBarrierInstalledEvent(EventBase):
    type: Literal[EventType.CHECKPOINT_DISPATCH_BARRIER_INSTALLED] = (
        EventType.CHECKPOINT_DISPATCH_BARRIER_INSTALLED
    )
    barrier: CheckpointDispatchBarrierFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CheckpointDispatchBarrierReleasedEvent(EventBase):
    type: Literal[EventType.CHECKPOINT_DISPATCH_BARRIER_RELEASED] = (
        EventType.CHECKPOINT_DISPATCH_BARRIER_RELEASED
    )
    release: CheckpointDispatchBarrierReleaseFact
    transition: LedgerMaterializationAccountTransitionFact
    resulting_account_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CustomEvent(EventBase):
    type: Literal[EventType.CUSTOM] = EventType.CUSTOM
    name: str
    value: dict[str, Any] = Field(default_factory=dict)


AgentEvent: TypeAlias = (
    RunStartEvent
    | ContextWindowOpenedEvent
    | ContextWindowClosedEvent
    | ContextWindowCompactionStartedEvent
    | ContextWindowCompactionCompletedEvent
    | ContextWindowCompactionFailedEvent
    | ContextProjectionRewritePageEvent
    | RolloutBudgetAccountOpenedEvent
    | RolloutBudgetAccountClosedEvent
    | ChildRolloutSubaccountClosedEvent
    | RolloutBudgetReservationCreatedEvent
    | RolloutBudgetReservationSettledEvent
    | RolloutPhaseTransitionedEvent
    | SubagentRolloutBudgetResolvedEvent
    | McpCapabilitySnapshotInstalledEvent
    | RunInteractionResumeBoundaryEvent
    | CapabilityExposureResolvedEvent
    | RunEndEvent
    | ReplyStartEvent
    | ReplyEndEvent
    | RunErrorEvent
    | ContextCompiledEvent
    | CapabilityGateDecisionEvent
    | ModelCallStartEvent
    | ProviderInputGenerationStartedEvent
    | ProviderInputAppendCommittedEvent
    | ExistingGenerationPreparationAbandonedEvent
    | ScopedGenerationPreparationAbandonedEvent
    | ProviderInputGenerationRolloverResolvedEvent
    | ProviderInputGenerationClosedEvent
    | ModelCallTerminalProjectionCommittedEvent
    | ModelCallEndEvent
    | ProviderModelStreamErrorEvent
    | ModelCallControlDispositionResolvedEvent
    | ModelCallRejectedEvent
    | TextBlockStartEvent
    | TextBlockSegmentEvent
    | TextBlockEndEvent
    | DataBlockStartEvent
    | DataBlockSegmentEvent
    | DataBlockEndEvent
    | ThinkingBlockStartEvent
    | ThinkingBlockSegmentEvent
    | ThinkingBlockEndEvent
    | HintBlockEvent
    | ToolCallStartEvent
    | ToolCallArgumentsSegmentEvent
    | ToolCallEndEvent
    | ToolResultStartEvent
    | ToolResultTextDeltaEvent
    | ToolResultDataDeltaEvent
    | ToolResultTerminalProjectionCommittedEvent
    | ToolResultEndEvent
    | ToolExecutionSuspendedEvent
    | RequireUserConfirmEvent
    | UserConfirmResultEvent
    | RequireExternalExecutionEvent
    | ExternalExecutionResultEvent
    | TerminalProcessCompletedEvent
    | PlanModeEnteredEvent
    | PlanQuestionAskedEvent
    | PlanQuestionAnsweredEvent
    | PlanExitRequestedEvent
    | PlanExitResolvedEvent
    | PlanModeExitedEvent
    | MemoryCandidateProposedEvent
    | MemoryWriteResultEvent
    | MemoryWriteFailedEvent
    | MemoryReflectionCompletedEvent
    | MemoryReflectionFailedEvent
    | MemorySupersededEvent
    | MemoryContradictionLinkedEvent
    | MemoryMarkedStaleEvent
    | MemoryMaintenanceProposedEvent
    | MemoryMaintenanceAppliedEvent
    | MemoryMaintenanceRejectedEvent
    | MemoryGovernanceBatchPreparedEvent
    | MemoryGovernanceBatchCompletedEvent
    | MemoryGovernanceBatchFailedEvent
    | MemoryGovernanceBatchBlockedEvent
    | MemoryCandidateEvidenceRejectedEvent
    | ProjectionRequestedEvent
    | ProjectionReadyEvent
    | ProjectionFailedEvent
    | ContextCompactionStartedEvent
    | ContextCompactionCompletedEvent
    | ContextCompactionMemoryCandidatesProposedEvent
    | ContextCompactionFailedEvent
    | SubagentRunStartedEvent
    | SubagentMessageSentEvent
    | SubagentRunSuspendedEvent
    | SubagentRunCompletedEvent
    | SubagentRunFailedEvent
    | SubagentRunCancelledEvent
    | SubagentEdgeRecordedEvent
    | SubagentResultDeliveredEvent
    | SubagentTaskCreatedEvent
    | SubagentTaskScheduledEvent
    | SubagentTaskStartedEvent
    | SubagentTaskBlockedEvent
    | SubagentTaskCompletedEvent
    | SubagentTaskFailedEvent
    | SubagentTaskCancelledEvent
    | SubagentPhaseReportedEvent
    | SubagentResultSubmittedEvent
    | SubagentResultConsumedEvent
    | SubagentGraphCheckpointCommittedEvent
    | TranscriptProjectionCheckpointIntentEvent
    | TranscriptProjectionCheckpointCommittedEvent
    | TranscriptProjectionCheckpointFailedEvent
    | TranscriptProjectionCheckpointCancelledEvent
    | TranscriptProjectionCheckpointRecoveredInterruptedEvent
    | LedgerMaterializationAccountGenesisEvent
    | LedgerMaterializationConsumerRegisteredEvent
    | LedgerMaterializationConsumerHorizonAdvancedEvent
    | LedgerMaterializationConsumerRetiredEvent
    | LedgerMaterializationGenerationAdvancedEvent
    | PhysicalOperationReservationCreatedEvent
    | PhysicalOperationChargeAppliedEvent
    | PhysicalOperationReservationSuspendedEvent
    | PhysicalOperationReservationSettledEvent
    | CheckpointDispatchBarrierInstalledEvent
    | CheckpointDispatchBarrierReleasedEvent
    | CustomEvent
)
