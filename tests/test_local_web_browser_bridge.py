from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import ClientSession, DummyCookieJar

import pulsara_agent.web_app.browser_bridge as browser_bridge_module
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.browser_bridge import (
    BridgeDetachFailed,
    BridgeDetachFull,
    BridgeSettlementFailed,
    BridgeSettlementFull,
)
from pulsara_agent.web_app.protocol_client import (
    _ATTACHED_FIELDS,
    ProtocolBridgeError,
    TerminalProtocolClient,
)
from pulsara_agent.terminal_protocol.v3_gateway import MAXIMUM_FRAME_BYTES
from pulsara_agent.web_app.session_controller import (
    HostSessionHandle,
    LocalSessionController,
    NoOldHostReadyToResume,
    OldHostCloseFull,
    PreparedRuntimeReopenOperation,
    PreparedRuntimeResumeOperation,
    SessionControlRejected,
)
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime

BROWSER_ONE = "00000000-0000-4000-8000-000000000001"
BROWSER_TWO = "00000000-0000-4000-8000-000000000002"
BROWSER_THREE = "00000000-0000-4000-8000-000000000003"


def test_pr03_background_reads_use_attached_protocol_requests() -> None:
    assert "list_background_processes" in _ATTACHED_FIELDS
    assert "read_background_process_log" in _ATTACHED_FIELDS


class _Sessions:
    async def resume_session(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=session_id,
            host_session_id=f"host:{session_id}",
        )


class _RuntimeConnection:
    next_id = 0

    def __init__(self, *, session_id: str, generation: int, role: str) -> None:
        type(self).next_id += 1
        self.connection_id = f"connection:{type(self).next_id}"
        self.session_id = session_id
        self.generation = generation
        self.role = role
        self.closed = False
        self.requests: list[tuple[str, object]] = []
        self.controller = self
        self.observer = self
        self.attachment_id = f"attachment:{generation}"

    @classmethod
    async def open(
        cls,
        *,
        server: object,
        session_id: str,
        host_session_id: str,
        generation: int,
        role: str,
    ) -> "_RuntimeConnection":
        assert server is not None
        assert host_session_id == f"host:{session_id}"
        return cls(session_id=session_id, generation=generation, role=role)

    async def aclose(self) -> None:
        self.closed = True

    async def request(self, kind: str, request: object) -> wire.ServerFrame:
        self.requests.append((kind, request))
        request_id = str(getattr(request, "request_id", ""))
        if kind == "command":
            return wire.ServerFrame(
                command_outcome=wire.CommandOutcome(
                    request_id=request_id,
                    command_id=str(getattr(request, "command_id")),
                    status=wire.PENDING,
                )
            )
        if kind == "query_command":
            return wire.ServerFrame(
                query_command=wire.QueryCommandResponse(
                    request_id=request_id,
                    found=False,
                    control_query_status=wire.CONTROL_QUERY_RESULT_UNAVAILABLE,
                )
            )
        if kind == "read_content":
            return wire.ServerFrame(
                content=wire.CanonicalContentChunk(
                    request_id=request_id,
                    digest="sha256:exact",
                    content=b"exact-image-bytes",
                    complete_size=len(b"exact-image-bytes"),
                    complete=True,
                )
            )
        if kind == "list_background_processes":
            return wire.ServerFrame(
                background_processes=wire.BackgroundProcessPage(
                    request_id=request_id,
                    session_id=self.session_id,
                    host_session_id=f"host:{self.session_id}",
                )
            )
        if kind == "read_background_process_log":
            return wire.ServerFrame(
                background_process_log=wire.BackgroundProcessLog(
                    request_id=request_id,
                    session_id=self.session_id,
                    host_session_id=f"host:{self.session_id}",
                    output="exact output",
                )
            )
        raise AssertionError(f"unexpected request kind: {kind}")

    @property
    def is_open(self) -> bool:
        return not self.closed


def test_browser_connections_share_one_controller_without_reconnect_ping_pong(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_browser_connection_roles(monkeypatch))


