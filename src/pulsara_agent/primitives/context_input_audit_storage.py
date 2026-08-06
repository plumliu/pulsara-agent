"""Storage-only vocabulary for optional context-input compiler audit data."""

from __future__ import annotations

from enum import StrEnum
import hashlib
from typing import Any, Literal

from pydantic import Field, model_validator

from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.storage_frozen import (
    FrozenStorageFactBase,
    build_frozen_storage_fact,
    register_durable_storage_fact,
)


Fingerprint = str

CONTEXT_INPUT_AUDIT_PLAN_MEDIA_TYPE = (
    "application/vnd.pulsara.context-input-audit-plan+json;version=1"
)
CONTEXT_INPUT_AUDIT_PAGE_MEDIA_TYPE = (
    "application/vnd.pulsara.context-input-audit-page+json;version=1"
)
CONTEXT_INPUT_AUDIT_ROOT_MEDIA_TYPE = (
    "application/vnd.pulsara.context-input-audit-root+json;version=1"
)

MAX_AUDIT_COMPONENT_REFERENCES = 256
MAX_AUDIT_INLINE_ITEM_BYTES = 8 * 1024
MAX_AUDIT_TOTAL_INLINE_BYTES = 64 * 1024
MAX_AUDIT_PAGES = 64
MAX_AUDIT_PAGE_CANONICAL_BYTES = 256 * 1024
MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES = 16 * 1024 * 1024
MAX_AUDIT_PLAN_CANONICAL_BYTES = 128 * 1024
MAX_AUDIT_ROOT_CANONICAL_BYTES = 64 * 1024


def _ordered_accumulator(domain: str, values: tuple[str, ...]) -> str:
    accumulator = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        accumulator = context_fingerprint(f"{domain}:step", (accumulator, value))
    return accumulator


class ContextInputAuditComponentKind(StrEnum):
    SNAPSHOT = "snapshot"
    SUBAGENT_GRAPH_SEMANTIC_SOURCE = "subagent_graph_semantic_source"
    SUBAGENT_GRAPH_ACCELERATION = "subagent_graph_acceleration"
    PREPARED_CANDIDATE_SET = "prepared_candidate_set"
    ORDERED_TRANSCRIPT_PROJECTION_IDENTITY = "ordered_transcript_projection_identity"
    PREPARED_PROVIDER_INPUT_PLAN = "prepared_provider_input_plan"
    CANONICAL_PROVIDER_INPUT_PLAN = "canonical_provider_input_plan"
    TRANSCRIPT_PROVIDER_PROJECTION = "transcript_provider_projection"
    TRANSCRIPT_AUTHORITY = "transcript_authority"
    TOOL_RESULT_RENDER_POLICY = "tool_result_render_policy"
    ACTIVE_WINDOW = "active_window"
    WINDOW_POLICY = "window_policy"
    PROJECTION_STATE = "projection_state"
    PROJECTED_TOOL_RESULT_REFS = "projected_tool_result_refs"
    PREPARED_ROLLUP_UNITS = "prepared_rollup_units"
    ROLLOUT_STATE = "rollout_state"
    CONTEXT_BUDGET_DECISION = "context_budget_decision"
    PROJECTION_PRESSURE_SHADOW = "projection_pressure_shadow"
    PROJECTION_TARGET_UNREACHABLE = "projection_target_unreachable"
    COMPILED_SECTIONS = "compiled_sections"
    COMPILED_TOOL_SPECS = "compiled_tool_specs"
    COMPILED_DIAGNOSTICS = "compiled_diagnostics"
    COMPILED_LIFECYCLE_DECISIONS = "compiled_lifecycle_decisions"
    TOOL_RESULT_RENDER_DECISIONS = "tool_result_render_decisions"
    TOOL_RESULT_BUDGET_REPORT = "tool_result_budget_report"
    TOOL_RESULT_RENDER_DECISION_FACTS = "tool_result_render_decision_facts"
    TOOL_RESULT_RENDER_OPERATIONAL_FACTS = "tool_result_render_operational_facts"
    MODEL_START_ATTRIBUTION = "model_start_attribution"


