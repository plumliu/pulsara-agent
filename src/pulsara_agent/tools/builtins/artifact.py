"""Artifact read tool for persisted tool result outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import int_arg, object_schema, str_arg


DEFAULT_ARTIFACT_READ_CHARS = 20_000
MAX_ARTIFACT_READ_CHARS = 100_000


@dataclass(slots=True)
class ArtifactReadTool:
    runtime_session: RuntimeSession
    name: str = "artifact_read"
    description: str = (
        "Read a retained tool result artifact by artifact_id. "
        "Use this when a tool result includes artifacts[] and you need details beyond the inline output_preview. "
        "If the artifact ref includes preview.read_more.suggested_offset_chars, prefer that offset instead of rereading from 0. "
        "Offsets are character offsets; use has_more to page through text."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: object_schema(
            properties={
                "artifact_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["text", "info"], "default": "text"},
                "offset_chars": {"type": "integer", "default": 0},
                "max_chars": {"type": "integer", "default": DEFAULT_ARTIFACT_READ_CHARS},
            },
            required=["artifact_id"],
        )
    )
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        artifact_id = str_arg(call.arguments, "artifact_id")
        if not artifact_id:
            return self._json_result(call, status=ToolResultState.ERROR, payload=_not_found_payload(""))

        record = self.runtime_session.tool_result_artifacts.get_for_session(
            artifact_id,
            session_id=self.runtime_session.runtime_session_id,
        )
        if record is None:
            return self._json_result(call, status=ToolResultState.ERROR, payload=_not_found_payload(artifact_id))

        mode = str_arg(call.arguments, "mode") or "text"
        if mode not in {"text", "info"}:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={"status": "error", "error": f"unsupported artifact_read mode: {mode}"},
            )

        try:
            if mode == "info":
                info = self.runtime_session.archive.get_info(
                    artifact_id,
                    session_id=self.runtime_session.runtime_session_id,
                )
                payload: dict[str, Any] = {
                    "status": "success",
                    "artifact_id": info.id,
                    "media_type": info.media_type,
                    "size_bytes": info.size_bytes,
                    "stored_complete": record.stored_complete,
                    "loss_reason": record.loss_reason,
                    "role": record.role,
                }
            else:
                max_chars = min(
                    max(int_arg(call.arguments, "max_chars", DEFAULT_ARTIFACT_READ_CHARS), 1),
                    MAX_ARTIFACT_READ_CHARS,
                )
                offset_chars = max(int_arg(call.arguments, "offset_chars", 0), 0)
                text_slice = self.runtime_session.archive.read_text(
                    artifact_id,
                    session_id=self.runtime_session.runtime_session_id,
                    offset_chars=offset_chars,
                    max_chars=max_chars,
                )
                payload = {
                    "status": "success",
                    "artifact_id": text_slice.artifact.id,
                    "media_type": text_slice.artifact.media_type,
                    "size_bytes": text_slice.artifact.size_bytes,
                    "offset_chars": text_slice.offset_chars,
                    "returned_chars": text_slice.returned_chars,
                    "total_chars": text_slice.total_chars,
                    "has_more": text_slice.has_more,
                    "stored_complete": record.stored_complete,
                    "loss_reason": record.loss_reason,
                    "role": record.role,
                    "text": text_slice.text,
                }
        except KeyError:
            return self._json_result(call, status=ToolResultState.ERROR, payload=_not_found_payload(artifact_id))
        except ValueError as exc:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={"status": "error", "artifact_id": artifact_id, "error": str(exc)},
            )

        return self._json_result(call, status=ToolResultState.SUCCESS, payload=payload)

    def _json_result(self, call: ToolCall, *, status: ToolResultState, payload: dict[str, Any]) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=json.dumps(payload, ensure_ascii=False),
        )


def _not_found_payload(artifact_id: str) -> dict[str, Any]:
    return {
        "status": "not_found",
        "artifact_id": artifact_id,
        "error": "artifact not found",
    }
