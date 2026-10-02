"""Real relational branch isolation, inherited snapshot and fixed late-result cuts."""

import asyncio
import json
from datetime import datetime, timezone
from time import monotonic

import pytest

from tests.test_round10_hierarchical_subagent_orchestration import (
    _id,
    _round10_id,
    _prepare_root_tool_batch,
    _manager_launch_kwargs,
    _CompletingChildRunner,
    _permission_fingerprint,
)
from tests.test_conversation_fork import compact, rows
from tests.support.postgres import verified_postgres_provider
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelRepository,
    AssistantToolCallBlock,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.subagent import KernelSubagentManager
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.todo_runtime import TodoRunStateOwner
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItemKind,
    CanonicalInputOriginKind,
    ContextBindingBaseKind,
    provider_input_item_text,
)
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.primitives.context import freeze_json


@pytest.mark.postgres
@pytest.mark.parametrize("compact_source", [False, True])
def test_sibling_branches_and_grandchild_use_one_effective_history(
    stage2_migrated_postgres_database, compact_source
):
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repo = ConversationKernelRepository(provider)

    async def exercise():
        session = _id("session")
        attempts = [_id("attempt") for _ in range(4)]
        ids = [_round10_id("subagent-task", a, "0") for a in attempts]
        args = [
            dict(task="objective-A", task_name="branch-context-A"),
            dict(
                task="objective-B", context=dict(mode="worker_history", task_id=ids[0])
            ),
            dict(
                task="objective-C", context=dict(mode="worker_history", task_id=ids[0])
            ),
            dict(
                task="objective-D", context=dict(mode="worker_history", task_id=ids[1])
            ),
        ]
        lease, contexts = _prepare_root_tool_batch(
            repo,
            session_id=session,
            workspace_id=_id("workspace"),
            calls=tuple(
                ("spawn_agent", _id("call"), a, arg)
                for a, arg in zip(attempts, args, strict=True)
            ),
        )
        manager = KernelSubagentManager(
            **_manager_launch_kwargs(repo, lease.guard),
            repository=repo,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(session_id=session, owner_epoch=_id("todo")),
        )
        sources, started = {}, []

        class Worker(_CompletingChildRunner):
            async def run_admitted_subagent_turn(self, *, launch, cancellation_intent):
                if compact_source and launch.task_start.objective == "objective-A":
                    initial = rows(
                        repo,
                        "SELECT e.entry_sequence FROM pulsara_v3.turns t JOIN pulsara_v3.transcript_entries e ON e.id=t.initial_entry_id WHERE t.id=%s",
                        (launch.child_turn_id,),
                    )[0]
                    compact(
                        repo,
                        lease.guard,
                        launch.child_turn_id,
                        initial["entry_sequence"],
                        summary="A summary",
                    )
                return await super().run_admitted_subagent_turn(
                    launch=launch, cancellation_intent=cancellation_intent
                )

        manager.bind_runner_factory(
            lambda scope: Worker(
                repository=repo,
                lease=lease,
                manager=manager,
                started=started,
                source_bodies=sources,
            )
        )
        await manager.open_root_completion_delivery(contexts[0].turn_id)
        reader = CanonicalProviderInputReader(provider)
        try:
            for i, (arg, ctx) in enumerate(zip(args, contexts, strict=True)):
                created = await manager.invoke(
                    tool_name="spawn_agent", arguments=arg, invocation_context=ctx
                )
                assert created.state == "SUCCESS", created.content
                waited = await manager.invoke(
                    tool_name="wait_agent",
                    arguments=dict(task_ids=[ids[i]], settle="all", timeout_seconds=10),
                    invocation_context=ctx,
                )
                assert json.loads(waited.content)["pending_task_ids"] == []
                turn = rows(
                    repo,
                    "SELECT * FROM pulsara_v3.turns WHERE session_id=%s AND scope_subagent_task_id=%s",
                    (session, ids[i]),
                )[0]
                cut = repo.prepare_compaction_input_cut(
                    lease.guard,
                    turn_id=turn["id"],
                    allow_terminal=True,
                    deadline_monotonic=monotonic() + 30,
                )
                read = reader.read_frozen_compaction_cut(
                    cut, deadline_monotonic=monotonic() + 30
                )
                quote = reader.read_compaction_headroom_preflight(
                    cut, deadline_monotonic=monotonic() + 30
                )
                assert (
                    quote.effective_materialization_lineage_floor
                    == read.lineage_base.effective_materialization_lineage_floor
                )
                assert (
                    quote.selected_canonical_expanded_bytes
                    >= read.dispatch_read.compile_snapshot.canonical_input.canonical_expanded_bytes
                )
                if i == 1 and compact_source:
                    assert read.lineage_base.persisted_context_snapshot_id is None
                    assert (
                        read.dispatch_read.compile_snapshot.context_binding_fact.base_kind
                        is ContextBindingBaseKind.SNAPSHOT
                    )
                    snapshot = read.snapshot_carrier
                    assert snapshot.active_request is None
                    assert [
                        r.input_origin for r in snapshot.retained_historical_requests
                    ] == [CanonicalInputOriginKind.SUBAGENT_OBJECTIVE]
                    assert (
                        snapshot.retained_historical_requests[0].content.parts[0].text
                        == "objective-A"
                    )
                    # Adoption compares B's raw genesis, while summarizing inherited A + B.
                    compact(
                        repo,
                        lease.guard,
                        turn["id"],
                        cut.provider_input_through_sequence,
                        summary="A and B summary",
                        idle=True,
                    )
                    after_cut = repo.prepare_compaction_input_cut(
                        lease.guard,
                        turn_id=turn["id"],
                        allow_terminal=True,
                        deadline_monotonic=monotonic() + 30,
                    )
                    after = reader.read_frozen_compaction_cut(
                        after_cut, deadline_monotonic=monotonic() + 30
                    )
                    assert (
                        after.snapshot_carrier.earlier_context_summary
                        == "A and B summary"
                    )
                    from pulsara_agent.conversation_kernel.compaction.planner import (
                        build_synthetic_compaction_dispatch_read,
                    )

                    snapshot_row = rows(
                        repo,
                        "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s AND id=%s",
                        (session, after.lineage_base.snapshot_id),
                    )[0]
                    dry = build_synthetic_compaction_dispatch_read(
                        canonical_read=read,
                        source_through_sequence=cut.provider_input_through_sequence,
                        snapshot_id=snapshot_row["id"],
                        binding_revision_id=after_cut.context_binding_revision_id,
                        binding_revision_ordinal=after.lineage_base.binding_revision_ordinal,
                        snapshot_carrier=after.snapshot_carrier,
                        snapshot_content_digest=snapshot_row["content_digest"],
                        snapshot_content_size=snapshot_row["content_size"],
                        snapshot_content_media_type=snapshot_row["content_media_type"],
                        snapshot_content_codec=snapshot_row["content_codec"],
                        snapshot_blob_id=snapshot_row["blob_id"],
                    )
                    assert dry == after.dispatch_read
                    assert (
                        sum(
                            x.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
                            for x in after.dispatch_read.compile_snapshot.canonical_input.items
                        )
                        == 1
                    )
                if i == 2:
                    texts = "\n".join(sources["objective-C"])
                    assert "objective-A" in texts
                    assert "objective-B" not in texts
                if i == 3:
                    texts = "\n".join(sources["objective-D"])
                    assert (
                        ("A and B summary" in texts)
                        if compact_source
                        else ("objective-A" in texts and "objective-B" in texts)
                    )
                    assert "objective-C" not in texts
                    objectives = [
                        provider_input_item_text(x)
                        for x in read.dispatch_read.compile_snapshot.canonical_input.items
                        if x.input_origin is CanonicalInputOriginKind.SUBAGENT_OBJECTIVE
                    ]
                    assert objectives == (
                        ["objective-D"]
                        if compact_source
                        else ["objective-A", "objective-B", "objective-D"]
                    )
            # The inspector shares the fixed effective source, but keeps its own dialogue.
            page_rows, after_sequence = [], 0
            while True:
                entries, _, _, more, context = repo.list_subagent_task_activities(
                    session_id=session, task_id=ids[3], maximum_items=2,
                    after_entry_sequence=after_sequence, deadline_monotonic=monotonic()+30)
                page_rows.extend(entries)
                if not more:
                    break
                after_sequence = entries[-1]["entry_sequence"]
            assert context is not None
            assert not any(row["scope_subagent_task_id"] == ids[2] for row in page_rows)
            assert any(not row["inherited"] and row["scope_subagent_task_id"] == ids[3] for row in page_rows)
            if compact_source:
                assert any(material["body"] == "A and B summary" for material in context["materials"])
                assert not any(row["inherited"] for row in page_rows)
            else:
                assert {row["scope_subagent_task_id"] for row in page_rows if row["inherited"]} == {ids[0], ids[1]}
            assert started == [
                "objective-A",
                "objective-B",
                "objective-C",
                "objective-D",
            ]
            assert all(
                row["status"] == "COMPLETED"
                for row in rows(
                    repo,
                    "SELECT status FROM pulsara_v3.subagent_tasks WHERE session_id=%s",
                    (session,),
                )
            )
        finally:
            await manager.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_accepted_branch_cut_ignores_source_tool_result_committed_later(
    stage2_migrated_postgres_database,
):
    from tests.test_round10_hierarchical_subagent_orchestration import (
        _BlockingLaunchPreparation,
    )
    from pulsara_agent.conversation_kernel.repository import (
        build_prepared_tool_result_acceptance,
    )
    from pulsara_agent.ports.artifact import (
        ToolOutputArtifactDisposition,
        ToolResultDisplayKind,
    )
    from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from pulsara_agent.model_input.contracts import StructuredModelInputLimits

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repo = ConversationKernelRepository(provider)

    async def exercise():
        session, workspace = _id("session"), _id("workspace")
        attempts = [_id("attempt") for _ in range(3)]
        ids = [_round10_id("subagent-task", a, "0") for a in attempts]
        args = [
            dict(task="tool-source-A"),
            dict(
                task="followup-B", context=dict(mode="worker_history", task_id=ids[0])
            ),
            dict(
                task="followup-C", context=dict(mode="worker_history", task_id=ids[1])
            ),
        ]
        lease, contexts = _prepare_root_tool_batch(
            repo,
            session_id=session,
            workspace_id=workspace,
            calls=tuple(
                ("spawn_agent", _id("call"), a, arg)
                for a, arg in zip(attempts, args, strict=True)
            ),
        )
        kwargs = _manager_launch_kwargs(repo, lease.guard)
        launch_gate = _BlockingLaunchPreparation(kwargs["launch_preparation"])
        launch_gate.release.set()
        kwargs["launch_preparation"] = launch_gate
        manager = KernelSubagentManager(
            **kwargs,
            repository=repo,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(session_id=session, owner_epoch=_id("todo")),
        )
        sources, started, pending = {}, [], {}

        class Worker(_CompletingChildRunner):
            async def run_admitted_subagent_turn(self, *, launch, cancellation_intent):
                if launch.task_start.objective == "tool-source-A":
                    turn = launch.child_turn_id
                    cut = repo.prepare_provider_input_cut(
                        lease.guard, turn_id=turn, deadline_monotonic=monotonic() + 30
                    )
                    entry, call, attempt = _id("entry"), _id("call"), _id("attempt")
                    repo.commit_assistant_message(
                        lease.guard,
                        cut=cut,
                        entry_id=entry,
                        parent_content=InlineContent.from_bytes(b"tool requested"),
                        blocks=(
                            AssistantToolCallBlock(
                                _id("block"),
                                call,
                                "terminal",
                                freeze_json({"command": "echo historical"}),
                            ),
                        ),
                        occurred_at=datetime.now(timezone.utc),
                        actor_id=ids[0],
                        deadline_monotonic=monotonic() + 30,
                    )
                    repo.accept_tool_attempt(
                        lease.guard,
                        attempt_id=attempt,
                        assistant_entry_id=entry,
                        tool_call_id=call,
                        authorization_kind="policy",
                        authorization_reference="allow",
                        actor_kind="runtime",
                        actor_id="executor",
                        remote_idempotency_key=None,
                        retry_of_attempt_id=None,
                        permission_snapshot_fingerprint=_permission_fingerprint(
                            repo, session_id=session, turn_id=turn
                        ),
                        occurred_at=datetime.now(timezone.utc),
                        deadline_monotonic=monotonic() + 30,
                    )
                    pending.update(turn=turn, entry=entry, call=call, attempt=attempt)
                return await super().run_admitted_subagent_turn(
                    launch=launch, cancellation_intent=cancellation_intent
                )

        manager.bind_runner_factory(
            lambda scope: Worker(
                repository=repo,
                lease=lease,
                manager=manager,
                started=started,
                source_bodies=sources,
            )
        )
        reader = CanonicalProviderInputReader(provider)
        try:
            await manager.open_root_completion_delivery(contexts[0].turn_id)
            first = await manager.invoke(
                tool_name="spawn_agent",
                arguments=args[0],
                invocation_context=contexts[0],
            )
            assert first.state == "SUCCESS"
            await manager.invoke(
                tool_name="wait_agent",
                arguments=dict(task_ids=[ids[0]], settle="all", timeout_seconds=10),
                invocation_context=contexts[0],
            )
            source_cut = repo.prepare_compaction_input_cut(
                lease.guard,
                turn_id=pending["turn"],
                allow_terminal=True,
                deadline_monotonic=monotonic() + 30,
            )
            before = reader.read_frozen_dispatch(
                source_cut, deadline_monotonic=monotonic() + 30
            ).compile_snapshot.canonical_input
            launch_gate.release.clear()
            launch_gate.entered.clear()
            creating = asyncio.create_task(
                manager.invoke(
                    tool_name="spawn_agent",
                    arguments=args[1],
                    invocation_context=contexts[1],
                )
            )
            await asyncio.wait_for(launch_gate.entered.wait(), 5)
            accepted = rows(
                repo,
                "SELECT history_cut_sequence FROM pulsara_v3.subagent_tasks WHERE session_id=%s AND id=%s",
                (session, ids[1]),
            )[0]
            assert (
                accepted["history_cut_sequence"]
                == source_cut.provider_input_through_sequence
            )
            candidate = build_prepared_tool_result_acceptance(
                guard=lease.guard,
                workspace_id=workspace,
                result_id=_id("result"),
                result_entry_id=_id("entry"),
                turn_id=pending["turn"],
                assistant_entry_id=pending["entry"],
                tool_call_id=pending["call"],
                attempt_id=pending["attempt"],
                result_state="SUCCESS",
                canonical_preview_content=InlineContent.from_bytes(b"AFTER_BRANCH_CUT"),
                artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
                artifact_id=None,
                artifact_blob_descriptor=None,
                source_coverage=ToolOutputSourceCoverage.COMPLETE,
                display_kind=ToolResultDisplayKind.COMPLETE,
                source_coverage_reason=None,
                artifact_unavailability_reason=None,
                observed_at=datetime.now(timezone.utc),
                observation_duration_microseconds=1000,
                observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
                trusted_tool_reported_duration_microseconds=None,
                actor_id="terminal",
            )
            repo.accept_tool_result(
                lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
            )
            launch_gate.release.set()
            second = await creating
            assert second.state == "SUCCESS"
            waited = await manager.invoke(
                tool_name="wait_agent",
                arguments=dict(task_ids=[ids[1]], settle="all", timeout_seconds=10),
                invocation_context=contexts[1],
            )
            assert json.loads(waited.content)["pending_task_ids"] == []
            after = reader.read_frozen_dispatch(
                source_cut, deadline_monotonic=monotonic() + 30
            ).compile_snapshot.canonical_input
            assert after.items == before.items and after.closures == before.closures
            turn = rows(
                repo,
                "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND scope_subagent_task_id=%s",
                (session, ids[1]),
            )[0]["id"]
            child_cut = repo.prepare_compaction_input_cut(
                lease.guard,
                turn_id=turn,
                allow_terminal=True,
                deadline_monotonic=monotonic() + 30,
            )
            child = reader.read_frozen_dispatch(
                child_cut, deadline_monotonic=monotonic() + 30
            ).compile_snapshot.canonical_input
            assert child.items[: len(before.items)] == before.items
            assert child.closures == before.closures
            assert not any(
                "AFTER_BRANCH_CUT" in provider_input_item_text(i) for i in child.items
            )
            messages = [
                lower_canonical_item(
                    i,
                    artifact_read_available=False,
                    limits=StructuredModelInputLimits(),
                ).fixed_message
                for i in child.items
            ]
            assert any(m is not None and m.tool_calls for m in messages)
            # Partial compaction must preserve the protected ancestor tool tail.
            from pulsara_agent.conversation_kernel.compaction.planner import (
                build_synthetic_compaction_dispatch_read,
            )

            before_compact = reader.read_frozen_compaction_cut(
                child_cut, deadline_monotonic=monotonic() + 30
            )
            boundary = before.items[0].source_entry_sequence
            carrier = compact(
                repo, lease.guard, turn, boundary, summary="A prefix only", idle=True
            )
            after_cut = repo.prepare_compaction_input_cut(
                lease.guard,
                turn_id=turn,
                allow_terminal=True,
                deadline_monotonic=monotonic() + 30,
            )
            after_compact = reader.read_frozen_compaction_cut(
                after_cut, deadline_monotonic=monotonic() + 30
            )
            snapshot_row = rows(
                repo,
                "SELECT * FROM pulsara_v3.context_snapshots WHERE session_id=%s AND id=%s",
                (session, after_compact.lineage_base.snapshot_id),
            )[0]
            dry = build_synthetic_compaction_dispatch_read(
                canonical_read=before_compact,
                source_through_sequence=boundary,
                snapshot_id=snapshot_row["id"],
                binding_revision_id=after_cut.context_binding_revision_id,
                binding_revision_ordinal=after_compact.lineage_base.binding_revision_ordinal,
                snapshot_carrier=carrier,
                snapshot_content_digest=snapshot_row["content_digest"],
                snapshot_content_size=snapshot_row["content_size"],
                snapshot_content_media_type=snapshot_row["content_media_type"],
                snapshot_content_codec=snapshot_row["content_codec"],
                snapshot_blob_id=snapshot_row["blob_id"],
            )
            assert dry == after_compact.dispatch_read
            retained_tail = after_compact.dispatch_read.compile_snapshot.canonical_input
            assert any(
                i.source_entry_id == pending["entry"]
                for i in retained_tail.items
                if i.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            )
            assert not any(
                "AFTER_BRANCH_CUT" in provider_input_item_text(i)
                for i in retained_tail.items
                if i.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            )
            quote = reader.read_compaction_headroom_preflight(
                after_cut, deadline_monotonic=monotonic() + 30
            )
            assert quote.effective_materialization_lineage_floor == boundary
            assert (
                quote.selected_canonical_expanded_bytes
                >= retained_tail.canonical_expanded_bytes
            )
            created = await manager.invoke(
                tool_name="spawn_agent",
                arguments=args[2],
                invocation_context=contexts[2],
            )
            assert created.state == "SUCCESS", created.content
            await manager.invoke(
                tool_name="wait_agent",
                arguments=dict(task_ids=[ids[2]], settle="all", timeout_seconds=10),
                invocation_context=contexts[2],
            )
            next_turn = rows(
                repo,
                "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND scope_subagent_task_id=%s",
                (session, ids[2]),
            )[0]["id"]
            next_cut = repo.prepare_compaction_input_cut(
                lease.guard,
                turn_id=next_turn,
                allow_terminal=True,
                deadline_monotonic=monotonic() + 30,
            )
            next_input = reader.read_frozen_dispatch(
                next_cut, deadline_monotonic=monotonic() + 30
            ).compile_snapshot.canonical_input
            assert (
                sum(
                    i.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
                    for i in next_input.items
                )
                == 1
            )
            assert next_input.items[: len(retained_tail.items)] == retained_tail.items
            assert any(i.source_entry_id == pending["entry"] for i in next_input.items)
            assert not any(
                "AFTER_BRANCH_CUT" in provider_input_item_text(i)
                for i in next_input.items
                if i.item_kind is not FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            )
            ui_entries, _, ui_results, _, context = repo.list_subagent_task_activities(
                session_id=session, task_id=ids[2], maximum_items=50,
                after_entry_sequence=0, deadline_monotonic=monotonic()+30)
            assert context is not None and any(material["body"]=="A prefix only" for material in context["materials"])
            assert any(row["id"]==pending["entry"] and row["inherited"] for row in ui_entries)
            assert not any(row["id"]==candidate.result_entry_id for row in ui_entries)
            assert not any(result["result_entry_id"]==candidate.result_entry_id for result in ui_results)
            # Later source compaction must not reselect the branch's frozen binding.
            compact(repo, lease.guard, pending["turn"], boundary, summary="A changed later", idle=True)
            old_ui_entries, _, old_ui_results, _, old_context = repo.list_subagent_task_activities(
                session_id=session, task_id=ids[1], maximum_items=50,
                after_entry_sequence=0, deadline_monotonic=monotonic()+30)
            assert old_context is not None and not old_context["materials"]
            assert any(row["id"]==pending["entry"] for row in old_ui_entries)
            assert not any(row["id"]==candidate.result_entry_id for row in old_ui_entries)
            assert not old_ui_results
            assert started == [
                "tool-source-A",
                "followup-B",
                "followup-C",
            ]  # Old tool history is never scheduled again.
        finally:
            launch_gate.release.set()
            await manager.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())


