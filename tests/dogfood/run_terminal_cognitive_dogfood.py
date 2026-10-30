"""Real-provider terminal observations using saved, read-only production settings.

Each run uses an isolated Pulsara home/workspace and a verified disposable local
clean-v0 database. Evidence retains prompts, replies and exact tool calls/results.
Only configured credential values are scrubbed. No product call-count limit is
introduced: the finite scenarios below describe this experiment, not runtime policy.
"""

from __future__ import annotations
from pulsara_agent.identity import derive_id

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
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
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
                "directories": "Use exec_process to read marker.txt in alpha, then beta, then alpha again, as three separate commands. Then run one command that changes to beta and exports COGNITIVE_FLAG=inside and prints both pwd and that variable. In a final separate terminal call without workdir or terminal session selection, print pwd and the value of COGNITIVE_FLAG. Report observed directories and whether the export persisted.",
                "interaction": f"Start {repl!r} with yield_time_ms=100. Send hello without a newline using write, then submit world to finish the line. Obtain ANSWER:helloworld, then submit quit. Use the advertised defaults for write/submit, except use yield_time_ms=0 for write if that option exists. Report the exact answer and which calls were needed to observe it.",
                "pty": f"Start {repl!r} with tty=true and yield_time_ms=100. Submit delayed and obtain ANSWER:delayed, not merely the echoed input. Then submit quit. Use the advertised submit defaults. Report the answer.",
                "long": 'Start the exact command "sleep 1.2; printf LONG_DONE" with yield_time_ms=0. Follow the returned process until it finishes and report its output and exit code.',
                "truncation": f"Run {python} -c \"print('A'*9000); print('MIDDLE_7319'); print('B'*9000)\" with max_output_chars=512. Recover the exact MIDDLE marker from the omitted output using read_artifact; do not rerun the command.",
                "capacity": 'Start eight separate "sleep 120" terminal commands with yield_time_ms=0. Then attempt one additional "printf SHOULD_NOT_START" command. Report whether it was admitted and the exact failure reason if blocked. Use query_processes, then kill the eight sleep processes you started, using their exact IDs. Do not start replacements.',
                "submit_then_wait": f"Start {repl!r} with yield_time_ms=100. Submit compare with yield_time_ms=0, then call wait with timeout_seconds=1. Read ANSWER:compare, then submit quit with yield_time_ms=1000. Perform this sequence exactly for the timing comparison.",
                "submit_window": f"Start {repl!r} with yield_time_ms=100. Submit compare with yield_time_ms=1000. Read ANSWER:compare, then submit quit with yield_time_ms=1000. Perform this sequence exactly for the timing comparison.",
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
                    system_prompt="Carry out the requested terminal experiment exactly. Use only exec_process, query_processes, manage_process and read_artifact. Keep final answers brief. Never inspect credentials or environment values other than COGNITIVE_FLAG.",
                )
                await session.update_model_call_binding(_binding(runtime, connection))
                print("Provider owner ready", flush=True)
                for name in selected:
                    trace_before = len(await asyncio.to_thread(_tool_trace, session))
                    start = monotonic()
                    outcome = await session.run_turn(
                        PromptContent.text(prompts[name]),
                        command_id=derive_id("command", "test-fixture", f"command:terminal-cognitive:{name}"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                    )
                    case = {
                        "case": name,
                        "elapsed_seconds": monotonic() - start,
                        "reply": outcome.final_text,
                    }
                    report["cases"].append(case)
                    report["trace"] = await asyncio.to_thread(_tool_trace, session)
                    case["tool_call_count"] = len(report["trace"]) - trace_before
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
