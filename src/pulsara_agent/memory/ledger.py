"""Execution Evidence Ledger MVP."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pulsara_agent.event import InMemoryEventLog
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.archive import InMemoryArchiveStore
from pulsara_agent.memory.entities import Artifact, Claim, Evidence, ToolResult, Turn
from pulsara_agent.memory.graph import InMemoryGraphStore
from pulsara_agent.memory.records import ClaimRecord, EvidenceRecord, ToolResultRecord
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.message import DataBlock, TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.ontology import memory


LARGE_OUTPUT_THRESHOLD = 2_000


@dataclass(slots=True)
class ExecutionEvidenceLedger:
    graph: InMemoryGraphStore
    archive: InMemoryArchiveStore
    gate: MemoryWriteGate
    event_log: InMemoryEventLog | None = None

    def record_tool_result(
        self,
        *,
        turn_id: str,
        tool_name: str,
        status: memory.ToolExecutionStatus,
        input_summary: str,
        output: str,
        scope: str,
    ) -> ToolResultRecord:
        _assert_enum(status, memory.ToolExecutionStatus)
        tool_result_id = f"tool-result:{uuid4()}"
        artifact_id: str | None = None
        output_preview = _make_output_preview(output)

        if len(output) > LARGE_OUTPUT_THRESHOLD:
            artifact_id = f"artifact:{uuid4()}"
            blob = self.archive.put_text(artifact_id, output)
            self.graph.put_jsonld(
                Artifact(
                    id=artifact_id,
                    stored_at=f"archive://{artifact_id}",
                    digest=blob.digest,
                    summary=output_preview,
                    created_at=utc_now(),
                    scope=scope,
                ).to_jsonld()
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
            ).to_jsonld()
        )

        if self.graph.has_jsonld(turn_id):
            self.graph.add_relation(turn_id, memory.PRODUCED, tool_result_id)
        else:
            self.graph.put_jsonld(
                Turn(
                    id=turn_id,
                    produced=(NodeRef(tool_result_id),),
                    scope=scope,
                    updated_at=utc_now(),
                ).to_jsonld()
            )

        return ToolResultRecord(
            tool_result_id=tool_result_id,
            artifact_id=artifact_id,
            output_summary=output_preview,
            status=status,
        )

    def record_tool_result_from_events(
        self,
        *,
        reply_id: str,
        tool_call_id: str,
        input_summary: str = "",
        scope: str | None = None,
    ) -> ToolResultRecord:
        if self.event_log is None:
            raise ValueError("record_tool_result_from_events requires an event_log")

        events = self.event_log.iter(reply_id=reply_id)
        if not events:
            raise KeyError(f"No events found for reply_id: {reply_id}")

        msg = self.event_log.replay(reply_id)
        block = _find_tool_result_block(msg.content, tool_call_id)
        if block is None:
            raise KeyError(f"No tool result found for tool_call_id: {tool_call_id}")

        return self.record_tool_result(
            turn_id=events[0].turn_id,
            tool_name=block.name,
            status=_to_tool_execution_status(block.state),
            input_summary=input_summary,
            output=_tool_result_output_text(block),
            scope=scope or f"ctx:{events[0].turn_id}",
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
            ).to_jsonld()
        )
        self.graph.add_relation(tool_result_id, memory.PROVIDES, evidence_id)
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
            ).to_jsonld()
        )
        for evidence_id in evidence_ids:
            self.graph.add_relation(evidence_id, memory.SUPPORTS, claim_id)
        return ClaimRecord(
            claim_id=claim_id,
            statement=statement,
            status=decision.status,
            confidence_level=decision.confidence_level,
            verification_status=verification_status,
        )


def _make_output_preview(text: str, limit: int = 500) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _assert_enum(value: object, enum_type: type) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"Expected {enum_type.__name__}, got {type(value).__name__}")


def _find_tool_result_block(blocks: list, tool_call_id: str) -> ToolResultBlock | None:
    for block in blocks:
        if isinstance(block, ToolResultBlock) and block.id == tool_call_id:
            return block
    return None


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
