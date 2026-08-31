"""Process-local browser generations over Terminal Protocol v3."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from pulsara_agent.terminal_protocol.generated_v3 import (
    terminal_kernel_v3_pb2 as wire,
)
from pulsara_agent.primitives.plan_workflow import (
    MAXIMUM_PLAN_CHUNK_BYTES,
    MINIMUM_PLAN_CHUNK_BYTES,
)
from pulsara_agent.terminal_protocol.canonical_v3 import MAXIMUM_CONTROL_ITEMS
from pulsara_agent.terminal_protocol.v3_gateway import (
    MAXIMUM_OBSERVATION_EVENTS,
    MAXIMUM_OBSERVATION_WAIT_MS,
    TerminalKernelProtocolServer,
)
from pulsara_agent.web_app.protocol_client import (
    BrowserRuntimeConnection,
    ProtocolBridgeError,
    ProtocolTransportClosed,
)
from pulsara_agent.web_app.session_controller import LocalSessionController


def protobuf_json(message: Message) -> dict[str, object]:
    """Render exact proto names/enums without inventing a second schema."""

    value = MessageToDict(
        message,
        preserving_proto_field_name=True,
        use_integers_for_enums=False,
    )
    if not isinstance(value, dict):
        raise RuntimeError("Protocol-v3 JSON projection lost its object shape")
    return value


class LocalBrowserBridge:
    """Own browser connection replacement and route attached operations."""

    def __init__(
        self,
        *,
        sessions: LocalSessionController,
        protocol_server: TerminalKernelProtocolServer,
    ) -> None:
        self.sessions = sessions
        self.protocol_server = protocol_server
        self._connections: dict[str, BrowserRuntimeConnection] = {}
        self._controller_by_session: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._next_generation = 0
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    async def connect(
        self, session_id: str, *, takeover: bool = False
    ) -> dict[str, object]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._lock:
            if self._closing:
                raise RuntimeError("Local Web application is draining")
            session_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with session_lock:
            old: BrowserRuntimeConnection | None = None
            async with self._lock:
                controller_id = self._controller_by_session.get(session_id)
                if controller_id is not None and controller_id not in self._connections:
                    self._controller_by_session.pop(session_id, None)
                    controller_id = None
                if takeover and controller_id is not None:
                    self._controller_by_session.pop(session_id, None)
                    old = self._connections.pop(controller_id, None)
                    controller_id = None
                role = "controller" if controller_id is None else "observer"
            if old is not None:
                await old.aclose()

            handle = await self.sessions.resume_session(session_id)
            async with self._lock:
                if self._closing:
                    raise RuntimeError("Local Web application is draining")
                self._next_generation += 1
                generation = self._next_generation
            try:
                connection = await BrowserRuntimeConnection.open(
                    server=self.protocol_server,
                    session_id=handle.session_id,
                    host_session_id=handle.host_session_id,
                    generation=generation,
                    role=role,
                )
            except ProtocolBridgeError as exc:
                if role != "controller" or exc.code != "CONTROLLER_UNAVAILABLE":
                    raise
                role = "observer"
                connection = await BrowserRuntimeConnection.open(
                    server=self.protocol_server,
                    session_id=handle.session_id,
                    host_session_id=handle.host_session_id,
                    generation=generation,
                    role=role,
                )
            try:
                async with self._lock:
                    if self._closing:
                        raise RuntimeError("Local Web application is draining")
                    self._connections[connection.connection_id] = connection
                    if role == "controller":
                        self._controller_by_session[session_id] = (
                            connection.connection_id
                        )
                return self._connection_payload(connection)
            except BaseException:
                await connection.aclose()
                raise

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            connection = self._connections.pop(connection_id, None)
            if (
                connection is not None
                and self._controller_by_session.get(connection.session_id)
                == connection_id
            ):
                self._controller_by_session.pop(connection.session_id, None)
        if connection is not None:
            await connection.aclose()

    async def disconnect_session(self, session_id: str) -> None:
        async with self._lock:
            ids = tuple(
                connection_id
                for connection_id, connection in self._connections.items()
                if connection.session_id == session_id
            )
        for connection_id in ids:
            await self.disconnect(connection_id)

    async def snapshot(self, connection_id: str) -> dict[str, object]:
        connection = await self._connection(connection_id)
        frame = await connection.controller.request(
            "snapshot",
            wire.SnapshotRequest(
                maximum_entries=256,
                maximum_control_items=MAXIMUM_CONTROL_ITEMS,
            ),
        )
        return protobuf_json(frame)

    async def history(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        cursor = body.get("cursor")
        if not isinstance(cursor, dict):
            raise ValueError("history cursor is required")
        request = wire.HistoryPageRequest(
            cursor=wire.HistoryCursor(
                session_id=str(cursor.get("session_id", "")),
                cut_sequence=_uint(cursor.get("cut_sequence"), "cut_sequence"),
                entry_sequence=_uint(cursor.get("entry_sequence"), "entry_sequence"),
            ),
            maximum_entries=_bounded_uint(
                body.get("maximum_entries", 128),
                "maximum_entries",
                minimum=1,
                maximum=256,
            ),
            maximum_serialized_bytes=_bounded_uint(
                body.get("maximum_serialized_bytes", 2 << 20),
                "maximum_serialized_bytes",
                minimum=1024,
                maximum=8 << 20,
            ),
        )
        return protobuf_json(
            await connection.controller.request("history_page", request)
        )

    async def observe(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ObserveRequest(
            after_event_sequence=_uint(
                body.get("after_event_sequence", 0), "after_event_sequence"
            ),
            live_owner_epoch=_uint(body.get("live_owner_epoch", 0), "live_owner_epoch"),
            after_live_revision=_uint(
                body.get("after_live_revision", 0), "after_live_revision"
            ),
            maximum_events=_bounded_uint(
                body.get("maximum_events", 128),
                "maximum_events",
                minimum=1,
                maximum=MAXIMUM_OBSERVATION_EVENTS,
            ),
            maximum_bytes=_bounded_uint(
                body.get("maximum_bytes", 2 << 20),
                "maximum_bytes",
                minimum=1024,
                maximum=8 << 20,
            ),
            wait_ms=_bounded_uint(
                body.get("wait_ms", min(2_000, MAXIMUM_OBSERVATION_WAIT_MS)),
                "wait_ms",
                minimum=0,
                maximum=MAXIMUM_OBSERVATION_WAIT_MS,
            ),
            live_control_owner_epoch=_uint(
                body.get("live_control_owner_epoch", 0),
                "live_control_owner_epoch",
            ),
            after_live_control_revision=_uint(
                body.get("after_live_control_revision", 0),
                "after_live_control_revision",
            ),
        )
        return protobuf_json(await connection.observer.request("observe", request))

    async def live_control_snapshot(self, connection_id: str) -> dict[str, object]:
        connection = await self._connection(connection_id)
        return protobuf_json(
            await connection.observer.request(
                "live_control_snapshot", wire.LiveControlSnapshotRequest()
            )
        )

    async def command(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        command_id = _required_string(body, "command_id")
        kind_name = _required_string(body, "command_kind")
        kind = _enum_value(
            kind_name,
            allowed={
                "SUBMIT_PROMPT",
                "STOP_ACTIVE_TURN",
                "DETACH",
                "CLOSE_SESSION",
                "STEER_ACTIVE_TURN",
                "ACCEPT_SUBAGENT_COMPLETION",
                "ENTER_PLAN",
                "CANCEL_PLAN",
                "FORCE_EXIT_PLAN",
                "COMPACT_CONTEXT",
            },
        )
        permission_name = str(
            body.get("requested_permission_mode", "PERMISSION_MODE_UNSPECIFIED")
        )
        permission = _enum_value(
            permission_name,
            allowed={
                "PERMISSION_MODE_UNSPECIFIED",
                "PERMISSION_MODE_ACCEPT_EDITS",
                "PERMISSION_MODE_READ_ONLY",
                "PERMISSION_MODE_ASK_PERMISSIONS",
                "PERMISSION_MODE_BYPASS_PERMISSIONS",
            },
        )
        request = wire.CommandRequest(
            command_id=command_id,
            command_kind=kind,
            client_submission_id=str(body.get("client_submission_id", command_id)),
            text=str(body.get("text", "")),
            target_turn_id=str(body.get("target_turn_id", "")),
            subagent_task_id=str(body.get("subagent_task_id", "")),
            requested_permission_mode=permission,
            target_plan_workflow_id=str(body.get("target_plan_workflow_id", "")),
            expected_plan_workflow_revision=_uint(
                body.get("expected_plan_workflow_revision", 0),
                "expected_plan_workflow_revision",
            ),
            force=bool(body.get("force", False)),
        )
        return protobuf_json(await connection.controller.request("command", request))

    async def query_command(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.QueryCommandRequest(
            command_id=_required_string(body, "command_id")
        )
        return protobuf_json(
            await connection.controller.request("query_command", request)
        )

    async def read_content(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ReadContentRequest(
            entry_id=_required_string(body, "entry_id"),
            block_id=str(body.get("block_id", "")),
            offset_bytes=_uint(body.get("offset_bytes", 0), "offset_bytes"),
            limit_bytes=_bounded_uint(
                body.get("limit_bytes", 256 << 10),
                "limit_bytes",
                minimum=1,
                maximum=1 << 20,
            ),
        )
        return protobuf_json(
            await connection.controller.request("read_content", request)
        )

    async def resolve_interaction(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        decision_name = _required_string(body, "decision")
        decision = _enum_value(
            decision_name, allowed={"INTERACTION_ALLOW", "INTERACTION_DENY"}
        )
        request = wire.ResolveInteractionRequest(
            command_id=_required_string(body, "command_id"),
            expected_writer_generation=_uint(
                body.get("expected_writer_generation"),
                "expected_writer_generation",
            ),
            expected_owner_epoch=_uint(
                body.get("expected_owner_epoch"), "expected_owner_epoch"
            ),
            expected_live_revision=_uint(
                body.get("expected_live_revision"), "expected_live_revision"
            ),
            interaction_id=_required_string(body, "interaction_id"),
            decision=decision,
        )
        return protobuf_json(
            await connection.controller.request("resolve_interaction", request)
        )

    async def resolve_plan_interaction(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ResolvePlanInteractionRequest(
            command_id=_required_string(body, "command_id"),
            attempt_expected_writer_generation=_uint(
                body.get("attempt_expected_writer_generation"),
                "attempt_expected_writer_generation",
            ),
            interaction_id=_required_string(body, "interaction_id"),
            workflow_id=_required_string(body, "workflow_id"),
            expected_workflow_revision=_uint(
                body.get("expected_workflow_revision"),
                "expected_workflow_revision",
            ),
        )
        resolution_kind = _required_string(body, "resolution_kind")
        if resolution_kind == "question_option":
            request.question_answer.option_ordinal = _uint(
                body.get("option_ordinal"), "option_ordinal"
            )
        elif resolution_kind == "question_text":
            request.question_answer.free_text = _required_string(body, "free_text")
        elif resolution_kind == "draft":
            request.draft.decision = _enum_value(
                _required_string(body, "draft_decision"),
                allowed={
                    "PLAN_DRAFT_APPROVE",
                    "PLAN_DRAFT_REVISE",
                    "PLAN_DRAFT_CANCEL",
                },
            )
            feedback = body.get("feedback")
            if feedback is not None:
                if not isinstance(feedback, str):
                    raise ValueError("feedback must be a string")
                request.draft.feedback = feedback
        else:
            raise ValueError("unsupported Plan interaction resolution")
        return protobuf_json(
            await connection.controller.request("resolve_plan_interaction", request)
        )

    async def read_plan_question(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ReadPlanQuestionContentRequest(
            interaction_id=_required_string(body, "interaction_id")
        )
        return protobuf_json(
            await connection.controller.request("read_plan_question", request)
        )

    async def read_plan_draft(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ReadPlanDraftTextChunkRequest(
            interaction_id=_required_string(body, "interaction_id"),
            offset_utf8_bytes=_uint(
                body.get("offset_utf8_bytes", 0), "offset_utf8_bytes"
            ),
            limit_bytes=_bounded_uint(
                body.get("limit_bytes", MAXIMUM_PLAN_CHUNK_BYTES),
                "limit_bytes",
                minimum=MINIMUM_PLAN_CHUNK_BYTES,
                maximum=MAXIMUM_PLAN_CHUNK_BYTES,
            ),
        )
        expected_digest = body.get("expected_plan_utf8_digest")
        if expected_digest is not None:
            if not isinstance(expected_digest, str):
                raise ValueError("expected_plan_utf8_digest must be a string")
            request.expected_plan_utf8_digest = expected_digest
        return protobuf_json(
            await connection.controller.request("read_plan_draft", request)
        )

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_owner(), name="local-browser-bridge-close"
            )
            self._close_task = task
        await asyncio.shield(task)

    async def _close_owner(self) -> None:
        async with self._lock:
            self._closing = True
            connections = tuple(self._connections.values())
            self._connections.clear()
            self._controller_by_session.clear()
        await asyncio.gather(
            *(connection.aclose() for connection in connections),
            return_exceptions=True,
        )

    async def _connection(self, connection_id: str) -> BrowserRuntimeConnection:
        stale: BrowserRuntimeConnection | None = None
        async with self._lock:
            if self._closing:
                raise RuntimeError("Local Web application is draining")
            connection = self._connections.get(connection_id)
            if connection is not None and not connection.is_open:
                stale = self._connections.pop(connection_id)
                if (
                    self._controller_by_session.get(connection.session_id)
                    == connection_id
                ):
                    self._controller_by_session.pop(connection.session_id, None)
                connection = None
        if stale is not None:
            await stale.aclose()
            raise ProtocolTransportClosed()
        if connection is None:
            raise KeyError(connection_id)
        return connection

    @staticmethod
    def _connection_payload(
        connection: BrowserRuntimeConnection,
    ) -> dict[str, object]:
        return {
            "connection_id": connection.connection_id,
            "connection_generation": connection.generation,
            "session_id": connection.session_id,
            "role": connection.role,
            "hello": protobuf_json(connection.hello),
            "live_hello": protobuf_json(connection.live_hello),
            "snapshot": protobuf_json(connection.initial_snapshot)["snapshot"],
            "live_control_snapshot": protobuf_json(connection.initial_live_control)[
                "live_control_snapshot"
            ],
        }


def _required_string(body: dict[str, object], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


def _uint(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _bounded_uint(value: object, field: str, *, minimum: int, maximum: int) -> int:
    result = _uint(value, field)
    if not minimum <= result <= maximum:
        raise ValueError(f"{field} is out of bounds")
    return result


def _enum_value(name: str, *, allowed: Iterable[str]) -> int:
    if name not in allowed:
        raise ValueError(f"unsupported Protocol-v3 enum value: {name}")
    value = getattr(wire, name, None)
    if not isinstance(value, int):
        raise ValueError(f"unknown Protocol-v3 enum value: {name}")
    return value


__all__ = ["LocalBrowserBridge", "protobuf_json"]
