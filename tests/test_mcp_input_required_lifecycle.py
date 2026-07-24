from __future__ import annotations

from tests.conftest import run_end_contract_fields

from pulsara_agent.event import EventContext, RunEndEvent
from pulsara_agent.primitives.context import (
    ContextEventReferenceFact,
    context_fingerprint,
)
from pulsara_agent.runtime.mcp.lifecycle import (
    McpInputRequiredLifecycleRecord,
    McpInputRequiredLifecycleStore,
)
from tests.support.events import typed_non_transcript_event


def _reference(
    *,
    runtime_session_id: str,
    event_id: str,
    sequence: int,
    event_type: str,
) -> ContextEventReferenceFact:
    return ContextEventReferenceFact(
        runtime_session_id=runtime_session_id,
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        payload_fingerprint=context_fingerprint(
            "test-mcp-lifecycle-reference:v1",
            (event_id, sequence, event_type),
        ),
    )


def test_run_end_accepts_prior_normal_terminal_and_latest_typed_closure() -> None:
    runtime_session_id = "runtime:test:mcp-multiple-interactions"
    run_id = "run:test:mcp-multiple-interactions"
    closure = _reference(
        runtime_session_id=runtime_session_id,
        event_id="mcp-closure:second",
        sequence=8,
        event_type="MCP_INPUT_REQUIRED_INTERACTION_CLOSED",
    )
    store = McpInputRequiredLifecycleStore(
        runtime_session_id=runtime_session_id,
        through_sequence=8,
    )
    store._records = {
        "mcp:first": McpInputRequiredLifecycleRecord(
            interaction_id="mcp:first",
            runtime_session_id=runtime_session_id,
            run_id=run_id,
            turn_id="turn:test",
            reply_id="reply:test",
            tool_call_id="call:first",
            tool_name="mcp__docs__lookup",
            round_count=1,
            status="terminal",
            source_suspension_event_reference=_reference(
                runtime_session_id=runtime_session_id,
                event_id="mcp-suspension:first",
                sequence=2,
                event_type="TOOL_EXECUTION_SUSPENDED",
            ),
            source_suspension_fact_fingerprint=context_fingerprint(
                "test-mcp-suspension:v1",
                "first",
            ),
        ),
        "mcp:second": McpInputRequiredLifecycleRecord(
            interaction_id="mcp:second",
            runtime_session_id=runtime_session_id,
            run_id=run_id,
            turn_id="turn:test",
            reply_id="reply:test",
            tool_call_id="call:second",
            tool_name="mcp__docs__lookup",
            round_count=1,
            status="closed",
            source_suspension_event_reference=_reference(
                runtime_session_id=runtime_session_id,
                event_id="mcp-suspension:second",
                sequence=5,
                event_type="TOOL_EXECUTION_SUSPENDED",
            ),
            source_suspension_fact_fingerprint=context_fingerprint(
                "test-mcp-suspension:v1",
                "second",
            ),
            closure_event_reference=closure,
        ),
    }
    run_end = RunEndEvent(
        **run_end_contract_fields(
            run_id,
            status="aborted",
            abort_kind="host_teardown",
        ),
        **EventContext(
            run_id=run_id,
            turn_id="turn:test",
            reply_id="reply:test",
        ).event_fields(),
        sequence=9,
        status="aborted",
        stop_reason="aborted",
        abort_kind="host_teardown",
        mcp_input_required_closure_event_reference=closure,
    )

    store.apply_committed((run_end,))

    assert store.records() == ()


def test_unrelated_batches_take_constant_space_in_live_mcp_reducer() -> None:
    store = McpInputRequiredLifecycleStore(
        runtime_session_id="runtime:test:mcp-fast-path"
    )
    unrelated = tuple(
        typed_non_transcript_event(
            label=f"mcp-fast-path:{index}",
            sequence=index,
        )
        for index in range(1, 2_001)
    )

    store.apply_committed(unrelated)

    assert store.through_sequence == 2_000
    assert store.records() == ()
    assert store._events_by_id == {}
    assert not hasattr(store, "_history")


def test_inspector_capture_keeps_terminal_snapshot_outside_live_state() -> None:
    runtime_session_id = "runtime:test:mcp-inspector-history"
    run_id = "run:test:mcp-inspector-history"
    record = McpInputRequiredLifecycleRecord(
        interaction_id="mcp:inspection",
        runtime_session_id=runtime_session_id,
        run_id=run_id,
        turn_id="turn:test",
        reply_id="reply:test",
        tool_call_id="call:inspection",
        tool_name="mcp__docs__lookup",
        round_count=1,
        status="terminal",
        source_suspension_event_reference=_reference(
            runtime_session_id=runtime_session_id,
            event_id="mcp-suspension:inspection",
            sequence=2,
            event_type="TOOL_EXECUTION_SUSPENDED",
        ),
        source_suspension_fact_fingerprint=context_fingerprint(
            "test-mcp-suspension:v1",
            "inspection",
        ),
    )
    store = McpInputRequiredLifecycleStore(
        runtime_session_id=runtime_session_id,
        capture_terminal_snapshots=True,
    )
    store._records[record.interaction_id] = record
    store._retire_interaction(record.interaction_id)
    run_end = RunEndEvent(
        **run_end_contract_fields(run_id, status="finished"),
        **EventContext(
            run_id=run_id,
            turn_id="turn:test",
            reply_id="reply:test",
        ).event_fields(),
        sequence=3,
        status="finished",
        stop_reason="final",
    )

    store.apply_committed((run_end,))

    assert store._records == {}
    assert len(store.records()) == 1
    assert store.records()[0].status == "run_ended"
    assert store.records()[0].run_end_event_reference is not None
