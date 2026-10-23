"""Transient product lifecycles and their only permitted database mutation."""

import asyncio
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.ephemeral import (
    AcceptedUserOrigin,
    SessionEphemeralProducts,
)
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.llm.ephemeral import EphemeralResult, EphemeralResultStatus
from pulsara_agent.llm.input import LLMMessage
from tests.test_conversation_fork import repo as repo, new_session, rows, turn
from tests.support.model_config import (
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)

_WIRE_PLAN = object()


def _host():
    binding = model_binding(model_runtime())

    async def run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    host = SimpleNamespace(
        session_id="session:test",
        host_session_id="host:test",
        _memory_domain_id="u_local",
        _lease=SimpleNamespace(guard=object()),
        _io=SimpleNamespace(run=run),
        _canonical_deadline=lambda: monotonic() + 30,
        _model_runtime=model_runtime(),
        _model=SimpleNamespace(transport_timeout_policy=object()),
        _closing=False,
        _active_task=None,
        model_call_binding=AsyncMock(return_value=binding),
        _input_continuity=SimpleNamespace(
            current_cohort=lambda scope: SimpleNamespace(
                view=SimpleNamespace(wire_input_plan=_WIRE_PLAN)
            )
        ),
        _compaction=SimpleNamespace(is_fenced=lambda **kw: False),
    )
    host.repository = SimpleNamespace(
        read_ephemeral_user_origin=lambda *a, **kw: AcceptedUserOrigin(
            kw["turn_id"],
            "entry:user",
            binding,
            LLMMessage.user("为什么缓存前缀要保持一致？"),
        ),
        has_pending_prompt=lambda **kw: False,
        adopt_generated_session_title=lambda **kw: True,
    )
    return host, binding


async def _drain(owner):
    while owner._tasks:
        await asyncio.gather(*tuple(owner._tasks))
        await asyncio.sleep(0)


def _result(binding):
    return SimpleNamespace(
        turn_id="turn:final",
        final_entry_id="entry:final",
        pending_plan_interaction_id=None,
        final_source=SimpleNamespace(
            call_target=SimpleNamespace(model_call_binding=binding),
            wire_input_plan=_WIRE_PLAN,
        ),
    )


def test_reads_never_start_work_and_completion_waits_for_admission(monkeypatch):
    async def scenario():
        host, binding = _host()
        owner = SessionEphemeralProducts(host)
        prepared = []
        monkeypatch.setattr(
            "pulsara_agent.llm.ephemeral.prepare_text_ephemeral_request",
            lambda **kw: prepared.append(kw) or "title",
        )
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.direct_model.prepare_suggestion_request",
            lambda **kw: prepared.append(kw) or "suggestion",
        )
        execute = AsyncMock(
            side_effect=lambda **kw: EphemeralResult(
                EphemeralResultStatus.COMPLETED,
                "缓存前缀" if kw["prepared"] == "title" else "怎样验证缓存命中？",
            )
        )
        monkeypatch.setattr(
            "pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute
        )
        for _ in range(3):
            assert owner.read()["suggestion"]["state"] == "absent"
        execute.assert_not_awaited()
        owner.activate("turn:origin")
        assert owner.read()["suggestion"]["state"] == "awaiting_completion"
        owner.admitted("turn:origin")
        owner.admitted("turn:origin")
        owner.completed("turn:origin", _result(binding))
        assert owner.read()["suggestion"]["state"] == "pending"
        await _drain(owner)
        assert owner.read()["title"]["state"] == "adopted"
        assert owner.read()["suggestion"]["text"] == "怎样验证缓存命中？"
        assert execute.await_count == 2
        assert next(item for item in prepared if "messages" in item)["messages"] == (
            LLMMessage.user("为什么缓存前缀要保持一致？"),
        )
        assert owner.read()["suggestion"]["turn_id"] == "turn:final"
        await owner.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason", ["nonhuman", "paused", "failure", "queue", "binding", "compacted"]
)
def test_ineligible_completions_do_not_request_suggestion(monkeypatch, reason):
    async def scenario():
        host, binding = _host()
        host.repository.read_ephemeral_user_origin = lambda *a, **kw: (
            None
            if reason == "nonhuman"
            else AcceptedUserOrigin(kw["turn_id"], "entry:u", binding, None)
        )
        if reason == "queue":
            host.repository.has_pending_prompt = lambda **kw: True
        if reason == "binding":
            host.model_call_binding.return_value = None
        if reason == "compacted":
            host._input_continuity.current_cohort = lambda scope: None
        owner = SessionEphemeralProducts(host)
        execute = AsyncMock()
        monkeypatch.setattr(
            "pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute
        )
        owner.activate("turn:origin")
        owner.admitted("turn:origin")
        result = _result(binding)
        if reason == "paused":
            result.pending_plan_interaction_id = "interaction:pending"
        owner.completed("turn:origin", None if reason == "failure" else result)
        await _drain(owner)
        assert owner.read()["suggestion"]["state"] == "absent"
        execute.assert_not_awaited()
        await owner.aclose()

    asyncio.run(scenario())


