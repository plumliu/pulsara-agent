"""Single-authority Protocol v3 gateway for the canonical conversation kernel."""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
from time import monotonic
from typing import Callable
from uuid import uuid4

from google.protobuf.message import DecodeError, Message

from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.interaction import (
    ToolInteractionDecisionNotAccepted,
    ToolInteractionDecisionOutcomeUnknown,
)
from pulsara_agent.conversation_kernel.user_control import (
    ControlQueryStatus,
    UserControlOperation,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    ARTIFACT_READ_HARD_CHARS,
    PostgresToolArtifactReadPort,
)
from pulsara_agent.conversation_kernel.live import LiveObservationKind
from pulsara_agent.conversation_kernel.live import LiveSettlementKind
from pulsara_agent.ports.live_agent_event import (
    DataDeltaPayload,
    DataEndPayload,
    DataStartPayload,
    InteractionClosedPayload,
    InteractionOpenedPayload,
    InteractionReplacedPayload,
    ReasoningPresentationKind,
    SubagentProgressPayload,
    TerminalMonitorClosedPayload,
    TerminalMonitorObservationPayload,
    TerminalMonitorOpenedPayload,
    TerminalProcessCompletedPayload,
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
    TodoSnapshotUpdatedPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    ToolResultDeltaPayload,
    ToolResultEndPayload,
    ToolResultStartPayload,
)
from pulsara_agent.conversation_kernel.live_control import (
    CurrentInteractionView,
    LiveControlEventKind,
    LiveControlObservationKind,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolReader,
    CanonicalProtocolGap,
    CanonicalProtocolResourceExhausted,
    CanonicalQueueContentNotPending,
    MAXIMUM_HISTORY_PAGE_BYTES,
    MAXIMUM_OBSERVATION_EVENTS,
    MAXIMUM_SNAPSHOT_BYTES,
)
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    PlanDraftIdentityConflict,
    PlanQuestionAnswer,
)
from pulsara_agent.ports.artifact import ArtifactContentError
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftDecision,
    PlanQuestionAnswerKind,
)
from pulsara_agent.llm.input import (
    LLMTextPart,
    PromptContent,
    PromptImagePart,
    prompt_text_utf8_bytes,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_process.models import TerminalProcessInfo


PROTOCOL_MAJOR = 3
PROTOCOL_MINOR = 0
PROTOCOL_SCHEMA_FINGERPRINT = (
    "sha256:9c393b239755610acb10131477c7965ec313e20350c43730ac27695e67d4fd9c"
)
MAXIMUM_FRAME_BYTES = 8 << 20
MAXIMUM_OBSERVATION_WAIT_MS = STAGE2_LIMITS.committed_observation_hard_wait_ms
HEARTBEAT_INTERVAL_MS = 10_000
MAXIMUM_COMMAND_ID_BYTES = 512
MAXIMUM_LIVE_CONTROL_EVENTS = STAGE2_LIMITS.live_control_hard_events
SessionProvider = Callable[[str], KernelHostSession]


@dataclass(slots=True)
class _Connection:
    attachment_id: str = ""
    attachment_generation: int = 0
    host_session: KernelHostSession | None = None
    protocol_reader: CanonicalProtocolReader | None = None
    live_observer_id: str = ""
    live_epoch: int = 0
    live_revision: int = 0
    live_control_subscriber_id: str = ""
    live_control_epoch: int = 0
    live_control_revision: int = 0
    granted_role: int = wire.ATTACHMENT_ROLE_UNSPECIFIED
    authenticated: bool = False


def protocol_identity() -> wire.ProtocolIdentity:
    return wire.ProtocolIdentity(
        major=PROTOCOL_MAJOR,
        minor=PROTOCOL_MINOR,
        schema_fingerprint=PROTOCOL_SCHEMA_FINGERPRINT,
    )


def install_fingerprint(namespace: str, message: Message, field: str) -> str:
    clone = type(message)()
    clone.CopyFrom(message)
    setattr(clone, field, "")
    value = (
        "sha256:"
        + sha256(
            namespace.encode() + b"\0" + clone.SerializeToString(deterministic=True)
        ).hexdigest()
    )
    setattr(message, field, value)
    return value


class TerminalKernelProtocolServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        session_provider: SessionProvider,
        maximum_frame_bytes: int = MAXIMUM_FRAME_BYTES,
    ) -> None:
        if not 1024 <= maximum_frame_bytes <= MAXIMUM_FRAME_BYTES:
            raise ValueError("Protocol v3 frame bound is invalid")
        self.socket_path = socket_path
        self._session_provider = session_provider
        self._maximum_frame_bytes = maximum_frame_bytes
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._launch_id = f"terminal-v3-launch:{uuid4().hex}"
        self._launch_capability = secrets.token_bytes(32)
        self._attachment_generation = 0

    @property
    def launch_id(self) -> str:
        return self._launch_id

    @property
    def launch_capability(self) -> bytes:
        return bytes(self._launch_capability)

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Protocol v3 server already started")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._accept, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._launch_capability = b""

    async def controller_attachment_closed(
        self, *, session_id: str, host_session_id: str, attachment_id: str,
    ) -> None:
        """Private bridge adaptation of its own exact attachment lifecycle.

        The stream can still be settling an admitted decision. Revoke through
        the original Host now; its later gateway finally is a compare-and-detach
        of the same unique attachment and cannot revoke a replacement.
        """
        try:
            session = self._session_provider(host_session_id)
        except (KeyError, RuntimeError):
            return
        if session.session_id != session_id or session.host_session_id != host_session_id:
            raise ConversationKernelConflict("browser attachment Host binding changed")
        await session.controller_detached(attachment_id)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        state = _Connection()
        try:
            while True:
                frame = await self._read_frame(reader)
                response = await self._dispatch(state, frame)
                await self._write_frame(writer, response)
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            try:
                await self._write_frame(
                    writer,
                    wire.ServerFrame(
                        error=wire.ProtocolError(
                            stable_code=_stable_error_code(exc),
                            public_message="Protocol v3 request was rejected.",
                        )
                    ),
                )
            except BaseException:
                pass
        finally:
            if state.host_session is not None and state.live_observer_id:
                state.host_session.live_bus.detach(state.live_observer_id)
            if state.host_session is not None and state.live_control_subscriber_id:
                state.host_session.live_control.detach(state.live_control_subscriber_id)
            if (
                state.host_session is not None
                and state.granted_role == wire.ATTACHMENT_ROLE_CONTROLLER
                and state.attachment_id
            ):
                await state.host_session.controller_detached(state.attachment_id)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self._connections.discard(task)

    async def _dispatch(
        self, state: _Connection, frame: wire.ClientFrame
    ) -> wire.ServerFrame:
        kind = frame.WhichOneof("request")
        if kind == "hello":
            return await self._hello(state, frame.hello)
        if not state.authenticated or state.host_session is None:
            return _error(_request_id(frame), "AUTH_REQUIRED")
        request = getattr(frame, kind) if kind else None
        if request is None or not self._attachment_matches(state, request):
            return _error(_request_id(frame), "STALE_ATTACHMENT")
        if kind == "snapshot":
            return await self._snapshot(state, request)
        if kind == "history_page":
            return await self._history(state, request)
        if kind == "observe":
            return await self._observe(state, request)
        if kind == "command":
            return await self._command(state, request)
        if kind == "query_command":
            return await self._query_command(state, request)
        if kind == "read_content":
            return await self._read_content(state, request)
        if kind == "read_tool_artifact":
            return await self._read_tool_artifact(state, request)
        if kind == "list_background_processes":
            return await self._list_background_processes(state, request)
        if kind == "read_background_process_log":
            return await self._read_background_process_log(state, request)
        if kind == "heartbeat":
            return wire.ServerFrame(
                heartbeat=wire.HeartbeatResponse(
                    request_id=request.request_id, active=True
                )
            )
        if kind == "live_control_snapshot":
            return self._live_control_snapshot(state, request)
        if kind == "resolve_interaction":
            return await self._resolve_interaction(state, request)
        if kind == "resolve_plan_interaction":
            return await self._resolve_plan_interaction(state, request)
        if kind == "read_plan_question":
            return await self._read_plan_question(state, request)
        if kind == "read_plan_draft":
            return await self._read_plan_draft(state, request)
        return _error(_request_id(frame), "UNKNOWN_REQUEST")

    async def _hello(
        self, state: _Connection, request: wire.HelloRequest
    ) -> wire.ServerFrame:
        if state.authenticated:
            return _error(request.request_id, "HELLO_ALREADY_ACCEPTED")
        protocol = request.protocol
        if (
            protocol.major != PROTOCOL_MAJOR
            or protocol.minor != PROTOCOL_MINOR
            or protocol.schema_fingerprint != PROTOCOL_SCHEMA_FINGERPRINT
        ):
            return _error(request.request_id, "PROTOCOL_V3_REQUIRED")
        if request.launch_id != self._launch_id or not hmac.compare_digest(
            bytes(request.launch_capability), self._launch_capability
        ):
            return _error(request.request_id, "INVALID_LAUNCH_CAPABILITY")
        try:
            session = self._session_provider(request.host_session_id)
        except (KeyError, RuntimeError):
            return _error(request.request_id, "HOST_SESSION_NOT_FOUND")
        if session.session_id != request.session_id:
            return _error(request.request_id, "SESSION_BINDING_MISMATCH")
        if request.requested_role not in (
            wire.ATTACHMENT_ROLE_OBSERVER,
            wire.ATTACHMENT_ROLE_CONTROLLER,
        ):
            return _error(request.request_id, "ATTACHMENT_ROLE_INVALID")
        self._attachment_generation += 1
        attachment_generation = self._attachment_generation
        attachment_id = f"terminal-v3-attachment:{uuid4().hex}"
        # Bind cleanup before the awaitable attach/promote boundary so a
        # cancelled HELLO cannot leave an ownerless controller attachment.
        state.attachment_id = attachment_id
        state.attachment_generation = attachment_generation
        state.host_session = session
        state.granted_role = request.requested_role
        if (
            request.requested_role == wire.ATTACHMENT_ROLE_CONTROLLER
            and not await session.attach_controller(attachment_id)
        ):
            return _error(request.request_id, "CONTROLLER_UNAVAILABLE")
        state.protocol_reader = CanonicalProtocolReader(
            session.repository.connection_provider
        )
        state.live_observer_id, live_snapshot = (
            session.live_bus.subscribe_with_snapshot()
        )
        state.live_epoch = live_snapshot.generation
        state.live_revision = live_snapshot.through_revision
        state.granted_role = request.requested_role
        state.authenticated = True
        accepted = wire.HelloAccepted(
            request_id=request.request_id,
            protocol=protocol_identity(),
            attachment_id=state.attachment_id,
            attachment_generation=state.attachment_generation,
            granted_role=request.requested_role,
            heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS,
            maximum_frame_bytes=self._maximum_frame_bytes,
            live_owner_epoch=state.live_epoch,
            live_revision=state.live_revision,
            live_snapshot=_live_snapshot_to_wire(live_snapshot),
        )
        install_fingerprint(
            "terminal-v3-hello-accepted:v1", accepted, "result_fingerprint"
        )
        return wire.ServerFrame(hello=accepted)

    async def _snapshot(
        self, state: _Connection, request: wire.SnapshotRequest
    ) -> wire.ServerFrame:
        try:
            snapshot = await asyncio.to_thread(
                state.protocol_reader.snapshot,
                session_id=state.host_session.session_id,
                maximum_entries=request.maximum_entries,
                maximum_control_items=request.maximum_control_items,
                deadline_monotonic=monotonic() + 10.0,
                maximum_serialized_bytes=min(
                    MAXIMUM_SNAPSHOT_BYTES,
                    max(1024, self._maximum_frame_bytes - 1024),
                ),
            )
        except CanonicalProtocolResourceExhausted:
            return _error(request.request_id, "SNAPSHOT_RESOURCE_EXHAUSTED")
        result = wire.ServerFrame(
            snapshot=wire.SnapshotResponse(
                request_id=request.request_id, snapshot=snapshot
            )
        )
        return (
            result
            if self._fits_frame(result)
            else _error(request.request_id, "SNAPSHOT_RESOURCE_EXHAUSTED")
        )

    async def _history(
        self, state: _Connection, request: wire.HistoryPageRequest
    ) -> wire.ServerFrame:
        if request.cursor.session_id != state.host_session.session_id:
            return _error(request.request_id, "HISTORY_CURSOR_SCOPE_MISMATCH")
        if not 1 <= request.maximum_serialized_bytes <= MAXIMUM_HISTORY_PAGE_BYTES:
            return _error(request.request_id, "HISTORY_RESOURCE_EXHAUSTED")
        try:
            entries, cursor, has_more = await asyncio.to_thread(
                state.protocol_reader.history_page,
                session_id=state.host_session.session_id,
                cut_sequence=request.cursor.cut_sequence,
                before_entry_sequence=request.cursor.entry_sequence,
                maximum_entries=request.maximum_entries,
                deadline_monotonic=monotonic() + 10.0,
                maximum_serialized_bytes=min(
                    MAXIMUM_HISTORY_PAGE_BYTES,
                    request.maximum_serialized_bytes,
                    max(1024, self._maximum_frame_bytes - 1024),
                ),
            )
        except CanonicalProtocolGap:
            return _error(request.request_id, "HISTORY_GAP")
        except CanonicalProtocolResourceExhausted:
            return _error(request.request_id, "HISTORY_RESOURCE_EXHAUSTED")
        response = wire.HistoryPageResponse(
            request_id=request.request_id, entries=entries, has_more=has_more
        )
        if cursor is not None:
            response.older_history_cursor.CopyFrom(cursor)
        result = wire.ServerFrame(history_page=response)
        return (
            result
            if self._fits_frame(result)
            else _error(request.request_id, "HISTORY_RESOURCE_EXHAUSTED")
        )

    def _live_control_snapshot(
        self, state: _Connection, request: wire.LiveControlSnapshotRequest
    ) -> wire.ServerFrame:
        if state.live_control_subscriber_id:
            state.host_session.live_control.detach(state.live_control_subscriber_id)
        subscriber_id, snapshot = (
            state.host_session.live_control.snapshot_and_subscribe()
        )
        state.live_control_subscriber_id = subscriber_id
        state.live_control_epoch = snapshot.owner_epoch
        state.live_control_revision = snapshot.revision
        return wire.ServerFrame(
            live_control_snapshot=wire.LiveControlSnapshotResponse(
                request_id=request.request_id,
                snapshot=_live_control_snapshot_to_wire(
                    snapshot,
                    state.host_session.current_todo_snapshots(),
                    state.host_session.current_compaction_projection(),
                    host_session_id=state.host_session.host_session_id,
                    active_root_turn_id=state.host_session.active_root_turn_id(),
                    control_admission_deadline_ms=(
                        state.host_session.control_admission_deadline_ms()
                    ),
                ),
            )
        )

    async def _observe(
        self, state: _Connection, request: wire.ObserveRequest
    ) -> wire.ServerFrame:
        if not 0 <= request.wait_ms <= MAXIMUM_OBSERVATION_WAIT_MS:
            return _error(request.request_id, "OBSERVATION_WAIT_OUT_OF_BOUNDS")
        deadline = monotonic() + request.wait_ms / 1000
        while True:
            batch = await asyncio.to_thread(
                state.protocol_reader.observe_committed,
                session_id=state.host_session.session_id,
                after_event_sequence=request.after_event_sequence,
                maximum_events=request.maximum_events,
                maximum_bytes=request.maximum_bytes,
                deadline_monotonic=monotonic() + 10.0,
            )
            if request.live_owner_epoch not in (0, state.live_epoch):
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        request_id=request.request_id,
                        through_event_sequence=batch.through_event_sequence,
                        live_owner_epoch=state.live_epoch,
                        gap=wire.ObservationGap(
                            kind=wire.LIVE_GAP,
                            reason="LIVE_OWNER_EPOCH_CHANGED",
                        ),
                    )
                )
            live = state.host_session.live_bus.observe(
                state.live_observer_id,
                after_revision=request.after_live_revision,
                maximum_events=max(
                    1, min(request.maximum_events, MAXIMUM_OBSERVATION_EVENTS)
                ),
            )
            if batch.gap_reason is not None:
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        request_id=request.request_id,
                        through_event_sequence=batch.through_event_sequence,
                        live_owner_epoch=state.live_epoch,
                        through_live_revision=live.latest_revision,
                        gap=wire.ObservationGap(
                            kind=wire.COMMITTED_GAP,
                            latest_sequence=batch.through_event_sequence,
                            reason=batch.gap_reason,
                        ),
                    )
                )
            if live.kind in (LiveObservationKind.GAP, LiveObservationKind.DETACHED):
                state.host_session.live_bus.detach(state.live_observer_id)
                (
                    state.live_observer_id,
                    state.live_epoch,
                    state.live_revision,
                ) = state.host_session.live_bus.subscribe()
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        request_id=request.request_id,
                        through_event_sequence=batch.through_event_sequence,
                        committed=batch.projections,
                        live_owner_epoch=state.live_epoch,
                        through_live_revision=state.live_revision,
                        gap=wire.ObservationGap(
                            kind=wire.LIVE_GAP,
                            latest_sequence=state.live_revision,
                            reason="LIVE_RING_OVERFLOW",
                        ),
                    )
                )
            live_wire = tuple(
                _live_to_wire(state.live_epoch, item) for item in live.events
            )
            settlements = tuple(
                _settlement_to_wire(state.live_epoch, item) for item in live.settlements
            )
            if not state.live_control_subscriber_id:
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        request_id=request.request_id,
                        through_event_sequence=batch.through_event_sequence,
                        live_owner_epoch=state.live_epoch,
                        through_live_revision=live.latest_revision,
                        gap=wire.ObservationGap(
                            kind=wire.LIVE_CONTROL_GAP,
                            reason="LIVE_CONTROL_SNAPSHOT_REQUIRED",
                        ),
                    )
                )
            control = state.host_session.live_control.observe(
                state.live_control_subscriber_id,
                owner_epoch=request.live_control_owner_epoch,
                after_revision=request.after_live_control_revision,
                maximum_events=max(
                    1, min(request.maximum_events, MAXIMUM_LIVE_CONTROL_EVENTS)
                ),
            )
            if control.kind in (
                LiveControlObservationKind.GAP,
                LiveControlObservationKind.DETACHED,
            ):
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        request_id=request.request_id,
                        through_event_sequence=batch.through_event_sequence,
                        live_owner_epoch=state.live_epoch,
                        through_live_revision=live.latest_revision,
                        live_control_owner_epoch=control.owner_epoch,
                        through_live_control_revision=control.latest_revision,
                        gap=wire.ObservationGap(
                            kind=wire.LIVE_CONTROL_GAP,
                            latest_sequence=control.latest_revision,
                            reason="LIVE_CONTROL_RING_GAP",
                        ),
                    )
                )
            control_wire = tuple(
                _live_control_event_to_wire(item) for item in control.events
            )
            presentation_notices = (
                state.host_session.take_presentation_notices(state.attachment_id)
                if state.granted_role == wire.ATTACHMENT_ROLE_CONTROLLER
                else ()
            )
            if (
                batch.projections
                or live_wire
                or settlements
                or control_wire
                or presentation_notices
                or monotonic() >= deadline
            ):
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        request_id=request.request_id,
                        through_event_sequence=batch.through_event_sequence,
                        committed=batch.projections,
                        live_owner_epoch=state.live_epoch,
                        through_live_revision=live.latest_revision,
                        live=live_wire,
                        settlements=settlements,
                        live_control_owner_epoch=control.owner_epoch,
                        through_live_control_revision=control.through_revision,
                        live_control=control_wire,
                        presentation_notices=presentation_notices,
                    )
                )
            await asyncio.sleep(min(0.05, max(0.0, deadline - monotonic())))

    async def _command(
        self, state: _Connection, request: wire.CommandRequest
    ) -> wire.ServerFrame:
        if not _valid_command_id(request.command_id):
            return _error(request.request_id, "COMMAND_ID_INVALID")
        if request.client_submission_id not in ("", request.command_id):
            return _error(request.request_id, "COMMAND_SUBMISSION_ID_MISMATCH")
        if request.command_kind not in (
            wire.ACCEPT_SUBAGENT_COMPLETION,
            wire.CANCEL_SUBAGENT_TASK,
        ) and (
            request.subagent_task_id
        ):
            return _error(request.request_id, "COMMAND_SOURCE_UNION_INVALID")
        if request.command_kind != wire.DETACH and (
            not self._has_controller_capability(state)
        ):
            return _error(request.request_id, "CONTROLLER_REQUIRED")
        if request.force and request.command_kind != wire.COMPACT_CONTEXT:
            return _error(request.request_id, "COMMAND_FORCE_FIELD_NOT_ALLOWED")
        queue_action = request.command_kind in (
            wire.CANCEL_QUEUED_PROMPT, wire.STEER_QUEUED_PROMPT,
        )
        if queue_action:
            if (not request.target_queue_item_id or request.plan_reason
                or request.HasField("prompt_content")
                or bool(request.target_turn_id) != (request.command_kind == wire.STEER_QUEUED_PROMPT)):
                return _error(request.request_id, "QUEUE_ACTION_INVALID")
        elif request.target_queue_item_id:
            return _error(request.request_id, "QUEUE_TARGET_FIELD_NOT_ALLOWED")
        requested_permission = _permission_from_wire(request.requested_permission_mode)
        permission_command = request.command_kind in (
            wire.SUBMIT_PROMPT,
            wire.ACCEPT_SUBAGENT_COMPLETION,
            wire.ENTER_PLAN,
        )
        if (
            not permission_command
            and request.requested_permission_mode != wire.PERMISSION_MODE_UNSPECIFIED
        ):
            return _error(request.request_id, "PERMISSION_FIELD_NOT_ALLOWED")
        plan_command = request.command_kind in (
            wire.ENTER_PLAN,
            wire.CANCEL_PLAN,
            wire.FORCE_EXIT_PLAN,
        )
        if not plan_command and (
            request.target_plan_workflow_id or request.expected_plan_workflow_revision
        ):
            return _error(request.request_id, "PLAN_COMMAND_FIELDS_NOT_ALLOWED")
        control_command = request.command_kind in (
            wire.STOP_ACTIVE_TURN,
            wire.CANCEL_SUBAGENT_TASK,
            wire.TERMINATE_BACKGROUND_PROCESS,
        )
        if control_command:
            if (
                not request.expected_session_id
                or not request.expected_host_session_id
                or request.client_submission_id
                or request.requested_permission_mode
                != wire.PERMISSION_MODE_UNSPECIFIED
                or request.target_plan_workflow_id
                or request.expected_plan_workflow_revision
                or request.force
                or request.plan_reason
                or request.HasField("prompt_content")
            ):
                return _error(request.request_id, "CONTROL_REQUEST_INVALID")
        elif (
            request.expected_session_id
            or request.expected_host_session_id
            or request.target_process_id
        ):
            return _error(request.request_id, "CONTROL_FIELDS_NOT_ALLOWED")
        if request.command_kind == wire.SUBMIT_PROMPT:
            if (
                request.plan_reason
                or not request.HasField("prompt_content")
                or request.target_turn_id
            ):
                return _error(request.request_id, "PROMPT_INVALID")
            if requested_permission is None:
                return _error(request.request_id, "PERMISSION_MODE_REQUIRED")
            try:
                content = _prompt_content_from_wire(request.prompt_content)
            except (TypeError, ValueError):
                return _error(request.request_id, "PROMPT_INVALID")
            outcome = await state.host_session.submit_prompt(
                command_id=request.command_id,
                content=content,
                requested_permission_mode=requested_permission,
            )
        elif request.command_kind == wire.CANCEL_QUEUED_PROMPT:
            outcome = await state.host_session.cancel_queued_prompt(
                command_id=request.command_id,
                source_queue_item_id=request.target_queue_item_id,
            )
        elif request.command_kind == wire.STEER_QUEUED_PROMPT:
            outcome = await state.host_session.steer_queued_prompt(
                command_id=request.command_id,
                source_queue_item_id=request.target_queue_item_id,
                target_turn_id=request.target_turn_id,
            )
        elif request.command_kind == wire.STOP_ACTIVE_TURN:
            if (
                not request.target_turn_id
                or request.subagent_task_id
                or request.target_process_id
            ):
                return _error(request.request_id, "STOP_REQUEST_INVALID")
            outcome = await state.host_session.request_stop_turn(
                command_id=request.command_id,
                expected_session_id=request.expected_session_id,
                expected_host_session_id=request.expected_host_session_id,
                target_turn_id=request.target_turn_id,
            )
        elif request.command_kind == wire.CANCEL_SUBAGENT_TASK:
            if (
                request.target_turn_id
                or not request.subagent_task_id
                or request.target_process_id
            ):
                return _error(request.request_id, "SUBAGENT_CANCEL_REQUEST_INVALID")
            outcome = await state.host_session.request_cancel_subagent(
                command_id=request.command_id,
                expected_session_id=request.expected_session_id,
                expected_host_session_id=request.expected_host_session_id,
                task_id=request.subagent_task_id,
            )
        elif request.command_kind == wire.TERMINATE_BACKGROUND_PROCESS:
            if (
                request.target_turn_id
                or request.subagent_task_id
                or not request.target_process_id
            ):
                return _error(request.request_id, "PROCESS_CONTROL_REQUEST_INVALID")
            outcome = await state.host_session.request_terminate_background_process(
                command_id=request.command_id,
                expected_session_id=request.expected_session_id,
                expected_host_session_id=request.expected_host_session_id,
                process_id=request.target_process_id,
            )
        elif request.command_kind == wire.ACCEPT_SUBAGENT_COMPLETION:
            new_root = not request.target_turn_id
            if (
                request.plan_reason
                or request.HasField("prompt_content")
                or not request.subagent_task_id
                or (new_root and requested_permission is None)
                or (not new_root and requested_permission is not None)
            ):
                return _error(request.request_id, "SUBAGENT_COMPLETION_REQUEST_INVALID")
            outcome = await state.host_session.accept_subagent_completion(
                command_id=request.command_id,
                target_turn_id=request.target_turn_id or None,
                requested_permission_mode=requested_permission,
                task_id=request.subagent_task_id,
                actor_id=state.attachment_id,
            )
        elif request.command_kind == wire.ENTER_PLAN:
            if (
                requested_permission is None
                or not _valid_prompt(request.plan_reason)
                or request.HasField("prompt_content")
                or request.target_turn_id
                or request.target_plan_workflow_id
                or request.expected_plan_workflow_revision
            ):
                return _error(request.request_id, "PLAN_ENTER_REQUEST_INVALID")
            try:
                outcome = await state.host_session.enter_plan(
                    command_id=request.command_id,
                    entry_reason=request.plan_reason,
                    resume_permission_mode=requested_permission,
                )
            except (ConversationKernelConflict, ValueError):
                return _error(request.request_id, "PLAN_ENTER_CONFLICT")
        elif request.command_kind in (wire.CANCEL_PLAN, wire.FORCE_EXIT_PLAN):
            if (
                request.plan_reason
                or request.HasField("prompt_content")
                or request.target_turn_id
                or not request.target_plan_workflow_id
                or request.expected_plan_workflow_revision < 1
            ):
                return _error(request.request_id, "PLAN_EXIT_REQUEST_INVALID")
            method = (
                state.host_session.cancel_plan
                if request.command_kind == wire.CANCEL_PLAN
                else state.host_session.force_exit_plan
            )
            try:
                outcome = await method(
                    command_id=request.command_id,
                    workflow_id=request.target_plan_workflow_id,
                    expected_workflow_revision=(
                        request.expected_plan_workflow_revision
                    ),
                )
            except (ConversationKernelConflict, ValueError):
                return _error(request.request_id, "PLAN_EXIT_CONFLICT")
        elif request.command_kind == wire.COMPACT_CONTEXT:
            if (
                request.plan_reason
                or request.HasField("prompt_content")
                or request.subagent_task_id
                or request.target_plan_workflow_id
                or request.expected_plan_workflow_revision
            ):
                return _error(request.request_id, "COMPACTION_REQUEST_INVALID")
            value = await state.host_session.compact_context(
                command_id=request.command_id,
                force=request.force,
                expected_active_turn_id=request.target_turn_id or None,
            )
            from pulsara_agent.conversation_kernel.host import KernelCommandOutcome

            outcome = KernelCommandOutcome(
                command_id=request.command_id,
                status=(
                    "SUCCEEDED"
                    if value.disposition.value in {"COMPACTED", "NOT_NEEDED"}
                    else "PENDING"
                    if value.disposition.value == "DEFERRED_TO_SAFE_POINT"
                    else "REJECTED"
                ),
                target_id=value.target_turn_id,
                public_code=value.disposition.value,
                public_message=value.public_code,
            )
        elif request.command_kind == wire.DETACH:
            if (
                request.plan_reason
                or request.HasField("prompt_content")
                or request.target_turn_id
            ):
                return _error(request.request_id, "DETACH_REQUEST_INVALID")
            from pulsara_agent.conversation_kernel.host import KernelCommandOutcome

            await state.host_session.controller_detached(state.attachment_id)
            outcome = KernelCommandOutcome(
                request.command_id, "SUCCEEDED", "", "DETACHED", "Client detached."
            )
        elif request.command_kind == wire.CLOSE_SESSION:
            if (
                request.plan_reason
                or request.HasField("prompt_content")
                or request.target_turn_id
            ):
                return _error(request.request_id, "CLOSE_REQUEST_INVALID")
            from pulsara_agent.conversation_kernel.host import KernelCommandOutcome

            outcome = KernelCommandOutcome(
                request.command_id,
                "PENDING",
                state.host_session.session_id,
                "SESSION_CLOSE_PENDING",
                "Session close is owned by the Python launcher.",
            )
        else:
            return _error(request.request_id, "COMMAND_KIND_INVALID")
        return wire.ServerFrame(
            command_outcome=_outcome_to_wire(request.request_id, outcome)
        )

    async def _resolve_interaction(
        self, state: _Connection, request: wire.ResolveInteractionRequest
    ) -> wire.ServerFrame:
        if not self._has_controller_capability(state):
            return _error(request.request_id, "CONTROLLER_REQUIRED")
        if not _valid_command_id(request.command_id) or not request.interaction_id:
            return _error(request.request_id, "INTERACTION_REQUEST_INVALID")
        decision = {
            wire.INTERACTION_ALLOW: "ALLOW",
            wire.INTERACTION_DENY: "DENY",
        }.get(request.decision)
        if decision is None:
            return _error(request.request_id, "INTERACTION_DECISION_INVALID")
        try:
            outcome = await state.host_session.resolve_tool_interaction(
                expected_writer_generation=request.expected_writer_generation,
                expected_owner_epoch=request.expected_owner_epoch,
                expected_live_revision=request.expected_live_revision,
                interaction_id=request.interaction_id,
                command_id=request.command_id,
                decision=decision,
                actor_id=state.attachment_id,
            )
        except ToolInteractionDecisionNotAccepted:
            return _error(request.request_id, "INTERACTION_NOT_ACCEPTED")
        except ToolInteractionDecisionOutcomeUnknown:
            return _error(request.request_id, "INTERACTION_OUTCOME_UNKNOWN")
        except ConversationKernelConflict:
            return _error(request.request_id, "INTERACTION_STALE")
        return wire.ServerFrame(
            command_outcome=_outcome_to_wire(request.request_id, outcome)
        )

    async def _resolve_plan_interaction(
        self, state: _Connection, request: wire.ResolvePlanInteractionRequest
    ) -> wire.ServerFrame:
        if not self._has_controller_capability(state):
            return _error(request.request_id, "CONTROLLER_REQUIRED")
        if (
            not _valid_command_id(request.command_id)
            or not request.interaction_id
            or not request.workflow_id
            or request.expected_workflow_revision < 1
            or request.attempt_expected_writer_generation < 1
        ):
            return _error(request.request_id, "PLAN_RESOLUTION_REQUEST_INVALID")
        branch = request.WhichOneof("resolution")
        try:
            if branch == "question_answer":
                answer_branch = request.question_answer.WhichOneof("answer")
                if answer_branch == "option_ordinal":
                    answer = PlanQuestionAnswer(
                        PlanQuestionAnswerKind.OPTION,
                        option_ordinal=request.question_answer.option_ordinal,
                    )
                elif answer_branch == "free_text":
                    if (
                        not request.question_answer.free_text
                        or len(request.question_answer.free_text.encode("utf-8"))
                        > 32 * 1024
                    ):
                        return _error(
                            request.request_id, "PLAN_QUESTION_ANSWER_INVALID"
                        )
                    answer = PlanQuestionAnswer(
                        PlanQuestionAnswerKind.FREE_TEXT,
                        free_text=request.question_answer.free_text,
                    )
                else:
                    return _error(request.request_id, "PLAN_QUESTION_ANSWER_INVALID")
                outcome = await state.host_session.resolve_plan_question(
                    command_id=request.command_id,
                    workflow_id=request.workflow_id,
                    expected_workflow_revision=(request.expected_workflow_revision),
                    interaction_id=request.interaction_id,
                    answer=answer,
                    write_expected_writer_generation=(
                        request.attempt_expected_writer_generation
                    ),
                )
            elif branch == "draft":
                decision = {
                    wire.PLAN_DRAFT_APPROVE: PlanDraftDecision.APPROVE,
                    wire.PLAN_DRAFT_REVISE: PlanDraftDecision.REVISE,
                    wire.PLAN_DRAFT_CANCEL: PlanDraftDecision.CANCEL,
                }.get(request.draft.decision)
                if decision is None:
                    return _error(request.request_id, "PLAN_DRAFT_DECISION_INVALID")
                feedback = (
                    request.draft.feedback
                    if request.draft.HasField("feedback")
                    else None
                )
                if decision in {
                    PlanDraftDecision.APPROVE,
                    PlanDraftDecision.CANCEL,
                } and request.draft.HasField("feedback"):
                    return _error(request.request_id, "PLAN_DRAFT_FEEDBACK_NOT_ALLOWED")
                if feedback is not None and len(feedback.encode("utf-8")) > 32 * 1024:
                    return _error(request.request_id, "PLAN_DRAFT_FEEDBACK_INVALID")
                # Missing and present-empty REVISE feedback have one semantic
                # candidate.  APPROVE/CANCEL presence is rejected by Host.
                if decision is PlanDraftDecision.REVISE and feedback == "":
                    feedback = None
                outcome = await state.host_session.resolve_plan_draft_review(
                    command_id=request.command_id,
                    workflow_id=request.workflow_id,
                    expected_workflow_revision=(request.expected_workflow_revision),
                    interaction_id=request.interaction_id,
                    decision=decision,
                    feedback=feedback,
                    write_expected_writer_generation=(
                        request.attempt_expected_writer_generation
                    ),
                )
            else:
                return _error(request.request_id, "PLAN_RESOLUTION_KIND_INVALID")
        except (ConversationKernelConflict, ValueError, KeyError):
            return _error(request.request_id, "PLAN_RESOLUTION_CONFLICT")
        return wire.ServerFrame(
            resolve_plan_interaction=wire.ResolvePlanInteractionResponse(
                request_id=request.request_id,
                command_id=outcome.command_id,
                workflow_id=outcome.workflow_id,
                workflow_status=outcome.workflow_status.value,
                interaction_id=outcome.interaction_id,
                interaction_status=outcome.interaction_status,
                resume_permission_mode=_permission_to_wire(
                    outcome.resume_permission_mode
                ),
                continuation_turn_id=outcome.continuation_turn_id or "",
                handoff_created_at_commit=outcome.handoff_created_at_commit,
                draft_decision={
                    None: wire.PLAN_DRAFT_DECISION_UNSPECIFIED,
                    PlanDraftDecision.APPROVE: wire.PLAN_DRAFT_APPROVE,
                    PlanDraftDecision.REVISE: wire.PLAN_DRAFT_REVISE,
                    PlanDraftDecision.CANCEL: wire.PLAN_DRAFT_CANCEL,
                }[outcome.draft_decision],
                workflow_revision=outcome.workflow_revision,
            )
        )

    async def _read_plan_question(
        self, state: _Connection, request: wire.ReadPlanQuestionContentRequest
    ) -> wire.ServerFrame:
        if not self._has_controller_capability(state):
            return _error(request.request_id, "CONTROLLER_REQUIRED")
        try:
            question = await asyncio.to_thread(
                state.host_session.repository.read_plan_question_content,
                session_id=state.host_session.session_id,
                interaction_id=request.interaction_id,
                deadline_monotonic=monotonic() + 10.0,
            )
        except (ConversationKernelConflict, ValueError, KeyError):
            return _error(request.request_id, "PLAN_CONTENT_INVALID")
        return wire.ServerFrame(
            plan_question=wire.PlanQuestionContent(
                request_id=request.request_id,
                interaction_id=question.interaction_id,
                question=question.question,
                options=(
                    wire.PlanQuestionOption(
                        ordinal=item.ordinal,
                        label=item.label,
                        description=item.description,
                        recommended=item.recommended,
                    )
                    for item in question.options
                ),
                allow_free_text=question.allow_free_text,
                typed_content_fingerprint=question.typed_content_fingerprint,
            )
        )

    async def _read_plan_draft(
        self, state: _Connection, request: wire.ReadPlanDraftTextChunkRequest
    ) -> wire.ServerFrame:
        if not self._has_controller_capability(state):
            return _error(request.request_id, "CONTROLLER_REQUIRED")
        try:
            chunk = await asyncio.to_thread(
                state.host_session.repository.read_plan_draft_text_chunk,
                session_id=state.host_session.session_id,
                interaction_id=request.interaction_id,
                offset_utf8_bytes=request.offset_utf8_bytes,
                limit_bytes=request.limit_bytes,
                expected_plan_utf8_digest=(
                    request.expected_plan_utf8_digest
                    if request.HasField("expected_plan_utf8_digest")
                    else None
                ),
                deadline_monotonic=monotonic() + 10.0,
            )
        except PlanDraftIdentityConflict:
            return _error(request.request_id, "PLAN_DRAFT_IDENTITY_CONFLICT")
        except (ConversationKernelConflict, ValueError, KeyError):
            return _error(request.request_id, "PLAN_CONTENT_INVALID")
        identity = chunk.identity
        return wire.ServerFrame(
            plan_draft=wire.PlanDraftTextChunk(
                request_id=request.request_id,
                interaction_id=identity.interaction_id,
                assistant_entry_id=identity.assistant_entry_id,
                tool_call_id=identity.tool_call_id,
                request_semantic_digest=identity.request_semantic_digest,
                plan_utf8_size=identity.plan_utf8_size,
                plan_utf8_digest=identity.plan_utf8_digest,
                offset_utf8_bytes=chunk.offset_utf8_bytes,
                body=chunk.body,
                next_offset_utf8_bytes=chunk.next_offset_utf8_bytes,
                eof=chunk.eof,
            )
        )

    async def _query_command(
        self, state: _Connection, request: wire.QueryCommandRequest
    ) -> wire.ServerFrame:
        if not _valid_command_id(request.command_id):
            return _error(request.request_id, "COMMAND_ID_INVALID")
        if request.HasField("expected_control"):
            if not self._has_controller_capability(state):
                return _error(request.request_id, "CONTROLLER_REQUIRED")
            expected = _control_request_from_wire(
                request.command_id, request.expected_control
            )
            if expected is None:
                return _error(request.request_id, "CONTROL_QUERY_INVALID")
            control = await state.host_session.query_control_command(expected)
            response = wire.QueryCommandResponse(
                request_id=request.request_id,
                found=control.status is ControlQueryStatus.FOUND,
                control_query_status={
                    ControlQueryStatus.FOUND: wire.CONTROL_QUERY_FOUND,
                    ControlQueryStatus.RESULT_UNAVAILABLE: (
                        wire.CONTROL_QUERY_RESULT_UNAVAILABLE
                    ),
                    ControlQueryStatus.OWNER_UNAVAILABLE: (
                        wire.CONTROL_QUERY_OWNER_UNAVAILABLE
                    ),
                }[control.status],
            )
            if control.outcome is not None:
                response.outcome.CopyFrom(
                    _outcome_to_wire(request.request_id, control.outcome)
                )
            return wire.ServerFrame(query_command=response)
        if request.command_id.startswith("command:control:"):
            return _error(request.request_id, "CONTROL_QUERY_EXPECTATION_REQUIRED")
        outcome = await state.host_session.query_command(request.command_id)
        response = wire.QueryCommandResponse(
            request_id=request.request_id, found=outcome is not None
        )
        if outcome is not None:
            response.outcome.CopyFrom(_outcome_to_wire(request.request_id, outcome))
        return wire.ServerFrame(query_command=response)

    async def _read_content(
        self, state: _Connection, request: wire.ReadContentRequest
    ) -> wire.ServerFrame:
        if not 1 <= request.limit_bytes <= 1 << 20:
            return _error(request.request_id, "CONTENT_RANGE_INVALID")
        target = request.WhichOneof("target")
        if target is None or (target == "queue_item_id" and request.block_id) or (
            request.HasField("image_ref_ordinal") and request.block_id
        ) or (
            request.HasField("visualization_ordinal")
            and (
                target != "entry_id" or request.block_id
                or request.HasField("image_ref_ordinal")
            )
        ):
            return _error(request.request_id, "CONTENT_TARGET_INVALID")
        try:
            reference = await asyncio.to_thread(
                state.protocol_reader.resolve_content_reference,
                session_id=state.host_session.session_id,
                entry_id=request.entry_id if target == "entry_id" else None,
                queue_item_id=(
                    request.queue_item_id if target == "queue_item_id" else None
                ),
                block_id=request.block_id or None,
                image_ref_ordinal=(
                    request.image_ref_ordinal
                    if request.HasField("image_ref_ordinal")
                    else None
                ),
                visualization_ordinal=(
                    request.visualization_ordinal
                    if request.HasField("visualization_ordinal")
                    else None
                ),
                deadline_monotonic=monotonic() + 10.0,
            )
        except CanonicalQueueContentNotPending:
            return _error(request.request_id, "CONTENT_QUEUE_NOT_PENDING")
        except KeyError:
            return _error(request.request_id, "CONTENT_REFERENCE_MISSING")
        except ConversationKernelConflict:
            return _error(request.request_id, "CONTENT_REFERENCE_CORRUPT")
        except ValueError:
            return _error(request.request_id, "CONTENT_REFERENCE_CORRUPT")
        inline = reference["inline_content"]
        if inline is not None:
            value = bytes(inline)
            digest = "sha256:" + sha256(value).hexdigest()
            if (
                reference["blob_id"] is not None
                or digest != str(reference["content_digest"])
                or len(value) != int(reference["content_size"])
                or not reference["content_media_type"]
                or not reference["content_codec"]
            ):
                return _error(request.request_id, "CONTENT_REFERENCE_CORRUPT")
            if request.offset_bytes > len(value):
                return _error(request.request_id, "CONTENT_RANGE_INVALID")
            chunk = value[
                request.offset_bytes : request.offset_bytes + request.limit_bytes
            ]
            return wire.ServerFrame(
                content=wire.CanonicalContentChunk(
                    request_id=request.request_id,
                    digest=str(reference["content_digest"]),
                    complete_size=len(value),
                    offset_bytes=request.offset_bytes,
                    content=chunk,
                    complete=request.offset_bytes + len(chunk) == len(value),
                )
            )
        if not reference["blob_id"]:
            return _error(request.request_id, "CONTENT_REFERENCE_CORRUPT")
        store = PostgresCanonicalBlobStore(
            state.host_session.repository.connection_provider
        )
        try:
            value = await asyncio.to_thread(
                store.read_chunk,
                blob_id=str(reference["blob_id"]),
                expected_digest=str(reference["content_digest"]),
                expected_size=int(reference["content_size"]),
                expected_media_type=str(reference["content_media_type"]),
                expected_codec=str(reference["content_codec"]),
                offset=request.offset_bytes,
                maximum_bytes=request.limit_bytes,
                deadline_monotonic=monotonic() + 10.0,
            )
        except KeyError:
            return _error(request.request_id, "CONTENT_BLOB_MISSING")
        except ConversationKernelConflict:
            return _error(request.request_id, "CONTENT_BLOB_CORRUPT")
        except ValueError:
            return _error(request.request_id, "CONTENT_RANGE_INVALID")
        return wire.ServerFrame(
            content=wire.CanonicalContentChunk(
                request_id=request.request_id,
                digest=value.digest,
                complete_size=value.total_size,
                offset_bytes=value.offset,
                content=value.content,
                complete=not value.has_more,
            )
        )

    async def _read_tool_artifact(
        self, state: _Connection, request: wire.ReadToolArtifactRequest
    ) -> wire.ServerFrame:
        if (
            not request.result_entry_id
            or not 1 <= request.max_chars <= ARTIFACT_READ_HARD_CHARS
        ):
            return _error(request.request_id, "TOOL_ARTIFACT_RANGE_INVALID")
        try:
            reference = await asyncio.to_thread(
                state.protocol_reader.resolve_tool_artifact_reference,
                session_id=state.host_session.session_id,
                result_entry_id=request.result_entry_id,
                deadline_monotonic=monotonic() + 10.0,
            )
        except KeyError:
            return _error(request.request_id, "TOOL_RESULT_MISSING")
        disposition = str(reference["output_artifact_disposition"])
        if disposition not in {"AVAILABLE", "INCOMPLETE"}:
            return _error(request.request_id, "TOOL_ARTIFACT_UNAVAILABLE")
        artifact_id = str(reference["output_artifact_id"] or "")
        if not artifact_id:
            return _error(request.request_id, "TOOL_ARTIFACT_CORRUPT")
        port = PostgresToolArtifactReadPort(
            state.host_session.repository.connection_provider,
            session_id=state.host_session.session_id,
            workspace_id=str(reference["workspace_id"]),
        )
        try:
            page = await asyncio.to_thread(
                port.read_text,
                artifact_id,
                offset_chars=request.offset_chars,
                max_chars=request.max_chars,
            )
        except ValueError:
            return _error(request.request_id, "TOOL_ARTIFACT_RANGE_INVALID")
        except KeyError:
            return _error(request.request_id, "TOOL_ARTIFACT_MISSING")
        except (ArtifactContentError, ConversationKernelConflict):
            return _error(request.request_id, "TOOL_ARTIFACT_CORRUPT")
        return wire.ServerFrame(
            tool_artifact=wire.ToolArtifactTextChunk(
                request_id=request.request_id,
                result_entry_id=request.result_entry_id,
                artifact_disposition=page.record.artifact_disposition.value,
                source_coverage=page.record.source_coverage.value,
                display_kind=page.record.display_kind.value,
                source_coverage_reason=(
                    page.record.source_coverage_reason.value
                    if page.record.source_coverage_reason is not None
                    else ""
                ),
                artifact_unavailability_reason=(
                    page.record.artifact_unavailability_reason.value
                    if page.record.artifact_unavailability_reason is not None
                    else ""
                ),
                text=page.text,
                offset_chars=page.offset_chars,
                returned_chars=page.returned_chars,
                total_chars=page.total_chars,
                has_more=page.has_more,
                next_offset_chars=page.next_offset_chars or 0,
            )
        )

    async def _list_background_processes(
        self, state: _Connection, request: wire.ListBackgroundProcessesRequest
    ) -> wire.ServerFrame:
        if (
            request.expected_session_id != state.host_session.session_id
            or request.expected_host_session_id
            != state.host_session.host_session_id
            or not 1 <= request.maximum_items <= 50
        ):
            return _error(request.request_id, "BACKGROUND_OWNER_MISMATCH")
        try:
            after = (
                _decode_background_cursor(
                    request.cursor,
                    session_id=request.expected_session_id,
                    host_session_id=request.expected_host_session_id,
                )
                if request.cursor
                else None
            )
        except ValueError:
            return _error(request.request_id, "BACKGROUND_CURSOR_INVALID")
        processes = await asyncio.to_thread(
            state.host_session.list_background_processes,
            expected_session_id=request.expected_session_id,
            expected_host_session_id=request.expected_host_session_id,
        )
        ordered = tuple(
            sorted(processes, key=lambda item: (item.started_at_monotonic, item.process_id))
        )
        if after is not None:
            ordered = tuple(
                item
                for item in ordered
                if (item.started_at_monotonic, item.process_id) > after
            )
        page = ordered[: request.maximum_items]
        has_more = len(ordered) > len(page)
        next_cursor = (
            _encode_background_cursor(
                session_id=request.expected_session_id,
                host_session_id=request.expected_host_session_id,
                started_at=page[-1].started_at_monotonic,
                process_id=page[-1].process_id,
            )
            if page and has_more
            else ""
        )
        return wire.ServerFrame(
            background_processes=wire.BackgroundProcessPage(
                request_id=request.request_id,
                session_id=state.host_session.session_id,
                host_session_id=state.host_session.host_session_id,
                processes=tuple(_background_process_to_wire(item) for item in page),
                next_cursor=next_cursor,
            )
        )

    async def _read_background_process_log(
        self, state: _Connection, request: wire.ReadBackgroundProcessLogRequest
    ) -> wire.ServerFrame:
        if (
            request.expected_session_id != state.host_session.session_id
            or request.expected_host_session_id
            != state.host_session.host_session_id
        ):
            return _error(request.request_id, "BACKGROUND_OWNER_MISMATCH")
        if (
            not request.process_id
            or not 512 <= request.max_output_chars <= 32_000
        ):
            return _error(request.request_id, "BACKGROUND_LOG_RANGE_INVALID")
        try:
            result = await asyncio.to_thread(
                state.host_session.read_background_process_log,
                expected_session_id=request.expected_session_id,
                expected_host_session_id=request.expected_host_session_id,
                process_id=request.process_id,
                maximum_chars=request.max_output_chars,
                since_cursor=request.output_cursor or None,
            )
        except KeyError:
            return _error(request.request_id, "BACKGROUND_PROCESS_UNAVAILABLE")
        except ValueError:
            return _error(request.request_id, "BACKGROUND_CURSOR_INVALID")
        return wire.ServerFrame(
            background_process_log=wire.BackgroundProcessLog(
                request_id=request.request_id,
                session_id=state.host_session.session_id,
                host_session_id=state.host_session.host_session_id,
                process=_background_process_to_wire(result.process),
                output=result.output,
                truncated=result.truncated,
                output_disposition=result.output_disposition.value,
                output_cursor=result.output_cursor,
                retained_from_cursor=result.retained_from_cursor,
                gap_before_output=result.gap_before_output,
                truncated_by_response_bound=result.truncated_by_response_bound,
                source_coverage=result.source_coverage.value,
            )
        )

    @staticmethod
    def _attachment_matches(state: _Connection, request: object) -> bool:
        return (
            getattr(request, "attachment_id", None) == state.attachment_id
            and getattr(request, "attachment_generation", None)
            == state.attachment_generation
        )

    @staticmethod
    def _has_controller_capability(state: _Connection) -> bool:
        """Join every controller request with the current Host attachment."""

        return (
            state.granted_role == wire.ATTACHMENT_ROLE_CONTROLLER
            and state.host_session is not None
            and bool(state.attachment_id)
            and state.host_session.has_controller_attachment(state.attachment_id)
        )

    async def _read_frame(self, reader: asyncio.StreamReader) -> wire.ClientFrame:
        header = await reader.readexactly(4)
        size = int.from_bytes(header, "big")
        if not 1 <= size <= self._maximum_frame_bytes:
            raise ValueError("Protocol v3 input frame is out of bounds")
        payload = await reader.readexactly(size)
        frame = wire.ClientFrame()
        try:
            frame.ParseFromString(payload)
        except DecodeError as exc:
            raise ValueError("Protocol v3 frame is malformed") from exc
        if frame.WhichOneof("request") is None:
            raise ValueError("Protocol v3 request union is empty")
        return frame

    async def _write_frame(
        self, writer: asyncio.StreamWriter, frame: wire.ServerFrame
    ) -> None:
        payload = frame.SerializeToString(deterministic=True)
        if not 1 <= len(payload) <= self._maximum_frame_bytes:
            raise ValueError("Protocol v3 output frame is out of bounds")
        writer.write(len(payload).to_bytes(4, "big") + payload)
        await writer.drain()

    def _fits_frame(self, frame: wire.ServerFrame) -> bool:
        return (
            len(frame.SerializeToString(deterministic=True))
            <= self._maximum_frame_bytes
        )


