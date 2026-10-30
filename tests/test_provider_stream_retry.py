"""Real SDK + local HTTP/SSE exercise the logical request recovery boundary."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import random
import re
import socket
import ssl
from threading import Thread, Event as ThreadEvent

import pytest

from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.adapters.openai.errors import (
    classify_llm_error,
    parse_retry_after_seconds,
)
from pulsara_agent.llm.adapters.openai.retrying import _safe_host, _seconds_to_millis
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import resolve_model_call
from pulsara_agent.llm.retry import (
    LLMRetryConfig,
    RetryDecisionKind,
    compute_retry_delay,
    retry_config_for_purpose,
)
from pulsara_agent.ports.provider_stream import (
    ProviderGenerationRetry,
    ProviderNormalizedTerminalKind,
    ProviderRetryPhase,
    ProviderRetryReason,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ProviderModelStreamErrorCode,
)
from tests.support.model_config import test_model_runtime, test_model_binding

APIS = ("openai_chat_completions", "openai_responses")


def chat(delta, finish=None):
    return {
        "id": "fixture",
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def partial(api, kind="text"):
    if api == APIS[0]:
        delta = (
            {"content": "DISCARDED"}
            if kind == "text"
            else (
                {"reasoning_content": "DISCARDED"}
                if kind == "thinking"
                else {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "discarded-call",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":'},
                        }
                    ]
                }
            )
        )
        return [chat(delta)]
    if kind == "tool":
        return [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "discarded-item",
                    "type": "function_call",
                    "call_id": "discarded-call",
                    "name": "write_file",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "discarded-item",
                "delta": '{"path":',
            },
        ]
    if kind == "thinking":
        return [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "discarded-item", "type": "reasoning", "summary": []},
            },
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "summary_index": 0,
                "delta": "DISCARDED",
            },
        ]
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "discarded-item",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "DISCARDED",
        },
    ]


def success(api):
    if api == APIS[0]:
        return [chat({"content": "ACCEPTED"}), chat({}, "stop")]
    return [
        {
            "type": "response.completed",
            "response": {
                "id": "accepted-response",
                "model": "test-model",
                "status": "completed",
                "output": [
                    {
                        "id": "accepted-item",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "ACCEPTED",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        }
    ]


@contextmanager
def server(scripts):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            index = len(requests)
            requests.append(
                json.loads(body)
                if body
                else {"method": self.command, "path": self.path}
            )
            script = scripts[index % len(scripts)]
            if isinstance(script, int):
                script = {"status": script}
            status = script.get("status", 200)
            self.send_response(status)
            self.send_header("Connection", "close")
            for key, value in script.get("headers", {}).items():
                self.send_header(key, value)
            if status != 200:
                data = json.dumps(
                    script.get(
                        "body",
                        {
                            "error": {
                                "message": "temporary upstream failure",
                                "code": "server_error",
                            }
                        },
                    )
                ).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
                self.close_connection = True
                return
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event in script.get("events", []):
                payload = ("data: " + json.dumps(event) + "\n\n").encode()
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
            if script.get("hold") is not None:
                script["hold"].wait()
            if not script.get("cut"):
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_WR)

    Handler.do_GET = Handler.do_POST
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/v1", requests
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def execution(
    api,
    endpoint,
    *,
    sleep=None,
    observer=None,
    profile=None,
    purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    key="fixture-api-key",
):
    runtime = test_model_runtime(
        base_url=endpoint, wire_api=api, route_wire_profile=profile, api_key=key
    )
    binding = test_model_binding(runtime)
    target = runtime.resolve_target(
        binding, timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, None)
    )
    call = resolve_model_call(target=target, binding=binding, purpose=purpose)
    context = LLMContext(
        messages=(LLMMessage.user("Write a response."),),
        tools=(
            ToolSpec(
                "write_file",
                "write",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        context_id="fixture:retry",
        resolved_model_call_id=call.resolved_model_call_id,
        model_call_index=0 if purpose is ModelCallPurpose.AGENT_MODEL_LOOP else None,
        retry_observer=observer,
    )
    adapter = target.transport._adapter
    adapter.retry_rng = random.Random(0)
    delays = []

    async def fast_sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    adapter.retry_sleep = sleep or fast_sleep
    return (
        target.transport.open_stream(call=call, context=context),
        delays,
        adapter,
        context,
        call,
    )


async def drain(value):
    items = []
    while (item := await value.read_next()) is not None:
        items.append(item)
    return items


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("kind", ("text", "thinking", "tool"))
def test_partial_stream_replaced_after_real_sdk_close(api, kind, monkeypatch, caplog):
    import openai

    original_close = openai.AsyncStream.close
    closed = []

    async def close(stream):
        await original_close(stream)
        closed.append(stream)

    monkeypatch.setattr(openai.AsyncStream, "close", close)
    updates = []
    with server(
        [{"events": partial(api, kind), "cut": True}, {"events": success(api)}]
    ) as (endpoint, requests):
        value, delays, _, _, _ = execution(api, endpoint, observer=updates.append)

        async def run():
            items = []
            while (item := await value.read_next()) is not None:
                if isinstance(item, ProviderGenerationRetry):
                    assert len(closed) == 1 and len(requests) == 1
                    assert item.reason is ProviderRetryReason.CONNECTION_RECOVERY
                items.append(item)
            return items

        items = asyncio.run(run())
    assert items[-1].terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len(closed) == 2 and len(delays) == 1
    retry_index = next(
        i for i, item in enumerate(items) if isinstance(item, ProviderGenerationRetry)
    )
    assert "DISCARDED" not in str(items[retry_index + 1 :])
    assert "discarded-call" not in str(items[-1].completed_replay_payload)
    assert "ACCEPTED" in str(items[retry_index + 1 :])
    assert [u.phase for u in updates if u is not None] == [
        ProviderRetryPhase.WAITING,
        ProviderRetryPhase.RECONNECTING,
    ]
    assert updates[-1] is None
    assert (
        '"attempt": 2' in caplog.text
        and '"retry_count": 1' in caplog.text
        and '"decision": "success"' in caplog.text
    )


@pytest.mark.parametrize("api", APIS)
def test_six_real_sdk_attempts_five_waits_and_exact_exhaustion(api, caplog):
    with server([{"events": partial(api), "cut": True}]) as (endpoint, requests):
        value, delays, _, _, _ = execution(api, endpoint)
        items = asyncio.run(drain(value))
    final = items[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert final.error.code is ProviderModelStreamErrorCode.PROVIDER_CONNECTION_ERROR
    summary = final.error.retry_summary
    assert (
        summary.final_attempt,
        summary.max_attempts,
        summary.retry_count,
        summary.exhausted,
    ) == (6, 6, 5, True)
    assert len(requests) == 6 and len(delays) == 5
    assert all(body == requests[0] for body in requests)
    assert all(
        base * 0.8 <= delay <= base * 1.2
        for base, delay in zip((2, 4, 8, 16, 30), delays, strict=True)
    )
    assert [trace.attempt for trace in summary.attempts] == [1, 2, 3, 4, 5]
    assert '"skipped_reason": "attempts_exhausted"' in caplog.text


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("succeeded_on", (2, 4, 6))
def test_success_on_first_middle_and_last_retry(api, succeeded_on):
    with server([*([503] * (succeeded_on - 1)), {"events": success(api)}]) as (
        endpoint,
        requests,
    ):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(requests) == succeeded_on and len(waits) == succeeded_on - 1


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "extra",
    (
        {"tools": [{"type": "web_search_preview"}]},
        {"background": True},
        {"provider_actions": True},
    ),
)
@pytest.mark.parametrize("has_output", (False, True))
def test_effective_extra_body_blocks_unsafe_opening_and_partial_replay(
    api, extra, has_output
):
    profile = RouteWireProfile(wire_api=api, request_extra_body=extra)
    with server([{"events": partial(api) if has_output else [], "cut": True}]) as (
        endpoint,
        requests,
    ):
        value, waits, _, _, _ = execution(api, endpoint, profile=profile)
        final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert final.error.retry_summary.skipped_reason == "unsafe_to_replay"
    assert len(requests) == 1 and waits == []


@pytest.mark.parametrize("api", APIS)
def test_clean_eof_is_protocol_error_without_retry(api):
    with server([{"events": partial(api)}]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.error.code is ProviderModelStreamErrorCode.TRANSPORT_PROTOCOL_ERROR
    assert len(requests) == 1 and waits == []


@pytest.mark.parametrize("api", APIS)
def test_terminal_then_close_failure_never_replays_or_accepts(api, monkeypatch, caplog):
    import openai

    original = openai.AsyncStream.close

    async def failed_close(stream):
        await original(stream)
        raise ConnectionError("concrete cleanup failure")

    monkeypatch.setattr(openai.AsyncStream, "close", failed_close)
    with server([{"events": success(api)}]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert "已收到结果，但连接收尾失败" in final.error.message
    assert final.error.retry_summary.skipped_reason == "completed_stream_close_failed"
    assert final.error.retry_summary.retry_count == 0
    assert len(requests) == 1 and waits == []
    assert "concrete cleanup failure" in caplog.text


@pytest.mark.parametrize("api", APIS)
def test_partial_failure_and_close_error_both_retained(api, monkeypatch, caplog):
    import openai

    original = openai.AsyncStream.close

    async def failed_close(stream):
        await original(stream)
        raise RuntimeError("specific close failure")

    monkeypatch.setattr(openai.AsyncStream, "close", failed_close)
    with server([{"events": partial(api), "cut": True}]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.error.retry_summary.skipped_reason == "physical_close_unconfirmed"
    assert len(requests) == 1 and waits == []
    assert (
        "specific close failure" in caplog.text and "RemoteProtocolError" in caplog.text
    )


@pytest.mark.parametrize("api", APIS)
def test_real_cancellable_wait_sends_no_next_attempt(api, caplog):
    updates = []
    with server([503]) as (endpoint, requests):

        async def run():
            waiting = asyncio.Event()

            def observer(update):
                updates.append(update)
                if update is not None and update.phase is ProviderRetryPhase.WAITING:
                    waiting.set()

            value, _, _, _, _ = execution(
                api, endpoint, sleep=asyncio.sleep, observer=observer
            )
            task = asyncio.create_task(drain(value))
            await asyncio.wait_for(waiting.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await value.aclose()

        asyncio.run(run())
    assert len(requests) == 1 and updates[-1] is None
    assert (
        '"phase": "waiting"' in caplog.text
        and '"retry_count": 0' in caplog.text
        and '"decision": "cancelled"' in caplog.text
    )


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "status,body",
    (
        (401, None),
        (403, None),
        (400, None),
        (429, {"error": {"code": "insufficient_quota", "message": "quota exhausted"}}),
    ),
)
def test_deterministic_http_errors_do_not_retry(api, status, body):
    with server([{"status": status, **({"body": body} if body else {})}]) as (
        endpoint,
        requests,
    ):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and waits == []
    assert final.error.retry_summary.retry_count == 0


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("hint", (31, 601, 1e300))
def test_oversized_retry_after_stops_and_cannot_misreport_600_seconds(api, hint):
    with server([{"status": 429, "headers": {"Retry-After": str(hint)}}]) as (
        endpoint,
        requests,
    ):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and waits == []
    assert final.error.retry_summary.final_reason == "retry_after_exceeded"
    assert final.error.retry_summary.retry_after_exceeded
    assert _seconds_to_millis(hint) == (hint * 1000 if hint <= 600 else None)


def test_retry_after_parsing_and_jitter_boundaries():
    config = retry_config_for_purpose(None, ModelCallPurpose.AGENT_MODEL_LOOP)
    for value in ("NaN", "Infinity", "-1", "invalid"):
        assert parse_retry_after_seconds(headers={"Retry-After": value}) is None
    assert parse_retry_after_seconds(headers={"Retry-After": "0"}) == 0
    assert parse_retry_after_seconds(message="retry after -1 seconds") is None
    assert parse_retry_after_seconds(message="retry after 1e300 seconds") == 1e300
    future = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True
    )
    assert 8 <= parse_retry_after_seconds(headers={"Retry-After": future}) <= 10
    for index, base in enumerate((2, 4, 8, 16, 30), 1):
        for seed in range(20):
            assert (
                0.8 * base
                <= compute_retry_delay(
                    attempt_index=index,
                    config=config,
                    retry_after_seconds=None,
                    rng=random.Random(seed),
                )
                <= 1.2 * base
            )
            assert (
                base
                <= compute_retry_delay(
                    attempt_index=index,
                    config=config,
                    retry_after_seconds=0,
                    rng=random.Random(seed),
                )
                <= 1.2 * base
            )
            assert (
                max(base, 20)
                <= compute_retry_delay(
                    attempt_index=index,
                    config=config,
                    retry_after_seconds=20,
                    rng=random.Random(seed),
                )
                <= max(base, 20) * 1.2
            )


def test_classification_is_typed_and_deterministic_configuration_wins():
    import httpx2

    for cause in (
        ssl.SSLCertVerificationError("certificate verify failed"),
        httpx2.UnsupportedProtocol("ftp URL"),
        httpx2.InvalidURL("bad URL"),
    ):
        error = ConnectionError("Connection error")
        error.__cause__ = cause
        assert classify_llm_error(error).kind is RetryDecisionKind.DO_NOT_RETRY
    for error in (
        ValueError("programming timeout ssl"),
        RuntimeError("random failure"),
    ):
        assert classify_llm_error(error).kind is RetryDecisionKind.DO_NOT_RETRY
    root = ConnectionError("reset")
    root.__cause__ = root
    assert classify_llm_error(root).kind is RetryDecisionKind.RETRY
    assert (
        _safe_host("https://username:secret@example.test/path?token=secret")
        == "example.test"
    )


def test_purpose_defaults_are_scoped():
    assert (
        retry_config_for_purpose(None, ModelCallPurpose.AGENT_MODEL_LOOP).attempts == 6
    )
    for purpose in ModelCallPurpose:
        if purpose is not ModelCallPurpose.AGENT_MODEL_LOOP:
            assert retry_config_for_purpose(None, purpose) == LLMRetryConfig()


@pytest.mark.parametrize("api", APIS)
def test_temporary_sse_failure_is_replaced_before_normalized_terminal(api):
    error = {
        "code": "server_error",
        "message": "temporary provider failure, preserve this context",
    }
    failure = (
        {"error": error}
        if api == APIS[0]
        else {
            "type": "response.failed",
            "response": {"id": "failed-response", "status": "failed", "error": error},
        }
    )
    with server([{"events": [*partial(api), failure]}, {"events": success(api)}]) as (
        endpoint,
        requests,
    ):
        value, waits, _, _, _ = execution(api, endpoint)
        output = asyncio.run(drain(value))
    assert len(requests) == 2 and len(waits) == 1
    assert output[-1].terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert sum(isinstance(item, ProviderGenerationRetry) for item in output) == 1
    assert all(not hasattr(item, "terminal_kind") for item in output[:-1])


def malformed(api):
    if api == APIS[0]:
        return [
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "discarded-call",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":'},
                        }
                    ]
                }
            ),
            chat({}, "tool_calls"),
        ]
    return [
        {
            "type": "response.completed",
            "response": {
                "id": "invalid-response",
                "status": "completed",
                "model": "test-model",
                "output": [
                    {
                        "id": "invalid-item",
                        "type": "function_call",
                        "call_id": "discarded-call",
                        "name": "write_file",
                        "arguments": '{"path":',
                    }
                ],
            },
        }
    ]


@pytest.mark.parametrize("api", APIS)
def test_opening_partial_and_tool_regeneration_share_six_attempts(api):
    scripts = [
        503,
        {"events": partial(api), "cut": True},
        {"events": malformed(api)},
        503,
        {"events": partial(api), "cut": True},
        {"events": success(api)},
    ]
    with server(scripts) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        items = asyncio.run(drain(value))
    assert items[-1].terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(requests) == 6 and len(waits) == 5
    reasons = [
        item.reason for item in items if isinstance(item, ProviderGenerationRetry)
    ]
    assert reasons == [
        ProviderRetryReason.PROVIDER_RECOVERY,
        ProviderRetryReason.CONNECTION_RECOVERY,
        ProviderRetryReason.TOOL_ARGUMENT_REGENERATION,
        ProviderRetryReason.PROVIDER_RECOVERY,
        ProviderRetryReason.CONNECTION_RECOVERY,
    ]
    assert all(body == requests[0] for body in requests)


@pytest.mark.parametrize("api", APIS)
def test_cancellation_during_physical_close_is_joined_and_cannot_retry(
    api, monkeypatch
):
    import openai

    original = openai.AsyncStream.close
    with server([{"events": partial(api), "cut": True}]) as (endpoint, requests):

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            physically_closed = []

            async def close(stream):
                entered.set()
                await release.wait()
                await original(stream)
                physically_closed.append(stream)

            monkeypatch.setattr(openai.AsyncStream, "close", close)
            value, _, _, _, _ = execution(api, endpoint)
            task = asyncio.create_task(drain(value))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not physically_closed
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(physically_closed) == 1
            await value.aclose()

        asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("api", APIS)
def test_retry_after_wait_is_never_earlier_than_server_floor(api):
    with server(
        [{"status": 429, "headers": {"Retry-After": "20"}}, {"events": success(api)}]
    ) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(requests) == 2 and 20 <= waits[0] <= 24


@pytest.mark.parametrize("api", APIS)
def test_usage_and_model_identity_are_replaced_with_attempt(api):
    if api == APIS[0]:
        first = [chat({"content": "DISCARDED"})]
        first[0]["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }
        second = [
            *success(api),
            {
                "choices": [],
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
        ]
    else:
        first = [
            {
                "type": "response.created",
                "response": {
                    "id": "bad",
                    "model": "test-model",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "total_tokens": 300,
                    },
                },
            },
            *partial(api),
        ]
        second = success(api)
        second[0]["response"]["usage"] = {
            "input_tokens": 11,
            "output_tokens": 22,
            "total_tokens": 33,
        }
    with server([{"events": first, "cut": True}, {"events": second}]) as (endpoint, _):
        value, _, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert (
        final.usage.usage.input_tokens == 11 and final.usage.usage.output_tokens == 22
    )


@pytest.mark.parametrize("api", APIS)
def test_six_attempt_budget_is_fresh_for_more_than_six_logical_requests(api):
    with server([503, {"events": success(api)}]) as (endpoint, requests):

        async def run():
            for _ in range(7):
                value, waits, _, _, _ = execution(api, endpoint)
                assert (await drain(value))[
                    -1
                ].terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
                assert len(waits) == 1

        asyncio.run(run())
    assert len(requests) == 14


@pytest.mark.parametrize("api", APIS)
def test_final_serialized_logs_and_error_scrub_exact_credentials(
    api, monkeypatch, caplog
):
    # Real credentials and their representations are scrubbed before clipping
    # and again after serialization; ordinary error context remains inspectable.
    caplog.set_level("DEBUG")
    secret = 'key-"quoted"-\\slash'
    proxy_secret = "ProxyP@ss"
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:ProxyP%40ss@127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://proxy-user:ProxyP%40ss@127.0.0.1:9")
    # localhost stays on httpx's ordinary no-proxy route.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
    encoded = quote(secret, safe="")
    body = {
        "error": {
            "code": "authentication",
            "message": f"normal diagnostic body | {secret} | {escaped} | {encoded} | {proxy_secret} | http://proxy-user:ProxyP%40ss@proxy.test/path",
        }
    }
    scrubbers = []
    with server([{"status": 401, "body": body}]) as (endpoint, requests):
        value, _, adapter, context, call = execution(api, endpoint, key=secret)
        # The first capture is exported to the exact execution owner for its
        # final assembled terminal_public_detail, without another settings read.
        context = replace(context, provider_error_scrubber_observer=scrubbers.append)
        value = call.target.transport.open_stream(call=call, context=context)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and len(scrubbers) == 1
    final_detail = scrubbers[0](
        f"{final.error.code.value}: {final.error.message} | {secret} | {encoded} | {escaped}"
    )
    for value in (secret, escaped, encoded, proxy_secret, "ProxyP%40ss", "proxy-user"):
        assert value not in caplog.text and value not in final_detail
    assert (
        "normal diagnostic body" in caplog.text
        and "normal diagnostic body" in final_detail
    )


@pytest.mark.parametrize("api", APIS)
def test_effective_defaults_with_hosted_tools_are_unsafe(api):
    profile = RouteWireProfile(
        wire_api=api,
        request_defaults=(
            {"web_search_options": {}} if api == APIS[0] else {"background": True}
        ),
    )
    with server([503]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint, profile=profile)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and waits == []
    assert final.error.retry_summary.skipped_reason == "unsafe_to_replay"


@pytest.mark.parametrize("api", APIS)
def test_dependency_diagnostics_scrub_at_each_real_sdk_await_without_cross_yield_context(
    api, monkeypatch, caplog
):
    import logging
    import openai

    caplog.set_level(logging.DEBUG)
    key = "fixture-known-key/value"
    original_next, original_close = (
        openai.AsyncStream.__anext__,
        openai.AsyncStream.close,
    )

    async def next_item(stream):
        # Controlled diagnostic injection inside the actual SDK read operation.
        logging.getLogger("openai._base_client").debug(
            "normal SDK read context token=%s", key
        )
        return await original_next(stream)

    async def close(stream):
        logging.getLogger("httpx2").info(
            "normal HTTP context https://user:%s@example.test/path?api_key=%s",
            key,
            quote(key, safe=""),
        )
        return await original_close(stream)

    monkeypatch.setattr(openai.AsyncStream, "__anext__", next_item)
    monkeypatch.setattr(openai.AsyncStream, "close", close)
    with server([{"events": success(api), "headers": {"X-Fixture-Secret": key}}]) as (
        endpoint,
        _,
    ):
        value, _, _, _, _ = execution(api, endpoint, key=key)

        async def run():
            # Each read may run in a different task, as in real Runner races.
            output = []
            while (item := await asyncio.create_task(value.read_next())) is not None:
                output.append(item)
            logging.getLogger("httpx2").info("outside operation context")
            return output

        output = asyncio.run(run())
    assert output[-1].terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert key not in caplog.text and quote(key, safe="") not in caplog.text
    assert (
        "normal SDK read context" in caplog.text
        and "normal HTTP context" in caplog.text
    )
    assert "outside operation context" in caplog.text


@pytest.mark.parametrize("api", APIS)
def test_cancelled_reconnect_admission_never_starts_another_http_request(
    api, monkeypatch
):
    from importlib import import_module

    module = import_module(
        "pulsara_agent.llm.adapters.openai."
        + ("chat_completions" if api == APIS[0] else "responses")
    )
    original = module.admit_provider_request
    with server([503, {"events": success(api)}]) as (endpoint, requests):

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            admissions = []

            async def admit(**kwargs):
                admissions.append(kwargs)
                if len(admissions) == 2:
                    entered.set()
                    await release.wait()
                return await original(**kwargs)

            monkeypatch.setattr(module, "admit_provider_request", admit)
            value, _, _, _, _ = execution(api, endpoint)
            task = asyncio.create_task(drain(value))
            await asyncio.wait_for(entered.wait(), 3)
            assert len(requests) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            await value.aclose()

        asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("api", APIS)
def test_cancelled_stream_read_joins_sdk_close_and_does_not_retry(api, monkeypatch):
    import openai

    original_close = openai.AsyncStream.close
    closed = []

    async def close(stream):
        await original_close(stream)
        closed.append(stream)

    monkeypatch.setattr(openai.AsyncStream, "close", close)
    hold = ThreadEvent()
    try:
        with server([{"events": partial(api), "hold": hold, "cut": True}]) as (
            endpoint,
            requests,
        ):

            async def run():
                seen = asyncio.Event()
                value, _, _, _, _ = execution(api, endpoint)

                async def collect():
                    while (item := await value.read_next()) is not None:
                        if "DISCARDED" in str(item):
                            seen.set()

                task = asyncio.create_task(collect())
                await asyncio.wait_for(seen.wait(), 3)
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert len(closed) == 1
                await value.aclose()
                hold.set()

            asyncio.run(run())
            assert len(requests) == 1
    finally:
        hold.set()


@pytest.mark.parametrize("api", APIS)
def test_success_terminal_then_network_reset_is_not_accepted_or_retried(api):
    with server([{"events": success(api), "cut": True}]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert final.error.retry_summary.skipped_reason == "terminal_already_observed"
    assert "已收到结果，但连接收尾失败" in final.error.message
    assert len(requests) == 1 and waits == []


def test_sanitizer_preserves_non_secret_error_words_and_url_evidence():
    from pulsara_agent.llm.provider_sanitization import sanitize_provider_failure

    text = "password field is unsupported; Authorization: schema property expected. https://example.test/path?q=ordinary&password_field=name#details https://example.test?q=user@domain#fragment"
    assert sanitize_provider_failure(message=text).message == text
    authenticated = sanitize_provider_failure(
        message="Authorization: Bearer actual-header-token https://user:password@example.test/path?q=ordinary#details"
    )
    assert (
        "actual-header-token" not in authenticated.message
        and "user:password" not in authenticated.message
    )
    assert "https://example.test/path?q=ordinary#details" in authenticated.message


@pytest.mark.parametrize("api", APIS)
def test_normalizer_exact_scrubs_exception_after_adapter_credential_capture(
    api, monkeypatch
):
    from importlib import import_module

    module = import_module(
        "pulsara_agent.llm.adapters.openai."
        + ("chat_completions" if api == APIS[0] else "responses")
    )
    key = 'construction-key-"quoted"-\\slash'

    def broken_constructor(**kwargs):
        raise ValueError(
            "concrete constructor failure "
            + repr(key)
            + " "
            + quote(key, safe="")
            + " https://example.test/?mode=ordinary#details"
        )

    monkeypatch.setattr(module, "build_async_openai_client", broken_constructor)
    value, _, _, _, _ = execution(api, "http://127.0.0.1:9/v1", key=key)
    final = asyncio.run(drain(value))[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert (
        key not in final.error.message
        and repr(key)[1:-1] not in final.error.message
        and quote(key, safe="") not in final.error.message
    )
    assert (
        "concrete constructor failure" in final.error.message
        and "?mode=ordinary#details" in final.error.message
    )


def test_model_discovery_owns_exact_credential_scrub_and_keeps_error_context(caplog):
    from pulsara_agent.llm.model_discovery import (
        discover_model_ids,
        ModelDiscoveryFailure,
    )

    key = 'discovery-key-"quoted"-\\slash'
    caplog.set_level("DEBUG")
    message = (
        "model list unavailable "
        + key
        + " "
        + quote(key, safe="")
        + " "
        + json.dumps(key)[1:-1]
        + " https://example.test/?mode=ordinary#details"
    )
    with server([{"status": 401, "body": {"error": {"message": message}}}]) as (
        endpoint,
        requests,
    ):
        with pytest.raises(ModelDiscoveryFailure) as captured:
            asyncio.run(discover_model_ids(base_url=endpoint, api_key=key))
    assert len(requests) == 1 and requests[0]["method"] == "GET"
    assert "model list unavailable" in str(
        captured.value
    ) and "?mode=ordinary#details" in str(captured.value)
    for value in (key, quote(key, safe=""), json.dumps(key)[1:-1]):
        assert value not in caplog.text and value not in str(captured.value)


@pytest.mark.parametrize("api", APIS)
def test_ephemeral_probe_owns_same_exact_scrub_without_changing_single_attempt_policy(
    api, caplog
):
    from pulsara_agent.llm.connection_probe import (
        probe_model_connection,
        ModelConnectionProbeFailure,
    )

    key = 'probe-key-"quoted"-\\slash'
    caplog.set_level("DEBUG")
    message = (
        "probe diagnostic "
        + key
        + " "
        + quote(key, safe="")
        + " https://example.test/?mode=ordinary#details"
    )
    with server([{"status": 401, "body": {"error": {"message": message}}}]) as (
        endpoint,
        requests,
    ):
        runtime = test_model_runtime(base_url=endpoint, wire_api=api, api_key=key)
        binding = test_model_binding(runtime)
        resolved = runtime.freeze_resolution_snapshot().validate(binding)
        with pytest.raises(ModelConnectionProbeFailure) as captured:
            asyncio.run(
                probe_model_connection(
                    resolved=resolved,
                    catalog=runtime.catalog.selectable(),
                    route_wires=runtime.route_wires,
                    api_key=key,
                )
            )
    assert len(requests) == 1
    assert "probe diagnostic" in str(
        captured.value
    ) and "?mode=ordinary#details" in str(captured.value)
    for value in (key, quote(key, safe=""), json.dumps(key)[1:-1]):
        assert value not in caplog.text and value not in str(captured.value)


def test_explicit_account_quota_codes_are_never_retried():
    from pulsara_agent.llm.adapters.openai.retrying import ProviderFailureResponseError

    for code in ("quota_exceeded", "insufficient_quota", "insufficient_balance"):
        decision = classify_llm_error(
            ProviderFailureResponseError(
                {"error": {"code": code, "message": "account limit"}}
            )
        )
        assert decision.kind is RetryDecisionKind.DO_NOT_RETRY
    assert (
        classify_llm_error(
            ProviderFailureResponseError({"error": {"type": "internal_server_error"}})
        ).kind
        is RetryDecisionKind.RETRY
    )


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "error_type,message",
    (
        (ValueError, "connection error in local assembler invariant"),
        (RuntimeError, "connection reset counter mismatch"),
    ),
)
def test_programming_error_words_never_grant_transport_retry(
    api, error_type, message, monkeypatch
):
    from importlib import import_module

    module = import_module(
        "pulsara_agent.llm.adapters.openai."
        + ("chat_completions" if api == APIS[0] else "responses")
    )
    accumulator = (
        module.ChatCompletionAccumulator
        if api == APIS[0]
        else module.ResponsesCompletionAccumulator
    )

    def broken_apply(self, raw):
        raise error_type(message)

    monkeypatch.setattr(accumulator, "apply", broken_apply)
    with server([{"events": success(api)}]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and waits == []
    assert final.error.code is ProviderModelStreamErrorCode.UNKNOWN_PROVIDER_ERROR
    assert final.error.retry_summary.retry_count == 0
    assert message in final.error.message


def test_query_names_preserve_non_authentication_values_and_scrub_real_tokens():
    from pulsara_agent.llm.adapters.openai.retrying import capture_retry_scrubber
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    key = 'actual-credential/"quoted"'
    settings = test_model_runtime(api_key=key).settings.read()
    token = 'actual-url-token/"value"'
    scrub = capture_retry_scrubber(
        settings,
        ProcessCredentialBoundary(key),
        "https://example.test/path?monkey=banana&design=ordinary&access_token="
        + quote(token, safe="")
        + "#detail",
    )
    diagnostic = (
        "banana ordinary context " + key + " " + token + " " + quote(token, safe="")
    )
    cleaned = scrub.scrub_text(diagnostic)
    assert "banana ordinary context" in cleaned
    assert (
        key not in cleaned
        and token not in cleaned
        and quote(token, safe="") not in cleaned
    )


@pytest.mark.parametrize(
    "key,encoded",
    (
        ("fixture/secret:token", "fixture%2fsecret%3Atoken"),
        ("fixture/secret:token", "fixtur%65%2fsecret%3Atoken"),
        ("fixture/secret:token", "fixture%2fsecret:token"),
        (
            "fixture/secret:token",
            "".join("%%%02x" % byte for byte in b"fixture/secret:token"),
        ),
        ("fixture/secret:token", r"fixture\u002Fsecret\u003atoken"),
        ("fixture/secret/token", r"fixture\/secret/token"),
    ),
)
@pytest.mark.parametrize("api", APIS)
def test_equivalent_secret_encodings_scrub_real_sdk_logs_and_final_detail(
    api, key, encoded, caplog
):
    caplog.set_level("DEBUG")
    message = (
        "concrete upstream diagnostic https://example.test/error?api_key="
        + encoded
        + "&mode=normal#details"
    )
    scrubbers = []
    with server(
        [
            {
                "status": 401,
                "body": {"error": {"code": "authentication", "message": message}},
            }
        ]
    ) as (endpoint, requests):
        _, _, _, context, call = execution(api, endpoint, key=key)
        context = replace(context, provider_error_scrubber_observer=scrubbers.append)
        value = call.target.transport.open_stream(call=call, context=context)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and len(scrubbers) == 1
    detail = scrubbers[0](
        f"{final.error.code.value}: {final.error.message} original={encoded}"
    )
    for spelling in (key, encoded, repr(encoded)[1:-1], json.dumps(encoded)[1:-1]):
        assert (
            spelling not in final.error.message
            and spelling not in detail
            and spelling not in caplog.text
        )
    for evidence in (final.error.message, detail, caplog.text):
        assert "concrete upstream diagnostic" in evidence
        assert "&mode=normal#details" in evidence


def test_encoding_equivalence_preserves_secret_character_case_and_plain_context():
    from pulsara_agent.llm.adapters.openai.retrying import (
        expand_provider_diagnostic_scrubber,
    )
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet

    exact = ProcessCredentialScrubSet()
    exact.observe("Case/Key")
    exact.observe("密钥/Token")
    exact.observe(r"literal\uAbCd/value")
    scrub = expand_provider_diagnostic_scrubber(exact, None)
    normal = r"banana design=ordinary mode=normal case%2fKey literal\uabcd\/value"
    assert scrub.scrub_text(normal) == normal
    unicode_encoded = r"\u5BC6\u94a5\/Token"
    for text in (unicode_encoded, repr(unicode_encoded), json.dumps(unicode_encoded)):
        assert scrub.contains(text)
        assert unicode_encoded not in scrub.scrub_text(text)
        assert "5BC6" not in scrub.scrub_text(text)
    encoded = quote("密钥/Token", safe="")
    for seed in range(12):
        rng = random.Random(seed)
        mixed = re.sub(
            r"%[0-9A-F]{2}",
            lambda match: (
                "%"
                + "".join(
                    char.lower() if rng.random() < 0.5 else char.upper()
                    for char in match.group()[1:]
                )
            ),
            encoded,
        )
        assert scrub.contains(mixed)
        assert mixed not in scrub.scrub_text("normal " + mixed)


@pytest.mark.parametrize("api", APIS)
def test_form_space_plus_is_secret_only_in_authentication_url_query(api, caplog):
    caplog.set_level("DEBUG")
    key = "fixture secret"
    auth_url = "https://example.test/error?api_key=fixture+secret&mode=normal#details"
    ordinary_url = "https://example.test/view?monkey=fixture+secret&design=ordinary"
    message = "formula fixture+secret ; " + auth_url + " ; " + ordinary_url
    scrubbers = []
    with server(
        [
            {
                "status": 401,
                "body": {"error": {"code": "authentication", "message": message}},
            }
        ]
    ) as (endpoint, requests):
        _, _, _, context, call = execution(api, endpoint, key=key)
        context = replace(context, provider_error_scrubber_observer=scrubbers.append)
        final = asyncio.run(
            drain(call.target.transport.open_stream(call=call, context=context))
        )[-1]
    assert len(requests) == 1 and len(scrubbers) == 1
    detail = scrubbers[0](
        f"{final.error.code.value}: {final.error.message}; {auth_url}; formula fixture+secret"
    )
    for evidence in (final.error.message, detail, caplog.text):
        assert "api_key=fixture+secret" not in evidence
        assert "formula fixture+secret" in evidence
        assert "monkey=fixture+secret&design=ordinary" in evidence
        assert "&mode=normal#details" in evidence


def test_authentication_query_form_encoding_uses_context_for_contains_and_scrub():
    from pulsara_agent.llm.adapters.openai.retrying import (
        expand_provider_diagnostic_scrubber,
    )
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet

    exact = ProcessCredentialScrubSet()
    exact.observe("fixture secret")
    scrub = expand_provider_diagnostic_scrubber(exact, None)
    normal = "fixture+secret https://example.test/?monkey=fixture+secret#api_key=fixture+secret"
    assert not scrub.contains(normal)
    assert scrub.scrub_text(normal) == normal
    authenticated = "https://example.test/?api%5fkey=fixture+secret&monkey=fixture+secret&mode=normal#readable"
    assert scrub.contains(authenticated)
    cleaned = scrub.scrub_text(authenticated)
    assert "api%5fkey=fixture+secret" not in cleaned
    assert "&monkey=fixture+secret&mode=normal#readable" in cleaned


@pytest.mark.parametrize("api", APIS)
def test_legal_query_apostrophe_and_form_space_scrub_full_secret_in_sdk_repr(
    api, caplog
):
    caplog.set_level("DEBUG")
    key = "fixture secret'token"
    auth_url = (
        "https://example.test/error?api_key=fixture+secret'token&mode=normal#details"
    )
    # Double quotes force Python's SDK body repr to escape the credential's
    # apostrophe as well; that formatting boundary still carries the same value.
    message = 'concrete "quoted" context ; ' + auth_url
    with server(
        [
            {
                "status": 401,
                "body": {"error": {"code": "authentication", "message": message}},
            }
        ]
    ) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint, key=key)
        final = asyncio.run(drain(value))[-1]
    assert len(requests) == 1 and waits == []
    for evidence in (final.error.message, caplog.text):
        assert "fixture+secret'token" not in evidence
        assert r"fixture+secret\'token" not in evidence
        assert key not in evidence
        assert "concrete" in evidence and "&mode=normal#details" in evidence


def test_form_query_apostrophe_does_not_extend_into_ordinary_quoted_context():
    from pulsara_agent.llm.adapters.openai.retrying import (
        expand_provider_diagnostic_scrubber,
    )
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet

    exact = ProcessCredentialScrubSet()
    exact.observe("fixture secret")
    exact.observe("fixture secret'token")
    scrub = expand_provider_diagnostic_scrubber(exact, None)
    ordinary = "'https://example.test/?api_key=unrelated'formula=fixture+secret quoted fixture+secret'token https://example.test/?monkey=fixture+secret'token#api_key=fixture+secret"
    assert scrub.scrub_text(ordinary) == ordinary
    auth_url = "https://example.test/path'part?api_key=fixture+secret'token&mode=normal#details"
    cleaned = scrub.scrub_text(auth_url)
    assert "fixture+secret'token" not in cleaned
    assert "/path'part?api_key=" in cleaned
    assert "&mode=normal#details" in cleaned


def successful_function_response(api):
    arguments = json.dumps(
        {
            "target": {"kind": "PATH", "path": "must-not-exist.txt"},
            "content": "must not execute",
        }
    )
    if api == APIS[0]:
        return [
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call:close-failed",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": arguments},
                        }
                    ]
                }
            ),
            chat({}, "tool_calls"),
        ]
    return [
        {
            "type": "response.completed",
            "response": {
                "id": "response-close-failed",
                "status": "completed",
                "model": "test-model",
                "output": [
                    {
                        "id": "item-close-failed",
                        "type": "function_call",
                        "call_id": "call:close-failed",
                        "name": "write_file",
                        "arguments": arguments,
                    }
                ],
            },
        }
    ]


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("outcome", ("success", "failure"))
def test_owned_sdk_client_close_failure_is_combined_before_any_terminal_acceptance(
    api, outcome, monkeypatch, caplog
):
    import openai

    original_close = openai.AsyncOpenAI.close
    closed = []

    async def close(client):
        await original_close(client)
        assert client.is_closed()
        closed.append(client)
        raise ConnectionError("specific final client close failure")

    monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
    script = (
        {"events": successful_function_response(api)}
        if outcome == "success"
        else {
            "status": 401,
            "body": {
                "error": {
                    "code": "authentication",
                    "message": "specific original authentication failure",
                }
            },
        }
    )
    with server([script]) as (endpoint, requests):
        value, waits, _, _, _ = execution(api, endpoint)

        async def run():
            items = []
            while (item := await value.read_next()) is not None:
                if hasattr(item, "terminal_kind"):
                    assert len(closed) == 1
                items.append(item)
            await value.aclose()
            return items

        items = asyncio.run(run())
    final = items[-1]
    assert final.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert final.completed_replay_payload is None
    assert len(requests) == 1 and waits == [] and len(closed) == 1
    assert "specific final client close failure" in final.error.message
    assert (
        final.error.retry_summary.retry_count == 0
        and not final.error.retry_summary.exhausted
    )
    if outcome == "success":
        assert "已收到结果，但连接收尾失败" in final.error.message
        assert (
            final.error.retry_summary.skipped_reason == "completed_stream_close_failed"
        )
    else:
        assert "specific original authentication failure" in final.error.message
        assert final.error.code is ProviderModelStreamErrorCode.AUTHENTICATION_FAILED
        assert final.error.retry_summary.final_status_code == 401
        assert final.error.retry_summary.skipped_reason == "physical_close_unconfirmed"
    assert '"close_scope": "sdk_client"' in caplog.text
    assert '"client_close_confirmed": false' in caplog.text
    assert "specific final client close failure" in caplog.text
    if outcome == "failure":
        assert "specific original authentication failure" in caplog.text
        assert "AuthenticationError" in caplog.text and "ConnectionError" in caplog.text


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("close_fails", (False, True))
def test_cancel_during_final_owned_client_close_joins_close_and_wins_over_result(
    api, close_fails, monkeypatch
):
    import openai

    original_close = openai.AsyncOpenAI.close
    with server([{"events": successful_function_response(api)}]) as (
        endpoint,
        requests,
    ):

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            finished = []

            async def close(client):
                entered.set()
                await release.wait()
                await original_close(client)
                finished.append(client)
                if close_fails:
                    raise ConnectionError("close failure after cancellation")

            monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
            value, _, _, _, _ = execution(api, endpoint)
            task = asyncio.create_task(drain(value))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(finished) == 1 and finished[0].is_closed()
            await value.aclose()

        asyncio.run(run())
    assert len(requests) == 1
