"""Run content-free Round 7.1 real-provider ToolResult projection probes.

The report contains only closed dispositions, counts, byte lengths and SHA-256
digests.  It never emits credentials, DSNs, prompts, tool/artifact bodies,
provider responses, environment values, hidden reasoning or private URLs.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.config import (
    DEFAULT_OPENAI_API,
    OPENAI_CHAT_COMPLETIONS_API,
)
from pulsara_agent.settings import PulsaraSettings, load_env_file
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput


_APIS = (OPENAI_CHAT_COMPLETIONS_API, DEFAULT_OPENAI_API)
_COMPLETE_MARKER = "ROUND71_COMPLETE_SENTINEL_42"
_MIDDLE_MARKER = "ROUND71_MIDDLE_SENTINEL_73"


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assert_local_pulsara_dsn(value: str, *, label: str) -> None:
    parameters = conninfo_to_dict(value)
    host = str(parameters.get("host", "")).strip().lower()
    database = str(parameters.get("dbname", "")).strip()
    if host not in {"localhost", "127.0.0.1", "::1"} or database != "pulsara":
        raise RuntimeError(f"{label} must target exact loopback database pulsara")


def _load_settings(env_file: str, *, api: str) -> PulsaraSettings:
    load_env_file(env_file, override=False)
    os.environ["PULSARA_API"] = api
    settings = PulsaraSettings.from_env()
    admin_dsn = os.getenv("PULSARA_POSTGRES_ADMIN_DSN", "").strip()
    if not admin_dsn:
        raise RuntimeError("PULSARA_POSTGRES_ADMIN_DSN is required")
    _assert_local_pulsara_dsn(admin_dsn, label="admin DSN")
    _assert_local_pulsara_dsn(settings.storage.postgres_dsn, label="runtime DSN")
    PostgresMigrationRunner(
        admin_dsn=admin_dsn,
        runtime_dsn=settings.storage.postgres_dsn,
    ).migrate(deadline_monotonic=monotonic() + 240.0)
    return settings


def _rows(session: Any) -> list[dict[str, object]]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30.0,
    ) as connection:
        return list(
            connection.execute(
                """
                SELECT b.tool_name, r.result_state, r.output_display_kind,
                       r.output_artifact_disposition,
                       octet_length(e.inline_content) AS inline_utf8_bytes
                FROM pulsara_v3.tool_results AS r
                JOIN pulsara_v3.assistant_message_blocks AS b
                  ON b.session_id = r.session_id
                 AND b.assistant_entry_id = r.tool_call_entry_id
                 AND b.tool_call_id = r.tool_call_id
                JOIN pulsara_v3.transcript_entries AS e
                  ON e.session_id = r.session_id AND e.id = r.result_entry_id
                WHERE r.session_id = %s
                ORDER BY r.accepted_at, r.id
                """,
                (session.session_id,),
            ).fetchall()
        )


def _system_prompt() -> str:
    return """
