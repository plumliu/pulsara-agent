"""Artifact read tool for persisted tool result outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import ToolArtifactReadPort
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.schemas import int_arg, str_arg


DEFAULT_ARTIFACT_READ_CHARS = 20_000
MAX_ARTIFACT_READ_CHARS = 100_000


@dataclass(slots=True)
class ArtifactReadTool:
    artifact_read_port: ToolArtifactReadPort
    name: str = "artifact_read"

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        artifact_id = str_arg(call.arguments, "artifact_id")
        if not artifact_id:
            return self._json_result(
                call, status=ToolResultState.ERROR, payload=_not_found_payload("")
            )

        record = self.artifact_read_port.lookup(artifact_id)
        if record is None:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload=_not_found_payload(artifact_id),
            )

        mode = str_arg(call.arguments, "mode") or "text"
        if mode not in {"text", "info"}:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={
                    "status": "error",
                    "error": f"unsupported artifact_read mode: {mode}",
                },
            )

        try:
            if mode == "info":
                info = self.artifact_read_port.info(artifact_id)
                payload: dict[str, Any] = {
                    "status": "success",
                    "artifact_id": info.record.artifact_id,
                    "media_type": info.record.media_type,
                    "size_bytes": info.record.size_bytes,
                    "stored_complete": record.stored_complete,
                    "loss_reason": record.loss_reason,
                    "role": record.role,
                }
            else:
                max_chars = min(
                    max(
                        int_arg(
                            call.arguments, "max_chars", DEFAULT_ARTIFACT_READ_CHARS
                        ),
                        1,
                    ),
                    MAX_ARTIFACT_READ_CHARS,
                )
                offset_chars = max(int_arg(call.arguments, "offset_chars", 0), 0)
                text_slice = self.artifact_read_port.read_text(
                    artifact_id,
                    offset_chars=offset_chars,
                    max_chars=max_chars,
                )
                payload = {
                    "status": "success",
                    "artifact_id": text_slice.info.record.artifact_id,
                    "media_type": text_slice.info.record.media_type,
                    "size_bytes": text_slice.info.record.size_bytes,
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
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload=_not_found_payload(artifact_id),
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

        return self._json_result(call, status=ToolResultState.SUCCESS, payload=payload)

    def _json_result(
        self, call: ToolCall, *, status: ToolResultState, payload: dict[str, Any]
    ) -> ToolExecutionResult:
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
