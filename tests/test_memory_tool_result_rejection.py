"""Detached task diagnostics retain their real failure semantics."""

import asyncio


def test_detached_root_failure_is_logged_but_normal_cancellation_is_not(caplog):
    from pulsara_agent.conversation_kernel.host import KernelHostSession

    async def exercise():
        async def fail():
            raise ValueError("exact regression failure")

        task = asyncio.create_task(fail(), name="root-regression")
        try:
            await task
        except ValueError:
            pass
        KernelHostSession._observe_active_root_task_done(task)
        cancelled = asyncio.create_task(asyncio.sleep(0))
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        KernelHostSession._observe_active_root_task_done(cancelled)

    asyncio.run(exercise())
    records = [r for r in caplog.records if "ROOT execution failed" in r.message]
    assert len(records) == 1
    assert records[0].exc_info[0] is ValueError
    assert str(records[0].exc_info[1]) == "exact regression failure"
