"""Secure local adapter for the renderer-neutral terminal client protocol."""

from __future__ import annotations

import asyncio
import errno
import hmac
import os
import secrets
import socket
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable
from uuid import uuid4

from google.protobuf.message import DecodeError

from pulsara_agent.ports.terminal_application import (
    CancelMcpInteractionRequest,
    CloseSessionRequest,
    ControllerTakeoverRequest,
    DetachSessionRequest,
    QueueCancelRequest,
    ResolveApprovalRequest,
    ResolveMcpInteractionRequest,
    ResolvePlanExitRequest,
    ResolvePlanQuestionRequest,
    StartSuccessorSessionRequest,
    StopRunRequest,
    SubmitPromptRequest,
    TerminalCommandBinding,
)
from pulsara_agent.ports.terminal_presentation import (
    PresentationHistoryCursorStale,
    PresentationHistoryPageData,
    PresentationHistoryPageReadLimits,
    PresentationHistoryRebaseRequired,
    PresentationHistoryReconciliationRequired,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.terminal_application.services import (
    terminal_request_semantic_fingerprint,
)
from pulsara_agent.terminal_protocol.codec import (
    HEARTBEAT_GRACE_MS,
    HEARTBEAT_INTERVAL_MS,
    HEARTBEAT_MAXIMUM_MISSED_COUNT,
    MAXIMUM_FRAME_BYTES,
    MAXIMUM_HISTORY_PAGE_BYTES,
    MAXIMUM_HISTORY_PAGE_CELLS,
    MAXIMUM_OBSERVATION_WAIT_MS,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PROTOCOL_SCHEMA_FINGERPRINT,
    SECRET_FRAME_MAXIMUM_BYTES,
    attachment_to_wire,
    cursor_from_wire,
    cursor_to_wire,
    entry_to_wire,
    outcome_to_wire,
    operational_change_to_wire,
    operational_snapshot_to_wire,
    protocol_version,
    root_advanced_to_wire,
    root_to_wire,
    snapshot_to_wire,
)
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire


SessionProvider = Callable[[str], object]
CloseSession = Callable[[str, bool], Awaitable[None]]

_SERVER_CAPABILITIES = (
    "attachment_controller_v1",
    "command_idempotency_v1",
    "history_page_v1",
    "mcp_secret_lease_v1",
    "operational_cursor_v1",
    "operational_snapshot_v1",
    "presentation_root_advance_v1",
    "projection_snapshot_v1",
)
_MAXIMUM_CONNECTION_INPUT_BYTES = 512 * 1024 * 1024
_MAXIMUM_CONNECTION_OUTPUT_BYTES = 512 * 1024 * 1024
_MAXIMUM_UNIX_SOCKET_PATH_BYTES = 103


@dataclass(slots=True)
class _ConnectionState:
    connection_id: str = field(
        default_factory=lambda: f"terminal-connection:{uuid4().hex}"
    )
    client_instance_id: str | None = None
    requested_role: int | None = None
    attachment_challenge: bytes | None = None
    hello_transcript_fingerprint: str | None = None
    host_session: object | None = None
    attachment_id: str | None = None
    attachment_generation: int | None = None
    root_lease_ids: dict[str, str] = field(default_factory=dict)
    input_bytes: int = 0
    output_bytes: int = 0


class TerminalProtocolServer:
    """Versioned local gateway; all domain decisions remain Python-owned."""

    def __init__(
        self,
        *,
        socket_path: Path,
        session_provider: SessionProvider,
        close_session: CloseSession | None = None,
        maximum_frame_bytes: int = MAXIMUM_FRAME_BYTES,
        launch_capability: bytes | None = None,
    ) -> None:
        self.socket_path = socket_path.expanduser()
        self.session_provider = session_provider
        self.close_session = close_session
        self.maximum_frame_bytes = maximum_frame_bytes
        self.launch_capability = launch_capability or secrets.token_bytes(32)
        if len(self.launch_capability) < 32:
            raise ValueError("terminal launch capability is too short")
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[object]] = set()

    async def start(self) -> None:
        if self._server is not None:
            return
        parent = self.socket_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise PermissionError("terminal protocol runtime directory is unsafe")
        if (
            len(os.fsencode(os.path.abspath(self.socket_path)))
            > _MAXIMUM_UNIX_SOCKET_PATH_BYTES
        ):
            raise ValueError("terminal protocol socket path exceeds the POSIX V1 bound")
        self._remove_stale_socket()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        current = asyncio.current_task()
        pending = tuple(task for task in self._connections if task is not current)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._connections.clear()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _remove_stale_socket(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise PermissionError("terminal protocol path is not an owned socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise RuntimeError(
                    "terminal protocol socket liveness is unknown"
                ) from exc
        else:
            raise RuntimeError("terminal protocol socket is already live")
        finally:
            probe.close()
        self.socket_path.unlink(missing_ok=True)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        state = _ConnectionState()
        try:
            _validate_peer_uid(writer)
            while True:
                request = await _read_frame(
                    reader, wire.ClientFrame, maximum_bytes=self.maximum_frame_bytes
                )
                request_bytes = request.ByteSize() + 4
                state.input_bytes += request_bytes
                if state.input_bytes > _MAXIMUM_CONNECTION_INPUT_BYTES:
                    raise ValueError("terminal protocol input budget is exhausted")
                if (
                    request.WhichOneof("request")
                    in {"secret_reveal", "secret_form_submit"}
                    and request_bytes > SECRET_FRAME_MAXIMUM_BYTES
                ):
                    raise ValueError("terminal secret frame exceeds its hard bound")
                response = await self._dispatch(request, state)
                response_bytes = response.ByteSize() + 4
                state.output_bytes += response_bytes
                if state.output_bytes > _MAXIMUM_CONNECTION_OUTPUT_BYTES:
                    raise ValueError("terminal protocol output budget is exhausted")
                if (
                    response.WhichOneof("response")
                    in {"secret_reveal", "secret_submit", "secret_revoked"}
                    and response_bytes > SECRET_FRAME_MAXIMUM_BYTES
                ):
                    raise ValueError("terminal secret response exceeds its hard bound")
                await _write_frame(
                    writer, response, maximum_bytes=self.maximum_frame_bytes
                )
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            try:
                await _write_frame(
                    writer,
                    _error_frame(
                        request_id="",
                        code=f"PROTOCOL_{type(exc).__name__.upper()}",
                        message="The terminal protocol request was rejected.",
                    ),
                    maximum_bytes=self.maximum_frame_bytes,
                )
            except BaseException:
                pass
        finally:
            self._detach_connection_state(state)
            writer.close()
            try:
                await writer.wait_closed()
            except BaseException:
                pass
            if task is not None:
                self._connections.discard(task)

    def _detach_connection_state(self, state: _ConnectionState) -> None:
        if state.host_session is None or state.attachment_id is None:
            return
        services = state.host_session.terminal_application_services
        foundation = state.host_session.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        foundation.retention_owner.release_attachment(state.attachment_id)
        services.secrets.revoke_attachment(state.attachment_id)
        try:
            services.attachments.detach(
                attachment_id=state.attachment_id,
                attachment_generation=state.attachment_generation or 0,
            )
        except BaseException:
            pass

    async def _dispatch(
        self, frame: wire.ClientFrame, state: _ConnectionState
    ) -> wire.ServerFrame:
        branch = frame.WhichOneof("request")
        if branch == "hello":
            return self._hello(frame.hello, state)
        if state.client_instance_id is None:
            raise PermissionError("terminal hello must be first")
        if branch == "attach":
            return self._attach(frame.attach, state)
        host = _require_attached(state)
        services = host.terminal_application_services
        services.attachments.validate_attachment(_state_binding(state, host))
        if branch == "heartbeat":
            request = frame.heartbeat
            _require_attachment(request, state)
            lease = services.attachments.heartbeat(
                attachment_id=request.attachment_id,
                attachment_generation=request.attachment_generation,
            )
            self._renew_root_leases(state, host)
            return wire.ServerFrame(
                heartbeat=wire.HeartbeatResponse(
                    request_id=request.request_id,
                    attachment=attachment_to_wire(lease),
                )
            )
        if branch == "snapshot":
            snapshot = services.query.snapshot()
            self._borrow_root(
                state, host, snapshot.viewport.active_head.confirmed_root_identity
            )
            return wire.ServerFrame(
                snapshot=snapshot_to_wire(
                    snapshot, request_id=frame.snapshot.request_id
                )
            )
        if branch == "operational_snapshot":
            snapshot = host.wiring.runtime_wiring.runtime_session.ui_operational_activity_store.snapshot()
            return wire.ServerFrame(
                operational_snapshot=operational_snapshot_to_wire(
                    snapshot,
                    request_id=frame.operational_snapshot.request_id,
                )
            )
        if branch == "observe_next":
            return await self._observe_next(frame.observe_next, state, host)
        if branch == "history_page":
            return await self._history_page(frame.history_page, state, host)
        if branch == "mutation":
            request = _mutation_from_wire(frame.mutation)
            _require_binding_connection(request.binding, state)
            outcome = await _execute_mutation(request, services)
            if (
                isinstance(request, DetachSessionRequest)
                and outcome.status == "succeeded"
            ):
                foundation = host.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
                foundation.retention_owner.release_attachment(
                    request.binding.attachment_id
                )
                services.secrets.revoke_attachment(request.binding.attachment_id)
                state.host_session = None
                state.attachment_id = None
                state.attachment_generation = None
                state.root_lease_ids.clear()
            return wire.ServerFrame(
                command_outcome=outcome_to_wire(
                    outcome, request_id=frame.mutation.request_id
                )
            )
        if branch == "query_command":
            request = frame.query_command
            if (
                request.runtime_session_id != host.runtime_session_id
                or request.original_client_instance_id != state.client_instance_id
            ):
                raise PermissionError("terminal command query identity is stale")
            outcome = await services.commands.query(
                client_instance_id=state.client_instance_id,
                command_id=request.command_id,
            )
            response = wire.QueryCommandResponse(
                request_id=request.request_id, found=outcome is not None
            )
            if outcome is not None:
                response.outcome.CopyFrom(
                    outcome_to_wire(outcome, request_id=request.request_id)
                )
            return wire.ServerFrame(query_command=response)
        if branch == "secret_reveal":
            request = frame.secret_reveal
            binding = _binding_from_wire(request.binding)
            _require_binding_connection(binding, state)
            lease = services.secrets.issue_url_reveal_lease(
                binding=binding,
                interaction_id=request.interaction_id,
                request_key=request.request_key,
            )
            private_url = services.secrets.reveal_url_once(
                lease_identity_fingerprint=lease.identity_fingerprint,
                binding=binding,
            )
            return wire.ServerFrame(
                secret_reveal=wire.SecretRevealResult(
                    request_id=request.request_id,
                    lease_identity=wire.TerminalSecretLeaseIdentity(
                        attachment_id=lease.attachment_id,
                        attachment_generation=lease.attachment_generation,
                        controller_generation=lease.controller_generation,
                        interaction_id=lease.interaction_id,
                        request_key=lease.request_key,
                        secret_kind=lease.secret_kind,
                        owner_epoch=lease.owner_epoch,
                        lease_generation=lease.lease_generation,
                        expires_at_utc=lease.expires_at_utc,
                        identity_fingerprint=lease.identity_fingerprint,
                    ),
                    private_url=private_url,
                )
            )
        if branch == "secret_form_submit":
            request = frame.secret_form_submit
            binding = _binding_from_wire(request.binding)
            _require_binding_connection(binding, state)
            handle = services.secrets.seal_form_response(
                binding=binding,
                interaction_id=request.interaction_id,
                request_key=request.request_key,
                plaintext_json=bytes(request.response_json),
            )
            return wire.ServerFrame(
                secret_submit=wire.SecretSubmitReceipt(
                    request_id=request.request_id,
                    sealed_response_handle_id=handle,
                )
            )
        raise ValueError("terminal client frame branch is unknown")

    def _hello(self, request, state: _ConnectionState) -> wire.ServerFrame:
        if state.client_instance_id is not None:
            raise ValueError("terminal hello is duplicated")
        version = request.supported_version_range
        role = request.requested_attachment_mode
        if (
            version.major != PROTOCOL_MAJOR
            or version.minimum_minor > PROTOCOL_MINOR
            or version.maximum_minor < PROTOCOL_MINOR
            or version.schema_contract_fingerprint != PROTOCOL_SCHEMA_FINGERPRINT
            or role
            not in {
                wire.ATTACHMENT_ROLE_OBSERVER,
                wire.ATTACHMENT_ROLE_CONTROLLER,
            }
            or not request.client_instance_id
            or not hmac.compare_digest(
                bytes(request.launch_capability), self.launch_capability
            )
        ):
            return _error_frame(
                request_id=request.request_id,
                code="PROTOCOL_NEGOTIATION_REJECTED",
                message="The protocol version or launch authority is incompatible.",
            )
        challenge = secrets.token_bytes(32)
        transcript = context_fingerprint(
            "terminal-protocol-hello-transcript:v1",
            {
                "request_id": request.request_id,
                "client_instance_id": request.client_instance_id,
                "client_build_identity": request.client_build_identity,
                "supported_capabilities": tuple(request.supported_capabilities),
                "requested_attachment_mode": role,
                "selected_protocol": {
                    "major": PROTOCOL_MAJOR,
                    "minor": PROTOCOL_MINOR,
                    "schema_contract_fingerprint": PROTOCOL_SCHEMA_FINGERPRINT,
                },
                "attachment_challenge_sha256": context_fingerprint(
                    "terminal-attachment-challenge:v1", challenge.hex()
                ),
            },
        )
        state.client_instance_id = request.client_instance_id
        state.requested_role = role
        state.attachment_challenge = challenge
        state.hello_transcript_fingerprint = transcript
        return wire.ServerFrame(
            hello=wire.HelloResponse(
                request_id=request.request_id,
                selected_protocol=protocol_version(),
                server_build_identity="pulsara-python-terminal-foundation:v1",
                server_runtime_identity=f"process:{os.getpid()}",
                negotiated_limits=wire.NegotiatedLimits(
                    maximum_frame_bytes=self.maximum_frame_bytes,
                    maximum_history_page_cells=MAXIMUM_HISTORY_PAGE_CELLS,
                    maximum_history_page_decoded_bytes=MAXIMUM_HISTORY_PAGE_BYTES,
                    maximum_observation_wait_ms=MAXIMUM_OBSERVATION_WAIT_MS,
                    secret_frame_maximum_bytes=SECRET_FRAME_MAXIMUM_BYTES,
                ),
                attachment_challenge=challenge,
                supported_capabilities=_SERVER_CAPABILITIES,
                hello_transcript_fingerprint=transcript,
            )
        )

    def _attach(self, request, state: _ConnectionState) -> wire.ServerFrame:
        if state.host_session is not None:
            raise ValueError("terminal connection is already attached")
        if (
            request.hello_transcript_fingerprint != state.hello_transcript_fingerprint
            or not hmac.compare_digest(
                bytes(request.attachment_challenge),
                state.attachment_challenge or b"",
            )
            or request.requested_role != state.requested_role
        ):
            raise PermissionError("terminal attach proof is stale")
        host = self.session_provider(request.host_session_id)
        services = host.terminal_application_services
        lease = services.attachments.attach(
            connection_id=state.connection_id,
            client_instance_id=state.client_instance_id or "",
            request_controller=(
                request.requested_role == wire.ATTACHMENT_ROLE_CONTROLLER
            ),
        )
        foundation = host.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        foundation.start_background_if_possible()
        state.host_session = host
        state.attachment_id = lease.attachment_id
        state.attachment_generation = lease.attachment_generation
        disposition = (
            "controller_granted"
            if lease.role == "controller"
            else (
                "controller_unavailable_observer_attached"
                if request.requested_role == wire.ATTACHMENT_ROLE_CONTROLLER
                else "observer_attached"
            )
        )
        return wire.ServerFrame(
            attach=wire.AttachResponse(
                request_id=request.request_id,
                attachment=attachment_to_wire(lease),
                controller_disposition=disposition,
                bootstrap_requirement="projection_snapshot_required",
                heartbeat_policy=wire.HeartbeatPolicy(
                    interval_ms=HEARTBEAT_INTERVAL_MS,
                    grace_ms=HEARTBEAT_GRACE_MS,
                    maximum_missed_count=HEARTBEAT_MAXIMUM_MISSED_COUNT,
                ),
            )
        )

    def _borrow_root(self, state: _ConnectionState, host, root_identity) -> None:
        fingerprint = root_identity.root_identity_fingerprint
        foundation = host.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        ttl_seconds = min(
            30.0,
            float(
                host.wiring.runtime_wiring.runtime_session.presentation_history_materialization_policy.root_retention_ttl_seconds
            ),
        )
        existing = state.root_lease_ids.get(fingerprint)
        if existing is not None and foundation.retention_owner.renew(
            existing, ttl_seconds=ttl_seconds
        ):
            return
        state.root_lease_ids.pop(fingerprint, None)
        lease = foundation.retention_owner.borrow(
            attachment_id=state.attachment_id or "",
            root_identity_fingerprint=fingerprint,
            ttl_seconds=ttl_seconds,
        )
        state.root_lease_ids[fingerprint] = lease.lease_id

    def _renew_root_leases(self, state: _ConnectionState, host) -> None:
        foundation = host.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        ttl_seconds = min(
            30.0,
            float(
                host.wiring.runtime_wiring.runtime_session.presentation_history_materialization_policy.root_retention_ttl_seconds
            ),
        )
        for fingerprint, lease_id in tuple(state.root_lease_ids.items()):
            if not foundation.retention_owner.renew(lease_id, ttl_seconds=ttl_seconds):
                state.root_lease_ids.pop(fingerprint, None)

    async def _observe_next(self, request, state, host) -> wire.ServerFrame:
        maximum_wait = (
            min(max(request.maximum_wait_ms, 1), MAXIMUM_OBSERVATION_WAIT_MS) / 1000
        )
        deadline = monotonic() + maximum_wait
        runtime = host.wiring.runtime_wiring.runtime_session
        foundation = runtime.terminal_presentation_foundation_service
        while True:
            operational = runtime.ui_operational_activity_store.read_after(
                operational_generation=request.after_operational_generation,
                operational_cursor=request.after_operational_cursor,
            )
            operational_generation = operational.operational_generation
            operational_cursor = operational.operational_cursor
            read = foundation.read_observation_after(
                projection_revision=request.after_projection_revision
            )
            if (
                request.after_authority_high_water > read.latest_authority_high_water
                or request.after_projection_revision > read.latest_projection_revision
                or request.after_operational_generation > operational_generation
                or (
                    request.after_operational_generation == operational_generation
                    and request.after_operational_cursor > operational_cursor
                )
            ):
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        gap=wire.ObservationGap(
                            request_id=request.request_id,
                            latest_authority_high_water=(
                                read.latest_authority_high_water
                            ),
                            latest_projection_revision=(
                                read.latest_projection_revision
                            ),
                            latest_operational_generation=operational_generation,
                            latest_operational_cursor=operational_cursor,
                            reason="client_cursor_ahead",
                        )
                    )
                )
            if read.status == "gap":
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        gap=wire.ObservationGap(
                            request_id=request.request_id,
                            latest_authority_high_water=(
                                read.latest_authority_high_water
                            ),
                            latest_projection_revision=(
                                read.latest_projection_revision
                            ),
                            latest_operational_generation=operational_generation,
                            latest_operational_cursor=operational_cursor,
                            reason="projection_transition_evicted",
                        )
                    )
                )
            if operational.status == "gap":
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        gap=wire.ObservationGap(
                            request_id=request.request_id,
                            latest_authority_high_water=(
                                read.latest_authority_high_water
                            ),
                            latest_projection_revision=(
                                read.latest_projection_revision
                            ),
                            latest_operational_generation=operational_generation,
                            latest_operational_cursor=operational_cursor,
                            reason="operational_cursor_gap",
                        )
                    )
                )
            if read.status == "next":
                assert read.root_advanced is not None
                self._borrow_root(
                    state,
                    host,
                    read.root_advanced.resulting_active_head.confirmed_root_identity,
                )
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        root_advanced=root_advanced_to_wire(
                            read.root_advanced, request_id=request.request_id
                        )
                    )
                )
            if operational.status == "next":
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        operational_delta=wire.OperationalDeltaFrame(
                            request_id=request.request_id,
                            operational_generation=operational_generation,
                            operational_cursor=operational_cursor,
                            ordered_changes=(
                                operational_change_to_wire(item)
                                for item in operational.ordered_changes
                            ),
                        )
                    )
                )
            if monotonic() >= deadline:
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(
                        no_change=wire.ObservationNoChange(
                            request_id=request.request_id
                        )
                    )
                )
            await asyncio.sleep(min(0.025, max(0.0, deadline - monotonic())))

    async def _history_page(self, request, state, host) -> wire.ServerFrame:
        runtime = host.wiring.runtime_wiring.runtime_session
        service = runtime.terminal_presentation_foundation_service
        if request.runtime_session_id != host.runtime_session_id:
            raise PermissionError("terminal history request crosses sessions")
        cursor = cursor_from_wire(request.cursor, foundation_service=service)
        if cursor is None:
            latest = service.snapshot().active_head.confirmed_root_identity
            self._borrow_root(state, host, latest)
            return wire.ServerFrame(
                history_page=wire.HistoryPageResponse(
                    stale=wire.HistoryCursorStale(
                        request_id=request.request_id,
                        requested_cursor_fingerprint=(
                            request.cursor.cursor_fingerprint
                        ),
                        latest_root_identity=root_to_wire(latest),
                        response_fingerprint=context_fingerprint(
                            "terminal-history-stale-wire:v1",
                            {
                                "cursor": request.cursor.cursor_fingerprint,
                                "latest": latest.root_identity_fingerprint,
                            },
                        ),
                    )
                )
            )
        if (
            request.expected_projection_contract_fingerprint
            != cursor.history_root_identity.history_projection_contract_fingerprint
        ):
            raise ValueError("terminal history projection contract mismatch")
        self._borrow_root(state, host, cursor.history_root_identity)
        direction = {
            wire.HistoryPageRequest.BEFORE: "before",
            wire.HistoryPageRequest.AFTER: "after",
        }.get(request.direction)
        if direction is None:
            raise ValueError("terminal history direction is unspecified")
        absolute_deadline = monotonic() + 5.0
        outcome = await service.read_history_page_async(
            cursor=cursor,
            direction=direction,
            limits=PresentationHistoryPageReadLimits(
                maximum_entries=min(
                    max(request.maximum_cells, 1), MAXIMUM_HISTORY_PAGE_CELLS
                ),
                maximum_canonical_bytes=min(
                    max(request.maximum_decoded_bytes, 1),
                    MAXIMUM_HISTORY_PAGE_BYTES,
                ),
                maximum_rendered_bytes=min(
                    max(request.maximum_decoded_bytes, 1),
                    MAXIMUM_HISTORY_PAGE_BYTES,
                ),
                maximum_node_reads=runtime.presentation_history_materialization_policy.read_max_node_reads,
                maximum_tree_height=runtime.presentation_history_materialization_policy.read_max_tree_height,
            ),
            absolute_deadline=absolute_deadline,
        )
        return wire.ServerFrame(
            history_page=_history_outcome_to_wire(outcome, request.request_id)
        )


