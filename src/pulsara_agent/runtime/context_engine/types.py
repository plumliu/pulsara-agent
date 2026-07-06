"""Typed object model for Pulsara's context compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pulsara_agent.capability.exposure import CapabilityExposurePlan
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.message import Msg
from pulsara_agent.runtime.state import LoopBudget, LoopState

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
    ) -> None:
        super().__init__(message)
        self.context_id = context_id
        self.model_call_index = model_call_index
        self.diagnostics = diagnostics
        self.tool_result_render_decisions = tool_result_render_decisions
        self.tool_result_budget_report = tool_result_budget_report or {}


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

    def collect(self, request: "ContextCompileRequest") -> ContextSourceOutput:
        ...


@dataclass(frozen=True, slots=True)
class ContextCompileRequest:
    """Inputs owned by AgentRuntime for a single model call context compile."""

    context_id: str
    runtime_session_id: str
    run_id: str
    turn_id: str
    reply_id: str
    model_call_index: int
    model_role: ModelRole
    state: LoopState
    current_user_message: Msg | None
    current_user_input: str
    current_user_anchor: str | None
    tools: tuple[ToolSpec, ...]
    exposure: CapabilityExposurePlan | None
    budget: LoopBudget


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
    context_window_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
    sections_estimated_tokens: int
    tools_estimated_tokens: int
    envelope_estimated_tokens: int
    total_estimated_tokens: int

    def to_event_value(self) -> dict[str, int]:
        return {
            "context_window_tokens": self.context_window_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "input_budget_tokens": self.input_budget_tokens,
            "sections_estimated_tokens": self.sections_estimated_tokens,
            "tools_estimated_tokens": self.tools_estimated_tokens,
            "envelope_estimated_tokens": self.envelope_estimated_tokens,
            "total_estimated_tokens": self.total_estimated_tokens,
        }


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
    tool_result_render_decisions: tuple[dict[str, Any], ...] = ()
    tool_result_budget_report: dict[str, Any] = field(default_factory=dict)

    def to_event_value(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "model_call_index": self.llm_context.model_call_index,
            "estimated_tokens": self.estimated_tokens,
            "budget": self.budget.to_event_value(),
            "sections": [section.to_event_value() for section in self.sections],
            "tool_specs": [tool.to_event_value() for tool in self.tool_specs],
            "diagnostics": [diagnostic.to_event_value() for diagnostic in self.diagnostics],
            "lifecycle_decisions": [
                decision.to_event_value() for decision in self.lifecycle_decisions
            ],
            "tool_result_render_decisions": [
                dict(decision) for decision in self.tool_result_render_decisions
            ],
            "tool_result_budget_report": dict(self.tool_result_budget_report),
        }
