"""Deterministic ASCII provider naming for direct MCP tools."""

from __future__ import annotations

import hashlib
import re
import unicodedata


MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES = 96


def mangle_mcp_tool_names(
    server_id: str, remote_names: tuple[str, ...]
) -> dict[str, str]:
    if len(remote_names) != len(set(remote_names)):
        raise ValueError("MCP remote tool names are not unique")
    server = _slug(server_id, 24)
    preliminary = {name: f"mcp__{server}__{_slug(name, 48)}" for name in remote_names}
    groups: dict[str, list[str]] = {}
    for remote, provider in preliminary.items():
        groups.setdefault(provider, []).append(remote)
    result: dict[str, str] = {}
    for provider, remotes in groups.items():
        for remote in remotes:
            candidate = provider
            if len(remotes) > 1:
                suffix = hashlib.sha256(remote.encode("utf-8")).hexdigest()[:10]
                candidate = f"{provider[:83]}__{suffix}"
            encoded = candidate.encode("ascii")
            if len(encoded) > MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES:
                suffix = hashlib.sha256(remote.encode("utf-8")).hexdigest()[:10]
                candidate = f"{candidate[:83]}__{suffix}"
            result[remote] = candidate
    if len(set(result.values())) != len(result):
        raise ValueError("MCP provider tool normalization collision")
    return result


def _slug(value: str, maximum: int) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_")
    if not slug:
        slug = "x_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    is_canonical = bool(
        re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", value)
        and value == value.lower()
        and len(value) <= maximum
    )
    if not is_canonical:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[: maximum - 12]}__{suffix}"
    elif len(slug) > maximum:
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[: maximum - 12]}__{suffix}"
    return slug


__all__ = ["MAXIMUM_MCP_PROVIDER_TOOL_NAME_BYTES", "mangle_mcp_tool_names"]
