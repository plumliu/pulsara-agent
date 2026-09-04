"""Database-independent closed local settings for Pulsara."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

import yaml

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.llm.model_connections import (
    ModelConnectionConfig,
    model_connection_from_dict,
    model_connection_to_dict,
)
from pulsara_agent.local_credentials import LocalCredentialStore, ModelProviderCredential
from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    open_or_create_absolute_directory_nofollow,
)


LOCAL_SETTINGS_SCHEMA = "pulsara-local-settings:v1"
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
class LocalSettings:
    postgres: LocalPostgresConfig | None = None
    model_connections: tuple[ModelConnectionConfig, ...] = ()

    def __post_init__(self) -> None:
        ids = tuple(item.id for item in self.model_connections)
        if len(ids) != len(set(ids)):
            raise ValueError("local settings contain duplicate model connection IDs")

    def connection(self, connection_id) -> ModelConnectionConfig | None:
        return next(
            (item for item in self.model_connections if item.id == connection_id),
            None,
        )


class LocalSettingsUnavailable(RuntimeError):
    pass


class LocalSettingsPublishIndeterminate(RuntimeError):
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
            model_connection_to_dict(item) for item in settings.model_connections
        ],
    }


def local_settings_from_dict(value: object) -> LocalSettings:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "postgres",
        "model_connections",
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
    return LocalSettings(
        postgres=postgres,
        model_connections=tuple(
            model_connection_from_dict(item) for item in raw_connections
        ),
    )


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
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LocalSettingsUnavailable("local settings are not a regular file")
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
        async with self._lock:
            current = await asyncio.to_thread(self._read_for_explicit_repair)
            updated = LocalSettings(postgres, current.model_connections)
            await asyncio.to_thread(self._writer, self.path, updated)
            return updated

    async def add_model_connection(
        self,
        *,
        connection: ModelConnectionConfig,
        api_key: str,
        credentials: LocalCredentialStore,
    ) -> LocalSettings:
        key = ModelProviderCredential(connection.id)
        credentials.put(key, api_key)
        settlement = asyncio.create_task(
            self._settle_model_connection_add(
                connection=connection,
                credentials=credentials,
            ),
            name="publish-model-connection-metadata",
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

    async def _settle_model_connection_add(
        self,
        *,
        connection: ModelConnectionConfig,
        credentials: LocalCredentialStore,
    ) -> LocalSettings:
        key = ModelProviderCredential(connection.id)
        async with self._lock:
            try:
                current = await asyncio.to_thread(self._read_for_explicit_repair)
                if current.connection(connection.id) is not None:
                    raise ValueError("model connection ID already exists")
                updated = LocalSettings(
                    current.postgres,
                    (*current.model_connections, connection),
                )
                await asyncio.to_thread(self._writer, self.path, updated)
                return updated
            except _LocalSettingsCommitUnknown as exc:
                try:
                    observed = await asyncio.to_thread(self.read)
                except LocalSettingsUnavailable as read_exc:
                    raise LocalSettingsPublishIndeterminate(
                        "model connection metadata publication is indeterminate"
                    ) from read_exc
                published = observed.connection(connection.id)
                if published == connection:
                    return observed
                if published is None:
                    credentials.delete(key)
                    raise LocalSettingsUnavailable(
                        "model connection metadata was not published"
                    ) from exc
                raise LocalSettingsPublishIndeterminate(
                    "model connection ID resolved to different metadata"
                ) from exc
            except BaseException:
                credentials.delete(key)
                raise


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
    "LocalPostgresConfig",
    "LocalSettings",
    "LocalSettingsPublishIndeterminate",
    "LocalSettingsStore",
    "LocalSettingsUnavailable",
    "default_local_settings_path",
    "local_settings_from_dict",
    "local_settings_to_dict",
    "read_local_settings",
    "write_local_settings",
]
