from __future__ import annotations
from pulsara_agent.identity import derive_id

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unicodedata

import pytest

from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import LiveControlSnapshot
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolAuthorizationKind,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
)
from pulsara_agent.conversation_kernel.turn_admission import (
    SubagentTurnAdmissionPostCommitError,
    TurnAdmissionCoordinator,
)
from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.repository import AcceptedEntry
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    SUBJECT_SLOTS,
)
from pulsara_agent.ports.live_agent_event import (
    TodoLiveItemProjection,
    TodoSnapshotUpdatedPayload,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.terminal_protocol.v3_gateway import (
    _live_control_snapshot_to_wire,
    _live_to_wire,
)
from tests.support.round3 import (
    authorize_direct_tool,
    direct_tool_invocation_context,
)
from pulsara_agent.tools.builtins.todo import (
    MAXIMUM_TODO_CANONICAL_JSON_BYTES,
    TodoValidationError,
    parse_todo_replacement,
)
from pulsara_agent.conversation_kernel.todo_runtime import (
    TodoLiveDisposition,
    TodoRunStateOwner,
    build_child_activation,
    build_root_activation,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    CompactionRuntimeHandoffBoundError,
    freeze_compaction_runtime_handoff,
)


def _root_activation(turn: str = 'turn:nzc36cttgo7xxki7bcjc7ops7a'):
    return build_root_activation(
        session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
        admission_kind="DIRECT",
        command_id='command:3mkyvtvppfglbmqedm6zqizjvy',
        exact_turn_id=turn,
        exact_initial_entry_id=derive_id("entry", "test-fixture", f"entry:{turn}"),
        exact_context_binding_revision_id=derive_id("context", "test-fixture", f"context:{turn}"),
    )


def _child_activation(turn: str = 'turn:nbjllslbuktypzvmqopgumgyjy'):
    return build_child_activation(
        session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
        subagent_task_id='task:jjp4dy4hmhapm5toxqet6i7qkm',
        exact_turn_id=turn,
        exact_initial_entry_id=derive_id("entry", "test-fixture", f"entry:{turn}"),
        exact_context_binding_revision_id=derive_id("context", "test-fixture", f"context:{turn}"),
    )


def test_todo_complete_snapshot_contract_and_byte_truth() -> None:
    candidate = parse_todo_replacement(
        {
            "items": [
                {"text": "Inspect failure", "status": "in_progress"},
                {"text": "Implement fix", "status": "pending"},
                {"text": "Retain proof", "status": "completed"},
            ]
        }
    )
    assert [item.ordinal for item in candidate.ordered_items] == [0, 1, 2]
    assert (
        candidate.pending_count,
        candidate.in_progress_count,
        candidate.completed_count,
    ) == (1, 1, 1)
    assert candidate.canonical_json_utf8_bytes <= MAXIMUM_TODO_CANONICAL_JSON_BYTES
    assert parse_todo_replacement({"items": []}).ordered_items == ()


@pytest.mark.parametrize("text", ["x" * 192, "界" * 64, "😀" * 48])
def test_todo_text_accepts_exact_utf8_boundaries(text: str) -> None:
    candidate = parse_todo_replacement({"items": [{"text": text, "status": "pending"}]})
    assert candidate.ordered_items[0].text == text


def test_todo_item_and_aggregate_bounds_are_independent() -> None:
    sixteen = [
        {"text": f"{index:02d}-" + "x" * 157, "status": "pending"}
        for index in range(16)
    ]
    assert len(parse_todo_replacement({"items": sixteen}).ordered_items) == 16
    with pytest.raises(TodoValidationError, match="at most 16"):
        parse_todo_replacement(
            {"items": sixteen + [{"text": "one too many", "status": "pending"}]}
        )
    oversized = [
        {"text": f"{index:02d}" + "\\" * 190, "status": "pending"}
        for index in range(16)
    ]
    with pytest.raises(TodoValidationError, match="4 KiB"):
        parse_todo_replacement({"items": oversized})

    near_bound = parse_todo_replacement(
        {
            "items": [
                {"text": f"{index:02d}" + "x" * 190, "status": "pending"}
                for index in range(16)
            ]
        }
    )
    assert near_bound.canonical_json_utf8_bytes == 3579
    # Live framing adds transport metadata, but the sole 4 KiB product quote
    # remains the provider-neutral ordered-item snapshot rather than the outer
    # observation envelope.
    TodoSnapshotUpdatedPayload(
        todo_run_id="todo-root-run:near-bound",
        todo_revision=1,
        disposition="ACTIVE",
        ordered_items=tuple(
            TodoLiveItemProjection(item.ordinal, item.text, item.status.value)
            for item in near_bound.ordered_items
        ),
        pending_count=16,
        in_progress_count=0,
        completed_count=0,
    )


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"action": "list"}, "only the items"),
        (
            {
                "items": [
                    {"text": "A", "status": "in_progress"},
                    {"text": "B", "status": "in_progress"},
                ]
            },
            "at most one",
        ),
        (
            {
                "items": [
                    {"text": "duplicate", "status": "pending"},
                    {"text": "duplicate", "status": "completed"},
                ]
            },
            "duplicate",
        ),
        ({"items": [{"text": " padded ", "status": "pending"}]}, "whitespace"),
        ({"items": [{"text": "line\nbreak", "status": "pending"}]}, "safe line"),
        ({"items": [{"text": "nul\x00byte", "status": "pending"}]}, "safe line"),
        (
            {"items": [{"text": "valid", "status": "pending", "id": "old"}]},
            "requires only",
        ),
        (
            {"items": [{"text": "x" * 193, "status": "pending"}]},
            "192 UTF-8 bytes",
        ),
        (
            {"items": [{"text": "😀" * 49, "status": "pending"}]},
            "192 UTF-8 bytes",
        ),
        (
            {
                "items": [
                    {
                        "text": unicodedata.normalize("NFD", "é"),
                        "status": "pending",
                    }
                ]
            },
            "Unicode NFC",
        ),
    ],
)
def test_todo_invalid_candidates_fail_before_owner_mutation(
    arguments: dict[str, object], message: str
) -> None:
    with pytest.raises(TodoValidationError, match=message):
        parse_todo_replacement(arguments)


