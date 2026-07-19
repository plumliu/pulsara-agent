"""Source-specific, process-local inputs for ContextSource bindings.

These carriers are intentionally narrower than ``ContextFactSnapshotFact``.
Only the composition-root builder may see the full snapshot; a source binding
receives one discriminator-specific input and cannot inspect another source's
facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from pulsara_agent.primitives.context_source import (
    ActiveSkillPayloadFact,
    CanonicalContextSourceRevisionFact,
    CapabilityCatalogPayloadFact,
    ContextArtifactReferenceFact,
    ContextCandidateLoweringIntentFact,
    ContextSourceAbsoluteTimingFact,
    ContextSourceInputAuthorityFact,
    ContextSourceLifecycleFact,
    McpDiagnosticPayloadFact,
    MemoryInstructionPayloadFact,
    MemoryProjectionPayloadFact,
    PlanRevisionPayloadFact,
    RecoveryObservationPayloadFact,
    RolloutStatusPayloadFact,
    RuntimeClockProposalPayloadFact,
    RuntimeEnvironmentPayloadFact,
    SubagentHandoffPayloadFact,
    SubagentResultPayloadFact,
    SystemInstructionPayloadFact,
    WorkspaceSkillPayloadFact,
    ContextSourceId,
)
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    context_fingerprint,
)


@dataclass(frozen=True, slots=True)
class _BaseSourceInput:
    authority: ContextSourceInputAuthorityFact
    source_instance_id: str
    candidate_key: str
    source_revision: CanonicalContextSourceRevisionFact
    lifecycle: ContextSourceLifecycleFact
    priority: int
    required: bool
    lowering_intent: ContextCandidateLoweringIntentFact
    source_event_refs: tuple[ContextEventReferenceFact, ...]
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...]
    source_absolute_timing: ContextSourceAbsoluteTimingFact | None


@dataclass(frozen=True, slots=True)
class SystemSourceInput(_BaseSourceInput):
    payload: SystemInstructionPayloadFact


@dataclass(frozen=True, slots=True)
class RuntimeEnvironmentSourceInput(_BaseSourceInput):
    payload: RuntimeEnvironmentPayloadFact


@dataclass(frozen=True, slots=True)
class RuntimeClockSourceInput(_BaseSourceInput):
    payload: RuntimeClockProposalPayloadFact


@dataclass(frozen=True, slots=True)
class MemoryInstructionSourceInput(_BaseSourceInput):
    payload: MemoryInstructionPayloadFact


@dataclass(frozen=True, slots=True)
class MemoryProjectionSourceInput(_BaseSourceInput):
    payload: MemoryProjectionPayloadFact


@dataclass(frozen=True, slots=True)
class CapabilityCatalogSourceInput(_BaseSourceInput):
    payload: CapabilityCatalogPayloadFact


@dataclass(frozen=True, slots=True)
class ActiveSkillSourceInput(_BaseSourceInput):
    payload: ActiveSkillPayloadFact


@dataclass(frozen=True, slots=True)
class WorkspaceSkillSourceInput(_BaseSourceInput):
    payload: WorkspaceSkillPayloadFact


@dataclass(frozen=True, slots=True)
class PlanSourceInput(_BaseSourceInput):
    payload: PlanRevisionPayloadFact


@dataclass(frozen=True, slots=True)
class RecoverySourceInput(_BaseSourceInput):
    payload: RecoveryObservationPayloadFact


@dataclass(frozen=True, slots=True)
class RolloutStatusSourceInput(_BaseSourceInput):
    payload: RolloutStatusPayloadFact


@dataclass(frozen=True, slots=True)
class SubagentHandoffSourceInput(_BaseSourceInput):
    payload: SubagentHandoffPayloadFact


@dataclass(frozen=True, slots=True)
class SubagentResultSourceInput(_BaseSourceInput):
    payload: SubagentResultPayloadFact


@dataclass(frozen=True, slots=True)
class McpDiagnosticSourceInput(_BaseSourceInput):
    payload: McpDiagnosticPayloadFact


ContextSourceCollectInput: TypeAlias = (
    SystemSourceInput
    | RuntimeEnvironmentSourceInput
    | RuntimeClockSourceInput
    | MemoryInstructionSourceInput
    | MemoryProjectionSourceInput
    | CapabilityCatalogSourceInput
    | ActiveSkillSourceInput
    | WorkspaceSkillSourceInput
    | PlanSourceInput
    | RecoverySourceInput
    | RolloutStatusSourceInput
    | SubagentHandoffSourceInput
    | SubagentResultSourceInput
    | McpDiagnosticSourceInput
)


CONTEXT_SOURCE_INPUT_TYPES = {
    ContextSourceId.SYSTEM: SystemSourceInput,
    ContextSourceId.RUNTIME_ENVIRONMENT: RuntimeEnvironmentSourceInput,
    ContextSourceId.RUNTIME_CLOCK: RuntimeClockSourceInput,
    ContextSourceId.MEMORY_INSTRUCTION: MemoryInstructionSourceInput,
    ContextSourceId.MEMORY_PROJECTION: MemoryProjectionSourceInput,
    ContextSourceId.CAPABILITY_CATALOG: CapabilityCatalogSourceInput,
    ContextSourceId.ACTIVE_SKILL: ActiveSkillSourceInput,
    ContextSourceId.WORKSPACE_SKILL: WorkspaceSkillSourceInput,
    ContextSourceId.PLAN: PlanSourceInput,
    ContextSourceId.RECOVERY: RecoverySourceInput,
    ContextSourceId.ROLLOUT_STATUS: RolloutStatusSourceInput,
    ContextSourceId.SUBAGENT_HANDOFF: SubagentHandoffSourceInput,
    ContextSourceId.SUBAGENT_RESULT: SubagentResultSourceInput,
    ContextSourceId.MCP_DIAGNOSTIC: McpDiagnosticSourceInput,
}

CONTEXT_SOURCE_PAYLOAD_TYPES = {
    ContextSourceId.SYSTEM: SystemInstructionPayloadFact,
    ContextSourceId.RUNTIME_ENVIRONMENT: RuntimeEnvironmentPayloadFact,
    ContextSourceId.RUNTIME_CLOCK: RuntimeClockProposalPayloadFact,
    ContextSourceId.MEMORY_INSTRUCTION: MemoryInstructionPayloadFact,
    ContextSourceId.MEMORY_PROJECTION: MemoryProjectionPayloadFact,
    ContextSourceId.CAPABILITY_CATALOG: CapabilityCatalogPayloadFact,
    ContextSourceId.ACTIVE_SKILL: ActiveSkillPayloadFact,
    ContextSourceId.WORKSPACE_SKILL: WorkspaceSkillPayloadFact,
    ContextSourceId.PLAN: PlanRevisionPayloadFact,
    ContextSourceId.RECOVERY: RecoveryObservationPayloadFact,
    ContextSourceId.ROLLOUT_STATUS: RolloutStatusPayloadFact,
    ContextSourceId.SUBAGENT_HANDOFF: SubagentHandoffPayloadFact,
    ContextSourceId.SUBAGENT_RESULT: SubagentResultPayloadFact,
    ContextSourceId.MCP_DIAGNOSTIC: McpDiagnosticPayloadFact,
}


def context_source_input_dependency_fingerprint(
    *,
    source_instance_id: str,
    candidate_key: str,
    source_revision,
    payload,
    lifecycle,
    priority: int,
    required: bool,
    lowering_intent,
    source_event_refs,
    source_artifact_refs,
    source_absolute_timing,
    source_contract_fingerprint: str,
) -> str:
    """Cover every caller-supplied field before a binding accepts the input."""

    return context_fingerprint(
        "context-source-input-dependency:v2",
        {
            "source_instance_id": source_instance_id,
            "candidate_key": candidate_key,
            "source_revision": source_revision,
            "payload": payload,
            "lifecycle": lifecycle,
            "priority": priority,
            "required": required,
            "lowering_intent": lowering_intent,
            "source_event_refs": source_event_refs,
            "source_artifact_refs": source_artifact_refs,
            "source_absolute_timing": source_absolute_timing,
            "source_contract_fingerprint": source_contract_fingerprint,
        },
    )


def recompute_context_source_input_dependency(
    source_input: ContextSourceCollectInput,
) -> str:
    return context_source_input_dependency_fingerprint(
        source_instance_id=source_input.source_instance_id,
        candidate_key=source_input.candidate_key,
        source_revision=source_input.source_revision,
        payload=source_input.payload,
        lifecycle=source_input.lifecycle,
        priority=source_input.priority,
        required=source_input.required,
        lowering_intent=source_input.lowering_intent,
        source_event_refs=source_input.source_event_refs,
        source_artifact_refs=source_input.source_artifact_refs,
        source_absolute_timing=source_input.source_absolute_timing,
        source_contract_fingerprint=(
            source_input.authority.source_contract_fingerprint
        ),
    )


__all__ = [
    "ActiveSkillSourceInput",
    "CapabilityCatalogSourceInput",
    "ContextSourceCollectInput",
    "McpDiagnosticSourceInput",
    "MemoryInstructionSourceInput",
    "MemoryProjectionSourceInput",
    "PlanSourceInput",
    "RecoverySourceInput",
    "RolloutStatusSourceInput",
    "RuntimeClockSourceInput",
    "RuntimeEnvironmentSourceInput",
    "SubagentHandoffSourceInput",
    "SubagentResultSourceInput",
    "SystemSourceInput",
    "WorkspaceSkillSourceInput",
    "CONTEXT_SOURCE_INPUT_TYPES",
    "CONTEXT_SOURCE_PAYLOAD_TYPES",
    "context_source_input_dependency_fingerprint",
    "recompute_context_source_input_dependency",
]
