from pulsara_agent.event import (
    EventContext,
    ReplyStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.graph import InMemoryGraphStore
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.foundation.provenance import RuntimeEventSpan
from pulsara_agent.memory.foundation.records import ArtifactWriteResult
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.message import ToolResultBlock, ToolResultState
from pulsara_agent.ontology import memory, runtime as rt


def build_ledger() -> ExecutionEvidenceLedger:
    return ExecutionEvidenceLedger(
        graph=InMemoryGraphStore(),
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )


def _tool_events(
    *,
    run_id: str,
    turn_id: str,
    reply_id: str,
    tool_call_id: str,
    tool_name: str,
    text: str = "",
    state: ToolResultState = ToolResultState.SUCCESS,
    include_start: bool = True,
    include_end: bool = True,
):
    ctx = EventContext(run_id=run_id, turn_id=turn_id, reply_id=reply_id)
    events = [ReplyStartEvent(**ctx.event_fields(), name="assistant")]
    if include_start:
        events.append(
            ToolResultStartEvent(
                **ctx.event_fields(),
                tool_call_id=tool_call_id,
                tool_call_name=tool_name,
            )
        )
    if text:
        events.append(
            ToolResultTextDeltaEvent(
                **ctx.event_fields(),
                tool_call_id=tool_call_id,
                delta=text,
            )
        )
    if include_end:
        events.append(
            ToolResultEndEvent(
                **ctx.event_fields(),
                tool_call_id=tool_call_id,
                state=state,
            )
        )
    return events


def test_tool_result_creates_ledger_nodes() -> None:
    ledger = build_ledger()

    result = ledger.record_tool_result(
        turn_id="turn:test/001",
        tool_name="read_file",
        status=rt.ToolExecutionStatus.SUCCESS,
        input_summary="Read README",
        output="Pulsara README",
        scope="ctx:test",
    )

    assert ledger.graph.get_jsonld(result.tool_result_id)["@type"] == [rt.TOOL_RESULT.name]
    assert ledger.graph.get_jsonld("turn:test/001")[rt.PRODUCED.name] == [{"@id": result.tool_result_id}]
    assert result.artifact_id is None


def test_turn_can_produce_multiple_tool_results() -> None:
    ledger = build_ledger()

    first = ledger.record_tool_result(
        turn_id="turn:test/multi",
        tool_name="read_file",
        status=rt.ToolExecutionStatus.SUCCESS,
        input_summary="Read pyproject",
        output="pyproject content",
        scope="ctx:test",
    )
    second = ledger.record_tool_result(
        turn_id="turn:test/multi",
        tool_name="read_file",
        status=rt.ToolExecutionStatus.SUCCESS,
        input_summary="Read README",
        output="README content",
        scope="ctx:test",
    )

    assert ledger.graph.get_jsonld("turn:test/multi")[rt.PRODUCED.name] == [
        {"@id": first.tool_result_id},
        {"@id": second.tool_result_id},
    ]


def test_large_tool_result_creates_artifact() -> None:
    ledger = build_ledger()
    output = "x" * 2_100

    result = ledger.record_tool_result(
        turn_id="turn:test/002",
        tool_name="search_files",
        status=rt.ToolExecutionStatus.SUCCESS,
        input_summary="Search",
        output=output,
        scope="ctx:test",
    )

    assert result.artifact_id is not None
    artifact = ledger.graph.get_jsonld(result.artifact_id)
    assert artifact["@type"] == [rt.ARTIFACT.name]
    assert ledger.archive.get_text(result.artifact_id) == output
    assert rt.EVENT_SPAN_PROPERTY.name not in artifact


def test_archive_store_write_result_does_not_expose_content() -> None:
    archive = InMemoryArchiveStore()

    result = archive.put_text("artifact:test", "hello")

    assert isinstance(result, ArtifactWriteResult)
    assert result.id == "artifact:test"
    assert result.artifact_id == "artifact:test"
    assert result.stored_at == "archive://artifact:test"
    assert result.size_bytes == 5
    assert result.digest.startswith("sha256:")
    assert not hasattr(result, "content")
    assert archive.get_text("artifact:test") == "hello"


def test_record_tool_result_block_does_not_create_evidence_or_claim() -> None:
    ledger = build_ledger()

    record = ledger.record_tool_result_block(
        turn_id="turn:test/block",
        block=ToolResultBlock(
            id="call:block",
            name="read_file",
            output=[],
            state=ToolResultState.SUCCESS,
        ),
        input_summary='{"path":"README.md"}',
        scope="ctx:test",
    )

    assert ledger.graph.find_by_type(rt.TOOL_RESULT)
    assert ledger.graph.find_by_type(rt.EVIDENCE) == []
    assert ledger.graph.find_by_type(memory.CLAIM) == []
    assert record.status is rt.ToolExecutionStatus.SUCCESS


def test_evidence_supports_claim() -> None:
    ledger = build_ledger()
    result = ledger.record_tool_result(
        turn_id="turn:test/003",
        tool_name="rg",
        status=rt.ToolExecutionStatus.SUCCESS,
        input_summary="Search JSON-LD",
        output="Found JSON-LD flattening.",
        scope="ctx:test",
    )
    evidence = ledger.create_evidence_from_tool_result(
        result.tool_result_id,
        statement="The result mentions JSON-LD flattening.",
        scope="ctx:test",
    )

    claim = ledger.submit_claim(
        statement="The implementation needs JSON-LD semantic preservation.",
        scope="ctx:test",
        evidence_ids=[evidence.evidence_id],
        source_authority=memory.SourceAuthority.TOOL_RESULT,
        verification_status=memory.VerificationStatus.TOOL_VERIFIED,
    )

    assert claim.status is memory.NodeStatus.ACTIVE
    assert claim.confidence_level is memory.ConfidenceLevel.HIGH
    assert ledger.graph.get_jsonld(evidence.evidence_id)[memory.SUPPORTS.name] == [{"@id": claim.claim_id}]


def test_record_tool_result_from_event_slice_uses_only_current_slice() -> None:
    ledger = build_ledger()
    old_events = InMemoryEventLog().extend(
        _tool_events(
            run_id="run:old",
            turn_id="turn:old",
            reply_id="reply:old",
            tool_call_id="call:read",
            tool_name="read_file",
            text="OLD",
        )
    )
    new_events = InMemoryEventLog().extend(
        _tool_events(
            run_id="run:new",
            turn_id="turn:new",
            reply_id="reply:new",
            tool_call_id="call:read",
            tool_name="read_file",
            text="NEW",
        )
    )

    old_record = ledger.record_tool_result_from_event_slice(old_events, "call:read", session_id="runtime:test")
    new_record = ledger.record_tool_result_from_event_slice(new_events, "call:read", session_id="runtime:test")

    assert ledger.graph.get_jsonld(old_record.tool_result_id)[rt.OUTPUT_SUMMARY.name] == "OLD"
    assert ledger.graph.get_jsonld(new_record.tool_result_id)[rt.OUTPUT_SUMMARY.name] == "NEW"


def test_record_tool_result_from_event_slice_adds_provenance() -> None:
    ledger = build_ledger()
    events = InMemoryEventLog().extend(
        _tool_events(
            run_id="run:event",
            turn_id="turn:event/001",
            reply_id="reply:event/001",
            tool_call_id="call:read",
            tool_name="read_file",
            text="file content",
        )
    )

    result = ledger.record_tool_result_from_event_slice(
        events,
        "call:read",
        input_summary="Read file",
        scope="ctx:event",
        session_id="runtime:test",
    )

    assert result.status is rt.ToolExecutionStatus.SUCCESS
    tool_result = ledger.graph.get_jsonld(result.tool_result_id)
    span = tool_result[rt.EVENT_SPAN_PROPERTY.name]
    assert tool_result[rt.TOOL_NAME.name] == "read_file"
    assert tool_result[rt.OUTPUT_SUMMARY.name] == "file content"
    assert span[rt.SOURCE_SESSION.name] == "runtime:test"
    assert span[rt.SOURCE_RUN.name] == "run:event"
    assert span[rt.SOURCE_TURN.name] == "turn:event/001"
    assert span[rt.SOURCE_REPLY.name] == "reply:event/001"
    assert span[rt.SOURCE_EVENT.name] == {"@id": f"event:{events[1].id}"}
    assert span[rt.START_SEQUENCE.name] <= span[rt.END_SEQUENCE.name]


def test_large_slice_result_provenance_is_copied_to_artifact() -> None:
    ledger = build_ledger()
    payload = "x" * 2_100
    events = InMemoryEventLog().extend(
        _tool_events(
            run_id="run:artifact",
            turn_id="turn:artifact",
            reply_id="reply:artifact",
            tool_call_id="call:artifact",
            tool_name="dump",
            text=payload,
        )
    )

    result = ledger.record_tool_result_from_event_slice(
        events,
        "call:artifact",
        session_id="runtime:test",
    )

    assert result.artifact_id is not None
    artifact = ledger.graph.get_jsonld(result.artifact_id)
    assert artifact[rt.EVENT_SPAN_PROPERTY.name][rt.SOURCE_RUN.name] == "run:artifact"


def test_record_tool_result_from_persisted_event_ref_filters_by_span() -> None:
    event_log = InMemoryEventLog()
    first = event_log.extend(
        _tool_events(
            run_id="run:span",
            turn_id="turn:span",
            reply_id="reply:span",
            tool_call_id="call:read",
            tool_name="read_file",
            text="OLD",
        )
    )
    second = event_log.extend(
        _tool_events(
            run_id="run:span",
            turn_id="turn:span",
            reply_id="reply:span",
            tool_call_id="call:read",
            tool_name="read_file",
            text="NEW",
        )
    )
    ledger = build_ledger()
    span = RuntimeEventSpan(
        session_id="runtime:test",
        run_id="run:span",
        turn_id="turn:span",
        reply_id="reply:span",
        start_sequence=second[1].sequence or 0,
        end_sequence=second[-1].sequence or 0,
        source_event_id=second[1].id,
    )

    result = ledger.record_tool_result_from_persisted_event_ref(
        event_store=event_log,
        event_span=span,
        tool_call_id="call:read",
    )

    assert ledger.graph.get_jsonld(result.tool_result_id)[rt.OUTPUT_SUMMARY.name] == "NEW"
    assert first[1].id != second[1].id


def test_record_tool_result_from_event_slice_merges_text_and_appends_data_blocks() -> None:
    ledger = build_ledger()
    ctx = EventContext(run_id="run:data", turn_id="turn:data", reply_id="reply:data")
    events = InMemoryEventLog().extend(
        [
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            ToolResultStartEvent(**ctx.event_fields(), tool_call_id="call:data", tool_call_name="lookup"),
            ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id="call:data", delta="hello "),
            ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id="call:data", delta="world"),
            ToolResultDataDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:data",
                media_type="text/plain",
                data="Zm9v",
            ),
            ToolResultDataDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:data",
                media_type="text/uri-list",
                url="https://example.com",
            ),
            ToolResultEndEvent(**ctx.event_fields(), tool_call_id="call:data", state=ToolResultState.SUCCESS),
        ]
    )

    result = ledger.record_tool_result_from_event_slice(events, "call:data", session_id="runtime:test")
    doc = ledger.graph.get_jsonld(result.tool_result_id)

    assert doc[rt.OUTPUT_SUMMARY.name].startswith("hello world")
    assert "base64_bytes=4" in doc[rt.OUTPUT_SUMMARY.name]
    assert "url=https://example.com" in doc[rt.OUTPUT_SUMMARY.name]


