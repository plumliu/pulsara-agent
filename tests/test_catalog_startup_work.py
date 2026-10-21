"""Cold resource preparation and advisory catalog work preserve their owners."""
import asyncio
from dataclasses import replace
import ssl
from threading import Event, get_ident
from unittest.mock import AsyncMock

from aiohttp import ClientSession, web
import httpx
import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.identity import random_id
from pulsara_agent.llm import model_catalog as catalog_module
from pulsara_agent.llm.model_catalog import ModelCatalogInvalid, ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelConnectionAuthentication, ModelConnectionConfig, ModelConnectionId,
    ReasoningWireProfile, UserDeclaredModelTarget,
)
from pulsara_agent.llm.model_catalog import ModelTargetKey, ReasoningProviderDefault, WireApi
from pulsara_agent.llm.runtime import ModelRuntimeUnavailable
from pulsara_agent.settings import LocalModelApiKey, LocalSettings
from pulsara_agent.tool_permission import default_permission_policy
from tests.support.model_config import StaticTestSettingsStore, test_model_runtime as make_runtime
from tests.test_desktop_http_surface import _Bridge, _Sessions, _model_server_dependencies
from tests.test_llm_model_catalog import catalog_fixture


@pytest.mark.parametrize("failed", [False, True])
def test_prepare_awaits_original_resource_owner_without_reading_a_list(monkeypatch, failed):
    async def exercise():
        core = KernelHostCore.production(model_runtime=make_runtime())
        controller = LocalSessionController(
            core=core, permission_policy=default_permission_policy(),
            active_skill_names=frozenset(),
        )
        entered, release = asyncio.Event(), asyncio.Event()
        failure = RuntimeError("resource verification failed")

        async def ensure():
            entered.set()
            await release.wait()
            if failed:
                raise failure

        monkeypatch.setattr(core, "_ensure_resources", ensure)
        forbidden = AsyncMock(side_effect=AssertionError("discarded list was read"))
        monkeypatch.setattr(controller, "list_sessions", forbidden)
        task = asyncio.create_task(controller.prepare())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not task.done()
            release.set()
            if failed:
                with pytest.raises(RuntimeError) as observed:
                    await task
                assert observed.value is failure
            else:
                assert await task is None
            forbidden.assert_not_awaited()
        finally:
            release.set()
            if not task.done():
                await task

    asyncio.run(exercise())


