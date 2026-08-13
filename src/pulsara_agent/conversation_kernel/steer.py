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
from pulsara_agent.conversation_kernel.vocabulary import (
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendPlanningInput,
    ProviderInputContinuityScope,
)
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    FrozenContextBindingCompileFact,
    FrozenPlanWorkflowCompileFact,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.permission import PermissionMode


MAXIMUM_STEER_ITEMS_PER_SAFE_POINT = 128
MAXIMUM_STEER_CANDIDATE_UTF8_BYTES = 16 << 20
# Bounds cumulative canonical-prefix materialization across longest-first
# trials.  This is a process-local planning-work quote, not durable capacity.
MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES = 256 << 20


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


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
    content_digest: str
    content_size: int
    semantic_digest: str

    def __post_init__(self) -> None:
        if not all((self.session_id, self.command_id, self.queue_item_id)):
            raise ValueError("prompt ingress identity is incomplete")
        if self.content_size < 1 or not self.content_digest.startswith("sha256:"):
            raise ValueError("prompt ingress content identity is invalid")
        if (self.delivery_mode is PromptDeliveryMode.NEW_TURN) != (
            self.target_turn_id is None
            and self.permission_snapshot_id is not None
            and self.requested_permission_mode is not None
        ):
            raise ValueError("prompt ingress delivery union is invalid")
        if not self.semantic_digest.startswith("sha256:"):
            raise ValueError("prompt ingress semantic digest is invalid")


class PromptIngressConfirmationKind(StrEnum):
    FULL_COMPATIBLE = "FULL_COMPATIBLE"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class PromptIngressWriteRejection(StrEnum):
    COMMAND_CONFLICT = "COMMAND_CONFLICT"
    TARGET_STALE_OR_NON_STEERABLE = "TARGET_STALE_OR_NON_STEERABLE"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"


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
    content_utf8: bytes,
) -> PreparedPromptIngressCommand:
    digest = "sha256:" + sha256(content_utf8).hexdigest()
    semantic = context_fingerprint(
        "pulsara:queue-prompt-command:v1",
        {
            "queue_item_id": queue_item_id,
            "client_submission_id": client_submission_id,
            "delivery_mode": delivery_mode.value,
            "target_turn_id": target_turn_id,
            "content_digest": digest,
            "permission_snapshot_id": permission_snapshot_id,
            "requested_permission_mode": (
                None
                if requested_permission_mode is None
                else requested_permission_mode.value
            ),
        },
    )
    return PreparedPromptIngressCommand(
        session_id=session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=delivery_mode,
        target_turn_id=target_turn_id,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=requested_permission_mode,
        content_digest=digest,
        content_size=len(content_utf8),
        semantic_digest=semantic,
    )


@dataclass(frozen=True, slots=True)
class PendingPromptSteerFact:
    session_id: str
    workspace_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    content: CanonicalContent = field(repr=False)
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if self.queue_sequence < 1 or self.content.size < 1:
            raise ValueError("pending steer fact bounds are invalid")
        if self.fact_fingerprint != pending_prompt_steer_fact_fingerprint(self):
            raise ValueError("pending steer fact fingerprint mismatch")


def pending_prompt_steer_fact_fingerprint(fact: PendingPromptSteerFact) -> str:
    return context_fingerprint(
        "pulsara:pending-prompt-steer-fact:v1",
        {
            "session_id": fact.session_id,
            "workspace_id": fact.workspace_id,
            "queue_item_id": fact.queue_item_id,
            "queue_sequence": fact.queue_sequence,
            "command_id": fact.command_id,
            "target_turn_id": fact.exact_target_turn_id,
            "content": _content_manifest(fact.content),
        },
    )


