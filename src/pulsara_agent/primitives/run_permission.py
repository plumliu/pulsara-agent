"""Immutable, provider-neutral run permission snapshots.

The snapshot is the only production permission authority for one accepted
turn.  It deliberately carries no policy object, callback, repository, or
transport capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json

from pulsara_agent.primitives.permission import (
    PERMISSION_PRESET_CONTRACT_FINGERPRINT,
    PERMISSION_PRESET_CONTRACT_ID,
    PermissionMode,
)


class RunPermissionAdmissionSource(StrEnum):
    USER_SUBMISSION = "USER_SUBMISSION"
    EXTERNAL_RESULT_COMMAND = "EXTERNAL_RESULT_COMMAND"
    TERMINAL_OBSERVATION = "TERMINAL_OBSERVATION"
    SUBAGENT_INHERITANCE = "SUBAGENT_INHERITANCE"
    RUNTIME_PLAN_CONTINUATION = "RUNTIME_PLAN_CONTINUATION"


class RunPermissionOverlay(StrEnum):
    NONE = "NONE"
    PLAN_READ_ONLY = "PLAN_READ_ONLY"


@dataclass(frozen=True, slots=True)
class FrozenRunPermissionSnapshot:
    snapshot_id: str
    requested_mode: PermissionMode
    effective_mode: PermissionMode
    admission_source: RunPermissionAdmissionSource
    overlay: RunPermissionOverlay
    plan_context_ordinal_at_admission: int
    plan_workflow_id: str | None
    plan_workflow_revision_at_admission: int | None
    inherited_from_turn_id: str | None
    permission_contract_id: str
    permission_contract_fingerprint: str
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("permission snapshot identity is required")
        if self.plan_context_ordinal_at_admission < 0:
            raise ValueError("permission plan-context ordinal must be non-negative")
        if self.permission_contract_id != PERMISSION_PRESET_CONTRACT_ID:
            raise ValueError("permission preset contract is unavailable")
        if (
            self.permission_contract_fingerprint
            != PERMISSION_PRESET_CONTRACT_FINGERPRINT
        ):
            raise ValueError("permission preset contract fingerprint is unavailable")
        plan_bound = self.overlay is RunPermissionOverlay.PLAN_READ_ONLY
        if plan_bound != (
            self.plan_workflow_id is not None
            and self.plan_workflow_revision_at_admission is not None
        ):
            raise ValueError("permission Plan overlay identity is invalid")
        if plan_bound:
            if self.effective_mode is not PermissionMode.READ_ONLY:
                raise ValueError("Plan overlay must force read-only permission")
            if (
                self.plan_context_ordinal_at_admission < 1
                or self.plan_workflow_revision_at_admission is None
                or self.plan_workflow_revision_at_admission < 1
            ):
                raise ValueError("Plan permission cut is invalid")
        elif self.effective_mode is not self.requested_mode:
            raise ValueError(
                "permission without an overlay cannot change the requested mode"
            )
        if self.admission_source is RunPermissionAdmissionSource.SUBAGENT_INHERITANCE:
            if self.inherited_from_turn_id is None or plan_bound:
                raise ValueError("subagent permission inheritance is invalid")
        elif self.admission_source in {
            RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION,
            RunPermissionAdmissionSource.TERMINAL_OBSERVATION,
        }:
            if self.inherited_from_turn_id is None:
                raise ValueError("derived permission must identify its origin turn")
        elif self.inherited_from_turn_id is not None:
            raise ValueError("permission inheritance is not allowed for this source")
        expected = run_permission_snapshot_fingerprint(
            snapshot_id=self.snapshot_id,
            requested_mode=self.requested_mode,
            effective_mode=self.effective_mode,
            admission_source=self.admission_source,
            overlay=self.overlay,
            plan_context_ordinal_at_admission=(
                self.plan_context_ordinal_at_admission
            ),
            plan_workflow_id=self.plan_workflow_id,
            plan_workflow_revision_at_admission=(
                self.plan_workflow_revision_at_admission
            ),
            inherited_from_turn_id=self.inherited_from_turn_id,
            permission_contract_id=self.permission_contract_id,
            permission_contract_fingerprint=self.permission_contract_fingerprint,
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("permission snapshot fingerprint mismatch")


def run_permission_snapshot_fingerprint(
    *,
    snapshot_id: str,
    requested_mode: PermissionMode,
    effective_mode: PermissionMode,
    admission_source: RunPermissionAdmissionSource,
    overlay: RunPermissionOverlay,
    plan_context_ordinal_at_admission: int,
    plan_workflow_id: str | None,
    plan_workflow_revision_at_admission: int | None,
    inherited_from_turn_id: str | None,
    permission_contract_id: str = PERMISSION_PRESET_CONTRACT_ID,
    permission_contract_fingerprint: str = (
        PERMISSION_PRESET_CONTRACT_FINGERPRINT
    ),
) -> str:
    payload = {
        "snapshot_id": snapshot_id,
        "requested_mode": requested_mode.value,
        "effective_mode": effective_mode.value,
        "admission_source": admission_source.value,
        "overlay": overlay.value,
        "plan_context_ordinal_at_admission": plan_context_ordinal_at_admission,
        "plan_workflow_id": plan_workflow_id,
        "plan_workflow_revision_at_admission": (
            plan_workflow_revision_at_admission
        ),
        "inherited_from_turn_id": inherited_from_turn_id,
        "permission_contract_id": permission_contract_id,
        "permission_contract_fingerprint": permission_contract_fingerprint,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(
        b"pulsara:run-permission-snapshot:v1\0" + encoded
    ).hexdigest()


def build_run_permission_snapshot(
    *,
    snapshot_id: str,
    requested_mode: PermissionMode,
    effective_mode: PermissionMode,
    admission_source: RunPermissionAdmissionSource,
    overlay: RunPermissionOverlay = RunPermissionOverlay.NONE,
    plan_context_ordinal_at_admission: int = 0,
    plan_workflow_id: str | None = None,
    plan_workflow_revision_at_admission: int | None = None,
    inherited_from_turn_id: str | None = None,
) -> FrozenRunPermissionSnapshot:
    fingerprint = run_permission_snapshot_fingerprint(
        snapshot_id=snapshot_id,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        admission_source=admission_source,
        overlay=overlay,
        plan_context_ordinal_at_admission=plan_context_ordinal_at_admission,
        plan_workflow_id=plan_workflow_id,
        plan_workflow_revision_at_admission=plan_workflow_revision_at_admission,
        inherited_from_turn_id=inherited_from_turn_id,
    )
    return FrozenRunPermissionSnapshot(
        snapshot_id=snapshot_id,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        admission_source=admission_source,
        overlay=overlay,
        plan_context_ordinal_at_admission=plan_context_ordinal_at_admission,
        plan_workflow_id=plan_workflow_id,
        plan_workflow_revision_at_admission=(
            plan_workflow_revision_at_admission
        ),
        inherited_from_turn_id=inherited_from_turn_id,
        permission_contract_id=PERMISSION_PRESET_CONTRACT_ID,
        permission_contract_fingerprint=PERMISSION_PRESET_CONTRACT_FINGERPRINT,
        snapshot_fingerprint=fingerprint,
    )


__all__ = [
    "FrozenRunPermissionSnapshot",
    "RunPermissionAdmissionSource",
    "RunPermissionOverlay",
    "build_run_permission_snapshot",
    "run_permission_snapshot_fingerprint",
]