def _history_outcome_to_wire(outcome, request_id: str) -> wire.HistoryPageResponse:
    if isinstance(outcome, PresentationHistoryPageData):
        page = wire.HistoryPageData(
            request_id=request_id,
            validated_input_cursor_fingerprint=(
                outcome.validated_input_cursor_fingerprint
            ),
            validated_request_direction=(
                wire.HistoryPageRequest.BEFORE
                if outcome.validated_request_direction == "before"
                else wire.HistoryPageRequest.AFTER
            ),
            validated_root_identity=root_to_wire(outcome.validated_root_identity),
            ordered_history_entries=(
                entry_to_wire(item) for item in outcome.ordered_history_entries
            ),
            ordered_history_entry_accumulator=(
                outcome.ordered_history_entry_accumulator
            ),
            continuity_proof_fingerprint=outcome.continuity_proof_fingerprint,
            has_more_before=outcome.has_more_before,
            has_more_after=outcome.has_more_after,
            response_fingerprint=outcome.response_fingerprint,
        )
        if outcome.before_cursor is not None:
            page.before_cursor.CopyFrom(cursor_to_wire(outcome.before_cursor))
        if outcome.after_cursor is not None:
            page.after_cursor.CopyFrom(cursor_to_wire(outcome.after_cursor))
        return wire.HistoryPageResponse(page=page)
    if isinstance(outcome, PresentationHistoryCursorStale):
        stale = wire.HistoryCursorStale(
            request_id=request_id,
            requested_cursor_fingerprint=outcome.requested_cursor_fingerprint,
            latest_root_identity=root_to_wire(outcome.latest_root_identity),
            response_fingerprint=outcome.response_fingerprint,
        )
        if outcome.replacement_cursor is not None:
            stale.replacement_cursor.CopyFrom(
                cursor_to_wire(outcome.replacement_cursor)
            )
            stale.replacement_cursor_anchor_proof_fingerprint = (
                outcome.replacement_cursor_anchor_proof_fingerprint or ""
            )
        return wire.HistoryPageResponse(stale=stale)
    if isinstance(outcome, PresentationHistoryRebaseRequired):
        return wire.HistoryPageResponse(
            rebase=wire.HistoryRebaseRequired(
                request_id=request_id,
                requested_cursor_fingerprint=outcome.requested_cursor_fingerprint,
                latest_root_identity=root_to_wire(outcome.latest_root_identity),
                bounded_snapshot_or_rebase_token=(
                    outcome.bounded_snapshot_or_rebase_token
                ),
                response_fingerprint=outcome.response_fingerprint,
            )
        )
    if isinstance(outcome, PresentationHistoryReconciliationRequired):
        item = wire.HistoryReconciliationRequired(
            request_id=request_id,
            requested_cursor_fingerprint=outcome.requested_cursor_fingerprint,
            fault_code=outcome.fault_code,
            reconciliation_owner_identity=outcome.reconciliation_owner_identity,
            response_fingerprint=outcome.response_fingerprint,
        )
        if outcome.retry_after_ms is not None:
            item.retry_after_ms = outcome.retry_after_ms
        if outcome.trusted_latest_root_identity_hint is not None:
            item.trusted_latest_root_identity_hint.CopyFrom(
                root_to_wire(outcome.trusted_latest_root_identity_hint)
            )
        return wire.HistoryPageResponse(reconciliation=item)
    raise TypeError("unknown presentation history outcome")


