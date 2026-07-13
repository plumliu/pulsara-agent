from __future__ import annotations

import builtins
from uuid import uuid4

import pytest

from pulsara_agent.primitives.permission import PermissionMode, parse_permission_mode
from pulsara_agent.primitives.capability import (
    build_capability_execution_surface_identity,
    build_capability_resolve_basis,
)
from pulsara_agent.primitives.model_call import ModelTokenUsageFact, sha256_fingerprint
from pulsara_agent.primitives.run_boundary import (
    BoundaryTranscriptSnapshotFact,
    NewRunBoundaryFact,
)
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    HostRunBoundaryIdentityFact,
    SubagentRunEntryFact,
    text_sha256,
)
from pulsara_agent.primitives.subagent import (
    ChildExplicitResultEvidenceFact,
    ChildNativeTerminalReferenceFact,
    build_child_result_handoff,
    build_child_result_render_policy,
)
from pulsara_agent.runtime.permission import (
    preset_to_policy,
)
from pulsara_agent.capability.result_semantics import (
    build_unknown_result_semantics,
)
from pulsara_agent.capability.result_contracts import generic_result_render_contract
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import (
    ExternalExecutionRequirementReferenceFact,
    ExternalToolCallRequirementFact,
    ExternalToolResultIngressFact,
    FrozenToolResultBlockFact,
    ToolResultExecutionSemanticsFact,
    ToolResultRenderProfileFact,
    ToolResultRenderVariantCode,
    ToolResultStateFact,
)
from pulsara_agent.primitives.context import (
    CapabilityDescriptorRenderAttributionFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.message import ToolResultBlock, ToolResultState
from tests.support import test_resolved_target_fact


def tool_result_end_contract_fields(
    tool_call_id: str,
    *,
    tool_name: str = "test_tool",
    state: ToolResultState | str = ToolResultState.SUCCESS,
    observed_at_utc: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    parsed_state = (
        state if isinstance(state, ToolResultState) else ToolResultState(state)
    )
    semantics = build_unknown_result_semantics(
        result_state=ToolResultStateFact(parsed_state.value)
    )
    return {
        "observation_timing": ToolObservationTimingFact(
            observed_at_utc=observed_at_utc,
            source_started_at_utc=observed_at_utc,
            source_ended_at_utc=observed_at_utc,
            observation_duration_seconds=0,
            freshness="current_tool_observation",
            clock_source="tool_result_events",
            tool_origin="unknown",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        ),
        "render_profile": semantics.render_profile,
        "essential_capture_policy": semantics.essential_capture_policy,
        "essential_result": semantics.essential_result,
        "terminal_payload_timing": semantics.terminal_payload_timing,
    }


def external_tool_call_requirement_fact(
    tool_call_id: str,
    *,
    tool_name: str,
    raw_arguments_json: str = "{}",
) -> ExternalToolCallRequirementFact:
    contract = generic_result_render_contract()
    attribution_payload = {
        "owner_runtime_session_id": "runtime:test",
        "exposure_id": "capability-exposure:test",
        "exposure_fact_fingerprint": "exposure-fact:test",
        "descriptor_set_fingerprint": "descriptor-set:test",
        "descriptor_id": f"descriptor:test:{tool_name}",
        "descriptor_fingerprint": f"descriptor-fingerprint:test:{tool_name}",
        "result_render_contract_fingerprint": contract.contract_fingerprint,
        "descriptor_source_event_id": "capability-exposure-event:test",
        "descriptor_source_sequence": 1,
        "descriptor_source_payload_fingerprint": "sha256:" + "1" * 64,
    }
    attribution = CapabilityDescriptorRenderAttributionFact(
        **attribution_payload,
        attribution_fingerprint=context_fingerprint(
            "capability-descriptor-render-attribution:v1", attribution_payload
        ),
    )
    payload = {
        "tool_call_id": tool_call_id,
        "model_tool_name": tool_name,
        "raw_arguments_json": raw_arguments_json,
        "tool_origin": "custom",
        "descriptor_attribution": attribution,
        "result_render_contract": contract,
        "essential_capture_policy": None,
    }
    return ExternalToolCallRequirementFact(
        **payload,
        requirement_fingerprint=context_fingerprint(
            "external-tool-call-requirement:v1", payload
        ),
    )


def external_tool_result_ingress_fact(
    result: ToolResultBlock,
    *,
    requirement: ExternalToolCallRequirementFact | None = None,
    require_event_id: str = "require-external:test",
    require_event_sequence: int = 1,
) -> ExternalToolResultIngressFact:
    requirement = requirement or external_tool_call_requirement_fact(
        result.id, tool_name=result.name
    )
    block_payload = freeze_json(result.model_dump(mode="json"))
    assert hasattr(block_payload, "entries")
    state = ToolResultStateFact(result.state.value)
    frozen_block = FrozenToolResultBlockFact(
        tool_call_id=result.id,
        model_tool_name=result.name,
        result_state=state,
        canonical_block_payload=block_payload,
        block_payload_fingerprint=context_fingerprint(
            "tool-result-block:v1", block_payload
        ),
    )
    timing = ToolObservationTimingFact(
        observed_at_utc="2026-07-09T00:00:00Z",
        source_started_at_utc="2026-07-09T00:00:00Z",
        source_ended_at_utc="2026-07-09T00:00:00Z",
        observation_duration_seconds=0,
        freshness="current_tool_observation",
        clock_source="tool_runtime_metadata",
        tool_origin="unknown",
        tool_name=result.name,
        tool_call_id=result.id,
    )
    variant = next(
        item
        for item in requirement.result_render_contract.allowed_variants
        if item.variant_code is ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT
    )
    profile_payload = {
        "profile_version": "tool-result-profile:v1",
        "selected_variant": variant,
        "render_contract": requirement.result_render_contract,
        "tool_origin": "unknown",
        "descriptor_attribution": requirement.descriptor_attribution,
        "render_contract_fingerprint": (
            requirement.result_render_contract.contract_fingerprint
        ),
    }
    profile = ToolResultRenderProfileFact(
        **profile_payload,
        profile_fingerprint=context_fingerprint(
            "tool-result-render-profile:v1", profile_payload
        ),
    )
    semantics = ToolResultExecutionSemanticsFact(
        render_profile=profile,
        result_state=state,
        essential_capture_policy=None,
        essential_result=None,
        terminal_payload_timing=None,
    )
    reference = ExternalExecutionRequirementReferenceFact(
        owner_runtime_session_id="runtime:test",
        require_event_id=require_event_id,
        require_event_sequence=require_event_sequence,
        require_event_payload_fingerprint="sha256:" + "2" * 64,
        tool_call_id=result.id,
        requirement_fingerprint=requirement.requirement_fingerprint,
    )
    payload = {
        "requirement_ref": reference,
        "result_block": frozen_block,
        "observation_timing": timing,
        "execution_semantics": semantics,
    }
    return ExternalToolResultIngressFact(
        **payload,
        ingress_fingerprint=context_fingerprint(
            "external-tool-result-ingress:v1", payload
        ),
    )


def run_start_permission_fields(
    run_id: str,
    *,
    mode: str | PermissionMode = PermissionMode.BYPASS_PERMISSIONS,
    source: str = "session_default",
    user_input: str = "",
    turn_id: str | None = None,
    reply_id: str | None = None,
    mcp_installation_id: str = "mcp_installation:empty",
    mcp_installation_owner_runtime_session_id: str = "runtime:test",
    model_target=None,
    transcript_source_through_sequence: int = 0,
    transcript_source_event_count: int = 0,
) -> dict[str, object]:
    parsed = parse_permission_mode(mode)
    permission_snapshot_id = f"permission_snapshot:{run_id}"
    target = model_target or test_resolved_target_fact()
    runtime_session_id = mcp_installation_owner_runtime_session_id
    observed_at = "1970-01-01T00:00:00Z"
    resolved_turn_id = turn_id or run_id.replace("run:", "turn:", 1)
    resolved_reply_id = reply_id or run_id.replace("run:", "reply:", 1)
    current_user = CurrentUserMessageFact(
        message_id=f"user-message:{run_id}",
        source_kind=(
            "subagent_task" if source == "child_profile" else "host_user_input"
        ),
        text=user_input,
        observed_at_utc=observed_at,
        content_sha256=text_sha256(user_input),
        source_artifact_id=(
            f"artifact:task:{run_id}" if source == "child_profile" else None
        ),
    )
    common = {
        "permission_snapshot_id": permission_snapshot_id,
        "permission_mode": parsed.value,
        "permission_policy": preset_to_policy(parsed).to_dict(),
        "permission_snapshot_source": source,
        "model_target": target,
        "mcp_installation_id": mcp_installation_id,
        "mcp_installation_owner_runtime_session_id": runtime_session_id,
        "current_user_message": current_user,
        "terminal_run_end_event_id": test_run_end_event_id(run_id),
    }
    if source == "child_profile":
        return {
            **common,
            "run_entry_kind": "subagent_child",
            "new_run_boundary": None,
            "subagent_run_entry": SubagentRunEntryFact(
                subagent_run_id=run_id,
                subagent_task_id=f"task:{run_id}",
                parent_runtime_session_id=runtime_session_id,
                parent_run_id=f"parent:{run_id}",
                spawn_edge_id=f"edge:{run_id}",
                capability_profile_fingerprint="sha256:test-profile",
                task_artifact_id=f"artifact:task:{run_id}",
                task_observed_at_utc=observed_at,
                child_result_render_policy=build_child_result_render_policy(
                    renderer_version="test:v1",
                    max_summary_chars=4_000,
                    max_artifact_refs=32,
                ),
                permission_snapshot_id=permission_snapshot_id,
                model_target_fingerprint=target.target_fingerprint,
                mcp_installation_id=mcp_installation_id,
                mcp_installation_owner_runtime_session_id=runtime_session_id,
            ),
        }

    identity = HostRunBoundaryIdentityFact(
        boundary_id=f"run_boundary:test:{uuid4().hex}",
        kind="pre_run",
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        turn_id=resolved_turn_id,
        reply_id=resolved_reply_id,
        attempt_number=1,
        observed_at_utc=observed_at,
    )
    surface = build_capability_execution_surface_identity(
        surface_contract_version="test:v1",
        entries=(),
        mcp_installation_id=mcp_installation_id,
    )
    basis = build_capability_resolve_basis(
        basis_id=f"capability_basis:test:{uuid4().hex}",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=CapabilityExposureOwnerFact(
            owner_kind="host_boundary",
            owner_id=identity.boundary_id,
            host_boundary_kind="pre_run",
            runtime_session_id=runtime_session_id,
            run_id=run_id,
        ),
        workspace_identity_fingerprint="sha256:test-workspace",
        memory_domain_id="memory_domain:test",
        permission_snapshot_id=permission_snapshot_id,
        plan_active=False,
        active_skill_names=(),
        user_intent_fingerprint=sha256_fingerprint("test-user-intent:v1", user_input),
        prior_transcript_fingerprint="sha256:test-prior-transcript",
        mcp_installation_id=mcp_installation_id,
        execution_surface_identity=surface,
    )
    return {
        **common,
        "run_entry_kind": "host",
        "new_run_boundary": NewRunBoundaryFact(
            identity=identity,
            transcript=BoundaryTranscriptSnapshotFact(
                source_through_sequence=transcript_source_through_sequence,
                source_event_count=transcript_source_event_count,
                compacted_window_id=None,
                preflight_compaction_id=None,
                preflight_compaction_terminal_event_id=None,
                preflight_compaction_terminal_sequence=None,
            ),
            model_target_fingerprint=target.target_fingerprint,
            permission_snapshot_id=permission_snapshot_id,
            mcp_installation_id=mcp_installation_id,
            capability_basis=basis,
            degraded_reason_codes=(),
        ),
        "subagent_run_entry": None,
    }


def test_run_end_event_id(run_id: str) -> str:
    return "run_end:test:" + sha256_fingerprint(
        "test-run-end-id:v1", run_id
    ).removeprefix("sha256:")


def run_end_contract_fields(
    run_id: str,
    *,
    status: str,
    abort_kind: str | None = None,
    recovered: bool = False,
    error_message: str | None = None,
) -> dict[str, object]:
    if status == "finished":
        terminalization_kind = "normal"
        error_message = None
    elif status == "aborted":
        terminalization_kind = (
            "recovered_interrupted"
            if recovered
            else "host_teardown"
            if abort_kind == "host_teardown"
            else "user_stop"
        )
        error_message = None
    elif status == "failed":
        terminalization_kind = "execution_failure"
        error_message = error_message or "synthetic test execution failure"
    else:
        raise ValueError(f"unsupported test RunEnd status: {status}")
    return {
        "id": test_run_end_event_id(run_id),
        "terminalization_kind": terminalization_kind,
        "error_message": error_message,
    }


def subagent_result_handoff_fields(
    *,
    subagent_run_id: str,
    child_runtime_session_id: str,
    child_run_id: str,
    result_id: str,
    summary: str,
    result_artifact_id: str,
    artifact_ids: tuple[str, ...] | list[str],
    result_source: str = "inferred",
    tool_call_count: int = 0,
    token_usage: ModelTokenUsageFact | None = None,
) -> dict[str, object]:
    policy = build_child_result_render_policy(
        renderer_version="test:v1",
        max_summary_chars=4_000,
        max_artifact_refs=32,
    )
    terminal = ChildNativeTerminalReferenceFact(
        child_runtime_session_id=child_runtime_session_id,
        child_run_id=child_run_id,
        terminal_event_id=f"run_end:child:{subagent_run_id}",
        terminal_sequence=4,
        terminal_status="finished",
        terminalization_kind="normal",
        stop_reason="final",
    )
    evidence = (
        ChildExplicitResultEvidenceFact(
            source_result_submitted_event_id=f"submitted:{result_id}",
            source_result_submitted_event_sequence=1,
            child_runtime_session_id=child_runtime_session_id,
            child_run_id=child_run_id,
            source_tool_call_id="call:report-result",
            tool_call_start_event_id="event:tool-call-start",
            tool_call_start_sequence=1,
            tool_result_end_event_id="event:tool-result-end",
            tool_result_end_sequence=3,
        )
        if result_source == "explicit"
        else None
    )
    return {
        "result_handoff": build_child_result_handoff(
            handoff_kind=result_source,  # type: ignore[arg-type]
            policy=policy,
            child_terminal_reference=terminal,
            explicit_evidence=evidence,
            result_id=result_id,
            summary=summary,
            result_artifact_id=result_artifact_id,
            artifact_ids=tuple(artifact_ids),
            token_usage=token_usage,
            usage_status="complete" if token_usage is not None else "missing",
            tool_call_count=tool_call_count,
        )
    }


builtins.run_start_permission_fields = run_start_permission_fields
builtins.run_end_contract_fields = run_end_contract_fields
builtins.subagent_result_handoff_fields = subagent_result_handoff_fields


@pytest.fixture(autouse=True)
def _isolate_user_mcp_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests hermetic from ~/.pulsara/mcp.yaml.

    HostCore and HostSession intentionally load user-level MCP servers in
    production.  Unit tests should not inherit the developer's personal MCP
    config: a remote user MCP can make tests slow, flaky, or timing-dependent.
    MCP-specific tests can still override these patched symbols explicitly with
    their own monkeypatches.
    """

    def _empty_configs(*, workspace_root):
        return ()

    monkeypatch.setattr(
        "pulsara_agent.host.core.load_mcp_server_configs", _empty_configs
    )
    monkeypatch.setattr(
        "pulsara_agent.host.session.load_mcp_server_configs", _empty_configs
    )
