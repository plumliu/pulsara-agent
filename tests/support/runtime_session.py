"""Explicit in-memory RuntimeSession compatibility factory for tests."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
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