def _binding_from_wire(item) -> TerminalCommandBinding:
    return TerminalCommandBinding(
        client_instance_id=item.client_instance_id,
        attachment_id=item.attachment_id,
        attachment_generation=item.attachment_generation,
        command_id=item.command_id,
        runtime_session_id=item.runtime_session_id,
        expected_target_id=item.expected_target_id,
        expected_target_generation=item.expected_target_generation,
        expected_controller_generation=item.expected_controller_generation,
        request_semantic_fingerprint=item.request_semantic_fingerprint,
    )


def _mutation_from_wire(item):
    branch = item.WhichOneof("command")
    body = getattr(item, branch) if branch else None
    if body is None:
        raise ValueError("terminal mutation branch is unspecified")
    binding = _binding_from_wire(body.binding)
    common = {
        "binding": binding,
        "request_fingerprint": binding.request_semantic_fingerprint,
    }
    if branch == "submit_prompt":
        mode = {
            wire.SubmitPromptCommand.AUTO: "auto",
            wire.SubmitPromptCommand.STEER: "steer",
            wire.SubmitPromptCommand.FOLLOW_UP: "follow_up",
        }.get(body.requested_delivery_mode)
        if mode is None:
            raise ValueError("terminal prompt delivery mode is unspecified")
        request = SubmitPromptRequest(
            command_kind="submit_prompt",
            client_submission_id=body.client_submission_id,
            text=body.text,
            requested_delivery_mode=mode,
            **common,
        )
    elif branch == "stop_run":
        request = StopRunRequest(command_kind="stop_run", reason="user_stop", **common)
    elif branch == "resolve_approval":
        request = ResolveApprovalRequest(
            command_kind="resolve_approval",
            approval_id=body.approval_id,
            decisions=tuple(
                (value.tool_call_id, value.confirmed) for value in body.decisions
            ),
            **common,
        )
    elif branch == "resolve_plan_question":
        request = ResolvePlanQuestionRequest(
            command_kind="resolve_plan_question",
            interaction_id=body.interaction_id,
            answer_text=body.answer_text,
            selected_option=(
                body.selected_option if body.HasField("selected_option") else None
            ),
            **common,
        )
    elif branch == "resolve_plan_exit":
        if body.decision not in {"approve", "revise", "cancel"}:
            raise ValueError("terminal plan-exit decision is unknown")
        request = ResolvePlanExitRequest(
            command_kind="resolve_plan_exit",
            interaction_id=body.interaction_id,
            decision=body.decision,
            user_feedback=body.user_feedback,
            **common,
        )
    elif branch == "resolve_mcp_interaction":
        request = ResolveMcpInteractionRequest(
            command_kind="resolve_mcp_interaction",
            interaction_id=body.interaction_id,
            sealed_response_handle_id=body.sealed_response_handle_id,
            **common,
        )
    elif branch == "cancel_mcp_interaction":
        request = CancelMcpInteractionRequest(
            command_kind="cancel_mcp_interaction",
            interaction_id=body.interaction_id,
            **common,
        )
    elif branch == "queue_cancel":
        request = QueueCancelRequest(
            command_kind="queue_cancel", queue_item_id=body.queue_item_id, **common
        )
    elif branch == "start_successor_session":
        request = StartSuccessorSessionRequest(
            command_kind="start_successor_session",
            source_capacity_state_fingerprint=(body.source_capacity_state_fingerprint),
            **common,
        )
    elif branch == "detach_session":
        request = DetachSessionRequest(command_kind="detach_session", **common)
    elif branch == "close_session":
        request = CloseSessionRequest(
            command_kind="close_session",
            close_conversation=body.close_conversation,
            **common,
        )
    elif branch == "controller_takeover":
        request = ControllerTakeoverRequest(
            command_kind="controller_takeover",
            expected_previous_controller_generation=(
                body.expected_previous_controller_generation
            ),
            **common,
        )
    else:
        raise ValueError("terminal mutation branch is unknown")
    if (
        terminal_request_semantic_fingerprint(request)
        != binding.request_semantic_fingerprint
    ):
        raise ValueError("terminal mutation semantic fingerprint mismatch")
    return request