def test_record_tool_result_from_event_slice_missing_start_raises_key_error() -> None:
    ledger = build_ledger()
    ctx = EventContext(run_id="run:bad", turn_id="turn:bad", reply_id="reply:bad")
    events = InMemoryEventLog().extend(
        [ReplyStartEvent(**ctx.event_fields(), name="assistant")]
    )

    try:
        ledger.record_tool_result_from_event_slice(events, "call:missing", session_id="runtime:test")
    except KeyError as exc:
        assert "call:missing" in str(exc)
    else:
        raise AssertionError("Expected KeyError")


def test_record_tool_result_from_event_slice_delta_without_start_raises_value_error() -> None:
    ledger = build_ledger()
    ctx = EventContext(run_id="run:bad", turn_id="turn:bad", reply_id="reply:bad")
    events = InMemoryEventLog().extend(
        [
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            ToolResultTextDeltaEvent(**ctx.event_fields(), tool_call_id="call:bad", delta="orphan"),
        ]
    )

    try:
        ledger.record_tool_result_from_event_slice(events, "call:bad", session_id="runtime:test")
    except ValueError as exc:
        assert "without start" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_record_tool_result_from_event_slice_start_without_end_raises_value_error() -> None:
    ledger = build_ledger()
    events = InMemoryEventLog().extend(
        _tool_events(
            run_id="run:bad",
            turn_id="turn:bad",
            reply_id="reply:bad",
            tool_call_id="call:bad",
            tool_name="lookup",
            include_end=False,
        )
    )

    try:
        ledger.record_tool_result_from_event_slice(events, "call:bad", session_id="runtime:test")
    except ValueError as exc:
        assert "missing end" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_named_graph_supports_put_get_and_delete() -> None:
    graph = InMemoryGraphStore()
    document = {"@id": "tool-result:1", "@type": ["ToolResult"], "scope": "ctx:test"}

    graph.put_jsonld(document, graph_id="graph:test")
    assert graph.get_jsonld("tool-result:1", graph_id="graph:test")["@id"] == "tool-result:1"
    assert not graph.has_jsonld("tool-result:1")
    assert graph.has_jsonld("tool-result:1", graph_id="graph:test")
    try:
        graph.get_jsonld("tool-result:1")
    except KeyError:
        pass
    else:
        raise AssertionError("Default graph lookup should not search named graphs")
    graph.delete_graph("graph:test")

    try:
        graph.get_jsonld("tool-result:1", graph_id="graph:test")
    except KeyError:
        pass
    else:
        raise AssertionError("Expected deleted graph lookup to fail")


