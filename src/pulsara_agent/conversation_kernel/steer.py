"""Pure process-local contracts for bounded active-turn steer admission.

These values freeze one safe-point plan and its deterministic canonical
mutation candidates.  They are neither durable receipts nor a second queue
authority: PostgreSQL remains the sole truth for queue, entry, and event rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

from pulsara_agent.conversation_kernel.contracts import (
    BlobContent,
    CanonicalContent,
    CommittedEventDraft,
    CommittedEventSubject,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.prompt_content import FrozenCanonicalPrompt
from pulsara_agent.conversation_kernel.vocabulary import (
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_to_dict,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendPlanningInput,
    ProviderInputContinuityScope,
    SourceObservationPresence,
)
from pulsara_agent.model_input.contracts import (
    ApprovedPlanMaterializationFact,
    CanonicalInputOriginKind,
    ContextBindingBaseKind,
    ContextSourceKind,
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    FrozenContextBindingCompileFact,
    FrozenPlanWorkflowCompileFact,
    FrozenPlanHandoffCompileFact,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    PreparedProviderInputCut,
    ProviderWireSemanticInput,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)
from pulsara_agent.llm.request import FrozenProviderWireInputQuote
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanWorkflowStatus


MAXIMUM_STEER_ITEMS_PER_SAFE_POINT = 128
MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES = 16 << 20
# Bounds cumulative canonical-prefix materialization across longest-first
# trials.  This is a process-local planning-work quote, not durable capacity.
MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES = 256 << 20


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


@dataclass(frozen=True, slots=True)
class PreparedRootTurnIdentity:
    turn_id: str
    entry_id: str
    context_revision_id: str
    permission_snapshot_id: str


@dataclass(frozen=True, slots=True)
class PreparedRootProviderInputCandidate:
    """Exact future ROOT canonical cut frozen before its writer mutation.

    PostgreSQL still owns every durable fact.  This value carries the complete
    row-derived basis needed by the canonical reader and lets the writer prove
    that the input measured before admission is still the input it will create.
    """

    session_id: str
    workspace_id: str
    exact_turn_id: str
    exact_initial_entry_id: str
    exact_context_binding_revision_id: str
    unpublished_items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    unpublished_item_canonical_expanded_bytes: tuple[int, ...]
    permission_snapshot: FrozenRunPermissionSnapshot
    model_call_binding: ModelCallBinding
    expected_latest_entry_sequence: int
    context_base_kind: ContextBindingBaseKind
    context_snapshot_id: str | None
    source_through_sequence: int
    pending_plan_handoff_workflow_id: str | None
    pending_plan_handoff_interaction_id: str | None
    pending_plan_handoff_kind: str | None
    unpublished_plan_workflow_fact: FrozenPlanWorkflowCompileFact | None
    unpublished_plan_handoff_fact: FrozenPlanHandoffCompileFact | None
    unpublished_approved_plan_fact: ApprovedPlanMaterializationFact | None

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.exact_turn_id,
                self.exact_initial_entry_id,
                self.exact_context_binding_revision_id,
            )
        ):
            raise ValueError("prospective ROOT input identity is incomplete")
        if self.expected_latest_entry_sequence < 0:
            raise ValueError("prospective ROOT input head is invalid")
        if (
            not self.unpublished_items
            or any(
                not isinstance(item, FrozenProviderInputItem)
                for item in self.unpublished_items
            )
            or len(self.unpublished_items)
            != len(self.unpublished_item_canonical_expanded_bytes)
            or any(
                charge < 1
                for charge in self.unpublished_item_canonical_expanded_bytes
            )
        ):
            raise ValueError("prospective ROOT unpublished suffix is invalid")
        expected_sequences = tuple(
            range(
                self.expected_latest_entry_sequence + 1,
                self.expected_latest_entry_sequence + 1 + len(self.unpublished_items),
            )
        )
        if tuple(
            item.source_entry_sequence for item in self.unpublished_items
        ) != expected_sequences:
            raise ValueError("prospective ROOT unpublished suffix is not contiguous")
        item = self.unpublished_items[-1]
        if (
            item.source_entry_id != self.exact_initial_entry_id
            or item.source_entry_sequence != self.exact_initial_entry_sequence
            or item.source_turn_id != self.exact_turn_id
            or (item.item_kind, item.input_origin)
            not in {
                (
                    FrozenProviderInputItemKind.USER,
                    CanonicalInputOriginKind.HUMAN_MESSAGE,
                ),
                (
                    FrozenProviderInputItemKind.INTER_AGENT_MESSAGE,
                    CanonicalInputOriginKind.INTER_AGENT_MESSAGE,
                ),
                (
                    FrozenProviderInputItemKind.PLAN_CONTINUATION,
                    CanonicalInputOriginKind.PLAN_CONTINUATION,
                ),
                (FrozenProviderInputItemKind.TERMINAL_OBSERVATION, None),
            }
        ):
            raise ValueError("prospective ROOT initial item does not exact-join")
        if not isinstance(self.model_call_binding, ModelCallBinding):
            raise TypeError("prospective ROOT input lacks a model binding")
        snapshot = self.context_base_kind is ContextBindingBaseKind.SNAPSHOT
        if snapshot != (self.context_snapshot_id is not None):
            raise ValueError("prospective ROOT context base union is invalid")
        if snapshot:
            if self.source_through_sequence < 0:
                raise ValueError("prospective ROOT snapshot source cut is invalid")
        elif self.source_through_sequence != self.exact_initial_entry_sequence - 1:
            raise ValueError(
                "prospective ROOT full-history cut does not precede its initial entry"
            )
        handoff_values = (
            self.pending_plan_handoff_workflow_id,
            self.pending_plan_handoff_interaction_id,
            self.pending_plan_handoff_kind,
        )
        workflow_id, interaction_id, handoff_kind = handoff_values
        if (workflow_id is None) != (handoff_kind is None) or (
            interaction_id is not None and workflow_id is None
        ):
            raise ValueError("prospective ROOT Plan handoff union is invalid")
        workflow_fact = self.unpublished_plan_workflow_fact
        handoff_fact = self.unpublished_plan_handoff_fact
        approved_fact = self.unpublished_approved_plan_fact
        if workflow_fact is not None and (
            workflow_fact.session_id != self.session_id
            or workflow_fact.workspace_id != self.workspace_id
            or workflow_fact.turn_id != self.exact_turn_id
            or workflow_fact.permission_snapshot_id
            != self.permission_snapshot.snapshot_id
            or workflow_fact.permission_snapshot_fingerprint
            != self.permission_snapshot.snapshot_fingerprint
        ):
            raise ValueError("prospective ROOT Plan workflow fact does not exact-join")
        if handoff_fact is not None and (
            handoff_fact.session_id != self.session_id
            or handoff_fact.workspace_id != self.workspace_id
            or handoff_fact.target_turn_id != self.exact_turn_id
            or handoff_fact.carrier_entry_id != self.exact_initial_entry_id
            or handoff_fact.carrier_entry_sequence
            != self.exact_initial_entry_sequence
            or handoff_fact.workflow_id
            != (
                workflow_fact.workflow_id
                if workflow_fact is not None
                else self.pending_plan_handoff_workflow_id
            )
        ):
            raise ValueError("prospective ROOT Plan handoff fact does not exact-join")
        if approved_fact is not None and (
            handoff_fact is None
            or approved_fact.session_id != self.session_id
            or approved_fact.workspace_id != self.workspace_id
            or approved_fact.target_turn_id != self.exact_turn_id
            or approved_fact.workflow_id != handoff_fact.workflow_id
            or approved_fact.interaction_id != handoff_fact.interaction_id
        ):
            raise ValueError("prospective ROOT approved Plan fact does not exact-join")
        if handoff_fact is None:
            if workflow_fact is not None:
                raise ValueError(
                    "prospective ROOT unpublished Plan facts are incomplete"
                )
        elif handoff_fact.workflow_status is PlanWorkflowStatus.ACTIVE:
            if workflow_fact is None:
                raise ValueError(
                    "prospective ROOT active Plan facts are incomplete"
                )
        elif workflow_fact is not None:
            raise ValueError(
                "prospective ROOT terminal Plan handoff has an active workflow fact"
            )

    @property
    def exact_initial_entry_sequence(self) -> int:
        sequence = self.unpublished_items[-1].source_entry_sequence
        assert sequence is not None
        return sequence

    @property
    def unpublished_canonical_expanded_bytes(self) -> int:
        return sum(self.unpublished_item_canonical_expanded_bytes)


@dataclass(frozen=True, slots=True)
class PreparedRootProviderInputAdmission:
    """A fully compiled and materialized future ROOT input, before publication."""

    candidate: PreparedRootProviderInputCandidate = field(repr=False)
    canonical_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    semantic_input: ProviderWireSemanticInput = field(repr=False)
    wire_quote: FrozenProviderWireInputQuote

    def __post_init__(self) -> None:
        identity = self.canonical_read.compile_snapshot.canonical_input.identity
        if (
            identity.session_id != self.candidate.session_id
            or identity.turn_id != self.candidate.exact_turn_id
            or identity.initial_entry_id
            != self.candidate.exact_initial_entry_id
            or identity.context_binding_revision_id
            != self.candidate.exact_context_binding_revision_id
            or identity.provider_input_through_sequence
            != self.candidate.exact_initial_entry_sequence
            or self.semantic_input.canonical_input_identity != identity
            or self.wire_quote.semantic_estimated_input_tokens
            != self.semantic_input.final_estimate.total_input_tokens
            or self.wire_quote.final_wire_utf8_bytes > (64 << 20)
            or self.wire_quote.final_wire_estimated_input_tokens
            > self.wire_quote.effective_input_budget_tokens
        ):
            raise ValueError("prospective ROOT provider admission does not exact-join")


@dataclass(frozen=True, slots=True)
class PreparedActiveRootInputCandidate:
    """One unpublished canonical suffix bound to an exact active ROOT cut."""

    workspace_id: str
    expected_provider_input_cut: PreparedProviderInputCut
    next_model_call_index: int
    unpublished_items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    unpublished_item_canonical_expanded_bytes: tuple[int, ...]
    prospective_plan_workflow_fact: FrozenPlanWorkflowCompileFact | None = None

    def __post_init__(self) -> None:
        cut = self.expected_provider_input_cut
        if (
            not self.workspace_id
            or self.next_model_call_index < 1
            or not self.unpublished_items
            or len(self.unpublished_items)
            != len(self.unpublished_item_canonical_expanded_bytes)
            or any(
                not isinstance(item, FrozenProviderInputItem)
                for item in self.unpublished_items
            )
            or any(
                charge < 1
                for charge in self.unpublished_item_canonical_expanded_bytes
            )
        ):
            raise ValueError("prospective active ROOT suffix is invalid")
        expected_sequences = tuple(
            range(
                cut.provider_input_through_sequence + 1,
                cut.provider_input_through_sequence + 1
                + len(self.unpublished_items),
            )
        )
        if tuple(
            item.source_entry_sequence for item in self.unpublished_items
        ) != expected_sequences or any(
            item.source_turn_id != cut.turn_id for item in self.unpublished_items
        ):
            raise ValueError("prospective active ROOT suffix does not extend its cut")
        workflow = self.prospective_plan_workflow_fact
        if workflow is not None and (
            workflow.session_id != cut.session_id
            or workflow.workspace_id != self.workspace_id
            or workflow.turn_id != cut.turn_id
        ):
            raise ValueError("prospective active ROOT Plan fact does not exact-join")

    @property
    def resulting_provider_input_through_sequence(self) -> int:
        sequence = self.unpublished_items[-1].source_entry_sequence
        assert sequence is not None
        return sequence

    @property
    def unpublished_canonical_expanded_bytes(self) -> int:
        return sum(self.unpublished_item_canonical_expanded_bytes)


@dataclass(frozen=True, slots=True)
class PreparedActiveRootInputAdmission:
    """Compiled final-wire admission for one active ROOT suffix."""

    candidate: PreparedActiveRootInputCandidate = field(repr=False)
    canonical_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    semantic_input: ProviderWireSemanticInput = field(repr=False)
    wire_quote: FrozenProviderWireInputQuote

    def __post_init__(self) -> None:
        identity = self.canonical_read.compile_snapshot.canonical_input.identity
        cut = self.candidate.expected_provider_input_cut
        if (
            identity.session_id != cut.session_id
            or identity.turn_id != cut.turn_id
            or identity.context_binding_revision_id
            != cut.context_binding_revision_id
            or identity.provider_input_through_sequence
            != self.candidate.resulting_provider_input_through_sequence
            or self.semantic_input.canonical_input_identity != identity
            or self.wire_quote.semantic_estimated_input_tokens
            != self.semantic_input.final_estimate.total_input_tokens
            or self.wire_quote.final_wire_utf8_bytes > (64 << 20)
            or self.wire_quote.final_wire_estimated_input_tokens
            > self.wire_quote.effective_input_budget_tokens
        ):
            raise ValueError("prospective active ROOT admission does not exact-join")


def build_direct_root_turn_identity(
    session_id: str, command_id: str
) -> PreparedRootTurnIdentity:
    turn_id = _stable_id("turn", session_id, command_id)
    return PreparedRootTurnIdentity(
        turn_id=turn_id,
        entry_id=_stable_id("entry", turn_id, "user"),
        context_revision_id=_stable_id("context-revision", turn_id, "0"),
        permission_snapshot_id=_stable_id("permission-snapshot", turn_id),
    )


def build_queued_root_turn_identity(
    session_id: str, queue_item_id: str
) -> PreparedRootTurnIdentity:
    return PreparedRootTurnIdentity(
        turn_id=_stable_id("turn", session_id, queue_item_id),
        entry_id=_stable_id("entry", session_id, queue_item_id),
        context_revision_id=_stable_id(
            "context-revision", session_id, queue_item_id
        ),
        permission_snapshot_id=_stable_id(
            "permission-snapshot", session_id, queue_item_id
        ),
    )


def _content_manifest(content: CanonicalContent) -> dict[str, object]:
    value: dict[str, object] = {
        "digest": content.digest,
        "size": content.size,
        "media_type": content.media_type,
        "codec": content.codec,
        "storage": "BLOB" if isinstance(content, BlobContent) else "INLINE",
    }
    if isinstance(content, BlobContent):
        value["blob_id"] = content.blob_id
    return value


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


def _predecessor_value(
    planning: FrozenProviderInputAppendPlanningInput,
) -> dict[str, object]:
    predecessor = planning.predecessor_view
    return {
        "kind": planning.predecessor.value,
        "epoch_revision": 0 if predecessor is None else predecessor.epoch_revision,
        "frontier": (
            None
            if predecessor is None
            else context_fingerprint(
                "pulsara:provider-input-frontier:v1",
                {
                    "base": predecessor.canonical_frontier.context_base_semantic_identity,
                    "through": predecessor.canonical_frontier.through_sequence,
                    "items": predecessor.canonical_frontier.ordered_item_fingerprints,
                },
            )
        ),
        "prefix": (
            None if predecessor is None else predecessor.semantic_prefix_fingerprint
        ),
    }


@dataclass(frozen=True, slots=True)
class PreparedPromptIngressCommand:
    session_id: str
    command_id: str
    queue_item_id: str
    client_submission_id: str
    delivery_mode: PromptDeliveryMode
    target_turn_id: str | None
    permission_snapshot_id: str | None
    requested_permission_mode: PermissionMode | None
    canonical_prompt: FrozenCanonicalPrompt = field(repr=False)

    def __post_init__(self) -> None:
        if not all((self.session_id, self.command_id, self.queue_item_id)):
            raise ValueError("prompt ingress identity is incomplete")
        if not isinstance(self.canonical_prompt, FrozenCanonicalPrompt):
            raise TypeError("prompt ingress content must be canonical and frozen")
        new_turn = self.delivery_mode is PromptDeliveryMode.NEW_TURN
        new_turn_shape = (
            self.target_turn_id is None
            and self.permission_snapshot_id is not None
            and self.requested_permission_mode is not None
        )
        steer_shape = (
            self.target_turn_id is not None
            and self.permission_snapshot_id is None
            and self.requested_permission_mode is None
        )
        if (new_turn and not new_turn_shape) or (not new_turn and not steer_shape):
            raise ValueError("prompt ingress delivery union is invalid")


@dataclass(frozen=True, slots=True)
class PromptIngressAccepted:
    queue_sequence: int
    model_call_binding: ModelCallBinding | None
    reasoning_preference_reset: bool = False

    def __post_init__(self) -> None:
        if self.queue_sequence < 1:
            raise ValueError("prompt ingress sequence must be positive")


class PromptIngressConfirmationKind(StrEnum):
    FULL_COMPATIBLE = "FULL_COMPATIBLE"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class PromptIngressWriteRejection(StrEnum):
    COMMAND_CONFLICT = "COMMAND_CONFLICT"
    TARGET_STALE_OR_NON_STEERABLE = "TARGET_STALE_OR_NON_STEERABLE"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    INGRESS_PRECONDITION_CHANGED = "INGRESS_PRECONDITION_CHANGED"
    MODEL_CONFIGURATION_REQUIRED = "MODEL_CONFIGURATION_REQUIRED"
    MODEL_CONFIGURATION_UNAVAILABLE = "MODEL_CONFIGURATION_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class PromptIngressConfirmation:
    kind: PromptIngressConfirmationKind
    queue_sequence: int | None = None
    status: str | None = None
    rejection: PromptIngressWriteRejection | None = None

    def __post_init__(self) -> None:
        full = self.kind is PromptIngressConfirmationKind.FULL_COMPATIBLE
        conflict = self.kind is PromptIngressConfirmationKind.CONFLICT
        if full != (self.queue_sequence is not None and self.status is not None):
            raise ValueError("prompt ingress confirmation FULL union is invalid")
        if conflict != (self.rejection is not None):
            raise ValueError("prompt ingress confirmation conflict union is invalid")


@dataclass(frozen=True, slots=True)
class PreparedQueuedRootTurnAdmission:
    """Stable candidate for consuming one future-turn queue head.

    PostgreSQL remains the queue/turn authority.  This carrier only makes a
    single physical mutation confirmable after an unknown acknowledgement.
    """

    session_id: str
    workspace_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    client_submission_id: str
    body_storage: CanonicalContent = field(repr=False)
    canonical_prompt: FrozenCanonicalPrompt = field(repr=False)
    permission_snapshot: FrozenRunPermissionSnapshot
    model_call_binding: ModelCallBinding
    provider_input_candidate: PreparedRootProviderInputCandidate = field(repr=False)
    pending_plan_handoff_workflow_id: str | None
    pending_plan_handoff_interaction_id: str | None
    pending_plan_handoff_kind: str | None
    exact_turn_id: str
    exact_initial_entry_id: str
    exact_context_binding_revision_id: str
    occurred_at: datetime
    actor_id: str
    prompt_consumed_occurrence: CommittedEventDraft
    user_message_accepted_occurrence: CommittedEventDraft

    def __post_init__(self) -> None:
        if (
            not all(
                (
                    self.session_id,
                    self.workspace_id,
                    self.queue_item_id,
                    self.command_id,
                    self.client_submission_id,
                    self.exact_turn_id,
                    self.exact_initial_entry_id,
                    self.exact_context_binding_revision_id,
                    self.actor_id,
                )
            )
            or self.queue_sequence < 1
        ):
            raise ValueError("queued ROOT admission identity is incomplete")
        if not isinstance(self.model_call_binding, ModelCallBinding):
            raise ValueError("queued ROOT admission lacks a model binding")
        prospective = self.provider_input_candidate
        if (
            prospective.session_id != self.session_id
            or prospective.workspace_id != self.workspace_id
            or prospective.exact_turn_id != self.exact_turn_id
            or prospective.exact_initial_entry_id != self.exact_initial_entry_id
            or prospective.exact_context_binding_revision_id
            != self.exact_context_binding_revision_id
            or prospective.unpublished_items
            != (
                FrozenProviderInputItem(
                item_kind=FrozenProviderInputItemKind.USER,
                source_entry_id=self.exact_initial_entry_id,
                source_entry_sequence=prospective.exact_initial_entry_sequence,
                source_turn_id=self.exact_turn_id,
                content=self.canonical_prompt.content.parts,
                input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
                ),
            )
            or prospective.unpublished_item_canonical_expanded_bytes
            != (self.canonical_prompt.resource_quote.canonical_expanded_bytes,)
            or prospective.permission_snapshot != self.permission_snapshot
            or prospective.model_call_binding != self.model_call_binding
            or prospective.pending_plan_handoff_workflow_id
            != self.pending_plan_handoff_workflow_id
            or prospective.pending_plan_handoff_interaction_id
            != self.pending_plan_handoff_interaction_id
            or prospective.pending_plan_handoff_kind
            != self.pending_plan_handoff_kind
            or prospective.unpublished_plan_workflow_fact is not None
            or prospective.unpublished_plan_handoff_fact is not None
            or prospective.unpublished_approved_plan_fact is not None
        ):
            raise ValueError("queued ROOT provider candidate does not exact-join")
        handoff_values = (
            self.pending_plan_handoff_workflow_id,
            self.pending_plan_handoff_interaction_id,
            self.pending_plan_handoff_kind,
        )
        workflow_id, interaction_id, handoff_kind = handoff_values
        if (workflow_id is None) != (handoff_kind is None) or (
            interaction_id is not None and workflow_id is None
        ):
            raise ValueError("queued ROOT admission Plan handoff union is invalid")


def build_queued_root_turn_admission(
    *,
    session_id: str,
    workspace_id: str,
    queue_item_id: str,
    queue_sequence: int,
    command_id: str,
    client_submission_id: str,
    content: CanonicalContent,
    canonical_prompt: FrozenCanonicalPrompt,
    permission_snapshot: FrozenRunPermissionSnapshot,
    model_call_binding: ModelCallBinding,
    provider_input_candidate: PreparedRootProviderInputCandidate,
    pending_plan_handoff_workflow_id: str | None,
    pending_plan_handoff_interaction_id: str | None,
    pending_plan_handoff_kind: str | None,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedQueuedRootTurnAdmission:
    identity = build_queued_root_turn_identity(session_id, queue_item_id)
    turn_id = identity.turn_id
    entry_id = identity.entry_id
    revision_id = identity.context_revision_id
    consumed = CommittedEventDraft(
        event_id=_stable_id(
            "event", queue_item_id, CommittedEventType.PROMPT_CONSUMED.value
        ),
        event_type=CommittedEventType.PROMPT_CONSUMED,
        subject=CommittedEventSubject(SubjectSlot.QUEUE_ITEM, queue_item_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"entry_id": entry_id},
    )
    accepted = CommittedEventDraft(
        event_id=_stable_id(
            "event", entry_id, CommittedEventType.USER_MESSAGE_ACCEPTED.value
        ),
        event_type=CommittedEventType.USER_MESSAGE_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
        actor_kind="human",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"source": "PROMPT_QUEUE"},
    )
    return PreparedQueuedRootTurnAdmission(
        session_id=session_id,
        workspace_id=workspace_id,
        queue_item_id=queue_item_id,
        queue_sequence=queue_sequence,
        command_id=command_id,
        client_submission_id=client_submission_id,
        body_storage=content,
        canonical_prompt=canonical_prompt,
        permission_snapshot=permission_snapshot,
        model_call_binding=model_call_binding,
        provider_input_candidate=provider_input_candidate,
        pending_plan_handoff_workflow_id=pending_plan_handoff_workflow_id,
        pending_plan_handoff_interaction_id=pending_plan_handoff_interaction_id,
        pending_plan_handoff_kind=pending_plan_handoff_kind,
        exact_turn_id=turn_id,
        exact_initial_entry_id=entry_id,
        exact_context_binding_revision_id=revision_id,
        occurred_at=occurred_at,
        actor_id=actor_id,
        prompt_consumed_occurrence=consumed,
        user_message_accepted_occurrence=accepted,
    )


class QueuedRootTurnAdmissionConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class QueuedRootTurnAdmissionAccepted:
    entry_id: str
    turn_id: str
    entry_sequence: int
    event_sequence: int


@dataclass(frozen=True, slots=True)
class QueuedRootTurnAdmissionConfirmation:
    kind: QueuedRootTurnAdmissionConfirmationKind
    accepted: QueuedRootTurnAdmissionAccepted | None = None

    def __post_init__(self) -> None:
        if (self.kind is QueuedRootTurnAdmissionConfirmationKind.FULL) != (
            self.accepted is not None
        ):
            raise ValueError("queued ROOT confirmation union is invalid")


def build_prompt_ingress_command(
    *,
    session_id: str,
    command_id: str,
    queue_item_id: str,
    client_submission_id: str,
    delivery_mode: PromptDeliveryMode,
    target_turn_id: str | None,
    permission_snapshot_id: str | None,
    requested_permission_mode: PermissionMode | None,
    canonical_prompt: FrozenCanonicalPrompt,
) -> PreparedPromptIngressCommand:
    return PreparedPromptIngressCommand(
        session_id=session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=delivery_mode,
        target_turn_id=target_turn_id,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=requested_permission_mode,
        canonical_prompt=canonical_prompt,
    )


def prompt_ingress_semantic_digest(
    candidate: PreparedPromptIngressCommand,
    model_call_binding: ModelCallBinding | None,
) -> str:
    if (candidate.delivery_mode is PromptDeliveryMode.NEW_TURN) != (
        model_call_binding is not None
    ):
        raise ValueError("prompt ingress model binding union is invalid")
    return context_fingerprint(
        "pulsara:queue-prompt-command:v3",
        {
            "queue_item_id": candidate.queue_item_id,
            "client_submission_id": candidate.client_submission_id,
            "delivery_mode": candidate.delivery_mode.value,
            "target_turn_id": candidate.target_turn_id,
            "content_digest": "sha256:"
            + sha256(candidate.canonical_prompt.body).hexdigest(),
            "content_size": len(candidate.canonical_prompt.body),
            "content_media_type": "application/vnd.pulsara.prompt+json",
            "content_codec": "utf-8",
            "permission_snapshot_id": candidate.permission_snapshot_id,
            "requested_permission_mode": (
                None
                if candidate.requested_permission_mode is None
                else candidate.requested_permission_mode.value
            ),
            "model_call_binding": model_call_binding_to_dict(model_call_binding),
        },
    )


@dataclass(frozen=True, slots=True)
class PendingPromptSteerFact:
    session_id: str
    workspace_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    body_storage: CanonicalContent = field(repr=False)
    canonical_expanded_bytes: int

    def __post_init__(self) -> None:
        if (
            self.queue_sequence < 1
            or self.body_storage.size < 1
            or self.canonical_expanded_bytes < self.body_storage.size
            or self.canonical_expanded_bytes
            > MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES
        ):
            raise ValueError("pending steer fact bounds are invalid")


def build_pending_prompt_steer_fact(
    *,
    session_id: str,
    workspace_id: str,
    queue_item_id: str,
    queue_sequence: int,
    command_id: str,
    exact_target_turn_id: str,
    content: CanonicalContent,
    canonical_expanded_bytes: int,
) -> PendingPromptSteerFact:
    return PendingPromptSteerFact(
        session_id=session_id,
        workspace_id=workspace_id,
        queue_item_id=queue_item_id,
        queue_sequence=queue_sequence,
        command_id=command_id,
        exact_target_turn_id=exact_target_turn_id,
        body_storage=content,
        canonical_expanded_bytes=canonical_expanded_bytes,
    )


@dataclass(frozen=True, slots=True)
class PreparedSteerCanonicalBaseFence:
    """Immutable canonical/control facts revalidated by the consume transaction."""

    session_id: str
    exact_target_turn_id: str
    provider_input_through_sequence: int
    context_binding_fact: FrozenContextBindingCompileFact
    run_permission_snapshot: FrozenRunPermissionSnapshot
    plan_workflow_fact: FrozenPlanWorkflowCompileFact | None
    canonical_read_cut_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.exact_target_turn_id
            or self.provider_input_through_sequence < 1
            or not self.canonical_read_cut_fingerprint.startswith("sha256:")
        ):
            raise ValueError("steer canonical base fence is incomplete")
        workflow = self.plan_workflow_fact
        if workflow is not None and (
            workflow.session_id != self.session_id
            or workflow.turn_id != self.exact_target_turn_id
            or workflow.permission_snapshot_id
            != self.run_permission_snapshot.snapshot_id
            or workflow.permission_snapshot_fingerprint
            != self.run_permission_snapshot.snapshot_fingerprint
        ):
            raise ValueError("steer canonical base Plan fact does not exact-join")


def build_steer_canonical_base_fence(
    snapshot: FrozenCanonicalCompileSnapshot,
) -> PreparedSteerCanonicalBaseFence:
    identity = snapshot.canonical_input.identity
    return PreparedSteerCanonicalBaseFence(
        session_id=identity.session_id,
        exact_target_turn_id=identity.turn_id,
        provider_input_through_sequence=identity.provider_input_through_sequence,
        context_binding_fact=snapshot.context_binding_fact,
        run_permission_snapshot=snapshot.run_permission_snapshot,
        plan_workflow_fact=snapshot.plan_workflow_fact,
        canonical_read_cut_fingerprint=snapshot.canonical_read_cut_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class PreparedSteerConsumptionCandidate:
    session_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    body_storage: CanonicalContent = field(repr=False)
    canonical_prompt: FrozenCanonicalPrompt = field(repr=False)
    new_entry_id: str
    expected_entry_sequence: int
    occurred_at: datetime
    actor_id: str
    predecessor: FrozenProviderInputAppendPlanningInput
    canonical_base_fence: PreparedSteerCanonicalBaseFence
    prompt_consumed_occurrence: CommittedEventDraft
    user_steer_accepted_occurrence: CommittedEventDraft

    def __post_init__(self) -> None:
        if self.expected_entry_sequence < 1:
            raise ValueError("steer candidate entry sequence is invalid")
        if len(self.canonical_prompt.body) != self.body_storage.size:
            raise ValueError("steer candidate body size differs from content")
        if (
            "sha256:" + sha256(self.canonical_prompt.body).hexdigest()
            != self.body_storage.digest
        ):
            raise ValueError("steer candidate body digest differs from content")
        if (
            self.canonical_base_fence.session_id != self.session_id
            or self.canonical_base_fence.exact_target_turn_id
            != self.exact_target_turn_id
            or self.expected_entry_sequence
            <= self.canonical_base_fence.provider_input_through_sequence
        ):
            raise ValueError("steer candidate canonical base fence does not exact-join")


def build_steer_consumption_candidate(
    *,
    fact: PendingPromptSteerFact,
    canonical_prompt: FrozenCanonicalPrompt,
    expected_entry_sequence: int,
    predecessor: FrozenProviderInputAppendPlanningInput,
    canonical_base_fence: PreparedSteerCanonicalBaseFence,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedSteerConsumptionCandidate:
    entry_id = _stable_id("steer-entry", fact.session_id, fact.queue_item_id)
    prompt_event = CommittedEventDraft(
        event_id=_stable_id(
            "event", fact.queue_item_id, CommittedEventType.PROMPT_CONSUMED.value
        ),
        event_type=CommittedEventType.PROMPT_CONSUMED,
        subject=CommittedEventSubject(SubjectSlot.QUEUE_ITEM, fact.queue_item_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"entry_id": entry_id},
    )
    steer_event = CommittedEventDraft(
        event_id=_stable_id(
            "event", entry_id, CommittedEventType.USER_STEER_ACCEPTED.value
        ),
        event_type=CommittedEventType.USER_STEER_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
        actor_kind="human",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"source": "PROMPT_QUEUE"},
    )
    return PreparedSteerConsumptionCandidate(
        session_id=fact.session_id,
        queue_item_id=fact.queue_item_id,
        queue_sequence=fact.queue_sequence,
        command_id=fact.command_id,
        exact_target_turn_id=fact.exact_target_turn_id,
        body_storage=fact.body_storage,
        canonical_prompt=canonical_prompt,
        new_entry_id=entry_id,
        expected_entry_sequence=expected_entry_sequence,
        occurred_at=occurred_at,
        actor_id=actor_id,
        predecessor=predecessor,
        canonical_base_fence=canonical_base_fence,
        prompt_consumed_occurrence=prompt_event,
        user_steer_accepted_occurrence=steer_event,
    )


class SteerConsumptionConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class AcceptedSteerDispatchEntry:
    queue_item_id: str
    queue_sequence: int
    entry_id: str
    entry_sequence: int
    target_turn_id: str
    content_digest: str
    content_size: int
    prompt_consumed_event_id: str
    prompt_consumed_event_sequence: int
    user_steer_event_id: str
    user_steer_event_sequence: int


@dataclass(frozen=True, slots=True)
class SteerConsumptionConfirmation:
    kind: SteerConsumptionConfirmationKind
    accepted: AcceptedSteerDispatchEntry | None = None

    def __post_init__(self) -> None:
        if (self.kind is SteerConsumptionConfirmationKind.FULL) != (
            self.accepted is not None
        ):
            raise ValueError("steer consumption confirmation union is invalid")


@dataclass(frozen=True, slots=True)
class AcceptedSteerDispatchBatch:
    session_id: str
    target_turn_id: str
    entries: tuple[AcceptedSteerDispatchEntry, ...]
    canonical_expanded_bytes: int
    resulting_epoch_logical_bytes: int

    def __post_init__(self) -> None:
        if not self.entries or len(self.entries) > MAXIMUM_STEER_ITEMS_PER_SAFE_POINT:
            raise ValueError("accepted steer batch item count is invalid")
        queue_sequences = tuple(item.queue_sequence for item in self.entries)
        entry_sequences = tuple(item.entry_sequence for item in self.entries)
        if queue_sequences != tuple(sorted(set(queue_sequences))):
            raise ValueError("accepted steer batch is not lane FIFO")
        if entry_sequences != tuple(
            range(entry_sequences[0], entry_sequences[0] + len(entry_sequences))
        ):
            raise ValueError("accepted steer batch entry sequence is not contiguous")
        event_sequences = tuple(
            sequence
            for item in self.entries
            for sequence in (
                item.prompt_consumed_event_sequence,
                item.user_steer_event_sequence,
            )
        )
        if any(
            item.user_steer_event_sequence != item.prompt_consumed_event_sequence + 1
            for item in self.entries
        ) or event_sequences != tuple(sorted(set(event_sequences))):
            raise ValueError("accepted steer batch event order is invalid")
        if any(
            item.target_turn_id != self.target_turn_id or item.content_size < 1
            for item in self.entries
        ):
            raise ValueError("accepted steer batch target/content identity is invalid")
        if sum(item.content_size for item in self.entries) > self.canonical_expanded_bytes:
            raise ValueError("accepted steer batch body total exceeds expanded total")
        if not (
            0
            < self.canonical_expanded_bytes
            <= MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES
        ):
            raise ValueError("accepted steer batch expanded bound is invalid")
        if not 0 < self.resulting_epoch_logical_bytes <= (64 << 20):
            raise ValueError("accepted steer batch epoch bound is invalid")


@dataclass(frozen=True, slots=True)
class SteerSuffixAdmissionQuote:
    selected_item_count: int
    selected_canonical_expanded_bytes: int
    prospective_snapshot_hydrated_bytes: int
    resulting_epoch_logical_bytes: int
    resulting_target_estimate: TokenEstimate
    effective_target_budget: int
    estimator_fingerprint: str
    predecessor_prefix_fingerprint: str | None
    memory_recall_reservation: "MemorySourceInvalidationReservation | None"
    memory_response_preference_reservation: "MemorySourceInvalidationReservation | None"

    def __post_init__(self) -> None:
        if not 1 <= self.selected_item_count <= MAXIMUM_STEER_ITEMS_PER_SAFE_POINT:
            raise ValueError("steer quote selects no items")
        if (
            not 0
            < self.selected_canonical_expanded_bytes
            <= MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES
        ):
            raise ValueError("steer quote body bound is invalid")
        if not 0 < self.prospective_snapshot_hydrated_bytes <= (16 << 20):
            raise ValueError("steer quote snapshot bound is invalid")
        if not 0 < self.resulting_epoch_logical_bytes <= (64 << 20):
            raise ValueError("steer quote epoch bound is invalid")
        reservations = tuple(
            item
            for item in (
                self.memory_recall_reservation,
                self.memory_response_preference_reservation,
            )
            if item is not None
        )
        if (
            self.effective_target_budget < 1
            or self.resulting_target_estimate.total_input_tokens
            + sum(item.invalidation_input_token_ceiling for item in reservations)
            > self.effective_target_budget
        ):
            raise ValueError("steer quote target budget is invalid")
        if self.resulting_epoch_logical_bytes + sum(
            item.invalidation_epoch_bytes_ceiling for item in reservations
        ) > (64 << 20):
            raise ValueError("steer quote epoch reservation is invalid")
        if any(
            item.estimator_fingerprint != self.estimator_fingerprint
            for item in reservations
        ):
            raise ValueError("steer quote memory estimator differs")
        if (
            self.memory_recall_reservation is not None
            and self.memory_recall_reservation.source_kind
            is not ContextSourceKind.MEMORY_RECALL
        ) or (
            self.memory_response_preference_reservation is not None
            and self.memory_response_preference_reservation.source_kind
            is not ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD
        ):
            raise ValueError("steer quote memory reservation kind is invalid")
        if not self.estimator_fingerprint.startswith("sha256:"):
            raise ValueError("steer quote estimator fingerprint is invalid")
        if self.predecessor_prefix_fingerprint is not None and not (
            self.predecessor_prefix_fingerprint.startswith("sha256:")
        ):
            raise ValueError("steer quote predecessor fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class MemorySourceInvalidationReservation:
    """Exact process-local quote for one possible stale-memory invalidation."""

    source_kind: ContextSourceKind
    prior_presence: SourceObservationPresence
    prior_semantic_fingerprint: str
    desired_presence: SourceObservationPresence
    desired_semantic_fingerprint: str
    source_contract_fingerprint: str
    invalidation_provider_item_ceiling: int
    invalidation_encoded_utf8_bytes_ceiling: int
    invalidation_input_token_ceiling: int
    invalidation_epoch_bytes_ceiling: int
    full_encoded_utf8_bytes: int
    full_input_token_cost: int
    estimator_fingerprint: str

    def __post_init__(self) -> None:
        if self.source_kind not in {
            ContextSourceKind.MEMORY_RECALL,
            ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
        }:
            raise ValueError("memory invalidation reservation has foreign source")
        if self.prior_presence not in {
            SourceObservationPresence.VALUE,
            SourceObservationPresence.UNAVAILABLE,
        }:
            raise ValueError("memory invalidation reservation has no stale head")
        if self.desired_presence not in {
            SourceObservationPresence.VALUE,
            SourceObservationPresence.CLEARED,
            SourceObservationPresence.UNAVAILABLE,
        }:
            raise ValueError("memory invalidation desired presence is invalid")
        for value in (
            self.prior_semantic_fingerprint,
            self.desired_semantic_fingerprint,
            self.source_contract_fingerprint,
            self.estimator_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("memory invalidation fingerprint is invalid")
        if (
            self.invalidation_provider_item_ceiling != 1
            or min(
                self.invalidation_encoded_utf8_bytes_ceiling,
                self.invalidation_input_token_ceiling,
                self.invalidation_epoch_bytes_ceiling,
                self.full_encoded_utf8_bytes,
                self.full_input_token_cost,
            )
            < 0
        ):
            raise ValueError("memory invalidation quote is outside its bound")


def build_memory_source_invalidation_reservation(
    *,
    source_kind: ContextSourceKind,
    prior_presence: SourceObservationPresence,
    prior_semantic_fingerprint: str,
    desired_presence: SourceObservationPresence,
    desired_semantic_fingerprint: str,
    source_contract_fingerprint: str,
    invalidation_encoded_utf8_bytes_ceiling: int,
    invalidation_input_token_ceiling: int,
    invalidation_epoch_bytes_ceiling: int,
    full_encoded_utf8_bytes: int,
    full_input_token_cost: int,
    estimator_fingerprint: str,
) -> MemorySourceInvalidationReservation:
    return MemorySourceInvalidationReservation(
        source_kind=source_kind,
        prior_presence=prior_presence,
        prior_semantic_fingerprint=prior_semantic_fingerprint,
        desired_presence=desired_presence,
        desired_semantic_fingerprint=desired_semantic_fingerprint,
        source_contract_fingerprint=source_contract_fingerprint,
        invalidation_provider_item_ceiling=1,
        invalidation_encoded_utf8_bytes_ceiling=invalidation_encoded_utf8_bytes_ceiling,
        invalidation_input_token_ceiling=invalidation_input_token_ceiling,
        invalidation_epoch_bytes_ceiling=invalidation_epoch_bytes_ceiling,
        full_encoded_utf8_bytes=full_encoded_utf8_bytes,
        full_input_token_cost=full_input_token_cost,
        estimator_fingerprint=estimator_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class PreparedSteerSuffixAdmissionPlan:
    scope: ProviderInputContinuityScope
    predecessor: FrozenProviderInputAppendPlanningInput
    base_cut_fingerprint: str
    base_canonical_frontier_fingerprint: str
    base_compile_snapshot_fingerprint: str
    target_binding_fingerprint: str
    tool_surface_fingerprint: str
    source_facts_fingerprint: str
    ordered_pending_queue_facts: tuple[PendingPromptSteerFact, ...]
    selected_consumption_candidates: tuple[PreparedSteerConsumptionCandidate, ...]
    quote: SteerSuffixAdmissionQuote
    prospective_compiled_input: FrozenCompiledModelInput = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.selected_consumption_candidates) != self.quote.selected_item_count:
            raise ValueError("steer plan candidate count differs from quote")
        selected = self.selected_consumption_candidates
        sequences = tuple(item.queue_sequence for item in selected)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("steer plan candidates are not FIFO")
        pending_sequences = tuple(
            item.queue_sequence for item in self.ordered_pending_queue_facts
        )
        if pending_sequences != tuple(sorted(set(pending_sequences))):
            raise ValueError("steer plan pending facts are not FIFO")


@dataclass(frozen=True, slots=True)
class PreparedSteerResourceRejection:
    session_id: str
    workspace_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    content: CanonicalContent = field(repr=False)
    reason: str
    occurred_at: datetime
    actor_id: str
    prompt_rejected_occurrence: CommittedEventDraft
    turn_interrupted_occurrence: CommittedEventDraft

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or self.queue_sequence < 1
            or self.reason != "STEER_INPUT_RESOURCE_EXHAUSTED"
        ):
            raise ValueError("steer resource rejection is invalid")
        if (
            self.prompt_rejected_occurrence.event_type
            is not CommittedEventType.PROMPT_REJECTED
            or self.turn_interrupted_occurrence.event_type
            is not CommittedEventType.TURN_INTERRUPTED
        ):
            raise ValueError("steer resource rejection occurrences are invalid")


def build_steer_resource_rejection(
    *,
    fact: PendingPromptSteerFact,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedSteerResourceRejection:
    reason = "STEER_INPUT_RESOURCE_EXHAUSTED"
    prompt_event = CommittedEventDraft(
        event_id=_stable_id("event", fact.queue_item_id, f"PromptRejected:{reason}"),
        event_type=CommittedEventType.PROMPT_REJECTED,
        subject=CommittedEventSubject(SubjectSlot.QUEUE_ITEM, fact.queue_item_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"reason": reason},
    )
    turn_reason = "PROVIDER_INPUT_RESOURCE_EXHAUSTED"
    turn_event = CommittedEventDraft(
        event_id=_stable_id(
            "event",
            fact.exact_target_turn_id,
            fact.queue_item_id,
            f"TurnInterrupted:{turn_reason}",
        ),
        event_type=CommittedEventType.TURN_INTERRUPTED,
        subject=CommittedEventSubject(SubjectSlot.TURN, fact.exact_target_turn_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"reason": turn_reason},
    )
    return PreparedSteerResourceRejection(
        session_id=fact.session_id,
        workspace_id=fact.workspace_id,
        queue_item_id=fact.queue_item_id,
        queue_sequence=fact.queue_sequence,
        command_id=fact.command_id,
        exact_target_turn_id=fact.exact_target_turn_id,
        content=fact.body_storage,
        reason=reason,
        occurred_at=occurred_at,
        actor_id=actor_id,
        prompt_rejected_occurrence=prompt_event,
        turn_interrupted_occurrence=turn_event,
    )


class SteerResourceRejectionConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class SteerResourceRejectionConfirmation:
    kind: SteerResourceRejectionConfirmationKind


@dataclass(frozen=True, slots=True)
class PreparedSteerPlanConflictInterruption:
    session_id: str
    exact_target_turn_id: str
    source_plan_fingerprint: str
    occurred_at: datetime
    actor_id: str
    turn_interrupted_occurrence: CommittedEventDraft

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.exact_target_turn_id,
                self.source_plan_fingerprint,
                self.actor_id,
            )
        ):
            raise ValueError("steer plan-conflict interruption is incomplete")
        event = self.turn_interrupted_occurrence
        if (
            event.event_type is not CommittedEventType.TURN_INTERRUPTED
            or event.subject.slot is not SubjectSlot.TURN
            or event.subject.subject_id != self.exact_target_turn_id
            or event.payload != {"reason": "PROVIDER_INPUT_PLAN_CONFLICT"}
        ):
            raise ValueError("steer plan-conflict interruption event is invalid")


class SteerPlanConflictConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    HISTORICAL_TERMINAL = "HISTORICAL_TERMINAL"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class SteerPlanConflictConfirmation:
    kind: SteerPlanConflictConfirmationKind


def build_steer_plan_conflict_interruption(
    *,
    session_id: str,
    exact_target_turn_id: str,
    source_plan_fingerprint: str,
    occurred_at: datetime,
    actor_id: str,
) -> PreparedSteerPlanConflictInterruption:
    reason = "PROVIDER_INPUT_PLAN_CONFLICT"
    event = CommittedEventDraft(
        event_id=_stable_id(
            "event",
            exact_target_turn_id,
            source_plan_fingerprint,
            f"TurnInterrupted:{reason}",
        ),
        event_type=CommittedEventType.TURN_INTERRUPTED,
        subject=CommittedEventSubject(SubjectSlot.TURN, exact_target_turn_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"reason": reason},
    )
    return PreparedSteerPlanConflictInterruption(
        session_id=session_id,
        exact_target_turn_id=exact_target_turn_id,
        source_plan_fingerprint=source_plan_fingerprint,
        occurred_at=occurred_at,
        actor_id=actor_id,
        turn_interrupted_occurrence=event,
    )


def _estimate_value(estimate: TokenEstimate) -> dict[str, object]:
    return {
        "system": estimate.system_tokens,
        "messages": estimate.message_tokens,
        "message_by_index": estimate.message_tokens_by_index,
        "tools": estimate.tool_tokens,
        "envelope": estimate.envelope_tokens,
        "visual_image": estimate.visual_image_tokens,
        "total": estimate.total_input_tokens,
    }


def build_steer_suffix_quote(
    *,
    candidates: tuple[PreparedSteerConsumptionCandidate, ...],
    prospective_snapshot_hydrated_bytes: int,
    resulting_epoch_logical_bytes: int,
    resulting_target_estimate: TokenEstimate,
    effective_target_budget: int,
    estimator_fingerprint: str,
    predecessor_prefix_fingerprint: str | None,
    memory_recall_reservation: MemorySourceInvalidationReservation | None = None,
    memory_response_preference_reservation: (
        MemorySourceInvalidationReservation | None
    ) = None,
) -> SteerSuffixAdmissionQuote:
    canonical_bytes = sum(
        item.canonical_prompt.resource_quote.canonical_expanded_bytes
        for item in candidates
    )
    return SteerSuffixAdmissionQuote(
        selected_item_count=len(candidates),
        selected_canonical_expanded_bytes=canonical_bytes,
        prospective_snapshot_hydrated_bytes=prospective_snapshot_hydrated_bytes,
        resulting_epoch_logical_bytes=resulting_epoch_logical_bytes,
        resulting_target_estimate=resulting_target_estimate,
        effective_target_budget=effective_target_budget,
        estimator_fingerprint=estimator_fingerprint,
        predecessor_prefix_fingerprint=predecessor_prefix_fingerprint,
        memory_recall_reservation=memory_recall_reservation,
        memory_response_preference_reservation=(
            memory_response_preference_reservation
        ),
    )


def _legacy_pending_steer_identity(fact: PendingPromptSteerFact) -> str:
    return context_fingerprint(
        "pulsara:pending-prompt-steer-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "queue_item_id": fact.queue_item_id,
            "queue_sequence": fact.queue_sequence,
            "command_id": fact.command_id,
            "target_turn_id": fact.exact_target_turn_id,
            "content": _content_manifest(fact.body_storage),
        },
    )


def _legacy_steer_base_fence_identity(
    fence: PreparedSteerCanonicalBaseFence,
) -> str:
    return context_fingerprint(
        "pulsara:prepared-steer-canonical-base-fence:v1",
        {
            "session_id": fence.session_id,
            "target_turn_id": fence.exact_target_turn_id,
            "through_sequence": fence.provider_input_through_sequence,
            "context_binding": fence.context_binding_fact.fact_fingerprint,
            "run_permission": fence.run_permission_snapshot.snapshot_fingerprint,
            "plan_workflow": (
                None
                if fence.plan_workflow_fact is None
                else fence.plan_workflow_fact.fact_fingerprint
            ),
            "canonical_read_cut": fence.canonical_read_cut_fingerprint,
        },
    )


def steer_consumption_candidate_identity_fingerprint(
    candidate: PreparedSteerConsumptionCandidate,
) -> str:
    """Reproduce the pre-hard-cut stable identity at context/event ID boundaries."""

    return context_fingerprint(
        "pulsara:prepared-steer-consumption:v1",
        {
            "session_id": candidate.session_id,
            "queue_item_id": candidate.queue_item_id,
            "queue_sequence": candidate.queue_sequence,
            "command_id": candidate.command_id,
            "target_turn_id": candidate.exact_target_turn_id,
            "content": _content_manifest(candidate.body_storage),
            "entry_id": candidate.new_entry_id,
            "entry_sequence": candidate.expected_entry_sequence,
            "predecessor": _predecessor_value(candidate.predecessor),
            "canonical_base_fence": _legacy_steer_base_fence_identity(
                candidate.canonical_base_fence
            ),
            "prompt_consumed": _event_manifest(candidate.prompt_consumed_occurrence),
            "user_steer": _event_manifest(candidate.user_steer_accepted_occurrence),
        },
    )


def _legacy_memory_reservation_identity(
    reservation: MemorySourceInvalidationReservation,
) -> str:
    return context_fingerprint(
        "pulsara:memory-source-invalidation-reservation:v1",
        {
            "source": reservation.source_kind.value,
            "prior": (
                reservation.prior_presence.value,
                reservation.prior_semantic_fingerprint,
            ),
            "desired": (
                reservation.desired_presence.value,
                reservation.desired_semantic_fingerprint,
            ),
            "contract": reservation.source_contract_fingerprint,
            "invalidation": (
                reservation.invalidation_provider_item_ceiling,
                reservation.invalidation_encoded_utf8_bytes_ceiling,
                reservation.invalidation_input_token_ceiling,
                reservation.invalidation_epoch_bytes_ceiling,
            ),
            "full": (
                reservation.full_encoded_utf8_bytes,
                reservation.full_input_token_cost,
            ),
            "estimator": reservation.estimator_fingerprint,
        },
    )


def _legacy_steer_quote_identity(plan: PreparedSteerSuffixAdmissionPlan) -> str:
    quote = plan.quote
    return context_fingerprint(
        "pulsara:steer-suffix-admission-quote:v1",
        {
            "candidates": tuple(
                steer_consumption_candidate_identity_fingerprint(item)
                for item in plan.selected_consumption_candidates
            ),
            "selected_items": quote.selected_item_count,
            "selected_canonical_bytes": quote.selected_canonical_expanded_bytes,
            "snapshot_bytes": quote.prospective_snapshot_hydrated_bytes,
            "epoch_bytes": quote.resulting_epoch_logical_bytes,
            "estimate": _estimate_value(quote.resulting_target_estimate),
            "effective_budget": quote.effective_target_budget,
            "estimator": quote.estimator_fingerprint,
            "predecessor_prefix": quote.predecessor_prefix_fingerprint,
            "memory_recall_reservation": (
                None
                if quote.memory_recall_reservation is None
                else _legacy_memory_reservation_identity(
                    quote.memory_recall_reservation
                )
            ),
            "memory_response_preference_reservation": (
                None
                if quote.memory_response_preference_reservation is None
                else _legacy_memory_reservation_identity(
                    quote.memory_response_preference_reservation
                )
            ),
        },
    )


def prepared_steer_suffix_plan_identity_fingerprint(
    plan: PreparedSteerSuffixAdmissionPlan,
) -> str:
    """Reproduce the pre-hard-cut stable plan identity only at ID boundaries."""

    return context_fingerprint(
        "pulsara:prepared-steer-suffix-admission-plan:v1",
        {
            "scope": (
                plan.scope.session_id,
                plan.scope.scope_kind.value,
                plan.scope.scope_subagent_task_id,
            ),
            "predecessor": _predecessor_value(plan.predecessor),
            "base_cut": plan.base_cut_fingerprint,
            "base_frontier": plan.base_canonical_frontier_fingerprint,
            "base_compile": plan.base_compile_snapshot_fingerprint,
            "target": plan.target_binding_fingerprint,
            "surface": plan.tool_surface_fingerprint,
            "sources": plan.source_facts_fingerprint,
            "pending": tuple(
                _legacy_pending_steer_identity(item)
                for item in plan.ordered_pending_queue_facts
            ),
            "selected": tuple(
                steer_consumption_candidate_identity_fingerprint(item)
                for item in plan.selected_consumption_candidates
            ),
            "quote": _legacy_steer_quote_identity(plan),
            "compiled": plan.prospective_compiled_input.compiled_semantic_fingerprint,
        },
    )


def build_prepared_steer_suffix_plan(
    *,
    scope: ProviderInputContinuityScope,
    predecessor: FrozenProviderInputAppendPlanningInput,
    base_cut_fingerprint: str,
    base_canonical_frontier_fingerprint: str,
    base_compile_snapshot_fingerprint: str,
    target_binding_fingerprint: str,
    tool_surface_fingerprint: str,
    source_facts_fingerprint: str,
    ordered_pending_queue_facts: tuple[PendingPromptSteerFact, ...],
    selected_consumption_candidates: tuple[PreparedSteerConsumptionCandidate, ...],
    quote: SteerSuffixAdmissionQuote,
    prospective_compiled_input: FrozenCompiledModelInput,
) -> PreparedSteerSuffixAdmissionPlan:
    return PreparedSteerSuffixAdmissionPlan(
        scope=scope,
        predecessor=predecessor,
        base_cut_fingerprint=base_cut_fingerprint,
        base_canonical_frontier_fingerprint=base_canonical_frontier_fingerprint,
        base_compile_snapshot_fingerprint=base_compile_snapshot_fingerprint,
        target_binding_fingerprint=target_binding_fingerprint,
        tool_surface_fingerprint=tool_surface_fingerprint,
        source_facts_fingerprint=source_facts_fingerprint,
        ordered_pending_queue_facts=ordered_pending_queue_facts,
        selected_consumption_candidates=selected_consumption_candidates,
        quote=quote,
        prospective_compiled_input=prospective_compiled_input,
    )


def build_accepted_steer_dispatch_batch(
    *,
    session_id: str,
    target_turn_id: str,
    entries: tuple[AcceptedSteerDispatchEntry, ...],
    canonical_expanded_bytes: int,
    resulting_epoch_logical_bytes: int,
) -> AcceptedSteerDispatchBatch:
    return AcceptedSteerDispatchBatch(
        session_id=session_id,
        target_turn_id=target_turn_id,
        entries=entries,
        canonical_expanded_bytes=canonical_expanded_bytes,
        resulting_epoch_logical_bytes=resulting_epoch_logical_bytes,
    )


__all__ = [
    "AcceptedSteerDispatchBatch",
    "AcceptedSteerDispatchEntry",
    "MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES",
    "MAXIMUM_STEER_ITEMS_PER_SAFE_POINT",
    "MemorySourceInvalidationReservation",
    "MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES",
    "PendingPromptSteerFact",
    "PreparedPromptIngressCommand",
    "PreparedQueuedRootTurnAdmission",
    "PreparedActiveRootInputAdmission",
    "PreparedActiveRootInputCandidate",
    "PreparedRootProviderInputAdmission",
    "PreparedRootProviderInputCandidate",
    "PreparedRootTurnIdentity",
    "PreparedSteerCanonicalBaseFence",
    "PreparedSteerConsumptionCandidate",
    "PreparedSteerPlanConflictInterruption",
    "PreparedSteerResourceRejection",
    "PreparedSteerSuffixAdmissionPlan",
    "PromptIngressConfirmation",
    "PromptIngressAccepted",
    "PromptIngressConfirmationKind",
    "PromptIngressWriteRejection",
    "QueuedRootTurnAdmissionAccepted",
    "QueuedRootTurnAdmissionConfirmation",
    "QueuedRootTurnAdmissionConfirmationKind",
    "SteerConsumptionConfirmation",
    "SteerConsumptionConfirmationKind",
    "SteerPlanConflictConfirmation",
    "SteerPlanConflictConfirmationKind",
    "SteerResourceRejectionConfirmation",
    "SteerResourceRejectionConfirmationKind",
    "SteerSuffixAdmissionQuote",
    "build_prompt_ingress_command",
    "prompt_ingress_semantic_digest",
    "build_direct_root_turn_identity",
    "build_queued_root_turn_identity",
    "build_queued_root_turn_admission",
    "build_steer_canonical_base_fence",
    "build_pending_prompt_steer_fact",
    "build_accepted_steer_dispatch_batch",
    "build_prepared_steer_suffix_plan",
    "build_steer_consumption_candidate",
    "build_steer_plan_conflict_interruption",
    "build_steer_resource_rejection",
    "build_steer_suffix_quote",
    "build_memory_source_invalidation_reservation",
    "prepared_steer_suffix_plan_identity_fingerprint",
    "steer_consumption_candidate_identity_fingerprint",
]
