"""Typed, bounded facts for runtime audit and MCP lifecycle events.

This module is deliberately below the event and runtime layers. Durable facts
live here; process-local prepared carriers are marked with
``FrozenRuntimeStateBase`` and must never be serialized as authority.
"""

from __future__ import annotations

import re
from typing import Literal, Mapping

from pydantic import Field, field_validator, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    FrozenRuntimeStateBase,
    register_durable_fact,
)
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.primitives.mcp_continuation import (
    McpElicitationRequestFact,
    McpInputRequiredDurableContinuationFact,
    McpInputRequiredResolutionSemanticFact,
)
from pulsara_agent.primitives.mcp_protocol import McpClientInputMethod


MAX_RUNTIME_IDENTIFIER_BYTES = 512
MAX_RUNTIME_NAME_BYTES = 256
MAX_RUNTIME_ERROR_TYPE_BYTES = 128
MAX_RUNTIME_DIAGNOSTIC_BYTES = 1_024
MAX_MCP_INPUT_REQUESTS = 64
MAX_MCP_INPUT_REQUEST_BYTES = 64 * 1_024
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
    "mcp_user_visible_input_request.v2",
    "request_fingerprint",
    "mcp-user-visible-input-request:v2",
)
class McpUserVisibleInputRequestFact(FrozenFactBase):
    schema_version: Literal["mcp_user_visible_input_request.v2"] = (
        "mcp_user_visible_input_request.v2"
    )
    key: str
    method: McpClientInputMethod
    request: McpElicitationRequestFact
    request_fingerprint: str

    @field_validator("key")
    @classmethod
    def _key_bound(cls, value: str) -> str:
        return _bounded_utf8(
            value,
            maximum=MAX_RUNTIME_NAME_BYTES,
            label="MCP input request key",
        )

    @model_validator(mode="after")
    def _request_join(self) -> "McpUserVisibleInputRequestFact":
        if self.key != self.request.key or self.method is not self.request.method:
            raise ValueError("MCP user-visible request identity mismatch")
        return self


@_fact(
    "mcp_input_required_request_envelope.v2",
    "request_envelope_semantic_fingerprint",
    "mcp-input-required-request-envelope:v2",
)
class McpInputRequiredRequestEnvelopeFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_request_envelope.v2"] = (
        "mcp_input_required_request_envelope.v2"
    )
    protocol_revision: str
    ordered_user_visible_input_requests: tuple[McpUserVisibleInputRequestFact, ...] = (
        Field(max_length=MAX_MCP_INPUT_REQUESTS)
    )
    request_set_fingerprint: str
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
        expected_set = context_fingerprint(
            "mcp-input-request-set:v1",
            tuple(
                (item.key, item.request.request_fingerprint)
                for item in self.ordered_user_visible_input_requests
            ),
        )
        if self.request_set_fingerprint != expected_set:
            raise ValueError("MCP request envelope set fingerprint mismatch")
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
    "mcp_input_required_suspension.v2",
    "suspension_fact_fingerprint",
    "mcp-input-required-suspension:v2",
)
class McpInputRequiredSuspensionFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_suspension.v2"] = (
        "mcp_input_required_suspension.v2"
    )
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    durable_continuation: McpInputRequiredDurableContinuationFact
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    source_mcp_installation_id: str
    predecessor_resolution_submitted_event_reference: ContextEventReferenceFact | None
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

    @model_validator(mode="after")
    def _source_join(self) -> "McpInputRequiredSuspensionFact":
        reservation = self.pending_lease_reservation
        if (
            reservation.interaction_id != self.interaction.interaction_id
            or reservation.binding_identity != self.binding_identity
        ):
            raise ValueError("MCP suspension pending lease identity mismatch")
        continuation = self.durable_continuation
        if (
            continuation.request_set_fingerprint
            != self.request_envelope.request_set_fingerprint
            or continuation.protocol_semantic_fingerprint == ""
            or continuation.binding_contract_fingerprint == ""
            or continuation.round_ordinal != self.interaction.round_count
        ):
            raise ValueError("MCP suspension continuation authority mismatch")
        predecessor = self.predecessor_resolution_submitted_event_reference
        if (self.interaction.round_count == 1) != (predecessor is None):
            raise ValueError("MCP suspension predecessor/round matrix mismatch")
        return self


