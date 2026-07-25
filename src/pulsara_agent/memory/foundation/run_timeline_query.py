"""Read-side helpers for persisted runtime run timelines."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.timeline import RunTimeline, RunTimelineItem


_PERSISTENT_MANIFEST_SCHEMA = "run_timeline_persistent_manifest.v1"
_PERSISTENT_LEAF_SCHEMA = "run_timeline_persistent_leaf.v1"
_PERSISTENT_LEAF_MAX_ITEMS = 128
_PERSISTENT_LEAF_MAX_BYTES = 1024 * 1024
_PERSISTENT_MANIFEST_MAX_BYTES = 256 * 1024
_TIMELINE_PAGE_MAX_ITEMS = 256


class RunTimelineExportLimitExceeded(ValueError):
    """The explicit full export bound is smaller than the persisted timeline."""


@dataclass(frozen=True, slots=True)
class RunTimelinePageCursor:
    manifest_artifact_id: str
    leaf_artifact_id: str
    before_ordinal_exclusive: int


@dataclass(frozen=True, slots=True)
class RunTimelinePage:
    runtime_session_id: str
    run_id: str
    status: str
    total_completed_items: int
    items: tuple[RunTimelineItem, ...]
    open_items: tuple[RunTimelineItem, ...]
    next_cursor: RunTimelinePageCursor | None


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
    max_items: int,
) -> RunTimeline:
    if max_items < 1:
        raise ValueError("max_items must be positive")
    record = _find_run_timeline_record(
        graph=graph,
        run_id=run_id,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )
    stored_as = record.get(rt.STORED_AS.name)
    stored_as_id = _node_ref_id(stored_as)
    if stored_as_id is None:
        raise ValueError(
            f"Run timeline record for {run_id} does not reference an archived payload"
        )
    manifest_artifact_id = _artifact_id_from_node_ref(stored_as_id)
    payload = _read_bounded_json(
        archive,
        manifest_artifact_id,
        maximum_utf8_bytes=_PERSISTENT_MANIFEST_MAX_BYTES,
    )
    if payload.get("schema_version") != _PERSISTENT_MANIFEST_SCHEMA:
        timeline = RunTimeline.from_dict(payload)
        if len(timeline.items) > max_items:
            raise RunTimelineExportLimitExceeded(
                "legacy run timeline exceeds explicit export bound"
            )
        return timeline

    state = _persistent_state(payload)
    open_items = _open_timeline_items(payload)
    total = int(state["item_count"]) + len(open_items)
    if total > max_items:
        raise RunTimelineExportLimitExceeded(
            "persisted run timeline exceeds explicit export bound"
        )
    completed_pages: list[tuple[RunTimelineItem, ...]] = []
    open_timeline_items: tuple[RunTimelineItem, ...] = ()
    cursor: RunTimelinePageCursor | None = None
    while True:
        page = load_run_timeline_page(
            graph=graph,
            archive=archive,
            run_id=run_id,
            runtime_session_id=runtime_session_id,
            graph_id=graph_id,
            max_items=min(_TIMELINE_PAGE_MAX_ITEMS, max_items),
            cursor=cursor,
        )
        completed_pages.append(page.items)
        if cursor is None:
            open_timeline_items = page.open_items
        cursor = page.next_cursor
        if cursor is None:
            break
    items = [item for page_items in reversed(completed_pages) for item in page_items]
    items.extend(open_timeline_items)
    return RunTimeline(
        runtime_session_id=str(state["runtime_session_id"]),
        run_id=str(state["run_id"]),
        status=str(state["status"]),
        start_sequence=int(state["start_sequence"]),
        end_sequence=(
            int(state["end_sequence"])
            if state.get("end_sequence") is not None
            else None
        ),
        items=items,
    )


def load_run_timeline_page(
    *,
    graph: Any,
    archive: ArtifactStore,
    run_id: str,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
    max_items: int = 128,
    cursor: RunTimelinePageCursor | None = None,
) -> RunTimelinePage:
    if max_items < 1 or max_items > _TIMELINE_PAGE_MAX_ITEMS:
        raise ValueError(f"max_items must be between 1 and {_TIMELINE_PAGE_MAX_ITEMS}")
    record = _find_run_timeline_record(
        graph=graph,
        run_id=run_id,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )
    stored_as_id = _node_ref_id(record.get(rt.STORED_AS.name))
    if stored_as_id is None:
        raise ValueError(
            f"Run timeline record for {run_id} does not reference an archive"
        )
    manifest_artifact_id = _artifact_id_from_node_ref(stored_as_id)
    if cursor is not None and cursor.manifest_artifact_id != manifest_artifact_id:
        raise ValueError("run timeline page cursor names another manifest")
    manifest = _read_bounded_json(
        archive,
        manifest_artifact_id,
        maximum_utf8_bytes=_PERSISTENT_MANIFEST_MAX_BYTES,
    )
    if manifest.get("schema_version") != _PERSISTENT_MANIFEST_SCHEMA:
        raise ValueError("paged timeline reads require a persistent manifest")
    state = _persistent_state(manifest)
    leaf_id = (
        cursor.leaf_artifact_id
        if cursor is not None
        else manifest.get("tail_artifact_id")
    )
    before = (
        cursor.before_ordinal_exclusive
        if cursor is not None
        else int(state["item_count"])
    )
    selected: list[dict[str, Any]] = []
    next_cursor: RunTimelinePageCursor | None = None
    while leaf_id is not None and len(selected) < max_items:
        leaf = _read_timeline_leaf(
            archive,
            artifact_id=str(leaf_id),
            runtime_session_id=str(state["runtime_session_id"]),
            run_id=str(state["run_id"]),
        )
        candidates = [
            item
            for item in reversed(leaf["items"])
            if int(item["absolute_item_ordinal"]) < before
        ]
        available = max_items - len(selected)
        selected.extend(candidates[:available])
        if len(candidates) > available:
            next_cursor = RunTimelinePageCursor(
                manifest_artifact_id=manifest_artifact_id,
                leaf_artifact_id=str(leaf_id),
                before_ordinal_exclusive=int(
                    candidates[available - 1]["absolute_item_ordinal"]
                ),
            )
            break
        previous = leaf.get("previous_leaf_artifact_id")
        if previous is not None:
            next_cursor = RunTimelinePageCursor(
                manifest_artifact_id=manifest_artifact_id,
                leaf_artifact_id=str(previous),
                before_ordinal_exclusive=(int(leaf["absolute_start_ordinal"])),
            )
        else:
            next_cursor = None
        leaf_id = previous
        before = int(leaf["absolute_start_ordinal"])
    selected.reverse()
    return RunTimelinePage(
        runtime_session_id=str(state["runtime_session_id"]),
        run_id=str(state["run_id"]),
        status=str(state["status"]),
        total_completed_items=int(state["item_count"]),
        items=tuple(
            RunTimelineItem.from_dict(item["timeline_item"]) for item in selected
        ),
        open_items=(_open_timeline_items(manifest) if cursor is None else ()),
        next_cursor=next_cursor,
    )


def summarize_persisted_run_timeline(
    *,
    graph: Any,
    archive: ArtifactStore,
    run_id: str,
    runtime_session_id: str | None = None,
    graph_id: str | None = None,
    max_tail_items: int = 256,
) -> RunTimelineSummary:
    page = load_run_timeline_page(
        graph=graph,
        archive=archive,
        run_id=run_id,
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
        max_items=max_tail_items,
    )
    timeline = RunTimeline(
        runtime_session_id=page.runtime_session_id,
        run_id=page.run_id,
        status=page.status,
        start_sequence=None,
        end_sequence=None,
        items=[*page.items, *page.open_items],
    )
    summary = summarize_run_timeline(timeline)
    return RunTimelineSummary(
        runtime_session_id=summary.runtime_session_id,
        run_id=summary.run_id,
        status=summary.status,
        item_count=page.total_completed_items + len(page.open_items),
        assistant_text=summary.assistant_text,
        tool_traces=summary.tool_traces,
        errors=summary.errors,
    )


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
                    tool_name=str(
                        item.metadata.get(
                            "tool_name", call.get("tool_name", item.title)
                        )
                    ),
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
        assistant_text="\n".join(
            part.strip() for part in assistant_parts if part.strip()
        ),
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
        and (
            runtime_session_id is None
            or record.get(rt.SOURCE_SESSION.name) == runtime_session_id
        )
    ]
    if not records:
        raise KeyError(run_id)
    records.sort(
        key=lambda record: str(record.get(rt.UPDATED_AT.name, "")), reverse=True
    )
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


def _read_bounded_json(
    archive: ArtifactStore,
    artifact_id: str,
    *,
    maximum_utf8_bytes: int,
) -> dict[str, Any]:
    text = archive.get_text(artifact_id)
    if len(text.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError("run timeline artifact exceeds its physical bound")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("run timeline artifact is not a JSON object")
    return payload


def _persistent_state(manifest: dict[str, Any]) -> dict[str, Any]:
    state = manifest.get("state")
    if not isinstance(state, dict):
        raise ValueError("run timeline manifest lacks persistent state")
    required = {
        "runtime_session_id",
        "run_id",
        "through_sequence",
        "status",
        "start_sequence",
        "end_sequence",
        "item_count",
        "ordered_item_semantic_accumulator",
        "persistent_item_vector_root_semantic_fingerprint",
        "open_item_state_semantic_fingerprint",
        "state_semantic_fingerprint",
        "schema_version",
    }
    if set(state) != required:
        raise ValueError("run timeline persistent state shape drifted")
    return state


def _open_timeline_items(
    manifest: dict[str, Any],
) -> tuple[RunTimelineItem, ...]:
    open_state = manifest.get("open_state")
    if not isinstance(open_state, dict):
        raise ValueError("run timeline manifest lacks open-item state")
    raw_items = open_state.get("open_items")
    if not isinstance(raw_items, dict):
        raise ValueError("run timeline open-item state is invalid")
    ordered = sorted(
        raw_items.values(),
        key=lambda item: (
            int(item["timeline_item"]["start_sequence"]),
            str(item["timeline_item"]["kind"]),
        ),
    )
    return tuple(RunTimelineItem.from_dict(item["timeline_item"]) for item in ordered)


def _read_timeline_leaf(
    archive: ArtifactStore,
    *,
    artifact_id: str,
    runtime_session_id: str,
    run_id: str,
) -> dict[str, Any]:
    leaf = _read_bounded_json(
        archive,
        artifact_id,
        maximum_utf8_bytes=_PERSISTENT_LEAF_MAX_BYTES,
    )
    if (
        leaf.get("schema_version") != _PERSISTENT_LEAF_SCHEMA
        or leaf.get("runtime_session_id") != runtime_session_id
        or leaf.get("run_id") != run_id
    ):
        raise ValueError("run timeline leaf target or schema drifted")
    items = leaf.get("items")
    if (
        not isinstance(items, list)
        or not items
        or len(items) > _PERSISTENT_LEAF_MAX_ITEMS
    ):
        raise ValueError("run timeline leaf items violate physical bounds")
    ordinals = tuple(int(item["absolute_item_ordinal"]) for item in items)
    if ordinals != tuple(range(ordinals[0], ordinals[0] + len(ordinals))):
        raise ValueError("run timeline leaf ordinals are not contiguous")
    if (
        int(leaf.get("absolute_start_ordinal", -1)) != ordinals[0]
        or int(leaf.get("absolute_end_ordinal", -1)) != ordinals[-1]
    ):
        raise ValueError("run timeline leaf range does not match its items")
    expected_id = "timeline-leaf:" + context_fingerprint(
        "run-timeline-leaf-id:v1", leaf
    )
    if expected_id != artifact_id:
        raise ValueError("run timeline leaf content-addressed id drifted")
    return leaf
