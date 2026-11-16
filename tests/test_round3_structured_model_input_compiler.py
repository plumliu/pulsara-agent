"""Current compiler, append-only prefix and provider-wire contracts."""

from __future__ import annotations
from tests.support.model_config import scheduled_task_service
from tests.support.images import bound_test_image
from pulsara_agent.identity import derive_id
import ast
import asyncio
from copy import copy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
from time import monotonic, sleep
from types import SimpleNamespace
from typing import get_type_hints
from zoneinfo import ZoneInfo
import pytest
from pulsara_agent.conversation_kernel.assembler import CompletedToolCallBlock
from pulsara_agent.conversation_kernel.context_sources import (
    KernelContextSourceCollector,
)
from pulsara_agent.conversation_kernel.direct_model import (
    CompletedProviderModelExecution,
    DirectKernelModelPort,
)
from pulsara_agent.conversation_kernel.input_continuity import (
    ProviderInputContinuityConflict,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)
from pulsara_agent.conversation_kernel.memory.hints import MEMORY_WRITE_HINT_BODY
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from pulsara_agent.conversation_kernel.compaction.planner import (
    enumerate_safe_summary_prefixes,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    compaction_summary_request,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.extensions import OperationalHookType
from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolAuthorizationKind,
    KernelToolInvocationContext,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MessageRole,
    join_text_content,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    build_chat_completions_payload,
    project_chat_context_bearing_payload_fields,
)
from pulsara_agent.llm.adapters.openai.responses import (
    build_responses_payload,
    project_responses_context_bearing_payload_fields,
)
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.request import (
    LLMContext,
    provider_assistant_message_public_projection_fingerprint,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    CapabilityActivationSubjectKind,
    ContextBudgetClass,
    ContextChannel,
    ContextPublicDiagnosticCode,
    ContextSourceCandidate,
    ContextSourceKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    ModelInputScopeKind,
    ProviderToolCall,
    StructuredModelInputCompileError,
    StructuredModelInputCompileRequest,
    provider_input_item_text,
    model_input_compile_binding_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    EmptyScopeColdStart,
    ProviderInputContinuityScope,
    _issue_empty_scope_cold_start,
)
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.model_input.diagnostics import (
    CompileDecisionSampleKind,
    ModelInputCompileOperationalProjection,
    project_model_input_compile_observation,
)
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderStreamTerminal,
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.terminal_process.models import TerminalRequest, TerminalStatus
from tests.support.round3 import (
    StructuredToolPort,
    new_test_provider_input_continuity_owner,
    prepare_test_direct_tool_surface,
)
from pulsara_agent.model_input.continuity import InstalledObservationState

from tests.support.model_input import (
    _test_round9_capability_views,
    _collect_context_sources,
    _snapshot,
    _user,
    _tool_result,
    _prepared_request,
    _canonical_facts,
    _append_frontier,
    _append_anchor,
    _prepare_append_candidate,
    _compile_and_install_append,
    _replay_manifest_cut,
    _permission_snapshot,
    _round7_previous_fact,
    _with_round7_previous,
    _approved_plan_compile_facts,
    _CountingEstimator,
    _Capability,
    _MutableUnkeyedTimezone,
    _candidate,
    _sources,
    _PREPARED_MODEL_CALLS,
)


def test_approved_plan_restores_complete_exact_text_once(tmp_path):
    facts, plan = _approved_plan_compile_facts()
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=_Capability(),
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(conversation_scope_kind=ModelInputScopeKind.ROOT)
        .model_surface
    )
    sources = _collect_context_sources(
        collector,
        activation_subject=None,
        activation_text="",
        tool_surface=surface,
        canonical_facts=facts,
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(facts.canonical_input, sources, canonical_facts=facts)
    )
    observations = [
        json.loads(join_text_content(m.content))["pulsara_runtime_observation"]
        for m in compiled.messages
        if "pulsara_runtime_observation" in join_text_content(m.content)
    ]
    approved = [o for o in observations if o["source"] == "APPROVED_PLAN"]
    assert len(approved) == 1 and approved[0]["body"][1]["text"] == plan
    assert sum(join_text_content(m.content).count(plan) for m in compiled.messages) == 1


def test_round3_system_placement_is_independent_of_input_order():
    candidates = (
        _candidate(ContextSourceKind.ACTIVE_SKILL, "read the real skill", placement=52),
        _candidate(ContextSourceKind.SKILL_CATALOG, "full catalogue", placement=1),
        _candidate(ContextSourceKind.TURN_CONTEXT, "working environment", placement=30),
        _candidate(ContextSourceKind.BASE_SYSTEM, "BASE"),
    )
    request = _prepared_request(_snapshot(_user("hello")), _sources(*candidates))
    request = replace(
        request,
        dispatch_anchor_entry_id=request.canonical_input.items[-1].source_entry_id,
    )
    compiled = StructuredModelInputCompiler().compile(request)
    assert compiled.system_prompt == "BASE"
    assert join_text_content(compiled.messages[0].content) == "full catalogue"
    assert [value["source"] for value in _observations(compiled.messages)] == [
        "TURN_CONTEXT",
        "ACTIVE_SKILL",
    ]
    assert join_text_content(compiled.messages[-1].content) == "hello"


@pytest.mark.parametrize(
    "base_system_prompt",
    (DEFAULT_SYSTEM_PROMPT, "Custom root instructions"),
    ids=("default", "custom"),
)
def test_effective_root_system_preserves_the_collected_base_system(
    tmp_path: Path,
    base_system_prompt: str,
) -> None:
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=_Capability(),  # type: ignore[arg-type]
        base_system_prompt=base_system_prompt,
        display_timezone=timezone.utc,
    )
    surface = (
        StructuredToolPort(object(), tool_names=())
        .snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
        )
        .model_surface
    )
    canonical_facts = _canonical_facts()
    sources = _collect_context_sources(
        collector,
        activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        activation_text="inspect the project",
        tool_surface=surface,
        canonical_facts=canonical_facts,
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            canonical_facts.canonical_input,
            sources,
            canonical_facts=canonical_facts,
        )
    )

    base = next(
        candidate
        for candidate in sources.candidates
        if candidate.source_kind is ContextSourceKind.BASE_SYSTEM
    )
    assert compiled.system_prompt == base.text
    assert compiled.system_prompt == base_system_prompt


def test_round3_must_keep_source_stays_complete_in_unadmitted_candidate():
    request = _prepared_request(
        _snapshot(),
        _sources(_candidate(ContextSourceKind.BASE_SYSTEM, "required " * 100)),
        budget=4,
    )
    compiler = StructuredModelInputCompiler()
    compiled = compiler.compile(request)
    assert (
        compiled.system_prompt == "required " * 100
        and compiled.source_decisions[0].included
    )
    with pytest.raises(StructuredModelInputCompileError) as rejected:
        compiler.compile(request, omitted_sources=(ContextSourceKind.BASE_SYSTEM,))
    assert rejected.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID


def test_round3_active_skill_and_tool_schema_are_preserved_before_wire_admission() -> (
    None
):
    compiler = StructuredModelInputCompiler()
    active = _candidate(ContextSourceKind.ACTIVE_SKILL, "active " * 100)
    active_request = _prepared_request(_snapshot(), _sources(active), budget=4)
    active_candidate = compiler.compile(active_request)
    assert any(
        "active " * 100 in p.text
        for m in active_candidate.messages
        for p in m.content
        if isinstance(p, LLMTextPart)
    )
    assert next(
        item
        for item in active_candidate.source_decisions
        if item.source_kind is ContextSourceKind.ACTIVE_SKILL
    ).included
    schema_request = _prepared_request(
        _snapshot(), _sources(), budget=4, tool_names=("read_file",)
    )
    schema_candidate = compiler.compile(schema_request)
    assert not hasattr(schema_candidate, "final_estimate")
    assert (
        schema_candidate.tools == schema_request.compile_binding.tool_surface.tool_specs
    )


def test_round3_assistant_semantic_text_never_uses_parent_manifest() -> None:
    mutable_arguments = {"nested": {"value": 1}}
    frozen = freeze_json(mutable_arguments)
    call = ProviderToolCall("call:1", "exec_process", frozen)  # type: ignore[arg-type]
    tool_only = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        "entry:jfasfwsw2puygc3hrueckpvgba",
        1,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart(""),),
        tool_calls=(call,),
    )
    lowered = lower_canonical_item(
        tool_only,
    )
    assert lowered.message is not None
    assert lowered.message.role is MessageRole.ASSISTANT
    assert lowered.message.content == ()
    assert lowered.message.tool_calls[0].arguments == '{"nested":{"value":1}}'
    mutable_arguments["nested"]["value"] = 2  # type: ignore[index]
    assert lowered.message.tool_calls[0].arguments == '{"nested":{"value":1}}'

    mixed = replace(tool_only, content=(LLMTextPart("semantic assistant text"),))
    mixed_lowered = lower_canonical_item(
        mixed,
    )
    assert mixed_lowered.message is not None
    assert mixed_lowered.message.content == (LLMTextPart("semantic assistant text"),)
    assert "draft_identity" not in join_text_content(mixed_lowered.message.content)


