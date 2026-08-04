"""Headless conformance consumer using only the frozen terminal wire boundary."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.terminal_protocol.codec import (
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    PROTOCOL_SCHEMA_FINGERPRINT,
)
from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
from pulsara_agent.terminal_protocol.generated.terminal_client_fingerprint import (
    attachment_challenge_commitment,
    install_protobuf_fingerprint,
)


_S1_REQUIRED_CAPABILITIES = (
    wire.PRESENTATION_SNAPSHOT_V1,
    wire.OPERATIONAL_SNAPSHOT_V1,
    wire.BOOTSTRAP_CARRIER_V1,
    wire.LAUNCH_AUTH_PREFACE_V1,
    wire.ATTACH_ACK_V1,
)
_S2_REQUIRED_CAPABILITIES = tuple(
    sorted(
        {
            *_S1_REQUIRED_CAPABILITIES,
            wire.HISTORY_PAGE_V1,
            wire.OBSERVATION_STREAM_V1,
            wire.ROOT_ADVANCE_V1,
            wire.GAP_REBUILD_V1,
            wire.CONTROL_PROJECTION_OBSERVATION_V1,
            wire.RECONNECT_AUTH_ROTATION_V1,
        }
    )
)
_HEADLESS_SUPPORTED_CAPABILITIES = tuple(
    sorted(
        {
            *_S1_REQUIRED_CAPABILITIES,
            wire.HISTORY_PAGE_V1,
            wire.OBSERVATION_STREAM_V1,
            wire.ROOT_ADVANCE_V1,
            wire.GAP_REBUILD_V1,
            wire.CONTROL_PROJECTION_OBSERVATION_V1,
            wire.RECONNECT_AUTH_ROTATION_V1,
            wire.CONTROLLER_COMMAND_V1,
            wire.COMMAND_QUERY_V1,
            wire.SESSION_SUCCESSOR_V1,
        }
    )
)


@dataclass(slots=True)
class HeadlessTerminalConformanceClient:
    socket_path: Path
    client_instance_id: str
    launch_id: str
    launch_capability: bytes
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    auth_result: wire.TerminalTransportAuthResult | None = None
    candidate: wire.HandshakeRecoveryCandidateIdentity | None = None
    hello: wire.ServerHello | None = None
    attach_receipt: wire.AttachResultReceipt | None = None
    attachment: wire.AttachmentIdentity | None = None
    reconnect_credential: wire.ReconnectCredentialCarrier | None = None
    control_cursor: wire.ControlProjectionCursor | None = None
    heartbeat_generation: int = 0

    async def connect(
        self,
        *,
        host_session_id: str,
        runtime_session_id: str,
        controller: bool = True,
    ) -> None:
        requested_role = (
            wire.ATTACHMENT_ROLE_CONTROLLER
            if controller
            else wire.ATTACHMENT_ROLE_OBSERVER
        )
        candidate = self._candidate(
            host_session_id=host_session_id,
            runtime_session_id=runtime_session_id,
            requested_role=requested_role,
            generation=1,
        )
        await self._connect_and_negotiate(candidate=candidate, reconnect=None)

    async def ordinary_reconnect(self, *, controller: bool | None = None) -> None:
        """Replace one Ready attachment through the formal rotating credential."""

        previous_candidate = self.candidate
        previous_attachment = self.attachment
        credential = self.reconnect_credential
        if (
            previous_candidate is None
            or previous_attachment is None
            or credential is None
        ):
            raise RuntimeError("terminal reconnect authority is unavailable")
        requested_role = (
            previous_candidate.requested_attachment_role
            if controller is None
            else (
                wire.ATTACHMENT_ROLE_CONTROLLER
                if controller
                else wire.ATTACHMENT_ROLE_OBSERVER
            )
        )
        candidate = self._candidate(
            host_session_id=previous_candidate.host_session_id,
            runtime_session_id=previous_candidate.requested_runtime_session_id,
            requested_role=requested_role,
            generation=previous_candidate.attachment_attempt_generation + 1,
        )
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None
        await self._connect_and_negotiate(
            candidate=candidate,
            reconnect=credential,
        )
        await self.attach(candidate.host_session_id, controller=controller is not False)
        successor = self._require_attachment()
        if (
            successor.attachment_generation
            != previous_attachment.attachment_generation + 1
            or successor.attachment_id == previous_attachment.attachment_id
        ):
            raise RuntimeError("terminal reconnect did not install a successor")
        self.heartbeat_generation = 0

    async def attach(self, host_session_id: str, *, controller: bool = True) -> None:
        hello = self.hello
        candidate = self.candidate
        if hello is None or candidate is None:
            raise RuntimeError("terminal conformance client has not negotiated")
        expected_commitment = attachment_challenge_commitment(
            auth_attempt_id=hello.receipt.transport_auth_attempt_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            candidate_id=candidate.candidate_id,
            connection_id=hello.receipt.current_connection_id,
            negotiation_winner_fingerprint=(
                hello.negotiation_winner.negotiation_winner_fingerprint
            ),
            request_id=hello.receipt.request_id,
            challenge=bytes(hello.receipt.attachment_challenge),
        )
        if expected_commitment != hello.receipt.attachment_challenge_commitment:
            raise RuntimeError("terminal challenge commitment mismatch")
        response = await self.exchange(
            wire.ClientFrame(
                attach=wire.AttachRequest(
                    request_id=self._id(),
                    handshake_candidate_id=candidate.candidate_id,
                    handshake_candidate_fingerprint=candidate.candidate_fingerprint,
                    negotiation_winner_fingerprint=(
                        hello.negotiation_winner.negotiation_winner_fingerprint
                    ),
                    current_hello_receipt_fingerprint=(
                        hello.receipt.hello_receipt_fingerprint
                    ),
                    attachment_challenge=hello.receipt.attachment_challenge,
                    attachment_challenge_commitment=expected_commitment,
                )
            )
        )
        if response.WhichOneof("response") != "attach":
            raise RuntimeError("terminal conformance attach failed")
        receipt = response.attach
        attachment = receipt.attach_semantic_winner.attachment
        if attachment.runtime_session_id != candidate.requested_runtime_session_id:
            raise RuntimeError("terminal conformance attachment is stale")
        ack = wire.AttachReceiptAck(
            request_id=self._id(),
            attachment_id=attachment.attachment_id,
            attachment_generation=attachment.attachment_generation,
            semantic_winner_fingerprint=(
                receipt.attach_semantic_winner.semantic_winner_fingerprint
            ),
            current_transport_binding=receipt.current_transport_binding,
            attach_result_receipt_fingerprint=receipt.receipt_fingerprint,
        )
        install_protobuf_fingerprint(
            "terminal-attach-receipt-ack:v1",
            ack,
            own_field="ack_fingerprint",
        )
        ack_response = await self.exchange(wire.ClientFrame(attach_ack=ack))
        if ack_response.WhichOneof("response") != "attach_ack":
            raise RuntimeError("terminal conformance attach ACK failed")
        self.attach_receipt = receipt
        self.attachment = attachment
        if not receipt.HasField("next_reconnect_credential_carrier"):
            raise RuntimeError("terminal S2 reconnect credential is unavailable")
        self.reconnect_credential = wire.ReconnectCredentialCarrier()
        self.reconnect_credential.CopyFrom(receipt.next_reconnect_credential_carrier)

    async def attach_with_lost_ack_result(self) -> None:
        """Commit Attach/ACK, then drop the physical ACK response for recovery tests."""

        hello = self.hello
        candidate = self.candidate
        if hello is None or candidate is None:
            raise RuntimeError("terminal conformance client has not negotiated")
        commitment = attachment_challenge_commitment(
            auth_attempt_id=hello.receipt.transport_auth_attempt_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            candidate_id=candidate.candidate_id,
            connection_id=hello.receipt.current_connection_id,
            negotiation_winner_fingerprint=(
                hello.negotiation_winner.negotiation_winner_fingerprint
            ),
            request_id=hello.receipt.request_id,
            challenge=bytes(hello.receipt.attachment_challenge),
        )
        response = await self.exchange(
            wire.ClientFrame(
                attach=wire.AttachRequest(
                    request_id=self._id(),
                    handshake_candidate_id=candidate.candidate_id,
                    handshake_candidate_fingerprint=candidate.candidate_fingerprint,
                    negotiation_winner_fingerprint=(
                        hello.negotiation_winner.negotiation_winner_fingerprint
                    ),
                    current_hello_receipt_fingerprint=(
                        hello.receipt.hello_receipt_fingerprint
                    ),
                    attachment_challenge=hello.receipt.attachment_challenge,
                    attachment_challenge_commitment=commitment,
                )
            )
        )
        receipt = response.attach
        attachment = receipt.attach_semantic_winner.attachment
        ack = wire.AttachReceiptAck(
            request_id=self._id(),
            attachment_id=attachment.attachment_id,
            attachment_generation=attachment.attachment_generation,
            semantic_winner_fingerprint=(
                receipt.attach_semantic_winner.semantic_winner_fingerprint
            ),
            current_transport_binding=receipt.current_transport_binding,
            attach_result_receipt_fingerprint=receipt.receipt_fingerprint,
        )
        install_protobuf_fingerprint(
            "terminal-attach-receipt-ack:v1",
            ack,
            own_field="ack_fingerprint",
        )
        if self.writer is None:
            raise RuntimeError("terminal conformance client is disconnected")
        frame = wire.ClientFrame(attach_ack=ack)
        payload = frame.SerializeToString(deterministic=True)
        self.writer.write(len(payload).to_bytes(4, "big") + payload)
        await self.writer.drain()
        self.attach_receipt = receipt
        self.attachment = attachment
        # Let the server commit/write the ACK before abandoning the response.
        await asyncio.sleep(0.05)
        self.writer.close()
        await self.writer.wait_closed()
        self.reader = None
        self.writer = None

    async def send_attach_and_lose_result(self) -> None:
        """Send a valid Attach, then abandon its physical response pre-ACK."""

        hello = self.hello
        candidate = self.candidate
        if hello is None or candidate is None or self.writer is None:
            raise RuntimeError("terminal conformance client has not negotiated")
        commitment = attachment_challenge_commitment(
            auth_attempt_id=hello.receipt.transport_auth_attempt_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            candidate_id=candidate.candidate_id,
            connection_id=hello.receipt.current_connection_id,
            negotiation_winner_fingerprint=(
                hello.negotiation_winner.negotiation_winner_fingerprint
            ),
            request_id=hello.receipt.request_id,
            challenge=bytes(hello.receipt.attachment_challenge),
        )
        frame = wire.ClientFrame(
            attach=wire.AttachRequest(
                request_id=self._id(),
                handshake_candidate_id=candidate.candidate_id,
                handshake_candidate_fingerprint=candidate.candidate_fingerprint,
                negotiation_winner_fingerprint=(
                    hello.negotiation_winner.negotiation_winner_fingerprint
                ),
                current_hello_receipt_fingerprint=(
                    hello.receipt.hello_receipt_fingerprint
                ),
                attachment_challenge=hello.receipt.attachment_challenge,
                attachment_challenge_commitment=commitment,
            )
        )
        payload = frame.SerializeToString(deterministic=True)
        self.writer.write(len(payload).to_bytes(4, "big") + payload)
        await self.writer.drain()
        await asyncio.sleep(0.05)
        self.writer.close()
        await self.writer.wait_closed()
        self.reader = None
        self.writer = None

    async def recover_lost_attach_ack(self) -> wire.TerminalTransportAuthResult:
        """Consume only the typed auth tombstone branch; never replay Hello/Attach."""

        candidate = self.candidate
        attachment = self.attachment
        receipt = self.attach_receipt
        if candidate is None or attachment is None or receipt is None:
            raise RuntimeError("terminal ACK recovery predecessor is unavailable")
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket_path)
        )
        preface = wire.TerminalTransportAuthPreface(
            preface_version=1,
            auth_request_id=self._id(),
            client_instance_id=self.client_instance_id,
            handshake_candidate_id=candidate.candidate_id,
            handshake_candidate_fingerprint=candidate.candidate_fingerprint,
            connection_nonce=secrets.token_bytes(32),
            initial_launch=wire.InitialLaunchCredential(
                launch_id=self.launch_id,
                launch_capability=self.launch_capability,
            ),
        )
        install_protobuf_fingerprint(
            "terminal-transport-auth-preface:v1",
            preface,
            own_field="preface_fingerprint",
        )
        result = await self._exchange_raw(preface, wire.TerminalTransportAuthResult)
        if (
            result.disposition != wire.TRANSPORT_ACK_RESULT_RECOVERY
            or not result.HasField("recovered_attach_ack_result")
            or not result.HasField("recovered_transport_binding")
        ):
            raise RuntimeError("terminal ACK tombstone recovery was not returned")
        recovered = result.recovered_transport_binding
        if (
            recovered.previous_transport_binding_fingerprint
            != receipt.current_transport_binding.binding_fingerprint
            or result.recovered_attach_ack_result.attachment_id
            != attachment.attachment_id
        ):
            raise RuntimeError("terminal ACK recovery authority drifted")
        receipt.current_transport_binding.CopyFrom(
            recovered.resulting_transport_binding
        )
        self.auth_result = result
        return result

    async def snapshot(
        self,
        *,
        minimum_control_cursor: wire.ControlProjectionCursor | None = None,
    ) -> wire.ProjectionSnapshotFrame:
        attachment = self._require_attachment()
        request = wire.ProjectionSnapshotRequest(
            request_id=self._id(), runtime_session_id=attachment.runtime_session_id
        )
        if minimum_control_cursor is not None:
            request.minimum_observed_control_cursor.CopyFrom(minimum_control_cursor)
        install_protobuf_fingerprint(
            "terminal-projection-snapshot-request:v2",
            request,
            own_field="request_fingerprint",
        )
        response = await self.exchange(wire.ClientFrame(snapshot=request))
        if response.WhichOneof("response") != "snapshot":
            raise RuntimeError("terminal snapshot failed")
        outcome = response.snapshot.WhichOneof("outcome")
        if outcome != "snapshot":
            raise RuntimeError(f"terminal snapshot requires rebase: {outcome}")
        snapshot = response.snapshot.snapshot
        self.control_cursor = wire.ControlProjectionCursor()
        self.control_cursor.CopyFrom(snapshot.control_projection_snapshot.cursor)
        return snapshot

    async def operational_snapshot(self) -> wire.OperationalSnapshotFrame:
        attachment = self._require_attachment()
        receipt = self.attach_receipt
        if receipt is None:
            raise RuntimeError("terminal conformance attach receipt is unavailable")
        request = wire.OperationalSnapshotRequest(
            request_id=self._id(),
            runtime_session_id=attachment.runtime_session_id,
            attachment_id=attachment.attachment_id,
            attachment_generation=attachment.attachment_generation,
            attachment_identity_fingerprint=attachment.identity_fingerprint,
            current_transport_binding=receipt.current_transport_binding,
            requested_after_operational_generation=0,
            requested_after_operational_cursor=0,
        )
        install_protobuf_fingerprint(
            "terminal-operational-snapshot-request:v1",
            request,
            own_field="request_fingerprint",
        )
        response = await self.exchange(wire.ClientFrame(operational_snapshot=request))
        if response.WhichOneof("response") != "operational_snapshot":
            raise RuntimeError("terminal operational snapshot failed")
        return response.operational_snapshot

    async def heartbeat(self) -> wire.AttachmentIdentity:
        attachment = self._require_attachment()
        receipt = self.attach_receipt
        if receipt is None:
            raise RuntimeError("terminal conformance attach receipt is unavailable")
        next_generation = self.heartbeat_generation + 1
        semantic_winner = receipt.attach_semantic_winner
        candidate_fingerprint = context_fingerprint(
            "terminal-heartbeat-candidate:v1",
            {
                "runtime_session_id": attachment.runtime_session_id,
                "attachment_identity_fingerprint": attachment.identity_fingerprint,
                "semantic_winner_fingerprint": (
                    semantic_winner.semantic_winner_fingerprint
                ),
                "heartbeat_generation": next_generation,
                "previous_accepted_generation": self.heartbeat_generation,
            },
        )
        request = wire.HeartbeatRequest(
            request_id=self._id(),
            runtime_session_id=attachment.runtime_session_id,
            attachment_id=attachment.attachment_id,
            attachment_generation=attachment.attachment_generation,
            attachment_identity_fingerprint=attachment.identity_fingerprint,
            attach_semantic_winner_fingerprint=(
                semantic_winner.semantic_winner_fingerprint
            ),
            current_transport_binding=receipt.current_transport_binding,
            heartbeat_generation=next_generation,
            previous_accepted_heartbeat_generation=self.heartbeat_generation,
            heartbeat_candidate_fingerprint=candidate_fingerprint,
        )
        install_protobuf_fingerprint(
            "terminal-heartbeat-request:v1",
            request,
            own_field="request_fingerprint",
        )
        response = await self.exchange(wire.ClientFrame(heartbeat=request))
        if (
            response.WhichOneof("response") != "heartbeat"
            or response.heartbeat.WhichOneof("outcome") != "accepted"
        ):
            raise RuntimeError("terminal heartbeat failed")
        self.heartbeat_generation = next_generation
        return attachment

    async def rebuild_after_gap(
        self,
    ) -> tuple[wire.ProjectionSnapshotFrame, wire.OperationalSnapshotFrame]:
        """Use only formal snapshot RPCs to replace both observation cursors."""

        projection = await self.snapshot()
        operational = await self.operational_snapshot()
        return projection, operational

    async def observe_next(
        self,
        *,
        authority_high_water: int,
        projection_revision: int,
        operational_generation: int = 1,
        operational_cursor: int,
        maximum_wait_ms: int = 50,
    ) -> wire.ObservationResponse:
        if self.control_cursor is None:
            raise RuntimeError("terminal control cursor requires a projection snapshot")
        response = await self.exchange(
            wire.ClientFrame(
                observe_next=wire.ObserveNextRequest(
                    request_id=self._id(),
                    after_authority_high_water=authority_high_water,
                    after_projection_revision=projection_revision,
                    after_operational_generation=operational_generation,
                    after_operational_cursor=operational_cursor,
                    after_control_cursor=self.control_cursor,
                    maximum_wait_ms=maximum_wait_ms,
                )
            )
        )
        if response.WhichOneof("response") != "observation":
            raise RuntimeError("terminal observation failed")
        return response.observation

    async def page(
        self,
        cursor: wire.PresentationHistoryCursor,
        *,
        direction: int,
        maximum_cells: int = 64,
    ) -> wire.HistoryPageResponse:
        response = await self.exchange(
            wire.ClientFrame(
                history_page=wire.HistoryPageRequest(
                    request_id=self._id(),
                    runtime_session_id=cursor.runtime_session_id,
                    cursor=cursor,
                    direction=direction,
                    maximum_cells=maximum_cells,
                    maximum_decoded_bytes=1024 * 1024,
                    expected_projection_contract_fingerprint=(
                        cursor.root_identity.history_projection_contract_fingerprint
                    ),
                )
            )
        )
        if response.WhichOneof("response") != "history_page":
            raise RuntimeError("terminal history page failed")
        return response.history_page

    async def query_command(self, command_id: str) -> wire.QueryCommandResponse:
        attachment = self._require_attachment()
        response = await self.exchange(
            wire.ClientFrame(
                query_command=wire.QueryCommandRequest(
                    request_id=self._id(),
                    runtime_session_id=attachment.runtime_session_id,
                    original_client_instance_id=self.client_instance_id,
                    command_id=command_id,
                )
            )
        )
        if response.WhichOneof("response") != "query_command":
            raise RuntimeError("terminal command query failed")
        return response.query_command

    async def submit_prompt(
        self,
        *,
        target_id: str,
        text: str,
        delivery_mode: int = wire.SubmitPromptCommand.AUTO,
        command_id: str | None = None,
        client_submission_id: str | None = None,
    ) -> wire.CommandOutcome:
        command_id = command_id or f"command:{uuid4().hex}"
        client_submission_id = client_submission_id or f"submission:{uuid4().hex}"
        mode = {
            wire.SubmitPromptCommand.AUTO: "auto",
            wire.SubmitPromptCommand.STEER: "steer",
            wire.SubmitPromptCommand.FOLLOW_UP: "follow_up",
        }[delivery_mode]
        binding = self._binding(
            command_id=command_id,
            target_id=target_id,
            command_kind="submit_prompt",
            payload={
                "command_kind": "submit_prompt",
                "client_submission_id": client_submission_id,
                "text": text,
                "requested_delivery_mode": mode,
            },
        )
        response = await self.exchange(
            wire.ClientFrame(
                mutation=wire.MutationCommand(
                    request_id=self._id(),
                    submit_prompt=wire.SubmitPromptCommand(
                        binding=binding,
                        client_submission_id=client_submission_id,
                        text=text,
                        requested_delivery_mode=delivery_mode,
                    ),
                )
            )
        )
        if response.WhichOneof("response") != "command_outcome":
            raise RuntimeError("terminal prompt command failed")
        return response.command_outcome

    async def detach(self) -> wire.CommandOutcome:
        attachment = self._require_attachment()
        command_id = f"command:{uuid4().hex}"
        binding = self._binding(
            command_id=command_id,
            target_id=attachment.runtime_session_id,
            command_kind="detach_session",
            payload={"command_kind": "detach_session"},
        )
        response = await self.exchange(
            wire.ClientFrame(
                mutation=wire.MutationCommand(
                    request_id=self._id(),
                    detach_session=wire.DetachSessionCommand(binding=binding),
                )
            )
        )
        if response.WhichOneof("response") != "command_outcome":
            raise RuntimeError("terminal detach failed")
        self.attachment = None
        self.reconnect_credential = None
        return response.command_outcome

    async def start_successor_session(
        self,
        *,
        target_id: str,
        source_capacity_state_fingerprint: str,
        command_id: str | None = None,
    ) -> wire.CommandOutcome:
        command_id = command_id or f"command:{uuid4().hex}"
        binding = self._binding(
            command_id=command_id,
            target_id=target_id,
            command_kind="start_successor_session",
            payload={
                "command_kind": "start_successor_session",
                "source_capacity_state_fingerprint": (
                    source_capacity_state_fingerprint
                ),
            },
        )
        response = await self.exchange(
            wire.ClientFrame(
                mutation=wire.MutationCommand(
                    request_id=self._id(),
                    start_successor_session=wire.StartSuccessorSessionCommand(
                        binding=binding,
                        source_capacity_state_fingerprint=(
                            source_capacity_state_fingerprint
                        ),
                    ),
                )
            )
        )
        if response.WhichOneof("response") != "command_outcome":
            raise RuntimeError("terminal successor command failed")
        return response.command_outcome

    async def close_session(
        self,
        *,
        target_id: str,
        close_conversation: bool = False,
        command_id: str | None = None,
    ) -> wire.CommandOutcome:
        command_id = command_id or f"command:{uuid4().hex}"
        binding = self._binding(
            command_id=command_id,
            target_id=target_id,
            command_kind="close_session",
            payload={
                "command_kind": "close_session",
                "close_conversation": close_conversation,
            },
        )
        response = await self.exchange(
            wire.ClientFrame(
                mutation=wire.MutationCommand(
                    request_id=self._id(),
                    close_session=wire.CloseSessionCommand(
                        binding=binding,
                        close_conversation=close_conversation,
                    ),
                )
            )
        )
        if response.WhichOneof("response") != "command_outcome":
            raise RuntimeError("terminal close command failed")
        return response.command_outcome

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None
        self.hello = None
        self.auth_result = None
        self.candidate = None
        self.attach_receipt = None
        self.attachment = None
        self.reconnect_credential = None
        self.heartbeat_generation = 0

    async def _exchange_raw(self, request, response_type):
        if self.reader is None or self.writer is None:
            raise RuntimeError("terminal conformance client is disconnected")
        payload = request.SerializeToString(deterministic=True)
        self.writer.write(len(payload).to_bytes(4, "big") + payload)
        await self.writer.drain()
        size = int.from_bytes(await self.reader.readexactly(4), "big")
        response = response_type()
        response.ParseFromString(await self.reader.readexactly(size))
        return response

    def _candidate(
        self,
        *,
        host_session_id: str,
        runtime_session_id: str,
        requested_role: int,
        generation: int,
    ) -> wire.HandshakeRecoveryCandidateIdentity:
        candidate = wire.HandshakeRecoveryCandidateIdentity(
            candidate_version=1,
            client_instance_id=self.client_instance_id,
            attachment_attempt_generation=generation,
            host_session_id=host_session_id,
            requested_runtime_session_id=runtime_session_id,
            requested_attachment_role=requested_role,
            minimum_protocol_major=PROTOCOL_MAJOR,
            minimum_protocol_minor=PROTOCOL_MINOR,
            maximum_protocol_major=PROTOCOL_MAJOR,
            maximum_protocol_minor=PROTOCOL_MINOR,
            client_build_identity="python-headless-conformance:v2",
            supported_capabilities=_HEADLESS_SUPPORTED_CAPABILITIES,
            required_capabilities=_S2_REQUIRED_CAPABILITIES,
            schema_contract_fingerprint=PROTOCOL_SCHEMA_FINGERPRINT,
        )
        install_protobuf_fingerprint(
            "terminal-handshake-recovery-candidate:v1",
            candidate,
            own_field="candidate_fingerprint",
            clear_fields=("candidate_id",),
        )
        candidate.candidate_id = (
            "handshake:" + candidate.candidate_fingerprint.removeprefix("sha256:")
        )
        return candidate

    async def _connect_and_negotiate(
        self,
        *,
        candidate: wire.HandshakeRecoveryCandidateIdentity,
        reconnect: wire.ReconnectCredentialCarrier | None,
    ) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket_path)
        )
        preface = wire.TerminalTransportAuthPreface(
            preface_version=1,
            auth_request_id=self._id(),
            client_instance_id=self.client_instance_id,
            handshake_candidate_id=candidate.candidate_id,
            handshake_candidate_fingerprint=candidate.candidate_fingerprint,
            connection_nonce=secrets.token_bytes(32),
        )
        if reconnect is None:
            preface.initial_launch.CopyFrom(
                wire.InitialLaunchCredential(
                    launch_id=self.launch_id,
                    launch_capability=self.launch_capability,
                )
            )
        else:
            identity = reconnect.public_identity
            if (
                identity.client_instance_id != self.client_instance_id
                or identity.expected_next_attachment_attempt_generation
                != candidate.attachment_attempt_generation
            ):
                raise RuntimeError("terminal reconnect credential is stale")
            preface.reconnect.CopyFrom(
                wire.ReconnectCredential(
                    reconnect_credential_id=identity.reconnect_credential_id,
                    reconnect_capability=reconnect.reconnect_capability,
                    previous_attachment_id=identity.previous_attachment_id,
                    previous_attachment_generation=(
                        identity.previous_attachment_generation
                    ),
                )
            )
        install_protobuf_fingerprint(
            "terminal-transport-auth-preface:v1",
            preface,
            own_field="preface_fingerprint",
        )
        auth_result = await self._exchange_raw(
            preface, wire.TerminalTransportAuthResult
        )
        if auth_result.disposition not in {
            wire.TRANSPORT_AUTHENTICATED,
            wire.TRANSPORT_COMPATIBLE_AUTH_WINNER,
        }:
            raise RuntimeError("terminal transport authentication failed")
        response = await self.exchange(
            wire.ClientFrame(
                hello=wire.HelloRequest(
                    request_id=self._id(),
                    transport_auth_attempt_id=auth_result.auth_attempt_id,
                    transport_auth_result_fingerprint=(auth_result.result_fingerprint),
                    handshake_candidate=candidate,
                )
            )
        )
        if (
            response.WhichOneof("response") != "hello"
            or response.hello.WhichOneof("outcome") != "accepted"
        ):
            raise RuntimeError("terminal conformance hello failed")
        self.auth_result = auth_result
        self.candidate = candidate
        self.hello = response.hello.accepted

    async def exchange(self, request: wire.ClientFrame) -> wire.ServerFrame:
        if self.reader is None or self.writer is None:
            raise RuntimeError("terminal conformance client is disconnected")
        payload = request.SerializeToString(deterministic=True)
        self.writer.write(len(payload).to_bytes(4, "big") + payload)
        await self.writer.drain()
        size = int.from_bytes(await self.reader.readexactly(4), "big")
        response = wire.ServerFrame()
        response.ParseFromString(await self.reader.readexactly(size))
        if response.WhichOneof("response") == "error":
            raise RuntimeError(response.error.stable_code)
        return response

    def _binding(
        self,
        *,
        command_id: str,
        target_id: str,
        command_kind: str,
        payload: dict[str, object],
    ) -> wire.CommandBinding:
        attachment = self._require_attachment()
        binding_payload = {
            "client_instance_id": self.client_instance_id,
            "attachment_id": attachment.attachment_id,
            "attachment_generation": attachment.attachment_generation,
            "command_id": command_id,
            "runtime_session_id": attachment.runtime_session_id,
            "expected_target_id": target_id,
            "expected_target_generation": 1,
            "expected_controller_generation": attachment.controller_generation,
        }
        fingerprint = context_fingerprint(
            f"terminal-command-request:{command_kind}:v1",
            {"binding": binding_payload, "payload": payload},
        )
        return wire.CommandBinding(
            **binding_payload, request_semantic_fingerprint=fingerprint
        )

    def _require_attachment(self) -> wire.AttachmentIdentity:
        if self.attachment is None:
            raise RuntimeError("terminal conformance client is not attached")
        return self.attachment

    @staticmethod
    def _id() -> str:
        return f"headless:{uuid4().hex}"


__all__ = ["HeadlessTerminalConformanceClient"]
