"""Single-authority Protocol v3 gateway for the canonical conversation kernel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import hmac
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
from pulsara_agent.conversation_kernel.live import LiveObservationKind
from pulsara_agent.conversation_kernel.live import LiveSettlementKind
from pulsara_agent.ports.live_agent_event import (
    DataDeltaPayload,
    DataEndPayload,
    DataStartPayload,
    InteractionClosedPayload,
    InteractionOpenedPayload,
    InteractionReplacedPayload,
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
    MAXIMUM_HISTORY_PAGE_BYTES,
    MAXIMUM_OBSERVATION_EVENTS,
    MAXIMUM_SNAPSHOT_BYTES,
)
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    PlanDraftIdentityConflict,
    PlanQuestionAnswer,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    PlanDraftDecision,
    PlanQuestionAnswerKind,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire


PROTOCOL_MAJOR = 3
PROTOCOL_MINOR = 0
PROTOCOL_SCHEMA_FINGERPRINT = (
    "sha256:dc888ef204a1c0188dd7cbcd01f3e8cb9ce96fd9f2503538d678fc828455fb6c"
)
MAXIMUM_FRAME_BYTES = 8 << 20
MAXIMUM_OBSERVATION_WAIT_MS = STAGE2_LIMITS.committed_observation_hard_wait_ms
HEARTBEAT_INTERVAL_MS = 10_000
MAXIMUM_PROMPT_BYTES = STAGE2_LIMITS.prompt_hard_bytes
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
            return self._hello(state, frame.hello)
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

    def _hello(
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
        attachment_id = f"terminal-v3-attachment:{uuid4().hex}"
        if (
            request.requested_role == wire.ATTACHMENT_ROLE_CONTROLLER
            and not session.attach_controller(attachment_id)
        ):
            return _error(request.request_id, "CONTROLLER_UNAVAILABLE")
        state.attachment_id = attachment_id
        state.attachment_generation = self._attachment_generation
        state.host_session = session
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
                    snapshot, state.host_session.current_todo_snapshots()
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
            if (
                batch.projections
                or live_wire
                or settlements
                or control_wire
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
            wire.ACCEPT_SUBAGENT_RESULT,
            wire.ACCEPT_JOB_RESULT,
        ) and (request.source_subagent_result_id or request.source_job_id):
            return _error(request.request_id, "COMMAND_SOURCE_UNION_INVALID")
        if request.command_kind != wire.DETACH and (
            state.granted_role != wire.ATTACHMENT_ROLE_CONTROLLER
        ):
            return _error(request.request_id, "CONTROLLER_REQUIRED")
        requested_permission = _permission_from_wire(request.requested_permission_mode)
        if (
            request.command_kind not in (wire.SUBMIT_PROMPT, wire.ENTER_PLAN)
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
        if request.command_kind == wire.SUBMIT_PROMPT:
            if not _valid_prompt(request.text) or request.target_turn_id:
                return _error(request.request_id, "PROMPT_INVALID")
            if requested_permission is None:
                return _error(request.request_id, "PERMISSION_MODE_REQUIRED")
            outcome = await state.host_session.submit_prompt(
                command_id=request.command_id,
                text=request.text,
                requested_permission_mode=requested_permission,
            )
        elif request.command_kind == wire.STEER_ACTIVE_TURN:
            if not _valid_prompt(request.text) or not request.target_turn_id:
                return _error(request.request_id, "STEER_INVALID")
            outcome = await state.host_session.steer_active_turn(
                command_id=request.command_id,
                text=request.text,
                target_turn_id=request.target_turn_id,
            )
        elif request.command_kind == wire.STOP_ACTIVE_TURN:
            if request.text or request.target_turn_id:
                return _error(request.request_id, "STOP_REQUEST_INVALID")
            stopped = await state.host_session.stop_current_turn()
            from pulsara_agent.conversation_kernel.host import KernelCommandOutcome

            outcome = KernelCommandOutcome(
                request.command_id,
                "SUCCEEDED" if stopped else "REJECTED",
                "",
                "TURN_STOP_REQUESTED" if stopped else "NO_ACTIVE_TURN",
                "The active turn was stopped." if stopped else "No active turn exists.",
            )
        elif request.command_kind == wire.ACCEPT_SUBAGENT_RESULT:
            if (
                request.text
                or request.source_job_id
                or not request.source_subagent_result_id
            ):
                return _error(request.request_id, "SUBAGENT_RESULT_REQUEST_INVALID")
            outcome = await state.host_session.accept_subagent_result(
                command_id=request.command_id,
                target_turn_id=request.target_turn_id or None,
                child_result_id=request.source_subagent_result_id,
                actor_id=state.attachment_id,
            )
        elif request.command_kind == wire.ACCEPT_JOB_RESULT:
            if (
                request.text
                or request.source_subagent_result_id
                or not request.source_job_id
            ):
                return _error(request.request_id, "JOB_RESULT_REQUEST_INVALID")
            outcome = await state.host_session.accept_job_result(
                command_id=request.command_id,
                target_turn_id=request.target_turn_id or None,
                job_id=request.source_job_id,
                actor_id=state.attachment_id,
            )
        elif request.command_kind == wire.ENTER_PLAN:
            if (
                requested_permission is None
                or not _valid_prompt(request.text)
                or request.target_turn_id
                or request.target_plan_workflow_id
                or request.expected_plan_workflow_revision
            ):
                return _error(request.request_id, "PLAN_ENTER_REQUEST_INVALID")
            try:
                outcome = await state.host_session.enter_plan(
                    command_id=request.command_id,
                    entry_reason=request.text,
                    resume_permission_mode=requested_permission,
                )
            except (ConversationKernelConflict, ValueError):
                return _error(request.request_id, "PLAN_ENTER_CONFLICT")
        elif request.command_kind in (wire.CANCEL_PLAN, wire.FORCE_EXIT_PLAN):
            if (
                request.text
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
        elif request.command_kind == wire.DETACH:
            if request.text or request.target_turn_id:
                return _error(request.request_id, "DETACH_REQUEST_INVALID")
            from pulsara_agent.conversation_kernel.host import KernelCommandOutcome

            outcome = KernelCommandOutcome(
                request.command_id, "SUCCEEDED", "", "DETACHED", "Client detached."
            )
        elif request.command_kind == wire.CLOSE_SESSION:
            if request.text or request.target_turn_id:
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
        if state.granted_role != wire.ATTACHMENT_ROLE_CONTROLLER:
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
        except ConversationKernelConflict:
            return _error(request.request_id, "INTERACTION_STALE")
        return wire.ServerFrame(
            command_outcome=_outcome_to_wire(request.request_id, outcome)
        )

    async def _resolve_plan_interaction(
        self, state: _Connection, request: wire.ResolvePlanInteractionRequest
    ) -> wire.ServerFrame:
        if not self._has_plan_content_capability(state):
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
        if not self._has_plan_content_capability(state):
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
        if not self._has_plan_content_capability(state):
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
        try:
            reference = await asyncio.to_thread(
                state.protocol_reader.resolve_content_reference,
                session_id=state.host_session.session_id,
                entry_id=request.entry_id,
                block_id=request.block_id or None,
                deadline_monotonic=monotonic() + 10.0,
            )
        except KeyError:
            return _error(request.request_id, "CONTENT_REFERENCE_MISSING")
        except ConversationKernelConflict:
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

    @staticmethod
    def _attachment_matches(state: _Connection, request: object) -> bool:
        return (
            getattr(request, "attachment_id", None) == state.attachment_id
            and getattr(request, "attachment_generation", None)
            == state.attachment_generation
        )

    @staticmethod
    def _has_plan_content_capability(state: _Connection) -> bool:
        """Bind exact Plan reads to the currently attached controller owner."""

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
                block_identity=payload.block_identity
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
    )


def _live_control_snapshot_to_wire(
    snapshot: object, todo_snapshots: tuple[object, ...]
) -> wire.SessionLiveControlSnapshot:
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
    }[outcome.status]
    return wire.CommandOutcome(
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
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > MAXIMUM_PROMPT_BYTES:
        return False
    return all(character in "\n\t" or ord(character) >= 0x20 for character in value)


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