def test_next_root_rejects_late_result_even_if_provider_ignores_cancellation(
    monkeypatch,
):
    async def scenario():
        host, binding = _host()
        host.repository.read_ephemeral_user_origin = lambda *a, **kw: (
            AcceptedUserOrigin(kw["turn_id"], "entry:u", binding, None)
        )
        owner = SessionEphemeralProducts(host)
        started = asyncio.Event()

        async def execute(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return EphemeralResult(EphemeralResultStatus.COMPLETED, "过期问题？")

        monkeypatch.setattr(
            "pulsara_agent.llm.ephemeral.execute_ephemeral_request", execute
        )
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.direct_model.prepare_suggestion_request",
            lambda **kw: object(),
        )
        owner.activate("turn:origin")
        owner.admitted("turn:origin")
        owner.completed("turn:origin", _result(binding))
        await started.wait()
        owner.activate("turn:new")
        await _drain(owner)
        assert owner.read()["suggestion"]["text"] is None
        assert owner.read()["suggestion"]["origin_turn_id"] == "turn:new"
        await owner.aclose()

    asyncio.run(scenario())


def test_generated_title_only_mutates_null_title(repo):
    lease = new_session(repo)
    sid = lease.guard.session_id
    first, _, _ = turn(repo, lease.guard, "first accepted user input")
    origin = repo.read_ephemeral_user_origin(
        lease.guard, turn_id=first, deadline_monotonic=monotonic() + 30
    )
    assert origin is not None and origin.title_message == LLMMessage.user(
        "first accepted user input"
    )
    second, _, _ = turn(repo, lease.guard, "later input")
    assert (
        repo.read_ephemeral_user_origin(
            lease.guard, turn_id=second, deadline_monotonic=monotonic() + 30
        ).title_message
        is None
    )

    def snapshot():
        return rows(repo, "SELECT * FROM pulsara_v3.sessions WHERE id=%s", (sid,))[0]

    before = snapshot()
    assert repo.adopt_generated_session_title(
        session_id=sid,
        memory_domain_id="u_local",
        title="自动标题",
        deadline_monotonic=monotonic() + 30,
    )
    assert snapshot() == {**before, "title": "自动标题"}
    assert not repo.adopt_generated_session_title(
        session_id=sid,
        memory_domain_id="u_local",
        title="晚到的标题",
        deadline_monotonic=monotonic() + 30,
    )
    repo.rename_session(
        session_id=sid,
        memory_domain_id="u_local",
        title="用户改名",
        deadline_monotonic=monotonic() + 30,
    )
    assert not repo.adopt_generated_session_title(
        session_id=sid,
        memory_domain_id="u_local",
        title="覆盖用户",
        deadline_monotonic=monotonic() + 30,
    )
    assert snapshot()["title"] == "用户改名"


def test_generated_title_cannot_cross_domain_or_overwrite_manual_winner(repo):
    sid = new_session(repo).guard.session_id
    assert not repo.adopt_generated_session_title(
        session_id=sid,
        memory_domain_id="other",
        title="wrong",
        deadline_monotonic=monotonic() + 30,
    )
    repo.rename_session(
        session_id=sid,
        memory_domain_id="u_local",
        title="manual",
        deadline_monotonic=monotonic() + 30,
    )
    assert not repo.adopt_generated_session_title(
        session_id=sid,
        memory_domain_id="u_local",
        title="generated",
        deadline_monotonic=monotonic() + 30,
    )


def test_ephemeral_get_checks_user_scope_and_never_opens_host():
    async def scenario():
        core = SimpleNamespace(read_resumable_session=AsyncMock(return_value=object()))
        controller = SimpleNamespace(
            core=core,
            memory_domain_id="u_local",
            _lock=asyncio.Lock(),
            _closing=False,
            _operations={},
            _by_session={},
        )
        for _ in range(3):
            result = await LocalSessionController.read_ephemeral_results(
                controller, "session:x"
            )
            assert result["host_session_id"] is None
            assert result["suggestion"]["state"] == "absent"
        core.read_resumable_session.return_value = None
        with pytest.raises(KeyError):
            await LocalSessionController.read_ephemeral_results(controller, "session:x")

    asyncio.run(scenario())
