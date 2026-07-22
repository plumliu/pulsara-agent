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
    "ledger_materialization_accounts",
    "tool_execution_records",
    "artifacts",
    "working_context_summaries",
    "runtime_projection_checkpoints",
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
    event_schema_version TEXT NOT NULL,
    event_schema_fingerprint TEXT NOT NULL,
    event_domain_contract_fingerprint TEXT NOT NULL,
    transcript_event_domain TEXT NOT NULL CHECK (
        transcript_event_domain IN (
            'transcript_semantic',
            'transcript_acceleration',
            'non_transcript'
        )
    ),
    transcript_semantic_prefix_count BIGINT NOT NULL CHECK (
        transcript_semantic_prefix_count >= 0
    ),
    transcript_semantic_prefix_accumulator TEXT NOT NULL,
    ledger_continuity_accumulator TEXT NOT NULL,
    ledger_payload_prefix_bytes BIGINT NOT NULL CHECK (
        ledger_payload_prefix_bytes >= 0
    ),
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

CREATE INDEX IF NOT EXISTS idx_agent_events_session_type_sequence
    ON agent_events(session_id, event_type, sequence);

CREATE INDEX IF NOT EXISTS idx_agent_events_session_transcript_domain_sequence
    ON agent_events(session_id, transcript_event_domain, sequence);

CREATE INDEX IF NOT EXISTS idx_agent_events_session_model_call_sequence
    ON agent_events(
        session_id,
        (coalesce(
            payload #>> '{resolved_call,resolved_model_call_id}',
            payload #>> '{resolved_model_call_id}',
            payload #>> '{model_stream_attribution,resolved_model_call_id}'
        )),
        sequence
    );

CREATE TABLE IF NOT EXISTS ledger_materialization_accounts (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    account_state_fingerprint TEXT NOT NULL,
    ledger_materialization_generation BIGINT NOT NULL CHECK (
        ledger_materialization_generation >= 0
    ),
    consumer_horizon_revision BIGINT NOT NULL CHECK (
        consumer_horizon_revision >= 0
    ),
    ledger_through_sequence BIGINT NOT NULL CHECK (ledger_through_sequence >= 0),
    state_payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runtime_projection_checkpoints (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    projection_kind TEXT NOT NULL,
    through_sequence BIGINT NOT NULL CHECK (through_sequence >= 0),
    projection_schema_version TEXT NOT NULL,
    ledger_prefix JSONB NOT NULL,
    validation_base_through_sequence BIGINT NOT NULL CHECK (
        validation_base_through_sequence >= 0
        AND validation_base_through_sequence <= through_sequence
    ),
    validation_base_state_payload JSONB NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    state_payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, projection_kind)
);

ALTER TABLE runtime_projection_checkpoints
    ADD COLUMN IF NOT EXISTS ledger_prefix JSONB;
ALTER TABLE runtime_projection_checkpoints
    ADD COLUMN IF NOT EXISTS validation_base_through_sequence BIGINT;
ALTER TABLE runtime_projection_checkpoints
    ADD COLUMN IF NOT EXISTS validation_base_state_payload JSONB;
DELETE FROM runtime_projection_checkpoints
WHERE ledger_prefix IS NULL
   OR validation_base_through_sequence IS NULL
   OR validation_base_state_payload IS NULL;
ALTER TABLE runtime_projection_checkpoints
    ALTER COLUMN ledger_prefix SET NOT NULL;
ALTER TABLE runtime_projection_checkpoints
    ALTER COLUMN validation_base_through_sequence SET NOT NULL;
ALTER TABLE runtime_projection_checkpoints
    ALTER COLUMN validation_base_state_payload SET NOT NULL;

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
CREATE INDEX IF NOT EXISTS idx_artifacts_session_id ON artifacts(session_id);

CREATE TABLE IF NOT EXISTS tool_result_artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    reply_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'output',
    ordinal INTEGER NOT NULL DEFAULT 0,
    media_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    stored_complete BOOLEAN NOT NULL DEFAULT TRUE,
    loss_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, tool_call_id, role, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_tool_result_artifacts_session_id
    ON tool_result_artifacts(session_id);

CREATE INDEX IF NOT EXISTS idx_tool_result_artifacts_artifact_id
    ON tool_result_artifacts(artifact_id);

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
