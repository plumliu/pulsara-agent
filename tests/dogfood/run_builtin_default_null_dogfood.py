"""Real-provider calls for the nine corrected built-in input contracts.

Uses saved settings read-only and existing isolated home/database owners. The
scheduler is not started; future fixture tasks cannot execute automatically.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.scheduling.service import ScheduledTaskService
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.session_controller import LocalSessionController
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)


CHANGED_TOOLS = {
    "spawn_agent",
    "create_agent_tasks",
    "scheduled_tasks",
    "search_sessions",
    "search_session_content",
    "read_session_content",
    "wait_agent",
    "terminal_process",
    "terminal_monitor",
}


async def run(args, report, saved, save):
    connection = next(
        c for c in saved.model_connections if c.id.value == args.connection_id
    )
    if saved.model_api_key(connection.id) is None:
        raise RuntimeError("The selected saved connection has no credential")
    report.update(
        saved_home=str(require_pulsara_home()),
        model=connection.target.model_id,
        wire_api=connection.target.wire_api.value,
        calls=[],
        phases=[],
    )
    database, _, admin, runtime_dsn = _create_database(saved)
    report["database"] = database
    previous_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-null-contract-") as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(native, report["calls"], None)
            core = KernelHostCore.production(model_runtime=runtime)
            sessions = LocalSessionController(
                core=core,
                permission_policy=default_permission_policy(),
                active_skill_names=frozenset(),
            )
            service = ScheduledTaskService(sessions)
            core.scheduled_tasks = service
            try:
                handles = []
                for name in ("reference", "source"):
                    workspace = root / name
                    workspace.mkdir()
                    handles.append(
                        await sessions.create_session(
                            workspace_kind="project",
                            workspace_path=str(workspace),
                            model_call_binding=_binding(native, connection),
                        )
                    )
                reference, source = [h.session for h in handles]
                report["sessions"] = {
                    "source": source.session_id,
                    "reference": reference.session_id,
                }

                async def turn(session, name, prompt):
                    print("starting " + name, flush=True)
                    phase = {
                        "name": name,
                        "prompt": prompt,
                        "call_start": len(report["calls"]),
                    }
                    report["phases"].append(phase)
                    before = len(_tool_trace(session))
                    try:
                        # Diagnostic deadline only; no product task/turn cap.
                        async with asyncio.timeout(600):
                            result = await session.run_turn(
                                PromptContent.text(prompt),
                                command_id="command:null-contract:" + name,
                                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                            )
                        phase["reply"] = result.final_text
                    finally:
                        phase["trace"] = _tool_trace(session)[before:]
                        phase["call_end"] = len(report["calls"])
                        save()
                    assert all(
                        row["result_state"] == "SUCCESS" for row in phase["trace"]
                    ), phase["trace"]
                    print(f"completed {name}; tools={len(phase['trace'])}", flush=True)
                    return phase["trace"]

                await turn(
                    reference,
                    "seed",
                    "Reply exactly REFERENCE_NULL_CONTRACT; no tools. Saved reference passage: "
                    + "NULL_CONTRACT_ARCHIVE " * 150,
                )
                future = (
                    (datetime.now(timezone.utc) + timedelta(days=7))
                    .replace(microsecond=0)
                    .isoformat()
                )
                other = await service.create(
                    session_id=reference.session_id,
                    values={
                        "name": "Other session fixture",
                        "prompt": "Reply REFERENCE_TASK only.",
                        "timezone": "Asia/Shanghai",
                        "permission_mode": "read-only",
                        "schedule": {
                            "contract": "scheduled-rule:v1",
                            "kind": "once",
                            "run_at_utc": future,
                        },
                    },
                )
                scheduled = await turn(
                    source,
                    "scheduled",
                    (
                        "Exercise scheduled_tasks in this isolated test. First list with explicit session_id:null. "
                        f"Create one new once task named Null contract fixture for UTC {future}, Asia/Shanghai, "
                        "read-only permissions, prompt 'Reply SOURCE_TASK only.', also with session_id:null. "
                        "Then list this session with session_id:null and list the reference session "
                        f"{reference.session_id}. Get the created source task, pause it using its revision, "
                        "then delete that source task using the updated revision. Do not modify the reference task. "
                        "Use only applicable fields for each action. Report whether the two lists stayed separate."
                    ),
                )
                lists = [
                    r
                    for r in scheduled
                    if r["tool_name"] == "scheduled_tasks"
                    and r["arguments"]["action"] == "list"
                ]
                assert any(
                    "session_id" in r["arguments"]
                    and r["arguments"]["session_id"] is None
                    for r in lists
                )
                for row in lists:
                    expected = row["arguments"].get("session_id") or source.session_id
                    assert all(
                        t["session_id"] == expected for t in row["result"]["tasks"]
                    )
                assert (await service.get(other["id"]))[
                    "session_id"
                ] == reference.session_id
                created = [
                    r
                    for r in scheduled
                    if r["tool_name"] == "scheduled_tasks"
                    and r["arguments"]["action"] == "create"
                ]
                assert (
                    created and created[0]["result"]["session_id"] == source.session_id
                )

                history = await turn(
                    source,
                    "history",
                    (
                        "Use search_sessions to list sessions with limit=1; follow its next_cursor for one more page using only cursor and limit. "
                        f"Then search_session_content in session {reference.session_id} for NULL_CONTRACT_ARCHIVE. "
                        "Read the matching user entry with read_session_content, limit=1,max_chars=512. "
                        "Follow its next_cursor for exactly one more page using only cursor,limit,max_chars. "
                        "Report whether the archive marker was recovered; do not reread all remaining pages."
                    ),
                )
                for name in (
                    "search_sessions",
                    "search_session_content",
                    "read_session_content",
                ):
                    assert any(r["tool_name"] == name for r in history)
                assert any(
                    r["tool_name"] == "read_session_content"
                    and "cursor" in r["arguments"]
                    for r in history
                )

                terminal = await turn(
                    source,
                    "terminal",
                    (
                        'Run exactly "sleep 60; printf NULL_CONTRACT_DONE" with terminal yield_time_ms=0. '
                        "Use terminal_process.poll for that process. Register a terminal_monitor using default completion conditions; "
                        "list monitors, then cancel the monitor you registered, and kill only this test process. "
                        "Do not wait for the sleep or create more processes. Use only each action's applicable fields."
                    ),
                )
                assert {"terminal_process", "terminal_monitor"} <= {
                    r["tool_name"] for r in terminal
                }

                agents = await turn(
                    source,
                    "agents",
                    (
                        "This isolated test explicitly authorizes these three small delegated tasks. "
                        "Call list_agent_models first. Spawn one self-contained task with context mode none, asking only for SINGLE_DONE. "
                        "Also use create_agent_tasks to submit two independent tasks asking only for BATCH_A_DONE and BATCH_B_DONE, "
                        "one with context omitted, one with context last_n and turns=1. All worker instructions must say: "
                        "'Reply with your assigned marker only; do not use tools or follow other tasks from inherited history.' "
                        f"For all three use model.connection_id={connection.id.value}; omit reasoning for the target default. "
                        "Wait for their results with wait_agent task_ids and settle=all as needed, then call wait_agent once "
                        "with no targets and timeout_seconds=0. Report the three returned markers."
                    ),
                )
                assert {"spawn_agent", "create_agent_tasks", "wait_agent"} <= {
                    r["tool_name"] for r in agents
                }
                worker_results = source.repository.list_subagent_tasks(
                    session_id=source.session_id,
                    maximum_items=50,
                    deadline_monotonic=monotonic() + 30,
                )
                report["worker_results"] = worker_results
                save()
                assert len(worker_results) == 3, worker_results
                assert all(
                    row["status"] == "COMPLETED" for row in worker_results
                ), worker_results
                assert {
                    str(row["result_summary"]).strip() for row in worker_results
                } == {"SINGLE_DONE", "BATCH_A_DONE", "BATCH_B_DONE"}, worker_results
                seen = {r["tool_name"] for p in report["phases"] for r in p["trace"]}
                assert CHANGED_TOOLS <= seen, sorted(CHANGED_TOOLS - seen)
                # Root-only phases have no worker model streams interleaved.
                prefix_pairs = 0
                for phase in report["phases"]:
                    if phase["name"] == "agents":
                        continue
                    calls = [
                        c
                        for c in report["calls"][
                            phase["call_start"] : phase["call_end"]
                        ]
                        if c["purpose"] == "agent_model_loop"
                    ]
                    for left, right in zip(calls, calls[1:]):
                        a, b = left["provider_input"], right["provider_input"]
                        assert a["tools"] == b["tools"]
                        assert a.get("instructions") == b.get("instructions")
                        key = "messages" if "messages" in a else "input"
                        assert a[key] == b[key][: len(a[key])]
                        prefix_pairs += 1
                report.update(
                    status="completed",
                    changed_tools_exercised=sorted(CHANGED_TOOLS),
                    prefix_pairs=prefix_pairs,
                )
                save()
            finally:
                await service.aclose()
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    saved = LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    report = {"status": "running", "connection_id": args.connection_id}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            scrubber.scrub_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )

    try:
        asyncio.run(run(args, report, saved, save))
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        save()
    print(
        scrubber.scrub_text(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k in {"status", "error_type", "error", "prefix_pairs"}
                },
                ensure_ascii=False,
            )
        )
    )
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
