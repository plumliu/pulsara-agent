"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pulsara_agent.event import InMemoryEventLog
from pulsara_agent.runtime.terminal import TerminalSessionManager


@dataclass(slots=True)
class RuntimeSession:
    workspace_root: Path
    runtime_session_id: str = field(default_factory=lambda: f"runtime:{uuid4().hex}")
    event_log: InMemoryEventLog = field(default_factory=InMemoryEventLog)
    terminal_sessions: TerminalSessionManager = field(init=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.terminal_sessions = TerminalSessionManager(self.workspace_root)

    def create_tool_executor(self):
        from pulsara_agent.tools import ToolExecutor
        from pulsara_agent.tools.builtins.registry import build_core_tool_registry

        return ToolExecutor(
            registry=build_core_tool_registry(self),
            event_log=self.event_log,
        )

