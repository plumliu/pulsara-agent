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
    auxiliary_resource_checksums: tuple[tuple[str, str], ...]
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

    def verify_auxiliary_resources(self) -> None:
        root = files("pulsara_agent.storage.migrations.resources")
        for resource_name, expected_checksum in self.auxiliary_resource_checksums:
            if PurePosixPath(resource_name).name != resource_name:
                raise ValueError("migration auxiliary resource must be package-local")
            actual = sha256(root.joinpath(resource_name).read_bytes()).hexdigest()
            if actual != expected_checksum:
                raise ValueError(
                    f"migration auxiliary resource checksum mismatch for "
                    f"{resource_name}: expected {expected_checksum}, observed {actual}"
                )


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
            definition.verify_auxiliary_resources()


_RESOURCE_CHECKSUMS = (
    "d493eb39b2ccb56de2b3c4549ae93661b5fb2c54ee8c0b2b62a06797fd599d57",
    "9e9b2cfec47519f49ee73cb533c459e22f8ca54fe5ba1cbec59f3d5883fe191c",
    "c76a13ce7c39c5104d932e378769e0dbe25f0d041669500656552f411cb065fd",
    "c201c65ffb4331e8e9dfd18e5f602c4445f34acf1075185d446609dc2a39e843",
    "0c5b707a2738d184b55a1c0aa436ace7d3d68bcbce197f1c5d0c3b3a48c4f752",
    "e33dbd05d6609ac2ece274463ae85b06a093131f47e781d382d12dbfdff25a6f",
    "10bb441ddc12a18425dffd8e5d0407bd57984cdde775377fccdf65a9c441c65f",
    "c99c0e898899162f433235301cc23d247668ef1740ebe78e233beb823580516a",
    "47f261bfedb330ce47f00a78139666fc8beb306c868ebd467d2b4f1d6fcb8814",
    "f1c8d3fb3c62e10216b4ac659e4c2e87de1d789d316ca9211d37c56bba486229",
    "f193cffe490390b2e9d87a70c0ed2711f3f1faf558db898549cec26ded2dab9c",
    "351e59955e87cee541bb3e319b4ddaf57f5532f333fc0b92db54f296c5044d68",
)
_NAMES = (
    "schema_migration_ledger",
    "pgvector_extension",
    "runtime_truth_baseline",
    "memory_substrate_baseline",
    "memory_governance_baseline",
    "durable_projection_jobs",
    "canonical_mutation_surface_jobs",
    "run_timeline_projection_activation",
    "tool_result_evidence_projection_activation",
    "compaction_memory_extraction_projection_activation",
    "mcp_continuation_secret_store",
    "terminal_presentation_queue",
)
_AUXILIARY_RESOURCES: tuple[tuple[tuple[str, str], ...], ...] = (
    (),
    (),
    (),
    (),
    (),
    (
        (
            "0005_runtime_write_protected_relations_v1.json",
            "19338e4dc4fdb525a74339e36255e153fa51d767eff57e078c5a62013b8506df",
        ),
    ),
    (
        (
            "0006_runtime_write_protected_relations_v2.json",
            "12c61a50c3faa7673adaca19bfb9b8706af1ec88bdfe0ba89ed846ec92459565",
        ),
        (
            "0006_pre_activation_projection_contracts_v1.json",
            "f2298d619a8f0de1dd86d3daa55835861fcc996b001a0f0f4aeb40c3430f0a1b",
        ),
        (
            "0006_legacy_surface_binding_plan_contract_v1.json",
            "913fcf66242f69ef5aa41f0b581b7941ccf286257c1b671d4776f3c39af90e4c",
        ),
    ),
    (
        (
            "0007_run_timeline_activation_v1.json",
            "6f459b6eee3c4d0cab9e0293d48f39771f6b2ce3c94219c3d5f7f0241ac1ce26",
        ),
    ),
    (
        (
            "0008_tool_result_evidence_activation_v1.json",
            "c52e0bfad4bd26a6afd11375c5d1d1c3422cca28fd2b2380b8296b8ddef6c82f",
        ),
    ),
    (
        (
            "0009_compaction_memory_extraction_activation_v1.json",
            "d225ad7953d474c2a4bedf105ddcfef2b733efb6d1787fd91377deab4259d03e",
        ),
        (
            "0009_runtime_write_protected_relations_v1.json",
            "4b25ed93abb3bf1cacc43ae0b2c397b03dd3b4a5b9138097a921ad82bbf34b0b",
        ),
    ),
    (),
    (
        (
            "0011_runtime_write_protected_relations_v1.json",
            "a42841f95e6616a69cf96c154f5a29a3eb7af8f0afcb38f025b186b03ec94e84",
        ),
    ),
)


