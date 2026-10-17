"""Advisory, process-local facts from settled capability-page mutations."""

from dataclasses import dataclass
import json
from typing import Literal

from .management_intent import CapabilityManagementAction, CapabilityManagementScope
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class CapabilityUiChange:
    action: CapabilityManagementAction | Literal["SAVE_HOOK_CONFIG"]
    scope: CapabilityManagementScope
    target: FrozenJsonObjectFact
    result: str
    enabled: bool | None = None

    @classmethod
    def settled(cls, action, scope, target, result, *, enabled=None):
        # This constructor is server-only. Never accept request/config/result dicts
        # wholesale: only public target identity crosses into the short notice.
        allowed = {
            "kind",
            "scope",
            "skill_path",
            "plugin_id",
            "server_id",
            "source_kind",
        }
        if set(target) - allowed:
            raise ValueError("capability notice target is not a public identity")
        if target.get("kind") != "SKILL":
            target = {**target, "scope": str(scope)}
        return cls(
            CapabilityManagementAction(action)
            if action != "SAVE_HOOK_CONFIG"
            else action,
            CapabilityManagementScope(scope),
            freeze_json(target),
            result,
            enabled,
        )


@dataclass(frozen=True, slots=True)
class FrozenCapabilityChange:
    change: CapabilityUiChange
    revision: int
    changes: int
    adoption_attention: str | None = None

    def render(self) -> str:
        value = {
            "source": "CAPABILITY_UI",
            "scope": self.change.scope.value,
            "action": str(self.change.action),
            "target": thaw_json(self.change.target),
            "result": self.change.result,
        }
        if self.change.enabled is not None:
            value["enabled"] = self.change.enabled
        if self.changes > 1:
            value["changes"] = self.changes
        if self.adoption_attention:
            value.update(adoption="PARTIAL", diagnostic=self.adoption_attention)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
