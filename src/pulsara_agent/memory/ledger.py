"""Execution Evidence Ledger MVP."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pulsara_agent.event import (
    AgentEvent,
)
from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.entities.memory import ActionBoundary, Claim, Decision, Observation, Preference
from pulsara_agent.entities.runtime import Artifact, Evidence, ToolResult, Turn
from pulsara_agent.memory.protocols import ArtifactStore, RuntimeEventReadStore
from pulsara_agent.memory.provenance import RuntimeEventSpan, runtime_event_span_from_events
from pulsara_agent.memory.records import ClaimRecord, EvidenceRecord, MemoryWriteRecord, ToolResultRecord
from pulsara_agent.memory.write_gate import MemoryWriteGate
from pulsara_agent.message import DataBlock, TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.message.assembler import completed_tool_result_from_events
from pulsara_agent.ontology import memory, runtime as rt


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
        status: rt.ToolExecutionStatus,
        input_summary: str,
        output: str,
        scope: str,
        event_span: RuntimeEventSpan | None = None,
    ) -> ToolResultRecord:
        _assert_enum(status, rt.ToolExecutionStatus)
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
                source_type=rt.EvidenceSourceType.TOOL_RESULT,
                status=memory.NodeStatus.ACTIVE,
                observed_at=utc_now(),
                scope=scope,
                created_from=NodeRef(tool_result_id),
            ).to_jsonld(),
            graph_id=self.graph_id,
        )
        self._add_relation(tool_result_id, rt.PROVIDES, evidence_id)
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
            scope=scope,
            evidence_ids=evidence_ids,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        self._require_existing_nodes(evidence_ids, role="evidence")
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
            gate_reason=decision.reason,
        )

    def submit_preference(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str] | None = None,
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> MemoryWriteRecord:
        evidence_ids = evidence_ids or []
        decision = self.gate.evaluate_preference(
            statement=statement,
            scope=scope,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        self._require_existing_nodes(evidence_ids, role="evidence")
        preference_id = f"preference:{uuid4()}"
        self.graph.put_jsonld(
            Preference(
                id=preference_id,
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
            self._add_relation(evidence_id, memory.SUPPORTS, preference_id)
        return self._memory_write_record(preference_id, statement, decision, verification_status)

    def submit_action_boundary(
        self,
        *,
        statement: str,
        scope: str,
        applies_when: str,
        do_not_apply_when: str,
        trigger_tools: list[str] | None = None,
        trigger_actions: list[str] | None = None,
        trigger_file_globs: list[str] | None = None,
        trigger_scopes: list[str] | None = None,
        trigger_keywords: list[str] | None = None,
        negative_tools: list[str] | None = None,
        negative_actions: list[str] | None = None,
        negative_file_globs: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> MemoryWriteRecord:
        evidence_ids = evidence_ids or []
        trigger_tools = trigger_tools or []
        trigger_actions = trigger_actions or []
        trigger_file_globs = trigger_file_globs or []
        trigger_scopes = trigger_scopes or []
        trigger_keywords = trigger_keywords or []
        negative_tools = negative_tools or []
        negative_actions = negative_actions or []
        negative_file_globs = negative_file_globs or []
        decision = self.gate.evaluate_action_boundary(
            statement=statement,
            scope=scope,
            applies_when=applies_when,
            do_not_apply_when=do_not_apply_when,
            trigger_tools=trigger_tools,
            trigger_actions=trigger_actions,
            trigger_file_globs=trigger_file_globs,
            trigger_scopes=trigger_scopes,
            trigger_keywords=trigger_keywords,
            negative_tools=negative_tools,
            negative_actions=negative_actions,
            negative_file_globs=negative_file_globs,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        self._require_existing_nodes(evidence_ids, role="evidence")
        boundary_id = f"action-boundary:{uuid4()}"
        self.graph.put_jsonld(
            ActionBoundary(
                id=boundary_id,
                statement=statement,
                scope=scope,
                status=decision.status,
                applies_when=applies_when,
                do_not_apply_when=do_not_apply_when,
                source_authority=source_authority,
                confidence_level=decision.confidence_level,
                verification_status=verification_status,
                created_at=utc_now(),
                updated_at=utc_now(),
                gate_reason=decision.reason,
                evidence=tuple(NodeRef(evidence_id) for evidence_id in evidence_ids),
                trigger_tools=tuple(trigger_tools),
                trigger_actions=tuple(trigger_actions),
                trigger_file_globs=tuple(trigger_file_globs),
                trigger_scopes=tuple(trigger_scopes),
                trigger_keywords=tuple(trigger_keywords),
                negative_tools=tuple(negative_tools),
                negative_actions=tuple(negative_actions),
                negative_file_globs=tuple(negative_file_globs),
            ).to_jsonld(),
            graph_id=self.graph_id,
        )
        for evidence_id in evidence_ids:
            self._add_relation(evidence_id, memory.SUPPORTS, boundary_id)
        return self._memory_write_record(boundary_id, statement, decision, verification_status)

    def submit_observation(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
    ) -> MemoryWriteRecord:
        decision = self.gate.evaluate_observation(
            statement=statement,
            scope=scope,
            evidence_ids=evidence_ids,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        self._require_existing_nodes(evidence_ids, role="evidence")
        observation_id = f"observation:{uuid4()}"
        self.graph.put_jsonld(
            Observation(
                id=observation_id,
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
            self._add_relation(evidence_id, memory.SUPPORTS, observation_id)
        return self._memory_write_record(observation_id, statement, decision, verification_status)

    def submit_decision(
        self,
        *,
        statement: str,
        scope: str,
        evidence_ids: list[str],
        source_authority: memory.SourceAuthority,
        verification_status: memory.VerificationStatus,
        based_on_ids: list[str] | None = None,
    ) -> MemoryWriteRecord:
        based_on_ids = based_on_ids or []
        decision = self.gate.evaluate_decision(
            statement=statement,
            scope=scope,
            evidence_ids=evidence_ids,
            source_authority=source_authority,
            verification_status=verification_status,
        )
        self._require_existing_nodes(evidence_ids, role="evidence")
        self._require_existing_nodes(based_on_ids, role="basedOn")
        decision_id = f"decision:{uuid4()}"
        self.graph.put_jsonld(
            Decision(
                id=decision_id,
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
                based_on=tuple(NodeRef(based_on_id) for based_on_id in based_on_ids),
            ).to_jsonld(),
            graph_id=self.graph_id,
        )
        for evidence_id in evidence_ids:
            self._add_relation(evidence_id, memory.SUPPORTS, decision_id)
        return self._memory_write_record(decision_id, statement, decision, verification_status)

    def _memory_write_record(
        self,
        memory_id: str,
        statement: str,
        decision,
        verification_status: memory.VerificationStatus,
    ) -> MemoryWriteRecord:
        return MemoryWriteRecord(
            memory_id=memory_id,
            statement=statement,
            status=decision.status,
            confidence_level=decision.confidence_level,
            verification_status=verification_status,
            gate_reason=decision.reason,
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
        values = _as_list(document.get(rt.PRODUCED.name))
        target = {"@id": tool_result_id}
        if target not in values:
            values.append(target)
        document[rt.PRODUCED.name] = values
        document[rt.UPDATED_AT.name] = utc_now()
        self.graph.put_jsonld(document, graph_id=self.graph_id)

    def _add_relation(self, source_id: str, relation, target_id: str) -> None:
        document = self.graph.get_jsonld(source_id, graph_id=self.graph_id)
        values = _as_list(document.get(relation.name))
        target = {"@id": target_id}
        if target not in values:
            values.append(target)
        document[relation.name] = values
        self.graph.put_jsonld(document, graph_id=self.graph_id)

    def _require_existing_nodes(self, node_ids: list[str], *, role: str) -> None:
        missing = [
            node_id
            for node_id in node_ids
            if not self.graph.has_jsonld(node_id, graph_id=self.graph_id)
        ]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Cannot submit memory with missing {role} node(s): {joined}")


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


def _to_tool_execution_status(state: ToolResultState) -> rt.ToolExecutionStatus:
    if state is ToolResultState.SUCCESS:
        return rt.ToolExecutionStatus.SUCCESS
    if state is ToolResultState.INTERRUPTED:
        return rt.ToolExecutionStatus.CANCELLED
    return rt.ToolExecutionStatus.ERROR


def _tool_result_from_event_slice(events: list[AgentEvent], tool_call_id: str) -> ToolResultBlock:
    return completed_tool_result_from_events(events, tool_call_id)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
