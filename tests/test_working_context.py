from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

import asyncio
from pathlib import Path
from uuid import uuid4


from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from tests.support.model_stream import (
    make_text_block_segment_event,
    make_tool_call_start_event,
)

from pulsara_agent.event import (
    EventContext,
    ReplyEndEvent,
    RunEndEvent,
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
from pulsara_agent.runtime.state import RunActivationWorkingState
from pulsara_agent.replay.timeline import build_run_timeline
from pulsara_agent.memory.foundation.run_timeline_query import summarize_run_timeline
from pulsara_agent.memory.foundation.run_timeline_query import RunTimelineSummary
from pulsara_agent.settings import StorageConfig
from tests.conftest import tool_result_end_contract_fields
from tests.support import memory_hook_view


def test_working_context_guard_rejects_low_signal_run() -> None:
    ctx = _ctx()
    timeline = build_run_timeline(
        [
            make_text_block_segment_event(
                **ctx.event_fields(), block_id="text:1", delta="ok", sequence=1
            ),
            ReplyEndEvent(
                **ctx.event_fields(),
                sequence=2,
                model_terminal_outcome="completed",
            ),
        ],
        runtime_session_id="runtime:test",
    )

    update = propose_working_context_update(summarize_run_timeline(timeline))

    assert update.should_update is False
    assert update.reason == "low_signal_run"


def test_working_context_guard_rejects_empty_memory_search_run() -> None:
    ctx = _ctx()
    timeline = build_run_timeline(
        [
            make_tool_call_start_event(
                **ctx.event_fields(),
                tool_call_id="call:search",
                tool_call_name="memory_search",
            ),
            ToolResultStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:search",
                tool_call_name="memory_search",
            ),
            ToolResultTextDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:search",
                delta=(
                    '{"status":"empty","results":[],"guidance":"'
                    + ("no canonical match " * 40)
                    + '"}'
                ),
            ),
            ToolResultEndEvent(
                **ctx.event_fields(),
                **tool_result_end_contract_fields(
                    "call:search", tool_name="memory_search"
                ),
                tool_call_id="call:search",
                state=ToolResultState.SUCCESS,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
            ),
            make_text_block_segment_event(
                **ctx.event_fields(),
                block_id="text:1",
                delta=(
                    "I could not find canonical durable memory, so I do not know what happened before. "
                    "Please tell me what you were working on and I will continue from there."
                ),
            ),
            ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"),
        ],
        runtime_session_id="runtime:test",
    )

    update = propose_working_context_update(summarize_run_timeline(timeline))

    assert update.should_update is False
    assert update.reason == "low_signal_run"


def test_working_context_store_upserts_domain_latest() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    project_root = Path("/tmp/pulsara-working-context-test-project")
    domain = MemoryDomainContext(
        memory_domain_id=f"u_{uuid4().hex[:16]}",
        workspace_kind="project",
        stable_project_key=str(project_root),
        workspace_label="test-project",
    )
    store = PostgresWorkingContextStore(
        connection_provider=verified_postgres_provider(dsn)
    )
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
        assert latest.workspace_key == project_root.resolve().as_posix()
    finally:
        _delete_working_context(dsn, domain.memory_domain_id)