def _live_to_wire(owner_epoch: int, event: object) -> wire.LiveEventProjection:
    assert event.event_type in LiveEventType
    return wire.LiveEventProjection(
        owner_epoch=owner_epoch,
        live_revision=event.revision,
        event_type=getattr(wire, _snake(event.event_type.value)),
        session_id=event.session_id,
        turn_id=event.turn_id,
        draft_identity=event.draft_identity,
        payload=_live_payload_to_wire(event.payload),
        scope_kind=wire.ROOT if event.scope_kind == "ROOT" else wire.SUBAGENT_TASK,
        scope_subagent_task_id=event.scope_subagent_task_id or "",
        channel_kind=getattr(wire, f"LIVE_CHANNEL_{event.channel_kind.value}"),
        channel_tool_call_id=event.channel_tool_call_id or "",
        channel_attempt_id=event.channel_attempt_id or "",
        generation_id=event.generation_id,
        proposed_entry_id=event.proposed_entry_id or "",
        block_id=event.block_id,
        block_ordinal=event.block_ordinal,
        block_kind=getattr(wire, f"LIVE_BLOCK_{event.block_kind.value}"),
    )


def _live_payload_to_wire(payload: object) -> wire.LiveEventPayload:
    if isinstance(payload, TextStartPayload):
        return wire.LiveEventPayload(
            text_start=wire.LiveTextStartPayload(block_identity=payload.block_identity)
        )
    if isinstance(payload, TextDeltaPayload):
        return wire.LiveEventPayload(
            text_delta=wire.LiveTextDeltaPayload(
                block_identity=payload.block_identity, delta=payload.delta
            )
        )
    if isinstance(payload, TextEndPayload):
        return wire.LiveEventPayload(
            text_end=wire.LiveTextEndPayload(
                block_identity=payload.block_identity,
                final_text=payload.final_text,
                utf8_bytes=payload.utf8_bytes,
                digest=payload.digest,
            )
        )
    if isinstance(payload, ThinkingStartPayload):
        return wire.LiveEventPayload(
            thinking_start=wire.LiveThinkingStartPayload(
                block_identity=payload.block_identity,
                presentation_kind=(
                    wire.REASONING_PRESENTATION_SUMMARY
                    if payload.presentation_kind is ReasoningPresentationKind.SUMMARY
                    else wire.REASONING_PRESENTATION_FULL
                ),
            )
        )
    if isinstance(payload, ThinkingDeltaPayload):
        return wire.LiveEventPayload(
            thinking_delta=wire.LiveThinkingDeltaPayload(
                block_identity=payload.block_identity, delta=payload.delta
            )
        )
    if isinstance(payload, ThinkingEndPayload):
        return wire.LiveEventPayload(
            thinking_end=wire.LiveThinkingEndPayload(
                block_identity=payload.block_identity,
                final_text=payload.final_text,
                utf8_bytes=payload.utf8_bytes,
                digest=payload.digest,
            )
        )
    if isinstance(payload, DataStartPayload):
        return wire.LiveEventPayload(
            data_start=wire.LiveDataStartPayload(
                block_identity=payload.block_identity, media_type=payload.media_type
            )
        )
    if isinstance(payload, DataDeltaPayload):
        return wire.LiveEventPayload(
            data_delta=wire.LiveDataDeltaPayload(
                block_identity=payload.block_identity, data=payload.data
            )
        )
    if isinstance(payload, DataEndPayload):
        return wire.LiveEventPayload(
            data_end=wire.LiveDataEndPayload(
                block_identity=payload.block_identity,
                media_type=payload.media_type,
                final_data=payload.final_data,
                utf8_bytes=payload.utf8_bytes,
                digest=payload.digest,
            )
        )
    if isinstance(payload, ToolCallStartPayload):
        return wire.LiveEventPayload(
            tool_call_start=wire.LiveToolCallStartPayload(
                block_identity=payload.block_identity,
                tool_call_id=payload.tool_call_id,
                tool_name=payload.tool_name,
            )
        )
    if isinstance(payload, ToolCallDeltaPayload):
        return wire.LiveEventPayload(
            tool_call_delta=wire.LiveToolCallDeltaPayload(
                block_identity=payload.block_identity,
                tool_call_id=payload.tool_call_id,
                delta=payload.delta,
            )
        )
    if isinstance(payload, ToolCallEndPayload):
        return wire.LiveEventPayload(
            tool_call_end=wire.LiveToolCallEndPayload(
                block_identity=payload.block_identity,
                tool_call_id=payload.tool_call_id,
                tool_name=payload.tool_name,
                arguments_json=payload.arguments_json,
                utf8_bytes=payload.utf8_bytes,
                digest=payload.digest,
            )
        )
    if isinstance(payload, ToolResultStartPayload):
        return wire.LiveEventPayload(
            tool_result_start=wire.LiveToolResultStartPayload(
                block_identity=payload.block_identity,
                tool_call_id=payload.tool_call_id,
                attempt_id=payload.attempt_id,
            )
        )
    if isinstance(payload, ToolResultDeltaPayload):
        return wire.LiveEventPayload(
            tool_result_delta=wire.LiveToolResultDeltaPayload(
                block_identity=payload.block_identity, text=payload.text
            )
        )
    if isinstance(payload, ToolResultEndPayload):
        return wire.LiveEventPayload(
            tool_result_end=wire.LiveToolResultEndPayload(
                block_identity=payload.block_identity,
                result_state=payload.result_state,
                final_text=payload.final_text,
                utf8_bytes=payload.utf8_bytes,
                digest=payload.digest,
            )
        )
    if isinstance(payload, InteractionOpenedPayload):
        return wire.LiveEventPayload(
            interaction_opened=wire.LiveInteractionOpenedPayload(
                interaction_id=payload.interaction_id,
                interaction_kind=payload.interaction_kind,
                public_prompt=payload.public_prompt,
                public_options=payload.public_options,
                expires_at_utc=payload.expires_at_utc,
            )
        )
    if isinstance(payload, InteractionReplacedPayload):
        return wire.LiveEventPayload(
            interaction_replaced=wire.LiveInteractionReplacedPayload(
                replaced_interaction_id=payload.replaced_interaction_id,
                interaction_id=payload.interaction_id,
                interaction_kind=payload.interaction_kind,
                public_prompt=payload.public_prompt,
                public_options=payload.public_options,
                expires_at_utc=payload.expires_at_utc,
            )
        )
    if isinstance(payload, InteractionClosedPayload):
        return wire.LiveEventPayload(
            interaction_closed=wire.LiveInteractionClosedPayload(
                interaction_id=payload.interaction_id, reason=payload.reason
            )
        )
    if isinstance(payload, TerminalProcessCompletedPayload):
        value = wire.LiveTerminalProcessCompletedPayload(
            process_id=payload.process_id,
            status=payload.status,
            output_utf8_bytes=payload.output_utf8_bytes,
            output_digest=payload.output_digest,
        )
        if payload.exit_code is not None:
            value.exit_code = payload.exit_code
        return wire.LiveEventPayload(terminal_process_completed=value)
    if isinstance(payload, TerminalMonitorOpenedPayload):
        return wire.LiveEventPayload(
            terminal_monitor_opened=wire.LiveTerminalMonitorOpenedPayload(
                monitor_id=payload.monitor_id, process_id=payload.process_id
            )
        )
    if isinstance(payload, TerminalMonitorObservationPayload):
        return wire.LiveEventPayload(
            terminal_monitor_observation=wire.LiveTerminalMonitorObservationPayload(
                monitor_id=payload.monitor_id,
                process_id=payload.process_id,
                observation_kind=payload.observation_kind,
                public_preview=payload.public_preview,
                complete_utf8_bytes=payload.complete_utf8_bytes,
                complete_digest=payload.complete_digest,
            )
        )
    if isinstance(payload, TerminalMonitorClosedPayload):
        return wire.LiveEventPayload(
            terminal_monitor_closed=wire.LiveTerminalMonitorClosedPayload(
                monitor_id=payload.monitor_id,
                process_id=payload.process_id,
                reason=payload.reason,
            )
        )
    if isinstance(payload, SubagentProgressPayload):
        return wire.LiveEventPayload(
            subagent_progress=wire.LiveSubagentProgressPayload(
                task_id=payload.task_id,
                status=payload.status,
                public_summary=payload.public_summary,
                summary_utf8_bytes=payload.summary_utf8_bytes,
                summary_digest=payload.summary_digest,
            )
        )
    if isinstance(payload, TodoSnapshotUpdatedPayload):
        return wire.LiveEventPayload(
            todo_snapshot_updated=wire.LiveTodoSnapshotUpdatedPayload(
                todo_run_id=payload.todo_run_id,
                todo_revision=payload.todo_revision,
                disposition=payload.disposition,
                ordered_items=tuple(
                    wire.LiveTodoItemProjection(
                        ordinal=item.ordinal,
                        text=item.text,
                        status=item.status,
                    )
                    for item in payload.ordered_items
                ),
                pending_count=payload.pending_count,
                in_progress_count=payload.in_progress_count,
                completed_count=payload.completed_count,
            )
        )
    raise TypeError("live payload vocabulary is not closed")


