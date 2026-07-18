from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    EventContext,
    RunInteractionResumeBoundaryEvent,
)
from pulsara_agent.event_log.serialization import dump_agent_event, load_agent_event
from pulsara_agent.capability.builtin_provider import BuiltinToolCapabilityProvider
from pulsara_agent.capability.facts import (
    ProviderProjectionResult,
    build_capability_projection_fact,
    narrow_capability_projection_fact,
)
from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.render import render_catalog_prompt
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    RenderedCapabilityPrompt,
    RenderedCapabilityPromptFragment,
    ResolvedSkillCatalogEntry,
)
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.primitives.capability import (
    CapabilityProjectionEntryFact,
    build_capability_execution_surface_identity,
    build_capability_exposure_semantic,
    build_capability_exposure_snapshot,
    build_capability_resolve_basis,
    capability_authorization_fingerprint,
    capability_projection_entry_id,
    empty_capability_projection,
)
from pulsara_agent.primitives.model_call import ModelTokenUsageFact
from pulsara_agent.primitives.permission import (
    PermissionMode,
    PresetPermissionPolicyFact,
    preset_permission_payload,
    preset_permission_policy_fact,
)
from pulsara_agent.primitives.run_boundary import (
    BoundaryBatchConfirmation,
    BoundaryTranscriptSnapshotFact,
    InteractionResumeBoundaryFact,
    NewRunBoundaryFact,
    PlanWorkflowStateFact,
    resume_gate_policy_for,
)
from pulsara_agent.primitives.run_entry import (
    CapabilityExposureOwnerFact,
    CurrentUserMessageFact,
    HostRunBoundaryIdentityFact,
    SubagentRunEntryFact,
    text_sha256,
    validate_host_current_user_attribution,
    validate_subagent_current_user_attribution,
)
from pulsara_agent.primitives.subagent import (
    ChildExplicitResultEvidenceFact,
    ChildNativeTerminalReferenceFact,
    ChildResultHandoffFact,
    build_child_result_render_policy,
    rendered_payload_sha256,
)
from pulsara_agent.tools.registry import (
    ToolRegistry,
    build_tool_binding_contract,
)


UTC = "2026-07-12T01:02:03Z"


def _identity(*, kind: str = "pre_run") -> HostRunBoundaryIdentityFact:
    return HostRunBoundaryIdentityFact(
        boundary_id="boundary:1",
        kind=kind,
        runtime_session_id="runtime:1",
        run_id="run:1",
        turn_id="turn:1",
        reply_id="reply:1",
        attempt_number=1,
        observed_at_utc=UTC,
    )


def _exposure():
    surface = build_capability_execution_surface_identity(
        surface_contract_version="capability-surface:v1",
        entries=(),
        mcp_installation_id="mcp-installation:1",
    )
    owner = CapabilityExposureOwnerFact(
        owner_kind="host_boundary",
        owner_id="boundary:1",
        host_boundary_kind="pre_run",
        runtime_session_id="runtime:1",
        run_id="run:1",
    )
    basis = build_capability_resolve_basis(
        basis_id="basis:1",
        basis_kind="initial",
        source_basis_id=None,
        source_basis_fingerprint=None,
        owner=owner,
        workspace_identity_fingerprint="workspace-fp",
        memory_domain_id="memory-domain:1",
        permission_snapshot_id="permission:1",
        plan_active=False,
        active_skill_names=(),
        user_intent_fingerprint="user-intent-fp",
        prior_transcript_fingerprint="transcript-fp",
        mcp_installation_id="mcp-installation:1",
        execution_surface_identity=surface,
    )
    projection = empty_capability_projection()
    semantic = build_capability_exposure_semantic(
        execution_surface=surface,
        catalog_projection=projection,
        active_skill_projection=projection,
        authorization_fingerprint=capability_authorization_fingerprint(()),
    )
    return build_capability_exposure_snapshot(
        exposure_id="exposure:1",
        owner=owner,
        resolution_kind="initial",
        resolve_basis=basis,
        semantic=semantic,
        authorization_entries=(),
        source_exposure_id=None,
    )


