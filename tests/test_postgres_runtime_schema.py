from pulsara_agent.storage import RUNTIME_TRUTH_TABLES
from pulsara_agent.storage.migrations.manifest import (
    POSTGRES_LATEST_SCHEMA_MANIFEST,
)


def _relation(name: str) -> dict[str, object]:
    return next(
        item
        for item in POSTGRES_LATEST_SCHEMA_MANIFEST.owned_relations
        if item["relation_name"] == name
    )


def _constraint_definitions(name: str) -> set[str]:
    return {
        str(item["definition"])
        for item in _relation(name)["constraints"]
    }


def test_runtime_truth_is_owned_by_cumulative_manifest() -> None:
    names = {
        str(item["relation_name"])
        for item in POSTGRES_LATEST_SCHEMA_MANIFEST.owned_relations
    }
    assert set(RUNTIME_TRUTH_TABLES) <= names


def test_runtime_schema_preserves_tool_call_and_parent_contracts() -> None:
    tool_constraints = _constraint_definitions("tool_execution_records")
    turn_constraints = _constraint_definitions("turns")
    assert "UNIQUE (run_id, tool_call_id)" in tool_constraints
    assert "UNIQUE (session_id, tool_call_id)" not in tool_constraints
    assert "FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE" in (
        tool_constraints
    )
    assert "FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE" in (
        tool_constraints
    )
    assert "FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE" in (
        turn_constraints
    )


def test_working_context_remains_runtime_operational_state() -> None:
    relation = _relation("working_context_summaries")
    assert relation["runtime_writable"] is True
    assert "UNIQUE (memory_domain_id)" in _constraint_definitions(
        "working_context_summaries"
    )
