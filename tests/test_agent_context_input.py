"""Agent inputs reuse the ordinary compiler, continuity and compaction owners."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import (
    LLMTextPart,
    LLMImagePart,
    MessageRole,
)
from pulsara_agent.conversation_kernel.steer import StartedTurnInputGroup
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionTargetBranch,
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    freeze_compaction_continuation,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind as Origin,
    ContextBindingBaseKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind as Kind,
    ModelInputScopeKind,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
)
from tests.support.images import bound_test_image
from tests.support.round3 import new_test_provider_input_continuity_owner
from tests.support.model_input import (
    _snapshot,
    _sources,
    _prepared_request,
    _compile_and_install_append,
)


def _group(turn, start=1, image=False, *, include_context=True):
    contents = [
        (LLMTextPart("instruction"),),
        (LLMTextPart("initial context"),),
        (LLMTextPart("dependency A"),),
        (LLMTextPart("dependency C"),),
    ]
    if image:
        # Synthetic canonical image; Work delegation currently supplies text.
        contents[-1] += (bound_test_image("image/png", b"pixels", 2, 2),)
    items = tuple(
        FrozenProviderInputItem(
            Kind.USER,
            derive_id("entry", "agent-input-test", turn, str(i)),
            start + i,
            turn,
            content,
            input_origin=origin,
        )
        for i, (content, origin) in enumerate(
            zip(
                contents,
                (
                    Origin.AGENT_INSTRUCTION,
                    Origin.AGENT_MATERIAL,
                    Origin.AGENT_MATERIAL,
                    Origin.AGENT_MATERIAL,
                ),
                strict=True,
            )
        )
    )
    if include_context:
        return items
    return tuple(replace(item,source_entry_sequence=start+ordinal)
        for ordinal,item in enumerate(item for index,item in enumerate(items) if index!=1))


def _snapshot_for(items, instruction):
    snapshot = _snapshot(
        *items,
        scope=ModelInputScopeKind.SUBAGENT,
        turn_id=instruction.source_turn_id,
        canonical_expanded_bytes=sum(
            provider_input_item_canonical_expanded_bytes(x) for x in items
        ),
    )
    values = dict(
        session_id=snapshot.identity.session_id,
        turn_id=instruction.source_turn_id,
        initial_entry_id=instruction.source_entry_id,
        context_binding_revision_id=snapshot.identity.context_binding_revision_id,
        provider_input_through_sequence=items[-1].source_entry_sequence,
        conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
    )
    identity = replace(
        snapshot.identity,
        **values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(**values),
    )
    return replace(
        snapshot,
        identity=identity,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=snapshot.items,
            canonical_expanded_bytes=snapshot.canonical_expanded_bytes,
            closures=snapshot.closures,
            late_outcomes=snapshot.late_outcomes,
        ),
    )


def test_started_group_distinguishes_instruction_suffix_end_and_predecessor():
    items = _group(derive_id("turn", "agent-input-test", "candidate"), 11, image=True)
    group = StartedTurnInputGroup(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        turn_id=items[0].source_turn_id,
        instruction_entry_id=items[0].source_entry_id,
        ordered_items=items,
        predecessor_through_sequence=10,
        initial_context_binding_revision_id=derive_id("binding", "agent-input-test"),
        initial_base_kind=ContextBindingBaseKind.FULL_HISTORY,
        initial_context_snapshot_id=None,
        initial_source_through_sequence=10,
    )
    assert group.ordered_items[0].source_entry_sequence == 11
    assert group.provider_input_through_sequence == 14
    assert group.ordered_items == items
    assert group.initial_revision_ordinal == 0
    with pytest.raises(ValueError, match="exact-join"):
        replace(group, instruction_entry_id=items[1].source_entry_id)
    inherited = replace(
        group,
        initial_base_kind=ContextBindingBaseKind.SNAPSHOT,
        initial_context_snapshot_id=derive_id("snapshot", "agent-input-test"),
        initial_source_through_sequence=4,
    )
    assert inherited.initial_source_through_sequence == 4
    assert inherited.predecessor_through_sequence == 10
    with pytest.raises(ValueError, match="started turn input group is invalid"):
        replace(group, initial_source_through_sequence=11)
    with pytest.raises(ValueError, match="started turn input group does not exact-join"):
        replace(group, instruction_entry_id=derive_id("entry", "missing"))


def test_agent_group_has_independent_ordered_user_messages_and_instruction_compaction():
    items = _group(derive_id("turn", "agent-input-test", "compile"), image=True)
    snapshot = _snapshot_for(items, items[0])
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(snapshot, _sources())
    )
    texts = [
        part.text
        for message in compiled.messages
        if message.role is MessageRole.USER
        for part in message.content
        if isinstance(part, LLMTextPart)
        and part.text in {"initial context", "instruction", "dependency A", "dependency C"}
    ]
    assert texts == ["instruction","initial context","dependency A","dependency C"]
    assert (
        sum(
            isinstance(part, LLMImagePart)
            for message in compiled.messages
            for part in message.content
        )
        == 1
    )
    _, active = freeze_compaction_continuation(
        source_view=SimpleNamespace(
            canonical_dispatch_read=SimpleNamespace(
                compile_snapshot=SimpleNamespace(canonical_input=snapshot)
            )
        ),
        target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        source_through_sequence=3,
    )
    assert active == items[0].source_entry_id
    original = next(item for item in snapshot.items if item.source_entry_id == active)
    assert original.input_origin is Origin.AGENT_INSTRUCTION
    assert original.content == (LLMTextPart("instruction"),)


def test_reused_agent_appends_instruction_and_materials_within_same_epoch():
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first = _group(derive_id("turn", "agent-input-test", "first"))
    _, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=_prepared_request(_snapshot_for(first, first[0]), _sources()),
    )
    second = _group(
        derive_id("turn", "agent-input-test", "second"), 5, include_context=False
    )
    _, appended = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=_prepared_request(
            _snapshot_for((*first, *second), second[0]), _sources()
        ),
    )
    assert appended.epoch_nonce == installed.epoch_nonce
    assert appended.system_prompt == installed.system_prompt
    assert appended.tools == installed.tools
    assert appended.messages[: len(installed.messages)] == installed.messages
    suffix_text = [
        part.text
        for message in appended.messages[len(installed.messages):]
        for part in message.content
        if isinstance(part, LLMTextPart)
        and part.text in {"instruction", "dependency A", "dependency C"}
    ]
    assert suffix_text == ["instruction", "dependency A", "dependency C"]
    assert appended.canonical_frontier.through_sequence == 7


def test_live_cell_changes_do_not_change_environment_facts(tmp_path):
    import asyncio
    from datetime import datetime, timezone
    import json

    from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
    from pulsara_agent.conversation_kernel.context_sources import (
        KernelContextSourceCollector,
    )
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from tests.support.model_input import (
        _Capability,
        _collect_context_sources,
        _canonical_facts,
    )

    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=_Capability(),
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
    )
    surface = _prepared_request(_snapshot(), _sources()).compile_binding.tool_surface

    def environment():
        sources = _collect_context_sources(
            collector,
            activation_subject=None,
            activation_text="",
            tool_surface=surface,
            canonical_facts=_canonical_facts(),
        )
        return next(
            candidate for candidate in sources.candidates
            if candidate.source_kind is ContextSourceKind.TURN_CONTEXT
        )

    async def scenario():
        host = CodeModeHost()
        started, release = asyncio.Event(), asyncio.Event()

        async def invoke(_):
            started.set()
            await release.wait()
            return CodeModeToolResult(value={"settled": True})

        session = SimpleNamespace(_code_mode=host)
        before = environment()
        observation=json.loads(before.text)['pulsara_runtime_observation']
        assert observation['source']=='TURN_CONTEXT'
        text='\n'.join(item['text'] for item in observation['body'])
        assert str(tmp_path.resolve()) in text and 'project' in text and 'UTC 偏移：+00:00' in text
        assert '2026-10-29' in text
        try:
            cell_id = await host.start(
                source="console.log('keep'); await tools.read_file({});",
                source_exec_attempt_id=derive_id("attempt", "environment-facts"),
                turn_id=derive_id("turn", "environment-facts"),
                snapshot=object(), tool_catalog={"decide": {}, "read_file": {}}, invoke=invoke,
                decision_available=False,
            )
            await asyncio.wait_for(started.wait(), 5)
            running = KernelHostSession.code_mode_cells(session)
            assert running[0]["cell_id"] == cell_id
            assert running[0]["state"] == "running"
            assert running[0]["pending_calls"] == 1
            assert environment() == before

            await host.stop(cell_id)
            stopped = KernelHostSession.code_mode_cells(session)
            assert stopped[0]["cell_id"] == cell_id
            assert stopped[0]["state"] == "terminated"
            assert stopped[0]["has_unread_output"] is False
            assert stopped[0]["pending_calls"] == 1
            assert environment() == before
        finally:
            release.set()
            await host.aclose()

    asyncio.run(scenario())


def test_environment_fact_changes_are_appended_without_rebuilding_prefix():
    import json

    from pulsara_agent.model_input.contracts import ContextSourceKind
    from tests.support.model_input import _candidate

    kind = ContextSourceKind.TURN_CONTEXT
    previous_body = "工作区类型：project；根路径：/workspace；时区：UTC；UTC 偏移：+00:00。"
    current_body = "工作区类型：project；根路径：/workspace；时区：UTC+01:00；UTC 偏移：+01:00。"
    first_source = _candidate(kind, previous_body)
    next_source = _candidate(kind, current_body)
    items = _group(derive_id("turn", "environment-facts-prefix-test"))
    snapshot = _snapshot_for(items, items[0])
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    _, first = _compile_and_install_append(
        compiler=compiler, owner=owner,
        request=_prepared_request(snapshot, _sources(first_source)),
    )
    next_items = _group(
        derive_id("turn", "environment-facts-prefix-test-next"), 5, include_context=False
    )
    _, second = _compile_and_install_append(
        compiler=compiler, owner=owner,
        request=_prepared_request(
            _snapshot_for((*items, *next_items), next_items[0]), _sources(next_source)
        ),
    )
    assert second.epoch_nonce == first.epoch_nonce
    assert second.system_prompt == first.system_prompt
    assert second.tools == first.tools
    assert second.messages[:len(first.messages)] == first.messages
    appended = [
        json.loads(message.content[0].text)["pulsara_runtime_observation"]
        for message in second.messages[len(first.messages):]
        if message.role is MessageRole.USER and message.content
        and isinstance(message.content[0], LLMTextPart)
        and "pulsara_runtime_observation" in message.content[0].text
    ]
    environment = next(item for item in appended if item["source"]==kind.value)
    assert environment["body"]==[{"text":current_body}]
