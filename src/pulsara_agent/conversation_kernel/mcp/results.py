"""Known MCP observations, remote identities, and failure lowering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pulsara_agent.identity import derive_id, require_id
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint

from ..tool_surface import McpEffectKind
from .contracts import McpToolSemanticFact

if TYPE_CHECKING:
    from .sdk_facade import BoundedMcpSdkClient, MCPError


@dataclass(frozen=True, slots=True)
class McpKnownToolResult:
    """Known tool observation, including delivery failure with remote outcome unknown."""

    state: str
    content: bytes
    remote_identity: str
    remote_identity_verification: str

    def __post_init__(self) -> None:
        require_id(self.remote_identity, "mcp-tool")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.remote_identity_verification) is None:
            raise ValueError("MCP remote identity verification is invalid")


def _mcp_remote_identity(
    *, semantic: McpToolSemanticFact, connection_generation: int
) -> tuple[str, str]:
    """Freeze the short identity and full cross-restart verification fact."""
    return (
        derive_id(
            "mcp-tool", "remote-tool", semantic.server_id,
            str(connection_generation), semantic.remote_tool_name,
        ),
        context_fingerprint(
            "pulsara:mcp-remote-tool-identity:v1",
            {
                "server_id": semantic.server_id,
                "connection_generation": connection_generation,
                "remote_tool_name": semantic.remote_tool_name,
            },
        ),
    )


def _mcp_standard_operation_identity(
    *, server_id: str, connection_generation: int, tool_name: str
) -> tuple[str, str]:
    return (
        derive_id(
            "mcp-tool", "standard-operation", server_id,
            str(connection_generation), tool_name,
        ),
        context_fingerprint(
            "pulsara:mcp-standard-operation-identity:v1",
            {
                "server_id": server_id,
                "connection_generation": connection_generation,
                "tool_name": tool_name,
            },
        ),
    )


def mcp_failure_required_facts(effect_kind: McpEffectKind) -> dict[str, object]:
    """The same bounded uncertain-outcome facts used before and after dispatch."""
    guidance = (
        "No reliable observation was received. You may continue with another "
        "source or retry this read when the connection is available."
        if effect_kind is McpEffectKind.READ_ONLY
        else "The remote operation may have executed. Do not assume success or "
        "that nothing happened. Before repeating an operation with side effects, "
        "verify its outcome or idempotency; otherwise ask the user. You may "
        "continue with independent work or another read-only source."
    )
    return {"error": "MCP_CALL_FAILED", "remote_outcome": "UNKNOWN",
            "retry_performed": False, "guidance": guidance}


def _mcp_invocation_failure(
    *,
    error: Exception,
    client: BoundedMcpSdkClient,
    remote_identity: str,
    remote_identity_verification: str,
    effect_kind: McpEffectKind,
) -> McpKnownToolResult:
    # The failed observation is known; the remote operation's outcome is not.
    # Settle without replaying tools/call or claiming an effect did not happen.
    return McpKnownToolResult(
        state="SYSTEM_ERROR",
        content=canonical_json_bytes({
            **mcp_failure_required_facts(effect_kind),
            "message": client.diagnostic_message(error),
        }),
        remote_identity=remote_identity,
        remote_identity_verification=remote_identity_verification,
    )


def _mcp_error_is_transport_failure(error: MCPError) -> bool:
    from .sdk_facade import types

    # SDK 2.1.0 uses these same public codes for local dispatcher close/timeout
    # and peer replies. Keep those ambiguous codes conservative; its other
    # request errors represent a received JSON-RPC ErrorData, not dead I/O.
    return error.code in {types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT}


def _known_mcp_remote_error(
    *, error: MCPError, client: BoundedMcpSdkClient, remote_identity: str,
    remote_identity_verification: str,
) -> McpKnownToolResult:
    # The response and its data already crossed the transport's exact-secret
    # scrub boundary. Keep its diagnostic payload, without replaying the call
    # or treating a server rejection as a reason to retire a healthy slot.
    return McpKnownToolResult(
        state="SYSTEM_ERROR",
        content=canonical_json_bytes(
            {
                "error": "MCP_REMOTE_ERROR",
                "remote_error": {
                    "code": error.code,
                    "message": client.diagnostic_message(error),
                    **({"data": error.data} if error.data is not None else {}),
                },
                "retry_performed": False,
            }
        ),
        remote_identity=remote_identity,
        remote_identity_verification=remote_identity_verification,
    )


def _known_mcp_system_failure(
    *, error_code: str, remote_identity: str, remote_identity_verification: str,
    message: str | None = None,
) -> McpKnownToolResult:
    return McpKnownToolResult(
        state="SYSTEM_ERROR",
        content=canonical_json_bytes(
            {
                "error": error_code,
                "retry_performed": False,
                **({"guidance": "The remote request was not sent."}
                   if error_code == "MCP_TRANSPORT_UNWRITTEN" else {}),
                **({"message": message} if message is not None else {}),
            }
        ),
        remote_identity=remote_identity,
        remote_identity_verification=remote_identity_verification,
    )
