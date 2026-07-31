"""Host-owned capability proving typed MCP form interaction support."""

from __future__ import annotations

from pulsara_agent.ports.mcp_elicitation import McpFormInteractionPort
from pulsara_agent.primitives.context import context_fingerprint


HOST_SESSION_MCP_FORM_CONTRACT_FINGERPRINT = context_fingerprint(
    "host-session-mcp-form-interaction:v1",
    {
        "request_authority": "typed-mcp-elicitation-batch",
        "response_admission": "exact-key-set",
        "resolution": "all-or-nothing",
        "secret_projection": "none",
    },
)


class HostSessionMcpFormInteractionPort(McpFormInteractionPort):
    """Static Host composition capability used by the SDK advertisement gate."""

    __slots__ = ()

    @property
    def contract_fingerprint(self) -> str:
        return HOST_SESSION_MCP_FORM_CONTRACT_FINGERPRINT


__all__ = [
    "HOST_SESSION_MCP_FORM_CONTRACT_FINGERPRINT",
    "HostSessionMcpFormInteractionPort",
]
