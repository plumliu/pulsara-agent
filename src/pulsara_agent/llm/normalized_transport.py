"""Stage 2 adapter-to-live provider boundary without draft adoption."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator

from pulsara_agent.llm.provider_sanitization import (
    DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
    sanitize_provider_failure,
)
from pulsara_agent.llm.errors import LLMTransportContractError
from pulsara_agent.llm.request import LLMContext
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
)
from pulsara_agent.ports.provider_stream import (
    ProviderAdapterTerminal,
    ProviderAdapterTerminalKind,
    ProviderAdapterTransport,
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamFailure,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.model_call import ModelCallDiagnosticFact

if TYPE_CHECKING:
    from pulsara_agent.llm.resolution import ResolvedModelCall


@dataclass(slots=True)
class _OpenBlock:
    kind: str
    name_or_media: str | None
    chunks: list[str] = field(default_factory=list)


class NormalizedProviderTransportExecution:
    """One physical provider operation with a single typed stream boundary."""

    def __init__(
        self,
        stream: AsyncIterator[object],
        *,
        local_diagnostics: tuple[ModelCallDiagnosticFact, ...] = (),
    ) -> None:
        self._stream = stream
        self._local_diagnostics = local_diagnostics
        self._open: dict[str, _OpenBlock] = {}
        self._seen: set[str] = set()
        self._usage: TransportUsageReport | None = None
        self._terminal_delivered = False
        self._adapter_terminal_seen = False
        self._physical_completed = False
        self._physical_blocked = False

    @property
    def observed_usage(self) -> TransportUsageReport | None:
        """Current logical response observation, also inspectable after cancellation."""
        return self._usage

    async def read_next(self) -> ProviderStreamPayload | ProviderStreamTerminal | None:
        if self._terminal_delivered:
            return None
        while True:
            try:
                item = await anext(self._stream)
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                self._physical_completed = True
                return self._terminal_error(
                    "Provider stream ended before an explicit semantic terminal.",
                    "transport_protocol_error",
                )
            except BaseException as exc:
                return self._terminal_error(
                    exc,
                    (
                        exc.reason_code
                        if isinstance(exc, LLMTransportContractError)
                        else None
                    ),
                )

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
            if isinstance(item, ProviderAdapterTerminal):
                if self._adapter_terminal_seen:
                    return self._terminal_error(
                        "Provider emitted duplicate semantic terminals.",
                        "transport_protocol_error",
                    )
                self._adapter_terminal_seen = True
                if (
                    item.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
                    and self._open
                ):
                    return self._terminal_error(
                        "Provider completed with an open semantic block.",
                        "transport_protocol_error",
                    )
                try:
                    trailing = await anext(self._stream)
                except asyncio.CancelledError:
                    raise
                except StopAsyncIteration:
                    self._physical_completed = True
                except BaseException as exc:
                    return self._terminal_error(exc, None)
                else:
                    return self._terminal_error(
                        (
                            "Provider emitted an item after its explicit semantic "
                            f"terminal ({type(trailing).__name__})."
                        ),
                        "transport_protocol_error",
                    )
                self._terminal_delivered = True
                return ProviderStreamTerminal(
                    terminal_kind=(
                        ProviderNormalizedTerminalKind.COMPLETED
                        if item.terminal_kind is ProviderAdapterTerminalKind.COMPLETED
                        else ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
                    ),
                    usage=self._effective_usage(),
                    incomplete_reason=item.incomplete_reason,
                    completed_replay_payload=item.completed_replay_payload,
                )
            if not is_provider_stream_payload(item):
                return self._terminal_error(
                    "Provider emitted an unsupported semantic event.",
                    "transport_protocol_error",
                )
            try:
                # SDK decoding and replay storage own their resource bounds.
                # Re-serializing semantic deltas/end snapshots here makes the
                # accepted output depend on fragmentation and counts it twice.
                # See PULSARA_PROVIDER_OUTPUT_BUDGET_SPEC.zh.md.
                self._apply(item)
            except BaseException:
                return self._terminal_error(
                    "Provider emitted an invalid semantic event.",
                    "transport_protocol_error",
                )
            return item

    async def request_cancel(self, *, reason: str) -> None:
        del reason
        await self.aclose()

    async def aclose(self) -> None:
        closer = getattr(self._stream, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except asyncio.CancelledError:
                raise
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
            terminal_kind=ProviderNormalizedTerminalKind.PROVIDER_ERROR,
            usage=self._effective_usage(),
            error=sanitize_provider_failure(
                message=message,
                code_hint=code_hint,
                retry_summary=retry_summary,
            ),
        )

    def _effective_usage(self) -> TransportUsageReport:
        report = self._usage or TransportUsageReport(
            usage_status="missing", usage=None
        )
        if not self._local_diagnostics:
            return report
        return TransportUsageReport(
            usage_status=report.usage_status,
            usage=report.usage,
            provider_diagnostics=(
                *self._local_diagnostics,
                *report.provider_diagnostics,
            ),
            reported_model_id=report.reported_model_id,
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
            "normalized-live-provider-transport:v2-explicit-terminal",
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
        diagnostics = (
            (
                ModelCallDiagnosticFact(
                    code="catalog_tool_call_support_unknown",
                    message="The model catalog does not confirm tool-call support.",
                ),
            )
            if context.tools and call.target.contract.target_facts.tool_call is None
            else ()
        )
        return NormalizedProviderTransportExecution(
            stream,
            local_diagnostics=diagnostics,
        )


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
