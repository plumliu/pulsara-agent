"""Archive respects delegated work and terminal tool-attempt history."""

import pytest

from pulsara_agent.conversation_kernel.repository_errors import (
    SessionDeletionBusy,
    SessionWriterConflict,
)
from tests.test_agent_requests_persistence import (
    deadline,
    now,
    request,
    source as source,
    start,
)
from tests.test_conversation_fork import rows

pytestmark = pytest.mark.postgres


def test_root_archive_waits_for_child_queue_and_turn_then_ignores_unfinished_attempt_history(source):
    repository, root, admit = source
    work = request()
    admit(work)
    root_id = root.guard.session_id
    root_turn = rows(
        repository,
        "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'",
        (root_id,),
    )[0]["id"]
    assert repository.interrupt_turn(
        root.guard,
        turn_id=root_turn,
        reason="USER_STOPPED",
        actor_id="test",
        occurred_at=now(),
        deadline_monotonic=deadline(),
    )
    attempts = rows(
        repository,
        """SELECT i.source_kind, r.id AS result_id
           FROM pulsara_v3.tool_execution_attempts a
           JOIN pulsara_v3.tool_invocations i ON i.session_id=a.session_id AND i.id=a.invocation_id
           LEFT JOIN pulsara_v3.tool_results r ON r.session_id=a.session_id AND r.attempt_id=a.id
           WHERE a.session_id=%s ORDER BY i.source_kind""",
        (root_id,),
    )
    assert attempts == [
        {"source_kind": "CODE_MODE_CALL", "result_id": None},
        {"source_kind": "MODEL_TOOL_CALL", "result_id": None},
    ]

    def idle():
        return root_id in repository.idle_session_ids(
            memory_domain_id="u_local",
            local_session_ids=(root_id, work.agent_id),
            deadline_monotonic=deadline(),
        )

    def archive(**kwargs):
        return repository.archive_session(
            session_id=root_id,
            memory_domain_id="u_local",
            closed_writer=root.guard,
            deadline_monotonic=deadline(),
            **kwargs,
        )

    # The child has a pending accepted work item even though the root is stopped.
    assert not idle()
    with pytest.raises(SessionDeletionBusy):
        archive(check_only=True)

    child, candidate = start(repository, work)
    assert not idle()
    with pytest.raises(SessionDeletionBusy):
        archive(check_only=True, child_writers=(child.guard,))

    assert repository.interrupt_turn(
        child.guard,
        turn_id=candidate.exact_turn_id,
        reason="USER_STOPPED",
        actor_id="test",
        occurred_at=now(),
        deadline_monotonic=deadline(),
    )
    # The local, gated child can be checked but its live writer is not treated
    # as a closed writer when committing archive.
    assert archive(check_only=True, child_writers=(child.guard,)) == "OPEN"
    with pytest.raises(SessionWriterConflict):
        archive(check_only=True)
    with pytest.raises(SessionWriterConflict):
        archive()
    repository.release_host_writer(child.guard, deadline_monotonic=deadline())
    assert idle()
    assert archive(check_only=True) == "OPEN"
    assert archive() == "ARCHIVED"
