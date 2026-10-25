from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import traceback
from time import monotonic

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[2]
import pulsara_agent
sys.path.append(str(REPO))
from pulsara_agent.windows_interop import create_scope
from pulsara_agent.conversation_kernel.mcp.sdk_facade import _BoundedStdioTransport
from pulsara_agent.conversation_kernel.mcp.wire import DEFAULT_MCP_WIRE_BOUNDS
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from pulsara_agent.hooks.executor import HookCommandExecutor, HookExecutionRequest, SYNCHRONOUS_COMMAND_SLOTS
from pulsara_agent.hooks.contracts import HookEventType
from tests.test_round9_2_hook_subsystem import _definition, _scope


class ObservedHookExecutor(HookCommandExecutor):
    async def _run_process_attempt(self, *args, **kwargs):
        try:
            return await super()._run_process_attempt(*args, **kwargs)
        except BaseException as error:
            snapshot = subprocess.run(["ps", "-axo", "pid,ppid,pgid,uid,state"], capture_output=True, text=True).stdout
            attempt = kwargs.get("attempt")
            pid = attempt.process.pid if attempt is not None and attempt.process is not None else None
            rows = [line for line in snapshot.splitlines() if pid is not None and len(line.split()) >= 3 and line.split()[2] == str(pid)]
            with (OUT / "hook-exceptions.jsonl").open("a") as stream:
                stream.write(json.dumps({"pid": pid, "group_snapshot": rows, "stage": "run-process-attempt", "type": type(error).__name__, "message": str(error), "traceback": "".join(traceback.format_exception(error))}) + "\n")
            raise

    async def _spawn_and_drain(self, *args, **kwargs):
        try:
            return await super()._spawn_and_drain(*args, **kwargs)
        except BaseException as error:
            snapshot = subprocess.run(["ps", "-axo", "pid,ppid,pgid,uid,state"], capture_output=True, text=True).stdout
            attempt = kwargs.get("attempt")
            pid = attempt.process.pid if attempt is not None and attempt.process is not None else None
            rows = [line for line in snapshot.splitlines() if pid is not None and len(line.split()) >= 3 and line.split()[2] == str(pid)]
            with (OUT / "hook-exceptions.jsonl").open("a") as stream:
                stream.write(json.dumps({"pid": pid, "group_snapshot": rows, "stage": "spawn-and-drain", "type": type(error).__name__, "message": str(error), "traceback": "".join(traceback.format_exception(error))}) + "\n")
            raise


def group_live(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def workload(root, label, *, natural=False):
    script, marker = root / f"{label}.py", root / f"{label}.pid"
    script.write_text(
        "import os,pathlib,signal,sys,time\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        + (" time.sleep(.25)\n print('NATURAL_TAIL_DONE',flush=True)\n os._exit(0)\n" if natural else " while True: time.sleep(.05)\n")
        + "else:\n"
        " while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.005)\n"
        " os._exit(0)\n"
    )
    return script, marker


async def run(root):
    assert sys.platform == "darwin"
    assert create_scope() is None
    results = []

    def record(value):
        results.append(value)
        with (OUT / "real-scope-observations.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")
    for ordinal, natural in enumerate([True] * 30):
        label = ("hook-natural" if natural else "hook-cancel") + str(ordinal)
        script, marker = workload(root, label, natural=natural)
        executor = ObservedHookExecutor(credential_boundary=ProcessCredentialBoundary())
        cancel = asyncio.Event()
        request = HookExecutionRequest(_definition(root, HookEventType.USER_PROMPT_SUBMIT_EVENT, shlex.join([sys.executable, str(script), str(marker)])), {}, _scope(), 0, 0, root, None, monotonic()+2, cancel)
        task = asyncio.create_task(executor.execute(request))
        process = None
        try:
            await until(lambda: marker.exists() and executor._attempts and next(iter(executor._attempts)).process is not None)
            attempt = next(iter(executor._attempts))
            process = attempt.process
            await until(lambda: process.returncode is not None)
            assert process.returncode == 0 and group_live(process.pid)
            assert not task.done()
            if not natural:
                cancel.set()
            result = await asyncio.wait_for(task, 8)
            record({"case": label+"-observed", "root_pid": process.pid, "tail_pid": int(marker.read_text()), "root_returncode": process.returncode, "failure_code": result.failure_code, "stdout": result.stdout.decode(), "stderr": result.stderr.decode(), "attempt_joined": attempt.joined.is_set(), "workers_done": [worker.done() for worker in attempt.tasks]})
            assert result.failure_code == (None if natural else "HOOK_CANCELLED")
            if natural:
                assert b"NATURAL_TAIL_DONE" in result.stdout
            assert not group_live(process.pid)
            assert attempt.joined.is_set() and all(worker.done() for worker in attempt.tasks)
            assert not executor._attempts and executor._sync_slots._value == SYNCHRONOUS_COMMAND_SLOTS
            await executor.aclose()
            record({"case": label, "root_pid": process.pid, "tail_pid": int(marker.read_text()), "root_exit_before_settlement": 0, "failure_code": result.failure_code, "stdout": result.stdout.decode(), "group_absent": True, "workers_done": True, "permit_restored": True})
        finally:
            if process is not None and group_live(process.pid):
                os.killpg(process.pid, signal.SIGKILL)
            await executor.aclose()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return results


def main():
    report = {"platform": sys.platform, "python": sys.executable, "module": pulsara_agent.__file__, "status": "running"}
    try:
        with tempfile.TemporaryDirectory(prefix="native-scope-probe-") as temporary:
            root = Path(temporary).resolve()
            report["temporary_root"] = str(root)
            report["cases"] = asyncio.run(run(root))
        report.update(status="passed", temporary_root_removed=True)
    except BaseException as error:
        report.update(status="failed", failure_type=type(error).__name__, failure_message=str(error))
        raise
    finally:
        (OUT / "hook-focused-results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
