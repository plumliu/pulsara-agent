from __future__ import annotations

from dataclasses import fields

from pulsara_agent.terminal_protocol.generated import terminal_client_pb2 as wire
from pulsara_agent.terminal_protocol.transport_auth import (
    TerminalTransportAuthOwner,
    build_initial_auth_preface,
)


def _preface(*, request_id: str, launch_id: str, capability: bytes):
    return build_initial_auth_preface(
        auth_request_id=request_id,
        client_instance_id="terminal-client:auth-test",
        candidate_id="handshake:auth-test",
        candidate_fingerprint="sha256:" + "a" * 64,
        launch_id=launch_id,
        launch_capability=capability,
    )


def test_initial_transport_auth_stores_only_a_keyed_capability_commitment() -> None:
    owner = TerminalTransportAuthOwner()
    launch_id, capability = owner.issue_initial()
    record = owner._initial[launch_id]  # noqa: SLF001 - storage-shape contract test

    assert "launch_capability" not in {field.name for field in fields(record)}
    assert record.capability_commitment != capability
    assert capability not in bytes(record.capability_commitment)


def test_transport_auth_retries_are_request_scoped_and_ack_tombstone_bounded() -> None:
    owner = TerminalTransportAuthOwner()
    launch_id, capability = owner.issue_initial()
    first = _preface(
        request_id="terminal-request:auth-1",
        launch_id=launch_id,
        capability=capability,
    )

    accepted = owner.authenticate(first, connection_id="connection:one")
    assert accepted.disposition == wire.TRANSPORT_AUTHENTICATED

    exact_retry = owner.authenticate(first, connection_id="connection:one")
    assert exact_retry == accepted

    conflicting_retry = _preface(
        request_id=first.auth_request_id,
        launch_id=launch_id,
        capability=capability,
    )
    conflict = owner.authenticate(conflicting_retry, connection_id="connection:two")
    assert conflict.disposition == wire.TRANSPORT_AUTHENTICATION_REJECTED
    assert conflict.public_rejection_code == wire.CANDIDATE_CONFLICT

    physical_retry = _preface(
        request_id="terminal-request:auth-2",
        launch_id=launch_id,
        capability=capability,
    )
    compatible = owner.authenticate(physical_retry, connection_id="connection:two")
    assert compatible.disposition == wire.TRANSPORT_COMPATIBLE_AUTH_WINNER
    assert compatible.auth_request_id == physical_retry.auth_request_id

    owner.mark_acknowledged(launch_id)
    tombstone_retry = _preface(
        request_id="terminal-request:auth-3",
        launch_id=launch_id,
        capability=capability,
    )
    assert (
        owner.authenticate(
            tombstone_retry, connection_id="connection:three"
        ).disposition
        == wire.TRANSPORT_COMPATIBLE_AUTH_WINNER
    )

    invalid = _preface(
        request_id="terminal-request:auth-invalid",
        launch_id=launch_id,
        capability=b"x" * 32,
    )
    rejected = owner.authenticate(invalid, connection_id="connection:invalid")
    assert rejected.disposition == wire.TRANSPORT_AUTHENTICATION_REJECTED
    assert rejected.public_rejection_code == wire.INVALID_OR_EXPIRED_CREDENTIAL