def test_todo_owner_isolates_root_children_and_freezes_handoff() -> None:
    owner = TodoRunStateOwner(session_id='session:cbauiajfisf4gvpdotrq4qgxhq', owner_epoch='host:umsmaqdnqz3cpmtzml56ydq7ei')
    owner.activate_root_run(_root_activation())
    child = build_child_activation(
        session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
        subagent_task_id='task:7ekouunh3opwy6r5hpnpyw22fa',
        exact_turn_id='turn:h2g7tkxc3t3s7qaeuv3uyoxu7m',
        exact_initial_entry_id='entry:fhx2dcgwzqpysr5bxkzd7zvgve',
        exact_context_binding_revision_id='context:qfbpxjr42k7p4ehg2k7ki65k3y',
    )
    owner.activate_child_run(child)
    assert (
        owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
        ).revision
        == 0
    )
    assert (
        owner.snapshot(
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id='task:7ekouunh3opwy6r5hpnpyw22fa',
        ).revision
        == 0
    )
    assert (
        owner.snapshot(
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id='task:ofq7v6vy25ozw4nkz722653tpy',
        )
        is None
    )

    candidate = parse_todo_replacement(
        {
            "items": [
                {"text": "Work now", "status": "in_progress"},
                {"text": "Already done", "status": "completed"},
            ]
        }
    )
    prepared = owner.prepare_replace(
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        exact_turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
        attempt_id='attempt:dvce4jlhrqd3hzcfpszy4gujye',
        proposed_result_entry_id='entry:zvmjajlmix3vyoeoy2zhz6xd2a',
        candidate=candidate,
        acknowledgement=b'{"status":"UPDATED"}',
    )
    installed = owner.commit(prepared)
    assert installed.installed_snapshot.revision == 1
    assert installed.disposition is TodoLiveDisposition.ACTIVE
    handoff = owner.freeze_compaction_handoff(
        scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
    )
    assert handoff is not None
    assert [item.text for item in handoff.ordered_items] == [
        "Work now",
        "Already done",
    ]
    runtime_handoff = freeze_compaction_runtime_handoff(
        terminal_processes=(),
        terminal_monitors=(),
        todo=handoff,
        subagent_tasks=(),
    )
    assert runtime_handoff is not None
    for rendered in (runtime_handoff.full_text, runtime_handoff.compact_text):
        payload = json.loads(rendered)
        assert payload["todos"] == [
            {"ordinal": 0, "status": "in_progress", "text": "Work now"},
            {"ordinal": 1, "status": "completed", "text": "Already done"},
        ]
        assert payload["todo_counts"] == {
            "completed": 1,
            "in_progress": 1,
            "pending": 0,
            "total": 2,
        }
        assert payload["omitted"]["todos"] == 0
    with pytest.raises(CompactionRuntimeHandoffBoundError):
        freeze_compaction_runtime_handoff(
            terminal_processes=(),
            terminal_monitors=(),
            todo=handoff,
            subagent_tasks=(),
            maximum_utf8_bytes=32,
        )