async def _execute_mutation(request, services):
    if isinstance(request, SubmitPromptRequest):
        return await services.prompt_submission.submit(request)
    if isinstance(request, StopRunRequest):
        return await services.run_control.stop(request)
    if isinstance(
        request,
        (
            ResolveApprovalRequest,
            ResolvePlanQuestionRequest,
            ResolvePlanExitRequest,
            ResolveMcpInteractionRequest,
            CancelMcpInteractionRequest,
        ),
    ):
        return await services.interaction.resolve(request)
    if isinstance(request, QueueCancelRequest):
        return await services.queue.cancel(request)
    if isinstance(request, StartSuccessorSessionRequest):
        return await services.lifecycle.start_successor(request)
    if isinstance(request, DetachSessionRequest):
        return await services.lifecycle.detach(request)
    if isinstance(request, CloseSessionRequest):
        return await services.lifecycle.close(request)
    if isinstance(request, ControllerTakeoverRequest):
        return await services.lifecycle.takeover(request)
    raise TypeError("terminal mutation request is unknown")


def _state_binding(state: _ConnectionState, host) -> TerminalCommandBinding:
    return TerminalCommandBinding(
        client_instance_id=state.client_instance_id or "",
        attachment_id=state.attachment_id or "",
        attachment_generation=state.attachment_generation or 0,
        command_id="terminal-connection-validation",
        runtime_session_id=host.runtime_session_id,
        expected_target_id=host.runtime_session_id,
        expected_target_generation=1,
        expected_controller_generation=max(
            1, host.terminal_application_services.attachments.controller_generation
        ),
        request_semantic_fingerprint=PROTOCOL_SCHEMA_FINGERPRINT,
    )


