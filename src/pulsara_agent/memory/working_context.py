"""Operational working-context summaries.

Working context is recent activity state, not canonical semantic memory. It is
stored in the runtime Postgres schema and injected as a fenced projection block;
it never enters the governed memory graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.storage import RUNTIME_TRUTH_SCHEMA_SQL

if TYPE_CHECKING:
    from pulsara_agent.memory.foundation.run_timeline_query import RunTimelineSummary


@dataclass(frozen=True, slots=True)
class WorkingContextSummary:
    summary_id: str
    memory_domain_id: str
    summary: str
    source_session_id: str
    source_run_id: str
    workspace_label: str | None = None
    workspace_key: str | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkingContextUpdate:
    should_update: bool
    summary: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PostgresWorkingContextStore:
    dsn: str

    def __post_init__(self) -> None:
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(RUNTIME_TRUTH_SCHEMA_SQL)

    def get_latest(self, *, memory_domain_id: str, now: datetime | None = None) -> WorkingContextSummary | None:
        now = now or _utc_now()
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM working_context_summaries
                    WHERE memory_domain_id = %s
                      AND (expires_at IS NULL OR expires_at > %s)
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (memory_domain_id, now),
                )
                row = cursor.fetchone()
        return _summary_from_row(row) if row is not None else None

    def upsert(
        self,
        *,
        domain: MemoryDomainContext,
        source_session_id: str,
        source_run_id: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> WorkingContextSummary:
        now = now or _utc_now()
        expires_at = now + ttl if ttl is not None else None
        summary_id = f"working-context:{domain.memory_domain_id}"
        metadata = metadata or {}
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO working_context_summaries (
                        summary_id,
                        memory_domain_id,
                        summary,
                        workspace_label,
                        workspace_key,
                        source_session_id,
                        source_run_id,
                        updated_at,
                        expires_at,
                        metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (memory_domain_id) DO UPDATE SET
                        summary = EXCLUDED.summary,
                        workspace_label = EXCLUDED.workspace_label,
                        workspace_key = EXCLUDED.workspace_key,
                        source_session_id = EXCLUDED.source_session_id,
                        source_run_id = EXCLUDED.source_run_id,
                        updated_at = EXCLUDED.updated_at,
                        expires_at = EXCLUDED.expires_at,
                        metadata = EXCLUDED.metadata
                    RETURNING *
                    """,
                    (
                        summary_id,
                        domain.memory_domain_id,
                        summary,
                        domain.workspace_label,
                        domain.stable_project_key,
                        source_session_id,
                        source_run_id,
                        now,
                        expires_at,
                        Jsonb(metadata),
                    ),
                )
                row = cursor.fetchone()
        assert row is not None
        return _summary_from_row(row)


def propose_working_context_update(
    timeline: "RunTimelineSummary",
    *,
    existing_summary: WorkingContextSummary | None = None,
) -> WorkingContextUpdate:
    if not _has_substantive_signal(timeline):
        return WorkingContextUpdate(False, reason="low_signal_run")
    summary = _summary_text(timeline)
    if len(summary) < 24:
        return WorkingContextUpdate(False, reason="summary_too_short")
    if existing_summary is not None and _normalized(summary) == _normalized(existing_summary.summary):
        return WorkingContextUpdate(False, reason="summary_unchanged")
    return WorkingContextUpdate(
        True,
        summary=summary,
        reason="substantive_run",
        metadata={
            "timeline_status": timeline.status,
            "timeline_item_count": timeline.item_count,
            "tool_call_count": len(timeline.tool_traces),
            "error_count": len(timeline.errors),
            "update_id": f"working-context-update:{uuid4().hex}",
        },
    )


def working_context_projection(summary: WorkingContextSummary, *, token_budget: int) -> dict[str, Any]:
    text = _clip(
        "\n".join(
            [
                '<working-context-projection do_not_write_back="true" authority="recent_activity">',
                summary.summary,
                "</working-context-projection>",
            ]
        ),
        max_chars=max(160, token_budget * 4),
    )
    return {
        "summary": text,
        "items": [summary.summary],
        "included_memory_ids": [],
        "filtered_memory_ids": [],
        "do_not_write_back": True,
        "projection_kind": "working_context",
    }


def _has_substantive_signal(timeline: RunTimelineSummary) -> bool:
    if timeline.tool_traces:
        return True
    if timeline.errors:
        return True
    text = _normalized(timeline.assistant_text)
    return len(text) >= 80


def _summary_text(timeline: RunTimelineSummary) -> str:
    parts: list[str] = []
    if timeline.tool_traces:
        tools = ", ".join(_dedupe(trace.tool_name for trace in timeline.tool_traces if trace.tool_name))
        if tools:
            parts.append(f"Recent run used tools: {tools}.")
        result_bits = [
            trace.result_summary.strip()
            for trace in timeline.tool_traces
            if trace.result_summary and trace.status == "success"
        ]
        if result_bits:
            parts.append("Key tool result: " + _clip(" ".join(result_bits), max_chars=220))
    if timeline.assistant_text.strip():
        parts.append("Recent assistant summary: " + _clip(timeline.assistant_text.strip(), max_chars=260))
    if timeline.errors:
        parts.append("Recent run errors: " + _clip("; ".join(timeline.errors), max_chars=180))
    return _clip(" ".join(parts).strip(), max_chars=600)


def _summary_from_row(row: dict[str, Any]) -> WorkingContextSummary:
    return WorkingContextSummary(
        summary_id=row["summary_id"],
        memory_domain_id=row["memory_domain_id"],
        summary=row["summary"],
        workspace_label=row["workspace_label"],
        workspace_key=row["workspace_key"],
        source_session_id=row["source_session_id"],
        source_run_id=row["source_run_id"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        metadata=dict(row["metadata"] or {}),
    )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _clip(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