def test_todo_next_root_activation_closes_old_exact_run() -> None:
    owner = TodoRunStateOwner(session_id='session:cbauiajfisf4gvpdotrq4qgxhq', owner_epoch='host:umsmaqdnqz3cpmtzml56ydq7ei')
    first = _root_activation('turn:nzc36cttgo7xxki7bcjc7ops7a')
    owner.activate_root_run(first)
    assert owner.activate_root_run(first) is None
    with pytest.raises(RuntimeError, match="before becoming idle"):
        owner.activate_root_run(
            build_root_activation(
                session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
                admission_kind="QUEUED",
                queue_item_id='queue:n643ni32rsjinkyftscv43x3y4',
                queue_sequence=1,
                exact_turn_id='turn:r32o4pzdgx4q4pytdgc6ereg3i',
                exact_initial_entry_id='entry:rls5j3xowmv4ws2d3bms77ogzu',
                exact_context_binding_revision_id='context:wmqcqsdd7yegc6yvrb6hshwwei',
            )
        )
    owner.mark_root_idle(exact_turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a')
    closed = owner.activate_root_run(
        build_root_activation(
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            admission_kind="QUEUED",
            queue_item_id='queue:frtvnwjuu7muxu2y7xzgsrycay',
            queue_sequence=2,
            exact_turn_id='turn:u7jrb7ntdvxsawfkehwcc4rhu4',
            exact_initial_entry_id='entry:yym6qf3l4xz6437gradtmhjrke',
            exact_context_binding_revision_id='context:4bqlloqsjgcip73jgeokprl2oi',
        )
    )
    assert closed.last_turn_id == 'turn:nzc36cttgo7xxki7bcjc7ops7a'
    assert closed.closing_revision == 1
    assert closed.disposition is TodoLiveDisposition.CLOSED
    assert (
        owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
        ).revision
        == 0
    )


def test_todo_external_continuation_tolerates_state_lost_with_host() -> None:
    owner = TodoRunStateOwner(session_id='session:cbauiajfisf4gvpdotrq4qgxhq', owner_epoch='host:zgcu435qy75pmagj7jrf2g5wmi')
    assert not owner.bind_continuation_if_present(
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        turn_id='turn:z3y7csg3jjgexbqxsnpkynjdsu',
    )
    assert owner.current_snapshots() == ()

    owner.activate_root_run(_root_activation('turn:jwcvnoms3gl66moynh76r32u34'))
    assert owner.bind_continuation_if_present(
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        turn_id='turn:or2urxxbbqlxjsgc6dakbrpw3e',
    )
    owner.require_active(
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        exact_turn_id='turn:or2urxxbbqlxjsgc6dakbrpw3e',
    )
    assert (
        owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ).revision
        == 0
    )


