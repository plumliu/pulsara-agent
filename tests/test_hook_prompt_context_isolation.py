"""Prompt Hook text follows actual Host input admission, including async output."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulsara_agent.hooks.executor import HookCommandExecution, HookSecretScrubSet
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from tests.support.round3 import CallbackScriptedKernelModel, ScriptedKernelModel
from tests.test_round4_plan_host import _open_test_session
from tests.test_round9_2_hook_integration_postgres import (
    _config,
    _install_trusted_sources,
    _text_stream,
    _tool_stream,
)

pytestmark = pytest.mark.postgres

_QUEUED_PROMPT = "queued input B"
_CONTEXT = "PROMPT_BOUND_CONTEXT:"


class _PromptExecutor:
    """Control command completion while keeping dispatcher parsing and settlement real."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, request):
        prompt = request.public_stdin["prompt"]
        if prompt == _QUEUED_PROMPT:
            self.started.set()
            await self.release.wait()
        return HookCommandExecution(
            request.definition,
            request.event_dispatch_ordinal,
            request.source_ordinal,
            0,
            (_CONTEXT + prompt).encode(),
            b"",
            scrub_set=HookSecretScrubSet.capture(),
        )


class _QueueModel(CallbackScriptedKernelModel):
    def __init__(self, *, run_queued: bool) -> None:
        self.active_started = asyncio.Event()
        self.active_release = asyncio.Event()
        self.queued_started = asyncio.Event()
        self.queued_release = asyncio.Event()
        self.run_queued = run_queued
        super().__init__(self._stream)

    async def _stream(self, request):
        index = len(self.requests)
        if index == 1:
            self.active_started.set()
            await self.active_release.wait()
            items = _tool_stream(
                "read_file",
                "call:prompt-isolation-A",
                {"target": {"kind": "PATH", "path": "input.txt"}},
            )
        elif index == 2:
            items = _text_stream("A finished", "text:prompt-isolation-A")
        elif index == 3 and self.run_queued:
            self.queued_started.set()
            await self.queued_release.wait()
            items = _tool_stream(
                "read_file",
                "call:prompt-isolation-B",
                {"target": {"kind": "PATH", "path": "input.txt"}},
            )
        elif index == 4 and self.run_queued:
            items = _text_stream("B finished", "text:prompt-isolation-B")
        elif index == 3:
            items = _text_stream("C finished", "text:prompt-isolation-C")
        else:
            raise AssertionError("unexpected model call")
        for item in items:
            yield item


async def _open(
    tmp_path, monkeypatch, postgres_dsn, *, asynchronous, run_queued, model=None
):
    home = tmp_path / "home"
    (tmp_path / "input.txt").write_text("input for tool continuation")
    config = _config(
        "fixture-prompt-output", ("UserPromptSubmit",), additional_context_limit=32
    )
    config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["async"] = asynchronous
    _install_trusted_sources(
        home=home, workspace_root=tmp_path, user_config=config, workspace_config=None
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    if model is None:
        model = _QueueModel(run_queued=run_queued)
    core, session = await _open_test_session(
        tmp_path=tmp_path,
        model=model,
        postgres_dsn=postgres_dsn,
        monkeypatch=monkeypatch,
    )
    executor = _PromptExecutor()
    monkeypatch.setattr(session._hooks._executor, "execute", executor.execute)
    return core, session, model, executor


def _input_text(request):
    return "\n".join(
        text
        for message in request.compiled_input.messages
        for text in text_part_values(message.content)
    )


async def _settle_background(session):
    # Wait for real dispatcher settlement, rather than guessing when output reached its owner.
    await asyncio.wait_for(asyncio.gather(*tuple(session._hooks._background_tasks)), 5)


async def _wait_command(session, command, status):
    async def poll():
        while True:
            outcome = await session.query_command(command)
            if outcome is not None and outcome.status == status:
                return outcome
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 10)


async def _wait_idle(session):
    task = session._active_task
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)


async def _start_a_and_queue_b(session, model):
    running = asyncio.create_task(
        session.run_turn(
            PromptContent.text("active input A"),
            command_id=random_id("command"),
        )
    )
    await asyncio.wait_for(model.active_started.wait(), 10)
    command = random_id("command")
    queued = await session.submit_prompt(
        command_id=command, content=PromptContent.text(_QUEUED_PROMPT)
    )
    assert queued.status == "PENDING"
    return running, command, queued.prompt_delivery.queue_item_id


