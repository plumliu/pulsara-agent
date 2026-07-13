"""Immutable tool-result semantics and rendering contracts."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from pulsara_agent.primitives._context_base import (
    CapabilityDescriptorRenderAttributionFact,
    ContextEventReferenceFact,
    FrozenContextFact,
    FrozenJsonObjectFact,
    FrozenJsonValue,
    canonical_utc_timestamp,
    context_fingerprint,
    thaw_json,
)
from pulsara_agent.primitives.context import ResolvedToolResultRenderPolicyFact
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact


class ToolResultStateFact(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DENIED = "denied"


class ToolResultOperationalKind(StrEnum):
    GENERIC = "generic"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_COMMAND_ERROR = "terminal_command_error"
    TERMINAL_PROCESS_OBSERVATION = "terminal_process_observation"
    TERMINAL_PROCESS_INVENTORY = "terminal_process_inventory"
    TERMINAL_PROCESS_ERROR = "terminal_process_error"
    ARTIFACT = "artifact"


class ToolResultEssentialEnvelopeKind(StrEnum):
    NONE = "none"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_COMMAND_ERROR = "terminal_command_error"
    TERMINAL_PROCESS_OBSERVATION = "terminal_process_observation"
    TERMINAL_PROCESS_INVENTORY = "terminal_process_inventory"
    TERMINAL_PROCESS_ERROR = "terminal_process_error"
    ARTIFACT = "artifact"


class ToolResultRenderVariantCode(StrEnum):
    GENERIC_RESULT = "generic_result"
    GENERIC_DENIED = "generic_denied"
    TERMINAL_COMMAND_EXECUTED = "terminal_command_executed"
    TERMINAL_COMMAND_MALFORMED_ARGUMENTS = "terminal_command_malformed_arguments"
    TERMINAL_COMMAND_DENIED = "terminal_command_denied"
    TERMINAL_COMMAND_ADAPTER_ERROR = "terminal_command_adapter_error"
    TERMINAL_PROCESS_INVENTORY = "terminal_process_inventory"
    TERMINAL_PROCESS_OBSERVATION = "terminal_process_observation"
    TERMINAL_PROCESS_ERROR = "terminal_process_error"
    TERMINAL_PROCESS_ADAPTER_ERROR = "terminal_process_adapter_error"
    EXTERNAL_GENERIC_RESULT = "external_generic_result"
    EXTERNAL_TERMINAL_RESULT = "external_terminal_result"


class ToolResultBodyCandidateSource(StrEnum):
    INLINE = "inline"
    ARTIFACT_PREVIEW = "artifact_preview"
    DATA_PLACEHOLDER = "data_placeholder"
    NONE = "none"


class ToolResultMinimumEnvelopeKind(StrEnum):
    NONE = "none"
    ESSENTIAL = "essential"
    ARTIFACT = "artifact"


class ToolResultLatestReserveReasonCode(StrEnum):
    APPLIED = "applied"
    NOT_LATEST = "not_latest"
    NOT_ELIGIBLE = "not_eligible"
    BUDGET_UNSATISFIED = "latest_reserved_budget_unsatisfied"


class ToolResultPayloadFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    MIXED = "mixed"
    DATA_PLACEHOLDER = "data_placeholder"
    OMITTED = "omitted"


class ToolResultBodyPolicy(StrEnum):
    FULL_VISIBLE = "full_visible"
    ARTIFACT_PREVIEW = "artifact_preview"
    OMITTED_ARTIFACT = "omitted_artifact"
    CLIPPED = "clipped"
    OMITTED_NON_ARTIFACT = "omitted_non_artifact"


class ToolResultEnvelopePolicy(StrEnum):
    FULL = "full_envelope"
    COMPACT = "compact_envelope"
    MINIMAL = "minimal_envelope"
    OMITTED = "omitted_envelope"


class ToolResultRenderReasonCode(StrEnum):
    WITHIN_BUDGET = "within_budget"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ESSENTIAL_PRESERVED = "essential_preserved"
    ARTIFACT_PREVIEW = "artifact_preview"
    LATEST_RESERVED = "latest_reserved"


class ToolResultRenderDiagnosticCode(StrEnum):
    BUDGET_DEGRADED = "budget_degraded"
    LATEST_RESERVED_BUDGET_UNSATISFIED = "latest_reserved_budget_unsatisfied"
    CACHE_INVALID = "cache_invalid"
    CACHE_READ_FAILED = "cache_read_failed"
    CACHE_WRITE_FAILED = "cache_write_failed"
    ESSENTIAL_ENVELOPE_CLIPPED = "essential_envelope_clipped"


class ExternalToolResultIngressReasonCode(StrEnum):
    REQUIREMENT_NOT_COMMITTED = "requirement_not_committed"
    REQUIREMENT_IDENTITY_MISMATCH = "requirement_identity_mismatch"
    EXTERNAL_VARIANT_NOT_ALLOWED = "external_variant_not_allowed"
    BUILDER_BINDING_MISSING = "builder_binding_missing"
    BUILDER_IDENTITY_MISMATCH = "builder_identity_mismatch"
    BUILDER_CONTRACT_MISMATCH = "builder_contract_mismatch"
    CAPTURE_POLICY_REQUIRED = "capture_policy_required"
    CAPTURE_POLICY_MUST_BE_NONE = "capture_policy_must_be_none"
    CAPTURE_POLICY_UNSUPPORTED = "capture_policy_unsupported"
    DOMAIN_SUBMISSION_MISMATCH = "domain_submission_mismatch"
    TIMING_CONTRACT_MISMATCH = "timing_contract_mismatch"


class CapabilityResultRenderVariantFact(FrozenContextFact):
    variant_code: ToolResultRenderVariantCode
    operational_kind: ToolResultOperationalKind
    essential_envelope_kind: ToolResultEssentialEnvelopeKind
    allowed_result_states: tuple[ToolResultStateFact, ...]
    execution_phase: Literal["pre_execution", "executed", "post_execution"]
    terminal_payload_timing_requirement: Literal["required", "optional", "forbidden"]
    variant_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_variant(self) -> "CapabilityResultRenderVariantFact":
        values = tuple(state.value for state in self.allowed_result_states)
        if not values or values != tuple(sorted(set(values))):
            raise ValueError("variant result states must be sorted and unique")
        _validate_fingerprint(
            self, "tool-result-render-variant:v1", "variant_fingerprint"
        )
        return self


class ToolResultSemanticsBuilderContractFact(FrozenContextFact):
    schema_version: Literal["tool-result-semantics-builder-contract:v1"] = (
        "tool-result-semantics-builder-contract:v1"
    )
    builder_id: str = Field(min_length=1)
    builder_version: str = Field(min_length=1)
    input_schema_fingerprints: tuple[str, ...]
    output_schema_fingerprint: str = Field(min_length=1)
    variant_table_fingerprint: str = Field(min_length=1)
    classifier_policy_fingerprint: str = Field(min_length=1)
    normalization_contract_versions: tuple[str, ...]
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "ToolResultSemanticsBuilderContractFact":
        if len(self.input_schema_fingerprints) != 6:
            raise ValueError("semantics builder contract requires six input schemas")
        if not self.normalization_contract_versions:
            raise ValueError(
                "semantics builder contract requires normalization versions"
            )
        _validate_fingerprint(
            self, "tool-result-semantics-builder-contract:v1", "contract_fingerprint"
        )
        return self


class CapabilityResultRenderContractFact(FrozenContextFact):
    allowed_operational_kinds: tuple[ToolResultOperationalKind, ...]
    allowed_essential_envelope_kinds: tuple[ToolResultEssentialEnvelopeKind, ...]
    allowed_variants: tuple[CapabilityResultRenderVariantFact, ...]
    semantics_builder_id: str = Field(min_length=1)
    semantics_builder_version: str = Field(min_length=1)
    semantics_builder_contract: ToolResultSemanticsBuilderContractFact
    semantics_builder_contract_fingerprint: str = Field(min_length=1)
    pre_execution_denial_variant_code: ToolResultRenderVariantCode
    contract_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _contract(self) -> "CapabilityResultRenderContractFact":
        codes = tuple(variant.variant_code for variant in self.allowed_variants)
        if not codes or len(codes) != len(set(codes)):
            raise ValueError(
                "render contract variant codes must be non-empty and unique"
            )
        operational = tuple(
            sorted(
                {variant.operational_kind for variant in self.allowed_variants}, key=str
            )
        )
        essential = tuple(
            sorted(
                {variant.essential_envelope_kind for variant in self.allowed_variants},
                key=str,
            )
        )
        if self.allowed_operational_kinds != operational:
            raise ValueError("allowed operational kind projection mismatch")
        if self.allowed_essential_envelope_kinds != essential:
            raise ValueError("allowed essential kind projection mismatch")
        builder = self.semantics_builder_contract
        if (self.semantics_builder_id, self.semantics_builder_version) != (
            builder.builder_id,
            builder.builder_version,
        ):
            raise ValueError("render contract builder identity mismatch")
        if self.semantics_builder_contract_fingerprint != builder.contract_fingerprint:
            raise ValueError("render contract builder fingerprint mismatch")
        expected_table = context_fingerprint(
            "tool-result-variant-table:v1",
            [variant.model_dump(mode="json") for variant in self.allowed_variants],
        )
        if builder.variant_table_fingerprint != expected_table:
            raise ValueError("builder variant table fingerprint mismatch")
        denial = next(
            (
                variant
                for variant in self.allowed_variants
                if variant.variant_code == self.pre_execution_denial_variant_code
            ),
            None,
        )
        if denial is None:
            raise ValueError("pre-execution denial variant is missing")
        if denial.execution_phase != "pre_execution":
            raise ValueError("denial variant must be pre-execution")
        if not set(denial.allowed_result_states).issubset(
            {ToolResultStateFact.DENIED, ToolResultStateFact.ERROR}
        ):
            raise ValueError("denial variant has an invalid result state")
        if denial.terminal_payload_timing_requirement != "forbidden":
            raise ValueError("denial variant cannot carry terminal timing")
        _validate_fingerprint(
            self, "capability-result-render-contract:v1", "contract_fingerprint"
        )
        return self


class ToolResultRenderProfileFact(FrozenContextFact):
    profile_version: str = Field(min_length=1)
    selected_variant: CapabilityResultRenderVariantFact
    render_contract: CapabilityResultRenderContractFact
    tool_origin: Literal[
        "builtin", "terminal", "mcp", "subagent", "workflow", "custom", "unknown"
    ]
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None
    render_contract_fingerprint: str = Field(min_length=1)
    profile_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _profile(self) -> "ToolResultRenderProfileFact":
        if (
            self.render_contract_fingerprint
            != self.render_contract.contract_fingerprint
        ):
            raise ValueError("profile embedded render contract mismatch")
        matches = tuple(
            item
            for item in self.render_contract.allowed_variants
            if item.variant_code == self.selected_variant.variant_code
        )
        if len(matches) != 1 or matches[0] != self.selected_variant:
            raise ValueError("profile selected variant is absent from render contract")
        if self.descriptor_attribution is not None and (
            self.render_contract_fingerprint
            != self.descriptor_attribution.result_render_contract_fingerprint
        ):
            raise ValueError("profile render contract attribution mismatch")
        if self.descriptor_attribution is None and self.tool_origin != "unknown":
            raise ValueError("known tool origin requires descriptor attribution")
        _validate_fingerprint(
            self, "tool-result-render-profile:v1", "profile_fingerprint"
        )
        return self


class ToolResultEssentialCapturePolicyFact(FrozenContextFact):
    policy_version: str = Field(min_length=1)
    max_error_chars: int = Field(ge=0)
    max_process_summaries: int = Field(ge=0)
    max_process_command_chars: int = Field(ge=0)
    max_process_cwd_chars: int = Field(ge=0)
    policy_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _policy(self) -> "ToolResultEssentialCapturePolicyFact":
        _validate_fingerprint(
            self, "tool-result-essential-capture-policy:v1", "policy_fingerprint"
        )
        return self


class TerminalPayloadTimingFact(FrozenContextFact):
    observed_at_utc: str
    duration_seconds: float | None = Field(default=None, ge=0)
    freshness: Literal[
        "current_tool_observation",
        "background_process_observation",
        "historical_tool_observation",
    ]
    clock_source: Literal["tool_payload", "tool_runtime_metadata", "mixed"]
    command_started_at_utc: str | None
    process_started_at_utc: str | None
    last_output_at_utc: str | None
    timing_fingerprint: str = Field(min_length=1)

    @field_validator(
        "observed_at_utc",
        "command_started_at_utc",
        "process_started_at_utc",
        "last_output_at_utc",
    )
    @classmethod
    def _utc(cls, value: str | None) -> str | None:
        return canonical_utc_timestamp(value) if value is not None else None

    @field_validator("duration_seconds")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("terminal duration must be finite")
        return value

    @model_validator(mode="after")
    def _fingerprint(self) -> "TerminalPayloadTimingFact":
        _validate_fingerprint(self, "terminal-payload-timing:v1", "timing_fingerprint")
        return self


class ToolResultErrorPreviewFact(FrozenContextFact):
    text: str
    original_chars: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def _chars(self) -> "ToolResultErrorPreviewFact":
        if len(self.text) > self.original_chars:
            raise ValueError("error preview exceeds original char count")
        if self.truncated != (len(self.text) < self.original_chars):
            raise ValueError("error preview truncation flag mismatch")
        return self


class TerminalProcessSummaryFact(FrozenContextFact):
    process_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    exit_code: int | None
    command: str | None
    cwd: str | None
    terminal_session_id: str = Field(min_length=1)
    backend_type: str = Field(min_length=1)
    io_mode: str | None
    timed_out: bool
    stdin_closed: bool | None
    duration_seconds: float | None = Field(default=None, ge=0)
    summary_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def _summary(self) -> "TerminalProcessSummaryFact":
        if self.duration_seconds is not None and not math.isfinite(
            self.duration_seconds
        ):
            raise ValueError("process duration must be finite")
        _validate_fingerprint(
            self, "terminal-process-summary:v1", "summary_fingerprint"
        )
        return self


class TerminalCommandEssentialFact(FrozenContextFact):
    kind: Literal["terminal_command"] = "terminal_command"
    capture_policy_fingerprint: str
    action: Literal["execute"] = "execute"
    execution_started: Literal[True] = True
    command: str
    status: str
    exit_code: int | None
    cwd: str
    timed_out: bool
    output_truncated: bool
    error: ToolResultErrorPreviewFact | None
    process_id: str | None
    yielded_to_background: bool
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    stdin_closed: bool | None
    policy_code: str | None
    duration_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _background(self) -> "TerminalCommandEssentialFact":
        if self.yielded_to_background and not self.process_id:
            raise ValueError("background terminal command requires process ID")
        return self


class TerminalCommandErrorEssentialFact(FrozenContextFact):
    kind: Literal["terminal_command_error"] = "terminal_command_error"
    capture_policy_fingerprint: str
    action: Literal["execute"] = "execute"
    execution_started: Literal[False] = False
    requested_command: str | None
    failure_stage: Literal[
        "malformed_arguments",
        "exposure_denied",
        "permission_denied",
        "policy_denied",
        "adapter_initialization",
    ]
    status: str
    error: ToolResultErrorPreviewFact
    policy_code: str | None
    observed_cwd: str | None
    terminal_session_id: str | None
    backend_type: str | None
    io_mode: str | None

    @model_validator(mode="after")
    def _requested_command(self) -> "TerminalCommandErrorEssentialFact":
        if self.failure_stage != "malformed_arguments" and not self.requested_command:
            raise ValueError("terminal denial requires requested command")
        return self


class TerminalProcessObservationEssentialFact(FrozenContextFact):
    kind: Literal["terminal_process_observation"] = "terminal_process_observation"
    capture_policy_fingerprint: str
    action: Literal["log", "poll", "wait", "kill", "write", "submit", "close_stdin"]
    process_id: str = Field(min_length=1)
    status: str
    exit_code: int | None
    command: str | None
    cwd: str | None
    timed_out: bool
    output_truncated: bool
    error: ToolResultErrorPreviewFact | None
    yielded_to_background: bool
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    stdin_closed: bool | None
    policy_code: str | None
    duration_seconds: float | None = Field(default=None, ge=0)


class TerminalProcessInventoryEssentialFact(FrozenContextFact):
    kind: Literal["terminal_process_inventory"] = "terminal_process_inventory"
    capture_policy_fingerprint: str
    action: Literal["list"] = "list"
    status: str
    live_process_count: int = Field(ge=0)
    finished_process_count: int = Field(ge=0)
    process_summaries: tuple[TerminalProcessSummaryFact, ...]
    omitted_process_count: int = Field(ge=0)
    summaries_truncated: bool


class TerminalProcessErrorEssentialFact(FrozenContextFact):
    kind: Literal["terminal_process_error"] = "terminal_process_error"
    capture_policy_fingerprint: str
    requested_action: str
    process_id: str | None
    status: str
    error: ToolResultErrorPreviewFact
    policy_code: str | None
    terminal_session_id: str | None
    backend_type: str | None


class ArtifactEssentialResultFact(FrozenContextFact):
    kind: Literal["artifact"] = "artifact"
    capture_policy_fingerprint: str
    primary_artifact_id: str | None
    output_truncated: bool
    output_preview_available: bool


ToolResultEssentialFact: TypeAlias = (
    TerminalCommandEssentialFact
    | TerminalCommandErrorEssentialFact
    | TerminalProcessObservationEssentialFact
    | TerminalProcessInventoryEssentialFact
    | TerminalProcessErrorEssentialFact
    | ArtifactEssentialResultFact
)


class ToolResultExecutionSemanticsFact(FrozenContextFact):
    render_profile: ToolResultRenderProfileFact
    result_state: ToolResultStateFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    essential_result: ToolResultEssentialFact | None
    terminal_payload_timing: TerminalPayloadTimingFact | None

    @model_validator(mode="after")
    def _semantics(self) -> "ToolResultExecutionSemanticsFact":
        variant = self.render_profile.selected_variant
        if self.result_state not in variant.allowed_result_states:
            raise ValueError("tool result state is not allowed by selected variant")
        requirement = variant.terminal_payload_timing_requirement
        if requirement == "required" and self.terminal_payload_timing is None:
            raise ValueError("selected variant requires terminal timing")
        if requirement == "forbidden" and self.terminal_payload_timing is not None:
            raise ValueError("selected variant forbids terminal timing")
        if (self.essential_capture_policy is None) != (self.essential_result is None):
            raise ValueError("capture policy and essential result must be all-or-none")
        if self.essential_result is not None:
            if (
                self.essential_result.capture_policy_fingerprint
                != self.essential_capture_policy.policy_fingerprint
            ):  # type: ignore[union-attr]
                raise ValueError("essential result capture policy mismatch")
            expected_kind = ToolResultEssentialEnvelopeKind(self.essential_result.kind)
            if variant.essential_envelope_kind != expected_kind:
                raise ValueError(
                    "essential result branch does not match selected variant"
                )
        elif (
            variant.essential_envelope_kind is not ToolResultEssentialEnvelopeKind.NONE
        ):
            raise ValueError("selected variant requires essential result")
        return self


class ToolResultTextContentFact(FrozenContextFact):
    block_id: str
    text: str
    chars: int = Field(ge=0)
    content_fingerprint: str
    source_events: tuple[ContextEventReferenceFact, ...]

    @model_validator(mode="after")
    def _content(self) -> "ToolResultTextContentFact":
        if self.chars != len(self.text):
            raise ValueError("tool result text char count mismatch")
        if self.content_fingerprint != context_fingerprint(
            "tool-result-text:v1", self.text
        ):
            raise ValueError("tool result text fingerprint mismatch")
        return self


class ToolResultDataContentFact(FrozenContextFact):
    block_id: str
    name: str | None
    media_type: str
    source_kind: str
    inline_data_forbidden: Literal[True] = True
    artifact_ids: tuple[str, ...]
    source_events: tuple[ContextEventReferenceFact, ...]


class ToolResultContentFact(FrozenContextFact):
    text_blocks: tuple[ToolResultTextContentFact, ...]
    data_blocks: tuple[ToolResultDataContentFact, ...]
    content_fingerprint: str

    @model_validator(mode="after")
    def _content(self) -> "ToolResultContentFact":
        _validate_fingerprint(self, "tool-result-content:v1", "content_fingerprint")
        return self


class ContextToolResultPreviewFact(FrozenContextFact):
    preview_policy: Literal["full", "head_tail", "head_tail_huge"]
    preview_chars: int = Field(ge=0)
    original_chars: int = Field(ge=0)
    original_bytes: int = Field(ge=0)
    omitted_middle_chars: int = Field(ge=0)
    visible_head_chars: int = Field(ge=0)
    visible_tail_chars: int = Field(ge=0)
    read_more: FrozenJsonObjectFact


class ContextToolResultArtifactRefFact(FrozenContextFact):
    artifact_id: str
    role: str
    media_type: str
    size_bytes: int = Field(ge=0)
    stored_complete: bool
    loss_reason: str | None
    preview: ContextToolResultPreviewFact | None
    ref_fingerprint: str

    @model_validator(mode="after")
    def _ref(self) -> "ContextToolResultArtifactRefFact":
        _validate_fingerprint(
            self, "context-tool-result-artifact-ref:v1", "ref_fingerprint"
        )
        return self


class ToolResultRenderUnit(FrozenContextFact):
    schema_version: Literal["tool-result-unit:v1"] = "tool-result-unit:v1"
    unit_id: str
    tool_call_id: str
    model_tool_name: str
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact | None
    render_contract_fingerprint: str
    render_variant_fingerprint: str
    call_message_id: str
    result_message_id: str
    call_position: int = Field(ge=0)
    result_position: int = Field(ge=0)
    result_state: ToolResultStateFact
    content: ToolResultContentFact
    artifacts: tuple[ContextToolResultArtifactRefFact, ...]
    observation_timing: ToolObservationTimingFact
    terminal_payload_timing: TerminalPayloadTimingFact | None
    render_profile: ToolResultRenderProfileFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    essential: ToolResultEssentialFact | None
    source_sequence_start: int = Field(ge=1)
    source_sequence_end: int = Field(ge=1)
    source_event_ids: tuple[str, ...]
    unit_fingerprint: str

    @model_validator(mode="after")
    def _unit(self) -> "ToolResultRenderUnit":
        if self.call_position >= self.result_position:
            raise ValueError("tool result must follow tool call")
        if self.source_sequence_start > self.source_sequence_end:
            raise ValueError("tool result source range is reversed")
        if self.observation_timing.tool_call_id not in (None, self.tool_call_id):
            raise ValueError("tool observation call ID mismatch")
        profile = self.render_profile
        if self.descriptor_attribution != profile.descriptor_attribution:
            raise ValueError("tool unit descriptor attribution mismatch")
        if self.render_contract_fingerprint != profile.render_contract_fingerprint:
            raise ValueError("tool unit render contract mismatch")
        if (
            self.render_variant_fingerprint
            != profile.selected_variant.variant_fingerprint
        ):
            raise ValueError("tool unit render variant mismatch")
        semantics = ToolResultExecutionSemanticsFact(
            render_profile=profile,
            result_state=self.result_state,
            essential_capture_policy=self.essential_capture_policy,
            essential_result=self.essential,
            terminal_payload_timing=self.terminal_payload_timing,
        )
        del semantics
        artifact_ids = tuple(item.artifact_id for item in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("tool result artifact IDs must be unique")
        _validate_fingerprint(self, "tool-result-render-unit:v1", "unit_fingerprint")
        return self


class ToolResultRenderDiagnosticFact(FrozenContextFact):
    code: ToolResultRenderDiagnosticCode
    severity: Literal["info", "warning", "error"]
    attributes: tuple[tuple[str, FrozenJsonValue], ...]


class ToolResultRenderDecisionFact(FrozenContextFact):
    unit_id: str
    tool_call_id: str
    source_message_id: str
    source_assistant_message_id: str | None
    segment: Literal[
        "prior_history", "current_user", "current_run_tail", "legacy_history"
    ]
    render_order: int = Field(ge=0)
    state: str
    render_source_fingerprint: str
    artifact_fingerprint: str
    original_chars: int = Field(ge=0)
    body_candidate_chars: int | None = Field(default=None, ge=0)
    body_candidate_source: ToolResultBodyCandidateSource
    minimum_envelope_kind: ToolResultMinimumEnvelopeKind
    latest_reserved_candidate: bool
    latest_reserved_applied: bool
    latest_reserved_reason: ToolResultLatestReserveReasonCode
    visible_body_chars: int = Field(ge=0)
    rendered_tool_observation: ToolObservationTimingFact | None
    observation_timing_policy: Literal["full", "minimal", "omitted", "not_applicable"]
    rendered_terminal_payload_timing: TerminalPayloadTimingFact | None
    terminal_payload_timing_policy: Literal[
        "full", "minimal", "omitted", "not_applicable"
    ]
    rendered_header_chars: int = Field(ge=0)
    rendered_envelope_chars: int = Field(ge=0)
    rendered_total_chars: int = Field(ge=0)
    framing: Literal["pulsara_tool_result_header", "pulsara_tool_result_envelope"]
    payload_preserved: bool
    payload_format: ToolResultPayloadFormat
    body_budget_remaining: int = Field(ge=0)
    message_body_budget_remaining: int = Field(ge=0)
    envelope_budget_remaining: int = Field(ge=0)
    primary_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    body_policy: ToolResultBodyPolicy
    envelope_policy: ToolResultEnvelopePolicy
    reason_code: ToolResultRenderReasonCode
    clipped_envelope_fields: tuple[str, ...]
    read_more: FrozenJsonObjectFact | None
    diagnostics: tuple[ToolResultRenderDiagnosticFact, ...]
    decision_fingerprint: str

    @model_validator(mode="after")
    def _decision(self) -> "ToolResultRenderDecisionFact":
        if self.rendered_total_chars != (
            self.visible_body_chars + self.rendered_envelope_chars
        ):
            raise ValueError("tool-result rendered char accounting mismatch")
        if self.primary_artifact_id is not None and (
            self.primary_artifact_id not in self.artifact_ids
        ):
            raise ValueError("primary artifact is absent from artifact IDs")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("tool-result decision artifact IDs must be unique")
        _validate_fingerprint(
            self,
            "tool-result-render-decision:v1",
            "decision_fingerprint",
        )
        return self


class ToolResultRenderOperationalFact(FrozenContextFact):
    unit_id: str
    cache_status: Literal["hit", "miss", "invalidated", "not_configured"]
    cache_key: str | None
    diagnostics: tuple[ToolResultRenderDiagnosticFact, ...]

    @model_validator(mode="after")
    def _operational(self) -> "ToolResultRenderOperationalFact":
        if self.cache_status == "not_configured" and self.cache_key is not None:
            raise ValueError("unconfigured render cache cannot carry a key")
        return self


class ToolResultRenderCacheHint(FrozenContextFact):
    unit_id: str
    cache_key: str
    rendered_text: str
    rendered_text_fingerprint: str
    decision: ToolResultRenderDecisionFact
    hint_fingerprint: str

    @model_validator(mode="after")
    def _hint(self) -> "ToolResultRenderCacheHint":
        if self.decision.unit_id != self.unit_id:
            raise ValueError("tool-result cache hint unit mismatch")
        if self.rendered_text_fingerprint != context_fingerprint(
            "tool-result-rendered-text:v1", self.rendered_text
        ):
            raise ValueError("tool-result cache rendered text fingerprint mismatch")
        _validate_fingerprint(
            self, "tool-result-render-cache-hint:v1", "hint_fingerprint"
        )
        return self


class PreparedToolResultRenderInput(FrozenContextFact):
    units: tuple[ToolResultRenderUnit, ...]
    resolved_policy: ResolvedToolResultRenderPolicyFact
    cache_configured: bool
    cache_hints: tuple[ToolResultRenderCacheHint, ...]
    cache_read_failed_unit_ids: tuple[str, ...] = ()
    render_input_fingerprint: str
    cache_hints_fingerprint: str

    @model_validator(mode="after")
    def _prepared(self) -> "PreparedToolResultRenderInput":
        unit_ids = tuple(unit.unit_id for unit in self.units)
        if unit_ids != self.resolved_policy.ordered_unit_ids:
            raise ValueError("prepared tool-result units/policy order mismatch")
        hint_ids = tuple(hint.unit_id for hint in self.cache_hints)
        if len(hint_ids) != len(set(hint_ids)) or not set(hint_ids).issubset(unit_ids):
            raise ValueError("prepared tool-result cache hints are invalid")
        if not self.cache_configured and self.cache_hints:
            raise ValueError("unconfigured render cache cannot provide hints")
        failed_ids = self.cache_read_failed_unit_ids
        if len(failed_ids) != len(set(failed_ids)) or not set(failed_ids).issubset(
            unit_ids
        ):
            raise ValueError("prepared tool-result cache read failures are invalid")
        if not self.cache_configured and failed_ids:
            raise ValueError("unconfigured render cache cannot have read failures")
        expected_hints = context_fingerprint(
            "tool-result-render-cache-hints:v1",
            tuple(hint.hint_fingerprint for hint in self.cache_hints),
        )
        if self.cache_hints_fingerprint != expected_hints:
            raise ValueError("prepared tool-result cache hints fingerprint mismatch")
        return self


class FrozenToolResultBlockFact(FrozenContextFact):
    tool_call_id: str
    model_tool_name: str
    result_state: ToolResultStateFact
    canonical_block_payload: FrozenJsonObjectFact
    block_payload_fingerprint: str

    @model_validator(mode="after")
    def _block(self) -> "FrozenToolResultBlockFact":
        payload = thaw_json(self.canonical_block_payload)
        if (
            payload.get("id") != self.tool_call_id
            or payload.get("name") != self.model_tool_name
        ):
            raise ValueError("frozen tool result block identity mismatch")
        if payload.get("state") != self.result_state.value:
            raise ValueError("frozen tool result block state mismatch")
        if self.block_payload_fingerprint != context_fingerprint(
            "tool-result-block:v1", self.canonical_block_payload
        ):
            raise ValueError("frozen tool result block fingerprint mismatch")
        return self


class ExternalExecutionRequirementReferenceFact(FrozenContextFact):
    owner_runtime_session_id: str
    require_event_id: str
    require_event_sequence: int = Field(ge=1)
    require_event_payload_fingerprint: str
    tool_call_id: str
    requirement_fingerprint: str

    @model_validator(mode="after")
    def _reference(self) -> "ExternalExecutionRequirementReferenceFact":
        if not self.require_event_payload_fingerprint.startswith("sha256:"):
            raise ValueError("external requirement event fingerprint is invalid")
        return self


class ExternalToolCallRequirementFact(FrozenContextFact):
    tool_call_id: str
    model_tool_name: str
    raw_arguments_json: str
    tool_origin: Literal["builtin", "terminal", "mcp", "subagent", "workflow", "custom"]
    descriptor_attribution: CapabilityDescriptorRenderAttributionFact
    result_render_contract: CapabilityResultRenderContractFact
    essential_capture_policy: ToolResultEssentialCapturePolicyFact | None
    requirement_fingerprint: str

    @model_validator(mode="after")
    def _requirement(self) -> "ExternalToolCallRequirementFact":
        variants = tuple(
            item
            for item in self.result_render_contract.allowed_variants
            if item.execution_phase == "post_execution"
        )
        if not variants:
            raise ValueError("external requirement has no post-execution variant")
        can_emit_essential = any(
            item.essential_envelope_kind is not ToolResultEssentialEnvelopeKind.NONE
            for item in variants
        )
        if can_emit_essential != (self.essential_capture_policy is not None):
            raise ValueError("external requirement capture policy branch mismatch")
        _validate_fingerprint(
            self, "external-tool-call-requirement:v1", "requirement_fingerprint"
        )
        return self


class TerminalCommandDomainSubmissionFact(FrozenContextFact):
    kind: Literal["terminal_command"] = "terminal_command"
    command: str
    status: str
    exit_code: int | None
    cwd: str
    timed_out: bool
    output_truncated: bool
    error: ToolResultErrorPreviewFact | None
    process_id: str | None
    yielded_to_background: bool
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    stdin_closed: bool | None
    policy_code: str | None
    duration_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _background(self) -> "TerminalCommandDomainSubmissionFact":
        if self.yielded_to_background and not self.process_id:
            raise ValueError("background terminal command requires process ID")
        return self


class TerminalCommandErrorDomainSubmissionFact(FrozenContextFact):
    kind: Literal["terminal_command_error"] = "terminal_command_error"
    requested_command: str | None
    failure_stage: str
    status: str
    error: ToolResultErrorPreviewFact
    policy_code: str | None
    observed_cwd: str | None
    terminal_session_id: str | None
    backend_type: str | None
    io_mode: str | None


class TerminalProcessObservationDomainSubmissionFact(FrozenContextFact):
    kind: Literal["terminal_process_observation"] = "terminal_process_observation"
    action: str
    process_id: str
    status: str
    exit_code: int | None
    command: str | None
    cwd: str | None
    timed_out: bool
    output_truncated: bool
    error: ToolResultErrorPreviewFact | None
    yielded_to_background: bool
    terminal_session_id: str
    backend_type: str
    io_mode: str | None
    stdin_closed: bool | None
    policy_code: str | None
    duration_seconds: float | None = Field(default=None, ge=0)


class TerminalProcessInventoryDomainSubmissionFact(FrozenContextFact):
    kind: Literal["terminal_process_inventory"] = "terminal_process_inventory"
    status: str
    live_process_count: int = Field(ge=0)
    finished_process_count: int = Field(ge=0)
    process_summaries: tuple[TerminalProcessSummaryFact, ...]
    omitted_process_count: int = Field(ge=0)
    summaries_truncated: bool


class TerminalProcessErrorDomainSubmissionFact(FrozenContextFact):
    kind: Literal["terminal_process_error"] = "terminal_process_error"
    requested_action: str
    process_id: str | None
    status: str
    error: ToolResultErrorPreviewFact
    policy_code: str | None
    terminal_session_id: str | None
    backend_type: str | None


class ArtifactDomainSubmissionFact(FrozenContextFact):
    kind: Literal["artifact"] = "artifact"
    primary_artifact_id: str | None
    output_truncated: bool
    output_preview_available: bool


ToolResultDomainSubmissionFact: TypeAlias = (
    TerminalCommandDomainSubmissionFact
    | TerminalCommandErrorDomainSubmissionFact
    | TerminalProcessObservationDomainSubmissionFact
    | TerminalProcessInventoryDomainSubmissionFact
    | TerminalProcessErrorDomainSubmissionFact
    | ArtifactDomainSubmissionFact
)


class ExternalToolResultSubmissionFact(FrozenContextFact):
    result_block: FrozenToolResultBlockFact
    observation_timing: ToolObservationTimingFact
    selected_variant_code: ToolResultRenderVariantCode
    domain_result: ToolResultDomainSubmissionFact | None
    terminal_payload_timing: TerminalPayloadTimingFact | None
    submission_fingerprint: str

    @model_validator(mode="after")
    def _submission(self) -> "ExternalToolResultSubmissionFact":
        call_id = self.result_block.tool_call_id
        if self.observation_timing.tool_call_id not in (None, call_id):
            raise ValueError("external submission timing call ID mismatch")
        _validate_fingerprint(
            self, "external-tool-result-submission:v1", "submission_fingerprint"
        )
        return self


class ExternalToolResultIngressFact(FrozenContextFact):
    requirement_ref: ExternalExecutionRequirementReferenceFact
    result_block: FrozenToolResultBlockFact
    observation_timing: ToolObservationTimingFact
    execution_semantics: ToolResultExecutionSemanticsFact
    ingress_fingerprint: str

    @model_validator(mode="after")
    def _ingress(self) -> "ExternalToolResultIngressFact":
        call_id = self.result_block.tool_call_id
        if self.requirement_ref.tool_call_id != call_id:
            raise ValueError("external ingress requirement call ID mismatch")
        if self.observation_timing.tool_call_id not in (None, call_id):
            raise ValueError("external ingress timing call ID mismatch")
        if self.execution_semantics.result_state != self.result_block.result_state:
            raise ValueError("external ingress result state mismatch")
        _validate_fingerprint(
            self, "external-tool-result-ingress:v1", "ingress_fingerprint"
        )
        return self


def validate_tool_result_profile_contract(
    *,
    profile: ToolResultRenderProfileFact,
    contract: CapabilityResultRenderContractFact,
) -> None:
    if profile.render_contract_fingerprint != contract.contract_fingerprint:
        raise ValueError("tool result profile uses a different render contract")
    matches = tuple(
        variant
        for variant in contract.allowed_variants
        if variant.variant_code == profile.selected_variant.variant_code
    )
    if len(matches) != 1 or matches[0] != profile.selected_variant:
        raise ValueError("tool result profile variant is not in render contract")
    binding = contract.semantics_builder_contract
    if (
        contract.semantics_builder_id,
        contract.semantics_builder_version,
        contract.semantics_builder_contract_fingerprint,
    ) != (
        binding.builder_id,
        binding.builder_version,
        binding.contract_fingerprint,
    ):
        raise ValueError("tool result render contract builder identity drift")


def _validate_fingerprint(
    model: FrozenContextFact, namespace: str, field_name: str
) -> None:
    expected = context_fingerprint(
        namespace, model.model_dump(mode="json", exclude={field_name})
    )
    if getattr(model, field_name) != expected:
        raise ValueError(f"{field_name} mismatch")


__all__ = [name for name in globals() if not name.startswith("_")]
