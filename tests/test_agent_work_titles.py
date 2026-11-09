"""Work-owned titles survive FIFO, terminal state and cold canonical reads."""
import asyncio
from dataclasses import replace

import pytest
from psycopg.errors import CheckViolation
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.conversation_kernel.subagents.names import SCIENTIST_NAMES
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.llm.model_connections import model_call_binding_from_dict
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_collaboration_postgres import api_source as api_source, call
from tests.support.model_config import enqueue_test_prompt, start_test_root_turn, test_model_binding, test_model_runtime
from tests.test_agent_requests_persistence import deadline, finish, now, request, source as source, start

pytestmark = pytest.mark.postgres


def _state(repository, agent_id):
    return repository.read_agent_runtime_state(session_id=agent_id, deadline_monotonic=deadline())


def _confirmation(repository, req):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=deadline(),
    ) as connection:
        row = connection.execute(
            "SELECT t.*,q.source_tool_attempt_id FROM pulsara_v3.prompt_queue_items q "
            "JOIN pulsara_v3.turns t ON t.session_id=q.source_session_id AND t.id=q.source_turn_id "
            "WHERE q.id=%s", (req.work_id,),
        ).fetchone()
        return dict(source_turn_id=row['id'], source_tool_attempt_id=row['source_tool_attempt_id'],
                    model_call_binding=model_call_binding_from_dict(row['model_call_binding']),
                    permission_snapshot=repository._permission_from_row(row), deadline_monotonic=deadline())


def test_work_title_tracks_each_fifo_work_and_survives_stop_and_reuse(source):
    repository, root, admit = source
    first = request(work_title='原始调查', instruction='Only this instruction reaches the worker.')
    assert admit(first)[0]['work_title'] == first.work_title
    second = request(agent_id=first.agent_id, create=False, work_title='后续复审')
    third = request(agent_id=first.agent_id, create=False)
    assert [row['work_title'] for row in admit(second, third)] == ['后续复审', None]
    queued = _state(repository, first.agent_id)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row, deadline_monotonic=deadline(),
    ) as connection:
        agent_title = connection.execute(
            "SELECT title FROM pulsara_v3.sessions WHERE id=%s", (first.agent_id,),
        ).fetchone()['title']
    assert agent_title in SCIENTIST_NAMES
    assert queued['title'] == agent_title
    assert queued['ui_current_work_id'] is None and queued['ui_queued_work_count'] == 3
    lease, candidate = start(repository, first)
    active = _state(repository, first.agent_id)
    assert (active['ui_current_work_id'], active['ui_current_work_title'], active['ui_current_work_status']) == (
        first.work_id, first.work_title, 'running')
    assert active['has_running_turn'] and active['ui_queued_work_count'] == 2
    assert active['title'] == agent_title
    work = repository.query_agent_work(root_session_id=root.guard.session_id, work_id=first.work_id,
                                       deadline_monotonic=deadline())
    assert work['instruction'] == first.instruction
    assert work['work_title'] == first.work_title
    finish(repository, lease, candidate, 'FIRST_FINISHED')
    repository.release_host_writer(lease.guard, deadline_monotonic=deadline())
    lease, candidate = start(repository, second)
    active = _state(repository, first.agent_id)
    assert active['ui_current_work_id'] == second.work_id
    assert active['ui_current_work_title'] == second.work_title
    assert active['ui_queued_work_count'] == 1 and active['title'] == agent_title
    # A queued Work stopped before any turn still owns its nullable title.
    stopped = repository.cancel_agent_requests(root.guard, agent_id=first.agent_id,
        work_id=third.work_id, occurred_at=now(), actor_id='test', deadline_monotonic=deadline())
    assert stopped[0]['work_title'] is None and stopped[0]['status'] == 'stopped'
    facts = repository.read_agent_work_graph_facts(root_session_id=root.guard.session_id,
        work_id=third.work_id, deadline_monotonic=deadline())
    assert facts['work_title'] is None and facts['consumed_entry_id'] is None and facts['turn_id'] is None
    finish(repository, lease, candidate, 'SECOND_FINISHED')
    fourth = request(agent_id=first.agent_id, create=False, work_title=first.work_title)
    assert admit(fourth)[0]['work_id'] != first.work_id
    rows = repository.list_agent_works(root_session_id=root.guard.session_id, agent_id=first.agent_id,
                                       limit=10, deadline_monotonic=deadline())
    assert [(row['work_id'], row['work_title'], row['status']) for row in rows] == [
        (first.work_id, '原始调查', 'completed'), (second.work_id, '后续复审', 'completed'),
        (third.work_id, None, 'stopped'), (fourth.work_id, '原始调查', 'queued'),
    ]
    terminal = _state(repository, first.agent_id)
    assert terminal['ui_current_work_id'] is None and terminal['title'] == agent_title


