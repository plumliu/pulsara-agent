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
    NativeToolContinuation, ToolOutputContentError, ToolResultView, parse_tool_output_target,
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
                """SELECT r.*, COALESCE(c.tool_name,i.tool_name,'') AS source_tool_name,
                          COALESCE(c.tool_arguments,i.original_arguments,'{}'::jsonb) AS source_arguments
                   FROM pulsara_v3.tool_results r
                   JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id
                     AND e.id=r.result_entry_id AND e.workspace_id=r.workspace_id
                   LEFT JOIN pulsara_v3.tool_invocations i ON i.id=r.invocation_id AND i.session_id=r.session_id
                   LEFT JOIN pulsara_v3.content_parts c ON c.entry_id=r.tool_call_entry_id
                     AND c.session_id=r.session_id AND c.tool_call_id=r.tool_call_id AND c.type='tool_call'
                   WHERE r.session_id=%s AND r.workspace_id=%s AND r.result_entry_id=%s
                     AND e.entry_kind='TOOL_RESULT'""",
                (self._session_id, self._workspace_id, selected["entry_id"]),
            ).fetchone()
            if row is None:
                raise KeyError(selected["entry_id"])
            if row.get("source_result_entry_id") is not None and str(row["source_result_entry_id"]) != selected["entry_id"]:
                raise ToolOutputContentError("a native continuation observation is not a complete business result")
            return ToolResultView(selected, read_accepted_tool_business_value(connection, row),
                row.get("id"), str(row.get("source_tool_name", "")), row.get("source_arguments", {}),
                row.get("observed_at"), row.get("observation_duration_microseconds"))

    def read_native_page(self, target: Mapping[str, object]) -> NativeToolContinuation:
        """Resolve a NEXT's saved source and start; final page admission is native."""
        from pulsara_agent.conversation_kernel.native_tool_delivery import resolve_tool_result_next
        entry_id, position = resolve_tool_result_next(target, session_id=self._session_id)
        source = self.read_result({"kind": "SESSION_ENTRY", "entry_id": entry_id})
        return NativeToolContinuation(source, (position.segment, position.offset))

    def select_native_page(self, source: ToolResultView, *, position=(0, 0), supports_images=True):
        with self._provider.connection(lane=PostgresConnectionLane.ARTIFACT,
                row_factory=dict_row, deadline_monotonic=monotonic()+self._timeout,
                isolation_level=IsolationLevel.REPEATABLE_READ) as connection:
            return native_tool_page_in_connection(connection, session_id=self._session_id,
                source=source, position=position, supports_images=supports_images)


__all__ = ["PreparedToolResult", "ToolOutputProcessor", "PostgresToolOutputReadPort",
    "assemble_tool_business_value", "read_accepted_tool_business_value", "project_tool_result_export",
    "public_tool_result_state", "native_tool_observation_in_connection",
    "read_tool_result_source_in_connection", "native_tool_page_in_connection",
    "restore_native_tool_observation", "prepare_native_tool_observation",
    "project_native_settlement"]


def read_tool_result_source_in_connection(connection, *, session_id: str, entry_id: str,
                                         workspace_id: str | None = None) -> ToolResultView:
    row = connection.execute(
        """SELECT r.*, COALESCE(c.tool_name,i.tool_name,'') AS source_tool_name,
                  COALESCE(c.tool_arguments,i.original_arguments,'{}'::jsonb) AS source_arguments
           FROM pulsara_v3.tool_results r
           LEFT JOIN pulsara_v3.tool_invocations i ON i.id=r.invocation_id AND i.session_id=r.session_id
           LEFT JOIN pulsara_v3.content_parts c ON c.entry_id=r.tool_call_entry_id
             AND c.session_id=r.session_id AND c.tool_call_id=r.tool_call_id AND c.type='tool_call'
           WHERE r.session_id=%s AND r.result_entry_id=%s
             AND (%s::text IS NULL OR r.workspace_id=%s)""",
        (session_id, entry_id, workspace_id, workspace_id),
    ).fetchone()
    if row is None:
        raise KeyError(entry_id)
    if row["source_result_entry_id"] not in (None, entry_id):
        raise ToolOutputContentError("native continuation observations do not own a complete result")
    return ToolResultView({"kind": "SESSION_ENTRY", "entry_id": entry_id},
        read_accepted_tool_business_value(connection, row), row["id"], row["source_tool_name"],
        row["source_arguments"], row["observed_at"], row["observation_duration_microseconds"])


