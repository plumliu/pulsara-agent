"""Checkpoint candidates, terminal contracts, and barrier release facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives.authority_materialization import Fingerprint
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    StableEventIdentityFact,
    register_durable_fact,
)
from pulsara_agent.primitives.transcript_projection import (
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
    TranscriptProjectionCheckpointMaterializationFact,
    TranscriptProjectionScopeFact,
    TranscriptProjectionSemanticSourceFact,
    TranscriptProjectionStableSemanticStateFact,
)


NonNegativeInt = Annotated[int, Field(ge=0)]


class TranscriptProjectionCheckpointCandidateFact(FrozenFactBase):
    schema_version: Literal["transcript_projection_checkpoint_candidate.v1"]
    checkpoint_id: str = Field(min_length=1, max_length=128)
    scope: TranscriptProjectionScopeFact
    run_seed_semantic: RunTranscriptSeedSemanticFact
    run_seed_reference: RunTranscriptSeedReferenceFact
    semantic_source: TranscriptProjectionSemanticSourceFact
    stable_semantic_state: TranscriptProjectionStableSemanticStateFact
    materialization: TranscriptProjectionCheckpointMaterializationFact
    materialization_consumer_id: str = Field(min_length=1, max_length=256)
    previous_checkpoint_id: str | None = Field(default=None, max_length=128)
    source_ledger_materialization_generation: NonNegativeInt
    source_consumer_horizon_revision: NonNegativeInt
    candidate_ledger_through_sequence: NonNegativeInt
    candidate_ledger_continuity_accumulator: Fingerprint
    build_contract_fingerprint: Fingerprint
    candidate_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _strong_joins(self) -> "TranscriptProjectionCheckpointCandidateFact":
        source = self.semantic_source
        state = self.stable_semantic_state
        if source.semantic_source_event_count != state.semantic_source_event_count:
            raise ValueError("checkpoint source event count mismatch")
        if source.semantic_source_accumulator != state.semantic_source_accumulator:
            raise ValueError("checkpoint source accumulator mismatch")
        if source.resulting_state_fingerprint != state.state_semantic_fingerprint:
            raise ValueError("checkpoint source resulting state mismatch")
        if self.materialization.semantic_state_fingerprint != (
            state.state_semantic_fingerprint
        ):
            raise ValueError("checkpoint materialization state mismatch")
        if self.materialization.root_manifest_ref.normalized_transcript_fingerprint != (
            state.normalized_transcript_fingerprint
        ):
            raise ValueError("checkpoint root transcript mismatch")
        if self.run_seed_reference.seed_semantic_fingerprint != (
            self.run_seed_semantic.seed_semantic_fingerprint
        ):
            raise ValueError("checkpoint run seed identity mismatch")
        return self


CheckpointFailureStage: TypeAlias = Literal[
    "message_artifact_write",
    "message_artifact_confirmation",
    "leaf_artifact_write",
    "leaf_artifact_confirmation",
    "internal_node_write",
    "internal_node_confirmation",
    "root_write",
    "root_confirmation",
    "checkpoint_precommit_validation",
]
CheckpointCancellationSource: TypeAlias = Literal[
    "user_stop",
    "host_close",
    "session_shutdown",
    "operation_deadline",
]


class CheckpointFailureReasonCode(StrEnum):
    ARTIFACT_WRITE_FAILED = "checkpoint_artifact_write_failed"
    ARTIFACT_CONFIRMATION_FAILED = "checkpoint_artifact_confirmation_failed"
    PRECOMMIT_CONTRACT_MISMATCH = "checkpoint_precommit_contract_mismatch"
    OPERATION_BOUND_EXCEEDED = "checkpoint_operation_bound_exceeded"


class CheckpointCancellationReasonCode(StrEnum):
    USER_STOP = "checkpoint_cancelled_user_stop"
    HOST_CLOSE = "checkpoint_cancelled_host_close"
    SESSION_SHUTDOWN = "checkpoint_cancelled_session_shutdown"
    OPERATION_DEADLINE = "checkpoint_cancelled_operation_deadline"


class CheckpointTerminalDiagnosticCode(StrEnum):
    ARTIFACT_IO_FAILURE = "checkpoint_artifact_io_failure"
    ARTIFACT_CONFIRMATION_MISMATCH = "checkpoint_artifact_confirmation_mismatch"
    PRECOMMIT_VALIDATION_MISMATCH = "checkpoint_precommit_validation_mismatch"
    OPERATION_BOUND_VIOLATION = "checkpoint_operation_bound_violation"


class CheckpointFailureReasonStageRuleFact(FrozenFactBase):
    schema_version: Literal["checkpoint_failure_reason_stage_rule.v1"]
    reason_code: CheckpointFailureReasonCode
    allowed_failure_stages: tuple[CheckpointFailureStage, ...]
    allowed_diagnostic_codes: tuple[CheckpointTerminalDiagnosticCode, ...]
    rule_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_rules(self) -> "CheckpointFailureReasonStageRuleFact":
        if not self.allowed_failure_stages or tuple(self.allowed_failure_stages) != tuple(
            sorted(set(self.allowed_failure_stages))
        ):
            raise ValueError("checkpoint failure stages must be sorted and unique")
        diagnostic_values = tuple(item.value for item in self.allowed_diagnostic_codes)
        if not diagnostic_values or diagnostic_values != tuple(
            sorted(set(diagnostic_values))
        ):
            raise ValueError("checkpoint diagnostic codes must be sorted and unique")
        return self


class CheckpointCancellationReasonRuleFact(FrozenFactBase):
    schema_version: Literal["checkpoint_cancellation_reason_rule.v1"]
    cancellation_source: CheckpointCancellationSource
    reason_code: CheckpointCancellationReasonCode
    rule_fingerprint: Fingerprint


CheckpointDiagnosticSanitizationToken: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=64),
]


class CheckpointDiagnosticSanitizationContractFact(FrozenFactBase):
    schema_version: Literal["checkpoint_diagnostic_sanitization_contract.v2"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    unicode_normalization: Literal["NFC"]
    secret_key_normalization: Literal["casefold_strip_non_alnum"]
    secret_key_tokens: Annotated[
        tuple[CheckpointDiagnosticSanitizationToken, ...],
        Field(min_length=1, max_length=64),
    ]
    secret_marker_tokens: Annotated[
        tuple[CheckpointDiagnosticSanitizationToken, ...],
        Field(min_length=1, max_length=64),
    ]
    max_token_characters: Literal[64]
    max_token_utf8_bytes: Literal[256]
    max_secret_key_tokens_total_utf8_bytes: Literal[4096]
    max_secret_marker_tokens_total_utf8_bytes: Literal[4096]
    max_all_secret_tokens_total_utf8_bytes: Literal[4096]
    url_userinfo_policy: Literal["remove"]
    url_query_policy: Literal["remove"]
    url_fragment_policy: Literal["remove"]
    header_policy: Literal["remove_all"]
    cookie_policy: Literal["remove_all"]
    control_character_policy: Literal["replace_with_space"]
    redaction_token: Literal["[redacted]"]
    max_sanitization_passes: Literal[4]
    fixed_point_required: Literal[True]
    secret_safe_validation_required: Literal[True]
    max_output_characters: Literal[256]
    max_output_utf8_bytes: Literal[1024]
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounded_tokens(self) -> "CheckpointDiagnosticSanitizationContractFact":
        aggregate_bytes = 0
        for name in ("secret_key_tokens", "secret_marker_tokens"):
            tokens = getattr(self, name)
            if tokens != tuple(sorted(set(tokens))):
                raise ValueError(f"{name} must be sorted and unique")
            if any(len(token.encode("utf-8")) > self.max_token_utf8_bytes for token in tokens):
                raise ValueError(f"{name} token exceeds UTF-8 byte bound")
            limit = (
                self.max_secret_key_tokens_total_utf8_bytes
                if name == "secret_key_tokens"
                else self.max_secret_marker_tokens_total_utf8_bytes
            )
            token_bytes = sum(len(token.encode("utf-8")) for token in tokens)
            if token_bytes > limit:
                raise ValueError(f"{name} exceeds aggregate UTF-8 byte bound")
            aggregate_bytes += token_bytes
        if aggregate_bytes > self.max_all_secret_tokens_total_utf8_bytes:
            raise ValueError("secret token tuples exceed combined UTF-8 byte bound")
        return self


class CheckpointDiagnosticSanitizer(Protocol):
    def sanitize_once(self, current_detail: str) -> str | None: ...

    def is_secret_safe(self, sanitized_detail: str | None) -> bool: ...


@dataclass(frozen=True, slots=True)
class CheckpointDiagnosticSanitizerBinding:
    contract_id: str
    contract_version: str
    contract_fingerprint: str
    implementation_build_fingerprint: str | None
    sanitizer: CheckpointDiagnosticSanitizer


class CheckpointDiagnosticSanitizerRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._bindings: dict[
            tuple[str, str], CheckpointDiagnosticSanitizerBinding
        ] = {}

    def register(self, binding: CheckpointDiagnosticSanitizerBinding) -> None:
        key = (binding.contract_id, binding.contract_version)
        with self._lock:
            current = self._bindings.get(key)
            if current is not None and current != binding:
                raise ValueError("checkpoint sanitizer binding conflict")
            self._bindings[key] = binding

    def resolve_binding(
        self,
        contract_id: str,
        contract_version: str,
    ) -> CheckpointDiagnosticSanitizerBinding:
        with self._lock:
            try:
                return self._bindings[(contract_id, contract_version)]
            except KeyError as exc:
                raise ValueError("checkpoint sanitizer binding is missing") from exc


def sanitize_checkpoint_diagnostic_detail(
    raw_detail: str | None,
    *,
    contract: CheckpointDiagnosticSanitizationContractFact,
    registry: CheckpointDiagnosticSanitizerRegistry,
) -> str | None:
    """Apply the sole V1 four-pass convergence algorithm."""

    if raw_detail is None:
        return None
    binding = registry.resolve_binding(contract.contract_id, contract.contract_version)
    if binding.contract_fingerprint != contract.contract_fingerprint:
        raise ValueError("checkpoint sanitizer contract fingerprint mismatch")
    current = raw_detail
    for _ in range(4):
        try:
            next_detail = binding.sanitizer.sanitize_once(current)
        except BaseException:
            return None
        if next_detail is None:
            return None
        if next_detail == current:
            try:
                safe = binding.sanitizer.is_secret_safe(next_detail)
            except BaseException:
                return None
            if not safe:
                return None
            if len(next_detail) > contract.max_output_characters:
                return None
            if len(next_detail.encode("utf-8")) > contract.max_output_utf8_bytes:
                return None
            return next_detail
        current = next_detail
    return None


class CheckpointTerminalContractFact(FrozenFactBase):
    schema_version: Literal["checkpoint_terminal_contract.v1"]
    contract_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=64)
    failure_rules: tuple[CheckpointFailureReasonStageRuleFact, ...]
    cancellation_rules: tuple[CheckpointCancellationReasonRuleFact, ...]
    max_diagnostics: Literal[8]
    max_diagnostic_characters: Literal[256]
    max_diagnostic_utf8_bytes: Literal[1024]
    diagnostic_sanitization_contract: CheckpointDiagnosticSanitizationContractFact
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _complete_matrix(self) -> "CheckpointTerminalContractFact":
        failure_reasons = tuple(item.reason_code.value for item in self.failure_rules)
        if failure_reasons != tuple(sorted(set(failure_reasons))):
            raise ValueError("checkpoint failure rules must be sorted and unique")
        if set(failure_reasons) != {item.value for item in CheckpointFailureReasonCode}:
            raise ValueError("checkpoint failure rule matrix is incomplete")
        cancellation_sources = tuple(
            item.cancellation_source for item in self.cancellation_rules
        )
        if cancellation_sources != tuple(sorted(set(cancellation_sources))):
            raise ValueError("checkpoint cancellation rules must be sorted and unique")
        expected_sources = {"user_stop", "host_close", "session_shutdown", "operation_deadline"}
        if set(cancellation_sources) != expected_sources:
            raise ValueError("checkpoint cancellation rule matrix is incomplete")
        expected_pairs = {
            "user_stop": CheckpointCancellationReasonCode.USER_STOP,
            "host_close": CheckpointCancellationReasonCode.HOST_CLOSE,
            "session_shutdown": CheckpointCancellationReasonCode.SESSION_SHUTDOWN,
            "operation_deadline": CheckpointCancellationReasonCode.OPERATION_DEADLINE,
        }
        if any(expected_pairs[item.cancellation_source] is not item.reason_code for item in self.cancellation_rules):
            raise ValueError("checkpoint cancellation reason matrix mismatch")
        return self


class CheckpointTerminalDiagnosticFact(FrozenFactBase):
    schema_version: Literal["checkpoint_terminal_diagnostic.v2"]
    diagnostic_code: CheckpointTerminalDiagnosticCode
    failure_stage: CheckpointFailureStage
    sanitized_detail: Annotated[str | None, Field(max_length=256)] = None
    diagnostic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _detail_bytes(self) -> "CheckpointTerminalDiagnosticFact":
        if self.sanitized_detail is not None and len(
            self.sanitized_detail.encode("utf-8")
        ) > 1_024:
            raise ValueError("checkpoint diagnostic exceeds UTF-8 byte bound")
        return self


class CheckpointCommittedTerminalReferenceFact(FrozenFactBase):
    schema_version: Literal["checkpoint_committed_terminal_ref.v1"]
    release_outcome: Literal["checkpoint_committed"]
    checkpoint_committed_event_identity: StableEventIdentityFact
    consumer_horizon_advanced_event_identity: StableEventIdentityFact
    generation_advanced_event_identity: StableEventIdentityFact | None
    terminal_reference_fingerprint: Fingerprint


class CheckpointFailedTerminalReferenceFact(FrozenFactBase):
    schema_version: Literal["checkpoint_failed_terminal_ref.v1"]
    release_outcome: Literal["checkpoint_failed"]
    checkpoint_failed_event_identity: StableEventIdentityFact
    terminal_reference_fingerprint: Fingerprint


class CheckpointCancelledTerminalReferenceFact(FrozenFactBase):
    schema_version: Literal["checkpoint_cancelled_terminal_ref.v1"]
    release_outcome: Literal["checkpoint_cancelled"]
    checkpoint_cancelled_event_identity: StableEventIdentityFact
    terminal_reference_fingerprint: Fingerprint


class CheckpointRecoveredInterruptedTerminalReferenceFact(FrozenFactBase):
    schema_version: Literal["checkpoint_recovered_interrupted_terminal_ref.v1"]
    release_outcome: Literal["recovered_interrupted"]
    checkpoint_recovered_interrupted_event_identity: StableEventIdentityFact
    terminal_reference_fingerprint: Fingerprint


CheckpointBarrierTerminalReferenceFact: TypeAlias = Annotated[
    CheckpointCommittedTerminalReferenceFact
    | CheckpointFailedTerminalReferenceFact
    | CheckpointCancelledTerminalReferenceFact
    | CheckpointRecoveredInterruptedTerminalReferenceFact,
    Field(discriminator="release_outcome"),
]


class CheckpointDispatchBarrierReleaseFact(FrozenFactBase):
    schema_version: Literal["checkpoint_dispatch_barrier_release.v1"]
    barrier_id: str = Field(min_length=1, max_length=128)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_candidate_fingerprint: Fingerprint
    terminal_reference: CheckpointBarrierTerminalReferenceFact
    maintenance_settlement_event_identity: StableEventIdentityFact
    release_fingerprint: Fingerprint


_OWN: tuple[tuple[str, str | None, str], ...] = (
    ("transcript_projection_checkpoint_candidate.v1", "candidate_fingerprint", "transcript-projection-checkpoint-candidate:v1"),
    ("checkpoint_failure_reason_stage_rule.v1", "rule_fingerprint", "checkpoint-failure-reason-stage-rule:v1"),
    ("checkpoint_cancellation_reason_rule.v1", "rule_fingerprint", "checkpoint-cancellation-reason-rule:v1"),
    ("checkpoint_diagnostic_sanitization_contract.v2", "contract_fingerprint", "checkpoint-diagnostic-sanitization-contract:v2"),
    ("checkpoint_terminal_contract.v1", "contract_fingerprint", "checkpoint-terminal-contract:v1"),
    ("checkpoint_terminal_diagnostic.v2", "diagnostic_fingerprint", "checkpoint-terminal-diagnostic:v2"),
    ("checkpoint_committed_terminal_ref.v1", "terminal_reference_fingerprint", "checkpoint-committed-terminal-ref:v1"),
    ("checkpoint_failed_terminal_ref.v1", "terminal_reference_fingerprint", "checkpoint-failed-terminal-ref:v1"),
    ("checkpoint_cancelled_terminal_ref.v1", "terminal_reference_fingerprint", "checkpoint-cancelled-terminal-ref:v1"),
    ("checkpoint_recovered_interrupted_terminal_ref.v1", "terminal_reference_fingerprint", "checkpoint-recovered-interrupted-terminal-ref:v1"),
    ("checkpoint_dispatch_barrier_release.v1", "release_fingerprint", "checkpoint-dispatch-barrier-release:v1"),
)

for _schema, _field, _domain in _OWN:
    register_durable_fact(
        schema_version=_schema,
        own_fingerprint_field=_field,
        domain_separator=_domain,
    )


__all__ = [
    "CheckpointBarrierTerminalReferenceFact",
    "CheckpointCancellationReasonCode",
    "CheckpointCancellationReasonRuleFact",
    "CheckpointCancellationSource",
    "CheckpointCommittedTerminalReferenceFact",
    "CheckpointDiagnosticSanitizationContractFact",
    "CheckpointDiagnosticSanitizer",
    "CheckpointDiagnosticSanitizerBinding",
    "CheckpointDiagnosticSanitizerRegistry",
    "CheckpointDispatchBarrierReleaseFact",
    "CheckpointFailedTerminalReferenceFact",
    "CheckpointFailureReasonCode",
    "CheckpointFailureReasonStageRuleFact",
    "CheckpointFailureStage",
    "CheckpointRecoveredInterruptedTerminalReferenceFact",
    "CheckpointTerminalContractFact",
    "CheckpointTerminalDiagnosticCode",
    "CheckpointTerminalDiagnosticFact",
    "TranscriptProjectionCheckpointCandidateFact",
    "sanitize_checkpoint_diagnostic_detail",
]
