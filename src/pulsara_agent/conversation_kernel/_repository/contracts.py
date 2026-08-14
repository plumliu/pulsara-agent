"""Pure contracts and candidate builders for the repository facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4
from pulsara_agent.conversation_kernel.contracts import BlobContent, CanonicalContent, CommittedEventDraft, CommittedEventSubject, EntryKind, HostWriterGuard, InlineContent, JobAttemptClaimGuard, JobSafetyClass, canonical_digest
from pulsara_agent.conversation_kernel.job_catalog import MEMORY_GOVERNANCE, job_handler_contract
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.ports.artifact import ToolOutputArtifactDisposition, ToolOutputArtifactUnavailabilityReason, ToolResultDisplayKind
from pulsara_agent.ports.tool_execution import FrozenToolJsonDict, ToolOutputSourceCoverage, ToolOutputSourceCoverageReason, freeze_tool_json_object
from pulsara_agent.primitives.context import FrozenJsonObjectFact, thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import (
    ToolObservationOrigin,
    canonical_utc_timestamp,
    normalize_observation_duration,
)
from pulsara_agent.primitives.plan_workflow import PLAN_ENTRY_CONTRACT, ExtractedPlanDraft, PlanDraftDecision, PlanHandoffKind, PlanInteractionBinding, PlanInteractionKind, PlanQuestionAnswerKind, PlanQuestionContent, PlanWorkflowStatus, require_plan_interaction_contract
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType, SubjectSlot
from pulsara_agent.conversation_kernel.steer import PromptIngressWriteRejection

INLINE_CONTENT_LIMIT = STAGE2_LIMITS.inline_content_hard_bytes


COMMITTED_EVENT_PAYLOAD_LIMIT = STAGE2_LIMITS.committed_payload_hard_bytes


MAXIMUM_PLAN_INTERACTIONS_PER_TURN = 16


MAXIMUM_PLAN_DRAFT_REVISIONS_PER_WORKFLOW = 8


MAXIMUM_PLAN_INTERACTIONS_PER_WORKFLOW = 64


class ConversationKernelConflict(RuntimeError):
    """A stable identity already names a different semantic fact."""


class PromptIngressRejected(ConversationKernelConflict):
    def __init__(self, reason: PromptIngressWriteRejection) -> None:
        self.reason = reason
        super().__init__(reason.value)


class PlanToolBatchDisposition(StrEnum):
    APPLY = "APPLY"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"


class PlanDraftIdentityConflict(ConversationKernelConflict):
    """A requested Plan draft digest no longer names the canonical body."""


class StaleHostWriter(RuntimeError):
    """The supplied Host generation/owner no longer holds the session lease."""


class StaleJobClaim(RuntimeError):
    """The supplied job-attempt claim is absent, expired, or no longer active."""


class JobAttemptTerminalized(RuntimeError):
    """The requested physical admission was rejected and durably terminalized."""


class JobCancellationRequested(RuntimeError):
    """The aggregate accepted cancellation before another physical step."""


@dataclass(frozen=True, slots=True)
class AssistantTextBlock:
    block_id: str
    text: InlineContent | BlobContent


@dataclass(frozen=True, slots=True)
class AssistantDataBlock:
    block_id: str
    data: InlineContent | BlobContent


@dataclass(frozen=True, slots=True)
class AssistantToolCallBlock:
    block_id: str
    tool_call_id: str
    tool_name: str
    arguments: FrozenJsonObjectFact

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, FrozenJsonObjectFact):
            raise TypeError("assistant tool-call arguments must be recursively frozen")


AssistantBlock = AssistantTextBlock | AssistantDataBlock | AssistantToolCallBlock


@dataclass(frozen=True, slots=True)
class AcceptedEntry:
    entry_id: str
    turn_id: str
    entry_sequence: int
    event_sequence: int
    turn_completed: bool = False


class TurnAdmissionConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    HISTORICAL_TERMINAL = "HISTORICAL_TERMINAL"
    CONFLICT = "CONFLICT"


class ToolRemoteIdentityConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class PreparedRootTurnAdmission:
    session_id: str
    command_id: str
    turn_id: str
    entry_id: str
    context_binding_revision_id: str
    permission_snapshot_id: str
    requested_permission_mode: PermissionMode
    content: CanonicalContent
    occurred_at: datetime
    actor_kind: str
    actor_id: str
    semantic_digest: str
    event: CommittedEventDraft
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        payload = _root_turn_admission_payload(
            session_id=self.session_id,
            command_id=self.command_id,
            turn_id=self.turn_id,
            entry_id=self.entry_id,
            context_binding_revision_id=self.context_binding_revision_id,
            permission_snapshot_id=self.permission_snapshot_id,
            requested_permission_mode=self.requested_permission_mode,
            content=self.content,
            occurred_at=self.occurred_at,
            actor_kind=self.actor_kind,
            actor_id=self.actor_id,
        )
        expected_digest = canonical_digest(
            "pulsara:submit-prompt-command:v2", payload
        )
        expected_fingerprint = canonical_digest(
            "pulsara:prepared-root-turn-admission:v1",
            {**payload, "semantic_digest": expected_digest},
        )
        if (
            not self.session_id
            or not self.command_id
            or not self.turn_id
            or not self.entry_id
            or not self.context_binding_revision_id
            or not self.permission_snapshot_id
            or self.semantic_digest != expected_digest
            or self.candidate_fingerprint != expected_fingerprint
            or self.event.event_id
            != _stable_identity(
                "event", expected_fingerprint, "UserMessageAccepted"
            )
            or self.event.event_type is not CommittedEventType.USER_MESSAGE_ACCEPTED
            or self.event.subject
            != CommittedEventSubject(SubjectSlot.ENTRY, self.entry_id)
            or self.event.actor_kind != self.actor_kind
            or self.event.actor_id != self.actor_id
            or self.event.sensitivity_class != "PUBLIC"
            or self.event.projection_profile != "DEFAULT"
            or self.event.occurred_at != self.occurred_at
            or dict(self.event.payload)
            != {"entry_kind": EntryKind.USER_MESSAGE.value}
        ):
            raise ValueError("prepared ROOT turn admission is invalid")


@dataclass(frozen=True, slots=True)
class PreparedSubagentTurnAdmission:
    session_id: str
    task_id: str
    turn_id: str
    entry_id: str
    context_binding_revision_id: str
    permission_snapshot_id: str
    content: CanonicalContent
    occurred_at: datetime
    actor_id: str
    event: CommittedEventDraft
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        payload = _subagent_turn_admission_payload(
            session_id=self.session_id,
            task_id=self.task_id,
            turn_id=self.turn_id,
            entry_id=self.entry_id,
            context_binding_revision_id=self.context_binding_revision_id,
            permission_snapshot_id=self.permission_snapshot_id,
            content=self.content,
            occurred_at=self.occurred_at,
            actor_id=self.actor_id,
        )
        expected = canonical_digest(
            "pulsara:prepared-subagent-turn-admission:v1", payload
        )
        if (
            not self.session_id
            or not self.task_id
            or not self.turn_id
            or not self.entry_id
            or not self.context_binding_revision_id
            or not self.permission_snapshot_id
            or self.candidate_fingerprint != expected
            or self.event.event_id
            != _stable_identity("event", expected, "UserMessageAccepted")
            or self.event.event_type is not CommittedEventType.USER_MESSAGE_ACCEPTED
            or self.event.subject
            != CommittedEventSubject(SubjectSlot.ENTRY, self.entry_id)
            or self.event.actor_kind != "runtime"
            or self.event.actor_id != self.actor_id
            or self.event.sensitivity_class != "PUBLIC"
            or self.event.projection_profile != "DEFAULT"
            or self.event.occurred_at != self.occurred_at
            or dict(self.event.payload) != {"source": "SUBAGENT_TASK_OBJECTIVE"}
        ):
            raise ValueError("prepared subagent turn admission is invalid")


@dataclass(frozen=True, slots=True)
class TurnAdmissionConfirmation:
    kind: TurnAdmissionConfirmationKind
    accepted: AcceptedEntry | None = None

    def __post_init__(self) -> None:
        carries_entry = self.kind in {
            TurnAdmissionConfirmationKind.FULL,
            TurnAdmissionConfirmationKind.HISTORICAL_TERMINAL,
        }
        if carries_entry != (self.accepted is not None):
            raise ValueError("turn admission confirmation union is invalid")


@dataclass(frozen=True, slots=True)
class PreparedToolRemoteIdentityPublication:
    session_id: str
    attempt_id: str
    remote_identity: str
    occurred_at: datetime
    actor_id: str
    event: CommittedEventDraft
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        payload = _tool_remote_identity_publication_payload(
            session_id=self.session_id,
            attempt_id=self.attempt_id,
            remote_identity=self.remote_identity,
            occurred_at=self.occurred_at,
            actor_id=self.actor_id,
        )
        expected = canonical_digest(
            "pulsara:prepared-tool-remote-identity-publication:v1", payload
        )
        identity_bytes = self.remote_identity.encode("utf-8")
        if (
            not self.session_id
            or not self.attempt_id
            or not self.remote_identity
            or len(identity_bytes) > 4096
            or not self.actor_id
            or self.candidate_fingerprint != expected
            or self.event.event_id
            != _stable_identity(
                "event",
                expected,
                CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED.value,
            )
            or self.event.event_type
            is not CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED
            or self.event.subject
            != CommittedEventSubject(SubjectSlot.TOOL_ATTEMPT, self.attempt_id)
            or self.event.actor_kind != "tool"
            or self.event.actor_id != self.actor_id
            or self.event.sensitivity_class != "PUBLIC"
            or self.event.projection_profile != "DEFAULT"
            or self.event.occurred_at != self.occurred_at
            or dict(self.event.payload)
            != {
                "remote_identity_utf8_bytes": len(identity_bytes),
                "remote_identity_digest": "sha256:"
                + sha256(identity_bytes).hexdigest(),
            }
        ):
            raise ValueError("prepared tool remote identity publication is invalid")


class ToolResultSideBranchKind(StrEnum):
    NONE = "NONE"
    MEMORY_PROPOSAL = "MEMORY_PROPOSAL"


@dataclass(frozen=True, slots=True)
class NoToolResultSideBranch:
    branch_kind: ToolResultSideBranchKind = ToolResultSideBranchKind.NONE


@dataclass(frozen=True, slots=True)
class PreparedMemoryProposalSideBranch:
    memory_candidate_id: str
    proposal_kind: str
    proposal_payload: FrozenToolJsonDict
    candidate_semantic_digest: str
    governance_job_id: str
    intent_payload: FrozenToolJsonDict
    intent_digest: str
    automatic_intent_key: str
    retry_policy_id: str
    retry_policy_version: int
    maximum_attempts: int
    attempt_timeout_ms: int
    provider_input_token_limit_per_attempt: int
    provider_output_token_limit_per_attempt: int
    next_eligible_at: datetime
    job_queued_occurrence: CommittedEventDraft
    branch_kind: ToolResultSideBranchKind = ToolResultSideBranchKind.MEMORY_PROPOSAL
    job_handler_type: str = MEMORY_GOVERNANCE
    intent_schema_version: str = "memory_governance.v1"
    safety_class: str = "RETRY_SAFE"
    initial_status: str = "PENDING"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "proposal_payload", freeze_tool_json_object(self.proposal_payload)
        )
        object.__setattr__(
            self, "intent_payload", freeze_tool_json_object(self.intent_payload)
        )
        if self.proposal_kind not in {
            "FACT",
            "PREFERENCE",
            "RELATION",
            "CORRECTION",
            "LIFECYCLE",
        }:
            raise ValueError("memory proposal kind is not closed")
        if (
            min(
                self.maximum_attempts,
                self.attempt_timeout_ms,
                self.provider_input_token_limit_per_attempt,
                self.provider_output_token_limit_per_attempt,
            )
            < 1
        ):
            raise ValueError("memory proposal job bounds must be positive")


ToolResultSideBranch = NoToolResultSideBranch | PreparedMemoryProposalSideBranch


@dataclass(frozen=True, slots=True)
class PreparedToolResultAcceptance:
    session_id: str
    workspace_id: str
    result_id: str
    result_entry_id: str
    turn_id: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str | None
    result_state: str
    canonical_preview_content: InlineContent
    artifact_disposition: ToolOutputArtifactDisposition
    artifact_id: str | None
    artifact_blob_descriptor: BlobContent | None
    source_coverage: ToolOutputSourceCoverage
    display_kind: ToolResultDisplayKind
    source_coverage_reason: ToolOutputSourceCoverageReason | None
    artifact_unavailability_reason: ToolOutputArtifactUnavailabilityReason | None
    observed_at: datetime
    observation_duration_microseconds: int | None
    observation_origin_kind: ToolObservationOrigin
    trusted_tool_reported_duration_microseconds: int | None
    actor_id: str
    tool_result_occurrence: CommittedEventDraft
    side_branch: ToolResultSideBranch
    candidate_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.result_id,
                self.result_entry_id,
                self.turn_id,
                self.assistant_entry_id,
                self.tool_call_id,
                self.result_state,
                self.actor_id,
            )
        ):
            raise ValueError("prepared tool result identity is incomplete")
        if not isinstance(self.canonical_preview_content, InlineContent):
            raise TypeError("prepared tool result preview must be inline")
        attempted_states = {"SUCCESS", "APPLICATION_ERROR", "SYSTEM_ERROR", "CANCELLED"}
        no_attempt_states = {
            "INVALID_ARGUMENTS",
            "PERMISSION_DENIED",
            "TOOL_UNAVAILABLE",
            "CANCELLED_BEFORE_DISPATCH",
        }
        if (self.attempt_id is None and self.result_state not in no_attempt_states) or (
            self.attempt_id is not None and self.result_state not in attempted_states
        ):
            raise ValueError("prepared tool result attempt/state union is invalid")
        available = self.artifact_disposition in {
            ToolOutputArtifactDisposition.AVAILABLE,
            ToolOutputArtifactDisposition.INCOMPLETE,
        }
        if available != (
            self.artifact_id is not None and self.artifact_blob_descriptor is not None
        ):
            raise ValueError("prepared tool result artifact edge is inconsistent")
        if (self.artifact_disposition is ToolOutputArtifactDisposition.UNAVAILABLE) != (
            self.artifact_unavailability_reason is not None
        ):
            raise ValueError("prepared tool result unavailability is inconsistent")
        if (self.source_coverage is ToolOutputSourceCoverage.COMPLETE) != (
            self.source_coverage_reason is None
        ):
            raise ValueError("prepared tool result coverage is inconsistent")
        if self.canonical_preview_content.size > 65_536:
            raise ValueError("prepared tool result preview exceeds its hard bound")
        canonical_utc_timestamp(self.observed_at)
        if self.observation_duration_microseconds != normalize_observation_duration(
            self.observation_duration_microseconds
        ):
            raise ValueError("prepared tool observation duration is invalid")
        if self.trusted_tool_reported_duration_microseconds != (
            normalize_observation_duration(
                self.trusted_tool_reported_duration_microseconds
            )
        ):
            raise ValueError("prepared trusted tool duration is invalid")
        nonphysical = self.attempt_id is None
        if nonphysical:
            expected_origin = (
                ToolObservationOrigin.PLAN_CONTROL
                if self.result_state in {"SUCCESS", "APPLICATION_ERROR"}
                and self.observation_origin_kind is ToolObservationOrigin.PLAN_CONTROL
                else ToolObservationOrigin.POLICY
            )
            if (
                self.observation_origin_kind is not expected_origin
                or self.observation_duration_microseconds is not None
                or self.trusted_tool_reported_duration_microseconds is not None
            ):
                raise ValueError("prepared nonphysical observation is inconsistent")
        elif self.observation_origin_kind in {
            ToolObservationOrigin.POLICY,
            ToolObservationOrigin.PLAN_CONTROL,
        }:
            raise ValueError("prepared physical result has a nonphysical origin")
        if self.artifact_disposition is ToolOutputArtifactDisposition.NOT_REQUIRED:
            if self.source_coverage is not ToolOutputSourceCoverage.COMPLETE:
                raise ValueError("not-required artifact must own complete source")
        elif self.artifact_disposition is ToolOutputArtifactDisposition.AVAILABLE:
            if self.source_coverage is not ToolOutputSourceCoverage.COMPLETE:
                raise ValueError("available artifact must own complete source")
        elif self.artifact_disposition is ToolOutputArtifactDisposition.INCOMPLETE:
            if self.source_coverage is not ToolOutputSourceCoverage.RETAINED_SNAPSHOT:
                raise ValueError("incomplete artifact must be a retained snapshot")
        blob = self.artifact_blob_descriptor
        if blob is not None and (
            blob.media_type != "text/plain"
            or blob.codec != "utf-8"
            or blob.size > 16 << 20
        ):
            raise ValueError("prepared primary artifact descriptor is invalid")
        expected_event_id = _stable_identity(
            "event", self.result_entry_id, "ToolResultAccepted"
        )
        occurrence = self.tool_result_occurrence
        if (
            occurrence.event_id != expected_event_id
            or occurrence.event_type is not CommittedEventType.TOOL_RESULT_ACCEPTED
            or occurrence.subject
            != CommittedEventSubject(SubjectSlot.ENTRY, self.result_entry_id)
            or occurrence.actor_kind != "tool"
            or occurrence.actor_id != self.actor_id
            or occurrence.sensitivity_class != "PUBLIC"
            or occurrence.projection_profile != "DEFAULT"
            or occurrence.occurred_at != self.observed_at
            or dict(occurrence.payload)
            != {
                "tool_call_id": self.tool_call_id,
                "result_state": self.result_state,
            }
        ):
            raise ValueError("prepared tool result occurrence is not exact")
        if isinstance(self.side_branch, NoToolResultSideBranch):
            if self.side_branch.branch_kind is not ToolResultSideBranchKind.NONE:
                raise ValueError("prepared no-side-branch discriminator is invalid")
        elif isinstance(self.side_branch, PreparedMemoryProposalSideBranch):
            side_event = self.side_branch.job_queued_occurrence
            if (
                self.side_branch.branch_kind
                is not ToolResultSideBranchKind.MEMORY_PROPOSAL
                or side_event.event_id
                != _stable_identity(
                    "event", self.side_branch.governance_job_id, "JobQueued"
                )
                or side_event.event_type is not CommittedEventType.JOB_QUEUED
                or side_event.subject
                != CommittedEventSubject(
                    SubjectSlot.JOB, self.side_branch.governance_job_id
                )
                or side_event.actor_kind != "runtime"
                or side_event.sensitivity_class != "PUBLIC"
                or side_event.projection_profile != "DEFAULT"
                or side_event.occurred_at != self.observed_at
                or dict(side_event.payload)
                != {"handler_type": self.side_branch.job_handler_type}
            ):
                raise ValueError("prepared memory side occurrence is not exact")
        else:
            raise TypeError("prepared tool result side branch is not closed")
        payload = _prepared_tool_result_manifest(self)
        object.__setattr__(
            self,
            "candidate_fingerprint",
            canonical_digest("pulsara:prepared-tool-result-acceptance:v1", payload),
        )


@dataclass(frozen=True, slots=True)
class AcceptedToolAttempt:
    attempt_id: str
    session_id: str
    assistant_entry_id: str
    tool_call_id: str
    permission_snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class AcceptedCapabilityDecision:
    decision_id: str
    decision: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str | None
    result_entry_id: str | None
    permission_snapshot_fingerprint: str


@dataclass(frozen=True, slots=True)
class AcceptedInteractionDecision:
    decision_id: str
    command_id: str
    decision: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str | None
    result_entry_id: str | None
    permission_snapshot_fingerprint: str


class PlanToolControlKind(StrEnum):
    ENTER = "ENTER"
    QUESTION = "QUESTION"
    DRAFT = "DRAFT"


@dataclass(frozen=True, slots=True)
class PreparedPlanBatchCall:
    block_id: str
    tool_call_id: str
    tool_name: str
    result_id: str | None
    result_entry_id: str | None

    def __post_init__(self) -> None:
        if not self.block_id or not self.tool_call_id or not self.tool_name:
            raise ValueError("prepared Plan batch call identity is incomplete")
        if (self.result_id is None) != (self.result_entry_id is None):
            raise ValueError("prepared Plan batch result identity is incomplete")


@dataclass(frozen=True, slots=True)
class PreparedPlanToolBatch:
    session_id: str
    workspace_id: str
    origin_turn_id: str
    assistant_entry_id: str
    selected_call_ordinal: int
    control_kind: PlanToolControlKind
    selected_arguments: FrozenJsonObjectFact
    request_binding: PlanInteractionBinding
    permission_snapshot: FrozenRunPermissionSnapshot
    workflow_id: str
    expected_workflow_revision: int | None
    interaction_id: str | None
    continuation_turn_id: str | None
    continuation_entry_id: str | None
    continuation_context_binding_revision_id: str | None
    calls: tuple[PreparedPlanBatchCall, ...]
    occurred_at: datetime
    actor_id: str
    idempotent_existing: bool = False
    selected_disposition: PlanToolBatchDisposition = PlanToolBatchDisposition.APPLY
    candidate_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selected_arguments, FrozenJsonObjectFact):
            raise TypeError("prepared Plan arguments must be recursively frozen")
        if not 0 <= self.selected_call_ordinal < len(self.calls):
            raise ValueError("prepared Plan selected ordinal is invalid")
        selected = self.calls[self.selected_call_ordinal]
        expected_name = {
            PlanToolControlKind.ENTER: "enter_plan",
            PlanToolControlKind.QUESTION: "ask_plan_question",
            PlanToolControlKind.DRAFT: "exit_plan",
        }[self.control_kind]
        if selected.tool_name != expected_name:
            raise ValueError("prepared Plan selected call kind conflicts")
        if self.control_kind is PlanToolControlKind.ENTER:
            if self.request_binding.identity != PLAN_ENTRY_CONTRACT:
                raise ValueError("Plan entry contract is unavailable")
        else:
            require_plan_interaction_contract(
                self.request_binding, expected_tool_name=expected_name
            )
        if len({item.tool_call_id for item in self.calls}) != len(self.calls):
            raise ValueError("prepared Plan batch call identity is duplicated")
        rejected = self.selected_disposition is not PlanToolBatchDisposition.APPLY
        continuation = (
            not rejected
            and self.control_kind is PlanToolControlKind.ENTER
            and not self.idempotent_existing
        )
        if continuation != all(
            value is not None
            for value in (
                self.continuation_turn_id,
                self.continuation_entry_id,
                self.continuation_context_binding_revision_id,
            )
        ):
            raise ValueError("prepared Plan continuation union is invalid")
        interaction = not rejected and self.control_kind in {
            PlanToolControlKind.QUESTION,
            PlanToolControlKind.DRAFT,
        }
        if interaction != (self.interaction_id is not None):
            raise ValueError("prepared Plan interaction union is invalid")
        if rejected:
            if any(item.result_id is None for item in self.calls):
                raise ValueError("rejected Plan batch result identity is absent")
            if self.idempotent_existing:
                raise ValueError("rejected Plan control cannot be idempotent")
        elif self.control_kind is PlanToolControlKind.QUESTION:
            if selected.result_id is not None:
                raise ValueError("open Plan question cannot preinstall its result")
        elif selected.result_id is None:
            raise ValueError("Plan enter/draft result identity is absent")
        if rejected:
            pass
        elif self.control_kind is PlanToolControlKind.ENTER:
            if self.idempotent_existing:
                if not self.expected_workflow_revision:
                    raise ValueError(
                        "idempotent Plan enter requires an active revision"
                    )
            elif self.expected_workflow_revision is not None:
                raise ValueError("fresh Plan enter cannot target an existing revision")
        elif self.idempotent_existing:
            raise ValueError("only Plan enter may be idempotent")
        elif not self.expected_workflow_revision or self.expected_workflow_revision < 1:
            raise ValueError("Plan interaction workflow revision is invalid")
        payload = {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "origin_turn_id": self.origin_turn_id,
            "assistant_entry_id": self.assistant_entry_id,
            "selected_call_ordinal": self.selected_call_ordinal,
            "control_kind": self.control_kind.value,
            "selected_arguments": thaw_json(self.selected_arguments),
            "request_binding": {
                "id": self.request_binding.contract_id,
                "version": self.request_binding.contract_version,
                "fingerprint": self.request_binding.contract_fingerprint,
            },
            "permission_snapshot": self.permission_snapshot.snapshot_fingerprint,
            "workflow_id": self.workflow_id,
            "expected_workflow_revision": self.expected_workflow_revision,
            "interaction_id": self.interaction_id,
            "continuation": (
                self.continuation_turn_id,
                self.continuation_entry_id,
                self.continuation_context_binding_revision_id,
            ),
            "calls": tuple(
                (
                    item.block_id,
                    item.tool_call_id,
                    item.tool_name,
                    item.result_id,
                    item.result_entry_id,
                )
                for item in self.calls
            ),
            "occurred_at": self.occurred_at.isoformat(),
            "actor_id": self.actor_id,
            "idempotent_existing": self.idempotent_existing,
            "selected_disposition": self.selected_disposition.value,
        }
        object.__setattr__(
            self,
            "candidate_fingerprint",
            canonical_digest("pulsara:prepared-plan-tool-batch:v1", payload),
        )


@dataclass(frozen=True, slots=True)
class AcceptedPlanToolBatch:
    workflow_id: str
    workflow_revision: int
    interaction_id: str | None
    interaction_kind: PlanInteractionKind | None
    question: PlanQuestionContent | None
    draft: ExtractedPlanDraft | None
    selected_result_entry_id: str | None
    continuation_turn_id: str | None
    continuation_entry_id: str | None
    origin_turn_completed: bool


@dataclass(frozen=True, slots=True)
class PlanQuestionAnswer:
    kind: PlanQuestionAnswerKind
    option_ordinal: int | None = None
    free_text: str | None = None

    def __post_init__(self) -> None:
        if self.kind is PlanQuestionAnswerKind.OPTION:
            if (
                self.option_ordinal is None
                or self.option_ordinal < 0
                or self.free_text is not None
            ):
                raise ValueError("Plan option answer union is invalid")
        elif self.option_ordinal is not None or not self.free_text:
            raise ValueError("Plan free-text answer union is invalid")
        if (
            self.free_text is not None
            and len(self.free_text.encode("utf-8")) > 32 * 1024
        ):
            raise ValueError("Plan free-text answer exceeds its bound")


@dataclass(frozen=True, slots=True)
class AcceptedPlanResolution:
    command_id: str
    workflow_id: str
    workflow_status: PlanWorkflowStatus
    interaction_id: str
    interaction_status: str
    resume_permission_mode: PermissionMode
    continuation_turn_id: str | None
    continuation_entry_id: str | None
    handoff_created_at_commit: bool
    workflow_revision: int
    question_result_entry_id: str | None = None
    draft_decision: PlanDraftDecision | None = None


@dataclass(frozen=True, slots=True)
class AcceptedPlanWorkflowCommand:
    command_id: str
    workflow_id: str
    workflow_status: PlanWorkflowStatus
    resume_permission_mode: PermissionMode
    handoff_created_at_commit: bool
    workflow_revision: int

    def __post_init__(self) -> None:
        if not self.command_id or not self.workflow_id or self.workflow_revision < 1:
            raise ValueError("accepted Plan command identity is invalid")


@dataclass(frozen=True, slots=True)
class PlanContinuationInspection:
    turn_id: str
    initial_entry_id: str
    status: str
    workflow_id: str
    interaction_id: str | None
    handoff_kind: PlanHandoffKind
    session_lifecycle: str
    writer_generation: int
    writer_owner_id: str | None


class PlanContinuationDisposition(StrEnum):
    RUNNING_CURRENT_WRITER = "RUNNING_CURRENT_WRITER"
    HISTORICAL_TERMINAL = "HISTORICAL_TERMINAL"
    NOT_OWNED_BY_CURRENT_WRITER = "NOT_OWNED_BY_CURRENT_WRITER"


@dataclass(frozen=True, slots=True)
class ForcePlanExitPhaseOneResult:
    workflow_id: str
    workflow_revision: int
    expected_active_turn_id: str | None
    canonical_interrupted_turn_id: str | None
    terminal_reason: str
    turn_interrupted_at_commit: bool
    interaction_aborted_at_commit: bool


@dataclass(frozen=True, slots=True)
class _EligiblePlanHandoff:
    workflow_id: str
    interaction_id: str | None
    kind: PlanHandoffKind


@dataclass(frozen=True, slots=True)
class AcceptedJobAttempt:
    guard: JobAttemptClaimGuard
    attempt_ordinal: int
    deadline_at: datetime
    handler_type: str = ""
    safety_class: JobSafetyClass = JobSafetyClass.RETRY_SAFE
    intent_payload: Mapping[str, object] | None = None
    provider_input_token_limit: int | None = None
    provider_output_token_limit: int | None = None
    reclaimed_after_expiry: bool = False
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class AcceptedJobSettlement:
    job_id: str
    attempt_id: str
    attempt_status: str
    aggregate_status: str
    retry_scheduled: bool
    next_eligible_at: datetime | None


@dataclass(frozen=True, slots=True)
class AcceptedMemoryCandidate:
    candidate_id: str
    governance_job_id: str


@dataclass(frozen=True, slots=True)
class AcceptedMemoryGovernance:
    candidate_id: str
    decision_id: str
    decision: str
    fact_id: str | None
    relation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryVectorFactSource:
    fact_id: str
    semantic_digest: str
    embedding_text: str


@dataclass(frozen=True, slots=True)
class MemoryVectorSource:
    workspace_id: str
    target_generation: int
    handler_contract_id: str
    handler_contract_version: int
    source_digest: str
    facts: tuple[MemoryVectorFactSource, ...]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _stable_subagent_message_child_id(task_id: str, entry_id: str) -> str:
    digest = sha256(f"{task_id}\0{entry_id}".encode()).hexdigest()
    return f"subagent-message:{digest}"


def _deterministic_retry_due(
    *, accepted_at: datetime, job_id: str, attempt_ordinal: int
) -> datetime:
    delay_seconds = min(300, 2 ** min(attempt_ordinal, 8))
    jitter_millis = (
        int.from_bytes(
            sha256(f"{job_id}:{attempt_ordinal}".encode()).digest()[:2], "big"
        )
        % 1000
    )
    return accepted_at + timedelta(seconds=delay_seconds, milliseconds=jitter_millis)


def _content_columns(content: CanonicalContent) -> tuple[object, ...]:
    if isinstance(content, InlineContent):
        return (
            content.canonical_bytes,
            None,
            content.digest,
            content.size,
            content.media_type,
            content.codec,
        )
    return (
        None,
        content.blob_id,
        content.digest,
        content.size,
        content.media_type,
        content.codec,
    )


def _stable_identity(prefix: str, *parts: str) -> str:
    digest = sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _canonical_content_identity(content: CanonicalContent) -> Mapping[str, object]:
    return {
        "kind": "INLINE" if isinstance(content, InlineContent) else "BLOB",
        "blob_id": None if isinstance(content, InlineContent) else content.blob_id,
        "digest": content.digest,
        "size": content.size,
        "media_type": content.media_type,
        "codec": content.codec,
    }


def _canonical_content_matches_utf8_text(
    content: CanonicalContent, value: str
) -> bool:
    encoded = value.encode("utf-8")
    return (
        content.media_type == "text/plain"
        and content.codec == "utf-8"
        and content.size == len(encoded)
        and content.digest == "sha256:" + sha256(encoded).hexdigest()
        and (
            not isinstance(content, InlineContent)
            or content.canonical_bytes == encoded
        )
    )


def _root_turn_admission_payload(
    *,
    session_id: str,
    command_id: str,
    turn_id: str,
    entry_id: str,
    context_binding_revision_id: str,
    permission_snapshot_id: str,
    requested_permission_mode: PermissionMode,
    content: CanonicalContent,
    occurred_at: datetime,
    actor_kind: str,
    actor_id: str,
) -> Mapping[str, object]:
    return {
        "session_id": session_id,
        "command_id": command_id,
        "turn_id": turn_id,
        "entry_id": entry_id,
        "context_binding_revision_id": context_binding_revision_id,
        "permission_snapshot_id": permission_snapshot_id,
        "requested_permission_mode": requested_permission_mode.value,
        "content": _canonical_content_identity(content),
        "occurred_at": occurred_at.isoformat(),
        "actor_kind": actor_kind,
        "actor_id": actor_id,
    }


def build_prepared_root_turn_admission(
    *,
    session_id: str,
    command_id: str,
    turn_id: str,
    entry_id: str,
    context_binding_revision_id: str,
    permission_snapshot_id: str,
    requested_permission_mode: PermissionMode,
    content: CanonicalContent,
    occurred_at: datetime,
    actor_kind: str = "human",
    actor_id: str = "user",
) -> PreparedRootTurnAdmission:
    payload = _root_turn_admission_payload(
        session_id=session_id,
        command_id=command_id,
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=context_binding_revision_id,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=requested_permission_mode,
        content=content,
        occurred_at=occurred_at,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )
    semantic_digest = canonical_digest(
        "pulsara:submit-prompt-command:v2", payload
    )
    candidate_fingerprint = canonical_digest(
        "pulsara:prepared-root-turn-admission:v1",
        {**payload, "semantic_digest": semantic_digest},
    )
    event = CommittedEventDraft(
        event_id=_stable_identity(
            "event", candidate_fingerprint, "UserMessageAccepted"
        ),
        event_type=CommittedEventType.USER_MESSAGE_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
        actor_kind=actor_kind,
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload=MappingProxyType(
            {"entry_kind": EntryKind.USER_MESSAGE.value}
        ),
    )
    return PreparedRootTurnAdmission(
        session_id=session_id,
        command_id=command_id,
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=context_binding_revision_id,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=requested_permission_mode,
        content=content,
        occurred_at=occurred_at,
        actor_kind=actor_kind,
        actor_id=actor_id,
        semantic_digest=semantic_digest,
        event=event,
        candidate_fingerprint=candidate_fingerprint,
    )


def _subagent_turn_admission_payload(
    *,
    session_id: str,
    task_id: str,
    turn_id: str,
    entry_id: str,
    context_binding_revision_id: str,
    permission_snapshot_id: str,
    content: CanonicalContent,
    occurred_at: datetime,
    actor_id: str,
) -> Mapping[str, object]:
    return {
        "session_id": session_id,
        "task_id": task_id,
        "turn_id": turn_id,
        "entry_id": entry_id,
        "context_binding_revision_id": context_binding_revision_id,
        "permission_snapshot_id": permission_snapshot_id,
        "content": _canonical_content_identity(content),
        "occurred_at": occurred_at.isoformat(),
        "actor_id": actor_id,
    }


def build_prepared_subagent_turn_admission(
    *,
    session_id: str,
    task_id: str,
    turn_id: str,
    entry_id: str,
    context_binding_revision_id: str,
    permission_snapshot_id: str,
    content: CanonicalContent,
    occurred_at: datetime,
    actor_id: str = "subagent-manager",
) -> PreparedSubagentTurnAdmission:
    payload = _subagent_turn_admission_payload(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=context_binding_revision_id,
        permission_snapshot_id=permission_snapshot_id,
        content=content,
        occurred_at=occurred_at,
        actor_id=actor_id,
    )
    candidate_fingerprint = canonical_digest(
        "pulsara:prepared-subagent-turn-admission:v1", payload
    )
    event = CommittedEventDraft(
        event_id=_stable_identity(
            "event", candidate_fingerprint, "UserMessageAccepted"
        ),
        event_type=CommittedEventType.USER_MESSAGE_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload=MappingProxyType({"source": "SUBAGENT_TASK_OBJECTIVE"}),
    )
    return PreparedSubagentTurnAdmission(
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=context_binding_revision_id,
        permission_snapshot_id=permission_snapshot_id,
        content=content,
        occurred_at=occurred_at,
        actor_id=actor_id,
        event=event,
        candidate_fingerprint=candidate_fingerprint,
    )


def _tool_remote_identity_publication_payload(
    *,
    session_id: str,
    attempt_id: str,
    remote_identity: str,
    occurred_at: datetime,
    actor_id: str,
) -> Mapping[str, object]:
    return {
        "session_id": session_id,
        "attempt_id": attempt_id,
        "remote_identity": remote_identity,
        "occurred_at": occurred_at.isoformat(),
        "actor_id": actor_id,
    }


def build_prepared_tool_remote_identity_publication(
    *,
    session_id: str,
    attempt_id: str,
    remote_identity: str,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedToolRemoteIdentityPublication:
    payload = _tool_remote_identity_publication_payload(
        session_id=session_id,
        attempt_id=attempt_id,
        remote_identity=remote_identity,
        occurred_at=occurred_at,
        actor_id=actor_id,
    )
    candidate_fingerprint = canonical_digest(
        "pulsara:prepared-tool-remote-identity-publication:v1", payload
    )
    identity_bytes = remote_identity.encode("utf-8")
    event = CommittedEventDraft(
        event_id=_stable_identity(
            "event",
            candidate_fingerprint,
            CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED.value,
        ),
        event_type=CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED,
        subject=CommittedEventSubject(SubjectSlot.TOOL_ATTEMPT, attempt_id),
        actor_kind="tool",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload=MappingProxyType(
            {
                "remote_identity_utf8_bytes": len(identity_bytes),
                "remote_identity_digest": "sha256:"
                + sha256(identity_bytes).hexdigest(),
            }
        ),
    )
    return PreparedToolRemoteIdentityPublication(
        session_id=session_id,
        attempt_id=attempt_id,
        remote_identity=remote_identity,
        occurred_at=occurred_at,
        actor_id=actor_id,
        event=event,
        candidate_fingerprint=candidate_fingerprint,
    )


def _plan_inline(payload: Mapping[str, object]) -> InlineContent:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 48 * 1024:
        raise ValueError("Plan control result exceeds its inline bound")
    return InlineContent.from_bytes(
        encoded,
        media_type="application/vnd.pulsara.plan-control+json",
        codec="utf-8",
    )


def plan_question_resolution_semantic_fingerprint(
    *,
    workflow_id: str,
    expected_workflow_revision: int,
    interaction_id: str,
    answer: PlanQuestionAnswer,
    result_id: str,
    result_entry_id: str,
) -> str:
    return canonical_digest(
        "pulsara:resolve-plan-question:v1",
        {
            "workflow_id": workflow_id,
            "expected_workflow_revision": expected_workflow_revision,
            "interaction_id": interaction_id,
            "answer": {
                "kind": answer.kind.value,
                "option_ordinal": answer.option_ordinal,
                "free_text": answer.free_text,
            },
            "result_id": result_id,
            "result_entry_id": result_entry_id,
        },
    )


def plan_exit_semantic_fingerprint(
    *,
    command_kind: str,
    workflow_id: str,
    expected_workflow_revision: int,
) -> str:
    if command_kind not in {"CANCEL_PLAN", "FORCE_EXIT_PLAN"}:
        raise ValueError("Plan exit command kind is invalid")
    return canonical_digest(
        "pulsara:user-plan-exit:v1",
        {
            "command_kind": command_kind,
            "workflow_id": workflow_id,
            "expected_workflow_revision": expected_workflow_revision,
        },
    )


def plan_draft_review_semantic_candidate(
    *,
    workflow_id: str,
    expected_workflow_revision: int,
    interaction_id: str,
    decision: PlanDraftDecision,
    feedback: str | None,
    continuation_turn_id: str | None,
    continuation_entry_id: str | None,
    continuation_context_binding_revision_id: str | None,
) -> tuple[str | None, tuple[str | None, str | None, str | None], str]:
    normalized_feedback = None
    if decision is PlanDraftDecision.REVISE and feedback:
        if len(feedback.encode("utf-8")) > 32 * 1024:
            raise ValueError("Plan revision feedback exceeds its bound")
        normalized_feedback = feedback
    elif decision is not PlanDraftDecision.REVISE and feedback is not None:
        raise ValueError("Plan approve/cancel cannot carry feedback")
    creates_turn = decision in {
        PlanDraftDecision.APPROVE,
        PlanDraftDecision.REVISE,
    }
    continuation_values = (
        continuation_turn_id,
        continuation_entry_id,
        continuation_context_binding_revision_id,
    )
    if creates_turn != all(value is not None for value in continuation_values):
        raise ValueError("Plan review continuation union is invalid")
    semantic_digest = canonical_digest(
        "pulsara:resolve-plan-draft:v1",
        {
            "workflow_id": workflow_id,
            "expected_workflow_revision": expected_workflow_revision,
            "interaction_id": interaction_id,
            "decision": decision.value,
            "feedback": normalized_feedback,
            "continuation": continuation_values,
        },
    )
    return normalized_feedback, continuation_values, semantic_digest


def _content_manifest(content: CanonicalContent) -> dict[str, object]:
    payload: dict[str, object] = {
        "digest": content.digest,
        "size": content.size,
        "media_type": content.media_type,
        "codec": content.codec,
    }
    if isinstance(content, BlobContent):
        payload["blob_id"] = content.blob_id
    else:
        payload["inline"] = True
    return payload


def _event_manifest(event: CommittedEventDraft) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "subject_slot": event.subject.slot.value,
        "subject_id": event.subject.subject_id,
        "actor_kind": event.actor_kind,
        "actor_id": event.actor_id,
        "sensitivity_class": event.sensitivity_class,
        "projection_profile": event.projection_profile,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": dict(event.payload),
    }


def _prepared_tool_result_manifest(
    candidate: PreparedToolResultAcceptance,
) -> dict[str, object]:
    blob = candidate.artifact_blob_descriptor
    side = candidate.side_branch
    side_payload: dict[str, object]
    if isinstance(side, NoToolResultSideBranch):
        side_payload = {"branch_kind": side.branch_kind.value}
    else:
        side_payload = {
            "branch_kind": side.branch_kind.value,
            "memory_candidate_id": side.memory_candidate_id,
            "proposal_kind": side.proposal_kind,
            "proposal_payload": dict(side.proposal_payload),
            "candidate_semantic_digest": side.candidate_semantic_digest,
            "governance_job_id": side.governance_job_id,
            "job_handler_type": side.job_handler_type,
            "intent_schema_version": side.intent_schema_version,
            "intent_payload": dict(side.intent_payload),
            "intent_digest": side.intent_digest,
            "automatic_intent_key": side.automatic_intent_key,
            "safety_class": side.safety_class,
            "initial_status": side.initial_status,
            "retry_policy_id": side.retry_policy_id,
            "retry_policy_version": side.retry_policy_version,
            "maximum_attempts": side.maximum_attempts,
            "attempt_timeout_ms": side.attempt_timeout_ms,
            "provider_input_token_limit_per_attempt": (
                side.provider_input_token_limit_per_attempt
            ),
            "provider_output_token_limit_per_attempt": (
                side.provider_output_token_limit_per_attempt
            ),
            "next_eligible_at": side.next_eligible_at.isoformat(),
            "job_queued_occurrence": _event_manifest(side.job_queued_occurrence),
        }
    return {
        "session_id": candidate.session_id,
        "workspace_id": candidate.workspace_id,
        "result_id": candidate.result_id,
        "result_entry_id": candidate.result_entry_id,
        "turn_id": candidate.turn_id,
        "assistant_entry_id": candidate.assistant_entry_id,
        "tool_call_id": candidate.tool_call_id,
        "attempt_id": candidate.attempt_id,
        "result_state": candidate.result_state,
        "canonical_preview_content": _content_manifest(
            candidate.canonical_preview_content
        ),
        "artifact_disposition": candidate.artifact_disposition.value,
        "artifact_id": candidate.artifact_id,
        "artifact_blob_descriptor": None if blob is None else _content_manifest(blob),
        "source_coverage": candidate.source_coverage.value,
        "display_kind": candidate.display_kind.value,
        "source_coverage_reason": (
            None
            if candidate.source_coverage_reason is None
            else candidate.source_coverage_reason.value
        ),
        "artifact_unavailability_reason": (
            None
            if candidate.artifact_unavailability_reason is None
            else candidate.artifact_unavailability_reason.value
        ),
        "observed_at": canonical_utc_timestamp(candidate.observed_at),
        "observation_duration_microseconds": (
            candidate.observation_duration_microseconds
        ),
        "observation_origin_kind": candidate.observation_origin_kind.value,
        "trusted_tool_reported_duration_microseconds": (
            candidate.trusted_tool_reported_duration_microseconds
        ),
        "actor_id": candidate.actor_id,
        "tool_result_occurrence": _event_manifest(candidate.tool_result_occurrence),
        "side_branch": side_payload,
    }


def build_prepared_tool_result_acceptance(
    *,
    guard: HostWriterGuard,
    workspace_id: str,
    result_id: str,
    result_entry_id: str,
    turn_id: str,
    assistant_entry_id: str,
    tool_call_id: str,
    attempt_id: str | None,
    result_state: str,
    canonical_preview_content: InlineContent,
    artifact_disposition: ToolOutputArtifactDisposition,
    artifact_id: str | None,
    artifact_blob_descriptor: BlobContent | None,
    source_coverage: ToolOutputSourceCoverage,
    display_kind: ToolResultDisplayKind,
    source_coverage_reason: ToolOutputSourceCoverageReason | None,
    artifact_unavailability_reason: (ToolOutputArtifactUnavailabilityReason | None),
    observed_at: datetime,
    observation_duration_microseconds: int | None,
    observation_origin_kind: ToolObservationOrigin,
    trusted_tool_reported_duration_microseconds: int | None,
    actor_id: str,
    memory_candidate_id: str | None = None,
    memory_proposal_kind: str | None = None,
    memory_proposal_payload: Mapping[str, object] | None = None,
    memory_governance_job_id: str | None = None,
) -> PreparedToolResultAcceptance:
    """Freeze every semantic field before the first canonical write."""

    tool_result_occurrence = CommittedEventDraft(
        event_id=_stable_identity("event", result_entry_id, "ToolResultAccepted"),
        event_type=CommittedEventType.TOOL_RESULT_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, result_entry_id),
        actor_kind="tool",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=observed_at,
        payload={"tool_call_id": tool_call_id, "result_state": result_state},
    )
    memory_fields = (
        memory_candidate_id,
        memory_proposal_kind,
        memory_proposal_payload,
        memory_governance_job_id,
    )
    if any(item is not None for item in memory_fields) and not all(
        item is not None for item in memory_fields
    ):
        raise ValueError("memory proposal result branch is incomplete")
    if memory_candidate_id is None:
        side_branch: ToolResultSideBranch = NoToolResultSideBranch()
    else:
        assert memory_proposal_kind is not None
        assert memory_proposal_payload is not None
        assert memory_governance_job_id is not None
        proposal = freeze_tool_json_object(memory_proposal_payload)
        semantic_digest = canonical_digest(
            "pulsara:memory-candidate:v1",
            {
                "workspace_id": workspace_id,
                "proposal_kind": memory_proposal_kind,
                "proposal_payload": dict(proposal),
                "source_entry_id": assistant_entry_id,
            },
        )
        intent_payload = freeze_tool_json_object(
            {
                "candidate_id": memory_candidate_id,
                "candidate_semantic_digest": semantic_digest,
            }
        )
        governance = job_handler_contract(MEMORY_GOVERNANCE)
        side_branch = PreparedMemoryProposalSideBranch(
            memory_candidate_id=memory_candidate_id,
            proposal_kind=memory_proposal_kind,
            proposal_payload=proposal,
            candidate_semantic_digest=semantic_digest,
            governance_job_id=memory_governance_job_id,
            intent_payload=intent_payload,
            intent_digest=canonical_digest(
                "pulsara:job-intent:memory_governance.v1", dict(intent_payload)
            ),
            automatic_intent_key=f"memory-governance:{memory_candidate_id}",
            retry_policy_id="bounded-exponential",
            retry_policy_version=1,
            maximum_attempts=governance.maximum_attempts,
            attempt_timeout_ms=governance.attempt_timeout_ms,
            provider_input_token_limit_per_attempt=governance.input_token_limit,
            provider_output_token_limit_per_attempt=governance.output_token_limit,
            next_eligible_at=observed_at,
            job_queued_occurrence=CommittedEventDraft(
                event_id=_stable_identity(
                    "event", memory_governance_job_id, "JobQueued"
                ),
                event_type=CommittedEventType.JOB_QUEUED,
                subject=CommittedEventSubject(
                    SubjectSlot.JOB, memory_governance_job_id
                ),
                actor_kind="runtime",
                actor_id=guard.writer_owner_id,
                sensitivity_class="PUBLIC",
                projection_profile="DEFAULT",
                occurred_at=observed_at,
                payload={"handler_type": MEMORY_GOVERNANCE},
            ),
        )
    return PreparedToolResultAcceptance(
        session_id=guard.session_id,
        workspace_id=workspace_id,
        result_id=result_id,
        result_entry_id=result_entry_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state=result_state,
        canonical_preview_content=canonical_preview_content,
        artifact_disposition=artifact_disposition,
        artifact_id=artifact_id,
        artifact_blob_descriptor=artifact_blob_descriptor,
        source_coverage=source_coverage,
        display_kind=display_kind,
        source_coverage_reason=source_coverage_reason,
        artifact_unavailability_reason=artifact_unavailability_reason,
        observed_at=observed_at,
        observation_duration_microseconds=observation_duration_microseconds,
        observation_origin_kind=observation_origin_kind,
        trusted_tool_reported_duration_microseconds=(
            trusted_tool_reported_duration_microseconds
        ),
        actor_id=actor_id,
        tool_result_occurrence=tool_result_occurrence,
        side_branch=side_branch,
    )
