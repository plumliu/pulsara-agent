"""Durable prompt-queue authority, content, hold, and checkpoint facts."""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)
from pulsara_agent.primitives.storage_frozen import (
    FrozenStorageFactBase,
    build_frozen_storage_fact,
    register_durable_storage_fact,
)

Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
PROMPT_QUEUE_INLINE_MAX_UTF8_BYTES = 16 * 1024
PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES = 4 * 1024 * 1024
PROMPT_QUEUE_ARTIFACT_STORAGE_CONTRACT_FINGERPRINT = context_fingerprint(
    "prompt-queue-artifact-storage-contract:v1",
    {
        "content": "canonical_utf8_text",
        "identity": "session_scoped_content_address",
        "semantic_metadata": "exact_canonical_json",
        "preparation": "artifact_and_prepared_hold_one_transaction",
    },
)
PromptQueueDeliveryState = Literal[
    "accepted_pending",
    "steer_reserved",
    "follow_up_reserved",
    "committed_to_active_run",
    "committed_to_new_run",
    "cancelled",
    "delivery_rejected",
    "reconciliation_required",
]
PromptQueueContentRetentionState = Literal["active", "retired"]
PromptQueueDeliveryMode = Literal["auto", "steer", "follow_up"]
PromptQueueResolvedDeliveryMode = Literal["pending", "steer", "follow_up"]
PROMPT_QUEUE_EVENT_TYPE_VALUES = (
    "PROMPT_QUEUE_ACCEPTED",
    "PROMPT_QUEUE_RESERVATION_INSTALLED",
    "PROMPT_QUEUE_RESERVATION_RELEASED",
    "PROMPT_QUEUE_DELIVERY_REJECTED",
    "PROMPT_QUEUE_COMMITTED_TO_RUN",
    "PROMPT_QUEUE_COMMITTED_TO_PROVIDER_INPUT",
    "PROMPT_QUEUE_CANCELLED",
    "PROMPT_QUEUE_RECONCILIATION_REQUIRED",
    "PROMPT_QUEUE_CONTENT_RETIRED",
)
PromptQueueCompanionKind = Literal[
    "ACCEPT",
    "RESERVE",
    "RELEASE_RESERVATION",
    "COMMIT_TO_ACTIVE_RUN",
    "COMMIT_TO_NEW_RUN",
    "CANCEL",
    "DELIVERY_REJECT",
    "RECONCILIATION_LATCH",
    "CONTENT_RETIRE",
]

PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "prompt-queue-reducer-contract:v1",
    {
        "delivery_states": (
            "accepted_pending",
            "steer_reserved",
            "follow_up_reserved",
            "committed_to_active_run",
            "committed_to_new_run",
            "cancelled",
            "delivery_rejected",
            "reconciliation_required",
        ),
        "content_retention_states": ("active", "retired"),
        "ordering": "accepted_ordinal+transition_chain",
    },
)
PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT = context_fingerprint(
    "prompt-queue-event-registry:v1",
    tuple(sorted(PROMPT_QUEUE_EVENT_TYPE_VALUES)),
)
PROMPT_QUEUE_COMPANION_CHARGE_CONTRACT_FINGERPRINT = context_fingerprint(
    "prompt-queue-companion-charge-contract:v1",
    {
        "relations": (
            "prompt_queue_accounts",
            "prompt_queue_items",
            "prompt_queue_content_references",
            "prompt_queue_artifact_preparation_holds",
        ),
        "maximum_rows": 4,
        "maximum_auxiliary_payload_bytes": 64 * 1024,
    },
)
EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR = context_fingerprint(
    "prompt-queue-row-set:v1", ()
)
EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR = context_fingerprint(
    "prompt-queue-pending-head-set:v1", ()
)


def prompt_queue_transition_genesis_accumulator(runtime_session_id: str) -> str:
    return context_fingerprint(
        "prompt-queue-transition-genesis:v1",
        {
            "runtime_session_id": runtime_session_id,
            "reducer_contract_fingerprint": (PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT),
            "event_registry_fingerprint": (PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT),
        },
    )


