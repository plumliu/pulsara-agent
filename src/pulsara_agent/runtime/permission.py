"""Permission-gate skeleton for the main agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pulsara_agent.tools.base import ToolCall


class PermissionDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    WAIT_FOR_USER = "wait_for_user"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    kind: PermissionDecisionKind
    reason: str | None = None
    suggested_rules: list[dict] = field(default_factory=list)

    @classmethod
    def allow(cls) -> "PermissionDecision":
        return cls(kind=PermissionDecisionKind.ALLOW)


class PermissionGate(Protocol):
    async def evaluate(self, calls: list[ToolCall]) -> PermissionDecision: ...


class AllowAllPermissionGate:
    async def evaluate(self, calls: list[ToolCall]) -> PermissionDecision:
        return PermissionDecision.allow()
