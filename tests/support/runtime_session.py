"""Explicit in-memory RuntimeSession compatibility factory for tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any

from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.runtime.session import RuntimeSession
from tests.support.artifacts import FakeToolResultArtifactIndex


_IN_MEMORY_ARCHIVE_LOCK = RLock()
_IN_MEMORY_ARCHIVES_BY_EVENT_LOG: list[
    tuple[InMemoryEventLog, InMemoryArchiveStore]
] = []


def _archive_for_event_log(event_log: InMemoryEventLog) -> InMemoryArchiveStore:
    """Keep both durable substrates stable across an in-memory session restart."""

    with _IN_MEMORY_ARCHIVE_LOCK:
        for owner, archive in _IN_MEMORY_ARCHIVES_BY_EVENT_LOG:
            if owner is event_log:
                return archive
        archive = InMemoryArchiveStore()
        _IN_MEMORY_ARCHIVES_BY_EVENT_LOG.append((event_log, archive))
        return archive


def in_memory_runtime_session(workspace_root: Path, **kwargs: Any) -> RuntimeSession:
    """Build the legacy non-durable substrate explicitly inside tests."""

    event_log = kwargs.setdefault("event_log", InMemoryEventLog())
    kwargs.setdefault("archive", _archive_for_event_log(event_log))
    kwargs.setdefault("tool_result_artifacts", FakeToolResultArtifactIndex())
    kwargs.setdefault("allow_unbootstrapped_test_events", True)
    return RuntimeSession(workspace_root, **kwargs)


async def aclose_runtime_session_for_test(
    runtime_session: RuntimeSession,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Drain the real async owners before exercising synchronous close guards.

    Focused fixtures used to call ``RuntimeSession.close()`` directly.  That
    stopped being a legal ownership transition once runtime-projection
    checkpoint maintenance became asynchronous.  This helper intentionally
    mirrors the production physical drain without weakening reconciliation or
    close-if-idle assertions and remains test-only.
    """

    deadline = monotonic() + timeout_seconds
    await runtime_session.quiesce_provider_input_event_producers_for_close(
        deadline_monotonic=deadline
    )
    await runtime_session.mandatory_runtime_audit_owner.drain(
        deadline_monotonic=deadline
    )
    await runtime_session.drain_open_committed_reducer_barrier(
        deadline_monotonic=deadline
    )
    runtime_session.require_mutation_allowed()
    await runtime_session.runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain(
        deadline_monotonic=deadline
    )
    if runtime_session.window_compaction_service is not None:
        await runtime_session.window_compaction_service.drain_pending(
            deadline_monotonic=deadline
        )
    await runtime_session.transcript_projection_checkpoint_service.request_close_cancellation()
    await runtime_session.subagent_graph_checkpoint_service.drain_pending(
        deadline_monotonic=deadline
    )
    await runtime_session.transcript_projection_checkpoint_service.drain_pending(
        deadline_monotonic=deadline
    )
    await runtime_session.prompt_queue_checkpoint_service.drain_pending(
        deadline_monotonic=deadline
    )
    await runtime_session.terminal_presentation_foundation_service.stop_admission_and_drain(
        deadline_monotonic=deadline
    )
    await runtime_session.context_input_io_service.drain_pending(
        deadline_monotonic=deadline
    )
    runtime_session.close()


def close_runtime_session_for_test(
    runtime_session: RuntimeSession,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Synchronous wrapper for tests that do not already own an event loop."""

    asyncio.run(
        aclose_runtime_session_for_test(
            runtime_session,
            timeout_seconds=timeout_seconds,
        )
    )
