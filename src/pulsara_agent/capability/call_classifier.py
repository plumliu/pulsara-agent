"""Per-call effective classification for capability permission decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pulsara_agent.capability.descriptor import CapabilityDescriptor


TERMINAL_PROCESS_OBSERVE_ACTIONS = frozenset({"list", "log", "poll", "wait"})
TERMINAL_MONITOR_OBSERVE_ACTIONS = frozenset({"list"})


@dataclass(frozen=True, slots=True)
class CapabilityCallClassification:
    descriptor_id: str
    tool_name: str
    effective_read_only: bool
    effective_concurrency_safe: bool
    effective_permission_category: str
    effective_is_destructive: bool
    effective_is_open_world: bool
    approval_reason: str | None = None
    deny_reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "descriptor_id": self.descriptor_id,
            "tool_name": self.tool_name,
            "effective_read_only": self.effective_read_only,
            "effective_concurrency_safe": self.effective_concurrency_safe,
            "effective_permission_category": self.effective_permission_category,
            "effective_is_destructive": self.effective_is_destructive,
            "effective_is_open_world": self.effective_is_open_world,
            "approval_reason": self.approval_reason,
            "deny_reason": self.deny_reason,
            "metadata": self.metadata,
        }


class CapabilityCallClassifier(Protocol):
    def classify(
        self,
        call: Any,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityCallClassification: ...


class DefaultCapabilityCallClassifier:
    def classify(
        self,
        call: Any,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityCallClassification:
        if (
            call.name == "terminal_process"
            and _terminal_process_action(call) in TERMINAL_PROCESS_OBSERVE_ACTIONS
        ):
            return CapabilityCallClassification(
                descriptor_id=descriptor.id,
                tool_name=call.name,
                effective_read_only=True,
                effective_concurrency_safe=descriptor.is_concurrency_safe,
                effective_permission_category="terminal_process_observe",
                effective_is_destructive=False,
                effective_is_open_world=descriptor.is_open_world,
                metadata={"action": _terminal_process_action(call)},
            )
        if (
            call.name == "terminal_monitor"
            and _terminal_process_action(call) in TERMINAL_MONITOR_OBSERVE_ACTIONS
        ):
            return CapabilityCallClassification(
                descriptor_id=descriptor.id,
                tool_name=call.name,
                effective_read_only=True,
                effective_concurrency_safe=descriptor.is_concurrency_safe,
                effective_permission_category="terminal_monitor_observe",
                effective_is_destructive=False,
                effective_is_open_world=descriptor.is_open_world,
                metadata={"action": _terminal_process_action(call)},
            )
        return CapabilityCallClassification(
            descriptor_id=descriptor.id,
            tool_name=call.name,
            effective_read_only=descriptor.is_read_only,
            effective_concurrency_safe=descriptor.is_concurrency_safe,
            effective_permission_category=descriptor.permission_category,
            effective_is_destructive=descriptor.is_destructive,
            effective_is_open_world=descriptor.is_open_world,
        )


def _terminal_process_action(call: Any) -> str | None:
    action = call.arguments.get("action")
    return action if isinstance(action, str) else None
