"""Immutable packaged PostgreSQL migration registry."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Literal

from pulsara_agent.storage.migrations.contracts import (
    postgres_schema_fingerprint,
    require_checksum,
)
from pulsara_agent.storage.migrations.grants import build_postgres_runtime_grant_policy
from pulsara_agent.storage.migrations.manifest import build_postgres_schema_manifest


_GENESIS_PREFIX = postgres_schema_fingerprint(
    "pulsara:postgres-migration-registry-genesis:v1",
    {"schema_version": "postgres_migration_registry_genesis.v1"},
)


@dataclass(frozen=True, slots=True)
class PostgresMigrationDefinition:
    version: int
    name: str
    resource_name: str
    expected_sha256: str
    transaction_mode: Literal["atomic"]
    postcondition_contract_fingerprint: str
    resulting_object_manifest_fingerprint: str
    runtime_grant_policy_fingerprint: str
    migration_contract_fingerprint: str
    registry_prefix_fingerprint: str

    def resource_bytes(self) -> bytes:
        resource = files("pulsara_agent.storage.migrations.sql").joinpath(
            self.resource_name
        )
        payload = resource.read_bytes()
        actual = sha256(payload).hexdigest()
        if actual != self.expected_sha256:
            raise ValueError(
                f"migration resource checksum mismatch for {self.resource_name}: "
                f"expected {self.expected_sha256}, observed {actual}"
            )
        return payload

    def resource_text(self) -> str:
        return self.resource_bytes().decode("utf-8")


@dataclass(frozen=True, slots=True)
class PostgresMigrationRegistry:
    definitions: tuple[PostgresMigrationDefinition, ...]
    registry_fingerprint: str

    def __post_init__(self) -> None:
        _validate_registry(self)

    @property
    def latest_version(self) -> int:
        return self.definitions[-1].version

    def definition(self, version: int) -> PostgresMigrationDefinition:
        try:
            return self.definitions[version]
        except IndexError as exc:
            raise KeyError(version) from exc

    def verify_resources(self) -> None:
        for definition in self.definitions:
            definition.resource_bytes()


_RESOURCE_CHECKSUMS = (
    "d493eb39b2ccb56de2b3c4549ae93661b5fb2c54ee8c0b2b62a06797fd599d57",
    "9e9b2cfec47519f49ee73cb533c459e22f8ca54fe5ba1cbec59f3d5883fe191c",
    "c76a13ce7c39c5104d932e378769e0dbe25f0d041669500656552f411cb065fd",
    "c201c65ffb4331e8e9dfd18e5f602c4445f34acf1075185d446609dc2a39e843",
    "0c5b707a2738d184b55a1c0aa436ace7d3d68bcbce197f1c5d0c3b3a48c4f752",
)
_NAMES = (
    "schema_migration_ledger",
    "pgvector_extension",
    "runtime_truth_baseline",
    "memory_substrate_baseline",
    "memory_governance_baseline",
)


def _build_registry() -> PostgresMigrationRegistry:
    definitions: list[PostgresMigrationDefinition] = []
    previous_prefix = _GENESIS_PREFIX
    for version, (name, checksum) in enumerate(zip(_NAMES, _RESOURCE_CHECKSUMS, strict=True)):
        require_checksum(checksum)
        resource_name = f"{version:04d}_{name}.sql"
        if PurePosixPath(resource_name).name != resource_name:
            raise ValueError("migration resource must be a package-local filename")
        manifest = build_postgres_schema_manifest(version)
        grant_policy = build_postgres_runtime_grant_policy(version)
        postcondition = postgres_schema_fingerprint(
            "pulsara:postgres-migration-postcondition-contract:v1",
            {
                "version": version,
                "manifest_fingerprint": manifest.manifest_fingerprint,
            },
        )
        contract_payload = {
            "version": version,
            "name": name,
            "expected_sha256": checksum,
            "transaction_mode": "atomic",
            "postcondition_contract_fingerprint": postcondition,
            "resulting_object_manifest_fingerprint": manifest.manifest_fingerprint,
            "runtime_grant_policy_fingerprint": grant_policy.policy_fingerprint,
        }
        contract = postgres_schema_fingerprint(
            "pulsara:postgres-migration-contract:v1", contract_payload
        )
        prefix = postgres_schema_fingerprint(
            "pulsara:postgres-migration-registry-prefix:v1",
            {
                "previous_registry_prefix_fingerprint": previous_prefix,
                "migration_contract_fingerprint": contract,
            },
        )
        definitions.append(
            PostgresMigrationDefinition(
                version=version,
                name=name,
                resource_name=resource_name,
                expected_sha256=checksum,
                transaction_mode="atomic",
                postcondition_contract_fingerprint=postcondition,
                resulting_object_manifest_fingerprint=manifest.manifest_fingerprint,
                runtime_grant_policy_fingerprint=grant_policy.policy_fingerprint,
                migration_contract_fingerprint=contract,
                registry_prefix_fingerprint=prefix,
            )
        )
        previous_prefix = prefix
    registry = PostgresMigrationRegistry(
        definitions=tuple(definitions), registry_fingerprint=previous_prefix
    )
    return registry


def _validate_registry(registry: PostgresMigrationRegistry) -> None:
    if not registry.definitions:
        raise ValueError("migration registry must be non-empty")
    versions = tuple(item.version for item in registry.definitions)
    if versions != tuple(range(len(registry.definitions))):
        raise ValueError("migration versions must be contiguous from zero")
    names = tuple(item.name for item in registry.definitions)
    resources = tuple(item.resource_name for item in registry.definitions)
    if len(names) != len(set(names)) or len(resources) != len(set(resources)):
        raise ValueError("migration names and resources must be unique")
    previous_prefix = _GENESIS_PREFIX
    for item in registry.definitions:
        expected_prefix = f"{item.version:04d}_"
        if not item.resource_name.startswith(expected_prefix):
            raise ValueError("migration filename/version mismatch")
        if item.transaction_mode != "atomic":
            raise ValueError("all V1 migrations must be atomic")
        manifest = build_postgres_schema_manifest(item.version)
        grant_policy = build_postgres_runtime_grant_policy(item.version)
        postcondition = postgres_schema_fingerprint(
            "pulsara:postgres-migration-postcondition-contract:v1",
            {
                "version": item.version,
                "manifest_fingerprint": manifest.manifest_fingerprint,
            },
        )
        if item.postcondition_contract_fingerprint != postcondition:
            raise ValueError("migration postcondition fingerprint mismatch")
        if (
            item.resulting_object_manifest_fingerprint
            != manifest.manifest_fingerprint
        ):
            raise ValueError("migration manifest fingerprint mismatch")
        if item.runtime_grant_policy_fingerprint != grant_policy.policy_fingerprint:
            raise ValueError("migration grant policy fingerprint mismatch")
        contract = postgres_schema_fingerprint(
            "pulsara:postgres-migration-contract:v1",
            {
                "version": item.version,
                "name": item.name,
                "expected_sha256": item.expected_sha256,
                "transaction_mode": item.transaction_mode,
                "postcondition_contract_fingerprint": postcondition,
                "resulting_object_manifest_fingerprint": manifest.manifest_fingerprint,
                "runtime_grant_policy_fingerprint": grant_policy.policy_fingerprint,
            },
        )
        if item.migration_contract_fingerprint != contract:
            raise ValueError("migration contract fingerprint mismatch")
        expected_registry_prefix = postgres_schema_fingerprint(
            "pulsara:postgres-migration-registry-prefix:v1",
            {
                "previous_registry_prefix_fingerprint": previous_prefix,
                "migration_contract_fingerprint": contract,
            },
        )
        if item.registry_prefix_fingerprint != expected_registry_prefix:
            raise ValueError("migration registry prefix recurrence mismatch")
        previous_prefix = expected_registry_prefix
    if registry.registry_fingerprint != registry.definitions[-1].registry_prefix_fingerprint:
        raise ValueError("registry fingerprint/head prefix mismatch")


POSTGRES_MIGRATION_REGISTRY = _build_registry()


__all__ = [
    "POSTGRES_MIGRATION_REGISTRY",
    "PostgresMigrationDefinition",
    "PostgresMigrationRegistry",
]
