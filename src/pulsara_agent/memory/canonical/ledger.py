"""Canonical memory candidate ledger."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pulsara_agent.graph import DEFAULT_GRAPH_ID, GraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.entities.memory import (
    ActionBoundary,
    Claim,
    Decision,
    Observation,
    Preference,
)
from pulsara_agent.memory.foundation.records import ClaimRecord, MemoryWriteRecord
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.ontology import memory


@dataclass(slots=True)
class CanonicalMemoryLedger:
    graph: GraphStore
    gate: MemoryWriteGate
    graph_id: str = DEFAULT_GRAPH_ID

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
        return self._memory_write_record(
            preference_id, statement, decision, verification_status
        )

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
        return self._memory_write_record(
            boundary_id, statement, decision, verification_status
        )

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
        return self._memory_write_record(
            observation_id, statement, decision, verification_status
        )

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
        return self._memory_write_record(
            decision_id, statement, decision, verification_status
        )

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
            raise ValueError(
                f"Cannot submit memory with missing {role} node(s): {joined}"
            )


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