def test_http_catalog_json_and_projection_use_original_parsers_after_http_close(monkeypatch):
    async def exercise():
        events, threads = [], []
        original_client = httpx.AsyncClient
        original_json = httpx.Response.json
        original_parse = catalog_module.parse_models_dev_catalog

        class Client(original_client):
            async def __aexit__(self, *args):
                await super().__aexit__(*args)
                events.append("http_closed")

        def json(response, *args, **kwargs):
            events.append("json")
            threads.append(get_ident())
            return original_json(response, *args, **kwargs)

        def parse(payload):
            events.append("projection")
            threads.append(get_ident())
            return original_parse(payload)

        monkeypatch.setattr(httpx.Response, "json", json)
        monkeypatch.setattr(catalog_module, "parse_models_dev_catalog", parse)
        monkeypatch.setattr(catalog_module.httpx, "AsyncClient", lambda **kwargs: Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=catalog_fixture())),
            **kwargs,
        ))
        result = await ModelsDevCatalogClient().fetch()
        assert result.entries
        assert events == ["json", "http_closed", "projection"]
        assert threads and all(thread != get_ident() for thread in threads)

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_request", [False, True])
def test_catalog_http_client_preparation_keeps_real_httpx_proxy_tls_and_loop_owners(monkeypatch, cancel_request):
    async def exercise():
        from httpx._transports import default as transport_module

        main_thread = get_ident()
        observed, lifecycle, tls_threads = [], [], []
        request_started, request_release = asyncio.Event(), asyncio.Event()
        original_client = httpx.AsyncClient
        original_ssl = transport_module.create_ssl_context

        async def proxy(request):
            observed.append(request.raw_path)
            request_started.set()
            if cancel_request:
                await request_release.wait()
            return web.json_response(catalog_fixture())

        app = web.Application()
        app.router.add_get("/api.json", proxy)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        class Client(original_client):
            def __init__(self, **kwargs):
                lifecycle.append(("construct", get_ident()))
                assert kwargs["follow_redirects"] is True
                assert kwargs["timeout"].as_dict() == {
                    "connect": 10.0, "read": 30.0, "write": 10.0, "pool": 10.0,
                }
                super().__init__(**kwargs)

            async def __aenter__(self):
                lifecycle.append(("enter", get_ident()))
                return await super().__aenter__()

            async def get(self, *args, **kwargs):
                lifecycle.append(("get", get_ident()))
                return await super().get(*args, **kwargs)

            async def __aexit__(self, *args):
                await super().__aexit__(*args)
                lifecycle.append(("close", get_ident()))

        def create_ssl(*args, **kwargs):
            context = original_ssl(*args, **kwargs)
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
            tls_threads.append(get_ident())
            return context

        # A real SDK proxy exchange proves that passing a custom transport or
        # trust_env=False has not accidentally disabled the existing env owner.
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("NO_PROXY", "")
        for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(catalog_module, "MODELS_DEV_CATALOG_URL", "http://catalog.invalid/api.json")
        monkeypatch.setattr(catalog_module.httpx, "AsyncClient", Client)
        monkeypatch.setattr(transport_module, "create_ssl_context", create_ssl)
        try:
            owner = ModelCatalogOwner(ModelsDevCatalogClient())
            task = asyncio.create_task(owner.refresh())
            if cancel_request:
                await asyncio.wait_for(request_started.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert owner.snapshot is None
            else:
                assert (await task).entries
            assert observed == ["http://catalog.invalid/api.json"]
            assert [name for name, _thread in lifecycle] == ["construct", "enter", "get", "close"]
            assert lifecycle[0][1] != main_thread
            assert all(thread == main_thread for _name, thread in lifecycle[1:])
            assert tls_threads and all(thread != main_thread for thread in tls_threads)
        finally:
            request_release.set()
            await runner.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize("constructor_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
def test_cancelled_catalog_client_construction_joins_and_closes_unopened_result(
    monkeypatch, constructor_fails, close_fails,
):
    async def exercise():
        loop = asyncio.get_running_loop()
        main_thread = get_ident()
        started, close_started, close_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = Event()
        events, unhandled = [], []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        class Client:
            def __init__(self, **kwargs):
                assert get_ident() != main_thread
                loop.call_soon_threadsafe(started.set)
                assert release.wait(3), "constructor was not released"
                events.append("constructed")
                if constructor_fails:
                    raise OSError("late constructor failure")

            async def aclose(self):
                assert get_ident() == main_thread
                events.append("close_started")
                close_started.set()
                await close_release.wait()
                events.append("closed")
                if close_fails:
                    raise OSError("late close failure")

            async def __aenter__(self):
                raise AssertionError("cancelled client must not start HTTP")

        monkeypatch.setattr(catalog_module.httpx, "AsyncClient", Client)
        owner = ModelCatalogOwner(ModelsDevCatalogClient())
        task = asyncio.create_task(owner.refresh())
        try:
            await asyncio.wait_for(started.wait(), 1)
            heartbeat = asyncio.Event()
            loop.call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 1)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            assert owner.snapshot is None
            release.set()
            if not constructor_fails:
                await asyncio.wait_for(close_started.wait(), 1)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert events == (["constructed"] if constructor_fails else ["constructed", "close_started", "closed"])
            assert owner.snapshot is None
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            release.set()
            close_release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["json", "projection"])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelled_catalog_joins_worker_repeated_cancellation_and_consumes_failure(
    monkeypatch, stage, worker_fails,
):
    async def exercise():
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), Event()
        events, unhandled = [], []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        original_parse = catalog_module.parse_models_dev_catalog

        def block():
            assert get_ident() != main_thread
            events.append("worker_started")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), "test worker was not released"
            events.append("worker_finished")
            if worker_fails:
                raise ValueError("worker parse failed")

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                if stage == "json":
                    block()
                return catalog_fixture()

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                events.append("http_closed")

            async def get(self, *args, **kwargs):
                return Response()

        def parse(payload):
            if stage == "projection":
                block()
            return original_parse(payload)

        main_thread = get_ident()
        monkeypatch.setattr(catalog_module.httpx, "AsyncClient", Client)
        monkeypatch.setattr(catalog_module, "parse_models_dev_catalog", parse)
        owner = ModelCatalogOwner(ModelsDevCatalogClient())
        task = asyncio.create_task(owner.refresh())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            # This heartbeat executes while the real parser worker is blocked.
            heartbeat = asyncio.Event()
            loop.call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 1)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            assert ("http_closed" in events) is (stage == "projection")
            assert owner.snapshot is None
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert owner.snapshot is None
            assert "worker_finished" in events
            if stage == "json":
                assert events.index("worker_finished") < events.index("http_closed")
            else:
                assert events.index("http_closed") < events.index("worker_started")
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["json", "projection", "http_close"])
def test_catalog_errors_keep_json_close_projection_order(monkeypatch, stage):
    async def exercise():
        events = []
        failure = ModelCatalogInvalid("original projection failure")

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                events.append("json")
                if stage == "json":
                    raise ValueError("invalid JSON")
                return {}

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                events.append("http_closed")
                if stage == "http_close":
                    raise ValueError("HTTP close failed")

            async def get(self, *args, **kwargs):
                return Response()

        def parse(payload):
            events.append("projection")
            raise failure

        monkeypatch.setattr(catalog_module.httpx, "AsyncClient", Client)
        monkeypatch.setattr(catalog_module, "parse_models_dev_catalog", parse)
        with pytest.raises(ModelCatalogInvalid) as observed:
            await ModelsDevCatalogClient().fetch()
        if stage == "projection":
            assert observed.value is failure
            assert events == ["json", "http_closed", "projection"]
        else:
            assert str(observed.value) == "models.dev catalog is invalid"
            assert events == ["json", "http_closed"]

    asyncio.run(exercise())


