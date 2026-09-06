"""Database-independent closed local settings for Pulsara."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal, TypeVar
from uuid import uuid4

import yaml

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.llm.model_connections import (
    ModelConnectionConfig,
    ModelConnectionId,
    model_connection_from_dict,
    model_connection_to_dict,
)
from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    open_or_create_absolute_directory_nofollow,
)
from pulsara_agent.mcp_credentials import (
    LocalMcpCredential,
    LocalMcpOAuthRecord,
    McpCredentialBinding,
    McpCredentialOwner,
    binding_from_dict,
    binding_to_dict,
    owner_from_dict,
    owner_to_dict,
)


LOCAL_SETTINGS_SCHEMA = "pulsara-local-settings:v2"
LOCAL_SETTINGS_FILE_NAME = "local-settings.yaml"
MAXIMUM_LOCAL_SETTINGS_BYTES = 1 << 20
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True, slots=True)
class LocalPostgresConfig:
    runtime_dsn: str
    admin_dsn: str | None = None

    def __post_init__(self) -> None:
        if not self.runtime_dsn or self.runtime_dsn != self.runtime_dsn.strip():
            raise ValueError("runtime PostgreSQL DSN must be non-empty")
        if self.admin_dsn is not None and (
            not self.admin_dsn or self.admin_dsn != self.admin_dsn.strip()
        ):
            raise ValueError("admin PostgreSQL DSN must be non-empty when present")
        _validate_postgres_dsn(self.runtime_dsn)
        if self.admin_dsn is not None:
            _validate_postgres_dsn(self.admin_dsn)


@dataclass(frozen=True, slots=True)
class LocalModelApiKey:
    connection_id: ModelConnectionId
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.connection_id, ModelConnectionId):
            raise TypeError("model API key connection ID must be typed")
        if not self.value:
            raise ValueError("model API key must be non-empty")


DashScopeCredentialKind = Literal["embedding", "rerank"]


@dataclass(frozen=True, slots=True)
class LocalDashScopeCredentials:
    embedding_api_key: str | None = field(default=None, repr=False)
    rerank_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value in (self.embedding_api_key, self.rerank_api_key):
            if value is not None and not value:
                raise ValueError("DashScope API key must be non-empty when present")

    def api_key(self, kind: DashScopeCredentialKind) -> str | None:
        if kind == "embedding":
            return self.embedding_api_key
        if kind == "rerank":
            return self.rerank_api_key
        raise ValueError("unknown DashScope credential kind")

    def replacing(
        self, kind: DashScopeCredentialKind, value: str | None
    ) -> "LocalDashScopeCredentials":
        if kind == "embedding":
            return LocalDashScopeCredentials(value, self.rerank_api_key)
        if kind == "rerank":
            return LocalDashScopeCredentials(self.embedding_api_key, value)
        raise ValueError("unknown DashScope credential kind")


@dataclass(frozen=True, slots=True)
class LocalSettings:
    postgres: LocalPostgresConfig | None = None
    model_connections: tuple[ModelConnectionConfig, ...] = ()
    model_api_keys: tuple[LocalModelApiKey, ...] = field(default=(), repr=False)
    dashscope_credentials: LocalDashScopeCredentials = field(
        default_factory=LocalDashScopeCredentials,
        repr=False,
    )
    mcp_credentials: tuple[LocalMcpCredential, ...] = field(default=(), repr=False)
    mcp_oauth: tuple[LocalMcpOAuthRecord, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        bindings = tuple(item.binding for item in self.mcp_credentials)
        owners = tuple(item.owner for item in self.mcp_oauth)
        if len(bindings) != len(set(bindings)) or len(owners) != len(set(owners)):
            raise ValueError("duplicate MCP private binding")
        ids = tuple(item.id for item in self.model_connections)
        if len(ids) != len(set(ids)):
            raise ValueError("local settings contain duplicate model connection IDs")
        credential_ids = tuple(item.connection_id for item in self.model_api_keys)
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("local settings contain duplicate model API keys")
        required_ids = {
            item.id for item in self.model_connections if item.requires_api_key
        }
        if set(credential_ids) != required_ids:
            raise ValueError(
                "local settings model API keys do not match bearer connections"
            )

    def connection(self, connection_id) -> ModelConnectionConfig | None:
        return next(
            (item for item in self.model_connections if item.id == connection_id),
            None,
        )

    def model_api_key(self, connection_id) -> str | None:
        return next(
            (
                item.value
                for item in self.model_api_keys
                if item.connection_id == connection_id
            ),
            None,
        )

    def require_model_api_key(self, connection_id) -> str:
        value = self.model_api_key(connection_id)
        if value is None:
            raise LocalSettingsSecretMissing("model API key is unavailable")
        return value

    def dashscope_api_key(self, kind: DashScopeCredentialKind) -> str | None:
        return self.dashscope_credentials.api_key(kind)

    def mcp_secret(self, binding: McpCredentialBinding) -> str | None:
        return next(
            (item.value for item in self.mcp_credentials if item.binding == binding),
            None,
        )

    def mcp_authorization(
        self, owner: McpCredentialOwner
    ) -> LocalMcpOAuthRecord | None:
        return next((item for item in self.mcp_oauth if item.owner == owner), None)

    def require_dashscope_api_key(self, kind: DashScopeCredentialKind) -> str:
        value = self.dashscope_api_key(kind)
        if value is None:
            raise LocalSettingsSecretMissing("DashScope API key is unavailable")
        return value


class LocalSettingsUnavailable(RuntimeError):
    pass


class LocalSettingsPublishIndeterminate(RuntimeError):
    pass


class LocalSettingsSecretMissing(RuntimeError):
    pass


class _LocalSettingsCommitUnknown(OSError):
    pass


def default_local_settings_path() -> Path:
    return require_pulsara_home() / LOCAL_SETTINGS_FILE_NAME


def local_settings_to_dict(settings: LocalSettings) -> dict[str, object]:
    return {
        "schema": LOCAL_SETTINGS_SCHEMA,
        "postgres": (
            None
            if settings.postgres is None
            else {
                "runtime_dsn": settings.postgres.runtime_dsn,
                "admin_dsn": settings.postgres.admin_dsn,
            }
        ),
        "model_connections": [
            {
                **model_connection_to_dict(item),
                "api_key": settings.model_api_key(item.id),
            }
            for item in settings.model_connections
        ],
        "dashscope_credentials": {
            "embedding_api_key": settings.dashscope_credentials.embedding_api_key,
            "rerank_api_key": settings.dashscope_credentials.rerank_api_key,
        },
        "mcp_credentials": [
            {"binding": binding_to_dict(item.binding), "value": item.value}
            for item in settings.mcp_credentials
        ],
        "mcp_oauth": [
            {
                "owner": owner_to_dict(item.owner),
                "resource_url": item.resource_url,
                "issuer": item.issuer,
                "client_id": item.client_id,
                "scope": item.scope,
                "token_json": item.token_json,
                "client_json": item.client_json,
                "expires_at": item.expires_at,
                "metadata_json": item.metadata_json,
                "auth_json": item.auth_json,
            }
            for item in settings.mcp_oauth
        ],
    }


def local_settings_from_dict(value: object) -> LocalSettings:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "postgres",
        "model_connections",
        "dashscope_credentials",
        "mcp_credentials",
        "mcp_oauth",
    }:
        raise ValueError("local settings have an invalid closed shape")
    if value["schema"] != LOCAL_SETTINGS_SCHEMA:
        raise ValueError("local settings schema is unsupported")
    raw_postgres = value["postgres"]
    postgres: LocalPostgresConfig | None
    if raw_postgres is None:
        postgres = None
    elif isinstance(raw_postgres, dict) and set(raw_postgres) == {
        "runtime_dsn",
        "admin_dsn",
    }:
        runtime_dsn = raw_postgres["runtime_dsn"]
        admin_dsn = raw_postgres["admin_dsn"]
        if not isinstance(runtime_dsn, str) or (
            admin_dsn is not None and not isinstance(admin_dsn, str)
        ):
            raise ValueError("PostgreSQL settings fields are invalid")
        postgres = LocalPostgresConfig(runtime_dsn, admin_dsn)
    else:
        raise ValueError("PostgreSQL settings have an invalid closed shape")
    raw_connections = value["model_connections"]
    if not isinstance(raw_connections, list):
        raise ValueError("model_connections must be an array")
    connections: list[ModelConnectionConfig] = []
    model_api_keys: list[LocalModelApiKey] = []
    for item in raw_connections:
        if not isinstance(item, dict) or "api_key" not in item:
            raise ValueError("model connection settings have an invalid closed shape")
        api_key = item["api_key"]
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError("model connection API key is invalid")
        raw_connection = dict(item)
        raw_connection.pop("api_key")
        connection = model_connection_from_dict(raw_connection)
        connections.append(connection)
        if api_key is not None:
            model_api_keys.append(LocalModelApiKey(connection.id, api_key))
    raw_dashscope = value["dashscope_credentials"]
    if not isinstance(raw_dashscope, dict) or set(raw_dashscope) != {
        "embedding_api_key",
        "rerank_api_key",
    }:
        raise ValueError("DashScope credentials have an invalid closed shape")
    embedding_api_key = raw_dashscope["embedding_api_key"]
    rerank_api_key = raw_dashscope["rerank_api_key"]
    if any(
        item is not None and (not isinstance(item, str) or not item)
        for item in (embedding_api_key, rerank_api_key)
    ):
        raise ValueError("DashScope API key is invalid")
    return LocalSettings(
        postgres=postgres,
        model_connections=tuple(connections),
        model_api_keys=tuple(model_api_keys),
        dashscope_credentials=LocalDashScopeCredentials(
            embedding_api_key=embedding_api_key,
            rerank_api_key=rerank_api_key,
        ),
        mcp_credentials=_mcp_credentials_from_dict(value["mcp_credentials"]),
        mcp_oauth=_mcp_oauth_from_dict(value["mcp_oauth"]),
    )


def _mcp_credentials_from_dict(raw: object) -> tuple[LocalMcpCredential, ...]:
    if not isinstance(raw, list):
        raise ValueError("MCP credentials must be an array")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"binding", "value"}:
            raise ValueError("invalid MCP credential shape")
        result.append(
            LocalMcpCredential(binding_from_dict(item["binding"]), item["value"])
        )
    return tuple(result)


def _mcp_oauth_from_dict(raw: object) -> tuple[LocalMcpOAuthRecord, ...]:
    if not isinstance(raw, list):
        raise ValueError("MCP authorizations must be an array")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "owner",
            "resource_url",
            "issuer",
            "client_id",
            "scope",
            "token_json",
            "client_json",
            "expires_at",
            "metadata_json",
            "auth_json",
        }:
            raise ValueError("invalid MCP authorization shape")
        result.append(
            LocalMcpOAuthRecord(**{**item, "owner": owner_from_dict(item["owner"])})
        )
    return tuple(result)


def read_local_settings(path: Path | None = None) -> LocalSettings:
    settings_path = path or default_local_settings_path()
    if not settings_path.is_absolute():
        raise ValueError("local settings path must be absolute")
    try:
        parent = open_absolute_directory_nofollow(settings_path.parent)
    except FileNotFoundError:
        return LocalSettings()
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(settings_path.name, _READ_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return LocalSettings()
        parent_metadata = os.fstat(parent)
        if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
            raise LocalSettingsUnavailable(
                "local settings directory permissions are too broad"
            )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LocalSettingsUnavailable("local settings are not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise LocalSettingsUnavailable(
                "local settings file permissions are too broad"
            )
        if metadata.st_size > MAXIMUM_LOCAL_SETTINGS_BYTES:
            raise LocalSettingsUnavailable("local settings exceed their byte bound")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            raw = stream.read(MAXIMUM_LOCAL_SETTINGS_BYTES + 1)
        if len(raw) > MAXIMUM_LOCAL_SETTINGS_BYTES:
            raise LocalSettingsUnavailable("local settings exceed their byte bound")
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        if isinstance(exc, LocalSettingsUnavailable):
            raise
        raise LocalSettingsUnavailable("local settings cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
    try:
        decoded = yaml.safe_load(raw.decode("utf-8"))
        return local_settings_from_dict(decoded)
    except (UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise LocalSettingsUnavailable("local settings are invalid") from exc


def write_local_settings(path: Path, settings: LocalSettings) -> None:
    if not path.is_absolute():
        raise ValueError("local settings path must be absolute")
    payload = yaml.safe_dump(
        local_settings_to_dict(settings),
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    if len(payload) > MAXIMUM_LOCAL_SETTINGS_BYTES:
        raise ValueError("local settings exceed their byte bound")
    parent = open_or_create_absolute_directory_nofollow(path.parent, mode=0o700)
    temporary = f".{path.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    replaced = False
    try:
        os.fchmod(parent, 0o700)
        descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        replaced = True
        os.fsync(parent)
    except BaseException as exc:
        if replaced:
            raise _LocalSettingsCommitUnknown(
                "local settings publication result is unknown"
            ) from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


SettingsWriter = Callable[[Path, LocalSettings], None]
MutationResult = TypeVar("MutationResult")


class LocalSettingsStore:
    """The sole process-local read/modify/write owner for the closed document."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        writer: SettingsWriter = write_local_settings,
    ) -> None:
        self.path = path or default_local_settings_path()
        if not self.path.is_absolute():
            raise ValueError("local settings path must be absolute")
        self._writer = writer
        self._lock = asyncio.Lock()

    def read(self) -> LocalSettings:
        return read_local_settings(self.path)

    def resolve_mcp_secret(self, binding: McpCredentialBinding) -> str | None:
        return self.read().mcp_secret(binding)

    async def replace_mcp_secrets(
        self,
        owner: McpCredentialOwner,
        changes: tuple[tuple[McpCredentialBinding, str | None], ...],
    ) -> LocalSettings:
        if any(binding.owner != owner for binding, _ in changes):
            raise ValueError("MCP secret mutation crosses its owner")
        if len({binding for binding, _ in changes}) != len(changes):
            raise ValueError("duplicate MCP secret mutation")
        additions = tuple(
            LocalMcpCredential(binding, value)
            for binding, value in changes
            if value is not None
        )
        changed = {binding for binding, _ in changes}
        updated, _ = await self._run_mutation(
            lambda current: (
                replace(
                    current,
                    mcp_credentials=tuple(
                        item
                        for item in current.mcp_credentials
                        if item.binding not in changed
                    )
                    + additions,
                ),
                None,
            ),
            name="publish-mcp-credentials",
            repair=False,
        )
        return updated

    async def replace_mcp_authorization(
        self,
        owner: McpCredentialOwner,
        record: LocalMcpOAuthRecord | None,
        *,
        expected: LocalMcpOAuthRecord | None,
    ) -> LocalSettings:
        if record is not None and record.owner != owner:
            raise ValueError("OAuth record crosses its owner")

        def mutation(current: LocalSettings) -> tuple[LocalSettings, None]:
            if current.mcp_authorization(owner) != expected:
                raise ValueError("MCP authorization changed")
            return replace(
                current,
                mcp_oauth=tuple(
                    item for item in current.mcp_oauth if item.owner != owner
                )
                + (() if record is None else (record,)),
            ), None

        updated, _ = await self._run_mutation(
            mutation, name="publish-mcp-authorization", repair=False
        )
        return updated

    async def remove_mcp_credentials(self, owner: McpCredentialOwner) -> LocalSettings:
        # Caller holds the canonical mutation lane and has verified owner removal.
        updated, _ = await self._run_mutation(
            lambda current: (
                replace(
                    current,
                    mcp_credentials=tuple(
                        item
                        for item in current.mcp_credentials
                        if item.binding.owner != owner
                    ),
                    mcp_oauth=tuple(
                        item for item in current.mcp_oauth if item.owner != owner
                    ),
                ),
                None,
            ),
            name="remove-mcp-credentials",
            repair=False,
        )
        return updated

    def _read_for_explicit_repair(self) -> LocalSettings:
        """Read the latest document or start a user-requested replacement.

        A malformed document remains unavailable to ordinary readers.  An
        explicit settings mutation is the only repair boundary: the user has
        supplied the replacement value, so the closed document can be rebuilt
        without introducing a legacy parser or hidden recovery authority.
        """

        try:
            return self.read()
        except LocalSettingsUnavailable:
            return LocalSettings()

    async def save_postgres(
        self, postgres: LocalPostgresConfig | None
    ) -> LocalSettings:
        updated, _ = await self._run_mutation(
            lambda current: (
                LocalSettings(
                    postgres=postgres,
                    model_connections=current.model_connections,
                    model_api_keys=current.model_api_keys,
                    dashscope_credentials=current.dashscope_credentials,
                    mcp_credentials=current.mcp_credentials,
                    mcp_oauth=current.mcp_oauth,
                ),
                None,
            ),
            name="publish-postgres-settings",
        )
        return updated

    async def add_model_connection(
        self,
        *,
        connection: ModelConnectionConfig,
        api_key: str | None,
    ) -> LocalSettings:
        if connection.requires_api_key and not api_key:
            raise ValueError("model connection requires an API key")
        if not connection.requires_api_key and api_key is not None:
            raise ValueError("model connection without authentication cannot own a key")
        updated, _ = await self._run_mutation(
            lambda current: self._add_model_connection_value(
                current,
                connection=connection,
                api_key=api_key,
            ),
            name="publish-model-connection",
        )
        return updated

    @staticmethod
    def _add_model_connection_value(
        current: LocalSettings,
        *,
        connection: ModelConnectionConfig,
        api_key: str | None,
    ) -> tuple[LocalSettings, None]:
        if current.connection(connection.id) is not None:
            raise ValueError("model connection ID already exists")
        key = () if api_key is None else (LocalModelApiKey(connection.id, api_key),)
        return (
            LocalSettings(
                postgres=current.postgres,
                model_connections=(*current.model_connections, connection),
                model_api_keys=(*current.model_api_keys, *key),
                dashscope_credentials=current.dashscope_credentials,
                mcp_credentials=current.mcp_credentials,
                mcp_oauth=current.mcp_oauth,
            ),
            None,
        )

    async def delete_model_connection(
        self, connection_id: ModelConnectionId
    ) -> tuple[LocalSettings, bool]:
        return await self._run_mutation(
            lambda current: (
                LocalSettings(
                    postgres=current.postgres,
                    model_connections=tuple(
                        item
                        for item in current.model_connections
                        if item.id != connection_id
                    ),
                    model_api_keys=tuple(
                        item
                        for item in current.model_api_keys
                        if item.connection_id != connection_id
                    ),
                    dashscope_credentials=current.dashscope_credentials,
                    mcp_credentials=current.mcp_credentials,
                    mcp_oauth=current.mcp_oauth,
                ),
                current.connection(connection_id) is not None,
            ),
            name="delete-model-connection",
        )

    async def save_dashscope_api_key(
        self, kind: DashScopeCredentialKind, api_key: str
    ) -> LocalSettings:
        if not api_key:
            raise ValueError("DashScope API key must be non-empty")
        updated, _ = await self._run_mutation(
            lambda current: (
                LocalSettings(
                    postgres=current.postgres,
                    model_connections=current.model_connections,
                    model_api_keys=current.model_api_keys,
                    dashscope_credentials=current.dashscope_credentials.replacing(
                        kind, api_key
                    ),
                    mcp_credentials=current.mcp_credentials,
                    mcp_oauth=current.mcp_oauth,
                ),
                None,
            ),
            name=f"save-dashscope-{kind}-api-key",
        )
        return updated

    async def delete_dashscope_api_key(
        self, kind: DashScopeCredentialKind
    ) -> LocalSettings:
        updated, _ = await self._run_mutation(
            lambda current: (
                LocalSettings(
                    postgres=current.postgres,
                    model_connections=current.model_connections,
                    model_api_keys=current.model_api_keys,
                    dashscope_credentials=current.dashscope_credentials.replacing(
                        kind, None
                    ),
                    mcp_credentials=current.mcp_credentials,
                    mcp_oauth=current.mcp_oauth,
                ),
                None,
            ),
            name=f"delete-dashscope-{kind}-api-key",
        )
        return updated

    async def _run_mutation(
        self,
        mutation: Callable[[LocalSettings], tuple[LocalSettings, MutationResult]],
        *,
        name: str,
        repair: bool = True,
    ) -> tuple[LocalSettings, MutationResult]:
        settlement = asyncio.create_task(
            self._mutate(mutation, repair=repair), name=name
        )
        cancelled: asyncio.CancelledError | None = None
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                continue
        result = settlement.result()
        if cancelled is not None:
            raise cancelled
        return result

    async def _mutate(
        self,
        mutation: Callable[[LocalSettings], tuple[LocalSettings, MutationResult]],
        *,
        repair: bool = True,
    ) -> tuple[LocalSettings, MutationResult]:
        async with self._lock:
            current = await asyncio.to_thread(
                self._read_for_explicit_repair if repair else self.read
            )
            updated, result = mutation(current)
            if updated == current:
                return current, result
            try:
                await asyncio.to_thread(self._writer, self.path, updated)
            except _LocalSettingsCommitUnknown as exc:
                try:
                    observed = await asyncio.to_thread(self.read)
                except LocalSettingsUnavailable as read_exc:
                    raise LocalSettingsPublishIndeterminate(
                        "local settings publication is indeterminate"
                    ) from read_exc
                if observed != updated:
                    raise LocalSettingsPublishIndeterminate(
                        "local settings publication resolved to another value"
                    ) from exc
                return observed, result
            return updated, result


def _validate_postgres_dsn(value: str) -> None:
    from psycopg.conninfo import conninfo_to_dict

    try:
        parsed = conninfo_to_dict(value)
    except Exception as exc:
        raise ValueError("PostgreSQL DSN syntax is invalid") from exc
    if not parsed.get("dbname"):
        raise ValueError("PostgreSQL DSN must name a database")


__all__ = [
    "LOCAL_SETTINGS_FILE_NAME",
    "LOCAL_SETTINGS_SCHEMA",
    "DashScopeCredentialKind",
    "LocalDashScopeCredentials",
    "LocalModelApiKey",
    "LocalPostgresConfig",
    "LocalSettings",
    "LocalSettingsPublishIndeterminate",
    "LocalSettingsSecretMissing",
    "LocalSettingsStore",
    "LocalSettingsUnavailable",
    "default_local_settings_path",
    "local_settings_from_dict",
    "local_settings_to_dict",
    "read_local_settings",
    "write_local_settings",
]
