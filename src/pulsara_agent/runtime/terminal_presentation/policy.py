"""Exhaustive presentation-purpose policy and durable-audit extractor registry."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Literal

from pulsara_agent.event import AgentEvent, EventType, RunStartEvent
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    EventSchemaDomainRegistry,
)
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    AuditCell,
    DurableHistoryCell,
    PresentationAuditExtractorContractFact,
    PresentationEventPurposePolicyFact,
    PresentationPurposePolicyRegistryFact,
    PresentationTextContentBlockFact,
)
from pulsara_agent.primitives.stored_event import RawStoredEventEnvelope
from pulsara_agent.runtime.authority_materialization.contracts import (
    TranscriptEventDomainRegistryBinding,
)


AUDIT_EXTRACTOR_ID = "pulsara.presentation.durable-audit"
AUDIT_EXTRACTOR_VERSION = "1"

_RUN_LIFECYCLE = frozenset({EventType.RUN_START.value, EventType.RUN_END.value})
_INTERACTION = frozenset(
    {
        EventType.TOOL_EXECUTION_SUSPENDED.value,
        EventType.MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED.value,
        EventType.MCP_INPUT_REQUIRED_INTERACTION_CLOSED.value,
        EventType.MCP_INPUT_REQUIRED_EXPIRED.value,
        EventType.MCP_INPUT_REQUIRED_BINDING_CHANGED.value,
        EventType.MCP_INPUT_REQUIRED_RESUME_FAILED.value,
    }
)
_COMPACTION = frozenset(item.value for item in EventType if "COMPACTION" in item.value)
_RECOVERY = frozenset(
    item.value
    for item in EventType
    if "RECOVER" in item.value or "REOPEN" in item.value
)
_AUDIT_TYPES = (
    _RUN_LIFECYCLE
    | _INTERACTION
    | _COMPACTION
    | _RECOVERY
    | {
        EventType.RUN_ERROR.value,
        EventType.MODEL_CALL_REJECTED.value,
    }
)


@dataclass(frozen=True, slots=True)
class AuditPlacementRequest:
    request_kind: Literal["before_leaf", "after_leaf", "ledger_sequence"]
    target_transcript_message_id: str | None
    audit_local_ordinal: int


@dataclass(frozen=True, slots=True)
class ExtractedDurableAuditCell:
    cell: DurableHistoryCell
    placement_request: AuditPlacementRequest
    extractor_id: str
    extractor_version: str
    extractor_contract_fingerprint: str
    extractor_output_ordinal: int


def build_default_presentation_purpose_policy_registry(
    *,
    transcript_domains: TranscriptEventDomainRegistryBinding,
    event_schemas: EventSchemaDomainRegistry = DEFAULT_EVENT_SCHEMA_REGISTRY,
) -> PresentationPurposePolicyRegistryFact:
    """Build an exhaustive two-axis policy for every registered event schema."""

    transcript_by_key = {
        (item.event_type, item.event_schema_version): item
        for item in transcript_domains.contract.supported_events
    }
    policies: list[PresentationEventPurposePolicyFact] = []
    for schema in event_schemas.contracts():
        key = (schema.event_type, schema.event_schema_version)
        try:
            transcript = transcript_by_key[key]
        except KeyError as exc:
            raise ValueError(
                "presentation policy lacks transcript-domain binding"
            ) from exc
        purpose: Literal["semantic", "acceleration", "none"] = {
            "transcript_semantic": "semantic",
            "transcript_acceleration": "acceleration",
            "non_transcript": "none",
        }[transcript.event_domain]
        audit = "extract" if schema.event_type in _AUDIT_TYPES else "none"
        policies.append(
            build_frozen_fact(
                PresentationEventPurposePolicyFact,
                schema_version="presentation_event_purpose_policy.v1",
                event_type=schema.event_type,
                event_schema_version=schema.event_schema_version,
                event_schema_fingerprint=schema.event_schema_fingerprint,
                transcript_purpose=purpose,
                durable_audit_purpose=audit,
                audit_extractor_id=(AUDIT_EXTRACTOR_ID if audit == "extract" else None),
                permitted_audit_field_names=(
                    _audit_field_allowlist(schema.event_type)
                    if audit == "extract"
                    else ()
                ),
            )
        )
    ordered = tuple(
        sorted(
            policies,
            key=lambda item: (
                item.event_type,
                item.event_schema_version,
                item.event_schema_fingerprint,
            ),
        )
    )
    return build_frozen_fact(
        PresentationPurposePolicyRegistryFact,
        schema_version="presentation_purpose_policy_registry.v1",
        registry_id="pulsara.presentation-purpose-policy",
        registry_version="1",
        ordered_policies=ordered,
    )


def _audit_field_allowlist(event_type: str) -> tuple[str, ...]:
    common = {"id", "run_id", "turn_id", "reply_id", "type"}
    additions: set[str] = set()
    if event_type == EventType.RUN_START.value:
        additions.update(("run_entry_kind",))
    elif event_type == EventType.RUN_END.value:
        additions.update(("status", "stop_reason", "terminalization_kind"))
    elif event_type == EventType.RUN_ERROR.value:
        additions.update(("error_code",))
    elif event_type == EventType.MODEL_CALL_REJECTED.value:
        additions.update(("reason",))
    elif event_type == EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED.value:
        additions.update(("disposition", "recovery_reason_code"))
    return tuple(sorted(common | additions))


class PresentationPurposePolicyRegistry:
    def __init__(self, contract: PresentationPurposePolicyRegistryFact) -> None:
        self.contract = contract
        self._by_identity = {
            (
                item.event_type,
                item.event_schema_version,
                item.event_schema_fingerprint,
            ): item
            for item in contract.ordered_policies
        }

    def resolve(
        self, envelope: RawStoredEventEnvelope
    ) -> PresentationEventPurposePolicyFact:
        try:
            return self._by_identity[
                (
                    envelope.event_type,
                    envelope.event_schema_version,
                    envelope.event_schema_fingerprint,
                )
            ]
        except KeyError as exc:
            raise ValueError("stored event lacks presentation purpose policy") from exc


@dataclass(frozen=True, slots=True)
class PresentationAuditExtractorBinding:
    contract: PresentationAuditExtractorContractFact
    implementation_build_fingerprint: str

    def extract(
        self,
        *,
        runtime_session_id: str,
        event: AgentEvent,
        envelope: RawStoredEventEnvelope,
        policy: PresentationEventPurposePolicyFact,
    ) -> tuple[ExtractedDurableAuditCell, ...]:
        if policy.durable_audit_purpose == "none":
            return ()
        if policy.audit_extractor_id != self.contract.extractor_id:
            raise ValueError("presentation audit extractor identity mismatch")
        if str(event.type) != envelope.event_type or event.id != envelope.event_id:
            raise ValueError("presentation audit event/envelope identity mismatch")
        if envelope.event_type not in self.contract.ordered_supported_event_types:
            raise ValueError("presentation audit extractor does not support event")
        return (
            _extract_audit_cell(
                runtime_session_id=runtime_session_id,
                event=event,
                envelope=envelope,
                policy=policy,
                contract=self.contract,
            ),
        )


class PresentationAuditExtractorRegistry:
    """Exact historical binding registry; same ID/version conflicts fail closed."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._bindings: dict[
            tuple[str, str, str], PresentationAuditExtractorBinding
        ] = {}

    def register(self, binding: PresentationAuditExtractorBinding) -> None:
        contract = binding.contract
        key = (
            contract.extractor_id,
            contract.extractor_version,
            contract.contract_fingerprint,
        )
        with self._lock:
            conflicting = next(
                (
                    candidate
                    for identity, candidate in self._bindings.items()
                    if identity[:2] == key[:2] and identity[2] != key[2]
                ),
                None,
            )
            if conflicting is not None:
                raise ValueError("audit extractor ID/version contract conflict")
            existing = self._bindings.get(key)
            if existing is not None and existing != binding:
                raise ValueError("audit extractor implementation binding conflict")
            self._bindings[key] = binding

    def resolve_exact(
        self, extractor_id: str, extractor_version: str, contract_fingerprint: str
    ) -> PresentationAuditExtractorBinding:
        with self._lock:
            try:
                return self._bindings[
                    (extractor_id, extractor_version, contract_fingerprint)
                ]
            except KeyError as exc:
                raise ValueError(
                    "historical audit extractor binding is unavailable"
                ) from exc


