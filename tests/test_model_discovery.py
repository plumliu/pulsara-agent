from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import gzip
import json

from aiohttp import web
import pytest

from pulsara_agent.llm import model_discovery as discovery


@asynccontextmanager
async def provider(handler):
    app = web.Application()
    app.router.add_get('/gateway/v1/models', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        yield f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/gateway/v1/'
    finally:
        await runner.cleanup()


def track_clients(monkeypatch):
    clients = []
    build = discovery.build_async_openai_client

    def tracked(**kwargs):
        client = build(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(discovery, 'build_async_openai_client', tracked)
    return clients


@pytest.mark.parametrize('key', [None, 'discovery-key-sentinel'])
def test_sdk_discovery_path_auth_ids_and_close(monkeypatch, key):
    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-secret-must-not-be-used')
    clients = track_clients(monkeypatch)
    observed = []

    async def handler(request):
        observed.append(request.headers.get('Authorization'))
        return web.json_response({'data': [{'id': 'z/model'}, {'id': 'a-model'}, {'id': 'z/model'}]})

    async def exercise():
        async with provider(handler) as url:
            assert await discovery.discover_model_ids(base_url=url, api_key=key) == ['z/model', 'a-model']
        assert observed == ([f'Bearer {key}'] if key else [None])
        assert len(clients) == 1 and clients[0].is_closed()

    asyncio.run(exercise())


@pytest.mark.parametrize('payload', [None, {'models': []}, {'data': {}}, {'data': [{'id': ''}]}, {'data': [{'name': 'model'}]}, {'data': [{'id': ' key-sentinel '}]}, {'data': [{'id': 'key-sentinel'}]}])
def test_invalid_list_never_exposes_key(monkeypatch, payload):
    clients = track_clients(monkeypatch)

    async def handler(request):
        return web.json_response(payload)

    async def exercise():
        async with provider(handler) as url:
            with pytest.raises(discovery.ModelDiscoveryFailure) as error:
                await discovery.discover_model_ids(base_url=url, api_key='key-sentinel')
            assert 'key-sentinel' not in str(error.value)
        assert clients[0].is_closed()

    asyncio.run(exercise())


@pytest.mark.parametrize('status', [401, 403, 404, 405, 429, 500])
def test_provider_failure_is_single_attempt_with_exact_secret_redaction(monkeypatch, status):
    clients = track_clients(monkeypatch)
    observed = []

    async def handler(request):
        observed.append(request)
        return web.json_response({'detail': 'use key-sentinel; diagnostic retained'}, status=status)

    async def exercise():
        async with provider(handler) as url:
            with pytest.raises(discovery.ModelDiscoveryFailure) as error:
                await discovery.discover_model_ids(base_url=url, api_key='key-sentinel')
            assert str(status) in str(error.value)
            assert 'diagnostic retained' in str(error.value)
            assert 'key-sentinel' not in str(error.value)
        assert len(observed) == 1 and clients[0].is_closed()

    asyncio.run(exercise())


def test_empty_list_and_compressed_resource_boundary(monkeypatch):
    clients = track_clients(monkeypatch)
    monkeypatch.setattr(discovery, 'MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES', 1000)
    oversized = False

    async def handler(request):
        if not oversized:
            return web.json_response({'data': []})
        content = gzip.compress(json.dumps({'data': [{'id': 'x' * 2000}]}).encode())
        return web.Response(body=content, headers={'Content-Encoding': 'gzip', 'Content-Type': 'application/json'})

    async def exercise():
        nonlocal oversized
        async with provider(handler) as url:
            assert await discovery.discover_model_ids(base_url=url, api_key=None) == []
            oversized = True
            with pytest.raises(discovery.ModelDiscoveryFailure, match='byte bound'):
                await discovery.discover_model_ids(base_url=url, api_key=None)
        assert all(client.is_closed() for client in clients)

    asyncio.run(exercise())


@pytest.mark.parametrize('cancel', [True, False])
def test_cancellation_and_timeout_close_client(monkeypatch, cancel):
    clients = track_clients(monkeypatch)

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            started.set()
            await release.wait()
            return web.json_response({'data': []})

        if not cancel:
            monkeypatch.setattr(discovery, '_DISCOVERY_TIMEOUT', replace(discovery._DISCOVERY_TIMEOUT, total_seconds=0.1))
        async with provider(handler) as url:
            task = asyncio.create_task(discovery.discover_model_ids(base_url=url, api_key=None))
            try:
                await started.wait()
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(discovery.ModelDiscoveryFailure, match='超时'):
                        await task
                assert clients[0].is_closed()
            finally:
                release.set()

    asyncio.run(exercise())
