"""Agent inputs reuse the ordinary compiler, continuity and compaction owners."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    LLMImagePart,
    MessageRole,
)
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.steer import PreparedRootProviderInputCandidate
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
    _permission_snapshot,
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


def test_group_candidate_distinguishes_instruction_suffix_end_and_predecessor():
    items = _group(derive_id("turn", "agent-input-test", "candidate"), 11, image=True)
    prompts = tuple(
        freeze_canonical_prompt(FrozenPromptContent(x.content)) for x in items
    )
    candidate = PreparedRootProviderInputCandidate(
        session_id="session:336wrfwkamrr3up6kjtsgvgcte",
        workspace_id=derive_id("workspace", "agent-input-test"),
        exact_turn_id=items[1].source_turn_id,
        exact_initial_entry_id=items[1].source_entry_id,
        exact_context_binding_revision_id=derive_id("binding", "agent-input-test"),
        unpublished_items=items,
        unpublished_item_canonical_expanded_bytes=tuple(
            p.resource_quote.canonical_expanded_bytes for p in prompts
        ),
        permission_snapshot=_permission_snapshot(),
        model_call_binding=ModelCallBinding(
            derive_id("connection", "agent-input-test"), None
        ),
        expected_latest_entry_sequence=10,
        context_base_kind=ContextBindingBaseKind.FULL_HISTORY,
        context_snapshot_id=None,
        source_through_sequence=10,
        pending_plan_handoff_workflow_id=None,
        pending_plan_handoff_interaction_id=None,
        pending_plan_handoff_kind=None,
        unpublished_plan_workflow_fact=None,
        unpublished_plan_handoff_fact=None,
        unpublished_approved_plan_fact=None,
    )
    assert candidate.exact_initial_entry_sequence == 12
    assert candidate.resulting_provider_input_through_sequence == 14
    assert candidate.unpublished_canonical_expanded_bytes == sum(
        p.resource_quote.canonical_expanded_bytes for p in prompts
    )
    with pytest.raises(ValueError, match="full-history cut"):
        replace(candidate, source_through_sequence=11)
    with pytest.raises(ValueError, match="no instruction entry"):
        replace(candidate, exact_initial_entry_id=derive_id("entry", "missing"))


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


def test_terminal_cells_remain_in_context_until_late_settlement_output_is_consumed(
    tmp_path,
):
    from datetime import datetime, timezone
    import json
    from pulsara_agent.conversation_kernel.context_sources import (
        KernelContextSourceCollector,
    )
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from tests.test_round3_structured_model_input_compiler import (
        _Capability,
        _collect_context_sources,
        _canonical_facts,
    )

    current = {
        "cell_id": "cell:pending",
        "state": "terminated",
        "has_unread_output": False,
        "pending_calls": 1,
    }
    collector = KernelContextSourceCollector(
        workspace_kind="project",
        workspace_root=tmp_path,
        capability_composer=_Capability(),
        base_system_prompt="BASE",
        display_timezone=timezone.utc,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        code_mode_index_provider=lambda: (dict(current),),
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
        candidate = next(
            x
            for x in sources.candidates
            if x.source_kind is ContextSourceKind.RUNTIME_ENVIRONMENT
        )
        return [json.loads(x.text) for x in candidate.variants]

    assert all(x["code_cells"] == [current] for x in environment())
    current.update(state="failed", pending_calls=0, has_unread_output=True)
    assert all(x["code_cells"] == [current] for x in environment())
    current.update(has_unread_output=False)
    assert all("code_cells" not in x for x in environment())