async def _close(core, model, executor, running):
    executor.release.set()
    model.active_release.set()
    model.queued_release.set()
    if running is not None:
        await asyncio.gather(running, return_exceptions=True)
    await core.shutdown()


@pytest.mark.parametrize("arrival", ["before_acceptance", "after_acceptance"])
def test_async_queued_prompt_context_reaches_b_without_leaking_to_a(
    tmp_path: Path,
    monkeypatch,
    stage2_migrated_postgres_database,
    arrival,
):
    async def scenario():
        core, session, model, executor = await _open(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database.runtime_dsn,
            asynchronous=True,
            run_queued=True,
        )
        running = None
        try:
            running, command, _ = await _start_a_and_queue_b(session, model)
            await asyncio.wait_for(executor.started.wait(), 5)
            if arrival == "before_acceptance":
                executor.release.set()
                await _settle_background(session)
            model.active_release.set()
            assert (await asyncio.wait_for(running, 10)).final_text == "A finished"
            await asyncio.wait_for(model.queued_started.wait(), 10)
            assert _CONTEXT + _QUEUED_PROMPT not in _input_text(model.requests[1])
            if arrival == "after_acceptance":
                assert _CONTEXT + _QUEUED_PROMPT not in _input_text(model.requests[2])
                executor.release.set()
                await _settle_background(session)
            else:
                assert _CONTEXT + _QUEUED_PROMPT in _input_text(model.requests[2])
            model.queued_release.set()
            await _wait_command(session, command, "SUCCEEDED")
            assert len(model.requests) == 4
            assert _CONTEXT + _QUEUED_PROMPT in _input_text(model.requests[3])
            assert (
                _input_text(model.requests[1]).count(_CONTEXT + "active input A") == 1
            )
        finally:
            await _close(core, model, executor, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("disposition", ["cancel", "reject"])
@pytest.mark.parametrize("arrival", ["before_discard", "after_discard"])
def test_async_discarded_prompt_context_never_reaches_another_turn(
    tmp_path: Path,
    monkeypatch,
    stage2_migrated_postgres_database,
    disposition,
    arrival,
):
    async def scenario():
        core, session, model, executor = await _open(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database.runtime_dsn,
            asynchronous=True,
            run_queued=False,
        )
        running = None
        try:
            running, command, queue_id = await _start_a_and_queue_b(session, model)
            await asyncio.wait_for(executor.started.wait(), 5)
            if arrival == "before_discard":
                executor.release.set()
                await _settle_background(session)
            if disposition == "cancel":
                cancelled = await session.cancel_queued_prompt(
                    command_id=random_id("command"),
                    source_queue_item_id=queue_id,
                )
                assert cancelled.status == "SUCCEEDED"
            else:
                original = session.repository.consume_prepared_prompt_head

                def reject(guard, *, candidate, **kwargs):
                    if candidate.queue_item_id == queue_id:
                        raise ValueError("fixture rejects queued input before start")
                    return original(guard, candidate=candidate, **kwargs)

                monkeypatch.setattr(
                    session.repository, "consume_prepared_prompt_head", reject
                )
            model.active_release.set()
            assert (await asyncio.wait_for(running, 10)).final_text == "A finished"
            await _wait_command(session, command, "REJECTED")
            await _wait_idle(session)
            if arrival == "after_discard":
                executor.release.set()
                await _settle_background(session)
            result = await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text("later input C"),
                    command_id=random_id("command"),
                ),
                10,
            )
            assert result.final_text == "C finished"
            assert len(model.requests) == 3
            assert all(
                _CONTEXT + _QUEUED_PROMPT not in _input_text(request)
                for request in model.requests
            )
        finally:
            await _close(core, model, executor, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["reject_before_start", "cancel_after_full"])
