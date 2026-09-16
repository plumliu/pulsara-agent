"""Focused K3 image wire and v2-estimator contract tests."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from pulsara_agent.conversation_kernel.assembler import (
    CompletedAssistantMessage,
    CompletedTextBlock,
    CompletedToolCallBlock,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionScope,
    CompactionTrigger,
    RecentHumanMessageProof,
    ResolvedCompactionPolicy,
    freeze_compaction_canonical_range,
    provider_input_item_canonical_expanded_bytes,
    resolved_compaction_headroom_bounds,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    _can_enter_model_switch_tier_three,
    _can_retry_compaction_with_smaller_recent,
    _ordinary_recent_suffixes,
    _target_is_known_text_only,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    CompactionPlanningError,
    CompactionReclaimUnavailable,
    compaction_effective_history_has_image,
    crosses_compaction_resource_headroom,
    project_prompt_content_for_text_only_handover,
    recent_human_window_has_image,
)
from pulsara_agent.conversation_kernel.direct_model import (
    ProviderFollowupWireResourceQuote,
    freeze_provider_wire_measurement,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    FrozenPostResponseResourceQuote,
    OutputResourceInterruption,
    _completed_assistant_canonical_bytes,
    _entered_plan_continuation_followup_upper,
    _ordinary_result_followup_upper,
    _plan_result_followup_upper,
    _root_completion_followup_upper,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel._repository.contracts import (
    PLAN_CONTROL_RESULT_INLINE_HARD_BYTES,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
)
from pulsara_agent.conversation_kernel.subagents.contracts import (
    MAXIMUM_RESULT_SUMMARY_UTF8_BYTES,
    MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES,
    SubagentProfileKind,
    SubagentResultSource,
    SubagentTaskStatus,
    SubagentTerminalReason,
    build_subagent_completion_storage_body,
    project_subagent_completion_for_provider,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    chat_semantic_wire_group,
)
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.llm.estimator import (
    PulsaraHeuristicTokenEstimatorV2,
    estimate_image_visual_tokens,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    frozen_tool_result_public_text,
    join_text_content,
    llm_content_logical_bytes,
)
from pulsara_agent.conversation_kernel.prompt_content import canonical_prompt_body_bytes
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    ProviderToolCall,
    StructuredModelInputCompileError,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    freeze_json,
)
from pulsara_agent.primitives.tool_result_projection import (
    ToolResultLogicalMessageKind,
    conservative_tool_result_logical_message,
    provider_neutral_message_logical_bytes,
    render_provider_tool_result_logical_message,
)
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
    ToolObservationOrigin,
    freeze_tool_observation_timing_fact,
)


def _image(
    payload: bytes | None = None,
    *,
    media_type: str = "image/png",
    width: int = 512,
    height: int = 512,
) -> LLMImagePart:
    if payload is None:
        output = BytesIO()
        Image.new("RGB", (width, height), (11, 23, 41)).save(output, "PNG")
        payload = output.getvalue()
    return LLMImagePart(media_type, payload, width, height)


def _message(*parts: LLMTextPart | LLMImagePart) -> LLMMessage:
    return LLMMessage.user_content(FrozenPromptContent(tuple(parts)))


def _assert_source_variant_wire_upper(candidate, upper, *, wire_api: str) -> None:
    from pulsara_agent.model_input.compiler import _observation_lifecycle
    from pulsara_agent.model_input.continuity import (
        SourceObservationPresence,
        encode_runtime_observation,
    )

    def wire(message):
        if wire_api == "chat":
            return chat_semantic_wire_group(message)[0]
        return responses_semantic_wire_group(message)[0]

    estimator = PulsaraHeuristicTokenEstimatorV2()
    upper_wire = wire(upper)
    for variant in candidate.variants:
        actual = encode_runtime_observation(
            source_kind=candidate.source_kind,
            trust_class=candidate.trust_class,
            lifecycle=_observation_lifecycle(candidate.lifecycle),
            presence=SourceObservationPresence.VALUE,
            contract_version=candidate.source_contract_version,
            body=variant.text,
        )
        actual_wire = wire(actual)
        assert provider_neutral_message_logical_bytes(actual) <= (
            provider_neutral_message_logical_bytes(upper)
        )
        assert len(canonical_json_bytes(actual_wire)) <= len(
            canonical_json_bytes(upper_wire)
        )
        assert estimator.estimate_wire_json_component(actual_wire) <= (
            estimator.estimate_wire_json_component(upper_wire)
        )


def _source_upper_collector(
    tmp_path, *, display_timezone=timezone.utc, clock=None, hook_owner=None
):
    from pulsara_agent.conversation_kernel.context_sources import (
        KernelContextSourceCollector,
    )
    from tests.test_round3_structured_model_input_compiler import (
        _Capability,
        _TerminalCwd,
    )

    return KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        terminal_cwd=_TerminalCwd(tmp_path),
        capability_composer=_Capability(),
        base_system_prompt="BASE",
        display_timezone=display_timezone,
        clock=clock or (lambda: datetime(2026, 9, 14, tzinfo=timezone.utc)),
        hook_context_owner=hook_owner,
    )


@pytest.mark.parametrize("wire_api", ("chat", "responses"))
@pytest.mark.parametrize("timezone_name", ("UTC", "America/New_York", "Asia/Kathmandu"))
def test_clock_upper_bounds_actual_full_and_compact_sources(
    tmp_path, wire_api: str, timezone_name: str
) -> None:
    from zoneinfo import ZoneInfo
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from tests.support.round3 import StructuredToolPort
    from tests.test_round3_structured_model_input_compiler import (
        _canonical_facts,
        _collect_context_sources,
    )

    observed = [datetime(2024, 11, 3, 5, 59, 59, 999999, tzinfo=timezone.utc)]
    collector = _source_upper_collector(
        tmp_path,
        display_timezone=ZoneInfo(timezone_name),
        clock=lambda: observed[0],
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    (upper,) = collector.freeze_post_response_call_source_upper()
    # The second capture crosses the New York DST boundary; both must fit the
    # one upper frozen before either actual observation is collected.
    for instant in (
        observed[0],
        datetime(2024, 11, 3, 6, 0, 0, tzinfo=timezone.utc),
    ):
        observed[0] = instant
        sources = _collect_context_sources(
            collector,
            activation_subject=None,
            activation_text="",
            tool_surface=surface,
            canonical_facts=_canonical_facts(),
        )
        candidate = next(
            c
            for c in sources.candidates
            if c.source_kind is ContextSourceKind.RUNTIME_CLOCK
        )
        _assert_source_variant_wire_upper(candidate, upper, wire_api=wire_api)


@pytest.mark.parametrize("wire_api", ("chat", "responses"))
@pytest.mark.parametrize("mode", ("bounded_tool", "unlimited_tool", "stop"))
@pytest.mark.parametrize("character", ("\x01", "界", "🙂"))
def test_hook_upper_bounds_actual_accepted_context_and_continuation(
    tmp_path, wire_api: str, mode: str, character: str
) -> None:
    from pulsara_agent.hooks.config_parser import DEFAULT_ADDITIONAL_CONTEXT_LIMIT
    from pulsara_agent.hooks.context import HookContextOwner
    from pulsara_agent.hooks.contracts import (
        HookContextEntry,
        HookEventType,
        PostToolRef,
        StopRef,
    )
    from pulsara_agent.hooks.executor import HookSecretScrubSet
    from pulsara_agent.model_input.contracts import ContextSourceCandidate
    from tests.test_round9_2_hook_subsystem import _definition, _scope, _view

    owner = HookContextOwner()
    scope = _scope()
    owner.register_scope(scope)
    stop = mode == "stop"
    event = HookEventType.STOP_EVENT if stop else HookEventType.POST_TOOL_USE_EVENT
    context_limit = 0 if mode == "unlimited_tool" else 1 if stop else 256
    definition = _definition(tmp_path, event, "noop", context_limit=context_limit)
    view = _view(tmp_path, (definition,))
    runner = object.__new__(ConversationKernelRunner)
    runner._hook_dispatcher = SimpleNamespace(capture_view=lambda: view)
    runner._hook_context_owner = owner
    runner._hook_scope = scope
    upper = runner._hook_output_source_upper(
        call_count=0 if stop else 2, scope_kind=ModelInputScopeKind.ROOT
    )
    assert upper is not None
    estimator = PulsaraHeuristicTokenEstimatorV2()
    threshold = DEFAULT_ADDITIONAL_CONTEXT_LIMIT if stop else context_limit
    repeats = 65536 if threshold == 0 else threshold * 4
    while threshold and estimator.estimate_text(character * repeats) > threshold:
        repeats -= 1
    body = character * repeats
    for ordinal in range(1 if stop else 2):
        entry = HookContextEntry(
            definition, 0, ordinal + 1, body, HookSecretScrubSet.capture()
        )
        if stop:
            owner.accept_continuation(
                scope=scope,
                causal_ref=StopRef("turn:1", "entry:1", object()),
                source_entry=entry,
                reason=body,
            )
        else:
            owner.accept_sync(
                scope=scope,
                causal_ref=PostToolRef(
                    "turn:1",
                    f"call:{ordinal}",
                    f"result:{ordinal}",
                    f"entry:{ordinal}",
                    "SUCCESS",
                    object(),
                ),
                entries=(entry,),
            )
    collector = _source_upper_collector(tmp_path, hook_owner=owner)
    candidate, reservation = collector.freeze_hook_context_source(
        scope_kind="ROOT", child_task_id=None, estimator=estimator
    )
    assert isinstance(candidate, ContextSourceCandidate)
    assert reservation is not None
    try:
        assert body in candidate.variants[0].text
        _assert_source_variant_wire_upper(candidate, upper, wire_api=wire_api)
    finally:
        reservation.retire()


@pytest.mark.parametrize("wire_api", ("chat", "responses"))
def test_fresh_plan_upper_bounds_actual_source_collection(
    tmp_path, wire_api: str
) -> None:
    from dataclasses import fields
    from pulsara_agent.model_input.contracts import (
        FrozenCanonicalCompileSnapshot,
        FrozenPlanHandoffCompileFact,
        FrozenPlanWorkflowCompileFact,
        build_tool_observation_freshness_fact,
        canonical_compile_snapshot_fingerprint,
        plan_handoff_compile_fact_fingerprint,
        plan_workflow_compile_fact_fingerprint,
    )
    from pulsara_agent.model_input.continuity import decode_runtime_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.primitives.plan_workflow import (
        PlanHandoffKind,
        PlanWorkflowEnteredBy,
        PlanWorkflowStatus,
    )
    from pulsara_agent.primitives.run_permission import (
        RunPermissionAdmissionSource,
        RunPermissionOverlay,
        build_run_permission_snapshot,
    )
    from tests.support.round3 import StructuredToolPort
    from tests.test_round3_structured_model_input_compiler import (
        _canonical_facts,
        _collect_context_sources,
        _snapshot,
    )

    collector = _source_upper_collector(tmp_path)
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        .model_surface
    )
    base = _canonical_facts(
        _snapshot(
            FrozenProviderInputItem(
                FrozenProviderInputItemKind.PLAN_CONTINUATION,
                "entry:1",
                1,
                "turn:test",
                (
                    LLMTextPart(
                        '{"pulsara_plan_continuation":{"status":"ACTIVE","transition":"ENTERED_PLAN"}}'
                    ),
                ),
                input_origin=CanonicalInputOriginKind.PLAN_CONTINUATION,
            )
        )
    )
    identity = base.canonical_input.identity
    for requested_mode in PermissionMode:
        original_permission = build_run_permission_snapshot(
            snapshot_id="permission:before-plan",
            requested_mode=requested_mode,
            effective_mode=requested_mode,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        uppers = {
            decode_runtime_observation(message).source_kind: message
            for message in collector.freeze_fresh_entered_plan_source_upper(
                original_permission
            )
        }
        permission = build_run_permission_snapshot(
            snapshot_id="permission:plan",
            requested_mode=requested_mode,
            effective_mode=PermissionMode.READ_ONLY,
            admission_source=RunPermissionAdmissionSource.RUNTIME_PLAN_CONTINUATION,
            overlay=RunPermissionOverlay.PLAN_READ_ONLY,
            plan_context_ordinal_at_admission=1,
            plan_workflow_id="workflow:plan",
            plan_workflow_revision_at_admission=1,
            inherited_from_turn_id=identity.turn_id,
        )
        common = dict(
            session_id=identity.session_id,
            workspace_id="workspace:test",
            workflow_id="workflow:plan",
            workflow_ordinal=1,
            workflow_status=PlanWorkflowStatus.ACTIVE,
            resume_permission_mode=requested_mode,
        )
        workflow_values = dict(
            **common,
            turn_id=identity.turn_id,
            permission_snapshot_id=permission.snapshot_id,
            permission_snapshot_fingerprint=permission.snapshot_fingerprint,
            current_workflow_revision=1,
            entered_by=PlanWorkflowEnteredBy.AGENT,
            permission_contract_id=permission.permission_contract_id,
            permission_contract_fingerprint=permission.permission_contract_fingerprint,
        )
        workflow = FrozenPlanWorkflowCompileFact(
            **workflow_values,
            fact_fingerprint=plan_workflow_compile_fact_fingerprint(
                SimpleNamespace(**workflow_values)
            ),
        )
        handoff_values = dict(
            **common,
            target_turn_id=identity.turn_id,
            carrier_entry_id=identity.initial_entry_id,
            carrier_entry_sequence=1,
            workflow_revision_at_transition=1,
            interaction_id=None,
            handoff_kind=PlanHandoffKind.ENTERED_PLAN,
            transition_semantic_digest="sha256:" + "1" * 64,
        )
        handoff = FrozenPlanHandoffCompileFact(
            **handoff_values,
            fact_fingerprint=plan_handoff_compile_fact_fingerprint(
                SimpleNamespace(**handoff_values)
            ),
        )
        values = {
            field.name: getattr(base, field.name)
            for field in fields(base)
            if field.name != "canonical_read_cut_fingerprint"
        }
        values.update(
            run_permission_snapshot=permission,
            plan_workflow_fact=workflow,
            plan_handoff_fact=handoff,
            tool_observation_freshness_fact=build_tool_observation_freshness_fact(
                session_id=identity.session_id,
                workspace_id="workspace:test",
                current_turn_id=identity.turn_id,
                current_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                current_initial_entry_sequence=1,
                immediate_predecessor_turn_id="turn:before-plan",
            ),
        )
        facts = FrozenCanonicalCompileSnapshot(
            **values,
            canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
                SimpleNamespace(**values)
            ),
        )
        sources = _collect_context_sources(
            collector,
            activation_subject=None,
            activation_text="",
            tool_surface=surface,
            canonical_facts=facts,
        )
        actual_sources = {
            c.source_kind: c for c in sources.candidates if c.source_kind in uppers
        }
        assert actual_sources.keys() == uppers.keys()
        for kind, candidate in actual_sources.items():
            _assert_source_variant_wire_upper(
                candidate, uppers[kind], wire_api=wire_api
            )


def test_compaction_recovery_requires_the_exact_admitted_writer_owner() -> None:
    async def scenario() -> None:
        host = object.__new__(KernelHostSession)
        host._lock = asyncio.Lock()
        host._compaction = HostCompactionRuntimeOwner()
        host._compaction_write_reservations = {}
        host._root_compaction_write_changed = asyncio.Event()
        host._root_compaction_write_changed.set()
        host._active_task = None
        host._active_turn_id = None
        host._pending_root_successor = None
        host._external_new_turn_accepting = True
        host._plan_exit_fence = False
        host._closing = False
        host._closed = False
        host._queue_wake = asyncio.Event()
        host._monitor_wake = asyncio.Event()
        async with host._lock:
            exact = host._reserve_compaction_write_locked(
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
        foreign = CompactionWriteReservation(ModelInputScopeKind.ROOT, None)
        scope = CompactionScope(
            session_id="session:one",
            workspace_id="workspace:one",
            turn_id="turn:one",
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        owner_task = asyncio.current_task()
        assert owner_task is not None

        with pytest.raises(RuntimeError, match="not the exact owner"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:foreign",
                owner_task,
                foreign,
                "turn:new",
            )
        with pytest.raises(RuntimeError, match="admitted writer"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:none",
                owner_task,
                None,
                "turn:new",
            )

        await host._install_compaction_fence(
            scope,
            CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            "attempt:exact",
            owner_task,
            exact,
            "turn:new",
        )
        assert host._compaction.is_fenced(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        await host._remove_compaction_fence(scope, "attempt:exact", owner_task)
        await host._release_compaction_write_reservation(exact)

        host._external_new_turn_accepting = False
        host._active_task = owner_task
        host._active_turn_id = "turn:new"
        with pytest.raises(RuntimeError, match="does not exact-join"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:wrong-pending",
                owner_task,
                None,
                "turn:wrong",
            )

        not_owner = asyncio.create_task(asyncio.sleep(0))
        host._active_task = not_owner
        with pytest.raises(RuntimeError, match="does not exact-join"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:not-owner",
                owner_task,
                None,
                "turn:new",
            )
        await not_owner

        host._active_task = owner_task
        await host._install_compaction_fence(
            scope,
            CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            "attempt:pending-exact",
            owner_task,
            None,
            "turn:new",
        )
        await host._remove_compaction_fence(
            scope, "attempt:pending-exact", owner_task
        )

    asyncio.run(scenario())


def test_chat_and_responses_mixed_image_wire_goldens_preserve_occurrences() -> None:
    image = _image()
    message = _message(LLMTextPart("before"), image, LLMTextPart("between"), image)
    encoded = base64.b64encode(image.immutable_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"

    assert chat_semantic_wire_group(message) == (
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                },
                {"type": "text", "text": "between"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                },
            ],
        },
    )
    assert responses_semantic_wire_group(message) == (
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "before"},
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "auto",
                },
                {"type": "input_text", "text": "between"},
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "auto",
                },
            ],
        },
    )


def test_pure_image_is_one_formal_user_wire_item_and_pure_text_is_unchanged() -> None:
    pure_image = _message(_image())
    chat_image = chat_semantic_wire_group(pure_image)
    responses_image = responses_semantic_wire_group(pure_image)
    assert chat_image[0]["role"] == "user"
    assert [part["type"] for part in chat_image[0]["content"]] == ["image_url"]
    assert responses_image[0]["role"] == "user"
    assert [part["type"] for part in responses_image[0]["content"]] == ["input_image"]

    text = _message(LLMTextPart("one"), LLMTextPart("two"))
    assert chat_semantic_wire_group(text) == ({"role": "user", "content": "one\ntwo"},)
    assert responses_semantic_wire_group(text) == (
        {"role": "user", "content": "one\ntwo"},
    )


@pytest.mark.parametrize(
    ("wire_kind", "counted_content"),
    (
        (
            "chat",
            [
                {"type": "text", "text": "caption"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,",
                        "detail": "auto",
                    },
                },
            ],
        ),
        (
            "responses",
            [
                {"type": "input_text", "text": "caption"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,",
                    "detail": "auto",
                },
            ],
        ),
    ),
)
def test_v2_final_wire_quote_elides_only_formal_payload_and_adds_d1(
    wire_kind: str,
    counted_content: list[dict[str, object]],
) -> None:
    estimator = PulsaraHeuristicTokenEstimatorV2()
    image = _image()
    source = _message(LLMTextPart("caption"), image)
    if wire_kind == "chat":
        item = chat_semantic_wire_group(source)[0]
    else:
        item = responses_semantic_wire_group(source)[0]
    fixed = {"model": "model", "input": []}
    estimate = estimator.estimate_final_wire_json_components(
        fixed_context=fixed,
        ordered_input_items=(item,),
        ordered_input_sources=(source,),
    )
    visual = estimate_image_visual_tokens(width=512, height=512)
    expected_counted = {"role": "user", "content": counted_content}
    assert estimate.visual_image_tokens == visual == 316
    assert estimate.text_and_framing_tokens == (
        3
        + estimator.estimate_json(fixed)
        + 4
        + estimator.estimate_json(expected_counted)
    )
    assert estimate.total_input_tokens == estimate.text_and_framing_tokens + visual
    assert len(canonical_json_bytes(item)) > len(canonical_json_bytes(expected_counted))


def test_v2_estimator_counts_repeated_images_and_does_not_scan_text() -> None:
    estimator = PulsaraHeuristicTokenEstimatorV2()
    image = _image(width=1_024, height=1_024)
    repeated = _message(image, image, image)
    item = responses_semantic_wire_group(repeated)[0]
    estimate = estimator.estimate_final_wire_json_components(
        fixed_context={},
        ordered_input_items=(item,),
        ordered_input_sources=(repeated,),
    )
    assert estimate.visual_image_tokens == 3 * 1_198

    text = _message(LLMTextPart("data:image/png;base64," + "A" * 4_096))
    text_item = responses_semantic_wire_group(text)[0]
    text_estimate = estimator.estimate_final_wire_json_components(
        fixed_context={},
        ordered_input_items=(text_item,),
        ordered_input_sources=(text,),
    )
    assert text_estimate.visual_image_tokens == 0
    assert text_estimate.total_input_tokens == (
        3
        + estimator.estimate_json({})
        + estimator.estimate_wire_json_component(text_item)
    )


def test_v2_final_wire_traversal_rejects_source_or_payload_drift() -> None:
    estimator = PulsaraHeuristicTokenEstimatorV2()
    source = _message(_image(b"abc"))
    item = responses_semantic_wire_group(source)[0]

    with pytest.raises(ValueError, match="base64 differs"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(_message(_image(b"abd")),),
        )
    with pytest.raises(ValueError, match="MIME"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(_message(_image(b"abc", media_type="image/jpeg")),),
        )
    with pytest.raises(ValueError, match="do not align"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(),
        )
    with pytest.raises(ValueError, match="validated semantic source"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(None,),
        )
    with pytest.raises(ValueError, match="validated semantic source"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(_message(LLMTextPart("not an image")),),
        )


def test_v2_fact_freezes_d1_and_formal_wire_accounting() -> None:
    fact = PulsaraHeuristicTokenEstimatorV2().fact
    assert fact.estimator_id == "pulsara_heuristic"
    assert fact.estimator_version == "v2"
    assert fact.image_grid_pixels == 28
    assert fact.image_scale_numerator == 7
    assert fact.image_scale_denominator == 8
    assert fact.image_min_tokens == 256
    assert fact.image_wire_accounting_contract == (
        "formal_user_image_parts:v1-payload-elided-per-item-rounding"
    )


def _recent(entry_id: str, sequence: int, *parts: LLMTextPart | LLMImagePart):
    return RecentHumanMessageProof(
        entry_id=entry_id,
        entry_sequence=sequence,
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        content=FrozenPromptContent(tuple(parts)),
    )


def test_ordinary_compaction_reuses_one_recent_window_then_drops_oldest() -> None:
    recent = (
        _recent("entry:a", 1, LLMTextPart("a")),
        _recent("entry:b", 2, LLMTextPart("b")),
        _recent("entry:c", 3, LLMTextPart("c")),
    )

    assert _ordinary_recent_suffixes(recent) == (
        recent,
        recent[1:],
        recent[2:],
        (),
    )


def test_ordinary_recent_retry_is_limited_to_closed_resource_failures() -> None:
    assert _can_retry_compaction_with_smaller_recent(
        CompactionReclaimUnavailable("not enough reclaim")
    )
    assert _can_retry_compaction_with_smaller_recent(
        CompactionPlanningError(
            "compaction successor lacks executable final-wire admission"
        )
    )
    assert _can_retry_compaction_with_smaller_recent(
        StructuredModelInputCompileError(
            ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
        )
    )
    assert not _can_retry_compaction_with_smaller_recent(
        StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    )
    assert not _can_retry_compaction_with_smaller_recent(
        CompactionPlanningError("destination modality invariant failed")
    )
    assert not _can_retry_compaction_with_smaller_recent(RuntimeError("drift"))


def test_dry_wire_measurement_closes_text_only_target_before_adoption() -> None:
    target_fact = SimpleNamespace(input_modalities=("text",))
    call = SimpleNamespace(target=SimpleNamespace(fact=target_fact))
    binding = SimpleNamespace(
        target_fact=target_fact,
        binding_fingerprint="binding:test",
        tool_surface=SimpleNamespace(tool_specs=()),
    )
    semantic_input = SimpleNamespace(
        compile_binding_fingerprint="binding:test",
        tools=(),
        messages=(_message(_image()),),
    )

    with pytest.raises(
        ModelTargetCapabilityMismatch,
        match="does not declare image input",
    ):
        freeze_provider_wire_measurement(
            call=call,
            binding=binding,
            native_projection_set=SimpleNamespace(),
            semantic_input=semantic_input,
            replay_hydration=None,
        )

    mismatch = ModelTargetCapabilityMismatch(
        "model target does not declare image input"
    )
    assert _can_enter_model_switch_tier_three(mismatch)
    assert not _can_retry_compaction_with_smaller_recent(mismatch)


def test_large_image_tail_uses_descriptor_budget_then_full_d2_charge() -> None:
    output = BytesIO()
    Image.new("RGB", (1024, 1024), (11, 23, 41)).save(
        output, "PNG", compress_level=0
    )
    payload = output.getvalue()
    assert len(payload) > 2 << 20
    image = LLMImagePart("image/png", payload, 1024, 1024)
    scope = CompactionScope(
        "session:i36",
        "workspace:i36",
        "turn:i36",
        ModelInputScopeKind.ROOT,
        None,
    )

    def item_with_occurrences(count: int) -> FrozenProviderInputItem:
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER,
            f"entry:i36:{count}",
            1,
            "turn:i36",
            (image,) * count,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        )

    policy = ResolvedCompactionPolicy()
    bounds = resolved_compaction_headroom_bounds()
    admitted = item_with_occurrences(1)
    admitted_tail = freeze_compaction_canonical_range(
        scope=scope,
        effective_materialization_lineage_floor=0,
        source_through_sequence=1,
        ordered_items=(admitted,),
        closures=(),
        late_outcomes=(),
    )
    admitted_c = provider_input_item_canonical_expanded_bytes(admitted)
    admitted_l = llm_content_logical_bytes(admitted.content)
    assert admitted_tail.canonical_utf8_bytes < (
        policy.maximum_retained_tail_utf8_bytes
    )
    assert admitted_c < bounds.soft_canonical_expanded_byte_limit
    assert admitted_l < bounds.soft_epoch_logical_byte_limit
    assert not crosses_compaction_resource_headroom(
        selected_item_count=1,
        selected_canonical_expanded_bytes=admitted_c,
        continuity_epoch_logical_bytes=admitted_l,
    )

    overbound = item_with_occurrences(4)
    overbound_tail = freeze_compaction_canonical_range(
        scope=scope,
        effective_materialization_lineage_floor=0,
        source_through_sequence=1,
        ordered_items=(overbound,),
        closures=(),
        late_outcomes=(),
    )
    overbound_c = provider_input_item_canonical_expanded_bytes(overbound)
    overbound_l = llm_content_logical_bytes(overbound.content)
    assert overbound_tail.canonical_utf8_bytes < (
        policy.maximum_retained_tail_utf8_bytes
    )
    assert (
        overbound_c >= bounds.soft_canonical_expanded_byte_limit
        or overbound_l >= bounds.soft_epoch_logical_byte_limit
    )
    assert crosses_compaction_resource_headroom(
        selected_item_count=1,
        selected_canonical_expanded_bytes=overbound_c,
        continuity_epoch_logical_bytes=overbound_l,
    )


def test_text_only_projection_p_preserves_order_occurrences_and_is_idempotent() -> None:
    source = FrozenPromptContent(
        (
            LLMTextPart("before"),
            _image(b"same"),
            LLMTextPart("between"),
            _image(b"same"),
        )
    )

    projected = project_prompt_content_for_text_only_handover(source)

    assert projected.parts == (
        LLMTextPart("before"),
        LLMTextPart("[Image omitted]"),
        LLMTextPart("between"),
        LLMTextPart("[Image omitted]"),
    )
    assert project_prompt_content_for_text_only_handover(projected) == projected


def test_history_and_recent_image_checks_exclude_only_the_exact_active_request() -> (
    None
):
    old_image = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:old",
        source_entry_sequence=1,
        source_turn_id="turn:old",
        content=(_image(b"old"),),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )
    active_image = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:active",
        source_entry_sequence=2,
        source_turn_id="turn:active",
        content=(_image(b"active"),),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )

    def canonical_read(*items: FrozenProviderInputItem) -> SimpleNamespace:
        canonical = SimpleNamespace(
            identity=SimpleNamespace(initial_entry_id="entry:active"),
            items=items,
        )
        return SimpleNamespace(
            dispatch_read=SimpleNamespace(
                compile_snapshot=SimpleNamespace(canonical_input=canonical)
            )
        )

    assert compaction_effective_history_has_image(canonical_read(active_image)) is False
    assert (
        compaction_effective_history_has_image(canonical_read(old_image, active_image))
        is True
    )
    assert recent_human_window_has_image(
        (_recent("entry:recent", 3, LLMTextPart("text"), _image()),)
    )
    assert not recent_human_window_has_image(
        (_recent("entry:recent", 3, LLMTextPart("text")),)
    )


@pytest.mark.parametrize(
    ("modalities", "expected"),
    (
        (None, False),
        (("image",), False),
        (("audio",), False),
        (("text", "image"), False),
        (("text",), True),
    ),
)
def test_text_only_handover_requires_known_text_without_image(
    modalities: tuple[str, ...] | None,
    expected: bool,
) -> None:
    target = SimpleNamespace(
        target=SimpleNamespace(fact=SimpleNamespace(input_modalities=modalities))
    )
    assert _target_is_known_text_only(target) is expected


def test_post_response_charge_includes_assistant_arguments_and_result_uppers() -> None:
    arguments = freeze_json({"path": "\\\\quoted", "line": 7})
    completed = CompletedAssistantMessage(
        draft_identity="entry:assistant",
        blocks=(
            CompletedTextBlock("block:text", "answer"),
            CompletedToolCallBlock(
                "block:call",
                "call:1",
                "read_file",
                arguments,
            ),
        ),
        public_text="answer",
    )
    item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        source_entry_id="entry:assistant",
        source_entry_sequence=4,
        source_turn_id="turn:active",
        content=(LLMTextPart("answer"),),
        tool_calls=(ProviderToolCall("call:1", "read_file", arguments),),
    )
    expected_canonical = len(b"answer") + len(
        canonical_json_bytes({"path": "\\\\quoted", "line": 7})
    )

    assert _completed_assistant_canonical_bytes(completed) == expected_canonical
    assert provider_input_item_canonical_expanded_bytes(item) == expected_canonical

    call = completed.blocks[1]
    assert isinstance(call, CompletedToolCallBlock)
    ordinary_canonical, ordinary_logical, ordinary_items, ordinary_messages = (
        _ordinary_result_followup_upper(call)
    )
    assert ordinary_canonical >= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
    assert ordinary_items == len(ordinary_messages) == 2
    assert ordinary_logical == sum(
        provider_neutral_message_logical_bytes(message) for message in ordinary_messages
    )

    plan_canonical, plan_logical, plan_items, plan_messages = (
        _plan_result_followup_upper(call)
    )
    assert plan_canonical == PLAN_CONTROL_RESULT_INLINE_HARD_BYTES
    assert plan_items == len(plan_messages) == 1
    assert plan_logical == provider_neutral_message_logical_bytes(plan_messages[0])

    (
        continuation_canonical,
        continuation_logical,
        continuation_items,
        continuation_messages,
    ) = _entered_plan_continuation_followup_upper()
    assert continuation_canonical == len(
        canonical_json_bytes(
            {
                "transition": "ENTERED_PLAN",
                "workflow_id": "plan-workflow:" + ("0" * 64),
            }
        )
    )
    assert continuation_items == len(continuation_messages) == 1
    assert continuation_logical == provider_neutral_message_logical_bytes(
        continuation_messages[0]
    )
    assert json.loads(join_text_content(continuation_messages[0].content)) == {
        "pulsara_plan_continuation": {
            "status": "ACTIVE",
            "transition": "ENTERED_PLAN",
        }
    }


def test_typed_tool_result_canonical_charge_includes_prompt_body_and_image_bytes() -> None:
    from tests.test_round3_structured_model_input_compiler import _tool_result

    image = _image(b"typed-image-bytes", width=12, height=8)
    content = FrozenPromptContent((LLMTextPart("attached"), image))
    item = replace(
        _tool_result("plain", sequence=7, turn_id="turn:image", artifact=False),
        content=content.parts,
        tool_result_body_text=frozen_tool_result_public_text(content),
        tool_call_ordinal=0,
        tool_call_arguments=freeze_json({"path": "/tmp/image.png"}),
    )

    assert provider_input_item_canonical_expanded_bytes(item) == (
        len(canonical_prompt_body_bytes(content)) + len(image.immutable_bytes)
    )

    late_content = FrozenPromptContent(
        (
            LLMTextPart(
                '{"schema_version":"late_tool_outcome_observation.v1",'
                '"tool_call_id":"call:7","result_state":"SUCCESS",'
                '"result":"Image loaded."}'
            ),
            image,
        )
    )
    late = replace(
        item,
        item_kind=FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        content=late_content.parts,
    )
    assert provider_input_item_canonical_expanded_bytes(late) == (
        len(canonical_prompt_body_bytes(late_content)) + len(image.immutable_bytes)
    )


@pytest.mark.parametrize(
    "message_kind",
    tuple(ToolResultLogicalMessageKind),
)
@pytest.mark.parametrize("wire_api", ("chat", "responses"))
def test_tool_result_full_upper_bounds_escaping_body_wire_and_d1(
    message_kind: ToolResultLogicalMessageKind,
    wire_api: str,
) -> None:
    tool_call_id = "call:1"
    timing = freeze_tool_observation_timing_fact(
        session_id="session:1",
        turn_id="turn:1",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        observation_duration_microseconds=0,
        tool_reported_duration_microseconds=0,
        observation_origin=ToolObservationOrigin.BUILTIN,
    )

    def render(body_chars: int):
        return render_provider_tool_result_logical_message(
            message_kind=message_kind,
            tool_call_id=tool_call_id,
            body="\\" * body_chars,
            result_state="SUCCESS",
            timing=timing,
            citation_handle=None,
            model_visible_memory_ids=(),
        )

    low = 0
    high = MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    actual = render(0)
    while low <= high:
        middle = (low + high) // 2
        candidate = render(middle)
        if (
            candidate.logical_utf8_bytes
            <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        ):
            actual = candidate
            low = middle + 1
        else:
            high = middle - 1
    upper = conservative_tool_result_logical_message(
        message_kind=message_kind,
        tool_call_id=tool_call_id,
    )

    if wire_api == "chat":
        actual_item = chat_semantic_wire_group(actual.message)[0]
        upper_item = chat_semantic_wire_group(upper.message)[0]
    else:
        actual_item = responses_semantic_wire_group(actual.message)[0]
        upper_item = responses_semantic_wire_group(upper.message)[0]

    estimator = PulsaraHeuristicTokenEstimatorV2()
    assert actual.logical_utf8_bytes <= upper.logical_utf8_bytes
    assert len(canonical_json_bytes(actual_item)) <= len(
        canonical_json_bytes(upper_item)
    )
    assert estimator.estimate_wire_json_component(
        actual_item
    ) <= estimator.estimate_wire_json_component(upper_item)


@pytest.mark.parametrize("wire_api", ("chat", "responses"))
def test_root_completion_batch_upper_bounds_every_closed_projection(
    wire_api: str,
) -> None:
    upper_canonical, upper_logical, upper_items, upper_messages = (
        _root_completion_followup_upper()
    )
    assert upper_items == len(upper_messages) == 16
    upper_message = upper_messages[0]
    control_fill = "\x01"
    task_id = control_fill * 512
    dependencies = tuple(control_fill * 509 + f"{index:03d}" for index in range(16))
    bodies = [
        build_subagent_completion_storage_body(
            task_id=task_id,
            task_key="a" * 64,
            label=control_fill * 256,
            display_role=control_fill * 256,
            profile=max(SubagentProfileKind, key=lambda item: len(item.value)).value,
            status=SubagentTaskStatus.FAILED,
            terminal_reason=reason.value,
            terminal_public_detail=(
                control_fill * MAXIMUM_TERMINAL_PUBLIC_DETAIL_UTF8_BYTES
            ),
            failed_dependency_task_ids=dependencies,
            result_id=None,
            result_source=None,
            result_summary=None,
        )
        for reason in SubagentTerminalReason
    ]
    bodies.append(
        build_subagent_completion_storage_body(
            task_id=task_id,
            task_key="a" * 64,
            label=control_fill * 256,
            display_role=control_fill * 256,
            profile=max(SubagentProfileKind, key=lambda item: len(item.value)).value,
            status=SubagentTaskStatus.COMPLETED,
            terminal_reason=None,
            terminal_public_detail=None,
            failed_dependency_task_ids=(),
            result_id=control_fill * 512,
            result_source=SubagentResultSource.EXPLICIT.value,
            result_summary=control_fill * MAXIMUM_RESULT_SUMMARY_UTF8_BYTES,
        )
    )
    estimator = PulsaraHeuristicTokenEstimatorV2()
    if wire_api == "chat":
        upper_wire = chat_semantic_wire_group(upper_message)[0]
    else:
        upper_wire = responses_semantic_wire_group(upper_message)[0]
    for body in bodies:
        actual_text = project_subagent_completion_for_provider(
            body, source_task_id=task_id
        )
        actual_message = LLMMessage.user(actual_text)
        if wire_api == "chat":
            actual_wire = chat_semantic_wire_group(actual_message)[0]
        else:
            actual_wire = responses_semantic_wire_group(actual_message)[0]
        assert len(actual_text.encode("utf-8")) * upper_items <= upper_canonical
        assert (
            provider_neutral_message_logical_bytes(actual_message) * upper_items
            <= upper_logical
        )
        assert len(canonical_json_bytes(actual_wire)) <= len(
            canonical_json_bytes(upper_wire)
        )
        assert estimator.estimate_wire_json_component(
            actual_wire
        ) <= estimator.estimate_wire_json_component(upper_wire)


def _post_response_quote(
    *,
    current_canonical: int = 0,
    assistant_canonical: int = 0,
    followup_canonical: int = 0,
    current_logical: int = 0,
    assistant_logical: int = 0,
    followup_logical: int = 0,
    current_items: int = 0,
    followup_items: int = 0,
    wire: ProviderFollowupWireResourceQuote | None = None,
) -> FrozenPostResponseResourceQuote:
    return FrozenPostResponseResourceQuote(
        current_canonical_expanded_bytes=current_canonical,
        actual_assistant_canonical_bytes=assistant_canonical,
        bounded_followup_canonical_bytes=followup_canonical,
        canonical_upper_after=(
            current_canonical + assistant_canonical + followup_canonical
        ),
        current_epoch_logical_bytes=current_logical,
        actual_assistant_logical_bytes=assistant_logical,
        bounded_followup_logical_bytes=followup_logical,
        logical_upper_after=current_logical + assistant_logical + followup_logical,
        current_canonical_items=current_items,
        bounded_followup_items=followup_items,
        item_upper_after=current_items + 1 + followup_items,
        followup_wire=wire,
    )


@pytest.mark.parametrize(
    ("quote", "budget", "reason"),
    (
        (
            _post_response_quote(current_canonical=(16 << 20) + 1),
            10,
            "CANONICAL_BYTES",
        ),
        (
            _post_response_quote(current_logical=(64 << 20) + 1),
            10,
            "EPOCH_LOGICAL_BYTES",
        ),
        (_post_response_quote(current_items=4_096), 10, "CANONICAL_ITEMS"),
        (
            _post_response_quote(
                wire=ProviderFollowupWireResourceQuote((64 << 20) + 1, 1, 1)
            ),
            10,
            "FOLLOWUP_WIRE_BYTES",
        ),
        (
            _post_response_quote(wire=ProviderFollowupWireResourceQuote(1, 11, 1)),
            10,
            "FOLLOWUP_INPUT_TOKENS",
        ),
    ),
)
def test_post_response_gate_reports_each_closed_resource_boundary(
    quote: FrozenPostResponseResourceQuote,
    budget: int,
    reason: str,
) -> None:
    with pytest.raises(OutputResourceInterruption) as raised:
        ConversationKernelRunner._require_post_response_resources(
            quote,
            effective_input_budget_tokens=budget,
        )

    assert raised.value.reason == reason
