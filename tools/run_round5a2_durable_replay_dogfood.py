"""Content-free real-provider restart probe for Round 5A.2.

The report contains only closed API/status values, counts, byte sizes and
fingerprints.  Credentials, DSNs, prompts, assistant text, private replay
bodies, provider responses, headers and endpoint URLs are never emitted.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
)
from pulsara_agent.settings import (
    PulsaraSettings,
    StorageConfig,
    load_env_file,
)
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput


_APIS = ("openai_chat_completions", "openai_responses")


def _dsn_with_database(dsn: str, database_name: str) -> str:
    parameters = conninfo_to_dict(dsn)
    parameters["dbname"] = database_name
    return make_conninfo(**parameters)


def _require_loopback_pulsara(dsn: str, *, label: str) -> None:
    parameters = conninfo_to_dict(dsn)
    host = str(parameters.get("host", "")).strip().lower()
    database = str(parameters.get("dbname", "")).strip()
    if host not in {"localhost", "127.0.0.1", "::1"} or database != "pulsara":
        raise RuntimeError(f"{label} must target exact loopback database pulsara")


def _drop_database(admin_root_dsn: str, database_name: str) -> None:
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
        )


def _create_database(
    settings: PulsaraSettings,
) -> tuple[str, str, str]:
    admin_root_dsn = os.getenv("PULSARA_POSTGRES_ADMIN_DSN", "").strip()
    if not admin_root_dsn:
        raise RuntimeError("PULSARA_POSTGRES_ADMIN_DSN is required")
    _require_loopback_pulsara(admin_root_dsn, label="admin DSN")
    _require_loopback_pulsara(settings.storage.postgres_dsn, label="runtime DSN")
    database_name = f"pulsara_round5a2_{os.getpid()}_{uuid4().hex[:10]}"
    admin_dsn = _dsn_with_database(admin_root_dsn, database_name)
    runtime_dsn = _dsn_with_database(settings.storage.postgres_dsn, database_name)
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        report = PostgresMigrationRunner(
            admin_dsn=admin_dsn, runtime_dsn=runtime_dsn
        ).migrate(deadline_monotonic=monotonic() + 240)
        if report.migration_head_version != 0:
            raise RuntimeError("Round 5A.2 dogfood clean-v0 install failed")
    except BaseException:
        _drop_database(admin_root_dsn, database_name)
        raise
    return admin_root_dsn, database_name, runtime_dsn


def _safe_settings(env_file: str, *, api: str, runtime_dsn: str) -> PulsaraSettings:
    load_env_file(env_file, override=False)
    os.environ["PULSARA_API"] = api
    os.environ["PULSARA_MEMORY_AUTO_DENSE"] = "false"
    os.environ["PULSARA_MEMORY_EXPLICIT_RERANK"] = "false"
    os.environ["PULSARA_MEMORY_CHEAP_HINT_REFLECTION"] = "false"
    return replace(
        PulsaraSettings.from_env(), storage=StorageConfig(postgres_dsn=runtime_dsn)
    )


def _assistant_replay_summary(session) -> dict[str, object]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """
            SELECT count(*) FILTER (
                       WHERE e.provider_replay_disposition = 'NATIVE_REPLAY'
                   ) AS native_entries,
                   count(r.id) AS replay_rows,
                   coalesce(sum(r.payload_size), 0) AS replay_bytes,
                   coalesce(sum(r.item_count), 0) AS replay_items,
                   count(*) FILTER (
                       WHERE e.provider_replay_disposition = 'PUBLIC_SEMANTIC_ONLY'
                   ) AS public_only_entries
            FROM pulsara_v3.transcript_entries AS e
            LEFT JOIN pulsara_v3.provider_assistant_replay_fragments AS r
              ON r.session_id = e.session_id AND r.assistant_entry_id = e.id
            WHERE e.session_id = %s
              AND e.entry_kind IN ('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')
            """,
            (session.session_id,),
        ).fetchone()
    return {
        "native_entries": int(row["native_entries"]),
        "replay_rows": int(row["replay_rows"]),
        "replay_bytes": int(row["replay_bytes"]),
        "replay_items": int(row["replay_items"]),
        "public_only_entries": int(row["public_only_entries"]),
    }


async def _child(args: argparse.Namespace) -> dict[str, object]:
    runtime_dsn = os.environ["ROUND5A2_DOGFOOD_RUNTIME_DSN"]
    settings = _safe_settings(args.env_file, api=args.api, runtime_dsn=runtime_dsn)
    workspace = Path(os.environ["ROUND5A2_DOGFOOD_WORKSPACE"])
    observed_hydrations: list[tuple[bool, int]] = []
    original_hydrate = CanonicalProviderInputReader.hydrate_selected_provider_replays

    def record_hydration(reader, **kwargs):
        hydration = original_hydrate(reader, **kwargs)
        observed_hydrations.append(
            (
                hydration is not None,
                0 if hydration is None else len(hydration.fragments),
            )
        )
        return hydration

    CanonicalProviderInputReader.hydrate_selected_provider_replays = record_hydration
    import pulsara_agent.conversation_kernel.host as kernel_host

    kernel_host.load_mcp_server_configs = lambda **_: ()
    core = KernelHostCore.production(settings=settings)
    workspace_input = HostWorkspaceInput(
        workspace_kind="project", workspace_root=workspace
    )
    session = (
        await core.open_session(
            workspace_input,
            system_prompt=(
                "Return one brief plain-text answer. Do not call tools and do not "
                "repeat the request."
            ),
        )
        if args.mode == "create"
        else await core.resume_session(
            args.session_id,
            workspace_input=workspace_input,
            system_prompt=(
                "Return one brief plain-text answer. Do not call tools and do not "
                "repeat the request."
            ),
        )
    )
    try:
        result = await session.run_turn(
            "Reply with a short acknowledgement.",
            command_id=f"command:round5a2:{args.mode}",
        )
        report = {
            "status": "passed",
            "mode": args.mode,
            "api": settings.llm.api,
            "session_id": session.session_id,
            "completed": bool(result.final_entry_id),
            "public_answer_utf8_bytes": len(result.final_text.encode("utf-8")),
            "selected_hydration_present": any(
                present for present, _count in observed_hydrations
            ),
            "selected_replacement_count": sum(
                count for _present, count in observed_hydrations
            ),
            "canonical": _assistant_replay_summary(session),
        }
    except BaseException as exc:
        report = {
            "status": "external_failure",
            "mode": args.mode,
            "api": settings.llm.api,
            "failure_type": type(exc).__name__,
        }
        if isinstance(exc, ProviderModelExecutionFailed):
            report["failure_code"] = exc.error.code.value
        elif isinstance(exc, ProviderModelOutputIncomplete):
            report["incomplete_reason"] = exc.reason.value
    print(json.dumps(report, sort_keys=True), flush=True)
    if args.abrupt:
        os._exit(0)
    await core.shutdown()
    return report


def _invoke_child(
    *,
    env_file: str,
    api: str,
    mode: str,
    session_id: str,
    abrupt: bool,
    runtime_dsn: str,
    workspace: Path,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment["ROUND5A2_DOGFOOD_RUNTIME_DSN"] = runtime_dsn
    environment["ROUND5A2_DOGFOOD_WORKSPACE"] = str(workspace)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        "--env-file",
        env_file,
        "--api",
        api,
        "--mode",
        mode,
        "--session-id",
        session_id,
    ]
    if abrupt:
        command.append("--abrupt")
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not lines:
        return {
            "status": "external_failure",
            "mode": mode,
            "api": api,
            "failure_type": "CHILD_PROCESS_FAILED",
        }
    try:
        parsed = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {
            "status": "external_failure",
            "mode": mode,
            "api": api,
            "failure_type": "CHILD_REPORT_INVALID",
        }
    return parsed


def _parent(args: argparse.Namespace) -> dict[str, object]:
    load_env_file(args.env_file, override=False)
    os.environ["PULSARA_API"] = args.api
    settings = PulsaraSettings.from_env()
    admin_root_dsn, database_name, runtime_dsn = _create_database(settings)
    try:
        with TemporaryDirectory(prefix="pulsara-round5a2-") as directory:
            workspace = Path(directory)
            first = _invoke_child(
                env_file=args.env_file,
                api=args.api,
                mode="create",
                session_id="unused-for-create",
                abrupt=True,
                runtime_dsn=runtime_dsn,
                workspace=workspace,
            )
            if first.get("status") != "passed":
                return {
                    "schema_version": "round5a2-durable-replay-dogfood.v1",
                    "status": "external_failure",
                    "api": args.api,
                    "create": first,
                }
            second = _invoke_child(
                env_file=args.env_file,
                api=args.api,
                mode="continue",
                session_id=str(first["session_id"]),
                abrupt=False,
                runtime_dsn=runtime_dsn,
                workspace=workspace,
            )
            public_first = {
                key: value for key, value in first.items() if key != "session_id"
            }
            public_second = {
                key: value for key, value in second.items() if key != "session_id"
            }
            return {
                "schema_version": "round5a2-durable-replay-dogfood.v1",
                "status": (
                    "passed"
                    if second.get("status") == "passed"
                    else "external_failure"
                ),
                "api": args.api,
                "fresh_processes": 2,
                "host_a_abrupt_after_commit": True,
                "create": public_first,
                "continue": public_second,
                "private_content_or_credentials_recorded": False,
            }
    finally:
        _drop_database(admin_root_dsn, database_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--api", choices=_APIS, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--mode", choices=("create", "continue"), default="create")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--abrupt", action="store_true")
    args = parser.parse_args()
    if args.child:
        asyncio.run(_child(args))
        return 0
    report = _parent(args)
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
