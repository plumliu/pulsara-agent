"""ASR integration boundaries use the real SDK and credential-bound client."""

import asyncio
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from time import monotonic

import httpx2
import pytest

from pulsara_agent.media.asr import (
    AsrClient,
    AsrError,
    AsrRequestObservation,
    bounded_multipart_request_hook,
    validate_transcription,
)
from pulsara_agent.media.asr_config import AsrConnection
from pulsara_agent.settings import (
    LocalSettings,
    LocalSettingsStore,
    local_settings_from_dict,
    local_settings_to_dict,
)


CONFIG = AsrConnection(
    "https://asr.test/custom/transcriptions", "chosen-model", "protected-asr-test-key"
)
AUDIO = b"RIFF frozen WAV bytes"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def install_transport(monkeypatch, handler):
    import pulsara_agent.llm.adapters.openai.client as client

    original = client.ProcessCredentialBoundHttpx2Client
    monkeypatch.setattr(
        client,
        "ProcessCredentialBoundHttpx2Client",
        lambda **kwargs: original(**kwargs, transport=httpx2.MockTransport(handler)),
    )


async def transcribe(client=None, config=CONFIG, audio=AUDIO, **kwargs):
    return await (client or AsrClient()).transcribe_audio(
        config,
        audio,
        duration=2,
        maximum_upload_bytes=4096,
        maximum_response_bytes=4096,
        **kwargs,
    )


@pytest.mark.anyio
async def test_real_sdk_multipart_exact_endpoint_and_fields(monkeypatch):
    seen = []

    async def handler(request):
        seen.append(request)
        assert str(request.url) == CONFIG.endpoint
        assert request.headers["authorization"] == f"Bearer {CONFIG.api_key}"
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        parts = [
            (
                part.get_param("name", header="content-disposition"),
                part.get_payload(decode=True),
            )
            for part in message.iter_parts()
        ]
        assert ("model", b"chosen-model") in parts
        assert ("response_format", b"verbose_json") in parts
        assert ("timestamp_granularities[]", b"word") in parts
        assert ("timestamp_granularities[]", b"segment") in parts
        assert ("file", AUDIO) in parts
        return httpx2.Response(
            200,
            json={"text": "hello", "words": [{"word": "hello", "start": 0, "end": 1}]},
        )

    install_transport(monkeypatch, handler)
    result = await transcribe()
    assert result.granularity == "word"
    assert len(seen) == 1


@pytest.mark.parametrize("time", ["0", True, float("nan"), float("inf"), -1, 2])
def test_malformed_word_time_never_filters_partial_success(time):
    with pytest.raises(AsrError):
        validate_transcription(
            {"text": "hello", "words": [{"word": "hello", "start": time, "end": 1}]},
            duration=2,
        )


@pytest.mark.parametrize(
    "value",
    [
        {"text": "hello"},
        {"text": "", "words": None},
        {"output": {"text": "hi"}},
        {
            "text": "hello",
            "words": [
                {"word": "a", "start": 1, "end": 2},
                {"word": "b", "start": 0, "end": 1},
            ],
        },
        {"text": "hello", "segments": [{"text": "hello", "start": 1, "end": 3}]},
        {"text": "", "words": [{"word": "hi", "start": 0, "end": 1}]},
    ],
)
def test_response_rejects_unknown_wrappers_missing_or_invalid_times(value):
    with pytest.raises(AsrError):
        validate_transcription(value, duration=2)


def test_words_invalid_segments_valid_fallback_and_real_granularity():
    result = validate_transcription(
        {
            "text": "hello",
            "words": [{"word": "hello", "start": False, "end": 1}],
            "segments": [{"text": "hello", "start": 0, "end": 2, "speaker": "A"}],
        },
        duration=2,
    )
    assert result.granularity == "segment" and result.items[0].speaker == "A"
    assert result.diagnostics
    assert (
        validate_transcription(
            {"text": "", "words": [], "segments": []}, duration=2
        ).granularity
        is None
    )


