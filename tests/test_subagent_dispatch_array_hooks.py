"""One canonical dispatch keeps ordered per-member hooks, including index zero."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
from pulsara_agent.conversation_kernel.memory.contracts import FrozenModelCallMemoryContext
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult, PreparedResolvedToolInvocation
from pulsara_agent.conversation_kernel.tool_execution import (
    CodeModeToolRequest, ToolBatchExecutor, _batch_reservations, _code_result, _code_snapshot,
)
from pulsara_agent.conversation_kernel.tool_invocations import (
    ToolInvocationSource, ToolInvocationSourceKind, current_tool_invocation,
)
from pulsara_agent.hooks.contracts import ContextOutcome, GateDecision, GateOutcome
from pulsara_agent.hooks.context import _causal_identity
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_round9_2_hook_subsystem import _scope


def _fixture(tmp_path, dispatch, *, same_agent=False):
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import prepare_batch_refs
    assignments = [{"instruction": "one", "work_title": "original one"},
                   {"instruction": "two", "work_title": "original two"}]
    if same_agent:
        target = {"kind": "AGENT", "agent_id": random_id("session")}
        assignments = [{**item, "target": target} for item in assignments]
    refs = prepare_batch_refs([{ "target": item["target"] } if "target" in item else {} for item in assignments])
    arguments = {"members": [{"agent_id": refs["agents"][i]["agent_id"],
        "work_id": refs["works"][i]["work_id"], "parameters": item} for i, item in enumerate(assignments)]}
    owner = object.__new__(ToolBatchExecutor)
    owner._hooks, owner._hook_context = SimpleNamespace(dispatch=dispatch), None
    owner._tools = SimpleNamespace(snapshot_workspace_root=lambda: tmp_path)
    owner._deadlines = KernelExecutionDeadlineFactory()
    scope = _scope()
    request = CodeModeToolRequest(scope.session_id, "test", None, FrozenModelCallMemoryContext())
    facts = SimpleNamespace(run_permission_snapshot=SimpleNamespace(
        effective_mode=PermissionMode.BYPASS_PERMISSIONS, plan_workflow_id=None))
    prepared = PreparedResolvedToolInvocation("run_agent", "run_agent", freeze_json(arguments))
    call_id, turn_id = random_id("call"), random_id("turn")
    source = ToolInvocationSource(call_id, ToolInvocationSourceKind.CODE_MODE_CALL, turn_id,
        source_exec_attempt_id=random_id("attempt"), call_sequence=1,
        tool_name="run_agent", original_arguments=prepared.resolved_arguments)
    return owner, scope, request, facts, prepared, source, refs


@pytest.mark.parametrize("rewrite", ["title", "reuse_reserved_identity", "unknown", "oversize_title", "later_block", "fifo_cycle"])
def test_pre_hooks_preserve_index_and_revalidate_whole_batch(tmp_path, rewrite):
    observed = []
    refs = None

    async def dispatch(envelope, **kwargs):
        observed.append(envelope)
        index = envelope.causal_ref.member_index
        updated = dict(envelope.public_input.tool_input)
        updated["work_title"] = f"final {index}"
        if index == 0 and rewrite == "fifo_cycle":
            updated["depends_on"] = [refs["works"][1]]
        if index == 1:
            if rewrite == "reuse_reserved_identity":
                updated["target"] = refs["agents"][index]
            elif rewrite == "unknown":
                updated["unknown"] = True
            elif rewrite == "oversize_title":
                updated["work_title"] = "字" * 86
            elif rewrite == "later_block":
                return GateOutcome(decision=GateDecision.BLOCK, reason="later refused")
        return GateOutcome(updated_input=updated)

    owner, scope, request, facts, prepared, source, refs = _fixture(tmp_path, dispatch, same_agent=rewrite == "fifo_cycle")

    async def run():
        token = current_tool_invocation.set(source)
        reservations = _batch_reservations.set([])
        try:
            outcome, outer = await owner._dispatch_pre_tool(view=object(), scope=scope,
                prepared=prepared, turn_id=source.turn_id, assistant_entry_id=None,
                tool_call_id=source.invocation_id, request=request, canonical_facts=facts)
            assert [event.causal_ref.member_index for event in observed] == [0, 1]
            assert all(event.causal_ref.invocation_id == source.invocation_id for event in observed)
            assert len({_causal_identity(event.causal_ref) for event in observed}) == 2
            assert outer.member_index is None and outer.invocation_id == source.invocation_id
            assert outcome.decision is (GateDecision.PROCEED if rewrite == "title" else GateDecision.BLOCK)
            if rewrite == "title":
                assert [item["parameters"]["work_title"] for item in outcome.updated_input["members"]] == ["final 0", "final 1"]
        finally:
            _batch_reservations.reset(reservations)
            current_tool_invocation.reset(token)

    asyncio.run(run())


@dataclass(frozen=True)
class _Settlement:
    turn_id: str
    tool_call_id: str
    result_id: str
    result_entry_id: str
    result_state: str
    tool_name: str
    public_arguments: object


@pytest.mark.parametrize("failure", [RuntimeError("first post failed"), asyncio.CancelledError("stop during first post")])
def test_post_failure_still_visits_later_member_with_exact_receipt_and_one_result(tmp_path, failure):
    observed, diagnostics = [], []

    async def dispatch(envelope, **kwargs):
        observed.append(envelope)
        if envelope.causal_ref.member_index == 0:
            raise failure
        return ContextOutcome()

    owner, scope, request, facts, prepared, source, refs = _fixture(tmp_path, dispatch)
    owner._offer_operational_best_effort = diagnostics.append
    payload = {**refs, "details": [{"work_title": f"final {i}", "status": "PENDING",
        "inspect": {"tool": "query_agents", "arguments": {"target": target}}}
        for i, target in enumerate(refs["works"])]}
    settlement = _Settlement(source.turn_id, source.invocation_id, random_id("result"), random_id("entry"),
                             "SUCCESS", "run_agent", prepared.resolved_arguments)

    async def run():
        source_token = current_tool_invocation.set(source)
        snapshot_token = _code_snapshot.set(SimpleNamespace(hook_epoch_nonce=object()))
        result_token = _code_result.set([(KernelToolResult("SUCCESS", canonical_json_bytes(payload),
            public_format=ToolOutputSourceFormatHint.JSON), settlement)])
        try:
            async def finish():
                await owner._dispatch_post_tool(view=object(), scope=scope, prepared=prepared,
                    settlement=settlement, request=request, canonical_facts=facts, continuity_scope=None)
            if isinstance(failure, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError) as caught:
                    await finish()
                assert caught.value is failure
            else:
                await finish()
            assert [event.causal_ref.member_index for event in observed] == [0, 1]
            assert all(event.causal_ref.result_id == settlement.result_id for event in observed)
            for index, event in enumerate(observed):
                assert event.public_input.tool_response == {"agent": refs["agents"][index],
                    "work": refs["works"][index], **payload["details"][index]}
            assert len(diagnostics) == 1 and diagnostics[0].public_payload["member_index"] == 0
            assert _code_result.get()[0][1] is settlement
        finally:
            _code_result.reset(result_token)
            _code_snapshot.reset(snapshot_token)
            current_tool_invocation.reset(source_token)

    asyncio.run(run())
