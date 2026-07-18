"""Explicit in-memory RuntimeSession compatibility factory for tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.tool_artifacts import InMemoryToolResultArtifactIndex


def in_memory_runtime_session(workspace_root: Path, **kwargs: Any) -> RuntimeSession:
    """Build the legacy non-durable substrate explicitly inside tests."""

    kwargs.setdefault("event_log", InMemoryEventLog())
    kwargs.setdefault("archive", InMemoryArchiveStore())
    kwargs.setdefault("tool_result_artifacts", InMemoryToolResultArtifactIndex())
    kwargs.setdefault("allow_unbootstrapped_test_events", True)
    return RuntimeSession(workspace_root, **kwargs)
