"""The documented values are the complete results actually delivered to a Cell."""

import asyncio
import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.conversation_kernel.decision_tools import decision_output_schema
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import collaboration_output_schema
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_agent_code_mode_execution import _settled_formal_records
from tests.test_round4_plan_host import _DetachedDraftModel, _open_test_session, _text, _tool_call


@pytest.mark.postgres
def test_real_cell_returns_agent_values_and_actual_failure_values_matching_owner_schema(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    code = '''
const observed = [];
const keep = (tool, value) => { observed.push({tool, value}); return value; };
const admitted = keep("run_agent", await tools.run_agent([{instruction:"SDK_WORK"}]));
const first = admitted.content.find(part => part.type === "object").value;
let detail;
do {
  detail = await tools.query_agents({target:first.work});
  if (detail.content.find(part => part.type === "object").value.work_status !== "running") {
    await tools.wait_agent({works:[first.work], timeout_seconds:0.01});
  }
} while (detail.content.find(part => part.type === "object").value.work_status !== "running");
keep("query_agents", detail);
keep("query_agents", await tools.query_agents({}));
keep("query_agents", await tools.query_agents({target:first.agent}));
keep("wait_agent", await tools.wait_agent({works:[first.work], timeout_seconds:0}));
keep("send_agent_message", await tools.send_agent_message({target:first.agent, message:"SDK_MAIL"}));
keep("stop_agent", await tools.stop_agent({target:first.agent}));
keep("wait_agent", await tools.wait_agent({works:[first.work], timeout_seconds:0}));
keep("wait_agent", await tools.wait_agent({agents:[first.agent], timeout_seconds:0}));
keep("decide", await tools.decide({condition:"has content", inputs:[]}));
try { await tools.decide({condition:"has content", inputs:["text"]}); }
catch (error) { keep("decide", error.result); }
try { await tools.send_agent_message({target:first.agent, message:"after stop"}); }
catch (error) { keep("send_agent_message", error.result); }
return JSON.stringify(observed);
'''

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))

        class Model(_DetachedDraftModel):
            root_id = None

            async def stream(self, request):
                self.requests.append(request)
                if request.session_id != self.root_id:
                    await asyncio.Event().wait()
                elif sum(item.session_id == self.root_id for item in self.requests) == 1:
                    for event in _tool_call("exec_cell", "sdk-output-cell", {"code": code}):
                        yield event
                else:
                    for event in _text("SDK_RESULT_TYPES"):
                        yield event

        model = Model()
        core, session = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        model.root_id = session.session_id
        settings = session._decision_tools._settings
        settings.value = replace(settings.read(), memory_retrieval=replace(settings.read().memory_retrieval, decision=None))
        try:
            await asyncio.wait_for(session.run_turn(PromptContent.text("Inspect the actual SDK values."),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS), timeout=20)
            records = await _settled_formal_records(session, expected=1)
            assert len(records) == 1 and records[0]["kind"] == "return", records
            observed = json.loads(records[0]["text"])
            assert {item["tool"] for item in observed} == {
                "run_agent", "query_agents", "send_agent_message", "stop_agent", "wait_agent", "decide",
            }
            for item in observed:
                schema = decision_output_schema() if item["tool"] == "decide" else collaboration_output_schema(item["tool"])
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(item["value"])
            failures = {item["tool"] for item in observed if item["value"]["meta"]["status"] == "ERROR"}
            assert failures == {"decide", "send_agent_message"}
            assert all(any(item["tool"] == name and item["value"]["meta"]["status"] == "SUCCESS" for item in observed)
                for name in {"run_agent", "query_agents", "send_agent_message", "stop_agent", "wait_agent", "decide"})
        finally:
            await core.shutdown()

    asyncio.run(scenario())
