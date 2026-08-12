"""PostgreSQL repository for the canonical relational conversation kernel.

All product SQL is deliberately schema-qualified.  The repository owns the
only two mutation capabilities: a live Host writer lease and an exact durable
job-attempt claim.  Canonical rows and required occurrence events are written
inside the same physical transaction; no event is used to prove a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import json
import math
from threading import local
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from psycopg import Connection, IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.conversation_kernel.contracts import (
    AssistantBlockKind,
    BlobContent,
    CanonicalContent,
    CommittedEventDraft,
    CommittedEventSubject,
    ConversationScopeKind,
    EntryKind,
    HostWriterGuard,
    InlineContent,
    JobAttemptClaimGuard,
    JobSafetyClass,
    PromptDeliveryMode,
    StoredCommittedEvent,
    WriterLease,
    canonical_digest,
)
from pulsara_agent.conversation_kernel.job_catalog import (
    BACKGROUND_COMPACTION,
    MEMORY_GOVERNANCE,
    POST_COMPACTION_MEMORY_EXTRACTION,
    job_handler_contract,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    FrozenToolJsonDict,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
    freeze_tool_json_object,
)
from pulsara_agent.ports.terminal_observation import (
    ExistingTurnInstallation,
    NewTurnInstallation,
    TerminalObservationInstallationAttempt,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.conversation_kernel.vocabulary import (
    DESCRIPTOR_BY_TYPE,
    AppendGuardKind,
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


INLINE_CONTENT_LIMIT = STAGE2_LIMITS.inline_content_hard_bytes
COMMITTED_EVENT_PAYLOAD_LIMIT = STAGE2_LIMITS.committed_payload_hard_bytes


class ConversationKernelConflict(RuntimeError):
    """A stable identity already names a different semantic fact."""


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
    actor_id: str
    occurred_at: datetime
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
            or occurrence.occurred_at != self.occurred_at
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
                or side_event.occurred_at != self.occurred_at
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


@dataclass(frozen=True, slots=True)
class AcceptedCapabilityDecision:
    decision_id: str
    decision: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str | None
    result_entry_id: str | None


@dataclass(frozen=True, slots=True)
class AcceptedInteractionDecision:
    decision_id: str
    command_id: str
    decision: str
    assistant_entry_id: str
    tool_call_id: str
    attempt_id: str | None
    result_entry_id: str | None


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
        "actor_id": candidate.actor_id,
        "occurred_at": candidate.occurred_at.isoformat(),
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
    actor_id: str,
    occurred_at: datetime,
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
        occurred_at=occurred_at,
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
            next_eligible_at=occurred_at,
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
                occurred_at=occurred_at,
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
        actor_id=actor_id,
        occurred_at=occurred_at,
        tool_result_occurrence=tool_result_occurrence,
        side_branch=side_branch,
    )


class ConversationKernelRepository:
    """Single storage owner for Stage 2 product facts."""

    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
        *,
        post_commit_tap: Callable[[tuple[StoredCommittedEvent, ...]], None]
        | None = None,
    ) -> None:
        self._provider = connection_provider
        self._post_commit_tap = post_commit_tap
        self._event_batch_local = local()

    @property
    def connection_provider(self) -> VerifiedPostgresConnectionProviderProtocol:
        """Read-only construction seam for canonical query services."""

        return self._provider

    def read_session_workspace_id(
        self,
        guard: HostWriterGuard,
        *,
        deadline_monotonic: float,
    ) -> str:
        """Resolve the exact writer-scoped workspace before candidate freeze."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            return self._workspace_id(connection, guard.session_id)

    def acquire_host_writer(
        self,
        *,
        session_id: str,
        workspace_id: str,
        writer_owner_id: str,
        lease_seconds: float,
        deadline_monotonic: float,
    ) -> WriterLease:
        if lease_seconds <= 0:
            raise ValueError("writer lease must be finite and positive")
        expires_at = _utcnow() + timedelta(seconds=lease_seconds)
        self._begin_event_batch()
        try:
            with self._provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                row_factory=dict_row,
                deadline_monotonic=deadline_monotonic,
            ) as connection:
                row = connection.execute(
                    """
                    SELECT id, workspace_id, lifecycle, writer_generation,
                           writer_lease_owner_id, writer_lease_expires_at
                    FROM pulsara_v3.sessions
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.sessions (
                            id, workspace_id, lifecycle, writer_generation,
                            writer_lease_owner_id, writer_lease_expires_at
                        ) VALUES (%s, %s, 'OPEN', 1, %s, %s)
                        """,
                        (session_id, workspace_id, writer_owner_id, expires_at),
                    )
                    generation = 1
                else:
                    if str(row["workspace_id"]) != workspace_id:
                        raise ConversationKernelConflict("session workspace conflict")
                    if str(row["lifecycle"]) != "OPEN":
                        raise ConversationKernelConflict("session is closed")
                    same_live_owner = (
                        row["writer_lease_owner_id"] == writer_owner_id
                        and row["writer_lease_expires_at"] is not None
                        and row["writer_lease_expires_at"] > _utcnow()
                    )
                    if same_live_owner:
                        generation = int(row["writer_generation"])
                    else:
                        generation = int(row["writer_generation"]) + 1
                    connection.execute(
                        """
                        UPDATE pulsara_v3.sessions
                        SET writer_generation = %s,
                            writer_lease_owner_id = %s,
                            writer_lease_expires_at = %s,
                            updated_at = clock_timestamp()
                        WHERE id = %s
                        """,
                        (generation, writer_owner_id, expires_at, session_id),
                    )
                    if not same_live_owner:
                        self._interrupt_prior_generation(
                            connection,
                            guard=HostWriterGuard(
                                session_id=session_id,
                                writer_generation=generation,
                                writer_owner_id=writer_owner_id,
                            ),
                            workspace_id=workspace_id,
                        )
        except BaseException:
            self._finish_event_batch(committed=False)
            raise
        else:
            self._finish_event_batch(committed=True)
        return WriterLease(
            guard=HostWriterGuard(
                session_id=session_id,
                writer_generation=generation,
                writer_owner_id=writer_owner_id,
            ),
            expires_at=expires_at,
        )

    def renew_host_writer(
        self,
        guard: HostWriterGuard,
        *,
        lease_seconds: float,
        deadline_monotonic: float,
    ) -> WriterLease:
        if lease_seconds <= 0:
            raise ValueError("writer lease must be finite and positive")
        expires_at = _utcnow() + timedelta(seconds=lease_seconds)
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET writer_lease_expires_at = %s, updated_at = clock_timestamp()
                WHERE id = %s AND writer_generation = %s
                  AND writer_lease_owner_id = %s AND lifecycle = 'OPEN'
                  AND writer_lease_expires_at > clock_timestamp()
                RETURNING writer_generation
                """,
                (
                    expires_at,
                    guard.session_id,
                    guard.writer_generation,
                    guard.writer_owner_id,
                ),
            ).fetchone()
            if row is None:
                raise StaleHostWriter("host writer lease is stale")
        return WriterLease(guard=guard, expires_at=expires_at)

    def start_root_turn(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        turn_id: str,
        entry_id: str,
        context_binding_revision_id: str,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_kind: str = "human",
        actor_id: str = "user",
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        semantic_digest = canonical_digest(
            "pulsara:submit-prompt-command:v1",
            {
                "turn_id": turn_id,
                "entry_id": entry_id,
                "content_digest": content.digest,
            },
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                """
                SELECT command_kind, semantic_digest, target_turn_id
                FROM pulsara_v3.session_commands
                WHERE session_id = %s AND command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_kind"] != "SUBMIT_PROMPT"
                    or existing["semantic_digest"] != semantic_digest
                    or existing["target_turn_id"] != turn_id
                ):
                    raise ConversationKernelConflict("command identity conflict")
                return self._accepted_entry(connection, guard.session_id, entry_id)
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            workspace_id = self._workspace_id(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    status, initial_entry_id, current_context_binding_revision_id
                ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s)
                """,
                (
                    turn_id,
                    guard.session_id,
                    workspace_id,
                    entry_id,
                    context_binding_revision_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turn_context_binding_revisions (
                    id, session_id, turn_id, revision_ordinal,
                    base_kind, source_through_sequence
                ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
                """,
                (
                    context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    entry_sequence - 1,
                ),
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_turn_id
                ) VALUES (%s, %s, 'SUBMIT_PROMPT',
                          'submit_prompt.v1', %s, 'TURN', %s)
                """,
                (guard.session_id, command_id, semantic_digest, turn_id),
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        entry_id,
                        occurred_at=occurred_at,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        payload={"entry_kind": EntryKind.USER_MESSAGE.value},
                    ),
                ),
            )[0]
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
            )

    def prepare_provider_input_cut(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        deadline_monotonic: float,
    ) -> PreparedProviderInputCut:
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            row = connection.execute(
                """
                SELECT t.current_context_binding_revision_id,
                       s.latest_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.sessions AS s ON s.id = t.session_id
                WHERE t.session_id = %s AND t.id = %s AND t.status = 'RUNNING'
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if row is None:
                raise ConversationKernelConflict("turn is not running")
            return PreparedProviderInputCut(
                session_id=guard.session_id,
                turn_id=turn_id,
                context_binding_revision_id=str(
                    row["current_context_binding_revision_id"]
                ),
                provider_input_through_sequence=int(row["latest_entry_sequence"]),
            )

    def require_provider_safe_turn(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        deadline_monotonic: float,
    ) -> None:
        """Prove the canonical half of the provider safe-point predicate."""

        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            self._require_provider_safe_turn_in_transaction(
                connection,
                session_id=guard.session_id,
                turn_id=turn_id,
                lock=False,
            )

    def accept_terminal_observation(
        self,
        guard: HostWriterGuard,
        *,
        candidate: TerminalObservationInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        """Atomically accept one same-Host Terminal observation.

        The immutable process-local candidate is the only retry identity.  A
        successful transaction installs the entry, an optional new ROOT turn
        and its revision zero, plus the selective occurrence together.
        """

        if candidate.session_id != guard.session_id:
            raise ValueError("terminal observation belongs to another session")
        if candidate.writer_generation != guard.writer_generation:
            raise StaleHostWriter("terminal observation writer generation is stale")
        content = InlineContent.from_bytes(
            candidate.content.canonical_bytes(),
            media_type="application/vnd.pulsara.terminal-observation+json",
            codec="utf-8",
        )
        if content.digest != candidate.content_digest:
            raise ValueError("terminal observation content digest conflicts")
        target = candidate.target
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            if workspace_id != candidate.workspace_id:
                raise ConversationKernelConflict(
                    "terminal observation workspace conflicts"
                )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            if isinstance(target, ExistingTurnInstallation):
                turn = self._require_provider_safe_turn_in_transaction(
                    connection,
                    session_id=guard.session_id,
                    turn_id=target.turn_id,
                    lock=True,
                )
                if (
                    str(turn["workspace_id"]) != workspace_id
                    or str(turn["conversation_scope_kind"])
                    != ConversationScopeKind.ROOT.value
                ):
                    raise ConversationKernelConflict(
                        "terminal observation target is not a ROOT turn"
                    )
                turn_id = target.turn_id
                entry_id = target.entry_id
            elif isinstance(target, NewTurnInstallation):
                running = connection.execute(
                    """
                    SELECT id FROM pulsara_v3.turns
                    WHERE session_id = %s AND conversation_scope_kind = 'ROOT'
                      AND status = 'RUNNING'
                    LIMIT 1
                    """,
                    (guard.session_id,),
                ).fetchone()
                if running is not None:
                    raise ConversationKernelConflict(
                        "idle terminal observation has a running ROOT turn"
                    )
                turn_id = target.turn_id
                entry_id = target.initial_entry_id
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.turns (
                        id, session_id, workspace_id, conversation_scope_kind,
                        status, initial_entry_id,
                        current_context_binding_revision_id
                    ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s)
                    """,
                    (
                        turn_id,
                        guard.session_id,
                        workspace_id,
                        entry_id,
                        target.context_binding_revision_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.turn_context_binding_revisions (
                        id, session_id, turn_id, revision_ordinal,
                        base_kind, source_through_sequence
                    ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
                    """,
                    (
                        target.context_binding_revision_id,
                        guard.session_id,
                        turn_id,
                        entry_sequence - 1,
                    ),
                )
            else:  # pragma: no cover - closed union exhaustiveness
                raise TypeError("terminal observation installation target is unknown")
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TERMINAL_OBSERVATION,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(self._terminal_observation_event(candidate, entry_id),),
            )[0]
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
            )

    def confirm_terminal_observation_winner(
        self,
        guard: HostWriterGuard,
        *,
        candidate: TerminalObservationInstallationAttempt,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Stateless exact confirmation for an ambiguous observation ACK."""

        if candidate.session_id != guard.session_id:
            raise ValueError("terminal observation belongs to another session")
        content = InlineContent.from_bytes(
            candidate.content.canonical_bytes(),
            media_type="application/vnd.pulsara.terminal-observation+json",
            codec="utf-8",
        )
        if content.digest != candidate.content_digest:
            raise ValueError("terminal observation content digest conflicts")
        target = candidate.target
        entry_id = (
            target.entry_id
            if isinstance(target, ExistingTurnInstallation)
            else target.initial_entry_id
        )
        turn_id = target.turn_id
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            entry = connection.execute(
                """
                SELECT * FROM pulsara_v3.transcript_entries
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, entry_id),
            ).fetchone()
            event_id = _stable_identity(
                "event",
                candidate.content.observation_id,
                CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED.value,
            )
            event_rows = connection.execute(
                "SELECT * FROM pulsara_v3.agent_events WHERE event_id = %s",
                (event_id,),
            ).fetchall()
            if entry is None and not event_rows:
                return None
            if entry is None or len(event_rows) != 1:
                raise ConversationKernelConflict(
                    "terminal observation winner is partially installed"
                )
            if (
                str(entry["workspace_id"]) != candidate.workspace_id
                or str(entry["turn_id"]) != turn_id
                or str(entry["entry_kind"]) != EntryKind.TERMINAL_OBSERVATION.value
                or str(entry["conversation_scope_kind"])
                != ConversationScopeKind.ROOT.value
                or entry["scope_subagent_task_id"] is not None
                or entry["context_binding_revision_id"] is not None
                or entry["provider_input_through_sequence"] is not None
                or self._content_from_row(entry) != content
            ):
                raise ConversationKernelConflict(
                    "terminal observation identity names a different entry"
                )
            event = self._exact_event_for_confirmation(
                connection,
                self._terminal_observation_event(candidate, entry_id),
                session_id=guard.session_id,
                workspace_id=candidate.workspace_id,
            )
            turn = connection.execute(
                """
                SELECT * FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise ConversationKernelConflict("terminal observation turn is absent")
            if isinstance(target, NewTurnInstallation):
                revision = connection.execute(
                    """
                    SELECT * FROM pulsara_v3.turn_context_binding_revisions
                    WHERE session_id = %s AND id = %s
                    """,
                    (guard.session_id, target.context_binding_revision_id),
                ).fetchone()
                if (
                    str(turn["initial_entry_id"]) != target.initial_entry_id
                    or str(turn["current_context_binding_revision_id"])
                    != target.context_binding_revision_id
                    or revision is None
                    or str(revision["turn_id"]) != target.turn_id
                    or int(revision["revision_ordinal"]) != 0
                    or str(revision["base_kind"]) != "FULL_HISTORY"
                    or int(revision["source_through_sequence"])
                    != int(entry["entry_sequence"]) - 1
                ):
                    raise ConversationKernelConflict(
                        "terminal observation genesis differs from candidate"
                    )
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=turn_id,
                entry_sequence=int(entry["entry_sequence"]),
                event_sequence=int(event["event_sequence"]),
            )

    def adopt_context_snapshot(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        snapshot_id: str,
        context_binding_revision_id: str,
        source_through_sequence: int,
        source_digest: str,
        compiler_contract: str,
        prompt_contract: str,
        model_contract: str,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> int:
        """Install one immutable mid-turn binding revision.

        The process-local safe-point coordinator owns exclusion from a model
        operation.  This transaction independently rechecks the canonical
        predicate and exact current revision before advancing the pointer.
        """

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT t.workspace_id, t.current_context_binding_revision_id,
                       current.revision_ordinal,
                       current.source_through_sequence AS current_source_cut,
                       initial_entry.entry_sequence AS initial_entry_sequence
                FROM pulsara_v3.turns AS t
                JOIN pulsara_v3.turn_context_binding_revisions AS current
                  ON current.session_id = t.session_id
                 AND current.id = t.current_context_binding_revision_id
                JOIN pulsara_v3.transcript_entries AS initial_entry
                  ON initial_entry.session_id = t.session_id
                 AND initial_entry.id = t.initial_entry_id
                WHERE t.session_id = %s AND t.id = %s AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise ConversationKernelConflict("snapshot target turn is not running")
            missing = connection.execute(
                """
                SELECT 1
                FROM pulsara_v3.assistant_message_blocks AS b
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = b.session_id
                 AND r.tool_call_entry_id = b.assistant_entry_id
                 AND r.tool_call_id = b.tool_call_id
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id AND e.id = b.assistant_entry_id
                WHERE e.session_id = %s AND e.turn_id = %s
                  AND b.block_kind = 'TOOL_CALL' AND r.id IS NULL
                LIMIT 1
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if missing is not None:
                raise ConversationKernelConflict("tool request is not terminal")
            if source_through_sequence < int(
                turn["current_source_cut"]
            ) or source_through_sequence >= int(turn["initial_entry_sequence"]):
                raise ConversationKernelConflict("snapshot source range is invalid")
            revision_ordinal = int(turn["revision_ordinal"]) + 1
            connection.execute(
                """
                INSERT INTO pulsara_v3.context_snapshots (
                    id, session_id, workspace_id, source_through_sequence,
                    source_digest, compiler_contract, prompt_contract,
                    model_contract, inline_content, blob_id, content_digest,
                    content_size, content_media_type, content_codec
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s)
                """,
                (
                    snapshot_id,
                    guard.session_id,
                    turn["workspace_id"],
                    source_through_sequence,
                    source_digest,
                    compiler_contract,
                    prompt_contract,
                    model_contract,
                    *_content_columns(content),
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turn_context_binding_revisions (
                    id, session_id, turn_id, revision_ordinal,
                    base_kind, context_snapshot_id, source_through_sequence
                ) VALUES (%s, %s, %s, %s, 'SNAPSHOT', %s, %s)
                """,
                (
                    context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    revision_ordinal,
                    snapshot_id,
                    source_through_sequence,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET current_context_binding_revision_id = %s
                WHERE session_id = %s AND id = %s
                """,
                (context_binding_revision_id, guard.session_id, turn_id),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(turn["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.COMPACTION_ADOPTED,
                        SubjectSlot.CONTEXT_BINDING_REVISION,
                        context_binding_revision_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"revision_ordinal": revision_ordinal},
                    ),
                ),
            )
            return revision_ordinal

    def commit_assistant_message(
        self,
        guard: HostWriterGuard,
        *,
        cut: PreparedProviderInputCut,
        entry_id: str,
        parent_content: CanonicalContent,
        blocks: Sequence[AssistantBlock],
        complete_turn: bool = False,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        if cut.session_id != guard.session_id:
            raise ValueError("prepared input cut belongs to another session")
        if not blocks:
            raise ValueError("assistant message requires at least one block")
        tool_request = any(isinstance(item, AssistantToolCallBlock) for item in blocks)
        if complete_turn and tool_request:
            raise ValueError("a tool-request message cannot complete its turn")
        event_type = (
            CommittedEventType.ASSISTANT_TOOL_REQUEST_ACCEPTED
            if tool_request
            else CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED
        )
        entry_kind = (
            EntryKind.ASSISTANT_TOOL_REQUEST
            if tool_request
            else EntryKind.ASSISTANT_MESSAGE
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind,
                       scope_subagent_task_id, current_context_binding_revision_id
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                FOR UPDATE
                """,
                (guard.session_id, cut.turn_id),
            ).fetchone()
            if (
                turn is None
                or str(turn["current_context_binding_revision_id"])
                != cut.context_binding_revision_id
            ):
                raise ConversationKernelConflict("prepared input cut is stale")
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            if cut.provider_input_through_sequence >= entry_sequence:
                raise ConversationKernelConflict("provider input cut is not historical")
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(turn["workspace_id"]),
                turn_id=cut.turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=entry_kind,
                scope_kind=ConversationScopeKind(str(turn["conversation_scope_kind"])),
                scope_task_id=turn["scope_subagent_task_id"],
                content=parent_content,
                context_binding_revision_id=cut.context_binding_revision_id,
                provider_input_through_sequence=cut.provider_input_through_sequence,
            )
            for ordinal, block in enumerate(blocks):
                self._insert_assistant_block(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(turn["workspace_id"]),
                    entry_id=entry_id,
                    ordinal=ordinal,
                    block=block,
                )
            subagent_message: tuple[str, int, str] | None = None
            if (
                str(turn["conversation_scope_kind"])
                == ConversationScopeKind.SUBAGENT_TASK.value
            ):
                task_id = str(turn["scope_subagent_task_id"])
                message_ordinal = int(
                    connection.execute(
                        """
                        SELECT count(*) AS total
                        FROM pulsara_v3.subagent_task_children
                        WHERE session_id = %s AND task_id = %s
                          AND child_kind = 'MESSAGE'
                        """,
                        (guard.session_id, task_id),
                    ).fetchone()["total"]
                )
                child_id = _stable_subagent_message_child_id(task_id, entry_id)
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.subagent_task_children (
                        id, session_id, task_id, child_kind,
                        child_ordinal, entry_id
                    ) VALUES (%s, %s, %s, 'MESSAGE', %s, %s)
                    """,
                    (
                        child_id,
                        guard.session_id,
                        task_id,
                        message_ordinal,
                        entry_id,
                    ),
                )
                subagent_message = (child_id, message_ordinal, task_id)
            event_drafts = [
                self._event(
                    event_type,
                    SubjectSlot.ENTRY,
                    entry_id,
                    occurred_at=occurred_at,
                    actor_kind="model",
                    actor_id=actor_id,
                    payload={
                        "entry_kind": entry_kind.value,
                        "block_count": len(blocks),
                    },
                )
            ]
            if subagent_message is not None:
                child_id, message_ordinal, task_id = subagent_message
                event_drafts.append(
                    self._event(
                        CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED,
                        SubjectSlot.SUBAGENT_MESSAGE,
                        child_id,
                        occurred_at=occurred_at,
                        actor_kind="subagent",
                        actor_id=task_id,
                        payload={"child_ordinal": message_ordinal},
                    )
                )
            pending_steer = False
            if complete_turn:
                pending_steer = bool(
                    connection.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pulsara_v3.prompt_queue_items
                            WHERE session_id = %s AND status = 'PENDING'
                              AND delivery_mode = 'STEER_ACTIVE_TURN'
                              AND target_turn_id = %s
                        ) AS present
                        """,
                        (guard.session_id, cut.turn_id),
                    ).fetchone()["present"]
                )
            turn_completed = complete_turn and not pending_steer
            if turn_completed:
                terminal = connection.execute(
                    """
                    UPDATE pulsara_v3.turns
                    SET status = 'COMPLETED', final_entry_id = %s,
                        terminal_reason = 'COMPLETED',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                    RETURNING id
                    """,
                    (entry_id, guard.session_id, cut.turn_id),
                ).fetchone()
                if terminal is None:
                    raise ConversationKernelConflict("turn has a terminal winner")
                event_drafts.append(
                    self._event(
                        CommittedEventType.TURN_COMPLETED,
                        SubjectSlot.TURN,
                        cut.turn_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id="foreground-runner",
                        payload={"final_entry_id": entry_id},
                    )
                )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(turn["workspace_id"]),
                drafts=tuple(event_drafts),
            )[0]
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=cut.turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
                turn_completed=turn_completed,
            )

    def confirm_assistant_message_winner(
        self,
        guard: HostWriterGuard,
        *,
        cut: PreparedProviderInputCut,
        entry_id: str,
        parent_content: CanonicalContent,
        blocks: Sequence[AssistantBlock],
        complete_turn: bool,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Exact-confirm a stable assistant candidate after an unknown ACK.

        This is a read of canonical rows and their accepted occurrence.  It is
        neither a second write nor a confirmation receipt.
        """

        tool_request = any(isinstance(item, AssistantToolCallBlock) for item in blocks)
        expected_entry_kind = (
            EntryKind.ASSISTANT_TOOL_REQUEST
            if tool_request
            else EntryKind.ASSISTANT_MESSAGE
        )
        expected_event_type = (
            CommittedEventType.ASSISTANT_TOOL_REQUEST_ACCEPTED
            if tool_request
            else CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED
        )
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            rows = connection.execute(
                """
                SELECT e.*, a.event_sequence, a.event_type,
                       a.actor_kind, a.actor_id, a.occurred_at, a.payload
                FROM pulsara_v3.transcript_entries AS e
                JOIN pulsara_v3.agent_events AS a
                  ON a.session_id = e.session_id
                 AND a.subject_entry_id = e.id
                 AND a.event_type IN (
                    'AssistantMessageAccepted',
                    'AssistantToolRequestAccepted'
                 )
                WHERE e.session_id = %s AND e.id = %s
                """,
                (guard.session_id, entry_id),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ConversationKernelConflict(
                    "assistant winner occurrence is not unique"
                )
            row = rows[0]
            expected_payload = {
                "entry_kind": expected_entry_kind.value,
                "block_count": len(blocks),
            }
            if (
                cut.session_id != guard.session_id
                or str(row["turn_id"]) != cut.turn_id
                or str(row["entry_kind"]) != expected_entry_kind.value
                or str(row["context_binding_revision_id"])
                != cut.context_binding_revision_id
                or int(row["provider_input_through_sequence"])
                != cut.provider_input_through_sequence
                or self._content_from_row(row) != parent_content
                or str(row["event_type"]) != expected_event_type.value
                or str(row["actor_kind"]) != "model"
                or str(row["actor_id"]) != actor_id
                or row["occurred_at"] != occurred_at
                or dict(row["payload"]) != expected_payload
            ):
                raise ConversationKernelConflict(
                    "assistant entry identity names a different winner"
                )
            block_rows = connection.execute(
                """
                SELECT * FROM pulsara_v3.assistant_message_blocks
                WHERE session_id = %s AND assistant_entry_id = %s
                ORDER BY block_ordinal, id
                """,
                (guard.session_id, entry_id),
            ).fetchall()
            actual_blocks: list[AssistantBlock] = []
            for block_row in block_rows:
                kind = str(block_row["block_kind"])
                if kind == AssistantBlockKind.TOOL_CALL.value:
                    frozen_arguments = freeze_json(dict(block_row["tool_arguments"]))
                    if not isinstance(frozen_arguments, FrozenJsonObjectFact):
                        raise ConversationKernelConflict(
                            "assistant winner tool arguments are not an object"
                        )
                    actual_blocks.append(
                        AssistantToolCallBlock(
                            block_id=str(block_row["id"]),
                            tool_call_id=str(block_row["tool_call_id"]),
                            tool_name=str(block_row["tool_name"]),
                            arguments=frozen_arguments,
                        )
                    )
                elif kind == AssistantBlockKind.TEXT.value:
                    actual_blocks.append(
                        AssistantTextBlock(
                            str(block_row["id"]), self._content_from_row(block_row)
                        )
                    )
                elif kind == AssistantBlockKind.DATA.value:
                    actual_blocks.append(
                        AssistantDataBlock(
                            str(block_row["id"]), self._content_from_row(block_row)
                        )
                    )
                else:
                    raise ConversationKernelConflict(
                        "assistant winner contains an unknown block kind"
                    )
            if tuple(actual_blocks) != tuple(blocks):
                raise ConversationKernelConflict(
                    "assistant entry blocks differ from the stable candidate"
                )
            if (
                str(row["conversation_scope_kind"])
                == ConversationScopeKind.SUBAGENT_TASK.value
            ):
                task_id = str(row["scope_subagent_task_id"])
                child_id = _stable_subagent_message_child_id(task_id, entry_id)
                child = connection.execute(
                    """
                    SELECT c.child_kind, c.child_ordinal, c.entry_id,
                           a.event_type, a.actor_kind, a.actor_id, a.payload
                    FROM pulsara_v3.subagent_task_children AS c
                    JOIN pulsara_v3.agent_events AS a
                      ON a.session_id = c.session_id
                     AND a.subject_subagent_message_id = c.id
                    WHERE c.session_id = %s AND c.id = %s
                    """,
                    (guard.session_id, child_id),
                ).fetchall()
                if (
                    len(child) != 1
                    or str(child[0]["child_kind"]) != "MESSAGE"
                    or str(child[0]["entry_id"]) != entry_id
                    or str(child[0]["event_type"])
                    != CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED.value
                    or str(child[0]["actor_kind"]) != "subagent"
                    or str(child[0]["actor_id"]) != task_id
                    or dict(child[0]["payload"])
                    != {"child_ordinal": int(child[0]["child_ordinal"])}
                ):
                    raise ConversationKernelConflict(
                        "subagent assistant winner lacks its exact message child"
                    )
            terminal = connection.execute(
                """
                SELECT event_sequence FROM pulsara_v3.agent_events
                WHERE session_id = %s AND event_type = 'TurnCompleted'
                  AND subject_turn_id = %s
                  AND payload->>'final_entry_id' = %s
                """,
                (guard.session_id, cut.turn_id, entry_id),
            ).fetchall()
            if len(terminal) > 1 or (terminal and not complete_turn):
                raise ConversationKernelConflict(
                    "assistant candidate terminal disposition conflicts"
                )
            return AcceptedEntry(
                entry_id=entry_id,
                turn_id=cut.turn_id,
                entry_sequence=int(row["entry_sequence"]),
                event_sequence=int(row["event_sequence"]),
                turn_completed=bool(terminal),
            )

    def accept_tool_capability_decision(
        self,
        guard: HostWriterGuard,
        *,
        decision_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        decision: str,
        authorization_reference: str,
        redacted_subject: str,
        attempt_id: str | None,
        result_id: str | None,
        result_entry_id: str | None,
        denial_content: CanonicalContent | None,
        denial_result_state: str | None,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedCapabilityDecision:
        """Accept one machine-policy decision and its immediate effect atomically."""

        if decision not in {"ALLOW", "DENY", "REQUIRE_CONFIRMATION"}:
            raise ValueError("machine capability decision is not closed")
        allow = decision == "ALLOW"
        deny = decision == "DENY"
        require_confirmation = decision == "REQUIRE_CONFIRMATION"
        if allow != (
            attempt_id is not None
            and result_id is None
            and result_entry_id is None
            and denial_content is None
            and denial_result_state is None
        ):
            raise ValueError("allowed capability effect union is invalid")
        if deny != (
            attempt_id is None
            and result_id is not None
            and result_entry_id is not None
            and denial_content is not None
            and denial_result_state == "PERMISSION_DENIED"
        ):
            raise ValueError("denied capability effect union is invalid")
        if require_confirmation != (
            attempt_id is None
            and result_id is None
            and result_entry_id is None
            and denial_content is None
            and denial_result_state is None
        ):
            raise ValueError("confirmation capability effect union is invalid")
        if not redacted_subject or len(redacted_subject.encode("utf-8")) > 4096:
            raise ValueError("capability redacted subject is outside its bound")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            subject = connection.execute(
                """
                SELECT e.turn_id, e.workspace_id, e.conversation_scope_kind,
                       e.scope_subagent_task_id
                FROM pulsara_v3.assistant_message_blocks AS b
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id
                 AND e.id = b.assistant_entry_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE b.session_id = %s AND b.assistant_entry_id = %s
                  AND b.tool_call_id = %s AND b.block_kind = 'TOOL_CALL'
                  AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, assistant_entry_id, tool_call_id),
            ).fetchone()
            if subject is None:
                raise ConversationKernelConflict(
                    "capability tool-call subject is not active"
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.interaction_decisions (
                    id, session_id, command_id, subject_kind,
                    subject_tool_call_entry_id, subject_tool_call_id,
                    decision, actor_kind, actor_id, redacted_subject
                ) VALUES (%s, %s, NULL, 'TOOL_CALL', %s, %s, %s,
                          'machine', %s, %s)
                """,
                (
                    decision_id,
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                    decision,
                    actor_id,
                    redacted_subject,
                ),
            )
            drafts = [
                self._event(
                    CommittedEventType.CAPABILITY_DECISION_ACCEPTED,
                    SubjectSlot.INTERACTION_DECISION,
                    decision_id,
                    occurred_at=occurred_at,
                    actor_kind="machine",
                    actor_id=actor_id,
                    payload={"decision": decision, "subject_kind": "TOOL_CALL"},
                )
            ]
            if allow:
                assert attempt_id is not None
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_execution_attempts (
                        id, session_id, assistant_entry_id, tool_call_id,
                        authorization_kind, authorization_reference,
                        actor_kind, actor_id
                    ) VALUES (%s, %s, %s, %s, 'machine', %s,
                              'runtime', 'foreground-tool-executor')
                    """,
                    (
                        attempt_id,
                        guard.session_id,
                        assistant_entry_id,
                        tool_call_id,
                        authorization_reference,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_ATTEMPT_ACCEPTED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id="foreground-tool-executor",
                        payload={
                            "assistant_entry_id": assistant_entry_id,
                            "tool_call_id": tool_call_id,
                        },
                    )
                )
            elif deny:
                assert result_id is not None
                assert result_entry_id is not None
                assert denial_content is not None
                entry_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                self._insert_entry(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(subject["workspace_id"]),
                    turn_id=str(subject["turn_id"]),
                    entry_id=result_entry_id,
                    entry_sequence=entry_sequence,
                    entry_kind=EntryKind.TOOL_RESULT,
                    scope_kind=ConversationScopeKind(
                        str(subject["conversation_scope_kind"])
                    ),
                    scope_task_id=subject["scope_subagent_task_id"],
                    content=denial_content,
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_results (
                        id, session_id, workspace_id,
                        tool_call_entry_id, tool_call_id,
                        attempt_id, result_entry_id, result_state
                    ) VALUES (%s, %s, %s, %s, %s, NULL, %s, 'PERMISSION_DENIED')
                    """,
                    (
                        result_id,
                        guard.session_id,
                        subject["workspace_id"],
                        assistant_entry_id,
                        tool_call_id,
                        result_entry_id,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_RESULT_ACCEPTED,
                        SubjectSlot.ENTRY,
                        result_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="tool",
                        actor_id="permission",
                        payload={
                            "tool_call_id": tool_call_id,
                            "result_state": "PERMISSION_DENIED",
                        },
                    )
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(subject["workspace_id"]),
                drafts=tuple(drafts),
            )
        return AcceptedCapabilityDecision(
            decision_id=decision_id,
            decision=decision,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_entry_id=result_entry_id,
        )

    def accept_tool_attempt(
        self,
        guard: HostWriterGuard,
        *,
        attempt_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        authorization_kind: str,
        authorization_reference: str,
        actor_kind: str,
        actor_id: str,
        remote_idempotency_key: str | None,
        retry_of_attempt_id: str | None,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedToolAttempt:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.tool_execution_attempts (
                    id, session_id, assistant_entry_id, tool_call_id,
                    authorization_kind, authorization_reference,
                    actor_kind, actor_id, remote_idempotency_key,
                    retry_of_attempt_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                    authorization_kind,
                    authorization_reference,
                    actor_kind,
                    actor_id,
                    remote_idempotency_key,
                    retry_of_attempt_id,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.TOOL_ATTEMPT_ACCEPTED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        payload={
                            "assistant_entry_id": assistant_entry_id,
                            "tool_call_id": tool_call_id,
                        },
                    ),
                ),
            )
        return AcceptedToolAttempt(
            attempt_id=attempt_id,
            session_id=guard.session_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
        )

    def publish_tool_remote_identity(
        self,
        guard: HostWriterGuard,
        *,
        attempt_id: str,
        remote_identity: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> bool:
        """Install one immutable remote identity before accepting its result.

        A compatible repeat is a read-confirmed success and emits no second
        occurrence.  A different identity is canonical corruption.
        """

        if not attempt_id or not remote_identity:
            raise ValueError("tool remote identity is incomplete")
        if len(remote_identity.encode("utf-8")) > 4096:
            raise ValueError("tool remote identity exceeds its bound")
        installed = False
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT remote_identity
                FROM pulsara_v3.tool_execution_attempts
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, attempt_id),
            ).fetchone()
            if row is None:
                raise ConversationKernelConflict("tool attempt is absent")
            current = row["remote_identity"]
            if current is not None:
                if str(current) != remote_identity:
                    raise ConversationKernelConflict(
                        "tool remote identity conflicts with installed authority"
                    )
                return False
            connection.execute(
                """
                UPDATE pulsara_v3.tool_execution_attempts
                SET remote_identity = %s,
                    remote_identity_published_at = clock_timestamp()
                WHERE session_id = %s AND id = %s
                """,
                (remote_identity, guard.session_id, attempt_id),
            )
            workspace_id = self._workspace_id(connection, guard.session_id)
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.TOOL_REMOTE_IDENTITY_PUBLISHED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind="tool",
                        actor_id=actor_id,
                        payload={
                            "remote_identity_utf8_bytes": len(
                                remote_identity.encode("utf-8")
                            ),
                            "remote_identity_digest": (
                                "sha256:"
                                + sha256(remote_identity.encode("utf-8")).hexdigest()
                            ),
                        },
                    ),
                ),
            )
            installed = True
        return installed

    def accept_tool_result(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedToolResultAcceptance,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        if candidate.session_id != guard.session_id:
            raise ValueError("prepared tool result belongs to another session")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind, scope_subagent_task_id
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, candidate.turn_id),
            ).fetchone()
            if turn is None or str(turn["workspace_id"]) != candidate.workspace_id:
                raise ConversationKernelConflict("tool result turn is absent")
            if candidate.artifact_blob_descriptor is not None:
                self._require_exact_tool_artifact_blob(
                    connection,
                    workspace_id=candidate.workspace_id,
                    expected=candidate.artifact_blob_descriptor,
                )
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=candidate.workspace_id,
                turn_id=candidate.turn_id,
                entry_id=candidate.result_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.TOOL_RESULT,
                scope_kind=ConversationScopeKind(str(turn["conversation_scope_kind"])),
                scope_task_id=turn["scope_subagent_task_id"],
                content=candidate.canonical_preview_content,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.tool_results (
                    id, session_id, workspace_id,
                    tool_call_entry_id, tool_call_id, attempt_id,
                    result_entry_id, result_state,
                    output_artifact_disposition, output_artifact_id,
                    output_artifact_blob_id, output_source_coverage,
                    output_display_kind, output_source_coverage_reason,
                    output_artifact_unavailability_reason
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    candidate.result_id,
                    guard.session_id,
                    candidate.workspace_id,
                    candidate.assistant_entry_id,
                    candidate.tool_call_id,
                    candidate.attempt_id,
                    candidate.result_entry_id,
                    candidate.result_state,
                    candidate.artifact_disposition.value,
                    candidate.artifact_id,
                    (
                        None
                        if candidate.artifact_blob_descriptor is None
                        else candidate.artifact_blob_descriptor.blob_id
                    ),
                    candidate.source_coverage.value,
                    candidate.display_kind.value,
                    (
                        None
                        if candidate.source_coverage_reason is None
                        else candidate.source_coverage_reason.value
                    ),
                    (
                        None
                        if candidate.artifact_unavailability_reason is None
                        else candidate.artifact_unavailability_reason.value
                    ),
                ),
            )
            event_drafts = [candidate.tool_result_occurrence]
            side = candidate.side_branch
            if isinstance(side, PreparedMemoryProposalSideBranch):
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_candidates (
                        id, workspace_id, origin_session_id, source_entry_id,
                        proposal_kind, semantic_digest, proposal_payload, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                    """,
                    (
                        side.memory_candidate_id,
                        candidate.workspace_id,
                        guard.session_id,
                        candidate.assistant_entry_id,
                        side.proposal_kind,
                        side.candidate_semantic_digest,
                        Jsonb(dict(side.proposal_payload)),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.durable_jobs (
                        id, workspace_id, origin_session_id, handler_type,
                        intent_schema_version, intent_digest, intent_payload,
                        automatic_intent_key, safety_class, status,
                        retry_policy_id, retry_policy_version, maximum_attempts,
                        attempt_timeout_ms, provider_input_token_limit_per_attempt,
                        provider_output_token_limit_per_attempt, next_eligible_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        side.governance_job_id,
                        candidate.workspace_id,
                        guard.session_id,
                        side.job_handler_type,
                        side.intent_schema_version,
                        side.intent_digest,
                        Jsonb(dict(side.intent_payload)),
                        side.automatic_intent_key,
                        side.safety_class,
                        side.initial_status,
                        side.retry_policy_id,
                        side.retry_policy_version,
                        side.maximum_attempts,
                        side.attempt_timeout_ms,
                        side.provider_input_token_limit_per_attempt,
                        side.provider_output_token_limit_per_attempt,
                        side.next_eligible_at,
                    ),
                )
                event_drafts.append(side.job_queued_occurrence)
            event = self._append_events(
                connection,
                guard,
                workspace_id=candidate.workspace_id,
                drafts=tuple(event_drafts),
            )[0]
            return AcceptedEntry(
                entry_id=candidate.result_entry_id,
                turn_id=candidate.turn_id,
                entry_sequence=entry_sequence,
                event_sequence=event.event_sequence,
            )

    def confirm_tool_result_winner(
        self,
        guard: HostWriterGuard,
        *,
        candidate: PreparedToolResultAcceptance,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Stateless exact confirmation after an ambiguous canonical ACK."""

        if candidate.session_id != guard.session_id:
            raise ValueError("prepared tool result belongs to another session")
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            self._require_writer(connection, guard, lock=False)
            entry = connection.execute(
                """
                SELECT * FROM pulsara_v3.transcript_entries
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, candidate.result_entry_id),
            ).fetchone()
            result = connection.execute(
                """
                SELECT * FROM pulsara_v3.tool_results
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, candidate.result_id),
            ).fetchone()
            if entry is None and result is None:
                return None
            if entry is None or result is None:
                raise ConversationKernelConflict(
                    "tool result winner is only partially installed"
                )
            blob = candidate.artifact_blob_descriptor
            if (
                str(entry["workspace_id"]) != candidate.workspace_id
                or str(entry["turn_id"]) != candidate.turn_id
                or str(entry["entry_kind"]) != EntryKind.TOOL_RESULT.value
                or self._content_from_row(entry) != candidate.canonical_preview_content
                or str(result["workspace_id"]) != candidate.workspace_id
                or str(result["tool_call_entry_id"]) != candidate.assistant_entry_id
                or str(result["tool_call_id"]) != candidate.tool_call_id
                or result["attempt_id"] != candidate.attempt_id
                or str(result["result_entry_id"]) != candidate.result_entry_id
                or str(result["result_state"]) != candidate.result_state
                or str(result["output_artifact_disposition"])
                != candidate.artifact_disposition.value
                or result["output_artifact_id"] != candidate.artifact_id
                or result["output_artifact_blob_id"]
                != (None if blob is None else blob.blob_id)
                or str(result["output_source_coverage"])
                != candidate.source_coverage.value
                or str(result["output_display_kind"]) != candidate.display_kind.value
                or result["output_source_coverage_reason"]
                != (
                    None
                    if candidate.source_coverage_reason is None
                    else candidate.source_coverage_reason.value
                )
                or result["output_artifact_unavailability_reason"]
                != (
                    None
                    if candidate.artifact_unavailability_reason is None
                    else candidate.artifact_unavailability_reason.value
                )
            ):
                raise ConversationKernelConflict(
                    "tool result identity names a different winner"
                )
            result_event = self._exact_event_for_confirmation(
                connection,
                candidate.tool_result_occurrence,
                session_id=candidate.session_id,
                workspace_id=candidate.workspace_id,
            )
            if blob is not None:
                self._require_exact_tool_artifact_blob(
                    connection,
                    workspace_id=candidate.workspace_id,
                    expected=blob,
                )
            side = candidate.side_branch
            if isinstance(side, PreparedMemoryProposalSideBranch):
                self._confirm_memory_proposal_side_branch(connection, candidate, side)
            return AcceptedEntry(
                entry_id=candidate.result_entry_id,
                turn_id=candidate.turn_id,
                entry_sequence=int(entry["entry_sequence"]),
                event_sequence=int(result_event["event_sequence"]),
            )

    @staticmethod
    def _exact_event_for_confirmation(
        connection: Connection,
        expected: CommittedEventDraft,
        *,
        session_id: str,
        workspace_id: str,
    ) -> Mapping[str, object]:
        rows = connection.execute(
            "SELECT * FROM pulsara_v3.agent_events WHERE event_id = %s",
            (expected.event_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ConversationKernelConflict(
                "prepared tool result occurrence is absent or non-unique"
            )
        row = rows[0]
        subject_column = DESCRIPTOR_BY_TYPE[expected.event_type].subject_slot.value
        if (
            str(row["session_id"]) != session_id
            or str(row["workspace_id"]) != workspace_id
            or str(row["event_type"]) != expected.event_type.value
            or row[subject_column] != expected.subject.subject_id
            or row["occurred_at"] != expected.occurred_at
            or str(row["actor_kind"]) != expected.actor_kind
            or str(row["actor_id"]) != expected.actor_id
            or str(row["sensitivity_class"]) != expected.sensitivity_class
            or str(row["projection_profile"]) != expected.projection_profile
            or dict(row["payload"]) != dict(expected.payload)
        ):
            raise ConversationKernelConflict(
                "prepared occurrence identity names a different winner"
            )
        return row

    @staticmethod
    def _require_exact_tool_artifact_blob(
        connection: Connection,
        *,
        workspace_id: str,
        expected: BlobContent,
    ) -> None:
        row = connection.execute(
            """
            SELECT id, logical_digest, logical_size, media_type, codec
            FROM pulsara_v3.blobs
            WHERE id = %s AND workspace_id = %s
            """,
            (expected.blob_id, workspace_id),
        ).fetchone()
        if (
            row is None
            or BlobContent(
                blob_id=str(row["id"]),
                digest=str(row["logical_digest"]),
                size=int(row["logical_size"]),
                media_type=str(row["media_type"]),
                codec=str(row["codec"]),
            )
            != expected
        ):
            raise ConversationKernelConflict(
                "prepared tool artifact descriptor names a different blob"
            )

    def _confirm_memory_proposal_side_branch(
        self,
        connection: Connection,
        candidate: PreparedToolResultAcceptance,
        side: PreparedMemoryProposalSideBranch,
    ) -> None:
        memory = connection.execute(
            """
            SELECT * FROM pulsara_v3.memory_candidates
            WHERE workspace_id = %s AND id = %s
            """,
            (candidate.workspace_id, side.memory_candidate_id),
        ).fetchone()
        job = connection.execute(
            """
            SELECT * FROM pulsara_v3.durable_jobs
            WHERE workspace_id = %s AND id = %s
            """,
            (candidate.workspace_id, side.governance_job_id),
        ).fetchone()
        if memory is None or job is None:
            raise ConversationKernelConflict(
                "prepared memory proposal side branch is only partially installed"
            )
        if (
            str(memory["origin_session_id"]) != candidate.session_id
            or str(memory["source_entry_id"]) != candidate.assistant_entry_id
            or str(memory["proposal_kind"]) != side.proposal_kind
            or str(memory["semantic_digest"]) != side.candidate_semantic_digest
            or dict(memory["proposal_payload"]) != dict(side.proposal_payload)
            or str(memory["status"]) != "PENDING"
            or str(job["origin_session_id"]) != candidate.session_id
            or str(job["handler_type"]) != side.job_handler_type
            or str(job["intent_schema_version"]) != side.intent_schema_version
            or str(job["intent_digest"]) != side.intent_digest
            or dict(job["intent_payload"]) != dict(side.intent_payload)
            or str(job["automatic_intent_key"]) != side.automatic_intent_key
            or str(job["safety_class"]) != side.safety_class
            or str(job["status"]) != side.initial_status
            or str(job["retry_policy_id"]) != side.retry_policy_id
            or int(job["retry_policy_version"]) != side.retry_policy_version
            or int(job["maximum_attempts"]) != side.maximum_attempts
            or int(job["attempt_timeout_ms"]) != side.attempt_timeout_ms
            or int(job["provider_input_token_limit_per_attempt"])
            != side.provider_input_token_limit_per_attempt
            or int(job["provider_output_token_limit_per_attempt"])
            != side.provider_output_token_limit_per_attempt
            or job["next_eligible_at"] != side.next_eligible_at
        ):
            raise ConversationKernelConflict(
                "prepared memory proposal side branch names a different winner"
            )
        self._exact_event_for_confirmation(
            connection,
            side.job_queued_occurrence,
            session_id=candidate.session_id,
            workspace_id=candidate.workspace_id,
        )

    def accept_tool_interaction_decision(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        decision_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        decision: str,
        attempt_id: str | None,
        result_id: str | None,
        result_entry_id: str | None,
        denial_content: CanonicalContent | None,
        redacted_subject: str,
        actor_id: str,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedInteractionDecision:
        """Accept one human tool decision and its physical-effect boundary.

        ALLOW installs the exact execution attempt in this transaction.  DENY
        installs the no-attempt tool result and its transcript entry.  The
        process-local pending request is deliberately not represented here.
        """

        allow = decision == "ALLOW"
        deny = decision == "DENY"
        if not (allow or deny):
            raise ValueError("tool interaction decision must be ALLOW or DENY")
        if allow != (
            attempt_id is not None
            and result_id is None
            and result_entry_id is None
            and denial_content is None
        ):
            raise ValueError("allowed interaction effect union is invalid")
        if deny != (
            attempt_id is None
            and result_id is not None
            and result_entry_id is not None
            and denial_content is not None
        ):
            raise ValueError("denied interaction effect union is invalid")
        if not redacted_subject or len(redacted_subject.encode("utf-8")) > 4096:
            raise ValueError("interaction redacted subject is outside its bound")
        semantic_digest = canonical_digest(
            "pulsara:resolve-tool-interaction:v1",
            {
                "assistant_entry_id": assistant_entry_id,
                "tool_call_id": tool_call_id,
                "decision": decision,
            },
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                """
                SELECT c.semantic_digest, c.target_interaction_decision_id,
                       d.decision, d.subject_tool_call_entry_id,
                       d.subject_tool_call_id,
                       a.id AS attempt_id, r.result_entry_id
                FROM pulsara_v3.session_commands AS c
                JOIN pulsara_v3.interaction_decisions AS d
                  ON d.session_id = c.session_id
                 AND d.id = c.target_interaction_decision_id
                LEFT JOIN pulsara_v3.tool_execution_attempts AS a
                  ON a.session_id = d.session_id
                 AND a.assistant_entry_id = d.subject_tool_call_entry_id
                 AND a.tool_call_id = d.subject_tool_call_id
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = d.session_id
                 AND r.tool_call_entry_id = d.subject_tool_call_entry_id
                 AND r.tool_call_id = d.subject_tool_call_id
                WHERE c.session_id = %s AND c.command_id = %s
                  AND c.command_kind = 'RESOLVE_INTERACTION'
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["semantic_digest"] != semantic_digest
                    or existing["target_interaction_decision_id"] != decision_id
                    or existing["decision"] != decision
                    or existing["subject_tool_call_entry_id"] != assistant_entry_id
                    or existing["subject_tool_call_id"] != tool_call_id
                    or existing["attempt_id"] != attempt_id
                    or existing["result_entry_id"] != result_entry_id
                ):
                    raise ConversationKernelConflict(
                        "interaction command identity conflict"
                    )
                return AcceptedInteractionDecision(
                    decision_id,
                    command_id,
                    decision,
                    assistant_entry_id,
                    tool_call_id,
                    attempt_id,
                    result_entry_id,
                )
            subject = connection.execute(
                """
                SELECT e.turn_id, e.workspace_id, e.conversation_scope_kind,
                       e.scope_subagent_task_id
                FROM pulsara_v3.assistant_message_blocks AS b
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = b.session_id
                 AND e.id = b.assistant_entry_id
                JOIN pulsara_v3.turns AS t
                  ON t.session_id = e.session_id AND t.id = e.turn_id
                WHERE b.session_id = %s AND b.assistant_entry_id = %s
                  AND b.tool_call_id = %s AND b.block_kind = 'TOOL_CALL'
                  AND t.status = 'RUNNING'
                FOR UPDATE OF t
                """,
                (guard.session_id, assistant_entry_id, tool_call_id),
            ).fetchone()
            if subject is None:
                raise ConversationKernelConflict(
                    "interaction tool-call subject is not active"
                )
            prior_effect = connection.execute(
                """
                SELECT 1 FROM pulsara_v3.tool_execution_attempts
                WHERE session_id = %s AND assistant_entry_id = %s
                  AND tool_call_id = %s
                UNION ALL
                SELECT 1 FROM pulsara_v3.tool_results
                WHERE session_id = %s AND tool_call_entry_id = %s
                  AND tool_call_id = %s
                LIMIT 1
                """,
                (
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                    guard.session_id,
                    assistant_entry_id,
                    tool_call_id,
                ),
            ).fetchone()
            if prior_effect is not None:
                raise ConversationKernelConflict(
                    "interaction subject already has a physical outcome"
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.interaction_decisions (
                    id, session_id, command_id, subject_kind,
                    subject_tool_call_entry_id, subject_tool_call_id,
                    decision, actor_kind, actor_id, redacted_subject
                ) VALUES (%s, %s, %s, 'TOOL_CALL', %s, %s, %s,
                          'human', %s, %s)
                """,
                (
                    decision_id,
                    guard.session_id,
                    command_id,
                    assistant_entry_id,
                    tool_call_id,
                    decision,
                    actor_id,
                    redacted_subject,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_interaction_decision_id
                ) VALUES (%s, %s, 'RESOLVE_INTERACTION',
                          'resolve_tool_interaction.v1', %s,
                          'INTERACTION_DECISION', %s)
                """,
                (guard.session_id, command_id, semantic_digest, decision_id),
            )
            drafts = [
                self._event(
                    CommittedEventType.INTERACTION_DECISION_ACCEPTED,
                    SubjectSlot.INTERACTION_DECISION,
                    decision_id,
                    occurred_at=occurred_at,
                    actor_kind="human",
                    actor_id=actor_id,
                    payload={"decision": decision, "subject_kind": "TOOL_CALL"},
                )
            ]
            if allow:
                assert attempt_id is not None
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_execution_attempts (
                        id, session_id, assistant_entry_id, tool_call_id,
                        authorization_kind, authorization_reference,
                        actor_kind, actor_id
                    ) VALUES (%s, %s, %s, %s, 'human', %s,
                              'runtime', 'foreground-tool-executor')
                    """,
                    (
                        attempt_id,
                        guard.session_id,
                        assistant_entry_id,
                        tool_call_id,
                        f"interaction-decision:{decision_id}",
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_ATTEMPT_ACCEPTED,
                        SubjectSlot.TOOL_ATTEMPT,
                        attempt_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id="foreground-tool-executor",
                        payload={
                            "assistant_entry_id": assistant_entry_id,
                            "tool_call_id": tool_call_id,
                        },
                    )
                )
            else:
                assert result_id is not None
                assert result_entry_id is not None
                assert denial_content is not None
                entry_sequence = self._allocate_entry_sequence(
                    connection, guard.session_id
                )
                self._insert_entry(
                    connection,
                    session_id=guard.session_id,
                    workspace_id=str(subject["workspace_id"]),
                    turn_id=str(subject["turn_id"]),
                    entry_id=result_entry_id,
                    entry_sequence=entry_sequence,
                    entry_kind=EntryKind.TOOL_RESULT,
                    scope_kind=ConversationScopeKind(
                        str(subject["conversation_scope_kind"])
                    ),
                    scope_task_id=subject["scope_subagent_task_id"],
                    content=denial_content,
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.tool_results (
                        id, session_id, workspace_id,
                        tool_call_entry_id, tool_call_id,
                        attempt_id, result_entry_id, result_state
                    ) VALUES (%s, %s, %s, %s, %s, NULL, %s, 'PERMISSION_DENIED')
                    """,
                    (
                        result_id,
                        guard.session_id,
                        subject["workspace_id"],
                        assistant_entry_id,
                        tool_call_id,
                        result_entry_id,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.TOOL_RESULT_ACCEPTED,
                        SubjectSlot.ENTRY,
                        result_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="tool",
                        actor_id="permission",
                        payload={
                            "tool_call_id": tool_call_id,
                            "result_state": "PERMISSION_DENIED",
                        },
                    )
                )
            self._append_events(
                connection,
                guard,
                workspace_id=str(subject["workspace_id"]),
                drafts=tuple(drafts),
            )
        return AcceptedInteractionDecision(
            decision_id,
            command_id,
            decision,
            assistant_entry_id,
            tool_call_id,
            attempt_id,
            result_entry_id,
        )

    def interrupt_turn(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        reason: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> bool:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET status = 'INTERRUPTED', terminal_reason = %s,
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'RUNNING'
                RETURNING workspace_id
                """,
                (reason, guard.session_id, turn_id),
            ).fetchone()
            if row is None:
                return False
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.TURN_INTERRUPTED,
                        SubjectSlot.TURN,
                        turn_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"reason": reason},
                    ),
                ),
            )
            return True

    def accept_subagent_task(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        parent_turn_id: str,
        objective: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> None:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            parent = connection.execute(
                """
                SELECT conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, parent_turn_id),
            ).fetchone()
            if parent is None or parent["conversation_scope_kind"] != "ROOT":
                raise ConversationKernelConflict("subagent parent is not a ROOT turn")
            connection.execute(
                """
                INSERT INTO pulsara_v3.subagent_tasks (
                    id, session_id, workspace_id, parent_turn_id,
                    objective, status, execution_writer_generation
                ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s)
                """,
                (
                    task_id,
                    guard.session_id,
                    workspace_id,
                    parent_turn_id,
                    objective,
                    guard.writer_generation,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.SUBAGENT_TASK_ACCEPTED,
                        SubjectSlot.SUBAGENT_TASK,
                        task_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"status": "PENDING"},
                    ),
                ),
            )

    def set_subagent_task_status(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        status: str,
        reason: str | None,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> bool:
        if status not in {
            "ACTIVE",
            "COMPLETED",
            "FAILED",
            "INTERRUPTED",
            "CANCELLED",
        }:
            raise ValueError("subagent transition status is invalid")
        terminal = status in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                UPDATE pulsara_v3.subagent_tasks
                SET status = %s, terminal_reason = %s,
                    terminal_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
                WHERE session_id = %s AND id = %s
                  AND execution_writer_generation = %s
                  AND (
                    (status = 'PENDING' AND %s = 'ACTIVE') OR
                    (status IN ('PENDING', 'ACTIVE') AND %s IN (
                      'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED'
                    ))
                  )
                RETURNING workspace_id
                """,
                (
                    status,
                    reason,
                    terminal,
                    guard.session_id,
                    task_id,
                    guard.writer_generation,
                    status,
                    status,
                ),
            ).fetchone()
            if row is None:
                return False
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                        SubjectSlot.SUBAGENT_TASK,
                        task_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"status": status, "reason": reason},
                    ),
                ),
            )
            return True

    def start_subagent_turn(
        self,
        guard: HostWriterGuard,
        *,
        task_id: str,
        turn_id: str,
        entry_id: str,
        context_binding_revision_id: str,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            task = connection.execute(
                """
                SELECT workspace_id FROM pulsara_v3.subagent_tasks
                WHERE session_id = %s AND id = %s AND status = 'ACTIVE'
                  AND execution_writer_generation = %s
                FOR UPDATE
                """,
                (guard.session_id, task_id, guard.writer_generation),
            ).fetchone()
            if task is None:
                raise ConversationKernelConflict("subagent task is not active")
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    scope_subagent_task_id, status, initial_entry_id,
                    current_context_binding_revision_id
                ) VALUES (%s, %s, %s, 'SUBAGENT_TASK', %s,
                          'RUNNING', %s, %s)
                """,
                (
                    turn_id,
                    guard.session_id,
                    task["workspace_id"],
                    task_id,
                    entry_id,
                    context_binding_revision_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turn_context_binding_revisions (
                    id, session_id, turn_id, revision_ordinal,
                    base_kind, source_through_sequence
                ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
                """,
                (
                    context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    entry_sequence - 1,
                ),
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(task["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.SUBAGENT_TASK,
                scope_task_id=task_id,
                content=content,
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(task["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        entry_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"source": "SUBAGENT_TASK_OBJECTIVE"},
                    ),
                ),
            )[0]
            return AcceptedEntry(
                entry_id, turn_id, entry_sequence, event.event_sequence
            )

    def accept_subagent_child(
        self,
        guard: HostWriterGuard,
        *,
        child_id: str,
        task_id: str,
        child_kind: str,
        child_ordinal: int,
        entry_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> None:
        if child_kind not in {"MESSAGE", "RESULT"} or child_ordinal < 0:
            raise ValueError("subagent child carrier is invalid")
        event_type = (
            CommittedEventType.SUBAGENT_MESSAGE_ACCEPTED
            if child_kind == "MESSAGE"
            else CommittedEventType.SUBAGENT_RESULT_ACCEPTED
        )
        slot = (
            SubjectSlot.SUBAGENT_MESSAGE
            if child_kind == "MESSAGE"
            else SubjectSlot.SUBAGENT_RESULT
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            existing = connection.execute(
                """
                SELECT task_id, child_kind, child_ordinal, entry_id
                FROM pulsara_v3.subagent_task_children
                WHERE session_id = %s AND id = %s
                """,
                (guard.session_id, child_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["task_id"]) != task_id
                    or str(existing["child_kind"]) != child_kind
                    or int(existing["child_ordinal"]) != child_ordinal
                    or str(existing["entry_id"]) != entry_id
                ):
                    raise ConversationKernelConflict(
                        "subagent child identity names a different fact"
                    )
                return
            counts = connection.execute(
                """
                SELECT count(*) FILTER (WHERE child_kind = 'MESSAGE') AS messages,
                       count(*) FILTER (WHERE child_kind = 'RESULT') AS results
                FROM pulsara_v3.subagent_task_children
                WHERE session_id = %s AND task_id = %s
                """,
                (guard.session_id, task_id),
            ).fetchone()
            message_count = int(counts["messages"])
            result_count = int(counts["results"])
            if result_count or child_ordinal != message_count:
                raise ConversationKernelConflict(
                    "subagent child ordinal or terminal result conflicts"
                )
            connection.execute(
                """
                INSERT INTO pulsara_v3.subagent_task_children (
                    id, session_id, task_id, child_kind, child_ordinal, entry_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    child_id,
                    guard.session_id,
                    task_id,
                    child_kind,
                    child_ordinal,
                    entry_id,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        event_type,
                        slot,
                        child_id,
                        occurred_at=occurred_at,
                        actor_kind="subagent",
                        actor_id=actor_id,
                        payload={"child_ordinal": child_ordinal},
                    ),
                ),
            )

    def query_subagent_task(
        self,
        *,
        session_id: str,
        task_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object] | None:
        """Read durable task/result state without recovering execution."""
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT t.id, t.parent_turn_id, t.objective, t.status,
                       t.terminal_reason, c.id AS result_id,
                       c.entry_id AS result_entry_id,
                       accepted.id AS accepted_root_entry_id
                FROM pulsara_v3.subagent_tasks AS t
                LEFT JOIN pulsara_v3.subagent_task_children AS c
                  ON c.session_id = t.session_id AND c.task_id = t.id
                 AND c.child_kind = 'RESULT'
                LEFT JOIN pulsara_v3.transcript_entries AS accepted
                  ON accepted.session_id = c.session_id
                 AND accepted.source_subagent_result_id = c.id
                WHERE t.session_id = %s AND t.id = %s
                """,
                (session_id, task_id),
            ).fetchone()
            return None if row is None else dict(row)

    def list_subagent_tasks(
        self,
        *,
        session_id: str,
        maximum_items: int,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= maximum_items <= 50:
            raise ValueError("subagent list bound is invalid")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT t.id, t.parent_turn_id, t.objective, t.status,
                           t.terminal_reason, c.id AS result_id,
                           c.entry_id AS result_entry_id,
                           accepted.id AS accepted_root_entry_id
                    FROM pulsara_v3.subagent_tasks AS t
                    LEFT JOIN pulsara_v3.subagent_task_children AS c
                      ON c.session_id = t.session_id AND c.task_id = t.id
                     AND c.child_kind = 'RESULT'
                    LEFT JOIN pulsara_v3.transcript_entries AS accepted
                      ON accepted.session_id = c.session_id
                     AND accepted.source_subagent_result_id = c.id
                    WHERE t.session_id = %s
                    ORDER BY t.accepted_at DESC, t.id DESC LIMIT %s
                    """,
                    (session_id, maximum_items),
                ).fetchall()
            )

    def enqueue_job(
        self,
        guard: HostWriterGuard,
        *,
        job_id: str,
        handler_type: str,
        intent_schema_version: str,
        intent_payload: Mapping[str, object],
        automatic_intent_key: str | None,
        safety_class: JobSafetyClass,
        retry_policy_id: str,
        retry_policy_version: int,
        maximum_attempts: int,
        attempt_timeout_ms: int,
        provider_input_token_limit_per_attempt: int | None,
        provider_output_token_limit_per_attempt: int | None,
        next_eligible_at: datetime,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> None:
        contract = job_handler_contract(handler_type)
        if (
            safety_class is not contract.safety_class
            or retry_policy_id != contract.retry_policy_id
            or retry_policy_version != contract.retry_policy_version
            or maximum_attempts != contract.maximum_attempts
            or attempt_timeout_ms != contract.attempt_timeout_ms
            or provider_input_token_limit_per_attempt != contract.input_token_limit
            or provider_output_token_limit_per_attempt != contract.output_token_limit
        ):
            raise ValueError("job policy does not match the closed handler catalog")
        intent_digest = canonical_digest(
            f"pulsara:job-intent:{intent_schema_version}", dict(intent_payload)
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            workspace_id = self._workspace_id(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'PENDING', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    workspace_id,
                    guard.session_id,
                    handler_type,
                    intent_schema_version,
                    intent_digest,
                    Jsonb(dict(intent_payload)),
                    automatic_intent_key,
                    safety_class.value,
                    retry_policy_id,
                    retry_policy_version,
                    maximum_attempts,
                    attempt_timeout_ms,
                    provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt,
                    next_eligible_at,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.JOB_QUEUED,
                        SubjectSlot.JOB,
                        job_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"handler_type": handler_type},
                    ),
                ),
            )

    def enqueue_background_compaction(
        self,
        guard: HostWriterGuard,
        *,
        job_id: str,
        source_through_sequence: int,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> None:
        contract = job_handler_contract(BACKGROUND_COMPACTION)
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            session = self._require_writer(connection, guard, lock=False)
            if source_through_sequence > int(session["latest_entry_sequence"]):
                raise ConversationKernelConflict(
                    "compaction source exceeds canonical head"
                )
            rows = _load_root_transcript_cut(
                connection,
                session_id=guard.session_id,
                through_sequence=source_through_sequence,
            )
            source_digest = canonical_digest(
                "pulsara:background-compaction-source:v1", rows
            )
            intent = {
                "session_id": guard.session_id,
                "source_through_sequence": source_through_sequence,
                "source_digest": source_digest,
            }
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (
                    %s, %s, %s, 'BACKGROUND_COMPACTION',
                    'background_compaction.v1', %s, %s, %s,
                    'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                    %s, %s, %s, %s, clock_timestamp()
                )
                """,
                (
                    job_id,
                    session["workspace_id"],
                    guard.session_id,
                    canonical_digest(
                        "pulsara:job-intent:background_compaction.v1", intent
                    ),
                    Jsonb(intent),
                    f"background-compaction:{guard.session_id}:{source_through_sequence}",
                    contract.maximum_attempts,
                    contract.attempt_timeout_ms,
                    contract.input_token_limit,
                    contract.output_token_limit,
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(session["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.JOB_QUEUED,
                        SubjectSlot.JOB,
                        job_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"handler_type": "BACKGROUND_COMPACTION"},
                    ),
                ),
            )

    def enqueue_prompt(
        self,
        guard: HostWriterGuard,
        *,
        command_id: str,
        queue_item_id: str,
        client_submission_id: str,
        delivery_mode: PromptDeliveryMode,
        target_turn_id: str | None,
        content: CanonicalContent,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> int:
        if (delivery_mode is PromptDeliveryMode.NEW_TURN) != (target_turn_id is None):
            raise ValueError("prompt delivery target union is invalid")
        digest = canonical_digest(
            "pulsara:queue-prompt-command:v1",
            {
                "queue_item_id": queue_item_id,
                "client_submission_id": client_submission_id,
                "delivery_mode": delivery_mode.value,
                "target_turn_id": target_turn_id,
                "content_digest": content.digest,
            },
        )
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            existing = connection.execute(
                """
                SELECT semantic_digest, target_queue_item_id
                FROM pulsara_v3.session_commands
                WHERE session_id = %s AND command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["semantic_digest"] != digest
                    or existing["target_queue_item_id"] != queue_item_id
                ):
                    raise ConversationKernelConflict("queue command conflict")
                row = connection.execute(
                    """
                    SELECT queue_sequence FROM pulsara_v3.prompt_queue_items
                    WHERE session_id = %s AND id = %s
                    """,
                    (guard.session_id, queue_item_id),
                ).fetchone()
                if row is None:
                    raise ConversationKernelConflict("queue command target is absent")
                return int(row["queue_sequence"])
            if target_turn_id is not None:
                target = connection.execute(
                    """
                    SELECT conversation_scope_kind, status
                    FROM pulsara_v3.turns
                    WHERE session_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (guard.session_id, target_turn_id),
                ).fetchone()
                if target is None or target["conversation_scope_kind"] != "ROOT":
                    raise ConversationKernelConflict("steer target is not a ROOT turn")
                if target["status"] != "RUNNING":
                    raise ConversationKernelConflict("steer target is terminal")
            pending = connection.execute(
                """SELECT count(*) AS total
                   FROM pulsara_v3.prompt_queue_items
                   WHERE session_id = %s AND status = 'PENDING'""",
                (guard.session_id,),
            ).fetchone()
            if int(pending["total"]) >= STAGE2_LIMITS.pending_prompt_hard_items:
                raise ConversationKernelConflict("prompt queue capacity is exhausted")
            row = connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET latest_prompt_queue_sequence = latest_prompt_queue_sequence + 1,
                    updated_at = clock_timestamp()
                WHERE id = %s
                RETURNING workspace_id, latest_prompt_queue_sequence
                """,
                (guard.session_id,),
            ).fetchone()
            assert row is not None
            queue_sequence = int(row["latest_prompt_queue_sequence"])
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_queue_item_id
                ) VALUES (%s, %s, 'QUEUE_PROMPT', 'queue_prompt.v1',
                          %s, 'QUEUE_ITEM', %s)
                """,
                (guard.session_id, command_id, digest, queue_item_id),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.prompt_queue_items (
                    id, session_id, workspace_id, queue_sequence,
                    command_id, client_submission_id, delivery_mode,
                    target_turn_id, status, inline_content, blob_id,
                    content_digest, content_size, content_media_type,
                    content_codec
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING',
                          %s, %s, %s, %s, %s, %s)
                """,
                (
                    queue_item_id,
                    guard.session_id,
                    row["workspace_id"],
                    queue_sequence,
                    command_id,
                    client_submission_id,
                    delivery_mode.value,
                    target_turn_id,
                    *_content_columns(content),
                ),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_QUEUED,
                        SubjectSlot.QUEUE_ITEM,
                        queue_item_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={
                            "queue_sequence": queue_sequence,
                            "delivery_mode": delivery_mode.value,
                        },
                    ),
                ),
            )
            return queue_sequence

    def consume_prompt_head(
        self,
        guard: HostWriterGuard,
        *,
        new_turn_id: str,
        new_entry_id: str,
        new_context_binding_revision_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            self._reject_terminal_prompt_steer_heads(
                connection,
                guard,
                occurred_at=occurred_at,
                actor_id=actor_id,
            )
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                ORDER BY queue_sequence, id
                LIMIT 1
                FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            if item is None:
                return None
            if item["delivery_mode"] == PromptDeliveryMode.STEER_ACTIVE_TURN.value:
                # Every terminal steer in the bounded FIFO prefix has already
                # been rejected.  The remaining head belongs to the exact
                # still-running target and cannot be overtaken by a NEW_TURN.
                return None
            content = self._content_from_row(item)
            workspace_id = str(item["workspace_id"])
            turn_id = new_turn_id
            entry_kind = EntryKind.USER_MESSAGE
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            connection.execute(
                """
                INSERT INTO pulsara_v3.turns (
                    id, session_id, workspace_id, conversation_scope_kind,
                    status, initial_entry_id, current_context_binding_revision_id
                ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s)
                """,
                (
                    turn_id,
                    guard.session_id,
                    workspace_id,
                    new_entry_id,
                    new_context_binding_revision_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.turn_context_binding_revisions (
                    id, session_id, turn_id, revision_ordinal, base_kind,
                    source_through_sequence
                ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
                """,
                (
                    new_context_binding_revision_id,
                    guard.session_id,
                    turn_id,
                    entry_sequence - 1,
                ),
            )
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                entry_id=new_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=entry_kind,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
            )
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'CONSUMED', consumed_entry_id = %s,
                    terminal_reason = 'CONSUMED', terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (new_entry_id, guard.session_id, item["id"]),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("prompt queue terminal CAS lost")
            events = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_CONSUMED,
                        SubjectSlot.QUEUE_ITEM,
                        str(item["id"]),
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"entry_id": new_entry_id},
                    ),
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        new_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={"source": "PROMPT_QUEUE"},
                    ),
                ),
            )
            return AcceptedEntry(
                entry_id=new_entry_id,
                turn_id=turn_id,
                entry_sequence=entry_sequence,
                event_sequence=events[-1].event_sequence,
            )

    def _reject_terminal_prompt_steer_heads(
        self,
        connection: Connection,
        guard: HostWriterGuard,
        *,
        occurred_at: datetime,
        actor_id: str,
    ) -> None:
        """Reject the complete bounded prefix of terminal-target steers."""

        for _ in range(STAGE2_LIMITS.pending_prompt_hard_items):
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                ORDER BY queue_sequence, id
                LIMIT 1
                FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            if (
                item is None
                or item["delivery_mode"] != PromptDeliveryMode.STEER_ACTIVE_TURN.value
            ):
                return
            target = connection.execute(
                """
                SELECT conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s FOR UPDATE
                """,
                (guard.session_id, item["target_turn_id"]),
            ).fetchone()
            if (
                target is not None
                and target["conversation_scope_kind"] == "ROOT"
                and target["status"] == "RUNNING"
            ):
                return
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'REJECTED', terminal_reason = 'TARGET_TURN_TERMINAL',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (guard.session_id, item["id"]),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("stale steer rejection CAS lost")
            self._append_events(
                connection,
                guard,
                workspace_id=str(item["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_REJECTED,
                        SubjectSlot.QUEUE_ITEM,
                        str(item["id"]),
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"reason": "TARGET_TURN_TERMINAL"},
                    ),
                ),
            )

    def consume_prompt_steer_for_turn(
        self,
        guard: HostWriterGuard,
        *,
        target_turn_id: str,
        new_entry_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Consume the global FIFO head when it is this turn's steer."""

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            item = connection.execute(
                """
                SELECT * FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                ORDER BY queue_sequence, id
                LIMIT 1 FOR UPDATE
                """,
                (guard.session_id,),
            ).fetchone()
            if item is None:
                return None
            if item["delivery_mode"] == PromptDeliveryMode.NEW_TURN.value:
                # A later steer cannot overtake the globally oldest NEW_TURN.
                return None
            target = connection.execute(
                """
                SELECT conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s FOR UPDATE
                """,
                (guard.session_id, item["target_turn_id"]),
            ).fetchone()
            workspace_id = str(item["workspace_id"])
            if (
                target is None
                or target["conversation_scope_kind"] != "ROOT"
                or target["status"] != "RUNNING"
            ):
                connection.execute(
                    """
                    UPDATE pulsara_v3.prompt_queue_items
                    SET status = 'REJECTED', terminal_reason = 'TARGET_TURN_TERMINAL',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND id = %s AND status = 'PENDING'
                    """,
                    (guard.session_id, item["id"]),
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=(
                        self._event(
                            CommittedEventType.PROMPT_REJECTED,
                            SubjectSlot.QUEUE_ITEM,
                            str(item["id"]),
                            occurred_at=occurred_at,
                            actor_kind="runtime",
                            actor_id=actor_id,
                            payload={"reason": "TARGET_TURN_TERMINAL"},
                        ),
                    ),
                )
                return None
            if str(item["target_turn_id"]) != target_turn_id:
                # A different still-running ROOT owns the global head.  There
                # is no redirection or overtaking across target identities.
                return None
            entry_sequence = self._allocate_entry_sequence(connection, guard.session_id)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=workspace_id,
                turn_id=target_turn_id,
                entry_id=new_entry_id,
                entry_sequence=entry_sequence,
                entry_kind=EntryKind.USER_STEER,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=self._content_from_row(item),
            )
            updated = connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'CONSUMED', consumed_entry_id = %s,
                    terminal_reason = 'CONSUMED', terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (new_entry_id, guard.session_id, item["id"]),
            ).fetchone()
            if updated is None:
                raise ConversationKernelConflict("steer prompt terminal CAS lost")
            events = self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_CONSUMED,
                        SubjectSlot.QUEUE_ITEM,
                        str(item["id"]),
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=actor_id,
                        payload={"entry_id": new_entry_id},
                    ),
                    self._event(
                        CommittedEventType.USER_STEER_ACCEPTED,
                        SubjectSlot.ENTRY,
                        new_entry_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={"source": "PROMPT_QUEUE"},
                    ),
                ),
            )
            return AcceptedEntry(
                new_entry_id,
                target_turn_id,
                entry_sequence,
                events[-1].event_sequence,
            )

    def accept_subagent_result_into_root(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        child_result_id: str,
        command_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Accept one exact completed child result into an explicit ROOT target.

        The caller must own the process-local provider safe-point lock.  The
        source child is independently durable; this transaction owns only the
        unique ROOT-visible acceptance and its idempotent command.  A missing
        ``new_context_binding_revision_id`` means an existing RUNNING ROOT;
        otherwise this command creates a fresh ROOT turn.  Neither branch
        resumes or redirects child execution.
        """

        if not child_result_id or not command_id:
            raise ValueError("external result acceptance identity is empty")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT t.workspace_id, c.id AS child_id, c.entry_id,
                       e.inline_content, e.blob_id,
                       e.content_digest, e.content_size, e.content_media_type,
                       e.content_codec, accepted.id AS accepted_entry_id
                FROM pulsara_v3.subagent_tasks AS t
                JOIN pulsara_v3.subagent_task_children AS c
                  ON c.session_id = t.session_id AND c.task_id = t.id
                 AND c.child_kind = 'RESULT'
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.entry_id
                LEFT JOIN pulsara_v3.transcript_entries AS accepted
                  ON accepted.session_id = c.session_id
                 AND accepted.source_subagent_result_id = c.id
                WHERE t.session_id = %s AND t.status = 'COMPLETED' AND c.id = %s
                FOR UPDATE OF t
                """,
                (guard.session_id, child_result_id),
            ).fetchone()
            entry_id = "entry:" + sha256(command_id.encode()).hexdigest()
            if row is None:
                return None
            child_id = str(row["child_id"])
            digest = canonical_digest(
                "pulsara:accept-subagent-result:v1",
                {
                    "turn_id": turn_id,
                    "new_context_binding_revision_id": (
                        new_context_binding_revision_id
                    ),
                    "source_subagent_result_id": child_id,
                    "content_digest": str(row["content_digest"]),
                },
            )
            existing = connection.execute(
                """
                SELECT c.command_kind, c.semantic_digest, c.target_entry_id,
                       e.turn_id, e.entry_sequence,
                       e.source_subagent_result_id, a.event_sequence
                FROM pulsara_v3.session_commands AS c
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.target_entry_id
                LEFT JOIN pulsara_v3.agent_events AS a
                  ON a.session_id = e.session_id
                 AND a.subject_entry_id = e.id
                 AND a.event_type = 'UserMessageAccepted'
                WHERE c.session_id = %s AND c.command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_kind"] != "ACCEPT_SUBAGENT_RESULT"
                    or existing["semantic_digest"] != digest
                    or existing["target_entry_id"] != entry_id
                    or existing["turn_id"] != turn_id
                    or existing["source_subagent_result_id"] != child_id
                    or existing["event_sequence"] is None
                ):
                    raise ConversationKernelConflict(
                        "subagent result acceptance command conflict"
                    )
                return AcceptedEntry(
                    entry_id,
                    turn_id,
                    int(existing["entry_sequence"]),
                    int(existing["event_sequence"]),
                )
            if row["accepted_entry_id"] is not None:
                return None
            sequence = self._prepare_external_result_target(
                connection,
                guard,
                turn_id=turn_id,
                entry_id=entry_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                source_workspace_id=str(row["workspace_id"]),
            )
            if sequence is None:
                return None
            content = self._content_from_row(row)
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(row["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
                source_subagent_result_id=child_id,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_entry_id
                ) VALUES (%s, %s, 'ACCEPT_SUBAGENT_RESULT',
                          'accept_subagent_result.v1', %s, 'ENTRY', %s)
                """,
                (guard.session_id, command_id, digest, entry_id),
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        entry_id,
                        occurred_at=occurred_at,
                        actor_kind="subagent",
                        actor_id=actor_id,
                        payload={
                            "source_subagent_result_id": child_id,
                            "source_entry_id": str(row["entry_id"]),
                        },
                    ),
                ),
            )[0]
            return AcceptedEntry(entry_id, turn_id, sequence, event.event_sequence)

    def accept_job_result_into_root(
        self,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        new_context_binding_revision_id: str | None = None,
        job_id: str,
        command_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> AcceptedEntry | None:
        """Accept one immutable SUCCEEDED job result into an explicit ROOT target."""

        if not job_id or not command_id:
            raise ValueError("job result acceptance identity is empty")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            entry_id = "entry:" + sha256(command_id.encode()).hexdigest()
            job = connection.execute(
                """
                SELECT j.workspace_id, j.status, j.result_blob_id,
                       a.result_payload, accepted.id AS accepted_entry_id
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                LEFT JOIN pulsara_v3.transcript_entries AS accepted
                  ON accepted.session_id = j.origin_session_id
                 AND accepted.source_job_id = j.id
                WHERE j.origin_session_id = %s AND j.id = %s
                  AND j.status = 'SUCCEEDED'
                  AND a.terminal_status = 'SUCCEEDED'
                ORDER BY a.attempt_ordinal DESC
                LIMIT 1
                FOR UPDATE OF j, a
                """,
                (guard.session_id, job_id),
            ).fetchone()
            if job is None:
                return None
            if job["result_blob_id"] is not None:
                blob = connection.execute(
                    """
                    SELECT id, logical_digest, logical_size, media_type, codec
                    FROM pulsara_v3.blobs
                    WHERE id = %s AND workspace_id = %s
                    """,
                    (job["result_blob_id"], job["workspace_id"]),
                ).fetchone()
                if blob is None:
                    raise ConversationKernelConflict("job result blob is absent")
                content: CanonicalContent = BlobContent(
                    blob_id=str(blob["id"]),
                    digest=str(blob["logical_digest"]),
                    size=int(blob["logical_size"]),
                    media_type=str(blob["media_type"]),
                    codec=str(blob["codec"]),
                )
            else:
                encoded = json.dumps(
                    dict(job["result_payload"] or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                content = InlineContent.from_bytes(
                    encoded, media_type="application/json", codec="utf-8"
                )
            digest = canonical_digest(
                "pulsara:accept-job-result:v1",
                {
                    "turn_id": turn_id,
                    "new_context_binding_revision_id": (
                        new_context_binding_revision_id
                    ),
                    "source_job_id": job_id,
                    "content_digest": content.digest,
                },
            )
            compatible = connection.execute(
                """
                SELECT c.command_kind, c.semantic_digest, c.target_entry_id,
                       e.turn_id, e.entry_sequence, e.source_job_id,
                       a.event_sequence
                FROM pulsara_v3.session_commands AS c
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.target_entry_id
                LEFT JOIN pulsara_v3.agent_events AS a
                  ON a.session_id = e.session_id
                 AND a.subject_entry_id = e.id
                 AND a.event_type = 'UserMessageAccepted'
                WHERE c.session_id = %s AND c.command_id = %s
                """,
                (guard.session_id, command_id),
            ).fetchone()
            if compatible is not None:
                if (
                    compatible["command_kind"] != "ACCEPT_JOB_RESULT"
                    or compatible["semantic_digest"] != digest
                    or compatible["target_entry_id"] != entry_id
                    or compatible["turn_id"] != turn_id
                    or compatible["source_job_id"] != job_id
                    or compatible["event_sequence"] is None
                ):
                    raise ConversationKernelConflict(
                        "job result acceptance command conflict"
                    )
                return AcceptedEntry(
                    entry_id,
                    turn_id,
                    int(compatible["entry_sequence"]),
                    int(compatible["event_sequence"]),
                )
            if job["accepted_entry_id"] is not None:
                return None
            sequence = self._prepare_external_result_target(
                connection,
                guard,
                turn_id=turn_id,
                entry_id=entry_id,
                new_context_binding_revision_id=new_context_binding_revision_id,
                source_workspace_id=str(job["workspace_id"]),
            )
            if sequence is None:
                return None
            self._insert_entry(
                connection,
                session_id=guard.session_id,
                workspace_id=str(job["workspace_id"]),
                turn_id=turn_id,
                entry_id=entry_id,
                entry_sequence=sequence,
                entry_kind=EntryKind.USER_MESSAGE,
                scope_kind=ConversationScopeKind.ROOT,
                scope_task_id=None,
                content=content,
                source_job_id=job_id,
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.session_commands (
                    session_id, command_id, command_kind,
                    request_schema_version, semantic_digest,
                    target_kind, target_entry_id
                ) VALUES (%s, %s, 'ACCEPT_JOB_RESULT',
                          'accept_job_result.v1', %s, 'ENTRY', %s)
                """,
                (guard.session_id, command_id, digest, entry_id),
            )
            event = self._append_events(
                connection,
                guard,
                workspace_id=str(job["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.USER_MESSAGE_ACCEPTED,
                        SubjectSlot.ENTRY,
                        entry_id,
                        occurred_at=occurred_at,
                        actor_kind="job",
                        actor_id=actor_id,
                        payload={"source_job_id": job_id},
                    ),
                ),
            )[0]
            return AcceptedEntry(entry_id, turn_id, sequence, event.event_sequence)

    def _prepare_external_result_target(
        self,
        connection: Connection,
        guard: HostWriterGuard,
        *,
        turn_id: str,
        entry_id: str,
        new_context_binding_revision_id: str | None,
        source_workspace_id: str,
    ) -> int | None:
        """Prepare the only two legal ROOT acceptance targets.

        The writer transaction already owns the session allocator row.  A
        missing revision selects an existing RUNNING ROOT; a present revision
        creates revision zero for an exact new ROOT.  Terminal parents are
        never silently reused or redirected.
        """

        workspace_id = self._workspace_id(connection, guard.session_id)
        if workspace_id != source_workspace_id:
            raise ConversationKernelConflict("external result workspace drifted")
        if new_context_binding_revision_id is None:
            turn = connection.execute(
                """
                SELECT workspace_id, conversation_scope_kind, status
                FROM pulsara_v3.turns
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, turn_id),
            ).fetchone()
            if (
                turn is None
                or turn["conversation_scope_kind"] != "ROOT"
                or turn["status"] != "RUNNING"
            ):
                return None
            if str(turn["workspace_id"]) != source_workspace_id:
                raise ConversationKernelConflict("external result target drifted")
            return self._allocate_entry_sequence(connection, guard.session_id)
        if not new_context_binding_revision_id:
            raise ValueError("new ROOT context binding revision is empty")
        existing = connection.execute(
            """
            SELECT id FROM pulsara_v3.turns
            WHERE session_id = %s
              AND (id = %s OR (conversation_scope_kind = 'ROOT' AND status = 'RUNNING'))
            LIMIT 1
            """,
            (guard.session_id, turn_id),
        ).fetchone()
        if existing is not None:
            return None
        sequence = self._allocate_entry_sequence(connection, guard.session_id)
        connection.execute(
            """
            INSERT INTO pulsara_v3.turns (
                id, session_id, workspace_id, conversation_scope_kind,
                status, initial_entry_id, current_context_binding_revision_id
            ) VALUES (%s, %s, %s, 'ROOT', 'RUNNING', %s, %s)
            """,
            (
                turn_id,
                guard.session_id,
                workspace_id,
                entry_id,
                new_context_binding_revision_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO pulsara_v3.turn_context_binding_revisions (
                id, session_id, turn_id, revision_ordinal,
                base_kind, source_through_sequence
            ) VALUES (%s, %s, %s, 0, 'FULL_HISTORY', %s)
            """,
            (
                new_context_binding_revision_id,
                guard.session_id,
                turn_id,
                sequence - 1,
            ),
        )
        return sequence

    def cancel_prompt(
        self,
        guard: HostWriterGuard,
        *,
        queue_item_id: str,
        occurred_at: datetime,
        actor_id: str,
        deadline_monotonic: float,
    ) -> str:
        """CAS one pending queue item to CANCELLED and return the winner."""

        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT workspace_id, status
                FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, queue_item_id),
            ).fetchone()
            if row is None:
                raise KeyError(queue_item_id)
            if row["status"] != "PENDING":
                return str(row["status"])
            connection.execute(
                """
                UPDATE pulsara_v3.prompt_queue_items
                SET status = 'CANCELLED', terminal_reason = 'USER_CANCELLED',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND id = %s AND status = 'PENDING'
                """,
                (guard.session_id, queue_item_id),
            )
            self._append_events(
                connection,
                guard,
                workspace_id=str(row["workspace_id"]),
                drafts=(
                    self._event(
                        CommittedEventType.PROMPT_CANCELLED,
                        SubjectSlot.QUEUE_ITEM,
                        queue_item_id,
                        occurred_at=occurred_at,
                        actor_kind="human",
                        actor_id=actor_id,
                        payload={"reason": "USER_CANCELLED"},
                    ),
                ),
            )
            return "CANCELLED"

    def has_pending_prompt(
        self,
        *,
        session_id: str,
        delivery_mode: PromptDeliveryMode | None = None,
        deadline_monotonic: float,
    ) -> bool:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM pulsara_v3.prompt_queue_items
                    WHERE session_id = %s AND status = 'PENDING'
                      AND (%s::text IS NULL OR delivery_mode = %s::text)
                    LIMIT 1
                    """,
                    (
                        session_id,
                        None if delivery_mode is None else delivery_mode.value,
                        None if delivery_mode is None else delivery_mode.value,
                    ),
                ).fetchone()
                is not None
            )

    def pending_prompt_head_mode(
        self,
        *,
        session_id: str,
        deadline_monotonic: float,
    ) -> PromptDeliveryMode | None:
        """Return the single global FIFO head mode without claiming it."""

        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT delivery_mode FROM pulsara_v3.prompt_queue_items
                WHERE session_id = %s AND status = 'PENDING'
                ORDER BY queue_sequence, id LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return (
                None if row is None else PromptDeliveryMode(str(row["delivery_mode"]))
            )

    def request_job_cancel(
        self,
        guard: HostWriterGuard,
        *,
        job_id: str,
        actor_id: str,
        reason: str,
        deadline_monotonic: float,
    ) -> str:
        """Install one session-writer-owned cancellation request.

        This never fabricates a terminal result. The exact claim owner observes
        the set-once request and owns the attempt/job terminal transition.
        """

        if not job_id or not actor_id or not reason:
            raise ValueError("job cancellation request is incomplete")
        if len(reason.encode("utf-8")) > 4096:
            raise ValueError("job cancellation reason exceeds its bound")
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT status, cancel_requested_at, cancel_requested_by,
                       cancel_request_reason
                FROM pulsara_v3.durable_jobs
                WHERE origin_session_id = %s AND id = %s
                FOR UPDATE
                """,
                (guard.session_id, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            status = str(row["status"])
            if status in {"SUCCEEDED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN"}:
                return status
            if row["cancel_requested_at"] is not None:
                if (
                    str(row["cancel_requested_by"]) != actor_id
                    or str(row["cancel_request_reason"]) != reason
                ):
                    raise ConversationKernelConflict(
                        "job cancellation request conflicts with installed authority"
                    )
                return "CANCEL_REQUESTED"
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET cancel_requested_at = clock_timestamp(),
                    cancel_requested_by = %s,
                    cancel_request_reason = %s
                WHERE origin_session_id = %s AND id = %s
                  AND status IN ('PENDING', 'ACTIVE')
                  AND cancel_requested_at IS NULL
                """,
                (actor_id, reason, guard.session_id, job_id),
            )
            return "CANCEL_REQUESTED"

    def claim_due_job(
        self,
        *,
        handler_type: str,
        claim_owner_id: str,
        lease_seconds: float,
        expected_job_id: str | None = None,
        deadline_monotonic: float,
    ) -> AcceptedJobAttempt | None:
        if lease_seconds <= 0:
            raise ValueError("claim lease must be finite and positive")
        if expected_job_id is None:
            expected_job_id = self.prepare_job_claim_candidate(
                handler_type=handler_type,
                deadline_monotonic=deadline_monotonic,
            )
            if expected_job_id is None:
                return None
        with self._event_transaction(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            candidate = connection.execute(
                """
                SELECT origin_session_id
                FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = %s
                """,
                (expected_job_id, handler_type),
            ).fetchone()
            if candidate is None:
                return None
            origin_session_id = candidate["origin_session_id"]
            if origin_session_id is not None:
                session = connection.execute(
                    """
                    SELECT id FROM pulsara_v3.sessions
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (origin_session_id,),
                ).fetchone()
                if session is None:
                    raise ConversationKernelConflict("job origin session disappeared")
            expired = connection.execute(
                """
                SELECT j.*, a.id AS attempt_id, a.attempt_ordinal,
                       a.claim_generation, a.claim_owner_id, a.remote_identity,
                       a.accepted_at AS attempt_accepted_at
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.handler_type = %s AND j.status = 'ACTIVE'
                  AND j.id = %s
                  AND a.terminal_status IS NULL
                  AND a.lease_expires_at <= clock_timestamp()
                ORDER BY a.lease_expires_at, j.id
                FOR UPDATE OF j, a SKIP LOCKED
                LIMIT 1
                """,
                (handler_type, expected_job_id),
            ).fetchone()
            if expired is not None:
                safety = JobSafetyClass(str(expired["safety_class"]))
                retry_safe_with_budget = safety is JobSafetyClass.RETRY_SAFE and int(
                    expired["attempt_ordinal"]
                ) < int(expired["maximum_attempts"])
                if retry_safe_with_budget:
                    next_eligible_at = _deterministic_retry_due(
                        accepted_at=expired["attempt_accepted_at"],
                        job_id=str(expired["id"]),
                        attempt_ordinal=int(expired["attempt_ordinal"]),
                    )
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_job_attempts
                        SET terminal_status = 'FAILED', error_code = 'ATTEMPT_TIMEOUT',
                            terminal_at = clock_timestamp()
                        WHERE id = %s AND terminal_status IS NULL
                        """,
                        (expired["attempt_id"],),
                    )
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_jobs
                        SET status = 'PENDING', next_eligible_at = %s
                        WHERE id = %s AND status = 'ACTIVE'
                        """,
                        (next_eligible_at, expired["id"]),
                    )
                else:
                    terminal_at = _utcnow()
                    aggregate_status = (
                        "FAILED"
                        if safety is JobSafetyClass.RETRY_SAFE
                        else "OUTCOME_UNKNOWN"
                    )
                    terminal_reason = (
                        "RETRY_EXHAUSTED"
                        if safety is JobSafetyClass.RETRY_SAFE
                        else "LEASE_LOST_OUTCOME_UNKNOWN"
                    )
                    reaper_generation = int(expired["claim_generation"]) + 1
                    reaper_lease = _utcnow() + timedelta(seconds=lease_seconds)
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_job_attempts
                        SET claim_generation = %s, claim_owner_id = %s,
                            lease_expires_at = %s
                        WHERE id = %s AND terminal_status IS NULL
                        """,
                        (
                            reaper_generation,
                            claim_owner_id,
                            reaper_lease,
                            expired["attempt_id"],
                        ),
                    )
                    if expired["origin_session_id"] is not None:
                        reaper_guard = JobAttemptClaimGuard(
                            job_id=str(expired["id"]),
                            attempt_id=str(expired["attempt_id"]),
                            claim_generation=reaper_generation,
                            claim_owner_id=claim_owner_id,
                            origin_session_id=expired["origin_session_id"],
                        )
                        self._append_events(
                            connection,
                            reaper_guard,
                            workspace_id=str(expired["workspace_id"]),
                            drafts=(
                                self._event(
                                    CommittedEventType.JOB_TERMINAL_ACCEPTED,
                                    SubjectSlot.JOB,
                                    str(expired["id"]),
                                    occurred_at=terminal_at,
                                    actor_kind="job_worker",
                                    actor_id=claim_owner_id,
                                    payload={
                                        "status": aggregate_status,
                                        "terminal_reason": terminal_reason,
                                    },
                                ),
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE pulsara_v3.durable_job_attempts
                        SET terminal_status = %s, error_code = %s,
                            terminal_at = %s
                        WHERE id = %s AND terminal_status IS NULL
                        """,
                        (
                            aggregate_status,
                            terminal_reason,
                            terminal_at,
                            expired["attempt_id"],
                        ),
                    )
                    connection.execute(
                        """UPDATE pulsara_v3.durable_jobs
                           SET status = %s, terminal_reason = %s, terminal_at = %s
                           WHERE id = %s AND status = 'ACTIVE'""",
                        (
                            aggregate_status,
                            terminal_reason,
                            terminal_at,
                            expired["id"],
                        ),
                    )
            job = connection.execute(
                """
                SELECT * FROM pulsara_v3.durable_jobs
                WHERE handler_type = %s AND status = 'PENDING'
                  AND id = %s
                  AND next_eligible_at <= clock_timestamp()
                  AND (SELECT count(*) FROM pulsara_v3.durable_job_attempts a
                       WHERE a.job_id = pulsara_v3.durable_jobs.id) < maximum_attempts
                ORDER BY next_eligible_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (handler_type, expected_job_id),
            ).fetchone()
            if job is None:
                return None
            previous = connection.execute(
                """
                SELECT id, attempt_ordinal, claim_generation
                FROM pulsara_v3.durable_job_attempts
                WHERE job_id = %s
                ORDER BY attempt_ordinal DESC
                LIMIT 1
                """,
                (job["id"],),
            ).fetchone()
            ordinal = 1 if previous is None else int(previous["attempt_ordinal"]) + 1
            generation = (
                1 if previous is None else int(previous["claim_generation"]) + 1
            )
            attempt_id = _id("job-attempt")
            lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
            deadline_at = _utcnow() + timedelta(
                milliseconds=int(job["attempt_timeout_ms"])
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_job_attempts (
                    id, job_id, origin_session_id, attempt_ordinal,
                    claim_generation, claim_owner_id, lease_expires_at,
                    deadline_at, retry_of_attempt_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    job["id"],
                    job["origin_session_id"],
                    ordinal,
                    generation,
                    claim_owner_id,
                    lease_expires_at,
                    deadline_at,
                    None if previous is None else previous["id"],
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'ACTIVE'
                WHERE id = %s AND status = 'PENDING'
                """,
                (job["id"],),
            )
            guard = JobAttemptClaimGuard(
                job_id=str(job["id"]),
                attempt_id=attempt_id,
                claim_generation=generation,
                claim_owner_id=claim_owner_id,
                origin_session_id=job["origin_session_id"],
            )
            if job["origin_session_id"] is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(job["workspace_id"]),
                    drafts=(
                        self._event(
                            CommittedEventType.JOB_ATTEMPT_ACCEPTED,
                            SubjectSlot.JOB_ATTEMPT,
                            attempt_id,
                            occurred_at=_utcnow(),
                            actor_kind="job_worker",
                            actor_id=claim_owner_id,
                            payload={"attempt_ordinal": ordinal},
                        ),
                    ),
                )
            return AcceptedJobAttempt(
                guard=guard,
                attempt_ordinal=ordinal,
                deadline_at=deadline_at,
                handler_type=str(job["handler_type"]),
                safety_class=JobSafetyClass(str(job["safety_class"])),
                intent_payload=dict(job["intent_payload"]),
                provider_input_token_limit=job[
                    "provider_input_token_limit_per_attempt"
                ],
                provider_output_token_limit=job[
                    "provider_output_token_limit_per_attempt"
                ],
                reclaimed_after_expiry=False,
                cancel_requested=job["cancel_requested_at"] is not None,
            )

    def prepare_job_claim_candidate(
        self,
        *,
        handler_type: str,
        deadline_monotonic: float,
    ) -> str | None:
        """Select a stable claim candidate before the mutation transaction."""

        job_handler_contract(handler_type)
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT j.id
                FROM pulsara_v3.durable_jobs AS j
                LEFT JOIN pulsara_v3.durable_job_attempts AS a
                  ON a.job_id = j.id AND a.terminal_status IS NULL
                WHERE j.handler_type = %s AND (
                    (j.status = 'ACTIVE' AND a.lease_expires_at <= clock_timestamp())
                    OR
                    (j.status = 'PENDING' AND j.next_eligible_at <= clock_timestamp()
                     AND (SELECT count(*) FROM pulsara_v3.durable_job_attempts x
                          WHERE x.job_id = j.id) < j.maximum_attempts)
                )
                ORDER BY
                    CASE WHEN j.status = 'ACTIVE' THEN 0 ELSE 1 END,
                    COALESCE(a.lease_expires_at, j.next_eligible_at), j.id
                LIMIT 1
                """,
                (handler_type,),
            ).fetchone()
            return None if row is None else str(row["id"])

    def confirm_active_job_claim(
        self,
        *,
        job_id: str,
        handler_type: str,
        claim_owner_id: str,
        deadline_monotonic: float,
    ) -> AcceptedJobAttempt | None:
        """Exact-confirm a first/retry claim whose commit ACK was lost."""

        job_handler_contract(handler_type)
        with self._provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            row = connection.execute(
                """
                SELECT j.*, a.id AS attempt_id, a.attempt_ordinal,
                       a.claim_generation, a.deadline_at
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.id = %s AND j.handler_type = %s
                  AND j.status = 'ACTIVE'
                  AND a.claim_owner_id = %s
                  AND a.terminal_status IS NULL
                  AND a.lease_expires_at > clock_timestamp()
                ORDER BY a.attempt_ordinal DESC
                LIMIT 1
                """,
                (job_id, handler_type, claim_owner_id),
            ).fetchone()
            if row is None:
                return None
            guard = JobAttemptClaimGuard(
                job_id=job_id,
                attempt_id=str(row["attempt_id"]),
                claim_generation=int(row["claim_generation"]),
                claim_owner_id=claim_owner_id,
                origin_session_id=row["origin_session_id"],
            )
            return AcceptedJobAttempt(
                guard=guard,
                attempt_ordinal=int(row["attempt_ordinal"]),
                deadline_at=row["deadline_at"],
                handler_type=handler_type,
                safety_class=JobSafetyClass(str(row["safety_class"])),
                intent_payload=dict(row["intent_payload"]),
                provider_input_token_limit=row[
                    "provider_input_token_limit_per_attempt"
                ],
                provider_output_token_limit=row[
                    "provider_output_token_limit_per_attempt"
                ],
                reclaimed_after_expiry=False,
                cancel_requested=row["cancel_requested_at"] is not None,
            )

    def mark_job_provider_call_started(
        self,
        guard: JobAttemptClaimGuard,
        *,
        input_tokens: int,
        requested_output_tokens: int,
        deadline_monotonic: float,
    ) -> None:
        terminalized = False
        with self._job_transaction(
            guard,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            limits = connection.execute(
                """
                SELECT j.provider_input_token_limit_per_attempt AS input_limit,
                       j.provider_output_token_limit_per_attempt AS output_limit,
                       j.workspace_id, j.origin_session_id,
                       a.provider_call_started_at
                FROM pulsara_v3.durable_job_attempts AS a
                JOIN pulsara_v3.durable_jobs AS j ON j.id = a.job_id
                WHERE a.id = %s
                """,
                (guard.attempt_id,),
            ).fetchone()
            if limits is None or limits["provider_call_started_at"] is not None:
                raise StaleJobClaim("provider call admission already consumed")
            if (
                limits["input_limit"] is None
                or limits["output_limit"] is None
                or input_tokens < 0
                or requested_output_tokens <= 0
                or input_tokens > int(limits["input_limit"])
                or requested_output_tokens > int(limits["output_limit"])
            ):
                terminal_at = _utcnow()
                if limits["origin_session_id"] is not None:
                    self._append_events(
                        connection,
                        guard,
                        workspace_id=str(limits["workspace_id"]),
                        drafts=(
                            self._event(
                                CommittedEventType.JOB_TERMINAL_ACCEPTED,
                                SubjectSlot.JOB,
                                guard.job_id,
                                occurred_at=terminal_at,
                                actor_kind="job_worker",
                                actor_id=guard.claim_owner_id,
                                payload={
                                    "status": "FAILED",
                                    "terminal_reason": "PROVIDER_REQUEST_LIMIT_EXCEEDED",
                                },
                            ),
                        ),
                    )
                connection.execute(
                    """UPDATE pulsara_v3.durable_job_attempts
                       SET terminal_status = 'FAILED',
                           error_code = 'PROVIDER_REQUEST_LIMIT_EXCEEDED',
                           terminal_at = %s
                       WHERE id = %s AND terminal_status IS NULL""",
                    (terminal_at, guard.attempt_id),
                )
                connection.execute(
                    """UPDATE pulsara_v3.durable_jobs
                       SET status = 'FAILED',
                           terminal_reason = 'PROVIDER_REQUEST_LIMIT_EXCEEDED',
                           terminal_at = %s
                       WHERE id = %s AND status = 'ACTIVE'""",
                    (terminal_at, guard.job_id),
                )
                terminalized = True
            else:
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_job_attempts
                    SET provider_call_started_at = clock_timestamp(),
                        provider_input_tokens = %s,
                        provider_requested_output_tokens = %s
                    WHERE id = %s AND provider_call_started_at IS NULL
                    """,
                    (input_tokens, requested_output_tokens, guard.attempt_id),
                )
        if terminalized:
            raise JobAttemptTerminalized("provider request exceeded its frozen bound")

    def settle_job_attempt(
        self,
        guard: JobAttemptClaimGuard,
        *,
        terminal_status: str,
        result_payload: Mapping[str, object] | None,
        error_code: str | None,
        result_blob_id: str | None = None,
        retryable: bool = False,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedJobSettlement:
        if terminal_status not in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "OUTCOME_UNKNOWN",
        }:
            raise ValueError("job terminal status is not closed")
        with self._job_transaction(
            guard,
            deadline_monotonic=deadline_monotonic,
            allow_cancel_requested=True,
        ) as connection:
            row = connection.execute(
                """
                SELECT j.*, a.attempt_ordinal,
                       a.accepted_at AS attempt_accepted_at
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.id = %s AND a.id = %s
                FOR UPDATE OF j, a
                """,
                (guard.job_id, guard.attempt_id),
            ).fetchone()
            if row is None:
                raise StaleJobClaim("job attempt is absent")
            ordinal = int(row["attempt_ordinal"])
            safety = JobSafetyClass(str(row["safety_class"]))
            may_retry = (
                terminal_status == "FAILED"
                and retryable
                and safety
                in {JobSafetyClass.RETRY_SAFE, JobSafetyClass.REMOTE_QUERYABLE}
                and ordinal < int(row["maximum_attempts"])
            )
            terminal_at = _utcnow()
            if may_retry:
                next_eligible_at = _deterministic_retry_due(
                    accepted_at=row["attempt_accepted_at"],
                    job_id=guard.job_id,
                    attempt_ordinal=ordinal,
                )
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_job_attempts
                    SET terminal_status = 'FAILED', result_payload = %s,
                        error_code = %s, terminal_at = %s
                    WHERE id = %s AND terminal_status IS NULL
                    """,
                    (
                        None if result_payload is None else Jsonb(dict(result_payload)),
                        error_code,
                        terminal_at,
                        guard.attempt_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE pulsara_v3.durable_jobs
                    SET status = 'PENDING', next_eligible_at = %s
                    WHERE id = %s AND status = 'ACTIVE'
                    """,
                    (next_eligible_at, guard.job_id),
                )
                return AcceptedJobSettlement(
                    guard.job_id,
                    guard.attempt_id,
                    "FAILED",
                    "PENDING",
                    True,
                    next_eligible_at,
                )
            if (
                terminal_status == "FAILED"
                and retryable
                and ordinal >= int(row["maximum_attempts"])
            ):
                error_code = "RETRY_EXHAUSTED"
            if row["origin_session_id"] is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(row["workspace_id"]),
                    drafts=(
                        self._event(
                            CommittedEventType.JOB_TERMINAL_ACCEPTED,
                            SubjectSlot.JOB,
                            guard.job_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={
                                "status": terminal_status,
                                "terminal_reason": error_code,
                            },
                        ),
                    ),
                )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = %s, result_payload = %s,
                    error_code = %s, terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    terminal_status,
                    None if result_payload is None else Jsonb(dict(result_payload)),
                    error_code,
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = %s, result_blob_id = %s, terminal_reason = %s,
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (
                    terminal_status,
                    result_blob_id,
                    error_code,
                    terminal_at,
                    guard.job_id,
                ),
            )
            return AcceptedJobSettlement(
                guard.job_id,
                guard.attempt_id,
                terminal_status,
                terminal_status,
                False,
                None,
            )

    def read_compaction_job_source(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        """Read one immutable ROOT transcript cut owned by a compaction job."""

        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT handler_type, origin_session_id, intent_payload
                FROM pulsara_v3.durable_jobs WHERE id = %s
                """,
                (guard.job_id,),
            ).fetchone()
            if (
                job is None
                or job["handler_type"] != "BACKGROUND_COMPACTION"
                or job["origin_session_id"] is None
            ):
                raise ConversationKernelConflict("compaction job source is invalid")
            intent = dict(job["intent_payload"])
            session_id = str(job["origin_session_id"])
            through = _required_nonnegative_int(
                intent.get("source_through_sequence"), "source_through_sequence"
            )
            if intent.get("session_id") != session_id:
                raise ConversationKernelConflict("compaction session identity drifted")
            rows = _load_root_transcript_cut(
                connection,
                session_id=session_id,
                through_sequence=through,
            )
            digest = canonical_digest("pulsara:background-compaction-source:v1", rows)
            if intent.get("source_digest") != digest:
                raise ConversationKernelConflict("compaction source digest drifted")
            return {
                "session_id": session_id,
                "source_through_sequence": through,
                "source_digest": digest,
                "entries": rows,
            }

    def accept_compaction_job_result(
        self,
        guard: JobAttemptClaimGuard,
        *,
        summary: str,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> str:
        """Accept a compaction result and its extraction job atomically."""

        if not summary.strip() or len(summary.encode("utf-8")) > 256 * 1024:
            raise ValueError("compaction summary is outside its bound")
        extraction = job_handler_contract(POST_COMPACTION_MEMORY_EXTRACTION)
        summary_digest = canonical_digest(
            "pulsara:background-compaction-result:v1", {"summary": summary}
        )
        extraction_job_id = (
            "job:"
            + sha256(
                f"post-compaction:{guard.job_id}:{summary_digest}".encode()
            ).hexdigest()
        )
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT * FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'BACKGROUND_COMPACTION'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("compaction job is absent")
            extraction_intent = {
                "source_job_id": guard.job_id,
                "source_result_digest": summary_digest,
            }
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (
                    %s, %s, %s, 'POST_COMPACTION_MEMORY_EXTRACTION',
                    'post_compaction_memory_extraction.v1', %s, %s, %s,
                    'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                    %s, %s, %s, %s, clock_timestamp()
                )
                """,
                (
                    extraction_job_id,
                    job["workspace_id"],
                    job["origin_session_id"],
                    canonical_digest(
                        "pulsara:job-intent:post_compaction_memory_extraction.v1",
                        extraction_intent,
                    ),
                    Jsonb(extraction_intent),
                    f"post-compaction:{guard.job_id}",
                    extraction.maximum_attempts,
                    extraction.attempt_timeout_ms,
                    extraction.input_token_limit,
                    extraction.output_token_limit,
                ),
            )
            drafts: list[CommittedEventDraft] = []
            if guard.origin_session_id is not None:
                drafts.extend(
                    (
                        self._event(
                            CommittedEventType.JOB_QUEUED,
                            SubjectSlot.JOB,
                            extraction_job_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={
                                "handler_type": "POST_COMPACTION_MEMORY_EXTRACTION"
                            },
                        ),
                        self._event(
                            CommittedEventType.JOB_TERMINAL_ACCEPTED,
                            SubjectSlot.JOB,
                            guard.job_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={"status": "SUCCEEDED", "terminal_reason": None},
                        ),
                    )
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=str(job["workspace_id"]),
                    drafts=tuple(drafts),
                )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb(
                        {
                            "summary": summary,
                            "summary_digest": summary_digest,
                            "extraction_job_id": extraction_job_id,
                        }
                    ),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'COMPACTION_ACCEPTED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return extraction_job_id

    def read_memory_extraction_job_source(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT handler_type, intent_payload FROM pulsara_v3.durable_jobs
                WHERE id = %s
                """,
                (guard.job_id,),
            ).fetchone()
            if (
                job is None
                or job["handler_type"] != "POST_COMPACTION_MEMORY_EXTRACTION"
            ):
                raise ConversationKernelConflict("memory extraction job is invalid")
            intent = dict(job["intent_payload"])
            source_job_id = _required_string(
                intent.get("source_job_id"), "source_job_id"
            )
            source = connection.execute(
                """
                SELECT j.status, a.result_payload
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.durable_job_attempts AS a ON a.job_id = j.id
                WHERE j.id = %s AND j.handler_type = 'BACKGROUND_COMPACTION'
                  AND j.status = 'SUCCEEDED' AND a.terminal_status = 'SUCCEEDED'
                ORDER BY a.attempt_ordinal DESC LIMIT 1
                """,
                (source_job_id,),
            ).fetchone()
            if source is None or source["result_payload"] is None:
                raise ConversationKernelConflict("compaction result is unavailable")
            payload = dict(source["result_payload"])
            if intent.get("source_result_digest") != payload.get("summary_digest"):
                raise ConversationKernelConflict("compaction result identity drifted")
            summary = payload.get("summary")
            if not isinstance(summary, str):
                raise ConversationKernelConflict("compaction result is malformed")
            return {
                "source_job_id": source_job_id,
                "source_result_digest": payload["summary_digest"],
                "summary": summary,
            }

    def read_memory_candidate_for_governance(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            row = connection.execute(
                """
                SELECT c.*, j.intent_payload, j.handler_type,
                       j.workspace_id AS job_workspace_id
                FROM pulsara_v3.durable_jobs AS j
                JOIN pulsara_v3.memory_candidates AS c
                  ON c.id = j.intent_payload ->> 'candidate_id'
                WHERE j.id = %s
                """,
                (guard.job_id,),
            ).fetchone()
            if row is None or row["handler_type"] != "MEMORY_GOVERNANCE":
                raise ConversationKernelConflict("governance candidate is absent")
            intent = dict(row["intent_payload"])
            if (
                row["status"] != "PENDING"
                or row["workspace_id"] != row["job_workspace_id"]
                or intent.get("candidate_semantic_digest") != row["semantic_digest"]
            ):
                raise ConversationKernelConflict(
                    "governance candidate identity drifted"
                )
            return {
                "id": str(row["id"]),
                "workspace_id": str(row["workspace_id"]),
                "proposal_kind": str(row["proposal_kind"]),
                "proposal_payload": dict(row["proposal_payload"]),
                "semantic_digest": str(row["semantic_digest"]),
            }

    def accept_extracted_memory_bundle(
        self,
        guard: JobAttemptClaimGuard,
        *,
        candidates: Sequence[tuple[str, str, Mapping[str, object], str]],
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> tuple[AcceptedMemoryCandidate, ...]:
        if len(candidates) > 32:
            raise ValueError("memory extraction bundle exceeds 32 candidates")
        governance = job_handler_contract(MEMORY_GOVERNANCE)
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            source = connection.execute(
                """
                SELECT * FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'POST_COMPACTION_MEMORY_EXTRACTION'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if source is None:
                raise ConversationKernelConflict("memory extraction source is absent")
            workspace_id = str(source["workspace_id"])
            origin_session_id = source["origin_session_id"]
            accepted: list[AcceptedMemoryCandidate] = []
            drafts: list[CommittedEventDraft] = []
            for (
                candidate_id,
                proposal_kind,
                proposal_payload,
                governance_job_id,
            ) in candidates:
                if proposal_kind not in {
                    "FACT",
                    "PREFERENCE",
                    "RELATION",
                    "CORRECTION",
                    "LIFECYCLE",
                }:
                    raise ValueError("memory proposal kind is not closed")
                semantic_digest = canonical_digest(
                    "pulsara:memory-candidate:v1",
                    {
                        "workspace_id": workspace_id,
                        "proposal_kind": proposal_kind,
                        "proposal_payload": dict(proposal_payload),
                        "source_entry_id": None,
                    },
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_candidates (
                        id, workspace_id, origin_session_id, source_entry_id,
                        proposal_kind, semantic_digest, proposal_payload, status
                    ) VALUES (%s, %s, %s, NULL, %s, %s, %s, 'PENDING')
                    """,
                    (
                        candidate_id,
                        workspace_id,
                        origin_session_id,
                        proposal_kind,
                        semantic_digest,
                        Jsonb(dict(proposal_payload)),
                    ),
                )
                intent = {
                    "candidate_id": candidate_id,
                    "candidate_semantic_digest": semantic_digest,
                }
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.durable_jobs (
                        id, workspace_id, origin_session_id, handler_type,
                        intent_schema_version, intent_digest, intent_payload,
                        automatic_intent_key, safety_class, status,
                        retry_policy_id, retry_policy_version, maximum_attempts,
                        attempt_timeout_ms, provider_input_token_limit_per_attempt,
                        provider_output_token_limit_per_attempt, next_eligible_at
                    ) VALUES (
                        %s, %s, %s, 'MEMORY_GOVERNANCE',
                        'memory_governance.v1', %s, %s, %s,
                        'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                        %s, %s, %s, %s, clock_timestamp()
                    )
                    """,
                    (
                        governance_job_id,
                        workspace_id,
                        origin_session_id,
                        canonical_digest(
                            "pulsara:job-intent:memory_governance.v1", intent
                        ),
                        Jsonb(intent),
                        f"memory-governance:{candidate_id}",
                        governance.maximum_attempts,
                        governance.attempt_timeout_ms,
                        governance.input_token_limit,
                        governance.output_token_limit,
                    ),
                )
                accepted.append(
                    AcceptedMemoryCandidate(candidate_id, governance_job_id)
                )
                if origin_session_id is not None:
                    drafts.append(
                        self._event(
                            CommittedEventType.JOB_QUEUED,
                            SubjectSlot.JOB,
                            governance_job_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={"handler_type": "MEMORY_GOVERNANCE"},
                        )
                    )
            if origin_session_id is not None:
                drafts.append(
                    self._event(
                        CommittedEventType.JOB_TERMINAL_ACCEPTED,
                        SubjectSlot.JOB,
                        guard.job_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={"status": "SUCCEEDED", "terminal_reason": None},
                    )
                )
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=tuple(drafts),
                )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb({"candidate_ids": [item.candidate_id for item in accepted]}),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'CANDIDATES_ACCEPTED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return tuple(accepted)

    def accept_memory_candidate_and_governance_job(
        self,
        guard: HostWriterGuard | JobAttemptClaimGuard,
        *,
        candidate_id: str,
        source_entry_id: str | None,
        proposal_kind: str,
        proposal_payload: Mapping[str, object],
        governance_job_id: str,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryCandidate:
        if proposal_kind not in {
            "FACT",
            "PREFERENCE",
            "RELATION",
            "CORRECTION",
            "LIFECYCLE",
        }:
            raise ValueError("memory proposal kind is not closed")
        governance = job_handler_contract(MEMORY_GOVERNANCE)
        scope = (
            self._writer_transaction(guard, deadline_monotonic=deadline_monotonic)
            if isinstance(guard, HostWriterGuard)
            else self._job_transaction(guard, deadline_monotonic=deadline_monotonic)
        )
        with scope as connection:
            if isinstance(guard, HostWriterGuard):
                origin_session_id = guard.session_id
                workspace_id = self._workspace_id(connection, guard.session_id)
            else:
                origin_session_id = guard.origin_session_id
                source_job = connection.execute(
                    "SELECT workspace_id FROM pulsara_v3.durable_jobs WHERE id = %s",
                    (guard.job_id,),
                ).fetchone()
                if source_job is None:
                    raise StaleJobClaim("candidate source job is absent")
                workspace_id = str(source_job["workspace_id"])
            semantic_digest = canonical_digest(
                "pulsara:memory-candidate:v1",
                {
                    "workspace_id": workspace_id,
                    "proposal_kind": proposal_kind,
                    "proposal_payload": dict(proposal_payload),
                    "source_entry_id": source_entry_id,
                },
            )
            connection.execute(
                """
                INSERT INTO pulsara_v3.memory_candidates (
                    id, workspace_id, origin_session_id, source_entry_id,
                    proposal_kind, semantic_digest, proposal_payload, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    candidate_id,
                    workspace_id,
                    origin_session_id,
                    source_entry_id,
                    proposal_kind,
                    semantic_digest,
                    Jsonb(dict(proposal_payload)),
                ),
            )
            intent_payload = {
                "candidate_id": candidate_id,
                "candidate_semantic_digest": semantic_digest,
            }
            connection.execute(
                """
                INSERT INTO pulsara_v3.durable_jobs (
                    id, workspace_id, origin_session_id, handler_type,
                    intent_schema_version, intent_digest, intent_payload,
                    automatic_intent_key, safety_class, status,
                    retry_policy_id, retry_policy_version, maximum_attempts,
                    attempt_timeout_ms, provider_input_token_limit_per_attempt,
                    provider_output_token_limit_per_attempt, next_eligible_at
                ) VALUES (
                    %s, %s, %s, 'MEMORY_GOVERNANCE',
                    'memory_governance.v1', %s, %s, %s,
                    'RETRY_SAFE', 'PENDING', 'bounded-exponential', 1,
                    %s, %s, %s, %s, clock_timestamp()
                )
                """,
                (
                    governance_job_id,
                    workspace_id,
                    origin_session_id,
                    canonical_digest(
                        "pulsara:job-intent:memory_governance.v1", intent_payload
                    ),
                    Jsonb(intent_payload),
                    f"memory-governance:{candidate_id}",
                    governance.maximum_attempts,
                    governance.attempt_timeout_ms,
                    governance.input_token_limit,
                    governance.output_token_limit,
                ),
            )
            if origin_session_id is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=(
                        self._event(
                            CommittedEventType.JOB_QUEUED,
                            SubjectSlot.JOB,
                            governance_job_id,
                            occurred_at=occurred_at,
                            actor_kind=(
                                "runtime"
                                if isinstance(guard, HostWriterGuard)
                                else "job_worker"
                            ),
                            actor_id=(
                                guard.writer_owner_id
                                if isinstance(guard, HostWriterGuard)
                                else guard.claim_owner_id
                            ),
                            payload={"handler_type": "MEMORY_GOVERNANCE"},
                        ),
                    ),
                )
        return AcceptedMemoryCandidate(candidate_id, governance_job_id)

    def accept_memory_governance(
        self,
        guard: JobAttemptClaimGuard,
        *,
        candidate_id: str,
        decision_id: str,
        decision: str,
        lineage_payload: Mapping[str, object],
        fact_id: str | None,
        fact_kind: str | None,
        fact_payload: Mapping[str, object] | None,
        relations: Sequence[tuple[str, str, str]],
        index_handler_contract_id: str,
        index_handler_contract_version: int,
        occurred_at: datetime,
        deadline_monotonic: float,
    ) -> AcceptedMemoryGovernance:
        allowed = {"SKIP", "SUBMIT", "CORRECT", "MERGE", "SUPERSEDE", "CONTRADICT"}
        if decision not in allowed:
            raise ValueError("memory governance decision is not closed")
        if (decision == "SKIP") != (fact_id is None):
            raise ValueError("memory governance fact branch is invalid")
        if fact_id is not None and (not fact_kind or fact_payload is None):
            raise ValueError("accepted memory fact payload is incomplete")
        superseded_fact_ids_value = lineage_payload.get("superseded_fact_ids", ())
        if not isinstance(superseded_fact_ids_value, (list, tuple)):
            raise ValueError("memory lineage superseded-fact carrier is invalid")
        superseded_fact_ids = tuple(str(value) for value in superseded_fact_ids_value)
        if len(superseded_fact_ids) > 32 or any(
            not value for value in superseded_fact_ids
        ):
            raise ValueError("memory lineage exceeds its hard bound")
        lifecycle_decision = decision in {
            "CORRECT",
            "MERGE",
            "SUPERSEDE",
            "CONTRADICT",
        }
        if lifecycle_decision != bool(superseded_fact_ids):
            raise ValueError("memory lifecycle decision lineage is incomplete")
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            candidate = connection.execute(
                """
                SELECT c.*, j.workspace_id AS job_workspace_id,
                       j.intent_payload, j.handler_type
                FROM pulsara_v3.memory_candidates AS c
                JOIN pulsara_v3.durable_jobs AS j ON j.id = %s
                WHERE c.id = %s
                FOR UPDATE OF c, j
                """,
                (guard.job_id, candidate_id),
            ).fetchone()
            if (
                candidate is None
                or candidate["handler_type"] != "MEMORY_GOVERNANCE"
                or candidate["workspace_id"] != candidate["job_workspace_id"]
                or dict(candidate["intent_payload"]).get("candidate_id") != candidate_id
                or candidate["status"] != "PENDING"
            ):
                raise ConversationKernelConflict(
                    "governance job does not own the pending candidate"
                )
            workspace_id = str(candidate["workspace_id"])
            connection.execute(
                """
                INSERT INTO pulsara_v3.memory_governance_decisions (
                    id, candidate_id, job_id, decision, lineage_payload
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    decision_id,
                    candidate_id,
                    guard.job_id,
                    decision,
                    Jsonb(dict(lineage_payload)),
                ),
            )
            connection.execute(
                "UPDATE pulsara_v3.memory_candidates SET status = 'DECIDED' WHERE id = %s",
                (candidate_id,),
            )
            drafts: list[CommittedEventDraft] = []
            relation_ids: list[str] = []
            for superseded_fact_id in superseded_fact_ids:
                changed = connection.execute(
                    """
                    UPDATE pulsara_v3.memory_facts
                    SET lifecycle = 'SUPERSEDED', updated_at = clock_timestamp()
                    WHERE workspace_id = %s AND id = %s AND lifecycle = 'ACTIVE'
                    RETURNING id
                    """,
                    (workspace_id, superseded_fact_id),
                ).fetchone()
                if changed is None:
                    raise ConversationKernelConflict(
                        "memory lifecycle predecessor is not active"
                    )
                drafts.append(
                    self._event(
                        CommittedEventType.MEMORY_FACT_LIFECYCLE_CHANGED,
                        SubjectSlot.MEMORY_FACT,
                        superseded_fact_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={
                            "lifecycle": "SUPERSEDED",
                            "successor_fact_id": fact_id,
                            "decision": decision,
                        },
                    )
                )
            if fact_id is not None:
                assert fact_payload is not None and fact_kind is not None
                semantic_digest = canonical_digest(
                    "pulsara:memory-fact:v1",
                    {
                        "workspace_id": workspace_id,
                        "fact_kind": fact_kind,
                        "fact_payload": dict(fact_payload),
                    },
                )
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_facts (
                        id, workspace_id, governance_decision_id, lifecycle,
                        fact_kind, fact_payload, semantic_digest
                    ) VALUES (%s, %s, %s, 'ACTIVE', %s, %s, %s)
                    """,
                    (
                        fact_id,
                        workspace_id,
                        decision_id,
                        fact_kind,
                        Jsonb(dict(fact_payload)),
                        semantic_digest,
                    ),
                )
                drafts.append(
                    self._event(
                        CommittedEventType.MEMORY_FACT_ACCEPTED,
                        SubjectSlot.MEMORY_FACT,
                        fact_id,
                        occurred_at=occurred_at,
                        actor_kind="job_worker",
                        actor_id=guard.claim_owner_id,
                        payload={"fact_kind": fact_kind},
                    )
                )
                for relation_id, target_fact_id, relation_kind in relations:
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.memory_relations (
                            id, workspace_id, source_fact_id,
                            target_fact_id, relation_kind
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            relation_id,
                            workspace_id,
                            fact_id,
                            target_fact_id,
                            relation_kind,
                        ),
                    )
                    relation_ids.append(relation_id)
                    drafts.append(
                        self._event(
                            CommittedEventType.MEMORY_RELATION_ACCEPTED,
                            SubjectSlot.MEMORY_RELATION,
                            relation_id,
                            occurred_at=occurred_at,
                            actor_kind="job_worker",
                            actor_id=guard.claim_owner_id,
                            payload={"relation_kind": relation_kind},
                        )
                    )
                for channel in ("FTS", "VECTOR"):
                    connection.execute(
                        """
                        INSERT INTO pulsara_v3.memory_index_state (
                            workspace_id, channel, desired_generation,
                            desired_handler_contract_id,
                            desired_handler_contract_version,
                            applied_generation, applied_handler_contract_id,
                            applied_handler_contract_version
                        ) VALUES (%s, %s, 1, %s, %s, 0, %s, %s)
                        ON CONFLICT (workspace_id, channel) DO UPDATE
                        SET desired_generation =
                                pulsara_v3.memory_index_state.desired_generation + 1,
                            desired_handler_contract_id = EXCLUDED.desired_handler_contract_id,
                            desired_handler_contract_version = EXCLUDED.desired_handler_contract_version
                        """,
                        (
                            workspace_id,
                            channel,
                            index_handler_contract_id,
                            index_handler_contract_version,
                            index_handler_contract_id,
                            index_handler_contract_version,
                        ),
                    )
            drafts.append(
                self._event(
                    CommittedEventType.JOB_TERMINAL_ACCEPTED,
                    SubjectSlot.JOB,
                    guard.job_id,
                    occurred_at=occurred_at,
                    actor_kind="job_worker",
                    actor_id=guard.claim_owner_id,
                    payload={"status": "SUCCEEDED", "terminal_reason": None},
                )
            )
            if guard.origin_session_id is not None:
                self._append_events(
                    connection,
                    guard,
                    workspace_id=workspace_id,
                    drafts=tuple(drafts),
                )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb({"decision_id": decision_id, "fact_id": fact_id}),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'GOVERNANCE_ACCEPTED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return AcceptedMemoryGovernance(
            candidate_id,
            decision_id,
            decision,
            fact_id,
            tuple(relation_ids),
        )

    def apply_fts_memory_index(
        self,
        guard: JobAttemptClaimGuard,
        *,
        handler_contract_id: str,
        handler_contract_version: int,
        deadline_monotonic: float,
    ) -> int:
        """Apply one exact FTS target; this port cannot mutate conversation rows."""

        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT j.intent_payload, j.workspace_id
                FROM pulsara_v3.durable_jobs AS j
                WHERE j.id = %s AND j.handler_type = 'MEMORY_INDEX_REFRESH'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("index refresh job is absent")
            intent = dict(job["intent_payload"])
            if (
                intent.get("channel") != "FTS"
                or intent.get("workspace_id") != job["workspace_id"]
                or intent.get("handler_contract_id") != handler_contract_id
                or intent.get("handler_contract_version") != handler_contract_version
            ):
                raise ConversationKernelConflict("index refresh intent mismatch")
            target = int(intent["target_generation"])
            state = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel = 'FTS'
                FOR UPDATE
                """,
                (job["workspace_id"],),
            ).fetchone()
            if (
                state is None
                or int(state["desired_generation"]) != target
                or state["desired_handler_contract_id"] != handler_contract_id
                or int(state["desired_handler_contract_version"])
                != handler_contract_version
            ):
                raise ConversationKernelConflict("index target was superseded")
            connection.execute(
                "DELETE FROM pulsara_v3.memory_search_index WHERE workspace_id = %s",
                (job["workspace_id"],),
            )
            inserted = connection.execute(
                """
                INSERT INTO pulsara_v3.memory_search_index (
                    workspace_id, fact_id, generation, search_document
                )
                SELECT workspace_id, id, %s,
                       to_tsvector('simple', fact_kind || ' ' || fact_payload::text)
                FROM pulsara_v3.memory_facts
                WHERE workspace_id = %s AND lifecycle = 'ACTIVE'
                RETURNING fact_id
                """,
                (target, job["workspace_id"]),
            ).fetchall()
            connection.execute(
                """
                UPDATE pulsara_v3.memory_index_state
                SET applied_generation = %s,
                    applied_handler_contract_id = %s,
                    applied_handler_contract_version = %s
                WHERE workspace_id = %s AND channel = 'FTS'
                """,
                (
                    target,
                    handler_contract_id,
                    handler_contract_version,
                    job["workspace_id"],
                ),
            )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb({"indexed_fact_count": len(inserted)}),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'INDEX_APPLIED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return len(inserted)

    def snapshot_memory_vector_source(
        self,
        guard: JobAttemptClaimGuard,
        *,
        handler_contract_id: str,
        handler_contract_version: int,
        deadline_monotonic: float,
    ) -> MemoryVectorSource:
        """Freeze the immutable input of one exact VECTOR refresh attempt."""

        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT intent_payload, workspace_id
                FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'MEMORY_INDEX_REFRESH'
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("vector refresh job is absent")
            intent = dict(job["intent_payload"])
            if (
                intent.get("channel") != "VECTOR"
                or intent.get("workspace_id") != job["workspace_id"]
                or intent.get("handler_contract_id") != handler_contract_id
                or intent.get("handler_contract_version") != handler_contract_version
            ):
                raise ConversationKernelConflict("vector refresh intent mismatch")
            target = _required_nonnegative_int(
                intent.get("target_generation"), "target_generation"
            )
            state = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel = 'VECTOR'
                """,
                (job["workspace_id"],),
            ).fetchone()
            if (
                state is None
                or int(state["desired_generation"]) != target
                or state["desired_handler_contract_id"] != handler_contract_id
                or int(state["desired_handler_contract_version"])
                != handler_contract_version
            ):
                raise ConversationKernelConflict("vector index target was superseded")
            facts = tuple(
                MemoryVectorFactSource(
                    fact_id=str(row["id"]),
                    semantic_digest=str(row["semantic_digest"]),
                    embedding_text=(
                        str(row["fact_kind"])
                        + " "
                        + json.dumps(
                            dict(row["fact_payload"]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                )
                for row in connection.execute(
                    """
                    SELECT id, fact_kind, fact_payload, semantic_digest
                    FROM pulsara_v3.memory_facts
                    WHERE workspace_id = %s AND lifecycle = 'ACTIVE'
                    ORDER BY id
                    """,
                    (job["workspace_id"],),
                ).fetchall()
            )
            digest = canonical_digest(
                "pulsara:memory-vector-source:v1",
                {
                    "workspace_id": str(job["workspace_id"]),
                    "target_generation": target,
                    "handler_contract_id": handler_contract_id,
                    "handler_contract_version": handler_contract_version,
                    "facts": tuple(
                        {
                            "fact_id": item.fact_id,
                            "semantic_digest": item.semantic_digest,
                            "embedding_text": item.embedding_text,
                        }
                        for item in facts
                    ),
                },
            )
            return MemoryVectorSource(
                workspace_id=str(job["workspace_id"]),
                target_generation=target,
                handler_contract_id=handler_contract_id,
                handler_contract_version=handler_contract_version,
                source_digest=digest,
                facts=facts,
            )

    def apply_vector_memory_index(
        self,
        guard: JobAttemptClaimGuard,
        *,
        source: MemoryVectorSource,
        embeddings: Sequence[Sequence[float]],
        deadline_monotonic: float,
    ) -> int:
        if len(embeddings) != len(source.facts):
            raise ValueError("vector result count does not match source facts")
        normalized: list[str] = []
        dimensions: int | None = None
        for vector in embeddings:
            values = tuple(float(value) for value in vector)
            if not values or any(not math.isfinite(value) for value in values):
                raise ValueError("vector result contains invalid coordinates")
            if dimensions is None:
                dimensions = len(values)
            elif dimensions != len(values):
                raise ValueError("vector result dimensions are inconsistent")
            normalized.append(
                "[" + ",".join(format(value, ".17g") for value in values) + "]"
            )
        with self._job_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            job = connection.execute(
                """
                SELECT intent_payload, workspace_id
                FROM pulsara_v3.durable_jobs
                WHERE id = %s AND handler_type = 'MEMORY_INDEX_REFRESH'
                FOR UPDATE
                """,
                (guard.job_id,),
            ).fetchone()
            if job is None:
                raise ConversationKernelConflict("vector refresh job is absent")
            intent = dict(job["intent_payload"])
            if (
                source.workspace_id != job["workspace_id"]
                or intent.get("channel") != "VECTOR"
                or int(intent.get("target_generation", -1)) != source.target_generation
                or intent.get("handler_contract_id") != source.handler_contract_id
                or intent.get("handler_contract_version")
                != source.handler_contract_version
            ):
                raise ConversationKernelConflict(
                    "vector refresh source identity drifted"
                )
            state = connection.execute(
                """
                SELECT * FROM pulsara_v3.memory_index_state
                WHERE workspace_id = %s AND channel = 'VECTOR'
                FOR UPDATE
                """,
                (source.workspace_id,),
            ).fetchone()
            current = tuple(
                MemoryVectorFactSource(
                    fact_id=str(row["id"]),
                    semantic_digest=str(row["semantic_digest"]),
                    embedding_text=(
                        str(row["fact_kind"])
                        + " "
                        + json.dumps(
                            dict(row["fact_payload"]),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                )
                for row in connection.execute(
                    """
                    SELECT id, fact_kind, fact_payload, semantic_digest
                    FROM pulsara_v3.memory_facts
                    WHERE workspace_id = %s AND lifecycle = 'ACTIVE'
                    ORDER BY id
                    """,
                    (source.workspace_id,),
                ).fetchall()
            )
            expected_digest = canonical_digest(
                "pulsara:memory-vector-source:v1",
                {
                    "workspace_id": source.workspace_id,
                    "target_generation": source.target_generation,
                    "handler_contract_id": source.handler_contract_id,
                    "handler_contract_version": source.handler_contract_version,
                    "facts": tuple(
                        {
                            "fact_id": item.fact_id,
                            "semantic_digest": item.semantic_digest,
                            "embedding_text": item.embedding_text,
                        }
                        for item in current
                    ),
                },
            )
            if (
                state is None
                or int(state["desired_generation"]) != source.target_generation
                or state["desired_handler_contract_id"] != source.handler_contract_id
                or int(state["desired_handler_contract_version"])
                != source.handler_contract_version
                or current != source.facts
                or expected_digest != source.source_digest
            ):
                raise ConversationKernelConflict("vector refresh source was superseded")
            connection.execute(
                "DELETE FROM pulsara_v3.memory_vector_index WHERE workspace_id = %s",
                (source.workspace_id,),
            )
            for fact, vector_literal in zip(source.facts, normalized, strict=True):
                connection.execute(
                    """
                    INSERT INTO pulsara_v3.memory_vector_index (
                        workspace_id, fact_id, generation, embedding
                    ) VALUES (%s, %s, %s, %s::public.vector)
                    """,
                    (
                        source.workspace_id,
                        fact.fact_id,
                        source.target_generation,
                        vector_literal,
                    ),
                )
            connection.execute(
                """
                UPDATE pulsara_v3.memory_index_state
                SET applied_generation = %s,
                    applied_handler_contract_id = %s,
                    applied_handler_contract_version = %s
                WHERE workspace_id = %s AND channel = 'VECTOR'
                """,
                (
                    source.target_generation,
                    source.handler_contract_id,
                    source.handler_contract_version,
                    source.workspace_id,
                ),
            )
            terminal_at = _utcnow()
            connection.execute(
                """
                UPDATE pulsara_v3.durable_job_attempts
                SET terminal_status = 'SUCCEEDED', result_payload = %s,
                    terminal_at = %s
                WHERE id = %s AND terminal_status IS NULL
                """,
                (
                    Jsonb(
                        {
                            "indexed_fact_count": len(source.facts),
                            "source_digest": source.source_digest,
                            "dimensions": dimensions or 0,
                        }
                    ),
                    terminal_at,
                    guard.attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE pulsara_v3.durable_jobs
                SET status = 'SUCCEEDED', terminal_reason = 'INDEX_APPLIED',
                    terminal_at = %s
                WHERE id = %s AND status = 'ACTIVE'
                """,
                (terminal_at, guard.job_id),
            )
        return len(source.facts)

    def rehydrate_session(
        self,
        *,
        session_id: str,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT e.*, b.block_ordinal, b.block_kind, b.tool_call_id,
                           b.tool_name, b.tool_arguments,
                           b.inline_content AS block_inline_content,
                           b.blob_id AS block_blob_id
                    FROM pulsara_v3.transcript_entries AS e
                    LEFT JOIN pulsara_v3.assistant_message_blocks AS b
                      ON b.session_id = e.session_id
                     AND b.assistant_entry_id = e.id
                    WHERE e.session_id = %s
                    ORDER BY e.entry_sequence, b.block_ordinal NULLS FIRST
                    """,
                    (session_id,),
                ).fetchall()
            )

    def query_command(
        self,
        *,
        session_id: str,
        command_id: str,
        deadline_monotonic: float,
    ) -> Mapping[str, object] | None:
        """Return the canonical command target; no process receipt is replayed."""
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
            isolation_level=IsolationLevel.REPEATABLE_READ,
        ) as connection:
            row = connection.execute(
                """
                SELECT c.*, t.status AS turn_status, t.final_entry_id,
                       t.terminal_reason,
                       q.status AS queue_status,
                       q.terminal_reason AS queue_terminal_reason,
                       q.consumed_entry_id,
                       qe.turn_id AS consumed_turn_id,
                       qt.status AS consumed_turn_status,
                       qt.final_entry_id AS consumed_turn_final_entry_id,
                       qt.terminal_reason AS consumed_turn_terminal_reason,
                       te.turn_id AS target_entry_turn_id,
                       te.source_job_id AS target_entry_source_job_id,
                       te.source_subagent_result_id AS target_entry_source_subagent_result_id,
                       d.decision AS interaction_decision,
                       d.subject_kind AS interaction_subject_kind,
                       d.subject_tool_call_entry_id,
                       d.subject_tool_call_id
                FROM pulsara_v3.session_commands AS c
                LEFT JOIN pulsara_v3.turns AS t
                  ON t.session_id = c.session_id AND t.id = c.target_turn_id
                LEFT JOIN pulsara_v3.prompt_queue_items AS q
                  ON q.session_id = c.session_id AND q.id = c.target_queue_item_id
                LEFT JOIN pulsara_v3.transcript_entries AS qe
                  ON qe.session_id = q.session_id AND qe.id = q.consumed_entry_id
                LEFT JOIN pulsara_v3.turns AS qt
                  ON qt.session_id = qe.session_id AND qt.id = qe.turn_id
                LEFT JOIN pulsara_v3.transcript_entries AS te
                  ON te.session_id = c.session_id AND te.id = c.target_entry_id
                LEFT JOIN pulsara_v3.interaction_decisions AS d
                  ON d.session_id = c.session_id
                 AND d.id = c.target_interaction_decision_id
                WHERE c.session_id = %s AND c.command_id = %s
                """,
                (session_id, command_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def close_session(
        self,
        guard: HostWriterGuard,
        *,
        deadline_monotonic: float,
    ) -> None:
        with self._writer_transaction(
            guard, deadline_monotonic=deadline_monotonic
        ) as connection:
            running = connection.execute(
                """SELECT id FROM pulsara_v3.turns
                   WHERE session_id = %s AND status = 'RUNNING'""",
                (guard.session_id,),
            ).fetchall()
            if running:
                raise ConversationKernelConflict(
                    "session cannot close while canonical turns are running"
                )
            connection.execute(
                """
                UPDATE pulsara_v3.sessions
                SET lifecycle = 'CLOSED', writer_lease_owner_id = NULL,
                    writer_lease_expires_at = NULL, updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (guard.session_id,),
            )

    def events_after(
        self,
        *,
        session_id: str,
        after_sequence: int,
        limit: int,
        deadline_monotonic: float,
    ) -> tuple[Mapping[str, object], ...]:
        if limit < 1 or limit > 1024:
            raise ValueError("event page limit is out of bounds")
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM pulsara_v3.agent_events
                    WHERE session_id = %s AND event_sequence > %s
                    ORDER BY event_sequence
                    LIMIT %s
                    """,
                    (session_id, after_sequence, limit),
                ).fetchall()
            )

    def _writer_transaction(self, guard: HostWriterGuard, *, deadline_monotonic: float):
        repository = self

        class _Scope:
            def __enter__(self) -> Connection:
                repository._begin_event_batch()
                self._cm = repository._provider.connection(
                    lane=PostgresConnectionLane.HOST_CONTROL,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                )
                try:
                    connection = self._cm.__enter__()
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                try:
                    repository._require_writer(connection, guard, lock=True)
                except BaseException as error:
                    self._cm.__exit__(type(error), error, error.__traceback__)
                    repository._finish_event_batch(committed=False)
                    raise
                self._connection = connection
                return connection

            def __exit__(self, exc_type, exc, tb):
                try:
                    result = self._cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                repository._finish_event_batch(committed=exc_type is None)
                return result

        return _Scope()

    def _job_transaction(
        self,
        guard: JobAttemptClaimGuard,
        *,
        deadline_monotonic: float,
        allow_cancel_requested: bool = False,
    ):
        repository = self

        class _Scope:
            def __enter__(self) -> Connection:
                repository._begin_event_batch()
                self._cm = repository._provider.connection(
                    lane=PostgresConnectionLane.BACKGROUND_WORK,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                )
                try:
                    connection = self._cm.__enter__()
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                try:
                    if guard.origin_session_id is not None:
                        session = connection.execute(
                            """
                            SELECT id FROM pulsara_v3.sessions
                            WHERE id = %s
                            FOR UPDATE
                            """,
                            (guard.origin_session_id,),
                        ).fetchone()
                        if session is None:
                            raise StaleJobClaim("job origin session is absent")
                    repository._require_job_claim(connection, guard, lock=True)
                    if not allow_cancel_requested:
                        cancellation = connection.execute(
                            """
                            SELECT cancel_requested_at
                            FROM pulsara_v3.durable_jobs
                            WHERE id = %s
                            """,
                            (guard.job_id,),
                        ).fetchone()
                        if (
                            cancellation is not None
                            and cancellation["cancel_requested_at"] is not None
                        ):
                            raise JobCancellationRequested(
                                "job cancellation was requested"
                            )
                except BaseException as error:
                    self._cm.__exit__(type(error), error, error.__traceback__)
                    repository._finish_event_batch(committed=False)
                    raise
                self._connection = connection
                return connection

            def __exit__(self, exc_type, exc, tb):
                try:
                    result = self._cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                repository._finish_event_batch(committed=exc_type is None)
                return result

        return _Scope()

    def _event_transaction(
        self, *, lane: PostgresConnectionLane, deadline_monotonic: float
    ):
        repository = self

        class _Scope:
            def __enter__(self) -> Connection:
                repository._begin_event_batch()
                self._cm = repository._provider.connection(
                    lane=lane,
                    row_factory=dict_row,
                    deadline_monotonic=deadline_monotonic,
                )
                try:
                    return self._cm.__enter__()
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise

            def __exit__(self, exc_type, exc, tb):
                try:
                    result = self._cm.__exit__(exc_type, exc, tb)
                except BaseException:
                    repository._finish_event_batch(committed=False)
                    raise
                repository._finish_event_batch(committed=exc_type is None)
                return result

        return _Scope()

    def _begin_event_batch(self) -> None:
        stack = getattr(self._event_batch_local, "stack", None)
        if stack is None:
            stack = []
            self._event_batch_local.stack = stack
        stack.append([])

    def _record_event_batch(self, events: Sequence[StoredCommittedEvent]) -> None:
        if not events:
            return
        stack = getattr(self._event_batch_local, "stack", None)
        if stack:
            stack[-1].extend(events)

    def _finish_event_batch(self, *, committed: bool) -> None:
        stack = getattr(self._event_batch_local, "stack", None)
        if not stack:
            raise RuntimeError("repository event batch owner is absent")
        events = tuple(stack.pop())
        if not stack:
            del self._event_batch_local.stack
        if not committed:
            return
        if stack:
            stack[-1].extend(events)
            return
        tap = self._post_commit_tap
        if tap is None or not events:
            return
        try:
            tap(events)
        except BaseException:
            # An extension tap is process-local best effort.  A committed
            # canonical transaction can never be reclassified by observation.
            return

    @staticmethod
    def _require_writer(
        connection: Connection, guard: HostWriterGuard, *, lock: bool
    ) -> Mapping[str, object]:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT * FROM pulsara_v3.sessions
            WHERE id = %s AND lifecycle = 'OPEN'
              AND writer_generation = %s AND writer_lease_owner_id = %s
              AND writer_lease_expires_at > clock_timestamp()
            """
            + suffix,
            (guard.session_id, guard.writer_generation, guard.writer_owner_id),
        ).fetchone()
        if row is None:
            raise StaleHostWriter("host writer generation is stale")
        return row

    @staticmethod
    def _require_job_claim(
        connection: Connection, guard: JobAttemptClaimGuard, *, lock: bool
    ) -> Mapping[str, object]:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            """
            SELECT a.*, j.status AS job_status
            FROM pulsara_v3.durable_job_attempts AS a
            JOIN pulsara_v3.durable_jobs AS j ON j.id = a.job_id
            WHERE a.id = %s AND a.job_id = %s
              AND a.claim_generation = %s AND a.claim_owner_id = %s
              AND a.lease_expires_at > clock_timestamp()
              AND a.terminal_status IS NULL AND j.status = 'ACTIVE'
            """
            + suffix,
            (
                guard.attempt_id,
                guard.job_id,
                guard.claim_generation,
                guard.claim_owner_id,
            ),
        ).fetchone()
        if row is None:
            raise StaleJobClaim("job attempt claim is stale")
        return row

    def _interrupt_prior_generation(
        self,
        connection: Connection,
        *,
        guard: HostWriterGuard,
        workspace_id: str,
    ) -> None:
        session_id = guard.session_id
        turn_ids = tuple(
            str(row["id"])
            for row in connection.execute(
                """
                UPDATE pulsara_v3.turns
                SET status = 'INTERRUPTED', terminal_reason = 'HOST_TAKEOVER',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s AND status = 'RUNNING'
                RETURNING id
                """,
                (session_id,),
            ).fetchall()
        )
        task_ids = tuple(
            str(row["id"])
            for row in connection.execute(
                """
                UPDATE pulsara_v3.subagent_tasks
                SET status = 'INTERRUPTED',
                    terminal_reason = 'HOST_TAKEOVER',
                    terminal_at = clock_timestamp()
                WHERE session_id = %s
                  AND status IN ('PENDING', 'ACTIVE')
                RETURNING id
                """,
                (session_id,),
            ).fetchall()
        )
        rejected_steer_ids: tuple[str, ...] = ()
        if turn_ids:
            rejected_steer_ids = tuple(
                str(row["id"])
                for row in connection.execute(
                    """
                    UPDATE pulsara_v3.prompt_queue_items
                    SET status = 'REJECTED',
                        terminal_reason = 'TARGET_TURN_INTERRUPTED',
                        terminal_at = clock_timestamp()
                    WHERE session_id = %s AND status = 'PENDING'
                      AND delivery_mode = 'STEER_ACTIVE_TURN'
                      AND target_turn_id = ANY(%s)
                    RETURNING id
                    """,
                    (session_id, list(turn_ids)),
                ).fetchall()
            )
        if turn_ids or task_ids or rejected_steer_ids:
            occurred_at = _utcnow()
            drafts = (
                tuple(
                    self._event(
                        CommittedEventType.TURN_INTERRUPTED,
                        SubjectSlot.TURN,
                        turn_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"reason": "HOST_TAKEOVER"},
                    )
                    for turn_id in turn_ids
                )
                + tuple(
                    self._event(
                        CommittedEventType.SUBAGENT_TASK_STATUS_ACCEPTED,
                        SubjectSlot.SUBAGENT_TASK,
                        task_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"status": "INTERRUPTED", "reason": "HOST_TAKEOVER"},
                    )
                    for task_id in task_ids
                )
                + tuple(
                    self._event(
                        CommittedEventType.PROMPT_REJECTED,
                        SubjectSlot.QUEUE_ITEM,
                        queue_item_id,
                        occurred_at=occurred_at,
                        actor_kind="runtime",
                        actor_id=guard.writer_owner_id,
                        payload={"reason": "TARGET_TURN_INTERRUPTED"},
                    )
                    for queue_item_id in rejected_steer_ids
                )
            )
            self._append_events(
                connection,
                guard,
                workspace_id=workspace_id,
                drafts=drafts,
            )

    @staticmethod
    def _workspace_id(connection: Connection, session_id: str) -> str:
        row = connection.execute(
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("session is absent")
        return str(row["workspace_id"])

    @staticmethod
    def _require_provider_safe_turn_in_transaction(
        connection: Connection,
        *,
        session_id: str,
        turn_id: str,
        lock: bool,
    ) -> Mapping[str, object]:
        lock_clause = "FOR UPDATE OF t" if lock else ""
        row = connection.execute(
            f"""
            SELECT t.*
            FROM pulsara_v3.turns AS t
            WHERE t.session_id = %s AND t.id = %s AND t.status = 'RUNNING'
              AND NOT EXISTS (
                SELECT 1
                FROM pulsara_v3.assistant_message_blocks AS b
                LEFT JOIN pulsara_v3.tool_results AS r
                  ON r.session_id = b.session_id
                 AND r.tool_call_entry_id = b.assistant_entry_id
                 AND r.tool_call_id = b.tool_call_id
                WHERE b.session_id = t.session_id
                  AND b.block_kind = 'TOOL_CALL'
                  AND r.id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM pulsara_v3.transcript_entries AS e
                    WHERE e.session_id = b.session_id
                      AND e.id = b.assistant_entry_id
                      AND e.turn_id = t.id
                  )
              )
            {lock_clause}
            """,
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("turn is not at a provider safe point")
        return row

    @staticmethod
    def _allocate_entry_sequence(connection: Connection, session_id: str) -> int:
        row = connection.execute(
            """
            UPDATE pulsara_v3.sessions
            SET latest_entry_sequence = latest_entry_sequence + 1,
                updated_at = clock_timestamp()
            WHERE id = %s
            RETURNING latest_entry_sequence
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("session is absent")
        return int(row["latest_entry_sequence"])

    @staticmethod
    def _allocate_event_range(
        connection: Connection, session_id: str, count: int
    ) -> int:
        if count < 1:
            raise ValueError("event allocation count must be positive")
        row = connection.execute(
            """
            UPDATE pulsara_v3.sessions
            SET latest_event_sequence = latest_event_sequence + %s,
                updated_at = clock_timestamp()
            WHERE id = %s
            RETURNING latest_event_sequence
            """,
            (count, session_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("session is absent")
        return int(row["latest_event_sequence"]) - count + 1

    def _append_events(
        self,
        connection: Connection,
        guard: HostWriterGuard | JobAttemptClaimGuard,
        *,
        workspace_id: str,
        drafts: Sequence[CommittedEventDraft],
    ) -> tuple[StoredCommittedEvent, ...]:
        if not drafts:
            return ()
        if isinstance(guard, HostWriterGuard):
            self._require_writer(connection, guard, lock=False)
            session_id = guard.session_id
            guard_kind = AppendGuardKind.HOST_WRITER
        else:
            self._require_job_claim(connection, guard, lock=False)
            if guard.origin_session_id is None:
                raise ValueError("global job cannot append a session occurrence")
            session_id = guard.origin_session_id
            guard_kind = AppendGuardKind.JOB_ATTEMPT_CLAIM
        for draft in drafts:
            descriptor = DESCRIPTOR_BY_TYPE[draft.event_type]
            if draft.subject.slot is not descriptor.subject_slot:
                raise ValueError("event subject slot does not match descriptor")
            if guard_kind not in descriptor.append_guards:
                raise ValueError("append guard is not permitted for event type")
        start = self._allocate_event_range(connection, session_id, len(drafts))
        events = tuple(
            self._insert_event(
                connection,
                workspace_id=workspace_id,
                session_id=session_id,
                sequence=start + offset,
                draft=draft,
                turn_id=self._resolve_event_turn_id(connection, draft.subject),
            )
            for offset, draft in enumerate(drafts)
        )
        self._record_event_batch(events)
        return events

    @staticmethod
    def _insert_event(
        connection: Connection,
        *,
        workspace_id: str,
        session_id: str,
        sequence: int,
        draft: CommittedEventDraft,
        turn_id: str | None,
    ) -> StoredCommittedEvent:
        slots = {slot.value: None for slot in SubjectSlot}
        slots[draft.subject.slot.value] = draft.subject.subject_id
        ordered_slots = tuple(slots[slot.value] for slot in SubjectSlot)
        subagent_child_kind = None
        if draft.subject.slot is SubjectSlot.SUBAGENT_MESSAGE:
            subagent_child_kind = "MESSAGE"
        elif draft.subject.slot is SubjectSlot.SUBAGENT_RESULT:
            subagent_child_kind = "RESULT"
        row = connection.execute(
            """
            INSERT INTO pulsara_v3.agent_events (
                event_id, workspace_id, session_id, event_sequence,
                namespace, event_type, schema_major, schema_minor,
                occurred_at, actor_kind, actor_id, sensitivity_class,
                projection_profile, payload,
                subject_turn_id, subject_entry_id, subject_tool_attempt_id,
                subject_job_id, subject_job_attempt_id, subject_queue_item_id,
                subject_interaction_decision_id,
                subject_context_binding_revision_id,
                subject_subagent_task_id, subject_subagent_message_id,
                subject_subagent_result_id, subject_subagent_child_kind,
                subject_memory_fact_id,
                subject_memory_relation_id
            ) VALUES (
                %s, %s, %s, %s, 'pulsara.core', %s, 1, 0,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING accepted_at
            """,
            (
                draft.event_id,
                workspace_id,
                session_id,
                sequence,
                draft.event_type.value,
                draft.occurred_at,
                draft.actor_kind,
                draft.actor_id,
                draft.sensitivity_class,
                draft.projection_profile,
                Jsonb(dict(draft.payload)),
                *ordered_slots[:11],
                subagent_child_kind,
                *ordered_slots[11:],
            ),
        ).fetchone()
        assert row is not None
        return StoredCommittedEvent(
            event_id=draft.event_id,
            workspace_id=workspace_id,
            session_id=session_id,
            event_sequence=sequence,
            event_type=draft.event_type,
            subject=draft.subject,
            accepted_at=row["accepted_at"],
            occurred_at=draft.occurred_at,
            actor_kind=draft.actor_kind,
            actor_id=draft.actor_id,
            sensitivity_class=draft.sensitivity_class,
            projection_profile=draft.projection_profile,
            payload=draft.payload,
            turn_id=turn_id,
        )

    @staticmethod
    def _resolve_event_turn_id(
        connection: Connection, subject: CommittedEventSubject
    ) -> str | None:
        slot = subject.slot
        identity = subject.subject_id
        if slot is SubjectSlot.TURN:
            return identity
        query: str | None = None
        if slot is SubjectSlot.ENTRY:
            query = "SELECT turn_id FROM pulsara_v3.transcript_entries WHERE id = %s"
        elif slot is SubjectSlot.TOOL_ATTEMPT:
            query = """
                SELECT e.turn_id
                FROM pulsara_v3.tool_execution_attempts AS a
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = a.session_id AND e.id = a.assistant_entry_id
                WHERE a.id = %s
            """
        elif slot is SubjectSlot.QUEUE_ITEM:
            query = """
                SELECT coalesce(q.target_turn_id, e.turn_id) AS turn_id
                FROM pulsara_v3.prompt_queue_items AS q
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = q.session_id AND e.id = q.consumed_entry_id
                WHERE q.id = %s
            """
        elif slot is SubjectSlot.INTERACTION_DECISION:
            query = """
                SELECT coalesce(d.subject_turn_id, e.turn_id) AS turn_id
                FROM pulsara_v3.interaction_decisions AS d
                LEFT JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = d.session_id
                 AND e.id = d.subject_tool_call_entry_id
                WHERE d.id = %s
            """
        elif slot is SubjectSlot.CONTEXT_BINDING_REVISION:
            query = """
                SELECT turn_id FROM pulsara_v3.turn_context_binding_revisions
                WHERE id = %s
            """
        elif slot is SubjectSlot.SUBAGENT_TASK:
            query = "SELECT parent_turn_id AS turn_id FROM pulsara_v3.subagent_tasks WHERE id = %s"
        elif slot in {SubjectSlot.SUBAGENT_MESSAGE, SubjectSlot.SUBAGENT_RESULT}:
            query = """
                SELECT e.turn_id
                FROM pulsara_v3.subagent_task_children AS c
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = c.session_id AND e.id = c.entry_id
                WHERE c.id = %s
            """
        if query is None:
            return None
        row = connection.execute(query, (identity,)).fetchone()
        if row is None or row["turn_id"] is None:
            return None
        return str(row["turn_id"])

    @staticmethod
    def _event(
        event_type: CommittedEventType,
        slot: SubjectSlot,
        subject_id: str,
        *,
        occurred_at: datetime,
        actor_kind: str,
        actor_id: str,
        payload: Mapping[str, object],
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=_id("event"),
            event_type=event_type,
            subject=CommittedEventSubject(slot=slot, subject_id=subject_id),
            actor_kind=actor_kind,
            actor_id=actor_id,
            sensitivity_class="PUBLIC",
            projection_profile="DEFAULT",
            occurred_at=occurred_at,
            payload=payload,
        )

    @staticmethod
    def _terminal_observation_event(
        candidate: TerminalObservationInstallationAttempt,
        entry_id: str,
    ) -> CommittedEventDraft:
        return CommittedEventDraft(
            event_id=_stable_identity(
                "event",
                candidate.content.observation_id,
                CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED.value,
            ),
            event_type=CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED,
            subject=CommittedEventSubject(
                slot=SubjectSlot.ENTRY,
                subject_id=entry_id,
            ),
            actor_kind="runtime",
            actor_id=candidate.actor_id,
            sensitivity_class="S1",
            projection_profile="IMMUTABLE_ENTRY",
            occurred_at=candidate.occurred_at,
            payload={
                "entry_kind": EntryKind.TERMINAL_OBSERVATION.value,
                "observation_kind": candidate.content.observation_kind.value,
            },
        )

    @staticmethod
    def _insert_entry(
        connection: Connection,
        *,
        session_id: str,
        workspace_id: str,
        turn_id: str,
        entry_id: str,
        entry_sequence: int,
        entry_kind: EntryKind,
        scope_kind: ConversationScopeKind,
        scope_task_id: str | None,
        content: CanonicalContent,
        context_binding_revision_id: str | None = None,
        provider_input_through_sequence: int | None = None,
        source_job_id: str | None = None,
        source_subagent_result_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO pulsara_v3.transcript_entries (
                id, session_id, workspace_id, turn_id, entry_sequence,
                entry_kind, conversation_scope_kind, scope_subagent_task_id,
                context_binding_revision_id, provider_input_through_sequence,
                source_job_id, source_subagent_result_id,
                inline_content, blob_id, content_digest, content_size,
                content_media_type, content_codec
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entry_id,
                session_id,
                workspace_id,
                turn_id,
                entry_sequence,
                entry_kind.value,
                scope_kind.value,
                scope_task_id,
                context_binding_revision_id,
                provider_input_through_sequence,
                source_job_id,
                source_subagent_result_id,
                *_content_columns(content),
            ),
        )

    @staticmethod
    def _insert_assistant_block(
        connection: Connection,
        *,
        session_id: str,
        workspace_id: str,
        entry_id: str,
        ordinal: int,
        block: AssistantBlock,
    ) -> None:
        if isinstance(block, AssistantToolCallBlock):
            tool_arguments = thaw_json(block.arguments)
            if not isinstance(tool_arguments, dict):
                raise TypeError("assistant tool-call arguments must thaw as an object")
            connection.execute(
                """
                INSERT INTO pulsara_v3.assistant_message_blocks (
                    id, session_id, workspace_id, assistant_entry_id, block_ordinal,
                    block_kind, tool_call_id, tool_name, tool_arguments
                ) VALUES (%s, %s, %s, %s, %s, 'TOOL_CALL', %s, %s, %s)
                """,
                (
                    block.block_id,
                    session_id,
                    workspace_id,
                    entry_id,
                    ordinal,
                    block.tool_call_id,
                    block.tool_name,
                    Jsonb(tool_arguments),
                ),
            )
            return
        kind = (
            AssistantBlockKind.TEXT
            if isinstance(block, AssistantTextBlock)
            else AssistantBlockKind.DATA
        )
        content = block.text if isinstance(block, AssistantTextBlock) else block.data
        connection.execute(
            """
            INSERT INTO pulsara_v3.assistant_message_blocks (
                id, session_id, workspace_id, assistant_entry_id,
                block_ordinal, block_kind,
                inline_content, blob_id, content_digest, content_size,
                content_media_type, content_codec
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                block.block_id,
                session_id,
                workspace_id,
                entry_id,
                ordinal,
                kind.value,
                *_content_columns(content),
            ),
        )

    @staticmethod
    def _accepted_entry(
        connection: Connection, session_id: str, entry_id: str
    ) -> AcceptedEntry:
        row = connection.execute(
            """
            SELECT e.id, e.turn_id, e.entry_sequence, a.event_sequence
            FROM pulsara_v3.transcript_entries AS e
            JOIN pulsara_v3.agent_events AS a
              ON a.session_id = e.session_id
             AND a.subject_entry_id = e.id
            WHERE e.session_id = %s AND e.id = %s
            """,
            (session_id, entry_id),
        ).fetchone()
        if row is None:
            raise ConversationKernelConflict("accepted command target is absent")
        return AcceptedEntry(
            entry_id=str(row["id"]),
            turn_id=str(row["turn_id"]),
            entry_sequence=int(row["entry_sequence"]),
            event_sequence=int(row["event_sequence"]),
        )

    @staticmethod
    def _content_from_row(row: Mapping[str, object]) -> CanonicalContent:
        if row["inline_content"] is not None:
            return InlineContent(
                canonical_bytes=bytes(row["inline_content"]),
                digest=str(row["content_digest"]),
                size=int(row["content_size"]),
                media_type=str(row["content_media_type"]),
                codec=str(row["content_codec"]),
            )
        return BlobContent(
            blob_id=str(row["blob_id"]),
            digest=str(row["content_digest"]),
            size=int(row["content_size"]),
            media_type=str(row["content_media_type"]),
            codec=str(row["content_codec"]),
        )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConversationKernelConflict(f"{field} is missing")
    return value


def _required_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConversationKernelConflict(f"{field} is invalid")
    return value


def _load_root_transcript_cut(
    connection: Connection,
    *,
    session_id: str,
    through_sequence: int,
) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        """
        SELECT e.id, e.turn_id, e.entry_sequence, e.entry_kind,
               e.content_digest, e.content_size, e.content_codec,
               COALESCE(e.inline_content, eb.body) AS entry_body,
               b.id AS block_id, b.block_ordinal, b.block_kind,
               b.tool_call_id, b.tool_name, b.tool_arguments,
               b.content_digest AS block_digest, b.content_size AS block_size,
               b.content_codec AS block_codec,
               COALESCE(b.inline_content, bb.body) AS block_body
        FROM pulsara_v3.transcript_entries AS e
        LEFT JOIN pulsara_v3.blobs AS eb ON eb.id = e.blob_id
        LEFT JOIN pulsara_v3.assistant_message_blocks AS b
          ON b.session_id = e.session_id AND b.assistant_entry_id = e.id
        LEFT JOIN pulsara_v3.blobs AS bb ON bb.id = b.blob_id
        WHERE e.session_id = %s AND e.conversation_scope_kind = 'ROOT'
          AND e.entry_sequence <= %s
        ORDER BY e.entry_sequence, b.block_ordinal NULLS FIRST
        """,
        (session_id, through_sequence),
    ).fetchall()
    if len(rows) > 8192:
        raise ConversationKernelConflict("compaction source row bound exceeded")
    entries: list[dict[str, object]] = []
    by_id: dict[str, dict[str, object]] = {}
    total = 0
    for row in rows:
        entry_id = str(row["id"])
        entry = by_id.get(entry_id)
        if entry is None:
            body = bytes(row["entry_body"] or b"")
            total += len(body)
            entry = {
                "entry_id": entry_id,
                "turn_id": str(row["turn_id"]),
                "entry_sequence": int(row["entry_sequence"]),
                "entry_kind": str(row["entry_kind"]),
                "content_digest": str(row["content_digest"]),
                "text": body.decode(str(row["content_codec"]), errors="replace"),
                "blocks": [],
            }
            entries.append(entry)
            by_id[entry_id] = entry
        if row["block_id"] is None:
            continue
        block: dict[str, object] = {
            "block_id": str(row["block_id"]),
            "block_ordinal": int(row["block_ordinal"]),
            "block_kind": str(row["block_kind"]),
        }
        if row["block_kind"] == "TOOL_CALL":
            block.update(
                {
                    "tool_call_id": str(row["tool_call_id"]),
                    "tool_name": str(row["tool_name"]),
                    "arguments": dict(row["tool_arguments"]),
                }
            )
        else:
            body = bytes(row["block_body"] or b"")
            total += len(body)
            block.update(
                {
                    "content_digest": str(row["block_digest"]),
                    "text": body.decode(str(row["block_codec"]), errors="replace"),
                }
            )
        blocks = entry["blocks"]
        assert isinstance(blocks, list)
        blocks.append(block)
    if total > 16 << 20:
        raise ConversationKernelConflict("compaction source byte bound exceeded")
    frozen: list[dict[str, object]] = []
    for entry in entries:
        copied = dict(entry)
        blocks = copied["blocks"]
        assert isinstance(blocks, list)
        copied["blocks"] = tuple(dict(block) for block in blocks)
        frozen.append(copied)
    return tuple(frozen)


__all__ = [
    "AcceptedCapabilityDecision",
    "AcceptedEntry",
    "AcceptedInteractionDecision",
    "AcceptedJobAttempt",
    "AcceptedJobSettlement",
    "AcceptedToolAttempt",
    "AssistantBlock",
    "AssistantDataBlock",
    "AssistantTextBlock",
    "AssistantToolCallBlock",
    "ConversationKernelConflict",
    "ConversationKernelRepository",
    "JobAttemptTerminalized",
    "MemoryVectorFactSource",
    "MemoryVectorSource",
    "NoToolResultSideBranch",
    "PreparedMemoryProposalSideBranch",
    "PreparedToolResultAcceptance",
    "StaleHostWriter",
    "StaleJobClaim",
    "ToolResultSideBranch",
    "ToolResultSideBranchKind",
    "build_prepared_tool_result_acceptance",
]
