from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pulsara_agent.event import (
    EventContext,
    ProjectionReadyEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RunEndEvent,
    RunStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import PostgresEventLog
from pulsara_agent.inspector import InspectorService, PostgresInspectorStore
from pulsara_agent.memory.candidates.pool import CANDIDATE_POOL_SCHEMA_SQL
from pulsara_agent.message import ToolResultArtifactRef, ToolResultState
from pulsara_agent.settings import StorageConfig
from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL


def _connect_or_skip(dsn: str):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")


def _runtime_session_id() -> str:
    return f"runtime:inspector:{uuid4().hex}"


def _ctx(label: str) -> EventContext:
    unique = uuid4().hex
    return EventContext(
        run_id=f"run:{label}:{unique}",
        turn_id=f"turn:{label}:{unique}",
        reply_id=f"reply:{label}:{unique}",
    )


def _cleanup_session(dsn: str, runtime_session_id: str) -> None:
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))


def _service(dsn: str) -> InspectorService:
    return InspectorService(PostgresInspectorStore(dsn), oxigraph_url=None)


def _simple_run_events(ctx: EventContext, *, user_input: str, text: str):
    return [
        RunStartEvent(**ctx.event_fields(), user_input_chars=len(user_input), metadata={"user_input": user_input}),
        ReplyStartEvent(**ctx.event_fields(), name="assistant"),
        TextBlockStartEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        TextBlockDeltaEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}", delta=text),
        TextBlockEndEvent(**ctx.event_fields(), block_id=f"text:{ctx.run_id}"),
        ReplyEndEvent(**ctx.event_fields()),
        RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
    ]


