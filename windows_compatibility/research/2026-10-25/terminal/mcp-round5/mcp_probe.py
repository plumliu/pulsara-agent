"""Real MCP supervisor/executor/ClientSession with output-only physical seams."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
import uuid

from pulsara_agent.conversation_kernel.mcp import sdk_facade as F
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

BASE = Path(__file__).resolve().parent
round4 = BASE.parent / "round4" / "manager_probe.py"
spec = importlib.util.spec_from_file_location("g0_scope_fixture", round4)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
LOG_LOCK = threading.Lock()


def log(record, event, **facts):
    with LOG_LOCK:
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(dict(event=event, time=time.time(), **facts)) + "\n"
            )


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


async def until(predicate, seconds=25):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("G0 observation deadline")
        await asyncio.sleep(0.005)


async def run(config_path):
    config = json.loads(Path(config_path).read_text())
    record = config["mcp_record"]
    scope_config = config["scope"]
    scope = R.Scope(scope_config, record)
    scope.record = record
    candidate = config["candidate"]
    scenario = config["scenario"]
    observed_guard_entries = 0
    spawn_count = 0
    transports = []
    closing = {}
    context = ContextVar("g0_mcp_current_transport", default=None)
    sentinel = "g0-synthetic-caller-" + uuid.uuid4().hex

    class Boundary(ProcessCredentialBoundary):
        @asynccontextmanager
        async def async_guard(self, **arguments):
            nonlocal observed_guard_entries
            async with super().async_guard(**arguments) as guard:
                observed_guard_entries += 1
                log(record, "real-credential-guard-entered", locked=self._gate.locked())
                yield guard

    boundary = Boundary(sentinel)
    original_spawn = asyncio.create_subprocess_exec
    original_start = F._BoundedStdioTransport.start
    original_close = F._BoundedStdioTransport.aclose

    async def spawn(*arguments, **options):
        nonlocal spawn_count
        transport = context.get()
        exact = (
            len(arguments) >= 3
            and arguments[0] == sys.executable
            and arguments[1] == config["server_script"]
            and arguments[2] == config_path
        )
        if transport is None or not exact:
            return await original_spawn(*arguments, **options)
        spawn_count += 1
        assert boundary._gate.locked()
        log(record, "actual-stdio-sink-inside-credential-guard", candidate=candidate)
        if candidate:
            await asyncio.to_thread(scope.prepare)
            assert boundary._gate.locked()
        process = await original_spawn(*arguments, **options)
        log(
            record,
            "real-stdio-process-created",
            pid=process.pid,
            starttime=Path(f"/proc/{process.pid}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19],
            guardStillLocked=boundary._gate.locked(),
        )
        return process

    async def start(transport):
        transports.append(transport)
        token = context.set(transport)
        try:
            return await original_start(transport)
        finally:
            context.reset(token)

    async def scoped_close(transport):
        if not candidate or scope.receiver is None:
            return await original_close(transport)

        async def close_once():
            cancel_scope = scenario != "normal"
            log(
                record,
                "scoped-transport-close-entered",
                mode="cancel" if cancel_scope else "normal",
            )
            if cancel_scope:
                await asyncio.to_thread(scope.request_cancel)
            await original_close(transport)
            process = transport._process
            if process is not None:
                await until(lambda: not group_exists(process.pid))
            if cancel_scope:
                await until(lambda: scope.join_windows(0))
            else:
                await asyncio.to_thread(scope.normal_join)
            log(
                record,
                "scoped-transport-physical-joined",
                mode="cancel" if cancel_scope else "normal",
                linuxGroupAlive=bool(process and group_exists(process.pid)),
                stdioTasksDone=all(task.done() for task in transport._tasks),
                windows=json.loads(scope.path(".job-joined.json").read_text()),
            )

        if transport not in closing:
            closing[transport] = asyncio.create_task(
                close_once(), name="g0-mcp-exact-physical-close"
            )
        await asyncio.shield(closing[transport])

    asyncio.create_subprocess_exec = spawn
    F._BoundedStdioTransport.start = start
    F._BoundedStdioTransport.aclose = scoped_close
    args = [config["server_script"], config_path]
    if scenario == "guard-deny":
        args.append(sentinel)
    server_config = _parse_server(
        "g0",
        {
            "required": True,
            "default_tool_timeout_ms": 1500 if scenario == "timeout" else 20000,
            "effect_policy": {"default_effect": "EXTERNAL_EFFECT"},
            "transport": {
                "type": "stdio",
                "command": sys.executable,
                "args": args,
                "env": {"WSL_INTEROP": scope_config["directory"] + "/interop"},
            },
        },
    )
    supervisor = McpHostSupervisor(
        session_id="session:g0-mcp",
        workspace_root=Path.cwd(),
        configs=(server_config,),
        credential_boundary=boundary,
    )
    runtime = None
    call = None
    log(
        record,
        "probe-started",
        pid=os.getpid(),
        starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        candidate=candidate,
        scenario=scenario,
    )
    try:
        if not candidate:
            await asyncio.to_thread(scope.prepare)
        if scenario == "guard-deny":
            client = F.BoundedMcpSdkClient(
                server_config,
                workspace_root=Path.cwd(),
                notification_callback=lambda method: asyncio.sleep(0),
                credential_boundary=boundary,
            )
            try:
                await client.open()
            except ValueError as error:
                log(
                    record,
                    "guard-denied-before-spawn",
                    error=client.diagnostic_message(error),
                    spawnCount=spawn_count,
                    scopePrepared=scope.receiver is not None,
                )
            else:
                raise AssertionError("real credential guard admitted caller credential")
            finally:
                await client.aclose()
            assert spawn_count == 0 and scope.receiver is None
            return
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        executor = next(
            item
            for item in runtime.executors.values()
            if item.semantic.remote_tool_name == "spawn_tail"
        )
        permit = executor.admit(
            session_id="session:g0-mcp",
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            turn_id="turn:g0-mcp",
            tool_call_id="call:g0-mcp",
        )
        permit.mark_attempt_accepted()
        slot = executor.lease._slot
        client = slot.client
        log(
            record,
            "real-supervisor-ready-and-permit-accepted",
            protocol=client.protocol_version,
            effect=executor.policy.effect_kind.value,
            supportsParallel=client.supports_bounded_stateless_parallelism,
        )
        call = asyncio.create_task(
            executor.invoke(
                permit, {"mode": "normal" if scenario == "normal" else "hold"}
            )
        )
        await until(
            lambda: any(
                row["event"] == "child-ready"
                for row in R.rows(scope_config["pe_record"])
            )
        )
        if scenario == "call-cancel":
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            await asyncio.sleep(0.05)
            log(
                record,
                "single-call-cancel-observed",
                callCancelled=call.cancelled(),
                permitState=permit.state.value,
                clientClosed=client._closed,
                serverState=supervisor._state["g0"].value,
                activeOperations=slot.active_operation_count,
            )
        elif scenario == "server-crash":
            process = client._transport._process
            pidfd = os.pidfd_open(process.pid)
            starttime = (
                Path(f"/proc/{process.pid}/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[19]
            )
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            os.close(pidfd)
            log(
                record,
                "exact-mcp-server-crash-injected",
                pid=process.pid,
                starttime=starttime,
            )
            known = await call
            log(
                record,
                "real-tool-outcome",
                state=known.state,
                content=known.content.decode(),
                permitState=permit.state.value,
            )
        else:
            known = await call
            log(
                record,
                "real-tool-outcome",
                state=known.state,
                content=known.content.decode(),
                permitState=permit.state.value,
            )
        supervisor.stop_admission()
        runtime.release()
        runtime = None
        close = asyncio.create_task(supervisor.aclose())
        if candidate and scenario == "normal":
            await asyncio.sleep(0.1)
            scope.path(".observe").touch()
            await until(lambda: scope.path(".observed.json").exists())
            log(
                record,
                "normal-close-still-owned",
                closeDone=close.done(),
                clientClosed=client._closed,
                windows=json.loads(scope.path(".observed.json").read_text()),
            )
        await close
        log(
            record,
            "original-supervisor-close-returned",
            clientClosed=client._closed,
            processReturncode=client._transport._process.returncode,
            stdioTasksDone=all(task.done() for task in client._transport._tasks),
            linuxGroupAlive=group_exists(client._transport._process.pid),
            permitState=permit.state.value,
            spawnCount=spawn_count,
            guardEntries=observed_guard_entries,
        )
        Path(config["driver_prefix"] + ".observation-ready").touch()
        await until(
            lambda: Path(config["driver_prefix"] + ".observation-captured").exists()
        )
        if not candidate:
            log(record, "baseline-external-research-cleanup-entered")
            await asyncio.to_thread(scope.normal_join)
    except BaseException:
        log(record, "probe-failure", traceback=traceback.format_exc())
        raise
    finally:
        if runtime is not None:
            runtime.release()
        await supervisor.aclose()
        if call is not None and not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
        if scope.receiver is not None:
            if not scope.path(".job-joined.json").exists():
                await asyncio.to_thread(scope.cancel_and_join)
            log(
                record,
                "retirement-after-real-mcp-owner-close",
                transportsClosed=all(item._closed for item in transports),
                stdioTasksDone=all(
                    task.done() for item in transports for task in item._tasks
                ),
            )
            await asyncio.to_thread(scope.retire)
        log(
            record,
            "probe-finished",
            spawnCount=spawn_count,
            guardEntries=observed_guard_entries,
        )


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
