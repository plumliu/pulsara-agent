"""Shared OpenAI event translation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pulsara_agent.llm.raw_provider import (
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderFailure,
    RawProviderStreamItem,
    RawProviderTextDelta,
    RawProviderThinkingDelta,
    RawProviderToolCallDelta,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.provider import ModelIdentityPolicy
from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelTokenUsageFact,
    ProviderRetrySummaryFact,
)


@dataclass(slots=True)
class RawProviderItemBuilder:
    text_block_id: str | None = None
    thinking_block_id: str | None = None
    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    active_tool_call_ids: dict[str, None] = field(default_factory=dict)
    item_id_to_tool_call_id: dict[str, str] = field(default_factory=dict)
    tool_call_names: dict[str, str] = field(default_factory=dict)
    tool_call_argument_parts: dict[str, list[str]] = field(default_factory=dict)
    has_semantic_output: bool = False

    def run_error(
        self,
        *,
        message: str,
        code: str,
        retry_summary: ProviderRetrySummaryFact | None = None,
    ) -> RawProviderFailure:
        self.has_semantic_output = True
        return RawProviderFailure(
            message=message,
            code_hint=code,
            retry_summary=retry_summary,
        )

    def text_delta(self, delta: str) -> list[RawProviderStreamItem]:
        if not delta:
            return []
        self.has_semantic_output = True
        events: list[RawProviderStreamItem] = []
        if self.text_block_id is None:
            self.text_block_id = f"text:{uuid4()}"
            events.append(
                RawProviderBlockStart(
                    block_kind="text", block_id=self.text_block_id
                )
            )
        events.append(
            RawProviderTextDelta(block_id=self.text_block_id, delta=delta)
        )
        self.text_parts.append(delta)
        return events

    def thinking_delta(self, delta: str) -> list[RawProviderStreamItem]:
        if not delta:
            return []
        self.has_semantic_output = True
        events: list[RawProviderStreamItem] = []
        if self.thinking_block_id is None:
            self.thinking_block_id = f"thinking:{uuid4()}"
            events.append(
                RawProviderBlockStart(
                    block_kind="thinking",
                    block_id=self.thinking_block_id,
                )
            )
        events.append(
            RawProviderThinkingDelta(block_id=self.thinking_block_id, delta=delta)
        )
        self.thinking_parts.append(delta)
        return events

    def text_end(self, *, final_text: str | None = None) -> list[RawProviderStreamItem]:
        events: list[RawProviderStreamItem] = []
        if final_text is not None:
            if self.text_block_id is None and final_text:
                events.extend(self.text_delta(final_text))
            elif self.text_block_id is not None and "".join(self.text_parts) != final_text:
                raise LLMTransportContractError(
                    "provider text done payload differs from its delta prefix",
                    reason_code="transport_text_done_content_mismatch",
                )
        if self.text_block_id is None:
            return events
        block_id = self.text_block_id
        self.text_block_id = None
        self.text_parts.clear()
        events.append(RawProviderBlockEnd(block_kind="text", block_id=block_id))
        return events

    def thinking_end(
        self, *, final_text: str | None = None
    ) -> list[RawProviderStreamItem]:
        events: list[RawProviderStreamItem] = []
        if final_text is not None:
            if self.thinking_block_id is None and final_text:
                events.extend(self.thinking_delta(final_text))
            elif (
                self.thinking_block_id is not None
                and "".join(self.thinking_parts) != final_text
            ):
                raise LLMTransportContractError(
                    "provider thinking done payload differs from its delta prefix",
                    reason_code="transport_thinking_done_content_mismatch",
                )
        if self.thinking_block_id is None:
            return events
        block_id = self.thinking_block_id
        self.thinking_block_id = None
        self.thinking_parts.clear()
        events.append(RawProviderBlockEnd(block_kind="thinking", block_id=block_id))
        return events

    def tool_call_start(
        self,
        *,
        tool_call_id: str,
        tool_call_name: str,
        provider_item_id: str | None = None,
    ) -> list[RawProviderStreamItem]:
        if not tool_call_id:
            raise LLMTransportContractError(
                "tool-call start requires a stable ID",
                reason_code="transport_tool_call_identity_missing",
            )
        existing_call_id = (
            self.item_id_to_tool_call_id.get(provider_item_id)
            if provider_item_id
            else None
        )
        if existing_call_id is not None and existing_call_id != tool_call_id:
            raise LLMTransportContractError(
                "provider item changed its frozen tool-call identity",
                reason_code="transport_tool_call_identity_mismatch",
            )
        existing_name = self.tool_call_names.get(tool_call_id)
        if existing_name is not None:
            if tool_call_name and existing_name != tool_call_name:
                raise LLMTransportContractError(
                    "provider changed the frozen tool-call name",
                    reason_code="transport_tool_call_name_mismatch",
                )
            if tool_call_id in self.active_tool_call_ids:
                if provider_item_id:
                    self.item_id_to_tool_call_id[provider_item_id] = tool_call_id
                return []
            raise LLMTransportContractError(
                "provider restarted an already closed tool call",
                reason_code="transport_tool_call_restarted",
            )
        if not tool_call_name:
            raise LLMTransportContractError(
                "tool-call start requires a non-empty name",
                reason_code="transport_tool_call_identity_missing",
            )
        if provider_item_id:
            self.item_id_to_tool_call_id[provider_item_id] = tool_call_id
        self.has_semantic_output = True
        self.active_tool_call_ids[tool_call_id] = None
        self.tool_call_names[tool_call_id] = tool_call_name
        self.tool_call_argument_parts.setdefault(tool_call_id, [])
        return [
            RawProviderBlockStart(
                block_kind="tool_call",
                block_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
        ]

    def tool_call_delta(
        self, *, tool_call_id: str, delta: str
    ) -> list[RawProviderStreamItem]:
        if not tool_call_id or not delta:
            return []
        self.has_semantic_output = True
        events: list[RawProviderStreamItem] = []
        if tool_call_id not in self.active_tool_call_ids:
            raise LLMTransportContractError(
                "tool-call arguments arrived before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        self.tool_call_argument_parts.setdefault(tool_call_id, []).append(delta)
        events.append(
            RawProviderToolCallDelta(tool_call_id=tool_call_id, delta=delta)
        )
        return events

    def reconcile_tool_call_arguments(
        self,
        *,
        tool_call_id: str,
        final_arguments: str,
    ) -> list[RawProviderStreamItem]:
        if tool_call_id not in self.active_tool_call_ids:
            raise LLMTransportContractError(
                "tool-call final arguments arrived outside a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        parts = self.tool_call_argument_parts.setdefault(tool_call_id, [])
        accumulated = "".join(parts)
        if not parts and final_arguments:
            return self.tool_call_delta(
                tool_call_id=tool_call_id,
                delta=final_arguments,
            )
        if accumulated != final_arguments:
            raise LLMTransportContractError(
                "provider tool-call final arguments differ from their delta prefix",
                reason_code="transport_tool_arguments_done_content_mismatch",
            )
        return []

    def tool_call_end(self, *, tool_call_id: str) -> list[RawProviderStreamItem]:
        if not tool_call_id or tool_call_id not in self.active_tool_call_ids:
            return []
        self.has_semantic_output = True
        self.active_tool_call_ids.pop(tool_call_id)
        return [
            RawProviderBlockEnd(block_kind="tool_call", block_id=tool_call_id)
        ]

    def tool_call(
        self, *, tool_call_id: str, tool_call_name: str, arguments: str
    ) -> list[RawProviderStreamItem]:
        events: list[RawProviderStreamItem] = []
        events.extend(
            self.tool_call_start(
                tool_call_id=tool_call_id, tool_call_name=tool_call_name
            )
        )
        if arguments:
            events.extend(
                self.tool_call_delta(tool_call_id=tool_call_id, delta=arguments)
            )
        events.extend(self.tool_call_end(tool_call_id=tool_call_id))
        return events

    def resolve_tool_call_id(self, item_id_or_call_id: str) -> str:
        if not item_id_or_call_id:
            raise LLMTransportContractError(
                "tool-call arguments arrived before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        resolved = self.item_id_to_tool_call_id.get(
            item_id_or_call_id, item_id_or_call_id
        )
        if resolved not in self.active_tool_call_ids:
            raise LLMTransportContractError(
                "tool-call arguments arrived before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        return resolved

    def resolve_completed_tool_call_id(
        self,
        *,
        provider_item_id: str,
        tool_call_id: str,
    ) -> str:
        mapped = (
            self.item_id_to_tool_call_id.get(provider_item_id)
            if provider_item_id
            else None
        )
        if mapped is not None:
            if tool_call_id and tool_call_id != mapped:
                raise LLMTransportContractError(
                    "provider final item changed its frozen tool-call identity",
                    reason_code="transport_tool_call_identity_mismatch",
                )
            return mapped
        if tool_call_id:
            return tool_call_id
        if provider_item_id:
            return provider_item_id
        raise LLMTransportContractError(
            "provider final tool-call item lacks a stable identity",
            reason_code="transport_tool_call_identity_missing",
        )

    def close_active_blocks(self) -> list[RawProviderStreamItem]:
        events: list[RawProviderStreamItem] = []
        events.extend(self.text_end())
        events.extend(self.thinking_end())
        for tool_call_id in tuple(self.active_tool_call_ids):
            events.append(
                RawProviderBlockEnd(
                    block_kind="tool_call", block_id=tool_call_id
                )
            )
            self.active_tool_call_ids.pop(tool_call_id)
        return events


def sdk_event_to_dict(raw_event: Any) -> dict[str, Any]:
    """Normalize SDK model objects and test dictionaries into plain dicts."""

    if isinstance(raw_event, dict):
        return raw_event
    model_dump = getattr(raw_event, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    if hasattr(raw_event, "__dict__"):
        return {
            key: value
            for key, value in vars(raw_event).items()
            if not key.startswith("_")
        }
    return {"value": raw_event}


@dataclass(slots=True)
class ReportedModelIdentityObserver:
    """Observe one provider attempt without confusing aliases with fallback."""

    requested_model_id: str
    policy: ModelIdentityPolicy
    reported_model_id: str | None = None

    def observe(self, value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        reported = value.strip()
        if (
            self.policy is ModelIdentityPolicy.EXACT
            and reported != self.requested_model_id
        ):
            raise LLMTransportContractError(
                "transport_changed_model_target: provider reported "
                f"{reported!r}, expected exact identity {self.requested_model_id!r}",
                reason_code="transport_changed_model_target",
            )
        if self.reported_model_id is not None and self.reported_model_id != reported:
            raise LLMTransportContractError(
                "transport_changed_model_target: provider model identity changed within stream",
                reason_code="transport_changed_model_target",
            )
        self.reported_model_id = reported


def responses_reported_model(raw_event: Any) -> object:
    event = sdk_event_to_dict(raw_event)
    response = event.get("response")
    if isinstance(response, dict) and response.get("model") is not None:
        return response.get("model")
    return event.get("model")


def chat_completion_reported_model(raw_chunk: Any) -> object:
    return sdk_event_to_dict(raw_chunk).get("model")


def arguments_to_json_string(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        return raw_arguments
    if isinstance(raw_arguments, dict):
        return json.dumps(raw_arguments)
    return "{}"


def transport_usage_report_from_mapping(raw_usage: Any) -> TransportUsageReport:
    usage = sdk_event_to_dict(raw_usage) if raw_usage is not None else {}
    if not usage:
        return TransportUsageReport(usage_status="missing", usage=None)
    input_raw = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_raw = usage.get("output_tokens", usage.get("completion_tokens"))
    if input_raw is None or output_raw is None:
        return TransportUsageReport(
            usage_status="missing",
            usage=None,
            provider_diagnostics=(
                ModelCallDiagnosticFact(code="provider_usage_incomplete"),
            ),
        )
    input_tokens = int(input_raw)
    output_tokens = int(output_raw)
    normalized_total = input_tokens + output_tokens
    diagnostics: list[ModelCallDiagnosticFact] = []
    provider_total = usage.get("total_tokens")
    if provider_total is not None and int(provider_total) != normalized_total:
        diagnostics.append(
            ModelCallDiagnosticFact(
                code="provider_usage_total_mismatch",
                attributes=(
                    ("normalized_total", normalized_total),
                    ("provider_total", int(provider_total)),
                ),
            )
        )
    input_details = usage.get(
        "input_tokens_details", usage.get("prompt_tokens_details")
    )
    output_details = usage.get(
        "output_tokens_details", usage.get("completion_tokens_details")
    )
    cached = (
        input_details.get("cached_tokens")
        if isinstance(input_details, dict)
        and input_details.get("cached_tokens") is not None
        else None
    )
    reasoning = (
        output_details.get("reasoning_tokens")
        if isinstance(output_details, dict)
        and output_details.get("reasoning_tokens") is not None
        else None
    )
    fact = ModelTokenUsageFact(
        input_tokens=input_tokens,
        cached_input_tokens=int(cached) if cached is not None else None,
        output_tokens=output_tokens,
        reasoning_output_tokens=int(reasoning) if reasoning is not None else None,
        total_tokens=normalized_total,
    )
    return TransportUsageReport(
        usage_status="reported",
        usage=fact,
        provider_diagnostics=tuple(diagnostics),
    )


def event_includes_run_error(events: list[RawProviderStreamItem]) -> bool:
    return any(isinstance(event, RawProviderFailure) for event in events)
