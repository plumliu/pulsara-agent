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


@dataclass(slots=True)
class _ReconnectCredentialRecord:
    public_identity: wire.ReconnectCredentialPublicIdentity
    capability_commitment: bytes
    client_instance_id: str
    previous_attachment_id: str
    previous_attachment_generation: int
    previous_candidate_fingerprint: str
    expected_next_attachment_attempt_generation: int
    expires_at: datetime
    candidate_id: str | None = None
    candidate_fingerprint: str | None = None
    acknowledged: bool = False
    acknowledged_at: datetime | None = None
    request_receipts: dict[str, tuple[str, wire.TerminalTransportAuthResult]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class ReconnectCredentialPredecessor:
    credential_id: str
    client_instance_id: str
    previous_attachment_id: str
    previous_attachment_generation: int
    previous_candidate_fingerprint: str
    expected_next_attachment_attempt_generation: int


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
        self._reconnect: dict[str, _ReconnectCredentialRecord] = {}
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

    def issue_reconnect(
        self,
        *,
        client_instance_id: str,
        previous_attachment_id: str,
        previous_attachment_generation: int,
        previous_candidate_fingerprint: str,
        expected_next_attachment_attempt_generation: int,
    ) -> tuple[wire.ReconnectCredentialPublicIdentity, wire.ReconnectCredentialCarrier]:
        if (
            not client_instance_id
            or not previous_attachment_id
            or previous_attachment_generation < 1
            or not previous_candidate_fingerprint
            or expected_next_attachment_attempt_generation < 2
        ):
            raise ValueError("terminal reconnect predecessor is invalid")
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._ttl)
        credential_id = f"terminal-reconnect:{uuid4().hex}"
        capability = secrets.token_bytes(32)
        commitment = self._reconnect_credential_commitment(
            credential_id=credential_id,
            client_instance_id=client_instance_id,
            previous_attachment_id=previous_attachment_id,
            previous_attachment_generation=previous_attachment_generation,
            previous_candidate_fingerprint=previous_candidate_fingerprint,
            expected_next_attachment_attempt_generation=(
                expected_next_attachment_attempt_generation
            ),
            capability=capability,
        )
        identity = wire.ReconnectCredentialPublicIdentity(
            reconnect_credential_id=credential_id,
            client_instance_id=client_instance_id,
            previous_attachment_id=previous_attachment_id,
            previous_attachment_generation=previous_attachment_generation,
            expected_next_attachment_attempt_generation=(
                expected_next_attachment_attempt_generation
            ),
            issued_at_utc=now.isoformat(),
            expires_at_utc=expires.isoformat(),
            credential_commitment="hmac-sha256:" + commitment.hex(),
        )
        install_protobuf_fingerprint(
            "terminal-reconnect-credential-public-identity:v1",
            identity,
            own_field="identity_fingerprint",
        )
        carrier = wire.ReconnectCredentialCarrier(
            public_identity=identity,
            reconnect_capability=capability,
        )
        install_protobuf_fingerprint(
            "terminal-reconnect-credential-carrier:v1",
            carrier,
            own_field="carrier_fingerprint",
        )
        with self._lock:
            self._reconnect[credential_id] = _ReconnectCredentialRecord(
                public_identity=identity,
                capability_commitment=commitment,
                client_instance_id=client_instance_id,
                previous_attachment_id=previous_attachment_id,
                previous_attachment_generation=previous_attachment_generation,
                previous_candidate_fingerprint=previous_candidate_fingerprint,
                expected_next_attachment_attempt_generation=(
                    expected_next_attachment_attempt_generation
                ),
                expires_at=expires,
            )
            self._retire_expired_reconnect_unlocked(now)
        return identity, carrier

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
            or preface.WhichOneof("credential") not in {"initial_launch", "reconnect"}
        ):
            return self._rejected(
                preface,
                connection_id=connection_id,
                code=wire.INVALID_OR_EXPIRED_CREDENTIAL,
            )
        if preface.WhichOneof("credential") == "reconnect":
            return self._authenticate_reconnect(preface, connection_id=connection_id)
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
            reconnect = self._reconnect.get(launch_id)
            if record is None and reconnect is None:
                raise KeyError(launch_id)
            if reconnect is not None:
                reconnect.acknowledged = True
                if reconnect.acknowledged_at is None:
                    reconnect.acknowledged_at = datetime.now(UTC)
                self._retire_expired_reconnect_unlocked(datetime.now(UTC))
                return
            assert record is not None
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
            reconnect = self._reconnect.get(credential_id)
            if record is None and reconnect is None:
                raise KeyError(credential_id)
            target = reconnect if reconnect is not None else record
            assert target is not None
            current = target.request_receipts.get(auth_request_id)
            if current is None or current[0] != expected_preface_fingerprint:
                raise ValueError("terminal auth request result owner is stale")
            if result.auth_request_id != auth_request_id:
                raise ValueError("terminal auth recovery result crosses requests")
            stored = wire.TerminalTransportAuthResult()
            stored.CopyFrom(result)
            target.request_receipts[auth_request_id] = (
                expected_preface_fingerprint,
                stored,
            )

    def revoke(self, launch_id: str) -> None:
        with self._lock:
            self._initial.pop(launch_id, None)
            self._reconnect.pop(launch_id, None)

    def expected_candidate_generation(self, credential_id: str) -> int:
        with self._lock:
            if credential_id in self._initial:
                return 1
            reconnect = self._reconnect.get(credential_id)
            if reconnect is None:
                raise KeyError(credential_id)
            return reconnect.expected_next_attachment_attempt_generation

    def reconnect_predecessor(
        self, credential_id: str
    ) -> ReconnectCredentialPredecessor:
        """Return the immutable predecessor frozen by one reconnect credential."""

        with self._lock:
            reconnect = self._reconnect.get(credential_id)
            if reconnect is None:
                raise KeyError(credential_id)
            return ReconnectCredentialPredecessor(
                credential_id=credential_id,
                client_instance_id=reconnect.client_instance_id,
                previous_attachment_id=reconnect.previous_attachment_id,
                previous_attachment_generation=(
                    reconnect.previous_attachment_generation
                ),
                previous_candidate_fingerprint=(
                    reconnect.previous_candidate_fingerprint
                ),
                expected_next_attachment_attempt_generation=(
                    reconnect.expected_next_attachment_attempt_generation
                ),
            )

    def _credential_commitment(self, launch_id: str, capability: bytes) -> bytes:
        return hmac.digest(
            self._commitment_key,
            b"terminal-initial-launch-credential:v1\x00"
            + launch_id.encode("utf-8")
            + b"\x00"
            + capability,
            "sha256",
        )

    def _reconnect_credential_commitment(
        self,
        *,
        credential_id: str,
        client_instance_id: str,
        previous_attachment_id: str,
        previous_attachment_generation: int,
        previous_candidate_fingerprint: str,
        expected_next_attachment_attempt_generation: int,
        capability: bytes,
    ) -> bytes:
        semantic = "\x00".join(
            (
                credential_id,
                client_instance_id,
                previous_attachment_id,
                str(previous_attachment_generation),
                previous_candidate_fingerprint,
                str(expected_next_attachment_attempt_generation),
            )
        ).encode("utf-8")
        return hmac.digest(
            self._commitment_key,
            b"terminal-reconnect-credential:v1\x00" + semantic + b"\x00" + capability,
            "sha256",
        )

    def _authenticate_reconnect(
        self,
        preface: wire.TerminalTransportAuthPreface,
        *,
        connection_id: str,
    ) -> wire.TerminalTransportAuthResult:
        credential = preface.reconnect
        with self._lock:
            now = datetime.now(UTC)
            self._retire_expired_reconnect_unlocked(now)
            record = self._reconnect.get(credential.reconnect_credential_id)
            if (
                record is None
                or now >= record.expires_at
                or credential.previous_attachment_id != record.previous_attachment_id
                or credential.previous_attachment_generation
                != record.previous_attachment_generation
                or preface.client_instance_id != record.client_instance_id
                or not hmac.compare_digest(
                    self._reconnect_credential_commitment(
                        credential_id=credential.reconnect_credential_id,
                        client_instance_id=record.client_instance_id,
                        previous_attachment_id=record.previous_attachment_id,
                        previous_attachment_generation=(
                            record.previous_attachment_generation
                        ),
                        previous_candidate_fingerprint=(
                            record.previous_candidate_fingerprint
                        ),
                        expected_next_attachment_attempt_generation=(
                            record.expected_next_attachment_attempt_generation
                        ),
                        capability=bytes(credential.reconnect_capability),
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
            if record.candidate_id is None:
                record.candidate_id = preface.handshake_candidate_id
                record.candidate_fingerprint = preface.handshake_candidate_fingerprint
            elif (
                record.candidate_id != preface.handshake_candidate_id
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
                credential_id=credential.reconnect_credential_id,
                disposition=(
                    wire.TRANSPORT_COMPATIBLE_AUTH_WINNER
                    if record.request_receipts
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

    def _retire_expired_reconnect_unlocked(self, now: datetime) -> None:
        for credential_id, record in tuple(self._reconnect.items()):
            if now >= record.expires_at or (
                record.acknowledged_at is not None
                and now >= record.acknowledged_at + timedelta(seconds=30)
            ):
                self._reconnect.pop(credential_id, None)

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
    "ReconnectCredentialPredecessor",
    "TerminalTransportAuthOwner",
    "build_initial_auth_preface",
]