def _require_binding_connection(
    binding: TerminalCommandBinding, state: _ConnectionState
) -> None:
    if (
        binding.client_instance_id != state.client_instance_id
        or binding.attachment_id != state.attachment_id
        or binding.attachment_generation != state.attachment_generation
    ):
        raise PermissionError("terminal mutation crosses connection ownership")


def _require_attached(state: _ConnectionState):
    if state.host_session is None or state.attachment_id is None:
        raise PermissionError("terminal connection is not attached")
    return state.host_session


def _require_attachment(request, state: _ConnectionState) -> None:
    if (
        request.attachment_id != state.attachment_id
        or request.attachment_generation != state.attachment_generation
    ):
        raise PermissionError("terminal attachment identity is stale")


async def _read_frame(reader, message_type, *, maximum_bytes: int):
    header = await reader.readexactly(4)
    size = int.from_bytes(header, "big")
    if size <= 0 or size > maximum_bytes:
        raise ValueError("terminal protocol frame length is invalid")
    payload = await reader.readexactly(size)
    message = message_type()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError("terminal protocol frame is malformed") from exc
    if message.WhichOneof("request") is None:
        raise ValueError("terminal protocol frame has no request branch")
    return message


async def _write_frame(writer, message, *, maximum_bytes: int) -> None:
    payload = message.SerializeToString(deterministic=True)
    if not payload or len(payload) > maximum_bytes:
        raise ValueError("terminal protocol response frame is invalid")
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


