"""Pure process-local contracts for bounded active-turn steer admission.

These values freeze one safe-point plan and its deterministic canonical
mutation candidates.  They are neither durable receipts nor a second queue
authority: PostgreSQL remains the sole truth for queue, entry, and event rows.
"""

from __future__ import annotations

from pulsara_agent.identity import derive_id, require_id

from pulsara_agent.scheduling.contracts import ScheduledAdmission, ScheduledProvenance

from pulsara_agent.conversation_kernel.interruption import interruption_payload

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
    CanonicalInputOriginKind,
    ContextBindingBaseKind,
    ContextSourceKind,
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    FrozenContextBindingCompileFact,
    FrozenPlanWorkflowCompileFact,
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


MAXIMUM_STEER_ITEMS_PER_SAFE_POINT = 128
MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES = 16 << 20
# Bounds cumulative canonical-prefix materialization across longest-first
# trials.  This is a process-local planning-work quote, not durable capacity.
MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES = 256 << 20


@dataclass(frozen=True, slots=True)
class PreparedRootTurnIdentity:
    turn_id: str
    entry_id: str
    context_revision_id: str
    permission_snapshot_id: str


@dataclass(frozen=True, slots=True)
class StartedTurnInputGroup:
    """The immutable canonical inputs and inherited base of one started request."""

    session_id: str
    turn_id: str
    instruction_entry_id: str
    ordered_items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    predecessor_through_sequence: int
    initial_context_binding_revision_id: str
    initial_base_kind: ContextBindingBaseKind
    initial_context_snapshot_id: str | None
    initial_source_through_sequence: int

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or not self.instruction_entry_id
            or not self.initial_context_binding_revision_id
            or not self.ordered_items
            or self.predecessor_through_sequence < 0
            or self.initial_source_through_sequence < 0
            or self.initial_source_through_sequence > self.predecessor_through_sequence
            or (self.initial_base_kind is ContextBindingBaseKind.FULL_HISTORY
                and self.initial_source_through_sequence != self.predecessor_through_sequence)
            or (self.initial_base_kind is ContextBindingBaseKind.FULL_HISTORY)
            != (self.initial_context_snapshot_id is None)
        ):
            raise ValueError("started turn input group is invalid")
        if (
            tuple(item.source_entry_sequence for item in self.ordered_items)
            != tuple(range(self.predecessor_through_sequence + 1,
                           self.predecessor_through_sequence + 1 + len(self.ordered_items)))
            or any(item.source_turn_id != self.turn_id for item in self.ordered_items)
            or any(item.item_kind not in {
                FrozenProviderInputItemKind.USER,
                FrozenProviderInputItemKind.PLAN_CONTINUATION,
                FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
            } for item in self.ordered_items)
            or len({item.source_entry_id for item in self.ordered_items}) != len(self.ordered_items)
            or self.instruction_entry_id not in {
                item.source_entry_id for item in self.ordered_items
            }
        ):
            raise ValueError("started turn input group does not exact-join")

    @property
    def initial_revision_ordinal(self) -> int:
        return 0

    @property
    def provider_input_through_sequence(self) -> int:
        return self.ordered_items[-1].source_entry_sequence






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
                charge < 1 for charge in self.unpublished_item_canonical_expanded_bytes
            )
        ):
            raise ValueError("prospective active ROOT suffix is invalid")
        expected_sequences = tuple(
            range(
                cut.provider_input_through_sequence + 1,
                cut.provider_input_through_sequence + 1 + len(self.unpublished_items),
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
            or identity.context_binding_revision_id != cut.context_binding_revision_id
            or identity.provider_input_through_sequence
            != self.candidate.resulting_provider_input_through_sequence
            or self.semantic_input.canonical_input_identity != identity
            or self.wire_quote.final_wire_utf8_bytes > (64 << 20)
            or self.wire_quote.budget_input_tokens
            > self.wire_quote.effective_input_budget_tokens
        ):
            raise ValueError("prospective active ROOT admission does not exact-join")


def build_direct_root_turn_identity(
    session_id: str, command_id: str
) -> PreparedRootTurnIdentity:
    require_id(session_id, "session")
    require_id(command_id, "command")
    turn_id = derive_id("turn", "command", session_id, command_id)
    return PreparedRootTurnIdentity(
        turn_id=turn_id,
        entry_id=derive_id("entry", "root-user", turn_id),
        context_revision_id=derive_id("binding", "revision", turn_id, "0"),
        permission_snapshot_id=derive_id("permission", "admission", turn_id),
    )


def build_queued_root_turn_identity(
    session_id: str, queue_item_id: str
) -> PreparedRootTurnIdentity:
    require_id(session_id, "session")
    require_id(queue_item_id, "queue")
    turn_id = derive_id("turn", "queue", session_id, queue_item_id)
    return PreparedRootTurnIdentity(
        turn_id=turn_id,
        entry_id=derive_id("entry", "root-user", turn_id),
        context_revision_id=derive_id("binding", "revision", turn_id, "0"),
        permission_snapshot_id=derive_id("permission", "admission", turn_id),
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
    scheduled: ScheduledAdmission | None = None

    def __post_init__(self) -> None:
        if not all((self.session_id, self.command_id, self.queue_item_id)):
            raise ValueError("prompt ingress identity is incomplete")
        if not isinstance(self.canonical_prompt, FrozenCanonicalPrompt):
            raise TypeError("prompt ingress content must be canonical and frozen")
        if self.scheduled is not None and (
            self.delivery_mode is not PromptDeliveryMode.NEW_TURN
            or self.scheduled.session_id != self.session_id
            or self.scheduled.permission_mode != self.requested_permission_mode
        ):
            raise ValueError("scheduled admission does not join prompt ingress")
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

    @property
    def command_kind(self):
        return (
            "RUN_SCHEDULED_TASK"
            if self.scheduled is not None and self.scheduled.manual
            else "QUEUE_PROMPT"
        )

    @property
    def schema_version(self):
        return (
            "run_scheduled_task.v1"
            if self.command_kind == "RUN_SCHEDULED_TASK"
            else "queue_prompt.v3"
        )

    @property
    def input_origin(self):
        return (
            CanonicalInputOriginKind.SCHEDULED_TASK
            if self.scheduled is not None
            else (
                CanonicalInputOriginKind.HUMAN_MESSAGE
                if self.delivery_mode is PromptDeliveryMode.NEW_TURN
                else CanonicalInputOriginKind.HUMAN_STEER
            )
        )


@dataclass(frozen=True, slots=True)
class PromptIngressAccepted:
    queue_sequence: int
    model_call_binding: ModelCallBinding | None
    reasoning_preference_reset: bool = False
    queue_item_id: str | None = None

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
class PreparedQueuedInputEntry:
    entry_id: str
    body_storage: CanonicalContent = field(repr=False)
    canonical_prompt: FrozenCanonicalPrompt = field(repr=False)
    input_origin: CanonicalInputOriginKind


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
    command_id: str | None
    client_submission_id: str | None
    body_storage: CanonicalContent = field(repr=False)
    canonical_prompt: FrozenCanonicalPrompt = field(repr=False)
    permission_snapshot: FrozenRunPermissionSnapshot
    model_call_binding: ModelCallBinding
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
    input_origin: CanonicalInputOriginKind = CanonicalInputOriginKind.HUMAN_MESSAGE
    scheduled_input: ScheduledProvenance | None = None
    input_entries: tuple[PreparedQueuedInputEntry, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if (
            not all(
                (
                    self.session_id,
                    self.workspace_id,
                    self.queue_item_id,
                    self.exact_turn_id,
                    self.exact_initial_entry_id,
                    self.exact_context_binding_revision_id,
                    self.actor_id,
                )
            )
            or self.queue_sequence < 1
        ):
            raise ValueError("queued ROOT admission identity is incomplete")
        if self.input_origin is CanonicalInputOriginKind.AGENT_INSTRUCTION:
            if self.command_id is not None or self.client_submission_id is not None:
                raise ValueError(
                    "delegated request cannot impersonate a human submission"
                )
        elif not self.command_id or not self.client_submission_id:
            raise ValueError("human queue admission lacks submission identity")
        if not isinstance(self.model_call_binding, ModelCallBinding):
            raise ValueError("queued ROOT admission lacks a model binding")
        entries = self.ordered_input_entries
        if (
            len({entry.entry_id for entry in entries}) != len(entries)
            or not any(entry.entry_id == self.exact_initial_entry_id for entry in entries)
        ):
            raise ValueError("queued ROOT initial input group is invalid")
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

    @property
    def ordered_input_entries(self) -> tuple[PreparedQueuedInputEntry, ...]:
        return self.input_entries or (
            PreparedQueuedInputEntry(
                self.exact_initial_entry_id,
                self.body_storage,
                self.canonical_prompt,
                self.input_origin,
            ),
        )


def build_queued_root_turn_admission(
    *,
    session_id: str,
    workspace_id: str,
    queue_item_id: str,
    queue_sequence: int,
    command_id: str | None,
    client_submission_id: str | None,
    content: CanonicalContent,
    canonical_prompt: FrozenCanonicalPrompt,
    permission_snapshot: FrozenRunPermissionSnapshot,
    model_call_binding: ModelCallBinding,
    pending_plan_handoff_workflow_id: str | None,
    pending_plan_handoff_interaction_id: str | None,
    pending_plan_handoff_kind: str | None,
    occurred_at: datetime,
    actor_id: str,
    input_origin: CanonicalInputOriginKind = CanonicalInputOriginKind.HUMAN_MESSAGE,
    scheduled_input: ScheduledProvenance | None = None,
    input_entries: tuple[PreparedQueuedInputEntry, ...] = (),
) -> PreparedQueuedRootTurnAdmission:
    identity = build_queued_root_turn_identity(session_id, queue_item_id)
    turn_id = identity.turn_id
    entry_id = identity.entry_id
    revision_id = identity.context_revision_id
    consumed = CommittedEventDraft(
        event_id=derive_id(
            "event",
            "accepted",
            session_id,
            CommittedEventType.PROMPT_CONSUMED.name,
            SubjectSlot.QUEUE_ITEM.name,
            queue_item_id,
            "",
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
        event_id=derive_id(
            "event",
            "accepted",
            session_id,
            CommittedEventType.USER_MESSAGE_ACCEPTED.name,
            SubjectSlot.ENTRY.name,
            entry_id,
            "",
        ),
        event_type=CommittedEventType.USER_MESSAGE_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
        actor_kind="runtime"
        if scheduled_input is not None
        or input_origin is not CanonicalInputOriginKind.HUMAN_MESSAGE
        else "human",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={
            "source": "PROMPT_QUEUE",
            "input_origin": input_origin.value,
            "scheduled_input": None
            if scheduled_input is None
            else scheduled_input.to_dict(),
        },
    )
    return PreparedQueuedRootTurnAdmission(
        input_entries=input_entries,
        input_origin=input_origin,
        scheduled_input=scheduled_input,
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
    started_input_group: StartedTurnInputGroup


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
    scheduled: ScheduledAdmission | None = None,
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
        scheduled=scheduled,
    )


def prompt_ingress_semantic_digest(
    candidate: PreparedPromptIngressCommand,
    model_call_binding: ModelCallBinding | None,
) -> str:
    if candidate.scheduled is not None and candidate.scheduled.manual:
        return context_fingerprint(
            "pulsara:run-scheduled-task:v1", candidate.scheduled.action_value()
        )
    if (candidate.delivery_mode is PromptDeliveryMode.NEW_TURN) != (
        model_call_binding is not None
    ):
        raise ValueError("prompt ingress model binding union is invalid")
    return context_fingerprint(
        "pulsara:queue-prompt-command:v3",
        {
            "input_origin": candidate.input_origin.value,
            "scheduled_input": None
            if candidate.scheduled is None
            else candidate.scheduled.provenance(),
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
    command_id: str | None
    exact_target_turn_id: str
    body_storage: CanonicalContent = field(repr=False)
    canonical_expanded_bytes: int
    input_origin: CanonicalInputOriginKind = CanonicalInputOriginKind.HUMAN_STEER

    def __post_init__(self) -> None:
        if (
            self.queue_sequence < 1
            or self.body_storage.size < 1
            or self.canonical_expanded_bytes < self.body_storage.size
            or self.canonical_expanded_bytes > MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES
        ):
            raise ValueError("pending steer fact bounds are invalid")


def build_pending_prompt_steer_fact(
    *,
    session_id: str,
    workspace_id: str,
    queue_item_id: str,
    queue_sequence: int,
    command_id: str | None,
    exact_target_turn_id: str,
    content: CanonicalContent,
    canonical_expanded_bytes: int,
    input_origin: CanonicalInputOriginKind = CanonicalInputOriginKind.HUMAN_STEER,
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
        input_origin=input_origin,
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
    command_id: str | None
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
    input_origin: CanonicalInputOriginKind
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
    entry_id = derive_id("entry", "steer", fact.session_id, fact.queue_item_id)
    prompt_event = CommittedEventDraft(
        event_id=derive_id(
            "event",
            "accepted",
            fact.session_id,
            CommittedEventType.PROMPT_CONSUMED.name,
            SubjectSlot.QUEUE_ITEM.name,
            fact.queue_item_id,
            "",
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
        event_id=derive_id(
            "event",
            "accepted",
            fact.session_id,
            CommittedEventType.USER_STEER_ACCEPTED.name,
            SubjectSlot.ENTRY.name,
            entry_id,
            "",
        ),
        event_type=CommittedEventType.USER_STEER_ACCEPTED,
        subject=CommittedEventSubject(SubjectSlot.ENTRY, entry_id),
        actor_kind="human"
        if fact.input_origin is CanonicalInputOriginKind.HUMAN_STEER
        else "runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload={"source": "PROMPT_QUEUE", "input_origin": fact.input_origin.value},
    )
    return PreparedSteerConsumptionCandidate(
        input_origin=fact.input_origin,
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
        if (
            sum(item.content_size for item in self.entries)
            > self.canonical_expanded_bytes
        ):
            raise ValueError("accepted steer batch body total exceeds expanded total")
        if not (
            0 < self.canonical_expanded_bytes <= MAXIMUM_STEER_CANDIDATE_EXPANDED_BYTES
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
        if self.effective_target_budget < 1:
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
    invalidation_input_token_ceiling: int
    invalidation_epoch_bytes_ceiling: int
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
            min(
                self.invalidation_input_token_ceiling,
                self.invalidation_epoch_bytes_ceiling,
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
    invalidation_input_token_ceiling: int,
    invalidation_epoch_bytes_ceiling: int,
    estimator_fingerprint: str,
) -> MemorySourceInvalidationReservation:
    return MemorySourceInvalidationReservation(
        source_kind=source_kind,
        prior_presence=prior_presence,
        prior_semantic_fingerprint=prior_semantic_fingerprint,
        desired_presence=desired_presence,
        desired_semantic_fingerprint=desired_semantic_fingerprint,
        source_contract_fingerprint=source_contract_fingerprint,
        invalidation_input_token_ceiling=invalidation_input_token_ceiling,
        invalidation_epoch_bytes_ceiling=invalidation_epoch_bytes_ceiling,
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
        event_id=derive_id(
            "event",
            "accepted",
            fact.session_id,
            CommittedEventType.PROMPT_REJECTED.name,
            SubjectSlot.QUEUE_ITEM.name,
            fact.queue_item_id,
            "",
        ),
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
        event_id=derive_id(
            "event",
            "accepted",
            fact.session_id,
            CommittedEventType.TURN_INTERRUPTED.name,
            SubjectSlot.TURN.name,
            fact.exact_target_turn_id,
            "",
        ),
        event_type=CommittedEventType.TURN_INTERRUPTED,
        subject=CommittedEventSubject(SubjectSlot.TURN, fact.exact_target_turn_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload=interruption_payload(turn_reason),
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
    occurred_at: datetime
    actor_id: str
    turn_interrupted_occurrence: CommittedEventDraft

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.exact_target_turn_id,
                self.actor_id,
            )
        ):
            raise ValueError("steer plan-conflict interruption is incomplete")
        event = self.turn_interrupted_occurrence
        if (
            event.event_type is not CommittedEventType.TURN_INTERRUPTED
            or event.subject.slot is not SubjectSlot.TURN
            or event.subject.subject_id != self.exact_target_turn_id
            or event.payload != interruption_payload("PROVIDER_INPUT_PLAN_CONFLICT")
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
    occurred_at: datetime,
    actor_id: str,
) -> PreparedSteerPlanConflictInterruption:
    reason = "PROVIDER_INPUT_PLAN_CONFLICT"
    event = CommittedEventDraft(
        event_id=derive_id(
            "event",
            "accepted",
            session_id,
            CommittedEventType.TURN_INTERRUPTED.name,
            SubjectSlot.TURN.name,
            exact_target_turn_id,
            "",
        ),
        event_type=CommittedEventType.TURN_INTERRUPTED,
        subject=CommittedEventSubject(SubjectSlot.TURN, exact_target_turn_id),
        actor_kind="runtime",
        actor_id=actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=occurred_at,
        payload=interruption_payload(reason),
    )
    return PreparedSteerPlanConflictInterruption(
        session_id=session_id,
        exact_target_turn_id=exact_target_turn_id,
        occurred_at=occurred_at,
        actor_id=actor_id,
        turn_interrupted_occurrence=event,
    )


def build_steer_suffix_quote(
    *,
    candidates: tuple[PreparedSteerConsumptionCandidate, ...],
    prospective_snapshot_hydrated_bytes: int,
    resulting_epoch_logical_bytes: int,
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
        effective_target_budget=effective_target_budget,
        estimator_fingerprint=estimator_fingerprint,
        predecessor_prefix_fingerprint=predecessor_prefix_fingerprint,
        memory_recall_reservation=memory_recall_reservation,
        memory_response_preference_reservation=(memory_response_preference_reservation),
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
    "StartedTurnInputGroup",
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
]
