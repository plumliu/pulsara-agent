import asyncio

import pytest

from pulsara_agent.capability.management_effects import (
    ResolvedCapabilityEffectProjection,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
    ToolDispatchAuthorizationRequest,
)
from pulsara_agent.primitives.run_permission import (
    PermissionMode,
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize(
    "effects",
    [
        ResolvedCapabilityEffectProjection(workspace_write=True),
        ResolvedCapabilityEffectProjection(outside_workspace_write=True),
        ResolvedCapabilityEffectProjection(
            workspace_write=True, outside_workspace_write=True
        ),
        ResolvedCapabilityEffectProjection(workspace_write=True, process_control=True),
        ResolvedCapabilityEffectProjection(
            outside_workspace_write=True, process_control=True
        ),
    ],
)
def test_resolved_physical_effects_use_existing_four_permission_modes(
    tmp_path, mode, effects
):
    async def run():
        permission = build_run_permission_snapshot(
            snapshot_id="permission:capability",
            requested_mode=mode,
            effective_mode=mode,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        result = await DefaultToolDispatchAuthorizationPolicy().decide(
            ToolDispatchAuthorizationRequest(
                "manage_capability",
                "call:capability",
                # These raw labels must not relax the owner-resolved effects.
                {"scope": "WORKSPACE", "path": str(tmp_path / "safe"), "trusted": True},
                "turn:capability",
                "entry:capability",
                permission,
                tmp_path,
                effects,
            )
        )
        if mode is PermissionMode.READ_ONLY:
            expected = "DENY"
        elif mode is PermissionMode.BYPASS_PERMISSIONS:
            expected = "ALLOW"
        elif (
            mode is PermissionMode.ACCEPT_EDITS
            and not effects.outside_workspace_write
            and not effects.process_control
        ):
            expected = "ALLOW"
        else:
            expected = "REQUIRE_CONFIRMATION"
        assert result.kind.value == expected

    asyncio.run(run())


def test_model_arguments_cannot_supply_preparation_or_claim_write_authority(tmp_path):
    async def run():
        mode = PermissionMode.BYPASS_PERMISSIONS
        permission = build_run_permission_snapshot(
            snapshot_id="permission:capability",
            requested_mode=mode,
            effective_mode=mode,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        result = await DefaultToolDispatchAuthorizationPolicy().decide(
            ToolDispatchAuthorizationRequest(
                "manage_capability",
                "call:capability",
                {
                    "capability_effects": {"workspace_write": True},
                    "path": str(tmp_path),
                },
                "turn:capability",
                "entry:capability",
                permission,
                tmp_path,
            )
        )
        assert result.kind.value == "DENY"

    asyncio.run(run())


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_native_inspection_is_read_only_in_every_permission_mode(tmp_path, mode):
    async def run():
        permission = build_run_permission_snapshot(
            snapshot_id="permission:read",
            requested_mode=mode,
            effective_mode=mode,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        effects = ResolvedCapabilityEffectProjection()
        assert effects.read_only
        result = await DefaultToolDispatchAuthorizationPolicy().decide(
            ToolDispatchAuthorizationRequest(
                "manage_capability",
                "call:read",
                {"action": "INSPECT_LOOSE_SKILLS", "scope": "USER"},
                "turn:read",
                "entry:read",
                permission,
                tmp_path,
                effects,
            )
        )
        assert result.kind.value == "ALLOW"

    asyncio.run(run())
