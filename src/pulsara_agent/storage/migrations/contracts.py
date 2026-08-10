"""Closed identity contracts for the clean conversation-kernel universe."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import TypeAlias


UNIVERSE_ID = "pulsara.conversation-kernel.v1"
UNIVERSE_GENERATION = 1
BASELINE_VERSION = 0
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
class MigrationUniverseIdentity:
    baseline_sql_sha256: str
    catalog_sha256: str
    grant_sha256: str
    baseline_contract_fingerprint: str
    universe_fingerprint: str
    genesis_registry_prefix_fingerprint: str


def build_migration_universe_identity(
    *, baseline_sql_sha256: str, catalog_sha256: str, grant_sha256: str
) -> MigrationUniverseIdentity:
    for checksum in (baseline_sql_sha256, catalog_sha256, grant_sha256):
        require_checksum(checksum)
    baseline = postgres_schema_fingerprint(
        "pulsara:postgres-migration-baseline-contract:v1",
        {
            "schema_version": "postgres_migration_baseline_contract.v1",
            "version": 0,
            "name": BASELINE_NAME,
            "resource_name": BASELINE_RESOURCE,
            "resource_sha256": baseline_sql_sha256,
            "transaction_mode": "atomic",
            "catalog_resource_name": CATALOG_RESOURCE,
            "catalog_sha256": catalog_sha256,
            "grant_resource_name": GRANT_RESOURCE,
            "grant_sha256": grant_sha256,
        },
    )
    universe = postgres_schema_fingerprint(
        "pulsara:postgres-migration-universe:v1",
        {
            "schema_version": "postgres_migration_universe.v1",
            "universe_id": UNIVERSE_ID,
            "universe_generation": UNIVERSE_GENERATION,
            "baseline_version": BASELINE_VERSION,
            "baseline_resource_name": BASELINE_RESOURCE,
            "baseline_resource_sha256": baseline_sql_sha256,
            "catalog_resource_name": CATALOG_RESOURCE,
            "catalog_sha256": catalog_sha256,
            "grant_resource_name": GRANT_RESOURCE,
            "grant_sha256": grant_sha256,
            "baseline_migration_contract_fingerprint": baseline,
        },
    )
    prefix = postgres_schema_fingerprint(
        "pulsara:postgres-migration-registry-prefix:v2",
        {
            "universe_fingerprint": universe,
            "migration_contract_fingerprint": baseline,
        },
    )
    return MigrationUniverseIdentity(
        baseline_sql_sha256=baseline_sql_sha256,
        catalog_sha256=catalog_sha256,
        grant_sha256=grant_sha256,
        baseline_contract_fingerprint=baseline,
        universe_fingerprint=universe,
        genesis_registry_prefix_fingerprint=prefix,
    )


@dataclass(frozen=True, slots=True)
class PostgresMigrationLedgerRowFact:
    universe_id: str
    universe_generation: int
    universe_fingerprint: str
    version: int
    name: str
    resource_checksum: str
    migration_contract_fingerprint: str
    registry_prefix_fingerprint: str
    application_version: str
    applied_at_utc: str

    def __post_init__(self) -> None:
        if self.universe_id != UNIVERSE_ID or self.universe_generation != 1:
            raise ValueError("migration ledger belongs to another universe")
        if self.version != 0 or self.name != BASELINE_NAME:
            raise ValueError("clean migration ledger must contain version-0 genesis")
        require_checksum(self.resource_checksum)
        for field in (
            "universe_fingerprint",
            "migration_contract_fingerprint",
            "registry_prefix_fingerprint",
        ):
            require_fingerprint(str(getattr(self, field)), field_name=field)
        parsed = datetime.fromisoformat(self.applied_at_utc.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("applied_at_utc must be UTC")


def canonical_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_GOLDEN = build_migration_universe_identity(
    baseline_sql_sha256="11" * 32,
    catalog_sha256="22" * 32,
    grant_sha256="33" * 32,
)
if (
    _GOLDEN.baseline_contract_fingerprint
    != "sha256:8390ab92c98ed167b03a3fd73943750bd23b148538c4eb5f75714b5398cbd240"
    or _GOLDEN.universe_fingerprint
    != "sha256:9f3b3cc41831e3dd7ddff91ff9b0c4f35d421745c25a3d346331c95a2073ca19"
    or _GOLDEN.genesis_registry_prefix_fingerprint
    != "sha256:62c84b5c8e9dec93c3c76f1ba4da1892983dd431bc1be51d6d3d9cb12d7cdcc4"
):
    raise RuntimeError("migration-universe canonical identity golden changed")


__all__ = [
    "BASELINE_NAME",
    "BASELINE_RESOURCE",
    "BASELINE_VERSION",
    "CATALOG_RESOURCE",
    "GRANT_RESOURCE",
    "MigrationUniverseIdentity",
    "PostgresMigrationLedgerRowFact",
    "UNIVERSE_GENERATION",
    "UNIVERSE_ID",
    "build_migration_universe_identity",
    "canonical_json_bytes",
    "canonical_utc",
    "postgres_schema_fingerprint",
    "require_checksum",
    "require_fingerprint",
]
