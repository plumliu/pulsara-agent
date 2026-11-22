"""Demand-driven MCP document warming through the shared embedding client."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

import httpx2
import pytest

from pulsara_agent.capability.mcp_embedding_cache import (
    PostgresMcpToolEmbeddingCache,
    mcp_tool_embedding_cache_key,
)
from pulsara_agent.capability.mcp_search import mcp_tool_search_text
from pulsara_agent.conversation_kernel.mcp.tool_embeddings import McpToolEmbeddings
from pulsara_agent.llm.adapters.openai import client as openai_client
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundHttpx2Client
from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
from pulsara_agent.retrieval.config import RetrievalConnection
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)


_CONNECTION = RetrievalConnection(
    "https://embedding.example/v1/embeddings",
    "embedding-one",
    "openai_embedding",
    api_key="fixture-first-embedding-key",
)
_VECTOR = [1.0, *([0.0] * 1023)]


def _inject_transport(monkeypatch, respond):
    def client(**kwargs):
        return ProcessCredentialBoundHttpx2Client(
            **kwargs, transport=httpx2.MockTransport(respond), trust_env=False
        )

    monkeypatch.setattr(openai_client, "ProcessCredentialBoundHttpx2Client", client)


def _vectors(body):
    return httpx2.Response(
        200,
        json={
            "model": body["model"],
            "data": [
                {"index": index, "embedding": _VECTOR}
                for index, _ in enumerate(body["input"])
            ],
        },
    )


@pytest.mark.postgres
def test_cold_concurrent_notion_warm_and_reopen(
    monkeypatch, isolated_migrated_postgres_database
):
    directory = json.loads(
        (
            Path(__file__).parent / "fixtures" / "notion_native_tools_20261020.json"
        ).read_text()
    )["tools"]
    texts = tuple(
        mcp_tool_search_text(
            SimpleNamespace(
                remote_tool_name=tool["name"], description=tool["description"]
            )
        )
        for tool in directory
    )
    assert len(texts) == len(set(texts)) == 65
    dsn = isolated_migrated_postgres_database.runtime_dsn

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def respond(request):
            body = json.loads(request.content)
            calls.append(body["input"])
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return _vectors(body)

        _inject_transport(monkeypatch, respond)
        with acquire_verified_postgres_access_sync(
            dsn, deadline_monotonic=monotonic() + 30
        ) as access:
            cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
            shared = asyncio.Semaphore(5)
            service = McpToolEmbeddings(
                cache, connection=lambda: _CONNECTION, requests=shared
            )
            try:
                too_long = "oversized\n" + "a" * 40_000
                assert (
                    await service.scores(
                        "cold",
                        connection=_CONNECTION,
                        texts=(*texts, texts[0], too_long),
                    )
                    == {}
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert await asyncio.gather(
                    *(
                        service.scores(
                            "concurrent", connection=_CONNECTION, texts=texts[:20]
                        )
                        for _ in range(3)
                    )
                ) == [{}, {}, {}]
                assert len(calls) == 1
                # Four other consumers can use the Host budget while warming.
                for _ in range(4):
                    await asyncio.wait_for(shared.acquire(), timeout=5)
                assert shared.locked()
                for _ in range(4):
                    shared.release()
                release.set()
                await service._worker
            finally:
                await service.aclose()
            assert service.physical_close_complete
        assert [len(batch) for batch in calls] == [10, 10, 10, 10, 10, 10, 5]
        assert Counter(text for batch in calls for text in batch) == Counter(texts)

        with acquire_verified_postgres_access_sync(
            dsn, deadline_monotonic=monotonic() + 30
        ) as access:
            rotated = replace(_CONNECTION, api_key="fixture-rotated-embedding-key")
            cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
            service = McpToolEmbeddings(
                cache, connection=lambda: rotated, requests=asyncio.Semaphore(5)
            )
            try:
                assert await service.scores(
                    "hot", connection=rotated, texts=(texts[2], texts[0])
                ) == {
                    texts[2]: 1.0,
                    texts[0]: 1.0,
                }
            finally:
                await service.aclose()
        assert calls[7:] == [["hot"]]

    asyncio.run(exercise())


@pytest.mark.postgres
def test_connection_switch_settles_admitted_space_and_abandons_queued_space(
    monkeypatch, isolated_migrated_postgres_database
):
    dsn = isolated_migrated_postgres_database.runtime_dsn
    old, new = _CONNECTION, replace(_CONNECTION, model_id="embedding-two")
    texts = tuple(f"tool_{index}\nShared description" for index in range(12))

    async def exercise():
        connection = old
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def respond(request):
            body = json.loads(request.content)
            calls.append((body["model"], body["input"]))
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return _vectors(body)

        _inject_transport(monkeypatch, respond)
        with acquire_verified_postgres_access_sync(
            dsn, deadline_monotonic=monotonic() + 30
        ) as access:
            cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
            service = McpToolEmbeddings(
                cache, connection=lambda: connection, requests=asyncio.Semaphore(5)
            )
            try:
                assert (
                    await service.scores("cold old", connection=old, texts=texts) == {}
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                connection = None
                assert (
                    await service.scores("disabled", connection=old, texts=texts) == {}
                )
                connection = new
                assert (
                    await service.scores("cold new", connection=new, texts=(texts[0],))
                    == {}
                )
                release.set()
                await service._worker
                assert calls == [
                    (old.model_id, list(texts[:10])),
                    (new.model_id, [texts[0]]),
                ]
                old_keys = tuple(
                    mcp_tool_embedding_cache_key(old, text) for text in texts
                )
                assert cache.lookup(
                    old_keys, deadline_monotonic=monotonic() + 30
                ) == set(old_keys[:10])
                assert await service.scores(
                    "hot new", connection=new, texts=(texts[0],)
                ) == {texts[0]: 1.0}
            finally:
                await service.aclose()

    asyncio.run(exercise())


def test_stale_semaphore_waiter_never_sends_and_user_cancellation_escapes(monkeypatch):
    ranks = []

    class Cache:
        def lookup(self, keys, *, deadline_monotonic):
            return frozenset(keys)

        def rank(self, keys, vector, *, deadline_monotonic):
            ranks.append(keys)
            return dict.fromkeys(keys, 1.0)

    class ObservedGate(asyncio.Semaphore):
        async def acquire(self):
            if self.locked():
                queued.set()
            return await super().acquire()

    queued = asyncio.Event()

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        connection = _CONNECTION
        calls = []

        async def respond(request):
            body = json.loads(request.content)
            calls.append(body["input"])
            entered.set()
            await release.wait()
            return _vectors(body)

        _inject_transport(monkeypatch, respond)
        shared = ObservedGate(1)
        service = McpToolEmbeddings(
            Cache(), connection=lambda: connection, requests=shared
        )
        try:
            await shared.acquire()
            waiting = asyncio.create_task(
                service.scores(
                    "stale", connection=connection, texts=("tool\ndescription",)
                )
            )
            await asyncio.wait_for(queued.wait(), timeout=5)
            connection = None
            shared.release()
            assert await waiting == {}
            assert calls == []

            connection = _CONNECTION
            cancelled = asyncio.create_task(
                service.scores(
                    "cancelled", connection=connection, texts=("tool\ndescription",)
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert calls == [["cancelled"]]
            assert not shared.locked()

            entered.clear()
            admitted = asyncio.create_task(
                service.scores(
                    "admitted", connection=connection, texts=("tool\ndescription",)
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            connection = replace(_CONNECTION, api_key="rotated-credential")
            release.set()
            assert await admitted == {}
            assert calls == [["cancelled"], ["admitted"]]
            assert ranks == []
        finally:
            await service.aclose()

    asyncio.run(exercise())


def test_cache_and_embedding_failures_degrade_dense(monkeypatch):
    class Cache:
        lookup_fails = True

        def lookup(self, keys, *, deadline_monotonic):
            if self.lookup_fails:
                raise RuntimeError("cache lookup failed")
            return frozenset(keys)

        def rank(self, keys, vector, *, deadline_monotonic):
            raise RuntimeError("cache ranking failed")

    async def exercise():
        calls = []
        timed_out = asyncio.Event()

        async def respond(request):
            body = json.loads(request.content)
            calls.extend(body["input"])
            if body["input"] == ["rejected"]:
                return httpx2.Response(
                    503, json={"error": {"message": "test provider failure"}}
                )
            if body["input"] == ["timeout"]:
                try:
                    await asyncio.Future()
                finally:
                    timed_out.set()
            return _vectors(body)

        _inject_transport(monkeypatch, respond)
        cache = Cache()
        service = McpToolEmbeddings(
            cache, connection=lambda: _CONNECTION, requests=asyncio.Semaphore(5)
        )
        try:
            texts = ("tool\ndescription",)
            assert (
                await service.scores(
                    "cannot read cache", connection=_CONNECTION, texts=texts
                )
                == {}
            )
            assert calls == []
            cache.lookup_fails = False
            assert (
                await service.scores("rejected", connection=_CONNECTION, texts=texts)
                == {}
            )
            assert (
                await service.scores("cannot rank", connection=_CONNECTION, texts=texts)
                == {}
            )
            assert (
                await service.scores(
                    "timeout",
                    connection=_CONNECTION,
                    texts=texts,
                    deadline_monotonic=monotonic() + 0.5,
                )
                == {}
            )
            assert timed_out.is_set()
            assert calls == ["rejected", "cannot rank", "timeout"]
        finally:
            await service.aclose()

    asyncio.run(exercise())


def test_saved_credentials_are_excluded_at_document_and_wire_admission(
    monkeypatch, caplog
):
    saved = "fixture-other-model-credential"
    late = "fixture-new-oauth-credential"

    async def exercise():
        secrets = {saved}
        queued = asyncio.Event()
        rows = {}
        calls = []

        class Cache:
            def lookup(
                self,
                keys,
                *,
                deadline_monotonic,
                lane=PostgresConnectionLane.HOST_CONTROL,
            ):
                return frozenset(key for key in keys if key in rows)

            def insert(self, batch, *, deadline_monotonic):
                rows.update(batch)

        class ObservedGate(asyncio.Semaphore):
            async def acquire(self):
                if self.locked():
                    queued.set()
                return await super().acquire()

        def credentials():
            scrub = ProcessCredentialScrubSet()
            for credential in secrets:
                scrub.observe(credential)
            return scrub

        def respond(request):
            body = json.loads(request.content)
            calls.append(body["input"])
            if body["input"] == ["embedding failure"]:
                secrets.remove(saved)
                return httpx2.Response(
                    503,
                    json={
                        "error": {
                            "message": f"provider diagnostic {saved} remains useful"
                        }
                    },
                )
            return _vectors(body)

        _inject_transport(monkeypatch, respond)
        shared = ObservedGate(1)
        service = McpToolEmbeddings(
            Cache(),
            connection=lambda: _CONNECTION,
            requests=shared,
            credential_scrub_set=credentials,
        )
        try:
            await shared.acquire()
            clean = "clean_tool\nAn ordinary description"
            assert (
                await service.scores(
                    "cold",
                    connection=_CONNECTION,
                    texts=(f"saved\n{saved}", f"late\n{late}", clean),
                )
                == {}
            )
            await asyncio.wait_for(queued.wait(), timeout=5)
            secrets.add(late)
            shared.release()
            await service._worker
            assert calls == [[clean]]
            assert (
                await service.scores(
                    f"find {late}", connection=_CONNECTION, texts=(clean,)
                )
                == {}
            )
            assert calls == [[clean]]
            assert (
                await service.scores(
                    "embedding failure", connection=_CONNECTION, texts=(clean,)
                )
                == {}
            )
            assert calls == [[clean], ["embedding failure"]]
        finally:
            await service.aclose()

    caplog.set_level(
        "DEBUG", logger="pulsara_agent.conversation_kernel.mcp.tool_embeddings"
    )
    asyncio.run(exercise())
    assert saved not in caplog.text
    assert "provider diagnostic" in caplog.text and "remains useful" in caplog.text


def test_close_joins_an_admitted_write_and_drops_pending_batches(monkeypatch):
    async def exercise():
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        commit = Event()
        rows = {}
        calls = []
        lookup_lanes = []

        class Cache:
            def lookup(
                self,
                keys,
                *,
                deadline_monotonic,
                lane=PostgresConnectionLane.HOST_CONTROL,
            ):
                lookup_lanes.append(lane)
                return frozenset(key for key in keys if key in rows)

            def rank(self, keys, vector, *, deadline_monotonic):
                return dict.fromkeys(keys, 1.0)

            def insert(self, batch, *, deadline_monotonic):
                loop.call_soon_threadsafe(writing.set)
                assert commit.wait(timeout=5)
                rows.update(batch)

        def respond(request):
            body = json.loads(request.content)
            calls.append(body["input"])
            return _vectors(body)

        _inject_transport(monkeypatch, respond)
        cache = Cache()
        shared = asyncio.Semaphore(5)
        texts = tuple(f"tool_{index}\ndescription" for index in range(11))
        service = McpToolEmbeddings(
            cache, connection=lambda: _CONNECTION, requests=shared
        )
        try:
            assert (
                await service.scores("cold", connection=_CONNECTION, texts=texts) == {}
            )
            await asyncio.wait_for(writing.wait(), timeout=5)
            service.stop_admission()
            closing = asyncio.create_task(service.aclose())
            await asyncio.sleep(0)
            assert not service.physical_close_complete
            assert not closing.done()
            assert (
                await service.scores(
                    "during close", connection=_CONNECTION, texts=texts
                )
                == {}
            )
            commit.set()
            await closing
            assert service.physical_close_complete
            assert list(rows) == [
                mcp_tool_embedding_cache_key(_CONNECTION, text) for text in texts[:10]
            ]
            assert calls == [list(texts[:10])]
            assert lookup_lanes == [
                PostgresConnectionLane.HOST_CONTROL,
                PostgresConnectionLane.BACKGROUND_WORK,
            ]
        finally:
            commit.set()
            await service.aclose()

    asyncio.run(exercise())


def test_close_cancels_and_joins_an_admitted_query_embedding(monkeypatch):
    class Cache:
        def lookup(self, keys, *, deadline_monotonic):
            return frozenset(keys)

        def rank(self, keys, vector, *, deadline_monotonic):
            pytest.fail("a cancelled embedding request must not be ranked")

    async def exercise():
        entered, cleaning, released, exited = (asyncio.Event() for _ in range(4))

        async def respond(_request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await released.wait()
                exited.set()

        _inject_transport(monkeypatch, respond)
        shared = asyncio.Semaphore(1)
        service = McpToolEmbeddings(
            Cache(), connection=lambda: _CONNECTION, requests=shared
        )
        query = asyncio.create_task(
            service.scores("hot", connection=_CONNECTION, texts=("tool\ndescription",))
        )
        observed_cleanup = asyncio.create_task(cleaning.wait())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            closing = asyncio.create_task(service.aclose())
            done, _ = await asyncio.wait(
                (closing, observed_cleanup),
                timeout=5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert done == {observed_cleanup}
            assert not service.physical_close_complete
            assert shared.locked()
            query.cancel()
            await asyncio.sleep(0)
            assert not query.done()
            released.set()
            await closing
            with pytest.raises(asyncio.CancelledError):
                await query
            assert exited.is_set()
            assert service.physical_close_complete
            assert not shared.locked()
        finally:
            released.set()
            for task in (query, observed_cleanup):
                if not task.done():
                    task.cancel()
            await asyncio.gather(query, observed_cleanup, return_exceptions=True)
            await service.aclose()

    asyncio.run(exercise())
