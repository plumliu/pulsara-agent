"""Real POSIX zombie groups retain ownership until the original group disappears."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys
from time import monotonic, sleep

from pulsara_agent.conversation_kernel.mcp.sdk_facade import _terminate_stdio_group
from pulsara_agent.hooks.executor import (
    HookCommandExecutor, HookExecutionRequest, SYNCHRONOUS_COMMAND_SLOTS,
    _process_group_live, _signal_process_group,
)
from pulsara_agent.hooks.contracts import HookEventType
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round9_2_hook_subsystem import _definition, _scope


def _zombie(tmp_path: Path):
    marker = tmp_path / "exited"
    process = subprocess.Popen(
        [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)],
        start_new_session=True,
    )
    # Reading OS state does not poll/wait this child: the parent deliberately
    # retains its zombie until each test exercises the real group boundary.
    deadline = monotonic() + 5  # Test hang detector, not a product timeout.
    while monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-p", str(process.pid), "-o", "stat="],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if state.startswith("Z"):
            assert marker.exists()
            return process
        sleep(.005)
    process.kill()
    process.wait()
    raise AssertionError("child never reached an unreaped zombie state")


def test_unreaped_posix_group_is_live_and_signal_denial_does_not_prove_join(tmp_path):
    process = _zombie(tmp_path)
    try:
        if sys.platform == "darwin":
            try:
                os.killpg(process.pid, 0)
            except PermissionError:
                pass
            else:
                raise AssertionError("Darwin zombie-only group did not expose EPERM")
        assert _process_group_live(process)
        _signal_process_group(process, terminate=True)
        _signal_process_group(process, terminate=False)
    finally:
        assert process.wait(timeout=5) == 0
    assert not _process_group_live(process)


def test_stdio_close_keeps_zombie_group_owned_until_real_reap(tmp_path):
    process = _zombie(tmp_path)

    class AsyncProcess:
        pid = process.pid

        async def wait(self):
            return await asyncio.to_thread(process.wait)

    async def scenario():
        close = asyncio.create_task(_terminate_stdio_group(AsyncProcess()))
        try:
            await asyncio.sleep(.05)
            assert not close.done()
            assert process.wait(timeout=5) == 0
            await asyncio.wait_for(close, 5)
            assert not _process_group_live(process)
        finally:
            process.wait(timeout=5)
            await close

    asyncio.run(scenario())


def test_hook_success_waits_for_natural_tail_after_leader_exit(tmp_path):
    script = tmp_path / "fork_tail.py"
    marker = tmp_path / "tail.pid"
    script.write_text(
        "import os,pathlib,sys,time\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        " time.sleep(.25)\n"
        " print('NATURAL_TAIL_DONE',flush=True)\n"
        " os._exit(0)\n"
        "else:\n"
        " while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.005)\n"
        " os._exit(0)\n"
    )

    async def scenario():
        executor = HookCommandExecutor(credential_boundary=ProcessCredentialBoundary())
        request = HookExecutionRequest(
            _definition(tmp_path, HookEventType.USER_PROMPT_SUBMIT_EVENT,
                        shlex.join([sys.executable, str(script), str(marker)])),
            {}, _scope(), 0, 0, tmp_path, None, monotonic() + 5,
        )
        task = asyncio.create_task(executor.execute(request))
        process = None
        try:
            async with asyncio.timeout(5):
                while not marker.exists() or not executor._attempts:
                    await asyncio.sleep(.005)
                attempt = next(iter(executor._attempts))
                while attempt.process is None or attempt.process.returncode is None:
                    await asyncio.sleep(.005)
                process = attempt.process
                assert process.returncode == 0
                assert not task.done()
                result = await task
            assert result.failure_code is None
            assert result.exit_code == 0
            assert result.stdout == b"NATURAL_TAIL_DONE\n"
            assert not _process_group_live(process)
            assert attempt.joined.is_set()
            assert all(worker.done() for worker in attempt.tasks)
            assert not executor._attempts
            assert executor._sync_slots._value == SYNCHRONOUS_COMMAND_SLOTS
        finally:
            await executor.aclose()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