def _live_snapshot_to_wire(snapshot: object) -> wire.LiveSnapshotProjection:
    return wire.LiveSnapshotProjection(
        owner_epoch=snapshot.generation,
        retained_from_revision=snapshot.retained_from_revision,
        through_revision=snapshot.through_revision,
        events=tuple(
            _live_to_wire(snapshot.generation, event) for event in snapshot.events
        ),
        settlements=tuple(
            _settlement_to_wire(snapshot.generation, item)
            for item in snapshot.settlements
        ),
        truncated_before=snapshot.truncated_before,
    )


def _settlement_to_wire(
    owner_epoch: int, settlement: object
) -> wire.LiveGenerationSettlement:
    kind = (
        wire.LIVE_GENERATION_COMMITTED
        if settlement.kind is LiveSettlementKind.COMMITTED
        else wire.LIVE_GENERATION_ABORTED
    )
    return wire.LiveGenerationSettlement(
        owner_epoch=owner_epoch,
        live_revision=settlement.revision,
        kind=kind,
        session_id=settlement.session_id,
        turn_id=settlement.turn_id,
        draft_identity=settlement.draft_identity,
        committed_entry_id=settlement.committed_entry_id or "",
        reason_code=settlement.reason_code or "",
        scope_kind=(
            wire.ROOT if settlement.scope_kind == "ROOT" else wire.SUBAGENT_TASK
        ),
        scope_subagent_task_id=settlement.scope_subagent_task_id or "",
        channel_kind=getattr(wire, f"LIVE_CHANNEL_{settlement.channel_kind.value}"),
        channel_tool_call_id=settlement.channel_tool_call_id or "",
        channel_attempt_id=settlement.channel_attempt_id or "",
        generation_id=settlement.generation_id,
        proposed_entry_id=settlement.proposed_entry_id or "",
    )


