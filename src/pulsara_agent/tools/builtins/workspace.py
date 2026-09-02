"""Workspace-bound base class for built-in tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    PulsaraHomeResolutionError,
    UserHomeResolution,
    UserHomeResolutionError,
    resolve_pulsara_home,
    resolve_user_home,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
)


_PULSARA_HOME_READ_PREFIX = "${PULSARA_HOME}"


class WritePathScope(StrEnum):
    WORKSPACE = "workspace"
    HOST_LOCAL = "host_local"


@dataclass(slots=True)
class WorkspaceTool:
    """Base class for tools constrained to a workspace root."""

    workspace_root: Path
    pulsara_home_resolution: PulsaraHomeResolution | None = None
    user_home_resolution: UserHomeResolution | None = None

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if self.user_home_resolution is None:
            self.user_home_resolution = resolve_user_home()
        if self.pulsara_home_resolution is None:
            self.pulsara_home_resolution = resolve_pulsara_home(
                user_home_resolution=self.user_home_resolution
            )

    def _resolve_path(
        self,
        raw_path: str | None,
        *,
        write_scope: WritePathScope = WritePathScope.WORKSPACE,
    ) -> Path:
        if write_scope is WritePathScope.WORKSPACE:
            return self._resolve_workspace_path(raw_path)
        if write_scope is not WritePathScope.HOST_LOCAL:
            raise ValueError("write path scope is invalid")
        if not raw_path or not raw_path.strip():
            raise ValueError("path is required")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    def _resolved_user_home(self) -> Path | None:
        resolution = self.user_home_resolution
        if (
            resolution is None
            or resolution.disposition is not PulsaraHomeDisposition.RESOLVED
        ):
            return None
        return resolution.path

    def _resolve_workspace_path(self, raw_path: str | None) -> Path:
        if not raw_path or not raw_path.strip():
            raise ValueError("path is required")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        resolved = path.resolve()
        if (
            resolved != self.workspace_root
            and self.workspace_root not in resolved.parents
        ):
            raise ValueError(f"path escapes workspace root: {raw_path}")
        return resolved

    def _resolve_read_path(self, raw_path: str | None) -> Path:
        if not raw_path or not raw_path.strip():
            raise ValueError("path is required")
        raw = raw_path.strip()
        if raw == _PULSARA_HOME_READ_PREFIX or raw.startswith(
            _PULSARA_HOME_READ_PREFIX + "/"
        ):
            resolution = self.pulsara_home_resolution
            if resolution is None:  # pragma: no cover - frozen in post-init
                raise RuntimeError("Pulsara home binding is absent")
            if resolution.disposition is not PulsaraHomeDisposition.RESOLVED:
                raise PulsaraHomeResolutionError(resolution)
            base = resolution.path
            if base is None:  # pragma: no cover - closed resolution invariant
                raise RuntimeError("Pulsara home binding has no path")
            suffix = raw[len(_PULSARA_HOME_READ_PREFIX) :].lstrip("/")
            return (base / suffix).resolve()
        if raw == "~" or raw.startswith("~/"):
            resolution = self.user_home_resolution
            if resolution is None:  # pragma: no cover - frozen in post-init
                raise RuntimeError("user home binding is absent")
            if resolution.disposition is not PulsaraHomeDisposition.RESOLVED:
                raise UserHomeResolutionError(resolution)
            if resolution.path is None:  # pragma: no cover - closed invariant
                raise RuntimeError("user home binding has no path")
            suffix = raw.removeprefix("~").lstrip("/")
            return (resolution.path / suffix).resolve()
        if raw.startswith("~"):
            return Path(raw).expanduser().resolve()
        path = Path(raw)
        if path.is_absolute():
            return path.expanduser().resolve()
        return self._resolve_workspace_path(raw)

    def _result(
        self,
        call: ToolCall,
        *,
        status: ToolResultState,
        output: str,
        metadata: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=output,
            metadata=metadata or {},
        )
