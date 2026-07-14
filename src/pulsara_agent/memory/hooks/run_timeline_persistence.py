"""Persist assembled runtime run timelines via runtime event hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pulsara_agent.entities.runtime import RunTimelineRecord
from pulsara_agent.event import EventType, RunEndEvent
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.canonical.mutation_outbox import (
    MutationOutboxWriter,
    runtime_semantic_mutation_payload,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.runtime.hooks import HookContext
from pulsara_agent.runtime.timeline import build_run_timeline


@dataclass(slots=True)
class RunTimelinePersistenceHook:
    graph: Any
    archive: ArtifactStore
    event_store: Any
    scope: str | None = None
    graph_id: str | None = None
    mutation_outbox: MutationOutboxWriter | None = None

    async def __call__(self, context: HookContext, event: Any) -> None:
        if not _should_persist_timeline(event):
            return
        timeline = build_run_timeline(
            self.event_store.iter(run_id=context.run_id),
            runtime_session_id=context.runtime_session_id,
            run_id=context.run_id,
        )
        timeline_id = _timeline_id(context.runtime_session_id, context.run_id)
        blob_id = _timeline_blob_id(context.runtime_session_id, context.run_id, event)
        payload = json.dumps(timeline.to_dict(), ensure_ascii=True, sort_keys=True, indent=2)
        artifact = self.archive.put_text(
            blob_id,
            payload,
            session_id=context.runtime_session_id,
            run_id=context.run_id,
            media_type="application/json",
            metadata={"artifact_kind": "run_timeline"},
        )
        now = utc_now()
        record = RunTimelineRecord(
            id=timeline_id,
            runtime_session_id=context.runtime_session_id,
            run_id=context.run_id,
            turn_id=context.turn_id,
            reply_id=context.reply_id,
            scope=self.scope or (context.state.current_scope if context.state is not None and context.state.current_scope else f"ctx:{context.turn_id}"),
            status=timeline.status,
            item_count=len(timeline.items),
            created_at=_existing_created_at(self.graph, timeline_id, self.graph_id) or now,
            updated_at=now,
            stored_as=NodeRef(artifact.id),
        )
        document = record.to_jsonld()
        self.graph.put_jsonld(document, graph_id=self.graph_id)
        if self.mutation_outbox is not None:
            self.mutation_outbox.append_payload(
                runtime_semantic_mutation_payload(
                    node_id=timeline_id,
                    document=document,
                    source_runtime_session_id=context.runtime_session_id,
                    source_run_id=context.run_id,
                    source_turn_id=context.turn_id,
                    source_reply_id=context.reply_id,
                    source_artifact_ids=(artifact.id,),
                ),
                graph_id=self.graph_id or "graph:default",
                target_entry_key=timeline_id,
                sequence_key=self.graph_id or "graph:default",
            )


def _should_persist_timeline(event: Any) -> bool:
    if event.type in {EventType.REPLY_END, EventType.RUN_ERROR}:
        return True
    return isinstance(event, RunEndEvent)


def _timeline_id(runtime_session_id: str, run_id: str) -> str:
    return f"run-timeline:{runtime_session_id}:{run_id}"


def _timeline_blob_id(runtime_session_id: str, run_id: str, event: Any) -> str:
    sequence = getattr(event, "sequence", None)
    if sequence is None:
        return f"timeline:{runtime_session_id}:{run_id}:{getattr(event, 'id')}"
    return f"timeline:{runtime_session_id}:{run_id}:seq:{sequence}"


def _existing_created_at(graph: Any, timeline_id: str, graph_id: str | None) -> str | None:
    try:
        existing = graph.get_jsonld(timeline_id, graph_id=graph_id)
    except KeyError:
        return None
    created_at = existing.get(rt.CREATED_AT.name)
    if isinstance(created_at, str):
        return created_at
    return None
