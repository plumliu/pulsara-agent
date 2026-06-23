"""Workspace-bound base class for built-in tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolResultArtifactCandidate


@dataclass(slots=True)
class WorkspaceTool:
    """Base class for tools constrained to a workspace root."""

    workspace_root: Path

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()

    def _resolve_path(self, raw_path: str | None) -> Path:
        if not raw_path or not raw_path.strip():
            raise ValueError("path is required")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        resolved = path.resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError(f"path escapes workspace root: {raw_path}")
        return resolved

    def _result(
        self,
        call: ToolCall,
        *,
        status: ToolResultState,
        output: str,
        metadata: dict[str, Any] | None = None,
        artifact_candidates: tuple[ToolResultArtifactCandidate, ...] = (),
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=output,
            metadata=metadata or {},
            artifact_candidates=artifact_candidates,
        )
