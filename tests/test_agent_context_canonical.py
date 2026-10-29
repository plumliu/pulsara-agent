"""Both Agent kinds read the same session-owned canonical history."""

import pytest

from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ModelInputScopeKind,
)
from tests.test_agent_requests_persistence import (
    source as source,
    request,
    start,
    finish,
    deadline,
    now,
)
from tests.test_round3_structured_model_input_compiler import (
    _prepared_request,
    _sources,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.llm.input import LLMTextPart, MessageRole

pytestmark = pytest.mark.postgres


def test_reused_agent_reads_ordinary_history_and_exact_group_cut(source):
    repository, root, admit = source
    first = request(profile="research_worker", instruction="First investigation.")
    admit(first)
    lease, candidate = start(repository, first)
    finish(repository, lease, candidate, "First final stays in this Agent history.")
    repository.release_host_writer(lease.guard, deadline_monotonic=deadline())
    second = request(
        agent_id=first.agent_id,
        create=False,
        profile="review_worker",
        instruction="Continue critically.",
    )
    admit(second)
    prospective = repository.prepare_prompt_head_consumption(
        session_id=first.agent_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    reader = CanonicalProviderInputReader(
        repository.connection_provider,
        blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
    )
    prepared_read = reader.read_prospective_root_dispatch(
        prospective.provider_input_candidate, deadline_monotonic=deadline()
    )
    lease, accepted = start(repository, second)
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=accepted.exact_turn_id, deadline_monotonic=deadline()
    )
    actual = reader.read_frozen_dispatch(cut, deadline_monotonic=deadline())
    assert actual == prepared_read
    canonical = actual.compile_snapshot.canonical_input
    assert canonical.identity.conversation_scope_kind is ModelInputScopeKind.SUBAGENT
    assert canonical.identity.initial_entry_id == accepted.exact_initial_entry_id
    assert (
        canonical.identity.provider_input_through_sequence
        == accepted.provider_input_candidate.resulting_provider_input_through_sequence
    )
    assert [item.input_origin for item in canonical.items if item.input_origin] == [
        CanonicalInputOriginKind.AGENT_PROFILE,
        CanonicalInputOriginKind.AGENT_INSTRUCTION,
        CanonicalInputOriginKind.AGENT_PROFILE,
        CanonicalInputOriginKind.AGENT_INSTRUCTION,
    ]
    texts = [
        part.text
        for item in canonical.items
        for part in item.content
        if isinstance(part, LLMTextPart)
    ]
    assert texts[-3:] == [
        "First final stays in this Agent history.",
        accepted.ordered_input_entries[0].canonical_prompt.content.parts[0].text,
        "Continue critically.",
    ]
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(
            canonical, _sources(), canonical_facts=actual.compile_snapshot
        )
    )
    assert "First investigation." in [
        part.text
        for msg in compiled.messages
        if msg.role is MessageRole.USER
        for part in msg.content
        if isinstance(part, LLMTextPart)
    ]

    from pulsara_agent.conversation_kernel.session_content import SessionContentQuery
    from pulsara_agent.ports.session_content import SessionContentRange
    from tests.support.history_projection import maintain_history
    from pulsara_agent.identity import random_id

    root_query = SessionContentQuery(
        repository, session_id=root.guard.session_id, memory_domain_id="u_local"
    )
    received = root_query.invoke(
        "read_session_content",
        {"target": {"kind": "SESSION", "session_id": first.agent_id}},
        current_range=SessionContentRange(0),
        tool_call_id=random_id("call"),
        deadline_monotonic=deadline(),
    )
    assert "First final stays in this Agent history." in str(received)
    assert "Continue critically." in str(received)
    maintain_history(repository)
    found = root_query.invoke(
        "search_session_content",
        {
            "target": {"kind": "SESSION", "session_id": first.agent_id},
            "query": "investigation",
        },
        current_range=SessionContentRange(0),
        tool_call_id=random_id("call"),
        deadline_monotonic=deadline(),
    )
    assert first.agent_id in str(found)
    assert "First investigation." in str(found)


