"""Desktop sidebar uses the canonical ROOT-scoped Agent page."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.conversation_kernel.subagents.names import SCIENTIST_NAMES
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_agent_requests_persistence import deadline, finish, now, request, start
from tests.test_agent_requests_persistence import source as source

pytestmark = pytest.mark.postgres


def test_sidebar_agents_reads_root_inventory_across_pages(source):
    repository, root, admit = source
    members = sorted((request() for _ in range(51)), key=lambda item: item.agent_id)
    for offset in range(0, len(members), 16):
        admit(*members[offset:offset + 16])

    async def resources():
        return repository

    controller = LocalSessionController.__new__(LocalSessionController)
    controller.core = SimpleNamespace(
        _ensure_resources=resources,
        _canonical_deadline=deadline,
        _live_agent_session=lambda _: None,
    )
    controller.memory_domain_id = "u_local"

    async def read_pages():
        first = await controller.sidebar_agents(root.guard.session_id, {"max_items": 50})
        second = await controller.sidebar_agents(root.guard.session_id, {
            "max_items": 50, "cursor": first["next_cursor"],
        })
        assert [row["agent_id"] for row in first["agents"]] == [
            item.agent_id for item in members[:50]
        ]
        assert first["next_cursor"] == members[49].agent_id
        assert [row["agent_id"] for row in second["agents"]] == [members[50].agent_id]
        assert second["next_cursor"] is None

    asyncio.run(read_pages())


def test_sidebar_projects_current_work_separately_from_queued_and_stopped_titles(source):
    repository, root, admit = source
    first = request(work_title="运行中的调查")
    queued = request(agent_id=first.agent_id, create=False, work_title="等待中的复审")
    stopped = request(agent_id=first.agent_id, create=False, work_title="未开始即停止")
    admit(first, queued, stopped)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=deadline(),
    ) as connection:
        agent_title = connection.execute(
            "SELECT title FROM pulsara_v3.sessions WHERE id=%s", (first.agent_id,),
        ).fetchone()[0]
    assert agent_title in SCIENTIST_NAMES

    async def resources():
        return repository

    controller = LocalSessionController.__new__(LocalSessionController)
    controller.core = SimpleNamespace(
        _ensure_resources=resources, _canonical_deadline=deadline,
        _live_agent_session=lambda _: None,
    )
    controller.memory_domain_id = "u_local"

    async def scenario():
        page = await controller.sidebar_agents(root.guard.session_id, {})
        assert page["agents"] == [{
            "agent_id": first.agent_id, "title": agent_title, "current_work": None,
            "queued_work_count": 3, "has_running_turn": False, "host_session_id": None,
        }]
        child, candidate = start(repository, first)
        page = await controller.sidebar_agents(root.guard.session_id, {})
        assert page["agents"][0]["current_work"] == {
            "work_id": first.work_id, "work_title": "运行中的调查", "status": "running",
        }
        assert page["agents"][0]["queued_work_count"] == 2
        assert page["agents"][0]["title"] == agent_title
        assert page["agents"][0]["has_running_turn"] is True
        repository.cancel_agent_requests(root.guard, agent_id=first.agent_id, work_id=stopped.work_id,
                                         occurred_at=now(), actor_id="test", deadline_monotonic=deadline())
        stopped_card = (await controller.sidebar_work(root.guard.session_id, stopped.work_id))["work"]
        assert stopped_card["work_title"] == "未开始即停止"
        assert stopped_card["status"] == "stopped"
        assert stopped_card["turn_id"] is None and stopped_card["consumed_entry_id"] is None
        finish(repository, child, candidate, "DONE")
        page = await controller.sidebar_agents(root.guard.session_id, {})
        assert page["agents"][0]["current_work"] is None
        assert page["agents"][0]["queued_work_count"] == 1
        assert page["agents"][0]["title"] == agent_title
        assert page["agents"][0]["has_running_turn"] is False
        assert (await controller.sidebar_work(root.guard.session_id, first.work_id))["work"]["work_title"] == "运行中的调查"
    asyncio.run(scenario())
