"""Real HookCommandExecutor and TerminalEnvironmentOwner; no copied state machine."""

import asyncio
from dataclasses import replace  # noqa: F401 -- preserve executed research snapshot
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import traceback

BASE = Path(__file__).resolve().parent
ROOT = next(parent for parent in BASE.parents if (parent / "pyproject.toml").exists())
spec = importlib.util.spec_from_file_location(
    "hook_scope_fixture",
    ROOT / "output/windows-g0/interop_tasks_r3/round4/manager_probe.py",
)
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def launcher(config_path):
    cfg = json.loads(Path(config_path).read_text())
    raw = sys.stdin.buffer.read()
    S.append(
        cfg["owner_record"],
        "hook-workload-admitted",
        pid=os.getpid(),
        starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        credentialEnvAbsent="G0_HOOK_PRIVATE" not in os.environ,
        publicEnv=os.environ.get("G0_HOOK_PUBLIC"),
        stdinCredentialAbsent=b"g0-hook-borrowed-canary" not in raw,
    )
    child = subprocess.Popen(cfg["pe_args"], stdin=subprocess.PIPE)
    child.communicate(raw)
    sys.exit(child.returncode)


async def hook(cfg, scope):
    import pulsara_agent.hooks.executor as H
    from pulsara_agent.hooks.contracts import (
        FrozenHookDefinition,
        FrozenHookMatcherFact,
        FrozenHookSourceProvenance,
        LocalFileHookSourceIdentity,
        LocalFileHookTrustSubject,
        HookSourceKind,
        HookVisibilityScope,
        HookEventType,
        HookDispatchScopeRef,
        HookScopeKind,
    )
    from pulsara_agent.hooks.output_parser import parse_handler_output
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    secret = "g0-hook-borrowed-canary"
    os.environ["G0_HOOK_PRIVATE"] = secret
    os.environ["G0_HOOK_PUBLIC"] = "public-canary"
    command = shlex.join(
        [sys.executable, str(BASE / "guest_probe.py"), "launcher", cfg["config_path"]]
    )
    if cfg["scenario"] == "guard":
        command += " " + shlex.quote(secret)
    provenance = FrozenHookSourceProvenance(
        LocalFileHookSourceIdentity(
            HookSourceKind.USER_FILE,
            Path.cwd() / "g0-research-hook.json",
            HookVisibilityScope.USER,
        ),
        LocalFileHookTrustSubject(HookSourceKind.USER_FILE),
        None,
        "G0_RESEARCH",
        (("WSL_INTEROP", cfg["directory"] + "/interop"),),
    )
    definition = FrozenHookDefinition(
        provenance,
        0,
        0,
        0,
        0,
        HookEventType.SESSION_START_EVENT,
        FrozenHookMatcherFact("", True),
        command,
        None,
        cfg["timeout"],
        False,
        None,
        4096,
    )
    cancel = asyncio.Event()
    request = H.HookExecutionRequest(
        definition,
        {
            "hook_event_name": "SessionStart",
            "message": "public 中文",
            "borrowed": secret,
        },
        HookDispatchScopeRef(object(), object(), HookScopeKind.ROOT),
        1,
        0,
        Path.cwd(),
        None,
        None,
        cancel,
    )
    original_spawn = asyncio.create_subprocess_shell
    original_abort = H._abort_process_group
    processes = []

    async def spawned(command_arg, *args, **kwargs):
        process = await original_spawn(command_arg, *args, **kwargs)
        if command_arg == command and cfg["patched"]:
            original_wait = process.wait

            async def joined_wait():
                code = await original_wait()
                await asyncio.to_thread(scope.normal_join)
                return code

            process.wait = joined_wait
            processes.append(process)
            S.append(
                cfg["owner_record"], "hook-scope-bound-to-real-process", pid=process.pid
            )
        return process

    async def abort(process, *, physical_deadline):
        if process in processes:
            await asyncio.to_thread(scope.cancel_and_join)
        return await original_abort(process, physical_deadline=physical_deadline)

    class ScopedExecutor(H.HookCommandExecutor):
        async def _spawn_and_drain(self, request, **kwargs):
            # Preparation precedes the original final credential guard. Rotation
            # remains checked at its actual subprocess admission sink.
            await asyncio.to_thread(scope.prepare)
            return await super()._spawn_and_drain(request, **kwargs)

    if cfg["patched"]:
        asyncio.create_subprocess_shell = spawned
        H._abort_process_group = abort
        executor = ScopedExecutor(credential_boundary=ProcessCredentialBoundary(secret))
    else:
        await asyncio.to_thread(scope.prepare)
        executor = H.HookCommandExecutor(
            credential_boundary=ProcessCredentialBoundary(secret)
        )
    task = asyncio.create_task(executor.execute(request))

    async def trigger():
        if cfg["scenario"] not in ("cancel-tail", "task-cancel", "host-close"):
            return
        await asyncio.to_thread(
            S.wait_until,
            lambda: any(
                row["event"] == "child-ready"
                for row in S.rows(
                    cfg["windows_pe_record"].replace("C:", "/mnt/c").replace("\\", "/")
                )
            ),
        )
        if cfg["scenario"] in ("cancel-tail", "host-close"):
            await asyncio.sleep(0.15)
        S.append(
            cfg["owner_record"], "real-hook-control-request", scenario=cfg["scenario"]
        )
        if cfg["scenario"] == "cancel-tail":
            cancel.set()
        elif cfg["scenario"] == "task-cancel":
            task.cancel()
        else:
            executor.begin_host_close(deadline_monotonic=time.monotonic() + 8)
            await executor.aclose(deadline_monotonic=time.monotonic() + 8)
            S.append(
                cfg["owner_record"],
                "real-hook-aclose-returned",
                processes=len(executor._processes),
            )

    control = asyncio.create_task(trigger())
    started = time.monotonic()
    try:
        result = await task
        parsed = parse_handler_output(result)
        S.append(
            cfg["owner_record"],
            "real-hook-result",
            exit=result.exit_code,
            failureCode=result.failure_code,
            stdout=result.stdout.decode(errors="replace"),
            stderr=result.stderr.decode(errors="replace"),
            parsedType=type(parsed).__name__,
            duration=time.monotonic() - started,
            processes=len(executor._processes),
            syncSlots=executor._sync_slots._value,
            backgroundSlots=executor._background_slots._value,
        )
        expected = {
            "timeout": "HOOK_TIMEOUT",
            "cancel-tail": "HOOK_CANCELLED",
            "host-close": "HOOK_CANCELLED",
            "guard": "API_KEY_VALUE_PRESENT",
        }.get(cfg["scenario"])
        if cfg["patched"]:
            assert result.failure_code == expected, (result.failure_code, expected)
            if cfg["scenario"] == "normal":
                assert result.exit_code == 0
            if cfg["scenario"] == "nonzero":
                assert result.exit_code == 7
        if not cfg["patched"]:
            scope.path(".observe").touch()
            await asyncio.to_thread(
                S.wait_until, lambda: scope.path(".observed.json").exists()
            )
            S.append(
                cfg["owner_record"],
                "baseline-windows-at-hook-return",
                facts=json.loads(scope.path(".observed.json").read_text()),
            )
    except asyncio.CancelledError:
        S.append(
            cfg["owner_record"],
            "real-hook-task-cancelled",
            duration=time.monotonic() - started,
            processes=len(executor._processes),
        )
        assert cfg["scenario"] == "task-cancel"
    finally:
        await control
        await executor.aclose(deadline_monotonic=time.monotonic() + 8)
        assert (
            not executor._processes
            and executor._sync_slots._value == H.SYNCHRONOUS_COMMAND_SLOTS
        )
        if scope.receiver and not scope.path(".job-joined.json").exists():
            await asyncio.to_thread(scope.cancel_and_join)
        if scope.receiver:
            await asyncio.to_thread(scope.retire)
        asyncio.create_subprocess_shell = original_spawn
        H._abort_process_group = original_abort


