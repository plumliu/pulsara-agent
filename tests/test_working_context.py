from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import (
    EventContext,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.memory import InMemoryCandidatePool, MemoryDomainContext
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.hooks.durable import DurableMemoryHooks, _merge_projections
from pulsara_agent.memory.working_context import (
    PostgresWorkingContextStore,
    propose_working_context_update,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.timeline import build_run_timeline
from pulsara_agent.memory.foundation.run_timeline_query import summarize_run_timeline
from pulsara_agent.settings import StorageConfig


def test_working_context_guard_rejects_low_signal_run() -> None:
    ctx = _ctx()
    timeline = build_run_timeline(
        [
            TextBlockDeltaEvent(**ctx.event_fields(), block_id="text:1", delta="ok", sequence=1),
            ReplyEndEvent(**ctx.event_fields(), sequence=2),
        ],
        runtime_session_id="runtime:test",
    )

    update = propose_working_context_update(summarize_run_timeline(timeline))

    assert update.should_update is False
    assert update.reason == "low_signal_run"


def test_working_context_store_upserts_domain_latest() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    domain = MemoryDomainContext(
        memory_domain_id=f"u_{uuid4().hex[:16]}",
        workspace_kind="project",
        stable_project_key="test_project",
        workspace_label="test-project",
    )
    store = PostgresWorkingContextStore(dsn=dsn)
    try:
        first = store.upsert(
            domain=domain,
            source_session_id="runtime:first",
            source_run_id="run:first",
            summary="Recent run used tools: read_file. Key tool result: inspected the package layout.",
        )
        second = store.upsert(
            domain=domain,
            source_session_id="runtime:second",
            source_run_id="run:second",
            summary="Recent run used tools: pytest. Key tool result: validated the recall tests.",
        )

        latest = store.get_latest(memory_domain_id=domain.memory_domain_id)

        assert first.summary_id == second.summary_id
        assert latest is not None
        assert latest.summary == second.summary
        assert latest.workspace_key == "test_project"
    finally:
        _delete_working_context(dsn, domain.memory_domain_id)


def test_durable_hook_injects_and_updates_working_context() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    domain = MemoryDomainContext(memory_domain_id=f"u_{uuid4().hex[:16]}", workspace_kind="transient")
    store = PostgresWorkingContextStore(dsn=dsn)
    event_log = InMemoryEventLog()
    hooks = DurableMemoryHooks(
        candidate_pool=InMemoryCandidatePool(),
        sink=MemoryProposalSink(),
        event_store=event_log,
        working_context_store=store,
        working_context_domain=domain,
    )
    state = LoopState(session_id="runtime:test")
    ctx = EventContext(run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id)
    try:
        for event in [
            ToolCallStartEvent(**ctx.event_fields(), tool_call_id="call:read", tool_call_name="read_file"),
            ToolResultStartEvent(**ctx.event_fields(), tool_call_id="call:read", tool_call_name="read_file"),
            ToolResultTextDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:read",
                delta="Read MEMORY_SCOPE_DOMAIN_V1_IMPLEMENTATION.zh.md and verified the scope/domain plan.",
            ),
            ToolResultEndEvent(**ctx.event_fields(), tool_call_id="call:read", state=ToolResultState.SUCCESS),
            TextBlockDeltaEvent(
                **ctx.event_fields(),
                block_id="text:1",
                delta="I inspected the implementation plan and validated the scope/domain wiring.",
            ),
            ReplyEndEvent(**ctx.event_fields()),
        ]:
            event_log.append(event)

        asyncio.run(hooks.on_session_end(state))
        projection = asyncio.run(hooks.project(state, token_budget=120))

        latest = store.get_latest(memory_domain_id=domain.memory_domain_id)
        assert latest is not None
        assert "Recent run used tools" in latest.summary
        assert projection is not None
        assert "working-context-projection" in projection["summary"]
        assert "do_not_write_back" in projection["summary"]
    finally:
        _delete_working_context(dsn, domain.memory_domain_id)


def test_merge_projection_preserves_mixed_projection_metadata() -> None:
    working_context = {
        "summary": '<working-context-projection do_not_write_back="true">recent activity</working-context-projection>',
        "items": ["recent activity"],
        "included_memory_ids": [],
        "filtered_memory_ids": [],
        "do_not_write_back": True,
        "projection_kind": "working_context",
    }
    recalled = {
        "summary": '<recalled-memory-projection do_not_write_back="true">durable preference</recalled-memory-projection>',
        "items": ["durable preference"],
        "included_memory_ids": ["preference:1"],
        "filtered_memory_ids": ["decision:2"],
        "do_not_write_back": True,
    }

    projection = _merge_projections(working_context, recalled)

    assert projection is not None
    assert projection["projection_kind"] == "mixed"
    assert projection["projection_kinds"] == ["working_context", "recalled_memory"]
    assert "working-context-projection" in projection["summary"]
    assert "recalled-memory-projection" in projection["summary"]
    assert projection["included_memory_ids"] == ["preference:1"]
    assert projection["filtered_memory_ids"] == ["decision:2"]


def _ctx() -> EventContext:
    return EventContext(run_id=f"run:test:{uuid4().hex}", turn_id="turn:test", reply_id="reply:test")


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _delete_working_context(dsn: str, memory_domain_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM working_context_summaries WHERE memory_domain_id = %s",
                (memory_domain_id,),
            )
