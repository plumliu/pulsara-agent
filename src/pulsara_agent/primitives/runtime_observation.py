"""Durable contracts for typed provider user carriers and runtime observations.

The contracts in this module deliberately separate provider-visible semantics
from durable source and materialization attribution.  Provider input owns the
final vector placement; this module owns the carrier protocol, observation
lifecycle, and Long-Horizon observation rewrite proof vocabulary.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, PositiveInt, model_validator

from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_utc_timestamp,
    context_fingerprint,
)
from pulsara_agent.primitives.context_source import (
    ContextArtifactReferenceFact,
    ContextSourceId,
    LedgerAuthorityHorizonFact,
    LedgerAuthorityHorizonSetReferenceFact,
)
from pulsara_agent.primitives.frozen import FrozenFactBase, register_durable_fact
from pulsara_agent.primitives.provider_input import (
    ProviderInputTypedFragmentFact,
    ProviderTranscriptNodeIdentityFact,
)


Fingerprint = str


def _fact(schema_version: str, own_field: str, domain_separator: str):
    def decorate(cls):
        register_durable_fact(
            schema_version=schema_version,
            own_fingerprint_field=own_field,
            domain_separator=domain_separator,
        )
        return cls

    return decorate


ObservationTransitionKind = Literal[
    "observation",
    "snapshot_update",
    "explicit_empty",
    "guidance",
    "status_update",
    "terminal",
    "handoff",
    "delivery",
    "diagnostic_update",
]
ObservationLifecycleClass = Literal[
    "immutable_append_once",
    "causal_append_once",
    "replacement_snapshot",
]
RuntimeRequestKind = Literal[
    "subagent_task",
    "current_run_task",
    "compaction_request",
    "window_compaction_request",
    "governance_request",
    "reflection_request",
    "summarizer_request",
]


@_fact(
    "context_source_observation_producer.v1",
    "producer_fingerprint",
    "context-source-observation-producer:v1",
)
class ContextSourceObservationProducerFact(FrozenFactBase):
    schema_version: Literal["context_source_observation_producer.v1"] = (
        "context_source_observation_producer.v1"
    )
    producer_kind: Literal["context_source"] = "context_source"
    source_id: ContextSourceId
    transition_kind: ObservationTransitionKind
    producer_fingerprint: Fingerprint


@_fact(
    "transcript_lifecycle_observation_producer.v1",
    "producer_fingerprint",
    "transcript-lifecycle-observation-producer:v1",
)
class TranscriptLifecycleObservationProducerFact(FrozenFactBase):
    schema_version: Literal["transcript_lifecycle_observation_producer.v1"] = (
        "transcript_lifecycle_observation_producer.v1"
    )
    producer_kind: Literal["transcript_lifecycle"] = "transcript_lifecycle"
    event_domain_contract_id: str = Field(min_length=1)
    event_domain_contract_version: str = Field(min_length=1)
    event_domain_contract_fingerprint: Fingerprint
    supported_source_event_contract_set_fingerprint: Fingerprint
    reducer_contract_fingerprint: Fingerprint
    producer_fingerprint: Fingerprint


@_fact(
    "long_horizon_rewrite_observation_producer.v1",
    "producer_fingerprint",
    "long-horizon-rewrite-observation-producer:v1",
)
class LongHorizonRewriteObservationProducerFact(FrozenFactBase):
    schema_version: Literal["long_horizon_rewrite_observation_producer.v1"] = (
        "long_horizon_rewrite_observation_producer.v1"
    )
    producer_kind: Literal["long_horizon_rewrite"] = "long_horizon_rewrite"
    rewrite_contract_id: str = Field(min_length=1)
    rewrite_contract_version: str = Field(min_length=1)
    rewrite_contract_fingerprint: Fingerprint
    producer_fingerprint: Fingerprint


RuntimeObservationProducerFact: TypeAlias = Annotated[
    ContextSourceObservationProducerFact
    | TranscriptLifecycleObservationProducerFact
    | LongHorizonRewriteObservationProducerFact,
    Field(discriminator="producer_kind"),
]


@_fact(
    "runtime_observation_kind_contract.v1",
    "kind_contract_fingerprint",
    "runtime-observation-kind-contract:v1",
)
class RuntimeObservationKindContractFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_kind_contract.v1"] = (
        "runtime_observation_kind_contract.v1"
    )
    kind: str = Field(min_length=1, max_length=128)
    producers: tuple[RuntimeObservationProducerFact, ...]
    authority_class: Literal[
        "runtime_fact", "runtime_guidance", "runtime_fact_and_guidance"
    ]
    lifecycle_class: ObservationLifecycleClass
    payload_schema_version: str = Field(min_length=1)
    payload_schema_fingerprint: Fingerprint
    maximum_payload_utf8_bytes: PositiveInt
    rewrite_eligibility: Literal[
        "never", "after_causal_close", "superseded_only", "long_horizon_rewrite"
    ]
    protection_policy: Literal[
        "always", "protect_current_run", "protect_effective_head", "protect_until_closed"
    ]
    instruction_policy: Literal[
        "fact_only_not_instruction",
        "runtime_guidance_under_root_policy",
        "typed_fact_with_bounded_guidance",
    ]
    kind_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _producers(self) -> "RuntimeObservationKindContractFact":
        fingerprints = tuple(item.producer_fingerprint for item in self.producers)
        if not fingerprints or fingerprints != tuple(sorted(set(fingerprints))):
            raise ValueError("runtime observation producers must be ordered/unique")
        expected_payload_schema = context_fingerprint(
            "runtime-observation-payload-schema:v2",
            self.payload_schema_version,
        )
        if self.payload_schema_fingerprint != expected_payload_schema:
            raise ValueError("runtime observation payload schema binding drifted")
        return self


@_fact(
    "runtime_observation_canonical_codec_contract.v1",
    "codec_contract_fingerprint",
    "runtime-observation-canonical-codec-contract:v1",
)
class RuntimeObservationCanonicalCodecContractFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_canonical_codec_contract.v1"] = (
        "runtime_observation_canonical_codec_contract.v1"
    )
    codec_id: Literal["pulsara.runtime-user-carrier.canonical-json"] = (
        "pulsara.runtime-user-carrier.canonical-json"
    )
    codec_version: Literal["1"] = "1"
    encoding: Literal["utf-8"] = "utf-8"
    object_key_order: Literal["lexicographic"] = "lexicographic"
    unicode_normalization: Literal["NFC"] = "NFC"
    string_escaping: Literal["json"] = "json"
    non_finite_numbers: Literal["forbidden"] = "forbidden"
    unknown_fields: Literal["forbidden"] = "forbidden"
    maximum_wire_utf8_bytes: PositiveInt
    codec_contract_fingerprint: Fingerprint


@_fact(
    "runtime_observation_protocol_contract.v2",
    "protocol_contract_fingerprint",
    "runtime-observation-protocol-contract:v2",
)
class RuntimeObservationProtocolContractFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_protocol_contract.v2"] = (
        "runtime_observation_protocol_contract.v2"
    )
    protocol_id: Literal["pulsara.runtime-observation"] = (
        "pulsara.runtime-observation"
    )
    protocol_version: Literal["2"] = "2"
    wire_role: Literal["user"] = "user"
    codec_contract: RuntimeObservationCanonicalCodecContractFact
    ordered_kind_contracts: tuple[RuntimeObservationKindContractFact, ...]
    source_lifecycle_registry_contract_fingerprint: Fingerprint
    unknown_kind_policy: Literal["reject_before_adapter"] = "reject_before_adapter"
    unknown_contract_policy: Literal["reject_before_adapter"] = (
        "reject_before_adapter"
    )
    protocol_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _kinds(self) -> "RuntimeObservationProtocolContractFact":
        kinds = tuple(item.kind for item in self.ordered_kind_contracts)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("runtime observation kinds must be ordered/unique")
        return self


@_fact(
    "human_input_protocol_contract.v1",
    "protocol_contract_fingerprint",
    "human-input-protocol-contract:v1",
)
class HumanInputProtocolContractFact(FrozenFactBase):
    schema_version: Literal["human_input_protocol_contract.v1"] = (
        "human_input_protocol_contract.v1"
    )
    protocol_id: Literal["pulsara.human-input"] = "pulsara.human-input"
    protocol_version: Literal["1"] = "1"
    wire_role: Literal["user"] = "user"
    envelope_key: Literal["pulsara_human_input"] = "pulsara_human_input"
    codec_contract_fingerprint: Fingerprint
    raw_text_policy: Literal["escaped_typed_text_field_only"] = (
        "escaped_typed_text_field_only"
    )
    unsupported_multimodal_policy: Literal[
        "reject_until_typed_block_contract"
    ] = "reject_until_typed_block_contract"
    maximum_text_utf8_bytes: PositiveInt
    protocol_contract_fingerprint: Fingerprint


@_fact(
    "runtime_request_kind_contract.v1",
    "kind_contract_fingerprint",
    "runtime-request-kind-contract:v1",
)
class RuntimeRequestKindContractFact(FrozenFactBase):
    schema_version: Literal["runtime_request_kind_contract.v1"] = (
        "runtime_request_kind_contract.v1"
    )
    request_kind: RuntimeRequestKind
    instruction_policy: Literal["task_under_root_policy"] = "task_under_root_policy"
    lifecycle_class: Literal[
        "child_run_entry", "current_run_transcript", "one_shot_invocation"
    ]
    transcript_persistence: Literal[
        "persist_child_canonical_transcript",
        "persist_current_run_canonical_transcript",
        "invocation_scoped_only",
    ]
    allowed_owner_kinds: tuple[
        Literal[
            "subagent_spawn",
            "current_run",
            "compaction_operation",
            "window_compaction_operation",
            "governance_batch",
            "reflection_job",
            "summarizer_operation",
        ],
        ...,
    ]
    payload_schema_version: str = Field(min_length=1)
    payload_schema_fingerprint: Fingerprint
    maximum_payload_utf8_bytes: PositiveInt
    observation_rewrite_policy: Literal["never"] = "never"
    kind_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _owners(self) -> "RuntimeRequestKindContractFact":
        if self.allowed_owner_kinds != tuple(sorted(set(self.allowed_owner_kinds))):
            raise ValueError("runtime request owners must be ordered/unique")
        return self


@_fact(
    "runtime_request_protocol_contract.v1",
    "protocol_contract_fingerprint",
    "runtime-request-protocol-contract:v1",
)
class RuntimeRequestProtocolContractFact(FrozenFactBase):
    schema_version: Literal["runtime_request_protocol_contract.v1"] = (
        "runtime_request_protocol_contract.v1"
    )
    protocol_id: Literal["pulsara.runtime-request"] = "pulsara.runtime-request"
    protocol_version: Literal["1"] = "1"
    wire_role: Literal["user"] = "user"
    envelope_key: Literal["pulsara_runtime_request"] = "pulsara_runtime_request"
    codec_contract_fingerprint: Fingerprint
    ordered_kind_contracts: tuple[RuntimeRequestKindContractFact, ...]
    unknown_kind_policy: Literal["reject_before_adapter"] = "reject_before_adapter"
    unknown_contract_policy: Literal["reject_before_adapter"] = (
        "reject_before_adapter"
    )
    protocol_contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _kinds(self) -> "RuntimeRequestProtocolContractFact":
        kinds = tuple(item.request_kind for item in self.ordered_kind_contracts)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("runtime request kinds must be ordered/unique")
        return self


@_fact(
    "runtime_task_request_payload.v1",
    "payload_semantic_fingerprint",
    "runtime-task-request-payload:v1",
)
class RuntimeTaskRequestPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_task_request_payload.v1"] = (
        "runtime_task_request_payload.v1"
    )
    payload_kind: Literal["task"] = "task"
    task_text: str
    task_text_utf8_sha256: Fingerprint
    task_text_utf8_bytes: int = Field(ge=0)
    ordered_context_fragments: tuple[ProviderInputTypedFragmentFact, ...]
    payload_semantic_fingerprint: Fingerprint


@_fact(
    "runtime_operation_request_payload.v1",
    "payload_semantic_fingerprint",
    "runtime-operation-request-payload:v1",
)
class RuntimeOperationRequestPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_operation_request_payload.v1"] = (
        "runtime_operation_request_payload.v1"
    )
    payload_kind: Literal["operation"] = "operation"
    operation_kind: Literal[
        "compaction", "window_compaction", "governance", "reflection", "summarizer"
    ]
    objective_contract_fingerprint: Fingerprint
    ordered_model_visible_fragments: tuple[ProviderInputTypedFragmentFact, ...]
    input_document_semantic_fingerprints: tuple[Fingerprint, ...]
    output_contract_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint


RuntimeRequestPayloadFact: TypeAlias = Annotated[
    RuntimeTaskRequestPayloadFact | RuntimeOperationRequestPayloadFact,
    Field(discriminator="payload_kind"),
]


def _validate_payload_text(
    *,
    text: str,
    expected_sha256: Fingerprint,
    expected_utf8_bytes: int,
) -> None:
    encoded = text.encode("utf-8")
    if len(encoded) != expected_utf8_bytes:
        raise ValueError("runtime observation payload text byte count drifted")
    if f"sha256:{sha256(encoded).hexdigest()}" != expected_sha256:
        raise ValueError("runtime observation payload text digest drifted")


@_fact(
    "runtime_clock_observation_payload.v2",
    "payload_semantic_fingerprint",
    "runtime-clock-observation-payload:v2",
)
class RuntimeClockObservationPayloadFact(FrozenFactBase):
    schema_version: Literal["runtime_clock_observation_payload.v2"] = (
        "runtime_clock_observation_payload.v2"
    )
    payload_kind: Literal["runtime_clock"] = "runtime_clock"
    observed_at_utc: str
    timezone_name: str = Field(min_length=1, max_length=128)
    local_date: str = Field(min_length=1, max_length=32)
    proposal_reason: Literal[
        "compile",
        "user_turn",
        "long_operation_completed",
        "local_date_changed",
        "explicit_temporal_requirement",
    ]
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _canonical_time(self) -> "RuntimeClockObservationPayloadFact":
        if canonical_utc_timestamp(self.observed_at_utc) != self.observed_at_utc:
            raise ValueError("runtime clock observation is not canonical UTC")
        return self


@_fact(
    "context_source_append_observation_payload.v1",
    "payload_semantic_fingerprint",
    "context-source-append-observation-payload:v1",
)
class ContextSourceAppendObservationPayloadFact(FrozenFactBase):
    schema_version: Literal["context_source_append_observation_payload.v1"] = (
        "context_source_append_observation_payload.v1"
    )
    payload_kind: Literal["context_source_append"] = "context_source_append"
    source_id: ContextSourceId
    transition_kind: ObservationTransitionKind
    model_visible_content: str
    content_utf8_sha256: Fingerprint
    content_utf8_bytes: int = Field(ge=0)
    source_payload_schema_version: str = Field(min_length=1)
    source_payload_semantic_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_identity(self) -> "ContextSourceAppendObservationPayloadFact":
        _validate_payload_text(
            text=self.model_visible_content,
            expected_sha256=self.content_utf8_sha256,
            expected_utf8_bytes=self.content_utf8_bytes,
        )
        return self


@_fact(
    "context_source_replacement_observation_payload.v1",
    "payload_semantic_fingerprint",
    "context-source-replacement-observation-payload:v1",
)
class ContextSourceReplacementObservationPayloadFact(FrozenFactBase):
    schema_version: Literal[
        "context_source_replacement_observation_payload.v1"
    ] = "context_source_replacement_observation_payload.v1"
    payload_kind: Literal[
        "context_source_replacement"
    ] = "context_source_replacement"
    source_id: ContextSourceId
    transition_kind: ObservationTransitionKind
    model_visible_content: str
    content_utf8_sha256: Fingerprint
    content_utf8_bytes: int = Field(ge=0)
    source_payload_schema_version: str = Field(min_length=1)
    source_payload_semantic_fingerprint: Fingerprint
    replacement_scope: Literal["entire_source_instance"] = "entire_source_instance"
    replacement_revision: int = Field(ge=1)
    predecessor_observation_semantic_id: Fingerprint | Literal["genesis"]
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_and_lineage(
        self,
    ) -> "ContextSourceReplacementObservationPayloadFact":
        _validate_payload_text(
            text=self.model_visible_content,
            expected_sha256=self.content_utf8_sha256,
            expected_utf8_bytes=self.content_utf8_bytes,
        )
        if (self.replacement_revision == 1) != (
            self.predecessor_observation_semantic_id == "genesis"
        ):
            raise ValueError("replacement observation revision lineage mismatch")
        return self


@_fact(
    "transcript_lifecycle_observation_payload.v1",
    "payload_semantic_fingerprint",
    "transcript-lifecycle-observation-payload:v1",
)
class TranscriptLifecycleObservationPayloadFact(FrozenFactBase):
    schema_version: Literal[
        "transcript_lifecycle_observation_payload.v1"
    ] = "transcript_lifecycle_observation_payload.v1"
    payload_kind: Literal["transcript_lifecycle"] = "transcript_lifecycle"
    lifecycle_segment: str = Field(min_length=1, max_length=128)
    model_visible_content: str
    content_utf8_sha256: Fingerprint
    content_utf8_bytes: int = Field(ge=0)
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_identity(self) -> "TranscriptLifecycleObservationPayloadFact":
        _validate_payload_text(
            text=self.model_visible_content,
            expected_sha256=self.content_utf8_sha256,
            expected_utf8_bytes=self.content_utf8_bytes,
        )
        return self


@_fact(
    "derived_text_runtime_observation_payload.v1",
    "payload_semantic_fingerprint",
    "derived-text-runtime-observation-payload:v1",
)
class DerivedTextRuntimeObservationPayloadFact(FrozenFactBase):
    schema_version: Literal[
        "derived_text_runtime_observation_payload.v1"
    ] = "derived_text_runtime_observation_payload.v1"
    payload_kind: Literal["derived_text"] = "derived_text"
    derivation_kind: Literal[
        "compaction_replacement_summary", "long_horizon_rollup_observation"
    ]
    model_visible_content: str
    content_utf8_sha256: Fingerprint
    content_utf8_bytes: int = Field(ge=0)
    source_semantic_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _content_identity(self) -> "DerivedTextRuntimeObservationPayloadFact":
        _validate_payload_text(
            text=self.model_visible_content,
            expected_sha256=self.content_utf8_sha256,
            expected_utf8_bytes=self.content_utf8_bytes,
        )
        return self


@_fact(
    "runtime_observation_rewrite_projection_payload.v2",
    "payload_semantic_fingerprint",
    "runtime-observation-rewrite-projection-payload:v2",
)
class RuntimeObservationRewriteProjectionPayloadFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_rewrite_projection_payload.v2"
    ] = "runtime_observation_rewrite_projection_payload.v2"
    payload_kind: Literal["rewrite_projection"] = "rewrite_projection"
    covered_direct_member_count: int = Field(ge=1)
    covered_kind_counts: tuple[tuple[str, int], ...]
    covered_original_observation_count: int = Field(ge=1)
    coverage_semantic_fingerprint: Fingerprint
    ordered_original_causal_accumulator: Fingerprint
    ordered_original_semantic_accumulator: Fingerprint
    transitive_coverage_root_fingerprint: Fingerprint
    summary: str = Field(min_length=1, max_length=4096)
    payload_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _counts(self) -> "RuntimeObservationRewriteProjectionPayloadFact":
        keys = tuple(item[0] for item in self.covered_kind_counts)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("rewrite projection kind counts must be ordered/unique")
        if any(not key or count < 1 for key, count in self.covered_kind_counts):
            raise ValueError("rewrite projection kind count is invalid")
        if sum(count for _, count in self.covered_kind_counts) != (
            self.covered_direct_member_count
        ):
            raise ValueError("rewrite projection direct member count mismatch")
        if self.covered_original_observation_count < self.covered_direct_member_count:
            raise ValueError("rewrite projection transitive coverage undercounts members")
        return self


RuntimeObservationPayloadFact: TypeAlias = Annotated[
    RuntimeClockObservationPayloadFact
    | ContextSourceAppendObservationPayloadFact
    | ContextSourceReplacementObservationPayloadFact
    | TranscriptLifecycleObservationPayloadFact
    | DerivedTextRuntimeObservationPayloadFact
    | RuntimeObservationRewriteProjectionPayloadFact,
    Field(discriminator="payload_kind"),
]


@_fact(
    "provider_user_carrier_protocol_contract.v2",
    "contract_fingerprint",
    "provider-user-carrier-protocol-contract:v2",
)
class ProviderUserCarrierProtocolContractFact(FrozenFactBase):
    schema_version: Literal["provider_user_carrier_protocol_contract.v2"] = (
        "provider_user_carrier_protocol_contract.v2"
    )
    human_input_protocol: HumanInputProtocolContractFact
    runtime_request_protocol: RuntimeRequestProtocolContractFact
    runtime_observation_protocol: RuntimeObservationProtocolContractFact
    root_interpretation_fragment_semantic_fingerprint: Fingerprint
    user_item_policy: Literal["exactly_one_registered_outer_envelope"] = (
        "exactly_one_registered_outer_envelope"
    )
    contract_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _keys(self) -> "ProviderUserCarrierProtocolContractFact":
        keys = {
            self.human_input_protocol.envelope_key,
            self.runtime_request_protocol.envelope_key,
            "pulsara_runtime_observation",
        }
        if len(keys) != 3:
            raise ValueError("provider user carrier envelope keys overlap")
        return self


@_fact(
    "human_input_wire_semantic.v1",
    "semantic_fingerprint",
    "human-input-wire-semantic:v1",
)
class HumanInputWireSemanticFact(FrozenFactBase):
    schema_version: Literal["human_input_wire_semantic.v1"] = (
        "human_input_wire_semantic.v1"
    )
    protocol_version: Literal["1"] = "1"
    human_input_semantic_id: Fingerprint
    causal_occurrence_semantic_fingerprint: Fingerprint
    text: str
    text_utf8_sha256: Fingerprint
    text_utf8_bytes: int = Field(ge=0)
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: int = Field(ge=0)
    semantic_fingerprint: Fingerprint


@_fact(
    "runtime_request_wire_semantic.v1",
    "semantic_fingerprint",
    "runtime-request-wire-semantic:v1",
)
class RuntimeRequestWireSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_request_wire_semantic.v1"] = (
        "runtime_request_wire_semantic.v1"
    )
    protocol_version: Literal["1"] = "1"
    request_kind: RuntimeRequestKind
    request_semantic_id: Fingerprint
    business_occurrence_semantic_fingerprint: Fingerprint
    instruction_policy: Literal["task_under_root_policy"] = "task_under_root_policy"
    lifecycle_class: Literal[
        "child_run_entry", "current_run_transcript", "one_shot_invocation"
    ]
    payload: RuntimeRequestPayloadFact
    payload_schema_version: str
    payload_schema_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: int = Field(ge=0)
    semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _payload(self) -> "RuntimeRequestWireSemanticFact":
        if self.payload_semantic_fingerprint != self.payload.payload_semantic_fingerprint:
            raise ValueError("runtime request payload semantic join mismatch")
        return self


@_fact(
    "subagent_task_runtime_request_owner.v1",
    "owner_fingerprint",
    "subagent-task-runtime-request-owner:v1",
)
class SubagentTaskRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["subagent_task_runtime_request_owner.v1"] = (
        "subagent_task_runtime_request_owner.v1"
    )
    owner_kind: Literal["subagent_spawn"] = "subagent_spawn"
    runtime_session_id: str
    parent_run_id: str
    child_run_id: str
    spawn_event_reference: ContextEventReferenceFact
    owner_fingerprint: Fingerprint


@_fact(
    "current_run_runtime_request_owner.v1",
    "owner_fingerprint",
    "current-run-runtime-request-owner:v1",
)
class CurrentRunRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["current_run_runtime_request_owner.v1"] = (
        "current_run_runtime_request_owner.v1"
    )
    owner_kind: Literal["current_run"] = "current_run"
    runtime_session_id: str
    run_id: str
    turn_id: str | None
    request_occurrence_semantic_fingerprint: Fingerprint
    owner_fingerprint: Fingerprint


@_fact(
    "one_shot_runtime_request_owner.v1",
    "owner_fingerprint",
    "one-shot-runtime-request-owner:v1",
)
class OneShotRuntimeRequestOwnerFact(FrozenFactBase):
    schema_version: Literal["one_shot_runtime_request_owner.v1"] = (
        "one_shot_runtime_request_owner.v1"
    )
    owner_kind: Literal[
        "compaction_operation",
        "window_compaction_operation",
        "governance_batch",
        "reflection_job",
        "summarizer_operation",
    ]
    runtime_session_id: str
    operation_semantic_id: Fingerprint
    source_event_references: tuple[ContextEventReferenceFact, ...]
    owner_fingerprint: Fingerprint


RuntimeRequestOwnerFact: TypeAlias = Annotated[
    SubagentTaskRuntimeRequestOwnerFact
    | CurrentRunRuntimeRequestOwnerFact
    | OneShotRuntimeRequestOwnerFact,
    Field(discriminator="owner_kind"),
]


@_fact(
    "runtime_request_attribution.v1",
    "attribution_fingerprint",
    "runtime-request-attribution:v1",
)
class RuntimeRequestAttributionFact(FrozenFactBase):
    schema_version: Literal["runtime_request_attribution.v1"] = (
        "runtime_request_attribution.v1"
    )
    request_semantic_fingerprint: Fingerprint
    request_kind: RuntimeRequestKind
    owner: RuntimeRequestOwnerFact
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    attribution_fingerprint: Fingerprint


@_fact(
    "runtime_observation_wire_semantic.v2",
    "wire_semantic_fingerprint",
    "runtime-observation-wire-semantic:v2",
)
class RuntimeObservationWireSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_wire_semantic.v2"] = (
        "runtime_observation_wire_semantic.v2"
    )
    protocol_version: Literal["2"] = "2"
    observation_kind: str
    observation_semantic_id: Fingerprint
    source_instance_id: str
    authority_class: Literal[
        "runtime_fact", "runtime_guidance", "runtime_fact_and_guidance"
    ]
    lifecycle_class: ObservationLifecycleClass
    payload: RuntimeObservationPayloadFact
    payload_schema_version: str
    payload_schema_fingerprint: Fingerprint
    payload_semantic_fingerprint: Fingerprint
    canonical_wire_utf8_sha256: Fingerprint
    canonical_wire_utf8_bytes: int = Field(ge=0)
    wire_semantic_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _payload_join(self) -> "RuntimeObservationWireSemanticFact":
        if (
            self.payload_schema_version != self.payload.schema_version
            or self.payload_semantic_fingerprint
            != self.payload.payload_semantic_fingerprint
        ):
            raise ValueError("runtime observation payload identity join mismatch")
        expected_schema = context_fingerprint(
            "runtime-observation-payload-schema:v2",
            self.payload_schema_version,
        )
        if self.payload_schema_fingerprint != expected_schema:
            raise ValueError("runtime observation payload schema proof drifted")
        return self


@_fact(
    "runtime_observation_causal_placement_semantic.v1",
    "placement_semantic_fingerprint",
    "runtime-observation-causal-placement-semantic:v1",
)
class RuntimeObservationCausalPlacementSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_causal_placement_semantic.v1"
    ] = "runtime_observation_causal_placement_semantic.v1"
    causal_scope_kind: Literal[
        "runtime_session", "run", "model_invocation", "workflow", "subagent", "operation"
    ]
    causal_scope_semantic_id: Fingerprint
    placement_phase: Literal[
        "before_model_call",
        "after_model_call",
        "after_tool_result",
        "after_run_terminal",
        "status_at_frontier",
    ]
    stable_predecessor_transcript_node: ProviderTranscriptNodeIdentityFact | None
    source_occurrence_semantic_fingerprint: Fingerprint
    intra_boundary_order: int = Field(ge=0)
    placement_contract_fingerprint: Fingerprint
    placement_semantic_fingerprint: Fingerprint


@_fact(
    "runtime_observation_source_attribution.v3",
    "attribution_fingerprint",
    "runtime-observation-source-attribution:v3",
)
class RuntimeObservationSourceAttributionFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_source_attribution.v3"] = (
        "runtime_observation_source_attribution.v3"
    )
    observation_semantic_fingerprint: Fingerprint
    producer: RuntimeObservationProducerFact
    transition_kind: ObservationTransitionKind | None
    protection_scope_kind: Literal[
        "runtime_session", "run", "workflow", "subagent", "operation"
    ]
    protection_scope_semantic_id: Fingerprint
    owning_run_protection_scope_semantic_id: Fingerprint | None
    source_event_references: tuple[ContextEventReferenceFact, ...]
    source_artifact_references: tuple[ContextArtifactReferenceFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    attribution_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _producer_transition(self) -> "RuntimeObservationSourceAttributionFact":
        if isinstance(self.producer, ContextSourceObservationProducerFact):
            if self.transition_kind != self.producer.transition_kind:
                raise ValueError("ContextSource observation transition is unauthorized")
        elif self.transition_kind is not None:
            raise ValueError("derived observation cannot claim a ContextSource transition")
        return self


@_fact(
    "prepared_runtime_observation_provider_unit.v1",
    "fact_fingerprint",
    "prepared-runtime-observation-provider-unit:v1",
)
class PreparedRuntimeObservationProviderUnitFact(FrozenFactBase):
    schema_version: Literal["prepared_runtime_observation_provider_unit.v1"] = (
        "prepared_runtime_observation_provider_unit.v1"
    )
    wire_semantic: RuntimeObservationWireSemanticFact
    causal_placement: RuntimeObservationCausalPlacementSemanticFact
    source_attribution: RuntimeObservationSourceAttributionFact
    source_id: ContextSourceId | None
    source_candidate_key: str | None
    source_payload_semantic_fingerprint: Fingerprint | None
    owner_semantic_fingerprint: Fingerprint
    provider_fragment_semantic_fingerprint: Fingerprint
    provider_unit_semantic_fingerprint: Fingerprint
    unit_causal_semantic_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "PreparedRuntimeObservationProviderUnitFact":
        if (
            self.source_attribution.observation_semantic_fingerprint
            != self.wire_semantic.wire_semantic_fingerprint
        ):
            raise ValueError("runtime observation source/wire semantic mismatch")
        expected = context_fingerprint(
            "runtime-observation-provider-unit-causal:v1",
            (
                self.wire_semantic.wire_semantic_fingerprint,
                self.causal_placement.placement_semantic_fingerprint,
                self.provider_fragment_semantic_fingerprint,
                self.provider_unit_semantic_fingerprint,
            ),
        )
        if self.unit_causal_semantic_fingerprint != expected:
            raise ValueError("runtime observation causal unit identity mismatch")
        context_source = isinstance(
            self.source_attribution.producer,
            ContextSourceObservationProducerFact,
        )
        if context_source != (self.source_id is not None):
            raise ValueError("runtime observation ContextSource identity matrix mismatch")
        if context_source != (self.source_candidate_key is not None):
            raise ValueError("runtime observation candidate-key matrix mismatch")
        if context_source != (
            self.source_payload_semantic_fingerprint is not None
        ):
            raise ValueError("runtime observation source-payload matrix mismatch")
        if context_source and (
            self.source_attribution.producer.source_id is not self.source_id
        ):
            raise ValueError("runtime observation producer/source ID mismatch")
        return self


@_fact(
    "context_source_observation_kind_binding.v1",
    "binding_fingerprint",
    "context-source-observation-kind-binding:v1",
)
class ContextSourceObservationKindBindingFact(FrozenFactBase):
    schema_version: Literal["context_source_observation_kind_binding.v1"] = (
        "context_source_observation_kind_binding.v1"
    )
    transition_kind: ObservationTransitionKind
    observation_kind: str
    binding_fingerprint: Fingerprint


@_fact(
    "context_source_lifecycle_registry_entry.v2",
    "entry_fingerprint",
    "context-source-lifecycle-registry-entry:v2",
)
class ContextSourceLifecycleRegistryEntryFact(FrozenFactBase):
    schema_version: Literal["context_source_lifecycle_registry_entry.v2"] = (
        "context_source_lifecycle_registry_entry.v2"
    )
    source_id: ContextSourceId
    lifecycle_class: Literal[
        "generation_root", "immutable_append_once", "causal_append_once", "replacement_snapshot"
    ]
    source_instance_scope: Literal[
        "runtime_session", "continuity_cohort", "run", "turn", "workflow", "model_call", "subagent", "operation"
    ]
    absence_semantics: Literal["forbidden", "no_new_fact", "retain_effective_head"]
    closure_kind: Literal[
        "none", "empty_replacement", "typed_terminal_snapshot", "root_rollover"
    ]
    rollover_materialization: Literal[
        "rebuild_root_from_exact_reference",
        "reuse_effective_snapshot_reference",
        "copy_immutable_causal_unit",
        "consume_runtime_observation_rewrite",
    ]
    rewrite_eligibility: Literal[
        "never", "superseded_only", "after_causal_close", "long_horizon_rewrite"
    ]
    observation_kind_bindings: tuple[ContextSourceObservationKindBindingFact, ...]
    entry_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _bindings(self) -> "ContextSourceLifecycleRegistryEntryFact":
        keys = tuple(
            (item.transition_kind, item.observation_kind)
            for item in self.observation_kind_bindings
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("source observation bindings must be ordered/unique")
        if self.lifecycle_class == "generation_root" and keys:
            raise ValueError("generation-root source cannot emit observations")
        return self


@_fact(
    "context_source_lifecycle_registry_contract.v2",
    "registry_fingerprint",
    "context-source-lifecycle-registry-contract:v2",
)
class ContextSourceLifecycleRegistryContractFact(FrozenFactBase):
    schema_version: Literal["context_source_lifecycle_registry_contract.v2"] = (
        "context_source_lifecycle_registry_contract.v2"
    )
    registry_id: Literal["pulsara.context-source-lifecycle"] = (
        "pulsara.context-source-lifecycle"
    )
    registry_version: Literal["2"] = "2"
    ordered_entries: tuple[ContextSourceLifecycleRegistryEntryFact, ...]
    registry_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _entries(self) -> "ContextSourceLifecycleRegistryContractFact":
        ids = tuple(item.source_id for item in self.ordered_entries)
        if ids != tuple(sorted(set(ids), key=lambda item: item.value)):
            raise ValueError("source lifecycle entries must be ordered/unique")
        if set(ids) != set(ContextSourceId):
            raise ValueError("source lifecycle registry is not exhaustive")
        return self


@_fact(
    "runtime_observation_projection_physical_policy.v1",
    "policy_fingerprint",
    "runtime-observation-projection-physical-policy:v1",
)
class RuntimeObservationProjectionPhysicalPolicyFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_projection_physical_policy.v1"
    ] = "runtime_observation_projection_physical_policy.v1"
    leaf_max_entries: PositiveInt
    leaf_max_canonical_bytes: PositiveInt
    internal_max_fanout: PositiveInt
    maximum_tree_height: PositiveInt
    maximum_event_root_bytes: PositiveInt
    maximum_changed_nodes_per_rewrite: PositiveInt
    maximum_artifact_batches_per_rewrite: PositiveInt
    operation_deadline_seconds: PositiveInt
    policy_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _tree_contract(self) -> "RuntimeObservationProjectionPhysicalPolicyFact":
        if self.internal_max_fanout < 2:
            raise ValueError("observation projection tree fanout must be at least two")
        if self.maximum_tree_height > 8:
            raise ValueError("observation projection tree height exceeds V1 bound")
        if self.maximum_changed_nodes_per_rewrite < self.maximum_tree_height:
            raise ValueError("observation rewrite node bound cannot cover one root path")
        return self


@_fact(
    "runtime_observation_projection_set_node_reference.v1",
    "reference_fingerprint",
    "runtime-observation-projection-set-node-reference:v1",
)
class RuntimeObservationProjectionSetNodeReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_projection_set_node_reference.v1"
    ] = "runtime_observation_projection_set_node_reference.v1"
    node_kind: Literal["leaf", "internal"]
    height: PositiveInt
    member_count: PositiveInt
    first_causal_key: Fingerprint
    last_causal_key: Fingerprint
    ordered_semantic_accumulator: Fingerprint
    ordered_causal_accumulator: Fingerprint
    artifact_reference: ContextArtifactReferenceFact
    reference_fingerprint: Fingerprint


@_fact(
    "runtime_observation_projection_set_reference.v1",
    "reference_fingerprint",
    "runtime-observation-projection-set-reference:v1",
)
class RuntimeObservationProjectionSetReferenceFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_set_reference.v1"] = (
        "runtime_observation_projection_set_reference.v1"
    )
    set_kind: Literal[
        "active", "protected", "eligible", "retained", "rewritten", "open_lifecycle", "pending_dependency"
    ]
    member_count: int = Field(ge=0)
    ordered_semantic_accumulator: Fingerprint
    ordered_causal_accumulator: Fingerprint
    root_node_reference: RuntimeObservationProjectionSetNodeReferenceFact | None
    set_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _empty(self) -> "RuntimeObservationProjectionSetReferenceFact":
        if (self.member_count == 0) != (self.root_node_reference is None):
            raise ValueError("observation set empty/root matrix mismatch")
        if self.root_node_reference is not None and (
            self.root_node_reference.member_count != self.member_count
        ):
            raise ValueError("observation set root count mismatch")
        return self


@_fact(
    "runtime_observation_effective_head_set_reference.v1",
    "reference_fingerprint",
    "runtime-observation-effective-head-set-reference:v1",
)
class RuntimeObservationEffectiveHeadSetReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_effective_head_set_reference.v1"
    ] = "runtime_observation_effective_head_set_reference.v1"
    head_count: int = Field(ge=0)
    ordered_head_accumulator: Fingerprint
    root_node_reference: RuntimeObservationProjectionSetNodeReferenceFact | None
    set_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _empty(self) -> "RuntimeObservationEffectiveHeadSetReferenceFact":
        if (self.head_count == 0) != (self.root_node_reference is None):
            raise ValueError("effective head set empty/root matrix mismatch")
        return self


@_fact(
    "runtime_observation_projection_stable_state.v1",
    "stable_state_fingerprint",
    "runtime-observation-projection-stable-state:v1",
)
class RuntimeObservationProjectionStableStateFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_stable_state.v1"] = (
        "runtime_observation_projection_stable_state.v1"
    )
    state_revision: int = Field(ge=0)
    source_generation_id: str
    source_generation_core_fingerprint: Fingerprint
    authority_horizon_set_reference: LedgerAuthorityHorizonSetReferenceFact
    active_observations: RuntimeObservationProjectionSetReferenceFact
    protected_observations: RuntimeObservationProjectionSetReferenceFact
    eligible_observations: RuntimeObservationProjectionSetReferenceFact
    open_lifecycle_observations: RuntimeObservationProjectionSetReferenceFact
    pending_dependency_observations: RuntimeObservationProjectionSetReferenceFact
    effective_heads: RuntimeObservationEffectiveHeadSetReferenceFact
    classification_contract_fingerprint: Fingerprint
    physical_policy_fingerprint: Fingerprint
    stable_state_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _set_kinds(self) -> "RuntimeObservationProjectionStableStateFact":
        expected = (
            (self.active_observations, "active"),
            (self.protected_observations, "protected"),
            (self.eligible_observations, "eligible"),
            (self.open_lifecycle_observations, "open_lifecycle"),
            (self.pending_dependency_observations, "pending_dependency"),
        )
        if any(item.set_kind != kind for item, kind in expected):
            raise ValueError("observation stable-state set kind mismatch")
        return self


@_fact(
    "runtime_observation_projection_partition_proof.v1",
    "proof_fingerprint",
    "runtime-observation-projection-partition-proof:v1",
)
class RuntimeObservationProjectionPartitionProofFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_projection_partition_proof.v1"
    ] = "runtime_observation_projection_partition_proof.v1"
    source_stable_state_fingerprint: Fingerprint
    active_set_reference: RuntimeObservationProjectionSetReferenceFact
    protected_set_reference: RuntimeObservationProjectionSetReferenceFact
    retained_set_reference: RuntimeObservationProjectionSetReferenceFact
    rewritten_set_reference: RuntimeObservationProjectionSetReferenceFact
    eligible_set_reference: RuntimeObservationProjectionSetReferenceFact
    merkle_partition_proof_reference: ContextArtifactReferenceFact
    partition_contract_fingerprint: Fingerprint
    proof_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _set_kinds(self) -> "RuntimeObservationProjectionPartitionProofFact":
        expected = (
            (self.active_set_reference, "active"),
            (self.protected_set_reference, "protected"),
            (self.retained_set_reference, "retained"),
            (self.rewritten_set_reference, "rewritten"),
            (self.eligible_set_reference, "eligible"),
        )
        if any(item.set_kind != kind for item, kind in expected):
            raise ValueError("observation partition set kind mismatch")
        if (
            self.protected_set_reference.member_count
            + self.retained_set_reference.member_count
            + self.rewritten_set_reference.member_count
            != self.active_set_reference.member_count
        ):
            raise ValueError("observation partition does not cover the active set")
        if (
            self.rewritten_set_reference.member_count
            > self.eligible_set_reference.member_count
        ):
            raise ValueError("observation rewritten set exceeds eligible set")
        return self


@_fact(
    "runtime_observation_rewrite_coverage_semantic.v1",
    "coverage_semantic_fingerprint",
    "runtime-observation-rewrite-coverage-semantic:v1",
)
class RuntimeObservationRewriteCoverageSemanticFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_rewrite_coverage_semantic.v1"
    ] = "runtime_observation_rewrite_coverage_semantic.v1"
    direct_member_count: PositiveInt
    transitive_original_observation_count: PositiveInt
    ordered_original_semantic_accumulator: Fingerprint
    ordered_original_causal_accumulator: Fingerprint
    transitive_coverage_root_fingerprint: Fingerprint
    coverage_contract_fingerprint: Fingerprint
    coverage_semantic_fingerprint: Fingerprint


@_fact(
    "runtime_observation_rewrite_unit_semantic.v1",
    "unit_semantic_fingerprint",
    "runtime-observation-rewrite-unit-semantic:v1",
)
class RuntimeObservationRewriteUnitSemanticFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_rewrite_unit_semantic.v1"] = (
        "runtime_observation_rewrite_unit_semantic.v1"
    )
    observation_semantic_id: Fingerprint
    canonical_provider_fragment: ProviderInputTypedFragmentFact
    lowering_lane: Literal["runtime_observation"] = "runtime_observation"
    causal_placement: RuntimeObservationCausalPlacementSemanticFact
    coverage_semantic: RuntimeObservationRewriteCoverageSemanticFact
    unit_semantic_fingerprint: Fingerprint


@_fact(
    "runtime_observation_rewrite_unit_attribution.v1",
    "attribution_fingerprint",
    "runtime-observation-rewrite-unit-attribution:v1",
)
class RuntimeObservationRewriteUnitAttributionFact(FrozenFactBase):
    schema_version: Literal[
        "runtime_observation_rewrite_unit_attribution.v1"
    ] = "runtime_observation_rewrite_unit_attribution.v1"
    unit_semantic_fingerprint: Fingerprint
    rewritten_source_set_reference: RuntimeObservationProjectionSetReferenceFact
    source_stable_state_fingerprint: Fingerprint
    partition_proof_fingerprint: Fingerprint
    attribution_fingerprint: Fingerprint


@_fact(
    "prepared_runtime_observation_rewrite_projection_unit.v2",
    "fact_fingerprint",
    "prepared-runtime-observation-rewrite-projection-unit:v2",
)
class PreparedRuntimeObservationRewriteProjectionUnitFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_runtime_observation_rewrite_projection_unit.v2"
    ] = "prepared_runtime_observation_rewrite_projection_unit.v2"
    semantic: RuntimeObservationRewriteUnitSemanticFact
    attribution: RuntimeObservationRewriteUnitAttributionFact
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _join(self) -> "PreparedRuntimeObservationRewriteProjectionUnitFact":
        if self.semantic.unit_semantic_fingerprint != self.attribution.unit_semantic_fingerprint:
            raise ValueError("rewrite unit semantic/attribution join mismatch")
        return self


@_fact(
    "prepared_runtime_observation_rewrite_projection_reference.v1",
    "reference_fingerprint",
    "prepared-runtime-observation-rewrite-projection-reference:v1",
)
class PreparedRuntimeObservationRewriteProjectionReferenceFact(FrozenFactBase):
    schema_version: Literal[
        "prepared_runtime_observation_rewrite_projection_reference.v1"
    ] = "prepared_runtime_observation_rewrite_projection_reference.v1"
    unit_count: int = Field(ge=0)
    ordered_unit_semantic_accumulator: Fingerprint
    ordered_causal_placement_accumulator: Fingerprint
    root_artifact_reference: ContextArtifactReferenceFact | None
    projection_contract_fingerprint: Fingerprint
    reference_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _empty(self) -> "PreparedRuntimeObservationRewriteProjectionReferenceFact":
        if (self.unit_count == 0) != (self.root_artifact_reference is None):
            raise ValueError("rewrite projection empty/root matrix mismatch")
        return self


@_fact(
    "runtime_observation_projection_rewrite.v3",
    "fact_fingerprint",
    "runtime-observation-projection-rewrite:v3",
)
class RuntimeObservationProjectionRewriteFact(FrozenFactBase):
    schema_version: Literal["runtime_observation_projection_rewrite.v3"] = (
        "runtime_observation_projection_rewrite.v3"
    )
    rewrite_id: str
    parent_long_horizon_rewrite_event_reference: ContextEventReferenceFact
    source_stable_state: RuntimeObservationProjectionStableStateFact
    partition_proof: RuntimeObservationProjectionPartitionProofFact
    prepared_replacement_projection: PreparedRuntimeObservationRewriteProjectionReferenceFact
    resulting_effective_heads: RuntimeObservationEffectiveHeadSetReferenceFact
    coverage_lineage_contract_fingerprint: Fingerprint
    unified_ordered_projection_contract_fingerprint: Fingerprint
    resulting_ordered_provider_projection_fingerprint: Fingerprint
    physical_policy_fingerprint: Fingerprint
    rewrite_policy_id: str
    rewrite_policy_version: str
    rewrite_policy_fingerprint: Fingerprint
    fact_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _joins(self) -> "RuntimeObservationProjectionRewriteFact":
        stable = self.source_stable_state
        proof = self.partition_proof
        if proof.source_stable_state_fingerprint != stable.stable_state_fingerprint:
            raise ValueError("observation rewrite stable state/proof mismatch")
        if proof.active_set_reference != stable.active_observations:
            raise ValueError("observation rewrite active set mismatch")
        if proof.protected_set_reference != stable.protected_observations:
            raise ValueError("observation rewrite protected set mismatch")
        if proof.eligible_set_reference != stable.eligible_observations:
            raise ValueError("observation rewrite eligible set mismatch")
        if self.physical_policy_fingerprint != stable.physical_policy_fingerprint:
            raise ValueError("observation rewrite physical policy mismatch")
        return self


__all__ = [
    name
    for name in globals()
    if name.endswith("Fact")
    or name in {
        "ObservationLifecycleClass",
        "ObservationTransitionKind",
        "RuntimeObservationProducerFact",
        "RuntimeRequestKind",
        "RuntimeRequestOwnerFact",
        "RuntimeRequestPayloadFact",
    }
]