def test_permission_preset_fact_is_the_canonical_expansion() -> None:
    fact = preset_permission_policy_fact(PermissionMode.READ_ONLY)
    assert fact.expanded_policy == preset_permission_payload("read-only")
    with pytest.raises(ValidationError):
        PresetPermissionPolicyFact(
            mode="read-only",
            expanded_policy={**fact.expanded_policy, "terminal_access": "allow"},
        )


def test_host_boundary_timestamp_is_canonical_utc() -> None:
    identity = _identity().model_copy(
        update={"observed_at_utc": "2026-07-12T09:02:03+08:00"}
    )
    reparsed = HostRunBoundaryIdentityFact.model_validate(identity.model_dump())
    assert reparsed.observed_at_utc == "2026-07-12T01:02:03.000000Z"


def test_current_user_hash_and_host_attribution_are_enforced() -> None:
    current = CurrentUserMessageFact(
        message_id="message:1",
        source_kind="host_user_input",
        text="hello",
        observed_at_utc=UTC,
        content_sha256=text_sha256("hello"),
        source_artifact_id=None,
    )
    validate_host_current_user_attribution(boundary=_identity(), current_user=current)
    with pytest.raises(ValidationError):
        CurrentUserMessageFact(
            **{
                **current.model_dump(),
                "content_sha256": "0" * 64,
            }
        )


def test_subagent_task_and_primitive_entry_modes_are_distinct() -> None:
    policy = build_child_result_render_policy(
        renderer_version="child-result:v1",
        max_summary_chars=200,
        max_artifact_refs=32,
    )
    common = dict(
        subagent_run_id="subagent-run:1",
        parent_runtime_session_id="runtime:parent",
        parent_run_id="run:parent",
        spawn_edge_id="edge:1",
        capability_profile_fingerprint="capability-profile-fp",
        task_artifact_id="artifact:task",
        task_observed_at_utc=UTC,
        child_result_render_policy=policy,
        permission_snapshot_id="permission:child",
        model_target_fingerprint="model-target-fp",
        mcp_installation_id="mcp:parent",
        mcp_installation_owner_runtime_session_id="runtime:parent",
    )
    task_entry = SubagentRunEntryFact(subagent_task_id="task:1", **common)
    task_message = CurrentUserMessageFact(
        message_id="message:task",
        source_kind="subagent_task",
        text="do the task",
        observed_at_utc=UTC,
        content_sha256=text_sha256("do the task"),
        source_artifact_id="artifact:task",
    )
    validate_subagent_current_user_attribution(
        entry=task_entry, current_user=task_message
    )

    primitive = SubagentRunEntryFact(subagent_task_id=None, **common)
    with pytest.raises(ValueError):
        validate_subagent_current_user_attribution(
            entry=primitive, current_user=task_message
        )


def test_projection_entry_identity_includes_provider() -> None:
    first = capability_projection_entry_id(
        projection_kind="catalog_entry",
        provider_id="provider:a",
        source_kind="custom",
        stable_name="same",
    )
    second = capability_projection_entry_id(
        projection_kind="catalog_entry",
        provider_id="provider:b",
        source_kind="custom",
        stable_name="same",
    )
    assert first != second
    CapabilityProjectionEntryFact(
        projection_entry_id=first,
        projection_kind="catalog_entry",
        stable_name="same",
        provider_id="provider:a",
        source_kind="custom",
        content_fingerprint="content-fp",
        content_artifact_id="artifact:content",
    )


def test_new_run_boundary_cross_checks_capability_basis() -> None:
    exposure = _exposure()
    boundary = NewRunBoundaryFact(
        identity=_identity(),
        transcript=BoundaryTranscriptSnapshotFact(
            source_through_sequence=0,
            source_event_count=0,
            compacted_window_id=None,
            checkpoint_compaction_id=None,
            checkpoint_terminal_event_id=None,
            checkpoint_terminal_sequence=None,
            checkpoint_keep_after_sequence=None,
            preflight_compaction_id=None,
            preflight_compaction_terminal_event_id=None,
            preflight_compaction_terminal_sequence=None,
        ),
        model_target_fingerprint="model-target-fp",
        permission_snapshot_id="permission:1",
        mcp_installation_id="mcp-installation:1",
        capability_basis=exposure.resolve_basis,
        degraded_reason_codes=(),
    )
    assert boundary.identity.boundary_id == "boundary:1"