class PromptQueueArtifactWriteReceiptIdentityFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_artifact_write_receipt_identity.v1"]
    artifact_storage_contract_fingerprint: Fingerprint
    confirmation_status: Literal["inserted", "confirmed_identical"]
    artifact_id: str
    artifact_digest: Fingerprint
    artifact_size_bytes: int = Field(ge=0, le=PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES)
    media_type: str
    semantic_metadata_fingerprint: Fingerprint
    stored_location_identity: str
    receipt_identity_fingerprint: Fingerprint


class InlineQueueContentFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_inline_content.v1"]
    content_kind: Literal["inline"]
    canonical_utf8_text: str
    canonical_payload_sha256: Fingerprint
    canonical_byte_count: int = Field(ge=0, le=PROMPT_QUEUE_INLINE_MAX_UTF8_BYTES)
    media_type: Literal["text/plain; charset=utf-8"]
    codec: Literal["utf-8"]
    content_semantic_reference: str
    inline_admission_identity: str
    content_semantic_fingerprint: Fingerprint
    content_attribution_fingerprint: Fingerprint
    content_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content(self) -> "InlineQueueContentFact":
        encoded = self.canonical_utf8_text.encode("utf-8")
        if len(encoded) != self.canonical_byte_count:
            raise ValueError("inline queue content byte count mismatch")
        if f"sha256:{sha256(encoded).hexdigest()}" != self.canonical_payload_sha256:
            raise ValueError("inline queue content digest mismatch")
        return self


class ConfirmedArtifactQueueContentFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_confirmed_artifact_content.v1"]
    content_kind: Literal["confirmed_artifact"]
    preparation_id: str
    preparation_fingerprint: Fingerprint
    preparation_hold_revision: int = Field(ge=0)
    stable_content_addressed_artifact_id: str
    artifact_identity_fingerprint: Fingerprint
    canonical_payload_sha256: Fingerprint
    canonical_byte_count: int = Field(gt=0, le=PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES)
    media_type: str
    codec: str
    artifact_semantic_reference: str
    confirmed_write_receipt_identity: PromptQueueArtifactWriteReceiptIdentityFact
    confirmed_write_receipt_fingerprint: Fingerprint
    content_semantic_fingerprint: Fingerprint
    content_attribution_fingerprint: Fingerprint
    content_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _receipt_join(self) -> "ConfirmedArtifactQueueContentFact":
        receipt = self.confirmed_write_receipt_identity
        if (
            receipt.receipt_identity_fingerprint
            != self.confirmed_write_receipt_fingerprint
            or receipt.artifact_id != self.stable_content_addressed_artifact_id
            or receipt.artifact_digest != self.canonical_payload_sha256
            or receipt.artifact_size_bytes != self.canonical_byte_count
            or receipt.media_type != self.media_type
        ):
            raise ValueError("artifact queue content receipt join mismatch")
        return self


PreparedPromptQueueContentFact: TypeAlias = Annotated[
    InlineQueueContentFact | ConfirmedArtifactQueueContentFact,
    Field(discriminator="content_kind"),
]


class UserSteerSemanticFact(FrozenFactBase):
    """Provider-visible user intent committed at an active-run safe point."""

    schema_version: Literal["user_steer_semantic.v1"]
    message_id: str = Field(min_length=1, max_length=128)
    canonical_utf8_text: str
    canonical_payload_sha256: Fingerprint
    canonical_byte_count: int = Field(ge=0, le=64 * 1024)
    observed_at_utc: str
    source_queue_content_semantic_fingerprint: Fingerprint
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content(self) -> "UserSteerSemanticFact":
        encoded = self.canonical_utf8_text.encode("utf-8")
        if (
            len(encoded) != self.canonical_byte_count
            or f"sha256:{sha256(encoded).hexdigest()}" != self.canonical_payload_sha256
        ):
            raise ValueError("user steer canonical content identity mismatch")
        return self


class PromptQueueTransitionHeadFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_transition_head.v1"]
    runtime_session_id: str
    queue_item_id: str
    accepted_ordinal: int = Field(ge=1)
    transition_ordinal: int = Field(ge=0)
    predecessor_event_reference: ContextEventReferenceFact | None
    predecessor_candidate_payload_fingerprint: Fingerprint | None
    previous_delivery_state: PromptQueueDeliveryState | None
    resulting_delivery_state: PromptQueueDeliveryState
    previous_content_retention_state: PromptQueueContentRetentionState
    resulting_content_retention_state: PromptQueueContentRetentionState
    expected_item_revision: int = Field(ge=0)
    resulting_item_revision: int = Field(ge=1)
    expected_account_revision: int = Field(ge=0)
    resulting_account_revision: int = Field(ge=1)
    transition_semantic_fingerprint: Fingerprint
    transition_attribution_fingerprint: Fingerprint
    transition_fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _chain(self) -> "PromptQueueTransitionHeadFact":
        genesis = self.transition_ordinal == 0
        if genesis != (
            self.predecessor_event_reference is None
            and self.predecessor_candidate_payload_fingerprint is None
            and self.previous_delivery_state is None
        ):
            raise ValueError("prompt queue transition predecessor matrix mismatch")
        if self.resulting_item_revision != self.expected_item_revision + 1:
            raise ValueError("prompt queue item revision is not contiguous")
        if self.resulting_account_revision != self.expected_account_revision + 1:
            raise ValueError("prompt queue account revision is not contiguous")
        return self


class PromptQueueReservationFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_reservation.v1"]
    reservation_kind: Literal["steer", "follow_up"]
    reservation_id: str
    reservation_generation: int = Field(ge=1)
    ordered_item_set_fingerprint: Fingerprint
    target_run_id: str | None
    target_safe_point: str
    absolute_deadline_utc: str
    reservation_fingerprint: Fingerprint


class PromptQueueReducerContractFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_reducer_contract.v1"]
    reducer_id: str
    reducer_version: str
    reducer_contract_fingerprint: Fingerprint


class PromptQueueEventDomainRegistryFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_event_domain_registry.v1"]
    registry_id: str
    registry_version: str
    ordered_event_type_schema_accumulator: Fingerprint
    registry_fingerprint: Fingerprint


class PromptQueueDomainCheckpointFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_domain_checkpoint.v1"]
    runtime_session_id: str
    reducer_id: str
    reducer_version: str
    reducer_contract_fingerprint: Fingerprint
    event_registry_id: str
    event_registry_version: str
    event_registry_fingerprint: Fingerprint
    checkpoint_generation: int = Field(ge=0)
    through_sequence: int = Field(ge=0)
    transition_count: int = Field(ge=0)
    transition_accumulator: Fingerprint
    account_revision: int = Field(ge=0)
    next_accepted_ordinal: int = Field(ge=1)
    pending_item_head_set_accumulator: Fingerprint
    queue_row_set_accumulator: Fingerprint
    resulting_queue_head_event_id: str | None
    resulting_queue_head_payload_fingerprint: Fingerprint | None
    checkpoint_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _genesis(self) -> "PromptQueueDomainCheckpointFact":
        if self.transition_count != self.account_revision:
            raise ValueError("prompt queue checkpoint transition/account drift")
        if self.checkpoint_generation == 0:
            if (
                self.through_sequence != 0
                or self.transition_count != 0
                or self.account_revision != 0
                or self.next_accepted_ordinal != 1
                or self.resulting_queue_head_event_id is not None
                or self.resulting_queue_head_payload_fingerprint is not None
            ):
                raise ValueError("prompt queue checkpoint genesis is malformed")
        elif (
            self.resulting_queue_head_event_id is None
            or self.resulting_queue_head_payload_fingerprint is None
        ):
            raise ValueError("non-genesis queue checkpoint lacks head identity")
        return self


