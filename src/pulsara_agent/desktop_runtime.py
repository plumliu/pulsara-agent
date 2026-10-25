"""Private Electron child entry point; existing owners retain all execution semantics.

The parent owns PostgreSQL processes. This module composes its private Unix-socket
endpoint with ordinary local settings and calls the existing baseline/app owners.
stdin/stdout are a dedicated JSON-lines channel; all ordinary output is stderr.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress, redirect_stdout
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
from time import monotonic
from typing import TYPE_CHECKING

from pulsara_agent.capability.pulsara_home import require_pulsara_home

if TYPE_CHECKING:
    from pulsara_agent.settings import LocalPostgresConfig


def diagnostics_path() -> Path:
    return require_pulsara_home() / "desktop" / "diagnostics.sock"


def acquire_kernel_lock(*, wait: bool = False) -> int:
    """OS lifetime lock; a prior child keeps ownership through its close path."""
    import fcntl

    directory = require_pulsara_home() / "desktop"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.stat()
    if directory.resolve() != directory or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("desktop directory must be private to this user")
    descriptor = os.open(directory / "kernel.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def validate_managed_postgres(postgres: LocalPostgresConfig) -> None:
    """Reject any endpoint except the host's dedicated private socket directory."""
    from psycopg.conninfo import conninfo_to_dict

    endpoint = None
    for dsn, role in ((postgres.admin_dsn, "pulsara_admin"),
                      (postgres.runtime_dsn, "pulsara_runtime")):
        fields = conninfo_to_dict(dsn or "")
        if set(fields) - {"host", "port", "dbname", "user"}:
            raise ValueError("desktop PostgreSQL connection contains unsupported options")
        host = Path(fields.get("host", ""))
        private_root = require_pulsara_home() / "desktop"
        if (not host.is_absolute() or not host.is_relative_to(private_root)
                or host.resolve() != host or fields.get("dbname") != "pulsara"
                or fields.get("user") != role):
            raise ValueError("desktop PostgreSQL must use its dedicated private Unix socket")
        for directory in (private_root, host):
            info = directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("desktop PostgreSQL socket directory must be private to this user")
        target = (host, fields.get("port", "5432"))
        if endpoint is not None and endpoint != target:
            raise ValueError("desktop PostgreSQL role endpoints differ")
        endpoint = target


