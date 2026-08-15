"""Read-only model tool for canonical tool-output artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pulsara_agent.conversation_kernel.tool_artifacts import (
    ARTIFACT_READ_DEFAULT_CHARS,
    ARTIFACT_READ_HARD_CHARS,
    CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import ArtifactContentError, ToolArtifactReadPort
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult


DEFAULT_ARTIFACT_READ_CHARS = ARTIFACT_READ_DEFAULT_CHARS
MAX_ARTIFACT_READ_CHARS = ARTIFACT_READ_HARD_CHARS


@dataclass(slots=True)
class ArtifactReadTool:
    artifact_read_port: ToolArtifactReadPort
    name: str = "artifact_read"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        request = _request(call.arguments)
        if isinstance(request, str):
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={"status": "error", "error": request},
            )
        artifact_id, mode, offset_chars, max_chars = request
        try:
            if mode == "info":
                info = self.artifact_read_port.info(artifact_id)
                record = info.record
                payload: dict[str, Any] = _base_payload(record)
            else:
                text_slice = self.artifact_read_port.read_text(
                    artifact_id,
                    offset_chars=offset_chars,
                    max_chars=max_chars,
                )
                payload = _bounded_text_payload(text_slice)
                record = text_slice.info.record
        except KeyError:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload=_not_found_payload(artifact_id),
            )
        except ArtifactContentError as exc:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={
                    "status": "content_error",
                    "artifact_id": artifact_id,
                    "error_code": str(exc),
                    "error": "artifact content is unavailable or corrupt",
                },
            )
        except ValueError as exc:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={
                    "status": "error",
                    "artifact_id": artifact_id,
                    "error": str(exc),
                },
            )
        return self._json_result(
            call,
            status=ToolResultState.SUCCESS,
            payload=payload,
            model_visible_memory_fact_ids=(record.model_visible_memory_fact_ids),
        )

    @staticmethod
    def _json_result(
        call: ToolCall,
        *,
        status: ToolResultState,
        payload: dict[str, Any],
        model_visible_memory_fact_ids: tuple[str, ...] = (),
    ) -> ToolExecutionResult:
        output = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(output.encode("utf-8")) > CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES:
            raise AssertionError(
                "artifact_read response exceeded the inline hard bound"
            )
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=output,
            artifact_source_read=True,
            model_visible_memory_fact_ids=model_visible_memory_fact_ids,
        )


def _request(
    arguments: object,
) -> tuple[str, str, int, int] | str:
    if not isinstance(arguments, dict):
        # FrozenToolJsonDict is a dict subclass; keep the check deliberately
        # closed so arbitrary Mapping implementations cannot smuggle values.
        return "artifact_read arguments must be an object"
    allowed_keys = {"artifact_id", "mode", "offset_chars", "max_chars"}
    if not set(arguments).issubset(allowed_keys):
        return "artifact_read arguments contain unknown properties"
    artifact_id = arguments.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return "artifact_id must be a non-empty string"
    mode = arguments.get("mode", "text")
    if mode not in {"text", "info"}:
        return "mode must be text or info"
    offset = arguments.get("offset_chars", 0)
    maximum = arguments.get("max_chars", DEFAULT_ARTIFACT_READ_CHARS)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return "offset_chars must be a non-negative integer"
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= MAX_ARTIFACT_READ_CHARS
    ):
        return f"max_chars must be between 1 and {MAX_ARTIFACT_READ_CHARS}"
    return artifact_id, str(mode), offset, maximum


def _base_payload(record: object) -> dict[str, Any]:
    return {
        "status": "success",
        "artifact_id": record.artifact_id,
        "role": record.role,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "artifact_disposition": record.artifact_disposition.value,
        "source_coverage": record.source_coverage.value,
        "display_kind": record.display_kind.value,
        "source_coverage_reason": (
            None
            if record.source_coverage_reason is None
            else record.source_coverage_reason.value
        ),
        "artifact_unavailability_reason": (
            None
            if record.artifact_unavailability_reason is None
            else record.artifact_unavailability_reason.value
        ),
        "model_visible_memory_ids": list(record.model_visible_memory_fact_ids),
    }


def _bounded_text_payload(text_slice: object) -> dict[str, Any]:
    record = text_slice.info.record
    base = _base_payload(record)
    text = text_slice.text

    def build(value: str) -> dict[str, Any]:
        returned = len(value)
        next_offset = text_slice.offset_chars + returned
        has_more = next_offset < text_slice.total_chars
        payload = dict(base)
        payload.update(
            {
                "text": value,
                "offset_chars": text_slice.offset_chars,
                "returned_chars": returned,
                "total_chars": text_slice.total_chars,
                "has_more": has_more,
                "next_offset_chars": next_offset if has_more else None,
            }
        )
        return payload

    candidate = build(text)
    if _payload_size(candidate) <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES:
        return candidate
    low = 0
    high = len(text)
    winner = build("")
    while low <= high:
        length = (low + high) // 2
        current = build(text[:length])
        if _payload_size(current) <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES:
            winner = current
            low = length + 1
        else:
            high = length - 1
    return winner


def _payload_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _not_found_payload(artifact_id: str) -> dict[str, Any]:
    return {
        "status": "not_found",
        "artifact_id": artifact_id,
        "error": "artifact not found",
    }


__all__ = [
    "ArtifactReadTool",
    "DEFAULT_ARTIFACT_READ_CHARS",
    "MAX_ARTIFACT_READ_CHARS",
]