def build_prompt_queue_domain_checkpoint(
    *,
    runtime_session_id: str,
    checkpoint_generation: int,
    through_sequence: int,
    transition_count: int,
    transition_accumulator: str,
    account_revision: int,
    next_accepted_ordinal: int,
    pending_item_head_set_accumulator: str,
    queue_row_set_accumulator: str,
    resulting_queue_head_event_id: str | None,
    resulting_queue_head_payload_fingerprint: str | None,
) -> PromptQueueDomainCheckpointFact:
    """Build one checkpoint from a reducer-owned atomic snapshot."""

    return build_frozen_fact(
        PromptQueueDomainCheckpointFact,
        schema_version="prompt_queue_domain_checkpoint.v1",
        runtime_session_id=runtime_session_id,
        reducer_id="pulsara.prompt_queue.reducer",
        reducer_version="1",
        reducer_contract_fingerprint=PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT,
        event_registry_id="pulsara.prompt_queue.event_registry",
        event_registry_version="1",
        event_registry_fingerprint=PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
        checkpoint_generation=checkpoint_generation,
        through_sequence=through_sequence,
        transition_count=transition_count,
        transition_accumulator=transition_accumulator,
        account_revision=account_revision,
        next_accepted_ordinal=next_accepted_ordinal,
        pending_item_head_set_accumulator=pending_item_head_set_accumulator,
        queue_row_set_accumulator=queue_row_set_accumulator,
        resulting_queue_head_event_id=resulting_queue_head_event_id,
        resulting_queue_head_payload_fingerprint=(
            resulting_queue_head_payload_fingerprint
        ),
    )


class PromptQueueHeadReceiptFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_head_receipt.v1"]
    reducer_contract_fingerprint: Fingerprint
    event_registry_fingerprint: Fingerprint
    checkpoint_generation: int = Field(ge=0)
    checkpoint_fingerprint: Fingerprint
    bounded_tail_first_sequence: int = Field(ge=0)
    bounded_tail_last_sequence: int = Field(ge=0)
    bounded_tail_count: int = Field(ge=0, le=256)
    bounded_tail_accumulator: Fingerprint
    resulting_queue_head_event_id: str | None
    resulting_queue_head_payload_fingerprint: Fingerprint | None
    resulting_account_revision: int = Field(ge=0)
    resulting_row_set_accumulator: Fingerprint
    receipt_fingerprint: Fingerprint


class PromptQueueCompanionChargeFact(FrozenFactBase):
    schema_version: Literal["prompt_queue_companion_charge.v1"]
    companion_kind: PromptQueueCompanionKind
    runtime_session_id: str
    exact_ordered_event_batch_fingerprint: Fingerprint
    item_row_mutation_count: int = Field(ge=0, le=1)
    account_row_mutation_count: Literal[1]
    content_reference_mutation_count: int = Field(ge=0, le=1)
    artifact_hold_mutation_count: int = Field(ge=0, le=1)
    total_auxiliary_row_mutations: int = Field(ge=1, le=4)
    normalized_auxiliary_payload_base_bytes: int = Field(ge=0, le=64 * 1024)
    sequence_wrapper_max_bytes: int = Field(ge=0)
    revision_wrapper_max_bytes: int = Field(ge=0)
    conservative_charged_payload_bytes: int = Field(ge=0)
    charge_contract_fingerprint: Fingerprint
    storage_mutation_plan_fingerprint: Fingerprint
    charge_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _charge(self) -> "PromptQueueCompanionChargeFact":
        expected_total = (
            self.item_row_mutation_count
            + self.account_row_mutation_count
            + self.content_reference_mutation_count
            + self.artifact_hold_mutation_count
        )
        if self.total_auxiliary_row_mutations != expected_total:
            raise ValueError("prompt queue companion row count mismatch")
        if self.conservative_charged_payload_bytes != (
            self.normalized_auxiliary_payload_base_bytes
            + self.sequence_wrapper_max_bytes
            + self.revision_wrapper_max_bytes
        ):
            raise ValueError("prompt queue companion charge recurrence mismatch")
        if (
            self.charge_contract_fingerprint
            != PROMPT_QUEUE_COMPANION_CHARGE_CONTRACT_FINGERPRINT
        ):
            raise ValueError("prompt queue companion charge contract mismatch")
        return self