def test_round3_lowerer_preserves_the_frozen_native_projection_without_recropping():
    body = "meta:\n  status: SUCCESS\ncontent:\n  - type: text\n    value: " + (
        "🙂" * 1000
    )
    item = _tool_result(body, sequence=2, turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu")
    lowered = lower_canonical_item(item)
    assert lowered.source is item
    assert lowered.message == LLMMessage.tool_result(
        body, tool_call_id=item.tool_call_id
    )


def test_tool_image_results_share_one_ordered_carrier_per_assistant_batch() -> None:
    first_image = bound_test_image("image/png", b"first", 3, 2)
    second_image = bound_test_image("image/png", b"second", 4, 2)

    def image_result(
        *, sequence: int, call_ordinal: int, image: LLMImagePart
    ) -> FrozenProviderInputItem:
        content = FrozenPromptContent((LLMTextPart("Image loaded."), image))
        return replace(
            _tool_result(
                "placeholder",
                sequence=sequence,
                turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
            ),
            content=content.parts,
            tool_call_id=f"call:{call_ordinal}",
            tool_request_entry_id="entry:5kfg7huz5awr2non2ldye5t3de",
            tool_result_body_text="Image loaded.",
            tool_call_ordinal=call_ordinal,
            tool_call_name="view_image",
            tool_call_arguments=freeze_json(
                (
                    {
                        "target": {
                            "kind": "PATH",
                            "path": f'/tmp/image "{call_ordinal}".png',
                        }
                    }
                    if call_ordinal == 0
                    else {"target": {"kind": "IMAGE_REF", "image_ref": image.image_ref}}
                )
            ),
        )

    items = (
        image_result(sequence=1, call_ordinal=1, image=second_image),
        image_result(sequence=2, call_ordinal=0, image=first_image),
        _user("continue", sequence=3),
    )
    snapshot = _snapshot(
        *items,
        canonical_expanded_bytes=sum(
            provider_input_item_canonical_expanded_bytes(item) for item in items
        ),
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            snapshot,
            _sources(_candidate(ContextSourceKind.BASE_SYSTEM, "BASE")),
        )
    )

    carriers = tuple(
        message
        for message in compiled.messages
        if message.role is MessageRole.USER
        and any(isinstance(part, LLMImagePart) for part in message.content)
    )
    assert len(carriers) == 1
    assert tuple(
        part.immutable_bytes
        for part in carriers[0].content
        if isinstance(part, LLMImagePart)
    ) == (b"first", b"second")
    payloads = tuple(
        json.loads(part.text)
        for part in carriers[0].content
        if isinstance(part, LLMTextPart) and part.text.startswith("{")
    )
    assert payloads == (
        {
            "tool_call_id": "call:0",
            "image": {"kind": "IMAGE_REF", "image_ref": first_image.image_ref},
        },
        {
            "tool_call_id": "call:1",
            "image": {"kind": "IMAGE_REF", "image_ref": second_image.image_ref},
        },
    )
    assert (
        sum(message.role is MessageRole.TOOL_RESULT for message in compiled.messages)
        == 2
    )
    carrier_index = compiled.messages.index(carriers[0])
    continue_index = next(
        index
        for index, message in enumerate(compiled.messages)
        if message.content == (LLMTextPart("continue"),)
    )
    assert carrier_index < continue_index


def test_late_tool_image_reference_uses_the_same_labelled_carrier() -> None:
    image = bound_test_image("image/png", b"late-image", 4, 3)
    content = FrozenPromptContent((LLMTextPart("Image loaded."), image))
    late = replace(
        _tool_result(
            "placeholder",
            sequence=1,
            turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
        ),
        item_kind=FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        content=content.parts,
        tool_result_body_text="Image loaded.",
        tool_call_ordinal=0,
        tool_call_name="view_image",
        tool_call_arguments=freeze_json(
            {"target": {"kind": "IMAGE_REF", "image_ref": image.image_ref}}
        ),
    )
    snapshot = _snapshot(
        late,
        canonical_expanded_bytes=provider_input_item_canonical_expanded_bytes(late),
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            snapshot,
            _sources(_candidate(ContextSourceKind.BASE_SYSTEM, "BASE")),
        )
    )

    carriers = tuple(
        message
        for message in compiled.messages
        if message.role is MessageRole.USER
        and any(isinstance(part, LLMImagePart) for part in message.content)
    )
    assert len(carriers) == 1
    carrier = carriers[0]
    image_index = carrier.content.index(image)
    assert json.loads(carrier.content[image_index - 1].text) == {
        "tool_call_id": "call:1",
        "image": {"kind": "IMAGE_REF", "image_ref": image.image_ref},
    }
    assert join_text_content(compiled.messages[0].content).startswith(
        "tool_call_id: call:1\n"
    )


def test_round3_internal_tool_closure_schema_is_not_provider_visible() -> None:
    closure = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
        None,
        None,
        None,
        (
            LLMTextPart(
                canonical_json_bytes(
                    {
                        "tool_call_id": "call:closed",
                        "disposition": "interrupted_before_dispatch",
                    }
                ).decode("utf-8")
            ),
        ),
        tool_call_id="call:closed",
        tool_request_entry_id="entry:k5zfbzct6mhkogz33gyubxx3pa",
    )
    lowered = lower_canonical_item(
        closure,
    )
    assert lowered.message is not None
    assert json.loads(join_text_content(lowered.message.content)) == {
        "disposition": "interrupted_before_dispatch"
    }


def test_builtin_native_surface_exact_physical_byte_boundary(monkeypatch):
    import pulsara_agent.capability.planner as planner

    surface = (
        StructuredToolPort(object(), tool_names=("read_file",))
        .snapshot_tool_surface(conversation_scope_kind=ModelInputScopeKind.ROOT)
        .model_surface
    )
    owner = _Capability()
    snapshot = owner.freeze_owner_snapshot(
        conversation_scope_kind=ModelInputScopeKind.ROOT
    )
    projection = owner.freeze_projection_input(snapshot)
    plan, _ = _test_round9_capability_views(surface, skill_projection=projection)
    boundary = max(
        sum(len(tool.canonical_bytes) for tool in surface.tool_specs),
        sum(item.wire_utf8_bytes for item in plan.direct_projection_set.projections),
    )
    monkeypatch.setattr(planner, "MAXIMUM_NATIVE_TOOL_BYTES", boundary)
    _test_round9_capability_views(surface, skill_projection=projection)
    monkeypatch.setattr(planner, "MAXIMUM_NATIVE_TOOL_BYTES", boundary - 1)
    with pytest.raises(planner.CapabilityPlanningError, match="native bounds"):
        _test_round9_capability_views(surface, skill_projection=projection)


def test_round3_5000_item_allocation_does_not_full_reestimate_per_item() -> None:
    items = tuple(_user("x", sequence=index + 1) for index in range(5_000))
    request = _prepared_request(_snapshot(*items), _sources())
    counting = _CountingEstimator(request.compile_binding.estimator)
    request = replace(
        request,
        compile_binding=replace(request.compile_binding, estimator=counting),
    )
    compiled = StructuredModelInputCompiler().compile(request)
    assert sum(m.content == (LLMTextPart("x"),) for m in compiled.messages) == 5000
    assert counting.calls == 0


def test_round3_compiler_rejects_expired_deadline_before_allocation() -> None:
    request = _prepared_request(_snapshot(_user("deadline")), _sources())
    compiler = StructuredModelInputCompiler()
    with pytest.raises(StructuredModelInputCompileError) as failure:
        compiler.compile(request, deadline_monotonic=monotonic() - 1)
    assert failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED

    owner = new_test_provider_input_continuity_owner()
    scope = ProviderInputContinuityScope(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        scope_kind=ModelInputScopeKind.ROOT,
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(request),
        dispatch_anchor=_append_anchor(request),
    )
    with pytest.raises(StructuredModelInputCompileError) as append_failure:
        compiler.compile_new_epoch(
            request,
            planning=planning,
            deadline_monotonic=monotonic() - 1,
        )
    assert append_failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED


def test_round3_compiler_deadline_physically_exits_before_io_close(monkeypatch):
    import pulsara_agent.model_input.compiler as compiler_module

    real = compiler_module.provider_input_item_fingerprint

    def slow(item):
        sleep(0.04)
        return real(item)

    async def exercise():
        request = _prepared_request(
            _snapshot(_user("deadline"), _user("second", sequence=2)), _sources()
        )
        io_owner = KernelSessionIO()
        monkeypatch.setattr(compiler_module, "provider_input_item_fingerprint", slow)
        started = monotonic()
        with pytest.raises((TimeoutError, StructuredModelInputCompileError)):
            await io_owner.run(
                StructuredModelInputCompiler().compile,
                request,
                deadline_monotonic=started + 0.03,
            )
        assert monotonic() - started < 0.5
        await io_owner.aclose(deadline_monotonic=monotonic() + 0.2)
        assert io_owner.physical_close_complete

    asyncio.run(exercise())