@pytest.mark.parametrize("catalog_error", [None, "missing", KeyError, ValueError, ModelRuntimeUnavailable])
def test_model_configuration_api_observes_one_catalog_per_bulk_operation(
    tmp_path, monkeypatch, catalog_error,
):
    async def exercise():
        dependencies = _model_server_dependencies()
        runtime = dependencies["model_runtime"]
        connection = runtime.settings.read().model_connections[0]
        declared = ModelConnectionConfig(
            ModelConnectionId(random_id("connection")),
            ModelTargetKey("user_declared", WireApi.OPENAI_RESPONSES, "local"),
            "http://127.0.0.1:9/v1", ReasoningWireProfile.PROVIDER_DEFAULT,
            UserDeclaredModelTarget(
                configuration_name="Local", total_context_tokens=256_000,
                max_output_tokens=8192, tool_call=True,
                reasoning=ReasoningProviderDefault(),
                authentication=ModelConnectionAuthentication.NONE,
            ),
        )
        connections = tuple(replace(connection, id=ModelConnectionId(random_id("connection"))) for _ in range(3)) + (declared,)
        dependencies["settings"] = StaticTestSettingsStore(LocalSettings(
            model_connections=connections,
            model_api_keys=tuple(
                LocalModelApiKey(item.id, "fixture-catalog-key")
                for item in connections if item.requires_api_key
            ),
        ))
        original = ModelCatalogOwner.selectable
        calls = 0

        def selectable(owner):
            nonlocal calls
            calls += 1
            if catalog_error == "missing":
                return None
            if catalog_error is not None:
                raise catalog_error("unavailable catalog")
            return original(owner)

        monkeypatch.setattr(ModelCatalogOwner, "selectable", selectable)
        (tmp_path / "index.html").write_text("Pulsara")
        server = LocalHttpServer(
            sessions=_Sessions(), bridge=_Bridge(), static_root=tmp_path,
            requested_port=0, is_ready=lambda: True, is_draining=lambda: False,
            **dependencies,
        )
        await server.start()
        try:
            async with ClientSession() as client:
                for route in ("/api/app/bootstrap", "/api/model-configurations"):
                    before = calls
                    async with client.get(server.origin + route) as response:
                        assert response.status == 200
                        summaries = (await response.json())["model_configurations"]
                    assert calls - before == 1
                    assert len(summaries) == 4
                    assert [item["status"] for item in summaries] == (
                        ["ready"] * 4 if catalog_error is None else
                        ["unavailable"] * 3 + ["ready"] if catalog_error == "missing" else
                        ["unavailable"] * 4
                    )
            before = calls
            assert server._single_connection_summary(connection)["status"] == ("ready" if catalog_error is None else "unavailable")
            assert calls - before == 1
            before = calls
            assert await server._connection_summaries(LocalSettings()) == []
            assert calls == before
        finally:
            await server.aclose()

    asyncio.run(exercise())
