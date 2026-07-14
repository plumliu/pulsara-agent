"""Run-bound permission snapshot helpers.

The production runtime only supports preset permission modes as run contracts.
Custom ``EffectivePermissionPolicy`` values may still be used by component
tests, but they must not cross the AgentRuntime / HostSession / event-log
boundary as a run permission snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pulsara_agent.runtime.permission import (
    EffectivePermissionPolicy,
    PermissionState,
    mode_for_policy,
    preset_to_policy,
)
from pulsara_agent.primitives.permission import PermissionMode, parse_permission_mode
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    RunPermissionSnapshotFact,
    context_fingerprint,
    freeze_json,
)

RunPermissionSnapshotSource = Literal["session_default", "plan_mode", "child_profile"]


@dataclass(frozen=True, slots=True)
class RunPermissionSnapshot:
    snapshot_id: str
    runtime_session_id: str
    run_id: str
    permission_mode: PermissionMode
    permission_policy: dict[str, object]
    permission_snapshot_source: RunPermissionSnapshotSource

    def to_event_fields(self) -> dict[str, object]:
        return {
            "permission_snapshot_id": self.snapshot_id,
            "permission_mode": self.permission_mode.value,
            "permission_policy": dict(self.permission_policy),
            "permission_snapshot_source": self.permission_snapshot_source,
        }

    def to_tool_runtime_fields(self) -> dict[str, object]:
        return self.to_event_fields()

    def to_permission_state(self) -> PermissionState:
        return PermissionState(
            policy=preset_to_policy(self.permission_mode),
            mode=self.permission_mode,
        )

    def to_context_fact(self) -> RunPermissionSnapshotFact:
        expanded = freeze_json(self.permission_policy)
        if not isinstance(expanded, FrozenJsonObjectFact):
            raise AssertionError("permission policy must freeze as a JSON object")
        payload = {
            "snapshot_id": self.snapshot_id,
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "mode": self.permission_mode,
            "expanded_policy": expanded,
            "expanded_policy_fingerprint": context_fingerprint(
                "run-permission-expanded-policy:v1", expanded
            ),
            "source": self.permission_snapshot_source,
            "plan_restriction_active": self.permission_snapshot_source == "plan_mode",
        }
        provisional = RunPermissionSnapshotFact.model_construct(
            **payload,
            fingerprint="pending",
        )
        fingerprint_payload = provisional.model_dump(
            mode="json", exclude={"fingerprint"}
        )
        return RunPermissionSnapshotFact(
            **payload,
            fingerprint=context_fingerprint(
                "run-permission-snapshot:v1", fingerprint_payload
            ),
        )


def require_preset_permission_mode_for_policy(
    policy: EffectivePermissionPolicy,
    *,
    context: str,
) -> PermissionMode:
    mode = mode_for_policy(policy)
    if mode is None:
        raise ValueError(
            f"{context} requires a preset permission mode; custom policies are not supported"
        )
    validate_preset_policy_payload(mode, policy.to_dict(), context=context)
    return mode


def validate_preset_policy_payload(
    mode: str | PermissionMode,
    policy_payload: dict[str, Any],
    *,
    context: str,
) -> PermissionMode:
    parsed = parse_permission_mode(mode)
    expected = preset_to_policy(parsed).to_dict()
    if dict(policy_payload) != expected:
        raise ValueError(
            f"{context} permission_policy must equal preset_to_policy({parsed.value!r}).to_dict()"
        )
    return parsed


def snapshot_from_mode(
    *,
    runtime_session_id: str,
    run_id: str,
    permission_mode: str | PermissionMode,
    permission_snapshot_source: RunPermissionSnapshotSource,
) -> RunPermissionSnapshot:
    mode = parse_permission_mode(permission_mode)
    return RunPermissionSnapshot(
        snapshot_id=f"permission_snapshot:{run_id}",
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        permission_mode=mode,
        permission_policy=preset_to_policy(mode).to_dict(),
        permission_snapshot_source=permission_snapshot_source,
    )


def snapshot_from_run_start_event(
    event: Any,
    *,
    runtime_session_id: str,
) -> RunPermissionSnapshot:
    mode = validate_preset_policy_payload(
        getattr(event, "permission_mode"),
        dict(getattr(event, "permission_policy")),
        context="RunStartEvent",
    )
    source = getattr(event, "permission_snapshot_source")
    if source not in {"session_default", "plan_mode", "child_profile"}:
        raise ValueError(
            f"invalid RunStartEvent permission_snapshot_source: {source!r}"
        )
    return RunPermissionSnapshot(
        snapshot_id=str(getattr(event, "permission_snapshot_id")),
        runtime_session_id=runtime_session_id,
        run_id=str(getattr(event, "run_id")),
        permission_mode=mode,
        permission_policy=preset_to_policy(mode).to_dict(),
        permission_snapshot_source=source,
    )
