"""Failed/cancelled provider preparation cannot leak one-shot start context."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent, text_part_values
from tests.support.round3 import ScriptedKernelModel
from tests.test_round4_plan_host import _open_test_session, _text
from tests.test_round9_2_hook_integration_postgres import (
    _HOOK_DRIVER,
    _command,
    _config,
    _install_trusted_sources,
    _log_values,
)
from tests.test_round9_2_hook_subsystem import _Estimator

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("failure_kind", ["cancel", "error"])
@pytest.mark.parametrize("phase", ["pending_context", "frozen_context"])
def test_session_start_preparation_failure_retires_context_and_resources(
    tmp_path: Path,
    monkeypatch,
    stage2_migrated_postgres_database,
    failure_kind,
    phase,
):
    async def scenario():
        home = tmp_path / "home"
        log = tmp_path / "hooks.jsonl"
        driver = tmp_path / "hook_driver.py"
        driver.write_text(_HOOK_DRIVER)
        _install_trusted_sources(
            home=home,
            workspace_root=tmp_path,
            user_config=_config(_command(driver, log, "CLEANUP"), ("SessionStart",)),
            workspace_config=None,
        )
        monkeypatch.setenv("PULSARA_HOME", str(home))
        model = ScriptedKernelModel([list(_text("recovered"))])
        core, root = await _open_test_session(
            tmp_path=tmp_path,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        runner = root._runner
        provider = runner._provider_dispatch
        context = runner._hook_context_owner
        started = asyncio.Event()
        release = asyncio.Event()
        handles, borrows, pending, frozen = [], [], [], []
        running = None

        async def fail_preparation():
            assert pending and pending[0]._committed
            assert [item["stdin"]["hook_event_name"] for item in _log_values(log)] == [
                "SessionStart"
            ]
            assert len(frozen) == (1 if phase == "frozen_context" else 0)
            started.set()
            await release.wait()
            raise RuntimeError("Hook context preparation failed")

        original_prepare = provider.prepare

        async def prepare(**kwargs):
            handle = kwargs["existing_handle"]
            if not any(item is handle for item in handles):
                handles.append(handle)
            return await original_prepare(**kwargs)

        original_borrow = runner._tools.borrow_tool_surface

        def borrow(*args, **kwargs):
            value = original_borrow(*args, **kwargs)
            borrows.append(value)
            return value

        original_pending = context.prepare_sync

        def prepare_pending(**kwargs):
            value = original_pending(**kwargs)
            if value is not None:
                pending.append(value)
            return value

        original_freeze = context.freeze_for_target

        def freeze(**kwargs):
            value = original_freeze(**kwargs)
            if value is not None:
                frozen.append(value.reservation)
            return value

        original_change_reader = provider._capability_change_reader
        original_observations = provider._owner_dispatch_observations

        async def before_freeze():
            if pending:
                await fail_preparation()
            return await original_change_reader() if original_change_reader else None

        async def after_freeze(**kwargs):
            if not pending:
                return await original_observations(**kwargs)
            assert kwargs["handle"] is handles[-1]
            assert kwargs["borrow"] is borrows[-1]
            await fail_preparation()

        try:
            with monkeypatch.context() as patch:
                patch.setattr(provider, "prepare", prepare)
                patch.setattr(runner._tools, "borrow_tool_surface", borrow)
                patch.setattr(context, "prepare_sync", prepare_pending)
                patch.setattr(context, "freeze_for_target", freeze)
                if phase == "pending_context":
                    patch.setattr(provider, "_capability_change_reader", before_freeze)
                else:
                    patch.setattr(
                        provider, "_owner_dispatch_observations", after_freeze
                    )
                running = asyncio.create_task(
                    root.run_turn(
                        PromptContent.text("first attempt"),
                        command_id=random_id("command"),
                    )
                )
                await asyncio.wait_for(started.wait(), 10)
                assert handles and not handles[-1]._closed
                assert all(item._closed for item in handles[:-1])
                assert borrows and not borrows[-1]._closed
                if failure_kind == "cancel":
                    assert root._active_task is not None
                    root._active_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(running, 10)
                else:
                    release.set()
                    with pytest.raises(Exception) as failure:
                        await asyncio.wait_for(running, 10)
                    causes = []
                    error = failure.value
                    while error is not None:
                        causes.append(str(error))
                        error = error.__cause__
                    assert "Hook context preparation failed" in causes

            assert not model.requests
            assert all(item._closed for item in handles)
            assert all(item._closed for item in borrows)
            assert runner._safe_point.is_idle()
            assert all(item._retired for item in pending)
            assert all(item._settled for item in frozen)
            assert (
                original_freeze(
                    scope_kind="ROOT",
                    session_id=root.session_id,
                    estimator=_Estimator(),
                )
                is None
            )
            result = await asyncio.wait_for(
                root.run_turn(
                    PromptContent.text("retry without stale context"),
                    command_id=random_id("command"),
                ),
                10,
            )
            assert result.final_text == "recovered"
            assert len(model.requests) == 1
            rendered = "\n".join(
                text
                for message in model.requests[0].compiled_input.messages
                for text in text_part_values(message.content)
            )
            assert "SESSION_START_CONTEXT:CLEANUP" not in rendered
            assert len(_log_values(log)) == 1
        finally:
            release.set()
            if running is not None:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())
