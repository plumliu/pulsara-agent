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
from tests.test_round3_structured_model_input_compiler import (
    _snapshot,
    _sources,
    _prepared_request,
    _compile_and_install_append,
)


def _group(turn, start=1, image=False):
    contents = [
        (LLMTextPart("profile"),),
        (LLMTextPart("instruction"),),
        (LLMTextPart("dependency A"),),
        (LLMTextPart("dependency C"),),
    ]
    if image:
        contents[-1] += (bound_test_image("image/png", b"pixels", 2, 2),)
    return tuple(
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
                    Origin.AGENT_PROFILE,
                    Origin.AGENT_INSTRUCTION,
                    Origin.AGENT_MATERIAL,
                    Origin.AGENT_MATERIAL,
                ),
                strict=True,
            )
        )
    )


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
        turn_id=items[1].source_turn_id,
        instruction_entry_id=items[1].source_entry_id,
        ordered_items=items,
        predecessor_through_sequence=10,
        initial_context_binding_revision_id=derive_id("binding", "agent-input-test"),
        initial_base_kind=ContextBindingBaseKind.FULL_HISTORY,
        initial_context_snapshot_id=None,
        initial_source_through_sequence=10,
    )
    assert group.ordered_items[1].source_entry_sequence == 12
    assert group.provider_input_through_sequence == 14
    assert group.ordered_items == items
    assert group.initial_revision_ordinal == 0
    with pytest.raises(ValueError, match="started turn input group is invalid"):
        replace(group, initial_source_through_sequence=11)
    with pytest.raises(ValueError, match="started turn input group does not exact-join"):
        replace(group, instruction_entry_id=derive_id("entry", "missing"))


def test_agent_group_has_independent_ordered_user_messages_and_instruction_compaction():
    items = _group(derive_id("turn", "agent-input-test", "compile"), image=True)
    snapshot = _snapshot_for(items, items[1])
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(snapshot, _sources())
    )
    texts = [
        part.text
        for message in compiled.messages
        if message.role is MessageRole.USER
        for part in message.content
        if isinstance(part, LLMTextPart)
        and part.text in {"profile", "instruction", "dependency A", "dependency C"}
    ]
    assert texts == ["profile", "instruction", "dependency A", "dependency C"]
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
    assert active.entry_id == items[1].source_entry_id
    assert active.input_origin is Origin.AGENT_INSTRUCTION
    assert active.content.parts == (LLMTextPart("instruction"),)


def test_reused_agent_appends_profile_and_materials_within_same_epoch():
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    first = _group(derive_id("turn", "agent-input-test", "first"))
    _, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=_prepared_request(_snapshot_for(first, first[1]), _sources()),
    )
    second = _group(derive_id("turn", "agent-input-test", "second"), 5)
    second = (replace(second[0], content=(LLMTextPart("new profile"),)), *second[1:])
    _, appended = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=_prepared_request(
            _snapshot_for((*first, *second), second[1]), _sources()
        ),
    )
    assert appended.epoch_nonce == installed.epoch_nonce
    assert appended.system_prompt == installed.system_prompt
    assert appended.tools == installed.tools
    assert appended.messages[: len(installed.messages)] == installed.messages
    assert appended.canonical_frontier.through_sequence == 8


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
    from tests.test_round3_structured_model_input_compiler import (
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
            if candidate.source_kind is ContextSourceKind.RUNTIME_ENVIRONMENT
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
        expected = {
            "workspace_kind": "project",
            "workspace_root": str(tmp_path.resolve()),
            "timezone": "UTC+00:00",
            "utc_offset_minutes": 0,
        }
        assert all(json.loads(variant.text) == expected for variant in before.variants)
        try:
            cell_id = await host.start(
                source="console.log('keep'); await tools.read_file({});",
                source_exec_attempt_id=derive_id("attempt", "environment-facts"),
                turn_id=derive_id("turn", "environment-facts"),
                snapshot=object(), tool_catalog={"read_file": {}}, invoke=invoke,
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
            assert stopped[0]["has_unread_output"] is True
            assert stopped[0]["pending_calls"] == 1
            assert environment() == before
        finally:
            release.set()
            await host.aclose()

    asyncio.run(scenario())


def test_environment_fact_changes_are_appended_without_rebuilding_prefix():
    import json

    from pulsara_agent.model_input.continuity import decode_runtime_observation
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from tests.test_round3_structured_model_input_compiler import _candidate

    kind = ContextSourceKind.RUNTIME_ENVIRONMENT
    previous_body = '{"workspace_kind":"project","workspace_root":"/workspace","timezone":"UTC","utc_offset_minutes":0}'
    current_body = '{"workspace_kind":"project","workspace_root":"/workspace","timezone":"UTC+01:00","utc_offset_minutes":60}'
    first_source = _candidate(kind, (previous_body, previous_body))
    next_source = _candidate(kind, (current_body, current_body))
    items = _group(derive_id("turn", "environment-facts-prefix-test"))
    snapshot = _snapshot_for(items, items[1])
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    _, first = _compile_and_install_append(
        compiler=compiler, owner=owner,
        request=_prepared_request(snapshot, _sources(first_source)),
    )
    next_items = _group(derive_id("turn", "environment-facts-prefix-test-next"), 5)
    _, second = _compile_and_install_append(
        compiler=compiler, owner=owner,
        request=_prepared_request(
            _snapshot_for((*items, *next_items), next_items[1]), _sources(next_source)
        ),
    )
    assert second.epoch_nonce == first.epoch_nonce
    assert second.system_prompt == first.system_prompt
    assert second.tools == first.tools
    assert second.messages[:len(first.messages)] == first.messages
    appended = [
        decode_runtime_observation(message)
        for message in second.messages[len(first.messages):]
        if message.role is MessageRole.USER and message.content
        and isinstance(message.content[0], LLMTextPart)
        and "pulsara_runtime_observation" in message.content[0].text
    ]
    environment = next(item for item in appended if item.source_kind is kind)
    assert json.loads(environment.body) == json.loads(current_body)
