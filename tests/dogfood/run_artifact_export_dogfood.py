"""Real-provider comparison of paged artifact reads and optional local export.

The baseline omits only artifact_export before a new Host tool surface is sealed.
The snapshot fixture uses a smaller, existing terminal retention bound. Neither
fixture mutates an installed provider prefix or the saved production settings.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.terminal_process.output import TerminalOutputOwner
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
    _find_connection,
)
from tests.dogfood.run_terminal_cognitive_dogfood import save


def fixtures():
    log = [
        f"{i:05d} INFO worker=background status=ok payload=" + "x" * 45
        for i in range(1400)
    ]
    log[713] = "00713 ERROR job=K7319 reason=unique-constraint-violation retry=false"
    rows = [
        {
            "id": i,
            "group": ["alpha", "beta", "gamma"][i % 3],
            "status": "failed" if i % 17 == 0 else "ok",
            "duration_ms": (i * 37) % 997,
            "note": "正常",
        }
        for i in range(800)
    ]
    totals = {
        g: sum(
            r["duration_ms"]
            for r in rows
            if r["group"] == g and r["status"] == "failed"
        )
        for g in ("alpha", "beta", "gamma")
    }
    left = [{"id": i, "owner": f"owner-{i % 11}"} for i in range(1000)]
    right = [{"id": i, "failed": i in (217, 641, 809)} for i in range(1000)]
    return {
        "small": "a" * 21000 + "\nEXACT_MARKER=R74X\n" + "b" * 21000,
        "log": "\n".join(log),
        "json": json.dumps({"items": rows}, ensure_ascii=False),
        "invalid_json": "\n".join(log) + "\nstatus={unfinished\n",
        "multi_left": json.dumps(left),
        "multi_right": json.dumps(right),
        "snapshot": "LOST_TARGET=present-before-retention\n" + "\n".join(log),
    }, {
        "json": totals,
        "log": log[713],
        "small": "R74X",
        "multi": {str(i): f"owner-{i % 11}" for i in (217, 641, 809)},
    }


async def run(args, report):
    saved = LocalSettingsStore().read()
    report["saved_home"] = str(require_pulsara_home())
    connection = _find_connection(saved, args.model)
    database, _, admin, runtime_dsn = _create_database(saved)
    old_home = os.environ.get("PULSARA_HOME")
    data, expected = fixtures()
    report["expected"] = expected
    try:
        with TemporaryDirectory(prefix="pulsara-artifact-export-") as directory:
            root = Path(directory).resolve()
            home = root / "home"
            home.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            records = []
            runtime = _RecordingModelRuntime(native, records, None)
            core = KernelHostCore.production(model_runtime=runtime)
            original_init = DirectKernelToolPort.__init__

            def baseline_init(port, *pos, **kw):
                original_init(port, *pos, **kw)
                port._tools.pop("artifact_export", None)

            try:
                for name in args.cases.split(","):
                    workspace = root / name
                    workspace.mkdir()
                    producer = workspace / "fixture_producer.py"
                    producer.write_text(
                        "import sys\ndata="
                        + repr(data)
                        + "\nsys.stdout.write(data[sys.argv[1]])\n",
                        encoding="utf-8",
                    )
                    command = (
                        f"{shlex.quote(sys.executable)} {shlex.quote(str(producer))}"
                    )
                    sources = (
                        ["multi_left", "multi_right"]
                        if name == "multi"
                        else [
                            "json"
                            if name in {"existing", "readonly", "no_terminal"}
                            else name
                        ]
                    )
                    start_record = len(records)
                    with ExitStack() as stack:
                        if args.phase == "baseline":
                            stack.enter_context(
                                patch.object(
                                    DirectKernelToolPort, "__init__", baseline_init
                                )
                            )
                        session = await core.open_session(
                            HostWorkspaceInput(
                                workspace_kind="project",
                                workspace_root=workspace,
                                trust_workspace_mcp_config=False,
                            ),
                            system_prompt="Use the tools to answer accurately and concisely. Treat fixture_producer.py as an opaque data source: execute each requested producer command once, never read its file, inspect its code, rerun it, or create a substitute generator. Use saved output for follow-up analysis. Do not inspect credentials or unrelated files.",
                        )
                    case = {
                        "case": name,
                        "turns": [],
                        "trace": [],
                        "snapshot_retention_bytes": 65536
                        if name == "snapshot"
                        else None,
                    }
                    report["cases"].append(case)
                    try:
                        await session.update_model_call_binding(
                            _binding(native, connection)
                        )
                        first = (
                            "Run these exact producer commands once each using terminal with max_output_chars=512 and yield_time_ms=1000. Then reply only 'saved'; do not analyze or reread yet: "
                            + "; ".join(command + " " + s for s in sources)
                        )
                        queries = {
                            "small": "From the saved output, report the exact value of EXACT_MARKER near character position 21000. Do not rerun the producer.",
                            "log": "From the saved output, find all ERROR lines and quote each verbatim with the line immediately before and after it. Do not rerun the producer.",
                            "json": "From the saved output, filter status=failed and sum duration_ms separately for each group. Return the three exact totals. Do not rerun the producer.",
                            "invalid_json": "From the saved output, report the ERROR line verbatim and the final status line. Decide how to read the body from its actual contents. Do not rerun the producer.",
                            "multi": "Join the two saved results by id and report id and owner for all failed=true rows. Do not rerun either producer.",
                            "snapshot": "Search the saved output for LOST_TARGET and report whether it occurs. State what range of the original output your conclusion covers. Do not rerun the producer.",
                            "existing": "Create a local copy of the saved output at occupied.txt, then sum duration_ms for failed rows by group. Preserve any existing file contents; if occupied.txt already exists, choose a different filename explicitly. Do not rerun the producer.",
                            "readonly": "From the saved output, report the group of the first failed row. Current permissions permit read-only tools. Do not rerun the producer or try to bypass permissions.",
                            "no_terminal": "From the saved output, report the group of the first failed row. For this follow-up use file/artifact tools only, without terminal. Do not rerun the producer.",
                        }
                        (workspace / "occupied.txt").write_text("KEEP EXISTING\n")
                        for index, prompt in enumerate((first, queries[name])):
                            start = monotonic()
                            with ExitStack() as stack:
                                if index == 0 and name == "snapshot":

                                    def retained(**kw):
                                        return TerminalOutputOwner(
                                            **kw, maximum_bytes=65536
                                        )

                                    stack.enter_context(
                                        patch(
                                            "pulsara_agent.terminal_process.manager.TerminalOutputOwner",
                                            retained,
                                        )
                                    )
                                outcome = await session.run_turn(
                                    PromptContent.text(prompt),
                                    command_id=f"command:artifact-dogfood:{name}:{index}",
                                    requested_permission_mode=PermissionMode.READ_ONLY
                                    if name == "readonly" and index == 1
                                    else PermissionMode.BYPASS_PERMISSIONS,
                                )
                            case["turns"].append(
                                {
                                    "prompt": prompt,
                                    "reply": outcome.final_text,
                                    "elapsed_seconds": monotonic() - start,
                                }
                            )
                            case["trace"] = await asyncio.to_thread(
                                _tool_trace, session
                            )
                            report["provider_calls"] = records
                            save(args.output, report, saved)
                        case["existing_file_preserved"] = (
                            workspace / "occupied.txt"
                        ).read_text() == "KEEP EXISTING\n"
                        case["tool_call_count"] = len(case["trace"])
                        case["provider_call_range"] = [start_record, len(records)]
                        case["tool_result_utf8_bytes"] = sum(
                            len(json.dumps(t["result"], ensure_ascii=False).encode())
                            for t in case["trace"]
                        )
                        print("Completed " + args.phase + ":" + name, flush=True)
                    finally:
                        case["trace"] = await asyncio.to_thread(_tool_trace, session)
                        await core.close_session(session.host_session_id)
                        save(args.output, report, saved)
                report["status"] = "completed"
            finally:
                await core.shutdown()
    finally:
        if old_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = old_home
        _drop_database(saved, database)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="openai/gpt-6-luna")
    parser.add_argument("--phase", choices=["baseline", "candidate"], required=True)
    parser.add_argument(
        "--cases",
        default="small,log,json,invalid_json,multi,snapshot,existing,readonly,no_terminal",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    saved = LocalSettingsStore().read()
    report = {
        "phase": args.phase,
        "model": args.model,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": [],
        "provider_calls": [],
        "baseline_method": "omit export before sealing new tool surface; no installed prefix mutation",
        "usage_note": "input_tokens are reported total input, not a billed-token claim; this recorder does not separate cache reads",
    }
    try:
        asyncio.run(run(args, report))
    except BaseException as exc:
        report.update(
            status="failed", failure_type=type(exc).__name__, failure_message=str(exc)
        )
    save(args.output, report, saved)
    print(report["status"], flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
