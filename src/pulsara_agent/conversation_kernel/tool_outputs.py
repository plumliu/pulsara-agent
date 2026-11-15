"""One complete business value, projected from canonical fields and content."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Mapping

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.content_parts import ResultPart, public_result_parts, read_content_parts
from pulsara_agent.identity import require_id
from pulsara_agent.ports.tool_execution import (
    FrozenToolJsonDict, ToolBusinessValue, encode_tool_public_json, freeze_tool_json_object, thaw_tool_json_object,
)
from pulsara_agent.ports.tool_output import (
    ToolOutputContentError, ToolResultView, parse_tool_output_target,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane, VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class PreparedToolResult:
    public_fields: FrozenToolJsonDict
    content_parts: tuple[ResultPart, ...]
    result_facts: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)

    def __post_init__(self) -> None:
        value = ToolBusinessValue(self.public_fields, self.content_parts)
        object.__setattr__(self, "public_fields", value.public_fields)
        object.__setattr__(self, "content_parts", value.content)
        object.__setattr__(self, "result_facts", freeze_tool_json_object(self.result_facts))


class ToolOutputProcessor:
    """Freeze the complete declaration; the accepting transaction owns storage."""

    @staticmethod
    def prepare(*, business_value: ToolBusinessValue,
                result_facts: Mapping[str, object] | None = None) -> PreparedToolResult:
        if not isinstance(business_value, ToolBusinessValue):
            raise TypeError("complete typed business value is required")
        return PreparedToolResult(business_value.public_fields, business_value.content,
                                  freeze_tool_json_object(result_facts or {}))


def public_tool_result_state(state: str) -> str:
    if state == "SUCCESS":
        return "SUCCESS"
    if state in {"CANCELLED", "CANCELLED_BEFORE_DISPATCH"}:
        return "CANCELLED"
    if state in {"ERROR", "INVALID_ARGUMENTS", "APPLICATION_ERROR", "SYSTEM_ERROR", "PERMISSION_DENIED", "TOOL_UNAVAILABLE"}:
        return "ERROR"
    raise ValueError("unknown tool execution state")


def assemble_tool_business_value(*, public_fields: Mapping[str, object],
        content_parts: tuple[ResultPart, ...], result_state: str,
        ) -> dict[str, object]:
    """Read authoritative status/time, without consulting mutable business owners."""
    declared = ToolBusinessValue(public_fields, content_parts)
    value = thaw_tool_json_object(declared.public_fields)
    meta = value.pop("meta", {})
    result: dict[str, object] = {
        **value,
        "meta": {"status": public_tool_result_state(result_state), **meta},
        "content": [{"type": part.type, "value": part.value} for part in declared.content],
    }
    return thaw_tool_json_object(freeze_tool_json_object(result))


def read_accepted_tool_business_value(connection, row: Mapping[str, object]) -> dict[str, object]:
    if row["output_unavailability_reason"] is not None:
        raise ToolOutputContentError(str(row["output_unavailability_reason"]))
    stored_parts = read_content_parts(connection, entry_id=str(row["result_entry_id"]),
                                     session_id=str(row["session_id"]))
    parts = public_result_parts(stored_parts)
    if len(parts) != len(stored_parts):
        raise ToolOutputContentError("tool result contains non-business content")
    return assemble_tool_business_value(public_fields=row["public_fields"], content_parts=parts,
        result_state=str(row["result_state"]))


def project_tool_result_export(value: Mapping[str, object] | str) -> bytes:
    """Complete UTF-8 projection; unpaired code units remain visible escapes.

    Stored text and JS values retain their original code units. UTF-8 display
    and export use the same explicit escaping as other model text projections;
    ordinary Unicode, NUL and original line endings stay byte-identical.
    """
    if isinstance(value, str):
        return value.encode("utf-8", errors="backslashreplace")
    fields = {key: item for key, item in value.items() if key != "content"}
    result = bytearray(encode_tool_public_json(fields, indent=2, sort_keys=True) + b"\n")
    for part in value["content"]:
        kind, item = part["type"], part["value"]
        if kind == "text":
            result.extend(item.encode("utf-8", errors="backslashreplace"))
        elif kind in {"image", "target"}:
            result.extend(encode_tool_public_json(item, sort_keys=True) + b"\n")
        elif kind == "object":
            result.extend(encode_tool_public_json(item, indent=2, sort_keys=True) + b"\n")
        else:
            raise ToolOutputContentError("unrecognized public content kind")
    return bytes(result)


class PostgresToolOutputReadPort:
    def __init__(self, connection_provider: VerifiedPostgresConnectionProviderProtocol,
                 *, session_id: str, workspace_id: str, operation_timeout_seconds: float = 12.0) -> None:
        require_id(session_id, "session")
        require_id(workspace_id, "workspace")
        if operation_timeout_seconds <= 0:
            raise ValueError("result read timeout must be positive")
        self._provider, self._session_id, self._workspace_id = connection_provider, session_id, workspace_id
        self._timeout = operation_timeout_seconds

    def read_result(self, target: Mapping[str, object]) -> ToolResultView:
        selected = parse_tool_output_target(target)
        with self._provider.connection(lane=PostgresConnectionLane.ARTIFACT,
                row_factory=dict_row, deadline_monotonic=monotonic() + self._timeout,
                isolation_level=IsolationLevel.REPEATABLE_READ) as connection:
            row = connection.execute(
                """SELECT r.* FROM pulsara_v3.tool_results r
                   JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id
                     AND e.id=r.result_entry_id AND e.workspace_id=r.workspace_id
                   WHERE r.session_id=%s AND r.workspace_id=%s AND r.result_entry_id=%s
                     AND e.entry_kind='TOOL_RESULT'""",
                (self._session_id, self._workspace_id, selected["entry_id"]),
            ).fetchone()
            if row is None:
                raise KeyError(selected["entry_id"])
            return ToolResultView(selected, read_accepted_tool_business_value(connection, row))


__all__ = ["PreparedToolResult", "ToolOutputProcessor", "PostgresToolOutputReadPort",
    "assemble_tool_business_value", "read_accepted_tool_business_value", "project_tool_result_export",
    "public_tool_result_state"]
