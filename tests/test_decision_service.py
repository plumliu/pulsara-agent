"""Shared Decision protocol, physical batching and session-local ownership."""

import asyncio
from dataclasses import replace
import json
from time import monotonic

import httpx2
import pytest

from pulsara_agent.retrieval.config import DecisionBackendConfig, RetrievalConnection
from pulsara_agent.retrieval.decision import (
    DecisionError,
    DecisionQuestion,
    DecisionRequest,
    DecisionService,
)
from tests.test_memory_retrieval_adapters import install_transport


SHAPES = ("system_one", "openai_decisions")


def connection(shape):
    return RetrievalConnection(
        "https://provider.example/decisions"
        if shape == "openai_decisions"
        else "https://provider.example/systemone",
        "decision-test",
        shape,
        "none",
    )


def builder(inputs):
    def build(start, end):
        return DecisionRequest(
            state={
                "inputs": [{"index": i, "text": inputs[i]} for i in range(start, end)]
            },
            questions=tuple(
                DecisionQuestion(
                    f"input_{i}",
                    f"Judge input {i} against the explicit condition.",
                    "The condition is met.",
                    "The condition is not met.",
                )
                for i in range(start, end)
            ),
        )

    return build


def answers(request, *, probability=0.5):
    body = json.loads(request.content)
    questions = body["questions"]
    if isinstance(questions, dict):
        return {
            "answers": {
                name: {"type": "noul", "noul": probability}
                for name in reversed(questions)
            }
        }
    return {
        "answers": [
            {"type": "predicate", "name": question["name"], "probability": probability}
            for question in reversed(questions)
        ]
    }


@pytest.mark.parametrize("shape", SHAPES)
def test_batches_use_sdk_wire_and_preserve_all_ordered_probabilities(
    monkeypatch, shape
):
    seen = install_transport(
        monkeypatch, lambda request: httpx2.Response(200, json=answers(request))
    )

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection(shape),
            item_count=43,
            build_request=builder(["", "重复", "重复"] + ["text"] * 40),
        )
        try:
            result = await service.evaluate(batches)
            assert result == (0.5,) * 43
        finally:
            await service.aclose()
        assert service.physical_close_complete

    asyncio.run(run())
    assert [len(json.loads(request.content)["questions"]) for request in seen] == [
        20,
        20,
        3,
    ]
    assert all(len(request.content) <= 192 * 1024 for request in seen)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("probability", [True, False, "0.5", None, -0.1, 1.1, 10**400])
def test_actual_sdk_does_not_accept_coerced_probabilities(
    monkeypatch, shape, probability
):
    seen = install_transport(
        monkeypatch,
        lambda request: httpx2.Response(
            200, json=answers(request, probability=probability)
        ),
    )

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection(shape), item_count=1, build_request=builder(["x"])
        )
        try:
            with pytest.raises(DecisionError) as caught:
                await service.evaluate(batches)
            assert caught.value.code == "DECISION_RESPONSE_INVALID"
        finally:
            await service.aclose()

    asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("shape", SHAPES)
def test_every_single_item_is_checked_before_first_request(monkeypatch, shape):
    seen = install_transport(monkeypatch, {})
    service = DecisionService()
    with pytest.raises(DecisionError) as caught:
        service.prepare_batches(
            connection(shape),
            item_count=22,
            build_request=builder(["small"] * 21 + ["汉" * (192 * 1024)]),
        )
    assert caught.value.code == "DECISION_RESOURCE_BOUNDARY"
    assert caught.value.input_index == 21 and caught.value.limit == 192 * 1024
    assert seen == []


@pytest.mark.parametrize("shape", SHAPES)
def test_exact_utf8_envelope_determines_batch_boundary(monkeypatch, shape):
    seen = install_transport(
        monkeypatch, lambda request: httpx2.Response(200, json=answers(request))
    )

    async def run():
        service = DecisionService(
            config=replace(DecisionBackendConfig(), maximum_request_bytes=1600)
        )
        batches = service.prepare_batches(
            connection(shape), item_count=4, build_request=builder(['汉"\\' * 80] * 4)
        )
        try:
            assert await service.evaluate(batches) == (0.5,) * 4
            assert [batch.payload_bytes for batch in batches] == [
                request.content for request in seen
            ]
        finally:
            await service.aclose()

    asyncio.run(run())
    assert len(seen) > 1
    assert all(len(request.content) <= 1600 for request in seen)


