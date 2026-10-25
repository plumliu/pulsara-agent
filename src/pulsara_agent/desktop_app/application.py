"""Internal HTTP/kernel lifecycle composed by the desktop host."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
from importlib import import_module
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Awaitable, Callable

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import (
    ModelCatalogOwner,
    ModelsDevCatalogClient,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import (
    LocalPostgresConfig,
    LocalSettingsStore,
    LocalSettingsUnavailable,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.tool_permission import EffectivePermissionPolicy
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.scheduling.service import ScheduledTaskService


class DesktopApplicationState(StrEnum):
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
    RESET_REQUIRED = "database_reset_required"
    RESETTING = "database_resetting"
    RESTART_REQUIRED = "database_restart_required"
    READY = "ready"


def packaged_static_root() -> Path:
    return Path(__file__).resolve().parent / "static"


class DesktopApplication:
    """Root owner for Kernel, Protocol-v3, renderer bridge, and HTTP."""

    def __init__(
        self,
        *,
        settings: LocalSettingsStore | None = None,
        memory_domain_id: str = "u_local",
        trust_workspace_mcp_config: bool = False,
        permission_policy: EffectivePermissionPolicy,
        active_skill_names: frozenset[str] = frozenset(),
        port: int = 0,
        static_root: Path | None = None,
        catalog: ModelCatalogOwner | None = None,
        model_runtime: ModelRuntime | None = None,
        core: KernelHostCore | None = None,
        open_browser: Callable[[str], Awaitable[None]] | None = None,
        host: dict[str, str] | None = None,
        tls_identity=None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("desktop HTTP port is out of bounds")
        self.settings = settings or LocalSettingsStore()
        self.catalog = catalog or ModelCatalogOwner(ModelsDevCatalogClient())
        self.model_runtime = model_runtime or ModelRuntime.production(
            settings=self.settings,
            catalog=self.catalog,
        )
        self.permission_policy = permission_policy
        self.active_skill_names = active_skill_names
        self.requested_port = port
        self.static_root = (static_root or packaged_static_root()).resolve()
        self.host = host
        self.tls_identity = tls_identity
        self.workspace_directories = None
        if host is not None:
            from pulsara_agent.desktop_app.workspace_directories import WorkspaceDirectories
            self.workspace_directories = WorkspaceDirectories()
        self.core = core or KernelHostCore.production(
            model_runtime=self.model_runtime, open_browser=open_browser,
            workspace_validator=(lambda path: self.workspace_directories.select(str(path))) if self.workspace_directories is not None else None,
        )
        self.sessions = LocalSessionController(
            core=self.core,
            memory_domain_id=memory_domain_id,
            trust_workspace_mcp_config=trust_workspace_mcp_config,
            permission_policy=permission_policy,
            active_skill_names=active_skill_names,
        )
        self.scheduled_tasks = ScheduledTaskService(self.sessions)
        self.core.scheduled_tasks = self.scheduled_tasks
        self.protocol_server: TerminalKernelProtocolServer | None = None
        self.bridge: RendererBridge | None = None
        self.http: LocalHttpServer | None = None
        self.state = DesktopApplicationState.NEW
        self.database_state = DatabaseDataPlaneState.NOT_CONFIGURED
        self._runtime_directory: Path | None = None
        self._start_lock = asyncio.Lock()
        self._database_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._initial_catalog_task: asyncio.Task[None] | None = None
        self._initial_catalog_settled = False
        self._sdk_warmup_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self.state is DesktopApplicationState.READY

    @property
    def model_catalog_loading(self) -> bool:
        return not self._initial_catalog_settled

    @property
    def draining(self) -> bool:
        return self.state in {
            DesktopApplicationState.DRAINING,
            DesktopApplicationState.STOPPED,
            DesktopApplicationState.FAILED,
        }

    @property
    def origin(self) -> str:
        if self.http is None:
            raise RuntimeError("Pulsara local HTTP listener is unavailable")
        return self.http.origin

    async def start(self) -> None:
        async with self._start_lock:
            if self.state is not DesktopApplicationState.NEW:
                raise RuntimeError("Pulsara desktop application already started")
            self.state = DesktopApplicationState.STARTING
            try:
                if not (self.static_root / "index.html").is_file():
                    raise FileNotFoundError(
                        "Pulsara local frontend assets are missing; run "
                        "`cd frontend && npm run build:desktop`."
                    )
                runtime_directory = Path(tempfile.mkdtemp(prefix="pulsara-desktop-"))
                os.chmod(runtime_directory, 0o700)
                self._runtime_directory = runtime_directory
                protocol_server = TerminalKernelProtocolServer(
                    socket_path=runtime_directory / "terminal-v3.sock",
                    session_provider=self.sessions.session_by_host_id,
                )
                await protocol_server.start()
                self.protocol_server = protocol_server
                bridge = RendererBridge(
                    sessions=self.sessions, protocol_server=protocol_server
                )
                self.bridge = bridge
                http = LocalHttpServer(
                    sessions=self.sessions,
                    scheduled_tasks=self.scheduled_tasks,
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
                    reset_postgres=self.reset_postgres,
                    model_catalog_loading=lambda: self.model_catalog_loading,
                    host=self.host,
                    ssl_context=self.tls_identity.context if self.tls_identity is not None else None,
                    workspace_directories=self.workspace_directories,
                )
                await http.start()
                self.http = http
                self._initial_catalog_task = asyncio.create_task(
                    self._refresh_initial_catalog(), name="desktop-initial-model-catalog",
                )
                self._initial_catalog_task.add_done_callback(self._observe_initial_catalog_completion)
                await self.refresh_database_state()
                self.state = DesktopApplicationState.READY
                self._sdk_warmup_task = asyncio.create_task(
                    self._warm_sdk_imports(), name="desktop-sdk-warmup",
                )
            except BaseException:
                self.state = DesktopApplicationState.FAILED
                await self.aclose()
                raise

    @staticmethod
    def _import_mcp_sdk() -> None:
        import_module("pulsara_agent.conversation_kernel.mcp.sdk_facade")
        import_module("pulsara_agent.conversation_kernel.mcp.oauth")
        import_module("pulsara_agent.conversation_kernel.mcp.supervisor")

    async def _warm_sdk_imports(self) -> None:
        # Only normal dependency imports: no clients, connections or model
        # calls. Real operations retain their ordinary imports and errors.
        def warm() -> None:
            for label, load in (("OpenAI", lambda: import_module("openai")), ("MCP", self._import_mcp_sdk)):
                try:
                    load()
                except BaseException as error:
                    logging.getLogger(__name__).warning(
                        "%s SDK background import failed", label, exc_info=error,
                    )

        await asyncio.to_thread(warm)

    async def _close_sdk_warmup(self) -> None:
        # Cancelling a to_thread wrapper would leave its physical import
        # running. The existing shielded application-close owner joins the
        # worker without cancellation before releasing application resources.
        if self._sdk_warmup_task is not None:
            await self._sdk_warmup_task

    async def _refresh_initial_catalog(self) -> None:
        # ModelCatalogOwner and its client retain parsing, network and snapshot
        # ownership. Opening the local shell only observes this initial attempt.
        try:
            await self.catalog.refresh()
        except Exception:
            pass  # A failed initial attempt settles; manual refresh remains available.
        finally:
            self._initial_catalog_settled = True
        async with self._database_lock:
            await self._start_scheduler_if_prepared()

    @staticmethod
    def _observe_initial_catalog_completion(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.getLogger(__name__).error(
                "Initial model catalog startup continuation failed", exc_info=error,
            )

    async def _start_scheduler_if_prepared(self) -> None:
        # Initial models.dev resolution must settle before due tasks can be
        # prepared. Failure still starts scheduling, as the previous path did.
        if (
            self._initial_catalog_settled
            and self.database_state is DatabaseDataPlaneState.READY
            and not self.draining
        ):
            await self.scheduled_tasks.start()

    async def _close_initial_catalog(self) -> None:
        task = self._initial_catalog_task
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def refresh_database_state(self) -> DatabaseDataPlaneState:
        async with self._database_lock:
            return await self._refresh_database_state()

    async def _refresh_database_state(self) -> DatabaseDataPlaneState:
        if self.database_state is DatabaseDataPlaneState.RESTART_REQUIRED:
            return self.database_state
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
            if exc.code is PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED:
                self.database_state = DatabaseDataPlaneState.RESET_REQUIRED
                return self.database_state
            action_codes = {
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
            await self._start_scheduler_if_prepared()
        return self.database_state

    async def reset_postgres(self, postgres: LocalPostgresConfig) -> dict[str, object]:
        """Keep the settings shell alive while resetting the confirmed database."""
        from time import monotonic
        from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner

        runner = PostgresMigrationRunner(
            admin_dsn=postgres.admin_dsn or "", runtime_dsn=postgres.runtime_dsn
        )
        async with self._database_lock:
            restart_required = (
                self.core.has_database_resources
                or self.database_state is DatabaseDataPlaneState.RESTART_REQUIRED
            )
            self.database_state = DatabaseDataPlaneState.RESETTING
            try:
                if restart_required:
                    # Existing close owners settle live effects before any SQL
                    # deletion. A failure leaves the database untouched.
                    if self.bridge is not None:
                        await self.bridge.aclose()
                    if self.protocol_server is not None:
                        await self.protocol_server.close()
                    await self.scheduled_tasks.aclose()
                    await self.sessions.aclose()
                    await self.core.shutdown()
                work = asyncio.create_task(
                    asyncio.to_thread(
                        runner.reset, deadline_monotonic=monotonic() + 300.0
                    )
                )
                # A disconnected HTTP waiter cannot release the maintenance
                # lock while its physical deletion/installation is still running.
                cancelled: asyncio.CancelledError | None = None
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError as exc:
                        cancelled = cancelled or exc
                    except BaseException:
                        break
                report = work.result()
                if cancelled is not None:
                    raise cancelled
                return {**report.to_dict(), "restart_required": restart_required}
            finally:
                self.database_state = (
                    DatabaseDataPlaneState.RESTART_REQUIRED
                    if restart_required
                    else DatabaseDataPlaneState.CONFIGURED_UNVERIFIED
                )
                if not restart_required:
                    await self._refresh_database_state()

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_owner(), name="pulsara-desktop-close"
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
        failed = self.state is DesktopApplicationState.FAILED
        if self.state is DesktopApplicationState.STOPPED:
            return
        if not failed:
            self.state = DesktopApplicationState.DRAINING
        close_error: BaseException | None = None

        async def close(operation) -> None:
            nonlocal close_error
            try:
                await operation()
            except BaseException as exc:
                close_error = close_error or exc

        # Join the remote attempt before closing owners it may wake. Cancel
        # outside the database lock: its completion also uses that lock.
        await close(self._close_initial_catalog)
        await close(self._close_sdk_warmup)
        # HTTP stays bound but its admission middleware now sees DRAINING.
        async with self._database_lock:
            if self.bridge is not None:
                await close(self.bridge.aclose)
                self.bridge = None
            if self.protocol_server is not None:
                await close(self.protocol_server.close)
                self.protocol_server = None
            await close(self.scheduled_tasks.aclose)
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
            DesktopApplicationState.FAILED
            if failed
            else DesktopApplicationState.STOPPED
        )
        if close_error is not None:
            raise close_error


__all__ = [
    "DesktopApplication",
    "DesktopApplicationState",
    "packaged_static_root",
]