@pytest.mark.anyio
@pytest.mark.parametrize("slot", ["audio", "filename", "endpoint"])
async def test_protected_values_rejected_before_physical_send(monkeypatch, slot):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={"text": ""})

    install_transport(monkeypatch, handler)
    if slot == "endpoint":
        with pytest.raises(ValueError, match="公开配置"):
            replace(CONFIG, endpoint=CONFIG.endpoint + "/" + CONFIG.api_key)
        assert seen == []
        return
    kwargs = (
        {"audio": CONFIG.api_key.encode()}
        if slot == "audio"
        else (
            {"filename": CONFIG.api_key + ".wav"}
            if slot == "filename"
            else {
                "config": replace(
                    CONFIG, endpoint=CONFIG.endpoint + "/" + CONFIG.api_key
                )
            }
        )
    )
    with pytest.raises(AsrError):
        await transcribe(**kwargs)
    assert seen == []


@pytest.mark.anyio
async def test_bounded_hook_rejects_unknown_and_oversized_stream_before_read():
    for headers in ({}, {"content-length": "99999"}):
        reads = []

        async def stream():
            reads.append(True)
            yield AUDIO

        request = httpx2.Request(
            "POST", CONFIG.endpoint, headers=headers, content=stream()
        )
        with pytest.raises(AsrError):
            await bounded_multipart_request_hook(audio=AUDIO, maximum_bytes=100)(
                request
            )
        assert reads == []


@pytest.mark.anyio
async def test_unknown_acceptance_and_rate_limit_never_auto_post_again(monkeypatch):
    seen = []

    async def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx2.ReadTimeout("upload already sent", request=request)
        return httpx2.Response(
            429, headers={"Retry-After": "0"}, json={"error": {"message": "rate limit"}}
        )

    install_transport(monkeypatch, handler)
    with pytest.raises(AsrError) as caught:
        await transcribe()
    assert caught.value.remote_result_unknown
    assert len(seen) == 1
    with pytest.raises(AsrError):
        await transcribe()
    assert len(seen) == 2


@pytest.mark.anyio
async def test_openrouter_explicit_limit_retries_only_same_audio_after_wait(monkeypatch):
    config = replace(CONFIG, endpoint="https://openrouter.ai/api/v1/audio/transcriptions")
    seen = []
    observation = AsrRequestObservation()

    async def handler(request):
        assert AUDIO in request.content
        seen.append(monotonic())
        if len(seen) == 1:
            return httpx2.Response(429, headers={"Retry-After": "1"}, json={
                "error": {"message": "Provider returned 429", "code": 429},
            })
        return httpx2.Response(200, json={
            "text": "yes", "words": [{"start": 0, "end": 1, "word": "yes"}],
        })

    install_transport(monkeypatch, handler)
    result = await transcribe(config=config, deadline_monotonic=monotonic() + 10,
                              observation=observation)
    assert result.text == "yes" and len(seen) == 2 and seen[1] - seen[0] >= 1
    assert [attempt.response_status for attempt in observation.attempts] == [429, 200]
    assert observation.attempts[0].confirmed_rate_limit
    assert not observation.attempts[0].confirmed_not_sent
    assert not observation.remote_result_unknown


@pytest.mark.anyio
@pytest.mark.parametrize(("endpoint", "error", "unknown"), [
    (CONFIG.endpoint, {"code": 429, "message": "Provider returned 429"}, True),
    ("https://openrouter.ai/api/v1/audio/transcriptions", {}, True),
    ("https://openrouter.ai/api/v1/audio/transcriptions",
     {"code": 429, "message": "Provider returned 429"}, False),
])
async def test_ambiguous_limit_or_retry_beyond_deadline_stops(monkeypatch, endpoint, error, unknown):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx2.Response(429, headers={"Retry-After": "3600"}, json={"error": error})

    install_transport(monkeypatch, handler)
    with pytest.raises(AsrError) as caught:
        await transcribe(config=replace(CONFIG, endpoint=endpoint), deadline_monotonic=monotonic() + 5)
    assert len(seen) == 1
    assert caught.value.code == "ASR_RATE_LIMITED"
    assert caught.value.remote_result_unknown is unknown


