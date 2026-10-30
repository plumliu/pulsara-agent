from __future__ import annotations

from io import BytesIO
from time import monotonic

import pytest
from PIL import Image
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.images import bound_test_image
from tests.support.model_config import enqueue_test_prompt
from tests.test_round4_plan_postgres import _commit_plan_batch, _id, _lease, _now, _repository, _start_root

pytestmark = pytest.mark.postgres


def _enqueue(repository, lease, turn, content):
    queue = _id('queue')
    enqueue_test_prompt(
        repository, lease.guard, command_id=_id('command'), queue_item_id=queue,
        client_submission_id=_id('command'), delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
        target_turn_id=turn, permission_snapshot_id=None, requested_permission_mode=None,
        model_call_binding=None, content=content, occurred_at=_now(), actor_id='test',
        deadline_monotonic=monotonic()+30,
    )
    return queue


def _prepared(database):
    repository = _repository(database)
    lease = _lease(repository)
    workspace = repository.read_session_workspace_id(lease.guard, deadline_monotonic=monotonic()+30)
    turn, _ = _start_root(repository, lease)
    prepared = _commit_plan_batch(repository, lease, workspace_id=workspace, turn_id=turn,
        selected_tool_name='enter_plan', selected_arguments={'reason': 'plan'},
        workflow_id=_id('plan'), expected_workflow_revision=None, sibling_before=True)
    return repository, lease, workspace, turn, prepared.candidate


def test_plan_terminal_drains_all_pages_and_image_before_successor(stage2_migrated_postgres_database):
    repo, lease, workspace, turn, candidate = _prepared(stage2_migrated_postgres_database)
    # Synthetic canonical queue for pagination: ingress's human queue limit is
    # independent of the unbounded target lane and must not be bypassed.
    queues = []
    for i in range(129):
        queue = _id('queue')
        repo.enqueue_agent_steer(lease.guard, target_turn_id=turn, queue_item_id=queue,
            source_session_id=lease.guard.session_id, source_work_id=None,
            text=f'message {i}', occurred_at=_now(), actor_id='test',
            deadline_monotonic=monotonic()+30)
        queues.append(queue)
    out = BytesIO()
    Image.new('RGB', (2, 2), (20, 30, 40)).save(out, format='PNG')
    image = bound_test_image('image/png', out.getvalue(), 2, 2, workspace_id=workspace)
    queues.append(_enqueue(repo, lease, turn, FrozenPromptContent((image,))))
    # Commit first, then lose the acknowledgement; confirmation must recover
    # the original atomic Plan winner rather than replay any queue entry.
    def committed_then_lost_ack():
        repo.accept_plan_tool_batch(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+60)
        raise ConnectionError('injected post-commit ACK loss')
    with pytest.raises(ConnectionError, match='ACK loss'):
        committed_then_lost_ack()
    accepted = repo.confirm_plan_tool_batch_winner(candidate=candidate, deadline_monotonic=monotonic()+30)
    assert accepted is not None
    assert repo.accept_plan_tool_batch(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30) == accepted
    cut = repo.prepare_provider_input_cut(lease.guard, turn_id=accepted.continuation_turn_id,
        deadline_monotonic=monotonic()+30)
    history = CanonicalProviderInputReader(repo.connection_provider).read_frozen_dispatch(
        cut, deadline_monotonic=monotonic()+30).compile_snapshot.canonical_input.items
    with repo.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
            deadline_monotonic=monotonic()+30) as c:
        rows = c.execute('''SELECT q.id,q.status,q.consumed_entry_id,e.entry_sequence,e.turn_id,e.input_origin
            FROM pulsara_v3.prompt_queue_items q JOIN pulsara_v3.transcript_entries e
              ON e.session_id=q.session_id AND e.id=q.consumed_entry_id
            WHERE q.session_id=%s AND q.id=ANY(%s) ORDER BY q.queue_sequence''',
            (lease.guard.session_id, queues)).fetchall()
        assert [r['id'] for r in rows] == queues
        assert all(r['status']=='CONSUMED' and r['turn_id']==turn for r in rows)
        assert [r['input_origin'] for r in rows] == ['AGENT_MESSAGE'] * 129 + ['HUMAN_STEER']
        assert c.execute('SELECT count(*) AS n FROM pulsara_v3.canonical_image_refs WHERE transcript_entry_id=%s',
            (rows[-1]['consumed_entry_id'],)).fetchone()['n'] == 1
        successor_seq = c.execute('SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s',
            (accepted.continuation_entry_id,)).fetchone()['entry_sequence']
        assert rows[-1]['entry_sequence'] < successor_seq
        history_ids = [item.source_entry_id for item in history]
        assert all(row['consumed_entry_id'] in history_ids for row in rows)
        assert history_ids.index(rows[-1]['consumed_entry_id']) < history_ids.index(accepted.continuation_entry_id)
        events = c.execute('''SELECT event_type FROM pulsara_v3.agent_events WHERE session_id=%s
            AND event_type IN ('ToolResultAccepted','PromptConsumed','UserSteerAccepted','TurnCompleted','PlanContinuationAccepted')
            ORDER BY event_sequence''', (lease.guard.session_id,)).fetchall()
        kinds = [r['event_type'] for r in events]
        last_result = max(i for i, v in enumerate(kinds) if v == 'ToolResultAccepted')
        consumed = [i for i,v in enumerate(kinds) if v == 'PromptConsumed']
        assert len(consumed)==130 and last_result < consumed[0] < consumed[-1]
        assert kinds[-2:] == ['TurnCompleted', 'PlanContinuationAccepted']
        ordered = c.execute('''SELECT q.id,queued.occurred_at AS queued_at, consumed.occurred_at AS consumed_at
            FROM pulsara_v3.prompt_queue_items q
            JOIN pulsara_v3.agent_events queued ON queued.session_id=q.session_id
              AND queued.subject_queue_item_id=q.id AND queued.event_type='PromptQueued'
            JOIN pulsara_v3.agent_events consumed ON consumed.session_id=q.session_id
              AND consumed.subject_queue_item_id=q.id AND consumed.event_type='PromptConsumed'
            WHERE q.id=ANY(%s)''', (queues,)).fetchall()
        assert len(ordered)==130 and all(r['queued_at'] <= r['consumed_at'] for r in ordered)


