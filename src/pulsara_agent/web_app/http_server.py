"""Loopback-only HTTP surface for Pulsara Web."""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, cast

from aiohttp import web

from pulsara_agent.conversation_kernel.host import KernelHostCoreClosing
from pulsara_agent.web_app.browser_bridge import LocalBrowserBridge
from pulsara_agent.web_app.protocol_client import ProtocolBridgeError
from pulsara_agent.web_app.session_controller import (
    LocalSessionController,
    SessionWorkspaceKind,
)


LOOPBACK_HOST = "127.0.0.1"


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
    ) -> None:
        self.sessions = sessions
        self.bridge = bridge
        self.static_root = static_root.resolve()
        self.requested_port = requested_port
        self._is_ready = is_ready
        self._is_draining = is_draining
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
        self._app.router.add_get("/healthz", self._health)
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/og.png", self._public_file)
        self._app.router.add_get("/assets/{tail:.*}", self._public_file)
        self._app.router.add_get("/api/app/bootstrap", self._bootstrap)
        self._app.router.add_get("/api/sessions", self._list_sessions)
        self._app.router.add_get(
            "/api/sessions/{session_id}/tasks", self._list_session_tasks
        )
        self._app.router.add_post("/api/sessions", self._create_session)
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
        payload["runtime"] = {
            "status": "ready" if self._is_ready() else "starting",
            "origin": self.origin,
        }
        return web.json_response(payload)

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
        takeover = body.get("takeover", False)
        if not isinstance(takeover, bool):
            raise ValueError("takeover must be boolean")
        payload = await self.bridge.connect(
            request.match_info["session_id"], takeover=takeover
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
