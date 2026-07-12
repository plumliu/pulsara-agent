import asyncio
import os
import time

import pytest

from pulsara_agent.runtime.mcp import (
    McpServerConfig,
    McpRequiredStartupError,
    McpServerStatus,
    McpServerSupervisor,
    McpStreamableHttpConfig,
)


pytestmark = pytest.mark.skipif(
    os.getenv("PULSARA_RUN_REAL_MCP") != "1",
    reason="Set PULSARA_RUN_REAL_MCP=1 to run real remote MCP dogfood tests.",
)


def test_real_langchain_docs_mcp_dogfood() -> None:
    """Exercise a public, no-key streamable HTTP MCP server through the SDK-backed manager."""

    async def run() -> None:
        config = McpServerConfig(
            server_id="langchain-docs",
            transport=McpStreamableHttpConfig(url="https://docs.langchain.com/mcp"),
            required=True,
            connect_timeout_ms=20_000,
            discovery_timeout_ms=20_000,
            startup_deadline_ms=30_000,
            tool_timeout_ms=20_000,
        )
        supervisor = McpServerSupervisor()
        started = time.monotonic()
        ticket = supervisor.prepare((config,), trigger="initial")
        assert time.monotonic() - started < 0.5
        try:
            await supervisor.await_required(ticket)
            batch = supervisor.drain_installable_candidates(
                expected_epoch=ticket.config_epoch
            )
            candidate = batch.candidates[0]
            snapshot = candidate.server_snapshot
            supervisor.commit_slot_transition(
                candidates=(candidate,),
                retiring_slot_ids=(),
            )
            assert snapshot.status is McpServerStatus.READY
            tool_names = {tool.name for tool in snapshot.tools}
            assert "search_docs_by_lang_chain" in tool_names

            assert candidate.manager_slot is not None
            result = await candidate.manager_slot.manager.call_tool(
                "langchain-docs",
                "search_docs_by_lang_chain",
                {"query": "LangChain MCP adapters Python quickstart"},
                timeout_ms=20_000,
            )

            assert result.is_error is False
            assert "LangChain" in result.output
        finally:
            await supervisor.aclose(timeout_seconds=5)
        assert supervisor.lifecycle == "closed"
        assert supervisor.slots() == ()
        assert supervisor._owned_background_tasks == set()

    asyncio.run(run())


def test_real_required_unreachable_mcp_fails_with_bounded_deadline() -> None:
    """Exercise the real SDK failure path without relying on a fake worker."""

    async def run() -> None:
        config = McpServerConfig(
            server_id="unreachable-required",
            transport=McpStreamableHttpConfig(url="http://127.0.0.1:1/mcp"),
            required=True,
            connect_timeout_ms=250,
            discovery_timeout_ms=250,
            startup_deadline_ms=500,
            tool_timeout_ms=250,
        )
        supervisor = McpServerSupervisor()
        started = time.monotonic()
        ticket = supervisor.prepare((config,), trigger="initial")
        try:
            with pytest.raises(McpRequiredStartupError) as captured:
                await supervisor.await_required(ticket)
            assert captured.value.reason_code == "mcp_required_generation_unavailable"
            assert captured.value.server_ids == ("unreachable-required",)
            assert time.monotonic() - started < 2.0
        finally:
            await supervisor.aclose(timeout_seconds=5)
        assert supervisor.lifecycle == "closed"
        assert supervisor._owned_background_tasks == set()

    asyncio.run(run())