def _interaction_to_wire(value: CurrentInteractionView) -> wire.LiveInteractionView:
    return wire.LiveInteractionView(
        interaction_id=value.interaction_id,
        interaction_kind=value.interaction_kind,
        public_prompt=value.public_prompt,
        public_options=value.public_options,
        expires_at_utc=value.expires_at_utc,
        decision_in_progress=value.decision_in_progress,
    )


def _live_control_snapshot_to_wire(
    snapshot: object,
    todo_snapshots: tuple[object, ...],
    compaction_projection: tuple[bool, str | None, str | None, str | None] = (
        False,
        None,
        None,
        None,
    ),
    *,
    host_session_id: str = "",
    active_root_turn_id: str | None = None,
    control_admission_deadline_ms: int = 0,
) -> wire.SessionLiveControlSnapshot:
    in_progress, trigger, phase, scope = compaction_projection
    result = wire.SessionLiveControlSnapshot(
        session_id=snapshot.session_id,
        owner_epoch=snapshot.owner_epoch,
        live_revision=snapshot.revision,
        current_todos=tuple(
            wire.LiveTodoRunSnapshot(
                todo_run_id=item.run_identity.todo_run_id,
                todo_revision=item.revision,
                scope_kind=(
                    wire.ROOT
                    if item.run_identity.scope_kind.value == "ROOT"
                    else wire.SUBAGENT_TASK
                ),
                scope_subagent_task_id=item.run_identity.subagent_task_id or "",
                disposition="ACTIVE" if item.ordered_items else "CLEARED",
                ordered_items=tuple(
                    wire.LiveTodoItemProjection(
                        ordinal=todo.ordinal,
                        text=todo.text,
                        status=todo.status.value,
                    )
                    for todo in item.ordered_items
                ),
                pending_count=item.pending_count,
                in_progress_count=item.in_progress_count,
                completed_count=item.completed_count,
            )
            for item in todo_snapshots
        ),
        compaction_in_progress=in_progress,
        compaction_trigger=trigger or "",
        compaction_phase=phase or "",
        compaction_target_scope=scope or "",
        input_admission_deferred=in_progress,
        host_session_id=host_session_id,
        active_root_turn_id=active_root_turn_id or "",
        control_admission_deadline_ms=control_admission_deadline_ms,
    )
    if snapshot.current_interaction is not None:
        result.current_interaction.CopyFrom(
            _interaction_to_wire(snapshot.current_interaction)
        )
    return result


