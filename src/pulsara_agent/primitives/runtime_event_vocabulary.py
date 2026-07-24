"""Typed, bounded facts for runtime audit and MCP lifecycle events.

This module is deliberately below the event and runtime layers. Durable facts
live here; process-local prepared carriers are marked with
``FrozenRuntimeStateBase`` and must never be serialized as authority.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import Field, field_validator, model_validator

from pulsara_agent.message.blocks import ToolResultBlock
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    FrozenRuntimeStateBase,
    register_durable_fact,
)
from pulsara_agent.primitives.mcp import McpBindingIdentityFact


MAX_RUNTIME_IDENTIFIER_BYTES = 512
MAX_RUNTIME_NAME_BYTES = 256
MAX_RUNTIME_ERROR_TYPE_BYTES = 128
MAX_RUNTIME_DIAGNOSTIC_BYTES = 1_024
MAX_MCP_RESPONSE_KEYS = 64
MAX_MCP_INPUT_REQUESTS = 64
MAX_MCP_INPUT_REQUEST_BYTES = 64 * 1_024
MAX_MCP_PREPARED_RESPONSE_BYTES = 64 * 1_024
MAX_TOOL_RESULT_RECEIPT_ITEMS = 128
MAX_PUBLICATION_TERMINATION_REFS = 16


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


def _bounded_utf8(value: str, *, maximum: int, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds its UTF-8 byte bound")
    return value


def ordered_fingerprint_accumulator(domain: str, values: tuple[str, ...]) -> str:
    accumulator = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        accumulator = context_fingerprint(
            f"{domain}:step",
            (accumulator, value),
        )
    return accumulator


def stable_runtime_event_id(domain: str, *parts: object) -> str:
    return context_fingerprint(domain, parts).removeprefix("sha256:")


@_fact(
    "mcp_input_required_interaction.v1",
    "interaction_semantic_fingerprint",
    "mcp-input-required-interaction:v1",
)
class McpInputRequiredInteractionSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_interaction.v1"] = (
        "mcp_input_required_interaction.v1"
    )
    interaction_id: str
    tool_call_id: str
    tool_name: str
    server_id: str
    round_count: int = Field(ge=1)
    interaction_semantic_fingerprint: str

    @field_validator("interaction_id", "tool_call_id")
    @classmethod
    def _identifier_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_IDENTIFIER_BYTES,
            label="MCP interaction identity",
        )

    @field_validator("tool_name", "server_id")
    @classmethod
    def _name_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_NAME_BYTES,
            label="MCP tool/server name",
        )


@_fact(
    "mcp_user_visible_input_request.v1",
    "request_fingerprint",
    "mcp-user-visible-input-request:v1",
)
class McpUserVisibleInputRequestFact(FrozenFactBase):
    schema_version: Literal["mcp_user_visible_input_request.v1"] = (
        "mcp_user_visible_input_request.v1"
    )
    key: str
    method: str
    user_visible_params: FrozenJsonObjectFact
    params_semantic_fingerprint: str
    request_fingerprint: str

    @field_validator("key")
    @classmethod
    def _key_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_NAME_BYTES,
            label="MCP input request key",
        )

    @field_validator("method")
    @classmethod
    def _method_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_NAME_BYTES,
            label="MCP input request method",
        )

    @model_validator(mode="after")
    def _params_fingerprint(self) -> "McpUserVisibleInputRequestFact":
        expected = context_fingerprint(
            "mcp-user-visible-input-request-params:v1",
            self.user_visible_params,
        )
        if self.params_semantic_fingerprint != expected:
            raise ValueError("MCP input request params fingerprint mismatch")
        return self


@_fact(
    "mcp_input_required_request_envelope.v1",
    "request_envelope_semantic_fingerprint",
    "mcp-input-required-request-envelope:v1",
)
class McpInputRequiredRequestEnvelopeFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_request_envelope.v1"] = (
        "mcp_input_required_request_envelope.v1"
    )
    protocol_version: str | None
    ordered_user_visible_input_requests: tuple[
        McpUserVisibleInputRequestFact, ...
    ] = Field(max_length=MAX_MCP_INPUT_REQUESTS)
    original_request_semantic_fingerprint: str
    request_state_semantic_fingerprint: str | None
    request_envelope_semantic_fingerprint: str

    @model_validator(mode="after")
    def _request_set(self) -> "McpInputRequiredRequestEnvelopeFact":
        keys = tuple(item.key for item in self.ordered_user_visible_input_requests)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("MCP input request keys must be sorted and unique")
        payload = canonical_json_bytes(
            tuple(
                item.model_dump(mode="json")
                for item in self.ordered_user_visible_input_requests
            )
        )
        if len(payload) > MAX_MCP_INPUT_REQUEST_BYTES:
            raise ValueError("MCP input request envelope exceeds its byte bound")
        return self


@_fact(
    "mcp_pending_lease_reservation_identity.v1",
    "reservation_fingerprint",
    "mcp-pending-lease-reservation-identity:v1",
)
class McpPendingLeaseReservationIdentityFact(FrozenFactBase):
    schema_version: Literal["mcp_pending_lease_reservation_identity.v1"] = (
        "mcp_pending_lease_reservation_identity.v1"
    )
    reservation_id: str
    interaction_id: str
    binding_identity: McpBindingIdentityFact
    reservation_fingerprint: str

    @field_validator("reservation_id", "interaction_id")
    @classmethod
    def _identity_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_IDENTIFIER_BYTES,
            label="MCP reservation identity",
        )


@_fact(
    "mcp_input_required_suspension.v1",
    "suspension_fact_fingerprint",
    "mcp-input-required-suspension:v1",
)
class McpInputRequiredSuspensionFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_suspension.v1"] = (
        "mcp_input_required_suspension.v1"
    )
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    source_mcp_installation_id: str
    durable_deadline_utc: str | None
    deadline_policy_fingerprint: str
    predecessor_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    suspension_fact_fingerprint: str

    @field_validator(
        "rollout_reservation_id",
        "source_mcp_installation_id",
    )
    @classmethod
    def _identity_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_IDENTIFIER_BYTES,
            label="MCP suspension identity",
        )

    @field_validator("durable_deadline_utc")
    @classmethod
    def _deadline_utc(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("MCP durable deadline must be timezone-aware")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @model_validator(mode="after")
    def _source_join(self) -> "McpInputRequiredSuspensionFact":
        reservation = self.pending_lease_reservation
        if (
            reservation.interaction_id != self.interaction.interaction_id
            or reservation.binding_identity != self.binding_identity
        ):
            raise ValueError("MCP suspension pending lease identity mismatch")
        predecessor = self.predecessor_resolution_submitted_event_reference
        if (self.interaction.round_count == 1) != (predecessor is None):
            raise ValueError("MCP suspension predecessor/round matrix mismatch")
        return self


class PreparedMcpInputRequiredSuspension(FrozenRuntimeStateBase):
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    owned_original_request_json_bytes: bytes
    owned_request_state_json_bytes: bytes | None
    deadline_monotonic: float | None
    prepared_suspension_fingerprint: str
    tool_observation_timing_seed: FrozenJsonObjectFact | None = None

    @model_validator(mode="after")
    def _prepared_source(self) -> "PreparedMcpInputRequiredSuspension":
        if (
            self.pending_lease_reservation.interaction_id
            != self.interaction.interaction_id
            or self.pending_lease_reservation.binding_identity
            != self.binding_identity
        ):
            raise ValueError("prepared MCP suspension lease identity mismatch")
        if (
            context_fingerprint(
                "mcp-original-request:v1",
                self.owned_original_request_json_bytes.decode("utf-8"),
            )
            != self.request_envelope.original_request_semantic_fingerprint
        ):
            raise ValueError("prepared MCP original request fingerprint mismatch")
        state_fingerprint = (
            context_fingerprint(
                "mcp-request-state:v1",
                self.owned_request_state_json_bytes.decode("utf-8"),
            )
            if self.owned_request_state_json_bytes is not None
            else None
        )
        if state_fingerprint != self.request_envelope.request_state_semantic_fingerprint:
            raise ValueError("prepared MCP request state fingerprint mismatch")
        return self

    def thaw_original_request(self) -> dict[str, Any]:
        value = json.loads(self.owned_original_request_json_bytes.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("prepared MCP original request is not an object")
        return value

    def thaw_request_state(self) -> str | None:
        if self.owned_request_state_json_bytes is None:
            return None
        value = json.loads(self.owned_request_state_json_bytes.decode("utf-8"))
        if not isinstance(value, str):
            raise ValueError("prepared MCP request state is not a string")
        return value


@_fact(
    "mcp_input_required_source_authority.v1",
    "source_authority_fingerprint",
    "mcp-input-required-source-authority:v1",
)
class McpInputRequiredSourceAuthorityFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_source_authority.v1"] = (
        "mcp_input_required_source_authority.v1"
    )
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope_semantic_fingerprint: str
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    source_mcp_installation_id: str
    durable_deadline_utc: str | None
    deadline_policy_fingerprint: str
    predecessor_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    source_suspension_fact_fingerprint: str
    source_suspension_event_reference: ContextEventReferenceFact
    original_run_start_event_reference: ContextEventReferenceFact
    source_authority_fingerprint: str

    @model_validator(mode="after")
    def _ledger_join(self) -> "McpInputRequiredSourceAuthorityFact":
        references = (
            self.source_suspension_event_reference,
            self.original_run_start_event_reference,
        )
        runtime_ids = {item.runtime_session_id for item in references}
        if len(runtime_ids) != 1:
            raise ValueError("MCP source authority crosses runtime ledgers")
        if self.source_suspension_event_reference.event_type != (
            "TOOL_EXECUTION_SUSPENDED"
        ):
            raise ValueError("MCP source authority requires a suspension event")
        if self.original_run_start_event_reference.event_type != "RUN_START":
            raise ValueError("MCP source authority requires a RunStart event")
        if (
            self.pending_lease_reservation.interaction_id
            != self.interaction.interaction_id
            or self.pending_lease_reservation.binding_identity
            != self.binding_identity
        ):
            raise ValueError("MCP source authority pending lease mismatch")
        return self


@_fact(
    "mcp_input_required_resolution.v1",
    "resolution_semantic_fingerprint",
    "mcp-input-required-resolution:v1",
)
class McpInputRequiredResolutionSemanticFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_resolution.v1"] = (
        "mcp_input_required_resolution.v1"
    )
    cancelled: bool
    ordered_response_keys: tuple[str, ...] = Field(max_length=MAX_MCP_RESPONSE_KEYS)
    response_payload_receipt_fingerprint: str
    resolution_semantic_fingerprint: str

    @model_validator(mode="after")
    def _keys(self) -> "McpInputRequiredResolutionSemanticFact":
        if self.ordered_response_keys != tuple(
            sorted(set(self.ordered_response_keys))
        ):
            raise ValueError("MCP response keys must be sorted and unique")
        for key in self.ordered_response_keys:
            _bounded_utf8(
                key,
                maximum=MAX_RUNTIME_NAME_BYTES,
                label="MCP response key",
            )
        return self


class PreparedMcpResponseEntry(FrozenRuntimeStateBase):
    key: str
    canonical_response_json_bytes: bytes
    response_semantic_fingerprint: str


class PreparedMcpInputRequiredResolution(FrozenRuntimeStateBase):
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension_fact_fingerprint: str
    interaction_id: str
    cancelled: bool
    ordered_response_entries: tuple[PreparedMcpResponseEntry, ...]
    resolution_semantic: McpInputRequiredResolutionSemanticFact
    prepared_resolution_fingerprint: str

    @model_validator(mode="after")
    def _prepared_join(self) -> "PreparedMcpInputRequiredResolution":
        keys = tuple(item.key for item in self.ordered_response_entries)
        if keys != self.resolution_semantic.ordered_response_keys:
            raise ValueError("prepared MCP response keys drifted")
        total_bytes = sum(
            len(item.canonical_response_json_bytes)
            for item in self.ordered_response_entries
        )
        if total_bytes > MAX_MCP_PREPARED_RESPONSE_BYTES:
            raise ValueError("prepared MCP response payload exceeds its byte bound")
        return self

    def thaw_responses(self) -> dict[str, Any]:
        return {
            item.key: json.loads(item.canonical_response_json_bytes.decode("utf-8"))
            for item in self.ordered_response_entries
        }


@_fact(
    "mcp_input_required_resolution_attempt.v1",
    "attempt_fingerprint",
    "mcp-input-required-resolution-attempt:v1",
)
class McpInputRequiredResolutionAttemptFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_resolution_attempt.v1"] = (
        "mcp_input_required_resolution_attempt.v1"
    )
    round_count: int = Field(ge=1)
    attempt_ordinal: int = Field(ge=1)
    predecessor_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    predecessor_resume_failed_event_reference: ContextEventReferenceFact | None
    attempt_fingerprint: str

    @model_validator(mode="after")
    def _attempt_chain(self) -> "McpInputRequiredResolutionAttemptFact":
        predecessors = (
            self.predecessor_resolution_submitted_event_reference,
            self.predecessor_resume_failed_event_reference,
        )
        if self.attempt_ordinal == 1:
            if any(item is not None for item in predecessors):
                raise ValueError("first MCP resolution attempt cannot have predecessors")
        elif any(item is None for item in predecessors):
            raise ValueError("retried MCP resolution requires both predecessors")
        return self


@_fact(
    "bounded_runtime_failure_diagnostic.v1",
    "diagnostic_fingerprint",
    "bounded-runtime-failure-diagnostic:v1",
)
class BoundedRuntimeFailureDiagnosticFact(FrozenFactBase):
    schema_version: Literal["bounded_runtime_failure_diagnostic.v1"] = (
        "bounded_runtime_failure_diagnostic.v1"
    )
    error_type: str
    redacted_message: str
    redaction_profile_id: str
    redaction_contract_fingerprint: str
    diagnostic_fingerprint: str

    @field_validator("error_type")
    @classmethod
    def _error_type_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_ERROR_TYPE_BYTES,
            label="runtime diagnostic error type",
        )

    @field_validator("redacted_message")
    @classmethod
    def _message_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_RUNTIME_DIAGNOSTIC_BYTES:
            raise ValueError("runtime diagnostic message exceeds its byte bound")
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("runtime diagnostic contains unsupported control characters")
        return value


@_fact(
    "mcp_input_required_terminal_source.v1",
    "source_fingerprint",
    "mcp-input-required-terminal-source:v1",
)
class McpInputRequiredTerminalSourceFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_terminal_source.v1"] = (
        "mcp_input_required_terminal_source.v1"
    )
    source_suspension_event_reference: ContextEventReferenceFact
    source_resolution_submitted_event_reference: (
        ContextEventReferenceFact | None
    )
    source_fingerprint: str

    @model_validator(mode="after")
    def _source_types(self) -> "McpInputRequiredTerminalSourceFact":
        if self.source_suspension_event_reference.event_type != (
            "TOOL_EXECUTION_SUSPENDED"
        ):
            raise ValueError("MCP terminal source requires a suspension reference")
        resolution = self.source_resolution_submitted_event_reference
        if resolution is not None and resolution.event_type != (
            "MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED"
        ):
            raise ValueError("MCP terminal source resolution reference is invalid")
        if resolution is not None and (
            resolution.runtime_session_id
            != self.source_suspension_event_reference.runtime_session_id
        ):
            raise ValueError("MCP terminal source crosses runtime ledgers")
        return self


@_fact(
    "publication_latched_run_termination.v1",
    "termination_fact_fingerprint",
    "publication-latched-run-termination:v1",
)
class PublicationLatchedRunTerminationFact(FrozenFactBase):
    schema_version: Literal["publication_latched_run_termination.v1"] = (
        "publication_latched_run_termination.v1"
    )
    reason: Literal[
        "mcp_active_interaction_publication_unavailable",
        "mcp_terminal_disposition_publication_unavailable",
        "mcp_closure_publication_unavailable",
        "mandatory_runtime_audit_publication_unavailable",
        "compaction_publication_unavailable",
    ]
    source_event_references: tuple[ContextEventReferenceFact, ...] = Field(
        min_length=1,
        max_length=MAX_PUBLICATION_TERMINATION_REFS,
    )
    source_events_accumulator: str
    termination_fact_fingerprint: str

    @model_validator(mode="after")
    def _ordered_sources(self) -> "PublicationLatchedRunTerminationFact":
        sequences = tuple(item.sequence for item in self.source_event_references)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("publication termination refs must be ordered and unique")
        runtime_ids = {
            item.runtime_session_id for item in self.source_event_references
        }
        if len(runtime_ids) != 1:
            raise ValueError("publication termination refs cross runtime ledgers")
        expected = ordered_fingerprint_accumulator(
            "publication-latched-run-termination-sources:v1",
            tuple(item.payload_fingerprint for item in self.source_event_references),
        )
        if self.source_events_accumulator != expected:
            raise ValueError("publication termination source accumulator mismatch")
        return self


@_fact(
    "context_compaction_request.v1",
    "request_semantic_fingerprint",
    "context-compaction-request:v1",
)
class ContextCompactionRequestFact(FrozenFactBase):
    schema_version: Literal["context_compaction_request.v1"] = (
        "context_compaction_request.v1"
    )
    source: Literal["memory_hook_should_compact"] = "memory_hook_should_compact"
    safe_point: Literal["after_tool_results"] = "after_tool_results"
    basis_tool_result_terminal_event_references: tuple[
        ContextEventReferenceFact, ...
    ] = Field(min_length=1, max_length=MAX_TOOL_RESULT_RECEIPT_ITEMS)
    basis_event_ids_accumulator: str
    request_semantic_fingerprint: str

    @model_validator(mode="after")
    def _basis(self) -> "ContextCompactionRequestFact":
        if any(
            item.event_type != "TOOL_RESULT_END"
            for item in self.basis_tool_result_terminal_event_references
        ):
            raise ValueError("compaction request basis requires ToolResultEnd refs")
        sequences = tuple(
            item.sequence
            for item in self.basis_tool_result_terminal_event_references
        )
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("compaction request basis refs must be ordered and unique")
        expected = ordered_fingerprint_accumulator(
            "context-compaction-request-basis:v1",
            tuple(
                item.event_id
                for item in self.basis_tool_result_terminal_event_references
            ),
        )
        if self.basis_event_ids_accumulator != expected:
            raise ValueError("compaction request basis accumulator mismatch")
        return self


class CurrentToolResultReceiptItem(FrozenRuntimeStateBase):
    result_block: ToolResultBlock
    tool_result_end_reference: ContextEventReferenceFact
    terminal_projection_reference: ContextEventReferenceFact
    tool_call_id: str
    result_semantic_fingerprint: str
    item_fingerprint: str

    @model_validator(mode="after")
    def _identity(self) -> "CurrentToolResultReceiptItem":
        if (
            self.result_block.id != self.tool_call_id
            or self.tool_result_end_reference.event_type != "TOOL_RESULT_END"
            or self.terminal_projection_reference.event_type
            != "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED"
        ):
            raise ValueError("current ToolResult receipt identity mismatch")
        if (
            self.tool_result_end_reference.runtime_session_id
            != self.terminal_projection_reference.runtime_session_id
            or self.terminal_projection_reference.sequence
            >= self.tool_result_end_reference.sequence
        ):
            raise ValueError("current ToolResult receipt reference ordering mismatch")
        expected = context_fingerprint(
            "current-tool-result-receipt-item:v1",
            self.model_dump(mode="json", exclude={"item_fingerprint"}),
        )
        if self.item_fingerprint != expected:
            raise ValueError("current ToolResult receipt fingerprint mismatch")
        return self


class CurrentToolResultBatchReceipt(FrozenRuntimeStateBase):
    ordered_items: tuple[CurrentToolResultReceiptItem, ...] = Field(
        min_length=1,
        max_length=MAX_TOOL_RESULT_RECEIPT_ITEMS,
    )
    ordered_item_fingerprints_accumulator: str

    @model_validator(mode="after")
    def _ordered(self) -> "CurrentToolResultBatchReceipt":
        expected = ordered_fingerprint_accumulator(
            "current-tool-result-batch:v1",
            tuple(item.item_fingerprint for item in self.ordered_items),
        )
        if self.ordered_item_fingerprints_accumulator != expected:
            raise ValueError("current ToolResult batch accumulator mismatch")
        call_ids = tuple(item.tool_call_id for item in self.ordered_items)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("current ToolResult batch contains duplicate calls")
        return self


@_fact(
    "mid_turn_context_compaction_skip.v1",
    "skip_semantic_fingerprint",
    "mid-turn-context-compaction-skip:v1",
)
class MidTurnCompactionSkipFact(FrozenFactBase):
    schema_version: Literal["mid_turn_context_compaction_skip.v1"] = (
        "mid_turn_context_compaction_skip.v1"
    )
    reason: Literal[
        "current_run_start_missing",
        "no_compactable_prefix_before_current_run",
        "current_run_tail_missing",
        "current_run_rendered_tail_missing",
    ]
    current_run_start_event_reference: ContextEventReferenceFact | None
    safe_point: Literal["before_followup_model_call"] = "before_followup_model_call"
    skip_semantic_fingerprint: str

    @model_validator(mode="after")
    def _run_start_matrix(self) -> "MidTurnCompactionSkipFact":
        missing = self.reason == "current_run_start_missing"
        if missing != (self.current_run_start_event_reference is None):
            raise ValueError("mid-turn skip RunStart reference matrix mismatch")
        if (
            self.current_run_start_event_reference is not None
            and self.current_run_start_event_reference.event_type != "RUN_START"
        ):
            raise ValueError("mid-turn skip must reference RunStart")
        return self


@_fact(
    "tool_result_evidence_projection_source.v1",
    "source_fingerprint",
    "tool-result-evidence-projection-source:v1",
)
class ToolResultEvidenceProjectionSourceFact(FrozenFactBase):
    schema_version: Literal["tool_result_evidence_projection_source.v1"] = (
        "tool_result_evidence_projection_source.v1"
    )
    tool_call_id: str
    tool_result_end_reference: ContextEventReferenceFact
    terminal_projection_reference: ContextEventReferenceFact
    result_semantic_fingerprint: str
    source_fingerprint: str

    @model_validator(mode="after")
    def _source_join(self) -> "ToolResultEvidenceProjectionSourceFact":
        if (
            self.tool_result_end_reference.event_type != "TOOL_RESULT_END"
            or self.terminal_projection_reference.event_type
            != "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED"
            or self.tool_result_end_reference.runtime_session_id
            != self.terminal_projection_reference.runtime_session_id
        ):
            raise ValueError("evidence projection source references are invalid")
        return self


@_fact(
    "tool_result_evidence_projection_failure.v1",
    "failure_semantic_fingerprint",
    "tool-result-evidence-projection-failure:v1",
)
class ToolResultEvidenceProjectionFailureFact(FrozenFactBase):
    schema_version: Literal["tool_result_evidence_projection_failure.v1"] = (
        "tool_result_evidence_projection_failure.v1"
    )
    projection_contract_id: Literal["execution_evidence_persistence"] = (
        "execution_evidence_persistence"
    )
    projection_contract_version: Literal["1"] = "1"
    ordered_tool_result_sources: tuple[
        ToolResultEvidenceProjectionSourceFact, ...
    ] = Field(min_length=1, max_length=MAX_TOOL_RESULT_RECEIPT_ITEMS)
    ordered_source_fingerprints_accumulator: str
    diagnostic: BoundedRuntimeFailureDiagnosticFact
    failure_semantic_fingerprint: str

    @model_validator(mode="after")
    def _sources(self) -> "ToolResultEvidenceProjectionFailureFact":
        expected = ordered_fingerprint_accumulator(
            "tool-result-evidence-projection-sources:v1",
            tuple(item.source_fingerprint for item in self.ordered_tool_result_sources),
        )
        if self.ordered_source_fingerprints_accumulator != expected:
            raise ValueError("evidence projection source accumulator mismatch")
        return self


class RuntimeEventOperationDeadlineBudget(FrozenRuntimeStateBase):
    admitted_at_monotonic: float = Field(gt=0)
    ordinary_deadline_monotonic: float = Field(gt=0)
    terminal_deadline_monotonic: float = Field(gt=0)
    terminal_reserve_seconds: float = Field(gt=0)
    budget_fingerprint: str

    @model_validator(mode="after")
    def _deadline_order(self) -> "RuntimeEventOperationDeadlineBudget":
        if not (
            self.admitted_at_monotonic
            < self.ordinary_deadline_monotonic
            < self.terminal_deadline_monotonic
        ):
            raise ValueError("runtime event deadline budget is not ordered")
        expected = context_fingerprint(
            "runtime-event-operation-deadline-budget:v1",
            self.model_dump(mode="json", exclude={"budget_fingerprint"}),
        )
        if self.budget_fingerprint != expected:
            raise ValueError("runtime event deadline budget fingerprint mismatch")
        return self


class CompactionPublicationTerminalizationScope(FrozenRuntimeStateBase):
    scope_kind: Literal[
        "pre_run_without_active_run",
        "manual_without_active_run",
        "mid_turn_active_run",
    ]
    runtime_session_id: str
    active_run_id: str | None
    active_context_window_id: str | None
    active_rollout_account_id: str | None
    host_state_generation: int = Field(ge=0)
    scope_fingerprint: str

    @model_validator(mode="after")
    def _scope(self) -> "CompactionPublicationTerminalizationScope":
        active = (
            self.active_run_id,
            self.active_context_window_id,
            self.active_rollout_account_id,
        )
        if self.scope_kind == "mid_turn_active_run":
            if any(item is None for item in active):
                raise ValueError("mid-turn compaction scope requires active identities")
        elif any(item is not None for item in active):
            raise ValueError("no-active-run compaction scope cannot carry active identities")
        expected = context_fingerprint(
            "compaction-publication-terminalization-scope:v1",
            self.model_dump(mode="json", exclude={"scope_fingerprint"}),
        )
        if self.scope_fingerprint != expected:
            raise ValueError("compaction terminalization scope fingerprint mismatch")
        return self


class CompactionCandidateProjectionRequestIdentity(FrozenRuntimeStateBase):
    request_id: str
    compaction_id: str
    expected_completed_event_id: str
    extractor_id: str
    extractor_version: str
    extractor_contract_fingerprint: str
    projection_policy_fingerprint: str
    request_fingerprint: str

    @model_validator(mode="after")
    def _request_identity(self) -> "CompactionCandidateProjectionRequestIdentity":
        expected = context_fingerprint(
            "compaction-candidate-projection-request:v1",
            self.model_dump(mode="json", exclude={"request_fingerprint"}),
        )
        if self.request_fingerprint != expected:
            raise ValueError("compaction projection request fingerprint mismatch")
        return self


class PreparedCompactionCandidateProjectionInput(FrozenRuntimeStateBase):
    request_identity: CompactionCandidateProjectionRequestIdentity
    owner_id: str
    summary_artifact_id: str
    summary_artifact_content_fingerprint: str
    owned_summary_canonical_utf8_bytes: bytes
    prepared_input_fingerprint: str

    @model_validator(mode="after")
    def _prepared_input(self) -> "PreparedCompactionCandidateProjectionInput":
        try:
            summary = self.owned_summary_canonical_utf8_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("prepared compaction summary is not UTF-8") from exc
        if len(self.owned_summary_canonical_utf8_bytes) > 256 * 1_024:
            raise ValueError("prepared compaction summary exceeds its byte bound")
        if (
            context_fingerprint(
                "compaction-summary-artifact-content:v1",
                summary,
            )
            != self.summary_artifact_content_fingerprint
        ):
            raise ValueError("prepared compaction summary content drifted")
        expected = context_fingerprint(
            "prepared-compaction-candidate-projection-input:v1",
            {
                **self.model_dump(
                    mode="json",
                    exclude={
                        "prepared_input_fingerprint",
                        "owned_summary_canonical_utf8_bytes",
                    },
                ),
                "owned_summary_canonical_utf8": summary,
            },
        )
        if self.prepared_input_fingerprint != expected:
            raise ValueError("prepared compaction projection fingerprint mismatch")
        return self


CompactionCandidateProjectionStatus: TypeAlias = Literal[
    "not_requested",
    "preparation_failed",
    "owner_installation_failed",
    "suppressed_by_publication_latch",
    "owner_installed",
    "candidate_frozen",
    "producer_bundle_full",
    "projection_applied",
    "reconciliation_required",
]


class CompactionCandidateProjectionReceipt(FrozenRuntimeStateBase):
    completed_compaction_event_reference: ContextEventReferenceFact
    request_identity: CompactionCandidateProjectionRequestIdentity | None
    status: CompactionCandidateProjectionStatus
    owner_id: str | None
    prepared_input_fingerprint: str | None
    failure_stage: Literal["prepared_input_factory", "owner_installation"] | None
    failure_diagnostic: BoundedRuntimeFailureDiagnosticFact | None
    producer_event_id: str | None
    producer_payload_fingerprint: str | None
    producer_event_reference: ContextEventReferenceFact | None
    outbox_item_accumulator: str | None
    reconciliation_from_status: Literal[
        "owner_installed",
        "candidate_frozen",
        "producer_bundle_full",
        "projection_applied",
    ] | None

    @model_validator(mode="after")
    def _status_matrix(self) -> "CompactionCandidateProjectionReceipt":
        request_required = self.status != "not_requested"
        if request_required != (self.request_identity is not None):
            raise ValueError("compaction projection request identity matrix mismatch")
        failure = self.status in {"preparation_failed", "owner_installation_failed"}
        if failure != (
            self.failure_stage is not None and self.failure_diagnostic is not None
        ):
            raise ValueError("compaction projection failure field matrix mismatch")
        if self.status == "preparation_failed":
            if (
                self.failure_stage != "prepared_input_factory"
                or self.owner_id is not None
                or self.prepared_input_fingerprint is not None
            ):
                raise ValueError("compaction projection preparation failure drifted")
        if self.status == "owner_installation_failed":
            if (
                self.failure_stage != "owner_installation"
                or self.owner_id is not None
                or self.prepared_input_fingerprint is None
            ):
                raise ValueError("compaction projection owner failure drifted")
        owner_statuses = {
            "owner_installed",
            "candidate_frozen",
            "producer_bundle_full",
            "projection_applied",
            "reconciliation_required",
        }
        if (self.status in owner_statuses) != (
            self.owner_id is not None
            and self.prepared_input_fingerprint is not None
        ):
            raise ValueError("compaction projection owner field matrix mismatch")
        producer_frozen = self.status in {
            "candidate_frozen",
            "producer_bundle_full",
            "projection_applied",
        } or (
            self.status == "reconciliation_required"
            and self.reconciliation_from_status
            in {"candidate_frozen", "producer_bundle_full", "projection_applied"}
        )
        if producer_frozen != (
            self.producer_event_id is not None
            and self.producer_payload_fingerprint is not None
        ):
            raise ValueError("compaction projection producer identity matrix mismatch")
        durable = self.status in {"producer_bundle_full", "projection_applied"} or (
            self.status == "reconciliation_required"
            and self.reconciliation_from_status
            in {"producer_bundle_full", "projection_applied"}
        )
        if durable != (
            self.producer_event_reference is not None
            and self.outbox_item_accumulator is not None
        ):
            raise ValueError("compaction projection durable receipt matrix mismatch")
        if (self.status == "reconciliation_required") != (
            self.reconciliation_from_status is not None
        ):
            raise ValueError("compaction projection reconciliation matrix mismatch")
        return self


def build_mcp_interaction_semantic(
    *,
    interaction_id: str,
    tool_call_id: str,
    tool_name: str,
    server_id: str,
    round_count: int,
) -> McpInputRequiredInteractionSemanticFact:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    return build_frozen_fact(
        McpInputRequiredInteractionSemanticFact,
        schema_version="mcp_input_required_interaction.v1",
        interaction_id=interaction_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        server_id=server_id,
        round_count=round_count,
    )


def build_mcp_user_visible_request(
    *,
    key: str,
    method: str,
    params: Mapping[str, Any],
) -> McpUserVisibleInputRequestFact:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    frozen = freeze_json(dict(params))
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise TypeError("MCP input request params must be a JSON object")
    return build_frozen_fact(
        McpUserVisibleInputRequestFact,
        schema_version="mcp_user_visible_input_request.v1",
        key=key,
        method=method,
        user_visible_params=frozen,
        params_semantic_fingerprint=context_fingerprint(
            "mcp-user-visible-input-request-params:v1",
            frozen,
        ),
    )


def prepare_mcp_input_required_suspension(
    *,
    interaction_id: str,
    tool_call_id: str,
    tool_name: str,
    server_id: str,
    round_count: int,
    binding_identity: McpBindingIdentityFact,
    pending_lease_reservation_id: str,
    protocol_version: str | None,
    input_requests: tuple[Mapping[str, Any], ...],
    original_request: Mapping[str, Any],
    request_state: str | None,
    deadline_monotonic: float | None,
) -> PreparedMcpInputRequiredSuspension:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    interaction = build_mcp_interaction_semantic(
        interaction_id=interaction_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        server_id=server_id,
        round_count=round_count,
    )
    reservation = build_frozen_fact(
        McpPendingLeaseReservationIdentityFact,
        schema_version="mcp_pending_lease_reservation_identity.v1",
        reservation_id=pending_lease_reservation_id,
        interaction_id=interaction_id,
        binding_identity=binding_identity,
    )
    requests = tuple(
        sorted(
            (
                build_mcp_user_visible_request(
                    key=str(item["key"]),
                    method=str(item["method"]),
                    params=dict(item.get("params") or {}),
                )
                for item in input_requests
            ),
            key=lambda item: item.key,
        )
    )
    original_bytes = canonical_json_bytes(dict(original_request))
    request_state_bytes = (
        canonical_json_bytes(request_state) if request_state is not None else None
    )
    envelope = build_frozen_fact(
        McpInputRequiredRequestEnvelopeFact,
        schema_version="mcp_input_required_request_envelope.v1",
        protocol_version=protocol_version,
        ordered_user_visible_input_requests=requests,
        original_request_semantic_fingerprint=context_fingerprint(
            "mcp-original-request:v1",
            original_bytes.decode("utf-8"),
        ),
        request_state_semantic_fingerprint=(
            context_fingerprint(
                "mcp-request-state:v1",
                request_state_bytes.decode("utf-8"),
            )
            if request_state_bytes is not None
            else None
        ),
    )
    payload = {
        "interaction": interaction,
        "binding_identity": binding_identity,
        "pending_lease_reservation": reservation,
        "request_envelope": envelope,
        "owned_original_request_json_bytes": bytes(original_bytes),
        "owned_request_state_json_bytes": (
            bytes(request_state_bytes) if request_state_bytes is not None else None
        ),
        "deadline_monotonic": deadline_monotonic,
    }
    return PreparedMcpInputRequiredSuspension(
        **payload,
        prepared_suspension_fingerprint=context_fingerprint(
            "prepared-mcp-input-required-suspension:v1",
            {
                "interaction": interaction,
                "binding_identity": binding_identity,
                "pending_lease_reservation": reservation,
                "request_envelope": envelope,
                "owned_original_request_json": original_bytes.decode("utf-8"),
                "owned_request_state_json": (
                    request_state_bytes.decode("utf-8")
                    if request_state_bytes is not None
                    else None
                ),
                "deadline_monotonic": deadline_monotonic,
            },
        ),
    )


def prepare_mcp_input_required_resolution(
    *,
    source_suspension_event_reference: ContextEventReferenceFact,
    source_suspension_fact_fingerprint: str,
    interaction_id: str,
    responses: Mapping[str, Any],
    cancelled: bool,
) -> PreparedMcpInputRequiredResolution:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    keys = tuple(sorted(responses))
    if len(keys) > MAX_MCP_RESPONSE_KEYS or len(keys) != len(set(keys)):
        raise ValueError("MCP response key set is invalid")
    entries: list[PreparedMcpResponseEntry] = []
    receipt_values: list[str] = []
    total_bytes = 0
    for key in keys:
        _bounded_utf8(
            key,
            maximum=MAX_RUNTIME_NAME_BYTES,
            label="MCP response key",
        )
        frozen = freeze_json(responses[key])
        payload = canonical_json_bytes(thaw_json(frozen))
        total_bytes += len(payload)
        if total_bytes > MAX_MCP_PREPARED_RESPONSE_BYTES:
            raise ValueError("prepared MCP responses exceed their byte bound")
        fingerprint = context_fingerprint("mcp-response-entry:v1", payload.decode("utf-8"))
        entries.append(
            PreparedMcpResponseEntry(
                key=key,
                canonical_response_json_bytes=bytes(payload),
                response_semantic_fingerprint=fingerprint,
            )
        )
        receipt_values.append(fingerprint)
    receipt = ordered_fingerprint_accumulator(
        "mcp-response-payload-receipt:v1",
        tuple(receipt_values),
    )
    semantic = build_frozen_fact(
        McpInputRequiredResolutionSemanticFact,
        schema_version="mcp_input_required_resolution.v1",
        cancelled=cancelled,
        ordered_response_keys=keys,
        response_payload_receipt_fingerprint=receipt,
    )
    payload = {
        "source_suspension_event_reference": source_suspension_event_reference,
        "source_suspension_fact_fingerprint": source_suspension_fact_fingerprint,
        "interaction_id": interaction_id,
        "cancelled": cancelled,
        "ordered_response_entries": tuple(entries),
        "resolution_semantic": semantic,
    }
    return PreparedMcpInputRequiredResolution(
        **payload,
        prepared_resolution_fingerprint=context_fingerprint(
            "prepared-mcp-input-required-resolution:v1",
            {
                **payload,
                "ordered_response_entries": tuple(
                    {
                        "key": item.key,
                        "canonical_response_json": (
                            item.canonical_response_json_bytes.decode("utf-8")
                        ),
                        "response_semantic_fingerprint": (
                            item.response_semantic_fingerprint
                        ),
                    }
                    for item in entries
                ),
            },
        ),
    )


_DIAGNOSTIC_PROFILE_CONTRACTS: Mapping[str, Mapping[str, object]] = {
    "mcp_input_required_resume_error.v1": {
        "default_message": "MCP input-required resume failed.",
        "accepts_explicit_redacted_message": True,
    },
    "execution_evidence_projection_error.v1": {
        "default_message": "Tool-result evidence projection failed.",
        "accepts_explicit_redacted_message": False,
    },
    "compaction_candidate_projection_preparation_error.v1": {
        "default_message": "Compaction candidate projection preparation failed.",
        "accepts_explicit_redacted_message": False,
    },
    "compaction_candidate_projection_owner_installation_error.v1": {
        "default_message": "Compaction candidate projection owner installation failed.",
        "accepts_explicit_redacted_message": False,
    },
}
_DIAGNOSTIC_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{4,}\b"),
        "[REDACTED_SECRET]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
            r"authorization|password|secret)\b(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@"),
        r"\1[REDACTED]@",
    ),
)


def _sanitize_runtime_diagnostic_text(value: str) -> str:
    sanitized = value
    for pattern, replacement in _DIAGNOSTIC_SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    normalized = "".join(
        char if ord(char) >= 32 or char in "\n\t" else " " for char in sanitized
    )
    encoded = normalized.encode("utf-8")
    if len(encoded) <= MAX_RUNTIME_DIAGNOSTIC_BYTES:
        return normalized
    encoded = encoded[:MAX_RUNTIME_DIAGNOSTIC_BYTES]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def build_bounded_runtime_failure_diagnostic(
    *,
    error: BaseException,
    redaction_profile_id: str,
    redacted_message: str | None = None,
) -> BoundedRuntimeFailureDiagnosticFact:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    profile = _DIAGNOSTIC_PROFILE_CONTRACTS.get(redaction_profile_id)
    if profile is None:
        raise ValueError("unknown runtime diagnostic redaction profile")
    error_type = type(error).__name__[:MAX_RUNTIME_ERROR_TYPE_BYTES]
    accepts_explicit = bool(profile["accepts_explicit_redacted_message"])
    if redacted_message is not None and not accepts_explicit:
        raise ValueError("runtime diagnostic profile rejects explicit message text")
    source_message = (
        redacted_message
        if redacted_message is not None
        else str(profile["default_message"])
    )
    normalized = _sanitize_runtime_diagnostic_text(source_message)
    contract = context_fingerprint(
        "runtime-diagnostic-redaction-profile:v1",
        {
            "profile_id": redaction_profile_id,
            "profile": profile,
            "sanitizer_contract": "closed-secret-scrubber.v1",
        },
    )
    return build_frozen_fact(
        BoundedRuntimeFailureDiagnosticFact,
        schema_version="bounded_runtime_failure_diagnostic.v1",
        error_type=error_type,
        redacted_message=normalized,
        redaction_profile_id=redaction_profile_id,
        redaction_contract_fingerprint=contract,
    )


def build_runtime_event_deadline_budget(
    *,
    admitted_at_monotonic: float,
    total_timeout_seconds: float,
    terminal_reserve_seconds: float,
) -> RuntimeEventOperationDeadlineBudget:
    if total_timeout_seconds <= terminal_reserve_seconds:
        raise ValueError("runtime event deadline must reserve a terminal tail")
    payload = {
        "admitted_at_monotonic": admitted_at_monotonic,
        "ordinary_deadline_monotonic": (
            admitted_at_monotonic
            + total_timeout_seconds
            - terminal_reserve_seconds
        ),
        "terminal_deadline_monotonic": (
            admitted_at_monotonic + total_timeout_seconds
        ),
        "terminal_reserve_seconds": terminal_reserve_seconds,
    }
    return RuntimeEventOperationDeadlineBudget(
        **payload,
        budget_fingerprint=context_fingerprint(
            "runtime-event-operation-deadline-budget:v1",
            payload,
        ),
    )


__all__ = [
    "BoundedRuntimeFailureDiagnosticFact",
    "CompactionCandidateProjectionReceipt",
    "CompactionCandidateProjectionRequestIdentity",
    "CompactionCandidateProjectionStatus",
    "CompactionPublicationTerminalizationScope",
    "ContextCompactionRequestFact",
    "CurrentToolResultBatchReceipt",
    "CurrentToolResultReceiptItem",
    "MAX_MCP_INPUT_REQUEST_BYTES",
    "MAX_MCP_INPUT_REQUESTS",
    "MAX_MCP_PREPARED_RESPONSE_BYTES",
    "MAX_MCP_RESPONSE_KEYS",
    "MAX_PUBLICATION_TERMINATION_REFS",
    "MAX_TOOL_RESULT_RECEIPT_ITEMS",
    "McpInputRequiredInteractionSemanticFact",
    "McpInputRequiredRequestEnvelopeFact",
    "McpInputRequiredResolutionAttemptFact",
    "McpInputRequiredResolutionSemanticFact",
    "McpInputRequiredSourceAuthorityFact",
    "McpInputRequiredSuspensionFact",
    "McpInputRequiredTerminalSourceFact",
    "McpPendingLeaseReservationIdentityFact",
    "McpUserVisibleInputRequestFact",
    "MidTurnCompactionSkipFact",
    "PreparedCompactionCandidateProjectionInput",
    "PreparedMcpInputRequiredSuspension",
    "PreparedMcpInputRequiredResolution",
    "PreparedMcpResponseEntry",
    "PublicationLatchedRunTerminationFact",
    "RuntimeEventOperationDeadlineBudget",
    "ToolResultEvidenceProjectionFailureFact",
    "ToolResultEvidenceProjectionSourceFact",
    "build_bounded_runtime_failure_diagnostic",
    "build_mcp_interaction_semantic",
    "build_mcp_user_visible_request",
    "build_runtime_event_deadline_budget",
    "ordered_fingerprint_accumulator",
    "prepare_mcp_input_required_resolution",
    "prepare_mcp_input_required_suspension",
    "stable_runtime_event_id",
]