def test_round3_1_overbudget_append_can_be_projected_without_execution_authority() -> (
    None
):
    """Compaction can inspect the exact failed append before ordinary compile."""

    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first_item = _user("installed prefix", sequence=1)
    first_request = _prepared_request(
        _snapshot(first_item),
        _sources(),
        budget=800,
    )
    _first, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=first_request,
    )

    jumped = _user("large canonical jump " * 400, sequence=2)
    changed_permission = _candidate(
        ContextSourceKind.TURN_CONTEXT,
        "permission=ask",
    )
    request = replace(
        _prepared_request(
            _snapshot(first_item, jumped),
            _sources(changed_permission),
            budget=800,
        ),
        context_id="context:xqxwmyhiw3jvl77sfiqkabysey",
        model_call_index=2,
    )
    planning = owner.freeze_planning_input(
        scope=ProviderInputContinuityScope(
            session_id="session:336wrfwkamrr3up6kjtsgvgcte",
            scope_kind=ModelInputScopeKind.ROOT,
        ),
        canonical_frontier=_append_frontier(request),
        dispatch_anchor=_append_anchor(request),
    )
    candidate = compiler.compile_installed_append(request, planning=planning)
    assert any(m.content == jumped.content for m in candidate.compiled_input.messages)
    assert (
        candidate.compiled_input.messages[: len(installed.messages)]
        == installed.messages
    )

    projection = compiler.project_installed_append(
        request,
        planning=planning,
    )
    projected = projection.projected_input
    assert projected.messages == candidate.compiled_input.messages
    assert projected.system_prompt == installed.system_prompt
    assert projected.tools == installed.tools
    assert projected.messages[: len(installed.messages)] == installed.messages
    assert projection.appended_message_count == (
        len(projected.messages) - len(installed.messages)
    )

    source_fingerprint = context_fingerprint(
        "test:round5b-overbudget-source-view:v1",
        request.canonical_input.snapshot_fingerprint,
    )
    source_view = SimpleNamespace(
        source_view_fingerprint=source_fingerprint,
        latest_assistant_input_through_sequence=None,
        materialized_messages=lambda: projected.messages,
        materialized_system_prompt=lambda: projected.system_prompt,
        predecessor_epoch_view=installed,
        normal_compile_binding=request.compile_binding,
        canonical_dispatch_read=SimpleNamespace(
            compile_snapshot=request.canonical_facts
        ),
        exact_safe_canonical_head=(
            request.canonical_input.identity.provider_input_through_sequence
        ),
    )
    summary_request = compaction_summary_request()
    safe_prefixes = enumerate_safe_summary_prefixes(
        source_view=source_view,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projected,
        deadline_monotonic=monotonic() + 1,
    )
    assert safe_prefixes
    tail, prefix = safe_prefixes[-1]
    assert summary_request
    assert 0 < prefix.summary_prefix_message_count < len(projected.messages)
    assert tail.protected_tail_message_start_index == (
        prefix.summary_prefix_message_count
    )
    assert prefix.source_through_sequence < source_view.exact_safe_canonical_head


def test_round3_5000_tool_results_produce_complete_candidate_with_bounded_work() -> (
    None
):
    items = tuple(
        _tool_result(
            "x" * 1_024, sequence=index + 1, turn_id="turn:uyenyjpuysm57ziiygjzxjhcum"
        )
        for index in range(5_000)
    )
    request = _prepared_request(
        _snapshot(*items),
        _sources(),
        budget=128,
        tool_names=("read_file",),
    )
    counting = _CountingEstimator(request.compile_binding.estimator)
    request = replace(
        request,
        compile_binding=replace(request.compile_binding, estimator=counting),
    )
    candidate = StructuredModelInputCompiler().compile(request)
    assert len(candidate.tool_result_decisions) == 5_000
    assert (
        sum(
            message.role is MessageRole.TOOL_RESULT
            and join_text_content(message.content) == "x" * 1024
            for message in candidate.messages
        )
        == 5000
    )
    assert counting.calls == 0


def test_round3_temporal_capture_is_single_and_dst_consistent(tmp_path):
    calls = []

    def clock():
        calls.append(None)
        return datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc)

    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=_Capability(),
        base_system_prompt="BASE",
        display_timezone=ZoneInfo("America/New_York"),
        clock=clock,
    )
    value = collector.freeze_non_trigger_sources(
        tool_surface=None,
        canonical_facts=_canonical_facts(),
        tool_exposure_plan=None,
        skill_dispatch_view=None,
        skill_owner_snapshot=None,
        new_context=True,
    )
    body = json.loads(
        next(
            c.text
            for c in value.candidates
            if c.source_kind is ContextSourceKind.TURN_CONTEXT
        )
    )["pulsara_runtime_observation"]["body"]
    assert len(calls) == 1 and len(body) == 3
    assert (
        "UTC 偏移：-04:00" in body[0]["text"] and "America/New_York" in body[0]["text"]
    )
    assert "2024-11-03T01:30:00-04:00" in body[2]["text"]
    assert "冻结权限" in body[1]["text"]


def test_round3_unkeyed_timezone_is_frozen_to_opening_offset(tmp_path):
    dynamic = _MutableUnkeyedTimezone(timedelta(hours=-4))
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=None,
        base_system_prompt="BASE",
        display_timezone=dynamic,
        clock=lambda: datetime(2026, 12, 1, 12, tzinfo=timezone.utc),
    )
    dynamic.offset = timedelta(hours=-5)
    value = collector.freeze_non_trigger_sources(
        tool_surface=None,
        canonical_facts=_canonical_facts(),
        tool_exposure_plan=None,
        skill_dispatch_view=None,
        skill_owner_snapshot=None,
        new_context=True,
    )
    body = json.loads(value.candidates[1].text)["pulsara_runtime_observation"]["body"]
    assert "UTC-04:00" in body[0]["text"] and "UTC 偏移：-04:00" in body[0]["text"]
    assert "2026-12-01T08:00:00-04:00" in body[2]["text"]


def test_turn_context_clock_failure_is_sampled_once_and_does_not_install_state(
    tmp_path,
):
    calls = []

    def clock():
        calls.append(None)
        raise RuntimeError("actual clock failure")

    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=None,
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=clock,
    )
    state = InstalledObservationState()
    with pytest.raises(RuntimeError, match="actual clock failure"):
        collector.freeze_non_trigger_sources(
            tool_surface=None,
            canonical_facts=_canonical_facts(),
            tool_exposure_plan=None,
            skill_dispatch_view=None,
            skill_owner_snapshot=None,
            previous_state=state,
            new_context=True,
        )
    assert len(calls) == 1 and state == InstalledObservationState()


def test_round3_runtime_path_is_fixed_workspace_and_escaped(tmp_path):
    inside = tmp_path / "目录 with space\nand-newline"
    inside.mkdir()
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=inside,
        capability_composer=None,
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    value = collector.freeze_non_trigger_sources(
        tool_surface=None,
        canonical_facts=_canonical_facts(),
        tool_exposure_plan=None,
        skill_dispatch_view=None,
        skill_owner_snapshot=None,
        new_context=True,
    )
    source = value.candidates[1]
    assert "\\n" in source.text
    body = json.loads(source.text)["pulsara_runtime_observation"]["body"]
    assert str(inside) in body[0]["text"]
    assert (
        "terminal_current_cwd" not in source.text
        and "relative_workdir_base" not in source.text
    )


