"""Real-provider terminal observations using saved, read-only production settings.

Each run uses an isolated Pulsara home/workspace and a verified disposable local
clean-v0 database. Evidence retains prompts, replies and exact tool calls/results.
Only configured credential values are scrubbed. No product call-count limit is
introduced: the finite scenarios below describe this experiment, not runtime policy.
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
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from run_content_revision_line_edit_dogfood import _tool_trace
from run_pr03_user_control_dogfood import _rows
from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _binding,
    _create_database,
    _drop_database,
    _find_connection,
    _scrub,
)


async def run(args, report):
    saved = LocalSettingsStore().read()
    report["saved_home"] = str(require_pulsara_home())
    connection = _find_connection(saved, args.model)
    database, _, admin, runtime_dsn = _create_database(saved)
    original_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-terminal-cognitive-") as directory:
            root = Path(directory).resolve()
            workspace, home = root / "workspace", root / "home"
            workspace.mkdir()
            home.mkdir()
            for name in ("alpha", "beta"):
                (workspace / name).mkdir()
                (workspace / name / "marker.txt").write_text(name + "\n")
            (workspace / "repl.py").write_text(
                "import sys,time\nprint('READY',flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.strip()=='quit': break\n"
                " time.sleep(.2)\n print('ANSWER:'+line.strip(),flush=True)\n"
            )
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            runtime = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(model_runtime=runtime)
            session = None
            python = shlex.quote(sys.executable)
            repl = f"{python} -u {shlex.quote(str(workspace / 'repl.py'))}"
            prompts = {
                "directories": "Use terminal to read marker.txt in alpha, then beta, then alpha again, as three separate commands. Then run one command that changes to beta and exports COGNITIVE_FLAG=inside and prints both pwd and that variable. In a final separate terminal call without workdir or terminal session selection, print pwd and the value of COGNITIVE_FLAG. Report observed directories and whether the export persisted.",
                "interaction": f"Start {repl!r} with yield_time_ms=100. Send hello without a newline using write, then submit world to finish the line. Obtain ANSWER:helloworld, then submit quit. Use the advertised defaults for write/submit, except use yield_time_ms=0 for write if that option exists. Report the exact answer and which calls were needed to observe it.",
                "pty": f"Start {repl!r} with tty=true and yield_time_ms=100. Submit delayed and obtain ANSWER:delayed, not merely the echoed input. Then submit quit. Use the advertised submit defaults. Report the answer.",
                "long": 'Start the exact command "sleep 1.2; printf LONG_DONE" with yield_time_ms=0. Follow the returned process until it finishes and report its output and exit code.',
                "truncation": f"Run {python} -c \"print('A'*9000); print('MIDDLE_7319'); print('B'*9000)\" with max_output_chars=512. Recover the exact MIDDLE marker from the omitted output using artifact_read; do not rerun the command.",
                "monitor_race": 'Run "printf RACE_DONE" with yield_time_ms=0. Attempt to register a completion monitor for that exact process. If already finished, read its final state via terminal_process. Report the outcome without restarting it.',
                "monitor": 'Start "sleep 2; printf MONITOR_DONE" with yield_time_ms=0 and register its completion monitor. End this turn after registration. When resumed by its completion, report MONITOR_DONE.',
                "capacity": 'Start eight separate "sleep 120" terminal commands with yield_time_ms=0. Then attempt one additional "printf SHOULD_NOT_START" command. Report whether it was admitted and the exact failure reason if blocked. Use terminal_process.list, then kill the eight sleep processes you started, using their exact IDs. Do not start replacements.',
                "submit_then_wait": f"Start {repl!r} with yield_time_ms=100. Submit compare with yield_time_ms=0, then call wait with timeout_seconds=1. Read ANSWER:compare, then submit quit with yield_time_ms=1000. Perform this sequence exactly for the timing comparison.",
                "submit_window": f"Start {repl!r} with yield_time_ms=100. Submit compare with yield_time_ms=1000. Read ANSWER:compare, then submit quit with yield_time_ms=1000. Perform this sequence exactly for the timing comparison.",
                "monitor_wake": 'Start "sleep 30; printf MONITOR_WAKE_DONE" with yield_time_ms=0. Register its completion monitor, then finish the turn immediately. When the monitor resumes the conversation, report the completion output and do not start another command. If registration says PROCESS_ALREADY_TERMINAL, poll that exact process and report the final output.',
                "monitor_long_wake": 'Start "sleep 120; printf MONITOR_LONG_DONE" with yield_time_ms=0. Register a completion-only monitor for its exact process_id, then finish this turn immediately. Do not poll or send another command. When the monitor resumes the conversation, report MONITOR_LONG_DONE and the exit code.',
            }
            selected = args.cases.split(",") if args.cases else list(prompts)
            report["prompts"] = {name: prompts[name] for name in selected}
            try:
                session = await core.open_session(
                    HostWorkspaceInput(
                        workspace_kind="project",
                        workspace_root=workspace,
                        trust_workspace_mcp_config=False,
                    ),
                    system_prompt="Carry out the requested terminal experiment exactly. Use only terminal, terminal_process, terminal_monitor and artifact_read. Keep final answers brief. Never inspect credentials or environment values other than COGNITIVE_FLAG.",
                )
                await session.update_model_call_binding(_binding(runtime, connection))
                print("Provider owner ready", flush=True)
                for name in selected:
                    trace_before = len(await asyncio.to_thread(_tool_trace, session))
                    start = monotonic()
                    outcome = await session.run_turn(
                        PromptContent.text(prompts[name]),
                        command_id=f"command:terminal-cognitive:{name}",
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    case = {
                        "case": name,
                        "elapsed_seconds": monotonic() - start,
                        "reply": outcome.final_text,
                    }
                    report["cases"].append(case)
                    if name in {"monitor_wake", "monitor_long_wake"}:
                        marker = (
                            "MONITOR_LONG_DONE"
                            if name == "monitor_long_wake"
                            else "MONITOR_WAKE_DONE"
                        )
                        if name == "monitor_long_wake":
                            initial_trace = (
                                await asyncio.to_thread(_tool_trace, session)
                            )[trace_before:]
                            starts = [
                                item
                                for item in initial_trace
                                if item["tool_name"] == "terminal"
                            ]
                            monitors = [
                                item
                                for item in initial_trace
                                if item["tool_name"] == "terminal_monitor"
                            ]
                            if (
                                len(starts) != 1
                                or starts[0]["result"].get("status") != "running"
                                or len(monitors) != 1
                                or monitors[0]["result"].get("status") != "REGISTERED"
                                or monitors[0]["result"].get("process_id")
                                != starts[0]["result"].get("process_id")
                                or len(initial_trace) != 2
                                or case["elapsed_seconds"] >= 120
                            ):
                                raise RuntimeError(
                                    "Long monitor case did not register and end its initial turn before process completion"
                                )
                            case["registered_process_id"] = starts[0]["result"]["process_id"]
                            case["registered_monitor_id"] = monitors[0]["result"]["monitor_id"]
                            case["manual_followup_turns"] = 0
                        # Experiment deadline only; no runtime monitor/lifetime cap.
                        deadline = monotonic() + (
                            210 if name == "monitor_long_wake" else 90
                        )
                        while True:
                            rows = await asyncio.to_thread(
                                _rows,
                                session,
                                "SELECT e.entry_sequence, e.entry_kind, "
                                "convert_from(e.inline_content,'UTF8') AS content, "
                                "(SELECT string_agg(convert_from(b.inline_content,'UTF8'),'' ORDER BY b.block_ordinal) "
                                "FROM pulsara_v3.assistant_message_blocks b "
                                "WHERE b.session_id=e.session_id AND b.assistant_entry_id=e.id "
                                "AND b.block_kind='TEXT') AS assistant_text, "
                                "t.status AS turn_status, t.final_entry_id "
                                "FROM pulsara_v3.transcript_entries e "
                                "JOIN pulsara_v3.turns t ON t.id=e.turn_id AND t.session_id=e.session_id "
                                "WHERE e.session_id=%s AND e.turn_id IN "
                                "(SELECT turn_id FROM pulsara_v3.transcript_entries "
                                "WHERE session_id=%s AND entry_kind='TERMINAL_OBSERVATION') "
                                "ORDER BY e.entry_sequence",
                                (session.session_id, session.session_id),
                            )
                            if rows and any(row["final_entry_id"] for row in rows):
                                case["wake_transcript"] = rows
                                case["wake_elapsed_seconds"] = monotonic() - start
                                if not any(
                                    marker in (row["assistant_text"] or "")
                                    for row in rows
                                ):
                                    raise RuntimeError(
                                        "Monitor wake reply did not report the completion output"
                                    )
                                if name == "monitor_long_wake":
                                    observations = [
                                        json.loads(row["content"])
                                        for row in rows
                                        if row["entry_kind"] == "TERMINAL_OBSERVATION"
                                    ]
                                    if not any(
                                        item.get("observation_kind") == "COMPLETION"
                                        and item.get("process_id")
                                        == case["registered_process_id"]
                                        and item.get("monitor_id")
                                        == case["registered_monitor_id"]
                                        and item.get("output") == marker
                                        and item.get("exit_code") == 0
                                        for item in observations
                                    ):
                                        raise RuntimeError(
                                            "Long monitor completion did not match the registered process"
                                        )
                                    if case["wake_elapsed_seconds"] < 120:
                                        raise RuntimeError(
                                            "Long monitor wake happened before the intended sleep interval"
                                        )
                                break
                            if monotonic() >= deadline:
                                case["wake_transcript"] = rows
                                raise TimeoutError(
                                    "Monitor did not complete an autonomous model turn"
                                )
                            await asyncio.sleep(0.1)
                    report["trace"] = await asyncio.to_thread(_tool_trace, session)
                    case["tool_call_count"] = len(report["trace"]) - trace_before
                    if name == "monitor_long_wake" and case["tool_call_count"] != 2:
                        raise RuntimeError(
                            "Long monitor case used an extra tool call after registration"
                        )
                    save(args.output, report, saved)
                    print("Completed " + name, flush=True)
                report["status"] = "completed"
            finally:
                try:
                    if session is not None:
                        report["trace"] = await asyncio.to_thread(_tool_trace, session)
                        await core.close_session(session.host_session_id)
                finally:
                    await core.shutdown()
    finally:
        if original_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_home
        _drop_database(saved, database)


def save(path, report, saved):
    secrets = tuple(item.value for item in saved.model_api_keys)
    encoded = json.dumps(_scrub(report, secrets), ensure_ascii=False, indent=2)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("Report retained a configured credential")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="openai/gpt-6-luna")
    parser.add_argument("--cases")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    saved = LocalSettingsStore().read()
    report = {
        "phase": args.phase,
        "model": args.model,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": [],
        "trace": [],
    }
    try:
        asyncio.run(run(args, report))
    except BaseException as exc:
        report.update(
            status="failed", failure_type=type(exc).__name__, failure_message=str(exc)
        )
    save(args.output, report, saved)
    print(report["status"])
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
