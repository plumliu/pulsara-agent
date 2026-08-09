"""Typed, admin-only runtime privilege reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias

from psycopg import Connection, sql

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.manifest import build_postgres_schema_manifest


class PostgresSchemaPrivilege(StrEnum):
    USAGE = "USAGE"


class PostgresRelationPrivilege(StrEnum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class PostgresFunctionPrivilege(StrEnum):
    EXECUTE = "EXECUTE"


class PostgresTypePrivilege(StrEnum):
    USAGE = "USAGE"


@dataclass(frozen=True, slots=True)
class PostgresSchemaGrantTargetFact:
    target_kind: Literal["schema"]
    schema_name: str


@dataclass(frozen=True, slots=True)
class PostgresRelationGrantTargetFact:
    target_kind: Literal["relation"]
    schema_name: str
    relation_name: str
    relation_kind: Literal["table"]


@dataclass(frozen=True, slots=True)
class PostgresFunctionGrantTargetFact:
    target_kind: Literal["function"]
    schema_name: str
    function_name: str
    ordered_argument_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PostgresTypeGrantTargetFact:
    target_kind: Literal["type"]
    schema_name: str
    type_name: str


PostgresGrantTargetFact: TypeAlias = (
    PostgresSchemaGrantTargetFact
    | PostgresRelationGrantTargetFact
    | PostgresFunctionGrantTargetFact
    | PostgresTypeGrantTargetFact
)


@dataclass(frozen=True, slots=True)
class PostgresRuntimeGrantRequirementFact:
    target: PostgresGrantTargetFact
    ordered_required_privileges: tuple[str, ...]
    requirement_fingerprint: str

    def __post_init__(self) -> None:
        if not self.ordered_required_privileges or len(
            self.ordered_required_privileges
        ) != len(set(self.ordered_required_privileges)):
            raise ValueError("grant privileges must be non-empty and unique")
        if isinstance(self.target, PostgresSchemaGrantTargetFact):
            allowed = {item.value for item in PostgresSchemaPrivilege}
        elif isinstance(self.target, PostgresRelationGrantTargetFact):
            allowed = {item.value for item in PostgresRelationPrivilege}
        elif isinstance(self.target, PostgresFunctionGrantTargetFact):
            allowed = {item.value for item in PostgresFunctionPrivilege}
        elif isinstance(self.target, PostgresTypeGrantTargetFact):
            allowed = {item.value for item in PostgresTypePrivilege}
        else:  # pragma: no cover - closed union
            raise TypeError("unknown grant target")
        if not set(self.ordered_required_privileges) <= allowed:
            raise ValueError("grant privilege is invalid for its target kind")
        expected = postgres_schema_fingerprint(
            "pulsara:postgres-runtime-grant-requirement:v1",
            {
                "target": self.target,
                "ordered_required_privileges": self.ordered_required_privileges,
            },
        )
        if self.requirement_fingerprint != expected:
            raise ValueError("grant requirement fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PostgresRuntimeGrantPolicyFact:
    through_version: int
    requirements: tuple[PostgresRuntimeGrantRequirementFact, ...]
    revocations: tuple["PostgresRuntimePrivilegeRevocationFact", ...]
    policy_fingerprint: str

    def __post_init__(self) -> None:
        if self.through_version < 0:
            raise ValueError("grant policy through_version must be non-negative")
        fingerprint_payload: dict[str, object] = {
            "through_version": self.through_version,
            "requirement_fingerprints": tuple(
                item.requirement_fingerprint for item in self.requirements
            ),
        }
        if self.through_version >= 13:
            fingerprint_payload["revocation_fingerprints"] = tuple(
                item.revocation_fingerprint for item in self.revocations
            )
        expected = postgres_schema_fingerprint(
            "pulsara:postgres-runtime-grant-policy:v1",
            fingerprint_payload,
        )
        if self.policy_fingerprint != expected:
            raise ValueError("grant policy fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PostgresRuntimePrivilegeRevocationFact:
    target: PostgresGrantTargetFact
    ordered_revoked_privileges: tuple[str, ...]
    revocation_fingerprint: str

    def __post_init__(self) -> None:
        _validate_target_privileges(
            self.target,
            self.ordered_revoked_privileges,
            operation="revocation",
        )
        expected = postgres_schema_fingerprint(
            "pulsara:postgres-runtime-privilege-revocation:v1",
            {
                "target": self.target,
                "ordered_revoked_privileges": self.ordered_revoked_privileges,
            },
        )
        if self.revocation_fingerprint != expected:
            raise ValueError("privilege revocation fingerprint mismatch")


def _requirement(
    target: PostgresGrantTargetFact,
    privileges: tuple[str, ...],
) -> PostgresRuntimeGrantRequirementFact:
    if not privileges or len(privileges) != len(set(privileges)):
        raise ValueError("grant privileges must be non-empty and unique")
    payload = {"target": target, "ordered_required_privileges": privileges}
    return PostgresRuntimeGrantRequirementFact(
        target=target,
        ordered_required_privileges=privileges,
        requirement_fingerprint=postgres_schema_fingerprint(
            "pulsara:postgres-runtime-grant-requirement:v1", payload
        ),
    )


def _revocation(
    target: PostgresGrantTargetFact,
    privileges: tuple[str, ...],
) -> PostgresRuntimePrivilegeRevocationFact:
    _validate_target_privileges(target, privileges, operation="revocation")
    payload = {"target": target, "ordered_revoked_privileges": privileges}
    return PostgresRuntimePrivilegeRevocationFact(
        target=target,
        ordered_revoked_privileges=privileges,
        revocation_fingerprint=postgres_schema_fingerprint(
            "pulsara:postgres-runtime-privilege-revocation:v1", payload
        ),
    )


def _validate_target_privileges(
    target: PostgresGrantTargetFact,
    privileges: tuple[str, ...],
    *,
    operation: str,
) -> None:
    if not privileges or len(privileges) != len(set(privileges)):
        raise ValueError(f"{operation} privileges must be non-empty and unique")
    if isinstance(target, PostgresSchemaGrantTargetFact):
        allowed = {item.value for item in PostgresSchemaPrivilege}
    elif isinstance(target, PostgresRelationGrantTargetFact):
        allowed = {item.value for item in PostgresRelationPrivilege}
    elif isinstance(target, PostgresFunctionGrantTargetFact):
        allowed = {item.value for item in PostgresFunctionPrivilege}
    elif isinstance(target, PostgresTypeGrantTargetFact):
        allowed = {item.value for item in PostgresTypePrivilege}
    else:  # pragma: no cover - closed union
        raise TypeError("unknown privilege target")
    if not set(privileges) <= allowed:
        raise ValueError(f"{operation} privilege is invalid for its target kind")


def build_postgres_runtime_grant_policy(
    through_version: int,
) -> PostgresRuntimeGrantPolicyFact:
    manifest = build_postgres_schema_manifest(through_version)
    schema_names = tuple(
        sorted(
            {"public"}
            | {
                str(relation["schema_name"])
                for relation in manifest.owned_relations
            }
        )
    )
    requirements: list[PostgresRuntimeGrantRequirementFact] = [
        _requirement(
            PostgresSchemaGrantTargetFact(
                target_kind="schema", schema_name=schema_name
            ),
            (PostgresSchemaPrivilege.USAGE.value,),
        )
        for schema_name in schema_names
    ]
    for relation in manifest.owned_relations:
        declared_privileges = relation.get("runtime_privileges")
        if declared_privileges is None:
            writable = bool(relation["runtime_writable"])
            privileges = (
                (
                    PostgresRelationPrivilege.SELECT.value,
                    PostgresRelationPrivilege.INSERT.value,
                    PostgresRelationPrivilege.UPDATE.value,
                    PostgresRelationPrivilege.DELETE.value,
                )
                if writable
                else (PostgresRelationPrivilege.SELECT.value,)
            )
        else:
            privileges = tuple(str(item) for item in declared_privileges)
        if not privileges:
            continue
        requirements.append(
            _requirement(
                PostgresRelationGrantTargetFact(
                    target_kind="relation",
                    schema_name=str(relation["schema_name"]),
                    relation_name=str(relation["relation_name"]),
                    relation_kind="table",
                ),
                privileges,
            )
        )
    for function in manifest.required_functions:
        if function.get("runtime_executable") is False:
            continue
        requirements.append(
            _requirement(
                PostgresFunctionGrantTargetFact(
                    target_kind="function",
                    schema_name=str(function["schema_name"]),
                    function_name=str(function["function_name"]),
                    ordered_argument_types=tuple(function["ordered_argument_types"]),
                ),
                (PostgresFunctionPrivilege.EXECUTE.value,),
            )
        )
    for type_contract in manifest.required_types:
        requirements.append(
            _requirement(
                PostgresTypeGrantTargetFact(
                    target_kind="type",
                    schema_name=str(type_contract["schema_name"]),
                    type_name=str(type_contract["type_name"]),
                ),
                (PostgresTypePrivilege.USAGE.value,),
            )
        )
    ordered = tuple(requirements)
    revocations: tuple[PostgresRuntimePrivilegeRevocationFact, ...] = ()
    if through_version >= 13:
        legacy_manifest = build_postgres_schema_manifest(12)
        revocations = tuple(
            _revocation(
                PostgresRelationGrantTargetFact(
                    target_kind="relation",
                    schema_name="public",
                    relation_name=str(relation["relation_name"]),
                    relation_kind="table",
                ),
                tuple(item.value for item in PostgresRelationPrivilege),
            )
            for relation in legacy_manifest.owned_relations
            if str(relation["schema_name"]) == "public"
            and str(relation["relation_name"]) != "pulsara_schema_migrations"
        )
    fingerprint_payload: dict[str, object] = {
        "through_version": through_version,
        "requirement_fingerprints": tuple(
            requirement.requirement_fingerprint for requirement in ordered
        ),
    }
    if through_version >= 13:
        fingerprint_payload["revocation_fingerprints"] = tuple(
            item.revocation_fingerprint for item in revocations
        )
    fingerprint = postgres_schema_fingerprint(
        "pulsara:postgres-runtime-grant-policy:v1", fingerprint_payload
    )
    return PostgresRuntimeGrantPolicyFact(
        through_version=through_version,
        requirements=ordered,
        revocations=revocations,
        policy_fingerprint=fingerprint,
    )


class PostgresRuntimeGrantExecutor:
    """The sole Python owner allowed to construct PostgreSQL GRANT statements."""

    def apply_requirement(
        self,
        connection: Connection,
        *,
        runtime_role: str,
        requirement: PostgresRuntimeGrantRequirementFact,
    ) -> None:
        target = requirement.target
        privileges = sql.SQL(", ").join(
            sql.SQL(privilege) for privilege in requirement.ordered_required_privileges
        )
        if isinstance(target, PostgresSchemaGrantTargetFact):
            statement = sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(runtime_role),
            )
        elif isinstance(target, PostgresRelationGrantTargetFact):
            statement = sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(target.relation_name),
                sql.Identifier(runtime_role),
            )
        elif isinstance(target, PostgresFunctionGrantTargetFact):
            arguments = sql.SQL(", ").join(
                _qualified_type_sql(item) for item in target.ordered_argument_types
            )
            statement = sql.SQL("GRANT {} ON FUNCTION {}.{}({}) TO {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(target.function_name),
                arguments,
                sql.Identifier(runtime_role),
            )
        elif isinstance(target, PostgresTypeGrantTargetFact):
            statement = sql.SQL("GRANT {} ON TYPE {}.{} TO {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(target.type_name),
                sql.Identifier(runtime_role),
            )
        else:  # pragma: no cover - closed union
            raise TypeError("unsupported grant target")
        connection.execute(statement)

    def apply_revocation(
        self,
        connection: Connection,
        *,
        runtime_role: str,
        revocation: PostgresRuntimePrivilegeRevocationFact,
    ) -> None:
        target = revocation.target
        privileges = sql.SQL(", ").join(
            sql.SQL(privilege)
            for privilege in revocation.ordered_revoked_privileges
        )
        if isinstance(target, PostgresSchemaGrantTargetFact):
            statement = sql.SQL("REVOKE {} ON SCHEMA {} FROM {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(runtime_role),
            )
        elif isinstance(target, PostgresRelationGrantTargetFact):
            statement = sql.SQL("REVOKE {} ON TABLE {}.{} FROM {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(target.relation_name),
                sql.Identifier(runtime_role),
            )
        elif isinstance(target, PostgresFunctionGrantTargetFact):
            arguments = sql.SQL(", ").join(
                _qualified_type_sql(item) for item in target.ordered_argument_types
            )
            statement = sql.SQL("REVOKE {} ON FUNCTION {}.{}({}) FROM {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(target.function_name),
                arguments,
                sql.Identifier(runtime_role),
            )
        elif isinstance(target, PostgresTypeGrantTargetFact):
            statement = sql.SQL("REVOKE {} ON TYPE {}.{} FROM {}").format(
                privileges,
                sql.Identifier(target.schema_name),
                sql.Identifier(target.type_name),
                sql.Identifier(runtime_role),
            )
        else:  # pragma: no cover - closed union
            raise TypeError("unsupported privilege revocation target")
        connection.execute(statement)


def _qualified_type_sql(type_identity: str) -> sql.Composed:
    parts = type_identity.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError("function argument type must be schema-qualified")
    return sql.SQL("{}.{}").format(sql.Identifier(parts[0]), sql.Identifier(parts[1]))


__all__ = [
    "PostgresFunctionGrantTargetFact",
    "PostgresGrantTargetFact",
    "PostgresRelationGrantTargetFact",
    "PostgresRuntimeGrantExecutor",
    "PostgresRuntimeGrantPolicyFact",
    "PostgresRuntimeGrantRequirementFact",
    "PostgresRuntimePrivilegeRevocationFact",
    "PostgresSchemaGrantTargetFact",
    "PostgresTypeGrantTargetFact",
    "build_postgres_runtime_grant_policy",
]
