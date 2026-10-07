"""Cold ROOT history queries over canonical records; cursors are positions only."""

from __future__ import annotations

import base64
import json
from time import monotonic

from pulsara_agent.conversation_kernel.session_search import (
    search_session_page,
    SNIPPET_CHARACTERS,
)
from pulsara_agent.conversation_kernel.session_text import (
    decode_saved_text,
    keyword_patterns,
    match_position,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    ARTIFACT_READ_DEFAULT_CHARS,
    ARTIFACT_READ_HARD_CHARS,
    PostgresToolArtifactReadPort,
)
from pulsara_agent.ports.session_content import (
    SESSION_QUERY_MAX_ITEMS,
    SESSION_QUERY_TOOL_NAMES,
    SessionContentRange,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_artifact_page_logical_utf8_bytes,
)
from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader


class SessionQueryError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _encode(value):
    return base64.urlsafe_b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()


def _decode(cursor, operation):
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        expected = operation if operation == "search_sessions" else operation + ".v2"
        if not isinstance(value, dict) or value.get("operation") != expected:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SessionQueryError(
            "CURSOR_INVALID", "Invalid cursor for this operation; restart the query."
        ) from exc


def _integer(value, name, *, minimum=0, maximum=None):
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise SessionQueryError("INVALID_REQUEST", f"Invalid {name}.")
    return value


def _string(value, name):
    if not isinstance(value, str) or not value:
        raise SessionQueryError("INVALID_REQUEST", f"Invalid {name}.")
    return value


def _fits(payload, tool_call_id):
    return (
        conservative_artifact_page_logical_utf8_bytes(
            tool_call_id=tool_call_id,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            model_visible_memory_ids=(),
        )
        <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    )


def _resource_error():
    return SessionQueryError(
        "RESULT_BUDGET_EXCEEDED",
        "One readable item and its cursor cannot fit the tool result budget.",
    )


_ELIGIBLE = """(e.entry_kind IN ('USER_MESSAGE','USER_STEER') OR
    (e.entry_kind IN ('ASSISTANT_MESSAGE','ASSISTANT_TOOL_REQUEST') AND EXISTS (
      SELECT 1 FROM pulsara_v3.assistant_message_blocks a
      WHERE a.session_id=e.session_id AND a.assistant_entry_id=e.id
        AND (a.block_kind='TEXT' OR (%s AND a.block_kind='TOOL_CALL'))))
    OR (%s AND e.entry_kind='TOOL_RESULT'))"""


def _assistant_document(blocks):
    if not any(block["block_kind"] == "TOOL_CALL" for block in blocks):
        text = "\n".join(decode_saved_text(block) for block in blocks)
        return text, ((0, text),), ()
    pieces, segments, bounds = [], [], []
    offset = 0
    for block in blocks:
        if pieces:
            offset += 1
        if block["block_kind"] == "TEXT":
            piece = decode_saved_text(block)
            segments.append((offset, piece))
        else:
            name = block["tool_name"]
            arguments = canonical_json_bytes(block["tool_arguments"]).decode("utf-8")
            prefix, separator = "Tool call: ", "\nArguments: "
            piece = prefix + name + separator + arguments
            if name not in SESSION_QUERY_TOOL_NAMES:
                segments.extend(
                    (
                        (offset + len(prefix), name),
                        (offset + len(prefix) + len(name) + len(separator), arguments),
                    )
                )
        bounds.append((offset, offset + len(piece)))
        offset += len(piece)
        pieces.append(piece)
    return "\n".join(pieces), tuple(segments), tuple(bounds)


