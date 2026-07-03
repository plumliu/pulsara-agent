"""Minimal streamable-HTTP MCP client manager spike."""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.runtime.mcp.types import (
    McpDiscoveredTool,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpStreamableHttpConfig,
    McpToolAnnotations,
)


@dataclass(slots=True)
class HttpMcpClientManager(McpClientManager):
    _snapshots: tuple[McpServerSnapshot, ...]
    _configs: dict[str, McpServerConfig]
    _client: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), init=False, repr=False)
    _closed: bool = False

    @classmethod
    async def discover(cls, configs: tuple[McpServerConfig, ...]) -> "HttpMcpClientManager":
        manager = cls(_snapshots=(), _configs={config.server_id: config for config in configs})
        snapshots: list[McpServerSnapshot] = []
        for config in configs:
            if not isinstance(config.transport, McpStreamableHttpConfig):
                continue
            snapshots.append(await manager._discover_one(config))
        manager._snapshots = tuple(snapshots)
        return manager

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    async def _discover_one(self, config: McpServerConfig) -> McpServerSnapshot:
        if not config.enabled:
            return McpServerSnapshot(config=config, status=McpServerStatus.DISABLED)
        if config.transport.bearer_token_env_var and not os.getenv(config.transport.bearer_token_env_var):
            return McpServerSnapshot(
                config=config,
                status=McpServerStatus.NEEDS_AUTH,
                message=f"missing bearer token env var {config.transport.bearer_token_env_var}",
            )
        try:
            result = await self._json_rpc(config, "tools/list", {}, timeout_ms=config.startup_timeout_ms)
            tools = tuple(_tool_from_payload(config.server_id, item) for item in result.get("tools", ()))
            return McpServerSnapshot(config=config, status=McpServerStatus.READY, tools=tools)
        except Exception as exc:
            return McpServerSnapshot(
                config=config,
                status=McpServerStatus.FAILED,
                message=f"{type(exc).__name__}: {exc}",
            )

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> Any:
        if self._closed:
            raise RuntimeError("MCP HTTP manager is closed")
        config = self._configs[server_id]
        result = await self._json_rpc(
            config,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout_ms=timeout_ms,
        )
        return _format_call_result(result)

    async def _json_rpc(
        self,
        config: McpServerConfig,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        transport = config.transport
        if not isinstance(transport, McpStreamableHttpConfig):
            raise TypeError("HTTP MCP manager requires streamable HTTP config")
        headers = dict(transport.headers)
        for header, env_var in transport.env_headers.items():
            value = os.getenv(env_var)
            if value:
                headers[header] = value
        if transport.bearer_token_env_var:
            token = os.getenv(transport.bearer_token_env_var)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        response = await self._client.post(
            transport.url,
            json={
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": method,
                "params": params,
            },
            headers=headers,
            timeout=timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        result = payload.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def cancel_active(self) -> None:
        # httpx request cancellation is owned by the awaiting task.
        return None

    async def respond_elicitation(self, server_id: str, request_id: str, answer: dict[str, Any]) -> Any:
        config = self._configs[server_id]
        return await self._json_rpc(
            config,
            "elicitation/respond",
            {"requestId": request_id, "answer": answer},
            timeout_ms=config.tool_timeout_ms,
        )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()


def _tool_from_payload(server_id: str, payload: dict[str, Any]) -> McpDiscoveredTool:
    schema = payload.get("inputSchema") or payload.get("input_schema") or {}
    return McpDiscoveredTool(
        server_id=server_id,
        name=str(payload["name"]),
        description=str(payload.get("description") or payload["name"]),
        input_schema=dict(schema),
        annotations=McpToolAnnotations.from_mapping(payload.get("annotations")),
    )


def _format_call_result(result: dict[str, Any]) -> Any:
    content = result.get("content")
    if isinstance(content, list):
        texts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text") is not None
        ]
        if texts:
            return "\n".join(texts)
    return result