@pytest.mark.parametrize("shape", SHAPES)
def test_refusal_and_later_batch_failure_never_return_prefix(monkeypatch, shape):
    calls = []

    def respond(request):
        calls.append(request)
        result = answers(request)
        if len(calls) == 2:
            if shape == "system_one":
                name = next(iter(result["answers"]))
                result["answers"][name] = {"type": "refusal", "refusal": "cannot judge"}
            else:
                result["answers"][0] = {
                    "name": result["answers"][0]["name"],
                    "type": "refusal",
                    "refusal": "cannot judge",
                }
        return httpx2.Response(200, json=result)

    install_transport(monkeypatch, respond)

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection(shape), item_count=41, build_request=builder(["x"] * 41)
        )
        try:
            with pytest.raises(DecisionError) as caught:
                await service.evaluate(batches)
            assert caught.value.code == "DECISION_REFUSED"
        finally:
            await service.aclose()

    asyncio.run(run())
    assert len(calls) == 2


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    "body",
    [
        b'{"answers":{"input_0":{"type":"noul","noul":0.5},"input_0":{"type":"noul","noul":0.6}}}',
        b"x" * (256 * 1024 + 1),
        b'{"answers":null}',
    ],
)
def test_raw_response_boundaries_precede_sdk_parsing(monkeypatch, shape, body):
    seen = install_transport(
        monkeypatch, lambda request: httpx2.Response(200, content=body)
    )

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection(shape), item_count=1, build_request=builder(["x"])
        )
        try:
            with pytest.raises(DecisionError) as caught:
                await service.evaluate(batches)
            assert caught.value.code == "DECISION_RESPONSE_INVALID"
        finally:
            await service.aclose()

    asyncio.run(run())
    assert len(seen) == 1


def install_async_transport(monkeypatch, handler):
    from pulsara_agent.llm.adapters.openai import client as openai_client
    from pulsara_agent.process_credential_boundary import (
        ProcessCredentialBoundHttpx2Client,
    )

    def injected(**kwargs):
        return ProcessCredentialBoundHttpx2Client(
            **kwargs, transport=httpx2.MockTransport(handler), trust_env=False
        )

    monkeypatch.setattr(openai_client, "ProcessCredentialBoundHttpx2Client", injected)


def test_cancelled_request_holds_shared_gate_until_sdk_close_joins(monkeypatch):
    import openai

    original_close = openai.AsyncOpenAI.close

    async def run():
        request_started, close_started, close_release = (
            asyncio.Event() for _ in range(3)
        )
        observed, closes = [], []

        async def respond(request):
            observed.append(request)
            if len(observed) == 1:
                request_started.set()
                await asyncio.Event().wait()
            return httpx2.Response(200, json=answers(request))

        async def close(client):
            closes.append(client)
            if len(closes) == 1:
                close_started.set()
                await close_release.wait()
            await original_close(client)

        install_async_transport(monkeypatch, respond)
        monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=21, build_request=builder(["x"] * 21)
        )
        first = asyncio.create_task(service.evaluate(batches))
        await request_started.wait()
        second = asyncio.create_task(service.evaluate(batches[:1]))
        await asyncio.sleep(0)
        first.cancel("first cancel")
        await close_started.wait()
        first.cancel("second cancel")
        await asyncio.sleep(0)
        assert not first.done() and not second.done() and len(observed) == 1
        close_release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancel"):
            await first
        assert await second == (0.5,) * 20
        await service.aclose()
        assert (
            len(observed) == 2 and len(closes) == 2 and service.physical_close_complete
        )

    asyncio.run(run())


@pytest.mark.parametrize("trigger", ["event", "deadline"])
def test_waiting_for_shared_gate_stops_without_issuing_request(monkeypatch, trigger):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        observed = []

        async def respond(request):
            observed.append(request)
            started.set()
            await release.wait()
            return httpx2.Response(200, json=answers(request))

        install_async_transport(monkeypatch, respond)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )
        first = asyncio.create_task(service.evaluate(batches))
        await started.wait()
        event = asyncio.Event()
        second = asyncio.create_task(
            service.evaluate(
                batches,
                cancel_event=event,
                deadline_monotonic=monotonic() + 0.02
                if trigger == "deadline"
                else None,
            )
        )
        await asyncio.sleep(0)
        if trigger == "event":
            event.set()
        with pytest.raises(
            asyncio.CancelledError if trigger == "event" else TimeoutError
        ):
            await second
        release.set()
        assert await first == (0.5,)
        await service.aclose()
        assert len(observed) == 1

    asyncio.run(run())


def test_stop_between_batches_never_sends_next_batch(monkeypatch):
    import openai

    original_close = openai.AsyncOpenAI.close

    async def run():
        event = asyncio.Event()
        seen = install_transport(
            monkeypatch, lambda request: httpx2.Response(200, json=answers(request))
        )

        async def close(client):
            await original_close(client)
            event.set()

        monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=21, build_request=builder(["x"] * 21)
        )
        with pytest.raises(asyncio.CancelledError):
            await service.evaluate(batches, cancel_event=event)
        await service.aclose()
        assert len(seen) == 1 and service.physical_close_complete

    asyncio.run(run())


