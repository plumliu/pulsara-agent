from __future__ import annotations

from pathlib import Path

from benchmarks.suites.contracts import (
    EXPECTED_SCENARIO_IDS,
    SUITE_ID,
    SUITE_SCHEMA_VERSION,
    load_suite,
)


ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = ROOT / "benchmarks" / "suites" / "core" / "v1"


def test_kernel_dogfood_v2_contract_and_fingerprints_are_closed() -> None:
    suite = load_suite(SUITE_ROOT)

    assert suite.manifest.schema_version == SUITE_SCHEMA_VERSION
    assert suite.manifest.suite_id == SUITE_ID
    assert tuple(item.contract.scenario_id for item in suite.scenarios) == (
        EXPECTED_SCENARIO_IDS
    )
    assert all(item.file_inventory for item in suite.scenarios)


def test_kernel_dogfood_has_no_deleted_runtime_evidence_path() -> None:
    suite = load_suite(SUITE_ROOT)
    workflow_kinds = {
        item.contract.workflow.workflow_kind for item in suite.scenarios
    }
    assert workflow_kinds == {
        "cache_continuity",
        "canonical_resume",
        "long_context_continuity",
        "prompt_queue_fifo",
        "subagent_delegation",
        "workspace_task",
    }

    active_sources = tuple(
        (ROOT / "benchmarks" / "suites" / name).read_text(encoding="utf-8")
        for name in ("contracts.py", "graders.py", "runner.py", "run_core_dogfood.py")
    )
    forbidden = (
        "pulsara_agent.event_log",
        "pulsara_agent.runtime.projection_jobs",
        "pulsara_agent.runtime.session",
        "pulsara_agent.graph",
        "InspectorService",
    )
    assert all(token not in source for source in active_sources for token in forbidden)
    assert not (ROOT / "benchmarks" / "suites" / "durable_projection_pipeline.py").exists()