def prepare_database(postgres: LocalPostgresConfig, *, initialize: bool) -> None:
    """Create roles/database only on first initialization; never reset on failure."""
    from psycopg.conninfo import make_conninfo
    from pulsara_agent.storage.postgres_endpoint import ResolvedPostgresConnectionFactory
    from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner

    validate_managed_postgres(postgres)
    # The 300s deadline is the existing schema-management operation boundary.
    deadline = monotonic() + 300.0
    bootstrap = ResolvedPostgresConnectionFactory(
        make_conninfo(postgres.admin_dsn, dbname="postgres"),
        application_name="pulsara-desktop-bootstrap",
    )
    with bootstrap.connect(deadline_monotonic=deadline, autocommit=True) as connection:
        role = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
            "FROM pg_catalog.pg_roles WHERE rolname = 'pulsara_runtime'"
        ).fetchone()
        if role is None:
            if not initialize:
                raise RuntimeError("managed PostgreSQL runtime role is missing")
            connection.execute("CREATE ROLE pulsara_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
        elif tuple(role) != (False, False, False, False, False, True):
            raise RuntimeError("managed PostgreSQL runtime role has unexpected privileges")
        database = connection.execute(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname = 'pulsara'"
        ).fetchone()
        if database is None:
            if not initialize:
                raise RuntimeError("managed Pulsara database is missing")
            connection.execute("CREATE DATABASE pulsara OWNER pulsara_admin")
        elif database[0] != "pulsara_admin":
            raise RuntimeError("managed Pulsara database has an unexpected owner")
    PostgresMigrationRunner(admin_dsn=postgres.admin_dsn or "", runtime_dsn=postgres.runtime_dsn).migrate(
        deadline_monotonic=deadline
    )


def database_diagnostic(postgres: LocalPostgresConfig, *, command: str,
                        deadline_seconds: float, deep: bool = False) -> dict:
    from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
    from pulsara_agent.storage.migrations.runner import _read_identity_from_connection, read_migration_ledger
    from pulsara_agent.storage.migrations.verifier import classify_migration_history
    from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory

    if command not in {"status", "verify"} or not 1 <= deadline_seconds <= 3600:
        raise ValueError("invalid database diagnostic")
    deadline = monotonic() + deadline_seconds
    factory = PostgresRuntimeConnectionFactory(postgres.runtime_dsn)
    if command == "status":
        with factory.connect(deadline_monotonic=deadline, autocommit=False) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                identity = _read_identity_from_connection(connection)
                rows = read_migration_ledger(connection)
        return {
            "status": classify_migration_history(rows).value,
            "database_name": identity.database_name,
            "runtime_role": identity.runtime_role,
            "migration_head_version": rows[-1].version if rows else None,
            "expected_head_version": POSTGRES_MIGRATION_REGISTRY.latest_version,
            "observed_registry_prefix_fingerprint": rows[-1].registry_prefix_fingerprint if rows else None,
            "expected_registry_prefix_fingerprint": POSTGRES_MIGRATION_REGISTRY.registry_fingerprint,
        }
    if deep:
        return factory.verify_deep(deadline_monotonic=deadline).result.to_dict()
    bundle = factory.verify(deadline_monotonic=deadline)
    return {"status": "verified", "database_name": bundle.binding.database_name,
            "runtime_role": bundle.binding.runtime_role,
            "migration_head_version": bundle.binding.migration_head_version,
            "result_fingerprint": bundle.result.result_fingerprint}


def request_database_diagnostic(*, command: str, deadline_seconds: float, deep: bool = False) -> dict:
    """CLI reads the running managed instance; it never starts or connects to PG."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(deadline_seconds + 1)
            client.connect(str(diagnostics_path()))
            client.sendall((json.dumps({"command": command, "deadline_seconds": deadline_seconds, "deep": deep}) + "\n").encode())
            with client.makefile("rb") as stream:
                result = json.loads(stream.readline())
    except (OSError, ValueError) as exc:
        raise RuntimeError("Pulsara desktop database is unavailable; open the desktop application first") from exc
    if result.get("status") == "error":
        raise RuntimeError(result.get("detail", "managed database diagnostic failed"))
    return result


async def inspect_quit(application) -> dict:
    # Read existing owners at request time; no host-side task cache or new authority.
    sessions = await application.sessions.list_sessions()
    scheduled_count = 0
    cursor = None
    while True:
        page = await application.scheduled_tasks.list(cursor=cursor, status="ACTIVE")
        scheduled_count += len(page["tasks"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break
    return {"activeTasks": sum(not item.get("can_archive", False) for item in sessions),
            "scheduledTasks": scheduled_count}


async def run_desktop_child(reader: asyncio.StreamReader, emit, *, application_factory=None,
                            database_preparer=prepare_database) -> int:
    """Serve the finite host control surface and close through the original owner."""
    from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
    from pulsara_agent.tool_permission import preset_to_policy
    from pulsara_agent.desktop_app.application import DesktopApplication, DatabaseDataPlaneState
    from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore

    application = None
    server = None
    commands_task = None
    stop = asyncio.Event()
    host_emit = emit
    channel_closed = False

    def emit(value):
        nonlocal channel_closed
        if channel_closed:
            return
        try:
            host_emit(value)
        except BrokenPipeError:
            # A dead Electron parent closes both pipes. Losing the status pipe
            # must never bypass the original application close/settlement owner.
            channel_closed = True
            stop.set()
    settings = None
    failed = False
    kernel_lock = None
    socket_path = diagnostics_path()
    loop = asyncio.get_running_loop()
    installed = []
    oauth_pending: dict[str, asyncio.Future] = {}
    oauth_request = 0

    def close_oauth_requests():
        for future in tuple(oauth_pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("desktop host control channel closed"))

    async def open_oauth_browser(url: str) -> None:
        nonlocal oauth_request
        if stop.is_set() or channel_closed:
            raise ConnectionError("desktop host is closing")
        oauth_request += 1
        identifier = f"oauth-{oauth_request}"
        future = loop.create_future()
        oauth_pending[identifier] = future
        try:
            emit({"type": "open-oauth-browser", "requestId": identifier, "url": url})
            if stop.is_set() or channel_closed:
                raise ConnectionError("desktop host control channel closed")
            if not await future:
                raise RuntimeError("desktop host could not open the OAuth browser")
        finally:
            oauth_pending.pop(identifier, None)

    def request_stop():
        close_oauth_requests()
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, request_stop)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass

    def error_text(exc):
        text = f"{type(exc).__name__}: {exc}"
        if settings is not None:
            with suppress(Exception):
                return settings.read().credential_scrub_set().scrub_text(text)
        return text

    async def commands():
        while not stop.is_set():
            line = await reader.readline()
            if not line:
                close_oauth_requests()
                stop.set()
                return
            if not line.endswith(b"\n"):
                raise ValueError("incomplete desktop host command")
            request = json.loads(line)
            if request == {"type": "shutdown"}:
                request_stop()
            elif (set(request) == {"type", "requestId"}
                  and request["type"] == "inspect-quit" and isinstance(request["requestId"], str)):
                try:
                    reply = await inspect_quit(application)
                    emit({"type": "quit-state", "requestId": request["requestId"], **reply})
                except Exception as exc:
                    emit({"type": "quit-state", "requestId": request["requestId"], "error": error_text(exc)})
            elif (isinstance(request, dict) and set(request) == {"type", "requestId", "opened"}
                  and request["type"] == "open-oauth-browser-result"
                  and isinstance(request["requestId"], str) and type(request["opened"]) is bool):
                pending = oauth_pending.get(request["requestId"])
                if pending is not None and not pending.done():
                    pending.set_result(request["opened"])
            else:
                raise ValueError("unsupported desktop host command")

    async def diagnostic(reader, writer):
        try:
            request = json.loads(await reader.readline())
            if not isinstance(request, dict) or set(request) != {"command", "deadline_seconds", "deep"}:
                raise ValueError("invalid diagnostic request")
            if type(request["deep"]) is not bool or type(request["deadline_seconds"]) not in {int, float}:
                raise ValueError("invalid diagnostic fields")
            result = await asyncio.to_thread(database_diagnostic, settings.read().postgres, **request)
        except Exception as exc:
            result = {"status": "error", "detail": error_text(exc)}
        try:
            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        line = await reader.readline()
        if not line:
            return 0
        if not line.endswith(b"\n"):
            raise ValueError("incomplete desktop startup frame")
        request = json.loads(line)
        if (not isinstance(request, dict) or set(request) not in ({"type", "postgres", "initialize"}, {"type", "postgres", "initialize", "host"})
                or request["type"] != "start" or type(request["initialize"]) is not bool
                or not isinstance(request["postgres"], dict)
                or set(request["postgres"]) != {"runtime_dsn", "admin_dsn"}):
            raise ValueError("invalid desktop startup frame")
        host = request.get("host")
        if host is not None:
            if (not isinstance(host, dict) or set(host) != {"platform", "executionDomain", "distribution", "user", "interopHelper"}
                    or host["platform"] != "win32" or host["executionDomain"] != "wsl"
                    or not all(isinstance(value, str) and value and not any(ord(character) < 32 or ord(character) == 127 for character in value) for value in host.values())
                    or not Path(host["interopHelper"]).is_absolute()):
                raise ValueError("invalid WSL desktop host profile")
            from pulsara_agent.windows_interop import configure_interop
            configure_interop(helper_path=host["interopHelper"], distribution=host["distribution"], user=host["user"])
        kernel_lock = acquire_kernel_lock()
        postgres = LocalPostgresConfig(**request["postgres"])
        settings = LocalSettingsStore(postgres=postgres)
        # Read now so a malformed user document cannot be replaced by initialization.
        settings.read()
        emit({"type": "starting"})
        if stop.is_set():
            return 0
        commands_task = asyncio.create_task(commands(), name="desktop-host-control")
        commands_task.add_done_callback(lambda _task: request_stop())
        await asyncio.to_thread(database_preparer, postgres, initialize=request["initialize"])
        if stop.is_set():
            if commands_task.done():
                commands_task.result()
            return 0
        factory = application_factory or DesktopApplication
        options = {}
        tls_identity = None
        if host is not None:
            from pulsara_agent.desktop_app.tls_identity import create_desktop_tls_identity
            tls_identity = create_desktop_tls_identity(require_pulsara_home() / "desktop")
            options = {"open_browser": open_oauth_browser, "host": host, "tls_identity": tls_identity}
        application = factory(settings=settings, permission_policy=preset_to_policy(DEFAULT_PERMISSION_MODE), **options)
        await application.start()
        if application.database_state != DatabaseDataPlaneState.READY:
            raise RuntimeError(f"managed database is not ready: {application.database_state}")
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Electron's single-instance lock owns this path; stale socket only.
        if socket_path.exists() or socket_path.is_symlink():
            if not stat.S_ISSOCK(socket_path.lstat().st_mode):
                raise RuntimeError("desktop diagnostic path is not a socket")
            socket_path.unlink()
        server = await asyncio.start_unix_server(diagnostic, path=socket_path)
        os.chmod(socket_path, 0o600)
        if not stop.is_set():
            reply = {"type": "ready", "origin": application.origin, "database_state": application.database_state.value}
            if tls_identity is not None:
                reply["certificatePem"] = tls_identity.certificate_pem
            emit(reply)
        await stop.wait()
        if commands_task.done():
            commands_task.result()
    except Exception as exc:
        failed = True
        emit({"type": "failed", "error": error_text(exc)})
    finally:
        emit({"type": "stopping"})
        if server is not None:
            server.close()
            await server.wait_closed()
            socket_path.unlink(missing_ok=True)
        if commands_task is not None:
            commands_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await commands_task
        close_oauth_requests()
        if application is not None:
            try:
                await application.aclose()
            except Exception as exc:
                failed = True
                emit({"type": "failed", "error": error_text(exc)})
        for signum in installed:
            loop.remove_signal_handler(signum)
        if kernel_lock is not None:
            os.close(kernel_lock)
        emit({"type": "stopped"})
    return 1 if failed else 0


def main() -> None:
    if sys.argv[1:] == ["--wait-owner"]:
        os.close(acquire_kernel_lock(wait=True))
        return
    if sys.argv[1:]:
        raise SystemExit("unsupported desktop child arguments")
    protocol_output = sys.stdout

    def emit(value):
        protocol_output.write(json.dumps(value, ensure_ascii=False) + "\n")
        protocol_output.flush()

    async def run():
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            return await run_desktop_child(reader, emit)
        finally:
            transport.close()

    with redirect_stdout(sys.stderr):
        raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
