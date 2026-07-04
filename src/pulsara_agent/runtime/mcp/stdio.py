"""Minimal stdio MCP client manager spike using JSON-line framing fixtures."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.runtime.mcp.client import _format_call_result, _tool_from_payload
from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.runtime.mcp.types import McpServerConfig, McpServerSnapshot, McpServerStatus, McpStdioConfig


@dataclass(slots=True)
class _StdioServerProcess:
    config: McpServerConfig
    process: asyncio.subprocess.Process
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class StdioMcpClientManager(McpClientManager):
    _snapshots: tuple[McpServerSnapshot, ...]
    _processes: dict[str, _StdioServerProcess]
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), init=False, repr=False)
    _closed: bool = False

    @classmethod
    async def start(cls, configs: tuple[McpServerConfig, ...]) -> "StdioMcpClientManager":
        manager = cls(_snapshots=(), _processes={})
        snapshots: list[McpServerSnapshot] = []
        for config in configs:
            if not isinstance(config.transport, McpStdioConfig):
                continue
            snapshot = await manager._start_one(config)
            snapshots.append(snapshot)
        manager._snapshots = tuple(snapshots)
        return manager

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    async def _start_one(self, config: McpServerConfig) -> McpServerSnapshot:
        if not config.enabled:
            return McpServerSnapshot(config=config, status=McpServerStatus.DISABLED)
        transport = config.transport
        if not isinstance(transport, McpStdioConfig):
            raise TypeError("stdio MCP manager requires stdio config")
        env = os.environ.copy()
        env.update(dict(transport.env))
        try:
            process = await asyncio.create_subprocess_exec(
                transport.command,
                *transport.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(transport.cwd) if transport.cwd is not None else None,
                env=env,
            )
            self._processes[config.server_id] = _StdioServerProcess(config=config, process=process)
            result = await self._json_rpc(config.server_id, "tools/list", {}, timeout_ms=config.startup_timeout_ms)
            tools = tuple(_tool_from_payload(config.server_id, item) for item in result.get("tools", ()))
            return McpServerSnapshot(config=config, status=McpServerStatus.READY, tools=tools)
        except Exception as exc:
            await self._kill_process(config.server_id)
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
            raise RuntimeError("MCP stdio manager is closed")
        result = await self._json_rpc(
            server_id,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout_ms=timeout_ms,
        )
        return _format_call_result(result)

    async def _json_rpc(
        self,
        server_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        server = self._processes[server_id]
        process = server.process
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("MCP stdio process has no pipes")
        async with server.lock:
            request = {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": method,
                "params": params,
            }
            process.stdin.write(_encode_framed_json(request))
            await process.stdin.drain()
            payload = await asyncio.wait_for(_read_framed_json(process.stdout), timeout=timeout_ms / 1000)
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        result = payload.get("result")
        return result if isinstance(result, dict) else {"value": result}

    async def respond_elicitation(self, server_id: str, request_id: str, answer: dict[str, Any]) -> Any:
        config = self._processes[server_id].config
        return await self._json_rpc(
            server_id,
            "elicitation/respond",
            {"requestId": request_id, "answer": answer},
            timeout_ms=config.tool_timeout_ms,
        )

    def cancel_active(self) -> None:
        # Active stdio reads are cancelled by the awaiting task; process teardown
        # is handled by aclose.
        return None

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(self._kill_process(server_id, timeout_seconds=timeout_seconds) for server_id in tuple(self._processes)),
            return_exceptions=True,
        )

    async def _kill_process(self, server_id: str, *, timeout_seconds: float = 5.0) -> None:
        server = self._processes.pop(server_id, None)
        if server is None:
            return
        process = server.process
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()


def _encode_framed_json(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def _read_framed_json(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readuntil(b"\r\n\r\n")
    content_length: int | None = None
    for raw_line in header.decode("ascii").split("\r\n"):
        if raw_line.lower().startswith("content-length:"):
            content_length = int(raw_line.split(":", 1)[1].strip())
            break
    if content_length is None:
        raise RuntimeError("MCP stdio response missing Content-Length")
    body = await reader.readexactly(content_length)
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("MCP stdio response must be a JSON object")
    return payload
