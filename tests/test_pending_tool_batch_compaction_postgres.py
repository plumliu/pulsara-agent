"""Prepare/dry-quote first; accept real results before compaction adoption."""

import asyncio
from dataclasses import replace
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.images import bound_test_image
from tests.support.model_config import (
    frozen_test_prompt,
    test_model_resolution_snapshot,
    test_model_runtime,
)
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort
from tests.test_stage2_conversation_runner import (
    _AssertingTool,
    _CompactionSequencedDirectKernelModel,
    _acquire_bound_host_writer,
    verified_postgres_provider,
)


class _InspectedSuccessor(BaseException):
    pass


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("adopt", "cancel_after_full"),
    ((True, False), (False, False), (True, True)),
    ids=("adopt-complete-batch", "discard-before-adoption", "cancel-after-full"),
)
def test_pending_image_prefix_preparation_allows_actual_batch_append_before_adoption(
    stage2_migrated_postgres_database,
    adopt,
    cancel_after_full,
):
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, workspace_id = random_id("session"), random_id("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=random_id("host"),
        lease_seconds=120,
        deadline_monotonic=monotonic() + 30,
    )

    def response(content, calls=None):
        return (
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": content,
                            **({"tool_calls": calls} if calls else {}),
                        },
                        "finish_reason": "tool_calls" if calls else "stop",
                    }
                ]
            },
        )

    calls = [
        {
            "index": index,
            "id": f"pending-call-{index}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": '{"target":{"kind":"PATH","path":"/test/image.png"}}',
            },
        }
        for index, name in enumerate(("read_video_frames", "read_file"))
    ]
    model = _CompactionSequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            wire_api="openai_chat_completions", input_modalities=("text", "image")
        ),
        scripts=(
            response("historical detail " * 1600),
            response("inspect both", calls),
        ),
        summary="Earlier work established the historical context.",
    )
    image = bind_prompt_images(
        FrozenPromptContent(
            (
                LLMTextPart(
                    '{"video_frames":true,"frames":[{"n":1,"image":1,"t":12.5}],"next":null}'
                ),
                replace(
                    bound_test_image("image/png", b"pending-real-page", 2, 2),
                    image_ref=None,
                ),
            )
        ),
        workspace_id=workspace_id,
    )

    class Tool(_AssertingTool):
        async def invoke(self, **kwargs):
            await super().invoke(**kwargs)
            if kwargs["tool_name"] == "read_file":
                with provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as connection:
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id=%s",
                            (session_id,),
                        ).fetchone()[0]
                        == 1
                    )
                repository.require_pending_tool_batch_compaction_safe(
                    lease.guard,
                    **{
                        **observed["guard_arguments"],
                        "deadline_monotonic": monotonic() + 30,
                    },
                )
                observed["partial_batch_guard"] = True
            return (
                KernelToolResult(state="SUCCESS", content=image)
                if kwargs["tool_name"] == "read_video_frames"
                else KernelToolResult(
                    state="SUCCESS", content=b"associated source text"
                )
            )

    tool = Tool(provider, session_id)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool, tool_names=("read_video_frames", "read_file")),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    original_execute = runner._tool_batches.execute
    observed = {}

    async def inspect_batch(**kwargs):
        request = kwargs["request"]
        arguments = dict(
            closed_source_cut=request.cut,
            assistant_entry_id=kwargs["assistant_entry_id"],
            ordered_tool_call_ids=tuple(call.tool_call_id for call in kwargs["calls"]),
            deadline_monotonic=monotonic() + 30,
        )
        observed["guard_arguments"] = arguments
        repository.require_pending_tool_batch_compaction_safe(lease.guard, **arguments)
        with pytest.raises(ConversationKernelConflict):
            repository.require_pending_tool_batch_compaction_safe(
                lease.guard,
                **{
                    **arguments,
                    "ordered_tool_call_ids": arguments["ordered_tool_call_ids"][::-1],
                },
            )
        pending = await runner.compaction.prepare_pending_tool_batch(
            turn_id=kwargs["turn_id"],
            model_call_index=request.model_call_index + 1,
            inherited_memory_use_policy=runner._root_memory_use_policy,
            **{
                key: value
                for key, value in arguments.items()
                if key != "deadline_monotonic"
            },
        )
        assert pending is not None
        observed["prefix"] = pending.compiled_input.messages
        assert not any(
            part is image.parts[-1]
            for message in pending.compiled_input.messages
            for part in message.content
        )
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
        ) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                    (session_id,),
                ).fetchone()[0]
                == 0
            )
        try:
            if not adopt:
                pending.close()
            batch = await original_execute(**kwargs)
            assert batch.tool_call_count == 2
            # The pending closed-cut handle permits actual result acceptance;
            # the exact batch guard accepts the advanced, complete suffix.
            repository.require_pending_tool_batch_compaction_safe(
                lease.guard,
                **{
                    **arguments,
                    "deadline_monotonic": monotonic() + 30,
                },
            )
            actual_cut = repository.prepare_provider_input_cut(
                lease.guard,
                turn_id=kwargs["turn_id"],
                deadline_monotonic=monotonic() + 30,
            )
            actual_read = runner.compaction._input_reader.read_frozen_compaction_cut(
                actual_cut, deadline_monotonic=monotonic() + 30
            )
            assert (
                actual_read.latest_assistant_input_through_sequence
                == request.cut.provider_input_through_sequence
            )
            if not adopt:
                with provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 30,
                ) as connection:
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                            (session_id,),
                        ).fetchone()[0]
                        == 0
                    )
                handle = runner._safe_point.freeze_provider_input(
                    turn_id=kwargs["turn_id"], deadline_monotonic=monotonic() + 30
                )
                handle.close()
                raise _InspectedSuccessor
            if cancel_after_full:
                original_fenced = owner.run_fenced

                async def cancel_fence_return(**kwargs):
                    await original_fenced(**kwargs)
                    raise asyncio.CancelledError

                owner.run_fenced = cancel_fence_return
                try:
                    with pytest.raises(asyncio.CancelledError):
                        await pending.finalize()
                finally:
                    owner.run_fenced = original_fenced
                assert runner._safe_point._active_handle is None
                assert not runner._tools._active
                observed["cancelled_after_full"] = True
                raise _InspectedSuccessor
            outcome = await pending.finalize()
            assert outcome.outcome.disposition is CompactionDisposition.COMPACTED
            successor = outcome.successor_dispatch
            assert successor is not None
            messages = successor.append_result.compiled_input.messages
            pictures = tuple(
                part
                for message in messages
                for part in message.content
                if isinstance(part, LLMImagePart)
            )
            assert len(pictures) == 1
            assert pictures[0].image_ref == image.parts[-1].image_ref
            text = "\n".join(
                part.text
                for message in messages
                for part in message.content
                if isinstance(part, LLMTextPart)
            )
            assert (
                "12.5" in text
                and "pending-call-0" in text
                and "associated source text" in text
            )
            observed["successor"] = messages
            successor.close(error=_InspectedSuccessor())
        finally:
            pending.close()
        raise _InspectedSuccessor

    async def exercise():
        await runner.run_turn(frozen_test_prompt("retain earlier work"))
        owner.policy = replace(
            owner.policy, automatic_enabled=True, auto_trigger_ratio=0.99
        )
        runner._tool_batches.execute = inspect_batch
        with pytest.raises(_InspectedSuccessor):
            await runner.run_turn(
                frozen_test_prompt("inspect the image and its source text")
            )
        await owner.aclose()

    asyncio.run(exercise())
    assert observed["partial_batch_guard"]
    assert bool(observed.get("successor")) is (adopt and not cancel_after_full)
    assert bool(observed.get("cancelled_after_full")) is cancel_after_full
    assert len(tool.invocations) == 2
    assert len(model.summary_transport.contexts) == 1
