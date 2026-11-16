"""Current source envelopes, exact content and append-only compilation."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.context_sources import (
    KernelContextSourceCollector,
    observation_source,
)
from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelPreparationRequest,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    CollectedContextSources,
    ContextChannel,
    ContextSourceCandidate,
    ContextSourceKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    StructuredModelInputCompileRequest,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    InstalledObservationState,
    RuntimeObservationItem,
    encode_runtime_observation,
)
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, join_text_content
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import (
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.support.round3 import (
    StructuredToolPort,
    prepare_test_model_call,
    static_canonical_compile_facts,
)


def request(*, source_text="working", hook=None, maximum_input_tokens=100_000, capture_prepared=None):
    session, turn, entry, binding = (
        random_id(kind) for kind in ("session", "turn", "entry", "binding")
    )
    fields = dict(
        session_id=session,
        turn_id=turn,
        initial_entry_id=entry,
        context_binding_revision_id=binding,
        provider_input_through_sequence=1,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
    )
    identity = CanonicalModelInputIdentity(
        **fields,
        identity_fingerprint=canonical_model_input_identity_fingerprint(**fields),
    )
    items = (
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER,
            entry,
            1,
            turn,
            (LLMTextPart("do work"),),
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        ),
    )
    canonical = CanonicalModelInputSnapshot(
        identity,
        items,
        7,
        canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=items,
            canonical_expanded_bytes=7,
            closures=(),
            late_outcomes=(),
        ),
    )
    runtime = model_runtime(model_id="test-pro", wire_api="openai_chat_completions")
    port = DirectKernelModelPort(model_runtime=runtime)
    surface = StructuredToolPort(object(), tool_names=()).snapshot_tool_surface(
        conversation_scope_kind=ModelInputScopeKind.ROOT
    )
    prepared = prepare_test_model_call(
        port,
        KernelModelPreparationRequest(
            session_id=session,
            turn_id=turn,
            model_call_index=1,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            maximum_input_tokens=maximum_input_tokens,
            binding=model_binding(runtime),
            tool_surface=surface,
        ),
    )
    if capture_prepared is not None:
        capture_prepared.append((port, prepared))
    sources = CollectedContextSources(
        (
            ContextSourceCandidate(
                ContextSourceKind.BASE_SYSTEM, ContextChannel.SYSTEM, "SYSTEM", 0
            ),
            observation_source(
                ContextSourceKind.APPROVED_PLAN,
                (RuntimeObservationItem(source_text),),
                31,
            ),
        ),
        hook_context=hook,
        observation_state=InstalledObservationState(),
    )
    return StructuredModelInputCompileRequest(
        random_id("context"),
        1,
        canonical,
        static_canonical_compile_facts(canonical),
        prepared.compile_binding,
        sources,
        dispatch_anchor_entry_id=entry,
    )


def test_public_observation_preserves_order_and_exact_targets():
    target = {"kind": "PATH", "path": "/tmp/a"}
    message = encode_runtime_observation(
        source_kind=ContextSourceKind.USER_ACTIONS,
        body=(
            RuntimeObservationItem("first", target),
            RuntimeObservationItem("second"),
        ),
    )
    assert json.loads(join_text_content(message.content)) == {
        "pulsara_runtime_observation": {
            "source": "USER_ACTIONS",
            "body": [{"text": "first", "target": target}, {"text": "second"}],
        }
    }


def test_full_approved_plan_exceeds_former_source_cap_without_rewrite():
    text = '"\\\n' * (400_000)
    compiled = StructuredModelInputCompiler().compile(request(source_text=text))
    observation = json.loads(join_text_content(compiled.messages[0].content))
    assert observation["pulsara_runtime_observation"]["body"] == [{"text": text}]
    assert join_text_content(compiled.messages[-1].content) == "do work"


def test_hook_is_independent_after_trigger_and_keeps_duplicate_events():
    text = '{"pulsara_hook_context":[{"event":"PostToolUse","text":"same"},{"event":"PostToolUse","text":"same"}]}'
    compiled = StructuredModelInputCompiler().compile(
        request(hook=LLMMessage.user(text))
    )
    assert join_text_content(compiled.messages[-2].content) == "do work"
    assert join_text_content(compiled.messages[-1].content) == text
    assert len(json.loads(text)["pulsara_hook_context"]) == 2


def test_turn_context_does_not_repeat_on_ordinary_call_and_idle_compaction():
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root="/tmp",
        capability_composer=None,
        base_system_prompt="SYSTEM",
        display_timezone=ZoneInfo("Asia/Shanghai"),
        clock=lambda: datetime(2026, 11, 16, tzinfo=timezone.utc),
    )
    facts = request().canonical_facts
    fields = dict(
        tool_surface=None,
        canonical_facts=facts,
        tool_exposure_plan=None,
        skill_dispatch_view=None,
        skill_owner_snapshot=None,
    )
    first = collector.freeze_non_trigger_sources(**fields, new_context=True)
    assert [part.source_kind for part in first.candidates] == [
        ContextSourceKind.BASE_SYSTEM,
        ContextSourceKind.TURN_CONTEXT,
    ]
    state = InstalledObservationState(
        last_turn_id=facts.canonical_input.identity.turn_id
    )
    repeated = collector.freeze_non_trigger_sources(**fields, previous_state=state)
    assert [part.source_kind for part in repeated.candidates] == [
        ContextSourceKind.BASE_SYSTEM
    ]
    idle = collector.freeze_non_trigger_sources(
        **fields, new_context=True, allow_turn_observations=False
    )
    assert [part.source_kind for part in idle.candidates] == [
        ContextSourceKind.BASE_SYSTEM
    ]
    active = collector.freeze_non_trigger_sources(
        **fields, previous_state=state, new_context=True, active_compaction=True
    )
    body = json.loads(active.candidates[1].text)["pulsara_runtime_observation"]["body"]
    assert "UTC 偏移：+08:00" in body[0]["text"]
    assert "冻结权限" in body[1]["text"]
    assert "2026-11-16T08:00:00+08:00" in body[2]["text"]


def test_mcp_schema_diff_survives_notready_and_ignores_aba_aliases():
    from types import SimpleNamespace as NS
    from pulsara_agent.conversation_kernel.context_sources import (
        _mcp_changes,
        _mcp_comparison_snapshot,
    )
    from pulsara_agent.primitives.context import freeze_json

    def owner(*, description="old", status="READY", alias="remote_alias"):
        server = NS(
            server_id="server1",
            configuration=NS(transport="endpoint", auth="no-auth", public_headers=()),
            connection_target={"url": "https://service.example/mcp"},
            runtime_source=None,
            display_name="Service",
            status=NS(value=status),
            sanitized_instructions="Use it",
            scope_subagents=False,
        )
        semantic = NS(
            server_id="server1",
            configuration=NS(transport="endpoint", auth="no-auth", public_headers=()),
            remote_tool_name="lookup",
            description=description,
            provider_tool_name=alias,
            input_schema=freeze_json({"type": "object"}),
            output_schema=None,
            root_visible=True,
            subagent_visible=False,
        )
        tool = NS(
            semantic=semantic,
            policy=NS(effect_kind="READ_ONLY", parallel_safe=True, timeout_ms=1000),
        )
        return NS(
            catalog_snapshot=NS(servers=(server,)),
            inspection_inputs=(tool,) if status == "READY" else (),
        )

    first = _mcp_comparison_snapshot(None, owner())
    assert not _mcp_changes(
        first, _mcp_comparison_snapshot(first, owner(alias="different_alias"))
    )
    unavailable = _mcp_comparison_snapshot(first, owner(status="FAILED"))
    assert unavailable.inspection_inputs == first.inspection_inputs
    assert "暂不可用" in _mcp_changes(first, unavailable)[0].text
    restored = _mcp_comparison_snapshot(
        unavailable, owner(description="new instructions")
    )
    text = _mcp_changes(unavailable, restored)[0].text
    assert "恢复可用" in text and "工具使用说明" in text
    assert not _mcp_changes(first, _mcp_comparison_snapshot(first, owner()))
    changed = owner()
    changed.catalog_snapshot.servers[0].configuration.transport = "changed endpoint"
    notice = _mcp_changes(first, _mcp_comparison_snapshot(first, changed))
    assert len(notice) == 1 and "连接配置" in notice[0].text
    assert notice[0].target == _mcp_changes(None, first)[0].target


def test_memory_reads_only_real_root_human_and_carries_uninstalled_candidate():
    import asyncio
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from pulsara_agent.conversation_kernel.provider_dispatch import (
        ProviderDispatchCoordinator,
    )
    from pulsara_agent.conversation_kernel.memory.contracts import (
        MemoryObservationCandidate,
        MemoryObservationState,
        MemoryObservationText,
        FrozenMemoryTriggerPolicy,
        AutomaticMemoryTriggerDisposition,
        MemoryUsePolicy,
    )
    from pulsara_agent.model_input.contracts import CapabilityActivationSubjectKind

    req = request()
    previous = MemoryObservationState()
    next_state = MemoryObservationState(preferences=(), recall=())

    def classify(text):
        return FrozenMemoryTriggerPolicy(
            AutomaticMemoryTriggerDisposition.ELIGIBLE,
            MemoryUsePolicy.WRITE_DISABLED_BY_USER
            if text == "do not save this"
            else MemoryUsePolicy.ENABLED,
            True,
        )

    support = NS(
        available=True,
        classify_trigger=classify,
        prepare_observation=AsyncMock(
            return_value=MemoryObservationCandidate(
                (MemoryObservationText("memory text"),), next_state
            )
        ),
    )
    owner = ProviderDispatchCoordinator.__new__(ProviderDispatchCoordinator)
    owner._memory_support = support
    original = replace(
        req.sources, observation_state=InstalledObservationState(memory=previous)
    )

    async def scenario():
        ordinary, policy = await owner._prepare_human_memory(
            sources=original,
            facts=req.canonical_facts,
            activation=None,
            human_texts=("background",),
            inherited_policy=MemoryUsePolicy.ENABLED,
            resets_policy=False,
            model_surface=NS(tool_specs=()),
        )
        assert ordinary is original and support.prepare_observation.await_count == 0
        selected, policy = await owner._prepare_human_memory(
            sources=original,
            facts=req.canonical_facts,
            activation=CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
            human_texts=("remember preference", "do not save this"),
            inherited_policy=MemoryUsePolicy.ENABLED,
            resets_policy=True,
            model_surface=NS(tool_specs=(NS(name="remember"),)),
        )
        assert policy is MemoryUsePolicy.WRITE_DISABLED_BY_USER
        assert selected.observation_state.memory is next_state
        assert original.observation_state.memory is previous
        assert ContextSourceKind.MEMORY_RECALL in {
            item.source_kind for item in selected.candidates
        }
        assert ContextSourceKind.MEMORY_WRITE_HINT not in {
            item.source_kind for item in selected.candidates
        }

    asyncio.run(scenario())


def test_pending_native_fallback_keeps_source_frontier_and_leaves_range_undelivered():
    from pulsara_agent.conversation_kernel.native_tool_delivery import (
        NativeResultPosition,
        project_native_tool_page,
        bind_native_observation,
        resolve_tool_result_next,
    )
    from pulsara_agent.model_input.contracts import (
        ProviderToolCall,
        ProviderToolResultContextMetadata,
        provider_input_item_fingerprint,
    )
    from pulsara_agent.primitives.context import freeze_json, thaw_json
    from pulsara_agent.primitives.tool_observation import (
        freeze_tool_observation_timing_fact,
        ToolObservationOrigin,
    )

    req = request()
    identity = req.canonical_input.identity
    assistant_entry, result_entry = random_id("entry"), random_id("entry")
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    page = project_native_tool_page(
        session_id=identity.session_id,
        entry_id=result_entry,
        value={
            "meta": {"status": "SUCCESS"},
            "content": [{"type": "text", "value": "complete saved content"}],
        },
        image_reader=lambda _: None,
        start=NativeResultPosition(0),
    )
    page = bind_native_observation(page, observed_at=now, duration_microseconds=12)
    timing = freeze_tool_observation_timing_fact(
        session_id=identity.session_id,
        turn_id=identity.turn_id,
        observed_at=now,
        observation_duration_microseconds=12,
        tool_reported_duration_microseconds=None,
        observation_origin=ToolObservationOrigin.BUILTIN,
    )
    metadata = ProviderToolResultContextMetadata(
        identity.session_id,
        random_id("result"),
        result_entry,
        "SUCCESS",
        None,
        timing,
    )
    assistant = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        assistant_entry,
        2,
        identity.turn_id,
        (),
        tool_calls=(ProviderToolCall("call:1", "test_tool", freeze_json({})),),
    )
    result = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT,
        result_entry,
        3,
        identity.turn_id,
        (LLMTextPart(page.canonical_text),),
        tool_call_id="call:1",
        tool_request_entry_id=assistant_entry,
        tool_result_context=metadata,
        tool_result_body_text=page.canonical_text,
        tool_call_ordinal=0,
        native_page=page,
    )
    fields = dict(
        session_id=identity.session_id,
        turn_id=identity.turn_id,
        initial_entry_id=identity.initial_entry_id,
        context_binding_revision_id=identity.context_binding_revision_id,
        provider_input_through_sequence=3,
        conversation_scope_kind=identity.conversation_scope_kind,
    )
    identity = CanonicalModelInputIdentity(
        **fields,
        identity_fingerprint=canonical_model_input_identity_fingerprint(**fields),
    )
    items = (*req.canonical_input.items, assistant, result)
    canonical = CanonicalModelInputSnapshot(
        identity,
        items,
        1000,
        canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=items,
            canonical_expanded_bytes=1000,
            closures=(),
            late_outcomes=(),
        ),
    )
    req = replace(
        req,
        canonical_input=canonical,
        canonical_facts=static_canonical_compile_facts(canonical),
    )
    compiler = StructuredModelInputCompiler()
    normal = compiler.compile(req)
    fallback = compiler.compile(req, unavailable_native_pages=True)
    a, b = normal.tool_result_decisions[0], fallback.tool_result_decisions[0]
    assert a.source_entry_fingerprint == b.source_entry_fingerprint
    projection = thaw_json(b.native_projection)
    assert projection["start"] == projection["end"] == [0, 0]
    source, start = resolve_tool_result_next(
        projection["next"], session_id=identity.session_id
    )
    assert source == result_entry and start == NativeResultPosition(0)
    assert "SUCCESS" in join_text_content(normal.messages[-1].content)
    assert "ERROR" in join_text_content(fallback.messages[-1].content)
    assert "complete saved content" not in join_text_content(
        fallback.messages[-1].content
    )
    restored = replace(
        result,
        content=(LLMTextPart("different frozen presentation"),),
        tool_result_body_text="different frozen presentation",
        native_page=None,
    )
    assert provider_input_item_fingerprint(restored) == provider_input_item_fingerprint(
        result
    )


def test_compile_diagnostics_project_actual_single_value_decisions():
    from pulsara_agent.model_input.diagnostics import (
        project_model_input_compile_observation,
    )

    compiled = StructuredModelInputCompiler().compile(request())
    value = project_model_input_compile_observation(
        model_call_index=1, compiled=compiled
    ).public_payload()
    assert value["omitted_source_count"] == 0
    assert len(value["decision_samples"]) == 2
    assert all(item["included"] for item in value["decision_samples"])


def test_large_cell_is_complete_and_fails_only_at_actual_provider_budget():
    import pytest
    from pulsara_agent.ports.terminal_observation import CellOutputContent
    captured=[]
    req=request(maximum_input_tokens=1000,capture_prepared=captured)
    port,prepared=captured[0]
    text='x'*(17<<20)
    value=CellOutputContent(cell_id=random_id('cell'),source_turn_id=req.canonical_input.identity.turn_id,
        source_exec_attempt_id=random_id('attempt'),record_sequence=1,
        record={'kind':'log','content':[{'type':'text','value':text}]})
    original=req.canonical_input.items[0]
    item=replace(original,item_kind=FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        content=value.content.parts,input_origin=CanonicalInputOriginKind.CELL_OUTPUT,cell_output_source=value.source)
    canonical=replace(req.canonical_input,items=(item,),canonical_expanded_bytes=value.resource_quote.canonical_expanded_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(identity=req.canonical_input.identity,
            items=(item,),canonical_expanded_bytes=value.resource_quote.canonical_expanded_bytes,closures=(),late_outcomes=()))
    req=replace(req,canonical_input=canonical,canonical_facts=static_canonical_compile_facts(canonical),
        sources=replace(req.sources,candidates=(req.sources.candidates[0],)))
    compiled=StructuredModelInputCompiler().compile(req)
    assert compiled.messages[0].content == (
        LLMTextPart(f"Cell {value.cell_id} 输出（log，第 1 条）：\n"), LLMTextPart(text))
    measured=port.freeze_wire_measurement(call=prepared.call,compile_binding=prepared.compile_binding,
        native_projection_set=prepared.native_projection_set,semantic_input=compiled,replay_hydration=None)
    assert measured.quote.budget_input_tokens>measured.quote.effective_input_budget_tokens
    with pytest.raises(ValueError,match="provider wire plan exceeds the input budget"):
        measured.prepare_executable_plan()


def test_mcp_notice_targets_are_consumable_local_and_plugin_query_targets(tmp_path):
    from types import SimpleNamespace as NS
    from pulsara_agent.capability.source_query import parse_query
    from pulsara_agent.conversation_kernel.capability_targets import mcp_connection_target
    from pulsara_agent.conversation_kernel.context_sources import _mcp_changes
    from pulsara_agent.mcp_config import LocalConfiguredMcpRuntimeSource,ManagedPackageMcpRuntimeSource
    from pulsara_agent.plugins.mcp_adapter import framed_plugin_mcp_server_id
    from pulsara_agent.primitives.context import thaw_json
    plugin_id='catalog-plugin'
    sources=(('local-service',LocalConfiguredMcpRuntimeSource()),
        (framed_plugin_mcp_server_id(plugin_id,'package-service'),ManagedPackageMcpRuntimeSource(
            store_scope_key='user',package_owner_key=plugin_id,package_install_id=random_id('package'))))
    for server_id,origin in sources:
        target=mcp_connection_target(server_id=server_id,runtime_source=origin,
            workspace_root=tmp_path/'workspace',pulsara_home=tmp_path/'home',user_config_path=tmp_path/'home'/'mcp.json')
        server=NS(server_id=server_id,runtime_source=origin,connection_target=target,configuration=None,
            display_name='Service',status=NS(value='READY'),sanitized_instructions='',scope_subagents=False)
        notice=_mcp_changes(None,NS(catalog_snapshot=NS(servers=(server,)),inspection_inputs=()))[0]
        actual=thaw_json(notice.target)
        assert parse_query({'kind':'MCP','target':actual})['target']==target
        assert actual==target
        assert actual['server_id']==('local-service' if isinstance(origin,LocalConfiguredMcpRuntimeSource) else 'package-service')


def test_cell_projection_labels_actual_source_and_preserves_occurrence_order():
    from pulsara_agent.model_input.contracts import provider_input_item_fingerprint
    from pulsara_agent.model_input.lowering import lower_canonical_item
    from pulsara_agent.ports.terminal_observation import CellOutputSource
    from tests.support.images import bound_test_image

    import base64
    image = bound_test_image('image/png', base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yHWQAAAAASUVORK5CYII='), 1, 1)
    source = CellOutputSource(random_id('cell'), 7, 'return')
    item = FrozenProviderInputItem(FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        random_id('entry'), 1, random_id('turn'),
        (LLMTextPart('before'), image, LLMTextPart('after')),
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT, cell_output_source=source)
    message = lower_canonical_item(item).message
    assert message.content[0] == LLMTextPart(f'Cell {source.cell_id} 输出（return，第 7 条）：\n')
    assert message.content[1] == LLMTextPart('before')
    assert message.content[-1] == LLMTextPart('after')
    assert message.content[-2] is image
    assert len(message.content) == 5  # source label, text, IMAGE_REF, image, text
    assert provider_input_item_fingerprint(item) != provider_input_item_fingerprint(
        replace(item, cell_output_source=replace(source, record_sequence=8)))


@pytest.mark.parametrize('api', ['openai_chat_completions', 'openai_responses'])
def test_text_only_cell_projects_each_image_in_place_in_actual_wire(api):
    from io import BytesIO
    from PIL import Image
    from pulsara_agent.conversation_kernel.native_tool_delivery import IMAGE_INPUT_UNSUPPORTED_TEXT
    from pulsara_agent.model_input.contracts import provider_input_item_fingerprint
    from pulsara_agent.ports.terminal_observation import CellOutputSource
    from pulsara_agent.primitives.context import thaw_json
    from tests.support.images import bound_test_image
    from tests.test_stage2_direct_model import _prepared_execution

    encoded = BytesIO()
    Image.new('RGB', (2, 2), (11, 22, 33)).save(encoded, 'PNG')
    image = bound_test_image('image/png', encoded.getvalue(), 2, 2)
    source = CellOutputSource(random_id('cell'), 3, 'return')
    content = (LLMTextPart('before'), image, LLMTextPart('between'), image, LLMTextPart('after'))
    item = FrozenProviderInputItem(FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        random_id('entry'), 1, 'turn:klmmazz67vfgsfsvg6ceqgdsfu', content,
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT, cell_output_source=source)
    fingerprint = provider_input_item_fingerprint(item)
    port = DirectKernelModelPort(model_runtime=model_runtime(model_id='test-pro',
        wire_api=api, input_modalities=('text',)))
    prepared, _ = _prepared_execution(port, canonical_items=(item,))
    try:
        messages = prepared.compiled_input.messages
        assert len(messages) == 1
        parts = messages[0].content
        assert parts[0] == LLMTextPart(f'Cell {source.cell_id} 输出（return，第 3 条）：\n')
        assert parts[1] == LLMTextPart('before')
        assert parts[3] == parts[6] == LLMTextPart(IMAGE_INPUT_UNSUPPORTED_TEXT)
        assert parts[4] == LLMTextPart('between') and parts[7] == LLMTextPart('after')
        for index in (2, 5):
            assert json.loads(parts[index].text)['pulsara_image']['target'] == {
                'kind':'IMAGE_REF', 'image_ref':image.image_ref}
        assert all(isinstance(part, LLMTextPart) for part in parts)
        wire = json.dumps([thaw_json(value) for value in prepared.wire_input_plan.materialization.ordered_input_items], ensure_ascii=False)
        assert wire.count(IMAGE_INPUT_UNSUPPORTED_TEXT) == 2
        assert wire.count(image.image_ref) == 2
        assert 'image_url' not in wire and 'input_image' not in wire and 'data:image' not in wire
        assert prepared.wire_input_plan.quote.final_wire_visual_image_tokens == 0
        assert prepared.wire_input_plan.quote.raw_final_wire_estimated_input_tokens > 0
        placement, = prepared.compiled_input.message_placements
        assert placement.origin_entry_id == item.source_entry_id
        assert placement.origin_item_fingerprint == fingerprint
        assert provider_input_item_fingerprint(item) == fingerprint
        assert item.content == content and item.cell_output_source is source
    finally:
        prepared.surface_borrow.close()


def test_cell_payload_read_failure_keeps_identity_quote_and_installed_prefix():
    from io import BytesIO
    from PIL import Image
    from pulsara_agent.llm.input import LLMImagePart
    from pulsara_agent.model_input.contracts import provider_input_item_fingerprint
    from pulsara_agent.ports.terminal_observation import (
        CellOutputSource, CellOutputImageReadFailure, cell_output_canonical_body,
    )
    from tests.support.images import bound_test_image
    from tests.support.round3 import new_test_provider_input_continuity_owner
    from tests.support.model_input import (
        _compile_and_install_append, _prepared_request, _snapshot, _sources, _user,
    )

    data = BytesIO()
    Image.new('RGB', (3, 2), (11, 22, 33)).save(data, 'PNG')
    image = bound_test_image('image/png', data.getvalue(), 3, 2)
    source = CellOutputSource(random_id('cell'), 4, 'log')
    content = (LLMTextPart('before'), image, LLMTextPart('after'))
    original = FrozenProviderInputItem(FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        random_id('entry'), 1, 'turn:klmmazz67vfgsfsvg6ceqgdsfu', content,
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT, cell_output_source=source)
    failure = CellOutputImageReadFailure(image_ref=image.image_ref, digest=image.content_digest,
        encoded_bytes=len(image.immutable_bytes), media_type=image.media_type,
        width=image.width, height=image.height, reason='暂时无法读取图片：实际读取超时。')
    failed = replace(original, content=(content[0], failure, content[2]))
    assert provider_input_item_fingerprint(failed) == provider_input_item_fingerprint(original)
    assert provider_input_item_fingerprint(replace(failed,
        content=(content[0], replace(failure, reason='图片数据验证失败。'), content[2]))) == provider_input_item_fingerprint(original)
    assert cell_output_canonical_body(failed.content) == cell_output_canonical_body(original.content)
    from pulsara_agent.model_input.lowering import lower_canonical_item
    from pulsara_agent.conversation_kernel.native_tool_delivery import IMAGE_INPUT_UNSUPPORTED_TEXT
    unsupported = lower_canonical_item(failed, supports_images=False)
    assert unsupported.source is failed
    assert unsupported.message.content[3] == LLMTextPart(IMAGE_INPUT_UNSUPPORTED_TEXT)

    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    _, first = _compile_and_install_append(compiler=compiler, owner=owner,
        request=_prepared_request(_snapshot(failed,
            canonical_expanded_bytes=cell_output_canonical_body(failed.content)[1]), _sources()))
    assert len(first.messages) == 1
    first_parts = first.messages[0].content
    assert first_parts[1] == content[0] and first_parts[-1] == content[2]
    assert first_parts[3] == LLMTextPart(failure.reason)
    assert image.image_ref in first_parts[2].text
    assert first.wire_input_plan.quote.final_wire_visual_image_tokens == 0

    from pulsara_agent.conversation_kernel.compaction.contracts import provider_input_item_canonical_expanded_bytes
    next_item = _user('next', sequence=2)
    recovered = _prepared_request(_snapshot(original, next_item, canonical_expanded_bytes=sum(
        provider_input_item_canonical_expanded_bytes(item) for item in (original, next_item))), _sources())
    _, next_view = _compile_and_install_append(compiler=compiler, owner=owner, request=recovered)
    assert next_view.system_prompt == first.system_prompt and next_view.tools == first.tools
    assert next_view.messages[:len(first.messages)] == first.messages
    assert len(next_view.messages) == 2
    assert all(not isinstance(part, LLMImagePart) for message in next_view.messages for part in message.content)
    cold = compiler.compile(recovered)
    assert sum(isinstance(part, LLMImagePart) for message in cold.messages for part in message.content) == 1
    assert image in cold.messages[0].content
    assert not any(failure.reason in part.text for message in cold.messages for part in message.content if isinstance(part, LLMTextPart))