def test_confirmation_compares_title_for_creation_and_reuse_without_title_dedup(source):
    repository, root, admit = source
    first = request(work_title='界' * 85 + 'a')  # Exact inherited 256 UTF-8 byte field boundary.
    second = request(agent_id=first.agent_id, create=False, work_title=first.work_title)
    admit(first, second)
    facts = _confirmation(repository, first)
    assert [row['work_title'] for row in repository.confirm_agent_requests(
        root.guard, requests=(first, second), **facts)] == [first.work_title, second.work_title]
    for changed in ((replace(first, work_title=None), second),
                    (first, replace(second, work_title='different'))):
        with pytest.raises(ConversationKernelConflict, match='differs from its prepared'):
            repository.confirm_agent_requests(root.guard, requests=changed, **facts)
    third = request(agent_id=first.agent_id, create=False, work_title=first.work_title)
    assert admit(third)[0]['work_id'] not in {first.work_id, second.work_id}
    for req in (first, second, third):
        assert repository.read_agent_work_graph_facts(root_session_id=root.guard.session_id,
            work_id=req.work_id, deadline_monotonic=deadline())['work_title'] == first.work_title


def test_accepted_work_title_is_immutable_in_canonical_database(source):
    repository, root, admit = source
    req = request(work_title='已接受标题')
    admit(req)
    with pytest.raises(CheckViolation, match='accepted Work title is immutable'):
        with repository._writer_transaction(root.guard, deadline_monotonic=deadline()) as connection:
            connection.execute('UPDATE pulsara_v3.prompt_queue_items SET work_title=%s WHERE id=%s',
                               ('覆盖旧标题', req.work_id))
    assert repository.query_agent_work(root_session_id=root.guard.session_id, work_id=req.work_id,
                                       deadline_monotonic=deadline())['work_title'] == req.work_title
    enqueue_test_prompt(repository, root.guard, command_id=random_id('command'), queue_item_id=random_id('queue'),
        client_submission_id=random_id('command'), delivery_mode=PromptDeliveryMode.NEW_TURN, target_turn_id=None,
        permission_snapshot_id=random_id('permission'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        model_call_binding=test_model_binding(test_model_runtime()), content=FrozenPromptContent.text('Ordinary input'),
        occurred_at=now(), actor_id='test', deadline_monotonic=deadline())
    with pytest.raises(CheckViolation):
        with repository._writer_transaction(root.guard, deadline_monotonic=deadline()) as connection:
            connection.execute("UPDATE pulsara_v3.prompt_queue_items SET work_title='not a Work' "
                               "WHERE session_id=%s AND input_origin='HUMAN_MESSAGE'", (root.guard.session_id,))


def test_title_never_introduces_a_teammate_or_changes_instruction(source):
    repository, root, admit = source
    teammate = request()
    receiver = request(work_title=teammate.agent_id)
    admit(teammate, receiver)
    with repository.connection_provider.connection(lane=PostgresConnectionLane.INSPECTOR,
                                                   deadline_monotonic=deadline()) as connection:
        assert connection.execute('SELECT 1 FROM pulsara_v3.agent_known_sessions '
            'WHERE observer_session_id=%s AND known_session_id=%s',
            (receiver.agent_id, teammate.agent_id)).fetchone() is None
    work = repository.query_agent_work(root_session_id=root.guard.session_id, work_id=receiver.work_id,
                                       deadline_monotonic=deadline())
    assert work['instruction'] == receiver.instruction and work['work_title'] == teammate.agent_id


@pytest.mark.parametrize('active_work', [True, False], ids=['delegated-work', 'ordinary-turn'])
def test_message_activity_does_not_create_or_replace_current_work(source, api_source, active_work):
    repository, root, admit = source
    _, _, api, runtime, context = api_source
    child = request(work_title='委派任务')
    admit(child)
    lease, candidate = start(repository, child)
    binding = test_model_binding(test_model_runtime())
    message_turn = candidate.exact_turn_id
    if not active_work:
        finish(repository, lease, candidate, 'WORK_DONE')
        message_turn = random_id('turn')
        start_test_root_turn(repository, lease.guard, command_id=random_id('command'), turn_id=message_turn,
            entry_id=random_id('entry'), context_binding_revision_id=random_id('binding'),
            permission_snapshot_id=random_id('permission'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            model_call_binding=binding, content=FrozenPromptContent.text('Ordinary conversation activity'),
            occurred_at=now(), deadline_monotonic=deadline())
    expected_work_id = child.work_id if active_work else None
    assert _state(repository, child.agent_id)['current_work_id'] == expected_work_id
    message_id = random_id('queue')
    accepted = repository.enqueue_agent_steer(lease.guard, target_turn_id=message_turn, queue_item_id=message_id,
        source_session_id=root.guard.session_id, source_work_id=None, text='An actual Agent message.',
        occurred_at=now(), actor_id='test', deadline_monotonic=deadline())
    assert accepted is not None
    # Materialize the actual incoming message through the existing canonical queue writer.
    with repository._writer_transaction(lease.guard, deadline_monotonic=deadline()) as connection:
        repository._consume_plan_terminal_inputs(connection, lease.guard, turn_id=message_turn,
            workspace_id=_state(repository, child.agent_id)['workspace_id'])
    target = {'kind': 'AGENT', 'agent_id': child.agent_id}
    idle_arguments = {'agents': [target], 'until': 'idle', 'timeout_seconds': 0}
    busy = dict(state='timeout', until='idle',
        observations=[dict(target=target, condition='pending')], pending_targets=[target])
    consumed = _state(repository, child.agent_id)
    assert consumed['current_work_id'] == expected_work_id and consumed['has_running_turn']
    assert not consumed['has_pending_work'] and consumed['queued_count'] == 0
    assert asyncio.run(call(api, context, 'wait_agent', idle_arguments)) == busy
    enqueue_test_prompt(repository, lease.guard, command_id=random_id('command'), queue_item_id=random_id('queue'),
        client_submission_id=random_id('command'), delivery_mode=PromptDeliveryMode.NEW_TURN, target_turn_id=None,
        permission_snapshot_id=random_id('permission'), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        model_call_binding=binding, content=FrozenPromptContent.text('Queued ordinary input'),
        occurred_at=now(), actor_id='test', deadline_monotonic=deadline())
    following = request(agent_id=child.agent_id, create=False, work_title='尚未运行')
    admit(following)
    state = _state(repository, child.agent_id)
    assert state['has_running_turn']
    assert state['current_work_id'] == expected_work_id
    assert state['queued_count'] == 2
    assert (state['ui_current_work_id'], state['ui_current_work_title'], state['ui_current_work_status']) == (
        (child.work_id, child.work_title, 'running') if active_work else (None, None, None))
    assert state['ui_queued_work_count'] == 1
    assert repository.query_agent_work(root_session_id=root.guard.session_id, work_id=message_id,
        deadline_monotonic=deadline()) is None
    snapshot, = repository.read_agent_wait_states(root_session_id=root.guard.session_id,
        agent_ids=(child.agent_id,), bound_work_ids=(expected_work_id,), deadline_monotonic=deadline())
    assert snapshot == dict(agent_id=child.agent_id, current_work_id=expected_work_id,
        has_running_turn=True, has_pending_work=True, bound_work_id=expected_work_id,
        bound_work_status='running' if active_work else None)
    listed = repository.list_query_agent_sessions(root_session_id=root.guard.session_id,
        observer_session_id=root.guard.session_id, limit=10, deadline_monotonic=deadline())
    actual = next(row for row in listed if row['agent_id'] == child.agent_id)
    assert actual['has_running_turn'] and actual['ui_current_work_id'] == expected_work_id
    assert actual['ui_queued_work_count'] == 1
    assert asyncio.run(call(api, context, 'wait_agent', idle_arguments)) == busy
    assert runtime.suspensions == 0
