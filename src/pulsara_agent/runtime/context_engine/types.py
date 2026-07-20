"""Typed object model for Pulsara's context compiler."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    ResolvedModelCallFact,
    TokenEstimatorFact,
)
from pulsara_agent.primitives.tool_result import (
    ToolResultRenderDecisionFact,
    ToolResultRenderOperationalFact,
)
from pulsara_agent.primitives.transcript_projection import (
    ModelVisibleNamedFactSemanticSelectionFact,
    TranscriptProviderProjectionFact,
)

if TYPE_CHECKING:
    from pulsara_agent.primitives._context_base import ContextEventReferenceFact
    from pulsara_agent.primitives.context import ContextSectionCandidate
    from pulsara_agent.primitives.context_source import ContextSourceDispositionFact
    from pulsara_agent.runtime.context_input.provider_projection import (
        PreparedTranscriptProviderProjectionFact,
    )
    from pulsara_agent.runtime.provider_input.causal import (
        PreparedOrderedProviderTranscriptProjection,
    )
    from pulsara_agent.primitives.provider_input import (
        ResolvedProviderInputCausalAndPhysicalPolicyFact,
    )

ContextChannel = Literal[
    "system",
    "leading_user",
    "history",
    "current_user",
    "current_run_tail",
    "tool_context",
    "handoff_hint",
]
ContextStability = Literal["stable", "turn", "step", "ephemeral"]
ContextBudgetClass = Literal["must_keep", "important", "optional", "debug"]
ContextRenderMode = Literal["full", "compact", "summary", "ref_only", "omitted"]
ContextLifecycleStatus = Literal["freshly_collected", "reused", "not_cacheable"]


class ContextBudgetExceeded(ValueError):
    """Raised when a must-keep context section cannot fit the model input budget."""

    def __init__(
        self,
        message: str,
        *,
        context_id: str | None = None,
        model_call_index: int | None = None,
        diagnostics: tuple[ContextDiagnostic, ...] = (),
        tool_result_render_decisions: tuple[dict[str, Any], ...] = (),
        tool_result_budget_report: dict[str, Any] | None = None,
        budget_report: ContextBudgetReport | None = None,
    ) -> None:
        super().__init__(message)
        self.context_id = context_id
        self.model_call_index = model_call_index
        self.diagnostics = diagnostics
        self.tool_result_render_decisions = tool_result_render_decisions
        self.tool_result_budget_report = tool_result_budget_report or {}
        self.budget_report = budget_report


@dataclass(frozen=True, slots=True)
class ContextDiagnostic:
    """A compile warning/error/degradation fact, not a subsystem truth decision."""

    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    section_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_event_value(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "section_id": self.section_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AllocatedContextSection:
    """Process-local allocation state derived from typed candidate facts."""

    id: str
    source_id: str
    channel: ContextChannel
    priority: int
    stability: ContextStability
    budget_class: ContextBudgetClass
    text: str = ""
    render_mode: ContextRenderMode = "full"
    included: bool = True
    estimated_tokens: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    lifecycle_status: ContextLifecycleStatus | None = None
    lifecycle_reason: str | None = None
    dependency_fingerprint: str | None = None
    cache_key_scope: str | None = None


@dataclass(frozen=True, slots=True)
class CompiledContextSection:
    id: str
    source_id: str
    channel: ContextChannel
    render_mode: ContextRenderMode
    included: bool
    estimated_tokens: int
    lifecycle_status: ContextLifecycleStatus | None
    lifecycle_reason: str | None
    dependency_fingerprint: str | None
    cache_key_scope: str | None
    provenance: dict[str, Any]
    metadata: dict[str, Any]

    def to_event_value(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "channel": self.channel,
            "render_mode": self.render_mode,
            "included": self.included,
            "estimated_tokens": self.estimated_tokens,
            "lifecycle_status": self.lifecycle_status,
            "lifecycle_reason": self.lifecycle_reason,
            "dependency_fingerprint": self.dependency_fingerprint,
            "cache_key_scope": self.cache_key_scope,
            "provenance": dict(self.provenance),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompiledToolSpecUnit:
    name: str
    descriptor_id: str | None
    schema_chars: int
    estimated_tokens: int
    included: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_event_value(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "descriptor_id": self.descriptor_id,
            "schema_chars": self.schema_chars,
            "estimated_tokens": self.estimated_tokens,
            "included": self.included,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompiledProviderSystemFragment:
    """One source-owned fragment before final provider system lowering."""

    source_id: str
    source_instance_id: str
    owner_semantic_fingerprint: str
    rendered_text: str


@dataclass(frozen=True, slots=True)
class CompiledProviderSourceFragment:
    """One exact source-owned fragment accepted by compiler allocation."""

    candidate: ContextSectionCandidate
    render_mode: ContextRenderMode
    provider_lane: str
    message: LLMMessage
    estimated_tokens: int

    @property
    def source_id(self) -> str:
        return self.candidate.source_id.value

    @property
    def source_instance_id(self) -> str:
        return self.candidate.source_instance_id

    @property
    def owner_semantic_fingerprint(self) -> str:
        return self.candidate.semantic_fingerprint


@dataclass(frozen=True, slots=True)
class ContextBudgetReport:
    target_fingerprint: str
    resolved_model_call_id: str
    measurement_stage: Literal[
        "tool_result_render",
        "section_allocation",
        "final_payload",
    ]
    total_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    effective_output_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
    sections_estimated_tokens: int | None
    tools_estimated_tokens: int | None
    envelope_estimated_tokens: int | None
    allocation_estimated_tokens: int | None
    final_payload_estimated_tokens: int | None
    non_transcript_baseline_tokens: int | None
    transcript_estimated_tokens: int | None
    estimator: TokenEstimatorFact

    def to_event_value(self) -> ContextBudgetReportEvent:
        return ContextBudgetReportEvent(**asdict(self))


@dataclass(frozen=True, slots=True)
class CompiledContext:
    context_id: str
    llm_context: LLMContext
    sections: tuple[CompiledContextSection, ...]
    tool_specs: tuple[CompiledToolSpecUnit, ...]
    diagnostics: tuple[ContextDiagnostic, ...]
    lifecycle_decisions: tuple[dict[str, Any], ...]
    estimated_tokens: int
    budget: ContextBudgetReport
    resolved_model_call: ResolvedModelCallFact
    final_token_estimate: TokenEstimate
    message_budget_scopes: tuple[Literal["transcript", "non_transcript"], ...]
    prepared_transcript_provider_projection: PreparedTranscriptProviderProjectionFact
    prepared_ordered_transcript_projection: (
        PreparedOrderedProviderTranscriptProjection | None
    )
    provider_causal_physical_policy: (
        ResolvedProviderInputCausalAndPhysicalPolicyFact
    )
    model_visible_named_fact_semantic_selection: (
        ModelVisibleNamedFactSemanticSelectionFact
    )
    tool_result_render_decisions: tuple[dict[str, Any], ...] = ()
    tool_result_budget_report: dict[str, Any] = field(default_factory=dict)
    tool_result_render_decision_facts: tuple[ToolResultRenderDecisionFact, ...] = ()
    tool_result_render_operational_facts: tuple[
        ToolResultRenderOperationalFact, ...
    ] = ()
    provider_source_fragments: tuple[CompiledProviderSourceFragment, ...] = ()
    provider_source_dispositions: tuple[ContextSourceDispositionFact, ...] = ()
    transcript_source_event_refs_by_message: tuple[
        tuple[ContextEventReferenceFact, ...], ...
    ] = ()

    @property
    def provider_system_fragments(self) -> tuple[CompiledProviderSystemFragment, ...]:
        fragments: list[CompiledProviderSystemFragment] = []
        for fragment in self.provider_source_fragments:
            if fragment.provider_lane != "system_prompt":
                continue
            if len(fragment.message.content) != 1:
                raise ValueError("compiled system source must contain one text block")
            fragments.append(
                CompiledProviderSystemFragment(
                    source_id=fragment.source_id,
                    source_instance_id=fragment.source_instance_id,
                    owner_semantic_fingerprint=(fragment.owner_semantic_fingerprint),
                    rendered_text=fragment.message.content[0],
                )
            )
        return tuple(fragments)

    @property
    def transcript_provider_projection(self) -> TranscriptProviderProjectionFact:
        return self.prepared_transcript_provider_projection.projection_fact

    @property
    def transcript_provider_messages(self) -> tuple[LLMMessage, ...]:
        ordered = self.prepared_ordered_transcript_projection
        if ordered is not None:
            return ordered.lowered_messages
        return self.prepared_transcript_provider_projection.lowered_provider_messages

    def to_event_value(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "model_call_index": self.llm_context.model_call_index,
            "budget": self.budget.to_event_value(),
            "sections": [section.to_event_value() for section in self.sections],
            "tool_specs": [tool.to_event_value() for tool in self.tool_specs],
            "diagnostics": [
                diagnostic.to_event_value() for diagnostic in self.diagnostics
            ],
            "lifecycle_decisions": [
                dict(decision) for decision in self.lifecycle_decisions
            ],
            "tool_result_render_decisions": [
                dict(decision) for decision in self.tool_result_render_decisions
            ],
            "tool_result_budget_report": dict(self.tool_result_budget_report),
            "tool_result_render_decision_facts": list(
                self.tool_result_render_decision_facts
            ),
            "tool_result_render_operational_facts": list(
                self.tool_result_render_operational_facts
            ),
        }


def bind_compiled_context_to_provider_carrier(
    *,
    compiled_context: CompiledContext,
    provider_context: LLMContext,
    token_estimate: TokenEstimate,
    message_budget_scopes: tuple[Literal["transcript", "non_transcript"], ...],
) -> CompiledContext:
    """Bind compiler attribution to the exact provider-input carrier."""

    if len(message_budget_scopes) != len(provider_context.messages):
        raise ValueError("provider carrier message/scope projection drifted")
    transcript_tokens = sum(
        token_estimate.message_tokens_by_index[index]
        for index, scope in enumerate(message_budget_scopes)
        if scope == "transcript"
    )
    non_transcript_message_tokens = sum(
        token_estimate.message_tokens_by_index[index]
        for index, scope in enumerate(message_budget_scopes)
        if scope == "non_transcript"
    )
    non_transcript_baseline = (
        token_estimate.system_tokens
        + token_estimate.tool_tokens
        + token_estimate.envelope_tokens
        + non_transcript_message_tokens
    )
    section_tokens = token_estimate.system_tokens + non_transcript_message_tokens
    bound_context = replace(
        provider_context,
        compiler_estimated_input_tokens=token_estimate.total_input_tokens,
    )
    budget = replace(
        compiled_context.budget,
        sections_estimated_tokens=section_tokens,
        tools_estimated_tokens=token_estimate.tool_tokens,
        envelope_estimated_tokens=token_estimate.envelope_tokens,
        allocation_estimated_tokens=section_tokens + token_estimate.tool_tokens,
        final_payload_estimated_tokens=token_estimate.total_input_tokens,
        non_transcript_baseline_tokens=non_transcript_baseline,
        transcript_estimated_tokens=transcript_tokens,
    )
    return replace(
        compiled_context,
        llm_context=bound_context,
        estimated_tokens=token_estimate.total_input_tokens,
        budget=budget,
        final_token_estimate=token_estimate,
        message_budget_scopes=message_budget_scopes,
    )
