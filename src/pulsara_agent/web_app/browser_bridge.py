"""Process-local browser generations over Terminal Protocol v3."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

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
from pulsara_agent.web_app.session_controller import (
    LocalSessionController,
    NoOldHostReadyToResume,
    OldHostCloseFull,
    OldHostCloseQuarantined,
    PreparedRawCloseOperation,
    SessionRetirementOperation,
    RuntimeReopenOperation,
)
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.conversation_kernel.repository import ConversationKernelConflict


_BROWSER_DETACH_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class BrowserSessionDetachToken:
    session_id: str
    detach_nonce: str
    _operation: RuntimeReopenOperation | PreparedRawCloseOperation | SessionRetirementOperation
    _owner: "LocalBrowserBridge"
    _gate: object
    _settled: bool

    def __init__(
        self,
        *,
        session_id: str,
        detach_nonce: str,
        operation: RuntimeReopenOperation | PreparedRawCloseOperation | SessionRetirementOperation,
        gate: object,
        owner: "LocalBrowserBridge",
        _seal: object,
    ) -> None:
        if (
            _seal is not _BROWSER_DETACH_SEAL
            or operation.session_id != session_id
            or not detach_nonce
        ):
            raise TypeError("browser detach token is bridge-issued")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "detach_nonce", detach_nonce)
        object.__setattr__(self, "_operation", operation)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_gate", gate)
        object.__setattr__(self, "_settled", False)

    def _consume(self, owner: "LocalBrowserBridge") -> None:
        if self._owner is not owner or self._settled:
            raise RuntimeError("browser detach token is stale")
        object.__setattr__(self, "_settled", True)


@dataclass(frozen=True, slots=True)
class BridgeDetachNotStarted:
    reason: str


@dataclass(frozen=True, slots=True)
class BridgeDetachFull:
    token: BrowserSessionDetachToken


@dataclass(frozen=True, slots=True)
class BridgeDetachFailed:
    token: BrowserSessionDetachToken
    error: str


BridgeDetachOutcome = BridgeDetachNotStarted | BridgeDetachFull | BridgeDetachFailed


@dataclass(frozen=True, slots=True, init=False)
class BridgeSettlementFull:
    session_id: str
    _operation: RuntimeReopenOperation | PreparedRawCloseOperation | SessionRetirementOperation
    _owner: "LocalBrowserBridge"

    def __init__(
        self,
        *,
        token: BrowserSessionDetachToken,
        owner: "LocalBrowserBridge",
        _seal: object,
    ) -> None:
        if _seal is not _BROWSER_DETACH_SEAL or token._owner is not owner:
            raise TypeError("browser settlement is bridge-issued")
        object.__setattr__(self, "session_id", token.session_id)
        object.__setattr__(self, "_operation", token._operation)
        object.__setattr__(self, "_owner", owner)


@dataclass(frozen=True, slots=True)
class BridgeSettlementFailed:
    token: BrowserSessionDetachToken
    error: str


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
        self._browser_instance_by_connection: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_gate_users: dict[str, int] = {}
        self._quarantined_sessions: set[str] = set()
        self._next_generation = 0
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def _session_gate(self, session_id: str):
        async with self._lock:
            if self._closing:
                raise RuntimeError("Local Web application is draining")
            if session_id in self._quarantined_sessions:
                raise RuntimeError("browser Session gate is quarantined")
            session_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
            self._session_gate_users[session_id] = (
                self._session_gate_users.get(session_id, 0) + 1
            )
        try:
            async with session_lock:
                yield
        finally:
            async with self._lock:
                remaining = self._session_gate_users.get(session_id, 1) - 1
                if remaining > 0:
                    self._session_gate_users[session_id] = remaining
                else:
                    self._session_gate_users.pop(session_id, None)
                    if not any(
                        connection.session_id == session_id
                        for connection in self._connections.values()
                    ):
                        self._session_locks.pop(session_id, None)

    async def connect(
        self,
        session_id: str,
        *,
        browser_instance_id: str,
        takeover: bool = False,
    ) -> dict[str, object]:
        if not session_id:
            raise ValueError("session_id is required")
        if not browser_instance_id:
            raise ValueError("browser_instance_id is required")
        async with self._session_gate(session_id):
            old: BrowserRuntimeConnection | None = None
            async with self._lock:
                controller_id = self._controller_by_session.get(session_id)
                if controller_id is not None and controller_id not in self._connections:
                    self._controller_by_session.pop(session_id, None)
                    self._browser_instance_by_connection.pop(controller_id, None)
                    controller_id = None
                same_browser_instance = (
                    controller_id is not None
                    and self._browser_instance_by_connection.get(controller_id)
                    == browser_instance_id
                )
                if controller_id is not None and (takeover or same_browser_instance):
                    self._controller_by_session.pop(session_id, None)
                    old = self._connections.pop(controller_id, None)
                    self._browser_instance_by_connection.pop(controller_id, None)
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
                if (
                    role != "controller"
                    or exc.code != "CONTROLLER_UNAVAILABLE"
                    or (old is not None and takeover)
                ):
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
                    self._browser_instance_by_connection[connection.connection_id] = (
                        browser_instance_id
                    )
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
            self._browser_instance_by_connection.pop(connection_id, None)
            if (
                connection is not None
                and self._controller_by_session.get(connection.session_id)
                == connection_id
            ):
                self._controller_by_session.pop(connection.session_id, None)
        if connection is not None:
            await connection.aclose()

    async def disconnect_session(self, session_id: str) -> None:
        async with self._session_gate(session_id):
            await self._disconnect_session_under_gate(session_id)

    async def _disconnect_session_under_gate(self, session_id: str) -> None:
        async with self._lock:
            ids = tuple(
                connection_id
                for connection_id, connection in self._connections.items()
                if connection.session_id == session_id
            )
        for connection_id in ids:
            await self.disconnect(connection_id)

    async def detach_session_for_runtime_reopen(
        self, operation: RuntimeReopenOperation
    ) -> BridgeDetachOutcome:
        return await self._detach_session_for_operation(
            operation,
            confirm=lambda: self.sessions.confirm_runtime_reopen_operation(operation),
        )

    async def detach_session_for_retirement(self, operation: SessionRetirementOperation) -> BridgeDetachOutcome:
        return await self._detach_session_for_operation(
            operation, confirm=lambda: self.sessions.confirm_session_retirement_operation(operation),
        )

    async def settle_session_retirement_detach(self, token, *, close_full: bool):
        return await self.settle_raw_close_detach(token, close_full=close_full)

    async def detach_session_for_raw_close(
        self, operation: PreparedRawCloseOperation
    ) -> BridgeDetachOutcome:
        return await self._detach_session_for_operation(
            operation,
            confirm=lambda: self.sessions.confirm_raw_close_operation(operation),
        )

    async def _detach_session_for_operation(
        self,
        operation: RuntimeReopenOperation | PreparedRawCloseOperation | SessionRetirementOperation,
        *,
        confirm,
    ) -> BridgeDetachOutcome:
        """Detach all exact Session connections behind the shared connect gate."""

        gate = self._session_gate(operation.session_id)
        try:
            await gate.__aenter__()
        except BaseException as exc:
            return BridgeDetachNotStarted(str(exc))
        try:
            await confirm()
        except BaseException as exc:
            await gate.__aexit__(None, None, None)
            return BridgeDetachNotStarted(str(exc))

        token = BrowserSessionDetachToken(
            session_id=operation.session_id,
            detach_nonce=f"browser-detach:{uuid4().hex}",
            operation=operation,
            gate=gate,
            owner=self,
            _seal=_BROWSER_DETACH_SEAL,
        )
        async with self._lock:
            connections = tuple(
                connection
                for connection in self._connections.values()
                if connection.session_id == operation.session_id
            )
            for connection in connections:
                self._connections.pop(connection.connection_id, None)
                self._browser_instance_by_connection.pop(connection.connection_id, None)
                if (
                    self._controller_by_session.get(operation.session_id)
                    == connection.connection_id
                ):
                    self._controller_by_session.pop(operation.session_id, None)
        errors: list[str] = []
        for connection in connections:
            try:
                await connection.aclose()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        if errors:
            await self._quarantine_detach(operation.session_id)
            return BridgeDetachFailed(token, ",".join(errors))
        return BridgeDetachFull(token)

    async def settle_runtime_reopen_detach(
        self,
        token: BrowserSessionDetachToken,
        host_outcome: object,
    ) -> BridgeSettlementFull | BridgeSettlementFailed:
        if (
            token._operation.session_id != token.session_id
            or getattr(host_outcome, "operation", None) is not token._operation
        ):
            return await self._settle_detach_quarantined(
                token, "Host outcome lost exact detach operation binding"
            )
        if isinstance(host_outcome, OldHostCloseQuarantined):
            return await self._settle_detach_quarantined(
                token, "old Host close is quarantined"
            )
        if not isinstance(host_outcome, (OldHostCloseFull, NoOldHostReadyToResume)):
            return await self._settle_detach_quarantined(
                token, "Host outcome is outside the runtime-reopen closed union"
            )
        return await self._settle_detach_full(token)

    async def settle_runtime_reopen_abort(
        self, token: BrowserSessionDetachToken
    ) -> BridgeSettlementFull | BridgeSettlementFailed:
        return await self._settle_detach_full(token)

    async def settle_raw_close_detach(
        self, token: BrowserSessionDetachToken, *, close_full: bool
    ) -> BridgeSettlementFull | BridgeSettlementFailed:
        if not close_full:
            return await self._settle_detach_quarantined(
                token, "raw Session close is quarantined"
            )
        return await self._settle_detach_full(token)

    async def quarantine_detach_failure(
        self, token: BrowserSessionDetachToken, *, error: str
    ) -> BridgeSettlementFailed:
        return await self._settle_detach_quarantined(token, error)

    async def _settle_detach_full(
        self, token: BrowserSessionDetachToken
    ) -> BridgeSettlementFull | BridgeSettlementFailed:
        try:
            token._consume(self)
            await token._gate.__aexit__(None, None, None)
        except BaseException as exc:
            await self._quarantine_detach(token.session_id)
            return BridgeSettlementFailed(token, type(exc).__name__)
        return BridgeSettlementFull(token=token, owner=self, _seal=_BROWSER_DETACH_SEAL)

    async def _settle_detach_quarantined(
        self, token: BrowserSessionDetachToken, error: str
    ) -> BridgeSettlementFailed:
        await self._quarantine_detach(token.session_id)
        if not token._settled:
            token._consume(self)
            try:
                await token._gate.__aexit__(None, None, None)
            except BaseException as exc:
                error = f"{error};{type(exc).__name__}"
        return BridgeSettlementFailed(token, error)

    async def _quarantine_detach(self, session_id: str) -> None:
        async with self._lock:
            self._quarantined_sessions.add(session_id)

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
        allowed_fields = {
            "command_id",
            "command_kind",
            "client_submission_id",
            "plan_reason",
            "target_turn_id",
            "target_queue_item_id",
            "subagent_task_id",
            "requested_permission_mode",
            "target_plan_workflow_id",
            "expected_plan_workflow_revision",
            "force",
            "expected_session_id",
            "expected_host_session_id",
            "target_process_id",
            "prompt_content",
        }
        if set(body) - allowed_fields:
            raise ValueError("command contains an unexpected field")
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
                "CANCEL_QUEUED_PROMPT",
                "STEER_QUEUED_PROMPT",
                "ACCEPT_SUBAGENT_COMPLETION",
                "ENTER_PLAN",
                "CANCEL_PLAN",
                "FORCE_EXIT_PLAN",
                "COMPACT_CONTEXT",
                "CANCEL_SUBAGENT_TASK",
                "TERMINATE_BACKGROUND_PROCESS",
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
            client_submission_id=str(
                body.get(
                    "client_submission_id",
                    ""
                    if kind_name
                    in {
                        "STOP_ACTIVE_TURN",
                        "CANCEL_SUBAGENT_TASK",
                        "TERMINATE_BACKGROUND_PROCESS",
                    }
                    else command_id,
                )
            ),
            plan_reason=str(body.get("plan_reason", "")),
            target_turn_id=str(body.get("target_turn_id", "")),
            target_queue_item_id=str(body.get("target_queue_item_id", "")),
            subagent_task_id=str(body.get("subagent_task_id", "")),
            requested_permission_mode=permission,
            target_plan_workflow_id=str(body.get("target_plan_workflow_id", "")),
            expected_plan_workflow_revision=_uint(
                body.get("expected_plan_workflow_revision", 0),
                "expected_plan_workflow_revision",
            ),
            force=bool(body.get("force", False)),
            expected_session_id=str(body.get("expected_session_id", "")),
            expected_host_session_id=str(body.get("expected_host_session_id", "")),
            target_process_id=str(body.get("target_process_id", "")),
        )
        if "prompt_content" in body:
            request.prompt_content.CopyFrom(
                _prompt_content_from_json(body["prompt_content"])
            )
        return protobuf_json(await connection.controller.request("command", request))

    async def query_command(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.QueryCommandRequest(
            command_id=_required_string(body, "command_id")
        )
        expected = body.get("expected_control")
        if expected is not None:
            if not isinstance(expected, dict):
                raise ValueError("expected_control must be an object")
            operation = _enum_value(
                _required_string(expected, "operation"),
                allowed={
                    "USER_CONTROL_STOP_ACTIVE_TURN",
                    "USER_CONTROL_CANCEL_SUBAGENT_TASK",
                    "USER_CONTROL_TERMINATE_BACKGROUND_PROCESS",
                },
            )
            kind = _enum_value(
                _required_string(expected, "target_kind"),
                allowed={
                    "USER_CONTROL_ROOT_TURN",
                    "USER_CONTROL_SUBAGENT_TASK",
                    "USER_CONTROL_BACKGROUND_PROCESS",
                },
            )
            request.expected_control.CopyFrom(
                wire.ExpectedUserControl(
                    operation=operation,
                    session_id=_required_string(expected, "session_id"),
                    host_session_id=_required_string(expected, "host_session_id"),
                    target=wire.UserControlTarget(
                        kind=kind,
                        target_id=_required_string(expected, "target_id"),
                    ),
                )
            )
        return protobuf_json(
            await connection.controller.request("query_command", request)
        )

    async def read_content(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        entry_id = body.get("entry_id")
        queue_item_id = body.get("queue_item_id")
        if (isinstance(entry_id, str) and bool(entry_id)) == (
            isinstance(queue_item_id, str) and bool(queue_item_id)
        ):
            raise ValueError("exactly one content target is required")
        request = wire.ReadContentRequest(
            block_id=str(body.get("block_id", "")),
            offset_bytes=_uint(body.get("offset_bytes", 0), "offset_bytes"),
            limit_bytes=_bounded_uint(
                body.get("limit_bytes", 256 << 10),
                "limit_bytes",
                minimum=1,
                maximum=1 << 20,
            ),
        )
        if "image_ref_ordinal" in body:
            request.image_ref_ordinal = _uint(
                body["image_ref_ordinal"], "image_ref_ordinal"
            )
        if "visualization_ordinal" in body:
            request.visualization_ordinal = _uint(
                body["visualization_ordinal"], "visualization_ordinal"
            )
        if isinstance(entry_id, str) and entry_id:
            request.entry_id = entry_id
        else:
            request.queue_item_id = str(queue_item_id)
        return protobuf_json(
            await connection.controller.request("read_content", request)
        )

    async def read_tool_artifact(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ReadToolArtifactRequest(
            result_entry_id=_required_string(body, "result_entry_id"),
            offset_chars=_uint(body.get("offset_chars", 0), "offset_chars"),
            max_chars=_bounded_uint(
                body.get("max_chars", 32_000),
                "max_chars",
                minimum=1,
                maximum=32_000,
            ),
        )
        return protobuf_json(
            await connection.controller.request("read_tool_artifact", request)
        )

    async def list_background_processes(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ListBackgroundProcessesRequest(
            expected_session_id=_required_string(body, "expected_session_id"),
            expected_host_session_id=_required_string(body, "expected_host_session_id"),
            cursor=str(body.get("cursor", "")),
            maximum_items=_bounded_uint(
                body.get("maximum_items", 50),
                "maximum_items",
                minimum=1,
                maximum=50,
            ),
        )
        return protobuf_json(
            await connection.controller.request("list_background_processes", request)
        )

    async def read_background_process_log(
        self, connection_id: str, body: dict[str, object]
    ) -> dict[str, object]:
        connection = await self._connection(connection_id)
        request = wire.ReadBackgroundProcessLogRequest(
            expected_session_id=_required_string(body, "expected_session_id"),
            expected_host_session_id=_required_string(body, "expected_host_session_id"),
            process_id=_required_string(body, "process_id"),
            output_cursor=str(body.get("output_cursor", "")),
            max_output_chars=_bounded_uint(
                body.get("max_output_chars", 32_000),
                "max_output_chars",
                minimum=512,
                maximum=32_000,
            ),
        )
        return protobuf_json(
            await connection.controller.request("read_background_process_log", request)
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

    async def capability_form(
        self,
        connection_id: str,
        body: dict[str, object],
        *,
        submit: bool,
    ) -> dict[str, object]:
        # The private input stays on this narrow HTTP path; it is never sent to
        # Protocol-v3 command/interaction DTOs, broadcasts or command history.
        required = {"interaction_id", "expected_owner_epoch", "expected_live_revision"}
        allowed = required | ({"decision", "submission"} if submit else set())
        if set(body) - allowed or not required <= set(body):
            raise ProtocolBridgeError(
                "CAPABILITY_FORM_INVALID", "能力表单请求不完整，请重新打开。"
            )
        connection = await self._connection(connection_id)
        if connection.role != "controller":
            raise ProtocolBridgeError(
                "CONTROLLER_REQUIRED", "请在当前控制此会话的窗口操作。"
            )
        session = self.sessions.session_by_host_id(connection.host_session_id)
        kwargs = dict(
            attachment_id=connection.controller.attachment_id,
            interaction_id=_required_string(body, "interaction_id"),
            expected_owner_epoch=_uint(
                body.get("expected_owner_epoch"), "expected_owner_epoch"
            ),
            expected_live_revision=_uint(
                body.get("expected_live_revision"), "expected_live_revision"
            ),
        )
        try:
            if not submit:
                return {"form": thaw_json(session.read_capability_form(**kwargs))}
            await session.resolve_capability_form(
                **kwargs,
                decision=body.get("decision"),
                submission=body.get("submission"),
            )
        except ConversationKernelConflict:
            raise ProtocolBridgeError(
                "CAPABILITY_FORM_STALE", "能力表单已变化或已关闭，请等待当前会话更新。"
            ) from None
        except Exception:
            # A validator may include the submitted secret in its exception.
            # Do not serialize it or attach it to a public/protocol error.
            raise ProtocolBridgeError(
                "CAPABILITY_FORM_INVALID",
                "配置尚未提交，请检查填写内容与当前连接后重试。",
            ) from None
        return {"submitted": body.get("decision") == "SUBMIT"}

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
            self._browser_instance_by_connection.clear()
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
                self._browser_instance_by_connection.pop(connection_id, None)
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


def _prompt_content_from_json(value: object) -> wire.PromptContent:
    if not isinstance(value, dict) or set(value) != {"parts"}:
        raise ValueError("prompt_content must contain only ordered parts")
    raw_parts = value["parts"]
    if not isinstance(raw_parts, list):
        raise ValueError("prompt_content.parts must be an array")
    result = wire.PromptContent()
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            raise ValueError("prompt content part must be an object")
        kind = raw_part.get("type")
        item = result.parts.add()
        if kind == "text" and set(raw_part) == {"type", "text"}:
            text = raw_part["text"]
            if not isinstance(text, str):
                raise ValueError("prompt text part must contain text")
            item.text = text
        elif kind == "image" and set(raw_part) == {
            "type",
            "content_base64",
            "declared_media_type",
        }:
            content = raw_part["content_base64"]
            media_type = raw_part["declared_media_type"]
            if not isinstance(content, str) or not isinstance(media_type, str):
                raise ValueError("prompt image part is invalid")
            try:
                decoded = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("prompt image content is not valid base64") from exc
            item.image.content = decoded
            item.image.declared_media_type = media_type
        else:
            raise ValueError("prompt content part has an invalid shape")
    return result


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