def test_durable_hook_does_not_reconstruct_working_context_without_projection() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    domain = MemoryDomainContext(
        memory_domain_id=f"u_{uuid4().hex[:16]}", workspace_kind="transient"
    )
    store = PostgresWorkingContextStore(
        connection_provider=verified_postgres_provider(dsn)
    )
    event_log = InMemoryEventLog()
    hooks = DurableMemoryHooks(
        candidate_pool=InMemoryCandidatePool(),
        sink=MemoryProposalSink(),
        event_store=event_log,
        working_context_store=store,
        working_context_domain=domain,
    )
    state = RunActivationWorkingState(session_id="runtime:test")
    ctx = EventContext(
        run_id=state.run_id, turn_id=state.turn_id, reply_id=state.reply_id
    )
    try:
        for event in [
            make_tool_call_start_event(
                **ctx.event_fields(),
                tool_call_id="call:read",
                tool_call_name="read_file",
            ),
            ToolResultStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:read",
                tool_call_name="read_file",
            ),
            ToolResultTextDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:read",
                delta="Read MEMORY_SCOPE_DOMAIN_V1_IMPLEMENTATION.zh.md and verified the scope/domain plan.",
            ),
            ToolResultEndEvent(
                **ctx.event_fields(),
                **tool_result_end_contract_fields("call:read", tool_name="read_file"),
                tool_call_id="call:read",
                state=ToolResultState.SUCCESS,
                metadata={
                    "tool_observation_timing": {"observed_at": "2026-01-01T00:00:00Z"}
                },
            ),
            make_text_block_segment_event(
                **ctx.event_fields(),
                block_id="text:1",
                delta="I inspected the implementation plan and validated the scope/domain wiring.",
            ),
            ReplyEndEvent(**ctx.event_fields(), model_terminal_outcome="completed"),
        ]:
            event_log.append(event)

        asyncio.run(hooks.on_session_end(memory_hook_view(state)))
        projection = asyncio.run(
            hooks.project(memory_hook_view(state), token_budget=120)
        )

        assert store.get_latest(memory_domain_id=domain.memory_domain_id) is None
        # DPJ hard-cut: the hook may consume an already durable timeline
        # projection, but it must not reconstruct one from EventLog callbacks.
        assert projection is None
    finally:
        _delete_working_context(dsn, domain.memory_domain_id)


def test_durable_hook_lazily_refreshes_after_timeline_projection_arrives(
    monkeypatch,
) -> None:
    domain = MemoryDomainContext(
        memory_domain_id="u_lazy_projection",
        workspace_kind="transient",
    )

    class WorkingStore:
        latest = None

        def get_latest(self, *, memory_domain_id):
            assert memory_domain_id == domain.memory_domain_id
            return self.latest

        def upsert(
            self,
            *,
            domain,
            source_session_id,
            source_run_id,
            summary,
            metadata,
            ttl,
        ):
            self.latest = type(
                "Summary",
                (),
                {
                    "summary": summary,
                    "source_run_id": source_run_id,
                    "source_session_id": source_session_id,
                },
            )()
            return self.latest

    event_log = InMemoryEventLog(runtime_session_id="runtime:lazy")
    prior = EventContext("run:prior", "turn:prior", "reply:prior")
    event_log.append(
        RunEndEvent(
            **prior.event_fields(),
            status="finished",
            stop_reason="final",
            terminalization_kind="normal",
        )
    )
    observed: list[tuple[str, str]] = []

    def summarize(**kwargs):
        observed.append((kwargs["runtime_session_id"], kwargs["run_id"]))
        return RunTimelineSummary(
            runtime_session_id=kwargs["runtime_session_id"],
            run_id=kwargs["run_id"],
            status="finished",
            item_count=2,
            assistant_text=(
                "Validated the durable projection worker and recorded a "
                "substantive implementation result for the next turn."
            ),
        )

    monkeypatch.setattr(
        "pulsara_agent.memory.foundation.run_timeline_query."
        "summarize_persisted_run_timeline",
        summarize,
    )
    store = WorkingStore()
    operation_names: list[str] = []

    async def run_owned(operation_name, operation, deadline_monotonic):
        assert deadline_monotonic > 0
        operation_names.append(operation_name)
        return operation()

    hooks = DurableMemoryHooks(
        candidate_pool=InMemoryCandidatePool(),
        sink=MemoryProposalSink(),
        event_store=event_log,
        timeline_graph=object(),
        timeline_archive=object(),
        working_context_store=store,  # type: ignore[arg-type]
        working_context_domain=domain,
        working_context_async_operation_port=run_owned,
    )
    state = RunActivationWorkingState(session_id="runtime:lazy")
    assert hooks.baseline_projection(memory_hook_view(state), token_budget=120) is None
    assert observed == []

    async def exercise() -> None:
        view = memory_hook_view(state)
        assert await hooks.project(view, token_budget=120) is None
        assert await hooks.project(view, token_budget=120) is None

    asyncio.run(exercise())
    assert operation_names == ["working-context-lazy-refresh"]
    assert observed == [("runtime:lazy", "run:prior")]
    assert store.latest is not None
    assert store.latest.source_run_id == "run:prior"


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
    return EventContext(
        run_id=f"run:test:{uuid4().hex}", turn_id="turn:test", reply_id="reply:test"
    )


def _delete_working_context(dsn: str, memory_domain_id: str) -> None:
    del dsn, memory_domain_id
    # The fixture-owned database owns cleanup.
