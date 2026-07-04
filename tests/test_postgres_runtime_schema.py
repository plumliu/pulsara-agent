from pulsara_agent.storage import RUNTIME_TRUTH_SCHEMA_SQL, RUNTIME_TRUTH_TABLES


def test_postgres_runtime_truth_schema_declares_expected_tables() -> None:
    for table in RUNTIME_TRUTH_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in RUNTIME_TRUTH_SCHEMA_SQL


def test_postgres_runtime_truth_schema_does_not_own_semantic_memory_tables() -> None:
    forbidden_semantic_tables = [
        "claims",
        "evidence",
        "decisions",
        "preferences",
        "action_boundaries",
        "skills",
        "projection_policies",
    ]

    lowered = RUNTIME_TRUTH_SCHEMA_SQL.lower()

    for table in forbidden_semantic_tables:
        assert f"create table if not exists {table}" not in lowered


def test_tool_execution_records_allow_cross_run_tool_call_id_reuse() -> None:
    assert "UNIQUE (run_id, tool_call_id)" in RUNTIME_TRUTH_SCHEMA_SQL
    assert "UNIQUE (session_id, tool_call_id)" not in RUNTIME_TRUTH_SCHEMA_SQL


def test_runtime_fact_tables_reference_runs_and_turns() -> None:
    assert "run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE" in RUNTIME_TRUTH_SCHEMA_SQL
    assert "turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE" in RUNTIME_TRUTH_SCHEMA_SQL


def test_working_context_is_runtime_operational_state_not_semantic_memory() -> None:
    assert "CREATE TABLE IF NOT EXISTS working_context_summaries" in RUNTIME_TRUTH_SCHEMA_SQL
    assert "UNIQUE (memory_domain_id)" in RUNTIME_TRUTH_SCHEMA_SQL
