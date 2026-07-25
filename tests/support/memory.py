from __future__ import annotations

from uuid import uuid4

from pulsara_agent.entities.runtime import Evidence
from pulsara_agent.graph import GraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.ontology import memory, runtime as rt


def seed_tool_evidence(
    graph: GraphStore,
    *,
    graph_id: str | None = None,
    scope: str,
    statement: str = "The tool result provides verified evidence.",
) -> str:
    """Seed canonical evidence without invoking the retired evidence projector."""

    evidence_id = f"evidence:test:{uuid4().hex}"
    graph.put_jsonld(
        Evidence(
            id=evidence_id,
            statement=statement,
            source_type=rt.EvidenceSourceType.TOOL_RESULT,
            status=memory.NodeStatus.ACTIVE,
            observed_at=utc_now(),
            scope=scope,
            created_from=NodeRef(f"tool-result:test:{uuid4().hex}"),
        ).to_jsonld(),
        graph_id=graph_id,
    )
    return evidence_id
