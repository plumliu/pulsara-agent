"""Deterministic execution policy for terminal commands."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pulsara_agent.runtime.permission import (
    PermissionDecision,
    PermissionDecisionKind,
    PermissionGate,
)
from pulsara_agent.runtime.terminal.models import TerminalRequest
from pulsara_agent.tools.base import ToolCall


class ExecPolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    SUGGEST_MANAGED_TERMINAL = "suggest_managed_terminal"


@dataclass(frozen=True, slots=True)
class ExecPolicyDecision:
    kind: ExecPolicyDecisionKind
    reason: str | None = None
    effective_cwd: Path | None = None
    suggested_args: dict[str, Any] = field(default_factory=dict)
    code: str | None = None

    @classmethod
    def allow(cls, *, effective_cwd: Path) -> "ExecPolicyDecision":
        return cls(kind=ExecPolicyDecisionKind.ALLOW, effective_cwd=effective_cwd)


_PIPE_STDIN_REQUIRED_PATTERNS = [
    re.compile(r"(^|\s)gh\s+auth\s+login\s+--with-token(\s|$)"),
]

_SHELL_BACKGROUND_WRAPPER_PATTERNS = [
    re.compile(r"(?<!&)&\s*$"),
    re.compile(r"(^|\s)nohup\s+"),
    re.compile(r"(^|\s)setsid\s+"),
    re.compile(r"(^|\s)disown(\s|$)"),
]

_DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"(^|[;&|]\s*)rm\s+-[^\s]*[rR][^\s]*[fF][^\s]*(\s|$)"),
    re.compile(r"(^|[;&|]\s*)rm\s+-[^\s]*[fF][^\s]*[rR][^\s]*(\s|$)"),
    re.compile(r"(^|[;&|]\s*)sudo(\s|$)"),
    re.compile(r"(^|[;&|]\s*)chmod\s+-R(\s|$)"),
    re.compile(r"(^|[;&|]\s*)chown\s+-R(\s|$)"),
    re.compile(r"(^|[;&|]\s*)dd\s+.*\bof="),
    re.compile(r"(^|[;&|]\s*)mkfs(\.|\s|$)"),
    re.compile(r"(^|[;&|]\s*)ssh-keygen(\s|$)"),
]


class TerminalExecPolicy:
    """Deterministic floor for terminal command execution."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()

    def evaluate(self, request: TerminalRequest, *, current_cwd: Path) -> ExecPolicyDecision:
        command = request.command.strip()
        if not command:
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.BLOCK,
                reason="command must not be empty",
                code="empty_command",
            )
        if request.yield_time_ms < 0:
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.BLOCK,
                reason="yield_time_ms must be non-negative",
                code="invalid_yield_time",
            )
        if request.max_output_chars <= 0:
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.BLOCK,
                reason="max_output_chars must be positive",
                code="invalid_output_limit",
            )
        if request.max_lifetime_seconds is not None and request.max_lifetime_seconds <= 0:
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.BLOCK,
                reason="max_lifetime_seconds must be positive when provided",
                code="invalid_lifetime",
            )
        if request.tty and _matches_any(_PIPE_STDIN_REQUIRED_PATTERNS, command):
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.BLOCK,
                reason="tty mode is disabled for commands that require pipe stdin",
                code="pipe_stdin_required",
            )
        try:
            effective_cwd = self.resolve_workdir(request.workdir, current_cwd=current_cwd)
        except ValueError as exc:
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.BLOCK,
                reason=str(exc),
                code="workspace_escape",
            )
        if _matches_any(_SHELL_BACKGROUND_WRAPPER_PATTERNS, command):
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.SUGGEST_MANAGED_TERMINAL,
                reason="shell-level background wrappers should use terminal yield semantics instead",
                effective_cwd=effective_cwd,
                suggested_args={"yield_time_ms": 0},
                code="use_terminal_yield",
            )
        if _matches_any(_DANGEROUS_COMMAND_PATTERNS, command):
            return ExecPolicyDecision(
                kind=ExecPolicyDecisionKind.REQUIRE_CONFIRMATION,
                reason="terminal command requires user confirmation before execution",
                effective_cwd=effective_cwd,
                code="requires_confirmation",
            )
        return ExecPolicyDecision.allow(effective_cwd=effective_cwd)

    def resolve_workdir(self, workdir: str | None, *, current_cwd: Path) -> Path:
        normalized_workdir = workdir.strip() if workdir is not None else ""
        if not normalized_workdir or normalized_workdir in {".", "./"}:
            return self._recover_current_cwd(current_cwd)
        raw = Path(normalized_workdir).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace_root / raw
        resolved = candidate.resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError(f"workdir escapes workspace root: {workdir}")
        if not resolved.exists():
            raise ValueError(f"workdir does not exist: {workdir}")
        if not resolved.is_dir():
            raise ValueError(f"workdir is not a directory: {workdir}")
        return resolved

    def _recover_current_cwd(self, current_cwd: Path) -> Path:
        candidates = [current_cwd.expanduser(), *current_cwd.expanduser().parents]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not _is_within_workspace(resolved, self.workspace_root):
                continue
            if resolved.exists() and resolved.is_dir():
                return resolved
            if resolved == self.workspace_root:
                break
        return self.workspace_root


class TerminalPolicyPermissionGate:
    """Permission gate wrapper that escalates risky terminal commands."""

    def __init__(self, inner: PermissionGate) -> None:
        self.inner = inner

    async def evaluate(self, calls: list[ToolCall]) -> PermissionDecision:
        base = await self.inner.evaluate(calls)
        if base.kind is not PermissionDecisionKind.ALLOW:
            return base
        for call in calls:
            if call.name != "terminal":
                continue
            command = call.arguments.get("command")
            if not isinstance(command, str):
                continue
            if _matches_any(_DANGEROUS_COMMAND_PATTERNS, command.strip()):
                return PermissionDecision(
                    kind=PermissionDecisionKind.WAIT_FOR_USER,
                    reason="terminal command requires user confirmation before execution",
                    suggested_rules=[
                        {
                            "tool": "terminal",
                            "reason": "dangerous_terminal_command",
                            "command": command,
                        }
                    ],
                )
        return base


def _matches_any(patterns: list[re.Pattern[str]], command: str) -> bool:
    return any(pattern.search(command) for pattern in patterns)


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    return path == workspace_root or workspace_root in path.parents
