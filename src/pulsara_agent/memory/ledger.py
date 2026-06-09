"""Execution Evidence Ledger MVP."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
)
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.entities import Artifact, Claim, Evidence, ToolResult, Turn
from pulsara_agent.memory.protocols import ArtifactStore, RuntimeEventReadStore
from pulsara_agent.memory.provenance import RuntimeEventSpan, runtime_event_span_from_events
from pulsara_agent.memory.records import ClaimRecord, EvidenceRecord, ToolResultRecord
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.message import DataBlock, TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.message.assembler import completed_tool_result_from_events
from pulsara_agent.ontology import memory


LARGE_OUTPUT_THRESHOLD = 2_000


@dataclass(slots=True)
class ExecutionEvidenceLedger:
    graph: GraphStore
    archive: ArtifactStore
    gate: MemoryWriteGate
    graph_id: str = DEFAULT_GRAPH_ID

    def record_tool_result(
        self,
        *,
        turn_id: str,
        tool_name: str,
        status: memory.ToolExecutionStatus,
        input_summary: str,
        output: str,
        scope: str,
        event_span: RuntimeEventSpan | None = None,
    ) -> ToolResultRecord:
        _assert_enum(status, memory.ToolExecutionStatus)
        tool_result_id = f"tool-result:{uuid4()}"
        artifact_id: str | None = None
        output_preview = _make_output_preview(output)

        if len(output) > LARGE_OUTPUT_THRESHOLD:
            artifact_id = f"artifact:{uuid4()}"
            artifact_write = self.archive.put_text(artifact_id, output)
            self.graph.put_jsonld(
                Artifact(
                    id=artifact_id,
                    stored_at=artifact_write.stored_at,
                    digest=artifact_write.digest,
                    summary=output_preview,
                    created_at=utc_now(),
                    scope=scope,
                    event_span=event_span,
                ).to_jsonld(),
                graph_id=self.graph_id,
            )

        self.graph.put_jsonld(
            ToolResult(
                id=tool_result_id,
                tool_name=tool_name,
                status=status,
                input_summary=input_summary,
                output_summary=output_preview,
                truncated=len(output) > len(output_preview),
                scope=scope,
                created_at=utc_now(),
                stored_as=NodeRef(artifact_id) if artifact_id else None,
                event_span=event_span,
            ).to_jsonld(),
            graph_id=self.graph_id,
        )

        self._record_turn_produced(turn_id=turn_id, tool_result_id=tool_result_id, scope=scope)

        return ToolResultRecord(
            tool_result_id=tool_result_id,
            artifact_id=artifact_id,
            output_summary=output_preview,
            status=status,
            event_span=event_span,
        )

    def record_tool_result_block(
        self,
        *,
        turn_id: str,
        block: ToolResultBlock,
        input_summary: str = "",
        scope: str,
        event_span: RuntimeEventSpan | None = None,
    ) -> ToolResultRecord:
        return self.record_tool_result(
            turn_id=turn_id,
            tool_name=block.name,
            status=_to_tool_execution_status(block.state),
            input_summary=input_summary,
            output=_tool_result_output_text(block),
            scope=scope,
            event_span=event_span,
        )

    def record_tool_result_from_event_slice(
        self,
        events: list[AgentEvent],
        tool_call_id: str,
        *,
        input_summary: str = "",
        scope: str | None = None,
        session_id: str = "runtime:unknown",
        event_span: RuntimeEventSpan | None = None,
    ) -> ToolResultRecord:
        block = _tool_result_from_event_slice(events, tool_call_id)
        span = event_span or runtime_event_span_from_events(events, tool_call_id, session_id=session_id)
        return self.record_tool_result_block(
            turn_id=span.turn_id,
            block=block,
            input_summary=input_summary,
            scope=scope or f"ctx:{span.turn_id}",
            event_span=span,
        )

    def record_tool_result_from_persisted_event_ref(
        self,
        *,
        event_store: RuntimeEventReadStore,
        event_span: RuntimeEventSpan,
        tool_call_id: str,
        input_summary: str = "",
        scope: str | None = None,
    ) -> ToolResultRecord:
        events = [
            event
            for event in event_store.iter(
                run_id=event_span.run_id,
                turn_id=event_span.turn_id,
                reply_id=event_span.reply_id,
            )
            if event.sequence is not None
            and event_span.start_sequence <= event.sequence <= event_span.end_sequence
        ]
        return self.record_tool_result_from_event_slice(
            events,
            tool_call_id,
            input_summary=input_summary,
            scope=scope,
            session_id=event_span.session_id,
            event_span=event_span,
        )

    def create_evidence_from_tool_result(
        self,
        tool_result_id: str,
        *,
        statement: str,
        scope: str,
    ) -> EvidenceRecord:
        evidence_id = f"evidence:{uuid4()}"
        self.graph.put_jsonld(
            Evidence(
                id=evidence_id,
                statement=statement,
                source_type=memory.EvidenceSourceType.TOOL_RESULT,
                status=memory.NodeStatus.ACTIVE,
                observed_at=utc_now(),
                scope=scope,
                created_from=NodeRef(tool_result_id),
            ).to_jsonld(),
            graph_id=self.graph_id,
        )
        self._add_relation(tool_result_id, memory.PROVIDES, evidence_id)
        return EvidenceRecord(evidence_id=evidence_id, statement=statement, source_id=tool_result_id)

    def submit_claim(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> ClaimRecord:
        decision = self.gate.evaluate_claim(
            statement=statement,
            evidence_ids=evidence_ids,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        claim_id = f"claim:{uuid4()}"
        self.graph.put_jsonld(
            Claim(
                id=claim_id,
                statement=statement,
                scope=scope,
                status=decision.status,
                confidence_level=decision.confidence_level,
                verification_status=verification_status,
                source_authority=source_authority,
                created_at=utc_now(),
                updated_at=utc_now(),
                gate_reason=decision.reason,
                evidence=tuple(NodeRef(evidence_id) for evidence_id in evidence_ids),
            ).to_jsonld(),
            graph_id=self.graph_id,
        )
        for evidence_id in evidence_ids:
            self._add_relation(evidence_id, memory.SUPPORTS, claim_id)
        return ClaimRecord(
            claim_id=claim_id,
            statement=statement,
            status=decision.status,
            confidence_level=decision.confidence_level,
            verification_status=verification_status,
        )

    def _record_turn_produced(self, *, turn_id: str, tool_result_id: str, scope: str) -> None:
        try:
            document = self.graph.get_jsonld(turn_id, graph_id=self.graph_id)
        except KeyError:
            self.graph.put_jsonld(
                Turn(
                    id=turn_id,
                    produced=(NodeRef(tool_result_id),),
                    scope=scope,
                    updated_at=utc_now(),
                ).to_jsonld(),
                graph_id=self.graph_id,
            )
            return
        values = _as_list(document.get(memory.PRODUCED.name))
        target = {"@id": tool_result_id}
        if target not in values:
            values.append(target)
        document[memory.PRODUCED.name] = values
        document[memory.UPDATED_AT.name] = utc_now()
        self.graph.put_jsonld(document, graph_id=self.graph_id)

    def _add_relation(self, source_id: str, relation, target_id: str) -> None:
        document = self.graph.get_jsonld(source_id, graph_id=self.graph_id)
        values = _as_list(document.get(relation.name))
        target = {"@id": target_id}
        if target not in values:
            values.append(target)
        document[relation.name] = values
        self.graph.put_jsonld(document, graph_id=self.graph_id)


def _make_output_preview(text: str, limit: int = 500) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _assert_enum(value: object, enum_type: type) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"Expected {enum_type.__name__}, got {type(value).__name__}")


def _tool_result_output_text(block: ToolResultBlock) -> str:
    parts: list[str] = []
    for output_block in block.output:
        if isinstance(output_block, TextBlock):
            parts.append(output_block.text)
        elif isinstance(output_block, DataBlock):
            if output_block.source.type == "url":
                parts.append(f"[data:{output_block.source.media_type} url={output_block.source.url}]")
            else:
                parts.append(
                    f"[data:{output_block.source.media_type} base64_bytes={len(output_block.source.data)}]"
                )
    return "\n".join(parts)


def _to_tool_execution_status(state: ToolResultState) -> memory.ToolExecutionStatus:
    if state is ToolResultState.SUCCESS:
        return memory.ToolExecutionStatus.SUCCESS
    if state is ToolResultState.INTERRUPTED:
        return memory.ToolExecutionStatus.CANCELLED
    return memory.ToolExecutionStatus.ERROR


def _tool_result_from_event_slice(events: list[AgentEvent], tool_call_id: str) -> ToolResultBlock:
    return completed_tool_result_from_events(events, tool_call_id)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
