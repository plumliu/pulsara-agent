"""Compact durable authority for one compiled provider dispatch.

These facts deliberately contain only stable semantic identities and durable
references.  Compiler audit materialization is a separate, optional storage
plane and must never participate in model admission.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    LedgerAuthorityHorizonFact,
    LedgerAuthorityHorizonSetReferenceFact,
)
from pulsara_agent.primitives.frozen import (
    FrozenFactBase,
    build_frozen_fact,
    register_durable_fact,
)
from pulsara_agent.primitives.provider_input import (
    ProviderOrderedTranscriptProjectionIdentityFact,
)
from pulsara_agent.primitives.transcript_projection import (
    RunTranscriptSeedReferenceFact,
    TranscriptProjectionRootManifestRefFact,
)


Fingerprint = str
MAX_CONTEXT_COMPILE_COMMIT_BYTES = 64 * 1024
MAX_CONTEXT_INPUT_AUDIT_EXPECTATION_BYTES = 8 * 1024


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


@_fact(
    "run_seed_projection_base_reference.v1",
    "reference_fingerprint",
    "run-seed-projection-base-reference:v1",
)
class RunSeedProjectionBaseReferenceFact(FrozenFactBase):
    schema_version: Literal["run_seed_projection_base_reference.v1"] = (
        "run_seed_projection_base_reference.v1"
    )
    base_kind: Literal["run_seed"] = "run_seed"
    run_seed_reference: RunTranscriptSeedReferenceFact
    stable_semantic_state_fingerprint: Fingerprint
    source_base_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


@_fact(
    "checkpoint_projection_base_reference.v1",
    "reference_fingerprint",
    "checkpoint-projection-base-reference:v1",
)
class CheckpointProjectionBaseReferenceFact(FrozenFactBase):
    schema_version: Literal["checkpoint_projection_base_reference.v1"] = (
        "checkpoint_projection_base_reference.v1"
    )
    base_kind: Literal["checkpoint"] = "checkpoint"
    run_seed_reference: RunTranscriptSeedReferenceFact
    checkpoint_id: str = Field(min_length=1, max_length=128)
    checkpoint_committed_event_id: str = Field(min_length=1, max_length=128)
    checkpoint_root_reference: TranscriptProjectionRootManifestRefFact
    checkpoint_through_sequence: int = Field(ge=0)
    stable_semantic_state_fingerprint: Fingerprint
    source_base_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


TranscriptProjectionBaseReferenceFact: TypeAlias = Annotated[
    RunSeedProjectionBaseReferenceFact | CheckpointProjectionBaseReferenceFact,
    Field(discriminator="base_kind"),
]


@_fact(
    "context_compile_authority_reference_set.v1",
    "reference_set_fingerprint",
    "context-compile-authority-reference-set:v1",
)
class ContextCompileSourceReferenceSetFact(FrozenFactBase):
    schema_version: Literal["context_compile_authority_reference_set.v1"] = (
        "context_compile_authority_reference_set.v1"
    )
    run_start_event_reference: ContextEventReferenceFact
    continuation_event_reference: ContextEventReferenceFact | None
    primary_ledger_horizon: LedgerAuthorityHorizonFact
    authority_horizon_set_reference: LedgerAuthorityHorizonSetReferenceFact
    transcript_projection_base_reference: TranscriptProjectionBaseReferenceFact
    reference_set_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "ContextCompileSourceReferenceSetFact":
        if (
            self.run_start_event_reference.runtime_session_id
            != self.primary_ledger_horizon.runtime_session_id
        ):
            raise ValueError("context compile RunStart/primary horizon owner mismatch")
        if self.continuation_event_reference is not None and (
            self.continuation_event_reference.runtime_session_id
            != self.primary_ledger_horizon.runtime_session_id
        ):
            raise ValueError("context compile continuation owner mismatch")
        return self


@_fact(
    "context_compile_input_commit.v1",
    "commit_fingerprint",
    "context-compile-input-commit:v1",
)
class ContextCompileInputCommitFact(FrozenFactBase):
    schema_version: Literal["context_compile_input_commit.v1"] = (
        "context_compile_input_commit.v1"
    )
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    resolved_model_call_id: str = Field(min_length=1)
    resolved_model_target_fingerprint: Fingerprint
    model_call_index: int = Field(ge=1)
    compile_attempt_index: int = Field(ge=1)
    context_retry_index: int = Field(ge=0)
    source_through_sequence: int = Field(ge=1)
    source_references: ContextCompileSourceReferenceSetFact
    snapshot_semantic_fingerprint: Fingerprint
    ordered_projection_identity: ProviderOrderedTranscriptProjectionIdentityFact
    prepared_provider_input_plan_fingerprint: Fingerprint
    canonical_provider_input_plan_fingerprint: Fingerprint
    provider_neutral_payload_fingerprint: Fingerprint
    input_aggregate_fingerprint: Fingerprint
    canonical_render_decisions_fingerprint: Fingerprint
    token_estimator_fingerprint: Fingerprint
    input_budget_tokens: int = Field(ge=1)
    final_payload_estimated_tokens: int = Field(ge=0)
    budget_decision_fingerprint: Fingerprint
    commit_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "ContextCompileInputCommitFact":
        primary = self.source_references.primary_ledger_horizon
        if (
            primary.runtime_session_id != self.runtime_session_id
            or primary.through_sequence != self.source_through_sequence
        ):
            raise ValueError("context compile commit primary horizon mismatch")
        expected_budget = context_fingerprint(
            "context-compile-budget-decision:v1",
            {
                "resolved_model_target_fingerprint": (
                    self.resolved_model_target_fingerprint
                ),
                "token_estimator_fingerprint": self.token_estimator_fingerprint,
                "input_budget_tokens": self.input_budget_tokens,
                "final_payload_estimated_tokens": (self.final_payload_estimated_tokens),
            },
        )
        if self.budget_decision_fingerprint != expected_budget:
            raise ValueError("context compile budget decision fingerprint mismatch")
        if self.final_payload_estimated_tokens > self.input_budget_tokens:
            raise ValueError("compiled provider payload exceeds its input budget")
        if len(canonical_json_bytes(self)) > MAX_CONTEXT_COMPILE_COMMIT_BYTES:
            raise ValueError("context compile semantic commit exceeds 64 KiB")
        return self


@_fact(
    "context_input_audit_expectation.v1",
    "expectation_fingerprint",
    "context-input-audit-expectation:v1",
)
class ContextInputAuditExpectationFact(FrozenFactBase):
    schema_version: Literal["context_input_audit_expectation.v1"] = (
        "context_input_audit_expectation.v1"
    )
    semantic_commit_fingerprint: Fingerprint
    audit_contract_id: str = Field(min_length=1, max_length=128)
    audit_contract_version: str = Field(min_length=1, max_length=64)
    audit_contract_fingerprint: Fingerprint
    materialization_key: Fingerprint
    expected_plan_artifact_id: str = Field(min_length=1, max_length=256)
    expected_root_artifact_id: str = Field(min_length=1, max_length=256)
    expected_root_semantic_fingerprint: Fingerprint
    expectation_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _identity(self) -> "ContextInputAuditExpectationFact":
        expected_key = context_fingerprint(
            "context-input-audit-materialization-key:v1",
            (
                self.semantic_commit_fingerprint,
                self.audit_contract_id,
                self.audit_contract_version,
                self.audit_contract_fingerprint,
            ),
        )
        if self.materialization_key != expected_key:
            raise ValueError("context input audit materialization key mismatch")
        expected_plan_id = "context-input-audit-plan:" + context_fingerprint(
            "context-input-audit-plan-id:v1", self.materialization_key
        ).removeprefix("sha256:")
        expected_root_id = "context-input-audit-root:" + context_fingerprint(
            "context-input-audit-root-id:v1", self.materialization_key
        ).removeprefix("sha256:")
        expected_semantic = context_fingerprint(
            "context-input-audit-root-semantic:v1",
            (
                self.semantic_commit_fingerprint,
                self.audit_contract_fingerprint,
            ),
        )
        if (
            self.expected_plan_artifact_id != expected_plan_id
            or self.expected_root_artifact_id != expected_root_id
            or self.expected_root_semantic_fingerprint != expected_semantic
        ):
            raise ValueError("context input audit expected artifact identity mismatch")
        if len(canonical_json_bytes(self)) > MAX_CONTEXT_INPUT_AUDIT_EXPECTATION_BYTES:
            raise ValueError("context input audit expectation exceeds 8 KiB")
        return self


def build_context_input_audit_expectation(
    *,
    semantic_commit_fingerprint: Fingerprint,
    audit_contract_id: str,
    audit_contract_version: str,
    audit_contract_fingerprint: Fingerprint,
) -> ContextInputAuditExpectationFact:
    materialization_key = context_fingerprint(
        "context-input-audit-materialization-key:v1",
        (
            semantic_commit_fingerprint,
            audit_contract_id,
            audit_contract_version,
            audit_contract_fingerprint,
        ),
    )
    return build_frozen_fact(
        ContextInputAuditExpectationFact,
        schema_version="context_input_audit_expectation.v1",
        semantic_commit_fingerprint=semantic_commit_fingerprint,
        audit_contract_id=audit_contract_id,
        audit_contract_version=audit_contract_version,
        audit_contract_fingerprint=audit_contract_fingerprint,
        materialization_key=materialization_key,
        expected_plan_artifact_id=(
            "context-input-audit-plan:"
            + context_fingerprint(
                "context-input-audit-plan-id:v1", materialization_key
            ).removeprefix("sha256:")
        ),
        expected_root_artifact_id=(
            "context-input-audit-root:"
            + context_fingerprint(
                "context-input-audit-root-id:v1", materialization_key
            ).removeprefix("sha256:")
        ),
        expected_root_semantic_fingerprint=context_fingerprint(
            "context-input-audit-root-semantic:v1",
            (semantic_commit_fingerprint, audit_contract_fingerprint),
        ),
    )


__all__ = [
    "CheckpointProjectionBaseReferenceFact",
    "ContextCompileInputCommitFact",
    "ContextCompileSourceReferenceSetFact",
    "ContextInputAuditExpectationFact",
    "MAX_CONTEXT_COMPILE_COMMIT_BYTES",
    "MAX_CONTEXT_INPUT_AUDIT_EXPECTATION_BYTES",
    "RunSeedProjectionBaseReferenceFact",
    "TranscriptProjectionBaseReferenceFact",
    "build_context_input_audit_expectation",
]
