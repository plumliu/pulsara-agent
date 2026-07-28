"""Immutable event-write carrier shared across low-level boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class FrozenEventWriteCandidate:
    """One pre-commit event payload frozen against an exact schema binding."""

    event_id: str
    event_type: str
    event_schema_version: str
    event_schema_fingerprint: str
    event_domain_contract_fingerprint: str
    canonical_payload_bytes: bytes
    payload_fingerprint: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_type:
            raise ValueError("event write candidate identity is required")
        if (
            f"sha256:{sha256(self.canonical_payload_bytes).hexdigest()}"
            != self.payload_fingerprint
        ):
            raise ValueError("event write candidate payload fingerprint mismatch")
        try:
            payload = json.loads(self.canonical_payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(
                "event write candidate payload is not canonical JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("event write candidate payload must be an object")
        if (
            payload.get("id") != self.event_id
            or str(payload.get("type")) != self.event_type
            or payload.get("sequence") is not None
        ):
            raise ValueError("event write candidate wrapper identity mismatch")

    def fingerprint_payload(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_schema_version": self.event_schema_version,
            "event_schema_fingerprint": self.event_schema_fingerprint,
            "event_domain_contract_fingerprint": (
                self.event_domain_contract_fingerprint
            ),
            "canonical_payload_utf8": self.canonical_payload_bytes.decode("utf-8"),
            "payload_fingerprint": self.payload_fingerprint,
        }


__all__ = ["FrozenEventWriteCandidate"]
