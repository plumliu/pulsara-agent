"""Direct reading and exact local export of canonical tool-output artifacts."""

from __future__ import annotations

import json
import errno
from dataclasses import dataclass, field
from typing import Any

from pulsara_agent.conversation_kernel.tool_artifacts import (
    ARTIFACT_READ_DEFAULT_CHARS,
    ARTIFACT_READ_HARD_CHARS,
    CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.artifact import ArtifactContentError, ToolArtifactReadPort
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_artifact_page_logical_utf8_bytes,
)
from pulsara_agent.tools.builtins.filesystem import (
    _AtomicFileCreationError,
    _AtomicNoClobberUnavailable,
    _AtomicTargetExists,
    _atomic_create_bytes,
    _requested_leaf_path,
)
from pulsara_agent.tools.builtins.workspace import WorkspaceTool, WritePathScope


DEFAULT_ARTIFACT_READ_CHARS = ARTIFACT_READ_DEFAULT_CHARS
MAX_ARTIFACT_READ_CHARS = ARTIFACT_READ_HARD_CHARS


class _ArtifactPageLogicalBoundError(ValueError):
    pass


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
        artifact_id, offset_chars, max_chars = request
        try:
            text_slice = self.artifact_read_port.read_text(
                artifact_id,
                offset_chars=offset_chars,
                max_chars=max_chars,
            )
            payload = _bounded_text_payload(
                text_slice,
                tool_call_id=call.id,
            )
            record = text_slice.record
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
        except _ArtifactPageLogicalBoundError:
            return self._json_result(
                call,
                status=ToolResultState.ERROR,
                payload={
                    "status": "resource_boundary",
                    "artifact_id": artifact_id,
                    "error_code": "ARTIFACT_PAGE_NOT_INLINEABLE",
                    "error": "artifact page metadata exceeds its logical FULL bound",
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
            artifact_inline_result=True,
            model_visible_memory_fact_ids=model_visible_memory_fact_ids,
        )


@dataclass(slots=True)
class ArtifactExportTool(WorkspaceTool):
    artifact_read_port: ToolArtifactReadPort = field(kw_only=True)
    name: str = "artifact_export"

    def execute(
        self, call: ToolCall, *, write_scope: WritePathScope = WritePathScope.WORKSPACE
    ) -> ToolExecutionResult:
        args = call.arguments
        if set(args) != {"artifact_id", "path"} or any(
            not isinstance(args.get(key), str) or not args[key].strip()
            for key in ("artifact_id", "path")
        ):
            return _export_error(
                call,
                "INVALID_ARGUMENTS",
                "artifact_id and path must be non-empty strings; no other fields are accepted.",
            )
        artifact_id = args["artifact_id"]
        try:
            body = self.artifact_read_port.read_body(artifact_id)
        except KeyError:
            return _export_result(call, _not_found_payload(artifact_id), success=False)
        except ArtifactContentError as exc:
            return _export_result(
                call,
                {
                    "status": "content_error",
                    "artifact_id": artifact_id,
                    "error_code": str(exc),
                    "error": "artifact content is unavailable or corrupt",
                },
                success=False,
            )

        try:
            path = self._resolve_path(args["path"], write_scope=write_scope)
            requested_leaf = _requested_leaf_path(args["path"], self.workspace_root)
            if requested_leaf.is_symlink() or path.exists() or path.is_symlink():
                raise _AtomicTargetExists
            record = body.record
            payload = {
                "status": "success",
                "artifact_id": artifact_id,
                "path": str(path),
                "bytes_written": len(body.content),
                "source_coverage": record.source_coverage.value,
                "source_coverage_reason": (
                    None
                    if record.source_coverage_reason is None
                    else record.source_coverage_reason.value
                ),
            }
            # Measure the serialized location before any directories or files
            # are created. This is a per-result bound, not a whole-turn budget.
            if not _export_payload_fits(call, payload):
                return _export_boundary(call)
            _atomic_create_bytes(path, body.content)
        except _AtomicTargetExists:
            return _export_error(
                call,
                "FILE_ALREADY_EXISTS",
                "The destination exists; it was not changed.",
                "Choose a new path explicitly; do not delete or overwrite the existing destination.",
            )
        except _AtomicNoClobberUnavailable:
            return _export_error(
                call,
                "ATOMIC_NO_CLOBBER_UNAVAILABLE",
                "This filesystem cannot publish a new file without replacement. No destination was created.",
            )
        except _AtomicFileCreationError as exc:
            return _export_error(
                call,
                "FILE_PUBLICATION_UNCONFIRMED"
                if exc.published is not False
                else "FILE_CREATE_FAILED",
                (
                    "The file was published but successful completion could not be confirmed."
                    if exc.published is True
                    else "The destination may have been created; completion is uncertain."
                    if exc.published is None
                    else "The destination file was not created."
                )
                + (
                    f" Filesystem error: {errno.errorcode.get(exc.failure_errno, str(exc.failure_errno))}."
                    if exc.failure_errno is not None
                    else ""
                ),
                "Inspect the requested path before deciding whether to retry. Parent directories may remain.",
            )
        except (ValueError, OSError):
            return _export_error(
                call,
                "INVALID_DESTINATION",
                "The destination could not be resolved or is outside the allowed write scope. No destination was created.",
            )
        return _export_result(call, payload, success=True)


def _export_payload_fits(call: ToolCall, payload: dict[str, Any]) -> bool:
    output = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (
        len(output.encode("utf-8")) <= CANONICAL_TOOL_RESULT_PREVIEW_HARD_BYTES
        and conservative_artifact_page_logical_utf8_bytes(
            tool_call_id=call.id,
            body=output,
            model_visible_memory_ids=(),
        )
        <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    )


def _export_boundary(call: ToolCall) -> ToolExecutionResult:
    return ArtifactReadTool._json_result(
        call,
        status=ToolResultState.ERROR,
        payload={
            "status": "resource_boundary",
            "error_code": "ARTIFACT_EXPORT_NOT_INLINEABLE",
            "error": "Export metadata exceeds the inline result boundary. No destination was created.",
        },
    )


def _export_result(
    call: ToolCall, payload: dict[str, Any], *, success: bool
) -> ToolExecutionResult:
    if not _export_payload_fits(call, payload):
        return _export_boundary(call)
    return ArtifactReadTool._json_result(
        call,
        status=ToolResultState.SUCCESS if success else ToolResultState.ERROR,
        payload=payload,
    )


def _export_error(
    call: ToolCall, code: str, message: str, hint: str = ""
) -> ToolExecutionResult:
    return _export_result(
        call,
        {
            "status": "error",
            "error": code,
            "message": message,
            "_hint": hint,
        },
        success=False,
    )


def _request(
    arguments: object,
) -> tuple[str, int, int] | str:
    if not isinstance(arguments, dict):
        # FrozenToolJsonDict is a dict subclass; keep the check deliberately
        # closed so arbitrary Mapping implementations cannot smuggle values.
        return "artifact_read arguments must be an object"
    allowed_keys = {"artifact_id", "offset_chars", "max_chars"}
    if not set(arguments).issubset(allowed_keys):
        return "artifact_read arguments contain unknown properties"
    artifact_id = arguments.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return "artifact_id must be a non-empty string"
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
    return artifact_id, offset, maximum


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
    }


def _bounded_text_payload(
    text_slice: object,
    *,
    tool_call_id: str,
) -> dict[str, Any]:
    record = text_slice.record
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

    def logical_bytes(payload: dict[str, Any]) -> int:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return conservative_artifact_page_logical_utf8_bytes(
            tool_call_id=tool_call_id,
            body=body,
            model_visible_memory_ids=record.model_visible_memory_fact_ids,
        )

    winner = build("")
    if logical_bytes(winner) > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
        raise _ArtifactPageLogicalBoundError
    candidate = build(text)
    if logical_bytes(candidate) <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
        return candidate
    low = 0
    high = len(text)
    while low <= high:
        length = (low + high) // 2
        current = build(text[:length])
        if logical_bytes(current) <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
            winner = current
            low = length + 1
        else:
            high = length - 1
    return winner


def _not_found_payload(artifact_id: str) -> dict[str, Any]:
    return {
        "status": "not_found",
        "artifact_id": artifact_id,
        "error": "artifact not found",
    }


__all__ = [
    "ArtifactReadTool",
    "ArtifactExportTool",
    "DEFAULT_ARTIFACT_READ_CHARS",
    "MAX_ARTIFACT_READ_CHARS",
]
