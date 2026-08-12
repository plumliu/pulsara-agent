"""The single Stage 2 tool-dispatch authorization policy owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol

from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.tool_permission import (
    AllowAllPermissionGate,
    PermissionDecisionKind,
    PolicyPermissionGate,
    preset_to_policy,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot


class ToolDispatchDecisionKind(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


@dataclass(frozen=True, slots=True)
class ToolDispatchAuthorizationRequest:
    tool_name: str
    tool_call_id: str
    arguments: Mapping[str, object]
    turn_id: str
    assistant_entry_id: str
    permission_snapshot: FrozenRunPermissionSnapshot


@dataclass(frozen=True, slots=True)
class ToolDispatchAuthorizationDecision:
    kind: ToolDispatchDecisionKind
    reference: str
    public_message: str


class ToolDispatchAuthorizationPolicy(Protocol):
    async def decide(
        self, request: ToolDispatchAuthorizationRequest
    ) -> ToolDispatchAuthorizationDecision: ...


class DefaultToolDispatchAuthorizationPolicy:
    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if not 0 < timeout_seconds <= 5.0:
            raise ValueError("tool authorization timeout exceeds its hard cap")
        self._timeout_seconds = timeout_seconds

    async def decide(
        self, request: ToolDispatchAuthorizationRequest
    ) -> ToolDispatchAuthorizationDecision:
        call = ToolCall(
            id=request.tool_call_id,
            name=request.tool_name,
            arguments=dict(request.arguments),
        )
        gate = PolicyPermissionGate(
            preset_to_policy(request.permission_snapshot.effective_mode),
            AllowAllPermissionGate(),
        )
        try:
            decision = await asyncio.wait_for(
                gate.evaluate([call]), timeout=self._timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return ToolDispatchAuthorizationDecision(
                ToolDispatchDecisionKind.REQUIRE_CONFIRMATION,
                "tool-policy:unavailable",
                "tool authorization is unavailable and requires confirmation",
            )
        if decision.kind is PermissionDecisionKind.ALLOW:
            kind = ToolDispatchDecisionKind.ALLOW
        elif decision.kind is PermissionDecisionKind.DENY:
            kind = ToolDispatchDecisionKind.DENY
        elif decision.kind is PermissionDecisionKind.WAIT_FOR_USER:
            kind = ToolDispatchDecisionKind.REQUIRE_CONFIRMATION
        else:  # pragma: no cover - legacy permission vocabulary is closed
            raise RuntimeError("permission decision vocabulary is invalid")
        return ToolDispatchAuthorizationDecision(
            kind,
            f"permission:{decision.kind.value}",
            decision.reason or kind.value.lower().replace("_", " "),
        )


__all__ = [
    "DefaultToolDispatchAuthorizationPolicy",
    "ToolDispatchAuthorizationDecision",
    "ToolDispatchAuthorizationPolicy",
    "ToolDispatchAuthorizationRequest",
    "ToolDispatchDecisionKind",
]
