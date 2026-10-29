"""Active Agent handoff pages derive from canonical requests before LIMIT."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.subagents.collaboration_api import CollaborationToolPort
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import agent_target, work_target
from pulsara_agent.identity import random_id
from tests.test_agent_collaboration_api import RuntimeOwner
from tests.test_agent_requests_persistence import deadline, now, request, start

pytest_plugins = ["tests.test_agent_requests_persistence"]
pytestmark = pytest.mark.postgres


def test_active_filter_precedes_fifty_row_page_and_preserves_idle_origin(source):
    repository, root, admit = source
    identities = sorted(random_id("session") for _ in range(101))
    requests = [request(agent_id=identity) for identity in identities]
    # Waiting on a running Work is queued work even while it cannot start.
    requests[-1] = replace(requests[-1], depends_on=(requests[50].work_id,))
    for offset in range(0, len(requests), 16):
        admit(*requests[offset:offset + 16])
    for item in requests[:50]:
        repository.cancel_agent_requests(
            root.guard, agent_id=item.agent_id, occurred_at=now(), actor_id="test",
            deadline_monotonic=deadline(),
        )
    idle_start = now() - timedelta(hours=2)
    assert repository.mark_agent_idle(
        session_id=identities[0], at=idle_start, deadline_monotonic=deadline(),
    ) == idle_start
    start(repository, requests[50])
    admit(request(agent_id=identities[50], create=False))

    # Retained idle Agents remain visible to the normal query owner.
    ordinary = repository.list_agent_sessions(
        root_session_id=root.guard.session_id, limit=50, deadline_monotonic=deadline(),
    )
    assert [row["agent_id"] for row in ordinary] == identities[:50]
    assert all(row["current_work_id"] is None and row["queued_count"] == 0 for row in ordinary)

    async def scenario():
        io = KernelSessionIO()
        runtime = RuntimeOwner(repository)
        runtime.read_deadline = deadline
        api = CollaborationToolPort(
            repository=repository, root_session_id=root.guard.session_id,
            session_id=root.guard.session_id, runtime=runtime,
            admission_facts=None, io=io,
        )
        try:
            first = await api.freeze_compaction_handoff()
            assert len(first) == 50
            assert [row["agent"]["agent_id"] for row in first] == identities[50:100]
            second = await api.freeze_compaction_handoff(cursor=first[-1]["agent"]["agent_id"])
            assert second == ({"agent": agent_target(identities[-1]),
                               "current_work": None, "queued_count": 1},)
            assert await api.freeze_compaction_handoff(cursor=identities[-1]) == ()
            assert first[0] == {"agent": agent_target(identities[50]),
                                "current_work": work_target(requests[50].work_id),
                                "queued_count": 1}
        finally:
            await io.aclose(deadline_monotonic=deadline())

    asyncio.run(scenario())
    waiting = repository.read_agent_runtime_state(
        session_id=identities[-1], deadline_monotonic=deadline(),
    )
    assert waiting["head_ready"] is False
    assert waiting["has_pending_work"] is True
    assert repository.read_agent_runtime_state(
        session_id=identities[0], deadline_monotonic=deadline(),
    )["idle_since"] == idle_start
    assert repository.list_active_agent_sessions(
        root_session_id=random_id("session"), deadline_monotonic=deadline(),
    ) == ()