def test_round3_runtime_source_keeps_workspace_after_foreground_and_yielded_cd(
    tmp_path: Path,
) -> None:
    foreground = tmp_path / "foreground cwd"
    yielded = tmp_path / "yielded cwd"
    foreground.mkdir()
    yielded.mkdir()
    port = DirectKernelToolPort(
        scheduled_tasks=scheduled_task_service(),
        workspace_root=tmp_path,
        host_owner_id="host:dugldp6ni2dxvuywv5a3utbnii",
        session_id="session:yb3rgzdbtfst4jirqaeox4wo6a",
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    try:
        terminal = port._terminal  # noqa: SLF001
        completed = terminal.execute(
            TerminalRequest(
                command=f"cd {shlex.quote(str(foreground))}",
                yield_time_ms=2_000,
            ),
            owner_host_session_id="host:dugldp6ni2dxvuywv5a3utbnii",
            decision_deadline_monotonic=port._deadlines.deadline(  # noqa: SLF001
                KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
            ),
        )
        assert completed.status is TerminalStatus.SUCCESS
        assert port.snapshot_workspace_root() == tmp_path.resolve()
        background = terminal.execute(
            TerminalRequest(
                command=f"cd {shlex.quote(str(yielded))}; sleep 5",
                yield_time_ms=5,
                max_lifetime_seconds=10,
            ),
            owner_host_session_id="host:dugldp6ni2dxvuywv5a3utbnii",
            decision_deadline_monotonic=port._deadlines.deadline(  # noqa: SLF001
                KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
            ),
        )
        assert background.status is TerminalStatus.RUNNING
        assert port.snapshot_workspace_root() == tmp_path.resolve()

        collector = KernelContextSourceCollector(
            workspace_kind="project",
            workspace_root=tmp_path,
            capability_composer=_Capability(),  # type: ignore[arg-type]
            base_system_prompt="BASE",
            display_timezone=timezone.utc,
            clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        collected = _collect_context_sources(
            collector,
            activation_subject=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
            activation_text="hello",
            tool_surface=prepare_test_direct_tool_surface(
                port,
                conversation_scope_kind=ModelInputScopeKind.ROOT,
            ).model_surface,
            canonical_facts=_canonical_facts(),
        )
        environment = next(
            item
            for item in collected.candidates
            if item.source_kind is ContextSourceKind.TURN_CONTEXT
        )
        assert (
            str(tmp_path.resolve())
            in json.loads(environment.text)["pulsara_runtime_observation"]["body"][0][
                "text"
            ]
        )
        assert str(yielded.resolve()) not in environment.text
    finally:
        asyncio.run(port.aclose(timeout_seconds=2))


def test_round3_tool_invocation_rejects_other_caller_access() -> None:
    tools = StructuredToolPort(object(), tool_names=("read_file",))
    prepared = tools.snapshot_tool_surface(
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
    )
    borrow = tools.borrow_tool_surface(prepared)
    try:
        with pytest.raises(ValueError, match="scope access does not exact-join"):
            KernelToolInvocationContext(
                session_id="session:336wrfwkamrr3up6kjtsgvgcte",
                workspace_id="workspace:wdl6b3mtrkghwnvjo4an3ekbau",
                turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                tool_call_id="call:test",
                attempt_id="attempt:wfltiqfg6cmo6cuivaxvkm4dr4",
                result_entry_id="entry:zvmjajlmix3vyoeoy2zhz6xd2a",
                conversation_scope_kind=ModelInputScopeKind.ROOT.value,
                host_owner_epoch=1,
                authorization_reference="authorization:test",
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                attempt_permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                surface_borrow=borrow,
            )
    finally:
        borrow.close()


def test_tool_runtime_dispatch_observation_requires_registry_current_borrow(
    tmp_path: Path,
) -> None:
    port = DirectKernelToolPort(
        scheduled_tasks=scheduled_task_service(),
        workspace_root=tmp_path,
        host_owner_id="host:yinyxklzunar54s7objjdbphcy",
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    try:
        surface = prepare_test_direct_tool_surface(port)
        cut = surface.capability_exposure_plan.dispatch_view.parent_dispatch_cut
        borrow = port.borrow_tool_surface(surface)
        copied_borrow = copy(borrow)
        with pytest.raises(RuntimeError, match="not active"):
            port.issue_capability_dispatch_observation(
                capability_dispatch_cut=cut,
                prepared_surface=surface,
                surface_borrow=copied_borrow,
            )
        with pytest.raises(RuntimeError, match="borrow active"):
            port.assert_no_tool_surface_borrows(
                scope_kind=ModelInputScopeKind.ROOT,
            )
        observation = port.issue_capability_dispatch_observation(
            capability_dispatch_cut=cut,
            prepared_surface=surface,
            surface_borrow=borrow,
        )
        assert observation.consume() == (cut, borrow)
        with pytest.raises(RuntimeError, match="already consumed"):
            observation.consume()
        borrow.close()
        with pytest.raises(RuntimeError, match="not active"):
            port.issue_capability_dispatch_observation(
                capability_dispatch_cut=cut,
                prepared_surface=surface,
                surface_borrow=borrow,
            )
        port.assert_no_tool_surface_borrows(
            scope_kind=ModelInputScopeKind.ROOT,
        )
    finally:
        asyncio.run(port.aclose(timeout_seconds=2))


def test_round3_tool_owner_rejects_foreign_host_surface_borrow(tmp_path: Path) -> None:
    async def scenario() -> None:
        ports = tuple(
            DirectKernelToolPort(
                scheduled_tasks=scheduled_task_service(),
                workspace_root=tmp_path,
                host_owner_id=derive_id("host", "test-fixture", f"host:{name}"),
                session_id="session:336wrfwkamrr3up6kjtsgvgcte",
                live_bus=LiveAgentEventBus(),
                authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            )
            for name in ("a", "b")
        )
        owner, foreign = ports
        owner_surface = prepare_test_direct_tool_surface(
            owner,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
        )
        foreign_surface = prepare_test_direct_tool_surface(
            foreign,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
        )
        assert owner_surface != foreign_surface
        borrow = foreign.borrow_tool_surface(foreign_surface)
        try:
            authorization = await owner.authorize(
                tool_name="read_file",
                arguments={"target": {"kind": "PATH", "path": "README.md"}},
                tool_call_id="call:test",
                turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                surface_borrow=borrow,
                permission_snapshot=_permission_snapshot(),
                memory_context=FrozenModelCallMemoryContext(),
            )
            assert authorization.kind is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            invocation = KernelToolInvocationContext(
                session_id="session:336wrfwkamrr3up6kjtsgvgcte",
                workspace_id="workspace:wdl6b3mtrkghwnvjo4an3ekbau",
                turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                tool_call_id="call:test",
                attempt_id="attempt:wfltiqfg6cmo6cuivaxvkm4dr4",
                result_entry_id="entry:zvmjajlmix3vyoeoy2zhz6xd2a",
                conversation_scope_kind=ModelInputScopeKind.ROOT.value,
                host_owner_epoch=1,
                authorization_reference="authorization:test",
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                attempt_permission_snapshot_fingerprint=(
                    _permission_snapshot().snapshot_fingerprint
                ),
                surface_borrow=borrow,
            )
            with pytest.raises(RuntimeError, match="surface borrow is not active"):
                await owner.invoke(
                    tool_name="read_file",
                    arguments={"target": {"kind": "PATH", "path": "README.md"}},
                    tool_call_id="call:test",
                    attempt_id="attempt:wfltiqfg6cmo6cuivaxvkm4dr4",
                    turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
                    assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                    invocation_context=invocation,
                )
        finally:
            borrow.close()
            await owner.aclose(timeout_seconds=2)
            await foreign.aclose(timeout_seconds=2)

    asyncio.run(scenario())


def test_round3_tool_surface_includes_member_process_and_schema_is_frozen(
    tmp_path: Path,
) -> None:
    port = DirectKernelToolPort(
        scheduled_tasks=scheduled_task_service(),
        workspace_root=tmp_path,
        host_owner_id="host:yinyxklzunar54s7objjdbphcy",
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        live_bus=LiveAgentEventBus(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    root = prepare_test_direct_tool_surface(
        port,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
    )
    child = prepare_test_direct_tool_surface(
        port,
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
    )
    assert "exec_process" in {tool.name for tool in root.model_surface.tool_specs}
    assert "exec_process" in {tool.name for tool in child.model_surface.tool_specs}
    assert not hasattr(port._terminal, "_sessions")  # noqa: SLF001
    assert port.snapshot_workspace_root() == tmp_path.resolve()
    assert not hasattr(port._terminal, "_sessions")  # noqa: SLF001
    terminal = next(
        tool for tool in root.model_surface.tool_specs if tool.name == "exec_process"
    )
    with pytest.raises((AttributeError, TypeError)):
        terminal.parameters["type"] = "array"  # type: ignore[index]
    borrow = port.borrow_tool_surface(root)
    old_binding = borrow.binding_fingerprint("exec_process")
    with pytest.raises(RuntimeError, match="sealed"):
        port.bind_subagent_port(SimpleNamespace(tool_names=frozenset({"spawn_agent"})))
    assert borrow.binding_fingerprint("exec_process") == old_binding
    borrow.close()
    asyncio.run(port.aclose(timeout_seconds=2))


def test_round3_tool_call_argument_owners_are_strictly_frozen() -> None:
    assert get_type_hints(CompletedToolCallBlock)["arguments"] is FrozenJsonObjectFact
    assert get_type_hints(AssistantToolCallBlock)["arguments"] is FrozenJsonObjectFact
    with pytest.raises(TypeError, match="recursively frozen"):
        CompletedToolCallBlock("block", "call", "exec_process", {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="recursively frozen"):
        AssistantToolCallBlock("block", "call", "exec_process", {})  # type: ignore[arg-type]


def test_round3_report_projection_is_bounded_and_contains_no_prompt() -> None:
    secret = "secret-user-prompt-never-project"
    items = tuple(
        _tool_result(
            f"{secret}:{index}",
            sequence=index + 1,
            turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
        )
        for index in range(70)
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            _snapshot(*items),
            _sources(_candidate(ContextSourceKind.BASE_SYSTEM, secret)),
            tool_names=("read_file",),
        )
    )
    assert len(compiled.tool_result_decisions) == 70
    assert secret not in repr(compiled)

    offers: list[object] = []
    owner = SimpleNamespace(
        _extensions=SimpleNamespace(offer_operational_nowait=offers.append),
        _writer_lease=SimpleNamespace(
            guard=SimpleNamespace(session_id="session:336wrfwkamrr3up6kjtsgvgcte")
        ),
    )
    ConversationKernelRunner._offer_compile_observation(
        owner,  # type: ignore[arg-type]
        turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
        model_call_index=1,
        compiled=compiled,
    )
    assert len(offers) == 1
    offer = offers[0]
    assert offer.event_type is OperationalHookType.MODEL_INPUT_COMPILE_OBSERVED
    assert offer.public_payload["decision_sample_count"] == 64
    assert offer.public_payload["decision_omitted_count"] == 7
    assert secret not in repr(offer.public_payload)

    failing_owner = SimpleNamespace(
        _extensions=SimpleNamespace(
            offer_operational_nowait=lambda _offer: (_ for _ in ()).throw(
                RuntimeError("isolated observer failure")
            )
        ),
        _writer_lease=owner._writer_lease,
    )
    ConversationKernelRunner._offer_compile_observation(
        failing_owner,  # type: ignore[arg-type]
        turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
        model_call_index=1,
        compiled=compiled,
    )


def test_round3_diagnostic_projector_is_the_bounded_public_owner() -> None:
    assert (
        get_type_hints(ModelInputCompileOperationalProjection)["diagnostic_codes"]
        == tuple[ContextPublicDiagnosticCode, ...]
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            _snapshot(
                *(
                    _tool_result(
                        f"private-body-{index}",
                        sequence=index + 1,
                        turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
                    )
                    for index in range(70)
                )
            ),
            _sources(_candidate(ContextSourceKind.BASE_SYSTEM, "private-system")),
            tool_names=("read_file",),
        )
    )
    projection = project_model_input_compile_observation(
        model_call_index=1,
        compiled=compiled,
    )
    assert len(projection.decision_samples) == 64
    assert projection.decision_omitted_count == 7
    assert {item.decision_kind for item in projection.decision_samples} <= {
        CompileDecisionSampleKind.SOURCE,
        CompileDecisionSampleKind.TOOL_RESULT,
    }
    payload = projection.public_payload()
    assert "private-system" not in repr(payload)
    assert "private-body" not in repr(payload)
    assert not any(
        key in repr(payload).lower()
        for key in ("public_detail", "filesystem_path", "tool_arguments")
    )


def test_round3_compile_binding_cannot_exceed_or_rewrite_target_budgets() -> None:
    request = _prepared_request(
        _snapshot(_user("hello")),
        _sources(_candidate(ContextSourceKind.BASE_SYSTEM, "base")),
    )
    binding = request.compile_binding
    with pytest.raises(ValueError, match="input budget exceeds"):
        replace(
            binding,
            effective_input_budget_tokens=(
                binding.target_fact.context_budget.input_budget_tokens + 1
            ),
            binding_fingerprint=model_input_compile_binding_fingerprint(
                call_fact=binding.call_fact,
                target_fact=binding.target_fact,
                estimator_fingerprint=binding.estimator_fingerprint,
                effective_input_budget_tokens=(
                    binding.target_fact.context_budget.input_budget_tokens + 1
                ),
                effective_output_tokens=binding.effective_output_tokens,
                tool_surface=binding.tool_surface,
            ),
        )
    with pytest.raises(ValueError, match="output budget differs"):
        replace(
            binding,
            effective_output_tokens=binding.effective_output_tokens - 1,
            binding_fingerprint=model_input_compile_binding_fingerprint(
                call_fact=binding.call_fact,
                target_fact=binding.target_fact,
                estimator_fingerprint=binding.estimator_fingerprint,
                effective_input_budget_tokens=binding.effective_input_budget_tokens,
                effective_output_tokens=binding.effective_output_tokens - 1,
                tool_surface=binding.tool_surface,
            ),
        )


def test_round3_1_compatible_epoch_appends_clock_without_rewriting_prefix() -> None:
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("first", sequence=1)
    first_request = _prepared_request(
        _snapshot(initial),
        _sources(_candidate(ContextSourceKind.TURN_CONTEXT, "clock=A")),
        tool_names=("read_file",),
    )
    _first, installed = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first_request
    )

    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:yym6qf3l4xz6437gradtmhjrke",
        2,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart("answer"),),
    )
    second_request = replace(
        _prepared_request(
            _snapshot(initial, assistant),
            _sources(_candidate(ContextSourceKind.TURN_CONTEXT, "clock=B")),
            tool_names=("read_file",),
        ),
        context_id="context:227vr3lxxerzucei2odo5zvd24",
        model_call_index=2,
    )
    second, successor = _compile_and_install_append(
        compiler=compiler, owner=owner, request=second_request
    )

    assert successor.system_prompt == installed.system_prompt
    assert successor.tools == installed.tools
    assert successor.messages[: len(installed.messages)] == installed.messages
    assert successor.system_prompt.encode("utf-8") == installed.system_prompt.encode(
        "utf-8"
    )
    first_wire = installed.wire_input_plan.materialization
    second_wire = successor.wire_input_plan.materialization
    assert first_wire.tool_items
    assert canonical_json_bytes(
        thaw_json(first_wire.root_policy_value)
    ) == canonical_json_bytes(thaw_json(second_wire.root_policy_value))
    assert canonical_json_bytes(
        [thaw_json(item) for item in first_wire.tool_items]
    ) == canonical_json_bytes([thaw_json(item) for item in second_wire.tool_items])
    assert [
        canonical_json_bytes(thaw_json(item)) for item in first_wire.ordered_input_items
    ] == [
        canonical_json_bytes(thaw_json(item))
        for item in second_wire.ordered_input_items[
            : len(first_wire.ordered_input_items)
        ]
    ]
    assert second.appended_message_count == len(successor.messages) - len(
        installed.messages
    )
    observations = [
        _observation(message)
        for message in successor.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
    ]
    assert [
        item["body"] for item in observations if item["source"] == "TURN_CONTEXT"
    ] == [
        [{"text": "clock=A"}],
        [{"text": "clock=B"}],
    ]


def test_round9_2_hook_context_is_one_shot_user_suffix_with_exact_prefix():
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("first")
    _, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=_prepared_request(_snapshot(initial), _sources()),
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:yym6qf3l4xz6437gradtmhjrke",
        2,
        initial.source_turn_id,
        (LLMTextPart("answer"),),
    )
    hook = LLMMessage.user(
        '{"pulsara_hook_context":[{"event":"PostToolUse","text":"exact observation"}]}'
    )
    request = replace(
        _prepared_request(_snapshot(initial, assistant), _sources(hook_context=hook)),
        context_id="context:24u7sp4dhbfdnbipigvd3tetji",
        model_call_index=2,
    )
    _, successor = _compile_and_install_append(
        compiler=compiler, owner=owner, request=request
    )
    assert successor.messages[: len(installed.messages)] == installed.messages
    assert (
        successor.messages[-1] is hook
        and successor.system_prompt == installed.system_prompt
        and successor.tools == installed.tools
    )
    followup = _user("follow-up", sequence=3)
    third = replace(
        _prepared_request(_snapshot(initial, assistant, followup), _sources()),
        context_id="context:vgcg7k4ft2kgvxluklis4t7ooi",
        model_call_index=3,
    )
    _, view = _compile_and_install_append(compiler=compiler, owner=owner, request=third)
    assert view.messages[: len(successor.messages)] == successor.messages
    assert sum(message == hook for message in view.messages) == 1


