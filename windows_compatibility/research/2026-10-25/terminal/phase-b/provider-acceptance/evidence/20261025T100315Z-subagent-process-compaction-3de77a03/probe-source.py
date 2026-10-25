"""Reuse original dogfood owners and the reviewed PG terminal test factories."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import json
import io
import os
from pathlib import Path
import shutil
import sys
import asyncio
import traceback
from threading import Lock
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pg_terminal_adapter as shared_adapter


def driver(api, *, kind, args):
    from pulsara_agent.llm.runtime import ModelRuntime
    from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
    from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from pulsara_agent.conversation_kernel.turn_admission import (
        TurnAdmissionCoordinator,
    )
    from tests.dogfood import run_round10_subagent_dogfood as subagent
    from tests.dogfood import run_capability_change_observation_dogfood as capability

    facade = SimpleNamespace(
        LocalSettingsStore=api.LocalSettingsStore,
        _create_database=api.shared._create_database,
        _drop_database=api.shared._drop_database,
        _find_connection=api.shared._find_connection,
        _ReadOnlySettingsStore=api.shared._ReadOnlySettingsStore,
        ModelRuntime=ModelRuntime,
        TemporaryDirectory=TemporaryDirectory,
        save=None,
    )
    original_capacity = subagent._run_capacity_mcp_compaction
    original_context = subagent._run_last_n_and_message

    async def run(runner_args, report):
        saved = facade.LocalSettingsStore().read()
        scrub = saved.credential_scrub_set()
        log_lock = Lock()

        def log(value):
            public = json.dumps(scrub.scrub_json(value), ensure_ascii=False)
            assert not scrub.contains(public)
            with (
                log_lock,
                (args.output.parent / "stages.jsonl").open(
                    "a", encoding="utf-8"
                ) as stream,
            ):
                stream.write(public + "\n")

        selected = next(
            item
            for item in saved.model_connections
            if item.id.value == args.connection_id
        )
        saved = replace(
            saved, model_connections=(selected,), mcp_credentials=(), mcp_oauth=()
        )
        previous_inputs = {}
        prefix_checks = []
        installed_callbacks = []
        process_states = []
        original_init = ConversationKernelRunner.__init__

        def initialize(owner, *positional, **keywords):
            previous = keywords.get("provider_input_installed_observer")

            def observe(request):
                compiled = request.compiled_input
                identity = compiled.canonical_input_identity
                scope = ProviderInputContinuityScope(
                    session_id=request.session_id,
                    scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                )
                view = owner._continuity.current_view(scope)
                assert view is not None
                key = (scope, view.epoch_nonce)
                old = previous_inputs.get(key)
                if old is not None:
                    assert compiled.system_prompt == old.system_prompt
                    assert compiled.tools == old.tools
                    assert compiled.messages[: len(old.messages)] == old.messages
                previous_inputs[key] = compiled
                prefix_checks.append(
                    dict(
                        session_id=request.session_id,
                        scope=identity.conversation_scope_kind.value,
                        task_id=identity.scope_subagent_task_id,
                        epoch=view.epoch_nonce,
                        revision=view.epoch_revision,
                        compared=old is not None,
                        messages=len(compiled.messages),
                    )
                )
                with (args.output.parent / "progress.jsonl").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(
                        json.dumps(
                            {"event": "provider-input-installed", **prefix_checks[-1]},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                if previous is not None:
                    previous(request)
                for callback in tuple(installed_callbacks):
                    callback(request)

            keywords["provider_input_installed_observer"] = observe
            original_init(owner, *positional, **keywords)

        report["prefix_checks"] = prefix_checks
        report["runner"] = kind
        observed_errors = set()

        def observe_repository(name, method):
            def call(*positional, **keywords):
                try:
                    return method(*positional, **keywords)
                except BaseException as error:
                    signature = (name, type(error).__name__, str(error))
                    if signature not in observed_errors:
                        observed_errors.add(signature)
                        head = None
                        admission = keywords.get("provider_input_admission")
                        if admission is not None:
                            from time import monotonic
                            from pulsara_agent.storage.postgres_connection_provider import (
                                PostgresConnectionLane,
                            )

                            candidate = admission.candidate
                            with positional[0].connection_provider.connection(
                                lane=PostgresConnectionLane.INSPECTOR,
                                deadline_monotonic=monotonic() + 10,
                            ) as connection:
                                actual = connection.execute(
                                    "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s",
                                    (candidate.session_id,),
                                ).fetchone()
                                entries = connection.execute(
                                    "SELECT entry_sequence,entry_kind,conversation_scope_kind,scope_subagent_task_id,turn_id FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence DESC LIMIT 12",
                                    (candidate.session_id,),
                                ).fetchall()
                            head = dict(
                                expected=candidate.expected_latest_entry_sequence,
                                actual=actual[0],
                                tail=entries,
                            )
                        log(
                            dict(
                                event="repository-error",
                                method=name,
                                type=type(error).__name__,
                                detail=str(error),
                                traceback=traceback.format_exc(),
                                head=head,
                            )
                        )
                    raise

            return call

        async def context_with_canonical_gate(session, workspace):
            admitted, release = asyncio.Event(), asyncio.Event()
            original_accept = TurnAdmissionCoordinator.accept_root_intent
            original_active = subagent._active_root_turn_id
            original_steer = session.steer_active_turn
            target = {}
            steers = []

            async def accept(owner, intent, **keywords):
                accepted = await original_accept(owner, intent, **keywords)
                if intent.command_id == "command:rl6vdp74wn6i44vyeaigf5sjyi":
                    target["turn_id"] = accepted.turn_id
                    log(
                        dict(
                            event="context-canonical-admitted", turn_id=accepted.turn_id
                        )
                    )
                    admitted.set()
                    await release.wait()
                return accepted

            async def active(owner, *, timeout_seconds):
                if owner is not session:
                    return await original_active(owner, timeout_seconds=timeout_seconds)
                await asyncio.wait_for(admitted.wait(), timeout=timeout_seconds)
                return target["turn_id"]

            async def steer(**keywords):
                result = await original_steer(**keywords)
                record = dict(
                    status=result.status,
                    public_code=getattr(result, "public_code", None),
                    detail=getattr(result, "detail", None),
                )
                steers.append(record)
                log(dict(event="context-steer-result", **record))
                if len(steers) == 2:
                    release.set()
                return result

            try:
                with (
                    patch.object(
                        TurnAdmissionCoordinator, "accept_root_intent", accept
                    ),
                    patch.object(subagent, "_active_root_turn_id", active),
                    patch.object(session, "steer_active_turn", steer),
                ):
                    return await original_context(session, workspace)
            finally:
                release.set()
                report["context_admission_gate"] = dict(
                    boundary="original canonical accept returned; before run_accepted_turn",
                    target=target,
                    steers=steers,
                )

        async def capacity_with_current_admission(session, workspace):
            # Round10's old synchronous seed predates the admission hard cut.
            # Reuse the maintained async fixture with a comparable history size.
            count = await api.shared._seed_completed_history(
                session, segments=5, repetitions=800
            )
            report["history_seed"] = dict(
                helper="run_model_switch_handover_dogfood._seed_completed_history",
                segments=5,
                repetitions=800,
                bytes=count,
                provider_native_replay_fabricated=False,
            )
            if runner_args.cases == "process-compaction":
                return await process_compaction(session, workspace)

            def stack(coroutine):
                frames = []
                while coroutine is not None:
                    frame = getattr(coroutine, "cr_frame", None) or getattr(
                        coroutine, "gi_frame", None
                    )
                    if frame is not None:
                        frames.append(
                            dict(
                                file=frame.f_code.co_filename,
                                line=frame.f_lineno,
                                function=frame.f_code.co_name,
                            )
                        )
                    coroutine = getattr(coroutine, "cr_await", None) or getattr(
                        coroutine, "gi_yieldfrom", None
                    )
                return frames

            async def observe_tasks():
                while True:
                    log(
                        dict(
                            event="task-stacks",
                            tasks=[
                                dict(
                                    name=task.get_name(),
                                    done=task.done(),
                                    cancelling=task.cancelling(),
                                    stack=stack(task.get_coro()),
                                )
                                for task in asyncio.all_tasks()
                                if task is not asyncio.current_task()
                            ],
                        )
                    )
                    await asyncio.sleep(15)

            def observed(name, method):
                async def call(*positional, **keywords):
                    log(dict(event="enter", method=name))
                    try:
                        result = await method(*positional, **keywords)
                    except BaseException as error:
                        log(dict(event="raise", method=name, type=type(error).__name__))
                        raise
                    log(
                        dict(
                            event="return",
                            method=name,
                            disposition=str(getattr(result, "disposition", "")),
                        )
                    )
                    return result

                return call

            monitor = asyncio.create_task(
                observe_tasks(), name="dogfood-readonly-task-stacks"
            )
            try:
                with ExitStack() as stage:
                    stage.enter_context(
                        patch.object(
                            subagent, "_seed_completed_history", lambda *a, **k: None
                        )
                    )
                    for name in ("run_turn", "compact_context", "reload_mcp_configs"):
                        stage.enter_context(
                            patch.object(
                                session, name, observed(name, getattr(session, name))
                            )
                        )
                    return await original_capacity(session, workspace)
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

        async def process_compaction(session, workspace):
            import shlex
            from pulsara_agent.identity import random_id
            from pulsara_agent.llm.input import PromptContent
            from pulsara_agent.primitives.permission import PermissionMode
            from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace

            release = workspace / ".process-compaction-release"
            fixture = workspace / "process_compaction.py"
            fixture.write_text(
                "from pathlib import Path\nimport sys,time\n"
                "print('PROCESS_COMPACTION_READY',flush=True)\n"
                "while not Path(sys.argv[1]).exists(): time.sleep(.05)\n"
                "print('PROCESS_COMPACTION_LINUX_DONE',flush=True)\n",
                encoding="utf-8",
            )
            command = (
                shlex.quote(sys.executable)
                + " -u "
                + shlex.quote(str(fixture))
                + " "
                + shlex.quote(str(release))
                + "; /mnt/c/Windows/System32/cmd.exe /d /c echo PROCESS_COMPACTION_PE_DONE"
            )
            permission = PermissionMode.BYPASS_PERMISSIONS
            prompts = [
                f"Call terminal exactly once with command {command!r}, tty=false and yield_time_ms=0. "
                "Return immediately after it reports a running process. Do not wait, register a monitor, "
                "call terminal_process, read the fixture or start another command. The external test driver "
                "will release this exact process after a real compaction.",
                "Find the existing running terminal process retained in COMPACTION_RUNTIME_HANDOFF. "
                "Follow that exact process with terminal_process wait until natural completion. "
                "Do not start or restart any command, kill a process, read the fixture, or register a monitor. "
                "Report the exact Linux and Windows completion markers and exit code. The test driver "
                "releases the existing process after this actual provider input is installed.",
            ]
            report["process_compaction_prompts"] = prompts
            handoff_capture = {}
            try:
                first = await session.run_turn(
                    PromptContent.text(prompts[0]),
                    command_id=random_id("command"),
                    requested_permission_mode=permission,
                )
                trace = await asyncio.to_thread(_tool_trace, session)
                starts = [item for item in trace if item["tool_name"] == "terminal"]
                assert len(starts) == 1, trace
                initial = starts[0]["result"]
                assert initial["status"] == "running", initial
                process_id = initial["process_id"]
                manager = session._tools._terminal
                state = manager.process_registry._states[process_id]
                process_states.append(state)
                scope = state.interop_scope
                assert scope is not None and not scope.physically_joined.is_set()
                assert not state.physical_completion.is_set()
                compacted = await session.compact_context(
                    command_id=random_id("command"),
                    force=True,
                )
                assert compacted.disposition.value == "COMPACTED", compacted
                current = manager.poll_process(
                    process_id,
                    owner_host_session_id=session.host_session_id,
                    max_output_chars=32000,
                )
                assert current.status.value == "running", current
                assert manager.process_registry._states[process_id] is state
                assert state.interop_scope is scope

                def installed(request):
                    if (
                        request.compiled_input.canonical_input_identity.conversation_scope_kind.value
                        != "ROOT"
                    ):
                        return
                    body, observations = subagent._root_runtime_handoff_probe(session)
                    value = json.loads(body)
                    processes = value["terminal_processes"]
                    assert any(
                        item["process_id"] == process_id for item in processes
                    ), value
                    handoff_capture.update(body=value, observations=observations)
                    release.touch()

                installed_callbacks.append(installed)
                try:
                    second = await session.run_turn(
                        PromptContent.text(prompts[1]),
                        command_id=random_id("command"),
                        requested_permission_mode=permission,
                    )
                finally:
                    installed_callbacks.remove(installed)
                trace = await asyncio.to_thread(_tool_trace, session)
                report["trace"] = trace
                assert (
                    len([item for item in trace if item["tool_name"] == "terminal"])
                    == 1
                )
                followups = [
                    item for item in trace if item["tool_name"] == "terminal_process"
                ]
                assert followups and all(
                    item["arguments"]["process_id"] == process_id
                    and item["arguments"]["action"] == "wait"
                    for item in followups
                ), trace
                final = followups[-1]["result"]
                assert final["status"] == "success" and final["exit_code"] == 0, final
                assert "PROCESS_COMPACTION_LINUX_DONE" in final["output"]
                assert "PROCESS_COMPACTION_PE_DONE" in final["output"]
                assert state.physical_completion.is_set()
                assert scope.physically_joined.is_set() and not scope.cancelled.is_set()
                assert scope._joined.result()["reason"] == "settled"
                return dict(
                    passed=True,
                    scenario="running_process_compaction_followup",
                    original_process_id=process_id,
                    initial=initial,
                    status_after_compaction=current.status.value,
                    compaction_disposition=compacted.disposition.value,
                    snapshot_id=compacted.snapshot_id,
                    handoff=handoff_capture,
                    final=final,
                    first_reply=first.final_text,
                    second_reply=second.final_text,
                    scope_joined=scope._joined.result(),
                    scope_cancelled=scope.cancelled.is_set(),
                )
            finally:
                release.touch()

        with ExitStack() as instrumentation:
            instrumentation.enter_context(
                patch.object(ConversationKernelRunner, "__init__", initialize)
            )
            for name in ("accept_root_turn_intent", "confirm_root_turn_intent"):
                instrumentation.enter_context(
                    patch.object(
                        ConversationKernelRepository,
                        name,
                        observe_repository(
                            name, getattr(ConversationKernelRepository, name)
                        ),
                    )
                )
            if kind == "subagent":
                database, _, admin, runtime = facade._create_database(saved)
                try:
                    if runner_args.cases == "root-regression":
                        from tests import (
                            test_round5_long_horizon_postgres as regression,
                        )

                        fixture = SimpleNamespace(runtime_dsn=runtime)
                        for after_unknown in (False, True):
                            await asyncio.to_thread(
                                regression.test_root_stale_head_reprepares_same_intent_without_rebuilding_prefix,
                                fixture,
                                after_unknown,
                            )
                        await asyncio.to_thread(
                            regression.test_root_stale_head_cancellation_during_confirmation_never_readmits,
                            fixture,
                        )
                        for repository, calls, opens in (
                            (regression._LostRootAdmissionAckRepository, 1, True),
                            (regression._RootNoneThenFullRepository, 2, True),
                            (regression._RootConflictRepository, 1, False),
                        ):
                            await asyncio.to_thread(
                                regression.test_round5_root_turn_admission_full_none_conflict_matrix,
                                fixture,
                                repository,
                                calls,
                                opens,
                            )
                        report.update(status="completed", regression_cases=6)
                        return
                    with facade.TemporaryDirectory() as temporary:
                        base = Path(temporary)
                        home, workspace = base / "home", base / "中文 空格工程"
                        home.mkdir(mode=0o700)
                        workspace.mkdir(mode=0o700)
                        shutil.copyfile(
                            "/mnt/c/python_workspace/pulsara-agent/pyproject.toml",
                            workspace / "pyproject.toml",
                        )
                        settings = replace(
                            saved,
                            postgres=__import__(
                                "pulsara_agent.settings",
                                fromlist=["LocalPostgresConfig"],
                            ).LocalPostgresConfig(runtime, admin),
                        )
                        with (
                            patch.dict(os.environ, {"PULSARA_HOME": str(home)}),
                            patch.object(subagent, "ModelRuntime", facade.ModelRuntime),
                            patch.object(
                                subagent,
                                "_ReadOnlySettingsStore",
                                facade._ReadOnlySettingsStore,
                            ),
                            patch.object(
                                subagent,
                                "_run_capacity_mcp_compaction",
                                capacity_with_current_admission,
                            ),
                            patch.object(
                                subagent,
                                "_run_last_n_and_message",
                                context_with_canonical_gate,
                            ),
                        ):
                            try:
                                result = await subagent._run(
                                    settings,
                                    workspace,
                                    scenario=(
                                        "capacity"
                                        if runner_args.cases == "process-compaction"
                                        else runner_args.cases
                                    ),
                                    connection_id=args.connection_id,
                                    reasoning_effort=None,
                                )
                                if process_states:
                                    facts = []
                                    for state in process_states:
                                        scope = state.interop_scope
                                        value = dict(
                                            process_id=state.process_id,
                                            physical_completion=state.physical_completion.is_set(),
                                            physical_state=state.physical_state.value,
                                            reader_alive=state.reader.is_alive(),
                                            watcher_alive=state.watcher.is_alive(),
                                            linux_exit=state.process.poll(),
                                            scope_joined=scope.physically_joined.is_set(),
                                            scope_worker_alive=scope._thread.is_alive(),
                                            helper_exit=scope.helper.returncode,
                                            scope_directory_retired=scope.directory
                                            is None,
                                        )
                                        assert (
                                            value["physical_completion"]
                                            and value["scope_joined"]
                                        )
                                        assert (
                                            not value["reader_alive"]
                                            and not value["watcher_alive"]
                                        )
                                        assert (
                                            value["linux_exit"]
                                            == value["helper_exit"]
                                            == 0
                                        )
                                        assert (
                                            not value["scope_worker_alive"]
                                            and value["scope_directory_retired"]
                                        )
                                        facts.append(value)
                                    report["after_original_core_shutdown"] = facts
                            except BaseException:
                                report["failure_traceback"] = traceback.format_exc()
                                raise
                        report["cases"].append(result)
                        report["status"] = (
                            "completed"
                            if result["status"] == "passed"
                            else "semantic_failure"
                        )
                finally:
                    facade._drop_database(saved, database)
            else:
                # The original capability runner owns its home/controller/core,
                # assertions and report. Only its existing test factories change.
                original_cwd = Path.cwd()
                os.chdir(args.output.parent)
                original_runtime = capability.ModelRuntime
                try:
                    with ExitStack() as stack:
                        for name, value in {
                            "LocalSettingsStore": lambda *a, **k: (
                                api.shared._ReadOnlySettingsStore(saved)
                            ),
                            "_create_database": facade._create_database,
                            "_drop_database": facade._drop_database,
                            "TemporaryDirectory": facade.TemporaryDirectory,
                            "ModelRuntime": original_runtime,
                        }.items():
                            stack.enter_context(patch.object(capability, name, value))
                        await capability.run(tuple(runner_args.cases.split(",")))
                    files = list(
                        Path("output/diagnostics/capability-change-observation").glob(
                            "*.json"
                        )
                    )
                    assert len(files) == 1
                    result = json.loads(files[0].read_text(encoding="utf-8"))
                    report["provider_calls"].extend(result.pop("calls"))
                    report["cases"].append(result)
                    report["status"] = (
                        "completed"
                        if result["status"] == "PASSED"
                        else "semantic_failure"
                    )
                finally:
                    os.chdir(original_cwd)

    facade.run = run
    return facade


def main():
    parser = shared_adapter.parser()
    parser.add_argument("--kind", choices=("subagent", "capability"), required=True)
    args = parser.parse_args()
    api = shared_adapter.imports()
    api.terminal = driver(api, kind=args.kind, args=args)
    if args.check_imports:
        print(
            "Installed owners and original runners imported; no saved settings, PG or provider accessed."
        )
        return 0
    required = (
        "saved_home",
        "connection_id",
        "model",
        "distribution",
        "user",
        "expected_uid",
        "interop_helper",
        "socket",
        "port",
        "admin_role",
        "runtime_role",
        "root_database",
        "expected_data",
        "expected_system_id",
        "scratch_root",
        "output",
    )
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        parser.error("execute requires " + ", ".join(missing))
    old_umask = os.umask(0o077)
    scrub = (
        api.LocalSettingsStore(args.saved_home / "local-settings.yaml")
        .read()
        .credential_scrub_set()
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return shared_adapter.run(args, api)
    finally:
        os.umask(old_umask)
        public_out, public_err = (
            scrub.scrub_text(stdout.getvalue()),
            scrub.scrub_text(stderr.getvalue()),
        )
        assert not scrub.contains(public_out) and not scrub.contains(public_err)
        sys.stdout.write(public_out)
        sys.stderr.write(public_err)


if __name__ == "__main__":
    raise SystemExit(main())
