"""Current execution retry facts never become a second execution authority."""

import asyncio
from threading import Lock
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.direct_model import (
    PreparedKernelModelExecution, _PreparedExecutionState,
)
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.identity import random_id
from pulsara_agent.ports.provider_stream import (
    ProviderRetryUpdate, ProviderRetryPhase, ProviderRetryReason, ProviderGenerationRetry,
)


def execution_slot():
    execution = PreparedKernelModelExecution.__new__(PreparedKernelModelExecution)
    execution._lock = Lock()
    execution._state = _PreparedExecutionState.STREAMING
    execution._retry_update = None
    execution._retry_deadline = None
    execution._draft_identity = None
    execution.request = SimpleNamespace(
        session_id=random_id('session'), turn_id=random_id('turn'),
        prepared_call=SimpleNamespace(call=SimpleNamespace(resolved_model_call_id='resolved:retry')),
        compiled_input=SimpleNamespace(canonical_input_identity=SimpleNamespace(
            conversation_scope_kind=SimpleNamespace(value='ROOT'))),
    )
    return execution


def test_waiting_deadline_reconnecting_and_closed_callbacks(monkeypatch):
    now = [10.0]
    monkeypatch.setattr('pulsara_agent.conversation_kernel.direct_model.monotonic', lambda: now[0])
    execution = execution_slot()
    draft = random_id('entry')
    execution.set_draft_identity(draft)
    execution._observe_retry(ProviderRetryUpdate(2, 1, 5, ProviderRetryPhase.WAITING,
        ProviderRetryReason.CONNECTION_RECOVERY, 8, True))
    now[0] += 2.25
    current = execution.provider_retry_observation()
    assert current['draft_id'] == draft
    assert current['remaining_seconds'] == 5.75
    assert current['reason'] == 'connection_recovery'
    execution._observe_retry(ProviderRetryUpdate(2, 1, 5, ProviderRetryPhase.RECONNECTING,
        ProviderRetryReason.CONNECTION_RECOVERY, 0, True))
    assert execution.provider_retry_observation()['remaining_seconds'] == 0
    execution.clear_retry_observation()
    execution._state = _PreparedExecutionState.PHYSICALLY_CLOSED
    execution._observe_retry(ProviderRetryUpdate(3, 2, 5, ProviderRetryPhase.WAITING,
        ProviderRetryReason.PROVIDER_RECOVERY, 8, True))
    assert execution.provider_retry_observation() is None


def test_generation_retry_cancellation_clears_exact_current_execution():
    async def run():
        runner = ConversationKernelRunner.__new__(ConversationKernelRunner)
        runner._active_execution = None
        runner._live_bus = LiveAgentEventBus()
        execution = execution_slot()
        original = random_id('entry')
        async def stream(permit):
            assert runner._active_execution is execution
            assert execution._draft_identity == original
            yield ProviderGenerationRetry(ProviderRetryReason.CONNECTION_RECOVERY)
            assert execution._draft_identity != original
            raise asyncio.CancelledError
        execution.open_once = stream
        with pytest.raises(asyncio.CancelledError):
            await runner._collect_model(execution.request, execution=execution,
                permit=None, proposed_entry_id=original)
        assert runner._active_execution is None
        assert execution.provider_retry_observation() is None
    asyncio.run(run())


def test_registration_failure_cannot_leave_current_execution():
    async def run():
        runner = ConversationKernelRunner.__new__(ConversationKernelRunner)
        runner._active_execution = None
        runner._live_bus = LiveAgentEventBus()
        execution = execution_slot()
        def fail(identity):
            raise RuntimeError('execution admission failed')
        execution.set_draft_identity = fail
        with pytest.raises(RuntimeError, match='admission failed'):
            await runner._collect_model(execution.request, execution=execution,
                permit=None, proposed_entry_id=random_id('entry'))
        assert runner._active_execution is None
    asyncio.run(run())


