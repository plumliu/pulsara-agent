"""Typed Host run-ingress and autonomous-notification admission contracts."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.frozen import FrozenFactBase, register_durable_fact
from pulsara_agent.primitives.runtime_observation import (
    HumanInputWireSemanticFact,
    RuntimeObservationWireSemanticFact,
    RuntimeRequestWireSemanticFact,
)
from pulsara_agent.primitives.run_entry import canonical_utc_timestamp
from pulsara_agent.primitives.terminal_observation import TerminalAutonomousDeliveryFact


Fingerprint = str


def _fact(
    schema_version: str,
    own_fingerprint_field: str,
    domain_separator: str,
):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_fingerprint_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


@_fact(
    "host_runtime_notification_attachment.v1",
    "attachment_fingerprint",
    "host-runtime-notification-attachment:v1",
)
class HostRuntimeNotificationAttachmentFact(FrozenFactBase):
    schema_version: Literal["host_runtime_notification_attachment.v1"] = (
        "host_runtime_notification_attachment.v1"
    )
    observation_wire_semantic: RuntimeObservationWireSemanticFact
    source_event_references: tuple[ContextEventReferenceFact, ...]
    wake_chain_id: str | None
    attachment_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _references(self) -> "HostRuntimeNotificationAttachmentFact":
        keys = tuple(
            (item.runtime_session_id, item.sequence, item.event_id)
            for item in self.source_event_references
        )
        if not keys or keys != tuple(sorted(set(keys))):
            raise ValueError("Host notification source references are invalid")
        return self


@_fact(
    "host_run_ingress_semantic.v1",
    "ingress_semantic_fingerprint",
    "host-run-ingress-semantic:v1",
)
class HostRunIngressSemanticFact(FrozenFactBase):
    schema_version: Literal["host_run_ingress_semantic.v1"] = (
        "host_run_ingress_semantic.v1"
    )
    ordered_current_input_semantic_fingerprints: tuple[Fingerprint, ...]
    ingress_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _items(self) -> "HostRunIngressSemanticFact":
        if not self.ordered_current_input_semantic_fingerprints:
            raise ValueError("Host run ingress requires current input")
        return self


@_fact(
    "host_ingress_item_placement.v1",
    "placement_fingerprint",
    "host-ingress-item-placement:v1",
)
class HostIngressItemPlacementFact(FrozenFactBase):
    schema_version: Literal["host_ingress_item_placement.v1"] = (
        "host_ingress_item_placement.v1"
    )
    item_kind: Literal["human_input", "runtime_notification", "runtime_request"]
    item_semantic_fingerprint: Fingerprint
    accepted_ingress_ordinal: int = Field(ge=1)
    item_ordinal: int = Field(ge=0)
    placement_fingerprint: Fingerprint


@_fact(
    "host_run_ingress_attribution.v1",
    "attribution_fingerprint",
    "host-run-ingress-attribution:v1",
)
class HostRunIngressAttributionFact(FrozenFactBase):
    schema_version: Literal["host_run_ingress_attribution.v1"] = (
        "host_run_ingress_attribution.v1"
    )
    ingress_id: str = Field(min_length=1)
    host_session_id: str = Field(min_length=1)
    conversation_id: str | None
    observed_at_utc: str
    ingress_semantic_fingerprint: Fingerprint
    ordered_item_placements: tuple[HostIngressItemPlacementFact, ...]
    attribution_fingerprint: Fingerprint

    @field_validator("observed_at_utc")
    @classmethod
    def _observed_at(cls, value: str) -> str:
        if canonical_utc_timestamp(value) != value:
            raise ValueError("Host ingress timestamp must be canonical UTC")
        return value

    @model_validator(mode="after")
    def _placements(self) -> "HostRunIngressAttributionFact":
        keys = tuple(
            (item.accepted_ingress_ordinal, item.item_ordinal)
            for item in self.ordered_item_placements
        )
        if not keys or keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("Host ingress placements are not ordered/unique")
        return self


@_fact(
    "human_run_ingress.v1",
    "fact_fingerprint",
    "human-run-ingress:v1",
)
class HumanRunIngressFact(FrozenFactBase):
    schema_version: Literal["human_run_ingress.v1"] = "human_run_ingress.v1"
    ingress_kind: Literal["human"] = "human"
    semantic_identity: HostRunIngressSemanticFact
    attribution: HostRunIngressAttributionFact
    human_message: HumanInputWireSemanticFact
    attached_runtime_notifications: tuple[HostRuntimeNotificationAttachmentFact, ...]
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _join(self) -> "HumanRunIngressFact":
        expected = (
            self.human_message.semantic_fingerprint,
            *(
                item.observation_wire_semantic.wire_semantic_fingerprint
                for item in self.attached_runtime_notifications
            ),
        )
        _validate_ingress_join(
            semantic=self.semantic_identity,
            attribution=self.attribution,
            expected=expected,
            expected_kinds=("human_input",)
            + ("runtime_notification",) * len(self.attached_runtime_notifications),
        )
        return self


@_fact(
    "runtime_request_run_ingress.v1",
    "fact_fingerprint",
    "runtime-request-run-ingress:v1",
)
class RuntimeRequestRunIngressFact(FrozenFactBase):
    schema_version: Literal["runtime_request_run_ingress.v1"] = (
        "runtime_request_run_ingress.v1"
    )
    ingress_kind: Literal["runtime_request"] = "runtime_request"
    semantic_identity: HostRunIngressSemanticFact
    attribution: HostRunIngressAttributionFact
    runtime_request: RuntimeRequestWireSemanticFact
    source_notifications: tuple[HostRuntimeNotificationAttachmentFact, ...]
    autonomy_delivery: TerminalAutonomousDeliveryFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _join(self) -> "RuntimeRequestRunIngressFact":
        if not self.source_notifications:
            raise ValueError("runtime Host ingress requires source notifications")
        expected = (
            self.runtime_request.semantic_fingerprint,
            *(
                item.observation_wire_semantic.wire_semantic_fingerprint
                for item in self.source_notifications
            ),
        )
        _validate_ingress_join(
            semantic=self.semantic_identity,
            attribution=self.attribution,
            expected=expected,
            expected_kinds=("runtime_request",)
            + ("runtime_notification",) * len(self.source_notifications),
        )
        attachments = tuple(
            item.attachment_fingerprint for item in self.source_notifications
        )
        if attachments != self.autonomy_delivery.ordered_source_attachment_fingerprints:
            raise ValueError(
                "runtime Host ingress automatic delivery attachments drifted"
            )
        chains = {item.wake_chain_id for item in self.source_notifications}
        if chains != {self.autonomy_delivery.wake_chain_id}:
            raise ValueError(
                "runtime Host ingress source notifications cross wake chains"
            )
        return self


HostRunIngressFact: TypeAlias = Annotated[
    HumanRunIngressFact | RuntimeRequestRunIngressFact,
    Field(discriminator="ingress_kind"),
]


def _validate_ingress_join(
    *,
    semantic: HostRunIngressSemanticFact,
    attribution: HostRunIngressAttributionFact,
    expected: tuple[str, ...],
    expected_kinds: tuple[str, ...],
) -> None:
    if (
        attribution.ingress_semantic_fingerprint
        != semantic.ingress_semantic_fingerprint
    ):
        raise ValueError("Host ingress semantic/attribution fingerprint mismatch")
    if semantic.ordered_current_input_semantic_fingerprints != expected:
        raise ValueError("Host ingress ordered semantic inputs mismatch")
    placements = attribution.ordered_item_placements
    if tuple(item.item_semantic_fingerprint for item in placements) != expected:
        raise ValueError("Host ingress placement semantic inputs mismatch")
    if tuple(item.item_kind for item in placements) != expected_kinds:
        raise ValueError("Host ingress placement kind mismatch")


@_fact(
    "host_autonomous_runtime_request_owner.v1",
    "owner_fingerprint",
    "host-autonomous-runtime-request-owner:v1",
)
class HostAutonomousRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["host_autonomous_runtime_request_owner.v1"] = (
        "host_autonomous_runtime_request_owner.v1"
    )
    owner_kind: Literal["host_autonomous_run_entry"] = "host_autonomous_run_entry"
    host_session_id: str = Field(min_length=1)
    conversation_id: str | None
    wake_chain_id: str = Field(min_length=1)
    ordered_attachment_fingerprints: tuple[Fingerprint, ...]
    owner_fingerprint: Fingerprint


@_fact(
    "host_ingress_admission_proof.v1",
    "admission_proof_fingerprint",
    "host-ingress-admission-proof:v1",
)
class HostIngressAdmissionProofFact(FrozenFactBase):
    schema_version: Literal["host_ingress_admission_proof.v1"] = (
        "host_ingress_admission_proof.v1"
    )
    admission_id: str = Field(min_length=1)
    admission_generation: int = Field(ge=1)
    ingress_fact_fingerprint: Fingerprint
    selected_ingress_item_ids: tuple[str, ...]
    selected_notification_head_fingerprints: tuple[Fingerprint, ...]
    expected_host_state_generation: int = Field(ge=0)
    expected_permission_policy_revision: int = Field(ge=0)
    expected_permission_policy_fingerprint: Fingerprint
    expected_close_intent_revision: int = Field(ge=0)
    expected_autonomy_chain_state_fingerprint: Fingerprint | None
    proposed_automatic_delivery_ordinal: int | None = Field(default=None, ge=1)
    admission_proof_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _automatic(self) -> "HostIngressAdmissionProofFact":
        if (self.expected_autonomy_chain_state_fingerprint is None) != (
            self.proposed_automatic_delivery_ordinal is None
        ):
            raise ValueError("Host ingress automatic admission fields are all-or-none")
        if len(self.selected_ingress_item_ids) != len(
            set(self.selected_ingress_item_ids)
        ):
            raise ValueError("Host ingress selected item IDs are not unique")
        if len(self.selected_notification_head_fingerprints) != len(
            set(self.selected_notification_head_fingerprints)
        ):
            raise ValueError("Host ingress selected notification heads are not unique")
        return self


@_fact(
    "host_ingress_coordinator_state.v1",
    "state_fingerprint",
    "host-ingress-coordinator-state:v1",
)
class HostIngressCoordinatorStateFact(FrozenFactBase):
    schema_version: Literal["host_ingress_coordinator_state.v1"] = (
        "host_ingress_coordinator_state.v1"
    )
    host_session_id: str = Field(min_length=1)
    state_generation: int = Field(ge=0)
    lifecycle_state: Literal[
        "open_idle",
        "preparing",
        "active",
        "waiting_user",
        "stopping",
        "closing",
        "closed",
        "latched",
    ]
    active_admission_id: str | None
    active_admission_generation: int | None = Field(default=None, ge=1)
    active_run_start_event_id: str | None
    permission_policy_revision: int = Field(ge=0)
    permission_policy_fingerprint: Fingerprint
    close_intent_revision: int = Field(ge=0)
    state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _active(self) -> "HostIngressCoordinatorStateFact":
        present = (
            self.active_admission_id is not None,
            self.active_admission_generation is not None,
        )
        if len(set(present)) != 1:
            raise ValueError("Host ingress active admission identity is all-or-none")
        if self.lifecycle_state in {"preparing", "active"} and not present[0]:
            raise ValueError("Host ingress preparing/active state requires admission")
        if self.lifecycle_state not in {"preparing", "active"} and present[0]:
            raise ValueError("Host ingress idle/terminal state cannot carry admission")
        if self.lifecycle_state == "active" and self.active_run_start_event_id is None:
            raise ValueError("Host ingress active state requires RunStart identity")
        if (
            self.lifecycle_state != "active"
            and self.active_run_start_event_id is not None
        ):
            raise ValueError("Host ingress non-active state cannot carry RunStart")
        return self


@_fact(
    "active_run_monitor_safe_point_commit_guard.v1",
    "guard_fingerprint",
    "active-run-monitor-safe-point-commit-guard:v1",
)
class ActiveRunMonitorSafePointCommitGuardFact(FrozenFactBase):
    schema_version: Literal["active_run_monitor_safe_point_commit_guard.v1"] = (
        "active_run_monitor_safe_point_commit_guard.v1"
    )
    runtime_session_id: str = Field(min_length=1)
    run_start_event_reference: ContextEventReferenceFact
    active_segment_id: str = Field(min_length=1)
    active_segment_generation: int = Field(ge=1)
    expected_host_state_generation: int = Field(ge=0)
    expected_next_model_call_index: int = Field(ge=0)
    expected_llm_lifecycle_generation: int = Field(ge=0)
    expected_termination_intent_revision: int = Field(ge=0)
    expected_stop_intent_revision: int = Field(ge=0)
    expected_close_intent_revision: int = Field(ge=0)
    expected_permission_policy_revision: int = Field(ge=0)
    expected_permission_policy_fingerprint: Fingerprint
    prior_model_control_disposition_reference: ContextEventReferenceFact
    previous_model_call_end_event_reference: ContextEventReferenceFact
    expected_provider_input_generation_id: str = Field(min_length=1)
    expected_provider_input_generation_revision: int = Field(ge=0)
    expected_provider_input_committed_state_fingerprint: Fingerprint
    expected_pending_interaction_frontier_fingerprint: Fingerprint
    expected_open_tool_pair_frontier_fingerprint: Fingerprint
    expected_notification_state_fingerprint: Fingerprint
    expected_selected_notification_head_fingerprints: tuple[Fingerprint, ...]
    expected_autonomy_chain_state_fingerprint: Fingerprint
    prepared_provider_input_append_fingerprint: Fingerprint
    guard_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _ledger(self) -> "ActiveRunMonitorSafePointCommitGuardFact":
        refs = (
            self.run_start_event_reference,
            self.prior_model_control_disposition_reference,
            self.previous_model_call_end_event_reference,
        )
        if any(item.runtime_session_id != self.runtime_session_id for item in refs):
            raise ValueError("active-run monitor safe-point crosses runtime ledgers")
        heads = self.expected_selected_notification_head_fingerprints
        if not heads or len(heads) != len(set(heads)):
            raise ValueError("active-run monitor safe-point heads are invalid")
        return self


@_fact(
    "host_active_run_monitor_delivery.v1",
    "delivery_fingerprint",
    "host-active-run-monitor-delivery:v1",
)
class HostActiveRunMonitorDeliveryFact(FrozenFactBase):
    schema_version: Literal["host_active_run_monitor_delivery.v1"] = (
        "host_active_run_monitor_delivery.v1"
    )
    owner_kind: Literal["host_active_run_pre_model_step"] = (
        "host_active_run_pre_model_step"
    )
    commit_guard: ActiveRunMonitorSafePointCommitGuardFact
    ordered_attachment_fingerprints: tuple[Fingerprint, ...]
    autonomy_delivery: TerminalAutonomousDeliveryFact
    delivery_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _attachments(self) -> "HostActiveRunMonitorDeliveryFact":
        if self.ordered_attachment_fingerprints != (
            self.autonomy_delivery.ordered_source_attachment_fingerprints
        ):
            raise ValueError("active-run monitor delivery attachments drifted")
        if self.autonomy_delivery.delivery_kind != "active_run_safe_point":
            raise ValueError("active-run monitor delivery kind mismatch")
        return self


@_fact(
    "human_current_input_message.v1",
    "fact_fingerprint",
    "human-current-input-message:v1",
)
class HumanCurrentInputMessageFact(FrozenFactBase):
    schema_version: Literal["human_current_input_message.v1"] = (
        "human_current_input_message.v1"
    )
    input_kind: Literal["human"] = "human"
    wire_semantic: HumanInputWireSemanticFact
    message_id: str = Field(min_length=1)
    observed_at_utc: str
    fact_fingerprint: Fingerprint

    @field_validator("observed_at_utc")
    @classmethod
    def _observed_at(cls, value: str) -> str:
        if canonical_utc_timestamp(value) != value:
            raise ValueError("Host current-input timestamp must be canonical UTC")
        return value


@_fact(
    "runtime_request_current_input_message.v1",
    "fact_fingerprint",
    "runtime-request-current-input-message:v1",
)
class RuntimeRequestCurrentInputMessageFact(FrozenFactBase):
    schema_version: Literal["runtime_request_current_input_message.v1"] = (
        "runtime_request_current_input_message.v1"
    )
    input_kind: Literal["runtime_request"] = "runtime_request"
    wire_semantic: RuntimeRequestWireSemanticFact
    message_id: str = Field(min_length=1)
    observed_at_utc: str
    fact_fingerprint: Fingerprint

    @field_validator("observed_at_utc")
    @classmethod
    def _observed_at(cls, value: str) -> str:
        if canonical_utc_timestamp(value) != value:
            raise ValueError("Host current-input timestamp must be canonical UTC")
        return value


@_fact(
    "runtime_notification_companion_message.v1",
    "fact_fingerprint",
    "runtime-notification-companion-message:v1",
)
class RuntimeNotificationCompanionMessageFact(FrozenFactBase):
    schema_version: Literal["runtime_notification_companion_message.v1"] = (
        "runtime_notification_companion_message.v1"
    )
    input_kind: Literal["runtime_notification"] = "runtime_notification"
    wire_semantic: RuntimeObservationWireSemanticFact
    attachment_fingerprint: Fingerprint
    message_id: str = Field(min_length=1)
    observed_at_utc: str
    fact_fingerprint: Fingerprint

    @field_validator("observed_at_utc")
    @classmethod
    def _observed_at(cls, value: str) -> str:
        if canonical_utc_timestamp(value) != value:
            raise ValueError("Host current-input timestamp must be canonical UTC")
        return value


HostCurrentInputMessageFact: TypeAlias = Annotated[
    HumanCurrentInputMessageFact
    | RuntimeRequestCurrentInputMessageFact
    | RuntimeNotificationCompanionMessageFact,
    Field(discriminator="input_kind"),
]


__all__ = [
    name
    for name in globals()
    if name.startswith("Host")
    or name.startswith("Human")
    or name.startswith("Runtime")
    or name.startswith("Active")
]