def test_plan_terminal_input_failure_rolls_back_everything(stage2_migrated_postgres_database, monkeypatch):
    repo, lease, _, turn, candidate = _prepared(stage2_migrated_postgres_database)
    queues = [_enqueue(repo, lease, turn, FrozenPromptContent.text(f'message {i}')) for i in range(3)]
    original = repo._write_consumed_prompt_steer
    count = 0
    def failing(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('injected input write failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, '_write_consumed_prompt_steer', failing)
    with pytest.raises(RuntimeError, match='injected input write failure'):
        repo.accept_plan_tool_batch(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    with repo.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
            deadline_monotonic=monotonic()+30) as c:
        rows = c.execute('SELECT status,consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE id=ANY(%s)', (queues,)).fetchall()
        assert len(rows)==3 and all(r['status']=='PENDING' and r['consumed_entry_id'] is None for r in rows)
        assert c.execute('SELECT status FROM pulsara_v3.turns WHERE id=%s', (turn,)).fetchone()['status']=='RUNNING'
        assert c.execute('SELECT count(*) AS n FROM pulsara_v3.tool_results WHERE session_id=%s', (lease.guard.session_id,)).fetchone()['n']==0
        assert c.execute('SELECT count(*) AS n FROM pulsara_v3.turns WHERE id=%s', (candidate.continuation_turn_id,)).fetchone()['n']==0
    monkeypatch.setattr(repo, '_write_consumed_prompt_steer', original)
    accepted = repo.accept_plan_tool_batch(lease.guard, candidate=candidate, deadline_monotonic=monotonic()+30)
    assert repo.confirm_plan_tool_batch_winner(candidate=candidate, deadline_monotonic=monotonic()+30)==accepted
