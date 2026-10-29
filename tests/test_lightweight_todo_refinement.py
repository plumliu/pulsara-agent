from __future__ import annotations
from pulsara_agent.identity import derive_id

import asyncio
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
from pulsara_agent.ports.live_agent_event import (
    TodoLiveItemProjection,
    TodoSnapshotUpdatedPayload,
)
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
    build_root_activation,
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
            scope_kind=ModelInputScopeKind.ROOT
        ).revision
        == 0
    )


def test_todo_external_continuation_tolerates_state_lost_with_host() -> None:
    owner = TodoRunStateOwner(session_id='session:cbauiajfisf4gvpdotrq4qgxhq', owner_epoch='host:zgcu435qy75pmagj7jrf2g5wmi')
    assert not owner.bind_continuation_if_present(
        scope_kind=ModelInputScopeKind.ROOT,
        turn_id='turn:z3y7csg3jjgexbqxsnpkynjdsu',
    )
    assert owner.current_snapshots() == ()

    owner.activate_root_run(_root_activation('turn:jwcvnoms3gl66moynh76r32u34'))
    assert owner.bind_continuation_if_present(
        scope_kind=ModelInputScopeKind.ROOT,
        turn_id='turn:or2urxxbbqlxjsgc6dakbrpw3e',
    )
    owner.require_active(
        scope_kind=ModelInputScopeKind.ROOT,
        exact_turn_id='turn:or2urxxbbqlxjsgc6dakbrpw3e',
    )
    assert (
        owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT,
        ).revision
        == 0
    )




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
        )
        assert before is not None and before.revision == 0
        settlement = await port.settle_process_local_effect(
            result.process_local_settlement,
            ProcessLocalEffectSettlementDisposition.COMMITTED,
        )
        assert settlement.outcome is ProcessLocalEffectSettlementOutcome.INSTALLED
        after = port.todo_owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT,
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

        # Exercise the production ROOT completion boundary, owner, live bus and
        # wire projection together. A stale completed task must not clear it.
        from pulsara_agent.conversation_kernel.host import KernelHostSession
        from unittest.mock import AsyncMock, Mock
        active = asyncio.current_task()
        host = SimpleNamespace(_lock=asyncio.Lock(), _active_task=active,
            _session_scope_kind=ModelInputScopeKind.ROOT,
            _active_turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a', _closing=False,
            _compaction=SimpleNamespace(take_manual=AsyncMock(return_value=None)),
            _tools=port, _mark_feedback_target_ended_locked=Mock(),
            _clear_active_root_locked=Mock())
        await KernelHostSession._settle_active_root_task(host, object())
        assert port.todo_owner.current_snapshots()[0].ordered_items
        await KernelHostSession._settle_active_root_task(host, active)
        host._clear_active_root_locked.assert_called_once()
        cleared = port.todo_owner.current_snapshots()[0]
        assert cleared.ordered_items == () and cleared.revision == 2
        assert after.ordered_items[1].status.value == 'pending'
        events = live_bus.observe(observer, after_revision=baseline, maximum_events=4).events
        assert len(events) == 2
        wire = _live_to_wire(generation, events[-1])
        assert wire.payload.todo_snapshot_updated.disposition == 'CLEARED'
        assert not wire.payload.todo_snapshot_updated.ordered_items
        assert port.todo_owner.mark_root_idle(exact_turn_id=host._active_turn_id) is None
        port.todo_owner.bind_continuation(scope_kind=ModelInputScopeKind.ROOT,
            turn_id='turn:r32o4pzdgx4q4pytdgc6ereg3i')
        assert not port.todo_owner.current_snapshots()[0].ordered_items
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
            scope_kind=ModelInputScopeKind.ROOT
        )
        settlement = await port.settle_process_local_effect(
            result.process_local_settlement,
            ProcessLocalEffectSettlementDisposition.COMMITTED,
        )
        assert settlement.outcome is ProcessLocalEffectSettlementOutcome.INSTALLED
        snapshot = port.todo_owner.snapshot(
            scope_kind=ModelInputScopeKind.ROOT
        )
        assert snapshot is not None and snapshot.revision == 1
        await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())


def test_todo_close_fails_closed_for_ownerless_pending_settlement() -> None:
    owner = TodoRunStateOwner(session_id='session:cbauiajfisf4gvpdotrq4qgxhq', owner_epoch='host:umsmaqdnqz3cpmtzml56ydq7ei')
    owner.activate_root_run(_root_activation())
    prepared = owner.prepare_replace(
        scope_kind=ModelInputScopeKind.ROOT,
        exact_turn_id='turn:nzc36cttgo7xxki7bcjc7ops7a',
        attempt_id='attempt:dvce4jlhrqd3hzcfpszy4gujye',
        proposed_result_entry_id='entry:zvmjajlmix3vyoeoy2zhz6xd2a',
        candidate=parse_todo_replacement(
            {"items": [{"text": "settle before close", "status": "in_progress"}]}
        ),
        acknowledgement=b'{"status":"UPDATED"}',
    )
    assert owner.mark_closing(
        scope_kind=ModelInputScopeKind.ROOT
    )
    with pytest.raises(RuntimeError, match="before settlement drain"):
        owner.close_run(
            scope_kind=ModelInputScopeKind.ROOT,
        )
    installed = owner.commit(prepared)
    assert installed.installed_snapshot.revision == 1
    closed = owner.close_run(
        scope_kind=ModelInputScopeKind.ROOT
    )
    assert closed is not None and closed.closing_revision == 2
    assert (
        owner.close_run(
            scope_kind=ModelInputScopeKind.ROOT
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
            scope_kind=ModelInputScopeKind.ROOT
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
        )
        assert snapshot is not None and snapshot.revision == 0
        await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())




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
        )
        coordinator._writer_lease = SimpleNamespace(guard=object())
        coordinator._todo_finalizer = finalizer
        coordinator.interrupt_turn = terminalize
        intent = ActiveTurnCancellationIntent('turn:nzc36cttgo7xxki7bcjc7ops7a', ModelInputScopeKind.ROOT)
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
