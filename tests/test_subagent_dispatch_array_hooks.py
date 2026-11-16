"""One canonical dispatch keeps ordered per-member hooks, including index zero."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    PreparedResolvedToolInvocation,
)
from pulsara_agent.conversation_kernel.tool_execution import (
    CodeModeToolRequest,
    ToolBatchExecutor,
    _batch_reservations,
    _code_snapshot,
)
from pulsara_agent.conversation_kernel.tool_invocations import (
    ToolInvocationSource,
    ToolInvocationSourceKind,
    current_tool_invocation,
)
from pulsara_agent.hooks.contracts import (
    ContextOutcome,
    GateDecision,
    GateOutcome,
    HookContextEntry,
    HookEventType,
)
from pulsara_agent.hooks.context import HookContextOwner, _causal_identity
from pulsara_agent.hooks.executor import HookSecretScrubSet
from pulsara_agent.identity import random_id
from pulsara_agent.primitives.context import freeze_json, thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_round9_2_hook_subsystem import _Estimator, _definition, _scope


def _fixture(tmp_path, dispatch, *, same_agent=False):
    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
        prepare_batch_refs,
    )

    assignments = [
        {"instruction": "one", "work_title": "original one"},
        {"instruction": "two", "work_title": "original two"},
    ]
    if same_agent:
        target = {"kind": "AGENT", "agent_id": random_id("session")}
        assignments = [{**item, "target": target} for item in assignments]
    refs = prepare_batch_refs(
        [{"target": item["target"]} if "target" in item else {} for item in assignments]
    )
    arguments = {
        "members": [
            {
                "agent_id": refs["agents"][i]["agent_id"],
                "work_id": refs["works"][i]["work_id"],
                "parameters": item,
            }
            for i, item in enumerate(assignments)
        ]
    }
    owner = object.__new__(ToolBatchExecutor)
    owner._hooks, owner._hook_context = SimpleNamespace(dispatch=dispatch), None
    owner._tools = SimpleNamespace(snapshot_workspace_root=lambda: tmp_path)
    owner._deadlines = KernelExecutionDeadlineFactory()
    scope = _scope()
    request = CodeModeToolRequest(
        scope.session_id, "test", None, FrozenModelCallMemoryContext()
    )
    facts = SimpleNamespace(
        run_permission_snapshot=SimpleNamespace(
            effective_mode=PermissionMode.BYPASS_PERMISSIONS, plan_workflow_id=None
        )
    )
    prepared = PreparedResolvedToolInvocation(
        "run_agent", "run_agent", freeze_json(arguments)
    )
    call_id, turn_id = random_id("call"), random_id("turn")
    source = ToolInvocationSource(
        call_id,
        ToolInvocationSourceKind.CODE_MODE_CALL,
        turn_id,
        source_exec_attempt_id=random_id("attempt"),
        call_sequence=1,
        tool_name="run_agent",
        original_arguments=prepared.resolved_arguments,
    )
    return owner, scope, request, facts, prepared, source, refs


@pytest.mark.parametrize("block_at", [None, 0, 1])
def test_pre_hooks_keep_member_inputs_context_and_ordered_blocking(tmp_path, block_at):
    observed = []
    definition = _definition(tmp_path, HookEventType.PRE_TOOL_USE_EVENT, "noop")
    scrub = HookSecretScrubSet.capture()

    async def dispatch(envelope, **kwargs):
        observed.append(envelope)
        index = envelope.causal_ref.member_index
        return GateOutcome(
            decision=GateDecision.BLOCK if index == block_at else GateDecision.PROCEED,
            reason="member refused" if index == block_at else None,
            context_entries=(
                HookContextEntry(
                    definition, 0, index + 1, f"context for member {index}", scrub
                ),
            ),
        )

    owner, scope, request, facts, prepared, source, _ = _fixture(tmp_path, dispatch)
    owner._hook_context = HookContextOwner()
    owner._hook_context.register_scope(scope)
    original_arguments = thaw_json(prepared.resolved_arguments)

    async def run():
        token = current_tool_invocation.set(source)
        pending = []
        reservations = _batch_reservations.set(pending)
        try:
            outcome, outer = await owner._dispatch_pre_tool(
                view=object(),
                scope=scope,
                prepared=prepared,
                turn_id=source.turn_id,
                assistant_entry_id=None,
                tool_call_id=source.invocation_id,
                request=request,
                canonical_facts=facts,
            )
            expected_indices = list(range(2 if block_at is None else block_at + 1))
            assert [
                event.causal_ref.member_index for event in observed
            ] == expected_indices
            assert all(
                event.causal_ref.invocation_id == source.invocation_id
                for event in observed
            )
            assert len(
                {_causal_identity(event.causal_ref) for event in observed}
            ) == len(expected_indices)
            for index, event in enumerate(observed):
                assert (
                    event.public_input.tool_input
                    == original_arguments["members"][index]["parameters"]
                )
                assert (
                    event.public_input.input_origin
                    == ToolInvocationSourceKind.CODE_MODE_CALL.value
                )
            assert (
                outer.member_index is None
                and outer.invocation_id == source.invocation_id
            )
            assert outcome.decision is (
                GateDecision.PROCEED if block_at is None else GateDecision.BLOCK
            )
            assert outcome.reason == (None if block_at is None else "member refused")
            assert thaw_json(prepared.resolved_arguments) == original_arguments
            for reservation in pending:
                reservation.commit()
            context = owner._hook_context.freeze_for_target(
                scope_kind=scope.kind.value,
                session_id=scope.session_id,
                estimator=_Estimator(),
            )
            assert context is not None
            contributions = context.reservation.batch.contributions
            assert [item.text for item in contributions] == [
                f"context for member {index}" for index in expected_indices
            ]
            assert all(
                ("member_index", index) in item.occurrence.causal_identity
                for index, item in zip(expected_indices, contributions, strict=True)
            )
            context.reservation.retire()
        finally:
            for reservation in pending:
                reservation.retire()
            _batch_reservations.reset(reservations)
            current_tool_invocation.reset(token)

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["duplicate_work", "fifo_cycle"])
def test_invalid_batch_is_rejected_before_member_hooks(tmp_path, invalid):
    observed = []

    async def dispatch(envelope, **kwargs):
        observed.append(envelope)
        return GateOutcome()

    owner, scope, request, facts, prepared, source, refs = _fixture(
        tmp_path, dispatch, same_agent=invalid == "fifo_cycle"
    )
    arguments = thaw_json(prepared.resolved_arguments)
    if invalid == "duplicate_work":
        arguments["members"][1]["work_id"] = arguments["members"][0]["work_id"]
    else:
        arguments["members"][0]["parameters"]["depends_on"] = [refs["works"][1]]
    prepared = PreparedResolvedToolInvocation(
        "run_agent", "run_agent", freeze_json(arguments)
    )

    async def run():
        token = current_tool_invocation.set(source)
        try:
            with pytest.raises(
                ValueError, match="Work identities must be unique|waiting cycle"
            ):
                await owner._dispatch_pre_tool(
                    view=object(),
                    scope=scope,
                    prepared=prepared,
                    turn_id=source.turn_id,
                    assistant_entry_id=None,
                    tool_call_id=source.invocation_id,
                    request=request,
                    canonical_facts=facts,
                )
            assert observed == []
        finally:
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


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("first post failed"),
        asyncio.CancelledError("stop during first post"),
    ],
)
def test_post_failure_still_visits_later_member_with_exact_settlement_and_one_result(
    tmp_path, failure
):
    observed, diagnostics = [], []

    async def dispatch(envelope, **kwargs):
        observed.append(envelope)
        if envelope.causal_ref.member_index == 0:
            raise failure
        return ContextOutcome()

    owner, scope, request, facts, prepared, source, refs = _fixture(tmp_path, dispatch)
    owner._offer_operational_best_effort = diagnostics.append
    payload = {
        "meta": {"status": "SUCCESS"},
        "content": [
            {
                "type": "object",
                "value": {"agent": agent, "work": work, "work_status": "queued"},
            }
            for agent, work in zip(refs["agents"], refs["works"], strict=True)
        ],
    }
    reads = []

    def read_accepted_result(**kwargs):
        assert kwargs["session_id"] == request.session_id
        assert kwargs["result_id"] == settlement.result_id
        reads.append(kwargs)
        return {"value": payload}

    async def run_io(function, **kwargs):
        return function(**kwargs)

    owner._repository = SimpleNamespace(
        read_accepted_tool_result_observation=read_accepted_result
    )
    owner._io = SimpleNamespace(run=run_io)
    settlement = _Settlement(
        source.turn_id,
        source.invocation_id,
        random_id("result"),
        random_id("entry"),
        "SUCCESS",
        "run_agent",
        prepared.resolved_arguments,
    )

    async def run():
        source_token = current_tool_invocation.set(source)
        snapshot_token = _code_snapshot.set(SimpleNamespace(hook_epoch_nonce=object()))
        try:

            async def finish():
                await owner._dispatch_post_tool(
                    view=object(),
                    scope=scope,
                    prepared=prepared,
                    settlement=settlement,
                    request=request,
                    canonical_facts=facts,
                    continuity_scope=None,
                )

            if isinstance(failure, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError) as caught:
                    await finish()
                assert caught.value is failure
            else:
                await finish()
            assert [event.causal_ref.member_index for event in observed] == [0, 1]
            assert all(
                event.causal_ref.result_id == settlement.result_id for event in observed
            )
            for index, event in enumerate(observed):
                assert event.public_input.tool_response == {
                    "meta": payload["meta"],
                    "content": [payload["content"][index]],
                }
            assert (
                len(diagnostics) == 1
                and diagnostics[0].public_payload["member_index"] == 0
            )
            assert len(reads) == 1
        finally:
            _code_snapshot.reset(snapshot_token)
            current_tool_invocation.reset(source_token)

    asyncio.run(run())
