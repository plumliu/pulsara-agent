from __future__ import annotations

from benchmarks.suites.durable_projection_pipeline import run_pure_benchmark


def test_durable_projection_structural_benchmark_gate() -> None:
    report = run_pure_benchmark(event_count=10_000, wake_count=1_000)

    assert report["timeline"]["folded_source_event_count"] == 10_000
    assert report["timeline"]["resident_open_item_count"] == 0
    assert report["publisher_wake"]["new_worker_task_count"] == 0
    assert report["projection_executor"]["maximum_observed_active_workers"] == 9