@_fact(
    "mcp_input_required_source_authority.v2",
    "source_authority_fingerprint",
    "mcp-input-required-source-authority:v2",
)
class McpInputRequiredSourceAuthorityFact(FrozenFactBase):
    schema_version: Literal["mcp_input_required_source_authority.v2"] = (
        "mcp_input_required_source_authority.v2"
    )
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope_semantic_fingerprint: str
    request_set_fingerprint: str
    continuation_carrier_id: str
    continuation_fact_fingerprint: str
    operation_expires_at_utc: str
    expiry_fingerprint: str
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    source_mcp_installation_id: str
    predecessor_resolution_submitted_event_reference: ContextEventReferenceFact | None
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
            or self.pending_lease_reservation.binding_identity != self.binding_identity
        ):
            raise ValueError("MCP source authority pending lease mismatch")
        if (
            not self.request_set_fingerprint
            or not self.continuation_carrier_id
            or not self.continuation_fact_fingerprint
            or not self.operation_expires_at_utc
            or not self.expiry_fingerprint
        ):
            raise ValueError("MCP source authority continuation identity is incomplete")
        return self


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
    predecessor_resolution_submitted_event_reference: ContextEventReferenceFact | None
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
                raise ValueError(
                    "first MCP resolution attempt cannot have predecessors"
                )
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
            raise ValueError(
                "runtime diagnostic contains unsupported control characters"
            )
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
    source_resolution_submitted_event_reference: ContextEventReferenceFact | None
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
        runtime_ids = {item.runtime_session_id for item in self.source_event_references}
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
            item.sequence for item in self.basis_tool_result_terminal_event_references
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
    ordered_tool_result_sources: tuple[ToolResultEvidenceProjectionSourceFact, ...] = (
        Field(min_length=1, max_length=MAX_TOOL_RESULT_RECEIPT_ITEMS)
    )
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
            raise ValueError(
                "no-active-run compaction scope cannot carry active identities"
            )
        expected = context_fingerprint(
            "compaction-publication-terminalization-scope:v1",
            self.model_dump(mode="json", exclude={"scope_fingerprint"}),
        )
        if self.scope_fingerprint != expected:
            raise ValueError("compaction terminalization scope fingerprint mismatch")
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
    request: McpElicitationRequestFact,
) -> McpUserVisibleInputRequestFact:
    from pulsara_agent.primitives.frozen import build_frozen_fact

    return build_frozen_fact(
        McpUserVisibleInputRequestFact,
        schema_version="mcp_user_visible_input_request.v2",
        key=request.key,
        method=request.method,
        request=request,
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
    "durable_projection_job_error.v1": {
        "default_message": "Durable projection job failed.",
        "accepts_explicit_redacted_message": False,
    },
    "durable_projection_seed_error.v1": {
        "default_message": "Durable projection seed authority failed validation.",
        "accepts_explicit_redacted_message": False,
    },
    "canonical_mutation_surface_delivery_error.v1": {
        "default_message": "Canonical mutation surface delivery failed.",
        "accepts_explicit_redacted_message": False,
    },
    "runtime_session_bootstrap_error.v1": {
        "default_message": "Runtime session owner bootstrap failed.",
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
            admitted_at_monotonic + total_timeout_seconds - terminal_reserve_seconds
        ),
        "terminal_deadline_monotonic": (admitted_at_monotonic + total_timeout_seconds),
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
    "CompactionPublicationTerminalizationScope",
    "ContextCompactionRequestFact",
    "MAX_MCP_INPUT_REQUEST_BYTES",
    "MAX_MCP_INPUT_REQUESTS",
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
    "PublicationLatchedRunTerminationFact",
    "RuntimeEventOperationDeadlineBudget",
    "ToolResultEvidenceProjectionFailureFact",
    "ToolResultEvidenceProjectionSourceFact",
    "build_bounded_runtime_failure_diagnostic",
    "build_mcp_interaction_semantic",
    "build_mcp_user_visible_request",
    "build_runtime_event_deadline_budget",
    "ordered_fingerprint_accumulator",
    "stable_runtime_event_id",
]