@pytest.mark.parametrize("wire_api", ("openai_chat_completions", "openai_responses"))
def test_memory_write_hint_is_the_only_final_wire_difference_before_anchor(
    wire_api: str,
) -> None:
    profile = RouteWireProfile(
        id=f"test:memory-write-hint:{wire_api}", wire_api=wire_api
    )
    snapshot = _snapshot(_user("Please remember that I like concise answers"))
    no_hint_request = _prepared_request(
        snapshot,
        _sources(),
        route_wire_profile=profile,
    )
    hint_request = _prepared_request(
        snapshot,
        _sources(
            _candidate(
                ContextSourceKind.MEMORY_WRITE_HINT,
                MEMORY_WRITE_HINT_BODY,
            )
        ),
        route_wire_profile=profile,
    )
    no_hint, no_hint_view = _compile_and_install_append(
        compiler=StructuredModelInputCompiler(),
        owner=new_test_provider_input_continuity_owner(),
        request=no_hint_request,
    )
    with_hint, hint_view = _compile_and_install_append(
        compiler=StructuredModelInputCompiler(),
        owner=new_test_provider_input_continuity_owner(),
        request=hint_request,
    )

    assert (
        with_hint.compiled_input.system_prompt == no_hint.compiled_input.system_prompt
    )
    assert with_hint.compiled_input.tools == no_hint.compiled_input.tools
    hint_messages = tuple(
        message
        for message in with_hint.compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
        and _observation(message)["source"] == "MEMORY_WRITE_HINT"
    )
    assert len(hint_messages) == 1
    decoded = _observation(hint_messages[0])
    assert set(
        json.loads(join_text_content(hint_messages[0].content))[
            "pulsara_runtime_observation"
        ]
    ) == {"source", "body"}
    assert decoded["body"] == [{"text": MEMORY_WRITE_HINT_BODY}]
    assert with_hint.compiled_input.messages[-2] is hint_messages[0]
    assert with_hint.compiled_input.messages[-1].content == (
        LLMTextPart(provider_input_item_text(snapshot.items[-1])),
    )
    assert (
        tuple(
            message
            for message in with_hint.compiled_input.messages
            if message is not hint_messages[0]
        )
        == no_hint.compiled_input.messages
    )

    no_hint_projection = thaw_json(
        no_hint_view.wire_input_plan.materialization.context_bearing_projection
    )
    hint_projection = thaw_json(
        hint_view.wire_input_plan.materialization.context_bearing_projection
    )
    removed = 0

    def without_hint(value):
        nonlocal removed
        if isinstance(value, dict):
            return {key: without_hint(item) for key, item in value.items()}
        if isinstance(value, list):
            output = []
            for item in value:
                if MEMORY_WRITE_HINT_BODY in json.dumps(
                    item, ensure_ascii=False, sort_keys=True
                ):
                    removed += 1
                    continue
                output.append(without_hint(item))
            return output
        return value

    assert without_hint(hint_projection) == no_hint_projection
    assert removed == 1
    assert no_hint_view.wire_input_plan.quote.final_wire_utf8_bytes == len(
        canonical_json_bytes(no_hint_projection)
    )
    assert hint_view.wire_input_plan.quote.final_wire_utf8_bytes == len(
        canonical_json_bytes(hint_projection)
    )