def test_sync_queued_start_failure_retires_prompt_context(
    tmp_path: Path,
    monkeypatch,
    stage2_migrated_postgres_database,
    failure,
):
    async def scenario():
        core, session, model, executor = await _open(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database.runtime_dsn,
            asynchronous=False,
            run_queued=False,
        )
        executor.release.set()
        running = None
        release_start = asyncio.Event()
        try:
            running, command, queue_id = await _start_a_and_queue_b(session, model)
            start_seen = asyncio.Event()
            if failure == "reject_before_start":
                original = session.repository.consume_prepared_prompt_head

                def reject(guard, *, candidate, **kwargs):
                    if candidate.queue_item_id == queue_id:
                        raise ValueError("fixture rejects queued input before start")
                    return original(guard, candidate=candidate, **kwargs)

                monkeypatch.setattr(
                    session.repository, "consume_prepared_prompt_head", reject
                )
            else:
                original = session._settle_queued_start

                async def hold_full(candidate, intent):
                    confirmation = await original(candidate, intent)
                    if candidate.queue_item_id == queue_id:
                        assert confirmation.accepted is not None
                        start_seen.set()
                        await release_start.wait()
                    return confirmation

                monkeypatch.setattr(session, "_settle_queued_start", hold_full)
            model.active_release.set()
            assert (await asyncio.wait_for(running, 10)).final_text == "A finished"
            if failure == "cancel_after_full":
                await asyncio.wait_for(start_seen.wait(), 10)
                queued_task = session._active_task
                assert queued_task is not None
                queued_task.cancel()
                release_start.set()
                await asyncio.wait_for(
                    asyncio.gather(queued_task, return_exceptions=True), 10
                )
            else:
                await _wait_command(session, command, "REJECTED")
                await _wait_idle(session)
            result = await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text("later input C"),
                    command_id=random_id("command"),
                ),
                10,
            )
            assert result.final_text == "C finished"
            assert len(model.requests) == 3
            assert all(
                _CONTEXT + _QUEUED_PROMPT not in _input_text(request)
                for request in model.requests
            )
        finally:
            release_start.set()
            await _close(core, model, executor, running)

    asyncio.run(scenario())


def test_accepted_direct_prompt_async_context_survives_turn_completion(
    tmp_path: Path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        model = ScriptedKernelModel(
            [
                _text_stream("B finished", "text:direct-prompt-B"),
                _text_stream("C finished", "text:direct-prompt-C"),
            ]
        )
        core, session, _, executor = await _open(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database.runtime_dsn,
            asynchronous=True,
            run_queued=False,
            model=model,
        )
        try:
            accepted = await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text(_QUEUED_PROMPT),
                    command_id=random_id("command"),
                ),
                10,
            )
            assert accepted.final_text == "B finished"
            await asyncio.wait_for(executor.started.wait(), 5)
            assert _CONTEXT + _QUEUED_PROMPT not in _input_text(model.requests[0])

            # A successfully adopted input keeps the existing session-scoped late-output path.
            executor.release.set()
            await _settle_background(session)
            later = await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text("later input C"),
                    command_id=random_id("command"),
                ),
                10,
            )
            assert later.final_text == "C finished"
            assert len(model.requests) == 2
            assert _CONTEXT + _QUEUED_PROMPT in _input_text(model.requests[1])
        finally:
            executor.release.set()
            await core.shutdown()

    asyncio.run(scenario())


def test_rejected_direct_prompt_late_async_context_does_not_reach_later_input(
    tmp_path: Path,
    monkeypatch,
    stage2_migrated_postgres_database,
):
    async def scenario():
        model = ScriptedKernelModel(
            [
                _text_stream("C finished", "text:after-rejected-direct-prompt"),
            ]
        )
        core, session, _, executor = await _open(
            tmp_path,
            monkeypatch,
            stage2_migrated_postgres_database.runtime_dsn,
            asynchronous=True,
            run_queued=False,
            model=model,
        )
        try:

            async def reject(*args, **kwargs):
                await asyncio.wait_for(executor.started.wait(), 5)
                raise ValueError("fixture rejects direct input admission")

            with monkeypatch.context() as patch:
                patch.setattr(
                    session._runner._turn_admission, "accept_root_intent", reject
                )
                with pytest.raises(ValueError, match="rejects direct input admission"):
                    await asyncio.wait_for(
                        session.run_turn(
                            PromptContent.text(_QUEUED_PROMPT),
                            command_id=random_id("command"),
                        ),
                        10,
                    )
            assert not model.requests
            executor.release.set()
            await _settle_background(session)
            later = await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text("later input C"),
                    command_id=random_id("command"),
                ),
                10,
            )
            assert later.final_text == "C finished"
            assert len(model.requests) == 1
            assert _CONTEXT + _QUEUED_PROMPT not in _input_text(model.requests[0])
        finally:
            executor.release.set()
            await core.shutdown()

    asyncio.run(scenario())