def _live_control_event_to_wire(event: object) -> wire.LiveControlEventProjection:
    kind = {
        LiveControlEventKind.INTERACTION_OPENED: wire.LIVE_INTERACTION_OPENED,
        LiveControlEventKind.INTERACTION_REPLACED: wire.LIVE_INTERACTION_REPLACED,
        LiveControlEventKind.INTERACTION_CLOSED: wire.LIVE_INTERACTION_CLOSED,
    }[event.kind]
    result = wire.LiveControlEventProjection(
        owner_epoch=event.owner_epoch,
        live_revision=event.revision,
        kind=kind,
        closed_interaction_id=event.closed_interaction_id or "",
    )
    if event.interaction is not None:
        result.interaction.CopyFrom(_interaction_to_wire(event.interaction))
    return result


def _snake(value: str) -> str:
    result: list[str] = []
    for character in value:
        if character.isupper() and result:
            result.append("_")
        result.append(character.upper())
    return "".join(result)


def _outcome_to_wire(request_id: str, outcome: object) -> wire.CommandOutcome:
    status = {
        "SUCCEEDED": wire.SUCCEEDED,
        "REJECTED": wire.REJECTED,
        "PENDING": wire.PENDING,
        "FAILED": wire.FAILED,
    }[outcome.status]
    result = wire.CommandOutcome(
        request_id=request_id,
        command_id=outcome.command_id,
        status=status,
        target_id=outcome.target_id,
        public_code=outcome.public_code,
        public_message=outcome.public_message,
        plan_workflow_status=outcome.plan_workflow_status or "",
        resume_permission_mode=(
            wire.PERMISSION_MODE_UNSPECIFIED
            if outcome.resume_permission_mode is None
            else _permission_to_wire(outcome.resume_permission_mode)
        ),
        handoff_created_at_commit=outcome.handoff_created_at_commit,
        plan_workflow_revision=outcome.plan_workflow_revision or 0,
        plan_draft_decision={
            None: wire.PLAN_DRAFT_DECISION_UNSPECIFIED,
            PlanDraftDecision.APPROVE: wire.PLAN_DRAFT_APPROVE,
            PlanDraftDecision.REVISE: wire.PLAN_DRAFT_REVISE,
            PlanDraftDecision.CANCEL: wire.PLAN_DRAFT_CANCEL,
        }[outcome.plan_draft_decision],
        plan_continuation_turn_id=outcome.plan_continuation_turn_id or "",
    )
    if outcome.prompt_delivery is not None:
        result.prompt_delivery.CopyFrom(wire.PromptDelivery(
            queue_item_id=outcome.prompt_delivery.queue_item_id,
            queue_status=outcome.prompt_delivery.queue_status,
            consumed_entry_id=outcome.prompt_delivery.consumed_entry_id or "",
            delivery_mode=outcome.prompt_delivery.delivery_mode,
        ))
    if outcome.user_control is not None:
        result.user_control.CopyFrom(_user_control_outcome_to_wire(outcome.user_control))
    return result


