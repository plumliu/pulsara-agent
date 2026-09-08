"""Real-provider activation probe for revision-anchored filesystem tools.

The probe uses the current production Host, a verified ephemeral clean-v0
database, and one temporary workspace. Its report retains the actual prompts,
model replies, tool arguments, and tool results while scrubbing configured
model API-key values.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import (
    LocalPostgresConfig,
    LocalSettings,
    LocalSettingsStore,
)
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
    _find_connection,
)


SCHEMA_VERSION = "content-revision-line-edit-dogfood.v1"
DEFAULT_MODEL = "openai/gpt-5.6-luna"

SYSTEM_PROMPT = """
You are running a controlled Pulsara filesystem activation check. Follow each
human request literally and in the stated tool-call order. Use only read_file,
edit_file, and write_file. Never combine edits that are requested as separate
calls, never use terminal, and keep each final reply to one short sentence.
""".strip()

CHAIN_PROMPT = """
For chain.txt, call read_file once. Then call edit_file exactly twice and
sequentially: first replace original line 2 with BETA; after that succeeds, use
the returned new content_revision and changed window without rereading to
replace the then-current line 3 with GAMMA. Do not combine the edits.
""".strip()

STALE_READ_PROMPT = """
Call read_file once for stale.txt and make no edits. Keep its content_revision
available for the next human request.
""".strip()

STALE_RECOVERY_PROMPT = """
Using the stale.txt revision from the previous turn, first call edit_file
without rereading and try to replace line 2 with STAY. The call must encounter
the external change. After the mismatch result, call read_file once, then retry
edit_file with the new revision and change only line 2 to STAY.
""".strip()

CREATE_PROMPT = """
Call write_file once to create created.txt with exactly two lines, created and
editable, including a final newline. After it succeeds, call read_file once for
created.txt, then call edit_file once to replace line 2 with EDITED.
""".strip()


def _tool_trace(session) -> list[dict[str, object]]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            """
            SELECT request_entry.entry_sequence AS request_sequence,
                   result_entry.entry_sequence AS result_sequence,
                   b.tool_call_id, b.tool_name, b.tool_arguments,
                   r.result_state, result_entry.inline_content AS result_content
            FROM pulsara_v3.assistant_message_blocks AS b
            JOIN pulsara_v3.transcript_entries AS request_entry
              ON request_entry.session_id = b.session_id
             AND request_entry.id = b.assistant_entry_id
            JOIN pulsara_v3.tool_results AS r
              ON r.session_id = b.session_id
             AND r.tool_call_entry_id = b.assistant_entry_id
             AND r.tool_call_id = b.tool_call_id
            JOIN pulsara_v3.transcript_entries AS result_entry
              ON result_entry.session_id = r.session_id
             AND result_entry.id = r.result_entry_id
            WHERE b.session_id = %s AND b.block_kind = 'TOOL_CALL'
            ORDER BY request_entry.entry_sequence, b.block_ordinal
            """,
            (session.session_id,),
        ).fetchall()
    trace: list[dict[str, object]] = []
    for row in rows:
        raw_content = bytes(row["result_content"]).decode("utf-8")
        try:
            result: object = json.loads(raw_content)
        except json.JSONDecodeError:
            result = raw_content
        trace.append(
            {
                "request_sequence": row["request_sequence"],
                "result_sequence": row["result_sequence"],
                "tool_call_id": row["tool_call_id"],
                "tool_name": row["tool_name"],
                "arguments": row["tool_arguments"],
                "result_state": row["result_state"],
                "result": result,
            }
        )
    return trace


def _require_sequence(
    trace: list[dict[str, object]], expected: list[tuple[str, str]]
) -> None:
    actual = [(str(row["tool_name"]), str(row["result_state"])) for row in trace]
    if actual != expected:
        raise RuntimeError(f"unexpected tool sequence: {actual!r}")


def _assert_revision_chain(trace: list[dict[str, object]]) -> None:
    read_result = trace[0]["result"]
    first_arguments = trace[1]["arguments"]
    first_result = trace[1]["result"]
    second_arguments = trace[2]["arguments"]
    if not all(
        isinstance(item, dict)
        for item in (read_result, first_arguments, first_result, second_arguments)
    ):
        raise RuntimeError("revision chain lacks structured tool values")
    assert isinstance(read_result, dict)
    assert isinstance(first_arguments, dict)
    assert isinstance(first_result, dict)
    assert isinstance(second_arguments, dict)
    if first_arguments.get("base_revision") != read_result.get("content_revision"):
        raise RuntimeError("first edit did not use the read revision")
    if second_arguments.get("base_revision") != first_result.get("content_revision"):
        raise RuntimeError("second edit did not use the first edit revision")
    if not first_result.get("changed_windows"):
        raise RuntimeError("first edit did not return changed windows")


def _scrub(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<PULSARA_API_KEY>")
        return value
    if isinstance(value, dict):
        return {
            str(_scrub(key, secrets)): _scrub(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, secrets) for item in value]
    return value


async def _run(model_id: str) -> dict[str, object]:
    saved = LocalSettingsStore().read()
    connection = _find_connection(saved, model_id)
    database_name, _admin_root, ephemeral_admin, ephemeral_runtime = _create_database(
        saved
    )
    try:
        with TemporaryDirectory(
            prefix="pulsara-content-revision-dogfood-"
        ) as directory:
            workspace = Path(directory).resolve()
            (workspace / "chain.txt").write_text(
                "alpha\nbeta\ngamma\ndelta\n", encoding="utf-8"
            )
            stale_path = workspace / "stale.txt"
            stale_path.write_text("original\nstay\n", encoding="utf-8")
            runtime_settings: LocalSettings = replace(
                saved,
                postgres=LocalPostgresConfig(ephemeral_runtime, ephemeral_admin),
            )
            settings_store = _ReadOnlySettingsStore(runtime_settings)
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            runtime = ModelRuntime.production(  # type: ignore[arg-type]
                settings=settings_store,
                catalog=catalog,
            )
            binding = _binding(runtime, connection)
            core = KernelHostCore.production(model_runtime=runtime)
            session = None
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt=SYSTEM_PROMPT,
                )
                await session.update_model_call_binding(binding)
                replies: list[str] = []
                for ordinal, prompt in enumerate(
                    (CHAIN_PROMPT, STALE_READ_PROMPT), start=1
                ):
                    outcome = await session.run_turn(
                        prompt,
                        command_id=f"command:content-revision-dogfood:{ordinal}",
                        requested_permission_mode=PermissionMode.ACCEPT_EDITS,
                    )
                    replies.append(outcome.final_text)

                stale_path.write_text("external\nstay\n", encoding="utf-8")
                for ordinal, prompt in enumerate(
                    (STALE_RECOVERY_PROMPT, CREATE_PROMPT), start=3
                ):
                    outcome = await session.run_turn(
                        prompt,
                        command_id=f"command:content-revision-dogfood:{ordinal}",
                        requested_permission_mode=PermissionMode.ACCEPT_EDITS,
                    )
                    replies.append(outcome.final_text)

                trace = await asyncio.to_thread(_tool_trace, session)
                _require_sequence(
                    trace,
                    [
                        ("read_file", "SUCCESS"),
                        ("edit_file", "SUCCESS"),
                        ("edit_file", "SUCCESS"),
                        ("read_file", "SUCCESS"),
                        ("edit_file", "APPLICATION_ERROR"),
                        ("read_file", "SUCCESS"),
                        ("edit_file", "SUCCESS"),
                        ("write_file", "SUCCESS"),
                        ("read_file", "SUCCESS"),
                        ("edit_file", "SUCCESS"),
                    ],
                )
                _assert_revision_chain(trace[:3])
                stale_error = trace[4]["result"]
                if not isinstance(stale_error, dict) or stale_error.get("error") != (
                    "CONTENT_REVISION_MISMATCH"
                ):
                    raise RuntimeError("stale edit did not expose the typed mismatch")
                if trace[6]["arguments"].get("base_revision") != trace[5][  # type: ignore[union-attr]
                    "result"
                ].get("content_revision"):  # type: ignore[union-attr]
                    raise RuntimeError("stale recovery did not use the reread revision")
                if (workspace / "chain.txt").read_bytes() != (
                    b"alpha\nBETA\nGAMMA\ndelta\n"
                ):
                    raise RuntimeError("sequential edit chain produced wrong bytes")
                if stale_path.read_bytes() != b"external\nSTAY\n":
                    raise RuntimeError("stale recovery produced wrong bytes")
                if (workspace / "created.txt").read_bytes() != b"created\nEDITED\n":
                    raise RuntimeError("create-read-edit chain produced wrong bytes")
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "passed",
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "model_id": model_id,
                    "system_prompt": SYSTEM_PROMPT,
                    "turn_prompts": [
                        CHAIN_PROMPT,
                        STALE_READ_PROMPT,
                        STALE_RECOVERY_PROMPT,
                        CREATE_PROMPT,
                    ],
                    "model_replies": replies,
                    "tool_trace": trace,
                    "clean_v0_ephemeral_database": True,
                    "workspace_outcomes": {
                        "chain.txt": (workspace / "chain.txt").read_text(
                            encoding="utf-8"
                        ),
                        "stale.txt": stale_path.read_text(encoding="utf-8"),
                        "created.txt": (workspace / "created.txt").read_text(
                            encoding="utf-8"
                        ),
                    },
                }
            finally:
                if session is not None:
                    await core.close_session(
                        session.host_session_id, close_conversation=True
                    )
                await core.shutdown()
    finally:
        _drop_database(saved, database_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    saved = LocalSettingsStore().read()
    secrets = tuple(item.value for item in saved.model_api_keys)
    try:
        report = asyncio.run(_run(args.model))
    except BaseException as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "failure_type": type(exc).__name__,
            "failure_message": str(exc),
        }
    scrubbed = _scrub(report, secrets)
    encoded = json.dumps(scrubbed, ensure_ascii=False, indent=2, sort_keys=True)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("dogfood report retained a configured model API key")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
