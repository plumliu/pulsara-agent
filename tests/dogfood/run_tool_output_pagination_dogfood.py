"""Real-provider activation of shared result reads, source copy and source pages.

Saved settings are read before switching home; credentials are never exported.
The verified desktop endpoint only creates/drops this run's isolated database.
All model replies and compact/native tool results are retained as evidence.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sys
import traceback
from tempfile import TemporaryDirectory
from time import monotonic

from psycopg.rows import dict_row

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
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
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _find_connection,
)
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)


def _trace(session):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            """
            SELECT e.entry_sequence, i.source_kind,
                   COALESCE(i.tool_name,b.tool_name) AS tool_name,
                   COALESCE(i.original_arguments,b.tool_arguments) AS arguments,
                   r.result_state,r.result_entry_id,r.output_omitted,
                   r.output_body_size,r.output_source_coverage,r.result_facts,
                   e.inline_content
              FROM pulsara_v3.tool_results r
              JOIN pulsara_v3.tool_invocations i ON i.session_id=r.session_id AND i.id=r.invocation_id
              JOIN pulsara_v3.transcript_entries e ON e.session_id=r.session_id AND e.id=r.result_entry_id
              LEFT JOIN pulsara_v3.assistant_message_blocks b ON b.session_id=i.session_id
               AND b.assistant_entry_id=i.assistant_entry_id AND b.tool_call_id=i.tool_call_id
             WHERE r.session_id=%s ORDER BY e.entry_sequence
        """,
            (session.session_id,),
        ).fetchall()
        outputs = connection.execute(
            """
            SELECT id,entry_sequence,inline_content FROM pulsara_v3.transcript_entries
             WHERE session_id=%s AND input_origin='CELL_OUTPUT' ORDER BY entry_sequence
        """,
            (session.session_id,),
        ).fetchall()
    trace = []
    for row in rows:
        value = dict(row)
        raw = value.pop("inline_content")
        text = bytes(raw).decode() if raw is not None else ""
        try:
            value["result"] = json.loads(text)
        except json.JSONDecodeError:
            value["result"] = text
        trace.append(value)
    cells = []
    for row in outputs:
        cells.append(
            {
                "entry_id": row["id"],
                "sequence": row["entry_sequence"],
                "body": json.loads(bytes(row["inline_content"]).decode()),
            }
        )
    return trace, cells


def _prefix_checks(records, sections):
    checked = 0
    for begin, end in sections:
        calls = [
            item
            for item in records[begin:end]
            if item["purpose"] != "context_compaction_summary"
        ]
        for previous, following in zip(calls, calls[1:]):
            first, second = previous["provider_input"], following["provider_input"]
            if "messages" in first:
                assert second.get("tools") == first.get("tools"), (
                    "provider tools changed within an epoch"
                )
                messages = first["messages"]
                assert second["messages"][: len(messages)] == messages, (
                    "provider messages lost prefix continuity"
                )
            else:
                raise RuntimeError(
                    "This saved route's provider projection needs an explicit prefix inspector"
                )
            checked += 1
    return checked


def _fixture(workspace):
    for name, value in zip(
        ("a", "b", "c"), ("SMALL_ALPHA", "SMALL_BETA", "SMALL_GAMMA")
    ):
        (workspace / f"small_{name}.txt").write_text(value + "\n")
    (workspace / "paged.txt").write_text(
        "\n".join(f"PAGE_MARKER_{index:02d} " + '中\\"' * 300 for index in range(12))
    )
    for name in ("a", "b", "c"):
        (workspace / f"late_{name}.txt").write_text(
            "前" * 900 + f" LATE_HIT_{name.upper()} " + "尾" * 100
        )
    producer = workspace / "producer.py"
    producer.write_text("""import pathlib,sys
kind=sys.argv[1]
counter=pathlib.Path('producer_'+kind+'_runs.txt')
counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
if kind=='bridge':
 print('BRIDGE_START')
 for i in range(60000): print(f'{i:06d} bridge payload='+'z'*70)
 print('BRIDGE_END')
