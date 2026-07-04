"""Pre-execution validation and guardrails for terminal runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pulsara_agent.runtime.terminal.models import TerminalRequest
from pulsara_agent.runtime.terminal.policy import ExecPolicyDecisionKind, TerminalExecPolicy


@dataclass(frozen=True, slots=True)
class GuardDecision:
    allowed: bool
    error: str | None = None
    effective_cwd: Path | None = None
    suggested_args: dict[str, Any] = field(default_factory=dict)
    code: str | None = None


class CommandGuard:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.policy = TerminalExecPolicy(workspace_root)

    def validate(self, request: TerminalRequest, *, current_cwd: Path) -> GuardDecision:
        decision = self.policy.evaluate(request, current_cwd=current_cwd)
        if decision.kind is ExecPolicyDecisionKind.ALLOW:
            return GuardDecision(allowed=True, effective_cwd=decision.effective_cwd)
        return GuardDecision(
            allowed=False,
            error=decision.reason,
            effective_cwd=decision.effective_cwd,
            suggested_args=decision.suggested_args,
            code=decision.code,
        )

    def resolve_workdir(self, workdir: str | None, *, current_cwd: Path) -> Path:
        return self.policy.resolve_workdir(workdir, current_cwd=current_cwd)
