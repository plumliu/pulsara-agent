"""Process-local transport authentication for terminal Protocol 2.0."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
from pulsara_agent.terminal_protocol.generated.terminal_client_fingerprint import (
    install_protobuf_fingerprint,
    validate_protobuf_fingerprint,
)


PREFACE_MAXIMUM_BYTES = 16 * 1024
PREFACE_DEADLINE_SECONDS = 2.0


@dataclass(slots=True)
class _InitialCredentialRecord:
    launch_id: str
    capability_commitment: bytes
    issued_at: datetime
    expires_at: datetime
    client_instance_id: str | None = None
    candidate_id: str | None = None
    candidate_fingerprint: str | None = None
    acknowledged: bool = False
    acknowledged_at: datetime | None = None
    request_receipts: dict[str, tuple[str, wire.TerminalTransportAuthResult]] = field(
        default_factory=dict
    )


class TerminalTransportAuthOwner:
    """Single process owner for launch credentials and auth receipts."""

    def __init__(
        self,
        *,
        credential_ttl_seconds: float = 30 * 60,
        initial_launch_id: str | None = None,
        initial_launch_capability: bytes | None = None,
    ) -> None:
        if credential_ttl_seconds <= 0 or credential_ttl_seconds > 30 * 60:
            raise ValueError("terminal launch credential TTL must be 1..1800 seconds")
        self._ttl = credential_ttl_seconds
        self._lock = RLock()
        self._commitment_key = secrets.token_bytes(32)
        self._initial: dict[str, _InitialCredentialRecord] = {}
        if (initial_launch_id is None) != (initial_launch_capability is None):
            raise ValueError("initial terminal launch credential is partial")
        if initial_launch_id is not None and initial_launch_capability is not None:
            if len(initial_launch_capability) < 32:
                raise ValueError("terminal launch capability is too short")
            now = datetime.now(UTC)
            self._initial[initial_launch_id] = _InitialCredentialRecord(
                launch_id=initial_launch_id,
                capability_commitment=self._credential_commitment(
                    initial_launch_id, initial_launch_capability
                ),
                issued_at=now,
                expires_at=now + timedelta(seconds=self._ttl),
            )

    def issue_initial(self) -> tuple[str, bytes]:
        now = datetime.now(UTC)
        launch_id = f"terminal-launch:{uuid4().hex}"
        capability = secrets.token_bytes(32)
        with self._lock:
            self._initial[launch_id] = _InitialCredentialRecord(
                launch_id=launch_id,
                capability_commitment=self._credential_commitment(
                    launch_id, capability
                ),
                issued_at=now,
                expires_at=now + timedelta(seconds=self._ttl),
            )
        return launch_id, capability

    def authenticate(
        self,
        preface: wire.TerminalTransportAuthPreface,
        *,
        connection_id: str,
    ) -> wire.TerminalTransportAuthResult:
        if preface.preface_version != 1:
            return self._rejected(
                preface,
                connection_id=connection_id,
                code=wire.INVALID_OR_EXPIRED_CREDENTIAL,
            )
        try:
            validate_protobuf_fingerprint(
                "terminal-transport-auth-preface:v1",
                preface,
                own_field="preface_fingerprint",
            )
        except ValueError:
            return self._rejected(
                preface,
                connection_id=connection_id,
                code=wire.CANDIDATE_CONFLICT,
            )
        if (
            len(preface.connection_nonce) != 32
            or not preface.auth_request_id
            or not preface.client_instance_id
            or not preface.handshake_candidate_id
            or not preface.handshake_candidate_fingerprint
            or preface.WhichOneof("credential") != "initial_launch"
        ):
            return self._rejected(
                preface,
                connection_id=connection_id,
                code=wire.INVALID_OR_EXPIRED_CREDENTIAL,
            )
        credential = preface.initial_launch
        with self._lock:
            record = self._initial.get(credential.launch_id)
            now = datetime.now(UTC)
            if (
                record is None
                or now >= record.expires_at
                or (
                    record.acknowledged_at is not None
                    and now >= record.acknowledged_at + timedelta(seconds=30)
                )
                or not hmac.compare_digest(
                    self._credential_commitment(
                        credential.launch_id,
                        bytes(credential.launch_capability),
                    ),
                    record.capability_commitment,
                )
            ):
                return self._rejected(
                    preface,
                    connection_id=connection_id,
                    code=wire.INVALID_OR_EXPIRED_CREDENTIAL,
                )
            previous = record.request_receipts.get(preface.auth_request_id)
            if previous is not None:
                if previous[0] != preface.preface_fingerprint:
                    return self._rejected(
                        preface,
                        connection_id=connection_id,
                        code=wire.CANDIDATE_CONFLICT,
                    )
                result = wire.TerminalTransportAuthResult()
                result.CopyFrom(previous[1])
                return result
            if record.client_instance_id is None:
                record.client_instance_id = preface.client_instance_id
                record.candidate_id = preface.handshake_candidate_id
                record.candidate_fingerprint = preface.handshake_candidate_fingerprint
            elif (
                record.client_instance_id != preface.client_instance_id
                or record.candidate_id != preface.handshake_candidate_id
                or record.candidate_fingerprint
                != preface.handshake_candidate_fingerprint
            ):
                return self._rejected(
                    preface,
                    connection_id=connection_id,
                    code=wire.CANDIDATE_CONFLICT,
                )
            result = wire.TerminalTransportAuthResult(
                auth_request_id=preface.auth_request_id,
                auth_attempt_id=f"terminal-auth-attempt:{uuid4().hex}",
                connection_id=connection_id,
                client_instance_id=preface.client_instance_id,
                credential_id=record.launch_id,
                disposition=(
                    wire.TRANSPORT_COMPATIBLE_AUTH_WINNER
                    if len(record.request_receipts) > 0
                    else wire.TRANSPORT_AUTHENTICATED
                ),
                authenticated_candidate_fingerprint=(
                    preface.handshake_candidate_fingerprint
                ),
            )
            install_protobuf_fingerprint(
                "terminal-transport-auth-result:v1",
                result,
                own_field="result_fingerprint",
            )
            record.request_receipts[preface.auth_request_id] = (
                preface.preface_fingerprint,
                result,
            )
            return result

    def mark_acknowledged(self, launch_id: str) -> None:
        with self._lock:
            record = self._initial.get(launch_id)
            if record is None:
                raise KeyError(launch_id)
            record.acknowledged = True
            if record.acknowledged_at is None:
                record.acknowledged_at = datetime.now(UTC)

    def install_request_result(
        self,
        *,
        credential_id: str,
        auth_request_id: str,
        expected_preface_fingerprint: str,
        result: wire.TerminalTransportAuthResult,
    ) -> None:
        """Replace an admitted physical result with its exact recovery result."""

        with self._lock:
            record = self._initial.get(credential_id)
            if record is None:
                raise KeyError(credential_id)
            current = record.request_receipts.get(auth_request_id)
            if current is None or current[0] != expected_preface_fingerprint:
                raise ValueError("terminal auth request result owner is stale")
            if result.auth_request_id != auth_request_id:
                raise ValueError("terminal auth recovery result crosses requests")
            stored = wire.TerminalTransportAuthResult()
            stored.CopyFrom(result)
            record.request_receipts[auth_request_id] = (
                expected_preface_fingerprint,
                stored,
            )

    def revoke(self, launch_id: str) -> None:
        with self._lock:
            self._initial.pop(launch_id, None)

    def _credential_commitment(self, launch_id: str, capability: bytes) -> bytes:
        return hmac.digest(
            self._commitment_key,
            b"terminal-initial-launch-credential:v1\x00"
            + launch_id.encode("utf-8")
            + b"\x00"
            + capability,
            "sha256",
        )

    def _rejected(
        self,
        preface: wire.TerminalTransportAuthPreface,
        *,
        connection_id: str,
        code: int,
    ) -> wire.TerminalTransportAuthResult:
        credential_id = (
            preface.initial_launch.launch_id
            if preface.WhichOneof("credential") == "initial_launch"
            else (
                preface.reconnect.reconnect_credential_id
                if preface.WhichOneof("credential") == "reconnect"
                else "unknown"
            )
        )
        result = wire.TerminalTransportAuthResult(
            auth_request_id=preface.auth_request_id or "unknown",
            auth_attempt_id=f"terminal-auth-attempt:{uuid4().hex}",
            connection_id=connection_id,
            client_instance_id=preface.client_instance_id or "unknown",
            credential_id=credential_id or "unknown",
            disposition=wire.TRANSPORT_AUTHENTICATION_REJECTED,
            public_rejection_code=code,
        )
        install_protobuf_fingerprint(
            "terminal-transport-auth-result:v1",
            result,
            own_field="result_fingerprint",
        )
        return result


def build_initial_auth_preface(
    *,
    auth_request_id: str,
    client_instance_id: str,
    candidate_id: str,
    candidate_fingerprint: str,
    launch_id: str,
    launch_capability: bytes,
) -> wire.TerminalTransportAuthPreface:
    preface = wire.TerminalTransportAuthPreface(
        preface_version=1,
        auth_request_id=auth_request_id,
        client_instance_id=client_instance_id,
        handshake_candidate_id=candidate_id,
        handshake_candidate_fingerprint=candidate_fingerprint,
        connection_nonce=secrets.token_bytes(32),
        initial_launch=wire.InitialLaunchCredential(
            launch_id=launch_id,
            launch_capability=launch_capability,
        ),
    )
    install_protobuf_fingerprint(
        "terminal-transport-auth-preface:v1",
        preface,
        own_field="preface_fingerprint",
    )
    return preface


__all__ = [
    "PREFACE_DEADLINE_SECONDS",
    "PREFACE_MAXIMUM_BYTES",
    "TerminalTransportAuthOwner",
    "build_initial_auth_preface",
]