def test_unconfirmed_sdk_close_seals_service_before_releasing_gate(monkeypatch):
    import openai

    async def run():
        close_started, release = asyncio.Event(), asyncio.Event()
        seen = install_transport(
            monkeypatch, lambda request: httpx2.Response(200, json=answers(request))
        )

        async def close(client):
            close_started.set()
            await release.wait()
            raise RuntimeError("controlled close failure")

        monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )
        first = asyncio.create_task(service.evaluate(batches))
        await close_started.wait()
        second = asyncio.create_task(service.evaluate(batches))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(DecisionError, match="controlled close failure"):
            await first
        with pytest.raises(asyncio.CancelledError):
            await second
        with pytest.raises(DecisionError, match="closing"):
            await service.evaluate(batches)
        with pytest.raises(DecisionError, match="controlled close failure"):
            await service.aclose()
        assert len(seen) == 1 and not service.physical_close_complete

    asyncio.run(run())


def test_service_close_repeated_cancellation_joins_sdk_and_other_sessions_continue(
    monkeypatch,
):
    import openai

    original_close = openai.AsyncOpenAI.close

    async def run():
        request_started, close_started, close_release = (
            asyncio.Event() for _ in range(3)
        )
        closes = []

        async def respond(request):
            if json.loads(request.content)["model"] == "blocked":
                request_started.set()
                await asyncio.Event().wait()
            return httpx2.Response(200, json=answers(request))

        async def close(client):
            closes.append(client)
            if len(closes) == 1:
                close_started.set()
                await close_release.wait()
            await original_close(client)

        install_async_transport(monkeypatch, respond)
        monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
        service, other = DecisionService(), DecisionService()
        batches = service.prepare_batches(
            replace(connection("system_one"), model_id="blocked"),
            item_count=1,
            build_request=builder(["x"]),
        )
        call = asyncio.create_task(service.evaluate(batches))
        await request_started.wait()
        closing = asyncio.create_task(service.aclose())
        await close_started.wait()
        closing.cancel("first close cancel")
        await asyncio.sleep(0)
        closing.cancel("second close cancel")
        other_batches = other.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )
        assert await other.evaluate(other_batches) == (0.5,)
        assert (
            not closing.done()
            and not call.done()
            and not service.physical_close_complete
        )
        close_release.set()
        with pytest.raises(asyncio.CancelledError, match="first close cancel"):
            await closing
        with pytest.raises(asyncio.CancelledError):
            await call
        await service.aclose()
        await other.aclose()
        assert service.physical_close_complete and other.physical_close_complete

    asyncio.run(run())


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("fault", ["missing", "unknown", "duplicate", "wrong_type"])
def test_response_names_cover_each_requested_question_exactly_once(
    monkeypatch, shape, fault
):
    def respond(request):
        result = answers(request)
        if shape == "system_one":
            if fault == "missing":
                result["answers"].pop("input_0")
            elif fault == "unknown":
                result["answers"]["other"] = result["answers"].pop("input_0")
            elif fault == "wrong_type":
                result["answers"]["input_0"]["type"] = "predicate"
            else:
                return httpx2.Response(
                    200,
                    content=(
                        '{"answers":{"input_0":{"type":"noul","noul":0.5},'
                        '"input_0":{"type":"noul","noul":0.5},'
                        '"input_1":{"type":"noul","noul":0.5}}}'
                    ),
                )
        else:
            if fault == "missing":
                result["answers"].pop()
            elif fault == "unknown":
                result["answers"][0]["name"] = "other"
            elif fault == "duplicate":
                result["answers"][0]["name"] = result["answers"][1]["name"]
            else:
                result["answers"][0]["type"] = "noul"
        return httpx2.Response(200, json=result)

    seen = install_transport(monkeypatch, respond)

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection(shape), item_count=2, build_request=builder(["x", "x"])
        )
        with pytest.raises(DecisionError) as caught:
            await service.evaluate(batches)
        assert caught.value.code == "DECISION_RESPONSE_INVALID"
        await service.aclose()

    asyncio.run(run())
    assert len(seen) == 1


