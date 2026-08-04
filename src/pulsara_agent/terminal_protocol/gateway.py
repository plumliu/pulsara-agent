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
from datetime import UTC, datetime
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
    fit_projection_snapshot_bundle_resident_suffix,
    terminal_request_semantic_fingerprint,
)
from pulsara_agent.runtime.terminal_application.control_projection import (
    ControlProjectionCursor,
)
from pulsara_agent.runtime.terminal_presentation.public_text import (
    bounded_terminal_safe_public_text,
)
from pulsara_agent.terminal_protocol.codec import (
    HEARTBEAT_GRACE_MS,
    HEARTBEAT_INTERVAL_MS,
    HEARTBEAT_MAXIMUM_MISSED_COUNT,
    MAXIMUM_FRAME_BYTES,
    MAXIMUM_ACTIVE_QUEUE_ITEMS,
    MAXIMUM_CONTROL_OBSERVATION_BYTES,
    MAXIMUM_DURABLE_OBSERVATION_BYTES,
    MAXIMUM_HISTORY_PAGE_BYTES,
    MAXIMUM_HISTORY_PAGE_CELLS,
    MAXIMUM_PINNED_HISTORY_ROOTS,
    MAXIMUM_OBSERVATION_WAIT_MS,
    MAXIMUM_OBSERVATION_BATCH_BYTES,
    MAXIMUM_OPERATIONAL_ACTIVITY_CELLS,
    MAXIMUM_OPERATIONAL_OBSERVATION_BYTES,
    MAXIMUM_SERVER_CONTROL_NOTIFICATIONS,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PROTOCOL_SCHEMA_FINGERPRINT,
    SECRET_FRAME_MAXIMUM_BYTES,
    attachment_to_wire,
    control_cursor_to_wire,
    cursor_from_wire,
    cursor_to_wire,
    entry_to_wire,
    history_ranked_entry_vector_decoded_bytes,
    outcome_to_wire,
    operational_change_to_wire,
    operational_snapshot_to_wire,
    protocol_version,
    root_advanced_to_wire,
    root_to_wire,
    snapshot_to_wire,
)
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
from pulsara_agent.terminal_protocol.generated.terminal_client_fingerprint import (
    attachment_challenge_commitment,
    install_protobuf_fingerprint,
    validate_protobuf_fingerprint,
)
from pulsara_agent.terminal_protocol.transport_auth import (
    PREFACE_DEADLINE_SECONDS,
    PREFACE_MAXIMUM_BYTES,
    TerminalTransportAuthOwner,
)


SessionProvider = Callable[[str], object]
CloseSession = Callable[[str, bool], Awaitable[None]]

_SERVER_CAPABILITIES = tuple(
    sorted(
        (
            wire.PRESENTATION_SNAPSHOT_V1,
            wire.OPERATIONAL_SNAPSHOT_V1,
            wire.BOOTSTRAP_CARRIER_V1,
            wire.LAUNCH_AUTH_PREFACE_V1,
            wire.ATTACH_ACK_V1,
            wire.HISTORY_PAGE_V1,
            wire.OBSERVATION_STREAM_V1,
            wire.ROOT_ADVANCE_V1,
            wire.GAP_REBUILD_V1,
            wire.CONTROL_PROJECTION_OBSERVATION_V1,
            wire.RECONNECT_AUTH_ROTATION_V1,
            wire.CONTROLLER_COMMAND_V1,
            wire.COMMAND_QUERY_V1,
            wire.TYPED_INTERACTION_ACTIONS_V1,
            wire.SECRET_FORM_V1,
            wire.SECRET_PRIVATE_URL_V1,
            wire.SECRET_REVOKE_V1,
            wire.PROMPT_QUEUE_MUTATION_V1,
            wire.SESSION_SUCCESSOR_V1,
        )
    )
)
_S1_REQUIRED_CAPABILITIES = frozenset(
    {
        wire.PRESENTATION_SNAPSHOT_V1,
        wire.OPERATIONAL_SNAPSHOT_V1,
        wire.BOOTSTRAP_CARRIER_V1,
        wire.LAUNCH_AUTH_PREFACE_V1,
        wire.ATTACH_ACK_V1,
    }
)
_MAXIMUM_CONNECTION_INPUT_BYTES = 512 * 1024 * 1024
_MAXIMUM_CONNECTION_OUTPUT_BYTES = 512 * 1024 * 1024
_MAXIMUM_UNIX_SOCKET_PATH_BYTES = 103
_PLAN_EXIT_DECISION_FROM_WIRE = {
    wire.APPROVE: "approve",
    wire.REVISE: "revise",
    wire.CANCEL: "cancel",
}
_TERMINAL_SECRET_KIND_TO_WIRE = {
    "private_url": wire.PRIVATE_URL,
    "form_response": wire.FORM_RESPONSE,
}


def _closed_wire_value(mapping: dict[str, int], value: str, *, field: str) -> int:
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"unknown {field}: {value!r}") from exc


@dataclass(slots=True)
class _ConnectionState:
    connection_id: str = field(
        default_factory=lambda: f"terminal-connection:{uuid4().hex}"
    )
    client_instance_id: str | None = None
    requested_role: int | None = None
    attachment_challenge: bytes | None = None
    transport_auth_result: wire.TerminalTransportAuthResult | None = None
    handshake_candidate: wire.HandshakeRecoveryCandidateIdentity | None = None
    hello_negotiation_winner: wire.HelloNegotiationSemanticWinner | None = None
    hello_receipt: wire.ServerHelloReceipt | None = None
    attach_semantic_winner: wire.AttachSemanticWinner | None = None
    attach_result_receipt: wire.AttachResultReceipt | None = None
    transport_binding: wire.TerminalClientTransportBindingIdentity | None = None
    attachment_acknowledged: bool = False
    accepted_heartbeat_generation: int = 0
    selected_capabilities: frozenset[int] = field(default_factory=frozenset)
    host_session: object | None = None
    attachment_id: str | None = None
    attachment_generation: int | None = None
    root_lease_ids: dict[str, str] = field(default_factory=dict)
    root_lease_order: list[str] = field(default_factory=list)
    input_bytes: int = 0
    output_bytes: int = 0


@dataclass(slots=True)
class _HelloWinnerRecord:
    candidate_fingerprint: str
    winner: wire.HelloNegotiationSemanticWinner


