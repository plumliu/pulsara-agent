"""Saved Decision acceptance through a real Host, Node worker and Guest.

The ROOT model is a deterministic driver that requests exec_cell; Decision is
the real saved service, without mocked responses or a chat-model fallback.
Only a separately created local test database and temporary home are written.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from unittest.mock import patch

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _rows
from tests.dogfood.run_model_switch_handover_dogfood import _ReadOnlySettingsStore, _binding
from tests.dogfood.run_stop_chain_dogfood import Evidence, _database_preflight, _now
from tests.support.model_stream import _text, _tool_call
from tests.support.postgres_database import create_migrated_postgres_test_database, drop_postgres_test_database
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_agent_code_mode_execution import _formal_cell_rows


MATERIALS = (
    ("install.txt", "用户：安装 impeccable skill 到用户目录。", False),
    ("writing.txt", "用户：主要是 bundled skill 一定要好好写，写清楚。不要写大量过度防御性的话术。", True),
    ("broken.txt", "用户：skill 也无法打开或者关闭了。", False),
    ("guidance.txt", "用户：明确在 skill 里面说无需先打开或者在最后打开。", True),
)
CONDITION = "这段讨论中，用户是否明确提出了关于 Skill 内容、写法或使用指导的要求？"


def _program():
    return """
const available = tools.decide.available();
if (typeof available !== "boolean") throw new Error("available must be synchronous boolean");
if (!available) throw new Error("saved Decision was complete but Cell availability is false");
const descriptor = Object.getOwnPropertyDescriptor(tools.decide, "available");
if (!Object.isFrozen(tools.decide) || !Object.isFrozen(tools.decide.available)
    || descriptor.writable || descriptor.configurable)
  throw new Error("decide or local availability method is mutable");
for (let index = 0; index < 5; index++)
  if (tools.decide.available() !== available) throw new Error("availability changed within Cell");
const jsTools = ["run_agent", "query_agents", "send_agent_message", "wait_agent", "stop_agent"];
if (jsTools.some(name => typeof tools[name] !== "function")
    || "batch" in tools.run_agent || "works" in tools.wait_agent)
  throw new Error("JS collaboration directory does not match the unified array contract");
const knownAgents = await tools.query_agents({});
const candidates = [];
for (const path of PATHS) {
  const material = await tools.read_file({target:{kind:"PATH",path}});
  candidates.push({source:material.target, material});
}
const condition = CONDITION;
const empty = await tools.decide({condition,inputs:[]});
if (!Array.isArray(empty.decisions) || empty.decisions.length) throw new Error("empty call changed shape");
let invalid;
try { await tools.decide({condition:" ",inputs:["material"]}); }
catch (error) { invalid = {name:error.name,code:error.code,message:error.message,result:error.result}; }
if (!invalid || invalid.name !== "ToolError" || invalid.code !== "INVALID_ARGUMENTS")
  throw new Error("invalid condition was not a normal ToolError");
const {decisions} = await tools.decide({condition,inputs:candidates.map(item => item.material.content)});
if (!Array.isArray(decisions) || decisions.length !== candidates.length
    || decisions.some(value => typeof value !== "boolean")) throw new Error("invalid public Decision result");
const selected = candidates.filter((_, index) => decisions[index]);
const output = {condition,decisions,selected};
const written = await tools.write_file({target:{kind:"PATH",path:"selected.json"},content:JSON.stringify(output)});
return {available,jsTools,knownAgents,
  empty,invalid,candidates,decisions,selected,written};