def test_child_result_render_policy_fingerprint_covers_caps() -> None:
    policy = build_child_result_render_policy(
        renderer_version="child-result:v1",
        max_summary_chars=17,
        max_artifact_refs=3,
    )
    with pytest.raises(ValidationError):
        policy.model_validate({**policy.model_dump(), "max_summary_chars": 18})


def test_explicit_handoff_evidence_must_precede_terminal() -> None:
    terminal = ChildNativeTerminalReferenceFact(
        child_runtime_session_id="runtime:child",
        child_run_id="run:child",
        terminal_event_id="event:terminal",
        terminal_sequence=20,
        terminal_status="finished",
        terminalization_kind="normal",
        stop_reason="final",
    )
    evidence = ChildExplicitResultEvidenceFact(
        source_result_submitted_event_id="event:submitted",
        source_result_submitted_event_sequence=18,
        child_runtime_session_id="runtime:child",
        child_run_id="run:child",
        source_tool_call_id="call:report",
        tool_call_start_event_id="event:call",
        tool_call_start_sequence=17,
        tool_result_end_event_id="event:result",
        tool_result_end_sequence=19,
    )
    payload = {"summary": "done"}
    fact = ChildResultHandoffFact(
        handoff_kind="explicit",
        renderer_version="child-result:v1",
        render_policy_fingerprint="policy-fp",
        child_terminal_reference=terminal,
        explicit_evidence=evidence,
        result_id="result:1",
        summary="done",
        result_artifact_id="artifact:result",
        artifact_ids=("artifact:result",),
        rendered_payload_sha256=rendered_payload_sha256(payload),
        token_usage=ModelTokenUsageFact(
            input_tokens=10,
            cached_input_tokens=2,
            output_tokens=3,
            reasoning_output_tokens=1,
            total_tokens=13,
        ),
        usage_status="complete",
        tool_call_count=1,
    )
    assert fact.explicit_evidence == evidence
    with pytest.raises(ValidationError):
        ChildResultHandoffFact.model_validate(
            {
                **fact.model_dump(mode="json"),
                "explicit_evidence": {
                    **evidence.model_dump(mode="json"),
                    "tool_result_end_sequence": 20,
                },
            }
        )