class PromptQueueAccountProjectionFact(FrozenStorageFactBase):
    """Exact typed image of the mutable queue-account projection row."""

    schema_version: Literal["prompt_queue_account_projection.v1"]
    runtime_session_id: str
    next_accepted_ordinal: int = Field(ge=1)
    queue_chain_head_event_id: str | None
    queue_chain_head_sequence: int = Field(ge=0)
    queue_chain_head_payload_fingerprint: Fingerprint | None
    account_revision: int = Field(ge=0)
    checkpoint_generation: int = Field(ge=0)
    checkpoint_through_sequence: int = Field(ge=0)
    checkpoint_fingerprint: Fingerprint
    transition_count: int = Field(ge=0)
    transition_accumulator: Fingerprint
    bounded_tail_first_sequence: int = Field(ge=0)
    bounded_tail_count: int = Field(ge=0, le=256)
    bounded_tail_payload_bytes: int = Field(ge=0, le=8 * 1024 * 1024)
    bounded_tail_accumulator: Fingerprint
    pending_item_count: int = Field(ge=0)
    reserved_item_count: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)
    pending_item_head_set_accumulator: Fingerprint
    row_set_accumulator: Fingerprint
    reducer_contract_fingerprint: Fingerprint
    event_registry_fingerprint: Fingerprint
    account_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _matrix(self) -> "PromptQueueAccountProjectionFact":
        genesis = self.account_revision == 0
        head_empty = (
            self.queue_chain_head_event_id is None
            and self.queue_chain_head_sequence == 0
            and self.queue_chain_head_payload_fingerprint is None
        )
        if genesis != head_empty:
            raise ValueError("prompt queue account head/revision matrix mismatch")
        if self.checkpoint_through_sequence > self.queue_chain_head_sequence:
            raise ValueError("prompt queue checkpoint exceeds queue head")
        if self.bounded_tail_count == 0 and self.bounded_tail_first_sequence != 0:
            raise ValueError("empty prompt queue tail has a first sequence")
        if self.bounded_tail_count > 0 and self.bounded_tail_first_sequence < 1:
            raise ValueError("non-empty prompt queue tail lacks a first sequence")
        if (
            self.reducer_contract_fingerprint
            != PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT
            or self.event_registry_fingerprint
            != PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT
        ):
            raise ValueError("prompt queue account contract binding mismatch")
        return self


def build_prompt_queue_genesis_checkpoint(
    runtime_session_id: str,
) -> PromptQueueDomainCheckpointFact:
    """Build the one canonical generation-zero queue projection checkpoint."""

    return build_prompt_queue_domain_checkpoint(
        runtime_session_id=runtime_session_id,
        checkpoint_generation=0,
        through_sequence=0,
        transition_count=0,
        transition_accumulator=prompt_queue_transition_genesis_accumulator(
            runtime_session_id
        ),
        account_revision=0,
        next_accepted_ordinal=1,
        pending_item_head_set_accumulator=(
            EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR
        ),
        queue_row_set_accumulator=EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR,
        resulting_queue_head_event_id=None,
        resulting_queue_head_payload_fingerprint=None,
    )


def build_prompt_queue_genesis_account(
    runtime_session_id: str,
) -> PromptQueueAccountProjectionFact:
    checkpoint = build_prompt_queue_genesis_checkpoint(runtime_session_id)
    return build_prompt_queue_account_projection(
        runtime_session_id=runtime_session_id,
        next_accepted_ordinal=1,
        queue_chain_head_event_id=None,
        queue_chain_head_sequence=0,
        queue_chain_head_payload_fingerprint=None,
        account_revision=0,
        checkpoint_generation=0,
        checkpoint_through_sequence=0,
        checkpoint_fingerprint=checkpoint.checkpoint_fingerprint,
        transition_count=0,
        transition_accumulator=prompt_queue_transition_genesis_accumulator(
            runtime_session_id
        ),
        bounded_tail_first_sequence=0,
        bounded_tail_count=0,
        bounded_tail_payload_bytes=0,
        bounded_tail_accumulator=context_fingerprint(
            "prompt-queue-bounded-tail:v1", ()
        ),
        pending_item_count=0,
        reserved_item_count=0,
        artifact_bytes=0,
        pending_item_head_set_accumulator=(
            EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR
        ),
        row_set_accumulator=EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR,
        reducer_contract_fingerprint=PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT,
        event_registry_fingerprint=PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT,
    )


