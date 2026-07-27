"""Deterministic structural benchmark for the durable projection pipeline."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from concurrent.futures import Future
import json
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any

from pulsara_agent.event import EventContext, ReplyEndEvent, ReplyStartEvent
from pulsara_agent.runtime.blocking_executor import (
    blocking_executor_capacity,
    projection_maintenance_executor,
)
from pulsara_agent.runtime.projection_jobs.service import (
    DurableProjectionJobService,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionKind,
)
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.runtime.projection_jobs.timeline import (
    IncrementalRunTimelineReducer,
)
from pulsara_agent.settings import PulsaraSettings
from pulsara_agent.storage.postgres_connection_provider import (
    POSTGRES_POOL_POLICIES,
    PostgresConnectionLane,
)
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)


def run_pure_benchmark(
    *,
    event_count: int = 100_000,
    wake_count: int = 1_000,
) -> dict[str, Any]:
    if event_count < 3:
        raise ValueError("event_count must be at least three")
    if wake_count < 1:
        raise ValueError("wake_count must be positive")
    timeline = _fold_timeline(event_count)
    wake = asyncio.run(_measure_wake_coalescing(wake_count))
    executor = _measure_projection_executor()
    result = {
        "schema_version": "durable_projection_pipeline_benchmark.v1",
        "timeline": timeline,
        "publisher_wake": wake,
        "projection_executor": executor,
    }
    _require_pure_gate(result)
    return result


def run_postgres_saturation(*, runtime_dsn: str) -> dict[str, Any]:
    lease = acquire_verified_postgres_access_sync(
        runtime_dsn,
        deadline_monotonic=monotonic() + 30.0,
    )
    provider = lease.connection_provider
    projection_policy = POSTGRES_POOL_POLICIES[
        PostgresConnectionLane.PROJECTION_MAINTENANCE
    ]
    projection_pool = provider.pool(
        lane=PostgresConnectionLane.PROJECTION_MAINTENANCE,
        deadline_monotonic=monotonic() + 30.0,
    )
    event_pool = provider.pool(
        lane=PostgresConnectionLane.EVENT_LOG,
        deadline_monotonic=monotonic() + 30.0,
    )
    release = Event()
    all_projection_connections = Event()
    lock = Lock()
    active = 0

    def hold_projection_connection() -> None:
        nonlocal active
        with projection_pool.connection(timeout=10.0) as connection:
            connection.execute("select 1").fetchone()
            with lock:
                active += 1
                if active == projection_policy.max_size:
                    all_projection_connections.set()
            try:
                if not release.wait(timeout=20.0):
                    raise TimeoutError("projection saturation release timed out")
            finally:
                with lock:
                    active -= 1

    futures = tuple(
        projection_maintenance_executor().submit(hold_projection_connection)
        for _ in range(projection_policy.max_size)
    )
    try:
        if not all_projection_connections.wait(timeout=20.0):
            raise TimeoutError("projection lane did not reach saturation")
        started = monotonic()
        with event_pool.connection(timeout=5.0) as connection:
            assert connection.execute("select 1").fetchone()[0] == 1
        event_checkout_seconds = monotonic() - started
    finally:
        release.set()
        for future in futures:
            future.result(timeout=20.0)
        lease.release()
    result = {
        "schema_version": "durable_projection_postgres_saturation.v1",
        "projection_lane_capacity": projection_policy.max_size,
        "projection_connections_held": projection_policy.max_size,
        "event_log_checkout_succeeded": True,
        "event_log_checkout_seconds_diagnostic": event_checkout_seconds,
    }
    if not result["event_log_checkout_succeeded"]:
        raise AssertionError("critical EventLog lane was starved")
    return result


def _fold_timeline(event_count: int) -> dict[str, Any]:
    context = EventContext(
        run_id="run:durable-projection-benchmark",
        turn_id="turn:durable-projection-benchmark",
        reply_id="reply:durable-projection-benchmark",
    )
    reply_start = ReplyStartEvent(**context.event_fields(), name="assistant")
    reply_end = ReplyEndEvent(
        **context.event_fields(),
        model_terminal_outcome="completed",
    )
    reducer = IncrementalRunTimelineReducer(
        runtime_session_id="runtime:durable-projection-benchmark",
        run_id=context.run_id,
    )
    completed_item_count = 0
    started = monotonic()
    for sequence in range(1, event_count + 1):
        position = sequence % 100
        template = reply_start if position == 1 else reply_end
        reducer.apply(template.model_copy(update={"sequence": sequence}))
        completed_item_count += len(reducer.take_completed_items())
    elapsed = monotonic() - started
    open_state = reducer.open_state_payload()
    return {
        "source_event_count": event_count,
        "folded_source_event_count": event_count,
        "completed_item_count": completed_item_count,
        "resident_open_item_count": len(open_state["open_items"]),
        "resident_open_state_utf8_bytes": len(
            json.dumps(
                open_state,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "elapsed_seconds_diagnostic": elapsed,
    }


async def _measure_wake_coalescing(wake_count: int) -> dict[str, Any]:
    service = object.__new__(DurableProjectionJobService)
    service._accepting = True
    service._wake_count = 0
    service._wake_event = asyncio.Event()
    service._dirty_authority_hints = deque(maxlen=4096)
    service._trigger_kinds_by_event_type = {
        "REPLY_END": (DurableProjectionKind.RUN_TIMELINE,)
    }
    published = RuntimePublishedEvent(
        runtime_session_id="runtime:benchmark",
        event=ReplyEndEvent(
            **EventContext(
                "run:benchmark",
                "turn:benchmark",
                "reply:benchmark",
            ).event_fields(),
            sequence=1,
            model_terminal_outcome="completed",
        ),
    )
    before = set(asyncio.all_tasks())
    for _ in range(wake_count):
        await service.on_published_event(published)
    created = {
        task
        for task in asyncio.all_tasks()
        if task not in before and task is not asyncio.current_task()
    }
    return {
        "wake_count": service._wake_count,
        "wake_event_set": service._wake_event.is_set(),
        "new_worker_task_count": len(created),
        "serialized_source_payload_bytes": 0,
        "storage_or_external_call_count": 0,
        "dirty_authority_hint_count": len(service._dirty_authority_hints),
    }


def _measure_projection_executor() -> dict[str, Any]:
    capacity = blocking_executor_capacity().projection_maintenance_workers
    release = Event()
    all_workers_started = Event()
    lock = Lock()
    active = 0
    maximum_active = 0

    def block() -> None:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == capacity:
                all_workers_started.set()
        try:
            if not release.wait(timeout=20.0):
                raise TimeoutError("projection executor release timed out")
        finally:
            with lock:
                active -= 1

    futures: tuple[Future[None], ...] = tuple(
        projection_maintenance_executor().submit(block) for _ in range(capacity * 2)
    )
    try:
        if not all_workers_started.wait(timeout=20.0):
            raise TimeoutError("projection executor did not reach configured capacity")
    finally:
        release.set()
        for future in futures:
            future.result(timeout=20.0)
    return {
        "configured_worker_capacity": capacity,
        "maximum_observed_active_workers": maximum_active,
        "submitted_operation_count": len(futures),
    }


def _require_pure_gate(result: dict[str, Any]) -> None:
    timeline = result["timeline"]
    wake = result["publisher_wake"]
    executor = result["projection_executor"]
    if timeline["folded_source_event_count"] != timeline["source_event_count"]:
        raise AssertionError("timeline source fold count drifted")
    if timeline["resident_open_item_count"] != 0:
        raise AssertionError("timeline reducer retained closed source state")
    if wake["new_worker_task_count"] > 1:
        raise AssertionError("coalesced wakes created multiple worker tasks")
    if wake["storage_or_external_call_count"] != 0:
        raise AssertionError("publisher wake performed external I/O")
    if (
        executor["maximum_observed_active_workers"]
        > executor["configured_worker_capacity"]
    ):
        raise AssertionError("projection executor exceeded process-wide capacity")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic durable-projection structural benchmarks."
    )
    parser.add_argument("--event-count", type=int, default=100_000)
    parser.add_argument("--wake-count", type=int, default=1_000)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_pure_benchmark(
        event_count=args.event_count,
        wake_count=args.wake_count,
    )
    if args.postgres:
        settings = (
            PulsaraSettings.from_env_file(args.env_file)
            if args.env_file.exists()
            else PulsaraSettings.from_env()
        )
        report["postgres_saturation"] = run_postgres_saturation(
            runtime_dsn=settings.storage.postgres_dsn
        )
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
