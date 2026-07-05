"""Desired-state supervisor for MCP server managers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.runtime.mcp.sdk import SdkMcpClientManager
from pulsara_agent.runtime.mcp.types import (
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpStreamableHttpConfig,
)


@dataclass(slots=True)
class McpServerSupervisor:
    """Owns SDK-backed per-server managers and reconciles desired config state."""

    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    _managers: dict[str, McpClientManager] = field(default_factory=dict)
    _fingerprints: dict[str, str] = field(default_factory=dict)
    _disabled_configs: dict[str, McpServerConfig] = field(default_factory=dict)
    _retry_attempts: dict[str, int] = field(default_factory=dict)
    _next_retry_monotonic: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def manager(self) -> McpClientManager | None:
        if not self._managers and not self._disabled_configs:
            return None
        return self

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        active = tuple(snapshot for manager in self._managers.values() for snapshot in manager.snapshots)
        disabled = disabled_snapshots(tuple(self._disabled_configs.values()))
        return (*active, *disabled)

    async def sync_servers(self, configs: tuple[McpServerConfig, ...]) -> McpClientManager | None:
        async with self._lock:
            now = time.monotonic()
            desired = {config.server_id: config for config in configs}
            self._disabled_configs = {
                config.server_id: config
                for config in configs
                if not config.enabled
            }
            for server_id in tuple(self._managers):
                if server_id not in desired or not desired[server_id].enabled:
                    await self._disconnect(server_id)
            for config in configs:
                fingerprint = _config_fingerprint(config)
                if not config.enabled:
                    continue
                if self._fingerprints.get(config.server_id) == fingerprint and config.server_id in self._managers:
                    manager = self._managers[config.server_id]
                    if _manager_ready(manager):
                        await _refresh_manager_snapshot(manager)
                        if _manager_ready(manager):
                            self._clear_retry(config.server_id)
                        else:
                            self._schedule_retry(config.server_id, now=now)
                        continue
                    if now < self._next_retry_monotonic.get(config.server_id, 0.0):
                        continue
                await self._disconnect(config.server_id, clear_retry=False)
                manager = await SdkMcpClientManager.start((config,))
                self._managers[config.server_id] = manager
                self._fingerprints[config.server_id] = fingerprint
                if _manager_ready(manager):
                    self._clear_retry(config.server_id)
                else:
                    self._schedule_retry(config.server_id, now=now)
            return self.manager

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_ms: int,
    ) -> object:
        return await self._active_manager(server_id).call_tool(
            server_id,
            tool_name,
            arguments,
            timeout_ms=timeout_ms,
        )

    async def resume_suspended_request(
        self,
        *,
        server_id: str,
        original_request: McpOriginalRequest,
        request_state: str | None,
        resolution: McpInputRequiredResolution,
        timeout_ms: int,
    ) -> object:
        return await self._active_manager(server_id).resume_suspended_request(
            server_id=server_id,
            original_request=original_request,
            request_state=request_state,
            resolution=resolution,
            timeout_ms=timeout_ms,
        )

    def cancel_active(self) -> None:
        for manager in tuple(self._managers.values()):
            manager.cancel_active()

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        async with self._lock:
            managers = tuple(self._managers.values())
            self._managers.clear()
            self._fingerprints.clear()
            self._disabled_configs.clear()
            self._retry_attempts.clear()
            self._next_retry_monotonic.clear()
        await asyncio.gather(
            *(manager.aclose(timeout_seconds=timeout_seconds) for manager in managers),
            return_exceptions=True,
        )

    async def _disconnect(self, server_id: str, *, clear_retry: bool = True) -> None:
        manager = self._managers.pop(server_id, None)
        self._fingerprints.pop(server_id, None)
        if clear_retry:
            self._clear_retry(server_id)
        if manager is not None:
            await manager.aclose()

    def _active_manager(self, server_id: str) -> McpClientManager:
        manager = self._managers.get(server_id)
        if manager is None:
            raise KeyError(f"Unknown or inactive MCP server: {server_id}")
        return manager

    def _schedule_retry(self, server_id: str, *, now: float) -> None:
        attempt = self._retry_attempts.get(server_id, 0) + 1
        self._retry_attempts[server_id] = attempt
        delay = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** max(0, attempt - 1)))
        self._next_retry_monotonic[server_id] = now + max(0.0, delay)

    def _clear_retry(self, server_id: str) -> None:
        self._retry_attempts.pop(server_id, None)
        self._next_retry_monotonic.pop(server_id, None)


def disabled_snapshots(configs: tuple[McpServerConfig, ...]) -> tuple[McpServerSnapshot, ...]:
    return tuple(
        McpServerSnapshot(config=config, status=McpServerStatus.DISABLED)
        for config in configs
        if not config.enabled
    )


def _manager_ready(manager: McpClientManager) -> bool:
    snapshots = manager.snapshots
    return bool(snapshots) and all(snapshot.status is McpServerStatus.READY for snapshot in snapshots)


async def _refresh_manager_snapshot(manager: McpClientManager) -> None:
    refresh = getattr(manager, "refresh", None)
    if refresh is None:
        return
    result = refresh()
    if hasattr(result, "__await__"):
        await result


def _config_fingerprint(config: McpServerConfig) -> str:
    payload = {
        "server_id": config.server_id,
        "transport_kind": config.transport_kind.value,
        "transport": _transport_payload(config),
        "enabled": config.enabled,
        "required": config.required,
        "startup_timeout_ms": config.startup_timeout_ms,
        "tool_timeout_ms": config.tool_timeout_ms,
        "supports_parallel_tool_calls": config.supports_parallel_tool_calls,
        "enabled_tools": config.enabled_tools,
        "disabled_tools": config.disabled_tools,
        "default_approval_mode": config.default_approval_mode,
    }
    text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _transport_payload(config: McpServerConfig) -> dict[str, object]:
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        return {
            "command": transport.command,
            "args": list(transport.args),
            "env": dict(transport.env),
            "cwd": str(transport.cwd) if transport.cwd is not None else None,
        }
    if isinstance(transport, McpStreamableHttpConfig):
        return {
            "url": transport.url,
            "bearer_token_env_var": transport.bearer_token_env_var,
            "headers": dict(transport.headers),
            "env_headers": dict(transport.env_headers),
            "follow_redirects": transport.follow_redirects,
        }
    raise TypeError(f"unsupported MCP transport: {type(transport).__name__}")