def test_external_root_continuations_use_loss_tolerant_todo_binding() -> None:
    host_source = (
        Path(__file__).resolve().parents[1]
        / "src/pulsara_agent/conversation_kernel/host.py"
    ).read_text(encoding="utf-8")
    for method_name in (
        "_bind_plan_review_successor",
        "_start_terminal_observation_turn",
        "_start_subagent_completion_turn",
    ):
        method_source = host_source.split(f"    async def {method_name}(", 1)[1].split(
            "\n    async def ", 1
        )[0]
        assert ".bind_continuation_if_present(" in method_source
        assert ".bind_continuation(" not in method_source

    active_chain = host_source.split("    async def _finish_root_chain(", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert ".bind_continuation(" in active_chain
    assert ".bind_continuation_if_present(" not in active_chain


def test_todo_tool_installs_only_after_explicit_canonical_settlement(
    tmp_path,
) -> None:
    async def exercise() -> None:
        live_bus = LiveAgentEventBus()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id='host:umsmaqdnqz3cpmtzml56ydq7ei',
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            live_bus=live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.todo_owner.activate_root_run(_root_activation())
        arguments = {
            "items": [
                {"text": "Implement exact settlement", "status": "in_progress"},
                {"text": "Run retained gates", "status": "pending"},
            ]
        }
        authorization = await authorize_direct_tool(
            port,
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            tool_name="todo",
            arguments=arguments,
            tool_call_id="call:todo",
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
        )
        assert authorization.kind is KernelToolAuthorizationKind.ALLOW
        observer, generation, baseline = live_bus.subscribe()
        borrow, context = direct_tool_invocation_context(
            port,
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            tool_name="todo",
            tool_call_id="call:todo",
            attempt_id='attempt:llwufckx3a6axbwpqvkmkekppy',
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
        )
        try:
            result = await port.invoke(
                tool_name="todo",
                arguments=arguments,
                tool_call_id="call:todo",
                attempt_id='attempt:llwufckx3a6axbwpqvkmkekppy',
                turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
                assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
                invocation_context=context,
            )
        finally:
            borrow.close()
        assert result.process_local_settlement is not None
        before = port.todo_owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        assert before is not None and before.revision == 0
        settlement = await port.settle_process_local_effect(
            result.process_local_settlement,
            ProcessLocalEffectSettlementDisposition.COMMITTED,
        )
        assert settlement.outcome is ProcessLocalEffectSettlementOutcome.INSTALLED
        after = port.todo_owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        assert after is not None and after.revision == 1
        assert [item.text for item in after.ordered_items] == [
            "Implement exact settlement",
            "Run retained gates",
        ]
        observed = live_bus.observe(observer, after_revision=baseline, maximum_events=4)
        assert observed.generation == generation
        assert len(observed.events) == 1
        event = observed.events[0]
        assert event.event_type is LiveEventType.TODO_SNAPSHOT_UPDATED
        assert isinstance(event.payload, TodoSnapshotUpdatedPayload)
        assert event.payload.todo_revision == 1
        projected = _live_to_wire(generation, event)
        assert projected.event_type > 0
        assert projected.payload.WhichOneof("payload") == "todo_snapshot_updated"
        assert b"sha256:" not in projected.SerializeToString(deterministic=True)

        resync = _live_control_snapshot_to_wire(
            LiveControlSnapshot('session:cbauiajfisf4gvpdotrq4qgxhq', 1, 0, None),
            port.todo_owner.current_snapshots(),
        )
        assert len(resync.current_todos) == 1
        assert resync.current_todos[0].todo_run_id == after.run_identity.todo_run_id
        assert resync.current_todos[0].todo_revision == 1
        await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())


def test_todo_canonical_full_installs_while_run_is_closing(tmp_path) -> None:
    async def exercise() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id='host:umsmaqdnqz3cpmtzml56ydq7ei',
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.todo_owner.activate_root_run(_root_activation())
        borrow, context = direct_tool_invocation_context(
            port,
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            tool_name="todo",
            tool_call_id="call:todo",
            attempt_id='attempt:llwufckx3a6axbwpqvkmkekppy',
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
        )
        try:
            result = await port.invoke(
                tool_name="todo",
                arguments={"items": [{"text": "Finish close", "status": "completed"}]},
                tool_call_id="call:todo",
                attempt_id='attempt:llwufckx3a6axbwpqvkmkekppy',
                turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
                assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
                invocation_context=context,
            )
        finally:
            borrow.close()
        assert result.process_local_settlement is not None
        assert port.todo_owner.mark_closing(
            scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
        )
        settlement = await port.settle_process_local_effect(
            result.process_local_settlement,
            ProcessLocalEffectSettlementDisposition.COMMITTED,
        )
        assert settlement.outcome is ProcessLocalEffectSettlementOutcome.INSTALLED
        snapshot = port.todo_owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
        )
        assert snapshot is not None and snapshot.revision == 1
        await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())


