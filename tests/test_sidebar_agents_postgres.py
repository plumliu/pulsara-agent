"""Desktop sidebar uses the canonical ROOT-scoped Agent page."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.desktop_app.session_controller import LocalSessionController
from tests.test_agent_requests_persistence import deadline, request

pytest_plugins = ["tests.test_agent_requests_persistence"]
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