def build_default_audit_extractor_binding() -> PresentationAuditExtractorBinding:
    contract = build_frozen_fact(
        PresentationAuditExtractorContractFact,
        schema_version="presentation_audit_extractor_contract.v1",
        extractor_id=AUDIT_EXTRACTOR_ID,
        extractor_version=AUDIT_EXTRACTOR_VERSION,
        output_union_contract_fingerprint=context_fingerprint(
            "presentation-audit-output-union:v1",
            (
                "error",
                "interaction",
                "compaction_boundary",
                "recovery",
                "audit",
                "system_notice",
            ),
        ),
        ordered_supported_event_types=tuple(sorted(_AUDIT_TYPES)),
        maximum_outputs_per_event=1,
        maximum_public_text_utf8_bytes=1_024,
    )
    return PresentationAuditExtractorBinding(
        contract=contract,
        implementation_build_fingerprint="builtin-presentation-audit-extractor:v1",
    )


def _extract_audit_cell(
    *,
    runtime_session_id: str,
    event: AgentEvent,
    envelope: RawStoredEventEnvelope,
    policy: PresentationEventPurposePolicyFact,
    contract: PresentationAuditExtractorContractFact,
) -> ExtractedDurableAuditCell:
    ref = ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=envelope.event_id,
        sequence=envelope.sequence,
        event_type=envelope.event_type,
        payload_fingerprint=envelope.payload_fingerprint,
    )
    audit_kind, severity, text = _closed_audit_summary(event)
    block = build_frozen_fact(
        PresentationTextContentBlockFact,
        schema_version="presentation_text_content_block.v1",
        block_kind="text",
        text=text,
        text_utf8_bytes=len(text.encode("utf-8")),
        semantic_role="secondary",
    )
    cell_id = f"presentation:audit:{event.id}:0"
    source_accumulator = context_fingerprint(
        "presentation-history-cell-sources:v1",
        ((ref.sequence, ref.event_id, ref.payload_fingerprint),),
    )
    cell = build_frozen_fact(
        AuditCell,
        schema_version="presentation_audit_cell.v1",
        cell_kind="audit",
        stable_cell_id=cell_id,
        semantic_revision=1,
        ordered_source_event_references=(ref,),
        source_accumulator=source_accumulator,
        visibility_policy="always" if severity == "error" else "normal",
        content_blocks=(block,),
        semantic_group_id=f"run:{event.run_id}",
        audit_kind=audit_kind,
        severity=severity,
    )
    target = None
    placement: Literal["before_leaf", "after_leaf", "ledger_sequence"] = (
        "ledger_sequence"
    )
    if envelope.event_type == EventType.RUN_START.value:
        if not isinstance(event, RunStartEvent):
            raise TypeError("RUN_START audit extraction lacks its typed event")
        placement = "before_leaf"
        target = event.current_user_message.message_id
    return ExtractedDurableAuditCell(
        cell=cell,
        placement_request=AuditPlacementRequest(
            request_kind=placement,
            target_transcript_message_id=target,
            audit_local_ordinal=0,
        ),
        extractor_id=contract.extractor_id,
        extractor_version=contract.extractor_version,
        extractor_contract_fingerprint=contract.contract_fingerprint,
        extractor_output_ordinal=0,
    )


