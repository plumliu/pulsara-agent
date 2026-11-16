"""Child writer acquisition shares the root session's archive boundary."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pulsara_agent.conversation_kernel.repository_errors import (
    ConversationKernelConflict,
    SessionWriterConflict,
)
from pulsara_agent.identity import random_id
from tests.test_agent_requests_persistence import (
    deadline,
    now,
    request,
    source as source,
)
from tests.test_conversation_fork import rows

pytestmark = pytest.mark.postgres


def _idle_root_with_child(source):
    repository, root, admit = source
    work = request()
    admit(work)
    repository.cancel_agent_requests(
        root.guard,
        agent_id=work.agent_id,
        work_id=work.work_id,
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    root_id = root.guard.session_id
    turn_id = rows(
        repository,
        "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'",
        (root_id,),
    )[0]["id"]
    assert repository.interrupt_turn(
        root.guard,
        turn_id=turn_id,
        reason="USER_STOPPED",
        occurred_at=now(),
        actor_id="test",
        deadline_monotonic=deadline(),
    )
    workspace_id = repository.read_agent_runtime_state(
        session_id=work.agent_id, deadline_monotonic=deadline()
    )["workspace_id"]
    return repository, root, work.agent_id, workspace_id


def _archive(repository, root):
    return repository.archive_session(
        session_id=root.guard.session_id,
        memory_domain_id="u_local",
        closed_writer=root.guard,
        deadline_monotonic=deadline(),
    )


def _acquire(repository, agent_id, workspace_id):
    return repository.acquire_host_writer(
        intent="EXISTING",
        session_id=agent_id,
        workspace_id=workspace_id,
        writer_owner_id=random_id("host"),
        lease_seconds=300,
        deadline_monotonic=deadline(),
    )


def test_child_writer_requires_open_root_and_can_acquire_after_restore(source):
    repository, root, agent_id, workspace_id = _idle_root_with_child(source)
    assert _archive(repository, root) == "ARCHIVED"
    with pytest.raises(ConversationKernelConflict, match="root session is archived"):
        _acquire(repository, agent_id, workspace_id)
    assert repository.unarchive_session(
        session_id=root.guard.session_id,
        memory_domain_id="u_local",
        deadline_monotonic=deadline(),
    ) == "OPEN"
    child = _acquire(repository, agent_id, workspace_id)
    assert child.guard.session_id == agent_id
    repository.release_host_writer(child.guard, deadline_monotonic=deadline())


def test_child_writer_acquisition_serializes_with_root_archive(source):
    repository, root, agent_id, workspace_id = _idle_root_with_child(source)
    ready = Barrier(2)

    def attempt_archive():
        ready.wait(timeout=10)
        try:
            return _archive(repository, root)
        except SessionWriterConflict:
            return "BUSY"

    def attempt_acquire():
        ready.wait(timeout=10)
        try:
            return _acquire(repository, agent_id, workspace_id)
        except ConversationKernelConflict as exc:
            assert "root session is archived" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive = pool.submit(attempt_archive)
        acquire = pool.submit(attempt_acquire)
        archive_result = archive.result(timeout=30)
        child = acquire.result(timeout=30)
    if archive_result == "ARCHIVED":
        assert child is None
    else:
        assert archive_result == "BUSY"
        assert child is not None
        repository.release_host_writer(child.guard, deadline_monotonic=deadline())
        assert _archive(repository, root) == "ARCHIVED"
