"""Child guidance stays queued until the next ordinary provider preparation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager
from pulsara_agent.conversation_kernel.subagents.launch import (
    CanonicalSubagentLaunchPreparationPort,
)
from pulsara_agent.conversation_kernel.subagents.model_target import (
    FrozenSubagentModelTarget,
)
from pulsara_agent.conversation_kernel.todo_runtime import TodoRunStateOwner
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import test_model_resolution_snapshot
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort
from tests.test_round10_hierarchical_subagent_orchestration import (
    _id,
    _prepare_root_tool_batch,
    _round10_id,
)
from tests.test_stage2_conversation_runner import (
    _AssertingTool,
    _CompactionScriptedModel,
    _text_stream,
    _tool_stream,
    _named_tool_stream,
    join_text_content,
)


@pytest.mark.postgres
def test_child_mailbox_waits_for_ordinary_prepare_after_compaction(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, workspace_id, spawn_attempt = (
        _id("session"),
        _id("workspace"),
        _id("attempt"),
    )
    task_id = _round10_id("task", spawn_attempt, "0")
    guidance = (
        "FIRST_GUIDANCE: recheck the source",
        "SECOND_GUIDANCE: report the correction",
    )
    calls = (
        (
            "spawn_agent",
            _id("call"),
            spawn_attempt,
            {"task": "Inspect and report the result"},
        ),
        *(
            (
                "send_agent_message",
                _id("call"),
                _id("attempt"),
                {"task_id": task_id, "message": body},
            )
            for body in guidance
        ),
    )
    lease, contexts = _prepare_root_tool_batch(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        calls=calls,
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    model = _CompactionScriptedModel(
        [
            [*_text_stream("working notes " + "x" * 80_000), *_tool_stream()],
            _named_tool_stream(
                tool_name="terminal",
                tool_call_id="call:second",
                arguments={"command": "true"},
            ),
            _text_stream("Answer from the frozen successor"),
            _text_stream("Corrected answer after both instructions"),
        ],
        "The worker inspected the source and must finish its report.",
    )

    async def exercise():
        def parent_target(_subject, binding):
            target = model.model_runtime.resolve_target(
                binding, timeout_policy=model.transport_timeout_policy
            )
            return FrozenSubagentModelTarget.freeze(
                target.fact, target.contract.reasoning
            )

        manager = KernelSubagentManager(
            launch_preparation=CanonicalSubagentLaunchPreparationPort(
                inherit_parent_target=parent_target,
                repository=repository,
                guard=lease.guard,
                io_owner=KernelSessionIO(),
                model_runtime=model.model_runtime,
                deadline_factory=KernelExecutionDeadlineFactory(),
            ),
            hook_workspace_root=Path.cwd,
            repository=repository,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(
                session_id=session_id, owner_epoch="todo:mailbox-compaction"
            ),
        )
        compacted = asyncio.Event()
        guidance_sent = asyncio.Event()
        captured_successors = []
        build_initial_seed = manager.build_initial_seed
        initial_seed_preparations = []

        def observe_initial_seed(**kwargs):
            initial_seed_preparations.append((kwargs["task_id"], len(model.requests)))
            return build_initial_seed(**kwargs)

        monkeypatch.setattr(manager, "build_initial_seed", observe_initial_seed)

        class RequestCompactionTool(_AssertingTool):
            async def invoke(self, **kwargs):
                result = await super().invoke(**kwargs)
                if len(self.invocations) == 2:
                    await owner.request_manual(
                        command_id=_id("command"),
                        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                        scope_subagent_task_id=task_id,
                        expected_turn_id=kwargs["turn_id"],
                        force=True,
                    )
                return result

        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=StructuredToolPort(RequestCompactionTool(provider, session_id)),
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
            compaction_owner=owner,
            subagent_runtime=manager,
            workspace_id=workspace_id,
        )
        execute_active = runner.compaction.execute_active
        run_child = runner.run_admitted_subagent_turn
        child_failures = []
        consume_mailbox = manager.consume_mailbox_safe_point
        pending_after_first_answer = []

        async def observe_ordinary_mailbox_boundary(target_task_id):
            if len(model.requests) == 3:
                # The frozen successor's answer has settled, but the queued
                # guidance must keep both task and turn open until this point.
                assert [item.message for item in manager._mailboxes[task_id]] == list(
                    guidance
                )
                with provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR,
                    deadline_monotonic=monotonic() + 10,
                ) as connection:
                    state = connection.execute(
                        "SELECT task.status, turn.status, turn.final_entry_id, entry.entry_kind "
                        "FROM pulsara_v3.subagent_tasks AS task "
                        "JOIN pulsara_v3.turns AS turn ON turn.session_id = task.session_id "
                        "AND turn.scope_subagent_task_id = task.id "
                        "JOIN pulsara_v3.transcript_entries AS entry ON entry.session_id = turn.session_id "
                        "AND entry.turn_id = turn.id "
                        "WHERE task.session_id = %s AND task.id = %s "
                        "ORDER BY entry.entry_sequence DESC LIMIT 1",
                        (session_id, task_id),
                    ).fetchone()
                assert state == ("ACTIVE", "RUNNING", None, "ASSISTANT_MESSAGE")
                pending_after_first_answer.append(state)
            return await consume_mailbox(target_task_id)

        monkeypatch.setattr(
            manager, "consume_mailbox_safe_point", observe_ordinary_mailbox_boundary
        )

        async def observe_child_failure(**kwargs):
            try:
                return await run_child(**kwargs)
            except BaseException as exc:
                child_failures.append(exc)
                compacted.set()
                raise

        monkeypatch.setattr(runner, "run_admitted_subagent_turn", observe_child_failure)

        async def pause_after_successor_install(**kwargs):
            execution = await execute_active(**kwargs)
            assert execution.successor_dispatch is not None, execution.outcome
            installed = execution.successor_dispatch.installed_provider_open
            assert installed is not None
            captured_successors.append(installed.request)
            compacted.set()
            await guidance_sent.wait()
            return execution

        monkeypatch.setattr(
            runner.compaction, "execute_active", pause_after_successor_install
        )
        manager.bind_runner_factory(lambda _scope: runner)
        try:
            spawned = await manager.invoke(
                tool_name="spawn_agent",
                arguments=calls[0][3],
                invocation_context=contexts[0],
            )
            assert json.loads(spawned.content)["task_id"] == task_id
            await asyncio.wait_for(compacted.wait(), timeout=30)
            if child_failures:
                raise child_failures[0]
            child = manager._tasks[task_id].task
            for call, context in zip(calls[1:], contexts[1:], strict=True):
                sent = await manager.invoke(
                    tool_name="send_agent_message",
                    arguments=call[3],
                    invocation_context=context,
                )
                assert json.loads(sent.content)["status"] == "queued"
            guidance_sent.set()
            result = await asyncio.wait_for(asyncio.shield(child), timeout=30)
            assert result.final_text == "Corrected answer after both instructions"
            assert len(model.requests) == 4
            assert len(captured_successors) == 1
            # Cold headroom projection may prepare a seed before the actual
            # first request; no installed epoch may request the initial seed.
            assert initial_seed_preparations
            assert set(initial_seed_preparations) == {(task_id, 0)}
            assert len(pending_after_first_answer) == 1
            assert model.requests[2] is captured_successors[0]
            frozen, following = (
                request.compiled_input for request in model.requests[2:]
            )
            assert following.system_prompt == frozen.system_prompt
            assert following.tools == frozen.tools
            assert following.messages[: len(frozen.messages)] == frozen.messages
            frozen_text = "\n".join(
                join_text_content(message.content) for message in frozen.messages
            )
            suffix_text = "\n".join(
                join_text_content(message.content)
                for message in following.messages[len(frozen.messages) :]
            )
            assert all(body not in frozen_text for body in guidance)
            assert all(suffix_text.count(body) == 1 for body in guidance)
            assert (
                suffix_text.index("Answer from the frozen successor")
                < suffix_text.index(guidance[0])
                < suffix_text.index(guidance[1])
            )
            assert not manager._mailboxes.get(task_id)
            with provider.connection(
                lane=PostgresConnectionLane.INSPECTOR,
                deadline_monotonic=monotonic() + 10,
            ) as connection:
                entries = connection.execute(
                    "SELECT entry_kind, source_inter_agent_tool_attempt_id FROM pulsara_v3.transcript_entries "
                    "WHERE session_id = %s AND scope_subagent_task_id = %s ORDER BY entry_sequence",
                    (session_id, task_id),
                ).fetchall()
                assert [
                    attempt
                    for kind, attempt in entries
                    if kind == "INTER_AGENT_MESSAGE"
                ] == [call[2] for call in calls[1:]]
                assert [kind for kind, _ in entries][-4:] == [
                    "ASSISTANT_MESSAGE",
                    "INTER_AGENT_MESSAGE",
                    "INTER_AGENT_MESSAGE",
                    "ASSISTANT_MESSAGE",
                ]
                assert connection.execute(
                    "SELECT status FROM pulsara_v3.subagent_tasks WHERE session_id = %s AND id = %s",
                    (session_id, task_id),
                ).fetchone() == ("COMPLETED",)
        finally:
            guidance_sent.set()
            await manager.aclose(deadline_monotonic=monotonic() + 3)
            await owner.aclose()

    asyncio.run(exercise())