def test_memory_write_hint_call_append_does_not_repeat_on_tool_loop() -> None:
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("Please remember that I like concise answers", sequence=1)
    first_request = _prepared_request(
        _snapshot(initial),
        _sources(
            _candidate(
                ContextSourceKind.MEMORY_WRITE_HINT,
                MEMORY_WRITE_HINT_BODY,
            )
        ),
    )
    _first, first_view = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=first_request,
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:yym6qf3l4xz6437gradtmhjrke",
        2,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart("continuing after a tool result"),),
    )
    second_request = replace(
        _prepared_request(_snapshot(initial, assistant), _sources()),
        context_id="context:w7dwwdwyxz3j6ezkyvaivwwsse",
        model_call_index=2,
    )
    _second, second_view = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=second_request,
    )
    assert second_view.messages[: len(first_view.messages)] == first_view.messages
    hint_observations = tuple(
        _observation(message)
        for message in second_view.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
        and _observation(message)["source"] == "MEMORY_WRITE_HINT"
    )
    assert len(hint_observations) == 1
    assert hint_observations[0]["body"] == [{"text": MEMORY_WRITE_HINT_BODY}]


def _assert_replay_final_wire_projection(*, result, view, prepared_call) -> None:
    plan = view.wire_input_plan
    quote = plan.quote
    assert plan.replacements
    assert quote.replaced_generic_wire_estimated_tokens == sum(
        item.generic_wire_estimated_tokens for item in plan.replacements
    )
    assert quote.replay_wire_estimated_tokens == sum(
        item.replay_wire_estimated_tokens for item in plan.replacements
    )
    assert quote.raw_final_wire_estimated_input_tokens == (
        quote.generic_wire_estimated_input_tokens
        - quote.replaced_generic_wire_estimated_tokens
        + quote.replay_wire_estimated_tokens
    )
    projection = thaw_json(plan.materialization.context_bearing_projection)
    assert isinstance(projection, dict)
    assert quote.final_wire_utf8_bytes == len(canonical_json_bytes(projection))
    compiled = result.compiled_input
    context = LLMContext(
        messages=compiled.messages,
        context_id=compiled.context_id,
        resolved_model_call_id=prepared_call.call.resolved_model_call_id,
        model_call_index=1,
        system_prompt=compiled.system_prompt,
        provider_wire_input_plan=plan,
    )
    if plan.wire_api == "openai_chat_completions":
        payload = build_chat_completions_payload(
            call=prepared_call.call,
            context=context,
        )
        actual = project_chat_context_bearing_payload_fields(payload)
    else:
        payload = build_responses_payload(call=prepared_call.call, context=context)
        actual = project_responses_context_bearing_payload_fields(payload)
    assert actual == projection


def test_round5a1_reasoning_replay_replaces_exact_assistant_and_keeps_wire_prefix() -> (
    None
):
    profile = RouteWireProfile(
        id="test:chat-replay",
        wire_api="openai_chat_completions",
    )
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("first", sequence=1)
    first_request = _prepared_request(
        _snapshot(initial), _sources(), route_wire_profile=profile
    )
    _first, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=first_request,
    )
    _model, first_prepared = _PREPARED_MODEL_CALLS[
        first_request.compile_binding.call_fact.resolved_model_call_id
    ]
    replay_message = freeze_json(
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "opaque-process-local-reasoning",
        }
    )
    assert isinstance(replay_message, FrozenJsonObjectFact)
    replay_payload = freeze_provider_adapter_completed_replay_payload(
        codec_kind=profile.assistant_replay_codec_kind,
        ordered_items=(replay_message,),
    )
    completion = CompletedProviderModelExecution(
        terminal=ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            completed_replay_payload=replay_payload,
        ),
        replay_payload=replay_payload,
        replay_target=DirectKernelModelPort.replay_target(first_prepared),
    )
    public_message = LLMMessage.assistant("answer")
    fragment = completion.bind_assistant_entry(
        assistant_entry_id="entry:yym6qf3l4xz6437gradtmhjrke",
        public_projection_fingerprint=(
            provider_assistant_message_public_projection_fingerprint(public_message)
        ),
        has_tool_calls=False,
    )
    assert fragment is not None
    scope = installed.scope
    reservation = owner.reserve_assistant_replay_fragment(
        scope=scope,
        epoch_nonce=installed.epoch_nonce,
        epoch_revision=installed.epoch_revision,
        fragment=fragment,
    )
    owner.promote_assistant_replay_fragment(reservation)

    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:yym6qf3l4xz6437gradtmhjrke",
        2,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart("answer"),),
    )
    second_request = replace(
        _prepared_request(
            _snapshot(initial, assistant),
            _sources(),
            route_wire_profile=profile,
        ),
        context_id="context:227vr3lxxerzucei2odo5zvd24",
        model_call_index=2,
    )
    second_result, successor = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=second_request,
        replay_manifest_cut=_replay_manifest_cut(
            second_request.canonical_input,
            fragment,
            wire_api="openai_chat_completions",
        ),
        replay_fragments=(fragment,),
    )
    first_wire = installed.wire_input_plan.materialization
    second_wire = successor.wire_input_plan.materialization
    assert second_wire.root_policy_value == first_wire.root_policy_value
    assert second_wire.tool_items == first_wire.tool_items
    assert second_wire.ordered_input_items[: len(first_wire.ordered_input_items)] == (
        first_wire.ordered_input_items
    )
    assert thaw_json(second_wire.ordered_input_items[-1]) == thaw_json(replay_message)
    assert successor.messages[: len(installed.messages)] == installed.messages
    _assert_replay_final_wire_projection(
        result=second_result,
        view=successor,
        prepared_call=first_prepared,
    )

    follow_up = _user("follow-up", sequence=3)
    third_request = replace(
        _prepared_request(
            _snapshot(initial, assistant, follow_up),
            _sources(),
            route_wire_profile=profile,
        ),
        context_id="context:kvh63xitqwutzhjtmtzdnzf52a",
        model_call_index=3,
    )
    _third, third_view = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=third_request,
        replay_manifest_cut=_replay_manifest_cut(
            third_request.canonical_input,
            fragment,
            wire_api="openai_chat_completions",
        ),
        replay_fragments=(fragment,),
    )
    third_wire = third_view.wire_input_plan.materialization
    assert (
        third_wire.ordered_input_items[: len(second_wire.ordered_input_items)]
        == second_wire.ordered_input_items
    )


def test_round5a1_responses_replay_preserves_ordered_items_after_wire_prefix() -> None:
    profile = RouteWireProfile(id="test:responses-replay", wire_api="openai_responses")
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("first", sequence=1)
    first_request = _prepared_request(
        _snapshot(initial), _sources(), route_wire_profile=profile
    )
    _first, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=first_request,
    )
    _model, prepared = _PREPARED_MODEL_CALLS[
        first_request.compile_binding.call_fact.resolved_model_call_id
    ]
    replay_items = tuple(
        freeze_json(item)
        for item in (
            {
                "type": "reasoning",
                "id": "reasoning:1",
                "status": "completed",
                "summary": [],
                "encrypted_content": "opaque-process-local-value",
            },
            {
                "type": "message",
                "id": "message:1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        )
    )
    assert all(isinstance(item, FrozenJsonObjectFact) for item in replay_items)
    payload = freeze_provider_adapter_completed_replay_payload(
        codec_kind=profile.assistant_replay_codec_kind,
        ordered_items=replay_items,  # type: ignore[arg-type]
    )
    completion = CompletedProviderModelExecution(
        terminal=ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            completed_replay_payload=payload,
        ),
        replay_payload=payload,
        replay_target=DirectKernelModelPort.replay_target(prepared),
    )
    public_message = LLMMessage.assistant("answer")
    fragment = completion.bind_assistant_entry(
        assistant_entry_id="entry:yym6qf3l4xz6437gradtmhjrke",
        public_projection_fingerprint=(
            provider_assistant_message_public_projection_fingerprint(public_message)
        ),
        has_tool_calls=False,
    )
    assert fragment is not None
    reservation = owner.reserve_assistant_replay_fragment(
        scope=installed.scope,
        epoch_nonce=installed.epoch_nonce,
        epoch_revision=installed.epoch_revision,
        fragment=fragment,
    )
    owner.promote_assistant_replay_fragment(reservation)
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:yym6qf3l4xz6437gradtmhjrke",
        2,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart("answer"),),
    )
    second_request = replace(
        _prepared_request(
            _snapshot(initial, assistant),
            _sources(),
            route_wire_profile=profile,
        ),
        context_id="context:bcip5ing3nhb3ftvodtb37wjd4",
        model_call_index=2,
    )
    second_result, successor = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=second_request,
        replay_manifest_cut=_replay_manifest_cut(
            second_request.canonical_input,
            fragment,
            wire_api="openai_responses",
        ),
        replay_fragments=(fragment,),
    )
    first_wire = installed.wire_input_plan.materialization.ordered_input_items
    second_wire = successor.wire_input_plan.materialization.ordered_input_items
    assert second_wire[: len(first_wire)] == first_wire
    assert second_wire[-2:] == replay_items
    _assert_replay_final_wire_projection(
        result=second_result,
        view=successor,
        prepared_call=prepared,
    )