def native_tool_page_in_connection(connection, *, session_id: str, source: ToolResultView,
        position=(0, 0), supports_images: bool = True, projection=None):
    from pulsara_agent.conversation_kernel.native_tool_delivery import (
        NativeResultPosition, NativeToolPage, project_native_tool_page, validate_native_projection,
    )
    from pulsara_agent.conversation_kernel.prompt_storage import read_image_reference_in_connection

    def image_reader(image_ref):
        return read_image_reference_in_connection(connection, image_ref=image_ref)

    def dimensions(image_ref):
        row = connection.execute("SELECT width,height FROM pulsara_v3.blobs WHERE id=%s", (image_ref,)).fetchone()
        if row is None:
            raise KeyError(image_ref)
        return row["width"], row["height"]

    frozen = None if projection is None else validate_native_projection(source.value, projection)
    if projection is not None and projection["page_error"] is not None:
        return NativeToolPage(source.target["entry_id"], frozen, "", (), projection["next"], "ERROR", 0, projection["page_error"])
    return project_native_tool_page(session_id=session_id, entry_id=source.target["entry_id"],
        value=source.value, image_reader=image_reader, image_dimensions=dimensions,
        source_tool_name=source.source_tool_name, source_arguments=source.source_arguments,
        start=NativeResultPosition(*position), supports_images=supports_images,
        frozen_range=frozen, frozen_next=None if projection is None else projection["next"])


def restore_native_tool_observation(connection, result):
    """Restore the exact accepted range and NEXT, without repagination or source effects."""
    from pulsara_agent.conversation_kernel.native_tool_delivery import render_native_tool_page
    source_id, projection = result["source_result_entry_id"], result["native_projection"]
    if source_id is None or projection is None:
        raise ToolOutputContentError("native observation has no frozen result projection")
    source = read_tool_result_source_in_connection(connection,
        session_id=str(result.get("result_session_id") or result["session_id"]), entry_id=source_id)
    page = native_tool_page_in_connection(connection, session_id=str(result["session_id"]),
        source=source, projection=projection)
    text = render_native_tool_page(page, observed_at=result["observed_at"],
        duration_microseconds=result["observation_duration_microseconds"],
        continuation=str(result["result_entry_id"]) != source_id)
    return page, text


def prepare_native_tool_observation(repository, guard, *, workspace_id: str,
        result_entry_id: str, supports_images: bool = True, deadline_monotonic: float,
        prepared_page=None):
    """Prepare a candidate; only actual provider-input installation freezes it."""
    with repository.connection_provider.connection(lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row, deadline_monotonic=deadline_monotonic) as connection:
        row = connection.execute("SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s AND result_entry_id=%s AND workspace_id=%s",
            (guard.session_id, result_entry_id, workspace_id)).fetchone()
        if row is None:
            raise KeyError(result_entry_id)
        if prepared_page is not None:
            source = read_tool_result_source_in_connection(connection,
                session_id=guard.session_id, entry_id=prepared_page.source_entry_id)
            from pulsara_agent.conversation_kernel.native_tool_delivery import bind_native_observation
            page = native_tool_page_in_connection(connection, session_id=guard.session_id,
                source=source, projection=prepared_page.projection_facts())
            return bind_native_observation(page,observed_at=row["observed_at"],
                duration_microseconds=row["observation_duration_microseconds"],
                continuation=row["result_entry_id"] != page.source_entry_id)
        page, _pending = native_tool_observation_in_connection(connection, row, supports_images=supports_images)
        return page


def project_native_settlement(settlement, page):
    from dataclasses import replace
    from pulsara_agent.conversation_kernel.native_tool_delivery import render_native_tool_page
    from pulsara_agent.llm.input import LLMTextPart
    metadata = settlement.public_projection.metadata
    from datetime import datetime
    text = render_native_tool_page(page, observed_at=datetime.fromisoformat(metadata.timing.observed_at_utc.replace("Z", "+00:00")),
        duration_microseconds=metadata.timing.observation_duration_microseconds,
        continuation=settlement.result_entry_id != page.source_entry_id)
    return replace(settlement, public_projection=replace(settlement.public_projection, canonical_body=text),
        canonical_content=(LLMTextPart(text), *page.images) if page.images else None)


def native_tool_observation_in_connection(connection, result, *, supports_images=True):
    from pulsara_agent.conversation_kernel.native_tool_delivery import bind_native_observation, NativeResultPosition, failed_native_tool_page
    session_id = str(result.get("result_session_id") or result["session_id"])
    source_id = result["source_result_entry_id"] or result["result_entry_id"]
    source = read_tool_result_source_in_connection(connection, session_id=session_id, entry_id=source_id)
    projection = result["native_projection"]
    if projection is None:
        start = ((result["result_facts"]["native_start"])
            if source_id != result["result_entry_id"] else (0, 0))
        try:
            page = native_tool_page_in_connection(connection, session_id=session_id,
                source=source, position=start, supports_images=supports_images)
        except Exception as error:
            page = failed_native_tool_page(session_id=session_id, entry_id=source_id,
                start=NativeResultPosition.parse(start), value=source.value, error=error)
    else:
        page = native_tool_page_in_connection(connection, session_id=session_id, source=source, projection=projection)
    page = bind_native_observation(page, observed_at=result["observed_at"],
        duration_microseconds=result["observation_duration_microseconds"],
        continuation=result["result_entry_id"] != source_id)
    return page, projection is None
