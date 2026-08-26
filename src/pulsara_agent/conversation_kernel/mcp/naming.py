"""Deterministic flat provider naming for MCP tools.

The model-visible spelling is always ``mcp__<server>__<tool>``.  Both
separators are exactly two underscores; this is one flat function name, not a
provider namespace plus a function name.  Raw MCP server/tool identities stay
separate and are used for physical dispatch.  Provider names are normalized
and truncated only; they never carry identity hashes.
"""

from __future__ import annotations

import re
import unicodedata


MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES = 64
_MAXIMUM_MCP_SERVER_SLUG_BYTES = 24
_MCP_PROVIDER_PREFIX = "mcp__"
_MCP_PROVIDER_SEPARATOR = "__"


def mangle_mcp_tool_names(
    server_id: str, remote_names: tuple[str, ...]
) -> dict[str, str]:
    if len(remote_names) != len(set(remote_names)):
        raise ValueError("MCP remote tool names are not unique")
    server = _slug(server_id)[:_MAXIMUM_MCP_SERVER_SLUG_BYTES]
    prefix = f"{_MCP_PROVIDER_PREFIX}{server}{_MCP_PROVIDER_SEPARATOR}"
    remaining = MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES - len(prefix.encode("ascii"))
    if remaining < 1:  # pragma: no cover - closed by the frozen server bound.
        raise AssertionError("MCP provider name has no tool-name budget")
    result = {remote: f"{prefix}{_slug(remote)[:remaining]}" for remote in remote_names}
    if any(
        len(provider.encode("ascii")) > MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES
        for provider in result.values()
    ):  # pragma: no cover - construction owns the exact bound.
        raise AssertionError("MCP provider tool name exceeded its byte bound")
    return result


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_")
    return slug or "x"


__all__ = ["MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES", "mangle_mcp_tool_names"]