@pytest.mark.parametrize(
    ("usage_status", "usage"),
    [
        ("missing", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
        ("complete", None),
        ("partial", None),
    ],
)
def test_child_handoff_usage_status_matches_usage(
    usage_status: str, usage: dict[str, int] | None
) -> None:
    terminal = ChildNativeTerminalReferenceFact(
        child_runtime_session_id="runtime:child",
        child_run_id="run:child",
        terminal_event_id="event:terminal",
        terminal_sequence=2,
        terminal_status="finished",
        terminalization_kind="normal",
        stop_reason="final",
    )
    with pytest.raises(ValidationError):
        ChildResultHandoffFact(
            handoff_kind="inferred",
            renderer_version="v1",
            render_policy_fingerprint="policy",
            child_terminal_reference=terminal,
            explicit_evidence=None,
            result_id="result:1",
            summary="",
            result_artifact_id="artifact:1",
            artifact_ids=("artifact:1",),
            rendered_payload_sha256="0" * 64,
            token_usage=usage,
            usage_status=usage_status,
            tool_call_count=0,
        )


def test_resume_gate_policy_rejects_arbitrary_combinations() -> None:
    policy = resume_gate_policy_for("mcp_input_required")
    assert policy.permission_wait_behavior == "fail_closed_deny"
    with pytest.raises(ValidationError):
        policy.model_validate(
            {**policy.model_dump(), "permission_wait_behavior": "allow_wait"}
        )


def test_plan_workflow_inactive_cannot_carry_entry_attribution() -> None:
    permission = preset_permission_policy_fact("bypass-permissions")
    with pytest.raises(ValidationError):
        PlanWorkflowStateFact(
            workflow_id="plan:1",
            active=False,
            pending_entry_audit=False,
            revision=1,
            entered_event_id=None,
            entered_event_sequence=None,
            entry_run_id=None,
            entry_turn_id=None,
            entry_reply_id=None,
            stored_default_permission=permission,
            accepted_plan_artifact_id=None,
        )


def test_boundary_full_confirmation_requires_contiguous_complete_batch() -> None:
    confirmation = BoundaryBatchConfirmation(
        status="full",
        candidate_event_ids=("event:1", "event:2"),
        committed_event_ids=("event:1", "event:2"),
        committed_sequences=(4, 5),
        actual_last_sequence=5,
    )
    assert confirmation.status == "full"
    with pytest.raises(ValidationError):
        BoundaryBatchConfirmation(
            status="full",
            candidate_event_ids=("event:1", "event:2"),
            committed_event_ids=("event:1", "event:2"),
            committed_sequences=(4, 6),
            actual_last_sequence=6,
        )


def test_typed_capability_exposure_event_round_trip() -> None:
    event = CapabilityExposureResolvedEvent(
        **EventContext("run:1", "turn:1", "reply:1").event_fields(),
        exposure=_exposure(),
        exposure_revision=1,
    )
    assert load_agent_event(dump_agent_event(event)) == event


def test_typed_resume_boundary_event_round_trip() -> None:
    identity = _identity(kind="pre_interaction_resume")
    boundary = InteractionResumeBoundaryFact(
        identity=identity,
        original_run_start_event_id="event:run-start",
        original_run_start_sequence=1,
        interaction_id="interaction:1",
        interaction_kind="approval",
        suspended_state_token_fingerprint="suspended-token-fp",
        permission_snapshot_id="permission:1",
        model_target_fingerprint="model-target-fp",
        mcp_installation_id="mcp:1",
        source_exposure_id="exposure:1",
        source_exposure_semantic_fingerprint="semantic:1",
        source_exposure_fact_fingerprint="fact:1",
        effective_exposure_id="exposure:2",
        effective_exposure_semantic_fingerprint="semantic:1",
        effective_exposure_fact_fingerprint="fact:2",
        exposure_transition="reused",
        committed_mcp_audit_event_ids=(),
    )
    event = RunInteractionResumeBoundaryEvent(
        **EventContext("run:1", "turn:1", "reply:1").event_fields(),
        boundary=boundary,
    )
    assert load_agent_event(dump_agent_event(event)) == event


def test_run_start_created_at_is_not_required_to_equal_ingress_observation() -> None:
    observed = datetime.fromisoformat(UTC.replace("Z", "+00:00"))
    started = observed.replace(microsecond=1).astimezone(timezone.utc)
    assert started >= observed


def test_execution_surface_freeze_persists_descriptor_and_binding_identity(
    tmp_path,
) -> None:
    class ReadFileBinding:
        name = "read_file"
        description = "read"
        parameters: dict[str, object] = {}
        is_read_only = True
        is_concurrency_safe = True

        def execute(self, call):
            raise AssertionError(call)

    registry = ToolRegistry()
    tool = ReadFileBinding()
    registry.register(
        tool,
        binding_contract=build_tool_binding_contract(
            tool_name=tool.name,
            origin="builtin",
            contract_id="pulsara.builtin.read_file",
            contract_version="v1",
        ),
    )
    archive = InMemoryArchiveStore()
    frozen = CapabilityRuntime(
        providers=(BuiltinToolCapabilityProvider(),)
    ).freeze_execution_surface(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=tmp_path,
            workspace_kind="project",
            available_tool_names=frozenset({"read_file"}),
            mcp_installation_id="mcp:1",
        ),
        tool_registry=registry,
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id="boundary:1",
    )
    assert frozen.identity.entries[0].capability_name == "read_file"
    assert frozen.identity.entries[0].binding_contract_version == "v1"
    assert archive.get_text(
        frozen.identity.entries[0].descriptor_artifact_id,
        session_id="runtime:1",
    )


def test_execution_surface_freeze_rejects_callable_without_binding(tmp_path) -> None:
    with pytest.raises(ValueError, match="no stable binding contract"):
        CapabilityRuntime(
            providers=(BuiltinToolCapabilityProvider(),)
        ).freeze_execution_surface(
            CapabilityExecutionSurfaceSnapshotContext(
                workspace_root=tmp_path,
                workspace_kind="project",
                available_tool_names=frozenset({"read_file"}),
                mcp_installation_id="mcp:1",
            ),
            tool_registry=ToolRegistry(),
            archive=InMemoryArchiveStore(),
            runtime_session_id="runtime:1",
            owner_id="boundary:1",
        )