@pytest.mark.postgres
def test_real_child_cold_dispatch_accepts_shared_material_objective_anchor(
    stage2_migrated_postgres_database,
):
    from tests.test_round5_long_horizon_postgres import _runner, _text_stream
    from tests.support.round3 import ScriptedKernelModel

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repo = ConversationKernelRepository(provider)

    async def exercise():
        session = _id("session")
        attempts = [_id("attempt"), _id("attempt")]
        ids = [_round10_id("subagent-task", a, "0") for a in attempts]
        args = [
            dict(task="cold-source-A"),
            dict(
                task="cold-branch-B",
                context=dict(mode="worker_history", task_id=ids[0]),
                material_task_ids=[ids[0]],
            ),
        ]
        lease, contexts = _prepare_root_tool_batch(
            repo,
            session_id=session,
            workspace_id=_id("workspace"),
            calls=tuple(
                ("spawn_agent", _id("call"), a, arg)
                for a, arg in zip(attempts, args, strict=True)
            ),
        )
        kwargs = _manager_launch_kwargs(repo, lease.guard)
        from tests.support.model_config import test_model_runtime

        runtime = test_model_runtime(
            model_id="test-model", wire_api="openai_chat_completions"
        )
        launch_port = kwargs["launch_preparation"]
        launch_port._model_runtime = runtime
        launch_port.inherit_target = lambda subject, binding: launch_port.freeze_target(
            binding, use_default=False
        )[1]
        manager = KernelSubagentManager(
            **kwargs,
            repository=repo,
            guard=lease.guard,
            host_owner_id=_id("host"),
            io_owner=KernelSessionIO(),
            live_bus=LiveAgentEventBus(),
            todo_owner=TodoRunStateOwner(session_id=session, owner_epoch=_id("todo")),
        )
        models = []

        def runner(scope):
            model = ScriptedKernelModel(
                [_text_stream("cold public answer", block_id=_id("block"))]
            )
            # Match the target frozen by the manager's parent fixture.
            from pulsara_agent.conversation_kernel.direct_model import (
                DirectKernelModelPort,
            )

            model._model_runtime = runtime
            model._preparer = DirectKernelModelPort(model_runtime=model._model_runtime)
            models.append(model)
            actual = _runner(
                repo, lease, model, tool_names=(), subagent_runtime=manager
            )
            return actual

        manager.bind_runner_factory(runner)
        try:
            await manager.open_root_completion_delivery(contexts[0].turn_id)
            for i, (arg, ctx) in enumerate(zip(args, contexts, strict=True)):
                created = await manager.invoke(
                    tool_name="spawn_agent", arguments=arg, invocation_context=ctx
                )
                assert created.state == "SUCCESS", created.content
                waited = await manager.invoke(
                    tool_name="wait_agent",
                    arguments=dict(task_ids=[ids[i]], settle="all", timeout_seconds=10),
                    invocation_context=ctx,
                )
                assert json.loads(waited.content)["pending_task_ids"] == []
                task = repo.query_subagent_task(
                    session_id=session,
                    task_id=ids[i],
                    deadline_monotonic=monotonic() + 30,
                )
                assert task["status"] == "COMPLETED", task.get("terminal_public_detail")
                assert len(models[i].requests) == 1
            messages = models[1].requests[0].compiled_input.messages
            texts = [p.text for m in messages for p in m.content if hasattr(p, "text")]
            assert sum("pulsara_initial_context" in text for text in texts) == 1
            assert texts.count("cold-source-A") == 1
            assert texts.count("cold-branch-B") == 1
            assert texts.index("cold-source-A") < texts.index("cold-branch-B")
        finally:
            await manager.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())
