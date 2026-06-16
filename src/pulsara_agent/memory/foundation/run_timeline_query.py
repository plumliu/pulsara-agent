"""Read-side helpers for persisted runtime run timelines."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.runtime.timeline import RunTimeline


@dataclass(frozen=True, slots=True)
class RunTimelineToolTrace:
    tool_call_id: str
    tool_name: str
    arguments: str
    status: str | None
    result_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "status": self.status,
            "result_summary": self.result_summary,
        }


@dataclass(frozen=True, slots=True)
class RunTimelineSummary:
    runtime_session_id: str
    run_id: str
    status: str
    item_count: int
    assistant_text: str
    tool_traces: list[RunTimelineToolTrace] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "status": self.status,
            "item_count": self.item_count,
            "assistant_text": self.assistant_text,
            "tool_traces": [trace.to_dict() for trace in self.tool_traces],
            "errors": list(self.errors),
        }


def load_run_timeline(
    *,
    graph: Any,
    archive: ArtifactStore,
    run_id: str,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
) -> RunTimeline:
    record = _find_run_timeline_record(
        graph=graph,
        run_id=run_id,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )
    stored_as = record.get(rt.STORED_AS.name)
    stored_as_id = _node_ref_id(stored_as)
    if stored_as_id is None:
        raise ValueError(f"Run timeline record for {run_id} does not reference an archived payload")
    payload = json.loads(archive.get_text(_artifact_id_from_node_ref(stored_as_id)))
    return RunTimeline.from_dict(payload)


def summarize_run_timeline(timeline: RunTimeline) -> RunTimelineSummary:
    tool_calls: dict[str, dict[str, str]] = {}
    tool_traces: list[RunTimelineToolTrace] = []
    assistant_parts: list[str] = []
    errors: list[str] = []

    for item in timeline.items:
        if item.kind == "assistant_text" and item.summary:
            assistant_parts.append(item.summary)
            continue
        if item.kind == "error" and item.summary:
            errors.append(item.summary)
            continue
        if item.kind == "tool_call":
            tool_call_id = str(item.metadata.get("tool_call_id", ""))
            if not tool_call_id:
                continue
            tool_calls[tool_call_id] = {
                "tool_name": str(item.metadata.get("tool_name", item.title)),
                "arguments": str(item.metadata.get("arguments", "")),
            }
            continue
        if item.kind == "tool_result":
            tool_call_id = str(item.metadata.get("tool_call_id", ""))
            call = tool_calls.get(tool_call_id, {})
            tool_traces.append(
                RunTimelineToolTrace(
                    tool_call_id=tool_call_id,
                    tool_name=str(item.metadata.get("tool_name", call.get("tool_name", item.title))),
                    arguments=call.get("arguments", ""),
                    status=item.status,
                    result_summary=item.summary,
                )
            )

    return RunTimelineSummary(
        runtime_session_id=timeline.runtime_session_id,
        run_id=timeline.run_id,
        status=timeline.status,
        item_count=len(timeline.items),
        assistant_text="\n".join(part.strip() for part in assistant_parts if part.strip()),
        tool_traces=tool_traces,
        errors=errors,
    )


def _find_run_timeline_record(
    *,
    graph: Any,
    run_id: str,
    runtime_session_id: str | None,
    graph_id: str | None,
) -> dict[str, Any]:
    records = [
        record
        for record in graph.find_by_type(rt.RUN_TIMELINE, graph_id=graph_id)
        if record.get(rt.SOURCE_RUN.name) == run_id
        and (runtime_session_id is None or record.get(rt.SOURCE_SESSION.name) == runtime_session_id)
    ]
    if not records:
        raise KeyError(run_id)
    records.sort(key=lambda record: str(record.get(rt.UPDATED_AT.name, "")), reverse=True)
    return records[0]


def _artifact_id_from_node_ref(node_id: str) -> str:
    prefix = "urn:pulsara:"
    if node_id.startswith(prefix):
        return urllib.parse.unquote(node_id[len(prefix) :])
    return node_id


def _node_ref_id(value: Any) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("@id"), str):
        return value["@id"]
    if isinstance(value, list):
        for item in value:
            node_id = _node_ref_id(item)
            if node_id is not None:
                return node_id
    return None
