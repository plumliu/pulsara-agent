"""Complete frozen-scope conversation discovery, QA search and message reads."""

from __future__ import annotations

from time import monotonic

from psycopg import IsolationLevel
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.history_projection import (
    canonical_history_message,
)
from pulsara_agent.conversation_kernel.history_search import (
    FrozenHistoryReadScope,
    search_history,
    refetch_history,
)
from pulsara_agent.conversation_kernel.session_search import search_sessions_complete
from pulsara_agent.identity import require_id
from pulsara_agent.ports.session_content import (
    SessionContentRange,
    SESSION_QUERY_TOOL_NAMES,
    session_entry_target,
)
from pulsara_agent.ports.tool_execution import ToolBusinessValue
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


class SessionQueryError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def history_message_role(message):
    if message["role"] == "user":
        return "user steer" if message["label"] == "用户补充" else "user message"
    return (
        "assistant final answer"
        if message["label"] == "助手回答"
        else "assistant commentary"
    )


class SessionContentQuery:
    def __init__(self, repository, *, session_id, memory_domain_id):
        self.repository, self.session_id, self.memory_domain_id = (
            repository,
            session_id,
            memory_domain_id,
        )

    def invoke(
        self,
        operation,
        arguments,
        *,
        current_range: SessionContentRange,
        deadline_monotonic,
    ):
        if operation not in SESSION_QUERY_TOOL_NAMES:
            raise SessionQueryError("INVALID_REQUEST", "Unknown history query")
        allowed = (
            {"query", "lifecycle"}
            if operation == "search_sessions"
            else {"target", "query"}
            if operation == "search_session_content"
            else {"target", "limit"}
        )
        if set(arguments) - allowed:
            raise SessionQueryError(
                "INVALID_REQUEST", "Unknown history query arguments"
            )
        with self.repository.connection_provider.connection(
            lane=PostgresConnectionLane.MEMORY_QUERY,
            row_factory=dict_row,
            isolation_level=IsolationLevel.REPEATABLE_READ,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            self._authorized(connection, self.session_id)
            if operation == "search_sessions":
                query = arguments.get("query", "")
                lifecycle = arguments.get("lifecycle", "ALL")
                if (
                    not isinstance(query, str)
                    or not isinstance(lifecycle, str)
                    or lifecycle
                    not in {
                        "ALL",
                        "OPEN",
                        "ARCHIVED",
                    }
                ):
                    raise SessionQueryError(
                        "INVALID_REQUEST", "Invalid session query or lifecycle"
                    )
                records = search_sessions_complete(
                    connection,
                    memory_domain_id=self.memory_domain_id,
                    query=query,
                    lifecycle=lifecycle,
                    current_session_id=self.session_id,
                    current_range=current_range,
                    deadline_monotonic=deadline_monotonic,
                )
                return self._records(records, "没有找到匹配的会话。")
            target = arguments.get("target")
            session, anchor = self._target(
                connection, target, allow_entry=operation == "read_session_content"
            )
            bound = (
                current_range.through_sequence
                if session["id"] == self.session_id
                else int(session["latest_entry_sequence"])
            )
            excluded = (
                current_range.excluded_entry_id
                if session["id"] == self.session_id
                else None
            )
            if operation == "search_session_content":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise SessionQueryError(
                        "INVALID_REQUEST", "query must be nonblank text"
                    )
                scope = FrozenHistoryReadScope(
                    self.session_id, self.memory_domain_id, current_range
                )
                rows = search_history(
                    connection,
                    scope=scope,
                    query=query,
                    limit=None,
                    target_session_id=session["id"],
                )
                records = []
                for row in rows:
                    self._deadline(deadline_monotonic)
                    candidate = refetch_history(connection, row, scope=scope)
                    if candidate is not None:
                        text = "\n\n".join(
                            f"{history_message_role(message)} ({message['at']}):\n{message['text']}"
                            for message in candidate.messages
                        )
                        records.append(
                            {
                                "target": session_entry_target(candidate.q1_entry_id),
                                "text": text,
                            }
                        )
                return self._records(records, "没有找到匹配的对话。")
            limit = arguments.get("limit")
            if type(limit) is not int or limit == 0 or (anchor is None and limit > 0):
                raise SessionQueryError(
                    "INVALID_REQUEST",
                    "limit is required and nonzero; SESSION requires negative limit",
                )
            if anchor is not None:
                if (
                    int(anchor["entry_sequence"]) > bound
                    or anchor["id"] == excluded
                    or canonical_history_message(connection, anchor) is None
                ):
                    raise SessionQueryError(
                        "ENTRY_NOT_READABLE",
                        "The requested entry is not readable in this scope",
                    )
                start = int(anchor["entry_sequence"])
            else:
                start = bound
            compare, order = (">=", "ASC") if limit > 0 else ("<=", "DESC")
            records = []
            with connection.cursor(
                name="complete_history_read", row_factory=dict_row
            ) as stream:
                stream.itersize = 50
                stream.execute(
                    f"""SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s
                    AND entry_sequence<=%s AND entry_sequence {compare} %s AND id IS DISTINCT FROM %s
                    ORDER BY entry_sequence {order}""",
                    (session["id"], bound, start, excluded),
                )
                for entry in stream:
                    self._deadline(deadline_monotonic)
                    message = canonical_history_message(connection, entry)
                    if message is None:
                        continue
                    records.append(
                        {
                            "target": session_entry_target(entry["id"]),
                            "role": history_message_role(message),
                            "at": message["at"],
                            "text": message["text"],
                        }
                    )
                    if len(records) == abs(limit):
                        break
            return self._records(records, "该会话当前没有可读的历史消息。")

    @staticmethod
    def _records(records, empty):
        return ToolBusinessValue(
            {},
            tuple(ResultPart("object", record) for record in records)
            if records
            else (ResultPart("text", empty),),
        )

    def _authorized(self, connection, session_id):
        row = connection.execute(
            "SELECT * FROM pulsara_v3.sessions WHERE id=%s AND memory_domain_id=%s",
            (session_id, self.memory_domain_id),
        ).fetchone()
        if row is None:
            raise SessionQueryError(
                "SESSION_NOT_ACCESSIBLE",
                "Session is unavailable or outside this memory domain",
            )
        return row

    def _target(self, connection, target, *, allow_entry):
        if not isinstance(target, dict):
            raise SessionQueryError("INVALID_REQUEST", "target is required")
        try:
            if set(target) == {"kind", "session_id"} and target["kind"] == "SESSION":
                return self._authorized(
                    connection, require_id(target["session_id"], "session")
                ), None
            if (
                allow_entry
                and set(target) == {"kind", "entry_id"}
                and target["kind"] == "SESSION_ENTRY"
            ):
                entry = connection.execute(
                    "SELECT e.* FROM pulsara_v3.transcript_entries e JOIN pulsara_v3.sessions s ON s.id=e.session_id WHERE e.id=%s AND s.memory_domain_id=%s",
                    (require_id(target["entry_id"], "entry"), self.memory_domain_id),
                ).fetchone()
                if entry is None:
                    raise SessionQueryError(
                        "ENTRY_NOT_READABLE",
                        "Entry is unavailable or outside this memory domain",
                    )
                return self._authorized(connection, entry["session_id"]), entry
        except (TypeError, ValueError) as exc:
            if isinstance(exc, SessionQueryError):
                raise
            raise SessionQueryError(
                "INVALID_REQUEST", "Invalid history target"
            ) from exc
        raise SessionQueryError("INVALID_REQUEST", "Unsupported history target")

    @staticmethod
    def _deadline(deadline):
        if monotonic() >= deadline:
            raise TimeoutError("History query deadline exceeded")
