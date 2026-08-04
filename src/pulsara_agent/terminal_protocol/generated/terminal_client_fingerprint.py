"""Generated-style canonical fingerprint helpers for terminal Protocol 2.0.

The manifest generator owns the namespace table.  The wire algorithm is
language-neutral: SHA-256 over ``namespace || NUL || deterministic protobuf``
after clearing the message's own fingerprint field.
"""

from __future__ import annotations

from hashlib import sha256
import json
import struct

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message


def protobuf_fingerprint(
    namespace: str,
    message: Message,
    *,
    own_field: str,
    clear_fields: tuple[str, ...] = (),
) -> str:
    if not namespace or "\x00" in namespace:
        raise ValueError("terminal fingerprint namespace is malformed")
    clone = type(message)()
    clone.CopyFrom(message)
    clone.ClearField(own_field)
    for field_name in clear_fields:
        clone.ClearField(field_name)
    payload = clone.SerializeToString(deterministic=True)
    digest = sha256(namespace.encode("utf-8") + b"\x00" + payload).hexdigest()
    return f"sha256:{digest}"


def install_protobuf_fingerprint(
    namespace: str,
    message: Message,
    *,
    own_field: str,
    clear_fields: tuple[str, ...] = (),
) -> str:
    value = protobuf_fingerprint(
        namespace,
        message,
        own_field=own_field,
        clear_fields=clear_fields,
    )
    setattr(message, own_field, value)
    return value


def validate_protobuf_fingerprint(
    namespace: str,
    message: Message,
    *,
    own_field: str,
    clear_fields: tuple[str, ...] = (),
) -> None:
    observed = getattr(message, own_field)
    expected = protobuf_fingerprint(
        namespace,
        message,
        own_field=own_field,
        clear_fields=clear_fields,
    )
    if observed != expected:
        raise ValueError(f"terminal {own_field} mismatch")


def bytes_commitment(namespace: str, value: bytes) -> str:
    if not namespace or "\x00" in namespace:
        raise ValueError("terminal commitment namespace is malformed")
    return "sha256:" + sha256(namespace.encode("utf-8") + b"\x00" + value).hexdigest()


def canonical_protobuf_json_vector_bytes(values: tuple[Message, ...]) -> bytes:
    """Encode a protobuf value vector with the Protocol 2.0 JSON codec.

    Field names stay in proto spelling, enums are integer vocabulary values,
    absent fields remain absent, maps/floats/unknown fields are forbidden by
    the schema contract, and object keys are sorted by the final JSON encoder.
    """

    payload = tuple(
        MessageToDict(
            value,
            preserving_proto_field_name=True,
            use_integers_for_enums=True,
            always_print_fields_with_no_presence=False,
        )
        for value in values
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_json_fingerprint(namespace: str, value: object) -> str:
    if not namespace or "\x00" in namespace:
        raise ValueError("terminal JSON fingerprint namespace is malformed")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(namespace.encode("utf-8") + b"\x00" + encoded).hexdigest()


def operational_activity_accumulator(values: tuple[Message, ...]) -> str:
    """Return the grouping-independent ordered operational-cell accumulator."""

    accumulator = _canonical_json_fingerprint(
        "terminal-operational-activity-accumulator-genesis:v1", []
    )
    for index, value in enumerate(values):
        branch = value.WhichOneof("activity")
        if branch is None:
            raise ValueError("operational activity branch is missing")
        common = getattr(value, branch).common
        accumulator = _canonical_json_fingerprint(
            "terminal-operational-activity-accumulator-step:v1",
            {
                "activity_fingerprint": common.activity_fingerprint,
                "coalesce_key": common.coalesce_key,
                "index": index,
                "owner_generation": common.owner_generation,
                "owner_id": common.owner_id,
                "owner_kind": common.owner_kind,
                "previous_accumulator": accumulator,
            },
        )
    return accumulator


def attachment_challenge_commitment(
    *,
    auth_attempt_id: str,
    candidate_fingerprint: str,
    candidate_id: str,
    connection_id: str,
    negotiation_winner_fingerprint: str,
    request_id: str,
    challenge: bytes,
) -> str:
    """Return the Protocol 2.0 purpose-bound 32-byte challenge commitment."""

    if len(challenge) != 32:
        raise ValueError("terminal attachment challenge must contain 32 bytes")
    fields = {
        "auth_attempt_id": auth_attempt_id,
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_id": candidate_id,
        "connection_id": connection_id,
        "negotiation_winner_fingerprint": negotiation_winner_fingerprint,
        "request_id": request_id,
    }
    if any(not value for value in fields.values()):
        raise ValueError("terminal attachment challenge attribution is incomplete")
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256()
    digest.update(b"terminal-attachment-challenge:v1\x00")
    digest.update(canonical)
    digest.update(b"\x00")
    digest.update(struct.pack(">I", len(challenge)))
    digest.update(challenge)
    return f"sha256:{digest.hexdigest()}"


__all__ = [
    "bytes_commitment",
    "canonical_protobuf_json_vector_bytes",
    "attachment_challenge_commitment",
    "install_protobuf_fingerprint",
    "operational_activity_accumulator",
    "protobuf_fingerprint",
    "validate_protobuf_fingerprint",
]
