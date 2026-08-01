"""Headless conformance consumer using only the frozen terminal wire boundary."""

from __future__ import annotations

import asyncio
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


@dataclass(slots=True)
class HeadlessTerminalConformanceClient:
    socket_path: Path
    client_instance_id: str
    launch_capability: bytes
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    hello: wire.HelloResponse | None = None
    attachment: wire.AttachmentIdentity | None = None

    async def connect(self, *, controller: bool = True) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket_path)
        )
        response = await self.exchange(
            wire.ClientFrame(
                hello=wire.HelloRequest(
                    request_id=self._id(),
                    supported_version_range=wire.ProtocolVersionRange(
                        major=PROTOCOL_MAJOR,
                        minimum_minor=PROTOCOL_MINOR,
                        maximum_minor=PROTOCOL_MINOR,
                        schema_contract_fingerprint=PROTOCOL_SCHEMA_FINGERPRINT,
                    ),
                    client_instance_id=self.client_instance_id,
                    client_build_identity="python-headless-conformance:v1",
                    supported_capabilities=(
                        "history_page_v1",
                        "presentation_root_advance_v1",
                    ),
                    requested_attachment_mode=(
                        wire.ATTACHMENT_ROLE_CONTROLLER
                        if controller
                        else wire.ATTACHMENT_ROLE_OBSERVER
                    ),
                    launch_capability=self.launch_capability,
                )
            )
        )
        if response.WhichOneof("response") != "hello":
            raise RuntimeError("terminal conformance hello failed")
        self.hello = response.hello

    async def attach(self, host_session_id: str, *, controller: bool = True) -> None:
        hello = self.hello
        if hello is None:
            raise RuntimeError("terminal conformance client has not negotiated")
        response = await self.exchange(
            wire.ClientFrame(
                attach=wire.AttachRequest(
                    request_id=self._id(),
                    hello_transcript_fingerprint=(hello.hello_transcript_fingerprint),
                    host_session_id=host_session_id,
                    requested_role=(
                        wire.ATTACHMENT_ROLE_CONTROLLER
                        if controller
                        else wire.ATTACHMENT_ROLE_OBSERVER
                    ),
                    attachment_challenge=hello.attachment_challenge,
                )
            )
        )
        if response.WhichOneof("response") != "attach":
            raise RuntimeError("terminal conformance attach failed")
        self.attachment = response.attach.attachment

    async def snapshot(self) -> wire.ProjectionSnapshotFrame:
        response = await self.exchange(
            wire.ClientFrame(
                snapshot=wire.ProjectionSnapshotRequest(request_id=self._id())
            )
        )
        if response.WhichOneof("response") != "snapshot":
            raise RuntimeError("terminal snapshot failed")
        return response.snapshot

    async def operational_snapshot(self) -> wire.OperationalSnapshotFrame:
        response = await self.exchange(
            wire.ClientFrame(
                operational_snapshot=wire.OperationalSnapshotRequest(
                    request_id=self._id()
                )
            )
        )
        if response.WhichOneof("response") != "operational_snapshot":
            raise RuntimeError("terminal operational snapshot failed")
        return response.operational_snapshot

    async def heartbeat(self) -> wire.AttachmentIdentity:
        attachment = self._require_attachment()
        response = await self.exchange(
            wire.ClientFrame(
                heartbeat=wire.HeartbeatRequest(
                    request_id=self._id(),
                    attachment_id=attachment.attachment_id,
                    attachment_generation=attachment.attachment_generation,
                )
            )
        )
        if response.WhichOneof("response") != "heartbeat":
            raise RuntimeError("terminal heartbeat failed")
        self.attachment = response.heartbeat.attachment
        return response.heartbeat.attachment

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
        response = await self.exchange(
            wire.ClientFrame(
                observe_next=wire.ObserveNextRequest(
                    request_id=self._id(),
                    after_authority_high_water=authority_high_water,
                    after_projection_revision=projection_revision,
                    after_operational_generation=operational_generation,
                    after_operational_cursor=operational_cursor,
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
        self.attachment = None

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
