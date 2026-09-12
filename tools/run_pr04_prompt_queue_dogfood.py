"""PR04 real-provider/browser probe using saved, read-only production owners.

Only this probe holds assistant settlement at explicit file-controlled test
barriers. Production receives no flags, endpoints, queues or delivery owners.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import traceback

import pulsara_agent
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.web_app.application import LocalWebApplication
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_content_revision_line_edit_dogfood import _scrub
from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
)
from run_pr03_user_control_dogfood import (
    _Pr03RecordingRuntime,
    _json_default,
    _rows,
    _wait_until,
)
from run_round10_subagent_dogfood import (
    _wait_for_root_tool,
    _wait_for_task_key,
    _wait_for_task_status,
)


def _secrets(settings):
    values = [item.value for item in settings.model_api_keys]
    values.extend(item.value for item in settings.mcp_credentials)
    if settings.dashscope_credentials:
        values.extend(
            filter(
                None,
                (
                    settings.dashscope_credentials.embedding_api_key,
                    settings.dashscope_credentials.rerank_api_key,
                ),
            )
        )

    def tokens(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {
                    "access_token",
                    "refresh_token",
                    "client_secret",
                    "id_token",
                } and isinstance(item, str):
                    values.append(item)
                else:
                    tokens(item)

    for record in settings.mcp_oauth:
        for raw in (record.token_json, record.client_json, record.auth_json):
            tokens(json.loads(raw) if isinstance(raw, str) else raw)
    return tuple(value for value in values if value)


def _write(path, value, secrets):
    raw = json.dumps(
        _scrub(value, secrets), default=_json_default, ensure_ascii=False, indent=2
    )
    if any(secret in raw for secret in secrets):
        raise RuntimeError("configured credential survived dogfood scrubbing")
    path.write_text(raw + "\n")


class _SettlementBarrier:
    def __init__(self, session, directory):
        self.session = session
        self.directory = directory
        self.original = session._runner._assistant_settlements.settle

    async def _hold(self, phase, candidate, accepted=None):
        arm = self.directory / f"arm-{phase}"
        if not arm.exists() or not candidate.complete_turn:
            return
        arm.unlink()
        release = self.directory / f"release-{phase}"
        release.unlink(missing_ok=True)
        ready = self.directory / f"ready-{phase}.json"
        ready.write_text(
            json.dumps(
                {
                    "session_id": self.session.session_id,
                    "turn_id": candidate.cut.turn_id,
                    "assistant_entry_id": candidate.entry_id,
                    "phase": phase,
                    "turn_completed": None
                    if accepted is None
                    else accepted.turn_completed,
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        # Test-only barrier, no lifetime limit is imposed on the product.
        while not release.exists():
            await asyncio.sleep(0.02)

    async def settle(self, candidate):
        await self._hold("before", candidate)
        accepted = await self.original(candidate)
        await self._hold("after", candidate, accepted)
        return accepted


def _instrument(session, directory, installs):
    barrier = _SettlementBarrier(session, directory)
    session._runner._assistant_settlements.settle = barrier.settle
    original = session._runner._provider_input_installed_observer

    def observe(request):
        compiled = request.compiled_input
        installs.append(
            {
                "session_id": session.session_id,
                "turn_id": request.turn_id,
                "context_binding_revision_id": request.cut.context_binding_revision_id,
                "model_call_index": request.model_call_index,
                "system": compiled.system_prompt,
                "tools": repr(compiled.tools),
                "messages": [repr(message) for message in compiled.messages],
                "at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        if original:
            original(request)

    session._runner._provider_input_installed_observer = observe


async def _ready(directory, phase):
    return await _wait_until(
        f"{phase} settlement barrier",
        lambda: asyncio.sleep(0, result=(directory / f"ready-{phase}.json").exists()),
        bool,
    )


def _arm(directory, phase):
    (directory / f"ready-{phase}.json").unlink(missing_ok=True)
    (directory / f"arm-{phase}").touch()


def _release(directory, phase):
    (directory / f"release-{phase}").touch()


async def _queue_rows(session):
    return await asyncio.to_thread(
        _rows,
        session,
        "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence",
        (session.session_id,),
    )


async def _drain_queue(session):
    return await _wait_until(
        "FIFO completion",
        lambda: _queue_rows(session),
        lambda rows: (
            all(row["status"] != "PENDING" for row in rows)
            and session.active_root_turn_id() is None
        ),
        timeout_seconds=180,
    )


async def _provider_cases(session, workspace, directory, installs, calls):
    results = {}
    release_child = workspace / "release-child"
    hold = f"while [ ! -f {shlex.quote(str(release_child))} ]; do sleep 0.1; done; echo PR04_CHILD_DONE"
    prompt = f"""Create exactly one general_worker task with task_key pr04_child. Its objective must call terminal exactly once with command {hold!r}; after that command completes, report_agent_result alone with summary PR04_CHILD_DONE. Then call wait_agent for that exact task ID with settle=all and timeout_seconds=120. If an exact steer wakes the wait, acknowledge its marker and finish this ROOT without calling wait again. Do not finish before both create_agent_tasks and wait_agent have been called."""
    run = asyncio.create_task(
        session.run_turn(
            prompt,
            command_id="command:pr04:wait-root",
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        )
    )
    task = await _wait_for_task_key(session, task_key="pr04_child", timeout_seconds=90)
    root = session.active_root_turn_id()
    assert root
    await _wait_for_root_tool(
        session, turn_id=root, tool_name="wait_agent", timeout_seconds=90
    )
    texts = [
        "PR04_FIFO_ZERO: reply only FIFO_ZERO_DONE.",
        "PR04_WAIT_STEER: acknowledge PR04_WAIT_STEER and finish now without waiting again.",
        "  PR04_EDIT_ORIGINAL: reply only EDIT_DONE.  ",
        "PR04_DELETE_ORIGINAL: reply only DELETE_SHOULD_NOT_RUN.",
    ]
    queued = [
        await session.submit_prompt(
            command_id=f"command:pr04:source:{index}",
            text=text,
            requested_permission_mode=PermissionMode.READ_ONLY,
        )
        for index, text in enumerate(texts)
    ]
    await asyncio.sleep(0.3)
    wait_before = await _wait_for_root_tool(
        session, turn_id=root, tool_name="wait_agent", timeout_seconds=5
    )
    assert wait_before.get("result_state") is None
    assert [row["command_id"] for row in await _queue_rows(session)] == [
        item.command_id for item in queued
    ]
    redirect = await session.steer_queued_prompt(
        command_id="command:pr04:redirect-wait",
        source_queue_item_id=queued[1].prompt_delivery.queue_item_id,
        target_turn_id=root,
    )
    assert redirect.public_code in {
        "PROMPT_STEER_QUEUED",
        "PROMPT_CONSUMED",
        "TURN_RUNNING",
    }
    for index in (2, 3):
        cancelled = await session.cancel_queued_prompt(
            command_id=f"command:pr04:cancel:{index}",
            source_queue_item_id=queued[index].prompt_delivery.queue_item_id,
        )
        assert (
            cancelled.status == "SUCCEEDED"
            and cancelled.public_code == "PROMPT_CANCELLED"
        )
    edited = await session.submit_prompt(
        command_id="command:pr04:edited-resubmit",
        text=texts[2],
        requested_permission_mode=PermissionMode.READ_ONLY,
    )
    try:
        answer = await asyncio.wait_for(run, 150)
        assert answer.turn_id == root and "PR04_WAIT_STEER" in answer.final_text
        settled_wait = await _wait_for_root_tool(
            session,
            turn_id=root,
            tool_name="wait_agent",
            timeout_seconds=5,
            require_result=True,
        )
        raw = settled_wait["result_content"]
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        assert json.loads(raw)["outcome"] == "steer_available"
        child_before_release = await asyncio.to_thread(
            _rows,
            session,
            "SELECT status FROM pulsara_v3.subagent_tasks WHERE session_id=%s AND id=%s",
            (session.session_id, str(task["id"])),
        )
        assert child_before_release[0]["status"] not in {
            "CANCELLED",
            "INTERRUPTED",
            "FAILED",
        }
        results["wait_and_fifo"] = {
            "root_turn_id": root,
            "wait_before": wait_before,
            "wait_result": settled_wait,
            "queued_commands": [item.command_id for item in queued],
            "redirect_command": redirect.command_id,
            "edited_command": edited.command_id,
            "answer": answer.final_text,
            "child_before_release": child_before_release,
        }
    finally:
        release_child.touch()
    await _wait_for_task_status(
        session,
        task_id=str(task["id"]),
        expected=frozenset({"COMPLETED"}),
        timeout_seconds=120,
    )
    await _drain_queue(session)

    _arm(directory, "before")
    run = asyncio.create_task(
        session.run_turn(
            "Reply exactly PR04_FINAL_VISIBLE.", command_id="command:pr04:final-root"
        )
    )
    await _ready(directory, "before")
    root = session.active_root_turn_id()
    source = await session.submit_prompt(
        command_id="command:pr04:final-source",
        text="PR04_AFTER_FINAL: reply exactly SAME_TURN_DONE.",
    )
    count_before = len(calls)
    redirect = await session.steer_queued_prompt(
        command_id="command:pr04:final-action",
        source_queue_item_id=source.prompt_delivery.queue_item_id,
        target_turn_id=root,
    )
    assert redirect.status == "PENDING" and len(calls) == count_before
    _release(directory, "before")
    answer = await asyncio.wait_for(run, 120)
    assert answer.turn_id == root and "SAME_TURN_DONE" in answer.final_text
    pair = [item for item in installs if item["turn_id"] == root]
    assert len(pair) == 2
    assert (
        pair[0]["system"] == pair[1]["system"] and pair[0]["tools"] == pair[1]["tools"]
    )
    assert pair[1]["messages"][: len(pair[0]["messages"])] == pair[0]["messages"]
    results["accepted_after_final"] = {
        "root_turn_id": root,
        "source_command": source.command_id,
        "action_command": redirect.command_id,
        "answer": answer.final_text,
        "request_count_before_release": count_before,
    }

    _arm(directory, "before")
    _arm(directory, "after")
    run = asyncio.create_task(
        session.run_turn(
            "Reply exactly PR04_FENCE_FIRST.", command_id="command:pr04:fence-root"
        )
    )
    await _ready(directory, "before")
    root = session.active_root_turn_id()
    source = await session.submit_prompt(
        command_id="command:pr04:fence-source",
        text="PR04_FIFO_AFTER_FENCE: reply exactly FIFO_AFTER_FENCE_DONE.",
    )
    _release(directory, "before")
    await _ready(directory, "after")
    rejected = await session.steer_queued_prompt(
        command_id="command:pr04:fence-action",
        source_queue_item_id=source.prompt_delivery.queue_item_id,
        target_turn_id=root,
    )
    assert rejected.public_code == "STEER_TARGET_CLOSED"
    assert (
        await session.query_command(source.command_id)
    ).prompt_delivery.queue_status == "PENDING"
    _release(directory, "after")
    await run
    await _drain_queue(session)
    results["fence_first"] = {
        "closed_root_turn_id": root,
        "source_command": source.command_id,
        "action_code": rejected.public_code,
    }

    _arm(directory, "before")
    run = asyncio.create_task(
        session.run_turn(
            "Reply exactly PR04_LARGE_SOURCE_READY.",
            command_id="command:pr04:large-root",
        )
    )
    await _ready(directory, "before")
    root = session.active_root_turn_id()
    body = "PR04_LARGE_RAW\n" + "x" * (70 << 10) + "\nReply only LARGE_STEER_DONE."
    source = await session.submit_prompt(
        command_id="command:pr04:large-source", text=body
    )
    redirected = await session.steer_queued_prompt(
        command_id="command:pr04:large-action",
        source_queue_item_id=source.prompt_delivery.queue_item_id,
        target_turn_id=root,
    )
    duplicate_query = await session.query_command(redirected.command_id)
    assert (
        duplicate_query.prompt_delivery.queue_item_id
        == redirected.prompt_delivery.queue_item_id
    )
    large_rows = [
        row
        for row in await _queue_rows(session)
        if row["command_id"] in {source.command_id, redirected.command_id}
    ]
    assert (
        len(large_rows) == 2
        and large_rows[0]["blob_id"] == large_rows[1]["blob_id"]
        and large_rows[0]["blob_id"] is not None
    )
    _release(directory, "before")
    answer = await asyncio.wait_for(run, 150)
    assert answer.turn_id == root and "LARGE_STEER_DONE" in answer.final_text
    results["large_blob"] = {
        "root_turn_id": root,
        "source_command": source.command_id,
        "action_command": redirected.command_id,
        "utf8_size": len(body.encode()),
        "blob_id": large_rows[0]["blob_id"],
        "answer": answer.final_text,
    }
    return results


async def _run(args, saved, secrets):
    connection = next(
        item for item in saved.model_connections if item.id.value == args.connection_id
    )
    database, _, admin, runtime_dsn = _create_database(saved)
    report = {
        "schema_version": "pr04-prompt-queue-dogfood.v1",
        "status": "running",
        "package_path": pulsara_agent.__file__,
        "saved_home": str(require_pulsara_home()),
        "connection_id": connection.id.value,
        "model_id": connection.target.model_id,
        "postgres_runtime_dsn": runtime_dsn,
        "provider_calls": [],
        "provider_input_installs": [],
    }
    old_home = os.environ.get("PULSARA_HOME")
    sessions = []
    try:
        with TemporaryDirectory(prefix="pulsara-pr04-home-") as home:
            os.environ["PULSARA_HOME"] = home
            workspace = args.output_dir / "workspace"
            workspace.mkdir(exist_ok=True)
            runtime_settings = replace(
                saved, postgres=LocalPostgresConfig(runtime_dsn, admin)
            )
            store = _ReadOnlySettingsStore(runtime_settings)
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=store, catalog=catalog)
            runtime = _Pr03RecordingRuntime(delegate, report["provider_calls"], None)
            core = KernelHostCore.production(model_runtime=runtime)
            original_open = core.open_session

            async def opened(*a, **kw):
                session = await original_open(*a, **kw)
                _instrument(session, args.output_dir, report["provider_input_installs"])
                sessions.append(session)
                return session

            core.open_session = opened
            app = None
            try:
                workspace_input = HostWorkspaceInput(
                    workspace_kind="project",
                    workspace_root=workspace,
                    trust_workspace_mcp_config=False,
                )
                if args.serve_browser:
                    app = LocalWebApplication(
                        settings=store,
                        catalog=catalog,
                        model_runtime=runtime,
                        core=core,
                        workspace_input=workspace_input,
                        permission_policy=preset_to_policy(
                            PermissionMode.BYPASS_PERMISSIONS
                        ),
                        port=args.port,
                    )
                    await app.start()
                    handle = await app.sessions.create_session(
                        workspace_kind="project", workspace_path=str(workspace)
                    )
                    session = handle.session
                    await session.update_model_call_binding(
                        _binding(delegate, connection)
                    )
                    report["origin"] = app.origin
                    report["static_root"] = str(app.static_root)
                    report["session_id"] = session.session_id
                    report["workspace"] = str(workspace)
                    print(
                        json.dumps(
                            {
                                key: report[key]
                                for key in (
                                    "origin",
                                    "static_root",
                                    "session_id",
                                    "workspace",
                                    "package_path",
                                )
                            }
                        ),
                        flush=True,
                    )
                    while not (args.output_dir / "stop-server").exists():
                        report["canonical"] = [
                            await _canonical(session) for session in sessions
                        ]
                        _write(
                            args.output_dir / "browser-runtime.json", report, secrets
                        )
                        await asyncio.sleep(0.5)
                    report["status"] = "browser-recording-finished"
                else:
                    session = await core.open_session(
                        workspace_input,
                        system_prompt="Follow each human instruction exactly. This is a controlled queue/wait test. Use only the requested tools and keep replies short. Preserve IDs and markers.",
                    )
                    await session.update_model_call_binding(
                        _binding(delegate, connection)
                    )
                    report["session_id"] = session.session_id
                    report["cases"] = await _provider_cases(
                        session,
                        workspace,
                        args.output_dir,
                        report["provider_input_installs"],
                        report["provider_calls"],
                    )
                    report["status"] = "passed"
            except BaseException:
                report["status"] = "failed"
                report["failure"] = traceback.format_exc()
            finally:
                report["canonical"] = [
                    await _canonical(session) for session in sessions
                ]
                for phase in ("before", "after"):
                    _release(args.output_dir, phase)
                if app:
                    await app.aclose()
                await core.shutdown()
    finally:
        if old_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = old_home
        _drop_database(saved, database)
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write(
            args.output_dir
            / ("browser-runtime.json" if args.serve_browser else "real-provider.json"),
            report,
            secrets,
        )
    print(
        json.dumps({"status": report["status"], "output": str(args.output_dir)}),
        flush=True,
    )
    return 0 if report["status"] in {"passed", "browser-recording-finished"} else 1


async def _canonical(session):
    result = {"session_id": session.session_id}
    for key, statement in {
        "queue": "SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence",
        "commands": "SELECT * FROM pulsara_v3.session_commands WHERE session_id=%s ORDER BY accepted_at",
        "entries": "SELECT id, turn_id, entry_kind, entry_sequence, accepted_at, inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence",
        "turns": "SELECT id, status, terminal_reason, accepted_at, terminal_at FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at",
    }.items():
        result[key] = await asyncio.to_thread(
            _rows, session, statement, (session.session_id,)
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--serve-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    saved = LocalSettingsStore().read()
    return asyncio.run(_run(args, saved, _secrets(saved)))


if __name__ == "__main__":
    raise SystemExit(main())
