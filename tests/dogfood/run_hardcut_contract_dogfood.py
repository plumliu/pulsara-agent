"""Saved DeepSeek wires, current hardcut owners, isolated disposable database."""

from __future__ import annotations
import argparse
import asyncio
import base64
from io import BytesIO
import json
import os
import traceback
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tempfile import TemporaryDirectory
import yaml
from PIL import Image
from tests.test_mcp_rpc_errors_and_pagination import _SERVER
from time import monotonic
from psycopg.rows import dict_row
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from tests.support.postgres_database import (
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
    admin_root_dsn,
)
from tests.support.dogfood_results import _phase_cell_outputs, _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
    _create_database,
    _drop_database,
)
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
)


def _wire_evidence(records, *, expect_image):
    roots = [
        record["provider_input"]
        for record in records
        if any(
            tool.get("function", tool).get("name") == "exec_cell"
            for tool in record["provider_input"].get("tools", ())
        )
    ]
    assert roots, "the actual ROOT provider input was not recorded"
    first = roots[0]
    if "instructions" in first:
        assert first["instructions"] == DEFAULT_SYSTEM_PROMPT
    else:
        assert first["messages"][0] == {
            "role": "system", "content": DEFAULT_SYSTEM_PROMPT,
        }
    for previous, current in zip(roots, roots[1:]):
        key = "messages" if "messages" in current else "input"
        assert previous["tools"] == current["tools"]
        assert previous.get("instructions") == current.get("instructions")
        assert current[key][: len(previous[key])] == previous[key]

    def image_urls(value):
        if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from image_urls(item)
        elif isinstance(value, list):
            for item in value:
                yield from image_urls(item)

    images = []
    if expect_image:
        for url in image_urls(roots[-1]):
            with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as picture:
                assert picture.size == (96, 96)
                assert picture.convert("RGB").getpixel((0, 0)) == (20, 121, 189)
                images.append({"size": picture.size, "pixel": (20, 121, 189)})
        assert images, "the actual provider input contained no fixture image"
    return {
        "root_calls": len(roots),
        "unchanged_prefix_comparisons": len(roots) - 1,
        "actual_image_payloads": images,
    }