async def _exercise_browser_connection_roles(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(
        browser_bridge_module,
        "BrowserRuntimeConnection",
        _RuntimeConnection,
    )
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )

    first = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    second = await bridge.connect("session:one", browser_instance_id=BROWSER_TWO)
    other_session = await bridge.connect("session:two", browser_instance_id=BROWSER_ONE)

    assert first["role"] == "controller"
    assert second["role"] == "observer"
    assert other_session["role"] == "controller"
    first_connection = bridge._connections[str(first["connection_id"])]
    second_connection = bridge._connections[str(second["connection_id"])]
    other_connection = bridge._connections[str(other_session["connection_id"])]
    assert not first_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert bridge._controller_by_session["session:one"] == first["connection_id"]
    assert (
        bridge._controller_by_session["session:two"] == other_session["connection_id"]
    )

    refreshed = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)

    assert refreshed["role"] == "controller"
    assert first_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert bridge._controller_by_session["session:one"] == refreshed["connection_id"]
    await bridge.disconnect(str(first["connection_id"]))
    assert bridge._controller_by_session["session:one"] == refreshed["connection_id"]

    refreshed_connection = bridge._connections[str(refreshed["connection_id"])]
    third = await bridge.connect(
        "session:one",
        browser_instance_id=BROWSER_THREE,
        takeover=True,
    )

    assert third["role"] == "controller"
    assert refreshed_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert str(second["connection_id"]) in bridge._connections
    assert bridge._controller_by_session["session:one"] == third["connection_id"]
    assert (
        bridge._controller_by_session["session:two"] == other_session["connection_id"]
    )

    await bridge.aclose()


def test_dead_attachment_expires_the_whole_browser_generation(monkeypatch) -> None:
    asyncio.run(_exercise_dead_attachment_expiry(monkeypatch))