class ContextInputAuditComponentOwnership(StrEnum):
    """Physical ownership of one audit-plan component.

    Existing canonical authorities are represented by a bounded reference
    carrier and can never spill into audit-owned pages.  Only invocation detail
    with no other durable owner may be page-owned.
    """

    EXISTING_AUTHORITY_REFERENCE = "existing_authority_reference"
    PAGE_OWNED_DETAIL = "page_owned_detail"


def _storage_fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_storage_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


@_storage_fact(
    "context_input_audit_stored_artifact_reference.v1",
    "reference_fingerprint",
    "context-input-audit-stored-artifact-reference:v1",
)
class ContextInputAuditStoredArtifactReferenceFact(FrozenStorageFactBase):
    schema_version: Literal["context_input_audit_stored_artifact_reference.v1"]
    artifact_id: str = Field(min_length=1, max_length=512)
    content_sha256: Fingerprint
    content_bytes: int = Field(ge=1)
    media_type: str = Field(min_length=1, max_length=128)
    storage_fact_schema_version: str = Field(min_length=1, max_length=128)
    storage_fact_fingerprint: Fingerprint
    semantic_metadata_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint


@_storage_fact(
    "context_input_audit_component_reference.v1",
    "component_reference_fingerprint",
    "context-input-audit-component-reference:v1",
)
class ContextInputAuditComponentReferenceFact(FrozenStorageFactBase):
    schema_version: Literal["context_input_audit_component_reference.v1"]
    component_kind: ContextInputAuditComponentKind
    component_ownership: ContextInputAuditComponentOwnership
    component_ordinal: int = Field(ge=0)
    canonical_payload_sha256: Fingerprint
    canonical_payload_bytes: int = Field(ge=0)
    storage_kind: Literal["inline", "paged"]
    inline_canonical_json: str | None
    page_ordinals: tuple[int, ...]
    ordered_page_accumulator: Fingerprint
    component_reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _storage_matrix(self) -> "ContextInputAuditComponentReferenceFact":
        if (
            self.component_ownership
            is ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE
            and self.storage_kind != "inline"
        ):
            raise ValueError("existing audit authority reference cannot own pages")
        if self.storage_kind == "inline":
            if self.inline_canonical_json is None or self.page_ordinals:
                raise ValueError("inline audit component storage matrix mismatch")
            encoded = self.inline_canonical_json.encode("utf-8")
            if len(encoded) > MAX_AUDIT_INLINE_ITEM_BYTES:
                raise ValueError("inline audit component exceeds 8 KiB")
            if (
                len(encoded) != self.canonical_payload_bytes
                or "sha256:" + hashlib.sha256(encoded).hexdigest()
                != self.canonical_payload_sha256
            ):
                raise ValueError("inline audit component payload identity mismatch")
        elif self.inline_canonical_json is not None or not self.page_ordinals:
            raise ValueError("paged audit component storage matrix mismatch")
        if self.page_ordinals != tuple(sorted(set(self.page_ordinals))):
            raise ValueError("audit component page ordinals must be ordered/unique")
        return self


@_storage_fact(
    "context_input_audit_page.v1",
    "page_storage_fingerprint",
    "context-input-audit-page:v1",
)
class ContextInputAuditPageFact(FrozenStorageFactBase):
    schema_version: Literal["context_input_audit_page.v1"]
    source_runtime_session_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    materialization_key: Fingerprint
    page_ordinal: int = Field(ge=0)
    component_kind: ContextInputAuditComponentKind
    component_ordinal: int = Field(ge=0)
    fragment_ordinal: int = Field(ge=0)
    fragment_count: int = Field(ge=1)
    canonical_json_fragment: str
    canonical_payload_sha256: Fingerprint
    canonical_payload_bytes: int = Field(ge=0)
    page_storage_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounds(self) -> "ContextInputAuditPageFact":
        if self.fragment_ordinal >= self.fragment_count:
            raise ValueError("audit page fragment ordinal is outside its component")
        if (
            len(self.canonical_json_fragment.encode("utf-8"))
            != self.canonical_payload_bytes
        ):
            raise ValueError("audit page fragment byte attribution mismatch")
        if (
            "sha256:"
            + hashlib.sha256(self.canonical_json_fragment.encode("utf-8")).hexdigest()
            != self.canonical_payload_sha256
        ):
            raise ValueError("audit page fragment fingerprint mismatch")
        if len(canonical_json_bytes(self)) > MAX_AUDIT_PAGE_CANONICAL_BYTES:
            raise ValueError("context input audit page exceeds 256 KiB")
        return self


