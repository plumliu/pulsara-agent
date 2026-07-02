"""Runtime session ownership for one active Pulsara backend run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from pulsara_agent.event import AgentEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.runtime.hooks import RuntimeHookManager
from pulsara_agent.runtime.permission import PermissionState
from pulsara_agent.runtime.publisher import RuntimeEventPublisher, RuntimePublishedEvent
from pulsara_agent.runtime.state import LoopState
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    OwnedTerminalRuntime,
    TerminalOwnerContext,
    TerminalRuntimeBinding,
    TerminalSessionManager,
)
from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactIndex, ToolResultArtifactService


@dataclass(frozen=True, slots=True)
class RuntimeThreadRecorder:
    runtime_session: "RuntimeSession"
    state: LoopState | None = None

    def __call__(self, event: AgentEvent) -> AgentEvent:
        return self.runtime_session.emit_from_thread(event, state=self.state)


@dataclass(slots=True)
class RuntimeSession:
    workspace_root: Path
    event_log: EventLog
    archive: ArtifactStore
    tool_result_artifacts: ToolResultArtifactIndex
    runtime_session_id: str = field(default_factory=lambda: f"runtime:{uuid4().hex}")
    hook_manager: RuntimeHookManager = field(default_factory=RuntimeHookManager)
    memory_proposal_sink: MemoryProposalSink = field(default_factory=MemoryProposalSink)
    terminal_binding: TerminalRuntimeBinding | None = None
    publisher: RuntimeEventPublisher = field(init=False)
    terminal_sessions: TerminalSessionManager = field(init=False)
    artifact_service: ToolResultArtifactService = field(init=False)
    _owns_terminal_manager: bool = field(default=False, init=False, repr=False)
    _terminal_owner: TerminalOwnerContext | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.workspace_root = self.workspace_root.expanduser().resolve()
        self.artifact_service = ToolResultArtifactService(
            archive=self.archive,
            index=self.tool_result_artifacts,
            runtime_session_id=self.runtime_session_id,
        )
        self.publisher = RuntimeEventPublisher(
            runtime_session_id=self.runtime_session_id,
            next_sequence_to_publish=_next_publish_sequence(self.event_log),
        )
        self.publisher.subscribe(self.hook_manager)
        self._bind_terminal(self.terminal_binding)

    def _bind_terminal(self, binding: TerminalRuntimeBinding | None) -> None:
        # Default is owned-local: a bare RuntimeSession(workspace_root) keeps a
        # private manager it shuts down on close. HostCore injects a borrowed
        # binding whose lease release is the supervisor's job, not ours.
        binding = binding or OwnedTerminalRuntime()
        self.terminal_binding = binding
        if isinstance(binding, BorrowedWorkspaceTerminalRuntime):
            self.terminal_sessions = binding.manager
            self._owns_terminal_manager = False
            self._terminal_owner = binding.owner
        else:
            self.terminal_sessions = binding.manager or TerminalSessionManager(self.workspace_root)
            self._owns_terminal_manager = True
            self._terminal_owner = None

    @property
    def terminal_owner_host_session_id(self) -> str | None:
        return self._terminal_owner.host_session_id if self._terminal_owner is not None else None

    @property
    def terminal_owner_conversation_id(self) -> str | None:
        return self._terminal_owner.conversation_id if self._terminal_owner is not None else None

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

    def publish_stored_event(self, event: AgentEvent, *, state: LoopState | None = None) -> None:
        if event.sequence is None:
            raise ValueError("Stored events must have a canonical sequence")
        published = RuntimePublishedEvent(
            runtime_session_id=self.runtime_session_id,
            event=event,
            state=state,
        )
        if not self.publisher.publish_from_thread(published):
            self.publisher.discard_unpublished(published)

    def publish_stored_events(
        self,
        events: Iterable[AgentEvent],
        *,
        state: LoopState | None = None,
    ) -> None:
        for event in events:
            self.publish_stored_event(event, state=state)

    def make_thread_recorder(self, *, state: LoopState | None = None) -> RuntimeThreadRecorder:
        return RuntimeThreadRecorder(runtime_session=self, state=state)

    def close(self) -> None:
        # Owned-local: we shut the manager down. Borrowed (HostCore path): we do
        # NOT kill/detach/shutdown the shared manager here — lease release is the
        # supervisor/HostCore job and must run exactly once (contract §5).
        # Idempotent: shutting an already-shut manager down is a no-op.
        if self._owns_terminal_manager:
            self.terminal_sessions.shutdown()

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
            runtime_session_id=self.runtime_session_id,
        )


def _next_publish_sequence(event_log: EventLog) -> int:
    return event_log.next_sequence()