async def _exercise_dead_attachment_expiry(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(
        browser_bridge_module,
        "BrowserRuntimeConnection",
        _RuntimeConnection,
    )
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    connection.closed = True

    with pytest.raises(browser_bridge_module.ProtocolTransportClosed):
        await bridge._connection(connection_id)

    assert connection_id not in bridge._connections
    assert "session:one" not in bridge._controller_by_session
    replacement = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    assert replacement["role"] == "controller"
    await bridge.aclose()


def test_pr03_browser_bridge_preserves_exact_control_and_background_read_fields(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_pr03_browser_bridge_fields(monkeypatch))


async def _exercise_pr03_browser_bridge_fields(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(browser_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    command_id = (
        "command:control:9999999999999:44444444-4444-4444-8444-444444444444"
    )

    await bridge.command(
        connection_id,
        {
            "command_id": command_id,
            "command_kind": "TERMINATE_BACKGROUND_PROCESS",
            "expected_session_id": "session:one",
            "expected_host_session_id": "host:session:one",
            "target_process_id": "process:exact",
        },
    )
    kind, request = connection.requests[-1]
    assert kind == "command"
    assert isinstance(request, wire.CommandRequest)
    assert request.client_submission_id == ""
    assert request.expected_session_id == "session:one"
    assert request.expected_host_session_id == "host:session:one"
    assert request.target_process_id == "process:exact"
    assert not request.target_turn_id
    assert not request.subagent_task_id

    await bridge.query_command(
        connection_id,
        {
            "command_id": command_id,
            "expected_control": {
                "operation": "USER_CONTROL_TERMINATE_BACKGROUND_PROCESS",
                "session_id": "session:one",
                "host_session_id": "host:session:one",
                "target_kind": "USER_CONTROL_BACKGROUND_PROCESS",
                "target_id": "process:exact",
            },
        },
    )
    kind, query = connection.requests[-1]
    assert kind == "query_command"
    assert isinstance(query, wire.QueryCommandRequest)
    assert query.expected_control.target.target_id == "process:exact"
    assert query.expected_control.host_session_id == "host:session:one"

    await bridge.list_background_processes(
        connection_id,
        {
            "expected_session_id": "session:one",
            "expected_host_session_id": "host:session:one",
            "cursor": "cursor:page",
            "maximum_items": 7,
        },
    )
    kind, listing = connection.requests[-1]
    assert kind == "list_background_processes"
    assert isinstance(listing, wire.ListBackgroundProcessesRequest)
    assert listing.cursor == "cursor:page"
    assert listing.maximum_items == 7

    await bridge.read_background_process_log(
        connection_id,
        {
            "expected_session_id": "session:one",
            "expected_host_session_id": "host:session:one",
            "process_id": "process:exact",
            "output_cursor": "output:17",
            "max_output_chars": 4096,
        },
    )
    kind, log = connection.requests[-1]
    assert kind == "read_background_process_log"
    assert isinstance(log, wire.ReadBackgroundProcessLogRequest)
    assert log.process_id == "process:exact"
    assert log.output_cursor == "output:17"
    assert log.max_output_chars == 4096

    await bridge.aclose()


def test_u2_browser_bridge_preserves_typed_prompt_order_and_exact_image_read(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_typed_prompt_and_image_read(monkeypatch))


async def _exercise_typed_prompt_and_image_read(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(browser_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    repeated = b"\x89PNG\r\n\x1a\nexact"
    await bridge.command(
        connection_id,
        {
            "command_id": "command:typed",
            "command_kind": "SUBMIT_PROMPT",
            "requested_permission_mode": "PERMISSION_MODE_READ_ONLY",
            "prompt_content": {
                "parts": [
                    {"type": "text", "text": "before\\n[Figure 1]"},
                    {
                        "type": "image",
                        "content_base64": base64.b64encode(repeated).decode("ascii"),
                        "declared_media_type": "image/png",
                    },
                    {"type": "text", "text": "after"},
                    {
                        "type": "image",
                        "content_base64": base64.b64encode(repeated).decode("ascii"),
                        "declared_media_type": "image/png",
                    },
                ]
            },
        },
    )
    kind, request = connection.requests[-1]
    assert kind == "command"
    assert isinstance(request, wire.CommandRequest)
    assert request.command_kind == wire.SUBMIT_PROMPT
    assert request.target_turn_id == ""
    assert [part.WhichOneof("value") for part in request.prompt_content.parts] == [
        "text",
        "image",
        "text",
        "image",
    ]
    assert request.prompt_content.parts[0].text == "before\\n[Figure 1]"
    assert request.prompt_content.parts[1].image.content == repeated
    assert request.prompt_content.parts[1].image.declared_media_type == "image/png"
    assert request.prompt_content.parts[2].text == "after"
    assert request.prompt_content.parts[3].image.content == repeated

    response = await bridge.read_content(
        connection_id,
        {
            "entry_id": "entry:owner",
            "image_ref_ordinal": 2,
            "offset_bytes": 0,
            "limit_bytes": 1 << 20,
        },
    )
    kind, image_request = connection.requests[-1]
    assert kind == "read_content"
    assert isinstance(image_request, wire.ReadContentRequest)
    assert image_request.entry_id == "entry:owner"
    assert image_request.queue_item_id == ""
    assert image_request.HasField("image_ref_ordinal")
    assert image_request.image_ref_ordinal == 2
    assert response["content"]["content"] == base64.b64encode(
        b"exact-image-bytes"
    ).decode("ascii")
    await bridge.aclose()


@pytest.mark.parametrize(
    "body",
    [
        {
            "command_id": "command:legacy-text",
            "command_kind": "SUBMIT_PROMPT",
            "text": "legacy",
        },
        {
            "command_id": "command:side-channel",
            "command_kind": "SUBMIT_PROMPT",
            "prompt_content": {"parts": [{"type": "text", "text": "no"}]},
            "attachments": [],
        },
    ],
)
def test_u2_browser_bridge_does_not_restore_removed_prompt_paths(
    monkeypatch, body: dict[str, object]
) -> None:
    asyncio.run(_exercise_removed_prompt_path(monkeypatch, body))


async def _exercise_removed_prompt_path(monkeypatch, body: dict[str, object]) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(browser_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        LocalBrowserBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = LocalBrowserBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect("session:one", browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    with pytest.raises(ValueError):
        await bridge.command(connection_id, body)
    assert connection.requests == []
    await bridge.aclose()


class _FrameWriter:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.written.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class _FrameReader:
    def __init__(self, response: wire.ServerFrame) -> None:
        payload = response.SerializeToString(deterministic=True)
        self.pending = bytearray(len(payload).to_bytes(4, "big") + payload)

    async def readexactly(self, size: int) -> bytes:
        assert len(self.pending) >= size
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def at_eof(self) -> bool:
        return False


def test_u2_protocol_frame_counts_complete_typed_prompt_at_eight_mib_boundary() -> None:
    asyncio.run(_exercise_protocol_frame_boundary())


async def _exercise_protocol_frame_boundary() -> None:
    frame = wire.ClientFrame(
        command=wire.CommandRequest(
            request_id="request:frame-boundary",
            attachment_id="attachment:one",
            attachment_generation=1,
            command_id="command:frame-boundary",
            client_submission_id="command:frame-boundary",
            command_kind=wire.SUBMIT_PROMPT,
            requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
            prompt_content=wire.PromptContent(
                parts=[
                    wire.PromptContentPart(text="escaping \\\" 中文\n"),
                    wire.PromptContentPart(
                        image=wire.PromptImagePart(
                            content=b"x" * (MAXIMUM_FRAME_BYTES - 256),
                            declared_media_type="image/png",
                        )
                    ),
                ]
            ),
        )
    )
    current_size = len(frame.SerializeToString(deterministic=True))
    delta = MAXIMUM_FRAME_BYTES - current_size
    assert 0 < delta < 256
    frame.command.prompt_content.parts[1].image.content += b"x" * delta
    assert len(frame.SerializeToString(deterministic=True)) == MAXIMUM_FRAME_BYTES

    response = wire.ServerFrame(
        command_outcome=wire.CommandOutcome(
            request_id="request:frame-boundary",
            command_id="command:frame-boundary",
            status=wire.PENDING,
        )
    )
    writer = _FrameWriter()
    client = TerminalProtocolClient(
        reader=cast(asyncio.StreamReader, _FrameReader(response)),
        writer=cast(asyncio.StreamWriter, writer),
        role=wire.ATTACHMENT_ROLE_CONTROLLER,
        server=cast(TerminalKernelProtocolServer, object()),
        session_id="session:one",
        host_session_id="host:one",
    )
    returned = await client._round_trip(frame)
    assert returned.command_outcome.command_id == "command:frame-boundary"
    assert int.from_bytes(writer.written[:4], "big") == MAXIMUM_FRAME_BYTES
    assert len(writer.written) == MAXIMUM_FRAME_BYTES + 4

    frame.command.prompt_content.parts[1].image.content += b"x"
    rejected_writer = _FrameWriter()
    rejected = TerminalProtocolClient(
        reader=cast(asyncio.StreamReader, _FrameReader(response)),
        writer=cast(asyncio.StreamWriter, rejected_writer),
        role=wire.ATTACHMENT_ROLE_CONTROLLER,
        server=cast(TerminalKernelProtocolServer, object()),
        session_id="session:one",
        host_session_id="host:one",
    )
    with pytest.raises(ProtocolBridgeError) as caught:
        await rejected._round_trip(frame)
    assert caught.value.code == "PROTOCOL_FRAME_OUT_OF_BOUNDS"
    assert rejected_writer.written == b""


class _RuntimeReopenHost:
    def __init__(self, session_id: str, host_session_id: str, *, busy: bool = False):
        self.session_id = session_id
        self.host_session_id = host_session_id
        self.writer_generation = 1
        self.busy = busy
        self.gated = False
        self.committed = False
        self.aborted = False

    async def prepare_safe_runtime_reopen(self):
        if self.busy:
            raise RuntimeError("accepted work remains")
        self.gated = True
        return SimpleNamespace(
            session_id=self.session_id,
            host_session_id=self.host_session_id,
            writer_generation=self.writer_generation,
        )

    async def abort_safe_runtime_reopen(self, _quiescence: object) -> None:
        self.gated = False
        self.aborted = True

    async def commit_safe_runtime_reopen(self, _quiescence: object) -> None:
        assert self.gated
        self.committed = True


class _RuntimeReopenCore:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.closed_host_ids: list[str] = []
        self.resume_count = 0

    async def read_resumable_session(self, session_id: str, **_kwargs: object):
        return SimpleNamespace(
            session_id=session_id,
            workspace_kind="project",
            workspace_root=self.workspace_root,
            workspace_label="runtime reopen test",
            memory_domain_id="u_local",
        )

    async def close_session(self, host_session_id: str) -> None:
        self.closed_host_ids.append(host_session_id)

    async def resume_session(self, session_id: str, **_kwargs: object):
        self.resume_count += 1
        return _RuntimeReopenHost(
            session_id,
            f"host:resumed:{self.resume_count}",
        )


class _RuntimeReopenConnection:
    def __init__(self, connection_id: str, session_id: str, *, fail: bool = False):
        self.connection_id = connection_id
        self.session_id = session_id
        self.fail = fail
        self.close_attempted = False

    async def aclose(self) -> None:
        self.close_attempted = True
        if self.fail:
            raise RuntimeError("close failed")


def _runtime_reopen_controller(
    tmp_path,
    *,
    old_host: _RuntimeReopenHost | None,
) -> tuple[LocalSessionController, _RuntimeReopenCore]:
    workspace_input = HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=tmp_path,
        display_label="runtime reopen test",
        memory_domain_id="u_local",
        cleanup_workspace_root_on_close=False,
        trust_workspace_mcp_config=False,
    )
    core = _RuntimeReopenCore(str(tmp_path))
    controller = object.__new__(LocalSessionController)
    controller.core = core
    controller.workspace_input = workspace_input
    controller.permission_policy = cast(object, None)
    controller.active_skill_names = frozenset()
    controller._by_session = {}
    controller._by_host = {}
    controller._operations = {}
    controller._forks = set()
    controller._lock = asyncio.Lock()
    controller._closing = False
    controller._close_task = None
    if old_host is not None:
        handle = HostSessionHandle(cast(object, old_host), workspace_input)
        controller._by_session[old_host.session_id] = handle
        controller._by_host[old_host.host_session_id] = handle
    return controller, core


def test_runtime_reopen_busy_keeps_the_old_host_published(tmp_path) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost("session:busy", "host:busy", busy=True)
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)

        with pytest.raises(SessionControlRejected) as caught:
            await controller.prepare_runtime_reopen(old.session_id)

        assert caught.value.public_code == "RUNTIME_REOPEN_BUSY"
        assert controller._by_session[old.session_id].session is old
        assert controller._operations == {}
        assert core.closed_host_ids == []

    asyncio.run(scenario())


def test_runtime_reopen_full_detaches_closes_and_resumes_exactly_once(tmp_path) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost("session:full", "host:old")
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = LocalBrowserBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        first = _RuntimeReopenConnection("connection:one", old.session_id)
        second = _RuntimeReopenConnection("connection:two", old.session_id)
        bridge._connections = {first.connection_id: first, second.connection_id: second}
        bridge._controller_by_session[old.session_id] = first.connection_id

        operation = await controller.prepare_runtime_reopen(old.session_id)
        assert isinstance(operation, PreparedRuntimeReopenOperation)
        assert controller._by_session[old.session_id].session is old
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFull)
        assert first.close_attempted and second.close_attempted
        assert not bridge._connections

        host_outcome = await controller.prepare_runtime_reopen_close(operation)
        assert isinstance(host_outcome, OldHostCloseFull)
        settlement = await bridge.settle_runtime_reopen_detach(
            detach.token, host_outcome
        )
        assert isinstance(settlement, BridgeSettlementFull)
        observation = await controller.finalize_runtime_reopen(
            operation,
            host_outcome=host_outcome,
            bridge_settlement=settlement,
            bridge_owner=bridge,
        )
        resumed = await controller.resume_session(
            old.session_id,
            no_live_observation=observation,
        )

        assert old.committed
        assert core.closed_host_ids == [old.host_session_id]
        assert core.resume_count == 1
        assert resumed.host_session_id == "host:resumed:1"
        assert controller._by_session[old.session_id] is resumed

    asyncio.run(scenario())


def test_runtime_reopen_without_old_host_uses_distinct_closed_outcome(tmp_path) -> None:
    async def scenario() -> None:
        controller, core = _runtime_reopen_controller(tmp_path, old_host=None)
        bridge = LocalBrowserBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )

        operation = await controller.prepare_runtime_reopen("session:cold")
        assert isinstance(operation, PreparedRuntimeResumeOperation)
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFull)
        host_outcome = await controller.prepare_runtime_reopen_close(operation)
        assert isinstance(host_outcome, NoOldHostReadyToResume)
        settlement = await bridge.settle_runtime_reopen_detach(
            detach.token, host_outcome
        )
        assert isinstance(settlement, BridgeSettlementFull)
        observation = await controller.finalize_runtime_reopen(
            operation,
            host_outcome=host_outcome,
            bridge_settlement=settlement,
            bridge_owner=bridge,
        )
        resumed = await controller.resume_session(
            operation.session_id,
            no_live_observation=observation,
        )

        assert core.closed_host_ids == []
        assert resumed.host_session_id == "host:resumed:1"

    asyncio.run(scenario())


def test_runtime_reopen_abort_after_detach_restores_only_the_exact_old_host(
    tmp_path,
) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost("session:abort", "host:abort")
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = LocalBrowserBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        connection = _RuntimeReopenConnection("connection:abort", old.session_id)
        bridge._connections[connection.connection_id] = connection

        operation = await controller.prepare_runtime_reopen(old.session_id)
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFull)
        await controller.prepare_abort_runtime_reopen(operation)
        settlement = await bridge.settle_runtime_reopen_abort(detach.token)
        assert isinstance(settlement, BridgeSettlementFull)
        await controller.finalize_abort_runtime_reopen(
            operation,
            bridge_settlement=settlement,
            bridge_owner=bridge,
        )

        assert old.aborted and not old.gated and not old.committed
        assert controller._by_session[old.session_id].session is old
        assert core.closed_host_ids == []
        assert controller._operations == {}

    asyncio.run(scenario())


def test_runtime_reopen_connection_close_failure_quarantines_both_owners(
    tmp_path,
) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost("session:failed", "host:failed")
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = LocalBrowserBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        failed = _RuntimeReopenConnection(
            "connection:failed", old.session_id, fail=True
        )
        settled = _RuntimeReopenConnection("connection:settled", old.session_id)
        bridge._connections = {
            failed.connection_id: failed,
            settled.connection_id: settled,
        }

        operation = await controller.prepare_runtime_reopen(old.session_id)
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFailed)
        assert failed.close_attempted and settled.close_attempted
        settlement = await bridge.quarantine_detach_failure(
            detach.token,
            error=detach.error,
        )
        assert isinstance(settlement, BridgeSettlementFailed)
        await controller.quarantine_runtime_reopen(
            operation,
            public_code="RUNTIME_REOPEN_QUARANTINED",
        )

        with pytest.raises(SessionControlRejected) as caught:
            await controller.resume_session(old.session_id)
        assert caught.value.public_code == "RUNTIME_REOPEN_QUARANTINED"
        with pytest.raises(RuntimeError, match="quarantined"):
            await bridge.connect(
                old.session_id,
                browser_instance_id=BROWSER_ONE,
            )
        assert core.closed_host_ids == []

    asyncio.run(scenario())