@_storage_fact(
    "context_input_audit_materialization_plan.v1",
    "plan_fingerprint",
    "context-input-audit-materialization-plan:v1",
)
class ContextInputAuditMaterializationPlanFact(FrozenStorageFactBase):
    schema_version: Literal["context_input_audit_materialization_plan.v1"]
    source_runtime_session_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    source_context_id: str = Field(min_length=1)
    source_resolved_model_call_id: str = Field(min_length=1)
    semantic_commit_fingerprint: Fingerprint
    expectation_fingerprint: Fingerprint
    materialization_key: Fingerprint
    expected_root_artifact_id: str = Field(min_length=1, max_length=512)
    expected_root_semantic_fingerprint: Fingerprint
    audit_contract_fingerprint: Fingerprint
    components: tuple[ContextInputAuditComponentReferenceFact, ...]
    page_references: tuple[ContextInputAuditStoredArtifactReferenceFact, ...]
    component_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    total_inline_bytes: int = Field(ge=0)
    total_page_canonical_bytes: int = Field(ge=0)
    ordered_component_accumulator: Fingerprint
    ordered_page_accumulator: Fingerprint
    plan_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounds(self) -> "ContextInputAuditMaterializationPlanFact":
        if self.component_count != len(self.components) or self.page_count != len(
            self.page_references
        ):
            raise ValueError("context input audit plan count mismatch")
        component_ordinals = tuple(item.component_ordinal for item in self.components)
        if component_ordinals != tuple(range(len(self.components))):
            raise ValueError("context input audit components are not contiguous")
        component_kinds = tuple(item.component_kind for item in self.components)
        if len(component_kinds) != len(set(component_kinds)):
            raise ValueError("context input audit plan repeats a component kind")
        registry_ordinal = {
            kind: ordinal for ordinal, kind in enumerate(ContextInputAuditComponentKind)
        }
        if tuple(registry_ordinal[item] for item in component_kinds) != tuple(
            sorted(registry_ordinal[item] for item in component_kinds)
        ):
            raise ValueError("context input audit components violate registry order")
        if not component_kinds or component_kinds[-1] is not (
            ContextInputAuditComponentKind.MODEL_START_ATTRIBUTION
        ):
            raise ValueError(
                "context input audit plan lacks terminal Start attribution"
            )
        if tuple(reference.artifact_id for reference in self.page_references) != tuple(
            dict.fromkeys(item.artifact_id for item in self.page_references)
        ):
            raise ValueError("context input audit plan repeats a page")
        if self.component_count > MAX_AUDIT_COMPONENT_REFERENCES:
            raise ValueError("context input audit component bound exceeded")
        if self.page_count > MAX_AUDIT_PAGES:
            raise ValueError("context input audit page-count bound exceeded")
        if self.total_inline_bytes > MAX_AUDIT_TOTAL_INLINE_BYTES:
            raise ValueError("context input audit inline-byte bound exceeded")
        if self.total_page_canonical_bytes > MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES:
            raise ValueError("context input audit total page-byte bound exceeded")
        expected_page_ordinals = tuple(
            ordinal
            for component in self.components
            for ordinal in component.page_ordinals
        )
        if expected_page_ordinals != tuple(range(self.page_count)):
            raise ValueError("context input audit page coverage is not contiguous")
        for component in self.components:
            expected_component_page_accumulator = _ordered_accumulator(
                "context-input-audit-component-pages:v1",
                tuple(
                    self.page_references[ordinal].storage_fact_fingerprint
                    for ordinal in component.page_ordinals
                ),
            )
            if (
                component.ordered_page_accumulator
                != expected_component_page_accumulator
            ):
                raise ValueError(
                    "context input audit component/page accumulator mismatch"
                )
        if any(
            reference.media_type != CONTEXT_INPUT_AUDIT_PAGE_MEDIA_TYPE
            or reference.storage_fact_schema_version != "context_input_audit_page.v1"
            for reference in self.page_references
        ):
            raise ValueError("context input audit plan carries a non-page reference")
        if self.total_inline_bytes != sum(
            component.canonical_payload_bytes
            for component in self.components
            if component.storage_kind == "inline"
        ):
            raise ValueError("context input audit inline-byte attribution mismatch")
        if self.total_page_canonical_bytes != sum(
            reference.content_bytes for reference in self.page_references
        ):
            raise ValueError("context input audit page-byte attribution mismatch")
        if self.ordered_component_accumulator != _ordered_accumulator(
            "context-input-audit-components:v1",
            tuple(item.component_reference_fingerprint for item in self.components),
        ):
            raise ValueError("context input audit component accumulator mismatch")
        if self.ordered_page_accumulator != _ordered_accumulator(
            "context-input-audit-pages:v1",
            tuple(item.reference_fingerprint for item in self.page_references),
        ):
            raise ValueError("context input audit page accumulator mismatch")
        if len(canonical_json_bytes(self)) > MAX_AUDIT_PLAN_CANONICAL_BYTES:
            raise ValueError("context input audit plan exceeds 128 KiB")
        return self


