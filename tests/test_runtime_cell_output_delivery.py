"""The Host confirms one canonical queue input before releasing its Cell record."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.code_mode.contracts import CodeModeOutputLease
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.steer import PromptIngressConfirmationKind
from pulsara_agent.identity import random_id


@pytest.mark.parametrize('commit_ack_lost', [False, True])
def test_record_is_retained_until_original_queue_admission_is_confirmed(commit_ack_lost):
    async def run():
        cell_id = random_id('cell')
        source_turn = random_id('turn')
        attempt = random_id('attempt')
        leases = [CodeModeOutputLease(cell_id, source_turn, attempt, index,
                    {'kind': 'log', 'text': 'same'}, object(), datetime.now(timezone.utc))
                  for index in (1, 2)]
        committed = {}
        calls = []
        released = []
        class IO:
            async def run(self, operation, *args, **kwargs):
                return await operation(*args, **kwargs)
        async def confirm(*, candidate, **kwargs):
            return SimpleNamespace(kind=PromptIngressConfirmationKind.FULL_COMPATIBLE
                if candidate.queue_item_id in committed else PromptIngressConfirmationKind.NONE)
        async def enqueue(guard, *, candidate, **kwargs):
            calls.append(candidate.queue_item_id)
            assert candidate.content.record_sequence == len(committed) + 1
            committed[candidate.queue_item_id] = candidate
            if commit_ack_lost and len(committed) == 1:
                raise ConnectionError('COMMIT succeeded; acknowledgement lost')
        async def lease(cell_id):
            return leases[0]
        async def acknowledge(item, *, accepted=True):
            assert accepted and item is leases[0]
            assert len(committed) >= item.record_sequence
            released.append(item.record_sequence)
            leases.pop(0)
            if not leases:
                host._closing = True
        host = SimpleNamespace(
            session_id=random_id('session'), host_session_id=random_id('host'),
            workspace=SimpleNamespace(workspace_key=random_id('workspace')),
            _lease=SimpleNamespace(guard=object()), _io=IO(), _closing=False,
            _cell_output_wake=asyncio.Event(), _queue_wake=asyncio.Event(),
            _canonical_deadline=lambda: 999999999999,
            repository=SimpleNamespace(confirm_cell_output_ingress=confirm, enqueue_cell_output=enqueue),
            _code_mode=SimpleNamespace(pending_output_cells=lambda: (cell_id,) if leases else (),
                lease_output=lease, acknowledge_output=acknowledge, output_delivery_allowed=lambda item: True),
        )
        host._cell_output_wake.set()
        await asyncio.wait_for(KernelHostSession._cell_output_delivery_loop(host), 2)
        assert released == [1, 2]
        assert len(calls) == len(set(calls)) == 2
        assert len(committed) == 2
        assert [item.content.record['text'] for item in committed.values()] == ['same', 'same']
    asyncio.run(run())


@pytest.mark.postgres
def test_real_host_cell_records_reach_queue_and_canonical_without_wait(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    import json
    from time import monotonic
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round4_plan_host import _open_test_session, _text, _tool_call

    async def run():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        calls = 0
        code = (
            "await tools.exec_process({command:\"while [ ! -f release-cell ]; do sleep 0.01; done\",yield_time_ms:1000});"
            "console.log('same'); console.log('same');"
        )
        async def stream(request):
            nonlocal calls
            calls += 1
            events = (_tool_call('exec_cell', random_id('call'), {'code': code})
                      if calls == 1 else _text('root done'))
            for event in events:
                yield event
        core, host = await _open_test_session(
            tmp_path=tmp_path, model=CallbackScriptedKernelModel(stream),
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        try:
            first = await host.run_turn(PromptContent.text('run a program'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert first.final_text == 'root done'
            (host.workspace.workspace_root / 'release-cell').write_text('ready')
            async with asyncio.timeout(20):
                while True:
                    with host.repository.connection_provider.connection(
                        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 20,
                    ) as connection:
                        rows = connection.execute(
                            "SELECT q.id,q.queue_sequence,q.status,e.entry_kind,e.inline_content "
                            "FROM pulsara_v3.prompt_queue_items q LEFT JOIN pulsara_v3.transcript_entries e "
                            "ON e.session_id=q.session_id AND e.id=q.consumed_entry_id "
                            "WHERE q.session_id=%s AND q.input_origin='CELL_OUTPUT' ORDER BY q.queue_sequence",
                            (host.session_id,),
                        ).fetchall()
                    if len(rows) == 2 and all(row[2] == 'CONSUMED' for row in rows) and calls >= 3:
                        break
                    await asyncio.sleep(.01)
            assert len({row[0] for row in rows}) == 2
            assert rows[0][1] < rows[1][1]
            assert [row[3] for row in rows] == ['TERMINAL_OBSERVATION', 'TERMINAL_OBSERVATION']
            records = [json.loads(bytes(row[4])) for row in rows]
            assert [record['record_sequence'] for record in records] == [1, 2]
            assert [record['record']['text'] for record in records] == ['same', 'same']
            assert calls >= 3  # Original tool call/final and autonomous output processing.
        finally:
            (host.workspace.workspace_root / 'release-cell').write_text('ready')
            await core.shutdown()
    asyncio.run(run())


@pytest.mark.postgres
def test_active_root_consumes_two_cell_inputs_at_one_safe_point(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    import json
    from time import monotonic
    from pulsara_agent.llm.input import PromptContent, text_part_values
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from tests.support.round3 import CallbackScriptedKernelModel
    from tests.test_round4_plan_host import _open_test_session, _text, _tool_call

    async def run():
        monkeypatch.setenv('PULSARA_HOME', str(tmp_path / 'home'))
        calls = []
        host = None
        def rows():
            with host.repository.connection_provider.connection(
                lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 20,
            ) as connection:
                return connection.execute(
                    "SELECT delivery_mode,target_turn_id,status,consumed_entry_id "
                    "FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND input_origin='CELL_OUTPUT' "
                    "ORDER BY queue_sequence", (host.session_id,),
                ).fetchall()
        async def stream(request):
            calls.append(request)
            if len(calls) == 1:
                events = _tool_call('exec_cell', random_id('call'),
                    {'code': "await tools.exec_process({command:\"while [ ! -f release-active ]; do sleep 0.01; done\",yield_time_ms:1000}); console.log('first'); console.log('second');"})
            elif len(calls) == 2:
                (host.workspace.workspace_root / 'release-active').write_text('ready')
                async with asyncio.timeout(15):
                    while len(rows()) != 2:
                        await asyncio.sleep(.01)
                assert all(row[0] == 'STEER_ACTIVE_TURN' and row[2] == 'PENDING' for row in rows())
                events = _text('unread final cannot end this turn')
            else:
                events = _text('both read')
            for event in events:
                yield event
        core, host = await _open_test_session(
            tmp_path=tmp_path, model=CallbackScriptedKernelModel(stream),
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        try:
            result = await host.run_turn(PromptContent.text('run and report'),
                command_id=random_id('command'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert result.final_text == 'both read'
            assert len(calls) == 3
            final_rows = rows()
            assert len(final_rows) == 2
            assert len({row[1] for row in final_rows}) == 1
            assert all(row[2] == 'CONSUMED' for row in final_rows)
            assert len({row[3] for row in final_rows}) == 2
            wire_texts = [text for message in calls[-1].compiled_input.messages
                          for text in text_part_values(message.content)]
            cell_inputs = [json.loads(text)['pulsara_terminal_observation'] for text in wire_texts
                           if text.startswith('{"pulsara_terminal_observation":')]
            assert [item['record']['text'] for item in cell_inputs] == ['first', 'second']
        finally:
            await core.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('confirmation_kind', [PromptIngressConfirmationKind.NONE, PromptIngressConfirmationKind.CONFLICT])
def test_proven_permanent_output_failure_rejects_instead_of_acknowledging(confirmation_kind):
    from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict
    async def run():
        class IO:
            async def run(self, method, **kwargs):
                return await method(**kwargs)
        lease = SimpleNamespace(cell_id=random_id("cell"), source_exec_attempt_id=random_id("attempt"),
            source_turn_id=random_id("turn"), record_sequence=1)
        candidate = SimpleNamespace(queue_item_id=random_id("queue"))
        host = SimpleNamespace(
            _io=IO(), _queue_wake=asyncio.Event(), _canonical_deadline=lambda: 999999999,
            repository=SimpleNamespace(confirm_cell_output_ingress=AsyncMock(
                return_value=SimpleNamespace(kind=confirmation_kind))),
            _code_mode=SimpleNamespace(acknowledge_output=AsyncMock(), reject_output_delivery=AsyncMock()),
        )
        assert await KernelHostSession._settle_cell_output_failure(
            host, lease, candidate, ConversationKernelConflict('source/content conflict'))
        host._code_mode.acknowledge_output.assert_not_awaited()
        args = host._code_mode.reject_output_delivery.await_args.args
        assert args[0] is lease and args[1].code == 'output_delivery_rejected'
        assert 'source/content conflict' in str(args[1])
    asyncio.run(run())


def test_unknown_output_failure_cannot_be_mistaken_for_rejection_or_ack():
    async def run():
        class IO:
            async def run(self, method, **kwargs):
                return await method(**kwargs)
        lease = object()
        host = SimpleNamespace(
            _io=IO(), _queue_wake=asyncio.Event(), _canonical_deadline=lambda: 999999999,
            repository=SimpleNamespace(confirm_cell_output_ingress=AsyncMock(
                side_effect=[ConnectionError('cannot confirm COMMIT'),
                             SimpleNamespace(kind=PromptIngressConfirmationKind.FULL_COMPATIBLE)])),
            _code_mode=SimpleNamespace(acknowledge_output=AsyncMock(), reject_output_delivery=AsyncMock()),
        )
        with pytest.raises(ConnectionError, match='confirm COMMIT'):
            await KernelHostSession._settle_cell_output_failure(host, lease, object(), ValueError('write interrupted'))
        host._code_mode.acknowledge_output.assert_not_awaited()
        host._code_mode.reject_output_delivery.assert_not_awaited()
        assert await KernelHostSession._settle_cell_output_failure(host, lease, object(), ValueError('write interrupted'))
        host._code_mode.acknowledge_output.assert_awaited_once_with(lease)
        host._code_mode.reject_output_delivery.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('commit_won', [False, True])
def test_stop_after_unconfirmed_write_uses_original_commit_outcome(commit_won):
    from types import MethodType
    async def run():
        lease = CodeModeOutputLease(random_id('cell'), random_id('turn'), random_id('attempt'),
            1, {'kind': 'log', 'text': 'one'}, object(), datetime.now(timezone.utc))
        committed = set()
        pending = [lease]
        stopped = False
        dispositions = []
        calls = []
        class IO:
            async def run(self, method, *args, **kwargs):
                return await method(*args, **kwargs)
        async def confirm(*, candidate, **kwargs):
            return SimpleNamespace(kind=PromptIngressConfirmationKind.FULL_COMPATIBLE
                if candidate.queue_item_id in committed else PromptIngressConfirmationKind.NONE)
        async def enqueue(guard, *, candidate, **kwargs):
            nonlocal stopped
            calls.append(candidate.queue_item_id)
            if commit_won:
                committed.add(candidate.queue_item_id)
            stopped = True
            raise ConnectionError('write result unknown at Cell Stop')
        async def lease_output(_):
            return lease
        async def acknowledge(item, *, accepted=True):
            assert item is lease
            dispositions.append(accepted)
            pending.clear()
            host._closing = True
        host = SimpleNamespace(
            session_id=random_id('session'), host_session_id=random_id('host'),
            workspace=SimpleNamespace(workspace_key=random_id('workspace')),
            _lease=SimpleNamespace(guard=object()), _io=IO(), _closing=False,
            _cell_output_wake=asyncio.Event(), _queue_wake=asyncio.Event(),
            _canonical_deadline=lambda: 999999999999,
            repository=SimpleNamespace(confirm_cell_output_ingress=confirm, enqueue_cell_output=enqueue),
            _code_mode=SimpleNamespace(pending_output_cells=lambda: (lease.cell_id,) if pending else (),
                lease_output=lease_output, acknowledge_output=acknowledge,
                output_delivery_allowed=lambda _: not stopped,
                reject_output_delivery=AsyncMock()),
        )
        host._settle_cell_output_failure = MethodType(KernelHostSession._settle_cell_output_failure, host)
        host._cell_output_wake.set()
        await asyncio.wait_for(KernelHostSession._cell_output_delivery_loop(host), 2)
        assert len(calls) == 1
        assert dispositions == [commit_won]
        assert len(committed) == int(commit_won)
        host._code_mode.reject_output_delivery.assert_not_awaited()
    asyncio.run(run())