def test_todo_close_fails_closed_for_ownerless_pending_settlement() -> None:
    owner = TodoRunStateOwner(session_id='session:cbauiajfisf4gvpdotrq4qgxhq', owner_epoch='host:umsmaqdnqz3cpmtzml56ydq7ei')
    owner.activate_root_run(_root_activation())
    prepared = owner.prepare_replace(
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        exact_turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
        attempt_id='attempt:dvce4jlhrqd3hzcfpszy4gujye',
        proposed_result_entry_id='entry:zvmjajlmix3vyoeoy2zhz6xd2a',
        candidate=parse_todo_replacement(
            {"items": [{"text": "settle before close", "status": "in_progress"}]}
        ),
        acknowledgement=b'{"status":"UPDATED"}',
    )
    assert owner.mark_closing(
        scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
    )
    with pytest.raises(RuntimeError, match="before settlement drain"):
        owner.close_run(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
    installed = owner.commit(prepared)
    assert installed.installed_snapshot.revision == 1
    closed = owner.close_run(
        scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
    )
    assert closed is not None and closed.closing_revision == 2
    assert (
        owner.close_run(
            scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
        )
        is None
    )


def test_todo_read_only_authorization_and_invoke_close_race_are_known(
    tmp_path,
) -> None:
    async def exercise() -> None:
        live_bus = LiveAgentEventBus()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id='host:umsmaqdnqz3cpmtzml56ydq7ei',
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            live_bus=live_bus,
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.todo_owner.activate_root_run(_root_activation())
        permission = build_run_permission_snapshot(
            snapshot_id='permission:bhwqiflryq5b4avuvzyhp3xn4q',
            requested_mode=PermissionMode.READ_ONLY,
            effective_mode=PermissionMode.READ_ONLY,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        invalid = await authorize_direct_tool(
            port,
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            tool_name="todo",
            arguments={
                "items": [
                    {"text": "one", "status": "in_progress"},
                    {"text": "two", "status": "in_progress"},
                ]
            },
            tool_call_id="call:todo-invalid",
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
            permission_snapshot=permission,
        )
        assert invalid.kind is KernelToolAuthorizationKind.INVALID_ARGUMENTS
        arguments = {"items": [{"text": "Advisory local state", "status": "pending"}]}
        authorization = await authorize_direct_tool(
            port,
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            tool_name="todo",
            arguments=arguments,
            tool_call_id="call:todo-race",
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
            permission_snapshot=permission,
        )
        assert authorization.kind is KernelToolAuthorizationKind.ALLOW
        borrow, context = direct_tool_invocation_context(
            port,
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            tool_name="todo",
            tool_call_id="call:todo-race",
            attempt_id='attempt:vwhestkdi27h64gh7jeqo7mw3a',
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
            permission_snapshot=permission,
        )
        assert port.todo_owner.mark_closing(
            scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
        )
        try:
            result = await port.invoke(
                tool_name="todo",
                arguments=arguments,
                tool_call_id="call:todo-race",
                attempt_id='attempt:vwhestkdi27h64gh7jeqo7mw3a',
                turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
                assistant_entry_id='entry:5qidalqvkujk5ykzzzfpwie2qy',
                invocation_context=context,
            )
        finally:
            borrow.close()
        assert result.state == "APPLICATION_ERROR"
        assert result.process_local_settlement is None
        snapshot = port.todo_owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        assert snapshot is not None and snapshot.revision == 0
        await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())


def test_todo_refinement_preserves_the_closed_durability_oracle() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 30
    assert len(LiveEventType) == 24
    assert tuple(item for item in LiveEventType if "TODO" in item.name) == (
        LiveEventType.TODO_SNAPSHOT_UPDATED,
    )
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 31

    root = Path(__file__).resolve().parents[1]
    migration = (
        root
        / "src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql"
    ).read_text(encoding="utf-8")
    assert "todo_" not in migration.lower()
    model_input = root / "src/pulsara_agent/model_input"
    assert all(
        "TODO_CONTEXT" not in path.read_text(encoding="utf-8")
        for path in model_input.rglob("*.py")
    )


def test_todo_admission_finalizer_is_not_detached_by_waiter_cancellation() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        installed = False

        async def finalizer(_prepared, _accepted) -> None:
            nonlocal installed
            started.set()
            await release.wait()
            installed = True

        coordinator = object.__new__(TurnAdmissionCoordinator)
        coordinator._workspace_gate = None
        coordinator._todo_finalizer = finalizer
        task = asyncio.create_task(
            coordinator._finalize_todo(
                _root_activation(),
                AcceptedEntry('entry:kbwuv6gdfgm4t6267hmfj2ylka', 'turn:nzc36cttgo7xxki7bcjc7ops7a', 1, 1),
            )
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert installed

    asyncio.run(exercise())


def test_child_todo_activation_failure_preserves_postcommit_ownership() -> None:
    async def exercise() -> None:
        accepted = AcceptedEntry('entry:5ymhkczans3dopadxofmoe5nay', 'turn:nbjllslbuktypzvmqopgumgyjy', 1, 1)
        terminalized: list[tuple[str, str]] = []

        async def finalizer(_prepared, _accepted) -> None:
            raise RuntimeError("child TODO run capacity is exhausted")

        async def terminalize(turn_id: str, reason: str) -> None:
            terminalized.append((turn_id, reason))

        coordinator = object.__new__(TurnAdmissionCoordinator)
        coordinator._workspace_gate = None
        coordinator._todo_finalizer = finalizer
        coordinator.interrupt_turn = terminalize

        with pytest.raises(SubagentTurnAdmissionPostCommitError) as raised:
            await coordinator._finalize_todo(_child_activation(), accepted)

        assert raised.value.accepted == accepted
        assert isinstance(raised.value.activation_error, RuntimeError)
        assert terminalized == []

    asyncio.run(exercise())


def test_direct_admission_cancellation_terminalizes_full_winner_after_finalizer() -> (
    None
):
    class _ImmediateAdmissionIO:
        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                accepted=AcceptedEntry('entry:kbwuv6gdfgm4t6267hmfj2ylka', 'turn:nzc36cttgo7xxki7bcjc7ops7a', 1, 1)
            )

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        terminalized: list[tuple[str, str]] = []

        async def finalizer(_prepared, _accepted) -> None:
            started.set()
            await release.wait()

        async def terminalize(turn_id: str, reason: str) -> None:
            terminalized.append((turn_id, reason))

        coordinator = object.__new__(TurnAdmissionCoordinator)
        coordinator._workspace_gate = None
        coordinator._io = _ImmediateAdmissionIO()
        coordinator._deadlines = SimpleNamespace(deadline=lambda _owner: 999_999_999.0)
        coordinator._repository = SimpleNamespace(
            accept_root_turn_intent=object(),
            accept_subagent_turn=object(),
        )
        coordinator._writer_lease = SimpleNamespace(guard=object())
        coordinator._todo_finalizer = finalizer
        coordinator.interrupt_turn = terminalize
        intent = ActiveTurnCancellationIntent('turn:nzc36cttgo7xxki7bcjc7ops7a', ModelInputScopeKind.ROOT, None)
        candidate = SimpleNamespace(
            session_id='session:cbauiajfisf4gvpdotrq4qgxhq',
            command_id='command:3mkyvtvppfglbmqedm6zqizjvy',
            turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
            entry_id='entry:kbwuv6gdfgm4t6267hmfj2ylka',
            context_binding_revision_id='binding:ob5azto6we6xylhs2fep26ql3u',
        )
        task = asyncio.create_task(
            coordinator.accept_root_intent(
                candidate,
                provider_input_admission=object(),
                model_resolution_snapshot=object(),
                cancellation_intent=intent,
            )
        )
        await started.wait()
        intent.install_cause(ForegroundCancellationCause.USER_REQUEST)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert terminalized == [('turn:nzc36cttgo7xxki7bcjc7ops7a', "USER_STOPPED")]

    asyncio.run(exercise())