def test_put_jsonld_none_graph_id_writes_to_default_graph() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld(
        {"@id": "claim:none", "@type": [memory.CLAIM.name], "statement": "default via none"},
        graph_id=None,
    )

    assert None not in graph.graphs
    assert "graph:default" in graph.graphs
    assert graph.has_jsonld("claim:none")
    assert graph.get_jsonld("claim:none")["statement"] == "default via none"


def test_empty_graph_id_is_rejected() -> None:
    graph = InMemoryGraphStore()

    try:
        graph.put_jsonld({"@id": "claim:empty", "@type": [memory.CLAIM.name]}, graph_id="")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected empty graph_id to be rejected")


def test_default_graph_lookup_does_not_return_named_graph_duplicate() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld({"@id": "claim:1", "@type": ["Claim"], "statement": "default"})
    graph.put_jsonld({"@id": "claim:1", "@type": ["Claim"], "statement": "named"}, graph_id="graph:named")

    assert graph.get_jsonld("claim:1")["statement"] == "default"
    assert graph.get_jsonld("claim:1", graph_id="graph:named")["statement"] == "named"
    assert graph.has_jsonld("claim:1")
    assert graph.has_jsonld("claim:1", graph_id="graph:named")


def test_default_find_by_type_does_not_scan_named_graphs() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld({"@id": "claim:default", "@type": [memory.CLAIM.name], "statement": "default"})
    graph.put_jsonld(
        {"@id": "claim:named", "@type": [memory.CLAIM.name], "statement": "named"},
        graph_id="graph:named",
    )

    assert [doc["@id"] for doc in graph.find_by_type(memory.CLAIM)] == ["claim:default"]
    assert [doc["@id"] for doc in graph.find_by_type(memory.CLAIM, graph_id="graph:named")] == ["claim:named"]


def test_find_by_type_returns_defensive_copies() -> None:
    graph = InMemoryGraphStore()
    graph.put_jsonld({"@id": "claim:copy", "@type": [memory.CLAIM.name], "statement": "original"})

    docs = graph.find_by_type(memory.CLAIM)
    docs[0]["statement"] = "mutated"

    assert graph.get_jsonld("claim:copy")["statement"] == "original"