def build_prompt_queue_account_projection(
    **values: object,
) -> PromptQueueAccountProjectionFact:
    return build_frozen_storage_fact(
        PromptQueueAccountProjectionFact,
        schema_version="prompt_queue_account_projection.v1",
        **values,
    )


class PromptQueueArtifactPreparationHoldFact(FrozenStorageFactBase):
    schema_version: Literal["prompt_queue_artifact_preparation_hold.v1"]
    preparation_id: str
    runtime_session_id: str
    owner_client_submission_identity: str
    artifact_id: str
    artifact_identity_fingerprint: Fingerprint
    content_fingerprint: Fingerprint
    state: Literal["PREPARED", "CONSUMED", "RELEASED"]
    consuming_queue_item_id: str | None
    hold_revision: int = Field(ge=0)
    created_at_utc: str
    expires_at_utc: str
    confirmed_write_receipt_identity: PromptQueueArtifactWriteReceiptIdentityFact
    confirmed_write_receipt_fingerprint: Fingerprint
    preparation_fingerprint: Fingerprint
    hold_row_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _state(self) -> "PromptQueueArtifactPreparationHoldFact":
        if self.state == "PREPARED" and self.consuming_queue_item_id is not None:
            raise ValueError("prepared artifact hold cannot be consumed")
        if self.state == "CONSUMED" and not self.consuming_queue_item_id:
            raise ValueError("consumed artifact hold must retain consuming item")
        if (
            self.confirmed_write_receipt_identity.receipt_identity_fingerprint
            != self.confirmed_write_receipt_fingerprint
        ):
            raise ValueError("artifact hold write receipt fingerprint mismatch")
        return self


def prepare_inline_prompt_queue_content(
    *, text: str, inline_admission_identity: str
) -> InlineQueueContentFact:
    encoded = text.encode("utf-8")
    if len(encoded) > PROMPT_QUEUE_INLINE_MAX_UTF8_BYTES:
        raise ValueError("inline prompt queue content exceeds its frozen byte cap")
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    semantic_reference = "pulsara.canonical-utf8-text:v1"
    semantic = context_fingerprint(
        "prompt-queue-content-semantic:v1",
        {
            "canonical_payload_sha256": digest,
            "canonical_byte_count": len(encoded),
            "normalized_media_type": "text/plain; charset=utf-8",
            "codec": "utf-8",
            "content_semantic_reference": semantic_reference,
        },
    )
    attribution = context_fingerprint(
        "prompt-queue-content-attribution:v1",
        {
            "content_kind": "inline",
            "inline_admission_identity": inline_admission_identity,
        },
    )
    return build_frozen_fact(
        InlineQueueContentFact,
        schema_version="prompt_queue_inline_content.v1",
        content_kind="inline",
        canonical_utf8_text=text,
        canonical_payload_sha256=digest,
        canonical_byte_count=len(encoded),
        media_type="text/plain; charset=utf-8",
        codec="utf-8",
        content_semantic_reference=semantic_reference,
        inline_admission_identity=inline_admission_identity,
        content_semantic_fingerprint=semantic,
        content_attribution_fingerprint=attribution,
    )