def test_inspect_run_rebuilds_timeline_and_assistant_reply(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("basic")
        log.extend(_simple_run_events(ctx, user_input="hello", text="PULSARA_INSPECTOR_TEXT"))

        report = _service(dsn).inspect_run(ctx.run_id)

        assert report["inspect_kind"] == "run"
        assert report["session"]["id"] == runtime_session_id
        assert report["run"]["status"] == "finished"
        assert report["canonical"]["current_user_input"] == "hello"
        assert report["timeline"]["status"] == "completed"
        assert "PULSARA_INSPECTOR_TEXT" in report["assistant_replies"][0]["content"][0]["text"]
        assert report["diagnostics"] == []
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_stale_run_projection_without_repairing(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("stale")
        log.extend(_simple_run_events(ctx, user_input="hello", text="done"))
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "update runs set status = 'running', stop_reason = null, completed_at = null where id = %s",
                    (ctx.run_id,),
                )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert any(diagnostic["code"] == "run_projection_stale" for diagnostic in report["diagnostics"])
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select status, completed_at from runs where id = %s", (ctx.run_id,))
                status, completed_at = cursor.fetchone()
        assert status == "running"
        assert completed_at is None
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_prior_messages_are_bounded_to_target_run_start(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        first = _ctx("first")
        target = _ctx("target")
        future = _ctx("future")
        log.extend(_simple_run_events(first, user_input="first user", text="FIRST_ASSISTANT"))
        log.extend(_simple_run_events(target, user_input="target user", text="TARGET_ASSISTANT"))
        log.extend(_simple_run_events(future, user_input="future user", text="FUTURE_ASSISTANT"))

        report = _service(dsn).inspect_run(target.run_id)
        prior_text = str(report["prior_messages_as_seen"])

        assert "first user" in prior_text
        assert "FIRST_ASSISTANT" in prior_text
        assert "target user" not in prior_text
        assert "FUTURE_ASSISTANT" not in prior_text
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_only_projections_seen_by_that_run(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        target = _ctx("target-projection")
        future = _ctx("future-projection")
        log.extend(
            [
                RunStartEvent(**target.event_fields(), user_input_chars=6, metadata={"user_input": "target"}),
                ProjectionReadyEvent(
                    **target.event_fields(),
                    projection_id="projection:target",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    summary="TARGET_PROJECTION_AS_SEEN",
                ),
                ReplyStartEvent(**target.event_fields(), name="assistant"),
                TextBlockStartEvent(**target.event_fields(), block_id="text:target"),
                TextBlockDeltaEvent(**target.event_fields(), block_id="text:target", delta="target done"),
                TextBlockEndEvent(**target.event_fields(), block_id="text:target"),
                ReplyEndEvent(**target.event_fields()),
                RunEndEvent(**target.event_fields(), status="finished", stop_reason="final"),
            ]
        )
        log.extend(
            [
                RunStartEvent(**future.event_fields(), user_input_chars=6, metadata={"user_input": "future"}),
                ProjectionReadyEvent(
                    **future.event_fields(),
                    projection_id="projection:future",
                    role="pro",
                    scope="session",
                    token_budget=100,
                    summary="FUTURE_PROJECTION_NOT_SEEN",
                ),
                RunEndEvent(**future.event_fields(), status="finished", stop_reason="final"),
            ]
        )

        report = _service(dsn).inspect_run(target.run_id)
        projection_text = str(report["projections_as_seen"])

        assert "TARGET_PROJECTION_AS_SEEN" in projection_text
        assert "FUTURE_PROJECTION_NOT_SEEN" not in projection_text
        assert "working_context" not in report
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_reports_missing_artifact_ref(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        ctx = _ctx("missing-artifact")
        call_id = "call:missing-artifact"
        log.extend(
            [
                RunStartEvent(**ctx.event_fields(), user_input_chars=5, metadata={"user_input": "tool"}),
                ToolCallStartEvent(**ctx.event_fields(), tool_call_id=call_id, tool_call_name="read_file"),
                ToolCallEndEvent(**ctx.event_fields(), tool_call_id=call_id),
                ToolResultStartEvent(**ctx.event_fields(), tool_call_id=call_id, tool_call_name="read_file"),
                ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id=call_id, delta="preview"),
                ToolResultEndEvent(
                    **ctx.event_fields(),
                    tool_call_id=call_id,
                    state=ToolResultState.SUCCESS,
                    artifacts=[
                        ToolResultArtifactRef(
                            artifact_id="artifact:missing",
                            role="output",
                            media_type="text/plain",
                            size_bytes=100,
                        )
                    ],
                ),
                RunEndEvent(**ctx.event_fields(), status="finished", stop_reason="final"),
            ]
        )

        report = _service(dsn).inspect_run(ctx.run_id)

        assert any(diagnostic["code"] == "missing_artifact" for diagnostic in report["diagnostics"])
    finally:
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_run_outbox_uses_structured_lineage_not_payload_text(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    runtime_session_id = _runtime_session_id()
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/inspector:{uuid4().hex}"
    runtime_outbox_id = f"outbox:runtime:{uuid4().hex}"
    governed_outbox_id = f"outbox:governed:{uuid4().hex}"
    false_positive_outbox_id = f"outbox:false:{uuid4().hex}"
    governance_batch_id = f"governance:inspector:{uuid4().hex}"
    decision_id = f"decision:inspector:{uuid4().hex}"
    candidate_entry_id = f"pool:inspector:{uuid4().hex}"
    try:
        log = PostgresEventLog(dsn=dsn, runtime_session_id=runtime_session_id, workspace_root=tmp_path)
        target = _ctx("outbox-target")
        other = _ctx("outbox-other")
        log.extend(_simple_run_events(target, user_input="target", text="target done"))
        log.extend(_simple_run_events(other, user_input="other", text="other done"))
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)
                cursor.execute(CANDIDATE_POOL_SCHEMA_SQL)
                cursor.execute(
                    """
                    insert into memory_candidates (
                        entry_id,
                        payload,
                        origin,
                        source_session_id,
                        source_run_id,
                        source_turn_id,
                        source_reply_id
                    )
                    values (%s, %s, 'reflection', %s, %s, %s, %s)
                    """,
                    (
                        candidate_entry_id,
                        Jsonb({"statement": "durable preference from target run"}),
                        runtime_session_id,
                        target.run_id,
                        target.turn_id,
                        target.reply_id,
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_governance_decisions (
                        decision_id,
                        governance_batch_id,
                        decision,
                        write_outcome
                    )
                    values (%s, %s, %s, %s)
                    """,
                    (
                        decision_id,
                        governance_batch_id,
                        Jsonb(
                            {
                                "kind": "submit_as_is",
                                "target_entry_id": candidate_entry_id,
                                "reason": "valid durable memory",
                            }
                        ),
                        Jsonb({"kind": "no_write"}),
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key
                    )
                    values (%s, %s, %s, %s, 'applied', 'runtime_semantic', %s)
                    """,
                    (
                        runtime_outbox_id,
                        graph_id,
                        f"run-timeline:{target.run_id}",
                        Jsonb({"source_run_id": target.run_id, "documents": []}),
                        graph_id,
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        governance_batch_id,
                        decision_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key
                    )
                    values (%s, %s, %s, %s, %s, %s, 'applied', 'governed_memory', %s)
                    """,
                    (
                        governed_outbox_id,
                        graph_id,
                        governance_batch_id,
                        decision_id,
                        candidate_entry_id,
                        Jsonb({"documents": [{"node_id": "mem:governed"}]}),
                        graph_id,
                    ),
                )
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key
                    )
                    values (%s, %s, %s, %s, 'applied', 'runtime_semantic', %s)
                    """,
                    (
                        false_positive_outbox_id,
                        graph_id,
                        "run-timeline:other",
                        Jsonb(
                            {
                                "source_run_id": other.run_id,
                                "documents": [{"statement": f"mentions {target.run_id} only as text"}],
                            }
                        ),
                        graph_id,
                    ),
                )

        report = _service(dsn).inspect_run(target.run_id)
        outbox_ids = {row["outbox_id"] for row in report["outbox"]}

        assert runtime_outbox_id in outbox_ids
        assert governed_outbox_id in outbox_ids
        assert false_positive_outbox_id not in outbox_ids
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from memory_write_outbox where outbox_id = any(%s)",
                    ([runtime_outbox_id, governed_outbox_id, false_positive_outbox_id],),
                )
                cursor.execute("delete from memory_governance_decisions where decision_id = %s", (decision_id,))
        _cleanup_session(dsn, runtime_session_id)


def test_inspect_memory_unknown_id_raises_not_found() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    memory_id = f"mem:inspector-missing:{uuid4().hex}"
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)

    with pytest.raises(KeyError, match=memory_id):
        _service(dsn).inspect_memory(memory_id)


def test_inspect_health_reports_failed_outbox(tmp_path: Path) -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    outbox_id = f"outbox:inspector:{uuid4().hex}"
    graph_id = f"graph:test/inspector:{uuid4().hex}"
    try:
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute(MEMORY_SUBSTRATE_SCHEMA_SQL)
                cursor.execute(
                    """
                    insert into memory_write_outbox (
                        outbox_id,
                        graph_id,
                        target_entry_key,
                        payload,
                        status,
                        mutation_lane,
                        sequence_key,
                        last_error
                    )
                    values (%s, %s, %s, '{}'::jsonb, 'failed', 'runtime_semantic', %s, 'boom')
                    """,
                    (outbox_id, graph_id, f"target:{outbox_id}", graph_id),
                )

        report = _service(dsn).inspect_health()

        assert any(row["status"] == "failed" for row in report["outbox"])
    finally:
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("delete from memory_write_outbox where outbox_id = %s", (outbox_id,))
