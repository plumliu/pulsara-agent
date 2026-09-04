"""Single-launcher lifecycle for the local Pulsara Web application."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
import os
from pathlib import Path
import shutil
import signal
import tempfile
from typing import Callable
import webbrowser

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import (
    ModelCatalogOwner,
    ModelsDevCatalogClient,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalSettingsStore, LocalSettingsUnavailable
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.tool_permission import EffectivePermissionPolicy
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.http_server import LocalHttpServer
from pulsara_agent.web_app.session_controller import LocalSessionController
from pulsara_agent.workspace_identity import HostWorkspaceInput


class LocalWebApplicationState(StrEnum):
    NEW = "NEW"
    STARTING = "STARTING"
    READY = "READY"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class DatabaseDataPlaneState(StrEnum):
    NOT_CONFIGURED = "database_not_configured"
    CONFIGURED_UNVERIFIED = "database_configured_unverified"
    UNAVAILABLE = "database_unavailable"
    SCHEMA_ACTION_REQUIRED = "database_schema_action_required"
    READY = "ready"


def packaged_static_root() -> Path:
    return Path(__file__).resolve().parent / "static"


class LocalWebApplication:
    """Root owner for Kernel, Protocol-v3, browser bridge, and HTTP."""

    def __init__(
        self,
        *,
        settings: LocalSettingsStore | None = None,
        workspace_input: HostWorkspaceInput,
        permission_policy: EffectivePermissionPolicy,
        active_skill_names: frozenset[str] = frozenset(),
        port: int = 0,
        static_root: Path | None = None,
        catalog: ModelCatalogOwner | None = None,
        model_runtime: ModelRuntime | None = None,
        core: KernelHostCore | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("local Web port is out of bounds")
        self.settings = settings or LocalSettingsStore()
        self.catalog = catalog or ModelCatalogOwner(ModelsDevCatalogClient())
        self.model_runtime = model_runtime or ModelRuntime.production(
            settings=self.settings,
            catalog=self.catalog,
        )
        self.workspace_input = workspace_input
        self.permission_policy = permission_policy
        self.active_skill_names = active_skill_names
        self.requested_port = port
        self.static_root = (static_root or packaged_static_root()).resolve()
        self.core = core or KernelHostCore.production(model_runtime=self.model_runtime)
        self.sessions = LocalSessionController(
            core=self.core,
            workspace_input=workspace_input,
            permission_policy=permission_policy,
            active_skill_names=active_skill_names,
        )
        self.protocol_server: TerminalKernelProtocolServer | None = None
        self.bridge: LocalBrowserBridge | None = None
        self.http: LocalHttpServer | None = None
        self.state = LocalWebApplicationState.NEW
        self.database_state = DatabaseDataPlaneState.NOT_CONFIGURED
        self._runtime_directory: Path | None = None
        self._start_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self.state is LocalWebApplicationState.READY

    @property
    def draining(self) -> bool:
        return self.state in {
            LocalWebApplicationState.DRAINING,
            LocalWebApplicationState.STOPPED,
            LocalWebApplicationState.FAILED,
        }

    @property
    def origin(self) -> str:
        if self.http is None:
            raise RuntimeError("Pulsara local HTTP listener is unavailable")
        return self.http.origin

    async def start(self) -> None:
        async with self._start_lock:
            if self.state is not LocalWebApplicationState.NEW:
                raise RuntimeError("Pulsara local Web application already started")
            self.state = LocalWebApplicationState.STARTING
            try:
                if not (self.static_root / "index.html").is_file():
                    raise FileNotFoundError(
                        "Pulsara local frontend assets are missing; run "
                        "`cd frontend && npm run build:local`."
                    )
                runtime_directory = Path(tempfile.mkdtemp(prefix="pulsara-local-web-"))
                os.chmod(runtime_directory, 0o700)
                self._runtime_directory = runtime_directory
                protocol_server = TerminalKernelProtocolServer(
                    socket_path=runtime_directory / "terminal-v3.sock",
                    session_provider=self.sessions.session_by_host_id,
                )
                await protocol_server.start()
                self.protocol_server = protocol_server
                bridge = LocalBrowserBridge(
                    sessions=self.sessions, protocol_server=protocol_server
                )
                self.bridge = bridge
                http = LocalHttpServer(
                    sessions=self.sessions,
                    bridge=bridge,
                    static_root=self.static_root,
                    requested_port=self.requested_port,
                    is_ready=lambda: self.ready,
                    is_draining=lambda: self.draining,
                    settings=self.settings,
                    catalog=self.catalog,
                    model_runtime=self.model_runtime,
                    database_state=lambda: self.database_state.value,
                    refresh_database_state=self.refresh_database_state,
                    postgres_settings_saved=self.postgres_settings_saved,
                )
                await http.start()
                self.http = http
                self.state = LocalWebApplicationState.READY
                try:
                    await self.catalog.refresh()
                except Exception:
                    pass
                await self.refresh_database_state()
            except BaseException:
                self.state = LocalWebApplicationState.FAILED
                await self.aclose()
                raise

    async def refresh_database_state(self) -> DatabaseDataPlaneState:
        try:
            configured = self.settings.read().postgres
        except LocalSettingsUnavailable:
            self.database_state = DatabaseDataPlaneState.NOT_CONFIGURED
            return self.database_state
        if configured is None:
            self.database_state = DatabaseDataPlaneState.NOT_CONFIGURED
            return self.database_state
        try:
            await self.sessions.prepare()
        except PostgresSchemaError as exc:
            action_codes = {
                PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED,
                PostgresSchemaFailureCode.UNMANAGED_DATABASE,
                PostgresSchemaFailureCode.CATALOG_DRIFT,
                PostgresSchemaFailureCode.EXTENSION_MISSING,
                PostgresSchemaFailureCode.EXTENSION_TOO_OLD,
                PostgresSchemaFailureCode.PRIVILEGE_MISSING,
            }
            self.database_state = (
                DatabaseDataPlaneState.SCHEMA_ACTION_REQUIRED
                if exc.code in action_codes
                else DatabaseDataPlaneState.UNAVAILABLE
            )
        except Exception:
            self.database_state = DatabaseDataPlaneState.UNAVAILABLE
        else:
            self.database_state = DatabaseDataPlaneState.READY
        return self.database_state

    def postgres_settings_saved(self) -> DatabaseDataPlaneState:
        """Observe a new DSN without connecting or replacing a live data plane."""

        if self.database_state is not DatabaseDataPlaneState.READY:
            self.database_state = DatabaseDataPlaneState.CONFIGURED_UNVERIFIED
        return self.database_state

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_owner(), name="pulsara-local-web-close"
            )
            self._close_task = task
        waiter_cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                waiter_cancelled = waiter_cancelled or exc
                continue
            except BaseException:
                break
        if task.cancelled():
            raise asyncio.CancelledError
        task.result()
        if waiter_cancelled is not None:
            raise waiter_cancelled

    async def _close_owner(self) -> None:
        failed = self.state is LocalWebApplicationState.FAILED
        if self.state is LocalWebApplicationState.STOPPED:
            return
        if not failed:
            self.state = LocalWebApplicationState.DRAINING
        close_error: BaseException | None = None

        async def close(operation) -> None:
            nonlocal close_error
            try:
                await operation()
            except BaseException as exc:
                close_error = close_error or exc

        # HTTP stays bound but its admission middleware now sees DRAINING.
        if self.bridge is not None:
            await close(self.bridge.aclose)
            self.bridge = None
        if self.protocol_server is not None:
            await close(self.protocol_server.close)
            self.protocol_server = None
        await close(self.sessions.aclose)
        await close(self.core.shutdown)
        if self.http is not None:
            await close(self.http.aclose)
            self.http = None
        runtime_directory = self._runtime_directory
        self._runtime_directory = None
        if runtime_directory is not None:
            with suppress(FileNotFoundError):
                shutil.rmtree(runtime_directory)
        self.state = (
            LocalWebApplicationState.FAILED
            if failed
            else LocalWebApplicationState.STOPPED
        )
        if close_error is not None:
            raise close_error


async def run_local_web_application(
    application: LocalWebApplication,
    *,
    open_browser: bool,
    output: Callable[[str], None] = print,
    force_exit: Callable[[int], None] = os._exit,
) -> None:
    """Run one application until the first signal; a repeated signal forces."""

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    first_exit_code = 0
    signal_count = 0

    def interrupt(code: int) -> None:
        nonlocal first_exit_code, signal_count
        signal_count += 1
        if signal_count == 1:
            first_exit_code = code
            stop.set()
            return
        force_exit(code)

    installed: list[signal.Signals] = []
    for current, code in ((signal.SIGTERM, 0), (signal.SIGINT, 130)):
        try:
            loop.add_signal_handler(current, interrupt, code)
            installed.append(current)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await application.start()
        if not stop.is_set():
            output(f"Pulsara is ready at {application.origin}")
            output("No account login is required. Press Ctrl-C to stop.")
            if open_browser:
                await asyncio.to_thread(webbrowser.open, application.origin)
        await stop.wait()
    finally:
        await application.aclose()
        for current in installed:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(current)
    if first_exit_code:
        raise SystemExit(first_exit_code)


__all__ = [
    "LocalWebApplication",
    "LocalWebApplicationState",
    "packaged_static_root",
    "run_local_web_application",
]
