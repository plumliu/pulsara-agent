"""The single version-0 conversation-kernel migration registry."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from pulsara_agent.storage.migrations.contracts import (
    BASELINE_NAME,
    BASELINE_RESOURCE,
    CATALOG_RESOURCE,
    GRANT_RESOURCE,
    MigrationUniverseIdentity,
    build_migration_universe_identity,
)


@dataclass(frozen=True, slots=True)
class PostgresMigrationDefinition:
    version: int
    name: str
    resource_name: str
    catalog_resource_name: str
    grant_resource_name: str
    identity: MigrationUniverseIdentity
    transaction_mode: str = "atomic"

    @property
    def expected_sha256(self) -> str:
        return self.identity.baseline_sql_sha256

    @property
    def migration_contract_fingerprint(self) -> str:
        return self.identity.baseline_contract_fingerprint

    @property
    def registry_prefix_fingerprint(self) -> str:
        return self.identity.genesis_registry_prefix_fingerprint

    def resource_bytes(self) -> bytes:
        payload = files("pulsara_agent.storage.migrations.sql").joinpath(
            self.resource_name
        ).read_bytes()
        if sha256(payload).hexdigest() != self.identity.baseline_sql_sha256:
            raise ValueError("clean baseline SQL checksum mismatch")
        return payload

    def resource_text(self) -> str:
        return self.resource_bytes().decode("utf-8")

    def verify_auxiliary_resources(self) -> None:
        root = files("pulsara_agent.storage.migrations.resources")
        checks = (
            (self.catalog_resource_name, self.identity.catalog_sha256),
            (self.grant_resource_name, self.identity.grant_sha256),
        )
        for name, expected in checks:
            if sha256(root.joinpath(name).read_bytes()).hexdigest() != expected:
                raise ValueError(f"clean migration resource checksum mismatch: {name}")


@dataclass(frozen=True, slots=True)
class PostgresMigrationRegistry:
    definitions: tuple[PostgresMigrationDefinition, ...]
    registry_fingerprint: str
    universe_fingerprint: str

    @property
    def latest_version(self) -> int:
        return 0

    def definition(self, version: int) -> PostgresMigrationDefinition:
        if version != 0:
            raise KeyError(version)
        return self.definitions[0]

    def verify_resources(self) -> None:
        if len(self.definitions) != 1 or self.definitions[0].version != 0:
            raise ValueError("clean registry must contain exact version-0 baseline")
        self.definitions[0].resource_bytes()
        self.definitions[0].verify_auxiliary_resources()


def _checksum(package: str, resource: str) -> str:
    return sha256(files(package).joinpath(resource).read_bytes()).hexdigest()


_IDENTITY = build_migration_universe_identity(
    baseline_sql_sha256=_checksum(
        "pulsara_agent.storage.migrations.sql", BASELINE_RESOURCE
    ),
    catalog_sha256=_checksum(
        "pulsara_agent.storage.migrations.resources", CATALOG_RESOURCE
    ),
    grant_sha256=_checksum(
        "pulsara_agent.storage.migrations.resources", GRANT_RESOURCE
    ),
)
_DEFINITION = PostgresMigrationDefinition(
    version=0,
    name=BASELINE_NAME,
    resource_name=BASELINE_RESOURCE,
    catalog_resource_name=CATALOG_RESOURCE,
    grant_resource_name=GRANT_RESOURCE,
    identity=_IDENTITY,
)
POSTGRES_MIGRATION_REGISTRY = PostgresMigrationRegistry(
    definitions=(_DEFINITION,),
    registry_fingerprint=_IDENTITY.genesis_registry_prefix_fingerprint,
    universe_fingerprint=_IDENTITY.universe_fingerprint,
)


__all__ = [
    "POSTGRES_MIGRATION_REGISTRY",
    "PostgresMigrationDefinition",
    "PostgresMigrationRegistry",
]
