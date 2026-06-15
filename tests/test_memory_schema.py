from pulsara_agent.storage import MEMORY_SUBSTRATE_SCHEMA_SQL, MEMORY_SUBSTRATE_TABLES


def test_memory_substrate_schema_declares_phase_zero_tables() -> None:
    for table in MEMORY_SUBSTRATE_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in MEMORY_SUBSTRATE_SCHEMA_SQL


def test_memory_substrate_schema_partitions_graph_tables_by_graph_id() -> None:
    for table in ("graph_documents", "memory_nodes", "memory_relations"):
        table_start = MEMORY_SUBSTRATE_SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table}")
        table_end = MEMORY_SUBSTRATE_SCHEMA_SQL.index(");", table_start)
        table_sql = MEMORY_SUBSTRATE_SCHEMA_SQL[table_start:table_end]

        assert "graph_id TEXT NOT NULL" in table_sql


def test_memory_write_outbox_uses_decision_level_idempotency_key() -> None:
    assert "UNIQUE (governance_batch_id, decision_id)" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "target_entry_key TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL


def test_memory_search_index_uses_compound_canonical_fk() -> None:
    assert "FOREIGN KEY (graph_id, memory_id)" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "REFERENCES memory_nodes(graph_id, id) ON DELETE CASCADE" in MEMORY_SUBSTRATE_SCHEMA_SQL


def test_recall_trace_tables_are_partitioned_by_graph_and_session() -> None:
    assert "CREATE TABLE IF NOT EXISTS recall_traces" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "graph_id TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "session_id TEXT NOT NULL" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "CREATE INDEX IF NOT EXISTS idx_recall_traces_scope" in MEMORY_SUBSTRATE_SCHEMA_SQL
    assert "ON recall_traces(graph_id, session_id, created_at)" in MEMORY_SUBSTRATE_SCHEMA_SQL
