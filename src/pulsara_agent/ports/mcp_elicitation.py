"""Host-facing capabilities for MCP form and URL elicitation.

The ports deliberately carry no SDK objects and never accept a caller supplied
URL.  An exact private URL can only be revealed through the matching one-shot
launch authority issued by the MCP elicitation batch owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias

from pulsara_agent.ports.mcp_secret import McpPrivateUrlElicitationPayload
from pulsara_agent.primitives.context import context_fingerprint


class McpUrlLaunchDisposition(StrEnum):
    LAUNCHED = "launched"
    REJECTED_BY_PLATFORM = "rejected_by_platform"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class McpConfirmedUrlLaunchAuthority:
    request_key: str
    private_url_payload_fingerprint: str
    consent_receipt_fingerprint: str
    owner_id: str
    owner_generation: int

    def __post_init__(self) -> None:
        if (
            not self.request_key
            or not self.private_url_payload_fingerprint
            or not self.consent_receipt_fingerprint
            or not self.owner_id
            or self.owner_generation < 1
        ):
            raise ValueError("MCP URL launch authority is incomplete")


@dataclass(frozen=True, slots=True)
class McpUrlLaunchOutcome:
    disposition: McpUrlLaunchDisposition
    physical_operation_id: str
    sanitized_diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not self.physical_operation_id:
            raise ValueError("MCP URL launch operation identity is required")
        if (
            self.disposition is McpUrlLaunchDisposition.FAILED
            and self.sanitized_diagnostic is None
        ):
            raise ValueError("failed MCP URL launch requires a diagnostic")


class McpFormInteractionPort(Protocol):
    """Marker capability proving the Host can render and submit form input."""

    @property
    def contract_fingerprint(self) -> str: ...


class McpExternalBrowserPort(Protocol):
    """Open an owner-bound private URL after an exact human consent receipt."""

    @property
    def contract_fingerprint(self) -> str: ...

    def register_owner(
        self,
        *,
        owner_id: str,
        private_url_payloads: tuple[McpPrivateUrlElicitationPayload, ...],
    ) -> None: ...

    def exact_url_for_display(self, *, owner_id: str, request_key: str) -> str: ...

    def release_owner(self, *, owner_id: str) -> None: ...

    async def launch(
        self,
        authority: McpConfirmedUrlLaunchAuthority,
    ) -> McpUrlLaunchOutcome: ...


@dataclass(frozen=True, slots=True)
class McpElicitationCapabilityDisabled:
    capability_kind: Literal["disabled"] = "disabled"


@dataclass(frozen=True, slots=True)
class McpElicitationCapabilityFull:
    capability_kind: Literal["full"]
    form_interaction_port: McpFormInteractionPort
    external_browser_port: McpExternalBrowserPort
    contract_fingerprint: str

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "mcp-elicitation-capability-full:v1",
            {
                "form_interaction": (
                    self.form_interaction_port.contract_fingerprint
                ),
                "url_interaction": (
                    self.external_browser_port.contract_fingerprint
                ),
                "modes": ("form", "url"),
            },
        )
        if self.capability_kind != "full" or self.contract_fingerprint != expected:
            raise ValueError("MCP elicitation capability contract mismatch")


McpElicitationCapabilityComposition: TypeAlias = (
    McpElicitationCapabilityDisabled | McpElicitationCapabilityFull
)


def build_full_mcp_elicitation_capability(
    *,
    form_interaction_port: McpFormInteractionPort,
    external_browser_port: McpExternalBrowserPort,
) -> McpElicitationCapabilityFull:
    payload = {
        "form_interaction": form_interaction_port.contract_fingerprint,
        "url_interaction": external_browser_port.contract_fingerprint,
        "modes": ("form", "url"),
    }
    return McpElicitationCapabilityFull(
        capability_kind="full",
        form_interaction_port=form_interaction_port,
        external_browser_port=external_browser_port,
        contract_fingerprint=context_fingerprint(
            "mcp-elicitation-capability-full:v1",
            payload,
        ),
    )


__all__ = [
    "McpConfirmedUrlLaunchAuthority",
    "McpElicitationCapabilityComposition",
    "McpElicitationCapabilityDisabled",
    "McpElicitationCapabilityFull",
    "McpExternalBrowserPort",
    "McpFormInteractionPort",
    "McpUrlLaunchDisposition",
    "McpUrlLaunchOutcome",
    "build_full_mcp_elicitation_capability",
]