def test_empty_cold_start_keeps_owner_issuance_and_exact_destination() -> None:
    owner = new_test_provider_input_continuity_owner()
    request = _prepared_request(_snapshot(_user("initial")), _sources())
    _, candidate = _prepare_append_candidate(
        compiler=StructuredModelInputCompiler(), owner=owner, request=request
    )
    transition = candidate.transition
    assert isinstance(transition, EmptyScopeColdStart)
    assert transition.scope == candidate.scope
    assert transition.destination is candidate.call_target
    assert transition._preparation_basis is candidate._preparation_basis
    with pytest.raises(TypeError, match="continuity-issued"):
        EmptyScopeColdStart(
            scope=transition.scope,
            destination=transition.destination,
            seed=transition.seed,
            preparation_basis=transition._preparation_basis,
            _seal=object(),
        )
    equivalent_target = replace(candidate.call_target)
    assert equivalent_target == candidate.call_target
    with pytest.raises(ValueError, match="destination drifted"):
        replace(candidate, call_target=equivalent_target)


@pytest.mark.parametrize("drift", ["scope", "basis", "reservation", "revision"])
def test_empty_cold_registration_rejects_preparation_drift(drift: str) -> None:
    owner = new_test_provider_input_continuity_owner()
    request = _prepared_request(_snapshot(_user("initial")), _sources())
    _, candidate = _prepare_append_candidate(
        compiler=StructuredModelInputCompiler(), owner=owner, request=request
    )
    transition = candidate.transition
    assert isinstance(transition, EmptyScopeColdStart)
    equivalent_basis = copy(transition._preparation_basis)
    assert equivalent_basis == transition._preparation_basis
    assert equivalent_basis is not transition._preparation_basis
    if drift == "scope":
        with pytest.raises(ValueError, match="scope drifted"):
            _issue_empty_scope_cold_start(
                scope=ProviderInputContinuityScope(
                    "session:aaaaaaaaaaaaaaaaaaaaaaaaaa", ModelInputScopeKind.SUBAGENT
                ),
                destination=transition.destination,
                seed=transition.seed,
                preparation_basis=transition._preparation_basis,
            )
        return
    if drift in {"scope", "basis"}:
        transition = _issue_empty_scope_cold_start(
            scope=(
                ProviderInputContinuityScope(
                    session_id="session:aaaaaaaaaaaaaaaaaaaaaaaaaa",
                    scope_kind=ModelInputScopeKind.SUBAGENT,
                )
                if drift == "scope"
                else transition.scope
            ),
            destination=transition.destination,
            seed=transition.seed,
            preparation_basis=(
                equivalent_basis if drift == "basis" else transition._preparation_basis
            ),
        )
        bad_candidate = replace(candidate, transition=transition)
    elif drift == "reservation":
        reservation = candidate.planning._empty_preparation_reservation
        equivalent_reservation = copy(reservation)
        assert equivalent_reservation == reservation
        assert equivalent_reservation is not reservation
        bad_candidate = replace(
            candidate,
            planning=replace(
                candidate.planning,
                _empty_preparation_reservation=equivalent_reservation,
            ),
        )
    else:
        bad_candidate = replace(candidate, expected_epoch_revision=1)
    with pytest.raises(ProviderInputContinuityConflict, match="bound authority"):
        owner.register(bad_candidate)
    assert owner.current_view(candidate.scope) is None
    owner.register(candidate)
    owner.install(candidate=candidate, execution=object())
    assert owner.current_view(candidate.scope).epoch_revision == 1


@pytest.mark.parametrize("release", ["abort", "discard"])
def test_empty_cold_retry_rejects_released_candidate_and_keeps_one_shot_install(
    release: str,
) -> None:
    owner = new_test_provider_input_continuity_owner()
    compiler = StructuredModelInputCompiler()
    request = _prepared_request(_snapshot(_user("initial")), _sources())
    _, stale = _prepare_append_candidate(
        compiler=compiler, owner=owner, request=request
    )
    if release == "abort":
        owner.abort_planning(stale.planning)
    else:
        owner.register(stale)
        owner.discard(stale)
    _, current = _prepare_append_candidate(
        compiler=compiler, owner=owner, request=request
    )
    with pytest.raises(ProviderInputContinuityConflict, match="bound authority"):
        owner.register(stale)
    owner.register(current)
    execution = object()
    permit = owner.install(candidate=current, execution=execution)
    view = owner.current_view(current.scope)
    assert view.epoch_revision == 1
    with pytest.raises(ProviderInputContinuityConflict, match="exact-join"):
        owner.install(candidate=current, execution=execution)
    with pytest.raises(ProviderInputContinuityConflict, match="not issued"):
        owner.install_authority.consume(permit, candidate=current, execution=object())
    owner.install_authority.consume(permit, candidate=current, execution=execution)
    with pytest.raises(ProviderInputContinuityConflict, match="not issued"):
        owner.install_authority.consume(permit, candidate=current, execution=execution)
    assert owner.current_view(current.scope) is view


def test_empty_cold_candidate_cannot_register_after_host_close() -> None:
    owner = new_test_provider_input_continuity_owner()
    request = _prepared_request(_snapshot(_user("initial")), _sources())
    _, candidate = _prepare_append_candidate(
        compiler=StructuredModelInputCompiler(), owner=owner, request=request
    )
    owner.close()
    with pytest.raises(ProviderInputContinuityConflict, match="closed"):
        owner.register(candidate)
    assert owner.current_view(candidate.scope) is None


def test_round3_1_compatible_epoch_rejects_old_canonical_rewrite() -> None:
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first = _prepared_request(_snapshot(_user("original")), _sources())
    _compile_and_install_append(compiler=compiler, owner=owner, request=first)
    rewritten = _prepared_request(_snapshot(_user("rewritten")), _sources())

    scope = ProviderInputContinuityScope(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        scope_kind=ModelInputScopeKind.ROOT,
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(rewritten),
        dispatch_anchor=_append_anchor(rewritten),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        compiler.compile_installed_append(
            rewritten,
            planning=planning,
        )
    assert failure.value.kind is ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT


def test_round3_1_root_epoch_spans_turns_and_host_replacement_is_cold() -> None:
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first_user = _user(
        "turn one", sequence=1, turn_id="turn:xayicncb3vcols33532v4oov6a"
    )
    first_request = _prepared_request(
        _snapshot(first_user, turn_id="turn:xayicncb3vcols33532v4oov6a"), _sources()
    )
    _first, first_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first_request
    )

    answer = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:yym6qf3l4xz6437gradtmhjrke",
        2,
        "turn:xayicncb3vcols33532v4oov6a",
        (LLMTextPart("answer one"),),
    )
    second_user = _user(
        "turn two", sequence=3, turn_id="turn:hd5xd6xkjf6qdywoaszspvokyq"
    )
    second_request = replace(
        _prepared_request(
            _snapshot(
                first_user,
                answer,
                second_user,
                turn_id="turn:hd5xd6xkjf6qdywoaszspvokyq",
            ),
            _sources(),
        ),
        context_id="context:e52mb2kzg6mrmlmarekyisz2xy",
        model_call_index=2,
    )
    _second, second_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=second_request
    )
    assert second_view.epoch_nonce == first_view.epoch_nonce
    assert second_view.epoch_revision == first_view.epoch_revision + 1
    assert second_view.messages[: len(first_view.messages)] == first_view.messages

    replacement = new_test_provider_input_continuity_owner()
    scope = ProviderInputContinuityScope(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        scope_kind=ModelInputScopeKind.ROOT,
    )
    assert replacement.current_view(scope) is None
    planning = replacement.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(second_request),
        dispatch_anchor=_append_anchor(second_request),
    )
    assert planning.predecessor.value == "EMPTY"
    assert planning.predecessor_view is None


