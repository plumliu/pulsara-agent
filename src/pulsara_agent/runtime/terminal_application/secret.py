"""Attachment-bound MCP secret reveal and response hydration owner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock, Timer
from time import monotonic
from uuid import uuid4

from pulsara_agent.ports.terminal_application import TerminalCommandBinding
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.terminal_application.attachment import (
    TerminalAttachmentRegistry,
)


@dataclass(frozen=True, slots=True)
class TerminalSecretLeaseIdentity:
    attachment_id: str
    attachment_generation: int
    controller_generation: int
    interaction_id: str
    request_key: str
    secret_kind: str
    owner_epoch: int
    lease_generation: int
    expires_at_utc: str
    expires_at_monotonic: float
    identity_fingerprint: str


@dataclass(slots=True)
class _SecretLease:
    identity: TerminalSecretLeaseIdentity
    consumed: bool = False


@dataclass(slots=True)
class _SealedResponse:
    identity: TerminalSecretLeaseIdentity
    batch_owner_id: str
    batch_owner_generation: int
    batch_round_ordinal: int
    request_set_fingerprint: str
    request_fingerprint: str
    payload: bytearray
    consumed: bool = False


class TerminalMcpSecretService:
    """The only terminal boundary allowed to touch MCP private response bytes."""

    def __init__(
        self,
        *,
        host_session,
        attachments: TerminalAttachmentRegistry,
        maximum_secret_bytes: int = 64 * 1024,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        self._host_session = host_session
        self._attachments = attachments
        self._maximum_secret_bytes = maximum_secret_bytes
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lock = RLock()
        self._epoch = 1
        self._generation = 0
        self._leases: dict[str, _SecretLease] = {}
        self._responses: dict[str, _SealedResponse] = {}
        self._response_expiry_timers: dict[str, Timer] = {}
        self._closed = False

    def issue_url_reveal_lease(
        self,
        *,
        binding: TerminalCommandBinding,
        interaction_id: str,
        request_key: str,
    ) -> TerminalSecretLeaseIdentity:
        self._attachments.validate_controller(binding)
        pending = self._host_session.get_pending_interaction()
        if (
            pending is None
            or getattr(pending, "interaction_id", None) != interaction_id
        ):
            raise ValueError("MCP secret lease target is no longer pending")
        if getattr(pending, "kind", None) != "mcp_input_required":
            raise TypeError("secret reveal is only valid for MCP input-required")
        runtime_session = self._host_session.wiring.runtime_wiring.runtime_session
        port = runtime_session.mcp_tool_execution_port
        handle = (
            port.handle_for_interaction(interaction_id) if port is not None else None
        )
        if handle is None:
            raise RuntimeError("MCP secret reveal lost its pending owner")
        matching = tuple(
            slot
            for slot in handle.elicitation_batch_owner.item_slots
            if slot.request.key == request_key and slot.request.mode == "url"
        )
        if len(matching) != 1:
            raise ValueError("MCP URL request identity is unavailable")
        with self._lock:
            self._require_open_unlocked()
            self._generation += 1
            lease_id = f"terminal-secret-lease:{uuid4().hex}"
            expires_at_utc = (
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self._lease_ttl_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            payload = {
                "attachment_id": binding.attachment_id,
                "attachment_generation": binding.attachment_generation,
                "controller_generation": binding.expected_controller_generation,
                "interaction_id": interaction_id,
                "request_key": request_key,
                "owner_epoch": self._epoch,
                "lease_generation": self._generation,
                "expires_at_utc": expires_at_utc,
            }
            identity = TerminalSecretLeaseIdentity(
                attachment_id=binding.attachment_id,
                attachment_generation=binding.attachment_generation,
                controller_generation=binding.expected_controller_generation,
                interaction_id=interaction_id,
                request_key=request_key,
                secret_kind="private_url",
                owner_epoch=self._epoch,
                lease_generation=self._generation,
                expires_at_utc=expires_at_utc,
                expires_at_monotonic=monotonic() + self._lease_ttl_seconds,
                identity_fingerprint=context_fingerprint(
                    "terminal-secret-lease-identity:v1",
                    {"lease_id": lease_id, **payload},
                ),
            )
            self._leases[lease_id] = _SecretLease(identity=identity)
            return identity

    def reveal_url_once(
        self,
        *,
        lease_identity_fingerprint: str,
        binding: TerminalCommandBinding,
    ) -> bytes:
        self._attachments.validate_controller(binding)
        with self._lock:
            lease = next(
                (
                    item
                    for item in self._leases.values()
                    if item.identity.identity_fingerprint == lease_identity_fingerprint
                ),
                None,
            )
            if lease is None or lease.consumed:
                raise RuntimeError("terminal secret lease is unavailable")
            identity = lease.identity
            if (
                identity.owner_epoch != self._epoch
                or identity.expires_at_monotonic <= monotonic()
                or identity.attachment_id != binding.attachment_id
                or identity.attachment_generation != binding.attachment_generation
                or identity.controller_generation
                != binding.expected_controller_generation
            ):
                lease.consumed = True
                raise RuntimeError("terminal secret lease is stale")
            lease.consumed = True
        runtime_session = self._host_session.wiring.runtime_wiring.runtime_session
        port = runtime_session.mcp_tool_execution_port
        handle = port.handle_for_interaction(identity.interaction_id)
        if handle is None:
            raise RuntimeError("MCP secret reveal lost its pending owner")
        value = handle.elicitation_batch_owner.exact_url_for_display(
            request_key=identity.request_key
        ).encode("utf-8")
        if len(value) > self._maximum_secret_bytes:
            raise ValueError("MCP secret reveal exceeds terminal byte cap")
        return value

    def seal_form_response(
        self,
        *,
        binding: TerminalCommandBinding,
        interaction_id: str,
        request_key: str,
        plaintext_json: bytes,
    ) -> str:
        self._attachments.validate_controller(binding)
        if not 0 < len(plaintext_json) <= self._maximum_secret_bytes:
            raise ValueError("terminal secret response exceeds its byte cap")
        decoded = json.loads(plaintext_json)
        if not isinstance(decoded, dict):
            raise ValueError("MCP terminal response must be a JSON object")
        handle = self._pending_handle(interaction_id)
        matching = tuple(
            slot
            for slot in handle.elicitation_batch_owner.item_slots
            if slot.request.key == request_key and slot.request.mode == "form"
        )
        if len(matching) != 1:
            raise ValueError("MCP form request identity is unavailable")
        matched_request = matching[0].request
        batch_identity = handle.elicitation_batch_owner.identity
        expected_keys = handle.elicitation_batch_owner.identity.ordered_request_keys
        if tuple(sorted(decoded)) != expected_keys:
            raise ValueError(
                "MCP terminal response must cover the exact pending request batch"
            )
        if request_key not in decoded or not isinstance(decoded[request_key], dict):
            raise ValueError("MCP form response does not bind its exact request key")
        handle_id = f"terminal-sealed-response:{uuid4().hex}"
        with self._lock:
            self._require_open_unlocked()
            self._prune_expired_unlocked()
            self._generation += 1
            expires_at_utc = (
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self._lease_ttl_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            identity_payload = {
                "attachment_id": binding.attachment_id,
                "attachment_generation": binding.attachment_generation,
                "controller_generation": binding.expected_controller_generation,
                "interaction_id": interaction_id,
                "request_key": request_key,
                "secret_kind": "form_response",
                "owner_epoch": self._epoch,
                "lease_generation": self._generation,
                "expires_at_utc": expires_at_utc,
            }
            identity = TerminalSecretLeaseIdentity(
                **identity_payload,
                expires_at_monotonic=monotonic() + self._lease_ttl_seconds,
                identity_fingerprint=context_fingerprint(
                    "terminal-secret-lease-identity:v1",
                    {"lease_id": handle_id, **identity_payload},
                ),
            )
            self._responses[handle_id] = _SealedResponse(
                identity=identity,
                batch_owner_id=batch_identity.owner_id,
                batch_owner_generation=batch_identity.owner_generation,
                batch_round_ordinal=batch_identity.round_ordinal,
                request_set_fingerprint=batch_identity.request_set_fingerprint,
                request_fingerprint=matched_request.request_fingerprint,
                payload=bytearray(plaintext_json),
            )
            timer = Timer(
                self._lease_ttl_seconds,
                self._expire_response,
                args=(handle_id, identity.identity_fingerprint),
            )
            timer.daemon = True
            self._response_expiry_timers[handle_id] = timer
            timer.start()
        return handle_id

    def consume_response(
        self,
        *,
        handle_id: str,
        binding: TerminalCommandBinding,
        interaction_id: str,
    ) -> dict[str, dict[str, object]]:
        self._attachments.validate_controller(binding)
        pending_handle = self._pending_handle(interaction_id)
        with self._lock:
            self._prune_expired_unlocked()
            response = self._responses.get(handle_id)
            identity = response.identity if response is not None else None
            if (
                response is None
                or response.consumed
                or identity is None
                or identity.owner_epoch != self._epoch
                or identity.expires_at_monotonic <= monotonic()
                or identity.attachment_id != binding.attachment_id
                or identity.attachment_generation != binding.attachment_generation
                or identity.controller_generation
                != binding.expected_controller_generation
                or identity.interaction_id != interaction_id
                or identity.secret_kind != "form_response"
            ):
                raise RuntimeError("sealed MCP response handle is unavailable")
            matching = tuple(
                slot
                for slot in pending_handle.elicitation_batch_owner.item_slots
                if slot.request.key == identity.request_key
                and slot.request.mode == "form"
            )
            if len(matching) != 1:
                raise RuntimeError("sealed MCP response request binding is stale")
            batch_identity = pending_handle.elicitation_batch_owner.identity
            if (
                response.batch_owner_id != batch_identity.owner_id
                or response.batch_owner_generation != batch_identity.owner_generation
                or response.batch_round_ordinal != batch_identity.round_ordinal
                or response.request_set_fingerprint
                != batch_identity.request_set_fingerprint
                or response.request_fingerprint
                != matching[0].request.request_fingerprint
            ):
                self._release_response_unlocked(handle_id, response)
                raise RuntimeError("sealed MCP response exact request owner changed")
            response.consumed = True
            payload = bytes(response.payload)
            response.payload[:] = b"\x00" * len(response.payload)
            self._responses.pop(handle_id, None)
            timer = self._response_expiry_timers.pop(handle_id, None)
            if timer is not None:
                timer.cancel()
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in decoded.items()
        ):
            raise ValueError("sealed MCP response map is malformed")
        if tuple(sorted(decoded)) != (
            pending_handle.elicitation_batch_owner.identity.ordered_request_keys
        ):
            raise ValueError("sealed MCP response no longer matches its request batch")
        return {str(key): dict(value) for key, value in decoded.items()}

    def revoke_attachment(self, attachment_id: str) -> None:
        with self._lock:
            for lease in self._leases.values():
                if lease.identity.attachment_id == attachment_id:
                    lease.consumed = True
            for handle_id, response in tuple(self._responses.items()):
                if response.identity.attachment_id == attachment_id:
                    self._release_response_unlocked(handle_id, response)

    def revoke_interaction(self, interaction_id: str) -> None:
        with self._lock:
            for handle_id, response in tuple(self._responses.items()):
                if response.identity.interaction_id == interaction_id:
                    self._release_response_unlocked(handle_id, response)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._epoch += 1
            for lease in self._leases.values():
                lease.consumed = True
            for response in self._responses.values():
                response.payload[:] = b"\x00" * len(response.payload)
            self._responses.clear()
            for timer in self._response_expiry_timers.values():
                timer.cancel()
            self._response_expiry_timers.clear()

    def stop_admission(self) -> None:
        """Reject new secret state while existing leases remain revocable."""

        with self._lock:
            self._closed = True

    def _require_open_unlocked(self) -> None:
        if self._closed:
            raise RuntimeError("terminal secret service is closed")

    def _pending_handle(self, interaction_id: str):
        pending = self._host_session.get_pending_interaction()
        if (
            pending is None
            or getattr(pending, "interaction_id", None) != interaction_id
            or getattr(pending, "kind", None) != "mcp_input_required"
        ):
            raise ValueError("MCP secret response target is no longer pending")
        runtime_session = self._host_session.wiring.runtime_wiring.runtime_session
        port = runtime_session.mcp_tool_execution_port
        handle = (
            port.handle_for_interaction(interaction_id) if port is not None else None
        )
        if handle is None:
            raise RuntimeError("MCP secret response lost its pending owner")
        return handle

    def _expire_response(self, handle_id: str, identity_fingerprint: str) -> None:
        with self._lock:
            response = self._responses.get(handle_id)
            if (
                response is not None
                and response.identity.identity_fingerprint == identity_fingerprint
                and response.identity.expires_at_monotonic <= monotonic()
            ):
                self._release_response_unlocked(handle_id, response)

    def _prune_expired_unlocked(self) -> None:
        now = monotonic()
        for handle_id, response in tuple(self._responses.items()):
            if response.identity.expires_at_monotonic <= now:
                self._release_response_unlocked(handle_id, response)

    def _release_response_unlocked(
        self, handle_id: str, response: _SealedResponse
    ) -> None:
        response.consumed = True
        response.payload[:] = b"\x00" * len(response.payload)
        self._responses.pop(handle_id, None)
        timer = self._response_expiry_timers.pop(handle_id, None)
        if timer is not None:
            timer.cancel()


__all__ = ["TerminalMcpSecretService", "TerminalSecretLeaseIdentity"]
