#!/usr/bin/env python3
"""Repeatable CPU, RSS, latency, and renderer-cadence S0 benchmark.

The child remains the unmodified Bubble Tea PTY program. Resource sampling is
performed out of process, while renderer cadence comes from the child's exact
physical output-writer seam. No Pulsara production Runtime code is imported.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Protocol

SPIKE_ROOT = Path(__file__).resolve().parents[1]
if str(SPIKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SPIKE_ROOT))

from probe.parent_probe import ChildSession, ProbeFailure, READY_MARKER  # noqa: E402
from probe_wire import probe_pb2  # noqa: E402


SCHEMA_VERSION = "pulsara.terminal.s0.performance-baseline.v1"
DEFAULT_RATES_HZ = (20, 100)
DEFAULT_REPETITIONS = 20
DEFAULT_WARMUP_SECONDS = 1.0
DEFAULT_MEASUREMENT_SECONDS = 3.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.1
DEFAULT_KEY_PROBES = 20
DEFAULT_COOLDOWN_SECONDS = 0.25


class ResourceReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResourcePoint:
    monotonic_ns: int
    cpu_time_ns: int
    rss_bytes: int


class ProcessResourceReader(Protocol):
    method: str

    def read(self, pid: int) -> tuple[int, int]:
        """Return cumulative CPU nanoseconds and resident bytes."""


class _DarwinRUsageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


class _MachTimebaseInfo(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class DarwinProcessResourceReader:
    method = "darwin.proc_pid_rusage.v2@100ms"

    def __init__(self) -> None:
        self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._libproc.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self._libproc.proc_pid_rusage.restype = ctypes.c_int
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        timebase = _MachTimebaseInfo()
        if libsystem.mach_timebase_info(ctypes.byref(timebase)) != 0:
            raise ResourceReadError("mach_timebase_info failed")
        self._timebase_numer = int(timebase.numer)
        self._timebase_denom = int(timebase.denom)

    def read(self, pid: int) -> tuple[int, int]:
        usage = _DarwinRUsageInfoV2()
        result = self._libproc.proc_pid_rusage(
            pid,
            2,  # RUSAGE_INFO_V2
            ctypes.byref(usage),
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise ResourceReadError(
                f"proc_pid_rusage({pid}) failed with errno {error_number}"
            )
        cpu_ticks = int(usage.ri_user_time + usage.ri_system_time)
        cpu_ns = cpu_ticks * self._timebase_numer // self._timebase_denom
        return cpu_ns, int(usage.ri_resident_size)


class LinuxProcessResourceReader:
    method = "linux.procfs.stat+statm@100ms"

    def __init__(self) -> None:
        self._clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        self._page_size = int(os.sysconf("SC_PAGE_SIZE"))

    def read(self, pid: int) -> tuple[int, int]:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # comm is parenthesized and may itself contain spaces.
            fields_after_comm = stat[stat.rfind(")") + 2 :].split()
            user_ticks = int(fields_after_comm[11])
            system_ticks = int(fields_after_comm[12])
            statm = Path(f"/proc/{pid}/statm").read_text(encoding="utf-8").split()
            resident_pages = int(statm[1])
        except (OSError, ValueError, IndexError) as exc:
            raise ResourceReadError(
                f"cannot read /proc resource state for {pid}"
            ) from exc
        cpu_ns = (user_ticks + system_ticks) * 1_000_000_000 // self._clock_ticks
        return cpu_ns, resident_pages * self._page_size


def platform_resource_reader() -> ProcessResourceReader:
    system = platform.system()
    if system == "Darwin":
        return DarwinProcessResourceReader()
    if system == "Linux":
        return LinuxProcessResourceReader()
    raise ResourceReadError(f"unsupported resource sampler platform: {system}")


class ResourceSampler:
    def __init__(
        self,
        pid: int,
        *,
        reader: ProcessResourceReader,
        interval_seconds: float,
    ) -> None:
        self._pid = pid
        self._reader = reader
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._points: list[ResourcePoint] = []
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"s0-resource-sampler-{pid}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> tuple[ResourcePoint, ...]:
        self._stop.set()
        self._thread.join(timeout=max(self._interval_seconds * 4, 1.0))
        if self._thread.is_alive():
            raise ResourceReadError("resource sampler did not stop")
        if self._error is not None:
            raise ResourceReadError("resource sampler failed") from self._error
        return tuple(self._points)

    def _run(self) -> None:
        next_sample = time.perf_counter()
        try:
            while not self._stop.is_set():
                cpu_time_ns, rss_bytes = self._reader.read(self._pid)
                self._points.append(
                    ResourcePoint(
                        monotonic_ns=time.perf_counter_ns(),
                        cpu_time_ns=cpu_time_ns,
                        rss_bytes=rss_bytes,
                    )
                )
                next_sample += self._interval_seconds
                self._stop.wait(max(0.0, next_sample - time.perf_counter()))
        except BaseException as exc:  # preserve physical sampler failure
            if not self._stop.is_set():
                self._error = exc


def nearest_rank(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered) / 100) - 1
    return ordered[max(index, 0)]


def distribution(values: list[float], *, digits: int = 3) -> dict[str, Any]:
    if not values:
        return {
            "sample_count": 0,
            "p05": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }
    return {
        "sample_count": len(values),
        "p05": round(nearest_rank(values, 5), digits),
        "p50": round(nearest_rank(values, 50), digits),
        "p95": round(nearest_rank(values, 95), digits),
        "p99": round(nearest_rank(values, 99), digits),
        "max": round(max(values), digits),
    }


def _sleep_until(target: float) -> None:
    while True:
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(remaining)


def _send_scheduled_deltas(
    session: ChildSession,
    *,
    rate_hz: int,
    duration_seconds: float,
    sequence_start: int,
    measured: bool,
    content_prefix: str,
) -> tuple[int, list[float]]:
    count = round(rate_hz * duration_seconds)
    started = time.perf_counter()
    schedule_lag_ms: list[float] = []
    for offset in range(count):
        target = started + offset / rate_hz
        _sleep_until(target)
        schedule_lag_ms.append(max(0.0, (time.perf_counter() - target) * 1000))
        sequence = sequence_start + offset
        session.send_frame(
            probe_pb2.ProbeFrame(
                delta=probe_pb2.Delta(
                    sequence=sequence,
                    content=f"{content_prefix} {sequence:05d} 界",
                    sent_unix_nanos=time.time_ns() if measured else 0,
                )
            )
        )
    _sleep_until(started + duration_seconds)
    return count, schedule_lag_ms


def _resource_metrics(
    points: tuple[ResourcePoint, ...],
    *,
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    active = [point for point in points if start_ns <= point.monotonic_ns <= end_ns]
    if len(active) < 2:
        raise ProbeFailure(f"resource sample count too small: {len(active)}")
    cpu_percent: list[float] = []
    for previous, current in zip(active, active[1:], strict=False):
        wall_delta = current.monotonic_ns - previous.monotonic_ns
        cpu_delta = current.cpu_time_ns - previous.cpu_time_ns
        if wall_delta > 0 and cpu_delta >= 0:
            cpu_percent.append(cpu_delta / wall_delta * 100)
    if not cpu_percent:
        raise ProbeFailure("resource sampler produced no CPU intervals")
    elapsed_ns = active[-1].monotonic_ns - active[0].monotonic_ns
    cpu_total_ns = active[-1].cpu_time_ns - active[0].cpu_time_ns
    rss_mib = [point.rss_bytes / (1024 * 1024) for point in active]
    quartile = max(len(rss_mib) // 4, 1)
    rss_first = statistics.median(rss_mib[:quartile])
    rss_last = statistics.median(rss_mib[-quartile:])
    return {
        "resource_sample_count": len(active),
        "cpu_interval_percent": distribution(cpu_percent),
        "cpu_average_percent": round(cpu_total_ns / elapsed_ns * 100, 3),
        "rss_mib": distribution(rss_mib),
        "rss_growth_mib": round(rss_last - rss_first, 3),
    }


def _render_metrics(
    render_probe: dict[str, Any],
    *,
    start_unix_ns: int,
    end_unix_ns: int,
) -> dict[str, Any]:
    if render_probe.get("dropped_samples", 0) != 0:
        raise ProbeFailure(f"render probe dropped samples: {render_probe}")
    samples = [
        sample
        for sample in render_probe.get("samples", [])
        if start_unix_ns <= int(sample["unix_nanos"]) <= end_unix_ns
    ]
    timestamps = [int(sample["unix_nanos"]) for sample in samples]
    intervals_ms = [
        (current - previous) / 1_000_000
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
        if current > previous
    ]
    if not intervals_ms:
        raise ProbeFailure("renderer produced fewer than two writes in active window")
    interval_distribution = distribution(intervals_ms)
    return {
        "physical_write_count": len(samples),
        "physical_written_bytes": sum(int(sample["bytes"]) for sample in samples),
        "write_interval_ms": interval_distribution,
        "jitter_p95_minus_p50_ms": round(
            float(interval_distribution["p95"]) - float(interval_distribution["p50"]),
            3,
        ),
    }


def run_workload_once(
    binary: Path,
    *,
    rate_hz: int,
    repetition: int,
    warmup_seconds: float,
    measurement_seconds: float,
    sample_interval_seconds: float,
    key_probes: int,
    reader: ProcessResourceReader,
) -> dict[str, Any]:
    session = ChildSession(binary, extra_args=("--render-probe",))
    sampler = ResourceSampler(
        session.process.pid,
        reader=reader,
        interval_seconds=sample_interval_seconds,
    )
    sampler_started = False
    try:
        session.wait_for(READY_MARKER)
        session.send_frame(
            probe_pb2.ProbeFrame(
                snapshot=probe_pb2.Snapshot(
                    revision=1,
                    lines=["S0 performance warm-up", f"workload={rate_hz}Hz"],
                )
            )
        )
        base_draft = "持续输入中文🙂ASCII"
        draft_position = session.output_position()
        session.write_input(base_draft.encode("utf-8"))
        session.wait_for(base_draft.encode("utf-8"), since=draft_position)

        sampler.start()
        sampler_started = True
        warmup_count, _ = _send_scheduled_deltas(
            session,
            rate_hz=rate_hz,
            duration_seconds=warmup_seconds,
            sequence_start=1,
            measured=False,
            content_prefix=f"warmup-{rate_hz}",
        )
        warmup_marker = f"warmup-{rate_hz} {warmup_count:05d} 界".encode()
        session.wait_for(warmup_marker, timeout=2.0)

        measurement_start_monotonic_ns = time.perf_counter_ns()
        measurement_start_unix_ns = time.time_ns()
        producer_result: dict[str, Any] = {}
        producer_error: list[BaseException] = []

        def produce() -> None:
            try:
                count, schedule_lag_ms = _send_scheduled_deltas(
                    session,
                    rate_hz=rate_hz,
                    duration_seconds=measurement_seconds,
                    sequence_start=warmup_count + 1,
                    measured=True,
                    content_prefix=f"measure-{rate_hz}",
                )
                producer_result["count"] = count
                producer_result["schedule_lag_ms"] = schedule_lag_ms
            except BaseException as exc:
                producer_error.append(exc)

        producer = threading.Thread(
            target=produce,
            name=f"s0-performance-{rate_hz}hz-{repetition}",
        )
        producer.start()

        key_latencies_ms: list[float] = []
        markers: list[str] = []
        measurement_start = measurement_start_monotonic_ns / 1_000_000_000
        for index in range(key_probes):
            target = (
                measurement_start + (index + 0.5) * measurement_seconds / key_probes
            )
            _sleep_until(target)
            marker = chr(0x4E00 + index)
            markers.append(marker)
            output_position = session.output_position()
            started = time.perf_counter()
            session.write_input(marker.encode("utf-8"))
            session.wait_for(
                marker.encode("utf-8"),
                timeout=1.0,
                since=output_position,
            )
            key_latencies_ms.append((time.perf_counter() - started) * 1000)

        producer.join(timeout=measurement_seconds + 2.0)
        if producer.is_alive():
            raise ProbeFailure(f"{rate_hz}Hz producer did not finish")
        if producer_error:
            raise ProbeFailure(f"{rate_hz}Hz producer failed") from producer_error[0]

        measurement_end_monotonic_ns = measurement_start_monotonic_ns + round(
            measurement_seconds * 1e9
        )
        measurement_end_unix_ns = measurement_start_unix_ns + round(
            measurement_seconds * 1e9
        )
        _sleep_until(measurement_end_monotonic_ns / 1_000_000_000)
        resource_points = sampler.stop()
        sampler_started = False

        measured_count = int(producer_result["count"])
        total_count = warmup_count + measured_count
        final_marker = f"measure-{rate_hz} {total_count:05d} 界".encode()
        session.wait_for(final_marker, timeout=2.0)
        session.close_stream()
        session.write_input(b"\x11")
        returncode = session.wait()
        if returncode != 0:
            raise ProbeFailure(f"performance child exited {returncode}")

        process_result = session.metrics()
        model_metrics = process_result.get("metrics", {})
        if model_metrics.get("delta_count") != total_count:
            raise ProbeFailure(
                f"delta count mismatch: {model_metrics.get('delta_count')} != {total_count}"
            )
        expected_draft = base_draft + "".join(markers)
        if model_metrics.get("draft") != expected_draft:
            raise ProbeFailure("draft changed or was lost during measured workload")
        if model_metrics.get("delivery_sample_count") != measured_count:
            raise ProbeFailure(
                "measured delivery sample mismatch: "
                f"{model_metrics.get('delivery_sample_count')} != {measured_count}"
            )

        delivery_ms = {
            percentile: round(
                float(model_metrics[f"delivery_{percentile}_micros"]) / 1000,
                3,
            )
            for percentile in ("p50", "p95", "p99", "max")
        }
        delivery_ms["sample_count"] = measured_count
        return {
            "status": "pass",
            "repetition": repetition,
            "delta_rate_hz": rate_hz,
            "warmup_delta_count": warmup_count,
            "measured_delta_count": measured_count,
            "keypress_latency_ms": distribution(key_latencies_ms),
            "stream_delivery_latency_ms": delivery_ms,
            "producer_schedule_lag_ms": distribution(
                list(producer_result["schedule_lag_ms"])
            ),
            "resource": _resource_metrics(
                resource_points,
                start_ns=measurement_start_monotonic_ns,
                end_ns=measurement_end_monotonic_ns,
            ),
            "renderer": _render_metrics(
                process_result.get("render_probe", {}),
                start_unix_ns=measurement_start_unix_ns,
                end_unix_ns=measurement_end_unix_ns,
            ),
            "draft_sha256": hashlib.sha256(expected_draft.encode()).hexdigest(),
        }
    finally:
        if sampler_started:
            sampler.stop()
        session.close()


def _nested_number(value: dict[str, Any], path: str) -> float:
    current: Any = value
    for component in path.split("."):
        current = current[component]
    return float(current)


def _aggregate_metric(runs: list[dict[str, Any]], path: str) -> dict[str, Any]:
    return distribution([_nested_number(run, path) for run in runs])


def _thresholds(rate_hz: int, measurement_seconds: float) -> dict[str, float]:
    return {
        "keypress_p95_ms": 50.0,
        "keypress_p99_ms": 100.0,
        "stream_delivery_p95_ms": 10.0,
        "producer_schedule_lag_p95_ms": 10.0,
        "renderer_write_interval_p99_ms": 100.0,
        "renderer_jitter_ms": 50.0,
        "minimum_renderer_writes": math.floor(
            measurement_seconds * min(rate_hz, 60) * 0.5
        ),
        "cpu_average_percent": 25.0 if rate_hz == 20 else 50.0,
        "cpu_peak_percent": 150.0,
        "rss_peak_mib": 128.0,
        "rss_growth_mib": 16.0,
        "keypress_p95_cross_run_spread_ms": 25.0,
        "cpu_average_cross_run_spread_percent": 20.0,
        "rss_steady_cross_run_spread_mib": 16.0,
        "renderer_p95_cross_run_spread_ms": 25.0,
    }


def aggregate_workload(
    runs: list[dict[str, Any]],
    *,
    rate_hz: int,
    measurement_seconds: float,
) -> dict[str, Any]:
    paths = {
        "keypress_p95_ms": "keypress_latency_ms.p95",
        "keypress_p99_ms": "keypress_latency_ms.p99",
        "stream_delivery_p95_ms": "stream_delivery_latency_ms.p95",
        "producer_schedule_lag_p95_ms": "producer_schedule_lag_ms.p95",
        "renderer_write_count": "renderer.physical_write_count",
        "renderer_write_interval_p95_ms": "renderer.write_interval_ms.p95",
        "renderer_write_interval_p99_ms": "renderer.write_interval_ms.p99",
        "renderer_jitter_ms": "renderer.jitter_p95_minus_p50_ms",
        "cpu_average_percent": "resource.cpu_average_percent",
        "cpu_peak_percent": "resource.cpu_interval_percent.max",
        "rss_steady_p95_mib": "resource.rss_mib.p95",
        "rss_peak_mib": "resource.rss_mib.max",
        "rss_growth_mib": "resource.rss_growth_mib",
    }
    metrics = {name: _aggregate_metric(runs, path) for name, path in paths.items()}
    metrics["rss_absolute_growth_mib"] = distribution(
        [abs(_nested_number(run, "resource.rss_growth_mib")) for run in runs]
    )
    limits = _thresholds(rate_hz, measurement_seconds)

    def spread(name: str) -> float:
        return round(float(metrics[name]["p95"]) - float(metrics[name]["p05"]), 3)

    checks = {
        "all_runs_completed": all(run["status"] == "pass" for run in runs),
        "keypress_p95": metrics["keypress_p95_ms"]["p95"] <= limits["keypress_p95_ms"],
        "keypress_p99": metrics["keypress_p99_ms"]["p95"] <= limits["keypress_p99_ms"],
        "stream_delivery_p95": metrics["stream_delivery_p95_ms"]["p95"]
        <= limits["stream_delivery_p95_ms"],
        "producer_schedule_lag_p95": metrics["producer_schedule_lag_p95_ms"]["p95"]
        <= limits["producer_schedule_lag_p95_ms"],
        "renderer_write_interval_p99": metrics["renderer_write_interval_p99_ms"]["p95"]
        <= limits["renderer_write_interval_p99_ms"],
        "renderer_jitter": metrics["renderer_jitter_ms"]["p95"]
        <= limits["renderer_jitter_ms"],
        "renderer_write_count": metrics["renderer_write_count"]["p05"]
        >= limits["minimum_renderer_writes"],
        "cpu_average": metrics["cpu_average_percent"]["p95"]
        <= limits["cpu_average_percent"],
        "cpu_peak": metrics["cpu_peak_percent"]["max"] <= limits["cpu_peak_percent"],
        "rss_peak": metrics["rss_peak_mib"]["max"] <= limits["rss_peak_mib"],
        "rss_growth": metrics["rss_absolute_growth_mib"]["max"]
        <= limits["rss_growth_mib"],
        "keypress_cross_run_spread": spread("keypress_p95_ms")
        <= limits["keypress_p95_cross_run_spread_ms"],
        "cpu_cross_run_spread": spread("cpu_average_percent")
        <= limits["cpu_average_cross_run_spread_percent"],
        "rss_cross_run_spread": spread("rss_steady_p95_mib")
        <= limits["rss_steady_cross_run_spread_mib"],
        "renderer_cross_run_spread": spread("renderer_write_interval_p95_ms")
        <= limits["renderer_p95_cross_run_spread_ms"],
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "thresholds": limits,
        "checks": checks,
        "cross_run_spread": {
            "keypress_p95_ms": spread("keypress_p95_ms"),
            "cpu_average_percent": spread("cpu_average_percent"),
            "rss_steady_p95_mib": spread("rss_steady_p95_mib"),
            "renderer_write_interval_p95_ms": spread("renderer_write_interval_p95_ms"),
        },
        "metrics": metrics,
    }


def _command_output(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def host_identity(binary: Path, reader: ProcessResourceReader) -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "go_version": _command_output(["go", "version"]),
        "resource_sampler": reader.method,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }


def markdown_report(result: dict[str, Any]) -> str:
    contract = result["contract"]
    ordered_workloads = sorted(
        result["workloads"].items(),
        key=lambda item: int(item[0].removesuffix("hz")),
    )
    lines = [
        "# Bubble Tea S0 CPU / RSS / renderer cadence baseline",
        "",
        f"> generated: `{result['generated_at']}`  ",
        f"> overall gate: **{result['status'].upper()}**  ",
        f"> platform: `{result['host']['platform']}`",
        "",
        "## Frozen measurement contract",
        "",
        f"- repetitions: {contract['repetitions']} per workload",
        f"- warm-up: {contract['warmup_seconds']}s",
        f"- measured active window: {contract['measurement_seconds']}s",
        f"- resource sampling: {contract['sample_frequency_hz']}Hz",
        f"- key probes: {contract['key_probes_per_run']} per run",
        "- percentile algorithm: nearest-rank over raw samples; workload summaries "
        "are nearest-rank over per-run statistics",
        "- renderer cadence: physical non-empty writes at Bubble Tea's output writer",
        "",
        "## Results",
        "",
        "| workload | gate | key p95 | delivery p95 | render interval p99 | "
        "render jitter | CPU avg | RSS peak |",
        "|---:|:---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, workload in ordered_workloads:
        metrics = workload["aggregate"]["metrics"]
        lines.append(
            "| {name} | {status} | {key:.3f}ms | {delivery:.3f}ms | "
            "{render:.3f}ms | {jitter:.3f}ms | {cpu:.3f}% | {rss:.3f}MiB |".format(
                name=name,
                status=workload["aggregate"]["status"],
                key=metrics["keypress_p95_ms"]["p95"],
                delivery=metrics["stream_delivery_p95_ms"]["p95"],
                render=metrics["renderer_write_interval_p99_ms"]["p95"],
                jitter=metrics["renderer_jitter_ms"]["p95"],
                cpu=metrics["cpu_average_percent"]["p95"],
                rss=metrics["rss_peak_mib"]["max"],
            )
        )
    lines.extend(
        [
            "",
            "## Frozen workload gates",
            "",
            "| workload | key p95/p99 | delivery p95 | render p99/jitter | "
            "CPU avg/peak | RSS peak/growth |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, workload in ordered_workloads:
        limits = workload["aggregate"]["thresholds"]
        lines.append(
            "| {name} | ≤{key95:.0f}/{key99:.0f}ms | ≤{delivery:.0f}ms | "
            "≤{render:.0f}/{jitter:.0f}ms | ≤{cpuavg:.0f}/{cpupeak:.0f}% | "
            "≤{rsspeak:.0f}/{rssgrowth:.0f}MiB |".format(
                name=name,
                key95=limits["keypress_p95_ms"],
                key99=limits["keypress_p99_ms"],
                delivery=limits["stream_delivery_p95_ms"],
                render=limits["renderer_write_interval_p99_ms"],
                jitter=limits["renderer_jitter_ms"],
                cpuavg=limits["cpu_average_percent"],
                cpupeak=limits["cpu_peak_percent"],
                rsspeak=limits["rss_peak_mib"],
                rssgrowth=limits["rss_growth_mib"],
            )
        )
    first_limits = ordered_workloads[0][1]["aggregate"]["thresholds"]
    lines.extend(
        [
            "",
            "Cross-run allowed p95−p05 spread: keypress p95 "
            f"≤{first_limits['keypress_p95_cross_run_spread_ms']:.0f}ms, CPU average "
            f"≤{first_limits['cpu_average_cross_run_spread_percent']:.0f} percentage "
            f"points, RSS steady p95 ≤{first_limits['rss_steady_cross_run_spread_mib']:.0f}MiB, "
            f"renderer interval p95 ≤{first_limits['renderer_p95_cross_run_spread_ms']:.0f}ms.",
            "",
            "## Gate details",
            "",
        ]
    )
    for name, workload in ordered_workloads:
        aggregate = workload["aggregate"]
        failed = [key for key, passed in aggregate["checks"].items() if not passed]
        detail = "all checks passed" if not failed else "failed: " + ", ".join(failed)
        lines.append(f"- `{name}`: {detail}")
    lines.extend(
        [
            "",
            "CPU peak and cross-run spread are host-sensitive feasibility guards, "
            "not product SLOs. The JSON evidence retains every per-run summary.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--rates", type=int, nargs="+", default=DEFAULT_RATES_HZ)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--warmup-seconds", type=float, default=DEFAULT_WARMUP_SECONDS)
    parser.add_argument(
        "--measurement-seconds",
        type=float,
        default=DEFAULT_MEASUREMENT_SECONDS,
    )
    parser.add_argument(
        "--sample-interval-seconds",
        type=float,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
    )
    parser.add_argument("--key-probes", type=int, default=DEFAULT_KEY_PROBES)
    parser.add_argument(
        "--cooldown-seconds", type=float, default=DEFAULT_COOLDOWN_SECONDS
    )
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.repetitions < 1:
        raise ProbeFailure("repetitions must be positive")
    if not args.rates or any(rate < 1 for rate in args.rates):
        raise ProbeFailure("rates must contain positive integers")
    if args.warmup_seconds <= 0 or args.measurement_seconds <= 0:
        raise ProbeFailure("warm-up and measurement durations must be positive")
    if args.sample_interval_seconds <= 0:
        raise ProbeFailure("sample interval must be positive")
    if args.key_probes < 1:
        raise ProbeFailure("key probes must be positive")


def main() -> int:
    args = parse_args()
    validate_args(args)
    binary = args.binary.resolve()
    if not binary.is_file():
        raise ProbeFailure(f"binary not found: {binary}")
    reader = platform_resource_reader()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "host": host_identity(binary, reader),
        "contract": {
            "rates_hz": args.rates,
            "repetitions": args.repetitions,
            "warmup_seconds": args.warmup_seconds,
            "measurement_seconds": args.measurement_seconds,
            "sample_interval_seconds": args.sample_interval_seconds,
            "sample_frequency_hz": round(1 / args.sample_interval_seconds, 3),
            "key_probes_per_run": args.key_probes,
            "cooldown_seconds": args.cooldown_seconds,
            "percentile_algorithm": "nearest_rank",
            "renderer_measurement": "bubbletea_physical_output_write_interval",
        },
        "workloads": {},
    }
    for rate_hz in args.rates:
        runs: list[dict[str, Any]] = []
        for repetition in range(1, args.repetitions + 1):
            print(
                f"[{rate_hz}Hz] repetition {repetition}/{args.repetitions}",
                file=sys.stderr,
                flush=True,
            )
            runs.append(
                run_workload_once(
                    binary,
                    rate_hz=rate_hz,
                    repetition=repetition,
                    warmup_seconds=args.warmup_seconds,
                    measurement_seconds=args.measurement_seconds,
                    sample_interval_seconds=args.sample_interval_seconds,
                    key_probes=args.key_probes,
                    reader=reader,
                )
            )
            time.sleep(args.cooldown_seconds)
        result["workloads"][f"{rate_hz}hz"] = {
            "runs": runs,
            "aggregate": aggregate_workload(
                runs,
                rate_hz=rate_hz,
                measurement_seconds=args.measurement_seconds,
            ),
        }
    result["status"] = (
        "pass"
        if all(
            workload["aggregate"]["status"] == "pass"
            for workload in result["workloads"].values()
        )
        else "fail"
    )
    rendered_json = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    rendered_markdown = markdown_report(result)
    print(rendered_markdown)
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered_json + "\n", encoding="utf-8")
    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(rendered_markdown, encoding="utf-8")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