def test_code_results_are_visible_to_ui_but_filtered_before_provider_budget_search_and_fork(
    stage2_migrated_postgres_database,
):
    from tests.test_agent_code_mode_execution import _canonical_code_calls
    from tests.test_conversation_fork import turn, fork, rows
    from pulsara_agent.conversation_kernel.session_content import SessionContentQuery
    from pulsara_agent.ports.session_content import SessionContentRange
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from pulsara_agent.identity import random_id

    marker = "privateuniquecodebody"
    repository, lease, _, _, _, code_entries = _canonical_code_calls(
        stage2_migrated_postgres_database,
        code_result_body=(marker + " physical").encode(),
        code_denial_body=(marker + " denial").encode(),
    )
    turn_id, _, final_id = turn(
        repository, lease.guard, "Continue after the script.", answer="Public final."
    )
    # The same canonical range also feeds compaction, so private CODE bodies are
    # excluded before hydration and before ordinary native closure pairing.
    cut = repository.prepare_compaction_input_cut(
        lease.guard, turn_id=turn_id, allow_terminal=True, deadline_monotonic=deadline()
    )
    reader = CanonicalProviderInputReader(
        repository.connection_provider,
        blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
    )
    dispatch = reader.read_frozen_dispatch(cut, deadline_monotonic=deadline())
    canonical = dispatch.compile_snapshot.canonical_input
    assert not set(code_entries) & {x.source_entry_id for x in canonical.items}
    assert all(
        marker not in part.text
        for item in canonical.items
        for part in item.content
        if isinstance(part, LLMTextPart)
    )
    preflight = reader.read_compaction_headroom_preflight(
        cut, deadline_monotonic=deadline()
    )
    assert preflight.selected_item_count >= len(canonical.items)
    from tests.test_round3_structured_model_input_compiler import _candidate
    from pulsara_agent.model_input.contracts import ContextSourceKind
    from pulsara_agent.conversation_kernel.context_sources import (
        _render_previous_turn_outcome,
    )

    sources = _sources(
        _candidate(
            ContextSourceKind.PREVIOUS_TURN_OUTCOME,
            _render_previous_turn_outcome(
                dispatch.compile_snapshot.previous_turn_outcome_fact
            ),
        )
    )
    compiled = StructuredModelInputCompiler().compile(
        _prepared_request(canonical, sources, canonical_facts=dispatch.compile_snapshot)
    )
    assert all(
        marker not in part.text
        for message in compiled.messages
        for part in message.content
        if isinstance(part, LLMTextPart)
    )
    assert not canonical.late_outcomes
    assert (
        len(canonical.closures) == 1
    )  # The actual native exec still needs its closure.
    query = SessionContentQuery(
        repository, session_id=lease.guard.session_id, memory_domain_id="u_local"
    )
    result = query.invoke(
        "read_session_content",
        {"include_tools": True},
        current_range=SessionContentRange(cut.provider_input_through_sequence),
        tool_call_id=random_id("call"),
        deadline_monotonic=deadline(),
    )
    assert marker not in str(result)
    from tests.support.history_projection import maintain_history

    maintain_history(repository)
    searched = query.invoke(
        "search_session_content",
        {"query": marker, "include_tools": True},
        current_range=SessionContentRange(cut.provider_input_through_sequence),
        tool_call_id=random_id("call"),
        deadline_monotonic=deadline(),
    )
    assert searched["items"] == []
    snapshot = CanonicalProtocolReader(repository.connection_provider).snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=20,
        maximum_control_items=20,
        deadline_monotonic=deadline(),
    )
    assert all(entry_id in str(snapshot) for entry_id in code_entries)
    child = fork(repository, lease.guard.session_id, final_id)
    fork_rows = rows(
        repository,
        "SELECT id,inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s",
        (child.child_session_id,),
    )
    assert all(
        marker.encode() not in bytes(row["inline_content"] or b"") for row in fork_rows
    )
    assert (
        len(fork_rows) == 4
    )  # Source user/exec, successor user/final, no CODE result rows.


