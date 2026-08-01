from __future__ import annotations

from pathlib import Path
import sys

SPIKE_ROOT = Path(__file__).resolve().parents[2]
if str(SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SPIKE_ROOT))

from probe.performance_probe import (  # noqa: E402
    ResourcePoint,
    _render_metrics,
    _resource_metrics,
    aggregate_workload,
    distribution,
)
from probe.remote_ssh_probe import (  # noqa: E402
    remote_wsl_path_present,
    windows_path_to_wsl,
)


def test_distribution_uses_nearest_rank_percentiles() -> None:
    result = distribution([5.0, 1.0, 4.0, 2.0, 3.0])
    assert result == {
        "sample_count": 5,
        "p05": 1.0,
        "p50": 3.0,
        "p95": 5.0,
        "p99": 5.0,
        "max": 5.0,
    }


def test_resource_metrics_derive_cpu_and_resident_growth_from_active_window() -> None:
    mebibyte = 1024 * 1024
    result = _resource_metrics(
        (
            ResourcePoint(0, 0, 10 * mebibyte),
            ResourcePoint(1_000_000_000, 100_000_000, 12 * mebibyte),
            ResourcePoint(2_000_000_000, 300_000_000, 14 * mebibyte),
        ),
        start_ns=0,
        end_ns=2_000_000_000,
    )
    assert result["cpu_average_percent"] == 15.0
    assert result["cpu_interval_percent"]["max"] == 20.0
    assert result["rss_mib"]["max"] == 14.0
    assert result["rss_growth_mib"] == 4.0


def test_render_metrics_use_only_physical_writes_inside_active_window() -> None:
    result = _render_metrics(
        {
            "dropped_samples": 0,
            "samples": [
                {"unix_nanos": 1, "bytes": 99},
                {"unix_nanos": 100_000_000, "bytes": 10},
                {"unix_nanos": 150_000_000, "bytes": 20},
                {"unix_nanos": 250_000_000, "bytes": 30},
                {"unix_nanos": 400_000_000, "bytes": 99},
            ],
        },
        start_unix_ns=100_000_000,
        end_unix_ns=250_000_000,
    )
    assert result["physical_write_count"] == 3
    assert result["physical_written_bytes"] == 60
    assert result["write_interval_ms"]["p50"] == 50.0
    assert result["write_interval_ms"]["p95"] == 100.0
    assert result["jitter_p95_minus_p50_ms"] == 50.0


def _passing_run(*, rss_growth_mib: float) -> dict[str, object]:
    return {
        "status": "pass",
        "keypress_latency_ms": {"p95": 10.0, "p99": 12.0},
        "stream_delivery_latency_ms": {"p95": 1.0},
        "producer_schedule_lag_ms": {"p95": 1.0},
        "renderer": {
            "physical_write_count": 100,
            "write_interval_ms": {"p95": 20.0, "p99": 22.0},
            "jitter_p95_minus_p50_ms": 2.0,
        },
        "resource": {
            "cpu_average_percent": 5.0,
            "cpu_interval_percent": {"max": 10.0},
            "rss_mib": {"p95": 15.0, "max": 16.0},
            "rss_growth_mib": rss_growth_mib,
        },
    }


def test_workload_gate_applies_rss_growth_to_every_run_by_absolute_value() -> None:
    result = aggregate_workload(
        [_passing_run(rss_growth_mib=-17.0), _passing_run(rss_growth_mib=1.0)],
        rate_hz=20,
        measurement_seconds=3.0,
    )
    assert result["checks"]["rss_growth"] is False
    assert result["metrics"]["rss_absolute_growth_mib"]["max"] == 17.0
    assert result["status"] == "fail"


def test_windows_profile_path_maps_to_wsl_mount_without_host_assumptions() -> None:
    assert windows_path_to_wsl(r"C:\Users\plumlocal") == "/mnt/c/Users/plumlocal"


def test_remote_path_probe_uses_exit_status_not_command_output(monkeypatch) -> None:
    class Completed:
        returncode = 1

    monkeypatch.setattr(
        "probe.remote_ssh_probe.run_command",
        lambda *args, **kwargs: Completed(),
    )
    assert remote_wsl_path_present("user@host", "/tmp/probe") is False
