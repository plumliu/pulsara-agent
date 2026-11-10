"""Optional ROOT warming expires rather than recovering or catching up."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel import ephemeral
from pulsara_agent.conversation_kernel.ephemeral import SessionEphemeralProducts
from pulsara_agent.llm.ephemeral import EphemeralResult, EphemeralResultStatus
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.test_session_ephemeral import _host, _result


class Clock:
    def __init__(self):
        self.now = 0
        self.waiters = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        await future

    async def advance(self, at):
        self.now = at
        for due, future in self.waiters:
            if due <= at and not future.done():
                future.set_result(None)
        await settle()


async def settle():
    # Flush the real event loop, while only warming sees the controlled clock.
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def warming(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ephemeral, "asyncio", SimpleNamespace(**{
        **vars(asyncio),
        "get_running_loop": lambda: SimpleNamespace(time=lambda: clock.now),
        "sleep": clock.sleep,
    }))
    host, binding = _host()
    settings = replace(host._model_runtime.settings.read(),
                       cache_warming_enabled=True, input_suggestions_enabled=False)
    host._model_runtime.settings = SimpleNamespace(read=lambda: settings)
    owner = SessionEphemeralProducts(host)
    prepared = []
    calls = []

    def prepare(**kwargs):
        prepared.append(kwargs)
        return kwargs

    async def execute(**kwargs):
        calls.append(clock.now)
        return EphemeralResult(EphemeralResultStatus.COMPLETED, "OK")

    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.direct_model.prepare_conversation_ephemeral_request",
        prepare,
    )
    monkeypatch.setattr("pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute)
    return SimpleNamespace(clock=clock, host=host, binding=binding, owner=owner,
                           prepared=prepared, calls=calls, settings=settings)


async def complete(warming, turn="turn:origin"):
    # No human admission: scheduled ROOT completion qualifies independently.
    warming.owner.activate(turn)
    warming.owner.completed(turn, _result(warming.binding))
    await settle()


def test_fixed_idle_window_and_fresh_execution(warming):
    async def scenario():
        await complete(warming)
        assert warming.calls == []
        for at in (240, 480, 720, 960, 1200):
            await warming.clock.advance(at)
        assert warming.calls == [240, 480, 720, 960, 1200]
        assert {item["purpose"] for item in warming.prepared} == {
            ModelCallPurpose.CONVERSATION_CACHE_WARMING
        }
        assert len({id(item["source"]) for item in warming.prepared}) == 1
        await warming.clock.advance(1500)
        assert len(warming.calls) == 5
        # Reading an idle session never rearms it.
        warming.owner.read()
        await warming.clock.advance(1800)
        assert len(warming.calls) == 5
        await complete(warming, "turn:next")
        await warming.clock.advance(2040)
        assert warming.calls[-1] == 2040
        await warming.owner.aclose()
        assert warming.owner.physical_close_complete

    asyncio.run(scenario())


@pytest.mark.parametrize("wake,expected", [(800, [800, 960]), (1201, [])])
def test_delayed_wakeup_never_catches_up_or_extends(warming, wake, expected):
    async def scenario():
        await complete(warming)
        await warming.clock.advance(wake)
        if wake < 960:
            await warming.clock.advance(960)
        assert warming.calls == expected
        await warming.clock.advance(1440)
        assert warming.calls == expected
        await warming.owner.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["disabled", "subagent", "failed", "paused", "no_final"])
def test_no_warming_without_eligible_root_completion(warming, reason):
    async def scenario():
        if reason == "disabled":
            warming.host._model_runtime.settings.read = lambda: replace(
                warming.settings, cache_warming_enabled=False)
        if reason == "subagent":
            warming.host.root_session_id = "session:parent"
        warming.owner.activate("turn:origin")
        result = _result(warming.binding)
        if reason == "paused":
            result.pending_plan_interaction_id = "interaction:pending"
        if reason == "no_final":
            result.final_source = None
        warming.owner.completed("turn:origin", None if reason == "failed" else result)
        await settle()
        await warming.clock.advance(240)
        assert warming.prepared == warming.calls == []
        assert not warming.owner._tasks
        await warming.owner.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["model", "queue", "compaction", "closed", "preference"])
def test_changed_context_ends_window_without_resume(warming, reason):
    async def scenario():
        await complete(warming)
        await warming.clock.advance(240)
        if reason == "model":
            warming.host.model_call_binding.return_value = None
        elif reason == "queue":
            warming.host.repository.has_pending_prompt = lambda **kw: True
        elif reason == "compaction":
            warming.host._compaction.is_fenced = lambda **kw: True
        elif reason == "closed":
            warming.host._closing = True
        else:
            warming.host._model_runtime.settings.read = lambda: replace(
                warming.settings, cache_warming_enabled=False)
        await warming.clock.advance(480)
        warming.host.model_call_binding.return_value = warming.binding
        warming.host.repository.has_pending_prompt = lambda **kw: False
        warming.host._compaction.is_fenced = lambda **kw: False
        warming.host._closing = False
        warming.host._model_runtime.settings.read = lambda: warming.settings
        await warming.clock.advance(720)
        assert warming.calls == [240]
        await warming.owner.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("during_run", [False, True])
def test_explicit_invalidation_cannot_revive_on_old_completion(warming, during_run):
    async def scenario():
        warming.owner.activate("turn:origin")
        if not during_run:
            warming.owner.completed("turn:origin", _result(warming.binding))
            await settle()
        warming.owner.invalidate_cache_warming()
        # Covers model A -> B -> A and off -> on before the old completion.
        warming.owner.completed("turn:origin", _result(warming.binding))
        await settle()
        await warming.clock.advance(240)
        assert warming.calls == []
        await warming.owner.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["error", "tool", "incomplete", "preparation"])
def test_first_unsuccessful_ping_ends_window(warming, monkeypatch, failure):
    async def scenario():
        execute = AsyncMock(side_effect=RuntimeError("provider failed"))
        if failure in {"tool", "incomplete"}:
            execute.side_effect = None
            execute.return_value = EphemeralResult(
                EphemeralResultStatus.TOOL_CALL if failure == "tool"
                else EphemeralResultStatus.OUTPUT_INCOMPLETE, "")
        monkeypatch.setattr("pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute)
        if failure == "preparation":
            def reject(**kwargs):
                raise ValueError("prefix or budget unavailable")
            monkeypatch.setattr(
                "pulsara_agent.conversation_kernel.direct_model.prepare_conversation_ephemeral_request",
                reject,
            )
        await complete(warming)
        for at in (240, 480, 720):
            await warming.clock.advance(at)
        assert execute.await_count == (0 if failure == "preparation" else 1)
        await warming.owner.aclose()

    asyncio.run(scenario())


def test_cancellation_rejects_late_provider_and_blocks_overlap(warming, monkeypatch):
    async def scenario():
        entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def execute(**kwargs):
            warming.calls.append(warming.clock.now)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                closing.set()
                await release.wait()
                return EphemeralResult(EphemeralResultStatus.COMPLETED, "late OK")

        monkeypatch.setattr("pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute)
        await complete(warming)
        await warming.clock.advance(240)
        assert entered.is_set()
        warming.owner.invalidate_cache_warming()
        await closing.wait()
        # A new foreground run can finish without awaiting the old close.
        await complete(warming, "turn:new")
        release.set()
        await settle()
        assert warming.owner._warming_task is None
        await warming.clock.advance(480)
        assert warming.calls == [240]
        await warming.owner.aclose()

    asyncio.run(scenario())


def test_slow_ping_skips_slots_and_keeps_original_deadline(warming, monkeypatch):
    async def scenario():
        async def execute(**kwargs):
            warming.calls.append(warming.clock.now)
            if len(warming.calls) == 1:
                warming.clock.now = 900  # The first response crossed two slots.
            return EphemeralResult(EphemeralResultStatus.COMPLETED, "OK")

        monkeypatch.setattr("pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute)
        await complete(warming)
        await warming.clock.advance(240)
        assert warming.calls == [240]
        await warming.clock.advance(960)
        assert warming.calls == [240, 960]
        await warming.clock.advance(1201)
        assert warming.calls == [240, 960]
        await warming.owner.aclose()

    asyncio.run(scenario())


def test_invalidation_during_final_check_never_opens_provider(warming):
    async def scenario():
        await complete(warming)

        async def binding():
            if warming.prepared:
                warming.owner.invalidate_cache_warming()
            return warming.binding

        warming.host.model_call_binding.side_effect = binding
        await warming.clock.advance(240)
        assert len(warming.prepared) == 1
        assert warming.calls == []
        await warming.owner.aclose()

    asyncio.run(scenario())


def test_loaded_roots_have_independent_windows(warming):
    async def scenario():
        other, binding = _host()
        other._model_runtime.settings = warming.host._model_runtime.settings
        owner = SessionEphemeralProducts(other)
        owner.activate("turn:other")
        owner.completed("turn:other", _result(binding))
        await complete(warming)
        await warming.clock.advance(240)
        assert warming.calls == [240, 240]
        warming.owner.invalidate_cache_warming()
        await settle()
        await warming.clock.advance(480)
        assert warming.calls == [240, 240, 480]
        await warming.owner.aclose()
        await owner.aclose()

    asyncio.run(scenario())