def test_memory_and_generic_call_share_gate_and_release_it_per_batch(monkeypatch):
    from pulsara_agent.retrieval.rerank.factory import build_rerank_provider

    async def run():
        first_started, release_first = asyncio.Event(), asyncio.Event()
        models = []

        async def respond(request):
            models.append(json.loads(request.content)["model"])
            if len(models) == 1:
                first_started.set()
                await release_first.wait()
            return httpx2.Response(200, json=answers(request, probability=0.7))

        install_async_transport(monkeypatch, respond)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=21, build_request=builder(["x"] * 21)
        )
        memory = build_rerank_provider(
            replace(connection("system_one"), model_id="memory"),
            decision_service=service,
        )
        logical = asyncio.create_task(service.evaluate(batches))
        await first_started.wait()
        ranked = asyncio.create_task(
            memory.rerank("remember", ["first", "second"], candidate_ids=("a", "b"))
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert models == ["decision-test"]
        release_first.set()
        assert await logical == (0.7,) * 21
        assert [(row.index, row.score) for row in await ranked] == [(0, 0.7), (1, 0.7)]
        assert models == ["decision-test", "memory", "decision-test"]
        await memory.aclose()
        assert await service.evaluate(batches[-1:]) == (0.7,)
        await service.aclose()
        assert service.physical_close_complete

    asyncio.run(run())


def test_logical_deadline_covers_all_batches_without_refresh(monkeypatch):
    async def run():
        calls = []

        async def respond(request):
            calls.append(request)
            await asyncio.sleep(0.03)
            return httpx2.Response(200, json=answers(request))

        install_async_transport(monkeypatch, respond)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=41, build_request=builder(["x"] * 41)
        )
        with pytest.raises(TimeoutError):
            await service.evaluate(batches, deadline_monotonic=monotonic() + 0.05)
        await service.aclose()
        assert len(calls) == 2 and service.physical_close_complete

    asyncio.run(run())


def test_physical_timeout_is_request_failure_and_not_retried(monkeypatch):
    async def run():
        calls = []

        async def respond(request):
            calls.append(request)
            await asyncio.Event().wait()

        install_async_transport(monkeypatch, respond)
        service = DecisionService(
            config=replace(DecisionBackendConfig(), timeout_seconds=0.01)
        )
        batches = service.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )
        with pytest.raises(DecisionError) as caught:
            await service.evaluate(batches)
        assert caught.value.code == "DECISION_REQUEST_FAILED"
        await service.aclose()
        assert len(calls) == 1 and service.physical_close_complete

    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500])
def test_retryable_status_is_sent_once(monkeypatch, status):
    seen = install_transport(
        monkeypatch,
        lambda request: httpx2.Response(status, json={"error": "controlled failure"}),
    )

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection("openai_decisions"), item_count=1, build_request=builder(["x"])
        )
        with pytest.raises(DecisionError) as caught:
            await service.evaluate(batches)
        assert caught.value.code == "DECISION_REQUEST_FAILED"
        await service.aclose()

    asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("already_delivered", [False, True])
def test_pending_cancellation_before_admission_differs_from_historical_count(
    monkeypatch, already_delivered
):
    seen = install_transport(
        monkeypatch, lambda request: httpx2.Response(200, json=answers(request))
    )

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )

        async def caller():
            asyncio.current_task().cancel("preexisting request")
            if already_delivered:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.sleep(0)
                assert asyncio.current_task().cancelling() == 1
                assert await service.evaluate(batches) == (0.5,)
            else:
                with pytest.raises(asyncio.CancelledError, match="preexisting request"):
                    await service.evaluate(batches)

        await asyncio.create_task(caller())
        await service.aclose()
        assert len(seen) == int(already_delivered)

    asyncio.run(run())


def test_sdk_operation_cancellation_keeps_original_exception(monkeypatch):
    original = asyncio.CancelledError("provider operation self cancellation")

    async def respond(request):
        raise original

    install_async_transport(monkeypatch, respond)

    async def run():
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            await service.evaluate(batches)
        assert caught.value is original
        await service.aclose()
        assert service.physical_close_complete

    asyncio.run(run())


def test_pending_caller_cancellation_during_service_close_still_joins(monkeypatch):
    import openai

    original_close = openai.AsyncOpenAI.close

    async def run():
        started, close_started, release_close = (asyncio.Event() for _ in range(3))

        async def respond(request):
            started.set()
            await asyncio.Event().wait()

        async def close(client):
            close_started.set()
            await release_close.wait()
            await original_close(client)

        install_async_transport(monkeypatch, respond)
        monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
        service = DecisionService()
        batches = service.prepare_batches(
            connection("system_one"), item_count=1, build_request=builder(["x"])
        )
        call = asyncio.create_task(service.evaluate(batches))
        await started.wait()

        async def close_caller():
            asyncio.current_task().cancel("pending close cancellation")
            with pytest.raises(
                asyncio.CancelledError, match="pending close cancellation"
            ):
                await service.aclose()

        closer = asyncio.create_task(close_caller())
        await close_started.wait()
        assert not closer.done() and not service.physical_close_complete
        release_close.set()
        await closer
        with pytest.raises(asyncio.CancelledError):
            await call
        assert service.physical_close_complete

    asyncio.run(run())
