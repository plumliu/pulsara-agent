"""The current conversation-kernel PostgreSQL baseline resource."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from pulsara_agent.storage.migrations.contracts import (
    BASELINE_NAME, BASELINE_RESOURCE, CATALOG_RESOURCE, GRANT_RESOURCE,
)


@dataclass(frozen=True, slots=True)
class PostgresMigrationDefinition:
    name: str
    resource_name: str
    catalog_resource_name: str
    grant_resource_name: str

    @property
    def expected_sha256(self) -> str:
        return _BASELINE_SQL_SHA256

    def resource_bytes(self) -> bytes:
        payload = _resource_bytes("pulsara_agent.storage.migrations.sql", self.resource_name)
        if sha256(payload).hexdigest() != self.expected_sha256:
            raise ValueError("clean baseline SQL checksum mismatch")
        return payload

    def resource_text(self) -> str:
        return self.resource_bytes().decode("utf-8")

    def verify_auxiliary_resources(self) -> None:
        for name, checksum in (
            (self.catalog_resource_name, _CATALOG_SHA256),
            (self.grant_resource_name, _GRANT_SHA256),
        ):
            if _checksum("pulsara_agent.storage.migrations.resources", name) != checksum:
                raise ValueError(f"clean migration resource checksum mismatch: {name}")


def _resource_bytes(package: str, resource: str) -> bytes:
    # SQL execution and its durable checksum use the same LF representation,
    # independent of checkout or wheel line endings.
    return files(package).joinpath(resource).read_bytes().replace(b"\r\n", b"\n")


def _checksum(package: str, resource: str) -> str:
    return sha256(_resource_bytes(package, resource)).hexdigest()


_BASELINE_SQL_SHA256 = _checksum("pulsara_agent.storage.migrations.sql", BASELINE_RESOURCE)
_CATALOG_SHA256 = _checksum("pulsara_agent.storage.migrations.resources", CATALOG_RESOURCE)
_GRANT_SHA256 = _checksum("pulsara_agent.storage.migrations.resources", GRANT_RESOURCE)

POSTGRES_BASELINE = PostgresMigrationDefinition(
    name=BASELINE_NAME,
    resource_name=BASELINE_RESOURCE,
    catalog_resource_name=CATALOG_RESOURCE,
    grant_resource_name=GRANT_RESOURCE,
)

__all__ = ["POSTGRES_BASELINE", "PostgresMigrationDefinition"]
