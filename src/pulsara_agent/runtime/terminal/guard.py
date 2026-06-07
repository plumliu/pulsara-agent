"""Pre-execution validation and guardrails for terminal runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.runtime.terminal.models import TerminalRequest


_LONG_RUNNING_PATTERNS = [
    re.compile(r"(^|\s)npm\s+run\s+dev(\s|$)"),
    re.compile(r"(^|\s)pnpm\s+dev(\s|$)"),
    re.compile(r"(^|\s)yarn\s+dev(\s|$)"),
    re.compile(r"(^|\s)vite(\s|$)"),
    re.compile(r"(^|\s)uvicorn(\s|$)"),
    re.compile(r"(^|\s)python(\d+(\.\d+)?)?\s+-m\s+http\.server(\s|$)"),
    re.compile(r"(^|\s)tail\s+-f(\s|$)"),
    re.compile(r"(^|\s)watch(\s|$)"),
]


@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    error: str | None = None
    effective_cwd: Path | None = None


class CommandGuard:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def validate(self, request: TerminalRequest, *, current_cwd: Path) -> GuardDecision:
        command = request.command.strip()
        if not command:
            return GuardDecision(allowed=False, error="command must not be empty")
        if request.timeout_seconds <= 0:
            return GuardDecision(allowed=False, error="timeout_seconds must be positive")
        if request.max_output_chars <= 0:
            return GuardDecision(allowed=False, error="max_output_chars must be positive")
        for pattern in _LONG_RUNNING_PATTERNS:
            if pattern.search(command):
                return GuardDecision(
                    allowed=False,
                    error=(
                        "foreground long-running commands are blocked in terminal MVP; "
                        "background execution is not implemented yet"
                    ),
                )

        try:
            effective_cwd = self.resolve_workdir(request.workdir, current_cwd=current_cwd)
        except ValueError as exc:
            return GuardDecision(allowed=False, error=str(exc))
        return GuardDecision(allowed=True, effective_cwd=effective_cwd)

    def resolve_workdir(self, workdir: str | None, *, current_cwd: Path) -> Path:
        if workdir is None or not workdir.strip():
            return current_cwd
        raw = Path(workdir).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace_root / raw
        resolved = candidate.resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError(f"workdir escapes workspace root: {workdir}")
        if not resolved.exists():
            raise ValueError(f"workdir does not exist: {workdir}")
        if not resolved.is_dir():
            raise ValueError(f"workdir is not a directory: {workdir}")
        return resolved