else:
 for i in range(1500):
  print('MIDDLE_MARKER=K7319 ERROR unique-constraint-violation' if i==731 else f'{i:05d} worker=background payload='+'x'*75)
""")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(producer))}"


async def run(args, report, saved, save):
    connection = _find_connection(saved, args.model)
    # Desktop configuration is normally supplied by Electron, not saved in the
    # settings document. Use its production validator before the isolated test
    # database owner touches that endpoint; never reset the pulsara database.
    postgres = LocalPostgresConfig(os.environ["PULSARA_POSTGRES_DSN"], admin_root_dsn())
    validate_managed_postgres(postgres)
    report["database_root"] = {
        "runtime_dsn": postgres.runtime_dsn,
        "admin_dsn": postgres.admin_dsn,
    }
    database = create_migrated_postgres_test_database()
    report["isolated_database"] = database.database_name
    old_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-output-pages-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            command = _fixture(workspace)
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(
                    saved,
                    postgres=LocalPostgresConfig(
                        database.runtime_dsn, database.admin_dsn
                    ),
                )
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            records = report["provider_calls"]
            runtime = _RecordingModelRuntime(native, records)
            core = KernelHostCore.production(model_runtime=runtime)
            session = None
            sections = []
            section_start = 0
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt="Carry out these Pulsara activation experiments literally using the advertised production tools. "
                    "When asked to continue, copy the complete returned next/read call instead of calculating offsets. "
                    "producer.py is opaque: run each requested producer command exactly once; never read its source, "
                    "generate substitute data, or rerun it to recover omitted output. Use only the requested tools for each turn. "
                    "Never inspect credentials, environment, settings, or unrelated files. Keep final answers concise.",
                )
                await session.attach_controller(random_id("attachment"))
                await session.update_model_call_binding(_binding(native, connection))
                # The compaction experiment explicitly adopts its successor;
                # automatic compaction cannot create unrecorded comparison cuts.
                session._compaction.policy = ResolvedCompactionPolicy(
                    automatic_enabled=False, minimum_reclaim_tokens=1
                )
                prompts = [
                    (
                        "small_batch",
                        "Call read_file for small_a.txt, small_b.txt and small_c.txt as three native tool calls in the same response, before observing any result. Do not use exec_cell. Then return the three exact values.",
                    ),
                    (
                        "source_next",
                        "Using only native read_file, read paged.txt with limit=2. Copy and call that result's complete next exactly once. Do not continue further. Report the PAGE_MARKER values in those two pages.",
                    ),
                    (
                        "late_search",
                        "Using only native search_content, search pattern LATE_HIT with file_glob='late_*.txt' and limit=1. Copy and call the returned next exactly once. Quote the two hit excerpts, which must include the real matches beyond character 500.",
                    ),
                    (
                        "terminal_once",
                        f"Use native exec_process to run exactly `{command} log` once, max_output_chars=512 and yield_time_ms=1000. If still running, wait for it. Reply only saved; do not recover its omitted text yet.",
                    ),
                    (
                        "entry_read_copy",
                        "Use the exact read call returned for that saved terminal result once with read_file. Then write_file target PATH copied.log with source set to the ORIGINAL saved SESSION_ENTRY Target, not the new read result; do not use content. Finally use one terminal rg command on copied.log to find MIDDLE_MARKER, and quote it. Do not rerun the producer.",
                    ),
                    (
                        "code_mode_source_next",
                        "Use exec_cell to await tools.find_files({glob:'late_*.txt',limit:1}); then follow exactly one returned next using its tool and arguments. console.log only the two file paths you selected. Do not print internal full results or use native find_files.",
                    ),
                ]
                if args.search_smoke:
                    prompts = [item for item in prompts if item[0] == "late_search"]
                elif args.bridge:
                    prompts.append(
                        (
                            "code_mode_bridge",
                            f"Use exec_cell to await tools.exec_process for exactly `{command} bridge` once, with max_output_chars=512 and yield_time_ms=1000. If value_boundary is thrown, catch it and console.log only its code, state and read. Preserve the accepted command's success; do not rerun it. If the command is still running, wait for its completion without starting it again. Then use the exact read_file call for the ORIGINAL saved result once and report whether its beginning contains BRIDGE_START.",
                        )
                    )
                if not args.search_smoke:
                    prompts.append(
                        (
                            "pending_before_compaction",
                            "Using only native read_file, read paged.txt with limit=1 once. Do not continue yet. Keep that exact next call for my following request. Reply pending.",
                        )
                    )
                for name, prompt in prompts:
                    before = len((await asyncio.to_thread(_trace, session))[0])
                    start = monotonic()
                    print(f"Starting {name}", flush=True)
                    outcome = await session.run_turn(
                        PromptContent.text(prompt),
                        command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    report["turns"].append(
                        {
                            "name": name,
                            "prompt": prompt,
                            "reply": outcome.final_text,
                            "elapsed_seconds": monotonic() - start,
                            "trace_start": before,
                        }
                    )
                    report["trace"], report["cell_outputs"] = await asyncio.to_thread(
                        _trace, session
                    )
                    report["turns"][-1]["trace_end"] = len(report["trace"])
                    save()
                    print(f"Completed {name}", flush=True)
                sections.append((section_start, len(records)))
                report["prefix_pairs_checked"] = _prefix_checks(records, sections)
                if args.search_smoke:
                    trace = report["trace"]
                    report["checks"] = {
                        "search_next_copied": len(trace) == 2
                        and trace[0]["result"]["next"]
                        == {
                            "tool": trace[1]["tool_name"],
                            "arguments": trace[1]["arguments"],
                        },
                        "late_real_matches": len(trace) == 2
                        and all(
                            len(row["result"]["matches"]) == 1
                            and row["result"]["matches"][0]["match_char_offset"] > 500
                            and "LATE_HIT" in row["result"]["matches"][0]["excerpt"]
                            for row in trace
                        ),
                        "pages_complete": all(
                            row["result_state"] == "SUCCESS"
                            and not row["output_omitted"]
                            for row in trace
                        ),
                    }
                    assert all(report["checks"].values()), report["checks"]
                    report["status"] = "completed"
                    save()
                    return
                if args.compaction:
                    print("Starting explicit compaction", flush=True)
                    compacted = await session.compact_context(
                        command_id=random_id("command")
                    )
                    report["compaction"] = asdict(compacted)
                    section_start = len(records)
                    save()
                prompt = "Continue the exact pending read_file.next from my immediately preceding request exactly once. Do not start a new read or calculate offsets. Return the PAGE_MARKER you obtain."
                start = monotonic()
                outcome = await session.run_turn(
                    PromptContent.text(prompt),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                report["turns"].append(
                    {
                        "name": "continue_after_boundary",
                        "prompt": prompt,
                        "reply": outcome.final_text,
                        "elapsed_seconds": monotonic() - start,
                    }
                )
                if args.compaction:
                    sections.append((section_start, len(records)))
                else:
                    sections[-1] = (0, len(records))
                report["prefix_pairs_checked"] = _prefix_checks(records, sections)
                report["prefix_sections"] = sections
                report["trace"], report["cell_outputs"] = await asyncio.to_thread(
                    _trace, session
                )
                trace = report["trace"]
                copied = workspace / "copied.log"
                by_turn = {
                    turn["name"]: trace[turn["trace_start"] : turn["trace_end"]]
                    for turn in report["turns"]
                    if "trace_start" in turn
                }
                copied_next = {}
                for name in ("source_next", "late_search"):
                    calls = by_turn[name]
                    copied_next[name] = len(calls) == 2 and calls[0]["result"].get(
                        "next"
                    ) == {
                        "tool": calls[1]["tool_name"],
                        "arguments": calls[1]["arguments"],
                    }
                source_copies = [
                    row
                    for row in trace
                    if row["tool_name"] == "write_file" and "source" in row["arguments"]
                ]
                body_equal = False
                if source_copies and copied.exists():
                    body = PostgresToolOutputReadPort(
                        session.repository.connection_provider,
                        session_id=session.session_id,
                        workspace_id=session.workspace.workspace_key,
                    ).read_body(source_copies[-1]["arguments"]["source"])
                    body_equal = copied.read_bytes() == body.content
                pending = by_turn["pending_before_compaction"][-1]["result"].get("next")
                following = trace[report["turns"][-2]["trace_end"] :]
                report["checks"] = {
                    "copied_source_next_calls": copied_next,
                    "copy_bytes_equal_original_source": body_equal,
                    "pending_next_copied_after_boundary": bool(following)
                    and pending
                    == {
                        "tool": following[0]["tool_name"],
                        "arguments": following[0]["arguments"],
                    },
                    "log_producer_runs": (
                        workspace / "producer_log_runs.txt"
                    ).read_text()
                    if (workspace / "producer_log_runs.txt").exists()
                    else None,
                    "copy_contains_middle_marker": copied.exists()
                    and b"MIDDLE_MARKER=K7319 ERROR unique-constraint-violation"
                    in copied.read_bytes(),
                    "copy_bytes": copied.stat().st_size if copied.exists() else None,
                    "source_copy_calls": sum(
                        row["tool_name"] == "write_file"
                        and "source" in row["arguments"]
                        for row in trace
                    ),
                    "entry_read_calls": sum(
                        row["tool_name"] == "read_file"
                        and row["arguments"].get("target", {}).get("kind")
                        == "SESSION_ENTRY"
                        for row in trace
                    ),
                    "code_mode_calls": sum(
                        row["source_kind"] == "CODE_MODE_CALL" for row in trace
                    ),
                    "bridge_producer_runs": (
                        workspace / "producer_bridge_runs.txt"
                    ).read_text()
                    if (workspace / "producer_bridge_runs.txt").exists()
                    else None,
                    "bridge_boundary_observed": "value_boundary"
                    in json.dumps(report["cell_outputs"]),
                    "largest_model_tool_batch": max(
                        (
                            sum(
                                block["kind"] == "tool_call"
                                for block in row["normalized_blocks"]
                            )
                            for row in records
                        ),
                        default=0,
                    ),
                }
                assert report["checks"]["log_producer_runs"] == "1"
                assert report["checks"]["copy_contains_middle_marker"]
                assert report["checks"]["copy_bytes_equal_original_source"]
                assert all(report["checks"]["copied_source_next_calls"].values())
                assert report["checks"]["pending_next_copied_after_boundary"]
                assert (
                    report["checks"]["source_copy_calls"] >= 1
                    and report["checks"]["entry_read_calls"] >= 1
                )
                assert report["checks"]["code_mode_calls"] >= 2
                assert report["checks"]["largest_model_tool_batch"] >= 3
                if args.bridge:
                    assert report["checks"]["bridge_producer_runs"] == "1"
                    assert report["checks"]["bridge_boundary_observed"]
                report["status"] = "completed"
                save()
            finally:
                if session is not None:
                    report["trace"], report["cell_outputs"] = await asyncio.to_thread(
                        _trace, session
                    )
                    await core.close_session(session.host_session_id)
                await core.shutdown()
    finally:
        if old_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = old_home
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="openai/gpt-6-luna")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bridge", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--search-smoke", action="store_true")
    parser.add_argument(
        "--compaction", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args()
    home = require_pulsara_home()
    saved = LocalSettingsStore(home / "local-settings.yaml").read()
    scrub = secret_scrubber(saved)
    report = {
        "status": "running",
        "model": args.model,
        "saved_home": str(home),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "turns": [],
        "provider_calls": [],
        "trace": [],
        "cell_outputs": [],
    }

    def save():
        content = json.dumps(
            scrub.scrub_json(report), ensure_ascii=False, indent=2, default=str
        )
        if scrub.contains(content):
            raise RuntimeError("Evidence still contains a configured credential")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n")

    try:
        asyncio.run(run(args, report, saved, save))
    except BaseException as error:
        report.update(
            status="failed",
            failure_type=type(error).__name__,
            failure_message=str(error),
            failure_traceback=traceback.format_exc(),
        )
    save()
    print(report["status"], flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
