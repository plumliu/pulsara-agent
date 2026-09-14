"""I43: adopted omission survives real SQL/cold/compaction/fork boundaries."""

from __future__ import annotations

import asyncio
from time import monotonic

import pytest
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.prompt_storage import (
    hydrate_canonical_prompt_owner,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    join_text_content,
)
from pulsara_agent.llm.model_connections import ModelConnectionId
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from tests.support.model_config import (
    frozen_test_prompt,
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort
from tests.test_conversation_fork import child_lease, final, fork, rows
from tests.test_kernel_image_input_k2_postgres import (
    _bound_session,
    _direct_intent,
    _image,
    _repository,
)
from tests.test_stage2_conversation_runner import (
    _AssertingTool,
    _CompactionSequencedDirectKernelModel,
    _name,
    _stable_id,
)


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("wire_api", ("openai_chat_completions", "openai_responses"))
def test_omitted_images_do_not_return_after_cold_compaction_and_two_forks(
    stage2_migrated_postgres_database,
    wire_api,
):
    repository = _repository(stage2_migrated_postgres_database)
    lease, source_runtime, _ = _bound_session(repository)
    source_session = lease.guard.session_id
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    image = _image()
    original = FrozenPromptContent(
        (LLMTextPart("before"), image, LLMTextPart("between"), image)
    )
    intent, _, _ = _direct_intent(repository, lease, source_runtime, original)
    original_cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    final(repository, lease.guard, intent.turn_id, "source answer")
    original_refs = rows(
        repository,
        "SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs WHERE session_id=%s AND transcript_entry_id=%s ORDER BY ref_ordinal",
        (source_session, intent.entry_id),
    )
    assert len(original_refs) == 2
    text_runtime = model_runtime(
        model_id="text-destination",
        wire_api=wire_api,
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        input_modalities=("text",),
    )
    vision_runtime = model_runtime(
        model_id="vision-return",
        wire_api=wire_api,
        connection_id=ModelConnectionId("model-connection:" + "3" * 32),
        input_modalities=("text", "image"),
    )
    owned = []

    def runner_for(active_lease, runtime, replies, summaries):
        repository.update_session_model_call_binding(
            active_lease.guard,
            binding=model_binding(runtime),
            deadline_monotonic=monotonic() + 30,
        )

        def native_text(reply):
            if wire_api == "openai_chat_completions":
                return (
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": reply},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )
            return (
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "id": _name("message"),
                                "status": "completed",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": reply}],
                            }
                        ],
                    },
                },
            )

        model = _CompactionSequencedDirectKernelModel(
            model_runtime=runtime,
            scripts=tuple(native_text(reply) for reply in replies),
            summary=summaries,
        )
        owner = HostCompactionRuntimeOwner(
            policy=ResolvedCompactionPolicy(
                automatic_enabled=False, minimum_reclaim_tokens=1
            )
        )
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=runtime.freeze_resolution_snapshot,
            repository=repository,
            writer_lease=active_lease,
            model=model,
            tools=StructuredToolPort(
                _AssertingTool(
                    repository.connection_provider, active_lease.guard.session_id
                ),
                tool_names=(),
            ),
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
            compaction_owner=owner,
            workspace_id=workspace_id,
        )
        owned.append(owner)
        return runner, model, owner

    def assert_no_images(model):
        contexts = [request.compiled_input for request in model.requests]
        contexts.extend(model.summary_transport.contexts)
        assert contexts
        assert not any(
            isinstance(part, LLMImagePart)
            for context in contexts
            for message in context.messages
            for part in message.content
        )

    def latest_final(session_id):
        return rows(
            repository,
            "SELECT final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at DESC LIMIT 1",
            (session_id,),
        )[0]["final_entry_id"]

    async def compact_next(runner, owner, label):
        command = _name("command")
        turn_id = _stable_id("turn", source_session, command)
        _, waiter = await owner.request_manual(
            command_id=_name("compact"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        await runner.run_turn(frozen_test_prompt(label), command_id=command)
        outcome = await waiter
        assert outcome.disposition.value == "COMPACTED", outcome

    async def exercise():
        try:
            # No installed A epoch: the real cold entry must adopt Tier 3 P.
            first, first_model, first_owner = runner_for(
                lease,
                text_runtime,
                ["old answer " + "x" * 20_000],
                [
                    "Historical user said before and between; [图片已省略]; [图片已省略]."
                ],
            )
            await first.run_turn(frozen_test_prompt("continue with text"))
            assert_no_images(first_model)
            assert len(first_model.summary_transport.contexts) == 1
            assert (
                sum(
                    join_text_content(message.content).count("[图片已省略]")
                    for message in first_model.summary_transport.contexts[0].messages
                )
                == 2
            )
            await first_owner.aclose()

            # Discard all live provider/compaction state and rebuild from SQL.
            cold, cold_model, cold_owner = runner_for(
                lease,
                text_runtime,
                [
                    "cold answer " + "y" * 20_000,
                    "another answer " + "z" * 20_000,
                    "after second compaction",
                ],
                [
                    "Omitted historical images remain ordinary text.",
                    "The images remain omitted; no image data is available.",
                ],
            )
            literal = "User literal marker [图片已省略] is ordinary text."
            await cold.run_turn(frozen_test_prompt(literal))
            assert any(
                message.content == (LLMTextPart(literal),)
                for message in cold_model.requests[0].compiled_input.messages
            )
            await compact_next(
                cold, cold_owner, "continue after first ordinary compaction"
            )
            await compact_next(
                cold, cold_owner, "continue after second ordinary compaction"
            )
            assert len(cold_model.summary_transport.contexts) == 2
            assert_no_images(cold_model)
            await cold_owner.aclose()

            # Returning to a visual target may rebuild roots; it must not reach
            # back into covered raw history to resurrect the old image refs.
            vision, vision_model, vision_owner = runner_for(
                lease, vision_runtime, ["visual target sees no old image"], []
            )
            await vision.run_turn(frozen_test_prompt("continue with the visual target"))
            assert_no_images(vision_model)
            assert vision_model.summary_transport.contexts == []
            await vision_owner.aclose()

            parent = source_session
            for index in range(2):
                created = fork(repository, parent, latest_final(parent))
                assert created.created, created.public_code
                child = created.child_session_id
                child_runner, child_model, child_owner = runner_for(
                    child_lease(repository, child),
                    vision_runtime,
                    [f"fork {index} has no old image"],
                    [],
                )
                await child_runner.run_turn(
                    frozen_test_prompt("continue inherited history")
                )
                assert_no_images(child_model)
                assert child_model.summary_transport.contexts == []
                await child_owner.aclose()
                parent = child

            # Effective omission never rewrites the original accepted command.
            confirmation = repository.confirm_root_turn_intent(
                intent=intent, guard=lease.guard, deadline_monotonic=monotonic() + 30
            )
            assert confirmation.kind.value == "FULL"
            assert (
                rows(
                    repository,
                    "SELECT ref_ordinal, blob_id FROM pulsara_v3.canonical_image_refs WHERE session_id=%s AND transcript_entry_id=%s ORDER BY ref_ordinal",
                    (source_session, intent.entry_id),
                )
                == original_refs
            )
            # The old provider cut is correctly stale after adoption. Original
            # user-visible content is read through its immutable entry owner.
            with pytest.raises(
                ConversationKernelConflict, match="provider binding revision is stale"
            ):
                CanonicalProviderInputReader(
                    repository.connection_provider
                ).read_frozen_snapshot(
                    original_cut, deadline_monotonic=monotonic() + 30
                )
            with repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                row_factory=dict_row,
                isolation_level=IsolationLevel.REPEATABLE_READ,
                deadline_monotonic=monotonic() + 30,
            ) as connection:
                entry = connection.execute(
                    "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
                    (source_session, intent.entry_id),
                ).fetchone()
                assert entry is not None
                source = hydrate_canonical_prompt_owner(
                    connection, row=entry, transcript_entry_id=intent.entry_id
                )
                assert source.content == original
        finally:
            for owner in owned:
                await owner.aclose()

    asyncio.run(exercise())