def _control_request_from_wire(
    command_id: str, expected: wire.ExpectedUserControl
) -> UserControlRequest | None:
    operation = {
        wire.USER_CONTROL_STOP_ACTIVE_TURN: UserControlOperation.STOP_ACTIVE_TURN,
        wire.USER_CONTROL_CANCEL_SUBAGENT_TASK: (
            UserControlOperation.CANCEL_SUBAGENT_TASK
        ),
        wire.USER_CONTROL_TERMINATE_BACKGROUND_PROCESS: (
            UserControlOperation.TERMINATE_BACKGROUND_PROCESS
        ),
    }.get(expected.operation)
    kind = {
        wire.USER_CONTROL_ROOT_TURN: UserControlTargetKind.ROOT_TURN,
        wire.USER_CONTROL_SUBAGENT_TASK: UserControlTargetKind.SUBAGENT_TASK,
        wire.USER_CONTROL_BACKGROUND_PROCESS: UserControlTargetKind.BACKGROUND_PROCESS,
    }.get(expected.target.kind)
    expected_kind = {
        UserControlOperation.STOP_ACTIVE_TURN: UserControlTargetKind.ROOT_TURN,
        UserControlOperation.CANCEL_SUBAGENT_TASK: UserControlTargetKind.SUBAGENT_TASK,
        UserControlOperation.TERMINATE_BACKGROUND_PROCESS: (
            UserControlTargetKind.BACKGROUND_PROCESS
        ),
    }.get(operation)
    if (
        operation is None
        or kind is None
        or kind is not expected_kind
        or not expected.session_id
        or not expected.host_session_id
        or not expected.target.target_id
    ):
        return None
    return UserControlRequest(
        operation,
        command_id,
        expected.session_id,
        expected.host_session_id,
        UserControlTarget(kind, expected.target.target_id),
    )


