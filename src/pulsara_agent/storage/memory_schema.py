"""PostgreSQL schema for canonical memory graph storage."""

from __future__ import annotations


MEMORY_SUBSTRATE_TABLES = (
    "graph_documents",
    "memory_nodes",
    "memory_relations",
    "memory_write_outbox",
    "memory_governance_event_outbox",
    "memory_search_index",
    "memory_vector_index",
    "recall_traces",
    "recall_usages",
)


MEMORY_SUBSTRATE_BOOTSTRAP_SQL = "CREATE EXTENSION IF NOT EXISTS vector;"


MEMORY_SUBSTRATE_EXTENSION_REQUIREMENT_SQL = """
DO $pulsara$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_extension
        WHERE extname = 'vector'
    ) THEN
        RAISE EXCEPTION
            'Pulsara memory storage requires pgvector in database %',
            current_database()
            USING ERRCODE = '55000',
                  HINT = 'Have a PostgreSQL administrator install extension "vector" before starting Pulsara.';
    END IF;
END
$pulsara$;
""".strip()


MEMORY_SUBSTRATE_SCHEMA_SQL = (
    MEMORY_SUBSTRATE_EXTENSION_REQUIREMENT_SQL
    + "\n\n"
    + """
CREATE OR REPLACE FUNCTION pulsara_jsonb_text_array(value JSONB)
RETURNS JSONB
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE jsonb_typeof(value)
        WHEN 'array' THEN value
        WHEN 'string' THEN jsonb_build_array(value #>> '{}')
        ELSE '[]'::jsonb
    END
$$;

CREATE TABLE IF NOT EXISTS graph_documents (
    graph_id TEXT NOT NULL,
    id TEXT NOT NULL,
    type TEXT,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, id)
);

CREATE INDEX IF NOT EXISTS idx_graph_documents_type
    ON graph_documents(graph_id, type);

CREATE TABLE IF NOT EXISTS memory_nodes (
    graph_id TEXT NOT NULL,
    id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    statement TEXT NOT NULL,
    summary TEXT,
    source_authority TEXT,
    verification_status TEXT,
    confidence_level TEXT,
    applies_when TEXT,
    do_not_apply_when TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    node_revision BIGINT NOT NULL DEFAULT 1 CHECK (node_revision >= 1),
    stale_after TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    fts TSVECTOR,
    PRIMARY KEY (graph_id, id)
);

CREATE INDEX IF NOT EXISTS idx_memory_nodes_type_status_scope
    ON memory_nodes(graph_id, memory_type, status, scope);

CREATE INDEX IF NOT EXISTS idx_memory_nodes_status
    ON memory_nodes(graph_id, status);

CREATE INDEX IF NOT EXISTS idx_memory_nodes_updated_at
    ON memory_nodes(graph_id, updated_at);

ALTER TABLE memory_nodes
    ADD COLUMN IF NOT EXISTS node_revision BIGINT NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS memory_vector_index (
    graph_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    embedding_fingerprint TEXT NOT NULL,
    embedded_text_hash TEXT NOT NULL,
    builder_version TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, memory_id, embedding_fingerprint),
    FOREIGN KEY (graph_id, memory_id)
        REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mvi_embedding_hnsw
    ON memory_vector_index USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_mvi_graph_fingerprint
    ON memory_vector_index(graph_id, embedding_fingerprint);

CREATE TABLE IF NOT EXISTS memory_relations (
    graph_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    target_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (graph_id, source_id, predicate, target_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_relations_source
    ON memory_relations(graph_id, source_id, predicate);

CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations(graph_id, target_id, predicate);

CREATE TABLE IF NOT EXISTS memory_write_outbox (
    outbox_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL,
    governance_batch_id TEXT,
    decision_id TEXT,
    mutation_lane TEXT NOT NULL DEFAULT 'governed_memory',
    sequence_key TEXT NOT NULL DEFAULT '',
    target_entry_key TEXT NOT NULL,
    dirty_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ
);

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS vector_claim_token TEXT;

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS vector_claimed_until TIMESTAMPTZ;

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS mutation_lane TEXT NOT NULL DEFAULT 'governed_memory';

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS sequence_key TEXT NOT NULL DEFAULT '';

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS dirty_memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE memory_write_outbox
    ADD COLUMN IF NOT EXISTS last_error TEXT;

ALTER TABLE memory_write_outbox
    ALTER COLUMN governance_batch_id DROP NOT NULL;

ALTER TABLE memory_write_outbox
    ALTER COLUMN decision_id DROP NOT NULL;

UPDATE memory_write_outbox
SET sequence_key = graph_id
WHERE sequence_key = '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_write_outbox_governance_decision
    ON memory_write_outbox(governance_batch_id, decision_id)
    WHERE governance_batch_id IS NOT NULL AND decision_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_write_outbox_status
    ON memory_write_outbox(status, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_write_outbox_sequence
    ON memory_write_outbox(sequence_key, created_at, outbox_id);

CREATE TABLE IF NOT EXISTS memory_governance_event_outbox (
    outbox_id TEXT PRIMARY KEY,
    runtime_session_id TEXT NOT NULL,
    governance_batch_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    event_ids JSONB NOT NULL,
    events_payload JSONB NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    claimed_until TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ,
    UNIQUE (runtime_session_id, governance_batch_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_governance_event_outbox_pending
    ON memory_governance_event_outbox(runtime_session_id, status, created_at);

CREATE TABLE IF NOT EXISTS memory_search_index (
    graph_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    fts TSVECTOR NOT NULL,
    aliases TEXT[],
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (graph_id, memory_id),
    FOREIGN KEY (graph_id, memory_id)
        REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_msi_fts
    ON memory_search_index USING GIN(fts);

CREATE INDEX IF NOT EXISTS idx_msi_type_scope
    ON memory_search_index(graph_id, memory_type, scope);

CREATE TABLE IF NOT EXISTS recall_traces (
    trace_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    reply_id TEXT NOT NULL,
    query TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    candidate_ids JSONB NOT NULL,
    included_ids JSONB NOT NULL,
    filtered_ids JSONB NOT NULL,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency_ms INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE recall_traces
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_recall_traces_scope
    ON recall_traces(graph_id, session_id, created_at);

CREATE TABLE IF NOT EXISTS recall_usages (
    trace_id TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    injected BOOLEAN NOT NULL,
    selected_by_tool BOOLEAN NOT NULL DEFAULT false,
    cited_by_response BOOLEAN,
    later_confirmed BOOLEAN,
    later_contradicted BOOLEAN,
    PRIMARY KEY (trace_id, memory_id),
    FOREIGN KEY (trace_id) REFERENCES recall_traces(trace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_recall_usages_mem
    ON recall_usages(graph_id, memory_id);
""".strip()
)