def _closed_audit_summary(
    event: AgentEvent,
) -> tuple[
    Literal[
        "run_lifecycle",
        "suppressed_model_output",
        "permission",
        "interaction_lifecycle",
        "subagent_lifecycle",
        "compaction_lifecycle",
        "recovery_lifecycle",
    ],
    Literal["info", "warning", "error"],
    str,
]:
    event_type = str(event.type)
    if event_type == EventType.RUN_START.value:
        return "run_lifecycle", "info", "Run started"
    if event_type == EventType.RUN_END.value:
        status = str(getattr(event, "status", "finished"))
        severity: Literal["info", "warning", "error"] = (
            "info" if status in {"finished", "completed"} else "warning"
        )
        return "run_lifecycle", severity, f"Run ended: {status}"
    if event_type == EventType.RUN_ERROR.value:
        return "recovery_lifecycle", "error", "Run reported an execution error"
    if event_type == EventType.MODEL_CALL_CONTROL_DISPOSITION_RESOLVED.value:
        disposition = str(getattr(event, "disposition", "resolved"))
        return (
            "suppressed_model_output",
            "warning" if "SUPPRESSED" in disposition.upper() else "info",
            f"Model output control disposition: {disposition}",
        )
    if event_type in _INTERACTION:
        return "interaction_lifecycle", "info", "Interaction lifecycle changed"
    if event_type in _COMPACTION:
        return "compaction_lifecycle", "info", "Context compaction lifecycle changed"
    if event_type in _RECOVERY:
        return "recovery_lifecycle", "warning", "Runtime recovery lifecycle changed"
    return "permission", "warning", "Runtime request was rejected"


__all__ = [
    "AuditPlacementRequest",
    "ExtractedDurableAuditCell",
    "PresentationAuditExtractorBinding",
    "PresentationAuditExtractorRegistry",
    "PresentationPurposePolicyRegistry",
    "build_default_audit_extractor_binding",
    "build_default_presentation_purpose_policy_registry",
]
