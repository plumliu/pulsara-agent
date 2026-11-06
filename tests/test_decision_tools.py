"""Public decide input, frozen configuration and result admission contracts."""

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.decision_tools import (
    KernelDecisionToolPort,
    validate_decision_arguments,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
from pulsara_agent.retrieval.config import RetrievalConnection
from pulsara_agent.settings import LocalSettingsUnavailable


class Settings:
    def __init__(self, connection=None, mode="off", error=None):
        self.connection, self.mode, self.error, self.reads = connection, mode, error, 0

    def read(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(memory_retrieval=SimpleNamespace(decision=self.connection, ranking_mode=self.mode))


class Service:
    def __init__(self, probabilities=()):
        self.probabilities = probabilities
        self.prepared = []
        self.evaluated = []

    def prepare_batches(self, connection, *, item_count, build_request):
        value = build_request(0, item_count)
        self.prepared.append((connection, value))
        return (value,)

    async def evaluate(self, batches, *, deadline_monotonic, cancel_event):
        self.evaluated.append((batches, deadline_monotonic, cancel_event))
        return self.probabilities


class Deadlines:
    def __init__(self):
        self.owners = []

    def deadline(self, owner):
        self.owners.append(owner)
        return 1234.0


def connection(*, key="saved-key"):
    return RetrievalConnection("https://example.invalid/decisions", "decision-model", "openai_decisions", api_key=key)


def context(budget=None):
    return SimpleNamespace(tool_call_id="test-call", page_budget=budget, cell_cancel_event=asyncio.Event())


def port(settings, service=None, *, maximum=4096):
    deadlines = Deadlines()
    service = service or Service()
    return KernelDecisionToolPort(settings=settings, decision_service=service, deadline_factory=deadlines,
                                  max_value_bytes=maximum), service, deadlines


@pytest.mark.parametrize("arguments", [None, [], {}, {"condition": "x"}, {"inputs": []},
    {"condition": " \n", "inputs": []}, {"condition": 1, "inputs": []},
    {"condition": "x", "inputs": None}, {"condition": "x", "inputs": ("x",)},
    {"condition": "x", "inputs": [None]}, {"condition": "x", "inputs": [False]},
    {"condition": "x", "inputs": [], "threshold": .5}])
def test_decide_rejects_invalid_arguments_before_settings_or_network(arguments):
    settings = Settings(error=AssertionError("settings must not be read"))
    owner, service, _ = port(settings)
    with pytest.raises(ValueError):
        asyncio.run(owner.invoke(arguments=arguments, invocation_context=context()))
    assert settings.reads == 0 and service.prepared == service.evaluated == []


def test_decide_empty_inputs_do_not_read_settings_or_acquire_a_deadline():
    settings = Settings(error=AssertionError("empty call read settings"))
    owner, service, deadlines = port(settings)
    result = asyncio.run(owner.invoke(arguments={"condition": " valid condition ", "inputs": []}, invocation_context=context()))
    assert result.public_value() == {"decisions": []}
    assert result.state == "SUCCESS" and result.page_complete
    assert settings.reads == 0 and deadlines.owners == []
    assert service.prepared == service.evaluated == []


@pytest.mark.parametrize("mode", ["off", "rerank", "decision"])
def test_decide_preserves_order_text_and_threshold_independent_of_memory_mode(mode):
    settings = Settings(connection(), mode)
    owner, service, deadlines = port(settings, Service((0, .499999, .5, 1)))
    invocation = context()
    result = asyncio.run(owner.invoke(
        arguments={"condition": "  Is this relevant?  ", "inputs": ["", "duplicate", "duplicate", "字"]},
        invocation_context=invocation,
    ))
    assert result.public_value() == {"decisions": [False, False, True, True]}
    assert settings.reads == 1
    assert deadlines.owners == [KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION]
    selected, request = service.prepared[0]
    assert selected is settings.connection
    assert [item["text"] for item in request.state["inputs"]] == ["", "duplicate", "duplicate", "字"]
    assert all("  Is this relevant?  " in question.instructions for question in request.questions)
    assert service.evaluated[0][1:] == (1234.0, invocation.cell_cancel_event)


@pytest.mark.parametrize("configured", [None, connection(key=None), connection()])
def test_decide_available_checks_only_current_configuration(configured):
    owner, service, _ = port(Settings(configured))
    value = owner.available()
    assert type(value) is bool and value is (configured is not None and configured.complete)
    assert service.prepared == service.evaluated == []


def test_decide_settings_error_is_distinct_from_missing_configuration(caplog):
    owner, service, _ = port(Settings(error=LocalSettingsUnavailable("settings directory is unreadable")))
    assert owner.available() is False
    assert "settings directory is unreadable" in caplog.text
    result = asyncio.run(owner.invoke(arguments={"condition": "x", "inputs": ["text"]}, invocation_context=context()))
    assert result.state == "APPLICATION_ERROR"
    assert result.public_value()["error"] == "DECISION_UNAVAILABLE"
    assert "settings directory is unreadable" in result.public_value()["message"]
    assert "decisions" not in result.public_value()
    assert service.prepared == service.evaluated == []


@pytest.mark.parametrize("bridge_boundary", [False, True])
def test_decide_quotes_longest_complete_result_before_network(bridge_boundary):
    settings = Settings(connection())
    owner, service, _ = port(settings, maximum=1 if bridge_boundary else 4096)
    quote = ToolResultPageBudget("test-call", accepts_provider_body=lambda body, _ids: False)
    result = asyncio.run(owner.invoke(arguments={"condition": "x", "inputs": ["text"]},
                                     invocation_context=context(None if bridge_boundary else quote)))
    assert result.state == "APPLICATION_ERROR"
    assert result.public_value()["error"] == "DECISION_RESOURCE_BOUNDARY"
    assert "decisions" not in result.public_value()
    assert service.prepared == service.evaluated == []


def test_decide_validation_retains_original_text():
    assert validate_decision_arguments({"condition": " x ", "inputs": ["", " a "]}) == (" x ", ("", " a "))


@pytest.mark.parametrize("phase", ["prepare", "evaluate"])
def test_decide_preserves_service_failure_code_and_resource_details_without_partial_result(phase):
    from pulsara_agent.retrieval.decision import DecisionError

    error = DecisionError("DECISION_RESOURCE_BOUNDARY", "input exceeds request budget", input_index=1, limit=192 * 1024)

    class FailingService(Service):
        def prepare_batches(self, *args, **kwargs):
            if phase == "prepare":
                raise error
            return super().prepare_batches(*args, **kwargs)

        async def evaluate(self, *args, **kwargs):
            raise error

    owner, _, _ = port(Settings(connection()), FailingService())
    result = asyncio.run(owner.invoke(arguments={"condition": "x", "inputs": ["one", "two"]}, invocation_context=context()))
    assert result.state == "APPLICATION_ERROR"
    assert result.public_value() == {"error": error.code, "message": str(error), "input_index": 1, "limit": 192 * 1024}


@pytest.mark.parametrize("failure", [asyncio.CancelledError("cell stopped"), TimeoutError("whole call expired")])
def test_decide_propagates_cancellation_and_deadline_to_existing_tool_settlement(failure):
    class InterruptedService(Service):
        async def evaluate(self, *args, **kwargs):
            raise failure

    owner, _, _ = port(Settings(connection()), InterruptedService())

    async def run():
        with pytest.raises(type(failure)) as observed:
            await owner.invoke(arguments={"condition": "x", "inputs": ["one"]}, invocation_context=context())
        assert observed.value is failure

    asyncio.run(run())


def test_decide_each_call_reads_new_configuration_but_does_not_reselect_in_flight():
    settings = Settings(connection())

    class UpdatingService(Service):
        async def evaluate(self, batches, **kwargs):
            settings.connection = None
            return await super().evaluate(batches, **kwargs)

    owner, service, _ = port(settings, UpdatingService((1,)))

    async def run():
        result = await owner.invoke(arguments={"condition": "x", "inputs": ["text"]}, invocation_context=context())
        assert result.public_value() == {"decisions": [True]}
        assert settings.reads == 1
        second = await owner.invoke(arguments={"condition": "x", "inputs": ["text"]}, invocation_context=context())
        assert second.public_value()["error"] == "DECISION_UNAVAILABLE"
        assert settings.reads == 2
        assert len(service.prepared) == len(service.evaluated) == 1

    asyncio.run(run())


@pytest.mark.parametrize("shape", ["system_one", "openai_decisions"])
def test_decide_real_service_batches_and_restores_order(monkeypatch, shape):
    import json
    import httpx2
    from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
    from pulsara_agent.retrieval.decision import DecisionService
    from tests.test_memory_retrieval_adapters import install_transport

    def respond(request):
        payload = json.loads(request.content)
        names = list(payload["questions"]) if shape == "system_one" else [q["name"] for q in payload["questions"]]
        values = {name: (0, .5, 1)[int(name.removeprefix("input_")) % 3] for name in names}
        answers = ({name: {"type": "noul", "noul": values[name]} for name in reversed(names)}
                   if shape == "system_one" else
                   [{"name": name, "type": "predicate", "probability": values[name]} for name in reversed(names)])
        return httpx2.Response(200, json={"answers": answers})

    observed = install_transport(monkeypatch, respond)
    settings = Settings(RetrievalConnection("https://provider.example/decisions", "configured", shape, "none"))

    async def run():
        service = DecisionService()
        owner = KernelDecisionToolPort(settings=settings, decision_service=service,
            deadline_factory=KernelExecutionDeadlineFactory(), max_value_bytes=4096)
        try:
            result = await owner.invoke(arguments={"condition": "Does the material satisfy the requirement?",
                "inputs": ["duplicate"] * 23}, invocation_context=context())
            assert result.public_value() == {"decisions": [index % 3 != 0 for index in range(23)]}
            assert settings.reads == 1
            assert [len(json.loads(request.content)["questions"]) for request in observed] == [20, 3]
        finally:
            await service.aclose()
        assert service.physical_close_complete

    asyncio.run(run())


@pytest.mark.parametrize("closing", [False, True])
def test_memory_retains_own_request_until_sdk_cleanup_without_closing_shared_service(closing):
    from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
    from unittest.mock import AsyncMock

    async def run():
        memory = KernelMemoryToolPort.__new__(KernelMemoryToolPort)
        memory._closed, memory._remote_tasks = False, set()
        memory._decision_service = SimpleNamespace(aclose=AsyncMock())
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def physical():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        task = asyncio.create_task(memory._run_remote_exact(physical(), timeout_seconds=10, name="test-memory-decision"))
        await entered.wait()
        close = asyncio.create_task(memory.aclose()) if closing else None
        if not closing:
            task.cancel("first")
        await cleaning.wait()
        waiter = close if closing else task
        waiter.cancel("repeat")
        barrier = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(barrier.set_result, None)
        await barrier
        assert not waiter.done()
        assert len(memory._remote_tasks) == 1
        assert not memory.physical_close_complete
        release.set()
        await asyncio.gather(task, *((close,) if close else ()), return_exceptions=True)
        assert not memory._remote_tasks
        memory._decision_service.aclose.assert_not_called()

    asyncio.run(run())