def test_projection_fact_reconstructs_exact_fragmented_prompt() -> None:
    entries = (
        ResolvedSkillCatalogEntry(
            name="alpha",
            description="alpha skill",
            location=".agents/skills/alpha/SKILL.md",
            source="workspace",
        ),
        ResolvedSkillCatalogEntry(
            name="beta",
            description="beta skill",
            location="~/.agents/skills/beta/SKILL.md",
            source="user",
        ),
    )
    rendered = render_catalog_prompt(entries)
    output = CapabilityProjectionOutput(
        catalog_entries=entries,
        catalog_prompt=rendered.text,
        catalog_rendered=rendered,
    )
    archive = InMemoryArchiveStore()
    fact = build_capability_projection_fact(
        projection_type="catalog",
        provider_results=(
            ProviderProjectionResult(provider_id="local-skills", output=output),
        ),
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id="boundary:1",
        exposure_id="exposure:1",
    )
    rebuilt = "".join(
        archive.get_text(fragment.fragment_artifact_id, session_id="runtime:1")
        for fragment in fact.rendered_fragments
    )
    assert rebuilt == rendered.text
    assert fact.rendered_entry_count == 2
    assert fact.omitted_entry_count == 0
    assert {entry.source_kind for entry in fact.visible_source_entries} == {
        "workspace",
        "user",
    }


def test_continuation_projection_reuses_original_fragments_without_promotion() -> None:
    alpha = ResolvedSkillCatalogEntry(
        name="alpha",
        description="alpha skill",
        location=".agents/skills/alpha/SKILL.md",
        source="workspace",
    )
    beta = ResolvedSkillCatalogEntry(
        name="beta",
        description="beta skill",
        location="~/.agents/skills/beta/SKILL.md",
        source="user",
    )
    original_rendered = render_catalog_prompt((alpha, beta))
    archive = InMemoryArchiveStore()
    original = build_capability_projection_fact(
        projection_type="catalog",
        provider_results=(
            ProviderProjectionResult(
                provider_id="local-skills",
                output=CapabilityProjectionOutput(
                    catalog_entries=(alpha, beta),
                    catalog_prompt=original_rendered.text,
                    catalog_rendered=original_rendered,
                ),
            ),
        ),
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id="boundary:1",
        exposure_id="exposure:original",
    )
    candidate_rendered = RenderedCapabilityPrompt(
        text="CURRENT EXPANDED ALPHA",
        source_entry_count=1,
        fragments=(
            RenderedCapabilityPromptFragment(
                container_id="catalog",
                fragment_role="entry",
                static_scope=None,
                source_stable_name="alpha",
                text="CURRENT EXPANDED ALPHA",
            ),
        ),
    )
    candidate = build_capability_projection_fact(
        projection_type="catalog",
        provider_results=(
            ProviderProjectionResult(
                provider_id="local-skills",
                output=CapabilityProjectionOutput(
                    catalog_entries=(alpha,),
                    catalog_prompt=candidate_rendered.text,
                    catalog_rendered=candidate_rendered,
                ),
            ),
        ),
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id="boundary:resume",
        exposure_id="exposure:candidate",
        persist_artifacts=False,
    )
    narrowed, prompt = narrow_capability_projection_fact(
        projection_type="catalog",
        original=original,
        current_candidate=candidate,
        archive=archive,
        runtime_session_id="runtime:1",
        owner_id="boundary:resume",
        exposure_id="exposure:continuation",
    )
    original_alpha_fragments = [
        fragment
        for fragment in original.rendered_fragments
        if fragment.source_entry_id
        == next(
            entry.projection_entry_id
            for entry in original.visible_source_entries
            if entry.stable_name == "alpha"
        )
    ]
    assert "CURRENT EXPANDED ALPHA" not in (prompt or "")
    assert [
        fragment.fragment_id
        for fragment in narrowed.rendered_fragments
        if fragment.fragment_role == "entry"
    ] == [fragment.fragment_id for fragment in original_alpha_fragments]
    assert [entry.stable_name for entry in narrowed.visible_source_entries] == ["alpha"]
