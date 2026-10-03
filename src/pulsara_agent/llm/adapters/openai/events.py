"""Shared OpenAI event translation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pulsara_agent.ports.live_agent_event import (
    ProviderStreamPayload,
    ReasoningPresentationKind,
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.ports.provider_stream import ProviderStreamFailure
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.provider import ModelIdentityPolicy
from pulsara_agent.llm.stream_limits import (
    MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES,
    MAX_PROVIDER_DECODED_JSON_DEPTH,
    MAX_PROVIDER_DECODED_JSON_NODES,
    MAX_PROVIDER_DECODED_JSON_STRING_BYTES,
)
from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelTokenUsageFact,
    ProviderRetrySummaryFact,
)


@dataclass(slots=True)
class ProviderLiveItemBuilder:
    text_block_id: str | None = None
    thinking_block_id: str | None = None
    thinking_presentation_kind: ReasoningPresentationKind | None = None
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
    ) -> ProviderStreamFailure:
        self.has_semantic_output = True
        return ProviderStreamFailure(
            message=message,
            code_hint=code,
            retry_summary=retry_summary,
        )

    def text_delta(self, delta: str) -> list[ProviderStreamPayload]:
        if not delta:
            return []
        self.has_semantic_output = True
        events: list[ProviderStreamPayload] = []
        if self.text_block_id is None:
            self.text_block_id = f"text:{uuid4()}"
            events.append(TextStartPayload(block_identity=self.text_block_id))
        events.append(TextDeltaPayload(block_identity=self.text_block_id, delta=delta))
        self.text_parts.append(delta)
        return events

    def thinking_delta(
        self,
        delta: str,
        *,
        presentation_kind: ReasoningPresentationKind = ReasoningPresentationKind.FULL,
    ) -> list[ProviderStreamPayload]:
        if not delta:
            return []
        self.has_semantic_output = True
        events: list[ProviderStreamPayload] = []
        if self.thinking_block_id is None:
            self.thinking_block_id = f"thinking:{uuid4()}"
            self.thinking_presentation_kind = presentation_kind
            events.append(
                ThinkingStartPayload(
                    block_identity=self.thinking_block_id,
                    presentation_kind=presentation_kind,
                )
            )
        elif self.thinking_presentation_kind is not presentation_kind:
            raise LLMTransportContractError(
                "provider changed the presentation kind of an open reasoning block",
                reason_code="transport_thinking_presentation_kind_mismatch",
            )
        events.append(
            ThinkingDeltaPayload(block_identity=self.thinking_block_id, delta=delta)
        )
        self.thinking_parts.append(delta)
        return events

    def text_end(self, *, final_text: str | None = None) -> list[ProviderStreamPayload]:
        events: list[ProviderStreamPayload] = []
        if final_text is not None:
            if self.text_block_id is None and final_text:
                events.extend(self.text_delta(final_text))
            elif (
                self.text_block_id is not None
                and "".join(self.text_parts) != final_text
            ):
                raise LLMTransportContractError(
                    "provider text done payload differs from its delta prefix",
                    reason_code="transport_text_done_content_mismatch",
                )
        if self.text_block_id is None:
            return events
        block_id = self.text_block_id
        completed = "".join(self.text_parts)
        self.text_block_id = None
        self.text_parts.clear()
        events.append(
            TextEndPayload(
                block_identity=block_id,
                final_text=completed,
                utf8_bytes=len(completed.encode("utf-8")),
                digest=live_digest(completed),
            )
        )
        return events

    def thinking_end(
        self,
        *,
        final_text: str | None = None,
        presentation_kind: ReasoningPresentationKind | None = None,
    ) -> list[ProviderStreamPayload]:
        events: list[ProviderStreamPayload] = []
        if final_text is not None:
            if self.thinking_block_id is None and final_text:
                events.extend(
                    self.thinking_delta(
                        final_text,
                        presentation_kind=(
                            presentation_kind or ReasoningPresentationKind.FULL
                        ),
                    )
                )
            elif (
                self.thinking_block_id is not None
                and "".join(self.thinking_parts) != final_text
            ):
                raise LLMTransportContractError(
                    "provider thinking done payload differs from its delta prefix",
                    reason_code="transport_thinking_done_content_mismatch",
                )
        if (
            self.thinking_block_id is not None
            and presentation_kind is not None
            and self.thinking_presentation_kind is not presentation_kind
        ):
            raise LLMTransportContractError(
                "provider changed the presentation kind of an open reasoning block",
                reason_code="transport_thinking_presentation_kind_mismatch",
            )
        if self.thinking_block_id is None:
            return events
        block_id = self.thinking_block_id
        completed = "".join(self.thinking_parts)
        self.thinking_block_id = None
        self.thinking_presentation_kind = None
        self.thinking_parts.clear()
        events.append(
            ThinkingEndPayload(
                block_identity=block_id,
                final_text=completed,
                utf8_bytes=len(completed.encode("utf-8")),
                digest=live_digest(completed),
            )
        )
        return events

    def tool_call_start(
        self,
        *,
        tool_call_id: str,
        tool_call_name: str,
        provider_item_id: str | None = None,
    ) -> list[ProviderStreamPayload]:
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
            ToolCallStartPayload(
                block_identity=tool_call_id,
                tool_call_id=tool_call_id,
                tool_name=tool_call_name,
            )
        ]

    def tool_call_delta(
        self, *, tool_call_id: str, delta: str
    ) -> list[ProviderStreamPayload]:
        if not tool_call_id or not delta:
            return []
        self.has_semantic_output = True
        events: list[ProviderStreamPayload] = []
        if tool_call_id not in self.active_tool_call_ids:
            raise LLMTransportContractError(
                "tool-call arguments arrived before a named tool-call start",
                reason_code="transport_tool_call_start_missing",
            )
        self.tool_call_argument_parts.setdefault(tool_call_id, []).append(delta)
        events.append(
            ToolCallDeltaPayload(
                block_identity=tool_call_id,
                tool_call_id=tool_call_id,
                delta=delta,
            )
        )
        return events

    def reconcile_tool_call_arguments(
        self,
        *,
        tool_call_id: str,
        final_arguments: str,
    ) -> list[ProviderStreamPayload]:
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

    def tool_call_end(self, *, tool_call_id: str) -> list[ProviderStreamPayload]:
        if not tool_call_id or tool_call_id not in self.active_tool_call_ids:
            return []
        self.has_semantic_output = True
        events: list[ProviderStreamPayload] = []
        parts = self.tool_call_argument_parts.get(tool_call_id, ())
        if not parts:
            events.extend(self.tool_call_delta(tool_call_id=tool_call_id, delta="{}"))
        self.active_tool_call_ids.pop(tool_call_id)
        arguments = "".join(self.tool_call_argument_parts[tool_call_id])
        tool_name = self.tool_call_names[tool_call_id]
        events.append(
            ToolCallEndPayload(
                block_identity=tool_call_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_json=arguments,
                utf8_bytes=len(arguments.encode("utf-8")),
                digest=live_digest(arguments),
            )
        )
        return events

    def tool_call(
        self, *, tool_call_id: str, tool_call_name: str, arguments: str
    ) -> list[ProviderStreamPayload]:
        events: list[ProviderStreamPayload] = []
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

    def close_active_blocks(self) -> list[ProviderStreamPayload]:
        events: list[ProviderStreamPayload] = []
        events.extend(self.text_end())
        events.extend(self.thinking_end())
        for tool_call_id in tuple(self.active_tool_call_ids):
            events.extend(self.tool_call_end(tool_call_id=tool_call_id))
        return events


def sdk_event_to_dict(raw_event: Any) -> dict[str, Any]:
    """Normalize SDK model objects and test dictionaries into plain dicts."""

    if isinstance(raw_event, dict):
        result = raw_event
    else:
        model_dump = getattr(raw_event, "model_dump", None)
        if callable(model_dump):
            # OpenAI's Pydantic DTOs expose optional, absent fields as default
            # ``None`` values from a plain model_dump().  Treating those SDK
            # defaults as wire-presence both defeats the closed field
            # validators and loses the absence-vs-explicit-null distinction
            # needed by exact replay.  ``exclude_unset`` preserves fields that
            # were actually decoded (including an explicit null) while
            # omitting SDK-only defaults that were never present on the wire.
            result = model_dump(mode="python", exclude_unset=True)
        elif hasattr(raw_event, "__dict__"):
            result = {
                key: value
                for key, value in vars(raw_event).items()
                if not key.startswith("_")
            }
        else:
            result = {"value": raw_event}
    if not isinstance(result, dict):
        raise LLMTransportContractError(
            "provider SDK event is not an object",
            reason_code="transport_provider_json_shape_invalid",
        )
    _validate_decoded_provider_json(result)
    return result


def _validate_decoded_provider_json(value: object) -> None:
    """Bound an SDK-decoded JSON graph before any immutable deep copy.

    The SDK has already parsed its wire frame, so this is deliberately a
    post-parse physical fence.  It prevents opaque reasoning/output fields
    from bypassing the same completed-response working-set contract used by
    the public stream.
    """

    nodes = 0
    string_bytes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_PROVIDER_DECODED_JSON_DEPTH:
            raise LLMTransportContractError(
                "provider SDK event exceeds the JSON depth bound",
                reason_code="transport_provider_json_shape_exceeded",
            )
        nodes += 1
        if nodes > MAX_PROVIDER_DECODED_JSON_NODES:
            raise LLMTransportContractError(
                "provider SDK event exceeds the JSON node bound",
                reason_code="transport_provider_json_shape_exceeded",
            )
        if isinstance(current, str):
            string_bytes += len(current.encode("utf-8"))
            if string_bytes > MAX_PROVIDER_DECODED_JSON_STRING_BYTES:
                raise LLMTransportContractError(
                    "provider SDK event exceeds the JSON string-byte bound",
                    reason_code="transport_provider_json_shape_exceeded",
                )
            continue
        if current is None or isinstance(current, bool | int | float):
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise LLMTransportContractError(
                        "provider SDK event contains a non-text JSON key",
                        reason_code="transport_provider_json_shape_invalid",
                    )
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((item, depth + 1) for item in current)
            continue
        raise LLMTransportContractError(
            "provider SDK event contains a non-JSON value",
            reason_code="transport_provider_json_shape_invalid",
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LLMTransportContractError(
            "provider SDK event is not strict JSON",
            reason_code="transport_provider_json_shape_invalid",
        ) from exc
    if len(encoded) > MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES:
        raise LLMTransportContractError(
            "provider SDK event exceeds the completed-response byte bound",
            reason_code="transport_source_payload_limit_exceeded",
        )


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
        return json.dumps(
            raw_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    raise LLMTransportContractError(
        "provider function-call arguments are absent or invalid",
        reason_code="transport_tool_arguments_invalid",
    )


def transport_usage_report_from_mapping(
    raw_usage: Any, *, wire_api: str | None = None,
) -> TransportUsageReport:
    if raw_usage is None:
        return TransportUsageReport(usage_status="missing", usage=None)
    if hasattr(raw_usage, "model_dump"):
        raw_usage = raw_usage.model_dump(mode="python", exclude_unset=True)
    if not isinstance(raw_usage, dict):
        return TransportUsageReport(
            usage_status="invalid", usage=None,
            provider_diagnostics=(ModelCallDiagnosticFact(code="provider_usage_invalid"),),
        )
    if not raw_usage:
        return TransportUsageReport(usage_status="missing", usage=None)
    usage = raw_usage
    diagnostics: list[ModelCallDiagnosticFact] = []

    def count(value: Any, name: str) -> int | None:
        # PostgreSQL bigint is the single-response storage representation.
        if value is None:
            return None
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            diagnostics.append(ModelCallDiagnosticFact(
                code="provider_usage_field_invalid", attributes=(("field", name),),
            ))
            return None
        return value

    def aliased(primary: str, alias: str) -> int | None:
        first = count(usage.get(primary), primary)
        second = count(usage.get(alias), alias)
        if first is not None and second is not None and first != second:
            diagnostics.append(ModelCallDiagnosticFact(
                code="provider_usage_alias_mismatch", attributes=(("field", primary),),
            ))
        return first if primary in usage else second

    chat = wire_api == "openai_chat_completions"
    input_tokens = aliased("prompt_tokens", "input_tokens") if chat else aliased("input_tokens", "prompt_tokens")
    output_tokens = aliased("completion_tokens", "output_tokens") if chat else aliased("output_tokens", "completion_tokens")
    total = count(usage.get("total_tokens"), "total_tokens")
    input_details = usage.get("prompt_tokens_details" if chat else "input_tokens_details")
    if input_details is None:
        input_details = usage.get("input_tokens_details" if chat else "prompt_tokens_details")
    output_details = usage.get("completion_tokens_details" if chat else "output_tokens_details")
    if output_details is None:
        output_details = usage.get("output_tokens_details" if chat else "completion_tokens_details")
    cached = count(input_details.get("cached_tokens") if isinstance(input_details, dict) else None, "cached_tokens")
    hit = count(usage.get("prompt_cache_hit_tokens"), "prompt_cache_hit_tokens")
    miss = count(usage.get("prompt_cache_miss_tokens"), "prompt_cache_miss_tokens")
    if cached is not None and hit is not None and cached != hit:
        diagnostics.append(ModelCallDiagnosticFact(code="provider_cached_input_tokens_mismatch"))
    cached = cached if cached is not None else hit
    if input_tokens is not None and hit is not None and miss is not None and hit + miss != input_tokens:
        diagnostics.append(ModelCallDiagnosticFact(code="provider_prompt_cache_partition_mismatch"))
    reasoning = count(output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None, "reasoning_tokens")
    if cached is not None and input_tokens is not None and cached > input_tokens:
        diagnostics.append(ModelCallDiagnosticFact(code="provider_cached_input_tokens_invalid"))
        cached = None
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        diagnostics.append(ModelCallDiagnosticFact(code="provider_reasoning_output_tokens_invalid"))
        reasoning = None
    if total is not None and input_tokens is not None and output_tokens is not None and total != input_tokens + output_tokens:
        diagnostics.append(ModelCallDiagnosticFact(
            code="provider_usage_total_mismatch",
            attributes=(("computed_total", input_tokens + output_tokens), ("provider_total", total)),
        ))
    if all(v is None for v in (input_tokens, output_tokens, total, cached, reasoning)):
        return TransportUsageReport(usage_status="invalid", usage=None, provider_diagnostics=tuple(diagnostics))
    fact = ModelTokenUsageFact(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached, reasoning_output_tokens=reasoning,
        reported_total_tokens=total,
    )
    status = "reported" if input_tokens is not None and output_tokens is not None else "partial"
    return TransportUsageReport(usage_status=status, usage=fact, provider_diagnostics=tuple(diagnostics))


def merge_response_usage_reports(
    previous: TransportUsageReport | None, current: TransportUsageReport,
) -> TransportUsageReport:
    """Merge cumulative carriers of one response, never retry attempts/deltas."""
    if previous is None:
        return current
    diagnostics = list(dict.fromkeys((*previous.provider_diagnostics, *current.provider_diagnostics)))
    if previous.usage is None:
        return TransportUsageReport(
            usage_status=current.usage_status, usage=current.usage,
            provider_diagnostics=tuple(diagnostics), reported_model_id=current.reported_model_id or previous.reported_model_id,
        )
    values = previous.usage.model_dump()
    if current.usage is not None:
        for name, value in current.usage.model_dump().items():
            if value is not None:
                if values[name] is not None and values[name] != value:
                    diagnostics.append(ModelCallDiagnosticFact(code="provider_usage_carrier_changed", attributes=(("field", name),)))
                values[name] = value
    for detail, parent in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if values[detail] is not None and values[parent] is not None and values[detail] > values[parent]:
            values[detail] = None
            diagnostics.append(ModelCallDiagnosticFact(code="provider_usage_breakdown_invalid", attributes=(("field", detail),)))
    fact = ModelTokenUsageFact(**values)
    if fact.reported_total_tokens is not None and fact.computed_total_tokens is not None and fact.reported_total_tokens != fact.computed_total_tokens:
        diagnostics.append(ModelCallDiagnosticFact(code="provider_usage_total_mismatch"))
    return TransportUsageReport(
        usage_status="reported" if fact.input_tokens is not None and fact.output_tokens is not None else "partial",
        usage=fact, provider_diagnostics=tuple(dict.fromkeys(diagnostics)),
        reported_model_id=current.reported_model_id or previous.reported_model_id,
    )


def event_includes_run_error(events: list[object]) -> bool:
    return any(isinstance(event, ProviderStreamFailure) for event in events)
