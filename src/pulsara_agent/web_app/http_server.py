"""Loopback-only HTTP surface for Pulsara Web."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable, cast
from uuid import UUID

from aiohttp import web
from pulsara_agent.web_app.memory_controller import LocalMemoryController
from pulsara_agent.conversation_kernel.memory.management import MemoryManagementError

from pulsara_agent.conversation_kernel.host import KernelHostCoreClosing
from pulsara_agent.llm.model_catalog import (
    ModelCatalogEntry,
    ModelCatalogInvalid,
    ModelCatalogOwner,
    ModelCatalogUnavailable,
    ModelTargetKey,
    ReasoningEffortChoices,
    ReasoningFixedOn,
    ReasoningProviderDefault,
    ReasoningSelectableControls,
    ReasoningToggle,
    ReasoningUnavailable,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ModelConnectionAuthentication,
    ModelConnectionConfig,
    ModelConnectionId,
    UserDeclaredModelTarget,
    model_call_binding_from_dict,
    model_connection_to_dict,
    reasoning_selection_to_dict,
)
from pulsara_agent.llm.model_target import (
    ResolvedModelConnection,
    controls_supported_by_adapter,
    create_model_connection,
    create_user_declared_model_connection,
    default_reasoning_selection,
    resolve_model_target_contract,
)
from pulsara_agent.llm.connection_probe import (
    ModelConnectionProbeFailure,
    probe_model_connection,
)
from pulsara_agent.llm.runtime import ModelRuntime, ModelRuntimeUnavailable
from pulsara_agent.mcp_config import (
    McpConfiguredServerBoundExceeded,
)
from pulsara_agent.capability.mcp_management import (
    McpManagementConflict,
    McpSecretMutation,
)
from pulsara_agent.capability.local_skill_removal import LocalSkillRemovalIdentity
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.protocol_client import ProtocolBridgeError
from pulsara_agent.web_app.session_controller import (
    LocalSessionController,
    SessionWorkspaceKind,
)
from pulsara_agent.settings import (
    DashScopeCredentialKind,
    LocalPostgresConfig,
    LocalSettings,
    LocalSettingsPublishIndeterminate,
    LocalSettingsStore,
    LocalSettingsUnavailable,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)


LOOPBACK_HOST = "127.0.0.1"


def _reasoning_payload(value) -> dict[str, object]:
    if isinstance(value, ReasoningSelectableControls):
        return {
            "kind": "selectable",
            "effort": (
                None if value.effort is None else {"values": list(value.effort.values)}
            ),
            "toggle": value.toggle is not None,
            "budget_tokens": (
                None
                if value.budget is None
                else {
                    "minimum": value.budget.minimum_tokens,
                    "maximum": value.budget.maximum_tokens,
                }
            ),
        }
    if isinstance(value, ReasoningFixedOn):
        return {"kind": "fixed_on"}
    if isinstance(value, ReasoningUnavailable):
        return {"kind": "unavailable"}
    if isinstance(value, ReasoningProviderDefault):
        return {"kind": "provider_default"}
    raise TypeError(type(value).__name__)


def _user_declared_reasoning(value: object):
    if value == {"kind": "provider_default"}:
        return ReasoningProviderDefault()
    if value == {"kind": "toggle"}:
        return ReasoningSelectableControls(toggle=ReasoningToggle())
    if isinstance(value, dict) and set(value) == {"kind", "values"}:
        if value["kind"] != "effort":
            raise ValueError("custom reasoning kind is invalid")
        values = value["values"]
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item and item == item.strip() for item in values
        ):
            raise ValueError("custom reasoning efforts are invalid")
        return ReasoningSelectableControls(effort=ReasoningEffortChoices(tuple(values)))
    raise ValueError("custom reasoning has an invalid closed shape")


def _catalog_entry_payload(
    entry: ModelCatalogEntry, *, model_runtime: ModelRuntime
) -> dict[str, object]:
    wires: list[dict[str, object]] = []
    for wire_api in WireApi:
        if not model_runtime.route_wires.supports(entry, wire_api):
            wires.append(
                {
                    "wire_api": wire_api.value,
                    "executable": False,
                    "reason": "route_wire_adapter_unavailable",
                    "endpoint": None,
                }
            )
            continue
        route_wire = model_runtime.route_wires.contract_for(entry, wire_api)
        endpoint = model_runtime.route_wires.endpoint_for(entry)
        executable = entry.limits is not None and endpoint is not None
        wires.append(
            {
                "wire_api": wire_api.value,
                "executable": executable,
                "endpoint": endpoint,
                "reason": (
                    None
                    if executable
                    else (
                        "model_hard_limits_unavailable"
                        if entry.limits is None
                        else "model_endpoint_unknown"
                    )
                ),
                "reasoning": _reasoning_payload(
                    controls_supported_by_adapter(entry.reasoning, route_wire)
                ),
                "recommended": (
                    (
                        entry.wire_shape_hint == "responses"
                        and wire_api is WireApi.OPENAI_RESPONSES
                    )
                    or (
                        entry.wire_shape_hint == "completions"
                        and wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
                    )
                ),
            }
        )
    return {
        "model_id": entry.key.model_id,
        "display_name": entry.display_name,
        "wire_dialect": entry.wire_dialect.value,
        "context_tokens": entry.total_context_tokens,
        "input_tokens": None if entry.limits is None else entry.limits.max_input_tokens,
        "output_tokens": None
        if entry.limits is None
        else entry.limits.max_output_tokens,
        "tool_call": entry.tool_call,
        "wire_shape_hint": entry.wire_shape_hint,
        "wire_apis": wires,
    }


def _wire_shape_warning(hint: str | None, wire_api: WireApi) -> bool:
    if hint is None:
        return False
    expected = (
        WireApi.OPENAI_RESPONSES
        if hint == "responses"
        else WireApi.OPENAI_CHAT_COMPLETIONS
    )
    return wire_api is not expected


def _dashscope_credential_kind(kind: str) -> DashScopeCredentialKind:
    if kind in {"embedding", "rerank"}:
        return cast(DashScopeCredentialKind, kind)
    raise ValueError("unknown DashScope credential kind")


def _plugin_import_strings(body, key):
    value = body.get(key, {})
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise ValueError("插件普通输入与分类需要文字值")
    return tuple(sorted(value.items()))


def _optional_body_string(body: dict[str, object], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _mcp_import_source(body: dict[str, object]) -> dict[str, object]:
    content = _optional_body_string(body, "content")
    shape = body.get("shape", "auto")
    if content is None or shape not in {
        "auto",
        "mcpServers",
        "opencode",
        "map",
        "server",
    }:
        raise ValueError("MCP import requires content and a supported source shape")
    return {
        "content": content,
        "shape": shape,
        "server_id": _optional_body_string(body, "server_id"),
    }


def _mcp_import_values(body: dict[str, object], key: str) -> dict[str, str]:
    value = body.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(item, str) for item in value.values()
    ):
        raise ValueError("MCP import fields must be a string mapping")
    return value


def _retain_credentials_confirmed(body: dict[str, object]) -> bool:
    value = body.get("retain_credentials_confirmed", False)
    if not isinstance(value, bool):
        raise ValueError("MCP retained credential confirmation must be boolean")
    return value


def _mcp_secret_changes(body: dict[str, object]) -> tuple[McpSecretMutation, ...]:
    from pulsara_agent.capability.management_form import parse_mcp_secret_changes

    return parse_mcp_secret_changes(body.get("secret_changes", []))


class HttpPublicError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.public_message = message
        self.status = status
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


class LocalHttpServer:
    """Same-origin HTTP owner with an explicit ready/stop boundary."""

    def __init__(
        self,
        *,
        sessions: LocalSessionController,
        bridge: LocalBrowserBridge,
        static_root: Path,
        requested_port: int,
        is_ready: Callable[[], bool],
        is_draining: Callable[[], bool],
        settings: LocalSettingsStore,
        catalog: ModelCatalogOwner,
        model_runtime: ModelRuntime,
        database_state: Callable[[], str],
        refresh_database_state: Callable[[], Awaitable[object]],
        postgres_settings_saved: Callable[[], object],
    ) -> None:
        self.sessions = sessions
        self.bridge = bridge
        self.static_root = static_root.resolve()
        self.requested_port = requested_port
        self._is_ready = is_ready
        self._is_draining = is_draining
        self.settings = settings
        self.catalog = catalog
        self.model_runtime = model_runtime
        self._database_state = database_state
        self._refresh_database_state = refresh_database_state
        self._postgres_settings_saved = postgres_settings_saved
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._closed = False
        self._app = web.Application(
            client_max_size=8 << 20,
            middlewares=[self._errors, self._security],
        )
        self._install_routes()

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("local HTTP server has not started")
        return self._port

    @property
    def origin(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}"

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("local HTTP server already started")
        if not (self.static_root / "index.html").is_file():
            raise FileNotFoundError(
                f"Pulsara local frontend is not built: {self.static_root / 'index.html'}"
            )
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        self._runner = runner
        try:
            site = web.TCPSite(
                runner,
                host=LOOPBACK_HOST,
                port=self.requested_port,
                shutdown_timeout=0.0,
            )
            await site.start()
            self._site = site
            sockets = getattr(site._server, "sockets", None)
            if not sockets:
                raise RuntimeError("local HTTP listener has no bound socket")
            self._port = int(sockets[0].getsockname()[1])
        except BaseException:
            await runner.cleanup()
            self._runner = None
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        runner = self._runner
        self._runner = None
        self._site = None
        if runner is not None:
            await runner.cleanup()

    def _install_routes(self) -> None:
        memory = LocalMemoryController(self.sessions)
        self._app.router.add_get("/api/memories/projects", memory.projects)
        self._app.router.add_get("/api/memories", memory.catalog)
        self._app.router.add_get("/api/memories/{fact_id}", memory.detail)
        self._app.router.add_post(
            "/api/memories/{fact_id}/deletion-preview", memory.deletion
        )
        self._app.router.add_delete("/api/memories/{fact_id}", memory.deletion)
        self._app.router.add_get("/healthz", self._health)
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/og.png", self._public_file)
        self._app.router.add_get("/assets/{tail:.*}", self._public_file)
        self._app.router.add_get("/api/app/bootstrap", self._bootstrap)
        self._app.router.add_get("/api/model-catalog", self._model_catalog)
        self._app.router.add_post(
            "/api/model-catalog/refresh", self._refresh_model_catalog
        )
        self._app.router.add_get(
            "/api/model-configurations", self._model_configurations
        )
        self._app.router.add_post(
            "/api/model-configurations", self._add_model_configuration
        )
        self._app.router.add_delete(
            "/api/model-configurations/{connection_id}",
            self._delete_model_configuration,
        )
        self._app.router.add_post(
            "/api/model-configurations/test", self._test_model_configuration
        )
        self._app.router.add_get("/api/local-settings", self._local_settings)
        self._app.router.add_put(
            "/api/local-settings/postgres", self._save_postgres_settings
        )
        self._app.router.add_post(
            "/api/local-settings/postgres/check", self._check_postgres
        )
        self._app.router.add_post(
            "/api/local-settings/postgres/migrate", self._migrate_postgres
        )
        self._app.router.add_put(
            "/api/local-settings/dashscope-credentials/{kind}",
            self._put_dashscope_credential,
        )
        self._app.router.add_delete(
            "/api/local-settings/dashscope-credentials/{kind}",
            self._delete_dashscope_credential,
        )
        self._app.router.add_get("/api/capabilities", self._inspect_user_capabilities)
        self._app.router.add_post(
            "/api/capabilities/refresh", self._refresh_user_capabilities
        )
        self._app.router.add_post(
            "/api/capabilities/roots/{root}/open", self._open_capability_root
        )
        self._app.router.add_post(
            "/api/capabilities/skills/install", self._install_user_skill
        )
        self._app.router.add_post(
            "/api/capabilities/skills/preview", self._preview_skill_import
        )
        self._app.router.add_post(
            "/api/capabilities/skills/enabled", self._set_user_skill_enabled
        )
        self._app.router.add_post(
            "/api/capabilities/skills/remove", self._remove_skill
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/capabilities/skills/remove", self._remove_skill
        )
        self._app.router.add_post("/api/capabilities/mcp", self._create_user_mcp_server)
        self._app.router.add_post(
            "/api/capabilities/mcp/test", self._test_mcp_server
        )
        self._app.router.add_post(
            "/api/capabilities/mcp/import/preview", self._preview_mcp_import
        )
        self._app.router.add_post("/api/capabilities/mcp/import", self._import_mcp)
        self._app.router.add_put(
            "/api/capabilities/mcp/{server_id}", self._update_user_mcp_server
        )
        self._app.router.add_delete(
            "/api/capabilities/mcp/{server_id}", self._remove_user_mcp_server
        )
        self._app.router.add_post(
            "/api/capabilities/mcp/{server_id}/authorize", self._authorize_mcp
        )
        self._app.router.add_get(
            "/api/capabilities/mcp/{server_id}/authorization",
            self._mcp_authorization,
        )
        self._app.router.add_delete(
            "/api/capabilities/mcp/{server_id}/authorization",
            self._mcp_authorization,
        )
        self._app.router.add_post(
            "/api/capabilities/mcp/{server_id}/authorization/cancel",
            self._mcp_authorization,
        )
        self._app.router.add_post(
            "/api/capabilities/plugins/install", self._install_user_plugin
        )
        self._app.router.add_post("/api/capabilities/plugins/preview-import", self._preview_plugin_import)
        self._app.router.add_post(
            "/api/capabilities/plugins/{plugin_id}/enabled",
            self._set_user_plugin_enabled,
        )
        self._app.router.add_delete(
            "/api/capabilities/plugins/{plugin_id}", self._remove_user_plugin
        )
        self._app.router.add_put(
            "/api/capabilities/plugins/{plugin_id}/mcp/{server_id}", self._replace_user_plugin_connection
        )
        self._app.router.add_post(
            "/api/capabilities/plugins/{plugin_id}/mcp/{server_id}/authorization", self._plugin_mcp_authorization
        )
        self._app.router.add_get("/api/sessions", self._list_sessions)
        self._app.router.add_get(
            "/api/sessions/{session_id}/tasks", self._list_session_tasks
        )
        self._app.router.add_get(
            "/api/sessions/{session_id}/capabilities",
            self._inspect_session_capabilities,
        )
        self._app.router.add_post("/api/sessions", self._create_session)
        self._app.router.add_get("/api/sessions/{session_id}", self._read_session)
        self._app.router.add_post("/api/sessions/{session_id}/fork", self._fork_conversation)
        self._app.router.add_put(
            "/api/sessions/{session_id}/model-call-binding",
            self._update_model_call_binding,
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/capabilities/mcp/{server_id}/reconnect",
            self._reconnect_session_mcp,
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/capabilities/skills/install",
            self._install_session_skill,
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/capabilities/skills/enabled",
            self._set_session_skill_enabled,
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/capabilities/mcp",
            self._create_session_mcp_server,
        )
        self._app.router.add_post("/api/sessions/{session_id}/capabilities/mcp/import", self._import_mcp)
        self._app.router.add_post("/api/sessions/{session_id}/capabilities/mcp/test", self._test_mcp_server)
        self._app.router.add_post("/api/sessions/{session_id}/capabilities/mcp/{server_id}/authorize", self._authorize_mcp)
        self._app.router.add_get("/api/sessions/{session_id}/capabilities/mcp/{server_id}/authorization", self._mcp_authorization)
        self._app.router.add_delete("/api/sessions/{session_id}/capabilities/mcp/{server_id}/authorization", self._mcp_authorization)
        self._app.router.add_post("/api/sessions/{session_id}/capabilities/mcp/{server_id}/authorization/cancel", self._mcp_authorization)
        self._app.router.add_put(
            "/api/sessions/{session_id}/capabilities/mcp/{server_id}", self._update_session_mcp_server
        )
        self._app.router.add_post(
            "/api/sessions/{session_id}/capabilities/mcp/{server_id}/enabled",
            self._set_session_mcp_enabled,
        )
        self._app.router.add_delete(
            "/api/sessions/{session_id}/capabilities/mcp/{server_id}",
            self._remove_session_mcp_server,
        )
        self._app.router.add_delete("/api/sessions/{session_id}", self._close_session)
        self._app.router.add_post(
            "/api/sessions/{session_id}/connections", self._connect
        )
        self._app.router.add_delete(
            "/api/connections/{connection_id}", self._disconnect
        )
        for operation in (
            "snapshot",
            "history",
            "observe",
            "command",
            "query-command",
            "live-control-snapshot",
            "resolve-interaction",
            "read-capability-form",
            "resolve-capability-form",
            "resolve-plan-interaction",
            "read-plan-question",
            "read-plan-draft",
            "read-content",
        ):
            self._app.router.add_post(
                f"/api/connections/{{connection_id}}/{operation}",
                self._protocol_handler(operation),
            )

    @web.middleware
    async def _errors(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except HttpPublicError as exc:
            return self._error_response(
                exc.code,
                exc.public_message,
                status=exc.status,
                retryable=exc.retryable,
            )
        except ProtocolBridgeError as exc:
            return self._error_response(
                exc.code, exc.public_message, status=409, retryable=True
            )
        except McpManagementConflict:
            if "session_id" in request.match_info:
                return self._error_response(
                    "PROJECT_CAPABILITY_STALE",
                    "项目能力已经变化，请刷新后再修改。",
                    status=409,
                    retryable=True,
                )
            return web.json_response(
                {
                    "error": {
                        "code": "MCP_CONFIG_CHANGED",
                        "message": "连接已发生变化，请刷新后再修改。",
                        "retryable": True,
                    },
                    "capabilities": await self.sessions.inspect_user_capabilities(),
                },
                status=409,
            )
        except McpConfiguredServerBoundExceeded:
            return self._error_response(
                "PROJECT_MCP_CAPACITY_REACHED",
                "当前能力组合已无法再添加 MCP，请先移除一个不再使用的连接。",
                status=409,
                retryable=False,
            )
        except MemoryManagementError as exc:
            return self._error_response(
                exc.code,
                str(exc),
                status=exc.status,
                retryable=exc.status in {409, 504},
            )
        except LocalSettingsUnavailable:
            return self._error_response(
                "local_settings_unavailable",
                "本机设置文件无法读取；请在设置页保存新配置以修复。",
                status=409,
                retryable=False,
            )
        except LocalSettingsPublishIndeterminate:
            return self._error_response(
                "local_settings_publication_indeterminate",
                "无法确认本机设置是否已经保存；请重新读取设置后再决定是否重试。",
                status=503,
                retryable=True,
            )
        except ModelCatalogUnavailable:
            return self._error_response(
                "model_catalog_unavailable",
                "models.dev 模型目录当前不可用，请稍后重试。",
                status=503,
                retryable=True,
            )
        except ModelCatalogInvalid:
            return self._error_response(
                "model_catalog_invalid",
                "models.dev 模型目录返回了无法识别的内容。",
                status=502,
                retryable=True,
            )
        except ModelRuntimeUnavailable:
            return self._error_response(
                "model_configuration_unavailable",
                "模型配置当前不可用，请检查设置或刷新模型目录。",
                status=409,
                retryable=True,
            )
        except PostgresSchemaError as exc:
            if exc.code in {
                PostgresSchemaFailureCode.CONNECTION_FAILED,
                PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            }:
                return self._error_response(
                    "DATABASE_CONNECTION_FAILED",
                    "无法连接已保存的 PostgreSQL，请检查地址、账号与本机服务。",
                    status=409,
                    retryable=exc.retryable,
                )
            if exc.code in {
                PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED,
                PostgresSchemaFailureCode.UNMANAGED_DATABASE,
                PostgresSchemaFailureCode.CATALOG_DRIFT,
                PostgresSchemaFailureCode.EXTENSION_MISSING,
                PostgresSchemaFailureCode.EXTENSION_TOO_OLD,
                PostgresSchemaFailureCode.PRIVILEGE_MISSING,
            }:
                return self._error_response(
                    "DATABASE_SCHEMA_ACTION_REQUIRED",
                    "数据库尚未完成 Pulsara 初始化，或需要升级后才能使用。",
                    status=409,
                    retryable=False,
                )
            return self._error_response(
                "DATABASE_OPERATION_FAILED",
                "PostgreSQL 检查或初始化没有完成，请核对连接后重试。",
                status=409,
                retryable=exc.retryable,
            )
        except KeyError:
            return self._error_response(
                "LOCAL_RESOURCE_NOT_FOUND",
                "请求的本地资源已失效。",
                status=404,
                retryable=True,
            )
        except (ValueError, TypeError):
            return self._error_response(
                "BAD_REQUEST", "请求内容不正确。", status=400, retryable=False
            )
        except KernelHostCoreClosing:
            return self._error_response(
                "APPLICATION_DRAINING",
                "Pulsara 正在关闭。",
                status=503,
                retryable=True,
            )
        except RuntimeError:
            return self._error_response(
                "LOCAL_RUNTIME_ERROR",
                "本地服务暂时无法完成这次请求。",
                status=409,
                retryable=True,
            )
        except BaseException:
            return self._error_response(
                "INTERNAL_ERROR",
                "Pulsara 暂时无法完成这次请求。",
                status=500,
                retryable=False,
            )

    @web.middleware
    async def _security(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if self._port is not None:
            expected_host = f"{LOOPBACK_HOST}:{self._port}"
            if request.headers.get("Host") != expected_host:
                raise HttpPublicError(
                    "HOST_REJECTED",
                    "请求没有发往当前的 Pulsara 本地地址。",
                    status=421,
                )
        if request.path.startswith("/api/") and request.path != "/api/healthz":
            if (
                request.path.startswith(
                    ("/api/sessions", "/api/connections", "/api/memories")
                )
                and self._database_state() != "ready"
            ):
                raise HttpPublicError(
                    "DATABASE_DATA_PLANE_UNAVAILABLE",
                    "请先在设置中完成本机 PostgreSQL 配置。",
                    status=503,
                    retryable=True,
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("Origin")
                if origin is not None and origin != self.origin:
                    raise HttpPublicError(
                        "ORIGIN_REJECTED",
                        "请求不是从当前 Pulsara 页面发出的。",
                        status=403,
                    )
                fetch_site = request.headers.get("Sec-Fetch-Site")
                if fetch_site is not None and fetch_site not in {
                    "same-origin",
                    "none",
                }:
                    raise HttpPublicError(
                        "CROSS_SITE_REQUEST_REJECTED",
                        "不允许从其他网站发起请求。",
                        status=403,
                    )
                if self._is_draining():
                    raise HttpPublicError(
                        "APPLICATION_DRAINING",
                        "Pulsara 正在关闭。",
                        status=503,
                        retryable=True,
                    )
        return await handler(request)

    async def _health(self, _request: web.Request) -> web.Response:
        state = "ready" if self._is_ready() else "starting"
        if self._is_draining():
            state = "draining"
        return web.json_response({"status": state})

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.static_root / "index.html")

    async def _public_file(self, request: web.Request) -> web.StreamResponse:
        relative = request.path.lstrip("/")
        target = (self.static_root / relative).resolve()
        if self.static_root not in target.parents or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def _bootstrap(self, _request: web.Request) -> web.Response:
        payload = self.sessions.bootstrap_payload()
        payload.update(await self._settings_read_model())
        payload["runtime"] = {
            "status": "ready" if self._is_ready() else "starting",
            "origin": self.origin,
            "database_state": self._database_state(),
        }
        return web.json_response(payload)

    async def _model_catalog(self, _request: web.Request) -> web.Response:
        catalog = self.catalog.selectable()
        if catalog is None:
            return web.json_response({"status": "unavailable", "routes": []})
        return web.json_response(
            {
                "status": "ready",
                "routes": [
                    {
                        "route_id": route.route_id,
                        "display_name": route.display_name,
                        "models": [
                            _catalog_entry_payload(
                                entry,
                                model_runtime=self.model_runtime,
                            )
                            for entry in route.entries
                        ],
                    }
                    for route in catalog.routes
                ],
            }
        )

    async def _refresh_model_catalog(self, _request: web.Request) -> web.Response:
        await self.catalog.refresh()
        return await self._model_catalog(_request)

    async def _model_configurations(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"model_configurations": await self._connection_summaries()}
        )

    async def _add_model_configuration(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        resolved, api_key = self._resolve_model_configuration_body(body)
        await self.settings.add_model_connection(
            connection=resolved.config,
            api_key=api_key,
        )
        return web.json_response(
            {
                "model_configuration": await self._connection_summary(resolved.config),
                "wire_shape_warning": _wire_shape_warning(
                    resolved.target.target_facts.wire_shape_hint,
                    resolved.config.target.wire_api,
                ),
            },
            status=201,
        )

    async def _delete_model_configuration(self, request: web.Request) -> web.Response:
        connection_id = ModelConnectionId(request.match_info["connection_id"])
        settings, deleted = await self.settings.delete_model_connection(connection_id)
        return web.json_response(
            {
                "model_configuration_id": connection_id.value,
                "deleted": deleted,
                "model_configurations": await self._connection_summaries(settings),
            }
        )

    async def _test_model_configuration(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        resolved, api_key = self._resolve_model_configuration_body(body)
        try:
            await probe_model_connection(
                resolved=resolved,
                catalog=self.catalog.selectable(),
                route_wires=self.model_runtime.route_wires,
                api_key=api_key,
            )
        except ModelConnectionProbeFailure as exc:
            raise HttpPublicError(
                "MODEL_CONNECTION_TEST_FAILED",
                f"测试请求未通过（{exc.code}）：{exc.message}",
                status=409,
                retryable=True,
            ) from exc
        return web.json_response({"status": "ready"})

    def _resolve_model_configuration_body(
        self, body: dict[str, object]
    ) -> tuple[ResolvedModelConnection, str | None]:
        source = body.get("source")
        if source == "models_dev":
            expected = {"source", "route_id", "model_id", "wire_api", "api_key"}
            if set(body) != expected or not all(
                isinstance(body[key], str) and body[key] for key in expected
            ):
                raise ValueError("catalog model configuration has an invalid shape")
            target = ModelTargetKey(
                route_id=cast(str, body["route_id"]),
                wire_api=WireApi(cast(str, body["wire_api"])),
                model_id=cast(str, body["model_id"]),
            )
            return (
                create_model_connection(
                    catalog=self.model_runtime.selectable_catalog(),
                    target=target,
                    route_wires=self.model_runtime.route_wires,
                ),
                cast(str, body["api_key"]),
            )
        if source != "user_declared":
            raise ValueError("model configuration source is invalid")
        expected = {
            "source",
            "configuration_name",
            "base_url",
            "model_id",
            "wire_api",
            "authentication",
            "api_key",
            "context_tokens",
            "max_output_tokens",
            "tool_call",
            "reasoning",
        }
        if set(body) != expected:
            raise ValueError("custom model configuration has an invalid closed shape")
        name = body["configuration_name"]
        base_url = body["base_url"]
        model_id = body["model_id"]
        raw_wire_api = body["wire_api"]
        raw_authentication = body["authentication"]
        context_tokens = body["context_tokens"]
        max_output_tokens = body["max_output_tokens"]
        tool_call = body["tool_call"]
        if (
            not isinstance(name, str)
            or not isinstance(base_url, str)
            or not isinstance(model_id, str)
            or not isinstance(raw_wire_api, str)
            or not isinstance(raw_authentication, str)
            or isinstance(context_tokens, bool)
            or not isinstance(context_tokens, int)
            or isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not isinstance(tool_call, bool)
        ):
            raise ValueError("custom model configuration fields are invalid")
        authentication = ModelConnectionAuthentication(raw_authentication)
        api_key = body["api_key"]
        if authentication is ModelConnectionAuthentication.BEARER_API_KEY:
            if not isinstance(api_key, str) or not api_key:
                raise ValueError("custom bearer connection requires an API key")
        elif api_key is not None:
            raise ValueError("custom no-auth connection cannot include an API key")
        declaration = UserDeclaredModelTarget(
            configuration_name=name,
            total_context_tokens=context_tokens,
            max_output_tokens=max_output_tokens,
            tool_call=tool_call,
            reasoning=_user_declared_reasoning(body["reasoning"]),
            authentication=authentication,
        )
        return (
            create_user_declared_model_connection(
                model_id=model_id,
                wire_api=WireApi(raw_wire_api),
                base_url=base_url,
                declaration=declaration,
                route_wires=self.model_runtime.route_wires,
            ),
            cast(str | None, api_key),
        )

    async def _local_settings(self, _request: web.Request) -> web.Response:
        return web.json_response(await self._settings_read_model())

    async def _save_postgres_settings(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"runtime_dsn", "admin_dsn"}:
            raise ValueError("PostgreSQL settings have an invalid closed shape")
        runtime_dsn = body["runtime_dsn"]
        admin_dsn = body["admin_dsn"]
        if not isinstance(runtime_dsn, str) or (
            admin_dsn is not None and not isinstance(admin_dsn, str)
        ):
            raise ValueError("PostgreSQL settings fields are invalid")
        before = self._database_state()
        await self.settings.save_postgres(LocalPostgresConfig(runtime_dsn, admin_dsn))
        restart_required = before == "ready"
        self._postgres_settings_saved()
        payload = await self._settings_read_model()
        payload["restart_required"] = restart_required
        return web.json_response(payload)

    async def _check_postgres(self, _request: web.Request) -> web.Response:
        postgres = self.settings.read().postgres
        if postgres is None:
            raise HttpPublicError(
                "DATABASE_NOT_CONFIGURED",
                "请先保存 PostgreSQL 连接信息。",
                status=409,
            )
        from pulsara_agent.storage.postgres_connection_provider import (
            PostgresRuntimeConnectionFactory,
        )

        result = await asyncio.to_thread(
            PostgresRuntimeConnectionFactory(postgres.runtime_dsn).verify,
            deadline_monotonic=monotonic() + 30.0,
        )
        if self._database_state() != "ready":
            await self._refresh_database_state()
        return web.json_response(
            {
                "status": "verified",
                "database_name": result.binding.database_name,
                "runtime_role": result.binding.runtime_role,
                "migration_head_version": result.binding.migration_head_version,
            }
        )

    async def _migrate_postgres(self, _request: web.Request) -> web.Response:
        postgres = self.settings.read().postgres
        if postgres is None or postgres.admin_dsn is None:
            raise HttpPublicError(
                "DATABASE_ADMIN_DSN_REQUIRED",
                "初始化或升级数据库需要管理员 DSN。",
                status=409,
            )
        from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner

        report = await asyncio.to_thread(
            PostgresMigrationRunner(
                admin_dsn=postgres.admin_dsn,
                runtime_dsn=postgres.runtime_dsn,
            ).migrate,
            deadline_monotonic=monotonic() + 300.0,
        )
        await self._refresh_database_state()
        return web.json_response(report.to_dict())

    async def _put_dashscope_credential(self, request: web.Request) -> web.Response:
        kind = _dashscope_credential_kind(request.match_info["kind"])
        body = await self._json_body(request)
        if (
            set(body) != {"api_key"}
            or not isinstance(body["api_key"], str)
            or not body["api_key"]
        ):
            raise ValueError("DashScope credential has an invalid closed shape")
        await self.settings.save_dashscope_api_key(kind, body["api_key"])
        return web.json_response({"configured": True})

    async def _delete_dashscope_credential(self, request: web.Request) -> web.Response:
        kind = _dashscope_credential_kind(request.match_info["kind"])
        await self.settings.delete_dashscope_api_key(kind)
        return web.json_response({"configured": False})

    async def _update_model_call_binding(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        binding = model_call_binding_from_dict(body)
        if binding is None:
            raise ValueError("session model binding cannot be null")
        return web.json_response(
            await self.sessions.update_model_call_binding(
                request.match_info["session_id"], binding
            )
        )

    async def _settings_read_model(self) -> dict[str, object]:
        try:
            settings = self.settings.read()
            settings_state = "ready"
        except LocalSettingsUnavailable:
            settings = LocalSettings()
            settings_state = "unavailable"
        return {
            "local_settings": {
                "state": settings_state,
                "postgres": (
                    None
                    if settings.postgres is None
                    else {
                        "runtime_dsn": settings.postgres.runtime_dsn,
                        "admin_dsn": settings.postgres.admin_dsn,
                    }
                ),
                "dashscope_credentials": {
                    "embedding_configured": (
                        settings.dashscope_api_key("embedding") is not None
                    ),
                    "rerank_configured": (
                        settings.dashscope_api_key("rerank") is not None
                    ),
                },
            },
            "model_configurations": await self._connection_summaries(settings),
            "database_state": self._database_state(),
        }

    async def _connection_summaries(
        self, settings: LocalSettings | None = None
    ) -> list[dict[str, object]]:
        settings = settings or self.settings.read()
        return [
            await self._connection_summary(connection)
            for connection in settings.model_connections
        ]

    async def _connection_summary(
        self, connection: ModelConnectionConfig
    ) -> dict[str, object]:
        payload = model_connection_to_dict(connection)
        payload.pop("user_declared", None)
        payload.update(
            {
                "source": (
                    "models_dev"
                    if connection.user_declared is None
                    else "user_declared"
                ),
                "authentication": connection.authentication.value,
            }
        )
        try:
            contract = resolve_model_target_contract(
                catalog=self.catalog.selectable(),
                connection=connection,
                route_wires=self.model_runtime.route_wires,
            )
        except (KeyError, ValueError, ModelRuntimeUnavailable):
            payload.update(
                {
                    "status": "unavailable",
                    "credential_configured": connection.requires_api_key,
                    "reasoning": {"kind": "unavailable"},
                }
            )
            return payload
        payload.update(
            {
                "status": "ready",
                "credential_configured": connection.requires_api_key,
                "route_name": contract.target_facts.route_name,
                "display_name": contract.target_facts.display_name,
                "context_tokens": contract.target_facts.limits.total_context_tokens,
                "max_output_tokens": contract.target_facts.limits.max_output_tokens,
                "tool_call": contract.target_facts.tool_call,
                "reasoning": _reasoning_payload(contract.reasoning),
                "default_reasoning": reasoning_selection_to_dict(
                    default_reasoning_selection(contract.reasoning)
                ),
            }
        )
        return payload

    async def _list_sessions(self, _request: web.Request) -> web.Response:
        return web.json_response({"sessions": await self.sessions.list_sessions()})

    async def _list_session_tasks(self, request: web.Request) -> web.Response:
        raw_limit = request.query.get("limit", "50")
        try:
            maximum_items = int(raw_limit)
        except ValueError as exc:
            raise HttpPublicError(
                "TASK_PAGE_INVALID",
                "任务列表的分页参数不正确。",
                status=400,
            ) from exc
        try:
            payload = await self.sessions.list_session_tasks(
                request.match_info["session_id"],
                maximum_items=maximum_items,
                cursor=request.query.get("cursor"),
            )
        except ValueError as exc:
            raise HttpPublicError(
                "TASK_PAGE_INVALID",
                "任务列表的分页参数不正确。",
                status=400,
            ) from exc
        return web.json_response(payload)

    async def _inspect_session_capabilities(self, request: web.Request) -> web.Response:
        payload = await self.sessions.inspect_session_capabilities(
            request.match_info["session_id"]
        )
        return web.json_response(payload)

    async def _inspect_user_capabilities(self, request: web.Request) -> web.Response:
        payload = await self.sessions.inspect_user_capabilities(
            active_session_id=request.query.get("active_session_id")
        )
        return web.json_response(payload)

    async def _refresh_user_capabilities(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        active_session_id = _optional_body_string(body, "active_session_id")
        if set(body) - {"active_session_id"}:
            raise ValueError("unexpected capability refresh field")
        return web.json_response(
            await self.sessions.refresh_user_capabilities(
                active_session_id=active_session_id
            )
        )

    async def _open_capability_root(self, request: web.Request) -> web.Response:
        return web.json_response(
            await self.sessions.open_capability_root(request.match_info["root"])
        )

    async def _preview_skill_import(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"source_path"} or not isinstance(body["source_path"], str):
            raise ValueError("Skill preview requires a selected local source path")
        return web.json_response(
            await self.sessions.preview_skill_import(source_path=body["source_path"])
        )

    async def _install_user_skill(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"source_path", "active_session_id", "name", "description"}:
            raise ValueError("unexpected Skill install field")
        source_path = body.get("source_path")
        if not isinstance(source_path, str):
            raise ValueError("Skill source path is required")
        payload = await self.sessions.install_user_skill(
            source_path=source_path,
            active_session_id=_optional_body_string(body, "active_session_id"),
            name=_optional_body_string(body, "name"),
            description=_optional_body_string(body, "description"),
        )
        return web.json_response(payload, status=201)

    async def _set_user_skill_enabled(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"path", "enabled", "active_session_id"}:
            raise ValueError("unexpected Skill enablement field")
        path = body.get("path")
        enabled = body.get("enabled")
        if not isinstance(path, str) or not isinstance(enabled, bool):
            raise ValueError("Skill enablement fields are invalid")
        return web.json_response(
            await self.sessions.set_user_skill_enabled(
                skill_path=path,
                enabled=enabled,
                active_session_id=_optional_body_string(body, "active_session_id"),
            )
        )

    async def _remove_skill(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"path", "expected", "active_session_id"}:
            raise ValueError("unexpected Skill removal field")
        path = _optional_body_string(body, "path")
        raw = body.get("expected")
        names = {"root_device", "root_inode", "directory_device", "directory_inode"}
        if (
            path is None
            or not isinstance(raw, dict)
            or set(raw) != names
            or any(
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                for value in raw.values()
            )
        ):
            raise ValueError("Skill removal needs the current inspected identity")
        expected = LocalSkillRemovalIdentity(
            **{name: int(value) for name, value in raw.items()}
        )
        if "session_id" in request.match_info:
            if "active_session_id" in body:
                raise ValueError("Project Skill removal cannot select another session")
            return web.json_response(await self.sessions.remove_session_skill(
                request.match_info["session_id"], skill_path=path, expected=expected,
            ))
        return web.json_response(
            await self.sessions.remove_user_skill(
                skill_path=path,
                expected=expected,
                active_session_id=_optional_body_string(body, "active_session_id"),
            )
        )

    async def _preview_mcp_import(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"content", "shape", "server_id"}:
            raise ValueError("unexpected MCP import preview field")
        return web.json_response(
            await self.sessions.preview_mcp_import(**_mcp_import_source(body))
        )

    async def _import_mcp(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {
            "content",
            "shape",
            "server_id",
            "selected_server_id",
            "allow_http_localhost",
            "classifications",
            "values",
            "transport",
            "active_session_id",
        }:
            raise ValueError("unexpected MCP import field")
        selected = _optional_body_string(body, "selected_server_id")
        if selected is None:
            raise ValueError("MCP import requires a selected server")
        return web.json_response(
            await self.sessions.import_mcp(
                **_mcp_import_source(body),
                selected_server_id=selected,
                classifications=_mcp_import_values(body, "classifications"),
                values=_mcp_import_values(body, "values"),
                transport=_optional_body_string(body, "transport"),
                allow_http_localhost=body.get("allow_http_localhost", False),
                active_session_id=_optional_body_string(body, "active_session_id"),
                session_id=request.match_info.get("session_id"),
            ),
            status=201,
        )

    async def _create_user_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"server_id", "config", "secret_changes", "active_session_id"}:
            raise ValueError("unexpected MCP field")
        server_id = _optional_body_string(body, "server_id")
        config = body.get("config")
        if server_id is None or not isinstance(config, dict):
            raise ValueError("MCP id and complete config are required")
        return web.json_response(
            await self.sessions.create_user_mcp_server(
                server_id=server_id,
                config=config,
                secret_changes=_mcp_secret_changes(body),
                active_session_id=_optional_body_string(body, "active_session_id"),
            ),
            status=201,
        )

    async def _update_user_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {
            "config",
            "expected_identity",
            "secret_changes",
            "active_session_id",
            "retain_credentials_confirmed",
        }:
            raise ValueError("unexpected MCP field")
        config = body.get("config")
        expected = _optional_body_string(body, "expected_identity")
        if not isinstance(config, dict) or expected is None:
            raise ValueError("MCP config and current identity are required")
        return web.json_response(
            await self.sessions.update_user_mcp_server(
                server_id=request.match_info["server_id"],
                config=config,
                expected=expected,
                secret_changes=_mcp_secret_changes(body),
                active_session_id=_optional_body_string(body, "active_session_id"),
                retain_credentials_confirmed=_retain_credentials_confirmed(body),
            )
        )

    async def _test_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {
            "server_id",
            "config",
            "secret_changes",
            "retain_credentials_confirmed",
        }:
            raise ValueError("unexpected MCP test field")
        server_id = _optional_body_string(body, "server_id")
        config = body.get("config")
        if server_id is None or not isinstance(config, dict):
            raise ValueError("MCP id and complete test config are required")
        return web.json_response(
            await self.sessions.test_mcp_server(
                server_id=server_id,
                config=config,
                secret_changes=_mcp_secret_changes(body),
                retain_credentials_confirmed=_retain_credentials_confirmed(body),
                session_id=request.match_info.get("session_id"),
            )
        )

    async def _remove_user_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"expected_identity", "active_session_id"}:
            raise ValueError("unexpected MCP removal field")
        expected = _optional_body_string(body, "expected_identity")
        if expected is None:
            raise ValueError("MCP current identity is required")
        return web.json_response(
            await self.sessions.remove_user_mcp_server(
                server_id=request.match_info["server_id"],
                expected=expected,
                active_session_id=_optional_body_string(body, "active_session_id"),
            )
        )

    async def _authorize_mcp(self, request: web.Request) -> web.Response:
        if await self._json_body(request):
            raise ValueError("MCP login uses the saved target")
        return web.json_response(
            await self.sessions.authorize_mcp(request.match_info["server_id"], session_id=request.match_info.get("session_id")),
            status=202,
        )

    async def _mcp_authorization(self, request: web.Request) -> web.Response:
        if request.method != "GET" and await self._json_body(request):
            raise ValueError("MCP authorization action has no draft")
        action = (
            "status"
            if request.method == "GET"
            else "logout"
            if request.method == "DELETE"
            else "cancel"
        )
        return web.json_response(
            await self.sessions.mcp_authorization(
                request.match_info["server_id"],
                action=action,
                session_id=request.match_info.get("session_id"),
            )
        )

    async def _install_user_plugin(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"source_path", "active_session_id", "source_format", "classifications", "public_values"}:
            raise ValueError("unexpected Plugin install field")
        source_path = body.get("source_path")
        if not isinstance(source_path, str):
            raise ValueError("Plugin source path is required")
        payload = await self.sessions.install_user_plugin(
            source_path=source_path,
            active_session_id=_optional_body_string(body, "active_session_id"),
            source_format=_optional_body_string(body, "source_format") or "native",
            classifications=_plugin_import_strings(body, "classifications"),
            public_values=_plugin_import_strings(body, "public_values"),
        )
        return web.json_response(payload, status=201)

    async def _preview_plugin_import(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"source_path"} or not isinstance(body["source_path"], str):
            raise ValueError("请选择插件源目录")
        return web.json_response(await self.sessions.preview_plugin_import(**body))

    async def _set_user_plugin_enabled(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"enabled", "package_install_id", "active_session_id", "connection_review"}:
            raise ValueError("unexpected Plugin enablement field")
        enabled = body.get("enabled")
        package_install_id = body.get("package_install_id")
        if not isinstance(enabled, bool) or not isinstance(package_install_id, str):
            raise ValueError("Plugin enablement fields are invalid")
        from pulsara_agent.plugins.mcp_connection import review_from_dict

        connection_review = review_from_dict(body.get("connection_review"))
        return web.json_response(
            await self.sessions.set_user_plugin_enabled(
                plugin_id=request.match_info["plugin_id"],
                package_install_id=package_install_id,
                enabled=enabled,
                connection_review=connection_review,
                active_session_id=_optional_body_string(body, "active_session_id"),
            )
        )

    async def _plugin_mcp_authorization(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"action", "package_install_id"}:
            raise ValueError("Plugin MCP authorization fields are invalid")
        package = _optional_body_string(body, "package_install_id")
        action = _optional_body_string(body, "action")
        if not package or action not in {"login", "logout", "cancel", "status"}:
            raise ValueError("Plugin MCP authorization action is invalid")
        return web.json_response(await self.sessions.plugin_mcp_authorization(
            plugin_id=request.match_info["plugin_id"], server_id=request.match_info["server_id"],
            package_install_id=package, action=action,
        ))

    async def _remove_user_plugin(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"active_session_id", "package_install_id"}:
            raise ValueError("unexpected Plugin removal field")
        package_install_id = body.get("package_install_id")
        if not isinstance(package_install_id, str) or not package_install_id:
            raise ValueError("Plugin removal requires the inspected package")
        return web.json_response(
            await self.sessions.remove_user_plugin(
                plugin_id=request.match_info["plugin_id"],
                package_install_id=package_install_id,
                active_session_id=_optional_body_string(body, "active_session_id"),
            )
        )

    async def _replace_user_plugin_connection(self, request: web.Request) -> web.Response:
        from pulsara_agent.plugins.mcp_connection import overlay_from_dict

        body = await self._json_body(request)
        if set(body) - {"package_install_id", "expected_overlay", "overlay", "secret_changes", "retain_credentials_confirmed", "active_session_id"}:
            raise ValueError("unexpected Plugin connection field")
        package = body.get("package_install_id")
        if not isinstance(package, str) or not package or "expected_overlay" not in body or "overlay" not in body:
            raise ValueError("Plugin connection requires exact inspected values")
        return web.json_response(await self.sessions.replace_user_plugin_connection(
            plugin_id=request.match_info["plugin_id"], server_id=request.match_info["server_id"],
            package_install_id=package,
            expected_overlay=overlay_from_dict(body["expected_overlay"]) if body["expected_overlay"] is not None else None,
            overlay=overlay_from_dict(body["overlay"]) if body["overlay"] is not None else None,
            secret_changes=_mcp_secret_changes(body),
            retain_credentials_confirmed=_retain_credentials_confirmed(body),
            active_session_id=_optional_body_string(body, "active_session_id"),
        ))

    async def _reconnect_session_mcp(self, request: web.Request) -> web.Response:
        payload = await self.sessions.reconnect_session_mcp(
            request.match_info["session_id"],
            request.match_info["server_id"],
        )
        return web.json_response(payload)

    async def _install_session_skill(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if "source_path" not in body or set(body) - {
            "source_path",
            "name",
            "description",
        }:
            raise HttpPublicError(
                "SKILL_INSTALL_REQUEST_INVALID",
                "安装项目技能需要选择一个本地目录。",
                status=400,
            )
        source_path = body["source_path"]
        if not isinstance(source_path, str):
            raise HttpPublicError(
                "SKILL_INSTALL_REQUEST_INVALID",
                "安装项目技能需要选择一个本地目录。",
                status=400,
            )
        payload = await self.sessions.install_session_skill(
            request.match_info["session_id"],
            source_path=source_path,
            name=_optional_body_string(body, "name"),
            description=_optional_body_string(body, "description"),
        )
        return web.json_response(payload)

    async def _set_session_skill_enabled(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"skill_id", "enabled"}:
            raise ValueError("unexpected project Skill enablement field")
        skill_id = body.get("skill_id")
        enabled = body.get("enabled")
        if not isinstance(skill_id, str) or not isinstance(enabled, bool):
            raise ValueError("project Skill enablement fields are invalid")
        return web.json_response(
            await self.sessions.set_session_skill_enabled(
                request.match_info["session_id"],
                skill_id=skill_id,
                enabled=enabled,
            )
        )

    async def _create_session_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"server_id", "config", "secret_changes"}:
            raise ValueError("unexpected project MCP field")
        server_id = body.get("server_id")
        config = body.get("config")
        if not isinstance(server_id, str) or not isinstance(config, dict):
            raise ValueError("project MCP fields are invalid")
        payload = await self.sessions.create_session_mcp_server(
            request.match_info["session_id"],
            server_id=server_id,
            config=config,
            secret_changes=_mcp_secret_changes(body),
        )
        return web.json_response(payload, status=201)

    async def _update_session_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"config", "expected_identity", "secret_changes", "retain_credentials_confirmed"}:
            raise ValueError("unexpected project MCP update field")
        config = body.get("config")
        expected = _optional_body_string(body, "expected_identity")
        if not isinstance(config, dict) or expected is None:
            raise ValueError("project MCP requires complete config and inspected identity")
        return web.json_response(await self.sessions.update_session_mcp_server(
            request.match_info["session_id"], server_id=request.match_info["server_id"],
            config=config, expected_identity=expected, secret_changes=_mcp_secret_changes(body),
            retain_credentials_confirmed=_retain_credentials_confirmed(body),
        ))

    async def _set_session_mcp_enabled(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"enabled", "config_identity"}:
            raise ValueError("project MCP enablement fields are incomplete")
        if not isinstance(body.get("enabled"), bool) or not isinstance(
            body.get("config_identity"), str
        ):
            raise ValueError("project MCP enablement fields are invalid")
        return web.json_response(
            await self.sessions.set_session_mcp_enabled(
                request.match_info["session_id"],
                server_id=request.match_info["server_id"],
                enabled=body["enabled"],
                expected_config_identity=body["config_identity"],
            )
        )

    async def _remove_session_mcp_server(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"config_identity"} or not isinstance(
            body.get("config_identity"), str
        ):
            raise ValueError("project MCP removal identity is required")
        return web.json_response(
            await self.sessions.remove_session_mcp_server(
                request.match_info["session_id"],
                server_id=request.match_info["server_id"],
                expected_config_identity=body["config_identity"],
            )
        )

    async def _read_session(self, request: web.Request) -> web.Response:
        return web.json_response({"session": await self.sessions.read_session(request.match_info["session_id"])})

    async def _fork_conversation(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) != {"anchor_entry_id", "child_session_id"} or any(
            not isinstance(body[key], str) or not body[key] for key in body
        ):
            raise HttpPublicError("FORK_REQUEST_INVALID", "分叉需要消息 ID 和预先确定的新会话 ID。", status=400)
        return web.json_response(await self.sessions.fork_conversation(
            request.match_info["session_id"], anchor_entry_id=body["anchor_entry_id"],
            child_session_id=body["child_session_id"],
        ))

    async def _create_session(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        if set(body) - {"workspace_kind", "workspace_path"}:
            raise HttpPublicError(
                "WORKSPACE_REQUEST_INVALID",
                "新建会话只需要选择工作目录。",
                status=400,
            )
        workspace_kind = body.get("workspace_kind")
        workspace_path = body.get("workspace_path")
        if workspace_kind not in {"quick", "project"}:
            raise HttpPublicError(
                "WORKSPACE_KIND_REQUIRED",
                "请选择“快速开始”或“指定目录”。",
                status=400,
            )
        if workspace_path is not None and not isinstance(workspace_path, str):
            raise HttpPublicError(
                "WORKSPACE_PATH_INVALID",
                "工作目录路径不正确。",
                status=400,
            )
        try:
            handle = await self.sessions.create_session(
                workspace_kind=cast(SessionWorkspaceKind, workspace_kind),
                workspace_path=workspace_path,
            )
        except (OSError, ValueError) as exc:
            raise HttpPublicError(
                "WORKSPACE_UNAVAILABLE",
                "这个工作目录不存在或无法使用。",
                status=400,
            ) from exc
        summaries = await self.sessions.list_sessions()
        summary = next(item for item in summaries if item["id"] == handle.session_id)
        return web.json_response({"session": summary}, status=201)

    async def _close_session(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        session_id = request.match_info["session_id"]
        close_conversation = body.get("close_conversation", False)
        if not isinstance(close_conversation, bool):
            raise ValueError("close_conversation must be boolean")
        await self.bridge.disconnect_session(session_id)
        await self.sessions.close_session(
            session_id, close_conversation=close_conversation
        )
        return web.json_response(
            {
                "status": "closed",
                "canonical_conversation_closed": close_conversation,
            }
        )

    async def _connect(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        browser_instance_id = _optional_body_string(body, "browser_instance_id")
        if browser_instance_id is None:
            raise ValueError("browser_instance_id is required")
        try:
            parsed_browser_instance_id = UUID(browser_instance_id)
        except ValueError as exc:
            raise ValueError("browser_instance_id must be a canonical UUID") from exc
        if (
            parsed_browser_instance_id.version != 4
            or str(parsed_browser_instance_id) != browser_instance_id
        ):
            raise ValueError("browser_instance_id must be a canonical UUID")
        takeover = body.get("takeover", False)
        if not isinstance(takeover, bool):
            raise ValueError("takeover must be boolean")
        payload = await self.bridge.connect(
            request.match_info["session_id"],
            browser_instance_id=browser_instance_id,
            takeover=takeover,
        )
        return web.json_response(payload, status=201)

    async def _disconnect(self, request: web.Request) -> web.Response:
        await self.bridge.disconnect(request.match_info["connection_id"])
        return web.json_response({"status": "detached"})

    def _protocol_handler(
        self, operation: str
    ) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def handler(request: web.Request) -> web.Response:
            connection_id = request.match_info["connection_id"]
            body = await self._json_body(request)
            methods = {
                "snapshot": lambda: self.bridge.snapshot(connection_id),
                "history": lambda: self.bridge.history(connection_id, body),
                "observe": lambda: self.bridge.observe(connection_id, body),
                "command": lambda: self.bridge.command(connection_id, body),
                "query-command": lambda: self.bridge.query_command(connection_id, body),
                "live-control-snapshot": lambda: self.bridge.live_control_snapshot(
                    connection_id
                ),
                "resolve-interaction": lambda: self.bridge.resolve_interaction(
                    connection_id, body
                ),
                "read-capability-form": lambda: self.bridge.capability_form(
                    connection_id, body, submit=False,
                ),
                "resolve-capability-form": lambda: self.bridge.capability_form(
                    connection_id, body, submit=True,
                ),
                "resolve-plan-interaction": lambda: (
                    self.bridge.resolve_plan_interaction(connection_id, body)
                ),
                "read-plan-question": lambda: self.bridge.read_plan_question(
                    connection_id, body
                ),
                "read-plan-draft": lambda: self.bridge.read_plan_draft(
                    connection_id, body
                ),
                "read-content": lambda: self.bridge.read_content(connection_id, body),
            }
            return web.json_response(await methods[operation]())

        return handler

    @staticmethod
    async def _json_body(request: web.Request) -> dict[str, object]:
        if not request.can_read_body or request.content_length == 0:
            return {}
        try:
            value = await request.json()
        except Exception as exc:
            raise ValueError("request body must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    @staticmethod
    def _error_response(
        code: str,
        message: str,
        *,
        status: int,
        retryable: bool,
    ) -> web.Response:
        return web.json_response(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                }
            },
            status=status,
        )


__all__ = [
    "HttpPublicError",
    "LOOPBACK_HOST",
    "LocalHttpServer",
]
