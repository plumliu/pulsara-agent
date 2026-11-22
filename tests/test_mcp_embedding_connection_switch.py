"""Shared embedding settings take effect at each consumer's wire boundary."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from aiohttp import web
from httpcore2._backends.auto import AutoBackend
import pytest

from pulsara_agent.capability.mcp_embedding_cache import mcp_tool_embedding_cache_key
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.mcp.tool_embeddings import McpToolEmbeddings
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.retrieval import http as retrieval_http
from pulsara_agent.retrieval.config import EmbeddingBackendConfig, RetrievalConnection
from pulsara_agent.settings import LocalSettingsStore


class _WarmCache:
    def __init__(self, connection):
        self.key = mcp_tool_embedding_cache_key(connection, "tool")

    def lookup(self, keys, *, deadline_monotonic):
        return {self.key}.intersection(keys)

    def rank(self, keys, vector, *, deadline_monotonic):
        return dict.fromkeys(keys, 1.0)


@pytest.fixture(autouse=True)
def _direct_loopback(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@asynccontextmanager
async def _local_provider(handler):
    app = web.Application()
    app.router.add_post("/embeddings", handler)
    app.router.add_get("/redirected", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        [(host, port)] = runner.addresses
        yield f"http://{host}:{port}/embeddings"
    finally:
        await runner.cleanup()


async def _publish(store, connection):
    previous = store.read().memory_retrieval.embedding
    public = connection.public()
    del public["credential_configured"]
    await store.save_retrieval_connection(
        "embedding",
        {
            "connection": public,
            "key_action": "replace",
            "api_key": connection.api_key,
            "confirm_reembed": True,
            "expected_embedding": None
            if previous is None
            else {
                name: getattr(previous, name)
                for name in ("endpoint", "model_id", "shape")
            },
        },
    )


def _service(store, connection, requests=None):
    return McpToolEmbeddings(
        _WarmCache(connection),
        connection=lambda: store.read().memory_retrieval.embedding,
        requests=requests or asyncio.Semaphore(5),
    )


def _embedding_response(body):
    return web.json_response(
        {
            "model": body["model"],
            "data": [{"index": 0, "embedding": [1.0] + [0.0] * 1023}],
        }
    )


def test_clearing_shared_connection_during_tcp_connect_prevents_old_post(
    tmp_path, monkeypatch
):
    store = LocalSettingsStore(tmp_path / "settings" / "local-settings.yaml")
    calls = []

    async def handler(request):
        body = await request.json()
        calls.append(body)
        return _embedding_response(body)

    async def exercise():
        connecting, release = asyncio.Event(), asyncio.Event()
        connect_tcp = AutoBackend.connect_tcp

        async def delayed_connect(backend, *args, **kwargs):
            connecting.set()
            await release.wait()
            return await connect_tcp(backend, *args, **kwargs)

        monkeypatch.setattr(AutoBackend, "connect_tcp", delayed_connect)
        async with _local_provider(handler) as endpoint:
            old = RetrievalConnection(
                endpoint, "old-model", "openai_embedding", api_key="old-test-key"
            )
            await _publish(store, old)
            service = _service(store, old)
            query = asyncio.create_task(
                service.scores("find", connection=old, texts=("tool",))
            )
            try:
                await asyncio.wait_for(connecting.wait(), timeout=5)
                await store.clear_retrieval_connection("embedding")
                assert service.current_connection() is None
                release.set()
                assert await asyncio.wait_for(query, timeout=5) == {}
                assert calls == []
            finally:
                release.set()
                await asyncio.gather(query, return_exceptions=True)
                await service.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ("rotate", "clear", "disable_memory"))
def test_memory_requests_queued_behind_mcp_recheck_connection_before_sending(
    tmp_path, change
):
    store = LocalSettingsStore(tmp_path / "settings" / "local-settings.yaml")

    async def exercise():
        posted, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class ObservedGate(asyncio.Semaphore):
            waiting = 0

            async def acquire(self):
                if self.locked():
                    self.waiting += 1
                    if self.waiting == 2:
                        queued.set()
                return await super().acquire()

        async def handler(request):
            body = await request.json()
            calls.append((request.headers["Authorization"], body["input"]))
            if len(calls) == 1:
                posted.set()
                await release.wait()
            return _embedding_response(body)

        async with _local_provider(handler) as endpoint:
            old = RetrievalConnection(
                endpoint, "model", "openai_embedding", api_key="old-test-key"
            )
            await _publish(store, old)
            await store.set_retrieval_mode(embedding_enabled=True)
            requests = ObservedGate(1)
            mcp = _service(store, old, requests)
            memory = KernelMemoryToolPort(
                repository=SimpleNamespace(connection_provider=object()),
                session_id=random_id("session"),
                read_binding=object(),
                embedding_config=EmbeddingBackendConfig(),
                embedding_gate=requests,
                io_owner=KernelSessionIO(),
                settings=store,
            )
            tasks = [
                asyncio.create_task(
                    mcp.scores("find tool", connection=old, texts=("tool",))
                )
            ]
            try:
                await asyncio.wait_for(posted.wait(), timeout=5)
                tasks.extend(
                    (
                        asyncio.create_task(
                            memory._query_embedding("find memory", automatic=False)
                        ),
                        asyncio.create_task(
                            memory.embed_memory_batch(
                                ("memory fact",), timeout_seconds=5, connection=old
                            )
                        ),
                    )
                )
                await asyncio.wait_for(queued.wait(), timeout=5)
                if change == "rotate":
                    rotated = replace(old, api_key="rotated-test-key")
                    await _publish(store, rotated)
                elif change == "clear":
                    await store.clear_retrieval_connection("embedding")
                else:
                    await store.set_retrieval_mode(embedding_enabled=False)
                release.set()

                scores, memory_query, memory_batch = await asyncio.wait_for(
                    asyncio.gather(*tasks), timeout=5
                )
                assert calls == [("Bearer old-test-key", ["find tool"])]
                assert memory_query is memory_batch is None
                assert scores == ({"tool": 1.0} if change == "disable_memory" else {})

                if change == "rotate":
                    query = await memory._query_embedding("find fresh", automatic=False)
                    batch = await memory.embed_memory_batch(
                        ("fresh fact",), timeout_seconds=5, connection=rotated
                    )
                    assert query.connection == rotated
                    assert tuple(batch[0]) == query.vector
                    assert calls[1:] == [
                        ("Bearer rotated-test-key", ["find fresh"]),
                        ("Bearer rotated-test-key", ["fresh fact"]),
                    ]
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await memory.aclose()
                await mcp.aclose()

    asyncio.run(exercise())


def test_rotation_after_post_blocks_redirect_and_reuses_cached_vectors(
    tmp_path, monkeypatch
):
    store = LocalSettingsStore(tmp_path / "settings" / "local-settings.yaml")

    async def exercise():
        posted, release = asyncio.Event(), asyncio.Event()
        calls, boundaries = [], []
        build = retrieval_http.build_async_openai_client

        def track_boundary(**kwargs):
            boundaries.append(kwargs["credential_boundary"])
            return build(**kwargs)

        monkeypatch.setattr(retrieval_http, "build_async_openai_client", track_boundary)

        async def handler(request):
            body = (
                await request.json() if request.method == "POST" else {"model": "model"}
            )
            calls.append(
                (request.path, request.headers["Authorization"], body.get("input"))
            )
            if len(calls) == 1:
                posted.set()
                await release.wait()
            if request.path == "/embeddings":
                raise web.HTTPSeeOther("/redirected")
            return _embedding_response(body)

        async with _local_provider(handler) as endpoint:
            old = RetrievalConnection(
                endpoint, "model", "openai_embedding", api_key="old-test-key"
            )
            rotated = replace(old, api_key="rotated-test-key")
            await _publish(store, old)
            service = _service(store, old)
            query = asyncio.create_task(
                service.scores("find", connection=old, texts=("tool",))
            )
            try:
                await asyncio.wait_for(posted.wait(), timeout=5)
                # The original credential trace still releases admission as
                # soon as the request body is sent, before the response arrives.
                async with asyncio.timeout(5):
                    async with boundaries[0].async_guard() as guard:
                        assert guard.value == old.api_key
                await _publish(store, rotated)
                release.set()
                assert await asyncio.wait_for(query, timeout=5) == {}
                assert calls == [("/embeddings", "Bearer old-test-key", ["find"])]
                assert await service.scores(
                    "find again", connection=rotated, texts=("tool",)
                ) == {"tool": 1.0}
                assert calls[1:] == [
                    ("/embeddings", "Bearer rotated-test-key", ["find again"]),
                    ("/redirected", "Bearer rotated-test-key", None),
                ]
            finally:
                release.set()
                await asyncio.gather(query, return_exceptions=True)
                await service.aclose()

    asyncio.run(exercise())