""".replace("PATHS", json.dumps([path for path, _, _ in MATERIALS])).replace("CONDITION", json.dumps(CONDITION, ensure_ascii=False))


def _tool_results(session):
    return _rows(session, """
        SELECT i.id AS invocation_id,i.source_kind,i.tool_name,i.call_sequence,
               i.original_arguments,r.result_state,r.attempt_id,
               r.output_body_inline_content AS result_body
        FROM pulsara_v3.tool_invocations i LEFT JOIN pulsara_v3.tool_results r
          ON r.session_id=i.session_id AND r.invocation_id=i.id
        WHERE i.session_id=%s ORDER BY i.call_sequence NULLS FIRST
    """, (session.session_id,))


async def _exercise(runtime, binding_connection, workspace, evidence, timeout, stack):
    import pulsara_agent.conversation_kernel.host as host_module
    program = _program()
    evidence.data["cell_source"] = program
    requested = False

    async def stream(request):
        nonlocal requested
        names = [tool.name for tool in request.compiled_input.tools]
        evidence.data["root_driver_requests"].append({"turn_id":request.turn_id,
            "native_tools":names, "message_count":len(request.compiled_input.messages)})
        assert {"query_agents", "send_agent_message", "stop_agent", "exec_cell"} <= set(names)
        assert not {"run_agent", "wait_agent", "decide"} & set(names)
        if not requested:
            requested = True
            payloads = _tool_call("exec_cell", random_id("call"), {"code":program})
        else:
            payloads = _text(random_id("block"), "SCRIPTED_ROOT_OBSERVED_CELL")
        for payload in payloads:
            yield payload

    driver = CallbackScriptedKernelModel(stream)
    # Keep native target/schema preparation while replacing only ROOT output.
    driver._model_runtime = runtime
    driver._preparer = DirectKernelModelPort(model_runtime=runtime)
    stack.enter_context(patch.object(host_module, "DirectKernelModelPort", lambda **_: driver))
    core = KernelHostCore.production(model_runtime=runtime)
    session = None
    try:
        session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=workspace,
            trust_workspace_mcp_config=False))
        await session.update_model_call_binding(_binding(runtime, binding_connection))
        evidence.data.update(session_id=session.session_id, host_session_id=session.host_session_id)
        result = await session.run_turn(PromptContent.text("Run the exact saved Decision acceptance Cell."),
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS, command_id=random_id("command"))
        evidence.data["source_turn_id"] = result.turn_id
        async with asyncio.timeout(timeout):
            while True:
                rows = await asyncio.to_thread(_formal_cell_rows, session)
                records = [record for _, record in rows]
                evidence.data["cell_records"] = records
                errors = [record for record in records if record["kind"] == "error"]
                assert not errors, errors
                returned = [record for record in records if record["kind"] == "return"]
                if returned and not session.code_mode_cells() and session.active_root_turn_id() is None:
                    break
                await asyncio.sleep(0.02)
        assert len(returned) == 1, records
        value = json.loads(returned[0]["text"])
        evidence.data["cell_result"] = value
        assert value["available"] is True
        assert value["decisions"] == [expected for _, _, expected in MATERIALS], value["decisions"]
        selected_paths = [item["source"]["path"] for item in value["selected"]]
        assert selected_paths == [path for path, _, expected in MATERIALS if expected], selected_paths
        assert value["selected"] == [candidate for candidate, decision in zip(value["candidates"], value["decisions"], strict=True) if decision]
        written = json.loads((workspace / "selected.json").read_text())
        assert written == {"condition":CONDITION, "decisions":value["decisions"], "selected":value["selected"]}
        evidence.data["written_selection"] = written
        tools = await asyncio.to_thread(_tool_results, session)
        evidence.data["tool_results"] = tools
        decisions = [row for row in tools if row["tool_name"] == "decide"]
        assert len(decisions) == 3
        assert [row["result_state"] for row in decisions] == ["SUCCESS", "INVALID_ARGUMENTS", "SUCCESS"]
        assert all(row["source_kind"] == "CODE_MODE_CALL" for row in decisions)
        assert not any(row["tool_name"] == "decide.available" for row in tools)
        assert all(row["result_state"] == "SUCCESS" for row in tools if row["tool_name"] != "decide")
        requests = evidence.data["sdk_requests"]
        assert len(requests) == 1, "available, empty and invalid calls must not open HTTP requests"
        assert requests[0].get("closed_at") and "response" in requests[0], requests
        payload = requests[0]["request"]
        shape = evidence.data["decision"]["shape"]
        assert set(payload) == {"model", "state" if shape == "system_one" else "input", "questions"}
        assert payload["model"] == evidence.data["decision"]["model_id"]
        state = payload["state"] if shape == "system_one" else json.loads(payload["input"])
        assert state == {"inputs":[{"index":index,"text":item["material"]["content"]}
            for index,item in enumerate(value["candidates"])]}
        assert len(payload["questions"]) == len(MATERIALS)
        evidence.data["prefix_preserved"] = all(
            request.compiled_input.system_prompt == driver.requests[0].compiled_input.system_prompt
            and request.compiled_input.tools == driver.requests[0].compiled_input.tools
            and request.compiled_input.messages[:len(driver.requests[0].compiled_input.messages)] == driver.requests[0].compiled_input.messages
            for request in driver.requests
        )
        assert evidence.data["prefix_preserved"]
        evidence.data["assertions"] = {"real_decision":True,"source_preserved":True,
            "normal_tool_settlement":True,"root_native_matrix":True,"js_collaboration_directory":True,
            "local_available_no_http":True,"empty_and_invalid_no_http":True,"no_chat_http":True}
    finally:
        try:
            if session is not None:
                evidence.data["tool_results_at_close"] = await asyncio.to_thread(_tool_results, session)
        finally:
            await core.shutdown()
        evidence.data["core_shutdown_at"] = _now()
        if session is not None:
            evidence.data["physical_close"] = {
                "decision_service":session._decision_service.physical_close_complete,
                "code_mode":session._code_mode.physical_close_complete,
                "node_children_stopped":session._code_mode.local_children_stopped,
            }
            assert all(evidence.data["physical_close"].values()), evidence.data["physical_close"]


async def run(args):
    home = require_pulsara_home()
    saved = LocalSettingsStore(home / "local-settings.yaml").read()
    decision = saved.memory_retrieval.decision
    if decision is None or not decision.complete:
        raise RuntimeError("saved Jev-like Decision connection or required credential is missing; no fallback used")
    destination = args.output.resolve()
    if destination.exists():
        raise RuntimeError(f"evidence already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(destination, saved, decision.endpoint)
    evidence.data.update(saved_home=str(home), decision=decision.public(), ranking_mode=saved.memory_retrieval.ranking_mode,
        scope="real saved Decision; scripted ROOT driver; real Host/Node/Guest/tool leaf and canonical settlement",
        root_driver_requests=[], materials=[{"path":path,"text":text,"expected":expected} for path,text,expected in MATERIALS])
    logger = logging.getLogger()
    prior_handlers = logger.handlers[:]
    logger.handlers = [evidence]
    prior_env = {key:os.environ.get(key) for key in ("PULSARA_HOME","PULSARA_POSTGRES_ADMIN_DSN","PULSARA_POSTGRES_DSN")}
    database = None
    admin = None
    try:
        admin, runtime_dsn, facts = await asyncio.to_thread(_database_preflight, home, args.postgres_config)
        evidence.data["database_preflight"] = facts
        if args.prepare_only:
            evidence.data["status"] = "prepared"
            return evidence
        os.environ.update(PULSARA_POSTGRES_ADMIN_DSN=admin, PULSARA_POSTGRES_DSN=runtime_dsn)
        database = await asyncio.to_thread(create_migrated_postgres_test_database)
        evidence.data["isolated_database"] = asdict(database)
        with TemporaryDirectory(prefix="pulsara-decide-dogfood-") as temporary, ExitStack() as stack:
            root = Path(temporary)
            temporary_home, workspace = root / "home", root / "workspace"
            temporary_home.mkdir()
            workspace.mkdir()
            for path, text, _ in MATERIALS:
                (workspace / path).write_text(text, encoding="utf-8")
            os.environ["PULSARA_HOME"] = str(temporary_home)
            settings = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            runtime = ModelRuntime.production(settings=settings, catalog=catalog)
            connection = next((item for item in saved.model_connections if item.target.route_id == "deepseek"
                and item.target.wire_api.value == "openai_chat_completions"), None)
            if connection is None:
                raise RuntimeError("saved ROOT compilation binding is missing (no chat request is made)")
            import openai
            from openai.resources.chat.completions import AsyncCompletions
            from openai.resources.decisions import AsyncDecisions
            original_post, original_decide, original_close = openai.AsyncOpenAI.post, AsyncDecisions.create, openai.AsyncOpenAI.close
            clients = {}

            async def observed(owner, payload, operation):
                record = {"started_at":_now(),"request":payload}
                evidence.data["sdk_requests"].append(record)
                clients[id(owner)] = record
                began = monotonic()
                try:
                    response = await operation()
                    record["response"] = (response.http_response.json() if hasattr(response,"http_response")
                        else response.model_dump(mode="json") if hasattr(response,"model_dump") else response)
                    return response
                except BaseException as error:
                    record["error"] = {"type":type(error).__name__,"message":str(error)}
                    raise
                finally:
                    record["duration_seconds"] = monotonic() - began
                    evidence.save()

            async def post(owner, path, **kwargs):
                if decision.shape == "openai_decisions":
                    return await original_post(owner,path,**kwargs)
                assert decision.shape == "system_one" and str(path) == decision.endpoint
                return await observed(owner, kwargs["body"], lambda: original_post(owner,path,**kwargs))

            async def decide(owner, **kwargs):
                assert decision.shape == "openai_decisions"
                payload = {key:value for key,value in kwargs.items() if key not in {"extra_headers","timeout"}}
                return await observed(owner._client, payload, lambda: original_decide(owner,**kwargs))

            async def close(owner):
                result = await original_close(owner)
                if id(owner) in clients:
                    clients[id(owner)]["closed_at"] = _now()
                return result

            async def no_chat(*args, **kwargs):
                raise AssertionError("unexpected chat request: ROOT must remain a deterministic driver")

            stack.enter_context(patch.object(openai.AsyncOpenAI,"post",post))
            stack.enter_context(patch.object(AsyncDecisions,"create",decide))
            stack.enter_context(patch.object(openai.AsyncOpenAI,"close",close))
            stack.enter_context(patch.object(AsyncCompletions,"create",no_chat))
            await _exercise(runtime,connection,workspace,evidence,args.timeout,stack)
        evidence.data["status"] = "passed"
    except BaseException:
        evidence.data.update(status="failed",failure=traceback.format_exc())
    finally:
        if database is not None:
            await asyncio.to_thread(drop_postgres_test_database,admin,database.database_name)
            evidence.data["isolated_database_dropped"] = True
        for key,value in prior_env.items():
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key] = value
        evidence.data["finished_at"] = _now()
        evidence.save()
        logger.handlers = prior_handlers
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("output/decide-dogfood/report.json"))
    parser.add_argument("--postgres-config",type=Path,required=True)
    parser.add_argument("--prepare-only",action="store_true")
    parser.add_argument("--timeout",type=float,default=60,help="probe observation budget, not a product lifetime limit")
    args = parser.parse_args()
    evidence = asyncio.run(run(args))
    print(json.dumps({"status":evidence.data["status"],"report":str(evidence.destination)},ensure_ascii=False))
    return 0 if evidence.data["status"] in {"passed","prepared"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