def _migration_contract_payload(
    *,
    version: int,
    name: str,
    expected_sha256: str,
    transaction_mode: str,
    postcondition_contract_fingerprint: str,
    resulting_object_manifest_fingerprint: str,
    runtime_grant_policy_fingerprint: str,
    auxiliary_resource_checksums: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": version,
        "name": name,
        "expected_sha256": expected_sha256,
        "transaction_mode": transaction_mode,
        "postcondition_contract_fingerprint": postcondition_contract_fingerprint,
        "resulting_object_manifest_fingerprint": (
            resulting_object_manifest_fingerprint
        ),
        "runtime_grant_policy_fingerprint": runtime_grant_policy_fingerprint,
    }
    if auxiliary_resource_checksums:
        payload["auxiliary_resource_checksums"] = auxiliary_resource_checksums
    return payload


def _build_registry() -> PostgresMigrationRegistry:
    definitions: list[PostgresMigrationDefinition] = []
    previous_prefix = _GENESIS_PREFIX
    for version, (name, checksum, auxiliary_resources) in enumerate(
        zip(
            _NAMES,
            _RESOURCE_CHECKSUMS,
            _AUXILIARY_RESOURCES,
            strict=True,
        )
    ):
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
        contract_payload = _migration_contract_payload(
            version=version,
            name=name,
            expected_sha256=checksum,
            transaction_mode="atomic",
            postcondition_contract_fingerprint=postcondition,
            resulting_object_manifest_fingerprint=manifest.manifest_fingerprint,
            runtime_grant_policy_fingerprint=grant_policy.policy_fingerprint,
            auxiliary_resource_checksums=auxiliary_resources,
        )
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
                auxiliary_resource_checksums=auxiliary_resources,
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
        if item.resulting_object_manifest_fingerprint != manifest.manifest_fingerprint:
            raise ValueError("migration manifest fingerprint mismatch")
        if item.runtime_grant_policy_fingerprint != grant_policy.policy_fingerprint:
            raise ValueError("migration grant policy fingerprint mismatch")
        contract = postgres_schema_fingerprint(
            "pulsara:postgres-migration-contract:v1",
            _migration_contract_payload(
                version=item.version,
                name=item.name,
                expected_sha256=item.expected_sha256,
                transaction_mode=item.transaction_mode,
                postcondition_contract_fingerprint=postcondition,
                resulting_object_manifest_fingerprint=(manifest.manifest_fingerprint),
                runtime_grant_policy_fingerprint=grant_policy.policy_fingerprint,
                auxiliary_resource_checksums=item.auxiliary_resource_checksums,
            ),
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
    if (
        registry.registry_fingerprint
        != registry.definitions[-1].registry_prefix_fingerprint
    ):
        raise ValueError("registry fingerprint/head prefix mismatch")


POSTGRES_MIGRATION_REGISTRY = _build_registry()


__all__ = [
    "POSTGRES_MIGRATION_REGISTRY",
    "PostgresMigrationDefinition",
    "PostgresMigrationRegistry",
]
