from pathlib import Path

from pulsara_agent.storage import (
    MEMORY_SUBSTRATE_BOOTSTRAP_SQL,
    MEMORY_SUBSTRATE_EXTENSION_REQUIREMENT_SQL,
    MEMORY_SUBSTRATE_SCHEMA_SQL,
    MEMORY_SUBSTRATE_TABLES,
)


def test_memory_substrate_schema_declares_phase_zero_tables() -> None:
    for table in MEMORY_SUBSTRATE_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in MEMORY_SUBSTRATE_SCHEMA_SQL


def test_memory_substrate_schema_partitions_graph_tables_by_graph_id() -> None:
    for table in ("graph_documents", "memory_nodes", "memory_relations"):
        table_start = MEMORY_SUBSTRATE_SCHEMA_SQL.index(
            f"CREATE TABLE IF NOT EXISTS {table}"
        )
        table_end = MEMORY_SUBSTRATE_SCHEMA_SQL.index(");", table_start)
        table_sql = MEMORY_SUBSTRATE_SCHEMA_SQL[table_start:table_end]

        assert "graph_id TEXT NOT NULL" in table_sql


def test_memory_write_outbox_uses_decision_level_idempotency_key() -> None:
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_write_outbox_governance_decision"
        in MEMORY_SUBSTRATE_SCHEMA_SQL
    )
    assert (
        "WHERE governance_batch_id IS NOT NULL AND decision_id IS NOT NULL"
        in MEMORY_SUBSTRATE_SCHEMA_SQL
    )
    assert "target_entry_key TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "mutation_lane TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "sequence_key TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "dirty_memory_ids JSONB NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "attempt_count INTEGER NOT NULL DEFAULT 0" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "last_error TEXT" in MEMORY_SUBSTRATE_SCHEMA_SQL


def test_memory_search_index_uses_compound_canonical_fk() -> None:
    assert "FOREIGN KEY (graph_id, memory_id)" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert (
        "REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE"
        in MEMORY_SUBSTRATE_SCHEMA_SQL
    )


def test_memory_vector_index_declares_pgvector_fingerprint_and_hnsw_boundaries() -> (
    None
):
    assert "CREATE EXTENSION IF NOT EXISTS vector" in MEMORY_SUBSTRATE_BOOTSTRAP_SQL
    assert "CREATE EXTENSION" not in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "FROM pg_extension" in MEMORY_SUBSTRATE_EXTENSION_REQUIREMENT_SQL
    assert "requires pgvector" in MEMORY_SUBSTRATE_EXTENSION_REQUIREMENT_SQL
    assert "embedding_fingerprint TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "embedding VECTOR(1024) NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert (
        "PRIMARY KEY (graph_id, memory_id, embedding_fingerprint)"
        in MEMORY_SUBSTRATE_SCHEMA_SQL
    )
    assert "USING hnsw (embedding vector_cosine_ops)" in MEMORY_SUBSTRATE_SCHEMA_SQL


def test_recall_trace_tables_are_partitioned_by_graph_and_session() -> None:
    assert "CREATE TABLE IF NOT EXISTS recall_traces" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "graph_id TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "session_id TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert (
        "CREATE INDEX IF NOT EXISTS idx_recall_traces_scope"
        in MEMORY_SUBSTRATE_SCHEMA_SQL
    )
    assert (
        "ON recall_traces(graph_id, session_id, created_at)"
        in MEMORY_SUBSTRATE_SCHEMA_SQL
    )


def test_runtime_code_cannot_import_privileged_memory_bootstrap_sql() -> None:
    source_root = Path(__file__).parents[1] / "src" / "pulsara_agent"
    allowed = {
        Path("storage/__init__.py"),
        Path("storage/memory_schema.py"),
    }
    findings = {
        path.relative_to(source_root)
        for path in source_root.rglob("*.py")
        if "MEMORY_SUBSTRATE_BOOTSTRAP_SQL" in path.read_text(encoding="utf-8")
    }

    assert findings == allowed
