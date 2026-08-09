"""Stage 2 adapter-to-live provider boundary without draft adoption."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import AsyncIterator

from pulsara_agent.llm.provider_sanitization import (
    DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
    sanitize_provider_failure,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import (
    DataDeltaPayload,
    DataEndPayload,
    DataStartPayload,
    ProviderStreamPayload,
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    is_provider_stream_payload,
    payload_to_mapping,
)
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterTransport,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamFailure,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.authority_materialization import (
    MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL,
    MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint


_MAX_SINGLE_PAYLOAD_BYTES = 256 << 10


@dataclass(slots=True)
class _OpenBlock:
    kind: str
    name_or_media: str | None
    chunks: list[str] = field(default_factory=list)


class NormalizedProviderTransportExecution:
    """One physical provider operation with a single typed stream boundary."""

    def __init__(self, stream: AsyncIterator[object]) -> None:
        self._stream = stream
        self._open: dict[str, _OpenBlock] = {}
        self._seen: set[str] = set()
        self._usage: TransportUsageReport | None = None
        self._item_count = 0
        self._payload_bytes = 0
        self._terminal_delivered = False
        self._physical_completed = False
        self._physical_blocked = False

    async def read_next(self) -> ProviderStreamPayload | ProviderStreamTerminal | None:
        if self._terminal_delivered:
            return None
        while True:
            try:
                item = await anext(self._stream)
            except StopAsyncIteration:
                self._physical_completed = True
                if self._open:
                    return self._terminal_error(
                        "Provider stream ended with an open semantic block.",
                        "transport_protocol_error",
                    )
                self._terminal_delivered = True
                return ProviderStreamTerminal(
                    outcome="COMPLETED",
                    usage=self._usage
                    or TransportUsageReport(usage_status="missing", usage=None),
                )
            except BaseException as exc:
                return self._terminal_error(exc, None)

            if isinstance(item, TransportUsageReport):
                if self._usage is not None:
                    return self._terminal_error(
                        "Provider emitted duplicate usage reports.",
                        "transport_protocol_error",
                    )
                self._usage = item
                continue
            if isinstance(item, ProviderStreamFailure):
                return self._terminal_error(
                    item.message,
                    item.code_hint,
                    retry_summary=item.retry_summary,
                )
            if not is_provider_stream_payload(item):
                return self._terminal_error(
                    "Provider emitted an unsupported semantic event.",
                    "transport_protocol_error",
                )
            try:
                encoded = json.dumps(
                    payload_to_mapping(item),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                if len(encoded) > _MAX_SINGLE_PAYLOAD_BYTES:
                    return self._terminal_error(
                        "Provider source item exceeded the canonical byte cap.",
                        "transport_source_payload_limit_exceeded",
                    )
                if self._item_count + 1 > MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL:
                    return self._terminal_error(
                        "Provider stream exceeded the source-item circuit breaker.",
                        "transport_source_item_limit_exceeded",
                    )
                if (
                    self._payload_bytes + len(encoded)
                    > MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL
                ):
                    return self._terminal_error(
                        "Provider stream exceeded the sanitized-byte circuit breaker.",
                        "transport_source_payload_limit_exceeded",
                    )
                self._apply(item)
            except BaseException:
                return self._terminal_error(
                    "Provider emitted an invalid semantic event.",
                    "transport_protocol_error",
                )
            self._item_count += 1
            self._payload_bytes += len(encoded)
            return item

    async def request_cancel(self, *, reason: str) -> None:
        del reason
        await self.aclose()

    async def aclose(self) -> None:
        closer = getattr(self._stream, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except BaseException:
                self._physical_blocked = True
                return
        self._physical_completed = True

    async def wait_physical_completion(self) -> ProviderPhysicalCompletion:
        status = (
            ProviderPhysicalCompletionStatus.COMPLETED
            if self._physical_completed and not self._physical_blocked
            else ProviderPhysicalCompletionStatus.BLOCKED
        )
        return ProviderPhysicalCompletion(
            status=status,
            diagnostic_code=(
                None
                if status is ProviderPhysicalCompletionStatus.COMPLETED
                else "provider_physical_state_untrusted"
            ),
        )

    def _terminal_error(
        self,
        message: object,
        code_hint: str | None,
        *,
        retry_summary=None,
    ) -> ProviderStreamTerminal:
        self._terminal_delivered = True
        return ProviderStreamTerminal(
            outcome="PROVIDER_ERROR",
            usage=self._usage
            or TransportUsageReport(usage_status="missing", usage=None),
            error=sanitize_provider_failure(
                message=message,
                code_hint=code_hint,
                retry_summary=retry_summary,
            ),
        )

    def _apply(self, item: ProviderStreamPayload) -> None:
        identity = item.block_identity
        if isinstance(item, TextStartPayload):
            self._start(identity, "text", None)
        elif isinstance(item, ThinkingStartPayload):
            self._start(identity, "thinking", None)
        elif isinstance(item, DataStartPayload):
            self._start(identity, "data", item.media_type)
        elif isinstance(item, ToolCallStartPayload):
            if item.tool_call_id != identity:
                raise ValueError("tool-call stream identity mismatch")
            self._start(identity, "tool", item.tool_name)
        elif isinstance(item, TextDeltaPayload):
            self._delta(identity, "text", item.delta)
        elif isinstance(item, ThinkingDeltaPayload):
            self._delta(identity, "thinking", item.delta)
        elif isinstance(item, DataDeltaPayload):
            self._delta(identity, "data", item.data)
        elif isinstance(item, ToolCallDeltaPayload):
            if item.tool_call_id != identity:
                raise ValueError("tool-call delta identity mismatch")
            self._delta(identity, "tool", item.delta)
        elif isinstance(item, TextEndPayload):
            self._end(identity, "text", item.final_text, None)
        elif isinstance(item, ThinkingEndPayload):
            self._end(identity, "thinking", item.final_text, None)
        elif isinstance(item, DataEndPayload):
            self._end(identity, "data", item.final_data, item.media_type)
        elif isinstance(item, ToolCallEndPayload):
            if item.tool_call_id != identity:
                raise ValueError("tool-call terminal identity mismatch")
            self._end(identity, "tool", item.arguments_json, item.tool_name)
        else:  # pragma: no cover - guarded by the closed TypeGuard
            raise TypeError(type(item).__name__)

    def _start(self, identity: str, kind: str, name: str | None) -> None:
        if identity in self._seen:
            raise ValueError("provider reused a semantic block identity")
        self._seen.add(identity)
        self._open[identity] = _OpenBlock(kind=kind, name_or_media=name)

    def _delta(self, identity: str, kind: str, value: str) -> None:
        block = self._open.get(identity)
        if block is None or block.kind != kind:
            raise ValueError("provider delta lacks an exact start")
        block.chunks.append(value)

    def _end(
        self,
        identity: str,
        kind: str,
        final_value: str,
        name_or_media: str | None,
    ) -> None:
        block = self._open.pop(identity, None)
        if block is None or block.kind != kind:
            raise ValueError("provider end lacks an exact start")
        if block.name_or_media != name_or_media:
            raise ValueError("provider terminal block metadata changed")
        if "".join(block.chunks) != final_value:
            raise ValueError("provider terminal block differs from its deltas")


class NormalizedLLMTransport:
    """Production Stage 2 binding from adapter output to formal live payloads."""

    def __init__(self, adapter: ProviderAdapterTransport) -> None:
        self._adapter = adapter
        self.api = adapter.api
        self.binding_id = adapter.binding_id
        self.contract_version = adapter.contract_version
        self.sanitizer_contract_fingerprint = (
            DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.contract_fingerprint
        )
        self.boundary_contract_fingerprint = sha256_fingerprint(
            "normalized-live-provider-transport:v1",
            {
                "api": self.api,
                "binding_id": self.binding_id,
                "contract_version": self.contract_version,
                "sanitizer_contract_fingerprint": self.sanitizer_contract_fingerprint,
            },
        )

    def open_stream(
        self, *, call: ResolvedModelCall, context: LLMContext
    ) -> NormalizedProviderTransportExecution:
        try:
            stream = self._adapter.stream(
                call=call,
                context=context,
            )
        except BaseException as exc:
            failure_message = str(exc) or type(exc).__name__

            async def failed() -> AsyncIterator[object]:
                yield ProviderStreamFailure(message=failure_message)

            stream = failed()
        return NormalizedProviderTransportExecution(stream)


@dataclass(slots=True)
class NormalizedLLMTransportRegistry:
    production_mode: bool = True
    _transports: dict[str, NormalizedLLMTransport] = field(default_factory=dict)

    def register(self, transport: NormalizedLLMTransport) -> None:
        if not isinstance(transport, NormalizedLLMTransport):
            raise TypeError("normalized registry accepts only normalized transports")
        if transport.api in self._transports:
            raise ValueError(f"LLM transport already registered: {transport.api}")
        self._transports[transport.api] = transport

    def get(self, api: str) -> NormalizedLLMTransport:
        try:
            return self._transports[api]
        except KeyError as exc:
            raise KeyError(f"No normalized LLM transport for api: {api}") from exc


__all__ = [
    "NormalizedLLMTransport",
    "NormalizedLLMTransportRegistry",
    "NormalizedProviderTransportExecution",
]