class SessionContentQuery:
    """One cold owner, injected with the caller domain and exact installed range."""

    def __init__(self, repository, *, session_id: str, memory_domain_id: str):
        self.repository = repository
        self.session_id = session_id
        self.memory_domain_id = memory_domain_id
        self.reader = CanonicalProtocolReader(repository.connection_provider)

    def invoke(
        self,
        operation,
        arguments,
        *,
        current_range: SessionContentRange,
        tool_call_id: str,
        deadline_monotonic: float,
    ):
        if operation not in SESSION_QUERY_TOOL_NAMES:
            raise SessionQueryError("INVALID_REQUEST", "Unknown session query.")
        allowed = {"cursor", "limit"}
        allowed |= (
            {"query", "lifecycle"}
            if operation == "search_sessions"
            else {"session_id", "include_tools", "query"}
            if operation == "search_session_content"
            else {"session_id", "include_tools", "entry_id", "direction", "max_chars"}
        )
        if not set(arguments) <= allowed:
            raise SessionQueryError(
                "INVALID_REQUEST", "Unknown session query arguments."
            )
        limit = _integer(
            arguments.get("limit", 20),
            "limit",
            minimum=1,
            maximum=SESSION_QUERY_MAX_ITEMS,
        )
        maximum = _integer(
            arguments.get("max_chars", ARTIFACT_READ_DEFAULT_CHARS),
            "max_chars",
            minimum=1,
            maximum=ARTIFACT_READ_HARD_CHARS,
        )
        cursor = None
        if "cursor" in arguments:
            if set(arguments) - {"cursor", "limit", "max_chars"}:
                raise SessionQueryError(
                    "CURSOR_CONFLICT",
                    "Continue with only cursor and optional page budgets.",
                )
            cursor = _decode(_string(arguments["cursor"], "cursor"), operation)
        with self.reader._connection(deadline_monotonic) as connection:
            # Also reject queries from a deleted or out-of-domain caller.
            self._authorized(connection, self.session_id)
            if operation == "search_sessions":
                payload = self._sessions(
                    connection,
                    arguments,
                    cursor,
                    current_range,
                    limit,
                    tool_call_id,
                    deadline_monotonic,
                )
            else:
                payload = self._content(
                    connection,
                    operation,
                    arguments,
                    cursor,
                    current_range,
                    limit,
                    maximum,
                    tool_call_id,
                    deadline_monotonic,
                )
            if not _fits(payload, tool_call_id):
                raise _resource_error()
            return payload

    def _authorized(self, connection, target):
        try:
            return self.reader.authorized_session(
                connection, target, self.memory_domain_id
            )
        except KeyError as exc:
            raise SessionQueryError(
                "SESSION_NOT_ACCESSIBLE",
                "Session is unavailable or outside the caller's domain.",
            ) from exc

    def _check_range(self, connection, end, excluded, actual):
        _integer(end, "cursor range")
        if excluded is not None:
            _string(excluded, "cursor exclusion")
        if end > actual.through_sequence:
            raise SessionQueryError(
                "CURSOR_RANGE_CHANGED",
                "The earlier-content range changed; restart the query.",
            )
        if (
            actual.excluded_entry_id is not None
            and excluded != actual.excluded_entry_id
        ):
            active = connection.execute(
                "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s",
                (self.session_id, actual.excluded_entry_id),
            ).fetchone()
            if active is None or active["entry_sequence"] <= end:
                raise SessionQueryError(
                    "CURSOR_RANGE_CHANGED",
                    "The active request cannot be read; restart the query.",
                )

    def _sessions(
        self, connection, arguments, cursor, actual, limit, call_id, deadline
    ):
        if cursor is None:
            query = arguments.get("query", "")
            lifecycle = arguments.get("lifecycle", "ALL")
            end, excluded, inner = (
                actual.through_sequence,
                actual.excluded_entry_id,
                None,
            )
        else:
            expected = {
                "operation",
                "caller_session_id",
                "query",
                "lifecycle",
                "range_end",
                "excluded",
                "inner",
            }
            if (
                set(cursor) != expected
                or cursor["caller_session_id"] != self.session_id
            ):
                raise SessionQueryError(
                    "CURSOR_INVALID",
                    "Session search cursor does not match this caller.",
                )
            query, lifecycle = cursor["query"], cursor["lifecycle"]
            end, excluded, inner = (
                cursor["range_end"],
                cursor["excluded"],
                cursor["inner"],
            )
            _string(inner, "cursor position")
            self._check_range(connection, end, excluded, actual)
        if (
            not isinstance(query, str)
            or not isinstance(lifecycle, str)
            or lifecycle not in {"ALL", "OPEN", "ARCHIVED"}
        ):
            raise SessionQueryError("INVALID_REQUEST", "Invalid query or lifecycle.")

        def token(inner_cursor):
            return (
                None
                if inner_cursor is None
                else _encode(
                    {
                        "operation": "search_sessions",
                        "caller_session_id": self.session_id,
                        "query": query,
                        "lifecycle": lifecycle,
                        "range_end": end,
                        "excluded": excluded,
                        "inner": inner_cursor,
                    }
                )
            )

        # The existing live discovery ordering has no global history snapshot.
        found, after = search_session_page(
            self.repository,
            memory_domain_id=self.memory_domain_id,
            query=query,
            lifecycle=lifecycle,
            cursor=inner,
            limit=limit,
            deadline_monotonic=deadline,
            current_session_id=self.session_id,
            current_range=SessionContentRange(end, excluded),
            read_connection=connection,
        )
        items = []
        for row in found:
            item = {
                "session_id": row["id"],
                "title": row["title"]
                or "会话 " + row["id"].removeprefix("session:")[:8],
                "project": row["workspace_label"] or row["workspace_root"],
                "updated_at": row["updated_at"].isoformat(),
            }
            if "entry_id" in row:
                item.update(
                    {key: row[key] for key in ("entry_id", "role", "at", "text")}
                )
                if row["partial"]:
                    item["partial"] = True
                if row["scheduled_input"] is not None:
                    item["origin"] = "scheduled"
            # A live keyset cursor after this row permits a smaller response page.
            row_cursor = _encode(
                {
                    "query": query.strip(),
                    "lifecycle": lifecycle,
                    "rank": row["rank"],
                    "at": row["updated_at"].isoformat(),
                    "id": row["id"],
                }
            )
            candidate = {"items": [*items, item], "next_cursor": token(row_cursor)}
            if not _fits(candidate, call_id):
                if not items:
                    raise _resource_error()
                return {"items": items, "next_cursor": token(inner)}
            items.append(item)
            inner = row_cursor
        payload = {"items": items, "next_cursor": token(after)}
        if not _fits(payload, call_id):
            raise _resource_error()
        return payload

    def _content(
        self,
        connection,
        operation,
        arguments,
        cursor,
        actual,
        limit,
        maximum,
        call_id,
        deadline,
    ):
        if cursor is None:
            target = _string(arguments.get("session_id", self.session_id), "session_id")
            tools = arguments.get("include_tools", False)
            query = arguments.get("query")
            direction = arguments.get(
                "direction", "newer" if "entry_id" in arguments else "older"
            )
            excluded = actual.excluded_entry_id if target == self.session_id else None
        else:
            expected = {
                "operation",
                "session_id",
                "cut",
                "event_cut",
                "range_end",
                "excluded",
                "position",
                "offset",
                "include_tools",
                "query",
                "direction",
            }
            if set(cursor) != expected:
                raise SessionQueryError(
                    "CURSOR_INVALID", "Invalid content cursor fields."
                )
            target = _string(cursor["session_id"], "session_id")
            tools, query, direction, excluded = (
                cursor["include_tools"],
                cursor["query"],
                cursor["direction"],
                cursor["excluded"],
            )
        if (
            type(tools) is not bool
            or not isinstance(direction, str)
            or direction not in {"older", "newer"}
        ):
            raise SessionQueryError(
                "INVALID_REQUEST", "Invalid content filter or direction."
            )
        if operation == "search_session_content":
            if not isinstance(query, str) or not query.strip() or direction != "older":
                raise SessionQueryError(
                    "INVALID_REQUEST", "A first content search needs literal keywords."
                )
        elif query is not None:
            raise SessionQueryError(
                "CURSOR_INVALID", "Read cursor cannot contain a search query."
            )
        session = self._authorized(connection, target)
        if cursor is None:
            cut, event_cut = (
                int(session["latest_entry_sequence"]),
                int(session["latest_event_sequence"]),
            )
            end = (
                min(cut, actual.through_sequence) if target == self.session_id else cut
            )
            offset = 0
            position = end if direction == "older" else 1
            if "entry_id" in arguments:
                anchor = connection.execute(
                    """SELECT e.entry_sequence FROM pulsara_v3.transcript_entries e
                       WHERE e.session_id=%s AND e.id=%s AND e.conversation_scope_kind='ROOT'
                         AND e.entry_sequence<=%s AND e.id IS DISTINCT FROM %s AND """
                    + _ELIGIBLE,
                    (
                        target,
                        _string(arguments["entry_id"], "entry_id"),
                        end,
                        excluded,
                        tools,
                        tools,
                    ),
                ).fetchone()
                if anchor is None:
                    raise SessionQueryError(
                        "ENTRY_NOT_READABLE",
                        "Entry does not belong to this session's readable range.",
                    )
                position = int(anchor["entry_sequence"])
        else:
            cut = _integer(cursor["cut"], "cursor cut")
            event_cut = _integer(cursor["event_cut"], "cursor event cut")
            end = _integer(cursor["range_end"], "cursor range", maximum=cut)
            position = _integer(
                cursor["position"], "cursor position", minimum=1, maximum=end
            )
            offset = _integer(cursor["offset"], "cursor offset")
            if operation == "search_session_content" and offset:
                raise SessionQueryError(
                    "CURSOR_INVALID", "Search cursor has a text offset."
                )
            if target == self.session_id:
                self._check_range(connection, end, excluded, actual)
            elif excluded is not None:
                raise SessionQueryError(
                    "CURSOR_INVALID", "Other sessions have no active request exclusion."
                )
        self.reader.validate_history_cut(connection, session, cut, event_cut)
        if target == self.session_id and end == 0:
            return {"items": [], "next_cursor": None, "note": "没有更早的会话内容。"}

        def token(sequence, start):
            return _encode(
                {
                    "operation": operation + ".v2",
                    "session_id": target,
                    "cut": cut,
                    "event_cut": event_cut,
                    "range_end": end,
                    "excluded": excluded,
                    "position": sequence,
                    "offset": start,
                    "include_tools": tools,
                    "query": query,
                    "direction": direction,
                }
            )

        compare, order = ("<=", "DESC") if direction == "older" else (">=", "ASC")
        with connection.cursor(name="session_content") as stream:
            stream.itersize = SESSION_QUERY_MAX_ITEMS
            stream.execute(
                """SELECT e.* FROM pulsara_v3.transcript_entries e
                WHERE e.session_id=%s AND e.conversation_scope_kind='ROOT' AND e.entry_sequence<=%s
                  AND e.id IS DISTINCT FROM %s AND e.entry_sequence """
                + compare
                + " %s AND "
                + _ELIGIBLE
                + " ORDER BY e.entry_sequence "
                + order,
                (target, end, excluded, position, tools, tools),
            )
            if cursor is not None and offset:
                # An offset must name the exact eligible entry, never a later one.
                probe = connection.execute(
                    "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_sequence=%s AND conversation_scope_kind='ROOT'",
                    (target, position),
                ).fetchone()
                if probe is None or probe["id"] == excluded:
                    raise SessionQueryError(
                        "CURSOR_INVALID", "Text cursor entry is no longer readable."
                    )
            if operation == "search_session_content":
                return self._search_page(
                    connection,
                    stream,
                    query,
                    event_cut,
                    token,
                    limit,
                    call_id,
                    deadline,
                    tools,
                )
            return self._read_page(
                connection,
                stream,
                position,
                offset,
                event_cut,
                token,
                limit,
                maximum,
                call_id,
                deadline,
                tools,
            )

    def _document(
        self, connection, entry, event_cut, deadline, include_tools, *, for_search=False
    ):
        if monotonic() >= deadline:
            raise TimeoutError("session content query deadline exceeded")
        kind = entry["entry_kind"]
        role = (
            "user"
            if kind in {"USER_MESSAGE", "USER_STEER"}
            else "tool"
            if kind == "TOOL_RESULT"
            else "assistant"
        )
        item = {
            "session_id": entry["session_id"],
            "entry_id": entry["id"],
            "role": role,
            "at": entry["accepted_at"].isoformat(),
        }
        if entry["scheduled_input"] is not None:
            item["origin"] = "scheduled"
        fact = None
        if role == "tool":
            # Resolve ownership before loading result/artifact bodies. Missing
            # canonical edges are corruption, not an excuse to hide a result.
            fact = connection.execute(
                """SELECT r.*,a.tool_name FROM pulsara_v3.tool_results r
                JOIN pulsara_v3.assistant_message_blocks a ON a.session_id=r.session_id
                  AND a.assistant_entry_id=r.tool_call_entry_id AND a.tool_call_id=r.tool_call_id
                  AND a.block_kind='TOOL_CALL'
                WHERE r.session_id=%s AND r.result_entry_id=%s""",
                (entry["session_id"], entry["id"]),
            ).fetchone()
            if fact is None:
                raise RuntimeError("saved tool result has no canonical edge")
            if for_search and fact["tool_name"] in SESSION_QUERY_TOOL_NAMES:
                return None
        bounds = ()
        if role == "assistant":
            blocks = connection.execute(
                """SELECT COALESCE(a.inline_content,b.body) AS content,
                a.content_size AS size,a.content_digest AS digest,'assistant' AS kind,
                a.block_kind,a.tool_name,a.tool_arguments
                FROM pulsara_v3.assistant_message_blocks a LEFT JOIN pulsara_v3.blobs b
                  ON b.id=a.blob_id AND b.workspace_id=a.workspace_id
                WHERE a.session_id=%s AND a.assistant_entry_id=%s
                  AND (a.block_kind='TEXT' OR (%s AND a.block_kind='TOOL_CALL'))
                ORDER BY a.block_ordinal""",
                (entry["session_id"], entry["id"], include_tools),
            ).fetchall()
            text, segments, bounds = _assistant_document(blocks)
        else:
            content = entry["inline_content"]
            if content is None:
                blob = connection.execute(
                    "SELECT body FROM pulsara_v3.blobs WHERE id=%s AND workspace_id=%s",
                    (entry["blob_id"], entry["workspace_id"]),
                ).fetchone()
                content = None if blob is None else blob["body"]
            text = decode_saved_text(
                {
                    "content": content,
                    "digest": entry["content_digest"],
                    "size": entry["content_size"],
                    "kind": role,
                    "media_type": entry["content_media_type"],
                }
            )
            segments = ((0, text),)
        if role == "tool":
            item.update(tool_name=fact["tool_name"], result_state=fact["result_state"])
            disposition = fact["output_artifact_disposition"]
            if (
                fact["output_source_coverage"] != "COMPLETE"
                or fact["output_display_kind"] != "COMPLETE"
                or disposition in {"INCOMPLETE", "UNAVAILABLE"}
            ):
                for source, name in (
                    ("output_source_coverage", "source_coverage"),
                    ("output_display_kind", "display_kind"),
                    ("output_artifact_disposition", "artifact_disposition"),
                    ("output_source_coverage_reason", "source_coverage_reason"),
                    (
                        "output_artifact_unavailability_reason",
                        "artifact_unavailability_reason",
                    ),
                ):
                    if fact[source] is not None:
                        item[name] = fact[source]
            if disposition in {"AVAILABLE", "INCOMPLETE"}:
                reference = self.reader.resolve_tool_artifact_reference(
                    session_id=entry["session_id"],
                    result_entry_id=entry["id"],
                    deadline_monotonic=deadline,
                    read_connection=connection,
                )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("session artifact query deadline exceeded")
                port = PostgresToolArtifactReadPort(
                    self.repository.connection_provider,
                    session_id=entry["session_id"],
                    workspace_id=reference["workspace_id"],
                    operation_timeout_seconds=remaining,
                )
                try:
                    body = port.read_body(
                        reference["output_artifact_id"]
                    ).content.decode("utf-8")
                except KeyError as exc:
                    raise RuntimeError("saved tool artifact is missing") from exc
                prefix, separator = "Saved tool result:\n", "\n\nTool output:\n"
                segments = (
                    (len(prefix), text),
                    (len(prefix) + len(text) + len(separator), body),
                )
                text = prefix + text + separator + body
        notices = self.reader._notices(
            connection, entry["session_id"], event_cut, (entry["entry_sequence"],)
        )
        if notices:
            if len(notices) != 1:
                raise RuntimeError("saved entry has multiple interruption endings")
            notice = notices[0]
            item["interruption"] = {"reason": notice.reason}
            if notice.public_detail:
                item["interruption"]["detail"] = notice.public_detail
        return item, text, segments, bounds

    def _search_page(
        self,
        connection,
        rows,
        query,
        event_cut,
        token,
        limit,
        call_id,
        deadline,
        include_tools,
    ):
        patterns = keyword_patterns(query)
        items = []
        for entry in rows:
            document = self._document(
                connection, entry, event_cut, deadline, include_tools, for_search=True
            )
            if document is None:
                continue
            item, text, segments, bounds = document
            hit = match_position(patterns, segments)
            if hit is None:
                continue
            if len(items) == limit:
                return {
                    "items": items,
                    "next_cursor": token(entry["entry_sequence"], 0),
                }
            start = max(0, hit - 70)
            end = start + SNIPPET_CHARACTERS
            if bounds:
                block_start, block_end = next(
                    span for span in bounds if span[0] <= hit < span[1]
                )
                start = max(start, block_start)
                end = min(start + SNIPPET_CHARACTERS, block_end)
            snippet = text[start:end]
            item["text"] = snippet
            if start or len(snippet) < len(text):
                item["partial"] = True
            payload = {
                "items": [*items, item],
                "next_cursor": token(entry["entry_sequence"], 0),
            }
            if not _fits(payload, call_id):
                if items:
                    return {
                        "items": items,
                        "next_cursor": token(entry["entry_sequence"], 0),
                    }
                # Preserve the locator and a real snippet when only the text
                # must shrink; metadata alone exceeding admission is an error.
                low, high, winner = 1, len(snippet), None
                while low <= high:
                    length = (low + high) // 2
                    smaller = {**item, "text": snippet[:length], "partial": True}
                    if _fits(
                        {"items": [smaller], "next_cursor": payload["next_cursor"]},
                        call_id,
                    ):
                        winner = smaller
                        low = length + 1
                    else:
                        high = length - 1
                if winner is None:
                    raise _resource_error()
                item = winner
            items.append(item)
        return {"items": items, "next_cursor": None}

    def _read_page(
        self,
        connection,
        rows,
        position,
        offset,
        event_cut,
        token,
        limit,
        maximum,
        call_id,
        deadline,
        include_tools,
    ):
        items = []
        remaining = maximum
        iterator = iter(rows)
        entry = next(iterator, None)
        if offset and (entry is None or entry["entry_sequence"] != position):
            raise SessionQueryError(
                "CURSOR_INVALID", "Text cursor does not identify an eligible entry."
            )
        while entry is not None:
            item, text, _, _ = self._document(
                connection, entry, event_cut, deadline, include_tools
            )
            if offset > len(text) or (offset == len(text) and offset):
                raise SessionQueryError(
                    "CURSOR_INVALID", "Text cursor is outside this message."
                )
            next_entry = next(iterator, None)
            take = min(remaining, len(text) - offset)

            def build(length):
                value = {**item, "text": text[offset : offset + length]}
                if offset or length < len(text):
                    value["partial"] = True
                after = (
                    token(entry["entry_sequence"], offset + length)
                    if offset + length < len(text)
                    else token(next_entry["entry_sequence"], 0)
                    if next_entry is not None
                    else None
                )
                return {"items": [*items, value], "next_cursor": after}

            payload = build(take)
            if not _fits(payload, call_id):
                low, high, winner = 1, take, None
                while low <= high:
                    middle = (low + high) // 2
                    candidate = build(middle)
                    if _fits(candidate, call_id):
                        winner = candidate
                        low = middle + 1
                    else:
                        high = middle - 1
                if winner is None:
                    if not items:
                        raise _resource_error()
                    return {
                        "items": items,
                        "next_cursor": token(entry["entry_sequence"], offset),
                    }
                return winner
            items = payload["items"]
            remaining -= take
            if offset + take < len(text) or not remaining or len(items) == limit:
                return payload
            entry, offset = next_entry, 0
        return {"items": items, "next_cursor": None}