@_storage_fact(
    "context_input_audit_root.v1",
    "root_materialization_fingerprint",
    "context-input-audit-root:v1",
)
class ContextInputAuditRootFact(FrozenStorageFactBase):
    schema_version: Literal["context_input_audit_root.v1"]
    source_runtime_session_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    source_context_id: str = Field(min_length=1)
    source_resolved_model_call_id: str = Field(min_length=1)
    semantic_commit_fingerprint: Fingerprint
    materialization_key: Fingerprint
    plan_artifact_reference: ContextInputAuditStoredArtifactReferenceFact
    component_count: int = Field(ge=0, le=MAX_AUDIT_COMPONENT_REFERENCES)
    page_count: int = Field(ge=0, le=MAX_AUDIT_PAGES)
    ordered_component_accumulator: Fingerprint
    ordered_page_accumulator: Fingerprint
    materialization_contract_fingerprint: Fingerprint
    root_semantic_fingerprint: Fingerprint
    root_materialization_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bounds(self) -> "ContextInputAuditRootFact":
        if (
            self.plan_artifact_reference.media_type
            != CONTEXT_INPUT_AUDIT_PLAN_MEDIA_TYPE
            or self.plan_artifact_reference.storage_fact_schema_version
            != "context_input_audit_materialization_plan.v1"
        ):
            raise ValueError("context input audit root carries a non-plan reference")
        if len(canonical_json_bytes(self)) > MAX_AUDIT_ROOT_CANONICAL_BYTES:
            raise ValueError("context input audit root exceeds 64 KiB")
        return self


ContextInputAuditStorageFact = (
    ContextInputAuditPageFact
    | ContextInputAuditMaterializationPlanFact
    | ContextInputAuditRootFact
)


def build_context_input_audit_storage_fact(
    fact_type: type[ContextInputAuditStorageFact],
    /,
    **payload: Any,
) -> ContextInputAuditStorageFact:
    return build_frozen_storage_fact(fact_type, **payload)


__all__ = [
    "CONTEXT_INPUT_AUDIT_PAGE_MEDIA_TYPE",
    "CONTEXT_INPUT_AUDIT_PLAN_MEDIA_TYPE",
    "CONTEXT_INPUT_AUDIT_ROOT_MEDIA_TYPE",
    "ContextInputAuditComponentKind",
    "ContextInputAuditComponentOwnership",
    "ContextInputAuditComponentReferenceFact",
    "ContextInputAuditMaterializationPlanFact",
    "ContextInputAuditPageFact",
    "ContextInputAuditRootFact",
    "ContextInputAuditStorageFact",
    "ContextInputAuditStoredArtifactReferenceFact",
    "MAX_AUDIT_COMPONENT_REFERENCES",
    "MAX_AUDIT_INLINE_ITEM_BYTES",
    "MAX_AUDIT_PAGES",
    "MAX_AUDIT_PAGE_CANONICAL_BYTES",
    "MAX_AUDIT_PLAN_CANONICAL_BYTES",
    "MAX_AUDIT_ROOT_CANONICAL_BYTES",
    "MAX_AUDIT_TOTAL_INLINE_BYTES",
    "MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES",
    "build_context_input_audit_storage_fact",
]
