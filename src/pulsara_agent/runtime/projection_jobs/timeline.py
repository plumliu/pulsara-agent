"""Incremental, restart-safe run timeline reducer.

The reducer keeps only open business items resident. Completed items are emitted
once with their original absolute ordinal and can be appended to immutable
timeline leaves without rebuilding prior history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.event import (
    AgentEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
    TextBlockSegmentEvent,
    ThinkingBlockSegmentEvent,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)


OPEN_TIMELINE_STATE_MAX_BYTES = 1024 * 1024
TIMELINE_ITEM_MAX_BYTES = 1024 * 1024
TIMELINE_SUMMARY_MAX_CODEPOINTS = 500


@dataclass(slots=True)
class IncrementalRunTimelineReducer:
    runtime_session_id: str
    run_id: str
    next_item_ordinal: int = 0
    status: str = "running"
    start_sequence: int | None = None
    terminal_sequence: int | None = None
    waiting_user: bool = False
    failed: bool = False
    terminal_status: str | None = None
    open_items: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed_items: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def restore(
        cls,
        *,
        runtime_session_id: str,
        run_id: str,
        payload: dict[str, Any] | None,
        next_item_ordinal: int,
        status: str,
        start_sequence: int | None,
        terminal_sequence: int | None,
    ) -> "IncrementalRunTimelineReducer":
        if payload is None:
            return cls(
                runtime_session_id=runtime_session_id,
                run_id=run_id,
                next_item_ordinal=next_item_ordinal,
                status=status,
                start_sequence=start_sequence,
                terminal_sequence=terminal_sequence,
            )
        if payload.get("schema_version") != "run_timeline_open_state.v1":
            raise ValueError("unsupported run timeline open-state schema")
        if (
            payload.get("runtime_session_id") != runtime_session_id
            or payload.get("run_id") != run_id
            or int(payload.get("next_item_ordinal", -1)) != next_item_ordinal
        ):
            raise ValueError("run timeline open-state target drifted")
        raw_items = payload.get("open_items")
        if not isinstance(raw_items, dict):
            raise ValueError("run timeline open-state items are invalid")
        reducer = cls(
            runtime_session_id=runtime_session_id,
            run_id=run_id,
            next_item_ordinal=next_item_ordinal,
            status=status,
            start_sequence=start_sequence,
            terminal_sequence=terminal_sequence,
            waiting_user=bool(payload.get("waiting_user", False)),
            failed=bool(payload.get("failed", False)),
            terminal_status=(
                str(payload["terminal_status"])
                if payload.get("terminal_status") is not None
                else None
            ),
            open_items={
                str(key): _owned_open_item(value) for key, value in raw_items.items()
            },
        )
        reducer._refresh_status()
        reducer._validate_open_state()
        return reducer

    def apply(self, event: AgentEvent) -> None:
        if event.run_id != self.run_id:
            raise ValueError("timeline reducer received an event from another run")
        if event.sequence is None:
            raise ValueError("timeline reducer requires committed event sequences")
        if self.start_sequence is None:
            self.start_sequence = event.sequence

        if isinstance(event, RunStartEvent):
            if event.new_run_boundary is not None:
                boundary = event.new_run_boundary
                title = "Host run boundary"
                metadata = {
                    "run_entry_kind": "host",
                    "boundary_id": boundary.identity.boundary_id,
                    "boundary_kind": boundary.identity.kind.value,
                    "permission_snapshot_id": boundary.permission_snapshot_id,
                    "model_target_fingerprint": boundary.model_target_fingerprint,
                    "mcp_installation_id": boundary.mcp_installation_id,
                    "capability_basis_fingerprint": (
                        boundary.capability_basis.basis_fingerprint
                    ),
                }
            else:
                entry = event.subagent_run_entry
                title = "Subagent run entry"
                metadata = {
                    "run_entry_kind": "subagent_child",
                    "subagent_run_id": (
                        entry.subagent_run_id if entry is not None else None
                    ),
                    "subagent_task_id": (
                        entry.subagent_task_id if entry is not None else None
                    ),
                }
            self._append_closed(
                self._new_item(
                    event,
                    kind="run_boundary",
                    title=title,
                    status="committed",
                    metadata=metadata,
                )
            )
        elif isinstance(event, RunInteractionResumeBoundaryEvent):
            boundary = event.boundary
            self._append_closed(
                self._new_item(
                    event,
                    kind="continuation_boundary",
                    title=f"Interaction resume: {boundary.interaction_kind}",
                    status="committed",
                    metadata={
                        "boundary_id": boundary.identity.boundary_id,
                        "interaction_id": boundary.interaction_id,
                        "interaction_kind": boundary.interaction_kind,
                        "exposure_transition": boundary.exposure_transition,
                        "source_exposure_id": boundary.source_exposure_id,
                        "effective_exposure_id": boundary.effective_exposure_id,
                        "mcp_installation_id": boundary.mcp_installation_id,
                    },
                )
            )
        elif isinstance(event, ReplyStartEvent):
            self._open(
                f"reply:{event.reply_id}",
                self._new_item(
                    event,
                    kind="reply",
                    title="Assistant reply",
                    status="running",
                    metadata={"name": event.name, "role": event.role},
                ),
            )
        elif isinstance(event, ReplyEndEvent):
            self._close(f"reply:{event.reply_id}", event, status="completed")
            self._close_prefix(f"text:{event.reply_id}:", event, status=None)
            self._close_prefix(f"thinking:{event.reply_id}:", event, status=None)
            self._close(f"model:{event.reply_id}", event, status="completed")
        elif isinstance(event, ModelCallStartEvent):
            self._open(
                f"model:{event.reply_id}",
                self._new_item(
                    event,
                    kind="model_call",
                    title=f"Model call: {event.resolved_call.target.model_id}",
                    status="running",
                    metadata={
                        "resolved_model_call_id": (
                            event.resolved_call.resolved_model_call_id
                        ),
                        "target_fingerprint": (
                            event.resolved_call.target.target_fingerprint
                        ),
                        "model_id": event.resolved_call.target.model_id,
                        "model_role": event.resolved_call.target.model_role,
                        "provider": event.resolved_call.target.provider,
                        "context_id": event.context_id,
                        "model_call_index": event.model_call_index,
                    },
                ),
            )
        elif isinstance(event, ModelCallEndEvent):
            item = self.open_items.get(f"model:{event.reply_id}")
            if item is not None:
                item["timeline_item"]["metadata"].update(
                    {
                        "resolved_model_call_id": event.resolved_model_call_id,
                        "target_fingerprint": event.target_fingerprint,
                        "outcome": event.outcome,
                        "usage_status": event.usage_status,
                        "usage": (
                            event.usage.model_dump(mode="json")
                            if event.usage is not None
                            else None
                        ),
                        "estimated_input_tokens": event.estimated_input_tokens,
                    }
                )
            self._close(f"model:{event.reply_id}", event, status="completed")
        elif isinstance(event, TextBlockSegmentEvent):
            key = f"text:{event.reply_id}:{event.block_id}"
            item = self._ensure_open(
                key,
                event,
                kind="assistant_text",
                title="Assistant text",
            )
            _append_summary(item["timeline_item"], event.text)
            _touch(item, event)
        elif isinstance(event, ThinkingBlockSegmentEvent):
            key = f"thinking:{event.reply_id}:{event.block_id}"
            item = self._ensure_open(
                key,
                event,
                kind="assistant_thinking",
                title="Assistant thinking",
            )
            _append_summary(item["timeline_item"], event.thinking)
            _touch(item, event)
        elif isinstance(event, ToolCallStartEvent):
            self._open(
                f"tool-call:{event.tool_call_id}",
                self._new_item(
                    event,
                    kind="tool_call",
                    title=f"Tool call: {event.tool_call_name}",
                    status="running",
                    metadata={
                        "tool_call_id": event.tool_call_id,
                        "tool_name": event.tool_call_name,
                        "arguments": "",
                    },
                ),
            )
        elif isinstance(event, ToolCallArgumentsSegmentEvent):
            key = f"tool-call:{event.tool_call_id}"
            item = self._ensure_open(
                key,
                event,
                kind="tool_call",
                title=f"Tool call: {event.tool_call_id}",
                status="running",
                metadata={
                    "tool_call_id": event.tool_call_id,
                    "tool_name": "",
                    "arguments": "",
                },
            )
            item["timeline_item"]["metadata"]["arguments"] = (
                str(item["timeline_item"]["metadata"].get("arguments", ""))
                + event.arguments_json_fragment
            )
            _touch(item, event)
        elif isinstance(event, ToolCallEndEvent):
            self._close(
                f"tool-call:{event.tool_call_id}",
                event,
                status="completed",
            )
        elif isinstance(event, ToolResultStartEvent):
            self._open(
                f"tool-result:{event.tool_call_id}",
                self._new_item(
                    event,
                    kind="tool_result",
                    title=f"Tool result: {event.tool_call_name}",
                    status="running",
                    metadata={
                        "tool_call_id": event.tool_call_id,
                        "tool_name": event.tool_call_name,
                    },
                ),
            )
        elif isinstance(event, ToolResultTextDeltaEvent):
            key = f"tool-result:{event.tool_call_id}"
            item = self._ensure_open(
                key,
                event,
                kind="tool_result",
                title=f"Tool result: {event.tool_call_id}",
                status="running",
                metadata={"tool_call_id": event.tool_call_id, "tool_name": ""},
            )
            _append_summary(item["timeline_item"], event.delta)
            _touch(item, event)
        elif isinstance(event, ToolResultDataDeltaEvent):
            key = f"tool-result:{event.tool_call_id}"
            item = self._ensure_open(
                key,
                event,
                kind="tool_result",
                title=f"Tool result: {event.tool_call_id}",
                status="running",
                metadata={"tool_call_id": event.tool_call_id, "tool_name": ""},
            )
            metadata = item["timeline_item"]["metadata"]
            metadata["data_blocks"] = int(metadata.get("data_blocks", 0)) + 1
            _touch(item, event)
        elif isinstance(event, ToolResultEndEvent):
            self._close(
                f"tool-result:{event.tool_call_id}",
                event,
                status=event.state.value,
            )
        elif isinstance(event, RequireUserConfirmEvent):
            self.waiting_user = True
            self._open(
                "permission-request",
                self._new_item(
                    event,
                    kind="permission_request",
                    title="Permission request",
                    status="waiting",
                    metadata={"tool_call_ids": [call.id for call in event.tool_calls]},
                ),
            )
        elif isinstance(event, UserConfirmResultEvent):
            self.waiting_user = False
            self._close("permission-request", event, status="resolved")
        elif isinstance(event, RunErrorEvent):
            self.failed = True
            self.terminal_sequence = event.sequence
            self._append_closed(
                self._new_item(
                    event,
                    kind="error",
                    title=event.code,
                    status="error",
                    summary=event.message,
                )
            )
        elif isinstance(event, PlanModeEnteredEvent):
            self._append_closed(
                self._new_item(
                    event,
                    kind="plan_mode",
                    title="Plan mode entered",
                    status="active",
                    summary=event.reason,
                    metadata={
                        "source": event.source,
                        "previous_permission_mode": (event.previous_permission_mode),
                    },
                )
            )
        elif isinstance(event, PlanQuestionAskedEvent):
            self.waiting_user = True
            self._open(
                f"plan-question:{event.question_id}",
                self._new_item(
                    event,
                    kind="plan_question",
                    title="Plan question",
                    status="waiting",
                    summary=event.question,
                    metadata={
                        "question_id": event.question_id,
                        "tool_call_id": event.tool_call_id,
                        "options": [
                            option.model_dump(mode="json") for option in event.options
                        ],
                        "allow_free_text": event.allow_free_text,
                    },
                ),
            )
        elif isinstance(event, PlanQuestionAnsweredEvent):
            self.waiting_user = False
            key = f"plan-question:{event.question_id}"
            item = self.open_items.get(key)
            if item is not None:
                item["timeline_item"]["metadata"].update(
                    {
                        "answer_text": event.answer_text,
                        "selected_option": event.selected_option,
                    }
                )
            self._close(key, event, status="answered")
        elif isinstance(event, PlanExitRequestedEvent):
            self.waiting_user = True
            self._open(
                f"plan-exit:{event.exit_request_id}",
                self._new_item(
                    event,
                    kind="plan_exit_request",
                    title="Plan exit requested",
                    status="waiting",
                    summary=event.summary,
                    metadata={
                        "exit_request_id": event.exit_request_id,
                        "tool_call_id": event.tool_call_id,
                        "plan_artifact_id": event.plan_artifact_id,
                    },
                ),
            )
        elif isinstance(event, PlanExitResolvedEvent):
            self.waiting_user = False
            key = f"plan-exit:{event.exit_request_id}"
            item = self.open_items.get(key)
            if item is not None:
                item["timeline_item"]["metadata"]["user_feedback"] = event.user_feedback
            self._close(key, event, status=event.decision)
        elif isinstance(event, PlanModeExitedEvent):
            self._append_closed(
                self._new_item(
                    event,
                    kind="plan_mode",
                    title="Plan mode exited",
                    status="completed",
                    summary=event.accepted_plan_summary,
                    metadata={
                        "source": event.source,
                        "exit_request_id": event.exit_request_id,
                        "restored_permission_mode": (event.restored_permission_mode),
                        "accepted_plan_artifact_id": (event.accepted_plan_artifact_id),
                    },
                )
            )
        elif isinstance(event, RunEndEvent):
            self.waiting_user = False
            self.terminal_status = (
                "completed" if event.status == "finished" else event.status
            )
            self.terminal_sequence = event.sequence
            self._close_all(event)

        self._refresh_status()
        self._validate_open_state()

    def take_completed_items(self) -> tuple[dict[str, Any], ...]:
        completed = tuple(
            sorted(
                self.completed_items,
                key=lambda item: int(item["absolute_item_ordinal"]),
            )
        )
        self.completed_items.clear()
        return completed

    def open_state_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": "run_timeline_open_state.v1",
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "next_item_ordinal": self.next_item_ordinal,
            "waiting_user": self.waiting_user,
            "failed": self.failed,
            "terminal_status": self.terminal_status,
            "open_items": {
                key: self.open_items[key] for key in sorted(self.open_items)
            },
        }
        if len(canonical_json_bytes(payload)) > OPEN_TIMELINE_STATE_MAX_BYTES:
            raise ValueError("run timeline open state exceeds its physical bound")
        return payload

    def _new_item(
        self,
        event: AgentEvent,
        *,
        kind: str,
        title: str,
        status: str | None = None,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = {
            "timeline_item": {
                "kind": kind,
                "title": title,
                "run_id": event.run_id,
                "turn_id": event.turn_id,
                "reply_id": event.reply_id,
                "start_sequence": event.sequence,
                "end_sequence": event.sequence,
                "status": status,
                "summary": summary[:TIMELINE_SUMMARY_MAX_CODEPOINTS],
                "metadata": dict(metadata or {}),
            },
        }
        _validate_open_timeline_item(item)
        return item

    def _open(self, key: str, item: dict[str, Any]) -> None:
        if key in self.open_items:
            raise ValueError(f"run timeline open item already exists: {key}")
        self.open_items[key] = item

    def _ensure_open(
        self,
        key: str,
        event: AgentEvent,
        *,
        kind: str,
        title: str,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = self.open_items.get(key)
        if item is None:
            item = self._new_item(
                event,
                kind=kind,
                title=title,
                status=status,
                metadata=metadata,
            )
            self.open_items[key] = item
        return item

    def _close(
        self,
        key: str,
        event: AgentEvent,
        *,
        status: str | None,
    ) -> None:
        item = self.open_items.pop(key, None)
        if item is None:
            return
        _touch(item, event)
        if status is not None:
            item["timeline_item"]["status"] = status
        self._append_closed(item)

    def _close_prefix(
        self,
        prefix: str,
        event: AgentEvent,
        *,
        status: str | None,
    ) -> None:
        for key in tuple(
            sorted(
                (key for key in self.open_items if key.startswith(prefix)),
                key=lambda value: (
                    int(self.open_items[value]["timeline_item"]["start_sequence"]),
                    value,
                ),
            )
        ):
            self._close(key, event, status=status)

    def _close_all(self, event: AgentEvent) -> None:
        for key in tuple(
            sorted(
                self.open_items,
                key=lambda value: (
                    int(self.open_items[value]["timeline_item"]["start_sequence"]),
                    value,
                ),
            )
        ):
            self._close(key, event, status=None)

    def _append_closed(self, item: dict[str, Any]) -> None:
        item["absolute_item_ordinal"] = self.next_item_ordinal
        self.next_item_ordinal += 1
        _validate_timeline_item(item)
        semantic = context_fingerprint(
            "run-timeline-item-semantic:v1",
            item,
        )
        item = {
            **item,
            "item_semantic_fingerprint": semantic,
        }
        self.completed_items.append(item)

    def _refresh_status(self) -> None:
        self.status = self.terminal_status or (
            "error"
            if self.failed
            else "waiting_user"
            if self.waiting_user
            else "running"
        )

    def _validate_open_state(self) -> None:
        for item in self.open_items.values():
            _validate_open_timeline_item(item)
        if len(canonical_json_bytes(self.open_state_payload_unchecked())) > (
            OPEN_TIMELINE_STATE_MAX_BYTES
        ):
            raise ValueError("run timeline open state exceeds its physical bound")

    def open_state_payload_unchecked(self) -> dict[str, Any]:
        return {
            "schema_version": "run_timeline_open_state.v1",
            "runtime_session_id": self.runtime_session_id,
            "run_id": self.run_id,
            "next_item_ordinal": self.next_item_ordinal,
            "waiting_user": self.waiting_user,
            "failed": self.failed,
            "terminal_status": self.terminal_status,
            "open_items": {
                key: self.open_items[key] for key in sorted(self.open_items)
            },
        }


def _owned_open_item(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("run timeline open item is not an object")
    encoded = canonical_json_bytes(value)
    decoded = json.loads(encoded)
    _validate_open_timeline_item(decoded)
    return decoded


def _touch(item: dict[str, Any], event: AgentEvent) -> None:
    item["timeline_item"]["end_sequence"] = event.sequence


def _append_summary(item: dict[str, Any], value: str) -> None:
    current = str(item.get("summary", ""))
    if len(current) >= TIMELINE_SUMMARY_MAX_CODEPOINTS:
        return
    item["summary"] = (current + value)[:TIMELINE_SUMMARY_MAX_CODEPOINTS]


def _validate_timeline_item(item: dict[str, Any]) -> None:
    if not isinstance(item.get("absolute_item_ordinal"), int):
        raise ValueError("run timeline item ordinal is invalid")
    _validate_timeline_item_payload(item)


def _validate_open_timeline_item(item: dict[str, Any]) -> None:
    if "absolute_item_ordinal" in item:
        raise ValueError("open timeline item cannot reserve an append ordinal")
    _validate_timeline_item_payload(item)


def _validate_timeline_item_payload(item: dict[str, Any]) -> None:
    timeline_item = item.get("timeline_item")
    if not isinstance(timeline_item, dict):
        raise ValueError("run timeline item payload is invalid")
    required = {
        "kind",
        "title",
        "run_id",
        "turn_id",
        "reply_id",
        "start_sequence",
        "end_sequence",
        "status",
        "summary",
        "metadata",
    }
    if set(timeline_item) != required:
        raise ValueError("run timeline item payload shape drifted")
    if len(canonical_json_bytes(item)) > TIMELINE_ITEM_MAX_BYTES:
        raise ValueError("run timeline item exceeds its physical bound")


__all__ = [
    "IncrementalRunTimelineReducer",
    "OPEN_TIMELINE_STATE_MAX_BYTES",
    "TIMELINE_ITEM_MAX_BYTES",
    "TIMELINE_SUMMARY_MAX_CODEPOINTS",
]