def test_runtime_reopen_post_detach_exception_quarantines_gate_and_operation(
    tmp_path,
) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost("session:post-token", "host:post-token")
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = LocalBrowserBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        connection = _RuntimeReopenConnection(
            "connection:post-token", old.session_id
        )
        bridge._connections[connection.connection_id] = connection

        async def fail_after_detach(_operation):
            raise RuntimeError("injected post-detach failure")

        controller.prepare_runtime_reopen_close = fail_after_detach  # type: ignore[method-assign]
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("Pulsara", encoding="utf-8")
        runtime = test_model_runtime()

        async def refresh_database_state() -> None:
            return None

        async def unexpected_reset(_postgres):
            raise AssertionError("unexpected reset")

        server = LocalHttpServer(
            sessions=controller,
            bridge=bridge,
            static_root=static_root,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            settings=runtime.settings,
            catalog=runtime.catalog,
            model_runtime=runtime,
            database_state=lambda: "ready",
            refresh_database_state=refresh_database_state,
            postgres_settings_saved=lambda: None,
            reset_postgres=unexpected_reset,
        )
        await server.start()
        try:
            async with ClientSession(cookie_jar=DummyCookieJar()) as client:
                async with client.post(
                    f"{server.origin}/api/sessions/{old.session_id}/runtime/reopen",
                    headers={
                        "Origin": server.origin,
                        "Sec-Fetch-Site": "same-origin",
                    },
                ) as response:
                    assert response.status == 409
                    payload = await response.json()
                    assert payload["error"]["code"] == "RUNTIME_REOPEN_QUARANTINED"
            assert connection.close_attempted
            with pytest.raises(SessionControlRejected) as caught:
                await controller.resume_session(old.session_id)
            assert caught.value.public_code == "RUNTIME_REOPEN_QUARANTINED"
            with pytest.raises(RuntimeError, match="quarantined"):
                await bridge.connect(
                    old.session_id,
                    browser_instance_id=BROWSER_ONE,
                )
            assert core.closed_host_ids == []
        finally:
            await server.aclose()

    asyncio.run(scenario())
