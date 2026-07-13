"""Workspace-bound base class for built-in tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.tools.base import (
    ToolCall,
    ToolExecutionResult,
    ToolResultArtifactCandidate,
)
from pulsara_agent.primitives.tool_result import TerminalPayloadTimingFact
from pulsara_agent.primitives.context import FrozenJsonObjectFact

if TYPE_CHECKING:
    from pulsara_agent.capability.result_semantics import (
        ToolResultSemanticsRuntimeInput,
    )


@dataclass(slots=True)
class WorkspaceTool:
    """Base class for tools constrained to a workspace root."""

    workspace_root: Path

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()

    def _resolve_path(self, raw_path: str | None) -> Path:
        return self._resolve_workspace_path(raw_path)

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
        artifact_candidates: tuple[ToolResultArtifactCandidate, ...] = (),
        display_payload: FrozenJsonObjectFact | None = None,
        semantics_input: "ToolResultSemanticsRuntimeInput | None" = None,
        terminal_payload_timing: TerminalPayloadTimingFact | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=output,
            metadata=metadata or {},
            artifact_candidates=artifact_candidates,
            display_payload=display_payload,
            semantics_input=semantics_input,
            terminal_payload_timing=terminal_payload_timing,
        )
