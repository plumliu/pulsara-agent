"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog, InMemoryEventLog
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.runtime.hooks import RuntimeHookManager
from pulsara_agent.runtime.permission import PermissionState
from pulsara_agent.runtime.publisher import RuntimeEventPublisher, RuntimePublishedEvent
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.terminal import TerminalSessionManager
from pulsara_agent.runtime.tool_artifacts import (
    InMemoryToolResultArtifactIndex,
    ToolResultArtifactIndex,
    ToolResultArtifactService,
)


@dataclass(frozen=True, slots=True)
class RuntimeThreadRecorder:
    runtime_session: "RuntimeSession"
    state: LoopState | None = None

    def __call__(self, event: AgentEvent) -> AgentEvent:
        return self.runtime_session.emit_from_thread(event, state=self.state)


@dataclass(slots=True)
class RuntimeSession:
    workspace_root: Path
    runtime_session_id: str = field(default_factory=lambda: f"runtime:{uuid4().hex}")
    event_log: EventLog = field(default_factory=InMemoryEventLog)
    hook_manager: RuntimeHookManager = field(default_factory=RuntimeHookManager)
    memory_proposal_sink: MemoryProposalSink = field(default_factory=MemoryProposalSink)
    archive: ArtifactStore | None = None
    tool_result_artifacts: ToolResultArtifactIndex | None = None
    terminal_session_manager: TerminalSessionManager | None = None
    owns_terminal_session_manager: bool = True
    terminal_owner_host_session_id: str | None = None
    publisher: RuntimeEventPublisher = field(init=False)
    terminal_sessions: TerminalSessionManager = field(init=False)
    artifact_service: ToolResultArtifactService = field(init=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if self.archive is None:
            self.archive = InMemoryArchiveStore()
        if self.tool_result_artifacts is None:
            self.tool_result_artifacts = InMemoryToolResultArtifactIndex()
        self.artifact_service = ToolResultArtifactService(
            archive=self.archive,
            index=self.tool_result_artifacts,
            runtime_session_id=self.runtime_session_id,
        )
        self.publisher = RuntimeEventPublisher(runtime_session_id=self.runtime_session_id)
        self.publisher.subscribe(self.hook_manager)
        if self.terminal_session_manager is None:
            self.terminal_sessions = TerminalSessionManager(self.workspace_root)
            self.owns_terminal_session_manager = True
        else:
            self.terminal_sessions = self.terminal_session_manager

    def _require_runtime_managed_sequence(self, event: AgentEvent) -> None:
        if event.sequence is not None:
            raise ValueError(
                "RuntimeSession.emit requires sequence=None; canonical sequence is assigned by EventLog"
            )

    async def emit(self, event: AgentEvent, *, state: LoopState | None = None) -> AgentEvent:
        self._require_runtime_managed_sequence(event)
        stored = self.event_log.append(event)
        await self.publisher.publish(
            RuntimePublishedEvent(
                runtime_session_id=self.runtime_session_id,
                event=stored,
                state=state,
            )
        )
        return stored

    async def emit_many(
        self,
        events: Iterable[AgentEvent],
        *,
        state: LoopState | None = None,
    ) -> list[AgentEvent]:
        stored_events: list[AgentEvent] = []
        for event in events:
            stored_events.append(await self.emit(event, state=state))
        return stored_events

    def emit_from_thread(self, event: AgentEvent, *, state: LoopState | None = None) -> AgentEvent:
        self._require_runtime_managed_sequence(event)
        stored = self.event_log.append(event)
        published = RuntimePublishedEvent(
            runtime_session_id=self.runtime_session_id,
            event=stored,
            state=state,
        )
        if not self.publisher.publish_from_thread(published):
            self.publisher.discard_unpublished(published)
        return stored

    def make_thread_recorder(self, *, state: LoopState | None = None) -> RuntimeThreadRecorder:
        return RuntimeThreadRecorder(runtime_session=self, state=state)

    def close(self) -> None:
        if self.owns_terminal_session_manager:
            self.terminal_sessions.shutdown()
        elif self.terminal_owner_host_session_id is not None:
            self.terminal_sessions.kill_owned(self.terminal_owner_host_session_id)

    def create_tool_executor(
        self,
        *,
        record_event: RuntimeThreadRecorder | None = None,
        memory_proposal_sink: MemoryProposalSink | None = None,
        memory_recall_service=None,
        memory_query=None,
        graph_id: str | None = None,
        memory_read_scopes: frozenset[str] | None = None,
        permission_state: PermissionState | None = None,
    ):
        from pulsara_agent.tools import ToolExecutor
        from pulsara_agent.tools.builtins.registry import build_core_tool_registry

        if record_event is not None and not isinstance(record_event, RuntimeThreadRecorder):
            raise TypeError(
                "create_tool_executor(record_event=...) requires RuntimeSession.make_thread_recorder(...)"
            )

        return ToolExecutor(
            registry=build_core_tool_registry(
                self,
                memory_proposal_sink=memory_proposal_sink,
                memory_recall_service=memory_recall_service,
                memory_query=memory_query,
                graph_id=graph_id,
                memory_read_scopes=memory_read_scopes,
                permission_state=permission_state,
            ),
            record_event=record_event,
            artifact_service=self.artifact_service,
        )