@dataclass(slots=True)
class _AttachWinnerRecord:
    host_session: object
    handshake_candidate: wire.HandshakeRecoveryCandidateIdentity
    hello_negotiation_winner: wire.HelloNegotiationSemanticWinner
    semantic_winner: wire.AttachSemanticWinner
    lease: object
    binding_generation: int
    current_binding: wire.TerminalClientTransportBindingIdentity
    credential_id: str
    reconnect_credential_carrier: wire.ReconnectCredentialCarrier | None
    recovery_expires_at_monotonic: float
    acknowledged: bool = False
    ack_result: wire.AttachAckResult | None = None
    ack_tombstone_expires_at_monotonic: float | None = None
    heartbeat_results: dict[str, wire.HeartbeatResult] = field(default_factory=dict)


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
        launch_id: str | None = None,
    ) -> None:
        self.socket_path = socket_path.expanduser()
        self.session_provider = session_provider
        self.close_session = close_session
        self.maximum_frame_bytes = maximum_frame_bytes
        self.launch_capability = launch_capability or secrets.token_bytes(32)
        self.launch_id = launch_id or f"terminal-launch:{uuid4().hex}"
        self._transport_auth = TerminalTransportAuthOwner(
            initial_launch_id=self.launch_id,
            initial_launch_capability=self.launch_capability,
        )
        self._hello_winners: dict[tuple[str, int], _HelloWinnerRecord] = {}
        self._attach_winners: dict[tuple[str, int], _AttachWinnerRecord] = {}
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[object]] = set()

    def issue_launch_credential(self) -> tuple[str, bytes]:
        """Issue a fresh one-shot parent-owned launch credential."""

        return self._transport_auth.issue_initial()

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
            async with asyncio.timeout(PREFACE_DEADLINE_SECONDS):
                preface = await _read_message(
                    reader,
                    wire.TerminalTransportAuthPreface,
                    maximum_bytes=PREFACE_MAXIMUM_BYTES,
                )
            auth_result = self._transport_auth.authenticate(
                preface, connection_id=state.connection_id
            )
            auth_result = self._recover_acknowledged_attachment(
                preface=preface,
                ordinary_result=auth_result,
                state=state,
            )
            await _write_frame(writer, auth_result, maximum_bytes=PREFACE_MAXIMUM_BYTES)
            if auth_result.disposition == wire.TRANSPORT_AUTHENTICATION_REJECTED:
                return
            state.transport_auth_result = auth_result
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
        candidate = state.handshake_candidate
        if candidate is not None:
            record = self._attach_winners.get(
                (candidate.client_instance_id, candidate.attachment_attempt_generation)
            )
            if (
                record is not None
                and record.current_binding.connection_id != state.connection_id
            ):
                # A compatible retry already moved the semantic attachment to a
                # newer physical binding.  The superseded connection owns no
                # attachment-level cleanup authority.
                return
            if (
                record is not None
                and monotonic() < record.recovery_expires_at_monotonic
            ):
                # A pre-ACK response or ACK result may have been lost.  The
                # bounded stable winner, not the physical connection, owns the
                # attachment until recovery expiry.
                return
        services = state.host_session.terminal_application_services
        foundation = state.host_session.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        foundation.retention_owner.release_attachment(state.attachment_id)
        state.root_lease_ids.clear()
        state.root_lease_order.clear()
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
        if branch == "attach_ack":
            return self._attach_ack(frame.attach_ack, state)
        host = _require_attached(state)
        if not state.attachment_acknowledged:
            raise PermissionError("terminal attach acknowledgement is required")
        services = host.terminal_application_services
        services.attachments.validate_attachment(_state_binding(state, host))
        if branch == "heartbeat":
            return self._heartbeat(frame.heartbeat, state, host)
        if branch == "snapshot":
            request = frame.snapshot
            validate_protobuf_fingerprint(
                "terminal-projection-snapshot-request:v2",
                request,
                own_field="request_fingerprint",
            )
            if request.runtime_session_id != host.runtime_session_id:
                raise PermissionError("terminal snapshot request crosses sessions")
            bundle = services.query.snapshot_bundle()
            validated_minimum_fingerprint: str | None = None
            if request.HasField("minimum_observed_control_cursor"):
                minimum = _control_cursor_from_wire(
                    request.minimum_observed_control_cursor
                )
                latest = bundle.control_snapshot.cursor
                if minimum.control_generation != latest.control_generation:
                    rebase = wire.ProjectionSnapshotControlRebaseRequired(
                        request_id=request.request_id,
                        requested_minimum_control_cursor_fingerprint=(
                            minimum.cursor_fingerprint
                        ),
                        latest_control_cursor=control_cursor_to_wire(latest),
                        stable_reason=wire.ProjectionSnapshotControlRebaseRequired.CONTROL_GENERATION_REBASED,
                    )
                    install_protobuf_fingerprint(
                        "terminal-projection-snapshot-control-rebase:v1",
                        rebase,
                        own_field="response_fingerprint",
                    )
                    return wire.ServerFrame(
                        snapshot=wire.ProjectionSnapshotResponse(
                            control_rebase_required=rebase
                        )
                    )
                if latest.control_revision < minimum.control_revision:
                    raise RuntimeError(
                        "terminal control snapshot is older than the requested minimum"
                    )
                validated_minimum_fingerprint = minimum.cursor_fingerprint
            response, bundle = _bounded_snapshot_response(
                bundle,
                request_id=request.request_id,
                maximum_frame_bytes=self.maximum_frame_bytes,
                validated_minimum_control_cursor_fingerprint=(
                    validated_minimum_fingerprint
                ),
            )
            snapshot = bundle.session_snapshot
            self._borrow_root(
                state, host, snapshot.viewport.active_head.confirmed_root_identity
            )
            return response
        if branch == "operational_snapshot":
            request = frame.operational_snapshot
            validate_protobuf_fingerprint(
                "terminal-operational-snapshot-request:v1",
                request,
                own_field="request_fingerprint",
            )
            semantic = state.attach_semantic_winner
            binding = state.transport_binding
            if semantic is None or binding is None:
                raise PermissionError(
                    "terminal operational snapshot has no attachment authority"
                )
            if (
                request.runtime_session_id != host.runtime_session_id
                or request.attachment_id != state.attachment_id
                or request.attachment_generation != state.attachment_generation
                or request.attachment_identity_fingerprint
                != semantic.attachment.identity_fingerprint
                or request.current_transport_binding != binding
                or request.current_transport_binding.connection_id
                != state.connection_id
                or (
                    request.requested_after_operational_generation == 0
                    and request.requested_after_operational_cursor != 0
                )
            ):
                raise PermissionError(
                    "terminal operational snapshot authority is stale"
                )
            snapshot = host.wiring.runtime_wiring.runtime_session.ui_operational_activity_store.snapshot()
            return wire.ServerFrame(
                operational_snapshot=operational_snapshot_to_wire(
                    snapshot,
                    request=request,
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
                candidate = state.handshake_candidate
                if candidate is not None:
                    self._attach_winners.pop(
                        (
                            candidate.client_instance_id,
                            candidate.attachment_attempt_generation,
                        ),
                        None,
                    )
                state.host_session = None
                state.attachment_id = None
                state.attachment_generation = None
                state.root_lease_ids.clear()
                state.root_lease_order.clear()
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
                        terminal_secret_kind=_closed_wire_value(
                            _TERMINAL_SECRET_KIND_TO_WIRE,
                            lease.secret_kind,
                            field="terminal secret kind",
                        ),
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

    def _recover_acknowledged_attachment(
        self,
        *,
        preface: wire.TerminalTransportAuthPreface,
        ordinary_result: wire.TerminalTransportAuthResult,
        state: _ConnectionState,
    ) -> wire.TerminalTransportAuthResult:
        """Rebind one ACK-FULL semantic attachment without replaying Attach."""

        if ordinary_result.disposition in {
            wire.TRANSPORT_AUTHENTICATION_REJECTED,
            wire.TRANSPORT_ACK_RESULT_RECOVERY,
        }:
            return ordinary_result
        candidate_fingerprint = ordinary_result.authenticated_candidate_fingerprint
        matching = tuple(
            record
            for record in self._attach_winners.values()
            if record.acknowledged
            and record.credential_id == ordinary_result.credential_id
            and record.handshake_candidate.candidate_fingerprint
            == candidate_fingerprint
        )
        if not matching:
            return ordinary_result
        if len(matching) != 1:
            raise RuntimeError("terminal ACK tombstone identity is ambiguous")
        record = matching[0]
        if (
            record.ack_result is None
            or record.ack_tombstone_expires_at_monotonic is None
            or monotonic() >= record.ack_tombstone_expires_at_monotonic
        ):
            return ordinary_result
        previous = record.current_binding
        services = record.host_session.terminal_application_services
        services.attachments.rebind_connection(
            attachment_id=record.semantic_winner.attachment.attachment_id,
            attachment_generation=(
                record.semantic_winner.attachment.attachment_generation
            ),
            expected_previous_connection_id=previous.connection_id,
            resulting_connection_id=state.connection_id,
        )
        record.binding_generation += 1
        resulting_binding = _transport_binding(
            attachment=record.semantic_winner.attachment,
            connection_id=state.connection_id,
            generation=record.binding_generation,
        )
        rebind = wire.RecoveredAttachmentTransportBinding(
            previous_transport_binding_fingerprint=previous.binding_fingerprint,
            resulting_transport_binding=resulting_binding,
            disposition=wire.ATTACHMENT_TRANSPORT_REBOUND,
        )
        install_protobuf_fingerprint(
            "terminal-attachment-transport-rebind:v1",
            rebind,
            own_field="rebind_receipt_fingerprint",
        )
        result = wire.TerminalTransportAuthResult(
            auth_request_id=ordinary_result.auth_request_id,
            auth_attempt_id=ordinary_result.auth_attempt_id,
            connection_id=state.connection_id,
            client_instance_id=ordinary_result.client_instance_id,
            credential_id=ordinary_result.credential_id,
            disposition=wire.TRANSPORT_ACK_RESULT_RECOVERY,
            authenticated_candidate_fingerprint=candidate_fingerprint,
            recovered_attach_ack_result=record.ack_result,
            recovered_transport_binding=rebind,
        )
        install_protobuf_fingerprint(
            "terminal-transport-auth-result:v1",
            result,
            own_field="result_fingerprint",
        )
        self._transport_auth.install_request_result(
            credential_id=ordinary_result.credential_id,
            auth_request_id=preface.auth_request_id,
            expected_preface_fingerprint=preface.preface_fingerprint,
            result=result,
        )
        record.current_binding = resulting_binding
        self._install_recovered_connection_state(
            state=state,
            record=record,
            auth_result=result,
        )
        return result

    @staticmethod
    def _install_recovered_connection_state(
        *,
        state: _ConnectionState,
        record: _AttachWinnerRecord,
        auth_result: wire.TerminalTransportAuthResult,
    ) -> None:
        candidate = wire.HandshakeRecoveryCandidateIdentity()
        candidate.CopyFrom(record.handshake_candidate)
        hello_winner = wire.HelloNegotiationSemanticWinner()
        hello_winner.CopyFrom(record.hello_negotiation_winner)
        semantic = wire.AttachSemanticWinner()
        semantic.CopyFrom(record.semantic_winner)
        state.client_instance_id = candidate.client_instance_id
        state.requested_role = candidate.requested_attachment_role
        state.transport_auth_result = auth_result
        state.handshake_candidate = candidate
        state.hello_negotiation_winner = hello_winner
        state.attach_semantic_winner = semantic
        state.transport_binding = record.current_binding
        state.host_session = record.host_session
        state.attachment_id = semantic.attachment.attachment_id
        state.attachment_generation = semantic.attachment.attachment_generation
        state.attachment_acknowledged = True
        state.selected_capabilities = frozenset(hello_winner.selected_capabilities)

    def _hello(self, request, state: _ConnectionState) -> wire.ServerFrame:
        auth_result = state.transport_auth_result
        if state.client_instance_id is not None or auth_result is None:
            raise ValueError("terminal hello is duplicated")
        candidate = request.handshake_candidate
        validate_protobuf_fingerprint(
            "terminal-handshake-recovery-candidate:v1",
            candidate,
            own_field="candidate_fingerprint",
            clear_fields=("candidate_id",),
        )
        expected_candidate_id = (
            "handshake:" + candidate.candidate_fingerprint.removeprefix("sha256:")
        )
        supported = tuple(candidate.supported_capabilities)
        required = tuple(candidate.required_capabilities)
        candidate_valid = (
            candidate.candidate_version == 1
            and candidate.candidate_id == expected_candidate_id
            and candidate.client_instance_id == auth_result.client_instance_id
            and candidate.attachment_attempt_generation
            == self._transport_auth.expected_candidate_generation(
                auth_result.credential_id
            )
            and candidate.requested_attachment_role
            in {
                wire.ATTACHMENT_ROLE_OBSERVER,
                wire.ATTACHMENT_ROLE_CONTROLLER,
            }
            and candidate.minimum_protocol_major == PROTOCOL_MAJOR
            and candidate.maximum_protocol_major == PROTOCOL_MAJOR
            and candidate.minimum_protocol_minor <= PROTOCOL_MINOR
            and candidate.maximum_protocol_minor >= PROTOCOL_MINOR
            and candidate.schema_contract_fingerprint == PROTOCOL_SCHEMA_FINGERPRINT
            and supported == tuple(sorted(set(supported)))
            and required == tuple(sorted(set(required)))
            and 0 not in supported
            and 0 not in required
            and set(required).issubset(supported)
            and _S1_REQUIRED_CAPABILITIES.issubset(required)
            and set(required).issubset(_SERVER_CAPABILITIES)
            and request.transport_auth_attempt_id == auth_result.auth_attempt_id
            and request.transport_auth_result_fingerprint
            == auth_result.result_fingerprint
            and auth_result.authenticated_candidate_fingerprint
            == candidate.candidate_fingerprint
        )
        if not candidate_valid:
            return self._hello_rejected(
                request=request,
                state=state,
                candidate=candidate,
                reason=wire.SERVER_NEGOTIATION_POLICY_REJECTED,
            )
        key = (
            candidate.client_instance_id,
            candidate.attachment_attempt_generation,
        )
        record = self._hello_winners.get(key)
        if (
            record is not None
            and record.candidate_fingerprint != candidate.candidate_fingerprint
        ):
            raise PermissionError("terminal handshake candidate generation conflicts")
        selected = tuple(item for item in supported if item in _SERVER_CAPABILITIES)
        if record is None:
            limits = wire.NegotiatedLimits(
                maximum_frame_bytes=self.maximum_frame_bytes,
                maximum_history_page_cells=MAXIMUM_HISTORY_PAGE_CELLS,
                maximum_history_page_decoded_bytes=MAXIMUM_HISTORY_PAGE_BYTES,
                maximum_observation_wait_ms=MAXIMUM_OBSERVATION_WAIT_MS,
                secret_frame_maximum_bytes=SECRET_FRAME_MAXIMUM_BYTES,
                maximum_active_queue_items=MAXIMUM_ACTIVE_QUEUE_ITEMS,
                maximum_server_control_notifications=(
                    MAXIMUM_SERVER_CONTROL_NOTIFICATIONS
                ),
                maximum_operational_activity_cells=(MAXIMUM_OPERATIONAL_ACTIVITY_CELLS),
                maximum_durable_observation_bytes=(MAXIMUM_DURABLE_OBSERVATION_BYTES),
                maximum_operational_observation_bytes=(
                    MAXIMUM_OPERATIONAL_OBSERVATION_BYTES
                ),
                maximum_control_observation_bytes=MAXIMUM_CONTROL_OBSERVATION_BYTES,
                maximum_observation_batch_bytes=MAXIMUM_OBSERVATION_BATCH_BYTES,
            )
            capability_contract = context_fingerprint(
                "terminal-client-capability-contract:v2",
                {
                    "server_supported": _SERVER_CAPABILITIES,
                    "selected": selected,
                    "required": required,
                },
            )
            transcript = context_fingerprint(
                "terminal-hello-negotiation-transcript:v1",
                {
                    "candidate_fingerprint": candidate.candidate_fingerprint,
                    "selected_protocol": {
                        "major": PROTOCOL_MAJOR,
                        "minor": PROTOCOL_MINOR,
                        "schema": PROTOCOL_SCHEMA_FINGERPRINT,
                    },
                    "server_runtime_compatibility_identity": "pulsara-terminal-runtime:v2",
                    "limits": {
                        field.name: getattr(limits, field.name)
                        for field in limits.DESCRIPTOR.fields
                    },
                    "server_supported": _SERVER_CAPABILITIES,
                    "selected": selected,
                    "capability_contract": capability_contract,
                },
            )
            winner = wire.HelloNegotiationSemanticWinner(
                handshake_candidate_id=candidate.candidate_id,
                handshake_candidate_fingerprint=candidate.candidate_fingerprint,
                attachment_attempt_generation=(candidate.attachment_attempt_generation),
                selected_protocol=protocol_version(),
                server_build_identity="pulsara-python-terminal-foundation:v2",
                server_runtime_compatibility_identity="pulsara-terminal-runtime:v2",
                negotiated_limits=limits,
                server_supported_capabilities=_SERVER_CAPABILITIES,
                selected_capabilities=selected,
                capability_contract_fingerprint=capability_contract,
                negotiation_transcript_fingerprint=transcript,
            )
            install_protobuf_fingerprint(
                "terminal-hello-negotiation-winner:v1",
                winner,
                own_field="negotiation_winner_fingerprint",
            )
            record = _HelloWinnerRecord(
                candidate_fingerprint=candidate.candidate_fingerprint,
                winner=winner,
            )
            self._hello_winners[key] = record
        winner = wire.HelloNegotiationSemanticWinner()
        winner.CopyFrom(record.winner)
        challenge = secrets.token_bytes(32)
        commitment = attachment_challenge_commitment(
            auth_attempt_id=auth_result.auth_attempt_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            candidate_id=candidate.candidate_id,
            connection_id=state.connection_id,
            negotiation_winner_fingerprint=winner.negotiation_winner_fingerprint,
            request_id=request.request_id,
            challenge=challenge,
        )
        receipt = wire.ServerHelloReceipt(
            request_id=request.request_id,
            transport_auth_attempt_id=auth_result.auth_attempt_id,
            handshake_candidate_id=candidate.candidate_id,
            handshake_candidate_fingerprint=candidate.candidate_fingerprint,
            negotiation_winner_fingerprint=winner.negotiation_winner_fingerprint,
            current_connection_id=state.connection_id,
            attachment_challenge=challenge,
            attachment_challenge_commitment=commitment,
        )
        install_protobuf_fingerprint(
            "terminal-server-hello-receipt:v1",
            receipt,
            own_field="hello_receipt_fingerprint",
        )
        stored_candidate = wire.HandshakeRecoveryCandidateIdentity()
        stored_candidate.CopyFrom(candidate)
        state.client_instance_id = candidate.client_instance_id
        state.requested_role = candidate.requested_attachment_role
        state.attachment_challenge = challenge
        state.handshake_candidate = stored_candidate
        state.hello_negotiation_winner = winner
        state.hello_receipt = receipt
        state.selected_capabilities = frozenset(selected)
        return wire.ServerFrame(
            hello=wire.HelloOutcome(
                accepted=wire.ServerHello(
                    negotiation_winner=winner,
                    receipt=receipt,
                )
            )
        )

    def _attach(self, request, state: _ConnectionState) -> wire.ServerFrame:
        candidate = state.handshake_candidate
        hello_winner = state.hello_negotiation_winner
        hello_receipt = state.hello_receipt
        auth_result = state.transport_auth_result
        if state.host_session is not None or any(
            item is None
            for item in (candidate, hello_winner, hello_receipt, auth_result)
        ):
            raise ValueError("terminal connection is already attached")
        assert candidate is not None
        assert hello_winner is not None
        assert hello_receipt is not None
        assert auth_result is not None
        if (
            request.handshake_candidate_id != candidate.candidate_id
            or request.handshake_candidate_fingerprint
            != candidate.candidate_fingerprint
            or request.negotiation_winner_fingerprint
            != hello_winner.negotiation_winner_fingerprint
            or request.current_hello_receipt_fingerprint
            != hello_receipt.hello_receipt_fingerprint
            or not hmac.compare_digest(
                bytes(request.attachment_challenge),
                state.attachment_challenge or b"",
            )
            or request.attachment_challenge_commitment
            != hello_receipt.attachment_challenge_commitment
        ):
            raise PermissionError("terminal attach proof is stale")
        expected_commitment = attachment_challenge_commitment(
            auth_attempt_id=auth_result.auth_attempt_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            candidate_id=candidate.candidate_id,
            connection_id=state.connection_id,
            negotiation_winner_fingerprint=(
                hello_winner.negotiation_winner_fingerprint
            ),
            request_id=hello_receipt.request_id,
            challenge=bytes(request.attachment_challenge),
        )
        if expected_commitment != request.attachment_challenge_commitment:
            raise PermissionError("terminal attachment challenge is invalid")
        host = self.session_provider(candidate.host_session_id)
        if host.runtime_session_id != candidate.requested_runtime_session_id:
            raise PermissionError("terminal requested runtime session is stale")
        services = host.terminal_application_services
        key = (candidate.client_instance_id, candidate.attachment_attempt_generation)
        existing = self._attach_winners.get(key)
        if existing is not None:
            if existing.acknowledged:
                raise PermissionError(
                    "terminal acknowledged attachment requires auth recovery"
                )
            if (
                existing.semantic_winner.hello_negotiation_winner_fingerprint
                != hello_winner.negotiation_winner_fingerprint
                or existing.host_session is not host
            ):
                raise PermissionError("terminal pre-ACK attachment winner conflicts")
            previous = existing.current_binding
            services.attachments.rebind_connection(
                attachment_id=existing.semantic_winner.attachment.attachment_id,
                attachment_generation=(
                    existing.semantic_winner.attachment.attachment_generation
                ),
                expected_previous_connection_id=previous.connection_id,
                resulting_connection_id=state.connection_id,
            )
            existing.binding_generation += 1
            binding = _transport_binding(
                attachment=existing.semantic_winner.attachment,
                connection_id=state.connection_id,
                generation=existing.binding_generation,
            )
            receipt = wire.AttachResultReceipt(
                request_id=request.request_id,
                transport_auth_attempt_id=auth_result.auth_attempt_id,
                handshake_candidate_id=candidate.candidate_id,
                handshake_candidate_fingerprint=candidate.candidate_fingerprint,
                attach_semantic_winner=existing.semantic_winner,
                current_transport_binding=binding,
                previous_transport_binding_fingerprint=(previous.binding_fingerprint),
                disposition=wire.ATTACH_REBOUND_PRE_ACK,
            )
            if existing.reconnect_credential_carrier is not None:
                receipt.next_reconnect_credential_carrier.CopyFrom(
                    existing.reconnect_credential_carrier
                )
            install_protobuf_fingerprint(
                "terminal-attach-result-receipt:v1",
                receipt,
                own_field="receipt_fingerprint",
            )
            existing.current_binding = binding
            state.host_session = host
            state.attachment_id = existing.semantic_winner.attachment.attachment_id
            state.attachment_generation = (
                existing.semantic_winner.attachment.attachment_generation
            )
            state.attach_semantic_winner = existing.semantic_winner
            state.attach_result_receipt = receipt
            state.transport_binding = binding
            return wire.ServerFrame(attach=receipt)
        predecessor_key: tuple[str, int] | None = None
        if candidate.attachment_attempt_generation == 1:
            lease = services.attachments.attach(
                connection_id=state.connection_id,
                client_instance_id=state.client_instance_id or "",
                request_controller=(
                    candidate.requested_attachment_role
                    == wire.ATTACHMENT_ROLE_CONTROLLER
                ),
            )
        else:
            try:
                predecessor = self._transport_auth.reconnect_predecessor(
                    auth_result.credential_id
                )
            except KeyError as exc:
                raise PermissionError(
                    "terminal reconnect predecessor is unavailable"
                ) from exc
            predecessor_key = (
                candidate.client_instance_id,
                candidate.attachment_attempt_generation - 1,
            )
            predecessor_record = self._attach_winners.get(predecessor_key)
            if (
                predecessor.client_instance_id != candidate.client_instance_id
                or predecessor.expected_next_attachment_attempt_generation
                != candidate.attachment_attempt_generation
                or predecessor.previous_candidate_fingerprint
                != (
                    predecessor_record.handshake_candidate.candidate_fingerprint
                    if predecessor_record is not None
                    else ""
                )
                or predecessor_record is None
                or predecessor_record.host_session is not host
                or not predecessor_record.acknowledged
                or predecessor_record.ack_result is None
                or predecessor.previous_attachment_id
                != predecessor_record.semantic_winner.attachment.attachment_id
                or predecessor.previous_attachment_generation
                != predecessor_record.semantic_winner.attachment.attachment_generation
            ):
                raise PermissionError("terminal reconnect predecessor proof is stale")
            lease = services.attachments.supersede_for_reconnect(
                previous_attachment_id=predecessor.previous_attachment_id,
                previous_attachment_generation=(
                    predecessor.previous_attachment_generation
                ),
                connection_id=state.connection_id,
                client_instance_id=candidate.client_instance_id,
                request_controller=(
                    candidate.requested_attachment_role
                    == wire.ATTACHMENT_ROLE_CONTROLLER
                ),
            )
            foundation = host.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
            foundation.retention_owner.release_attachment(
                predecessor.previous_attachment_id
            )
            services.secrets.revoke_attachment(predecessor.previous_attachment_id)
        if lease.attachment_generation != candidate.attachment_attempt_generation:
            raise RuntimeError("terminal attachment successor generation drifted")
        foundation = host.wiring.runtime_wiring.runtime_session.terminal_presentation_foundation_service
        foundation.start_background_if_possible()
        attachment = attachment_to_wire(lease)
        install_protobuf_fingerprint(
            "terminal-attachment-identity:v2",
            attachment,
            own_field="identity_fingerprint",
        )
        controller_disposition = (
            wire.CONTROLLER_GRANTED
            if lease.role == "controller"
            else (
                wire.CONTROLLER_UNAVAILABLE_OBSERVER_ATTACHED
                if candidate.requested_attachment_role
                == wire.ATTACHMENT_ROLE_CONTROLLER
                else wire.OBSERVER_ATTACHED
            )
        )
        semantic_winner = wire.AttachSemanticWinner(
            handshake_candidate_id=candidate.candidate_id,
            handshake_candidate_fingerprint=candidate.candidate_fingerprint,
            attachment_attempt_generation=candidate.attachment_attempt_generation,
            hello_negotiation_winner_fingerprint=(
                hello_winner.negotiation_winner_fingerprint
            ),
            attachment=attachment,
            controller_disposition=controller_disposition,
            bootstrap_requirement=(wire.PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED),
            heartbeat_policy=wire.HeartbeatPolicy(
                interval_ms=HEARTBEAT_INTERVAL_MS,
                grace_ms=HEARTBEAT_GRACE_MS,
                maximum_missed_count=HEARTBEAT_MAXIMUM_MISSED_COUNT,
            ),
        )
        reconnect_carrier: wire.ReconnectCredentialCarrier | None = None
        if wire.RECONNECT_AUTH_ROTATION_V1 in state.selected_capabilities:
            reconnect_identity, reconnect_carrier = (
                self._transport_auth.issue_reconnect(
                    client_instance_id=candidate.client_instance_id,
                    previous_attachment_id=attachment.attachment_id,
                    previous_attachment_generation=attachment.attachment_generation,
                    previous_candidate_fingerprint=candidate.candidate_fingerprint,
                    expected_next_attachment_attempt_generation=(
                        candidate.attachment_attempt_generation + 1
                    ),
                )
            )
            semantic_winner.next_reconnect_credential_public_identity.CopyFrom(
                reconnect_identity
            )
        install_protobuf_fingerprint(
            "terminal-attach-semantic-winner:v1",
            semantic_winner,
            own_field="semantic_winner_fingerprint",
        )
        binding = _transport_binding(
            attachment=attachment,
            connection_id=state.connection_id,
            generation=1,
        )
        receipt = wire.AttachResultReceipt(
            request_id=request.request_id,
            transport_auth_attempt_id=auth_result.auth_attempt_id,
            handshake_candidate_id=candidate.candidate_id,
            handshake_candidate_fingerprint=candidate.candidate_fingerprint,
            attach_semantic_winner=semantic_winner,
            current_transport_binding=binding,
            disposition=wire.ATTACH_CREATED,
        )
        if reconnect_carrier is not None:
            receipt.next_reconnect_credential_carrier.CopyFrom(reconnect_carrier)
        install_protobuf_fingerprint(
            "terminal-attach-result-receipt:v1",
            receipt,
            own_field="receipt_fingerprint",
        )
        state.host_session = host
        state.attachment_id = lease.attachment_id
        state.attachment_generation = lease.attachment_generation
        state.attach_semantic_winner = semantic_winner
        state.attach_result_receipt = receipt
        state.transport_binding = binding
        stored_candidate = wire.HandshakeRecoveryCandidateIdentity()
        stored_candidate.CopyFrom(candidate)
        stored_hello_winner = wire.HelloNegotiationSemanticWinner()
        stored_hello_winner.CopyFrom(hello_winner)
        self._attach_winners[key] = _AttachWinnerRecord(
            host_session=host,
            handshake_candidate=stored_candidate,
            hello_negotiation_winner=stored_hello_winner,
            semantic_winner=semantic_winner,
            lease=lease,
            binding_generation=1,
            current_binding=binding,
            credential_id=auth_result.credential_id,
            reconnect_credential_carrier=reconnect_carrier,
            recovery_expires_at_monotonic=monotonic() + 30.0,
        )
        if predecessor_key is not None:
            self._attach_winners.pop(predecessor_key, None)
            self._hello_winners.pop(predecessor_key, None)
        return wire.ServerFrame(attach=receipt)

    def _attach_ack(self, request, state: _ConnectionState) -> wire.ServerFrame:
        candidate = state.handshake_candidate
        semantic = state.attach_semantic_winner
        receipt = state.attach_result_receipt
        binding = state.transport_binding
        auth_result = state.transport_auth_result
        if any(
            item is None
            for item in (candidate, semantic, receipt, binding, auth_result)
        ):
            raise PermissionError("terminal attach acknowledgement is premature")
        assert candidate is not None
        assert semantic is not None
        assert receipt is not None
        assert binding is not None
        assert auth_result is not None
        validate_protobuf_fingerprint(
            "terminal-attach-receipt-ack:v1",
            request,
            own_field="ack_fingerprint",
        )
        if (
            request.attachment_id != state.attachment_id
            or request.attachment_generation != state.attachment_generation
            or request.semantic_winner_fingerprint
            != semantic.semantic_winner_fingerprint
            or request.current_transport_binding.binding_fingerprint
            != binding.binding_fingerprint
            or request.attach_result_receipt_fingerprint != receipt.receipt_fingerprint
            or request.current_transport_binding != binding
        ):
            raise PermissionError("terminal attach acknowledgement proof is stale")
        key = (candidate.client_instance_id, candidate.attachment_attempt_generation)
        record = self._attach_winners.get(key)
        if record is None:
            raise RuntimeError("terminal attach winner is unavailable")
        if record.ack_result is None:
            result = wire.AttachAckResult(
                request_id=request.request_id,
                attachment_id=state.attachment_id,
                attachment_generation=state.attachment_generation,
                semantic_winner_fingerprint=semantic.semantic_winner_fingerprint,
                acknowledged_transport_binding_fingerprint=(
                    binding.binding_fingerprint
                ),
                disposition=wire.ATTACH_ACKNOWLEDGED,
                retired_credential_id=auth_result.credential_id,
            )
            install_protobuf_fingerprint(
                "terminal-attach-ack-result:v1",
                result,
                own_field="ack_result_fingerprint",
            )
            record.ack_result = result
            record.acknowledged = True
            record.ack_tombstone_expires_at_monotonic = monotonic() + 30.0
        else:
            result = wire.AttachAckResult()
            result.CopyFrom(record.ack_result)
            result.request_id = request.request_id
            result.disposition = wire.ATTACH_COMPATIBLE_ALREADY_ACKNOWLEDGED
            install_protobuf_fingerprint(
                "terminal-attach-ack-result:v1",
                result,
                own_field="ack_result_fingerprint",
            )
        self._transport_auth.mark_acknowledged(auth_result.credential_id)
        state.attachment_acknowledged = True
        return wire.ServerFrame(attach_ack=result)

    def _heartbeat(self, request, state: _ConnectionState, host) -> wire.ServerFrame:
        semantic = state.attach_semantic_winner
        binding = state.transport_binding
        candidate = state.handshake_candidate
        if semantic is None or binding is None or candidate is None:
            raise PermissionError("terminal heartbeat has no attachment authority")
        validate_protobuf_fingerprint(
            "terminal-heartbeat-request:v1",
            request,
            own_field="request_fingerprint",
        )
        expected_candidate = _heartbeat_candidate_fingerprint(
            runtime_session_id=host.runtime_session_id,
            attachment_identity_fingerprint=(semantic.attachment.identity_fingerprint),
            semantic_winner_fingerprint=semantic.semantic_winner_fingerprint,
            heartbeat_generation=request.heartbeat_generation,
            previous_accepted_generation=(
                request.previous_accepted_heartbeat_generation
            ),
        )
        if (
            request.runtime_session_id != host.runtime_session_id
            or request.attachment_id != state.attachment_id
            or request.attachment_generation != state.attachment_generation
            or request.attachment_identity_fingerprint
            != semantic.attachment.identity_fingerprint
            or request.attach_semantic_winner_fingerprint
            != semantic.semantic_winner_fingerprint
            or request.current_transport_binding != binding
            or request.heartbeat_candidate_fingerprint != expected_candidate
        ):
            raise PermissionError("terminal heartbeat authority is stale")
        key = (candidate.client_instance_id, candidate.attachment_attempt_generation)
        record = self._attach_winners[key]
        stored = record.heartbeat_results.get(expected_candidate)
        if stored is not None:
            branch = stored.WhichOneof("outcome")
            stored_request_id = (
                stored.accepted.request_id
                if branch == "accepted"
                else stored.rejected.request_id
            )
            if stored_request_id != request.request_id:
                raise PermissionError(
                    "terminal heartbeat retry changed its physical request identity"
                )
            result = wire.HeartbeatResult()
            result.CopyFrom(stored)
            return wire.ServerFrame(heartbeat=result)
        if (
            request.heartbeat_generation != state.accepted_heartbeat_generation + 1
            or request.previous_accepted_heartbeat_generation
            != state.accepted_heartbeat_generation
        ):
            raise PermissionError("terminal heartbeat generation is stale")
        lease = host.terminal_application_services.attachments.heartbeat(
            attachment_id=request.attachment_id,
            attachment_generation=request.attachment_generation,
        )
        semantic_result = context_fingerprint(
            "terminal-heartbeat-accepted-semantic-result:v1",
            {
                "candidate": expected_candidate,
                "liveness_disposition": "attachment_active_lease_renewed",
                "resulting_expiry": lease.expires_at_utc,
            },
        )
        accepted = wire.HeartbeatAcceptedReceipt(
            request_id=request.request_id,
            runtime_session_id=host.runtime_session_id,
            attachment_id=request.attachment_id,
            attachment_generation=request.attachment_generation,
            attachment_identity_fingerprint=(semantic.attachment.identity_fingerprint),
            attach_semantic_winner_fingerprint=(semantic.semantic_winner_fingerprint),
            acknowledged_transport_binding_fingerprint=(binding.binding_fingerprint),
            heartbeat_generation=request.heartbeat_generation,
            previous_accepted_heartbeat_generation=(
                request.previous_accepted_heartbeat_generation
            ),
            heartbeat_candidate_fingerprint=expected_candidate,
            resulting_attachment_lease_expires_at=lease.expires_at_utc,
            liveness_disposition=wire.ATTACHMENT_ACTIVE_LEASE_RENEWED,
            heartbeat_semantic_result_fingerprint=semantic_result,
        )
        install_protobuf_fingerprint(
            "terminal-heartbeat-accepted-receipt:v1",
            accepted,
            own_field="receipt_fingerprint",
        )
        result = wire.HeartbeatResult(accepted=accepted)
        record.heartbeat_results[expected_candidate] = result
        minimum_retained_generation = max(1, request.heartbeat_generation - 1)
        for fingerprint, retained in tuple(record.heartbeat_results.items()):
            branch = retained.WhichOneof("outcome")
            generation = (
                retained.accepted.heartbeat_generation
                if branch == "accepted"
                else retained.rejected.heartbeat_generation
            )
            if generation < minimum_retained_generation:
                record.heartbeat_results.pop(fingerprint, None)
        if len(record.heartbeat_results) > 2:
            raise AssertionError("terminal heartbeat tombstone registry is unbounded")
        state.accepted_heartbeat_generation = request.heartbeat_generation
        self._renew_root_leases(state, host)
        return wire.ServerFrame(heartbeat=result)

    def _hello_rejected(
        self,
        *,
        request,
        state: _ConnectionState,
        candidate,
        reason: int,
    ) -> wire.ServerFrame:
        auth_result = state.transport_auth_result
        assert auth_result is not None
        terminal = wire.HandshakeCandidateTerminalReceipt(
            handshake_candidate_id=candidate.candidate_id or "unknown",
            handshake_candidate_fingerprint=(
                candidate.candidate_fingerprint or "sha256:" + "0" * 64
            ),
            attachment_attempt_generation=candidate.attachment_attempt_generation,
            terminal_disposition=wire.HELLO_REJECTED,
            terminal_reason=reason,
            required_client_disposition=wire.FATAL_COMPATIBILITY,
            candidate_registry_revision=1,
        )
        install_protobuf_fingerprint(
            "terminal-handshake-candidate-terminal:v1",
            terminal,
            own_field="terminal_receipt_fingerprint",
        )
        rejected = wire.HelloRejected(
            request_id=request.request_id,
            transport_auth_attempt_id=auth_result.auth_attempt_id,
            current_connection_id=state.connection_id,
            handshake_candidate_id=terminal.handshake_candidate_id,
            handshake_candidate_fingerprint=(terminal.handshake_candidate_fingerprint),
            candidate_terminal_receipt=terminal,
        )
        install_protobuf_fingerprint(
            "terminal-hello-rejected:v1",
            rejected,
            own_field="outcome_fingerprint",
        )
        return wire.ServerFrame(hello=wire.HelloOutcome(rejected=rejected))

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
        if existing is not None:
            if foundation.retention_owner.renew(existing, ttl_seconds=ttl_seconds):
                return
            # Re-borrowing an expired physical lease does not make the semantic
            # root newly installed. Preserve the shared client/server FIFO
            # position so both sides retire the same fifth root.
            replacement = foundation.retention_owner.borrow(
                attachment_id=state.attachment_id or "",
                root_identity_fingerprint=fingerprint,
                ttl_seconds=ttl_seconds,
            )
            state.root_lease_ids[fingerprint] = replacement.lease_id
            return
        while len(state.root_lease_order) >= MAXIMUM_PINNED_HISTORY_ROOTS:
            retired_fingerprint = state.root_lease_order.pop(0)
            retired_lease_id = state.root_lease_ids.pop(retired_fingerprint, None)
            if retired_lease_id is not None:
                foundation.retention_owner.release(retired_lease_id)
        lease = foundation.retention_owner.borrow(
            attachment_id=state.attachment_id or "",
            root_identity_fingerprint=fingerprint,
            ttl_seconds=ttl_seconds,
        )
        state.root_lease_ids[fingerprint] = lease.lease_id
        state.root_lease_order.append(fingerprint)
        if len(state.root_lease_ids) > MAXIMUM_PINNED_HISTORY_ROOTS or len(
            state.root_lease_order
        ) != len(state.root_lease_ids):
            raise AssertionError("terminal attachment root lease set is unbounded")

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
                state.root_lease_order = [
                    item for item in state.root_lease_order if item != fingerprint
                ]

    async def _observe_next(self, request, state, host) -> wire.ServerFrame:
        if wire.CONTROL_PROJECTION_OBSERVATION_V1 not in state.selected_capabilities:
            raise PermissionError(
                "terminal control observation capability was not negotiated"
            )
        requested_control_cursor = _control_cursor_from_wire(
            request.after_control_cursor
        )
        maximum_wait = (
            min(max(request.maximum_wait_ms, 1), MAXIMUM_OBSERVATION_WAIT_MS) / 1000
        )
        deadline = monotonic() + maximum_wait
        runtime = host.wiring.runtime_wiring.runtime_session
        foundation = runtime.terminal_presentation_foundation_service
        while True:
            control = host.terminal_application_services.query.read_control_after(
                requested_control_cursor
            )
            operational = runtime.ui_operational_activity_store.read_after(
                operational_generation=request.after_operational_generation,
                operational_cursor=request.after_operational_cursor,
            )
            operational_generation = operational.operational_generation
            operational_cursor = operational.operational_cursor
            read = foundation.read_observation_after(
                projection_revision=request.after_projection_revision
            )
            durable_branch: wire.DurableObservationBranch | None = None
            operational_branch: wire.OperationalObservationBranch | None = None
            control_branch: wire.ControlObservationBranch | None = None

            if (
                request.after_authority_high_water > read.latest_authority_high_water
                or request.after_projection_revision > read.latest_projection_revision
            ):
                gap = wire.DurableObservationGapFrame(
                    request_id=request.request_id,
                    latest_authority_high_water=read.latest_authority_high_water,
                    latest_projection_revision=read.latest_projection_revision,
                    gap_reason=wire.CLIENT_CURSOR_AHEAD,
                )
                install_protobuf_fingerprint(
                    "terminal-durable-observation-gap:v1",
                    gap,
                    own_field="frame_fingerprint",
                )
                durable_branch = wire.DurableObservationBranch(gap=gap)
            elif read.status == "gap":
                gap = wire.DurableObservationGapFrame(
                    request_id=request.request_id,
                    latest_authority_high_water=read.latest_authority_high_water,
                    latest_projection_revision=read.latest_projection_revision,
                    gap_reason=wire.PROJECTION_TRANSITION_EVICTED,
                )
                install_protobuf_fingerprint(
                    "terminal-durable-observation-gap:v1",
                    gap,
                    own_field="frame_fingerprint",
                )
                durable_branch = wire.DurableObservationBranch(gap=gap)
            elif read.status == "next":
                assert read.root_advanced is not None
                self._borrow_root(
                    state,
                    host,
                    read.root_advanced.resulting_active_head.confirmed_root_identity,
                )
                durable_branch = wire.DurableObservationBranch(
                    root_advanced=root_advanced_to_wire(
                        read.root_advanced, request_id=request.request_id
                    )
                )

            if request.after_operational_generation > operational_generation or (
                request.after_operational_generation == operational_generation
                and request.after_operational_cursor > operational_cursor
            ):
                gap = wire.OperationalObservationGapFrame(
                    request_id=request.request_id,
                    latest_operational_generation=operational_generation,
                    latest_operational_cursor=operational_cursor,
                    gap_reason=wire.CLIENT_CURSOR_AHEAD,
                )
                install_protobuf_fingerprint(
                    "terminal-operational-observation-gap:v1",
                    gap,
                    own_field="frame_fingerprint",
                )
                operational_branch = wire.OperationalObservationBranch(gap=gap)
            elif operational.status == "gap":
                gap = wire.OperationalObservationGapFrame(
                    request_id=request.request_id,
                    latest_operational_generation=operational_generation,
                    latest_operational_cursor=operational_cursor,
                    gap_reason=wire.OPERATIONAL_CURSOR_GAP,
                )
                install_protobuf_fingerprint(
                    "terminal-operational-observation-gap:v1",
                    gap,
                    own_field="frame_fingerprint",
                )
                operational_branch = wire.OperationalObservationBranch(gap=gap)
            elif operational.status == "next":
                delta = wire.OperationalDeltaFrame(
                    request_id=request.request_id,
                    operational_generation=operational_generation,
                    operational_cursor=operational_cursor,
                    ordered_changes=(
                        operational_change_to_wire(item)
                        for item in operational.ordered_changes
                    ),
                )
                install_protobuf_fingerprint(
                    "terminal-operational-delta-frame:v1",
                    delta,
                    own_field="frame_fingerprint",
                )
                operational_branch = wire.OperationalObservationBranch(delta=delta)

            if control.status == "gap":
                reason = {
                    "generation_changed": wire.CONTROL_GENERATION_CHANGED,
                    "cursor_too_old": wire.CONTROL_CURSOR_TOO_OLD,
                    "transition_not_contiguous": (
                        wire.CONTROL_TRANSITION_NOT_CONTIGUOUS
                    ),
                    "contract_changed": wire.CONTROL_CONTRACT_CHANGED,
                }[control.gap_reason]
                gap = wire.ControlProjectionGapFrame(
                    request_id=request.request_id,
                    requested_control_cursor_fingerprint=(
                        requested_control_cursor.cursor_fingerprint
                    ),
                    latest_control_cursor=control_cursor_to_wire(control.latest_cursor),
                    stable_reason=reason,
                    disposition=wire.CONTROL_PROJECTION_SNAPSHOT_REQUIRED,
                )
                install_protobuf_fingerprint(
                    "terminal-control-projection-gap:v1",
                    gap,
                    own_field="frame_fingerprint",
                )
                control_branch = wire.ControlObservationBranch(gap=gap)
            elif control.status == "changed":
                changed = wire.ControlProjectionChangedFrame(
                    request_id=request.request_id,
                    validated_after_control_cursor_fingerprint=(
                        requested_control_cursor.cursor_fingerprint
                    ),
                    control_generation=control.latest_cursor.control_generation,
                    base_control_projection_revision=(
                        requested_control_cursor.control_revision
                    ),
                    base_control_projection_fingerprint=(
                        requested_control_cursor.control_projection_fingerprint
                    ),
                    resulting_control_projection_revision=(
                        control.latest_cursor.control_revision
                    ),
                    resulting_control_projection_fingerprint=(
                        control.latest_cursor.control_projection_fingerprint
                    ),
                    changed_sections=(
                        _control_section_to_wire(item)
                        for item in control.changed_sections
                    ),
                    consumed_transition_count=len(control.ordered_records),
                    consumed_transition_range_accumulator=(
                        control.transition_range_accumulator
                    ),
                    resulting_control_cursor=control_cursor_to_wire(
                        control.latest_cursor
                    ),
                    disposition=wire.CONTROL_PROJECTION_SNAPSHOT_REQUIRED,
                )
                install_protobuf_fingerprint(
                    "terminal-control-projection-changed:v1",
                    changed,
                    own_field="frame_fingerprint",
                )
                control_branch = wire.ControlObservationBranch(changed=changed)

            if any(
                item is not None
                for item in (control_branch, durable_branch, operational_branch)
            ):
                batch = wire.ObservationBatchFrame(
                    request_id=request.request_id,
                    included_plane_count=sum(
                        item is not None
                        for item in (
                            control_branch,
                            durable_branch,
                            operational_branch,
                        )
                    ),
                )
                if control_branch is not None:
                    batch.control.CopyFrom(control_branch)
                if durable_branch is not None:
                    batch.durable.CopyFrom(durable_branch)
                if operational_branch is not None:
                    batch.operational.CopyFrom(operational_branch)
                install_protobuf_fingerprint(
                    "terminal-observation-batch:v1",
                    batch,
                    own_field="batch_fingerprint",
                )
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(batch=batch)
                )
            if monotonic() >= deadline:
                no_change = wire.ObservationNoChangeFrame(
                    request_id=request.request_id,
                    echoed_authority_high_water=request.after_authority_high_water,
                    echoed_projection_revision=request.after_projection_revision,
                    echoed_operational_generation=(
                        request.after_operational_generation
                    ),
                    echoed_operational_cursor=request.after_operational_cursor,
                    echoed_control_cursor_fingerprint=(
                        requested_control_cursor.cursor_fingerprint
                    ),
                )
                install_protobuf_fingerprint(
                    "terminal-observation-no-change:v1",
                    no_change,
                    own_field="frame_fingerprint",
                )
                return wire.ServerFrame(
                    observation=wire.ObservationResponse(no_change=no_change)
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
            history_page=_history_outcome_to_wire(
                outcome,
                request.request_id,
                maximum_decoded_bytes=min(
                    max(request.maximum_decoded_bytes, 1),
                    MAXIMUM_HISTORY_PAGE_BYTES,
                ),
            )
        )


def _history_outcome_to_wire(
    outcome,
    request_id: str,
    *,
    maximum_decoded_bytes: int = MAXIMUM_HISTORY_PAGE_BYTES,
) -> wire.HistoryPageResponse:
    if isinstance(outcome, PresentationHistoryPageData):
        ordered_entries = tuple(
            entry_to_wire(item) for item in outcome.ordered_history_entries
        )
        decoded_bytes = history_ranked_entry_vector_decoded_bytes(ordered_entries)
        if decoded_bytes > maximum_decoded_bytes:
            fault_code = "PRESENTATION_PAGE_WIRE_DECODED_BOUND_EXCEEDED"
            return wire.HistoryPageResponse(
                reconciliation=wire.HistoryReconciliationRequired(
                    request_id=request_id,
                    requested_cursor_fingerprint=(
                        outcome.validated_input_cursor_fingerprint
                    ),
                    fault_code=fault_code,
                    reconciliation_owner_identity=(
                        "terminal-protocol:history-page-wire-accounting:v1"
                    ),
                    response_fingerprint=context_fingerprint(
                        "terminal-history-page-wire-accounting-reconciliation:v1",
                        {
                            "request_id": request_id,
                            "requested_cursor_fingerprint": (
                                outcome.validated_input_cursor_fingerprint
                            ),
                            "decoded_bytes": decoded_bytes,
                            "maximum_decoded_bytes": maximum_decoded_bytes,
                            "fault_code": fault_code,
                        },
                    ),
                )
            )
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
            ordered_history_entries=ordered_entries,
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
        try:
            decision = _PLAN_EXIT_DECISION_FROM_WIRE[body.plan_exit_decision]
        except KeyError:
            raise ValueError("terminal plan-exit decision is unknown")
        request = ResolvePlanExitRequest(
            command_kind="resolve_plan_exit",
            interaction_id=body.interaction_id,
            decision=decision,
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


def _transport_binding(
    *,
    attachment: wire.AttachmentIdentity,
    connection_id: str,
    generation: int,
) -> wire.TerminalClientTransportBindingIdentity:
    binding = wire.TerminalClientTransportBindingIdentity(
        attachment_id=attachment.attachment_id,
        attachment_generation=attachment.attachment_generation,
        connection_id=connection_id,
        transport_binding_generation=generation,
        bound_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    install_protobuf_fingerprint(
        "terminal-client-transport-binding:v1",
        binding,
        own_field="binding_fingerprint",
    )
    return binding


def _heartbeat_candidate_fingerprint(
    *,
    runtime_session_id: str,
    attachment_identity_fingerprint: str,
    semantic_winner_fingerprint: str,
    heartbeat_generation: int,
    previous_accepted_generation: int,
) -> str:
    return context_fingerprint(
        "terminal-heartbeat-candidate:v1",
        {
            "runtime_session_id": runtime_session_id,
            "attachment_identity_fingerprint": attachment_identity_fingerprint,
            "semantic_winner_fingerprint": semantic_winner_fingerprint,
            "heartbeat_generation": heartbeat_generation,
            "previous_accepted_generation": previous_accepted_generation,
        },
    )


def _control_cursor_from_wire(
    value: wire.ControlProjectionCursor,
) -> ControlProjectionCursor:
    if value is None:
        raise ValueError("terminal observation requires a control cursor")
    return ControlProjectionCursor(
        control_generation=value.control_generation,
        control_revision=value.control_revision,
        control_projection_fingerprint=value.control_projection_fingerprint,
        transition_prefix_accumulator=value.transition_prefix_accumulator,
        registry_contract_fingerprint=value.registry_contract_fingerprint,
        cursor_fingerprint=value.cursor_fingerprint,
    )


def _control_section_to_wire(section: str) -> int:
    try:
        return {
            "session_lifecycle": wire.CONTROL_SESSION_LIFECYCLE,
            "run_control": wire.CONTROL_RUN_CONTROL,
            "pending_interaction": wire.CONTROL_PENDING_INTERACTION,
            "prompt_queue": wire.CONTROL_PROMPT_QUEUE,
            "notifications": wire.CONTROL_NOTIFICATIONS,
        }[section]
    except KeyError as exc:  # pragma: no cover - runtime union is closed
        raise ValueError("terminal control transition section is unknown") from exc


def _bounded_snapshot_response(
    bundle,
    *,
    request_id: str,
    maximum_frame_bytes: int,
    validated_minimum_control_cursor_fingerprint: str | None = None,
):
    """Select the largest newest resident suffix that fits one wire frame."""

    def build(candidate_bundle):
        snapshot_kwargs = {}
        if validated_minimum_control_cursor_fingerprint is not None:
            snapshot_kwargs["validated_minimum_control_cursor_fingerprint"] = (
                validated_minimum_control_cursor_fingerprint
            )
        return wire.ServerFrame(
            snapshot=wire.ProjectionSnapshotResponse(
                snapshot=snapshot_to_wire(
                    candidate_bundle.session_snapshot,
                    control_snapshot=candidate_bundle.control_snapshot,
                    request_id=request_id,
                    **snapshot_kwargs,
                )
            )
        )

    full = build(bundle)
    if len(full.SerializeToString(deterministic=True)) <= maximum_frame_bytes:
        return full, bundle

    available = len(bundle.session_snapshot.viewport.ordered_resident_entries)
    lower = 0
    upper = available - 1
    winner = None
    winner_bundle = None
    while lower <= upper:
        count = (lower + upper) // 2
        candidate_bundle = fit_projection_snapshot_bundle_resident_suffix(
            bundle,
            maximum_entries=count,
        )
        candidate = build(candidate_bundle)
        encoded_bytes = len(candidate.SerializeToString(deterministic=True))
        if encoded_bytes <= maximum_frame_bytes:
            winner = candidate
            winner_bundle = candidate_bundle
            lower = count + 1
        else:
            upper = count - 1
    if winner is None or winner_bundle is None:
        raise RuntimeError(
            "terminal snapshot non-history envelope exceeds the frame bound"
        )
    if len(winner.SerializeToString(deterministic=True)) > maximum_frame_bytes:
        raise AssertionError("bounded terminal snapshot exceeded its wire budget")
    return winner, winner_bundle


async def _read_message(reader, message_type, *, maximum_bytes: int):
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
    return message


async def _read_frame(reader, message_type, *, maximum_bytes: int):
    message = await _read_message(reader, message_type, maximum_bytes=maximum_bytes)
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
            public_message=bounded_terminal_safe_public_text(
                message,
                maximum_code_points=512,
                maximum_utf8_bytes=2_048,
            ),
        )
    )


__all__ = ["TerminalProtocolServer"]
