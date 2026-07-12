"""Typed object model for Pulsara's context compiler."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.message import Msg
from pulsara_agent.runtime.state import LoopBudget, LoopState
from pulsara_agent.primitives.model_call import (
    ContextBudgetReportEvent,
    ResolvedModelCallFact,
    TokenEstimatorFact,
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
ContextTimingFreshness = Literal[
    "current_turn",
    "current_run_tail",
    "historical_replay",
    "compacted_history",
    "memory_projection",
    "current_tool_observation",
    "cached_snapshot",
    "background_process_observation",
    "subagent_result",
    "unknown",
]
ContextTimingClockSource = Literal[
    "event_created_at",
    "message_created_at",
    "tool_payload",
    "compiler_wall_clock",
    "mixed",
]


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
class ContextLifecycleDecisionDiagnostic:
    """A lifecycle cache decision about an old entry, not a final section state."""

    source_id: str
    section_id: str
    old_cache_key_scope: str
    old_dependency_fingerprint: str
    new_dependency_fingerprint: str
    decision: Literal["invalidated"]
    reason: str

    def to_event_value(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "section_id": self.section_id,
            "old_cache_key_scope": self.old_cache_key_scope,
            "old_dependency_fingerprint": self.old_dependency_fingerprint,
            "new_dependency_fingerprint": self.new_dependency_fingerprint,
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContextSectionSourceTiming:
    """Cacheable source-time facts for a context section.

    This object intentionally excludes compile-time facts such as
    ``compiled_at_utc`` and derived age. Those are render-time overlay data and
    must not participate in lifecycle dependency fingerprints.
    """

    observed_at: str | None = None
    source_started_at: str | None = None
    source_ended_at: str | None = None
    source_sequence_start: int | None = None
    source_sequence_end: int | None = None
    freshness: ContextTimingFreshness = "unknown"
    clock_source: ContextTimingClockSource = "mixed"

    def to_event_value(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "source_started_at": self.source_started_at,
            "source_ended_at": self.source_ended_at,
            "source_sequence_start": self.source_sequence_start,
            "source_sequence_end": self.source_sequence_end,
            "freshness": self.freshness,
            "clock_source": self.clock_source,
        }


@dataclass(frozen=True, slots=True)
class ContextSectionRenderTiming:
    """Final render-time timing facts emitted to inspect/model-visible headers."""

    compiled_at_utc: str
    session_timezone: str | None = None
    compiled_local_date: str | None = None
    age_seconds: float | None = None
    source: ContextSectionSourceTiming = field(
        default_factory=ContextSectionSourceTiming
    )

    def to_event_value(self) -> dict[str, Any]:
        return {
            "compiled_at_utc": self.compiled_at_utc,
            "session_timezone": self.session_timezone,
            "compiled_local_date": self.compiled_local_date,
            "age_seconds": self.age_seconds,
            "source": self.source.to_event_value(),
        }


@dataclass(frozen=True, slots=True)
class ContextSection:
    """A candidate fact projection before final lowering into LLMContext."""

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
class ContextSourceOutput:
    source_id: str
    sections: tuple[ContextSection, ...]
    diagnostics: tuple[ContextDiagnostic, ...] = ()


class ContextSource(Protocol):
    """Collect existing runtime facts as context sections.

    Implementations must not create new runtime truth: no recall, no permission
    checks, no database governance, no tool execution.
    """

    source_id: str

    def collect(self, request: "ContextCompileRequest") -> ContextSourceOutput: ...


@dataclass(frozen=True, slots=True)
class ContextCompileRequest:
    """Inputs owned by AgentRuntime for a single model call context compile."""

    context_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    model_call_index: int
    compiled_at_utc: str
    user_observed_at_utc: str
    resolved_call: ResolvedModelCall
    state: LoopState
    current_user_message: Msg | None
    current_user_input: str
    current_user_anchor: str | None
    tools: tuple[ToolSpec, ...]
    exposure: CapabilityExposurePlan | None
    budget: LoopBudget
    session_timezone: str | None = None
    compiled_local_date: str | None = None


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
    lifecycle_decisions: tuple[ContextLifecycleDecisionDiagnostic, ...]
    estimated_tokens: int
    budget: ContextBudgetReport
    resolved_model_call: ResolvedModelCallFact
    final_token_estimate: TokenEstimate
    message_budget_scopes: tuple[Literal["transcript", "non_transcript"], ...]
    tool_result_render_decisions: tuple[dict[str, Any], ...] = ()
    tool_result_budget_report: dict[str, Any] = field(default_factory=dict)

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
                decision.to_event_value() for decision in self.lifecycle_decisions
            ],
            "tool_result_render_decisions": [
                dict(decision) for decision in self.tool_result_render_decisions
            ],
            "tool_result_budget_report": dict(self.tool_result_budget_report),
        }