def test_round3_1_root_drift_cannot_rebuild_an_installed_epoch() -> None:
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    initial = _user("initial", sequence=1)
    first = _prepared_request(_snapshot(initial), _sources())
    _first, first_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first
    )

    changed_base = _candidate(ContextSourceKind.BASE_SYSTEM, "BASE v2")
    successor = replace(
        _prepared_request(
            _snapshot(initial, _user("next", sequence=2)),
            _sources(changed_base),
        ),
        context_id="context:ixdyu2ouniccgm3t6nguaoc37a",
        model_call_index=2,
    )
    scope = ProviderInputContinuityScope(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        scope_kind=ModelInputScopeKind.ROOT,
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=_append_frontier(successor),
        dispatch_anchor=_append_anchor(successor),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        compiler.compile_installed_append(successor, planning=planning)
    assert failure.value.kind is ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
    assert owner.current_view(scope) is first_view


def test_round3_pure_compiler_import_graph_has_no_kernel_transport_or_io() -> None:
    package = Path(__file__).parents[1] / "src/pulsara_agent/model_input"
    forbidden_modules = (
        "conversation_kernel.repository",
        "conversation_kernel._repository",
        "conversation_kernel.host",
        "conversation_kernel.io",
        "conversation_kernel.tool_runtime",
        "postgres",
        "normalized_transport",
        "provider_stream",
        "event_log",
    )
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            forbidden in module for module in imports for forbidden in forbidden_modules
        ), (path, imports)

    compiler_source = (package / "compiler.py").read_text(encoding="utf-8")
    assert "ResolvedModelCall" not in compiler_source
    assert "LLMContext" not in compiler_source
    assert "transport" not in compiler_source.lower()
    for carrier in (
        StructuredModelInputCompileRequest,
        ContextSourceCandidate,
        FrozenProviderInputItem,
    ):
        annotations = tuple(str(field.type) for field in fields(carrier))
        assert not any(
            "dict[" in annotation or "list[" in annotation for annotation in annotations
        )

    kernel = package.parent / "conversation_kernel"
    direct_tree = ast.parse((kernel / "direct_model.py").read_text(encoding="utf-8"))
    direct_imports = {
        node.module or ""
        for node in ast.walk(direct_tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name in module
        for module in direct_imports
        for name in ("repository", "reader", "context_sources")
    )
    assert "_to_llm_message" not in (kernel / "direct_model.py").read_text(
        encoding="utf-8"
    )
    assert "KernelModelRequest" not in (kernel / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "direct_model" not in (kernel / "context_sources.py").read_text(
        encoding="utf-8"
    )
    assert "project_model_input_compile_observation" in (
        kernel / "runner.py"
    ).read_text(encoding="utf-8")

    reader_source = (kernel / "reader.py").read_text(encoding="utf-8")
    assert "SELECT * FROM pulsara_v3.assistant_message_blocks" not in reader_source


def test_user_actions_one_shot_is_appended_and_never_cleared():
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first = _user("first", sequence=1)
    notice = _candidate(
        ContextSourceKind.USER_ACTIONS, '{"actions":[{"kind":"workspace_recreated"}]}'
    )
    first_request = _prepared_request(_snapshot(first), _sources(notice))
    _, first_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=first_request
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT,
        "entry:2xt3tmyw6w7nkoixnaassnppze",
        2,
        "turn:klmmazz67vfgsfsvg6ceqgdsfu",
        (LLMTextPart("done"),),
    )
    second = _user("next", sequence=3)
    second_request = replace(
        _prepared_request(_snapshot(first, assistant, second), _sources()),
        context_id="context:6igz6lmmsfk2xstdjjz6lfuzfy",
        model_call_index=2,
    )
    _, second_view = _compile_and_install_append(
        compiler=compiler, owner=owner, request=second_request
    )
    assert second_view.system_prompt == first_view.system_prompt
    assert second_view.tools == first_view.tools
    assert second_view.messages[: len(first_view.messages)] == first_view.messages
    notices = [
        _observation(message)
        for message in second_view.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
        and _observation(message)["source"] == "USER_ACTIONS"
    ]
    assert [item["body"] for item in notices] == [
        [{"text": '{"actions":[{"kind":"workspace_recreated"}]}'}]
    ]


def _observation(message):
    return json.loads(join_text_content(message.content))["pulsara_runtime_observation"]


def _observations(messages):
    return tuple(
        _observation(message)
        for message in messages
        if "pulsara_runtime_observation" in join_text_content(message.content)
    )


@pytest.mark.parametrize("already_installed", (False, True))
def test_native_projection_freeze_failure_does_not_advance_install_or_observation_state(
    already_installed,
):
    from pulsara_agent.conversation_kernel.memory.contracts import (
        MemoryObservationState,
    )

    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first = _user("first")
    old_state = InstalledObservationState(
        last_turn_id=first.source_turn_id, memory=MemoryObservationState()
    )
    prior = None
    if already_installed:
        _, prior = _compile_and_install_append(
            compiler=compiler,
            owner=owner,
            request=_prepared_request(
                _snapshot(first), _sources(observation_state=old_state)
            ),
        )
    items = (
        (first, _user("new actual human input", sequence=2))
        if already_installed
        else (first,)
    )
    new_state = replace(
        old_state, memory=MemoryObservationState(preferences=(), recall=())
    )
    request = _prepared_request(
        _snapshot(*items), _sources(observation_state=new_state)
    )
    _, candidate = _prepare_append_candidate(
        compiler=compiler, owner=owner, request=request
    )
    owner.register(candidate)
    callbacks = []

    def failed_freeze():
        callbacks.append("failed")
        assert owner.current_view(candidate.scope) is prior
        raise RuntimeError("native range database transaction failed")

    execution = object()
    with pytest.raises(RuntimeError, match="database transaction failed"):
        owner.install(
            candidate=candidate, execution=execution, before_install=failed_freeze
        )
    assert owner.current_view(candidate.scope) is prior
    if prior is not None:
        assert prior.observation_state.memory is old_state.memory
    permit = owner.install(
        candidate=candidate,
        execution=execution,
        before_install=lambda: callbacks.append("frozen"),
    )
    view = owner.current_view(candidate.scope)
    assert view.observation_state is candidate.resulting_observation_state
    assert view.observation_state.memory is new_state.memory and callbacks == [
        "failed",
        "frozen",
    ]
    owner.install_authority.consume(permit, candidate=candidate, execution=execution)
    assert view.epoch_revision == (2 if already_installed else 1)
    if prior is not None:
        assert view.system_prompt == prior.system_prompt and view.tools == prior.tools
        assert view.messages[: len(prior.messages)] == prior.messages


def test_previous_turn_outcome_is_one_new_turn_fact_and_active_compaction_restoration(
    tmp_path,
):
    facts = _canonical_facts(_snapshot(_user("next actual request")))
    facts = _with_round7_previous(facts, _round7_previous_fact(facts))
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=None,
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
    )
    arguments = dict(
        tool_surface=None,
        canonical_facts=facts,
        tool_exposure_plan=None,
        skill_dispatch_view=None,
        skill_owner_snapshot=None,
    )
    first = collector.freeze_non_trigger_sources(**arguments, new_context=True)
    source = next(
        item
        for item in first.candidates
        if item.source_kind is ContextSourceKind.PREVIOUS_TURN_OUTCOME
    )
    value = json.loads(source.text)["pulsara_runtime_observation"]
    assert value["source"] == "PREVIOUS_TURN_OUTCOME" and value["body"]
    assert value["body"] == [{"text": "紧邻前轮在任务完成前因执行失败结束。"}]
    state = InstalledObservationState(
        last_turn_id=facts.canonical_input.identity.turn_id
    )
    ordinary = collector.freeze_non_trigger_sources(**arguments, previous_state=state)
    assert all(
        item.source_kind is not ContextSourceKind.PREVIOUS_TURN_OUTCOME
        for item in ordinary.candidates
    )
    active = collector.freeze_non_trigger_sources(
        **arguments, previous_state=state, new_context=True, active_compaction=True
    )
    assert (
        next(
            item.text
            for item in active.candidates
            if item.source_kind is ContextSourceKind.PREVIOUS_TURN_OUTCOME
        )
        == source.text
    )


def test_single_source_contract_rejects_duplicates_and_only_omits_advisory_user_actions():
    with pytest.raises(ValueError, match="duplicated"):
        _sources(
            _candidate(ContextSourceKind.TURN_CONTEXT, "a"),
            _candidate(ContextSourceKind.TURN_CONTEXT, "b"),
        )
    with pytest.raises(ValueError, match="only the base"):
        _candidate(
            ContextSourceKind.APPROVED_PLAN, "exact", channel=ContextChannel.SYSTEM
        )
    with pytest.raises(ValueError, match="only advisory user actions"):
        _candidate(
            ContextSourceKind.MEMORY_RECALL,
            "complete",
            budget=ContextBudgetClass.OPTIONAL,
        )
    with pytest.raises(UnicodeEncodeError):
        _candidate(ContextSourceKind.BASE_SYSTEM, "\ud800")
    actions = _candidate(
        ContextSourceKind.USER_ACTIONS,
        "actual advisory action",
        budget=ContextBudgetClass.OPTIONAL,
    )
    plan = _candidate(
        ContextSourceKind.APPROVED_PLAN, "complete approved plan", placement=31
    )
    request = _prepared_request(_snapshot(_user("trigger")), _sources(actions, plan))
    compiler = StructuredModelInputCompiler()
    complete = compiler.compile(request)
    omitted = compiler.compile(
        request, omitted_sources=(ContextSourceKind.USER_ACTIONS,)
    )
    assert len(complete.messages) == len(omitted.messages) + 1
    assert omitted.compile_report.omitted_source_count == 1
    assert next(
        item
        for item in omitted.source_decisions
        if item.source_kind is ContextSourceKind.APPROVED_PLAN
    ).included
    assert not next(
        item
        for item in omitted.source_decisions
        if item.source_kind is ContextSourceKind.USER_ACTIONS
    ).included
    assert any(
        "complete approved plan" in join_text_content(m.content)
        for m in omitted.messages
    )
