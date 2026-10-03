"""Real-owner probes for missing Skills and bounded workspace instructions.

Finite diagnostic scenarios; no product lifetime limit or custom SYSTEM.
"""

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _create_database, _drop_database,
)
from tests.dogfood.run_prompt_cognitive_dogfood import (
    _WireRecordingRuntime, _read_calls, _run_turn, _saved_connection,
)


async def run(output: Path, cases: str = "sources") -> bool:
    saved = LocalSettingsStore().read()
    scrub = secret_scrubber(saved)
    connection = _saved_connection(saved)
    report = {"saved_home": str(require_pulsara_home()), "cases": [], "model_calls": []}
    database, _, admin, runtime_dsn = _create_database(saved)
    previous_home = os.environ.get("PULSARA_HOME")
    try:
        with TemporaryDirectory(prefix="pulsara-prompt-supplementary-") as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(replace(
                saved, postgres=LocalPostgresConfig(runtime_dsn, admin),
            ))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            delegate = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _WireRecordingRuntime(delegate, report["model_calls"], None)
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                if cases in {"sources", "all"}:
                    workspace = root / "missing-root"
                    (workspace / "app").mkdir(parents=True)
                    (workspace / "unrelated").mkdir()
                    (workspace / "unrelated" / "AGENTS.md").write_text(
                        "Unrelated instructions. Do not apply to app/.\n"
                    )
                    (workspace / "app" / "AGENTS.md").write_text(
                        "For files under app/, result.txt must contain BOUNDED_PATH_OK "
                        "as its only line; a final newline is optional.\n"
                    )
                    row, session, _ = await _run_turn(
                        core=core, runtime=runtime, connection=connection,
                        workspace=workspace, calls=report["model_calls"],
                        case_name="missing_root_does_not_scan_unrelated_subtrees",
                        prompt="Create app/result.txt with the only line BOUNDED_PATH_OK, "
                        "following applicable project instructions.",
                    )
                    report["cases"].append(row)
                    target = workspace / "app" / "result.txt"
                    reads = _read_calls(row["tool_trace"])
                    row["observed_read_paths"] = reads
                    row["target_contents"] = target.read_text() if target.exists() else None
                    broad_search = [
                        t for t in row["tool_trace"]
                        if t["tool_name"] in {"find_files", "search_content"}
                        and t.get("arguments", {}).get("path", ".") in {".", str(workspace)}
                        and "AGENTS" in json.dumps(t.get("arguments", {}))
                    ]
                    row["broad_instruction_searches"] = broad_search
                    row["decision_passed"] = (
                        row["turn_error"] is None
                        and row["target_contents"] is not None
                        and row["target_contents"].splitlines() == ["BOUNDED_PATH_OK"]
                        and not broad_search
                        and not any("unrelated" in p for p in reads)
                        and any(Path(p).name == "AGENTS.md" and "app" in p for p in reads)
                    )
                    await core.close_session(session.host_session_id)
                    print(row["case"], row["decision_passed"], flush=True)

                    workspace = root / "missing-skill"
                    workspace.mkdir()
                    name = "missing-skill-for-cognitive-probe"
                    row, session, _ = await _run_turn(
                        core=core, runtime=runtime, connection=connection,
                        workspace=workspace, calls=report["model_calls"],
                        case_name="missing_named_skill_recovery",
                        active_skills=frozenset({name}),
                        prompt=f"Use ${name} for this task if available. I also need "
                        "the sum of 42 and 17; handle that independent part even if "
                        "the Skill is unavailable. Do not install a replacement.",
                    )
                    report["cases"].append(row)
                    trace = row["tool_trace"]
                    queries = [t for t in trace if t["tool_name"] == "list_capabilities"
                               and t.get("arguments", {}).get("kind") == "SKILL"]
                    guessed_reads = [p for p in _read_calls(trace) if name in p]
                    row["skill_queries"] = queries
                    row["guessed_skill_reads"] = guessed_reads
                    row["decision_passed"] = (
                        row["turn_error"] is None and bool(queries)
                        and not guessed_reads
                        and not any(t["tool_name"] == "capability_management" for t in trace)
                        and "59" in str(row["reply"])
                    )
                    await core.close_session(session.host_session_id)
                    print(row["case"], row["decision_passed"], flush=True)
                if cases in {"interactions", "all"}:
                    from tests.dogfood.prompt_cognitive_interactions import probe_interactions
                    await probe_interactions(
                        core=core, runtime=runtime, connection=connection, root=root,
                        calls=report["model_calls"], rows=report["cases"],
                    )
            finally:
                await core.shutdown()
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)
        expected_cases = {"sources": 2, "interactions": 3, "all": 5}[cases]
        report["passed"] = (len(report["cases"]) == expected_cases and "error" not in report
                            and all(c.get("decision_passed") for c in report["cases"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(scrub.scrub_json(report), ensure_ascii=False, indent=2))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("output/prompt-implementation-20261003/supplementary-model.json"))
    parser.add_argument("--cases", choices=("sources", "interactions", "all"), default="sources")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.output, args.cases)) else 1)
