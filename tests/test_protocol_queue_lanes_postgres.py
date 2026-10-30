"""Current steer observation and future queue pagination retain distinct scopes."""

import pytest

from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
from tests.support.model_config import enqueue_test_prompt
from tests.test_unified_runtime_input_queue import candidate, deadline, enqueue, now

pytest_plugins = ["tests.test_unified_runtime_input_queue"]
pytestmark = pytest.mark.postgres


def test_current_cell_steer_remains_observable_without_counting_as_future_input(source):
    command, future = random_id("command"), random_id("queue")
    enqueue_test_prompt(
        source.repo, source.guard, command_id=command, queue_item_id=future,
        client_submission_id=command, delivery_mode=PromptDeliveryMode.NEW_TURN,
        target_turn_id=None, permission_snapshot_id=random_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=source.binding, content=FrozenPromptContent.text("next task"),
        occurred_at=now(), actor_id="user", deadline_monotonic=deadline(),
    )
    output = candidate(source, 1, "current task supplement")
    accepted = enqueue(source, output)
    assert accepted.delivery_mode is PromptDeliveryMode.STEER_ACTIVE_TURN
    assert accepted.target_turn_id == source.turn

    reader = CanonicalProtocolReader(source.repo.connection_provider)
    snapshot = reader.snapshot(session_id=source.guard.session_id, maximum_entries=128,
        maximum_control_items=128, deadline_monotonic=deadline())
    assert snapshot.control.prompt_queue_total_count == 1
    assert [item.queue_item_id for item in snapshot.control.prompt_queue] == [future, output.queue_item_id]
    steer = snapshot.control.prompt_queue[1]
    assert steer.delivery_mode == "STEER_ACTIVE_TURN"
    assert steer.target_turn_id == source.turn
    assert steer.input_origin == "CELL_OUTPUT"
    assert steer.source_tool_attempt_id == source.attempt

    future_page, cursor = reader.queue_page(session_id=source.guard.session_id,
        memory_domain_id="u_local", root_session_id=None, after_sequence=0,
        limit=128, deadline_monotonic=deadline())
    assert [item.queue_item_id for item in future_page] == [future]
    assert cursor is None


@pytest.mark.parametrize('body_size', [20, 61000])
def test_more_than_128_agent_supplements_have_independent_bounded_lane_pages(source, body_size):
    future_ids = []
    for number in range(3):
        command, future = random_id('command'), random_id('queue')
        enqueue_test_prompt(source.repo, source.guard, command_id=command, queue_item_id=future,
            client_submission_id=command, delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None, permission_snapshot_id=random_id('permission'),
            requested_permission_mode=DEFAULT_PERMISSION_MODE, model_call_binding=source.binding,
            content=FrozenPromptContent.text(f'future {number}'), occurred_at=now(), actor_id='user',
            deadline_monotonic=deadline())
        future_ids.append(future)
    steer_ids = []
    for number in range(129):
        item = random_id('queue')
        accepted = source.repo.enqueue_agent_steer(source.guard, target_turn_id=source.turn,
            queue_item_id=item, source_session_id=source.guard.session_id, source_work_id=None,
            text=f'{number}:' + 'x' * body_size, occurred_at=now(), actor_id='agent',
            deadline_monotonic=deadline())
        assert accepted is not None
        steer_ids.append(item)
    output = candidate(source, 1, 'Cell supplement after Agent messages')
    enqueue(source, output)
    steer_ids.append(output.queue_item_id)
    reader = CanonicalProtocolReader(source.repo.connection_provider)
    snapshot = reader.snapshot(session_id=source.guard.session_id, maximum_entries=128,
        maximum_control_items=128, deadline_monotonic=deadline())
    control = snapshot.control
    assert control.prompt_queue_total_count == 3
    assert control.pending_steer_total_count == 130
    assert control.pending_steer_target_turn_id == source.turn
    assert len(control.prompt_queue) <= 128
    assert [item.queue_item_id for item in control.prompt_queue if item.delivery_mode == 'NEW_TURN'] == future_ids
    selected = [item.queue_item_id for item in control.prompt_queue if item.delivery_mode == 'STEER_ACTIVE_TURN']
    assert 0 < len(selected) <= 128
    assert selected == steer_ids[:len(selected)]
    assert control.HasField('pending_steer_next_cursor')
    assert not control.HasField('prompt_queue_next_cursor')
    cursor = control.pending_steer_next_cursor
    while cursor is not None:
        items, cursor = reader.queue_page(session_id=source.guard.session_id,
            memory_domain_id='u_local', root_session_id=None, after_sequence=cursor,
            limit=128, delivery_mode='STEER_ACTIVE_TURN', target_turn_id=source.turn,
            deadline_monotonic=deadline())
        selected.extend(item.queue_item_id for item in items)
    assert selected == steer_ids

    # Observer control reads use their own existing frame budget. Large inline
    # records leave a continuation, rather than invalidating the connection.
    observation = reader.observe_committed(session_id=source.guard.session_id,
        after_event_sequence=0, maximum_events=128, maximum_bytes=2 << 20,
        deadline_monotonic=deadline())
    assert observation.gap_reason is None
    observed = next(item.current_control for item in observation.projections if item.HasField('current_control'))
    assert observed.prompt_queue_total_count == 3
    assert observed.pending_steer_total_count == 130
    current = [item for item in observed.prompt_queue if item.delivery_mode == 'STEER_ACTIVE_TURN']
    assert current and observed.HasField('pending_steer_next_cursor')
    assert sum(item.ByteSize() for item in observation.projections) <= 2 << 20
    if body_size == 61000:
        small = reader.snapshot(session_id=source.guard.session_id, maximum_entries=1,
            maximum_control_items=128, maximum_serialized_bytes=4096,
            deadline_monotonic=deadline())
        assert small.ByteSize() <= 4096
        assert small.control.pending_steer_total_count == 130
        assert small.control.HasField('pending_steer_next_cursor')
        assert small.control.pending_steer_next_cursor == 0
        first, next_cursor = reader.queue_page(session_id=source.guard.session_id,
            memory_domain_id='u_local', root_session_id=None, after_sequence=0,
            limit=1, delivery_mode='STEER_ACTIVE_TURN', target_turn_id=source.turn,
            deadline_monotonic=deadline())
        assert first[0].queue_item_id == steer_ids[0] and next_cursor is not None
        assert len(first[0].content.inline_content) > body_size