def build_pending_prompt_steer_fact(
    *,
    session_id: str,
    workspace_id: str,
    queue_item_id: str,
    queue_sequence: int,
    command_id: str,
    exact_target_turn_id: str,
    content: CanonicalContent,
) -> PendingPromptSteerFact:
    provisional = PendingPromptSteerFact.__new__(PendingPromptSteerFact)
    values = {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "queue_item_id": queue_item_id,
        "queue_sequence": queue_sequence,
        "command_id": command_id,
        "exact_target_turn_id": exact_target_turn_id,
        "content": content,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    return PendingPromptSteerFact(
        **values,
        fact_fingerprint=pending_prompt_steer_fact_fingerprint(provisional),
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
    fence_fingerprint: str

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
        if self.fence_fingerprint != steer_canonical_base_fence_fingerprint(self):
            raise ValueError("steer canonical base fence fingerprint mismatch")


def steer_canonical_base_fence_fingerprint(
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


def build_steer_canonical_base_fence(
    snapshot: FrozenCanonicalCompileSnapshot,
) -> PreparedSteerCanonicalBaseFence:
    identity = snapshot.canonical_input.identity
    values = {
        "session_id": identity.session_id,
        "exact_target_turn_id": identity.turn_id,
        "provider_input_through_sequence": identity.provider_input_through_sequence,
        "context_binding_fact": snapshot.context_binding_fact,
        "run_permission_snapshot": snapshot.run_permission_snapshot,
        "plan_workflow_fact": snapshot.plan_workflow_fact,
        "canonical_read_cut_fingerprint": snapshot.canonical_read_cut_fingerprint,
    }
    provisional = PreparedSteerCanonicalBaseFence.__new__(
        PreparedSteerCanonicalBaseFence
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fence_fingerprint", "")
    return PreparedSteerCanonicalBaseFence(
        **values,
        fence_fingerprint=steer_canonical_base_fence_fingerprint(provisional),
    )


@dataclass(frozen=True, slots=True)
class PreparedSteerConsumptionCandidate:
    session_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    content: CanonicalContent = field(repr=False)
    body_utf8: bytes = field(repr=False)
    new_entry_id: str
    expected_entry_sequence: int
    occurred_at: datetime
    actor_id: str
    predecessor: FrozenProviderInputAppendPlanningInput
    canonical_base_fence: PreparedSteerCanonicalBaseFence
    prompt_consumed_occurrence: CommittedEventDraft
    user_steer_accepted_occurrence: CommittedEventDraft
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if self.expected_entry_sequence < 1:
            raise ValueError("steer candidate entry sequence is invalid")
        if len(self.body_utf8) != self.content.size:
            raise ValueError("steer candidate body size differs from content")
        if "sha256:" + sha256(self.body_utf8).hexdigest() != self.content.digest:
            raise ValueError("steer candidate body digest differs from content")
        if (
            self.canonical_base_fence.session_id != self.session_id
            or self.canonical_base_fence.exact_target_turn_id
            != self.exact_target_turn_id
            or self.expected_entry_sequence
            <= self.canonical_base_fence.provider_input_through_sequence
        ):
            raise ValueError("steer candidate canonical base fence does not exact-join")
        if self.candidate_fingerprint != steer_consumption_candidate_fingerprint(self):
            raise ValueError("steer consumption candidate fingerprint mismatch")


def steer_consumption_candidate_fingerprint(
    candidate: PreparedSteerConsumptionCandidate,
) -> str:
    return context_fingerprint(
        "pulsara:prepared-steer-consumption:v1",
        {
            "session_id": candidate.session_id,
            "queue_item_id": candidate.queue_item_id,
            "queue_sequence": candidate.queue_sequence,
            "command_id": candidate.command_id,
            "target_turn_id": candidate.exact_target_turn_id,
            "content": _content_manifest(candidate.content),
            "entry_id": candidate.new_entry_id,
            "entry_sequence": candidate.expected_entry_sequence,
            "predecessor": _predecessor_value(candidate.predecessor),
            "canonical_base_fence": candidate.canonical_base_fence.fence_fingerprint,
            "prompt_consumed": _event_manifest(candidate.prompt_consumed_occurrence),
            "user_steer": _event_manifest(candidate.user_steer_accepted_occurrence),
        },
    )


def build_steer_consumption_candidate(
    *,
    fact: PendingPromptSteerFact,
    body_utf8: bytes,
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
    provisional = PreparedSteerConsumptionCandidate.__new__(
        PreparedSteerConsumptionCandidate
    )
    values = {
        "session_id": fact.session_id,
        "queue_item_id": fact.queue_item_id,
        "queue_sequence": fact.queue_sequence,
        "command_id": fact.command_id,
        "exact_target_turn_id": fact.exact_target_turn_id,
        "content": fact.content,
        "body_utf8": bytes(body_utf8),
        "new_entry_id": entry_id,
        "expected_entry_sequence": expected_entry_sequence,
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "predecessor": predecessor,
        "canonical_base_fence": canonical_base_fence,
        "prompt_consumed_occurrence": prompt_event,
        "user_steer_accepted_occurrence": steer_event,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "candidate_fingerprint", "")
    return PreparedSteerConsumptionCandidate(
        **values,
        candidate_fingerprint=steer_consumption_candidate_fingerprint(provisional),
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
    canonical_utf8_bytes: int
    resulting_epoch_logical_bytes: int
    batch_fingerprint: str

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
            item.user_steer_event_sequence
            != item.prompt_consumed_event_sequence + 1
            for item in self.entries
        ) or event_sequences != tuple(sorted(set(event_sequences))):
            raise ValueError("accepted steer batch event order is invalid")
        if any(
            item.target_turn_id != self.target_turn_id or item.content_size < 1
            for item in self.entries
        ):
            raise ValueError("accepted steer batch target/content identity is invalid")
        if sum(item.content_size for item in self.entries) != self.canonical_utf8_bytes:
            raise ValueError("accepted steer batch body total is invalid")
        if not 0 < self.canonical_utf8_bytes <= MAXIMUM_STEER_CANDIDATE_UTF8_BYTES:
            raise ValueError("accepted steer batch body bound is invalid")
        if not 0 < self.resulting_epoch_logical_bytes <= (64 << 20):
            raise ValueError("accepted steer batch epoch bound is invalid")
        if self.batch_fingerprint != accepted_steer_dispatch_batch_fingerprint(self):
            raise ValueError("accepted steer batch fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class SteerSuffixAdmissionQuote:
    selected_candidate_fingerprints: tuple[str, ...]
    selected_item_count: int
    selected_canonical_utf8_bytes: int
    prospective_snapshot_hydrated_bytes: int
    resulting_epoch_logical_bytes: int
    resulting_target_estimate: TokenEstimate
    effective_target_budget: int
    estimator_fingerprint: str
    predecessor_prefix_fingerprint: str | None
    quote_fingerprint: str

    def __post_init__(self) -> None:
        if not 1 <= self.selected_item_count <= MAXIMUM_STEER_ITEMS_PER_SAFE_POINT:
            raise ValueError("steer quote selects no items")
        if len(self.selected_candidate_fingerprints) != self.selected_item_count:
            raise ValueError("steer quote candidate count is invalid")
        if any(
            not value.startswith("sha256:")
            for value in self.selected_candidate_fingerprints
        ):
            raise ValueError("steer quote candidate fingerprint is invalid")
        if (
            not 0
            < self.selected_canonical_utf8_bytes
            <= MAXIMUM_STEER_CANDIDATE_UTF8_BYTES
        ):
            raise ValueError("steer quote body bound is invalid")
        if not 0 < self.prospective_snapshot_hydrated_bytes <= (16 << 20):
            raise ValueError("steer quote snapshot bound is invalid")
        if not 0 < self.resulting_epoch_logical_bytes <= (64 << 20):
            raise ValueError("steer quote epoch bound is invalid")
        if (
            self.effective_target_budget < 1
            or self.resulting_target_estimate.total_input_tokens
            > self.effective_target_budget
        ):
            raise ValueError("steer quote target budget is invalid")
        if not self.estimator_fingerprint.startswith("sha256:"):
            raise ValueError("steer quote estimator fingerprint is invalid")
        if self.predecessor_prefix_fingerprint is not None and not (
            self.predecessor_prefix_fingerprint.startswith("sha256:")
        ):
            raise ValueError("steer quote predecessor fingerprint is invalid")
        if self.quote_fingerprint != steer_suffix_quote_fingerprint(self):
            raise ValueError("steer quote fingerprint mismatch")


def steer_suffix_quote_fingerprint(quote: SteerSuffixAdmissionQuote) -> str:
    if (
        min(
            quote.selected_canonical_utf8_bytes,
            quote.prospective_snapshot_hydrated_bytes,
            quote.resulting_epoch_logical_bytes,
        )
        < 0
    ):
        raise ValueError("steer quote byte measure is invalid")
    return context_fingerprint(
        "pulsara:steer-suffix-admission-quote:v1",
        {
            "candidates": quote.selected_candidate_fingerprints,
            "selected_items": quote.selected_item_count,
            "selected_canonical_bytes": quote.selected_canonical_utf8_bytes,
            "snapshot_bytes": quote.prospective_snapshot_hydrated_bytes,
            "epoch_bytes": quote.resulting_epoch_logical_bytes,
            "estimate": _estimate_value(quote.resulting_target_estimate),
            "effective_budget": quote.effective_target_budget,
            "estimator": quote.estimator_fingerprint,
            "predecessor_prefix": quote.predecessor_prefix_fingerprint,
        },
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
    ordered_pending_queue_fingerprints: tuple[str, ...]
    selected_consumption_candidates: tuple[PreparedSteerConsumptionCandidate, ...]
    quote: SteerSuffixAdmissionQuote
    prospective_compiled_input: FrozenCompiledModelInput = field(repr=False)
    plan_fingerprint: str

    def __post_init__(self) -> None:
        if len(self.selected_consumption_candidates) != self.quote.selected_item_count:
            raise ValueError("steer plan candidate count differs from quote")
        selected = self.selected_consumption_candidates
        if tuple(item.candidate_fingerprint for item in selected) != (
            self.quote.selected_candidate_fingerprints
        ):
            raise ValueError("steer plan candidates differ from quote")
        sequences = tuple(item.queue_sequence for item in selected)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("steer plan candidates are not FIFO")
        if self.plan_fingerprint != prepared_steer_suffix_plan_fingerprint(self):
            raise ValueError("steer plan fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PreparedSteerResourceRejection:
    session_id: str
    source_plan_fingerprint: str
    workspace_id: str
    queue_item_id: str
    queue_sequence: int
    command_id: str
    exact_target_turn_id: str
    content: CanonicalContent = field(repr=False)
    expected_pending_fact_fingerprint: str
    reason: str
    occurred_at: datetime
    actor_id: str
    prompt_rejected_occurrence: CommittedEventDraft
    turn_interrupted_occurrence: CommittedEventDraft
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or self.queue_sequence < 1
            or self.reason != "STEER_INPUT_RESOURCE_EXHAUSTED"
            or not self.expected_pending_fact_fingerprint.startswith("sha256:")
        ):
            raise ValueError("steer resource rejection is invalid")
        expected_fact = build_pending_prompt_steer_fact(
            session_id=self.session_id,
            workspace_id=self.workspace_id,
            queue_item_id=self.queue_item_id,
            queue_sequence=self.queue_sequence,
            command_id=self.command_id,
            exact_target_turn_id=self.exact_target_turn_id,
            content=self.content,
        )
        if expected_fact.fact_fingerprint != self.expected_pending_fact_fingerprint:
            raise ValueError("steer resource rejection fact identity is invalid")
        if (
            self.prompt_rejected_occurrence.event_type
            is not CommittedEventType.PROMPT_REJECTED
            or self.turn_interrupted_occurrence.event_type
            is not CommittedEventType.TURN_INTERRUPTED
        ):
            raise ValueError("steer resource rejection occurrences are invalid")
        if self.candidate_fingerprint != steer_resource_rejection_fingerprint(self):
            raise ValueError("steer resource rejection fingerprint mismatch")


def steer_resource_rejection_fingerprint(
    candidate: PreparedSteerResourceRejection,
) -> str:
    return context_fingerprint(
        "pulsara:prepared-steer-resource-rejection:v1",
        {
            "session_id": candidate.session_id,
            "source_plan": candidate.source_plan_fingerprint,
            "workspace_id": candidate.workspace_id,
            "queue_item_id": candidate.queue_item_id,
            "queue_sequence": candidate.queue_sequence,
            "command_id": candidate.command_id,
            "target_turn_id": candidate.exact_target_turn_id,
            "content": _content_manifest(candidate.content),
            "pending_fact": candidate.expected_pending_fact_fingerprint,
            "reason": candidate.reason,
            "occurred_at": candidate.occurred_at.isoformat(),
            "actor_id": candidate.actor_id,
            "prompt_rejected": _event_manifest(candidate.prompt_rejected_occurrence),
            "turn_interrupted": _event_manifest(candidate.turn_interrupted_occurrence),
        },
    )


def build_steer_resource_rejection(
    *,
    source_plan_fingerprint: str,
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
    provisional = PreparedSteerResourceRejection.__new__(PreparedSteerResourceRejection)
    values = {
        "session_id": fact.session_id,
        "source_plan_fingerprint": source_plan_fingerprint,
        "workspace_id": fact.workspace_id,
        "queue_item_id": fact.queue_item_id,
        "queue_sequence": fact.queue_sequence,
        "command_id": fact.command_id,
        "exact_target_turn_id": fact.exact_target_turn_id,
        "content": fact.content,
        "expected_pending_fact_fingerprint": fact.fact_fingerprint,
        "reason": reason,
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "prompt_rejected_occurrence": prompt_event,
        "turn_interrupted_occurrence": turn_event,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "candidate_fingerprint", "")
    return PreparedSteerResourceRejection(
        **values,
        candidate_fingerprint=steer_resource_rejection_fingerprint(provisional),
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
    candidate_fingerprint: str

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
        if self.candidate_fingerprint != steer_plan_conflict_fingerprint(self):
            raise ValueError("steer plan-conflict fingerprint mismatch")


class SteerPlanConflictConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    HISTORICAL_TERMINAL = "HISTORICAL_TERMINAL"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class SteerPlanConflictConfirmation:
    kind: SteerPlanConflictConfirmationKind


def steer_plan_conflict_fingerprint(
    candidate: PreparedSteerPlanConflictInterruption,
) -> str:
    return context_fingerprint(
        "pulsara:prepared-steer-plan-conflict-interruption:v1",
        {
            "session_id": candidate.session_id,
            "target_turn_id": candidate.exact_target_turn_id,
            "source_plan": candidate.source_plan_fingerprint,
            "occurred_at": candidate.occurred_at.isoformat(),
            "actor_id": candidate.actor_id,
            "turn_interrupted": _event_manifest(candidate.turn_interrupted_occurrence),
        },
    )


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
    provisional = PreparedSteerPlanConflictInterruption.__new__(
        PreparedSteerPlanConflictInterruption
    )
    values = {
        "session_id": session_id,
        "exact_target_turn_id": exact_target_turn_id,
        "source_plan_fingerprint": source_plan_fingerprint,
        "occurred_at": occurred_at,
        "actor_id": actor_id,
        "turn_interrupted_occurrence": event,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "candidate_fingerprint", "")
    return PreparedSteerPlanConflictInterruption(
        **values,
        candidate_fingerprint=steer_plan_conflict_fingerprint(provisional),
    )


def _estimate_value(estimate: TokenEstimate) -> dict[str, object]:
    return {
        "system": estimate.system_tokens,
        "messages": estimate.message_tokens,
        "message_by_index": estimate.message_tokens_by_index,
        "tools": estimate.tool_tokens,
        "envelope": estimate.envelope_tokens,
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
) -> SteerSuffixAdmissionQuote:
    canonical_bytes = sum(item.content.size for item in candidates)
    values = {
        "selected_candidate_fingerprints": tuple(
            item.candidate_fingerprint for item in candidates
        ),
        "selected_item_count": len(candidates),
        "selected_canonical_utf8_bytes": canonical_bytes,
        "prospective_snapshot_hydrated_bytes": prospective_snapshot_hydrated_bytes,
        "resulting_epoch_logical_bytes": resulting_epoch_logical_bytes,
        "resulting_target_estimate": resulting_target_estimate,
        "effective_target_budget": effective_target_budget,
        "estimator_fingerprint": estimator_fingerprint,
        "predecessor_prefix_fingerprint": predecessor_prefix_fingerprint,
    }
    provisional = SteerSuffixAdmissionQuote.__new__(SteerSuffixAdmissionQuote)
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "quote_fingerprint", "")
    return SteerSuffixAdmissionQuote(
        **values,
        quote_fingerprint=steer_suffix_quote_fingerprint(provisional),
    )


def prepared_steer_suffix_plan_fingerprint(
    plan: PreparedSteerSuffixAdmissionPlan,
) -> str:
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
            "pending": plan.ordered_pending_queue_fingerprints,
            "selected": tuple(
                item.candidate_fingerprint
                for item in plan.selected_consumption_candidates
            ),
            "quote": plan.quote.quote_fingerprint,
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
    ordered_pending_queue_fingerprints: tuple[str, ...],
    selected_consumption_candidates: tuple[PreparedSteerConsumptionCandidate, ...],
    quote: SteerSuffixAdmissionQuote,
    prospective_compiled_input: FrozenCompiledModelInput,
) -> PreparedSteerSuffixAdmissionPlan:
    provisional = PreparedSteerSuffixAdmissionPlan.__new__(
        PreparedSteerSuffixAdmissionPlan
    )
    values = {
        "scope": scope,
        "predecessor": predecessor,
        "base_cut_fingerprint": base_cut_fingerprint,
        "base_canonical_frontier_fingerprint": base_canonical_frontier_fingerprint,
        "base_compile_snapshot_fingerprint": base_compile_snapshot_fingerprint,
        "target_binding_fingerprint": target_binding_fingerprint,
        "tool_surface_fingerprint": tool_surface_fingerprint,
        "source_facts_fingerprint": source_facts_fingerprint,
        "ordered_pending_queue_fingerprints": ordered_pending_queue_fingerprints,
        "selected_consumption_candidates": selected_consumption_candidates,
        "quote": quote,
        "prospective_compiled_input": prospective_compiled_input,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "plan_fingerprint", "")
    return PreparedSteerSuffixAdmissionPlan(
        **values,
        plan_fingerprint=prepared_steer_suffix_plan_fingerprint(provisional),
    )


def accepted_steer_dispatch_batch_fingerprint(
    batch: AcceptedSteerDispatchBatch,
) -> str:
    return context_fingerprint(
        "pulsara:accepted-steer-dispatch-batch:v1",
        {
            "session_id": batch.session_id,
            "target_turn_id": batch.target_turn_id,
            "canonical_bytes": batch.canonical_utf8_bytes,
            "epoch_bytes": batch.resulting_epoch_logical_bytes,
            "entries": tuple(
                (
                    item.queue_item_id,
                    item.queue_sequence,
                    item.entry_id,
                    item.entry_sequence,
                    item.content_digest,
                    item.content_size,
                    item.prompt_consumed_event_id,
                    item.prompt_consumed_event_sequence,
                    item.user_steer_event_id,
                    item.user_steer_event_sequence,
                )
                for item in batch.entries
            ),
        },
    )


def build_accepted_steer_dispatch_batch(
    *,
    session_id: str,
    target_turn_id: str,
    entries: tuple[AcceptedSteerDispatchEntry, ...],
    canonical_utf8_bytes: int,
    resulting_epoch_logical_bytes: int,
) -> AcceptedSteerDispatchBatch:
    provisional = AcceptedSteerDispatchBatch.__new__(AcceptedSteerDispatchBatch)
    for name, value in {
        "session_id": session_id,
        "target_turn_id": target_turn_id,
        "entries": entries,
        "canonical_utf8_bytes": canonical_utf8_bytes,
        "resulting_epoch_logical_bytes": resulting_epoch_logical_bytes,
    }.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "batch_fingerprint", "")
    return AcceptedSteerDispatchBatch(
        session_id=session_id,
        target_turn_id=target_turn_id,
        entries=entries,
        canonical_utf8_bytes=canonical_utf8_bytes,
        resulting_epoch_logical_bytes=resulting_epoch_logical_bytes,
        batch_fingerprint=accepted_steer_dispatch_batch_fingerprint(provisional),
    )


__all__ = [
    "AcceptedSteerDispatchBatch",
    "AcceptedSteerDispatchEntry",
    "MAXIMUM_STEER_CANDIDATE_UTF8_BYTES",
    "MAXIMUM_STEER_ITEMS_PER_SAFE_POINT",
    "MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES",
    "PendingPromptSteerFact",
    "PreparedPromptIngressCommand",
    "PreparedSteerCanonicalBaseFence",
    "PreparedSteerConsumptionCandidate",
    "PreparedSteerPlanConflictInterruption",
    "PreparedSteerResourceRejection",
    "PreparedSteerSuffixAdmissionPlan",
    "PromptIngressConfirmation",
    "PromptIngressConfirmationKind",
    "PromptIngressWriteRejection",
    "SteerConsumptionConfirmation",
    "SteerConsumptionConfirmationKind",
    "SteerPlanConflictConfirmation",
    "SteerPlanConflictConfirmationKind",
    "SteerResourceRejectionConfirmation",
    "SteerResourceRejectionConfirmationKind",
    "SteerSuffixAdmissionQuote",
    "build_prompt_ingress_command",
    "build_steer_canonical_base_fence",
    "build_pending_prompt_steer_fact",
    "build_accepted_steer_dispatch_batch",
    "build_prepared_steer_suffix_plan",
    "build_steer_consumption_candidate",
    "build_steer_plan_conflict_interruption",
    "build_steer_resource_rejection",
    "build_steer_suffix_quote",
    "pending_prompt_steer_fact_fingerprint",
    "steer_consumption_candidate_fingerprint",
    "steer_canonical_base_fence_fingerprint",
    "steer_plan_conflict_fingerprint",
    "steer_resource_rejection_fingerprint",
    "steer_suffix_quote_fingerprint",
]
