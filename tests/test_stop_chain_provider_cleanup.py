"""Stop-plan D1–D3: production consumer/DirectModel owns physical cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pulsara_agent.conversation_kernel.direct_model import PreparedKernelModelExecution
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    live_digest,
)
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from tests.support.model_config import (
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)


class ControlledTransport:
    def __init__(
        self,
        *,
        oversized=False,
        close_error=None,
        blocked=False,
        completed=False,
        read_error=None,
    ):
        items = [
            TextStartPayload("text"),
            TextDeltaPayload("text", "x" * ((4 << 20) + 1) if oversized else "hello"),
        ]
        if completed:
            items.extend(
                (
                    TextEndPayload("text", "hello", 5, live_digest("hello")),
                    ProviderStreamTerminal(
                        ProviderNormalizedTerminalKind.COMPLETED,
                        TransportUsageReport(usage_status="missing", usage=None),
                    ),
                )
            )
        self.items = iter(items)
        self.read_error = read_error
        self.reading = asyncio.Event()
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_error = close_error
        self.blocked = blocked
        self.close_count = 0
        self.completion_count = 0
        self.close_cancelled = False
        self.observed_usage = None

    async def read_next(self):
        item = next(self.items, None)
        if item is not None:
            return item
        self.reading.set()
        if self.read_error is not None:
            raise self.read_error
        await asyncio.Event().wait()

    async def aclose(self):
        self.close_count += 1
        self.closing.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        if self.close_error is not None:
            raise self.close_error

    async def wait_physical_completion(self):
        self.completion_count += 1
        return ProviderPhysicalCompletion(
            ProviderPhysicalCompletionStatus.BLOCKED
            if self.blocked
            else ProviderPhysicalCompletionStatus.COMPLETED,
            "test_unconfirmed" if self.blocked else None,
        )


def setup_execution(monkeypatch, transport, usage=None):
    # Only admission and physical transport are controlled. The real
    # PreparedKernelModelExecution.open_once and Runner consumer are exercised.
    session_id, turn_id = random_id("session"), random_id("turn")
    scope = SimpleNamespace(session_id=session_id, scope_kind=ModelInputScopeKind.ROOT)
    permit = SimpleNamespace(scope=scope, epoch_nonce="epoch", epoch_revision=1)
    timeout = OpenAITransportTimeoutPolicy(1, 1, 1, 1, None)
    configured = model_runtime(wire_api="openai_chat_completions")
    target = configured.resolve_target(
        model_binding(configured), timeout_policy=timeout
    )
    call = SimpleNamespace(
        fact=object(), resolved_model_call_id=random_id("call"), target=target
    )
    request = SimpleNamespace(
        session_id=session_id,
        turn_id=turn_id,
        compiled_input=SimpleNamespace(
            tools=(),
            canonical_input_identity=SimpleNamespace(
                conversation_scope_kind=ModelInputScopeKind.ROOT
            ),
        ),
        prepared_call=SimpleNamespace(
            call=call,
            epoch_call_target=object(),
            transport_timeout_policy_fingerprint="timeout",
        ),
    )
    borrowed = SimpleNamespace(
        call=call, open_stream=Mock(return_value=transport), close=Mock()
    )
    runtime = SimpleNamespace(borrow_transport=Mock(return_value=borrowed))
    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.direct_model._issue_epoch_agent_loop_provider_open_permit",
        lambda **kwargs: object(),
    )
    usage = usage if usage is not None else AsyncMock()
    execution = PreparedKernelModelExecution(
        request=request,
        final_context=LLMContext((), "context", call.resolved_model_call_id, 1),
        append_candidate=SimpleNamespace(
            epoch_nonce="epoch", expected_epoch_revision=0
        ),
        transport_timeout_policy_fingerprint="timeout",
        model_runtime=runtime,
        transport_timeout_policy=timeout,
        install_authority=SimpleNamespace(consume=Mock()),
        usage_observer=usage,
        transport_invocation_observer=None,
    )
    runner = ConversationKernelRunner.__new__(ConversationKernelRunner)
    runner._active_execution = None
    runner._live_bus = LiveAgentEventBus()
    runner._resolved_workspace_id = AsyncMock(return_value=random_id("workspace"))
    return runner, execution, permit, borrowed, usage


def collect(runner, execution, permit):
    return runner._collect_model(
        execution.request,
        execution=execution,
        permit=permit,
        proposed_entry_id=random_id("entry"),
    )


@pytest.mark.parametrize("cancel_during_close", [False, True])
def test_assembler_failure_joins_close_before_releasing_execution(
    monkeypatch, cancel_during_close
):
    async def run():
        transport = ControlledTransport(oversized=True)
        runner, execution, permit, borrowed, usage = setup_execution(
            monkeypatch, transport
        )
        task = asyncio.create_task(collect(runner, execution, permit))
        try:
            await asyncio.wait_for(transport.closing.wait(), 2)
            if cancel_during_close:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            assert runner._active_execution is execution
            borrowed.close.assert_not_called()
            assert not transport.close_cancelled
        finally:
            transport.release_close.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, RuntimeError)
        assert str(result) == "provider assistant draft exceeds bounded assembly"
        assert runner._active_execution is None
        assert transport.close_count == 1
        borrowed.close.assert_called_once()
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is False

    asyncio.run(run())


@pytest.mark.parametrize("cancel_at", ["close", "usage"])
def test_read_cancellation_joins_cleanup_despite_repeated_cancel(
    monkeypatch, cancel_at
):
    async def run():
        transport = ControlledTransport()
        usage_entered, release_usage = asyncio.Event(), asyncio.Event()
        calls = []

        async def usage(*args):
            calls.append(args)
            usage_entered.set()
            await release_usage.wait()

        runner, execution, permit, borrowed, _ = setup_execution(
            monkeypatch, transport, usage
        )
        task = asyncio.create_task(collect(runner, execution, permit))
        try:
            await asyncio.wait_for(transport.reading.wait(), 2)
            task.cancel("original")
            await asyncio.wait_for(transport.closing.wait(), 2)
            if cancel_at == "usage":
                transport.release_close.set()
                await asyncio.wait_for(usage_entered.wait(), 2)
            task.cancel("repeat")
            await asyncio.sleep(0)
            task.cancel("again")
            await asyncio.sleep(0)
            assert not task.done()
            assert runner._active_execution is execution
            assert not transport.close_cancelled
        finally:
            transport.release_close.set()
            release_usage.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert runner._active_execution is None
        assert transport.close_count == 1
        borrowed.close.assert_called_once()
        assert len(calls) == 1 and calls[0][-1] is False

    asyncio.run(run())


@pytest.mark.parametrize("blocked", [False, True])
def test_generator_close_exposes_physical_failure(monkeypatch, blocked):
    async def run():
        failure = RuntimeError("physical close failed")
        transport = ControlledTransport(
            close_error=None if blocked else failure, blocked=blocked
        )
        transport.release_close.set()
        _, execution, permit, borrowed, usage = setup_execution(monkeypatch, transport)
        stream = execution.open_once(permit)
        await anext(stream)
        with pytest.raises(RuntimeError) as caught:
            await stream.aclose()
        if blocked:
            assert str(caught.value) == "provider physical operation did not exit"
        else:
            assert caught.value is failure
        assert execution.state != "PHYSICALLY_CLOSED"
        borrowed.close.assert_called_once()
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is False

    asyncio.run(run())


@pytest.mark.parametrize("primary_kind", ["consumer", "read"])
def test_primary_error_and_close_error_are_both_preserved(monkeypatch, primary_kind):
    async def run():
        close_error, read_error = (
            RuntimeError("close failure"),
            ValueError("read failure"),
        )
        transport = ControlledTransport(
            oversized=primary_kind == "consumer",
            read_error=read_error if primary_kind == "read" else None,
            close_error=close_error,
        )
        transport.release_close.set()
        runner, execution, permit, borrowed, usage = setup_execution(
            monkeypatch, transport
        )
        captured = []
        if primary_kind == "consumer":
            from pulsara_agent.conversation_kernel.assembler import (
                ProviderStreamAssembler,
            )

            original = ProviderStreamAssembler.apply

            def observe_error(self, item):
                try:
                    return original(self, item)
                except BaseException as exc:
                    captured.append(exc)
                    raise

            monkeypatch.setattr(ProviderStreamAssembler, "apply", observe_error)
        with pytest.raises((RuntimeError, ValueError)) as caught:
            await collect(runner, execution, permit)
        assert caught.value is (
            captured[0] if primary_kind == "consumer" else read_error
        )
        assert caught.value.__cause__ is close_error
        assert runner._active_execution is None
        borrowed.close.assert_called_once()
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is False

    asyncio.run(run())


@pytest.mark.parametrize("cancel_at", ["close", "usage"])
def test_first_cancellation_during_completion_cleanup_is_propagated(
    monkeypatch, cancel_at
):
    async def run():
        transport = ControlledTransport(completed=True)
        entered, release = asyncio.Event(), asyncio.Event()
        observations = []

        async def usage(*args):
            observations.append(args)
            entered.set()
            await release.wait()

        runner, execution, permit, borrowed, _ = setup_execution(
            monkeypatch, transport, usage
        )
        task = asyncio.create_task(collect(runner, execution, permit))
        try:
            await asyncio.wait_for(transport.closing.wait(), 2)
            if cancel_at == "usage":
                transport.release_close.set()
                await asyncio.wait_for(entered.wait(), 2)
            task.cancel("first in cleanup")
            await asyncio.sleep(0)
            task.cancel("repeat in cleanup")
            await asyncio.sleep(0)
            assert not task.done()
            assert runner._active_execution is execution
            assert not transport.close_cancelled
        finally:
            transport.release_close.set()
            release.set()
            outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        assert runner._active_execution is None
        assert len(observations) == 1
        # The already submitted usage observation is not retroactively revoked.
        assert observations[0][-1] is (cancel_at == "usage")
        borrowed.close.assert_called_once()

    asyncio.run(run())


@pytest.mark.parametrize("usage_fails", [False, True])
def test_normal_completion_and_best_effort_usage_close_once(
    monkeypatch, caplog, usage_fails
):
    async def run():
        transport = ControlledTransport(completed=True)
        transport.release_close.set()
        observer = AsyncMock(
            side_effect=ValueError("usage unavailable") if usage_fails else None
        )
        runner, execution, permit, borrowed, usage = setup_execution(
            monkeypatch, transport, observer
        )
        result = await collect(runner, execution, permit)
        assert result.completed.public_text == "hello"
        assert runner._active_execution is None
        assert execution.state == "PHYSICALLY_CLOSED"
        assert transport.close_count == 1 and transport.completion_count == 1
        borrowed.close.assert_called_once()
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is True
        assert ("provider_usage_observation_failed" in caplog.text) is usage_fails

    asyncio.run(run())


@pytest.mark.parametrize("close_fails", [False, True])
def test_explicit_generator_close_preserves_first_cleanup_cancellation(
    monkeypatch, close_fails
):
    async def run():
        failure = RuntimeError("close failed during cancellation")
        transport = ControlledTransport(close_error=failure if close_fails else None)
        _, execution, permit, borrowed, usage = setup_execution(monkeypatch, transport)
        stream = execution.open_once(permit)
        await anext(stream)
        task = asyncio.create_task(stream.aclose())
        try:
            await asyncio.wait_for(transport.closing.wait(), 2)
            task.cancel("first close cancellation")
            await asyncio.sleep(0)
            task.cancel("second close cancellation")
            await asyncio.sleep(0)
            assert not task.done() and not transport.close_cancelled
        finally:
            transport.release_close.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("first close cancellation",)
        assert caught.value.__cause__ is (failure if close_fails else None)
        borrowed.close.assert_called_once()
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is False

    asyncio.run(run())


def test_completed_terminal_cannot_hide_close_failure(monkeypatch):
    async def run():
        failure = RuntimeError("completed response close failed")
        transport = ControlledTransport(completed=True, close_error=failure)
        transport.release_close.set()
        runner, execution, permit, borrowed, usage = setup_execution(
            monkeypatch, transport
        )
        with pytest.raises(RuntimeError) as caught:
            await collect(runner, execution, permit)
        assert caught.value is failure
        assert execution.state != "PHYSICALLY_CLOSED"
        assert runner._active_execution is None
        borrowed.close.assert_called_once()
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is False

    asyncio.run(run())


def test_cancellation_requested_before_usage_submission_prevents_anchor(monkeypatch):
    async def run():
        transport = ControlledTransport(completed=True)
        runner, execution, permit, borrowed, usage = setup_execution(
            monkeypatch, transport
        )
        task = asyncio.create_task(collect(runner, execution, permit))
        await asyncio.wait_for(transport.closing.wait(), 2)
        # Queue the cleanup continuation first, then cancel its waiter in the
        # same event-loop tick. Submission must inspect the cancellation request
        # even before the waiter's shield has resumed with CancelledError.
        transport.release_close.set()
        task.cancel("before usage submission")
        with pytest.raises(asyncio.CancelledError):
            await task
        usage.assert_awaited_once()
        assert usage.call_args.args[-1] is False
        borrowed.close.assert_called_once()
        assert runner._active_execution is None

    asyncio.run(run())


@pytest.mark.parametrize("consumer_error", [False, True])
def test_cleanup_task_cancellation_preserves_original_exception(
    monkeypatch, consumer_error
):
    async def run():
        transport = ControlledTransport(
            completed=not consumer_error, oversized=consumer_error
        )
        transport.release_close.set()
        original = asyncio.CancelledError("usage observer cancelled itself")
        usage = AsyncMock(side_effect=original)
        runner, execution, permit, borrowed, _ = setup_execution(
            monkeypatch, transport, usage
        )
        expected = RuntimeError if consumer_error else asyncio.CancelledError
        with pytest.raises(expected) as caught:
            await collect(runner, execution, permit)
        if consumer_error:
            assert (
                str(caught.value) == "provider assistant draft exceeds bounded assembly"
            )
            assert caught.value.__cause__ is original
        else:
            assert caught.value is original
        borrowed.close.assert_called_once()
        assert runner._active_execution is None

    asyncio.run(run())


@pytest.mark.parametrize("api", ["openai_chat_completions", "openai_responses"])
def test_consumer_failure_joins_real_sdk_stream_close(monkeypatch, api):
    import openai
    from pulsara_agent.conversation_kernel.assembler import ProviderStreamAssembler
    from tests.test_provider_stream_retry import (
        execution as sdk_execution,
        partial,
        server,
    )

    failure = RuntimeError("consumer rejected a decoded SDK delta")
    original_apply = ProviderStreamAssembler.apply
    original_close = openai.AsyncStream.close

    def consume(self, item):
        original_apply(self, item)
        if isinstance(item, TextDeltaPayload):
            raise failure

    monkeypatch.setattr(ProviderStreamAssembler, "apply", consume)
    with server([{"events": partial(api)}]) as (endpoint, requests):

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            closes = []

            async def close(resource):
                entered.set()
                await release.wait()
                await original_close(resource)
                closes.append(resource)

            monkeypatch.setattr(openai.AsyncStream, "close", close)
            transport, retries, _, _, _ = sdk_execution(api, endpoint)
            runner, execution, permit, borrowed, usage = setup_execution(
                monkeypatch, transport
            )
            task = asyncio.create_task(collect(runner, execution, permit))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel("stop during SDK close")
                await asyncio.sleep(0)
                task.cancel("stop again")
                await asyncio.sleep(0)
                assert not task.done() and not closes
                assert runner._active_execution is execution
                borrowed.close.assert_not_called()
            finally:
                release.set()
            with pytest.raises(RuntimeError) as caught:
                await task
            assert caught.value is failure
            assert len(closes) == len(requests) == 1 and retries == []
            borrowed.close.assert_called_once()
            usage.assert_awaited_once()
            assert usage.call_args.args[-1] is False
            assert runner._active_execution is None

        asyncio.run(run())


def test_historical_handled_cancellation_does_not_disable_usage_anchor(monkeypatch):
    async def run():
        caller = asyncio.current_task()
        caller.cancel("previously handled cancellation")
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert caller.cancelling() == 1
        transport = ControlledTransport(completed=True)
        transport.release_close.set()
        runner, execution, permit, _, usage = setup_execution(monkeypatch, transport)
        result = await collect(runner, execution, permit)
        assert result.completed.public_text == "hello"
        assert usage.call_args.args[-1] is True
        assert caller.cancelling() == 1

    asyncio.run(run())


@pytest.mark.parametrize("consumer_error", [False, True])
def test_existing_primary_cause_survives_cleanup_failure(monkeypatch, consumer_error):
    async def run():
        from pulsara_agent.conversation_kernel.assembler import ProviderStreamAssembler

        primary = ValueError("original failure")
        original_cause = OSError("original underlying failure")
        primary.__cause__ = original_cause
        secondary = RuntimeError("close detail secret-fixture")
        transport = ControlledTransport(read_error=primary, close_error=secondary)
        transport.release_close.set()
        if consumer_error:
            original_apply = ProviderStreamAssembler.apply

            def consume(self, item):
                original_apply(self, item)
                if isinstance(item, TextDeltaPayload):
                    raise primary

            monkeypatch.setattr(ProviderStreamAssembler, "apply", consume)
        runner, execution, permit, _, _ = setup_execution(monkeypatch, transport)
        execution._bind_provider_error_scrubber(
            lambda text: text.replace("secret-fixture", "[redacted]")
        )
        with pytest.raises(ValueError) as caught:
            await collect(runner, execution, permit)
        assert caught.value is primary
        assert primary.__cause__ is original_cause
        assert any("close detail [redacted]" in note for note in primary.__notes__)
        assert all("secret-fixture" not in note for note in primary.__notes__)

    asyncio.run(run())


def test_existing_cleanup_cause_survives_additional_borrow_failure(monkeypatch):
    async def run():
        close_error = RuntimeError("physical close failed")
        original_cause = OSError("socket detail")
        close_error.__cause__ = original_cause
        transport = ControlledTransport(close_error=close_error)
        transport.release_close.set()
        _, execution, permit, borrowed, _ = setup_execution(monkeypatch, transport)
        borrowed.close.side_effect = ValueError("borrow release failed")
        stream = execution.open_once(permit)
        await anext(stream)
        with pytest.raises(RuntimeError) as caught:
            await stream.aclose()
        assert caught.value is close_error
        assert close_error.__cause__ is original_cause
        assert any("borrow release failed" in note for note in close_error.__notes__)

    asyncio.run(run())