def _user_control_outcome_to_wire(value: object) -> wire.UserControlOutcome:
    result = wire.UserControlOutcome(
        operation={
            "STOP_ACTIVE_TURN": wire.USER_CONTROL_STOP_ACTIVE_TURN,
            "CANCEL_SUBAGENT_TASK": wire.USER_CONTROL_CANCEL_SUBAGENT_TASK,
            "TERMINATE_BACKGROUND_PROCESS": (
                wire.USER_CONTROL_TERMINATE_BACKGROUND_PROCESS
            ),
        }[value.operation.value],
        session_id=value.session_id,
        host_session_id=value.host_session_id,
        target=wire.UserControlTarget(
            kind={
                "ROOT_TURN": wire.USER_CONTROL_ROOT_TURN,
                "SUBAGENT_TASK": wire.USER_CONTROL_SUBAGENT_TASK,
                "BACKGROUND_PROCESS": wire.USER_CONTROL_BACKGROUND_PROCESS,
            }[value.target.kind.value],
            target_id=value.target.target_id,
        ),
        accepted=value.accepted,
        execution={
            "NOT_STARTED": wire.USER_CONTROL_NOT_STARTED,
            "RUNNING": wire.USER_CONTROL_RUNNING,
            "FINISHED": wire.USER_CONTROL_FINISHED,
        }[value.execution.value],
    )
    if value.root is not None:
        result.root.CopyFrom(
            wire.RootControlResult(
                status=value.root.status or "", reason=value.root.reason or ""
            )
        )
    if value.subagent is not None:
        result.subagent.CopyFrom(
            wire.SubagentControlResult(
                disposition=value.subagent.disposition,
                status=value.subagent.status or "",
                reason=value.subagent.reason or "",
            )
        )
    if value.process is not None:
        process = wire.ProcessControlResult(
            disposition=value.process.disposition,
            status=value.process.status,
            physical_state=value.process.physical_state,
        )
        if value.process.exit_code is not None:
            process.exit_code = value.process.exit_code
        if value.process.group_alive is not None:
            process.group_alive = value.process.group_alive
        result.process.CopyFrom(process)
    if value.monitor is not None:
        result.monitor.CopyFrom(
            wire.MonitorControlResult(
                monitor_id=value.monitor.monitor_id,
                outcome=value.monitor.outcome,
                in_flight_observation_ids=value.monitor.in_flight_observation_ids,
                detail=value.monitor.detail or "",
            )
        )
    if value.feedback is not None:
        feedback = wire.UserControlFeedbackState(
            canonical_status=value.feedback.canonical_status.value,
            inclusion_status=value.feedback.inclusion_status.value,
            owner_availability=value.feedback.owner_availability.value,
            reason=value.feedback.reason or "",
            target_root_turn_id=value.feedback.target_root_turn_id or "",
            entry_id=value.feedback.entry_id or "",
            context_binding_revision_id=(
                value.feedback.context_binding_revision_id or ""
            ),
            transport_invocation_attempted=(
                value.feedback.transport.invocation_attempted
            ),
            transport_detail=value.feedback.transport.detail or "",
        )
        if value.feedback.model_call_index is not None:
            feedback.model_call_index = value.feedback.model_call_index
        if value.feedback.transport.invocation_succeeded is not None:
            feedback.transport_invocation_succeeded = (
                value.feedback.transport.invocation_succeeded
            )
        result.feedback.CopyFrom(feedback)
    return result


