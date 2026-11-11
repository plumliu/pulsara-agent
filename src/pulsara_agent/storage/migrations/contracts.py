"""Canonical values and durable facts for the clean PostgreSQL baseline."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import TypeAlias


BASELINE_NAME = "conversation_kernel_baseline"
BASELINE_RESOURCE = "0000_conversation_kernel_baseline.sql"
CATALOG_RESOURCE = "0000_conversation_kernel_expected_catalog_v1.json"
GRANT_RESOURCE = "0000_conversation_kernel_runtime_grants_v1.json"

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")

CanonicalScalar: TypeAlias = type(None) | bool | int | str
CanonicalValue: TypeAlias = (
    CanonicalScalar | tuple["CanonicalValue", ...] | dict[str, "CanonicalValue"]
)


def _canonical(value: object) -> CanonicalValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, tuple):
        return tuple(_canonical(item) for item in value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("migration identity keys must be strings")
        return {key: _canonical(value[key]) for key in sorted(value)}
    raise TypeError(f"non-canonical migration identity value: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def postgres_schema_fingerprint(domain: str, value: object) -> str:
    if not domain:
        raise ValueError("fingerprint domain must be non-empty")
    return "sha256:" + sha256(
        domain.encode("utf-8") + b"\0" + canonical_json_bytes(value)
    ).hexdigest()


def require_fingerprint(value: str, *, field_name: str) -> str:
    if _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")
    return value


def require_checksum(value: str) -> str:
    if _CHECKSUM.fullmatch(value) is None:
        raise ValueError("resource checksum must be 64 lowercase hex")
    return value


@dataclass(frozen=True, slots=True)
class PostgresMigrationLedgerRowFact:
    name: str
    resource_checksum: str
    applied_at_utc: str

    def __post_init__(self) -> None:
        if self.name != BASELINE_NAME:
            raise ValueError("migration ledger does not identify the clean baseline")
        require_checksum(self.resource_checksum)
        parsed = datetime.fromisoformat(self.applied_at_utc.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("applied_at_utc must be UTC")


def canonical_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "BASELINE_NAME", "BASELINE_RESOURCE", "CATALOG_RESOURCE", "GRANT_RESOURCE",
    "PostgresMigrationLedgerRowFact", "canonical_json_bytes", "canonical_utc",
    "postgres_schema_fingerprint", "require_checksum", "require_fingerprint",
]