You are running a controlled ToolResult projection check. Follow the current
human request exactly. Use read_file first and use no tools other than
read_file and artifact_read. Never inspect credentials or environment values.
Return only the requested compact JSON object. Do not quote file contents.
""".strip()


def _complete_file() -> str:
    left = "a" * 17_000
    right = "b" * 17_000
    return left + _COMPLETE_MARKER + right + "\n"


def _middle_file() -> str:
    left = "c" * 31_000
    right = "d" * 31_000
    return "ROUND71_HEAD\n" + left + _MIDDLE_MARKER + right + "\nROUND71_TAIL\n"


def _require_exact_markers(text: str, markers: tuple[str, ...]) -> None:
    if not all(marker in text for marker in markers):
        raise RuntimeError("provider did not return the expected controlled marker")


async def _run_case(
    settings: PulsaraSettings,
    *,
    workspace: Path,
    scenario: str,
) -> dict[str, object]:
    import pulsara_agent.conversation_kernel.host as kernel_host

    kernel_host.load_mcp_server_configs = lambda **_: ()
    core = KernelHostCore.production(settings=settings)
    session = await core.open_session(
        HostWorkspaceInput(workspace_kind="project", workspace_root=workspace),
        system_prompt=_system_prompt(),
    )
    try:
        if scenario == "complete":
            expected_markers = (_COMPLETE_MARKER,)
            prompt = (
                "Read complete.txt exactly once. Return the marker embedded in the "
                "file and whether you called artifact_read as JSON keys marker and "
                "artifact_read_used. Do not call artifact_read."
            )
        elif scenario == "head_tail":
            expected_markers = ("ROUND71_HEAD", "ROUND71_TAIL")
            prompt = (
                "Read middle.txt exactly once. The only facts needed are the exact "
                "tokens at its visible beginning and end. Return both tokens and do "
                "not call artifact_read."
            )
        else:
            expected_markers = (_MIDDLE_MARKER,)
            prompt = (
                "Read middle.txt. The required marker is deliberately omitted from "
                "the visible head/tail preview. Use the visible artifact reference "
                "and artifact_read pages only as needed to recover it. Return JSON "
                "keys marker and artifact_read_used."
            )
        result = await session.run_turn(
            prompt,
            command_id=f"command:round7-1-dogfood:{scenario}:{settings.llm.api}",
        )
        _require_exact_markers(result.final_text, expected_markers)
        rows = await asyncio.to_thread(_rows, session)
        names = [str(row["tool_name"]) for row in rows]
        if not names or names[0] != "read_file":
            raise RuntimeError("provider did not begin with the controlled read_file")
        if any(name not in {"read_file", "artifact_read"} for name in names):
            raise RuntimeError("provider used an out-of-scope tool")
        read_row = rows[0]
        if scenario == "complete":
            if names != ["read_file"] or read_row["output_display_kind"] != "COMPLETE":
                raise RuntimeError("complete ToolResult projection contract failed")
        elif scenario == "head_tail":
            if names != ["read_file"] or read_row["output_display_kind"] != "HEAD_TAIL":
                raise RuntimeError("conditional head/tail projection contract failed")
        else:
            artifact_rows = [row for row in rows if row["tool_name"] == "artifact_read"]
            if (
                read_row["output_display_kind"] != "HEAD_TAIL"
                or read_row["output_artifact_disposition"] != "AVAILABLE"
                or not artifact_rows
                or any(row["output_display_kind"] != "COMPLETE" for row in artifact_rows)
            ):
                raise RuntimeError("artifact FULL-delivery projection contract failed")
        return {
            "scenario": scenario,
            "status": "passed",
            "tool_call_count": len(rows),
            "read_file_display_kind": read_row["output_display_kind"],
            "read_file_artifact_disposition": read_row[
                "output_artifact_disposition"
            ],
            "artifact_read_count": names.count("artifact_read"),
            "all_result_states_known": all(
                row["result_state"] == "SUCCESS" for row in rows
            ),
            "all_inline_result_bytes_within_canonical_bound": all(
                isinstance(row["inline_utf8_bytes"], int)
                and int(row["inline_utf8_bytes"]) <= 65_536
                for row in rows
            ),
            "final_utf8_bytes": len(result.final_text.encode("utf-8")),
            "final_sha256": _digest(result.final_text),
        }
    finally:
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--api", choices=_APIS, required=True)
    parser.add_argument(
        "--scenario", choices=("complete", "head_tail", "artifact"), required=True
    )
    args = parser.parse_args()
    settings = _load_settings(args.env_file, api=args.api)
    with TemporaryDirectory(prefix="pulsara-round7-1-") as directory:
        workspace = Path(directory)
        (workspace / "complete.txt").write_text(_complete_file(), encoding="utf-8")
        (workspace / "middle.txt").write_text(_middle_file(), encoding="utf-8")
        result = asyncio.run(
            _run_case(settings, workspace=workspace, scenario=args.scenario)
        )
    report = {
        "schema_version": "round7-1-tool-result-projection-dogfood.v1",
        "status": "passed",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider_api": settings.llm.api,
        "provider_model_role": "pro",
        "database_target_verified_loopback_pulsara": True,
        "result": result,
        "api_key_dsn_header_prompt_tool_or_artifact_body_provider_response_environment_or_hidden_reasoning_recorded": False,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