def _background_process_to_wire(value: TerminalProcessInfo) -> wire.BackgroundProcessItem:
    item = wire.BackgroundProcessItem(
        process_id=value.process_id,
        command=value.command,
        cwd=value.cwd,
        status=value.status,
        physical_state=value.physical_state,
        io_mode=value.io_mode,
        stream_id=value.stream_id,
        output_revision=value.output_revision,
        output_cursor=value.output_cursor,
        retained_from_cursor=value.retained_from_cursor,
        started_at_monotonic=value.started_at_monotonic,
        duration_seconds=value.duration_seconds,
        timed_out=value.timed_out,
        stdin_closed=value.stdin_closed,
        origin=wire.BackgroundProcessOrigin(
            turn_id=value.origin.turn_id,
            scope_kind=value.origin.conversation_scope_kind,
            subagent_task_id=value.origin.scope_subagent_task_id or "",
        ),
    )
    if value.exit_code is not None:
        item.exit_code = value.exit_code
    if value.ended_at_monotonic is not None:
        item.ended_at_monotonic = value.ended_at_monotonic
    return item


def _encode_background_cursor(
    *,
    session_id: str,
    host_session_id: str,
    started_at: float,
    process_id: str,
) -> str:
    body = json.dumps(
        {
            "v": 1,
            "kind": "background-processes",
            "session_id": session_id,
            "host_session_id": host_session_id,
            "started_at": started_at,
            "process_id": process_id,
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def _decode_background_cursor(
    value: str, *, session_id: str, host_session_id: str
) -> tuple[float, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if (
            not isinstance(decoded, dict)
            or set(decoded)
            != {
                "v",
                "kind",
                "session_id",
                "host_session_id",
                "started_at",
                "process_id",
            }
            or decoded["v"] != 1
            or decoded["kind"] != "background-processes"
            or decoded["session_id"] != session_id
            or decoded["host_session_id"] != host_session_id
            or not isinstance(decoded["started_at"], (int, float))
            or isinstance(decoded["started_at"], bool)
            or not isinstance(decoded["process_id"], str)
            or not decoded["process_id"]
        ):
            raise ValueError
        return float(decoded["started_at"]), decoded["process_id"]
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("background cursor is invalid") from exc


def _error(request_id: str, code: str) -> wire.ServerFrame:
    return wire.ServerFrame(
        error=wire.ProtocolError(
            request_id=request_id,
            stable_code=code,
            public_message="Protocol v3 request was rejected.",
        )
    )


def _request_id(frame: wire.ClientFrame) -> str:
    kind = frame.WhichOneof("request")
    return str(getattr(getattr(frame, kind), "request_id", "")) if kind else ""


def _stable_error_code(exc: BaseException) -> str:
    if isinstance(exc, (ValueError, KeyError)):
        return "INVALID_REQUEST"
    if isinstance(exc, TimeoutError):
        return "DEADLINE_EXCEEDED"
    return "SERVER_OPERATION_FAILED"


def _valid_command_id(value: str) -> bool:
    size = len(value.encode("utf-8"))
    return bool(value) and size <= MAXIMUM_COMMAND_ID_BYTES and "\x00" not in value


def _valid_prompt(value: str) -> bool:
    if not value:
        return False
    try:
        prompt_text_utf8_bytes((LLMTextPart(value),))
    except (UnicodeEncodeError, ValueError):
        return False
    return True


def _prompt_content_from_wire(value: wire.PromptContent) -> PromptContent:
    parts: list[LLMTextPart | PromptImagePart] = []
    for item in value.parts:
        kind = item.WhichOneof("value")
        if kind == "text":
            parts.append(LLMTextPart(item.text))
        elif kind == "image":
            parts.append(
                PromptImagePart(
                    original_bytes=bytes(item.image.content),
                    declared_mime=item.image.declared_media_type,
                )
            )
        else:
            raise ValueError("prompt content part has no value")
    return PromptContent(tuple(parts))


def _permission_from_wire(value: int) -> PermissionMode | None:
    return {
        wire.PERMISSION_MODE_ACCEPT_EDITS: PermissionMode.ACCEPT_EDITS,
        wire.PERMISSION_MODE_READ_ONLY: PermissionMode.READ_ONLY,
        wire.PERMISSION_MODE_ASK_PERMISSIONS: PermissionMode.ASK_PERMISSIONS,
        wire.PERMISSION_MODE_BYPASS_PERMISSIONS: PermissionMode.BYPASS_PERMISSIONS,
    }.get(value)


def _permission_to_wire(value: PermissionMode) -> int:
    return {
        PermissionMode.ACCEPT_EDITS: wire.PERMISSION_MODE_ACCEPT_EDITS,
        PermissionMode.READ_ONLY: wire.PERMISSION_MODE_READ_ONLY,
        PermissionMode.ASK_PERMISSIONS: wire.PERMISSION_MODE_ASK_PERMISSIONS,
        PermissionMode.BYPASS_PERMISSIONS: wire.PERMISSION_MODE_BYPASS_PERMISSIONS,
    }[value]


__all__ = [
    "HEARTBEAT_INTERVAL_MS",
    "MAXIMUM_FRAME_BYTES",
    "PROTOCOL_MAJOR",
    "PROTOCOL_MINOR",
    "PROTOCOL_SCHEMA_FINGERPRINT",
    "TerminalKernelProtocolServer",
    "install_fingerprint",
    "protocol_identity",
]