async def main(args):
    saved = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
    scrub = saved.credential_scrub_set()
    report = {"provider_calls": [], "checks": [], "completed": False}

    def save():
        raw = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        args.report.write_text(scrub.scrub_text(raw))

    wires = {
        c.target.wire_api.value: c
        for c in saved.model_connections
        if c.target.route_id == "deepseek" and c.target.model_id == "deepseek-flash"
    }
    required = {"openai_chat_completions", "openai_responses"}
    if set(wires) != required:
        raise RuntimeError("both saved DeepSeek flash protocols are required")
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    await catalog.refresh()
    if args.test_postgres:
        db = create_migrated_postgres_test_database()
        database_name, runtime_dsn, admin_dsn = (
            db.database_name,
            db.runtime_dsn,
            db.admin_dsn,
        )
    else:
        database_name, _, admin_dsn, runtime_dsn = _create_database(saved)
    old_home = os.environ.get("PULSARA_HOME")
    report["database"] = database_name
    try:
        with TemporaryDirectory(prefix="pulsara-hardcut-def-") as raw:
            root = Path(raw)
            home = root / "home"
            home.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            workspace_instructions = (
                "This is an isolated validation workspace. Follow each validation task literally. "
                "Use only the requested fixture files, the configured probe MCP, and the bundled "
                "Code Mode Skill when needed for its interface. Do not inspect settings, "
                "credentials, environment variables, or unrelated files. Keep final answers short."
            )
            (workspace / "AGENTS.md").write_text(workspace_instructions)
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin_dsn))
            )
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(native, report["provider_calls"])
            core = KernelHostCore.production(model_runtime=runtime)
            marker = "HARDCUT_NATIVE_END_20261116"
            text = (
                "".join(
                    f"{i:05d} normal text " + ("abcdef0123456789" * 9) + "\n"
                    for i in range(650)
                )
                + "\n"
                + marker
                + "\n"
            )
            (workspace / "long.txt").write_text(text)
            Image.new("RGB", (96, 96), "#1479bd").save(workspace / "test.png")
            if args.expanded:
                (workspace / "peer.py").write_text(
                    _SERVER.replace("OPTIONS", repr("{}"))
                )
                (home / "mcp.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "servers": {
                                "probe": {
                                    "scope_policy": "ROOT_AND_SUBAGENTS",
                                    "effect_policy": {"default_effect": "READ_ONLY"},
                                    "transport": {
                                        "type": "stdio",
                                        "command": sys.executable,
                                        "args": [str(workspace / "peer.py")],
                                        "cwd": ".",
                                    },
                                }
                            }
                        }
                    )
                )
            (workspace / "errors.js").write_text("""try {
  await tools.read_file({target:{kind:"PATH",path:"missing-file-for-error-check.txt"}});
  console.log({unexpectedSuccess:true});
} catch (err) {
  console.log({errResultStatus:err.result?.meta.status,details:err.result?.content});
}
""")
            (
                workspace / "images.js"
            ).write_text("""const image=await tools.view_image({target:{kind:"PATH",path:"test.png"}});
console.log({readStatus:image.meta.status,nested:{media:image.content.filter(p=>p.type==="image")}});
""")
            (
                workspace / "agents.js"
            ).write_text("""const admitted=await tools.run_agent([
  {instruction:"Reply exactly AGENT_A_OK. Do not call tools."},
  {instruction:({works})=>"Reply exactly AGENT_B_OK. Do not call tools.",depends_on:({works})=>[works[0]]}
]);
console.log(admitted);
const works=admitted.content.map(p=>p.value.work);
console.log(await tools.wait_agent({works,settle:"all",timeout_seconds:30}));
console.log(await tools.wait_agent({agents:admitted.content.map(p=>p.value.agent),timeout_seconds:0}));
""")
            (
                workspace / "complete.js"
            ).write_text("""const result = await tools.read_file({target:{kind:"PATH",path:"long.txt"}});
const text=result.content.filter(p=>p.type==="text").map(p=>p.value).join("");
console.log({length:text.length,tail:text.slice(-40),nativeNextPresent:Object.hasOwn(result,"next")});
""")
            try:
                for wire in args.wire or sorted(required):
                    connection = wires[wire]
                    section = {
                        "wire": wire,
                        "connection_id": connection.id.value,
                        "model": connection.target.model_id,
                        "steps": [],
                    }
                    report["checks"].append(section)
                    save()
                    session = await core.open_session(
                        HostWorkspaceInput(
                            workspace_kind="project",
                            workspace_root=workspace,
                            trust_workspace_mcp_config=False,
                        ),
                    )
                    await session.attach_controller(random_id("attachment"))
                    await session.update_model_call_binding(
                        _binding(native, connection)
                    )
                    session._compaction.policy = ResolvedCompactionPolicy(
                        automatic_enabled=False,
                        maximum_recent_human_messages=1,
                        maximum_recent_human_text_utf8_bytes=8 << 10,
                    )
                    prompts = [
                        (
                            "native_full",
                            "Use only native read_file to read long.txt completely and report the exact final HARDCUT marker found in the file. Read any returned NEXT targets with read_file until you reach the end. Do not use offset to skip ahead, terminal, exec_cell, or repeat the initial file read.",
                        ),
                        (
                            "js_full",
                            "Use exec_cell once with target PATH complete.js to run the already prepared validation script. Await its independently delivered output, then report its length, tail and nativeNextPresent. Do not rewrite the script or read long.txt natively. If needed, use manage_cell wait.",
                        ),
                    ]
                    if args.expanded:
                        prompts += [
                            (
                                "sdk_composition",
                                "Use native read_file to read the bundled pulsara-code-mode Skill's API references for read_file and search_file_content before writing code. Then author one inline exec_cell script that reads long.txt and searches it for HARDCUT_NATIVE_END_20261116 using search_file_content content mode. Use their documented return fields to console.log one JSON object with fullTextLength (the complete file text's length), totalLines (the returned total line count), and matchedLines (the original line numbers from the search object records). Do not use a prepared script, terminal, native reads of long.txt, or hardcoded measurements. Await the independent Cell output and report the actual object.",
                            ),
                            (
                                "js_error",
                                "Use exec_cell target PATH errors.js once. Await its output and report errResultStatus and the actual error. Do not fix or retry the missing-file operation.",
                            ),
                            (
                                "js_images",
                                "Use exec_cell target PATH images.js once. Report readStatus and describe what was actually delivered. Do not call another tool to read the image.",
                            ),
                            (
                                "agents",
                                "Use exec_cell target PATH agents.js once. Await the independent output, then report the two ordered Work statuses and final Agent idle states. Do not spawn extra work or rewrite the script.",
                            ),
                            (
                                "mcp_prepare",
                                "Use query_capabilities with kind MCP and target MCP_TOOL server_id probe remote_tool_name first. Read its input and optional output schema, description and invocation target. Do not call it yet. Report the exact invocation target for the next task.",
                            ),
                            (
                                "mcp_cell_one",
                                "Use the previously obtained MCP invocation target in one inline exec_cell script. Call tools.call_mcp_tool({target: <previous invocation target>, arguments: {}}) and console.log the complete result. Do not query it again. Wait for the independent output and report the result text.",
                            ),
                            (
                                "mcp_cell_two",
                                "Use the SAME previous MCP invocation target in a second, separate inline exec_cell script. Call tools.call_mcp_tool({target: <same previous invocation target>, arguments: {}}) and console.log the complete result. Do not query it again. Report the actual result text.",
                            ),
                        ]
                    if args.memory_only:
                        prompts = [
                            (
                                "memory_create",
                                'Use remember exactly once: context_kind CURRENT_PROJECT, kind RESPONSE_PREFERENCE, title "HARD_CUT_PREFERENCE_20261116: include exact numeric measurements", statement "this body must be discarded". Report its actual Target. Do not invoke other tools.',
                            ),
                            (
                                "memory_query",
                                'Use query_memory query "HARD_CUT_PREFERENCE_20261116" with limit 5. Report its exact title and statement, without modifying the record.',
                            ),
                            (
                                "memory_modify_delete",
                                "Use manage_memory to move the known preference to GLOBAL scope, then delete it using the returned same Target. The runtime supplies concurrency; do not pass a timestamp. Do not create another memory. Report actual results.",
                            ),
                        ]
                    if args.smoke:
                        prompts = [
                            (
                                "smoke",
                                "Reply exactly HARD_CUT_SMOKE_OK. Do not call tools.",
                            )
                        ]
                    epoch_start = len(report["provider_calls"])
                    for label, prompt in prompts:
                        if label == prompts[0][0]:
                            prompt = (
                                f"Complete AGENTS.md at this workspace root:\n{workspace_instructions}\n\n"
                                + prompt
                            )
                        start = len(report["provider_calls"])
                        trace_start = (
                            len(await asyncio.to_thread(_tool_trace, session))
                            if label == "sdk_composition" else None
                        )
                        result = await session.run_turn(
                            PromptContent.text(prompt), command_id=random_id("command")
                        )
                        step = {
                            "name": label,
                            "final": result.final_text,
                            "calls": len(report["provider_calls"]) - start,
                        }
                        section["steps"].append(step)
                        save()
                        if label == "sdk_composition":
                            trace = (await asyncio.to_thread(_tool_trace, session))[trace_start:]
                            references = [
                                row["arguments"].get("target", {}).get("path", "")
                                for row in trace
                                if row["tool_name"] == "read_file"
                                and row["source_kind"] == "MODEL_TOOL_CALL"
                                and row["result_state"] == "SUCCESS"
                            ]
                            assert all(
                                any(path.endswith(f"/api/{name}.md") for path in references)
                                for name in ("read_file", "search_file_content")
                            ), "the model did not read the requested installed API references"
                            cells = {
                                row["result"]["target"]["cell_id"]
                                for row in trace
                                if row["tool_name"] == "exec_cell" and row["result_state"] == "SUCCESS"
                            }
                            assert len(cells) == 1
                            assert any(
                                row["tool_name"] == "search_file_content"
                                and row["source_kind"] == "CODE_MODE_CALL"
                                and row["result_state"] == "SUCCESS"
                                for row in trace
                            )
                            outputs = await asyncio.to_thread(_phase_cell_outputs, session, cells)
                            assert not any(output["record"]["kind"] == "diagnostic" for output in outputs)
                            expected = {
                                "fullTextLength": len(text), "totalLines": len(text.splitlines()),
                                "matchedLines": [len(text.splitlines())],
                            }
                            summaries = []
                            for output in outputs:
                                if output["status"] != "CONSUMED":
                                    continue
                                body = "".join(
                                    part["value"] for part in output["record"]["content"]
                                    if part["type"] == "text"
                                )
                                try:
                                    summaries.append(json.loads(body))
                                except (ValueError, TypeError):
                                    pass
                            assert expected in summaries, (expected, summaries)
                            step["api_reference_reads"] = references
                            step["actual_cell_summary"] = expected
                            save()
                        if label == "native_full" and marker not in result.final_text:
                            raise AssertionError("native full-read marker missing")
                        if (
                            label == "js_full"
                            and str(len(text)) not in result.final_text
                        ):
                            raise AssertionError("complete JS string length missing")
                        if label.startswith("memory_"):
                            with session.repository.connection_provider.connection(
                                lane=PostgresConnectionLane.INSPECTOR,
                                row_factory=dict_row,
                                deadline_monotonic=monotonic() + 30,
                            ) as c:
                                step["memory_rows"] = [
                                    dict(row)
                                    for row in c.execute(
                                        "SELECT id,title,statement,context_id FROM pulsara_v3.memory_facts ORDER BY id"
                                    ).fetchall()
                                ]
                            if label in {"memory_create", "memory_query"}:
                                assert (
                                    len(step["memory_rows"]) == 1
                                    and step["memory_rows"][0]["statement"] == ""
                                )
                                assert step["memory_rows"][0]["context_id"].startswith(
                                    "workspace:"
                                )
                            else:
                                assert step["memory_rows"] == []
                            save()
                        if label == "js_error" and "ERROR" not in result.final_text:
                            raise AssertionError("err.result ERROR not observed")
                        if (
                            label in {"mcp_cell_one", "mcp_cell_two"}
                            and "same connection success" not in result.final_text
                        ):
                            raise AssertionError("same session MCP REF reuse failed")
                    section["before_compaction_call_range"] = [
                        epoch_start,
                        len(report["provider_calls"]),
                    ]
                    section["wire_evidence"] = _wire_evidence(
                        report["provider_calls"][epoch_start:],
                        expect_image=args.expanded,
                    )
                    if not args.smoke and not args.memory_only:
                        compact = await session.compact_context(
                            command_id=random_id("command")
                        )
                        section["compaction"] = str(compact.disposition)
                        save()
                        if compact.disposition.value != "COMPACTED":
                            raise AssertionError(f"compaction not adopted: {compact}")
                        result = await session.run_turn(
                            PromptContent.text("Reply exactly POST_COMPACTION_OK."),
                            command_id=random_id("command"),
                        )
                        section["steps"].append(
                            {"name": "after_compaction", "final": result.final_text}
                        )
                        save()
                        if "POST_COMPACTION_OK" not in result.final_text:
                            raise AssertionError("post-compaction failed")
                    with session.repository.connection_provider.connection(
                        lane=PostgresConnectionLane.INSPECTOR,
                        row_factory=dict_row,
                        deadline_monotonic=monotonic() + 30,
                    ) as c:
                        section["tool_results"] = [
                            dict(r)
                            for r in c.execute(
                                "SELECT result_state,source_result_entry_id,native_projection FROM pulsara_v3.tool_results WHERE session_id=%s ORDER BY observed_at",
                                (session.session_id,),
                            ).fetchall()
                        ]
                    section["passed"] = True
                    save()
            finally:
                await core.shutdown()
            report["completed"] = True
            save()
    except BaseException:
        report["error"] = traceback.format_exc()
        save()
        raise
    finally:
        if old_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = old_home
        if args.test_postgres:
            drop_postgres_test_database(admin_root_dsn(), database_name)
        else:
            _drop_database(saved, database_name)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--test-postgres",
        action="store_true",
        help="Use the verified test PostgreSQL injection instead of saved local PostgreSQL settings",
    )
    p.add_argument("--expanded", action="store_true")
    p.add_argument("--memory-only", action="store_true")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--wire", action="append")
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args))
