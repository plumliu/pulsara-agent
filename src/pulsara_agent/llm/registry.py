"""Transport registry keyed by provider API format."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.llm.sanitizing_transport import SanitizingLLMTransport
from pulsara_agent.llm.transport import LLMTransport


class LLMTransportBindingUntrusted(RuntimeError):
    """A public transport wrapper violated its process-local contract."""


@dataclass(slots=True)
class LLMTransportRegistry:
    production_mode: bool = False
    _transports: dict[str, LLMTransport] = field(default_factory=dict)
    _untrusted_bindings: dict[str, tuple[str, str, str]] = field(
        default_factory=dict
    )

    def register(self, transport: object) -> None:
        if not isinstance(transport, SanitizingLLMTransport):
            if self.production_mode:
                raise TypeError(
                    "production LLM registry only accepts SanitizingLLMTransport"
                )
            transport = SanitizingLLMTransport(transport)  # type: ignore[arg-type]
        if transport.api in self._transports:
            raise ValueError(f"LLM transport already registered: {transport.api}")
        self._transports[transport.api] = transport

    def get(self, api: str) -> LLMTransport:
        untrusted = self._untrusted_bindings.get(api)
        if untrusted is not None:
            binding_id, contract_version, reason_code = untrusted
            raise LLMTransportBindingUntrusted(
                "LLM transport binding is process-locally untrusted: "
                f"{api}/{binding_id}/{contract_version} ({reason_code})"
            )
        try:
            return self._transports[api]
        except KeyError as exc:
            raise KeyError(f"No LLM transport registered for api: {api}") from exc

    def latch_untrusted(
        self,
        transport: LLMTransport,
        *,
        reason_code: str,
    ) -> None:
        registered = self._transports.get(transport.api)
        if registered is not transport:
            raise ValueError("cannot latch an unregistered LLM transport binding")
        identity = (
            transport.binding_id,
            transport.contract_version,
            reason_code,
        )
        current = self._untrusted_bindings.get(transport.api)
        if current is not None and current != identity:
            raise LLMTransportBindingUntrusted(
                "LLM transport binding already has another untrusted identity"
            )
        self._untrusted_bindings[transport.api] = identity

    def apis(self) -> list[str]:
        return sorted(self._transports)


__all__ = [
    "LLMTransportBindingUntrusted",
    "LLMTransportRegistry",
]
