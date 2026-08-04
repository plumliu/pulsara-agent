"""Process-local attachment and single-controller ownership."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from uuid import uuid4

from pulsara_agent.ports.terminal_application import TerminalCommandBinding
from pulsara_agent.primitives.context import context_fingerprint


@dataclass(frozen=True, slots=True)
class TerminalAttachmentLease:
    connection_id: str
    attachment_id: str
    runtime_session_id: str
    attachment_generation: int
    client_instance_id: str
    role: str
    controller_generation: int
    issued_at_utc: str
    expires_at_utc: str
    expires_at_monotonic: float
    identity_fingerprint: str


@dataclass(slots=True)
class _Attachment:
    lease: TerminalAttachmentLease
    last_heartbeat_monotonic: float


class TerminalAttachmentRegistry:
    """Allows many observers and exactly one generation-scoped controller."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        maximum_attachments: int = 8,
        heartbeat_timeout_seconds: float = 30.0,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.maximum_attachments = maximum_attachments
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._lock = RLock()
        self._attachments: dict[str, _Attachment] = {}
        self._client_generations: dict[str, int] = {}
        self._controller_attachment_id: str | None = None
        self._controller_generation = 0
        self._closed = False

    def attach(
        self,
        *,
        connection_id: str,
        client_instance_id: str,
        request_controller: bool,
    ) -> TerminalAttachmentLease:
        if not connection_id or not client_instance_id:
            raise ValueError("terminal connection and client identities are required")
        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            if self._closed:
                raise RuntimeError("terminal attachment admission is closed")
            if len(self._attachments) >= self.maximum_attachments:
                raise RuntimeError("terminal attachment capacity is exhausted")
            generation = self._client_generations.get(client_instance_id, 0) + 1
            self._client_generations[client_instance_id] = generation
            attachment_id = f"terminal-attachment:{uuid4().hex}"
            role = "observer"
            if request_controller and self._controller_attachment_id is None:
                self._controller_generation += 1
                self._controller_attachment_id = attachment_id
                role = "controller"
            lease = self._lease(
                attachment_id=attachment_id,
                attachment_generation=generation,
                connection_id=connection_id,
                client_instance_id=client_instance_id,
                role=role,
                controller_generation=self._controller_generation,
                now=now,
            )
            self._attachments[attachment_id] = _Attachment(
                lease=lease, last_heartbeat_monotonic=now
            )
            return lease

    def heartbeat(
        self, *, attachment_id: str, attachment_generation: int
    ) -> TerminalAttachmentLease:
        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            attachment = self._require_unlocked(attachment_id, attachment_generation)
            attachment.last_heartbeat_monotonic = now
            attachment.lease = self._lease(
                attachment_id=attachment.lease.attachment_id,
                attachment_generation=attachment.lease.attachment_generation,
                connection_id=attachment.lease.connection_id,
                client_instance_id=attachment.lease.client_instance_id,
                role=attachment.lease.role,
                controller_generation=self._controller_generation,
                now=now,
            )
            return attachment.lease

    def rebind_connection(
        self,
        *,
        attachment_id: str,
        attachment_generation: int,
        expected_previous_connection_id: str,
        resulting_connection_id: str,
    ) -> TerminalAttachmentLease:
        """Move only the physical binding; preserve semantic lease identity/expiry."""

        if not expected_previous_connection_id or not resulting_connection_id:
            raise ValueError("terminal attachment rebind identity is incomplete")
        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            attachment = self._require_unlocked(attachment_id, attachment_generation)
            if attachment.lease.connection_id == resulting_connection_id:
                return attachment.lease
            if attachment.lease.connection_id != expected_previous_connection_id:
                raise PermissionError("terminal attachment physical binding is stale")
            attachment.lease = replace(
                attachment.lease,
                connection_id=resulting_connection_id,
            )
            return attachment.lease

    def supersede_for_reconnect(
        self,
        *,
        previous_attachment_id: str,
        previous_attachment_generation: int,
        connection_id: str,
        client_instance_id: str,
        request_controller: bool,
    ) -> TerminalAttachmentLease:
        """Atomically replace one ACKed semantic attachment on ordinary reconnect.

        This is deliberately distinct from ``rebind_connection``.  A pre-ACK
        retry keeps the same semantic attachment, while an ordinary Ready
        reconnect installs the client's exact next attachment generation and
        makes the predecessor fail closed at the same linearization point.
        """

        if (
            not previous_attachment_id
            or previous_attachment_generation < 1
            or not connection_id
            or not client_instance_id
        ):
            raise ValueError("terminal reconnect successor identity is incomplete")
        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            if self._closed:
                raise RuntimeError("terminal attachment admission is closed")
            previous = self._require_unlocked(
                previous_attachment_id, previous_attachment_generation
            )
            if previous.lease.client_instance_id != client_instance_id:
                raise PermissionError("terminal reconnect predecessor client is stale")
            if self._client_generations.get(client_instance_id) != (
                previous_attachment_generation
            ):
                raise PermissionError(
                    "terminal reconnect predecessor generation is stale"
                )

            previous_was_controller = (
                self._controller_attachment_id == previous_attachment_id
            )
            self._attachments.pop(previous_attachment_id)
            if previous_was_controller:
                self._controller_attachment_id = None

            generation = previous_attachment_generation + 1
            self._client_generations[client_instance_id] = generation
            attachment_id = f"terminal-attachment:{uuid4().hex}"
            role = "observer"
            if request_controller and self._controller_attachment_id is None:
                self._controller_generation += 1
                self._controller_attachment_id = attachment_id
                role = "controller"
            elif previous_was_controller:
                # The predecessor controller was retired but the successor did
                # not request control.  This is still a controller-generation
                # transition and must invalidate every old command binding.
                self._controller_generation += 1

            lease = self._lease(
                attachment_id=attachment_id,
                attachment_generation=generation,
                connection_id=connection_id,
                client_instance_id=client_instance_id,
                role=role,
                controller_generation=self._controller_generation,
                now=now,
            )
            self._attachments[attachment_id] = _Attachment(
                lease=lease,
                last_heartbeat_monotonic=now,
            )
            return lease

    def takeover(
        self, *, attachment_id: str, attachment_generation: int
    ) -> TerminalAttachmentLease:
        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            target = self._require_unlocked(attachment_id, attachment_generation)
            previous_id = self._controller_attachment_id
            self._controller_generation += 1
            self._controller_attachment_id = attachment_id
            if previous_id is not None and previous_id in self._attachments:
                previous = self._attachments[previous_id]
                previous.lease = self._lease(
                    attachment_id=previous.lease.attachment_id,
                    attachment_generation=previous.lease.attachment_generation,
                    connection_id=previous.lease.connection_id,
                    client_instance_id=previous.lease.client_instance_id,
                    role="observer",
                    controller_generation=self._controller_generation,
                    now=previous.last_heartbeat_monotonic,
                )
            target.lease = self._lease(
                attachment_id=target.lease.attachment_id,
                attachment_generation=target.lease.attachment_generation,
                connection_id=target.lease.connection_id,
                client_instance_id=target.lease.client_instance_id,
                role="controller",
                controller_generation=self._controller_generation,
                now=now,
            )
            return target.lease

    def validate_controller(self, binding: TerminalCommandBinding) -> None:
        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            attachment = self._require_unlocked(
                binding.attachment_id, binding.attachment_generation
            )
            if (
                attachment.lease.client_instance_id != binding.client_instance_id
                or attachment.lease.role != "controller"
                or self._controller_attachment_id != binding.attachment_id
                or self._controller_generation != binding.expected_controller_generation
                or binding.runtime_session_id != self.runtime_session_id
            ):
                raise PermissionError("terminal controller lease is stale")

    def validate_attachment(self, binding: TerminalCommandBinding) -> None:
        """Validate attachment ownership without requiring controller role."""

        now = monotonic()
        with self._lock:
            self._retire_expired_unlocked(now)
            attachment = self._require_unlocked(
                binding.attachment_id, binding.attachment_generation
            )
            if (
                attachment.lease.client_instance_id != binding.client_instance_id
                or binding.runtime_session_id != self.runtime_session_id
            ):
                raise PermissionError("terminal attachment binding is stale")

    @property
    def controller_generation(self) -> int:
        with self._lock:
            return self._controller_generation

    @property
    def controller_attachment_id(self) -> str | None:
        with self._lock:
            return self._controller_attachment_id

    def detach(self, *, attachment_id: str, attachment_generation: int) -> None:
        with self._lock:
            attachment = self._attachments.get(attachment_id)
            if attachment is None:
                return
            if attachment.lease.attachment_generation != attachment_generation:
                raise PermissionError("terminal attachment generation is stale")
            self._attachments.pop(attachment_id, None)
            if self._controller_attachment_id == attachment_id:
                self._controller_attachment_id = None
                self._controller_generation += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._attachments.clear()
            self._controller_attachment_id = None
            self._controller_generation += 1

    def _require_unlocked(
        self, attachment_id: str, attachment_generation: int
    ) -> _Attachment:
        attachment = self._attachments.get(attachment_id)
        if (
            attachment is None
            or attachment.lease.attachment_generation != attachment_generation
        ):
            raise PermissionError("terminal attachment lease is unavailable")
        return attachment

    def _retire_expired_unlocked(self, now: float) -> None:
        expired = tuple(
            attachment_id
            for attachment_id, attachment in self._attachments.items()
            if now - attachment.last_heartbeat_monotonic
            >= self.heartbeat_timeout_seconds
        )
        for attachment_id in expired:
            self._attachments.pop(attachment_id, None)
            if self._controller_attachment_id == attachment_id:
                self._controller_attachment_id = None
                self._controller_generation += 1

    def _lease(
        self,
        *,
        attachment_id: str,
        attachment_generation: int,
        connection_id: str,
        client_instance_id: str,
        role: str,
        controller_generation: int,
        now: float,
    ) -> TerminalAttachmentLease:
        issued = datetime.now(timezone.utc)
        expires = issued + timedelta(seconds=self.heartbeat_timeout_seconds)
        issued_at_utc = issued.isoformat().replace("+00:00", "Z")
        expires_at_utc = expires.isoformat().replace("+00:00", "Z")
        payload = {
            "runtime_session_id": self.runtime_session_id,
            "connection_id": connection_id,
            "attachment_id": attachment_id,
            "attachment_generation": attachment_generation,
            "client_instance_id": client_instance_id,
            "role": role,
            "controller_generation": controller_generation,
            "issued_at_utc": issued_at_utc,
            "expires_at_utc": expires_at_utc,
        }
        return TerminalAttachmentLease(
            connection_id=connection_id,
            attachment_id=attachment_id,
            runtime_session_id=self.runtime_session_id,
            attachment_generation=attachment_generation,
            client_instance_id=client_instance_id,
            role=role,
            controller_generation=controller_generation,
            issued_at_utc=issued_at_utc,
            expires_at_utc=expires_at_utc,
            expires_at_monotonic=now + self.heartbeat_timeout_seconds,
            identity_fingerprint=context_fingerprint(
                "terminal-attachment-identity:v1", payload
            ),
        )


__all__ = ["TerminalAttachmentLease", "TerminalAttachmentRegistry"]
