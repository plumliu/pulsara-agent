"""Context compiler for Pulsara's model-visible runtime inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256

from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.message import Msg
from pulsara_agent.runtime.context_engine.types import (
    CompiledContext,
    CompiledContextSection,
    CompiledToolSpecUnit,
    ContextBudgetReport,
    ContextBudgetExceeded,
    ContextCompileRequest,
    ContextDiagnostic,
    ContextLifecycleDecisionDiagnostic,
    ContextRenderMode,
    ContextSection,
)

from pulsara_agent.runtime.context_engine.lifecycle import ContextLifecycleCoordinator
from pulsara_agent.runtime.recovery import project_recovery_from_state, render_recovery_text


@dataclass(frozen=True, slots=True)
class ContextCompileInputs:
    """Rendered compatibility inputs that still come from existing runtime code."""

    system_prompt: str
    prior_messages: tuple[LLMMessage, ...]
    prior_history_messages: tuple[LLMMessage, ...] | None = None
    current_user_messages: tuple[LLMMessage, ...] | None = None
    current_run_tail_messages: tuple[LLMMessage, ...] | None = None
    recovery_message: LLMMessage | None = None
    component_prompts: tuple[tuple[str, str], ...] = ()
    tool_result_render_decisions: tuple[dict[str, object], ...] = ()
    tool_result_budget_report: dict[str, object] | None = None


def compile_context(
    request: ContextCompileRequest,
    *,
    inputs: ContextCompileInputs,
    lifecycle_coordinator: ContextLifecycleCoordinator | None = None,
) -> CompiledContext:
    """Compile a model-visible LLMContext and an inspectable report.

    The compiler owns the final lowering into ``LLMContext``: stable system
    instructions stay in ``system_prompt``; runtime facts, memory projections,
    and capability catalog prose become leading synthetic user context; the
    current user message remains anchored before any current-run assistant/tool
    tail so provider tool-call/tool-result ordering is preserved.
    """

    transcript_messages = _lower_messages(inputs, sections=())
    sections, diagnostics, lifecycle_decisions = _collect_sections(
        request,
        inputs=inputs,
        messages=tuple(transcript_messages),
        lifecycle_coordinator=lifecycle_coordinator,
    )
    tool_units = _compile_tool_units(request)
    context_window_tokens = _context_window_tokens(request)
    reserved_output_tokens = _reserved_output_tokens(request)
    safety_margin_tokens = int(context_window_tokens * 0.25)
    input_budget_tokens = max(0, context_window_tokens - reserved_output_tokens - safety_margin_tokens)
    sections, diagnostics = _apply_section_budget(
        sections,
        diagnostics,
        input_budget_tokens=input_budget_tokens,
        tools_estimated_tokens=sum(tool.estimated_tokens for tool in tool_units if tool.included),
    )
    system_prompt = _lower_system_prompt(sections)
    messages = _lower_messages(inputs, sections=sections)
    compiled_sections = tuple(_compiled_section(section) for section in sections)
    sections_estimated_tokens = sum(
        section.estimated_tokens
        for section in compiled_sections
        if section.included and section.metadata.get("counted_in") is None
    )
    tools_estimated_tokens = sum(tool.estimated_tokens for tool in tool_units if tool.included)
    envelope_estimated_tokens = max(1, len(messages) // 2)
    total_estimated_tokens = sections_estimated_tokens + tools_estimated_tokens + envelope_estimated_tokens
    budget = ContextBudgetReport(
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
        safety_margin_tokens=safety_margin_tokens,
        input_budget_tokens=input_budget_tokens,
        sections_estimated_tokens=sections_estimated_tokens,
        tools_estimated_tokens=tools_estimated_tokens,
        envelope_estimated_tokens=envelope_estimated_tokens,
        total_estimated_tokens=total_estimated_tokens,
    )
    _raise_if_current_user_exceeds_budget(compiled_sections, budget)
    llm_context = LLMContext(
        messages=tuple(messages),
        tools=request.tools,
        system_prompt=system_prompt,
        context_id=request.context_id,
        model_call_index=request.model_call_index,
    )
    return CompiledContext(
        context_id=request.context_id,
        llm_context=llm_context,
        sections=compiled_sections,
        tool_specs=tool_units,
        diagnostics=diagnostics,
        lifecycle_decisions=lifecycle_decisions,
        estimated_tokens=total_estimated_tokens,
        budget=budget,
        tool_result_render_decisions=inputs.tool_result_render_decisions,
        tool_result_budget_report=inputs.tool_result_budget_report or {},
    )


def _lower_system_prompt(sections: tuple[ContextSection, ...]) -> str:
    system_section = next((section for section in sections if section.id == "system:prompt"), None)
    parts = [system_section.text if system_section is not None else ""]
    parts.extend(
        section.text
        for section in sections
        if section.id != "system:prompt"
        and section.channel == "system"
        and section.included
        and section.text
    )
    parts.extend(
        f"## {_component_label(section.id)}\n{section.text}"
        for section in sections
        if section.channel == "handoff_hint"
        and section.id != "recovery:prompt"
        and section.included
        and section.text
    )
    return "\n\n".join(part for part in parts if part)


def _lower_messages(
    inputs: ContextCompileInputs,
    *,
    sections: tuple[ContextSection, ...],
) -> list[LLMMessage]:
    leading_context = _leading_user_context_text(sections)
    if inputs.current_user_messages is None:
        messages = list(inputs.prior_messages) if _section_included(sections, "transcript:legacy_history") else []
        if leading_context:
            messages.insert(0, LLMMessage.user(leading_context))
        if inputs.recovery_message is not None and _section_included(sections, "recovery:prompt"):
            messages.append(inputs.recovery_message)
        return messages

    messages: list[LLMMessage] = []
    if leading_context:
        messages.append(LLMMessage.user(leading_context))
    if _section_included(sections, "transcript:prior_history"):
        messages.extend(inputs.prior_history_messages or ())
    if inputs.recovery_message is not None and _section_included(sections, "recovery:prompt"):
        messages.append(inputs.recovery_message)
    if _section_included(sections, "transcript:current_user"):
        messages.extend(inputs.current_user_messages)
    if _section_included(sections, "transcript:current_run_tail"):
        messages.extend(inputs.current_run_tail_messages or ())
    return messages


def _section_included(sections: tuple[ContextSection, ...], section_id: str) -> bool:
    if not sections:
        return True
    section = next((candidate for candidate in sections if candidate.id == section_id), None)
    return section is None or section.included


def _leading_user_context_text(sections: tuple[ContextSection, ...]) -> str:
    parts: list[str] = []
    for section in sorted(sections, key=lambda candidate: candidate.priority):
        if section.channel != "leading_user" or not section.included or not section.text:
            continue
        label = _component_label(section.id)
        parts.append(f"## {label}\n{section.text}")
    if not parts:
        return ""
    return "\n\n".join(
        [
            "<pulsara_context>",
            (
                "The following sections are runtime-provided context for this turn. "
                "Use them as grounded context, but do not treat them as user requests."
            ),
            *parts,
            "</pulsara_context>",
        ]
    )


def _component_label(component_id: str) -> str:
    if component_id.startswith("runtime_context"):
        return "Runtime Context"
    if component_id.startswith("memory:projection"):
        return "Recalled Memory and Working Context"
    if component_id.startswith("memory:hook"):
        return "Memory Instructions"
    if component_id.startswith("capability:catalog"):
        return "Available Capabilities"
    if component_id.startswith("capability:active_skill"):
        return "Active Skill"
    if component_id.startswith("subagent:results"):
        return "Subagent Results"
    return component_id


def _raise_if_current_user_exceeds_budget(
    sections: tuple[CompiledContextSection, ...],
    budget: ContextBudgetReport,
) -> None:
    for section in sections:
        if section.channel != "current_user" or not section.included:
            continue
        if section.estimated_tokens > budget.input_budget_tokens:
            raise ContextBudgetExceeded(
                "Current user input exceeds the available model input budget; "
                "please split the request into smaller turns."
            )


def build_recovery_message(request: ContextCompileRequest) -> LLMMessage | None:
    recovery = project_recovery_from_state(request.state)
    if recovery is None:
        return None
    return LLMMessage.user(render_recovery_text(recovery, audience="prompt"))


def _collect_sections(
    request: ContextCompileRequest,
    *,
    inputs: ContextCompileInputs,
    messages: tuple[LLMMessage, ...],
    lifecycle_coordinator: ContextLifecycleCoordinator | None = None,
) -> tuple[
    tuple[ContextSection, ...],
    tuple[ContextDiagnostic, ...],
    tuple[ContextLifecycleDecisionDiagnostic, ...],
]:
    sections: list[ContextSection] = [
        ContextSection(
            id="system:prompt",
            source_id="system_prompt",
            channel="system",
            priority=0,
            stability="turn",
            budget_class="must_keep",
            text=inputs.system_prompt,
            estimated_tokens=estimate_text_tokens(inputs.system_prompt),
            provenance={"source": "compose_system_prompt"},
            metadata={"chars": len(inputs.system_prompt)},
        ),
    ]
    for component_id, text in inputs.component_prompts:
        if not text:
            continue
        source_id, channel, budget_class = _component_section_classification(component_id)
        metadata = {
            "chars": len(text),
            "lowered_to": "system_prompt" if channel in {"system", "handoff_hint"} else "messages",
        }
        sections.append(
            ContextSection(
                id=component_id,
                source_id=source_id,
                channel=channel,
                priority=10,
                stability="turn",
                budget_class=budget_class,
                text=text,
                estimated_tokens=estimate_text_tokens(text),
                provenance={"source": component_id},
                metadata=metadata,
                dependency_fingerprint=_component_dependency_fingerprint(request, component_id, text),
            )
        )
    diagnostics: list[ContextDiagnostic] = []
    split = _split_messages_by_current_user(request)
    if split is None:
        if request.current_user_anchor is not None:
            diagnostics.append(
                ContextDiagnostic(
                    severity="warning",
                    code="current_user_anchor_unavailable",
                    message=(
                        "Current user anchor was not found exactly once; treating transcript "
                        "as legacy history for this compatibility compile."
                    ),
                    metadata={"current_user_anchor": request.current_user_anchor},
                )
            )
        sections.append(
            ContextSection(
                id="transcript:legacy_history",
                source_id="transcript",
                channel="history",
                priority=100,
                stability="step",
                budget_class="important",
                text=_llm_messages_text(messages),
                estimated_tokens=estimate_messages_tokens(messages),
                provenance={"source": "msg_to_llm_messages"},
                metadata={"message_count": len(messages), "split": "legacy"},
                dependency_fingerprint=_messages_dependency_fingerprint(request.state.messages),
            )
        )
    else:
        prior, current_user, tail = split
        if prior:
            prior_text = _llm_messages_text(inputs.prior_history_messages or ())
            sections.append(
                ContextSection(
                    id="transcript:prior_history",
                    source_id="transcript",
                    channel="history",
                    priority=100,
                    stability="step",
                    budget_class="important",
                    text=prior_text,
                    estimated_tokens=estimate_text_tokens(prior_text),
                    provenance={"source": "msg_to_llm_messages"},
                    metadata={
                        "message_count": len(prior),
                        "llm_message_count": len(inputs.prior_history_messages or ()),
                    },
                    dependency_fingerprint=_messages_dependency_fingerprint(prior),
                )
            )
        sections.append(
            ContextSection(
                id="transcript:current_user",
                source_id="current_user",
                channel="current_user",
                priority=110,
                stability="turn",
                budget_class="must_keep",
                text=request.current_user_input or _messages_text([current_user]),
                estimated_tokens=estimate_text_tokens(request.current_user_input or _messages_text([current_user])),
                provenance={"message_id": current_user.id},
                metadata={"anchor": request.current_user_anchor},
                dependency_fingerprint=f"current_user:{current_user.id}",
            )
        )
        if tail:
            tail_text = _llm_messages_text(inputs.current_run_tail_messages or ())
            sections.append(
                ContextSection(
                    id="transcript:current_run_tail",
                    source_id="current_run_tail",
                    channel="current_run_tail",
                    priority=120,
                    stability="step",
                    budget_class="important",
                    text=tail_text,
                    estimated_tokens=estimate_text_tokens(tail_text),
                    provenance={"source": "state.messages"},
                    metadata={
                        "message_count": len(tail),
                        "llm_message_count": len(inputs.current_run_tail_messages or ()),
                        "structure_must_keep": True,
                        "body_may_degrade": True,
                    },
                    dependency_fingerprint=_messages_dependency_fingerprint(tail),
                )
            )
    if inputs.recovery_message is not None:
        text = "\n".join(inputs.recovery_message.content)
        sections.append(
            ContextSection(
                id="recovery:prompt",
                source_id="recovery",
                channel="handoff_hint",
                priority=90,
                stability="ephemeral",
                budget_class="important",
                text=text,
                estimated_tokens=estimate_text_tokens(text),
                provenance={"source": "project_recovery_from_state"},
                metadata={},
            )
        )
    lifecycle_decisions: tuple[ContextLifecycleDecisionDiagnostic, ...] = ()
    if lifecycle_coordinator is not None:
        sections, lifecycle_decisions = lifecycle_coordinator.apply(request, tuple(sections))
    return tuple(sections), tuple(diagnostics), lifecycle_decisions


def _apply_section_budget(
    sections: tuple[ContextSection, ...],
    diagnostics: tuple[ContextDiagnostic, ...],
    *,
    input_budget_tokens: int,
    tools_estimated_tokens: int,
) -> tuple[tuple[ContextSection, ...], tuple[ContextDiagnostic, ...]]:
    mutable = list(sections)
    emitted = list(diagnostics)
    if _section_total(mutable) + tools_estimated_tokens <= input_budget_tokens:
        return tuple(mutable), tuple(emitted)

    for index, section in list(enumerate(mutable)):
        if _section_total(mutable) + tools_estimated_tokens <= input_budget_tokens:
            break
        degraded = _compact_section(section)
        if degraded is section:
            continue
        mutable[index] = degraded
        emitted.append(_degrade_diagnostic(section, degraded))

    for index, section in list(enumerate(mutable)):
        if _section_total(mutable) + tools_estimated_tokens <= input_budget_tokens:
            break
        if section.budget_class == "must_keep" or section.channel in {"system", "current_user", "current_run_tail"}:
            continue
        omitted = replace(
            section,
            text="",
            estimated_tokens=0,
            render_mode="omitted",
            included=False,
            metadata={
                **section.metadata,
                "original_estimated_tokens": section.estimated_tokens,
                "omitted_reason": "context_budget_exhausted",
            },
        )
        mutable[index] = omitted
        emitted.append(_omit_diagnostic(section))

    if _section_total(mutable) + tools_estimated_tokens > input_budget_tokens:
        emitted.append(
            ContextDiagnostic(
                severity="warning",
                code="context_budget_still_exceeded_after_degradation",
                message=(
                    "Context estimate still exceeds the input budget after degrading all "
                    "non-must-keep sections."
                ),
                metadata={
                    "estimated_tokens": _section_total(mutable) + tools_estimated_tokens,
                    "input_budget_tokens": input_budget_tokens,
                },
            )
        )
    return tuple(mutable), tuple(emitted)


def _section_total(sections: list[ContextSection]) -> int:
    return sum(section.estimated_tokens for section in sections if section.included)


def _compact_section(section: ContextSection) -> ContextSection:
    if not section.included or section.render_mode != "full":
        return section
    if section.id.startswith("capability:catalog"):
        return _replace_with_compact_text(
            section,
            text=_clip_section_text(
                section.text,
                max_chars=2_000,
                marker="\n[CAPABILITY CATALOG COMPACTED: use read_file on the relevant SKILL.md for details.]",
            ),
            render_mode="compact",
            reason="capability_catalog_compacted_for_budget",
        )
    if section.id.startswith("memory:projection"):
        return _replace_with_compact_text(
            section,
            text=_clip_section_text(
                section.text,
                max_chars=1_600,
                marker="\n[MEMORY PROJECTION COMPACTED: use memory_search for more recalled context.]",
            ),
            render_mode="compact",
            reason="memory_projection_compacted_for_budget",
        )
    return section


def _replace_with_compact_text(
    section: ContextSection,
    *,
    text: str,
    render_mode: ContextRenderMode,
    reason: str,
) -> ContextSection:
    return replace(
        section,
        text=text,
        estimated_tokens=estimate_text_tokens(text),
        render_mode=render_mode,
        metadata={
            **section.metadata,
            "original_estimated_tokens": section.estimated_tokens,
            "degraded_reason": reason,
        },
    )


def _clip_section_text(text: str, *, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    kept = max(0, max_chars - len(marker))
    return text[:kept].rstrip() + marker


def _degrade_diagnostic(original: ContextSection, degraded: ContextSection) -> ContextDiagnostic:
    return ContextDiagnostic(
        severity="warning",
        code="context_section_degraded",
        message=f"Context section {original.id} was degraded to {degraded.render_mode} for budget.",
        section_id=original.id,
        metadata={
            "source_id": original.source_id,
            "from_render_mode": original.render_mode,
            "to_render_mode": degraded.render_mode,
            "original_estimated_tokens": original.estimated_tokens,
            "estimated_tokens": degraded.estimated_tokens,
            "reason": degraded.metadata.get("degraded_reason"),
        },
    )


def _omit_diagnostic(section: ContextSection) -> ContextDiagnostic:
    return ContextDiagnostic(
        severity="warning",
        code="context_section_omitted",
        message=f"Context section {section.id} was omitted for budget.",
        section_id=section.id,
        metadata={
            "source_id": section.source_id,
            "render_mode": "omitted",
            "original_estimated_tokens": section.estimated_tokens,
            "reason": "context_budget_exhausted",
        },
    )


def _compile_tool_units(request: ContextCompileRequest) -> tuple[CompiledToolSpecUnit, ...]:
    descriptor_ids: dict[str, str | None] = {}
    if request.exposure is not None:
        descriptor_ids = {
            name: descriptor.id for name, descriptor in request.exposure.descriptors_by_name.items()
        }
    return tuple(
        CompiledToolSpecUnit(
            name=tool.name,
            descriptor_id=descriptor_ids.get(tool.name),
            schema_chars=len(json.dumps(tool.parameters, ensure_ascii=False, sort_keys=True)),
            estimated_tokens=estimate_json_tokens(
                json.dumps(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            included=True,
            metadata={},
        )
        for tool in request.tools
    )


def _component_section_classification(
    component_id: str,
) -> tuple[str, str, str]:
    if component_id.startswith("runtime_context"):
        return "runtime_context", "leading_user", "must_keep"
    if component_id.startswith("capability:active_skill"):
        return "capability_exposure", "system", "important"
    if component_id.startswith("capability:diagnostics"):
        return "capability_exposure", "leading_user", "debug"
    if component_id.startswith("capability:catalog"):
        return "capability_exposure", "leading_user", "important"
    if component_id.startswith("memory:projection"):
        return "memory_projection", "leading_user", "important"
    if component_id.startswith("memory:hook"):
        return "memory_projection", "leading_user", "important"
    if component_id.startswith("subagent:results"):
        return "subagent_runtime", "handoff_hint", "important"
    return "system_prompt", "system", "important"


def _component_dependency_fingerprint(
    request: ContextCompileRequest,
    component_id: str,
    text: str,
) -> str | None:
    if component_id.startswith("runtime_context"):
        return f"{component_id}:workspace:{request.runtime_session_id}:{_text_fingerprint(text)}"
    if component_id.startswith("capability:"):
        generation = request.exposure.registry_generation if request.exposure is not None else "none"
        return f"{component_id}:registry:{generation}"
    if component_id.startswith("memory:projection") and request.state.memory_projection:
        projection = request.state.memory_projection
        included = projection.get("included_memory_ids") or projection.get("included_ids") or ()
        conflict_groups = projection.get("conflict_groups") or ()
        warnings = projection.get("warnings") or ()
        return (
            f"{component_id}:kind:{projection.get('projection_kind')}:"
            f"included:{tuple(included)}:"
            f"conflicts:{repr(conflict_groups)}:"
            f"warnings:{repr(warnings)}:"
            f"text:{_text_fingerprint(text)}"
        )
    if component_id.startswith("memory:hook"):
        return f"{component_id}:prompt:{_text_fingerprint(text)}"
    if component_id.startswith("subagent:results"):
        return f"{component_id}:text:{_text_fingerprint(text)}"
    return None


def _text_fingerprint(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()[:16]


def _messages_dependency_fingerprint(messages: list[Msg]) -> str:
    return "|".join(
        f"{message.id}:{message.role}:{len(message.content)}"
        for message in messages
    )


def _compiled_section(section: ContextSection) -> CompiledContextSection:
    return CompiledContextSection(
        id=section.id,
        source_id=section.source_id,
        channel=section.channel,
        render_mode=section.render_mode,
        included=section.included,
        estimated_tokens=section.estimated_tokens or estimate_text_tokens(section.text),
        lifecycle_status=section.lifecycle_status,
        lifecycle_reason=section.lifecycle_reason,
        dependency_fingerprint=section.dependency_fingerprint,
        cache_key_scope=section.cache_key_scope,
        provenance=dict(section.provenance),
        metadata=dict(section.metadata),
    )


def _split_messages_by_current_user(
    request: ContextCompileRequest,
) -> tuple[list[Msg], Msg, list[Msg]] | None:
    anchor = request.current_user_anchor
    if not anchor:
        return None
    matches = [index for index, message in enumerate(request.state.messages) if message.id == anchor]
    if len(matches) != 1:
        return None
    index = matches[0]
    message = request.state.messages[index]
    if message.role != "user":
        return None
    return request.state.messages[:index], message, request.state.messages[index + 1 :]


def _messages_text(messages: list[Msg]) -> str:
    parts: list[str] = []
    for message in messages:
        block_text: list[str] = []
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                block_text.append(text)
            elif getattr(block, "type", None) == "tool_call":
                block_text.append(f"[tool_call:{getattr(block, 'name', '')}]")
            elif getattr(block, "type", None) == "tool_result":
                block_text.append(f"[tool_result:{getattr(block, 'name', '')}:{getattr(getattr(block, 'state', None), 'value', '')}]")
        if block_text:
            parts.append(f"{message.role}: " + "\n".join(block_text))
    return "\n".join(parts)


def _llm_messages_text(messages: tuple[LLMMessage, ...]) -> str:
    parts: list[str] = []
    for message in messages:
        text = "\n".join(message.content)
        if text:
            parts.append(f"{message.role.value}: {text}")
        if message.tool_calls:
            parts.append(f"{message.role.value}: [tool_calls:{len(message.tool_calls)}]")
    return "\n".join(parts)


def estimate_messages_tokens(messages: tuple[LLMMessage, ...]) -> int:
    return estimate_text_tokens(_llm_messages_text(messages))


def estimate_text_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def estimate_json_tokens(text: str) -> int:
    return max(1, (len(text) + 1) // 2) if text else 0


def _context_window_tokens(request: ContextCompileRequest) -> int:
    # The current runtime does not carry ModelProfile into build_llm_context.
    return 256_000


def _reserved_output_tokens(request: ContextCompileRequest) -> int:
    del request
    return 8_000