def test_final_public_detail_is_scrubbed_after_retry_summary_is_composed():
    from pulsara_agent.llm.provider_sanitization import sanitize_provider_failure
    from pulsara_agent.primitives.model_call import ProviderRetrySummaryFact
    secret = 'private-value-from-frozen-connection'
    summary = ProviderRetrySummaryFact(
        enabled=True, final_attempt=1, max_attempts=6, retry_count=0,
        exhausted=False, has_semantic_output=True, skipped_reason='unsafe_to_replay',
        final_reason=secret, final_status_code=None, retry_after_exceeded=False, attempts=(),
    )
    error = sanitize_provider_failure(message='Concrete cause remains inspectable',
        code_hint='provider_transport_error', retry_summary=summary)
    execution = execution_slot()
    seen = []
    def scrub(text):
        seen.append(text)
        return text.replace(secret, '[redacted]')
    execution._bind_provider_error_scrubber(scrub)
    failed = execution._provider_failure(error)
    assert len(seen) == 1 and secret in seen[0]
    assert secret not in failed.public_detail
    assert 'Concrete cause remains inspectable' in failed.public_detail
    assert 'retries=0/5' in failed.public_detail
    assert 'decision=unsafe_to_replay' in failed.public_detail


@pytest.mark.parametrize('operation', ['stop', 'close'])
def test_provider_cancellation_precedes_blocked_cell_join(operation):
    from pulsara_agent.conversation_kernel.cancellation import ActiveTurnCancellationIntent
    from pulsara_agent.conversation_kernel.host import KernelHostSession
    from pulsara_agent.model_input.contracts import ModelInputScopeKind

    async def run():
        turn_id = random_id('turn')
        cancelled = asyncio.Event()
        provider_settlement = asyncio.Event()
        joining_cells = asyncio.Event()
        blocked_cell_join = asyncio.Event()
        actions = []
        async def provider():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                # A physical response/tool owner may still need to settle.
                await provider_settlement.wait()
                raise
        task = asyncio.create_task(provider())
        await asyncio.sleep(0)
        async def cell_join(*args):
            actions.append('join')
            joining_cells.set()
            await blocked_cell_join.wait()
        host = SimpleNamespace(
            _lock=asyncio.Lock(), _close_async_lock=asyncio.Lock(), _closed=False,
            _active_task=task, _active_turn_id=turn_id,
            _active_cancellation_intent=ActiveTurnCancellationIntent(turn_id, ModelInputScopeKind.ROOT),
            _external_new_turn_cancellation_intent=None,
            _presentation_notices=[], _queued_prompt_hooks={},
            _queue_wake=asyncio.Event(), _cell_output_wake=asyncio.Event(),
            _session_scope_kind=ModelInputScopeKind.ROOT,
            _tools=SimpleNamespace(todo_owner=SimpleNamespace(mark_closing=lambda **kwargs: None)),
            _code_mode=SimpleNamespace(
                mark_turn_stopped=lambda value: actions.append(('mark_turn', value)),
                mark_all_stopped=lambda: actions.append('mark_all'),
                stop_turn=cell_join, aclose=cell_join),
        )
        if operation == 'stop':
            host._existing_control_outcome_locked = lambda request: None
            host._validate_new_control_request_locked = lambda request: None
            host._retire_done_active_root_locked = lambda: None
            pending = object()
            host._pending_control_outcome = lambda request: pending
            attempts = []
            def install(request, outcome):
                attempt = SimpleNamespace(request=request, outcome=outcome, task=None)
                attempts.append(attempt)
                return attempt
            host._install_control_attempt_locked = install
            host._execute_stop_turn_attempt = lambda attempt, owner: KernelHostSession._execute_stop_turn_attempt(host, attempt, owner)
            result = await KernelHostSession.request_stop_turn(host,
                command_id=random_id('command'), admission_deadline_ms=1000,
                expected_session_id=random_id('session'), expected_host_session_id=random_id('host'),
                target_turn_id=turn_id)
            assert result is pending
            # Acceptance itself cancels; a separately scheduled control owner
            # need not run first to make Stop effective.
            assert task.cancelling() == 1
            owner = attempts[0].task
            assert actions[0] == ('mark_turn', turn_id)
        else:
            owner = asyncio.create_task(KernelHostSession.aclose(host, deadline_monotonic=999999999999))
        try:
            await asyncio.wait_for(joining_cells.wait(), 1)
            await asyncio.wait_for(cancelled.wait(), 1)
            assert actions[0] == (('mark_turn', turn_id) if operation == 'stop' else 'mark_all')
            assert actions[-1] == 'join'
            assert task.cancelling() == 1
            assert not owner.done() and not task.done()
        finally:
            owner.cancel()
            provider_settlement.set()
            await asyncio.gather(owner, task, return_exceptions=True)
    asyncio.run(run())