def environment(cfg, scope):
    import pulsara_agent.terminal_process.environment as E

    private = Path(cfg["env_home"])
    private.mkdir(mode=0o700)
    shell = private / "probe-shell"
    shell.write_text(
        "#!/bin/sh\n"
        + shlex.join(
            [
                sys.executable,
                str(BASE / "guest_probe.py"),
                "launcher",
                cfg["config_path"],
            ]
        )
        + ' </dev/null\nexec /bin/sh "$@"\n'
    )
    shell.chmod(0o700)
    original_popen = E.subprocess.Popen
    original_abort = E._terminate_and_join
    owner = E.TerminalEnvironmentOwner(
        Path.cwd(),
        config=E.TerminalEnvConfig(shell_snapshot_timeout_seconds=cfg["timeout"]),
        parent_env={
            "HOME": str(private),
            "USER": "plumwsl",
            "SHELL": str(shell),
            "PATH": "/usr/bin:/bin",
        },
    )
    processes = []

    def spawned(args, *a, **kw):
        target = bool(args and str(args[0]) == str(shell))
        if target:
            scope.prepare()
            kw["env"] = dict(kw["env"], WSL_INTEROP=cfg["directory"] + "/interop")
        process = original_popen(args, *a, **kw)
        if target and cfg["patched"]:
            old_wait = process.wait

            def joined_wait(timeout=None):
                code = old_wait(timeout=timeout)
                if not scope.normal_join(timeout):
                    raise TimeoutError(
                        "snapshot Windows scope exceeded existing probe deadline"
                    )
                return code

            process.wait = joined_wait
            processes.append(process)
        return process

    def abort(process, **kw):
        if process in processes:
            scope.cancel_and_join()
        return original_abort(process, **kw)

    E.subprocess.Popen = spawned
    if cfg["patched"]:
        E._terminate_and_join = abort
    values = []

    def build():
        values.append(owner._snapshot(E.TerminalShellConfig(shell, "g0-probe")))

    worker = threading.Thread(target=build, name="real-environment-probe")
    try:
        worker.start()
        if cfg["scenario"] == "close":
            S.wait_until(
                lambda: any(
                    row["event"] == "child-ready"
                    for row in S.rows(
                        cfg["windows_pe_record"]
                        .replace("C:", "/mnt/c")
                        .replace("\\", "/")
                    )
                )
            )
            owner.close_and_join()
            S.append(
                cfg["owner_record"],
                "real-environment-close-returned",
                attempts=len(owner._attempts),
            )
        worker.join(timeout=20)
        assert not worker.is_alive()
        snapshot, error = values[0]
        S.append(
            cfg["owner_record"],
            "real-environment-result",
            snapshotUsed=snapshot is not None,
            error=error,
            attempts=len(owner._attempts),
            cache=len(owner._cache),
        )
        if cfg["patched"]:
            assert snapshot is None and error == "PROBE_TIMEOUT", error
        else:
            scope.path(".observe").touch()
            S.wait_until(lambda: scope.path(".observed.json").exists())
            S.append(
                cfg["owner_record"],
                "baseline-windows-at-environment-return",
                facts=json.loads(scope.path(".observed.json").read_text()),
            )
    finally:
        owner.close_and_join()
        if scope.receiver and not scope.path(".job-joined.json").exists():
            scope.cancel_and_join()
        if scope.receiver:
            scope.retire()
        E.subprocess.Popen = original_popen
        E._terminate_and_join = original_abort
        shell.unlink()
        private.rmdir()


def main(config_path):
    cfg = json.loads(Path(config_path).read_text())
    S.append(
        cfg["owner_record"],
        "guest-start",
        pid=os.getpid(),
        starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        python=sys.version,
        productionModule=__import__("pulsara_agent").__file__,
    )
    scope = S.Scope(cfg, cfg["owner_record"])
    try:
        if cfg["kind"] == "hook":
            asyncio.run(hook(cfg, scope))
        else:
            environment(cfg, scope)
        S.append(cfg["owner_record"], "guest-finished")
    except BaseException:
        S.append(cfg["owner_record"], "guest-failure", traceback=traceback.format_exc())
        raise


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "launcher":
        launcher(sys.argv[2])
    else:
        main(sys.argv[1])