def _validate_peer_uid(writer: asyncio.StreamWriter) -> None:
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None:
        raise PermissionError("terminal protocol peer socket is unavailable")
    if hasattr(transport_socket, "getpeereid"):
        uid, _ = transport_socket.getpeereid()
    elif hasattr(socket, "LOCAL_PEERCRED"):
        credentials = transport_socket.getsockopt(
            getattr(socket, "SOL_LOCAL", 0), socket.LOCAL_PEERCRED, 12
        )
        if len(credentials) < 8:
            raise PermissionError("terminal protocol peer credential is truncated")
        uid = int.from_bytes(credentials[4:8], sys.byteorder)
    elif hasattr(socket, "SO_PEERCRED"):
        import struct

        credentials = transport_socket.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _, uid, _ = struct.unpack("3i", credentials)
    else:  # pragma: no cover - supported production platforms expose one branch
        raise PermissionError("terminal protocol cannot validate peer UID")
    if uid != os.getuid():
        raise PermissionError("terminal protocol peer UID mismatch")


def _error_frame(*, request_id: str, code: str, message: str) -> wire.ServerFrame:
    return wire.ServerFrame(
        error=wire.ProtocolError(
            request_id=request_id,
            stable_code=code[:128],
            public_message=message[:512],
        )
    )


__all__ = ["TerminalProtocolServer"]