@pytest.mark.anyio
async def test_cancel_during_rate_limit_wait_releases_quota(monkeypatch):
    config = replace(CONFIG, endpoint="https://openrouter.ai/api/v1/audio/transcriptions")
    client = AsrClient()
    observation = AsrRequestObservation()
    sent = []

    async def handler(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx2.Response(429, headers={"Retry-After": "60"}, json={
                "error": {"message": "Provider returned 429", "code": 429},
            })
        return httpx2.Response(200, json={
            "text": "yes", "words": [{"start": 0, "end": 1, "word": "yes"}],
        })

    install_transport(monkeypatch, handler)
    task = asyncio.create_task(transcribe(client, config, observation=observation,
                                          deadline_monotonic=monotonic() + 120))
    async with asyncio.timeout(5):
        while not observation.attempts or not observation.attempts[0].confirmed_rate_limit:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent) == 1
    async with asyncio.timeout(5):
        assert (await transcribe(client, config)).text == "yes"
    assert len(sent) == 2


@pytest.mark.anyio
async def test_cancel_waiter_and_running_request_release_gate(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    seen = []

    async def handler(request):
        seen.append(request)
        if len(seen) == 1:
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return httpx2.Response(200, json={"text": ""})

    install_transport(monkeypatch, handler)
    client = AsrClient()
    running = asyncio.create_task(transcribe(client))
    await started.wait()
    waiting = asyncio.create_task(transcribe(client))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert len(seen) == 1
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled.is_set()
    assert (await transcribe(client)).text == ""
    assert len(seen) == 2


@pytest.mark.anyio
async def test_independent_settings_slot_and_endpoint_bound_secret(tmp_path):
    path = tmp_path / "settings.yaml"
    store = LocalSettingsStore(path)
    body = {
        "connection": {"endpoint": CONFIG.endpoint, "model_id": CONFIG.model_id},
        "key_action": "replace",
        "api_key": CONFIG.api_key,
    }
    saved = await store.save_asr_connection(body)
    assert saved.asr == CONFIG
    assert CONFIG.api_key not in str(saved.asr.public())
    assert saved.credential_scrub_set().contains(CONFIG.api_key)
    assert local_settings_from_dict(local_settings_to_dict(saved)).asr == CONFIG
    kept = await store.save_asr_connection(
        {"connection": body["connection"], "key_action": "keep"}
    )
    assert kept.asr == CONFIG
    with pytest.raises(ValueError, match="地址已改变"):
        await store.save_asr_connection(
            {
                "connection": {
                    **body["connection"],
                    "endpoint": "https://other.test/transcriptions",
                },
                "key_action": "keep",
            }
        )
    assert (await store.clear_asr_connection()).asr is None
    assert LocalSettings().asr is None


@pytest.mark.anyio
async def test_real_loopback_http_multipart_and_cancel_during_upload():
    from aiohttp import web

    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    received = []

    async def handler(request):
        if request.path == "/cancel":
            count = len(await request.content.read(100))
            started.set()
            await release.wait()
            try:
                while chunk := await request.content.read(65536):
                    count += len(chunk)
            except ConnectionResetError:
                pass
            received.append(count)
            settled.set()
            return web.json_response({"text": ""})
        parts = []
        reader = await request.multipart()
        while part := await reader.next():
            parts.append((part.name, bytes(await part.read())))
        assert ("file", AUDIO) in parts
        assert ("response_format", b"verbose_json") in parts
        return web.json_response(
            {"text": "hello", "segments": [{"text": "hello", "start": 0, "end": 1}]}
        )

    app = web.Application()
    app.router.add_post("/exact/path", handler)
    app.router.add_post("/cancel", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    connection = replace(CONFIG, endpoint=f"http://127.0.0.1:{port}/exact/path")
    client = AsrClient()
    try:
        assert (await transcribe(client, config=connection)).granularity == "segment"
        # A body larger than socket buffers makes cancellation an actual upload
        # interruption, while the server intentionally stops consuming it.
        audio = b"large admitted audio" * 500_000
        task = asyncio.create_task(
            client.transcribe_audio(
                replace(connection, endpoint=f"http://127.0.0.1:{port}/cancel"),
                audio,
                duration=2,
                maximum_upload_bytes=len(audio) + 4096,
                maximum_response_bytes=4096,
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(settled.wait(), 5)
        assert received[0] < len(audio)
        assert (await transcribe(client, config=connection)).text == "hello"
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.anyio
async def test_all_saved_secrets_scrub_and_reject(monkeypatch):
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet

    scrub = ProcessCredentialScrubSet()
    scrub.observe("different-saved-key")
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "text": "different-saved-key",
                "words": [{"word": "different-saved-key", "start": 0, "end": 1}],
            },
        )

    install_transport(monkeypatch, handler)
    with pytest.raises(AsrError) as exc:
        await transcribe(audio=b"audio different-saved-key", credential_scrub_set=scrub)
    assert not exc.value.remote_result_unknown and not seen
    result = await transcribe(credential_scrub_set=scrub)
    assert "different-saved-key" not in result.text


@pytest.mark.anyio
async def test_asr_user_settings_http_has_no_active_session_dependency(
    tmp_path, monkeypatch
):
    from aiohttp import ClientSession
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.desktop_app.http_server import LocalHttpServer
    from pulsara_agent.media.asr import AsrTranscript, AsrTimestamp
    from tests.test_desktop_http_surface import (
        _Sessions,
        _Bridge,
        _model_server_dependencies,
    )
    import pulsara_agent.media.asr as asr_module

    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("Pulsara")
    store = LocalSettingsStore(tmp_path / "home" / "local-settings.yaml")
    sessions = _Sessions()
    clear = AsyncMock(
        return_value={"deleted_blocks": 0, "message": "暂无可清理的旧转写缓存"}
    )
    sessions.core = SimpleNamespace(clear_old_asr_cache=clear)
    deps = _model_server_dependencies()
    deps["settings"] = store
    test = AsyncMock(
        return_value=AsrTranscript("hello", (AsrTimestamp(0, 1, "hello"),), "word")
    )
    monkeypatch.setattr(asr_module, "transcribe_audio", test)
    server = LocalHttpServer(
        sessions=sessions,
        bridge=_Bridge(),
        static_root=root,
        requested_port=0,
        is_ready=lambda: True,
        is_draining=lambda: False,
        **deps,
    )
    await server.start()
    try:
        async with ClientSession(
            headers={
                "X-Pulsara-Connection-Id": "connection-1",
                "X-Pulsara-Connection-Generation": "1",
            }
        ) as client:
            base = server.origin + "/api/local-settings/asr"
            async with client.get(base) as response:
                assert response.status == 200 and await response.json() is None
            body = {
                "connection": {
                    "endpoint": CONFIG.endpoint,
                    "model_id": CONFIG.model_id,
                },
                "key_action": "replace",
                "api_key": CONFIG.api_key,
            }
            async with client.put(base, json=body) as response:
                assert response.status == 200
                assert CONFIG.api_key not in await response.text()
            test.assert_not_awaited()
            before = store.read()
            async with client.post(
                base + "/test",
                json={"connection": body["connection"], "key_action": "keep"},
            ) as response:
                assert response.status == 200
                assert (await response.json())["granularity"] == "word"
            test.assert_awaited_once()
            assert store.read() == before
            test.return_value = AsrTranscript("", (), None)
            async with client.post(
                base + "/test",
                json={"connection": body["connection"], "key_action": "keep"},
            ) as response:
                assert response.status == 400
            async with client.post(base + "/clear-old-cache") as response:
                assert response.status == 200
                assert (await response.json())["deleted_blocks"] == 0
            clear.assert_awaited_once()
            async with client.delete(base) as response:
                assert response.status == 200 and await response.json() is None
            assert store.read().asr is None
    finally:
        await server.aclose()


@pytest.mark.anyio
async def test_physical_quota_precedes_audio_preparation(monkeypatch):
    from types import SimpleNamespace

    started = asyncio.Event()
    release = asyncio.Event()
    prepared = []

    async def handler(request):
        started.set()
        await release.wait()
        return httpx2.Response(200, json={"text": ""})

    install_transport(monkeypatch, handler)

    async def prepare_audio():
        prepared.append(True)
        return SimpleNamespace(
            data=AUDIO, start=0, end=2, filename="audio.wav", mime_type="audio/wav"
        )

    client = AsrClient()
    running = asyncio.create_task(transcribe(client))
    await started.wait()
    waiting = asyncio.create_task(
        client.prepare_and_transcribe(
            CONFIG,
            prepare_audio,
            maximum_upload_bytes=4096,
            maximum_response_bytes=4096,
        )
    )
    await asyncio.sleep(0)
    assert prepared == []
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert prepared == []
    release.set()
    await running
    audio, result = await client.prepare_and_transcribe(
        CONFIG, prepare_audio, maximum_upload_bytes=4096, maximum_response_bytes=4096
    )
    assert prepared == [True] and audio.data == AUDIO and result.text == ""


@pytest.mark.anyio
async def test_real_connect_failure_retries_only_before_send_with_original_deadline(
    monkeypatch,
):
    import socket
    from time import monotonic
    from aiohttp import web
    import pulsara_agent.llm.retry as retry

    retrying = asyncio.Event()
    original_delay = retry.compute_retry_delay

    def observe_delay(**kwargs):
        retrying.set()
        return original_delay(**kwargs)

    monkeypatch.setattr(retry, "compute_retry_delay", observe_delay)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    connection = replace(CONFIG, endpoint=f"http://127.0.0.1:{port}/speech")
    accepted = []

    async def handler(request):
        accepted.append(await request.read())
        return web.json_response({"text": ""})

    app = web.Application()
    app.router.add_post("/speech", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    client = AsrClient()
    task = asyncio.create_task(
        transcribe(client, config=connection, deadline_monotonic=monotonic() + 5)
    )
    try:
        await asyncio.wait_for(retrying.wait(), 2)
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        result = await asyncio.wait_for(task, 5)
        assert result.text == "" and len(accepted) == 1 and AUDIO in accepted[0]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()


@pytest.mark.anyio
async def test_cancel_during_confirmed_connect_retry_wait_sends_no_next_request(
    monkeypatch,
):
    from time import monotonic
    import pulsara_agent.llm.retry as retry

    waiting = asyncio.Event()
    seen = []
    original_delay = retry.compute_retry_delay

    def observe_delay(**kwargs):
        waiting.set()
        return original_delay(**kwargs)

    monkeypatch.setattr(retry, "compute_retry_delay", observe_delay)

    async def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx2.ConnectError("connection was not established", request=request)
        return httpx2.Response(200, json={"text": ""})

    install_transport(monkeypatch, handler)
    client = AsrClient()
    task = asyncio.create_task(transcribe(client, deadline_monotonic=monotonic() + 5))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) == 1
    assert (await transcribe(client)).text == ""
    assert len(seen) == 2


def test_public_configuration_rejects_own_and_other_saved_secrets():
    for field in ("endpoint", "model_id"):
        with pytest.raises(ValueError, match="公开配置"):
            replace(CONFIG, **{field: getattr(CONFIG, field) + CONFIG.api_key})
        current = LocalSettings(asr=CONFIG)
        connection = {"endpoint": CONFIG.endpoint, "model_id": CONFIG.model_id}
        connection[field] += CONFIG.api_key
        with pytest.raises(ValueError, match="已保存凭据"):
            LocalSettingsStore.asr_draft(
                current,
                {
                    "connection": connection,
                    "key_action": "replace",
                    "api_key": "different-new-key",
                },
            )
