"""PostgreSQL schema for runtime truth storage.

Postgres owns replayable runtime facts: sessions, runs, turns, append-only
events, tool execution records, and artifact payloads. Semantic truth belongs
in GraphStore/Oxigraph and should only reference these runtime rows by stable
ids such as ``event:<event_id>`` and ``artifact:<artifact_id>``.
"""

from __future__ import annotations

RUNTIME_TRUTH_TABLES = (
    "sessions",
    "runs",
    "turns",
    "agent_events",
    "tool_execution_records",
    "artifacts",
    "working_context_summaries",
)

RUNTIME_TRUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    workspace_root TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    stop_reason TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_runs_session_id ON runs(session_id);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_turns_run_id ON turns(run_id);

CREATE TABLE IF NOT EXISTS agent_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    reply_id TEXT NOT NULL,
    sequence BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_agent_events_run_sequence
    ON agent_events(run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_agent_events_reply_sequence
    ON agent_events(reply_id, sequence);

CREATE INDEX IF NOT EXISTS idx_agent_events_type
    ON agent_events(event_type);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    run_id TEXT,
    media_type TEXT NOT NULL DEFAULT 'text/plain',
    text_body TEXT,
    binary_body BYTEA,
    digest TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    stored_at TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (text_body IS NOT NULL OR binary_body IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);

CREATE TABLE IF NOT EXISTS tool_execution_records (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    event_start_sequence BIGINT,
    event_end_sequence BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, tool_call_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_execution_records_run_id
    ON tool_execution_records(run_id);

CREATE INDEX IF NOT EXISTS idx_tool_execution_records_artifact_id
    ON tool_execution_records(artifact_id);

CREATE TABLE IF NOT EXISTS working_context_summaries (
    summary_id TEXT PRIMARY KEY,
    memory_domain_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    workspace_label TEXT,
    workspace_key TEXT,
    source_session_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (memory_domain_id)
);

CREATE INDEX IF NOT EXISTS idx_working_context_domain
    ON working_context_summaries(memory_domain_id, updated_at);
""".strip()
