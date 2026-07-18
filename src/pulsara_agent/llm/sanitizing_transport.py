"""Secret-safe adapter boundary for provider model streams."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, AsyncIterator, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pulsara_agent.event import EventContext
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
    SanitizedProviderSemanticEnvelope,
    build_semantic_draft,
    build_terminal_draft,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.raw_provider import (
    RawLLMTransport,
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderDataDelta,
    RawProviderFailure,
    RawProviderStreamItem,
    RawProviderTextDelta,
    RawProviderThinkingDelta,
    RawProviderToolCallDelta,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.primitives.model_call import (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT,
    ProviderErrorSanitizationContractFact,
    ProviderModelStreamErrorCode,
    ProviderRetrySummaryFact,
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


@dataclass(frozen=True, slots=True)
class _OutstandingSanitizedEnvelope:
    envelope: SanitizedProviderSemanticEnvelope
    raw_item: RawProviderStreamItem | None
    semantic_state_after: _SanitizingSemanticBlockState | None


@dataclass(frozen=True, slots=True)
class _SanitizingSemanticBlockState:
    active_text_blocks: frozenset[str]
    seen_text_blocks: frozenset[str]
    active_thinking_blocks: frozenset[str]
    seen_thinking_blocks: frozenset[str]
    active_data_blocks: tuple[tuple[str, str], ...]
    seen_data_blocks: frozenset[str]
    active_tool_calls: frozenset[str]
    seen_tool_calls: frozenset[str]


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


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_SECRET_RE = re.compile(
    r"(?i)(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:authorization|proxy[-_ ]?authorization|api[-_ ]?key|x[-_ ]?api[-_ ]?key|"
    r"access[-_ ]?token|refresh[-_ ]?token|password|passwd|secret|cookie|set[-_ ]?cookie)"
    r"\s*[:=]\s*[^\s,;}]+"
)
_MAX_SINGLE_SOURCE_ITEM_CANONICAL_BYTES = (
    DEFAULT_MODEL_STREAM_SEGMENT_POLICY_CONTRACT
    .max_single_source_item_canonical_bytes
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
    *,
    message: object,
    code_hint: str | None = None,
    retry_summary: ProviderRetrySummaryFact | None = None,
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
            "retry_summary": retry_summary,
        }
        provisional = ProviderSanitizedErrorFact.model_construct(
            **payload, error_fingerprint="pending"
        )
        canonical = provisional.model_dump(mode="json", exclude={"error_fingerprint"})
        return ProviderSanitizedErrorFact(
            **canonical,
            error_fingerprint=sha256_fingerprint(
                "provider-sanitized-error:v2", canonical
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
            "retry_summary": retry_summary,
        }
        provisional = ProviderSanitizedErrorFact.model_construct(
            **payload, error_fingerprint="pending"
        )
        canonical = provisional.model_dump(mode="json", exclude={"error_fingerprint"})
        return ProviderSanitizedErrorFact(
            **canonical,
            error_fingerprint=sha256_fingerprint(
                "provider-sanitized-error:v2", canonical
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
        raw_stream: AsyncIterator[RawProviderStreamItem | TransportUsageReport],
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
        self._source_accumulator = sha256_fingerprint(
            "model-stream-sanitized-source:v2", "empty"
        )
        self._accepted_source_item_count = 0
        self._accepted_source_payload_bytes = 0
        self._usage: TransportUsageReport | None = None
        self._pending_error = prefailed_error
        self._pending_error_counts_as_adapter_source_item = False
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
        self._outstanding: _OutstandingSanitizedEnvelope | None = None

    @property
    def source_accumulator(self) -> str:
        return self._source_accumulator

    @property
    def has_outstanding_envelope(self) -> bool:
        return self._outstanding is not None

    async def read_next(self) -> ProviderTransportStreamItem | None:
        if self._outstanding is not None:
            raise RuntimeError(
                "sanitizing transport cannot read while an envelope awaits adoption"
            )
        if self._terminal_delivered:
            return None
        if self._pending_error is not None:
            if self.state is SanitizingProviderTransportState.ERROR_DRAFT_PENDING:
                draft = build_semantic_draft(
                    ProviderErrorDraft,
                    transport_sequence_index=self.next_transport_sequence_index,
                    error=self._pending_error,
                )
                error_payload_bytes = len(
                    canonical_json_bytes(draft.model_dump(mode="json"))
                )
                if self._pending_error_counts_as_adapter_source_item and (
                    self._accepted_source_item_count + 1
                    > MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
                    or self._accepted_source_payload_bytes + error_payload_bytes
                    > MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL
                ):
                    self._pending_error = sanitize_provider_failure(
                        message="Provider stream exceeded its source circuit breaker.",
                        code_hint=(
                            "transport_source_item_limit_exceeded"
                            if self._accepted_source_item_count + 1
                            > MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
                            else "transport_source_payload_limit_exceeded"
                        ),
                    )
                    self._pending_error_counts_as_adapter_source_item = False
                    return await self.read_next()
                return self._prepare_envelope(
                    draft=draft,
                    raw_item=None,
                    counts_as_adapter_source_item=(
                        self._pending_error_counts_as_adapter_source_item
                    ),
                )
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
                semantic_source_accumulator=self._source_accumulator,
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
                    semantic_source_accumulator=self._source_accumulator,
                )
            except BaseException as exc:
                self._pending_error = sanitize_provider_failure(message=exc)
                self._pending_error_counts_as_adapter_source_item = False
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                return await self.read_next()

            if isinstance(item, TransportUsageReport):
                if self._usage is not None:
                    self._pending_error = sanitize_provider_failure(
                        message="Provider emitted duplicate usage reports.",
                        code_hint="transport_protocol_error",
                    )
                    self._pending_error_counts_as_adapter_source_item = False
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                self._usage = item
                continue
            if isinstance(item, RawProviderFailure):
                self._pending_error = sanitize_provider_failure(
                    message=item.message,
                    code_hint=item.code_hint,
                    retry_summary=item.retry_summary,
                )
                self._pending_error_counts_as_adapter_source_item = True
                # A structured adapter error may precede its terminal usage
                # report. Drain that report without accepting later semantics.
                self._collect_usage_while_draining_error = True
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                return await self.read_next()
            try:
                semantic_state_after = self._preview_semantic_state(item)
                draft = self._draft_from_raw_item(item, advance_cursor=False)
                payload_bytes = len(
                    canonical_json_bytes(draft.model_dump(mode="json"))
                )
                if payload_bytes > _MAX_SINGLE_SOURCE_ITEM_CANONICAL_BYTES:
                    self._pending_error = sanitize_provider_failure(
                        message="Provider source item exceeded the canonical byte cap.",
                        code_hint="transport_source_payload_limit_exceeded",
                    )
                    self._pending_error_counts_as_adapter_source_item = False
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                if (
                    self._accepted_source_item_count + 1
                    > MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
                ):
                    self._pending_error = sanitize_provider_failure(
                        message="Provider stream exceeded the source-item circuit breaker.",
                        code_hint="transport_source_item_limit_exceeded",
                    )
                    self._pending_error_counts_as_adapter_source_item = False
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
                    self._pending_error_counts_as_adapter_source_item = False
                    self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                    return await self.read_next()
                return self._prepare_envelope(
                    draft=draft,
                    raw_item=item,
                    semantic_state_after=semantic_state_after,
                    counts_as_adapter_source_item=True,
                    payload_bytes=payload_bytes,
                )
            except BaseException:
                self._pending_error = sanitize_provider_failure(
                    message="Provider emitted an unsupported semantic event.",
                    code_hint="transport_protocol_error",
                )
                self._pending_error_counts_as_adapter_source_item = False
                self.state = SanitizingProviderTransportState.ERROR_DRAFT_PENDING
                return await self.read_next()

    def _prepare_envelope(
        self,
        *,
        draft: Any,
        raw_item: RawProviderStreamItem | None,
        semantic_state_after: _SanitizingSemanticBlockState | None = None,
        counts_as_adapter_source_item: bool,
        payload_bytes: int | None = None,
    ) -> SanitizedProviderSemanticEnvelope:
        if self._outstanding is not None:
            raise RuntimeError("only one sanitized envelope may be outstanding")
        if draft.transport_sequence_index != self.next_transport_sequence_index:
            raise RuntimeError("sanitized envelope source index drift")
        resolved_payload_bytes = (
            payload_bytes
            if payload_bytes is not None
            else len(canonical_json_bytes(draft.model_dump(mode="json")))
        )
        if counts_as_adapter_source_item:
            if (
                self._accepted_source_item_count + 1
                > MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL
            ):
                raise RuntimeError("adapter source item circuit breaker drift")
            if (
                self._accepted_source_payload_bytes + resolved_payload_bytes
                > MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL
            ):
                raise RuntimeError("adapter source payload circuit breaker drift")
        source_before = self._source_accumulator
        source_after = sha256_fingerprint(
            "model-stream-sanitized-source-receipt:v2",
            {
                "source_accumulator_before": source_before,
                "transport_sequence_index": self.next_transport_sequence_index,
                "draft_kind": draft.draft_kind,
                "draft_fingerprint": draft.draft_fingerprint,
            },
        )
        envelope_id = sha256_fingerprint(
            "sanitized-provider-semantic-envelope:v1",
            {
                "resolved_model_call_id": self._resolved_model_call_id,
                "transport_sequence_index": self.next_transport_sequence_index,
                "source_accumulator_before": source_before,
                "source_accumulator_after": source_after,
                "draft_fingerprint": draft.draft_fingerprint,
            },
        )
        envelope = SanitizedProviderSemanticEnvelope(
            envelope_id=envelope_id,
            draft=draft,
            proposed_transport_sequence_index=self.next_transport_sequence_index,
            source_accumulator_before=source_before,
            source_accumulator_after=source_after,
            accepted_at_monotonic_ns=time.monotonic_ns(),
            adapter_source_payload_bytes=resolved_payload_bytes,
            counts_as_adapter_source_item=counts_as_adapter_source_item,
        )
        self._outstanding = _OutstandingSanitizedEnvelope(
            envelope=envelope,
            raw_item=raw_item,
            semantic_state_after=semantic_state_after,
        )
        return envelope

    def require_adoptable(
        self,
        envelope: SanitizedProviderSemanticEnvelope,
    ) -> None:
        outstanding = self._outstanding
        if outstanding is None or outstanding.envelope != envelope:
            raise RuntimeError("sanitized envelope adoption identity mismatch")
        if envelope.source_accumulator_before != self._source_accumulator:
            raise RuntimeError("sanitized envelope source accumulator drift")
        if (
            envelope.proposed_transport_sequence_index
            != self.next_transport_sequence_index
        ):
            raise RuntimeError("sanitized envelope source index drift")

    def acknowledge_adopted(self, envelope_id: str) -> None:
        outstanding = self._outstanding
        if outstanding is None or outstanding.envelope.envelope_id != envelope_id:
            raise RuntimeError("sanitized envelope adoption identity mismatch")
        envelope = outstanding.envelope
        self.require_adoptable(envelope)
        semantic_state = outstanding.semantic_state_after
        if semantic_state is not None:
            self._active_text_blocks = set(semantic_state.active_text_blocks)
            self._seen_text_blocks = set(semantic_state.seen_text_blocks)
            self._active_thinking_blocks = set(
                semantic_state.active_thinking_blocks
            )
            self._seen_thinking_blocks = set(semantic_state.seen_thinking_blocks)
            self._active_data_blocks = dict(semantic_state.active_data_blocks)
            self._seen_data_blocks = set(semantic_state.seen_data_blocks)
            self._active_tool_calls = set(semantic_state.active_tool_calls)
            self._seen_tool_calls = set(semantic_state.seen_tool_calls)
        if envelope.counts_as_adapter_source_item:
            self._accepted_source_item_count += 1
            self._accepted_source_payload_bytes += (
                envelope.adapter_source_payload_bytes
            )
        self.next_transport_sequence_index += 1
        self._source_accumulator = envelope.source_accumulator_after
        if isinstance(envelope.draft, ProviderErrorDraft):
            self.state = SanitizingProviderTransportState.ERROR_DRAFT_DELIVERED
        self._outstanding = None

    def discard_unadopted(self, envelope_id: str) -> None:
        outstanding = self._outstanding
        if outstanding is None or outstanding.envelope.envelope_id != envelope_id:
            raise RuntimeError("sanitized envelope discard identity mismatch")
        self._outstanding = None

    def _draft_from_raw_item(
        self,
        item: RawProviderStreamItem,
        *,
        advance_cursor: bool = True,
    ):
        index = self.next_transport_sequence_index
        draft_type: type[Any]
        payload: dict[str, Any]
        if isinstance(item, RawProviderBlockStart):
            if item.block_kind == "text":
                draft_type = ProviderTextBlockStartDraft
                payload = {"block_id": item.block_id}
            elif item.block_kind == "thinking":
                draft_type = ProviderThinkingBlockStartDraft
                payload = {"block_id": item.block_id}
            elif item.block_kind == "data":
                draft_type = ProviderDataBlockStartDraft
                payload = {"block_id": item.block_id, "media_type": item.media_type}
            else:
                draft_type = ProviderToolCallStartDraft
                payload = {
                    "tool_call_id": item.block_id,
                    "tool_call_name": item.tool_call_name,
                }
        elif isinstance(item, RawProviderTextDelta):
            draft_type = ProviderTextBlockDeltaDraft
            payload = {"block_id": item.block_id, "delta": item.delta}
        elif isinstance(item, RawProviderThinkingDelta):
            draft_type = ProviderThinkingBlockDeltaDraft
            payload = {"block_id": item.block_id, "delta": item.delta}
        elif isinstance(item, RawProviderDataDelta):
            draft_type = ProviderDataBlockDeltaDraft
            payload = {
                "block_id": item.block_id,
                "media_type": item.media_type,
                "data": item.data,
            }
        elif isinstance(item, RawProviderToolCallDelta):
            draft_type = ProviderToolCallDeltaDraft
            payload = {"tool_call_id": item.tool_call_id, "delta": item.delta}
        elif isinstance(item, RawProviderBlockEnd):
            if item.block_kind == "text":
                draft_type = ProviderTextBlockEndDraft
                payload = {"block_id": item.block_id}
            elif item.block_kind == "thinking":
                draft_type = ProviderThinkingBlockEndDraft
                payload = {"block_id": item.block_id}
            elif item.block_kind == "data":
                draft_type = ProviderDataBlockEndDraft
                payload = {"block_id": item.block_id}
            else:
                draft_type = ProviderToolCallEndDraft
                payload = {"tool_call_id": item.block_id}
        else:
            raise TypeError(type(item).__name__)
        draft = build_semantic_draft(
            draft_type,
            transport_sequence_index=index,
            **payload,
        )
        if advance_cursor:
            self.next_transport_sequence_index += 1
        return draft

    def _preview_semantic_state(
        self,
        item: RawProviderStreamItem,
    ) -> _SanitizingSemanticBlockState:
        active_text = set(self._active_text_blocks)
        seen_text = set(self._seen_text_blocks)
        active_thinking = set(self._active_thinking_blocks)
        seen_thinking = set(self._seen_thinking_blocks)
        active_data = dict(self._active_data_blocks)
        seen_data = set(self._seen_data_blocks)
        active_tools = set(self._active_tool_calls)
        seen_tools = set(self._seen_tool_calls)
        self._validate_semantic_item(
            item,
            active_text_blocks=active_text,
            seen_text_blocks=seen_text,
            active_thinking_blocks=active_thinking,
            seen_thinking_blocks=seen_thinking,
            active_data_blocks=active_data,
            seen_data_blocks=seen_data,
            active_tool_calls=active_tools,
            seen_tool_calls=seen_tools,
        )
        return _SanitizingSemanticBlockState(
            active_text_blocks=frozenset(active_text),
            seen_text_blocks=frozenset(seen_text),
            active_thinking_blocks=frozenset(active_thinking),
            seen_thinking_blocks=frozenset(seen_thinking),
            active_data_blocks=tuple(sorted(active_data.items())),
            seen_data_blocks=frozenset(seen_data),
            active_tool_calls=frozenset(active_tools),
            seen_tool_calls=frozenset(seen_tools),
        )

    def _validate_semantic_item(
        self,
        item: RawProviderStreamItem,
        *,
        active_text_blocks: set[str] | None = None,
        seen_text_blocks: set[str] | None = None,
        active_thinking_blocks: set[str] | None = None,
        seen_thinking_blocks: set[str] | None = None,
        active_data_blocks: dict[str, str] | None = None,
        seen_data_blocks: set[str] | None = None,
        active_tool_calls: set[str] | None = None,
        seen_tool_calls: set[str] | None = None,
    ) -> None:
        active_text_blocks = (
            self._active_text_blocks
            if active_text_blocks is None
            else active_text_blocks
        )
        seen_text_blocks = (
            self._seen_text_blocks if seen_text_blocks is None else seen_text_blocks
        )
        active_thinking_blocks = (
            self._active_thinking_blocks
            if active_thinking_blocks is None
            else active_thinking_blocks
        )
        seen_thinking_blocks = (
            self._seen_thinking_blocks
            if seen_thinking_blocks is None
            else seen_thinking_blocks
        )
        active_data_blocks = (
            self._active_data_blocks
            if active_data_blocks is None
            else active_data_blocks
        )
        seen_data_blocks = (
            self._seen_data_blocks if seen_data_blocks is None else seen_data_blocks
        )
        active_tool_calls = (
            self._active_tool_calls
            if active_tool_calls is None
            else active_tool_calls
        )
        seen_tool_calls = (
            self._seen_tool_calls if seen_tool_calls is None else seen_tool_calls
        )
        if isinstance(item, RawProviderBlockStart) and item.block_kind == "text":
            _open_semantic_id(
                item.block_id,
                active=active_text_blocks,
                seen=seen_text_blocks,
            )
        elif isinstance(item, RawProviderTextDelta):
            _require_active_semantic_id(item.block_id, active_text_blocks)
        elif isinstance(item, RawProviderBlockEnd) and item.block_kind == "text":
            _close_semantic_id(item.block_id, active_text_blocks)
        elif isinstance(item, RawProviderBlockStart) and item.block_kind == "thinking":
            _open_semantic_id(
                item.block_id,
                active=active_thinking_blocks,
                seen=seen_thinking_blocks,
            )
        elif isinstance(item, RawProviderThinkingDelta):
            _require_active_semantic_id(item.block_id, active_thinking_blocks)
        elif isinstance(item, RawProviderBlockEnd) and item.block_kind == "thinking":
            _close_semantic_id(item.block_id, active_thinking_blocks)
        elif isinstance(item, RawProviderBlockStart) and item.block_kind == "data":
            if item.block_id in seen_data_blocks:
                raise ValueError("duplicate data block start")
            seen_data_blocks.add(item.block_id)
            assert item.media_type is not None
            active_data_blocks[item.block_id] = item.media_type
        elif isinstance(item, RawProviderDataDelta):
            if active_data_blocks.get(item.block_id) != item.media_type:
                raise ValueError("data block delta identity mismatch")
        elif isinstance(item, RawProviderBlockEnd) and item.block_kind == "data":
            if active_data_blocks.pop(item.block_id, None) is None:
                raise ValueError("data block end outside active block")
        elif isinstance(item, RawProviderBlockStart) and item.block_kind == "tool_call":
            _open_semantic_id(
                item.block_id,
                active=active_tool_calls,
                seen=seen_tool_calls,
            )
        elif isinstance(item, RawProviderToolCallDelta):
            _require_active_semantic_id(item.tool_call_id, active_tool_calls)
        elif isinstance(item, RawProviderBlockEnd) and item.block_kind == "tool_call":
            _close_semantic_id(item.block_id, active_tool_calls)
        else:
            raise TypeError(type(item).__name__)

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
        if self._outstanding is not None:
            raise RuntimeError(
                "coordinator must discard an unadopted envelope before cancellation"
            )
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

            async def empty() -> AsyncIterator[
                RawProviderStreamItem | TransportUsageReport
            ]:
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