def test_code_results_do_not_consume_canonical_or_compaction_budget(
    stage2_migrated_postgres_database,
):
    from tests.test_agent_code_mode_execution import _canonical_code_calls

    quotes = []
    for body in (b"private-code-small", b"private-code-" + b"x" * 32_000):
        repository, lease, turn_id, *_ = _canonical_code_calls(
            stage2_migrated_postgres_database,
            code_result_body=body,
            code_denial_body=body,
        )
        cut = repository.prepare_compaction_input_cut(
            lease.guard,
            turn_id=turn_id,
            allow_terminal=True,
            deadline_monotonic=deadline(),
        )
        reader = CanonicalProviderInputReader(
            repository.connection_provider,
            blob_reader=PostgresCanonicalBlobStore(repository.connection_provider),
            maximum_canonical_bytes=8192,
        )
        canonical = reader.read_frozen_dispatch(
            cut, deadline_monotonic=deadline()
        ).compile_snapshot.canonical_input
        assert canonical.canonical_expanded_bytes < 8192
        quote = reader.read_compaction_headroom_preflight(
            cut, deadline_monotonic=deadline()
        )
        quotes.append(
            (
                quote.selected_item_count,
                quote.selected_canonical_expanded_bytes,
                canonical.canonical_expanded_bytes,
            )
        )
    assert quotes[0] == quotes[1]


@pytest.mark.parametrize(
    "initial_mode", ["read-only", "ask-permissions", "bypass-permissions"]
)
def test_real_host_cold_permission_selection_and_retained_epoch_prefix(
    initial_mode, tmp_path, monkeypatch, stage2_migrated_postgres_database
):
    import asyncio
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent, text_part_values
    from pulsara_agent.model_input.lowering import decode_tool_result_observation
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from pulsara_agent.identity import random_id
    from tests.support.model_config import test_model_runtime, test_model_binding
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round2_terminal_host import _tool_call, _text
    import pulsara_agent.conversation_kernel.host as kernel_host

    operations = {
        "exec_cell",
        "query_cells",
        "manage_cell",
        "run_agent",
        "query_agents",
        "send_agent_message",
        "wait_agent",
        "stop_agent",
    }
    mode = PermissionMode(initial_mode)

    async def stream(request):
        index = len(model.requests)
        if index == 2:
            values = _tool_call("exec_cell", random_id("call"), {"code": "return 1;"})
        else:
            values = _text(f"final-{index}", f"FINAL-{index}")
        for value in values:
            yield value

    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_kw: ()
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario():
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            assert (
                await session.run_turn(
                    PromptContent.text("first turn"), requested_permission_mode=mode
                )
            ).final_text == "FINAL-1"
            first = model.requests[0].compiled_input
            first_names = {tool.name for tool in first.tools}
            assert operations.intersection(first_names) == (
                operations if mode is PermissionMode.BYPASS_PERMISSIONS else set()
            )
            if mode is PermissionMode.BYPASS_PERMISSIONS:
                result = await session.run_turn(
                    PromptContent.text("continue read only"),
                    requested_permission_mode=PermissionMode.READ_ONLY,
                )
                assert result.final_text == "FINAL-3"
                for request in model.requests[1:]:
                    later = request.compiled_input
                    assert later.system_prompt == first.system_prompt
                    assert later.tools == first.tools
                    assert later.messages[: len(first.messages)] == first.messages
                last = model.requests[-1].compiled_input
                tool_messages = [
                    m for m in last.messages if m.role is MessageRole.TOOL_RESULT
                ]
                assert len(tool_messages) == 1
                observation = decode_tool_result_observation(
                    text_part_values(tool_messages[0].content)[0]
                )
                assert observation["result_state"] == "PERMISSION_DENIED"
                assert session._code_mode.index() == ()
        finally:
            await core.shutdown()

    asyncio.run(scenario())
