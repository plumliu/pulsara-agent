"""In-process ROOT/batch handshakes, using the real runner orchestration.

These tests isolate ownership, per-call routing and cancellation. Fake dry
compaction and quote results do not replace the real Host/DB adoption tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel import runner as runner_module
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    FrozenPostResponseResourceQuote,
)
from pulsara_agent.conversation_kernel.tool_contracts import FrozenImageToolResourceAllowance
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutionResult
from pulsara_agent.identity import random_id
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget


class _Closeable:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _Pending(_Closeable):
    def __init__(self, harness):
        super().__init__()
        self.harness = harness
        self.prepared_call = object()
        self.compiled_input = object()
        self.wire_input_plan = object()
        self.finalized = 0
        self.on_finalize = None

    async def finalize(self):
        self.finalized += 1
        assert asyncio.current_task() is self.harness.root_task
        assert self.harness.batch_settled
        if self.on_finalize is not None:
            return await self.on_finalize()
        return self.harness.execution


class _Harness:
    def __init__(self, monkeypatch, *, call_count=2):
        self._hook_scope = object()
        self.intent = object()
        self.compact_port, self.boundary_port = object(), object()
        self.surface = _Closeable()
        self.cut = object()
        self.request = SimpleNamespace(cut=self.cut)
        self.calls = tuple(
            SimpleNamespace(tool_call_id=random_id("call"), tool_name="read_video_frames")
            for _ in range(call_count)
        )
        quote_owner = object()
        original = tuple(
            FrozenImageToolResourceAllowance(
                call_ordinal=index,
                tool_call_id=call.tool_call_id,
                executor_binding_fingerprint="test-original-executor-binding",
                canonical_bytes=1,
                logical_bytes=2,
                wire_bytes=3,
                input_tokens=4,
                quote_owner=quote_owner,
            )
            for index, call in enumerate(self.calls)
        )
        budgets = tuple(ToolResultPageBudget(call.tool_call_id) for call in self.calls)
        self.output_quote = FrozenPostResponseResourceQuote(
            current_canonical_expanded_bytes=0,
            actual_assistant_canonical_bytes=0,
            bounded_followup_canonical_bytes=0,
            canonical_upper_after=0,
            current_epoch_logical_bytes=0,
            actual_assistant_logical_bytes=0,
            bounded_followup_logical_bytes=0,
            logical_upper_after=0,
            current_canonical_items=0,
            bounded_followup_items=call_count,
            item_upper_after=call_count + 1,
            image_call_allowances=original,
            result_page_budgets=budgets,
        )
        fresh_owner = object()
        self.fresh = {
            allowance.tool_call_id: (
                replace(
                    allowance,
                    canonical_bytes=1000 + index,
                    logical_bytes=2000 + index,
                    wire_bytes=3000 + index,
                    input_tokens=4000 + index,
                    quote_owner=fresh_owner,
                ),
                ToolResultPageBudget(allowance.tool_call_id),
            )
            for index, allowance in enumerate(original)
        }
        self.prepare_calls = []
        self.quote_calls = []
        self.root_task = self.batch_task = None
        self.batch_settled = False
        self.pending = _Pending(self)
        self.successor = _Closeable()
        self.execution = SimpleNamespace(successor_dispatch=self.successor)
        self.on_prepare = self.on_quote = None
        self.batch_body = None
        self.compaction = SimpleNamespace(prepare_pending_tool_batch=self._prepare)
        self._tool_batches = SimpleNamespace(execute=self._execute)
        monkeypatch.setattr(runner_module, "refresh_tool_result_page_resources", self._quote)

    def _compact_session_start_port(self, intent):
        assert intent is self.intent
        return self.compact_port

    def _compact_session_start_boundary_port(self, intent):
        assert intent is self.intent
        return self.boundary_port

    async def _prepare(self, **kwargs):
        assert asyncio.current_task() is self.root_task
        assert kwargs["closed_source_cut"] is self.cut
        assert kwargs["ordered_tool_call_ids"] == tuple(call.tool_call_id for call in self.calls)
        assert kwargs["session_start_compact_port"] is self.compact_port
        assert kwargs["session_start_boundary_port"] is self.boundary_port
        self.prepare_calls.append(kwargs)
        if self.on_prepare is not None:
            return await self.on_prepare()
        return self.pending

    def _quote(self, **kwargs):
        assert asyncio.current_task() is self.root_task
        assert kwargs["original_quote"] is self.output_quote
        assert kwargs["prepared_call"] is self.pending.prepared_call
        assert kwargs["compiled_input"] is self.pending.compiled_input
        assert kwargs["wire_input_plan"] is self.pending.wire_input_plan
        self.quote_calls.append(kwargs)
        if self.on_quote is not None:
            return self.on_quote()
        # Different tuple orders make accidental positional pairing observable.
        pairs = tuple(self.fresh.values())
        return tuple(pair[0] for pair in reversed(pairs)), tuple(pair[1] for pair in pairs)

    async def _execute(self, **kwargs):
        self.batch_task = asyncio.current_task()
        assert self.batch_task is not self.root_task
        assert kwargs["surface_borrow"] is self.surface
        assert kwargs["image_call_allowances"] is self.output_quote.image_call_allowances
        assert kwargs["result_page_budgets"] is self.output_quote.result_page_budgets
        try:
            await self.batch_body(kwargs["image_resource_refresh"])
            self.batch_settled = True
            return ToolBatchExecutionResult(len(self.calls))
        finally:
            self.surface.close()

    async def run(self):
        self.root_task = asyncio.current_task()
        return await ConversationKernelRunner._execute_tool_batch_with_page_recovery(
            self,
            turn_id=random_id("turn"),
            assistant_entry_id=random_id("entry"),
            calls=self.calls,
            canonical_facts=object(),
            canonical_identity=object(),
            request=self.request,
            agent_invocation_snapshot=None,
            continuity_scope=object(),
            surface_borrow=self.surface,
            output_quote=self.output_quote,
            intent=self.intent,
            model_call_index=2,
            inherited_memory_use_policy=object(),
        )


def test_concurrent_calls_get_their_own_fresh_share_from_one_root_preparation(monkeypatch):
    async def exercise():
        harness = _Harness(monkeypatch)
        both_queued = asyncio.Event()
        entered = 0
        observed = {}

        async def tool(call):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_queued.set()
            fresh = await refresh(call.tool_call_id)
            assert fresh == harness.fresh[call.tool_call_id]
            assert fresh[0] is harness.fresh[call.tool_call_id][0]
            assert fresh[1] is harness.fresh[call.tool_call_id][1]
            assert fresh[0].tool_call_id == fresh[1].tool_call_id == call.tool_call_id
            observed[call.tool_call_id] = fresh
            assert await refresh(call.tool_call_id) == fresh

        async def prepare():
            await both_queued.wait()
            return harness.pending

        async def batch(callback):
            nonlocal refresh
            refresh = callback
            await asyncio.gather(*(tool(call) for call in harness.calls))

        refresh = None
        harness.on_prepare, harness.batch_body = prepare, batch
        batch_result, execution = await harness.run()
        assert batch_result.tool_call_count == 2
        assert execution is harness.execution
        assert len(observed) == 2 and observed[harness.calls[0].tool_call_id] != observed[harness.calls[1].tool_call_id]
        assert len(harness.prepare_calls) == len(harness.quote_calls) == 1
        assert harness.pending.finalized == harness.pending.closed == 1
        assert harness.surface.closed == 1 and harness.batch_task.done()
        # A successful handoff retains its successor for the ROOT caller.
        assert harness.successor.closed == 0
        execution.successor_dispatch.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["prepare", "quote", "finalize"])
def test_root_value_error_is_not_delivered_as_a_media_argument_failure(monkeypatch, stage):
    async def exercise():
        harness = _Harness(monkeypatch, call_count=1)
        failure = ValueError(f"ROOT {stage} invariant failed")
        media_argument_errors = []

        async def fail_async():
            raise failure

        def fail_quote():
            raise failure

        async def batch(refresh):
            try:
                await refresh(harness.calls[0].tool_call_id)
            except ValueError as error:
                media_argument_errors.append(error)
                return

        harness.batch_body = batch
        if stage == "prepare":
            harness.on_prepare = fail_async
        elif stage == "quote":
            harness.on_quote = fail_quote
        else:
            harness.pending.on_finalize = fail_async
        with pytest.raises(ValueError) as exc:
            await harness.run()
        assert exc.value is failure
        assert media_argument_errors == []
        assert harness.batch_task.done() and harness.surface.closed == 1
        assert len(harness.prepare_calls) == 1
        assert harness.pending.closed == (0 if stage == "prepare" else 1)
        assert harness.pending.finalized == (1 if stage == "finalize" else 0)
        assert harness.successor.closed == 0  # No execution was handed out.

    asyncio.run(exercise())


def test_repeated_root_cancellation_drains_batch_before_closing_pending(monkeypatch):
    async def exercise():
        harness = _Harness(monkeypatch, call_count=1)
        running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        child_cleanup_completed = False
        child_cleanup_cancelled = False
        baseline = set(asyncio.all_tasks())

        async def batch(refresh):
            nonlocal child_cleanup_completed, child_cleanup_cancelled
            await refresh(harness.calls[0].tool_call_id)
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    child_cleanup_cancelled = True
                    raise
                child_cleanup_completed = True

        harness.batch_body = batch
        root = asyncio.create_task(harness.run())
        await asyncio.wait_for(running.wait(), 5)
        root.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        root.cancel()
        await asyncio.sleep(0)
        root.cancel()
        await asyncio.sleep(0)
        assert not root.done()
        assert harness.pending.closed == 0
        assert not child_cleanup_cancelled
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await root
        assert child_cleanup_completed and not child_cleanup_cancelled
        assert harness.pending.closed == 1 and harness.pending.finalized == 0
        assert harness.batch_task.done() and harness.surface.closed == 1
        assert harness.successor.closed == 0  # No successor was produced.
        assert set(asyncio.all_tasks()) <= baseline

    asyncio.run(exercise())


def test_cancellation_after_finalize_closes_unreturned_successor(monkeypatch):
    async def exercise():
        harness = _Harness(monkeypatch, call_count=1)

        async def batch(refresh):
            assert await refresh(harness.calls[0].tool_call_id) == harness.fresh[harness.calls[0].tool_call_id]

        async def finalize():
            # The task has a result, but ROOT's shielded drain still yields to
            # the loop before the prepared successor can be returned/adopted.
            asyncio.get_running_loop().call_soon(harness.root_task.cancel)
            return harness.execution

        harness.batch_body = batch
        harness.pending.on_finalize = finalize
        root = asyncio.create_task(harness.run())
        with pytest.raises(asyncio.CancelledError):
            await root
        assert harness.batch_settled
        assert harness.pending.finalized == harness.pending.closed == 1
        assert harness.successor.closed == 1
        assert harness.batch_task.done() and harness.surface.closed == 1

    asyncio.run(exercise())