for _schema, _field, _domain in (
    (
        "prompt_queue_artifact_write_receipt_identity.v1",
        "receipt_identity_fingerprint",
        "prompt-queue-artifact-write-receipt:v1",
    ),
    (
        "prompt_queue_inline_content.v1",
        "content_fact_fingerprint",
        "prompt-queue-content-fact:v1",
    ),
    (
        "prompt_queue_confirmed_artifact_content.v1",
        "content_fact_fingerprint",
        "prompt-queue-content-fact:v1",
    ),
    ("user_steer_semantic.v1", "semantic_fingerprint", "user-steer-semantic:v1"),
    (
        "prompt_queue_transition_head.v1",
        "transition_fact_fingerprint",
        "prompt-queue-transition-fact:v1",
    ),
    (
        "prompt_queue_reservation.v1",
        "reservation_fingerprint",
        "prompt-queue-reservation:v1",
    ),
    (
        "prompt_queue_reducer_contract.v1",
        "reducer_contract_fingerprint",
        "prompt-queue-reducer-contract:v1",
    ),
    (
        "prompt_queue_event_domain_registry.v1",
        "registry_fingerprint",
        "prompt-queue-event-registry:v1",
    ),
    (
        "prompt_queue_domain_checkpoint.v1",
        "checkpoint_fingerprint",
        "prompt-queue-domain-checkpoint:v1",
    ),
    (
        "prompt_queue_head_receipt.v1",
        "receipt_fingerprint",
        "prompt-queue-head-receipt:v1",
    ),
    (
        "prompt_queue_companion_charge.v1",
        "charge_fingerprint",
        "prompt-queue-companion-charge:v1",
    ),
):
    register_durable_fact(
        schema_version=_schema,
        own_fingerprint_field=_field,
        domain_separator=_domain,
    )

register_durable_storage_fact(
    schema_version="prompt_queue_artifact_preparation_hold.v1",
    own_fingerprint_field="hold_row_fingerprint",
    domain_separator="prompt-queue-artifact-preparation-hold-row:v1",
)
register_durable_storage_fact(
    schema_version="prompt_queue_account_projection.v1",
    own_fingerprint_field="account_fingerprint",
    domain_separator="prompt-queue-account-row:v1",
)


__all__ = [
    "EMPTY_PROMPT_QUEUE_PENDING_HEAD_SET_ACCUMULATOR",
    "EMPTY_PROMPT_QUEUE_ROW_SET_ACCUMULATOR",
    "PROMPT_QUEUE_COMPANION_CHARGE_CONTRACT_FINGERPRINT",
    "PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES",
    "PROMPT_QUEUE_ARTIFACT_STORAGE_CONTRACT_FINGERPRINT",
    "PROMPT_QUEUE_EVENT_REGISTRY_FINGERPRINT",
    "PROMPT_QUEUE_INLINE_MAX_UTF8_BYTES",
    "PROMPT_QUEUE_EVENT_TYPE_VALUES",
    "PROMPT_QUEUE_REDUCER_CONTRACT_FINGERPRINT",
    "ConfirmedArtifactQueueContentFact",
    "InlineQueueContentFact",
    "PreparedPromptQueueContentFact",
    "PromptQueueArtifactPreparationHoldFact",
    "PromptQueueArtifactWriteReceiptIdentityFact",
    "PromptQueueAccountProjectionFact",
    "PromptQueueCompanionChargeFact",
    "PromptQueueCompanionKind",
    "PromptQueueContentRetentionState",
    "PromptQueueDeliveryMode",
    "PromptQueueResolvedDeliveryMode",
    "PromptQueueDeliveryState",
    "PromptQueueDomainCheckpointFact",
    "PromptQueueEventDomainRegistryFact",
    "PromptQueueHeadReceiptFact",
    "PromptQueueReducerContractFact",
    "PromptQueueReservationFact",
    "PromptQueueTransitionHeadFact",
    "UserSteerSemanticFact",
    "build_prompt_queue_genesis_checkpoint",
    "build_prompt_queue_domain_checkpoint",
    "build_prompt_queue_genesis_account",
    "build_prompt_queue_account_projection",
    "prepare_inline_prompt_queue_content",
    "prompt_queue_transition_genesis_accumulator",
]
