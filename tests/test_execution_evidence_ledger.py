from pulsara_agent.event import (
    EventContext,
    InMemoryEventLog,
    ReplyStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.graph import InMemoryGraphStore
from pulsara_agent.memory.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.message import ToolResultState
from pulsara_agent.ontology import memory


def build_ledger() -> ExecutionEvidenceLedger:
    return ExecutionEvidenceLedger(
        graph=InMemoryGraphStore(),
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
    )


def build_event_ledger(event_log: InMemoryEventLog) -> ExecutionEvidenceLedger:
    return ExecutionEvidenceLedger(
        graph=InMemoryGraphStore(),
        archive=InMemoryArchiveStore(),
        gate=MemoryWriteGate(),
        event_log=event_log,
    )


def test_tool_result_creates_ledger_nodes() -> None:
    ledger = build_ledger()

    result = ledger.record_tool_result(
        turn_id="turn:test/001",
        tool_name="read_file",
        status=memory.ToolExecutionStatus.SUCCESS,
        input_summary="Read README",
        output="Pulsara README",
        scope="ctx:test",
    )

    assert ledger.graph.get_jsonld(result.tool_result_id)["@type"] == [memory.TOOL_RESULT.name]
    assert ledger.graph.get_jsonld("turn:test/001")[memory.PRODUCED.name] == [{"@id": result.tool_result_id}]
    assert result.artifact_id is None


def test_turn_can_produce_multiple_tool_results() -> None:
    ledger = build_ledger()

    first = ledger.record_tool_result(
        turn_id="turn:test/multi",
        tool_name="read_file",
        status=memory.ToolExecutionStatus.SUCCESS,
        input_summary="Read pyproject",
        output="pyproject content",
        scope="ctx:test",
    )
    second = ledger.record_tool_result(
        turn_id="turn:test/multi",
        tool_name="read_file",
        status=memory.ToolExecutionStatus.SUCCESS,
        input_summary="Read README",
        output="README content",
        scope="ctx:test",
    )

    assert ledger.graph.get_jsonld("turn:test/multi")[memory.PRODUCED.name] == [
        {"@id": first.tool_result_id},
        {"@id": second.tool_result_id},
    ]


def test_large_tool_result_creates_artifact() -> None:
    ledger = build_ledger()
    output = "x" * 2_100

    result = ledger.record_tool_result(
        turn_id="turn:test/002",
        tool_name="search_files",
        status=memory.ToolExecutionStatus.SUCCESS,
        input_summary="Search",
        output=output,
        scope="ctx:test",
    )

    assert result.artifact_id is not None
    artifact = ledger.graph.get_jsonld(result.artifact_id)
    assert artifact["@type"] == [memory.ARTIFACT.name]
    assert ledger.archive.get_text(result.artifact_id) == output


def test_evidence_supports_claim() -> None:
    ledger = build_ledger()
    result = ledger.record_tool_result(
        turn_id="turn:test/003",
        tool_name="rg",
        status=memory.ToolExecutionStatus.SUCCESS,
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


def test_tool_result_events_feed_execution_evidence_ledger() -> None:
    event_log = InMemoryEventLog()
    ctx = EventContext(run_id="run:test", turn_id="turn:event/001", reply_id="reply:event/001")
    event_log.extend(
        [
            ReplyStartEvent(**ctx.event_fields(), name="assistant"),
            ToolResultStartEvent(
                **ctx.event_fields(),
                tool_call_id="call:read",
                tool_call_name="read_file",
            ),
            ToolResultTextDeltaEvent(
                **ctx.event_fields(),
                tool_call_id="call:read",
                delta="file content",
            ),
            ToolResultEndEvent(
                **ctx.event_fields(),
                tool_call_id="call:read",
                state=ToolResultState.SUCCESS,
            ),
        ]
    )
    ledger = build_event_ledger(event_log)

    result = ledger.record_tool_result_from_events(
        reply_id="reply:event/001",
        tool_call_id="call:read",
        input_summary="Read file",
        scope="ctx:event",
    )

    assert result.status is memory.ToolExecutionStatus.SUCCESS
    tool_result = ledger.graph.get_jsonld(result.tool_result_id)
    assert tool_result[memory.TOOL_NAME.name] == "read_file"
    assert tool_result[memory.OUTPUT_SUMMARY.name] == "file content"
    assert ledger.graph.get_jsonld("turn:event/001")[memory.PRODUCED.name] == [
        {"@id": result.tool_result_id}
    ]
