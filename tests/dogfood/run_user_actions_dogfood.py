"""Saved DeepSeek acceptance for lossy, session-local user-action observations."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import traceback
from tempfile import TemporaryDirectory
from time import monotonic

import psycopg
from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.user_control import (
    UserControlOperation, UserControlRequest, UserControlTarget, UserControlTargetKind,
)
from pulsara_agent.desktop_runtime import validate_managed_postgres
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _RecordingModelRuntime, _binding,
)
from tests.support.postgres_database import (
    create_migrated_postgres_test_database, drop_postgres_test_database,
)


def _saved_settings():
    home = require_pulsara_home()
    store = LocalSettingsStore(home / "local-settings.yaml")
    saved = store.read()
    if saved.postgres is None:
        # Desktop supplies this configuration to LocalSettingsStore at bootstrap.
        socket = str(home / "desktop" / "socket")
        managed = LocalPostgresConfig(
            runtime_dsn=make_conninfo(host=socket, port=5432, dbname="pulsara", user="pulsara_runtime"),
            admin_dsn=make_conninfo(host=socket, port=5432, dbname="pulsara", user="pulsara_admin"),
        )
        validate_managed_postgres(managed)
        with psycopg.connect(managed.admin_dsn) as connection:
            database, role, directory = connection.execute(
                "SELECT current_database(),current_user,current_setting('data_directory')"
            ).fetchone()
        if (database, role, directory) != ("pulsara", "pulsara_admin", str(home / "desktop" / "postgres")):
            raise RuntimeError("Desktop PostgreSQL owner does not match the effective home")
        saved = LocalSettingsStore(store.path, postgres=managed).read()
    return saved


def _observations(value):
    if isinstance(value, str):
        if value.startswith('{"pulsara_runtime_observation":'):
            yield json.loads(value)["pulsara_runtime_observation"]
    elif isinstance(value, list):
        for item in value:
            yield from _observations(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _observations(item)


def _actions(call):
    return [json.loads(item["body"])["actions"]
            for item in _observations(call["provider_input"])
            if item["source"] == "USER_ACTIONS" and item["presence"] == "VALUE"]


def _rows(session):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30,
    ) as connection:
        entries = connection.execute(
            "SELECT entry_kind,count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id=%s GROUP BY entry_kind ORDER BY entry_kind", (session.session_id,),
        ).fetchall()
        events = connection.execute(
            "SELECT event_type,count(*) FROM pulsara_v3.agent_events "
            "WHERE session_id=%s GROUP BY event_type ORDER BY event_type", (session.session_id,),
        ).fetchall()
    return {"entries": dict(entries), "events": dict(events)}


def _assert_prefix(calls):
    for previous, current in zip(calls, calls[1:]):
        old, new = previous["provider_input"], current["provider_input"]
        key = "messages" if "messages" in old else "input"
        assert new[key][:len(old[key])] == old[key]
        assert {k: v for k, v in old.items() if k != key} == {
            k: v for k, v in new.items() if k != key
        }
    for call in calls:
        assert all(item["source"] not in {"CAPABILITY_CHANGE", "TOOL_OBSERVATION_FRESHNESS"}
                   for item in _observations(call["provider_input"]))
        assert all(item["presence"] == "VALUE" for item in _observations(call["provider_input"])
                   if item["source"] == "USER_ACTIONS")


async def _run(saved, database, temporary: Path, wires, report):
    settings = _ReadOnlySettingsStore(replace(saved, input_suggestions_enabled=False,
        postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)))
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    await catalog.refresh()
    delegate = ModelRuntime.production(settings=settings, catalog=catalog)
    all_calls = report["calls"]
    core = KernelHostCore.production(model_runtime=_RecordingModelRuntime(delegate, all_calls))

    async def session(name, connection):
        root = temporary / name
        root.mkdir()
        host = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=root))
        await host.update_model_call_binding(_binding(delegate, connection))
        host.repository.rename_session(session_id=host.session_id,
            memory_domain_id=host._memory_domain_id, title=name,
            deadline_monotonic=monotonic() + 30)
        return host, root

    async def turn(host, text):
        return await host.run_turn(PromptContent.text(text), command_id=random_id("command"),
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)

    try:
        for api in wires:
            connection = next((c for c in saved.model_connections
                if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"
                and c.target.wire_api.value == api), None)
            if connection is None or saved.model_api_key(connection.id) is None:
                raise RuntimeError(f"Saved DeepSeek connection/credential is missing for {api}")

            host, root = await session(f"workspace-{api}", connection)
            start = len(all_calls)
            await turn(host, "这是隔离验收。不要调用工具，只回复 READY。")
            (root / "original.txt").write_text("Original content must not be restored.\n")
            before = _rows(host)
            call_count = len(all_calls)
            shutil.rmtree(root)
            await host.restore_missing_workspace()
            assert list(root.iterdir()) == [] and _rows(host) == before
            assert len(all_calls) == call_count
            result = await turn(host,
                '不要调用工具。仅依据实际收到的 runtime observation，返回 JSON 对象：'
                'directory_recreated（是否重建目录）、original_files_restored（是否恢复原文件）。'
                '没有相关观察则两项均返回 null。只输出原始 JSON，不要 Markdown 或代码围栏。')
            observed = json.loads(result.final_text)
            assert observed == {"directory_recreated": True, "original_files_restored": False}
            received = _actions(all_calls[call_count])
            assert len(received) == 1 and received[0][0]["kind"] == "workspace_recreated"
            await turn(host, "不要调用工具，只回复 NEXT。")
            assert _actions(all_calls[-1]) == received
            _assert_prefix(all_calls[start:])
            report["cases"].append({"wire": api, "kind": "workspace_recreated",
                "session_id": host.session_id, "reply": result.final_text,
                "original_files_restored": False, "idle_calls": 0,
                "canonical_before_control": before, "canonical_after_turns": _rows(host),
                "calls": [start, len(all_calls)], "prefix_continuity": True})

            host, root = await session(f"process-{api}", connection)
            start = len(all_calls)
            program = "import time; print('USER_ACTIONS_PROCESS_READY',flush=True); time.sleep(600)"
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
            await turn(host,
                "这是隔离验收。仅调用一次 exec_process 运行下面命令，yield_time_ms=0；"
                "返回后不要等待、轮询或停止进程，回复 PROCESS_STARTED。命令：\n" + command)
            processes = host.list_background_processes(expected_session_id=host.session_id,
                expected_host_session_id=host.host_session_id)
            process = next(p for p in processes if p.command == command)
            assert host.active_root_turn_id() is None
            before = _rows(host)
            call_count = len(all_calls)
            request = UserControlRequest(UserControlOperation.TERMINATE_BACKGROUND_PROCESS,
                random_id("command"), host.session_id, host.host_session_id,
                UserControlTarget(UserControlTargetKind.BACKGROUND_PROCESS, process.process_id),
                host.control_admission_deadline_ms())
            await host.request_terminate_background_process(command_id=request.command_id,
                admission_deadline_ms=request.admission_deadline_ms,
                expected_session_id=host.session_id, expected_host_session_id=host.host_session_id,
                process_id=process.process_id)
            deadline = monotonic() + 90
            while True:
                query = await host.query_control_command(request)
                if query.outcome is not None and query.outcome.status != "PENDING":
                    break
                if monotonic() >= deadline:
                    raise TimeoutError("The controlled process did not terminate")
                await asyncio.sleep(0.02)
            assert query.outcome.status == "SUCCEEDED"
            actual = query.outcome.user_control.process
            assert actual.disposition == "TERMINATION_COMPLETED"
            assert _rows(host) == before and len(all_calls) == call_count
            result = await turn(host,
                '不要调用工具。仅依据实际收到的 runtime observation 报告用户控制的进程。'
                '只输出 JSON 对象，字段 process_id、disposition、status，保持观察原值；'
                '没有相关观察则字段均为 null。只输出原始 JSON，不要 Markdown 或代码围栏。')
            observed = json.loads(result.final_text)
            assert observed == {"process_id": process.process_id,
                "disposition": actual.disposition, "status": actual.status}
            received = _actions(all_calls[call_count])
            assert len(received) == 1 and received[0][0]["kind"] == "process_control"
            await turn(host, "不要调用工具，只回复 NEXT。")
            assert _actions(all_calls[-1]) == received
            _assert_prefix(all_calls[start:])
            report["cases"].append({"wire": api, "kind": "process_control",
                "session_id": host.session_id, "process_id": process.process_id,
                "reply": result.final_text, "physical_state": actual.physical_state,
                "group_alive": actual.group_alive, "idle_calls": 0,
                "canonical_before_control": before, "canonical_after_turns": _rows(host),
                "calls": [start, len(all_calls)], "prefix_continuity": True})

        for item in report["cases"]:
            assert "USER_CONTROL_FEEDBACK" not in item["canonical_after_turns"]["entries"]
            assert "UserControlFeedbackAccepted" not in item["canonical_after_turns"]["events"]
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        return report
    finally:
        await core.shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("output/runtime-observation-implementation/real-provider.json"))
    parser.add_argument("--wire", action="append", choices=("openai_chat_completions", "openai_responses"))
    args = parser.parse_args()
    saved = _saved_settings()
    scrub = secret_scrubber(saved)
    original = {key: os.environ.get(key) for key in
        ("PULSARA_HOME", "PULSARA_POSTGRES_DSN", "PULSARA_POSTGRES_ADMIN_DSN")}
    database = None
    report = {"completed_at": None, "cases": [], "calls": []}
    try:
        os.environ["PULSARA_POSTGRES_DSN"] = saved.postgres.runtime_dsn
        os.environ["PULSARA_POSTGRES_ADMIN_DSN"] = saved.postgres.admin_dsn
        database = create_migrated_postgres_test_database()
        with TemporaryDirectory(prefix="pulsara-user-actions-") as raw:
            base = Path(raw)
            home = base / "home"
            home.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            result = asyncio.run(_run(saved, database, base,
                args.wire or ["openai_chat_completions", "openai_responses"], report))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(scrub.scrub_text(json.dumps(result, ensure_ascii=False, indent=2)) + "\n")
        print(json.dumps({"evidence": str(args.output.resolve()), "cases": len(result["cases"]),
            "provider_calls": len(result["calls"]), "prefix_continuity": True}, ensure_ascii=False))
    except BaseException:
        report["error"] = traceback.format_exc()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(scrub.scrub_text(json.dumps(report, ensure_ascii=False, indent=2)) + "\n")
        raise
    finally:
        if database is not None:
            drop_postgres_test_database(saved.postgres.admin_dsn, database.database_name)
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    main()
