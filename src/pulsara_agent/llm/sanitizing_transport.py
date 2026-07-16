"""Secret-safe adapter boundary for provider model streams."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, AsyncIterator, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pulsara_agent.event import (
    AgentEvent,
    CustomEvent,
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventContext,
    RunErrorEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.llm.drafts import (
    ProviderDataBlockDeltaDraft,
    ProviderDataBlockEndDraft,
    ProviderDataBlockStartDraft,
    ProviderErrorDraft,
    ProviderTextBlockDeltaDraft,
    ProviderTextBlockEndDraft,
    ProviderTextBlockStartDraft,
    ProviderThinkingBlockDeltaDraft,
    ProviderThinkingBlockEndDraft,
    ProviderThinkingBlockStartDraft,
    ProviderToolCallDeltaDraft,
    ProviderToolCallEndDraft,
    ProviderToolCallStartDraft,
    ProviderTransportStreamItem,
    build_semantic_draft,
    build_terminal_draft,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.primitives.model_call import (
    ProviderErrorSanitizationContractFact,
    ProviderModelStreamErrorCode,
    ProviderSanitizedErrorFact,
    sha256_fingerprint,
)
from pulsara_agent.primitives.authority_materialization import (
    MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL,
    MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL,
)
from pulsara_agent.primitives.context import canonical_json_bytes


class ProviderTransportPhysicalCompletionStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED_UNTRUSTED = "blocked_untrusted"


@dataclass(frozen=True, slots=True)
class ProviderTransportPhysicalCompletion:
    status: ProviderTransportPhysicalCompletionStatus
    diagnostic_code: str | None
    completion_fingerprint: str


class SanitizingProviderTransportState(StrEnum):
    OPEN = "open"
    ERROR_DRAFT_PENDING = "error_draft_pending"
    ERROR_DRAFT_DELIVERED = "error_draft_delivered"
    FAILURE_PHYSICAL_DRAIN = "failure_physical_drain"
    ERROR_TERMINAL_PENDING = "error_terminal_pending"
    TERMINAL_DELIVERED = "terminal_delivered"
    CANCELLING = "cancelling"
    PHYSICAL_DRAIN_BLOCKED = "physical_drain_blocked"
    CLOSED = "closed"


class RawLLMTransport:
    """Structural marker for adapter-private legacy event producers."""

    api: str
    binding_id: str
    contract_version: str

    def stream(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent | TransportUsageReport]:
        raise NotImplementedError


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_SECRET_RE = re.compile(
    r"(?i)(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:authorization|proxy[-_ ]?authorization|api[-_ ]?key|x[-_ ]?api[-_ ]?key|"
    r"access[-_ ]?token|refresh[-_ ]?token|password|passwd|secret|cookie|set[-_ ]?cookie)"
    r"\s*[:=]\s*[^\s,;}]+"
)


def _contract() -> ProviderErrorSanitizationContractFact:
    payload = {
        "contract_id": "pulsara.provider-error-sanitizer",
        "contract_version": "v1",
        "stable_code_mapping_fingerprint": sha256_fingerprint(
            "provider-error-code-map:v1", tuple(item.value for item in ProviderModelStreamErrorCode)
        ),
        "sensitive_key_policy_fingerprint": sha256_fingerprint(
            "provider-error-sensitive-keys:v1",
            (
                "authorization",
                "proxyauthorization",
                "apikey",
                "xapikey",
                "accesstoken",
                "refreshtoken",
                "password",
                "passwd",
                "secret",
                "cookie",
                "setcookie",
            ),
        ),
        "secret_pattern_policy_fingerprint": sha256_fingerprint(
            "provider-error-secret-patterns:v1", _SECRET_RE.pattern
        ),
        "url_redaction_policy_fingerprint": sha256_fingerprint(
            "provider-error-url-policy:v1", "remove-userinfo-query-fragment"
        ),
        "diagnostic_attribute_allowlist_fingerprint": sha256_fingerprint(
            "provider-error-diagnostic-allowlist:v1", ()
        ),
        "max_message_chars": 512,
        "max_diagnostic_count": 8,
        "max_diagnostic_attribute_chars": 128,
    }
    provisional = ProviderErrorSanitizationContractFact.model_construct(
        **payload, contract_fingerprint="pending"
    )
    canonical = provisional.model_dump(mode="json", exclude={"contract_fingerprint"})
    return ProviderErrorSanitizationContractFact(
        **canonical,
        contract_fingerprint=sha256_fingerprint(
            "provider-error-sanitization-contract:v1", canonical
        ),
    )


DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT = _contract()


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:)":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[redacted-url]" + trailing
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        SplitResult(parsed.scheme, host, parsed.path, "", "")
    ) + trailing


def sanitize_provider_failure(
    *, message: object, code_hint: str | None = None
) -> ProviderSanitizedErrorFact:
    """Map one raw provider failure to a bounded event-safe fact."""

    try:
        raw = str(message)
        text = _URL_RE.sub(_redact_url, raw)
        text = _SECRET_RE.sub("[redacted]", text)
        truncated = len(text) > DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.max_message_chars
        text = text[: DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.max_message_chars]
        hint = (code_hint or "").casefold()
        if hint == "transport_source_item_limit_exceeded":
            stable_code = (
                ProviderModelStreamErrorCode.TRANSPORT_SOURCE_ITEM_LIMIT_EXCEEDED
            )
        elif hint == "transport_source_payload_limit_exceeded":
            stable_code = (
                ProviderModelStreamErrorCode.TRANSPORT_SOURCE_PAYLOAD_LIMIT_EXCEEDED
            )
        elif "auth" in hint or "401" in hint:
            stable_code = ProviderModelStreamErrorCode.AUTHENTICATION_FAILED
        elif "permission" in hint or "403" in hint:
            stable_code = ProviderModelStreamErrorCode.PERMISSION_DENIED
        elif "rate" in hint or "429" in hint:
            stable_code = ProviderModelStreamErrorCode.RATE_LIMITED
        elif "timeout" in hint:
            stable_code = ProviderModelStreamErrorCode.PROVIDER_TIMEOUT
        elif "overload" in hint:
            stable_code = ProviderModelStreamErrorCode.PROVIDER_OVERLOADED
        elif "invalid" in hint or "400" in hint:
            stable_code = ProviderModelStreamErrorCode.INVALID_REQUEST
        else:
            stable_code = ProviderModelStreamErrorCode.UNKNOWN_PROVIDER_ERROR
        payload = {
            "code": stable_code,
            "message": text or "Provider model stream failed.",
            "diagnostics": (),
            "redaction_count": 0,
            "truncated": truncated,
            "sanitization_contract": DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
        }
        provisional = ProviderSanitizedErrorFact.model_construct(
            **payload, error_fingerprint="pending"
        )
        canonical = provisional.model_dump(mode="json", exclude={"error_fingerprint"})
        return ProviderSanitizedErrorFact(
            **canonical,
            error_fingerprint=sha256_fingerprint(
                "provider-sanitized-error:v1", canonical
            ),
        )
    except BaseException:
        payload = {
            "code": ProviderModelStreamErrorCode.TRANSPORT_PROTOCOL_ERROR,
            "message": "Provider error sanitization failed.",
            "diagnostics": (),
            "redaction_count": 0,
            "truncated": False,
            "sanitization_contract": DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
        }
        provisional = ProviderSanitizedErrorFact.model_construct(
            **payload, error_fingerprint="pending"
        )
        canonical = provisional.model_dump(mode="json", exclude={"error_fingerprint"})
        return ProviderSanitizedErrorFact(
            **canonical,
            error_fingerprint=sha256_fingerprint(
                "provider-sanitized-error:v1", canonical
            ),
        )


def _open_semantic_id(
    semantic_id: str,
    *,
    active: set[str],
    seen: set[str],
) -> None:
    if semantic_id in seen:
        raise ValueError("duplicate semantic block start")
    seen.add(semantic_id)
    active.add(semantic_id)


def _require_active_semantic_id(semantic_id: str, active: set[str]) -> None:
    if semantic_id not in active:
        raise ValueError("semantic delta outside active block")


def _close_semantic_id(semantic_id: str, active: set[str]) -> None:
    if semantic_id not in active:
        raise ValueError("semantic end outside active block")
    active.remove(semantic_id)


class SanitizingProviderTransportExecution:
    def __init__(
        self,
        *,
        raw_stream: AsyncIterator[AgentEvent | TransportUsageReport],
        resolved_model_call_id: str,
        prefailed_error: ProviderSanitizedErrorFact | None = None,
    ) -> None:
        self._raw_stream = raw_stream
        self._resolved_model_call_id = resolved_model_call_id
        self.state = (
            SanitizingProviderTransportState.ERROR_DRAFT_PENDING
            if prefailed_error is not None
            else SanitizingProviderTransportState.OPEN
        )
        self.next_transport_sequence_index = 0
        self._accepted_source_item_count = 0
        self._accepted_source_payload_bytes = 0
        self._usage: TransportUsageReport | None = None
        self._pending_error = prefailed_error
        self._collect_usage_while_draining_error = False
        self._terminal_delivered = False
        self._physical_completed = False
        self._cancel_reason: Literal["user_stop", "host_teardown"] | None = None
        self._active_text_blocks: set[str] = set()
        self._seen_text_blocks: set[str] = set()
        self._active_thinking_blocks: set[str] = set()
        self._seen_thinking_blocks: set[str] = set()
        self._active_data_blocks: dict[str, str] = {}
        self._seen_data_blocks: set[str] = set()
        self._active_tool_calls: set[str] = set()
        self._seen_tool_calls: set[str] = set()

    async def read_next(self) -> ProviderTransportStreamItem | None:
        if self._terminal_delivered:
            return None
        if self._pending_error is not None:
            if self.state is SanitizingProviderTransportState.ERROR_DRAFT_PENDING:
                draft = build_semantic_draft(
                    ProviderErrorDraft,
                    transport_sequence_index=self.next_transport_sequence_index,
                    error=self._pending_error,
                )
                self.next_transport_sequence_index += 1
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_DELIVERED
                return draft
            await self._drain_after_failure()
            report = (
                self._usage
                if self._collect_usage_while_draining_error
                else None
            )
            terminal = build_terminal_draft(
                outcome="provider_error",
                usage=report.usage if report is not None else None,
                usage_status=(
                    report.usage_status if report is not None else "missing"
                ),
                reported_model_id=(
                    report.reported_model_id if report is not None else None
                ),
                semantic_item_count=self.next_transport_sequence_index,
            )
            self._terminal_delivered = True
            self.state = SanitizingProviderTransportState.TERMINAL_DELIVERED
            return terminal
        if self._cancel_reason is not None:
            await self.aclose()
            return None

        while True:
            try:
                item = await anext(self._raw_stream)
            except StopAsyncIteration:
                self._physical_completed = True
                if self._has_open_semantic_blocks():
                    self._pending_error = sanitize_provider_failure(
                        message="Provider stream ended with an open semantic block.",
                        code_hint="transport_protocol_error",
                    )
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                report = self._usage or TransportUsageReport(
                    usage_status="missing", usage=None
                )
                self._terminal_delivered = True
                self.state = SanitizingProviderTransportState.TERMINAL_DELIVERED
                return build_terminal_draft(
                    outcome="completed",
                    usage=report.usage,
                    usage_status=report.usage_status,
                    reported_model_id=report.reported_model_id,
                    semantic_item_count=self.next_transport_sequence_index,
                )
            except BaseException as exc:
                self._pending_error = sanitize_provider_failure(message=exc)
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                return await self.read_next()

            if isinstance(item, TransportUsageReport):
                if self._usage is not None:
                    self._pending_error = sanitize_provider_failure(
                        message="Provider emitted duplicate usage reports.",
                        code_hint="transport_protocol_error",
                    )
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                self._usage = item
                continue
            if isinstance(item, RunErrorEvent):
                self._pending_error = sanitize_provider_failure(
                    message=item.message,
                    code_hint=item.code,
                )
                # A structured adapter error may precede its terminal usage
                # report. Drain that report without accepting later semantics.
                self._collect_usage_while_draining_error = True
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                return await self.read_next()
            if isinstance(item, CustomEvent) and item.name == "llm.retry":
                continue
            try:
                self._validate_semantic_event(item)
                draft = self._draft_from_event(item, advance_cursor=False)
                payload_bytes = len(
                    canonical_json_bytes(draft.model_dump(mode="json"))
                )
                if (
                    self._accepted_source_item_count + 1
                    > MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
                ):
                    self._pending_error = sanitize_provider_failure(
                        message="Provider stream exceeded the source-item circuit breaker.",
                        code_hint="transport_source_item_limit_exceeded",
                    )
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                if (
                    self._accepted_source_payload_bytes + payload_bytes
                    > MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL
                ):
                    self._pending_error = sanitize_provider_failure(
                        message="Provider stream exceeded the sanitized-byte circuit breaker.",
                        code_hint="transport_source_payload_limit_exceeded",
                    )
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                self._accepted_source_item_count += 1
                self._accepted_source_payload_bytes += payload_bytes
                self.next_transport_sequence_index += 1
                return draft
            except BaseException:
                self._pending_error = sanitize_provider_failure(
                    message="Provider emitted an unsupported semantic event.",
                    code_hint="transport_protocol_error",
                )
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                return await self.read_next()

    def _draft_from_event(
        self,
        event: AgentEvent,
        *,
        advance_cursor: bool = True,
    ):
        index = self.next_transport_sequence_index
        mapping: tuple[tuple[type[AgentEvent], type[Any], dict[str, Any]], ...] = (
            (TextBlockStartEvent, ProviderTextBlockStartDraft, {"block_id": getattr(event, "block_id", "")}),
            (TextBlockDeltaEvent, ProviderTextBlockDeltaDraft, {"block_id": getattr(event, "block_id", ""), "delta": getattr(event, "delta", "")}),
            (TextBlockEndEvent, ProviderTextBlockEndDraft, {"block_id": getattr(event, "block_id", "")}),
            (ThinkingBlockStartEvent, ProviderThinkingBlockStartDraft, {"block_id": getattr(event, "block_id", "")}),
            (ThinkingBlockDeltaEvent, ProviderThinkingBlockDeltaDraft, {"block_id": getattr(event, "block_id", ""), "delta": getattr(event, "delta", "")}),
            (ThinkingBlockEndEvent, ProviderThinkingBlockEndDraft, {"block_id": getattr(event, "block_id", "")}),
            (DataBlockStartEvent, ProviderDataBlockStartDraft, {"block_id": getattr(event, "block_id", ""), "media_type": getattr(event, "media_type", "")}),
            (DataBlockDeltaEvent, ProviderDataBlockDeltaDraft, {"block_id": getattr(event, "block_id", ""), "media_type": getattr(event, "media_type", ""), "data": getattr(event, "data", "")}),
            (DataBlockEndEvent, ProviderDataBlockEndDraft, {"block_id": getattr(event, "block_id", "")}),
            (ToolCallStartEvent, ProviderToolCallStartDraft, {"tool_call_id": getattr(event, "tool_call_id", ""), "tool_call_name": getattr(event, "tool_call_name", "")}),
            (ToolCallDeltaEvent, ProviderToolCallDeltaDraft, {"tool_call_id": getattr(event, "tool_call_id", ""), "delta": getattr(event, "delta", "")}),
            (ToolCallEndEvent, ProviderToolCallEndDraft, {"tool_call_id": getattr(event, "tool_call_id", "")}),
        )
        for event_type, draft_type, payload in mapping:
            if isinstance(event, event_type):
                draft = build_semantic_draft(
                    draft_type,
                    transport_sequence_index=index,
                    **payload,
                )
                if advance_cursor:
                    self.next_transport_sequence_index += 1
                return draft
        raise TypeError(type(event).__name__)

    def _validate_semantic_event(self, event: AgentEvent) -> None:
        if isinstance(event, TextBlockStartEvent):
            _open_semantic_id(
                event.block_id,
                active=self._active_text_blocks,
                seen=self._seen_text_blocks,
            )
        elif isinstance(event, TextBlockDeltaEvent):
            _require_active_semantic_id(event.block_id, self._active_text_blocks)
        elif isinstance(event, TextBlockEndEvent):
            _close_semantic_id(event.block_id, self._active_text_blocks)
        elif isinstance(event, ThinkingBlockStartEvent):
            _open_semantic_id(
                event.block_id,
                active=self._active_thinking_blocks,
                seen=self._seen_thinking_blocks,
            )
        elif isinstance(event, ThinkingBlockDeltaEvent):
            _require_active_semantic_id(event.block_id, self._active_thinking_blocks)
        elif isinstance(event, ThinkingBlockEndEvent):
            _close_semantic_id(event.block_id, self._active_thinking_blocks)
        elif isinstance(event, DataBlockStartEvent):
            if event.block_id in self._seen_data_blocks:
                raise ValueError("duplicate data block start")
            self._seen_data_blocks.add(event.block_id)
            self._active_data_blocks[event.block_id] = event.media_type
        elif isinstance(event, DataBlockDeltaEvent):
            if self._active_data_blocks.get(event.block_id) != event.media_type:
                raise ValueError("data block delta identity mismatch")
        elif isinstance(event, DataBlockEndEvent):
            if self._active_data_blocks.pop(event.block_id, None) is None:
                raise ValueError("data block end outside active block")
        elif isinstance(event, ToolCallStartEvent):
            _open_semantic_id(
                event.tool_call_id,
                active=self._active_tool_calls,
                seen=self._seen_tool_calls,
            )
        elif isinstance(event, ToolCallDeltaEvent):
            _require_active_semantic_id(event.tool_call_id, self._active_tool_calls)
        elif isinstance(event, ToolCallEndEvent):
            _close_semantic_id(event.tool_call_id, self._active_tool_calls)

    def _has_open_semantic_blocks(self) -> bool:
        return bool(
            self._active_text_blocks
            or self._active_thinking_blocks
            or self._active_data_blocks
            or self._active_tool_calls
        )

    async def _drain_after_failure(self) -> None:
        self.state = SanitizingProviderTransportState.FAILURE_PHYSICAL_DRAIN
        if self._collect_usage_while_draining_error:
            try:
                while True:
                    item = await anext(self._raw_stream)
                    if isinstance(item, TransportUsageReport) and self._usage is None:
                        self._usage = item
            except StopAsyncIteration:
                # Exhausting the logical iterator does not prove that its SDK
                # resources are closed. The adapter close remains the physical
                # completion boundary for terminal commit and Host teardown.
                await self.aclose()
            except BaseException:
                await self.aclose()
        else:
            await self.aclose()
        self.state = SanitizingProviderTransportState.ERROR_TERMINAL_PENDING

    async def request_cancel(
        self, *, reason: Literal["user_stop", "host_teardown"]
    ) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.state = SanitizingProviderTransportState.CANCELLING
        await self.aclose()

    async def aclose(self) -> None:
        closer = getattr(self._raw_stream, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except BaseException:
                self.state = SanitizingProviderTransportState.PHYSICAL_DRAIN_BLOCKED
                return
        self._physical_completed = True
        if not self._terminal_delivered:
            self.state = SanitizingProviderTransportState.CLOSED

    async def wait_physical_completion(self) -> ProviderTransportPhysicalCompletion:
        status = (
            ProviderTransportPhysicalCompletionStatus.COMPLETED
            if self._physical_completed
            else ProviderTransportPhysicalCompletionStatus.BLOCKED_UNTRUSTED
        )
        diagnostic = None if self._physical_completed else "provider_physical_state_untrusted"
        payload = {"status": status.value, "diagnostic_code": diagnostic}
        return ProviderTransportPhysicalCompletion(
            status=status,
            diagnostic_code=diagnostic,
            completion_fingerprint=sha256_fingerprint(
                "provider-transport-physical-completion:v1", payload
            ),
        )


class SanitizingLLMTransport:
    """The only transport binding exposed by a production registry."""

    def __init__(self, raw_transport: RawLLMTransport) -> None:
        self._raw_transport = raw_transport
        self.api = raw_transport.api
        self.binding_id = raw_transport.binding_id
        self.contract_version = raw_transport.contract_version
        self.sanitizer_contract_fingerprint = (
            DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.contract_fingerprint
        )
        self.boundary_contract_fingerprint = sha256_fingerprint(
            "sanitizing-llm-transport:v1",
            {
                "api": self.api,
                "binding_id": self.binding_id,
                "contract_version": self.contract_version,
                "sanitizer_contract_fingerprint": self.sanitizer_contract_fingerprint,
            },
        )

    def open_stream(
        self, *, call: ResolvedModelCall, context: LLMContext
    ) -> SanitizingProviderTransportExecution:
        raw_context = EventContext(
            run_id=f"raw-provider:{call.fact.resolved_model_call_id}",
            turn_id=f"raw-provider:{call.fact.resolved_model_call_id}",
            reply_id=f"raw-provider:{call.fact.resolved_model_call_id}",
        )
        try:
            stream = self._raw_transport.stream(
                call=call,
                context=context,
                event_context=raw_context,
            )
        except BaseException as exc:
            error = sanitize_provider_failure(message=exc)

            async def empty() -> AsyncIterator[AgentEvent | TransportUsageReport]:
                if False:  # pragma: no cover
                    yield TransportUsageReport(usage_status="missing", usage=None)

            stream = empty()
            return SanitizingProviderTransportExecution(
                raw_stream=stream,
                resolved_model_call_id=call.fact.resolved_model_call_id,
                prefailed_error=error,
            )
        return SanitizingProviderTransportExecution(
            raw_stream=stream,
            resolved_model_call_id=call.fact.resolved_model_call_id,
        )


__all__ = [
    "DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT",
    "ProviderTransportPhysicalCompletion",
    "ProviderTransportPhysicalCompletionStatus",
    "RawLLMTransport",
    "SanitizingLLMTransport",
    "SanitizingProviderTransportExecution",
    "SanitizingProviderTransportState",
    "sanitize_provider_failure",
]
