"""HTTP 413 runner recovery and the physical-close admission boundary."""

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pulsara_agent.conversation_kernel.cancellation import (
    ActiveTurnCancellationIntent,
    ForegroundCancellationCause,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    CompactionOutcome,
    CompactionTrigger,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import CompactionExecutionResult
from pulsara_agent.conversation_kernel.direct_model import (
    KernelProviderModelExecutionFailed,
    PreparedKernelModelCall,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
from pulsara_agent.conversation_kernel.provider_dispatch import PreparedProviderDispatch
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.identity import random_id
from pulsara_agent.llm.provider_sanitization import sanitize_provider_failure
from pulsara_agent.llm.request import FrozenProviderWireInputQuote
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderNormalizedTerminalKind,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.model_call import ProviderRetrySummaryFact
from tests.test_stop_chain_provider_cleanup import ControlledTransport, collect, setup_execution


def failure(status=413, *, semantic=False, reason=None):
    reason = reason or f"http_{status}_non_retryable"
    summary = ProviderRetrySummaryFact(
        enabled=True, final_attempt=1, max_attempts=3, retry_count=0,
        exhausted=False, has_semantic_output=semantic, skipped_reason=reason,
        final_reason=reason, final_status_code=status, retry_after_exceeded=False,
        attempts=(),
    )
    fact = sanitize_provider_failure(
        message=f"concrete HTTP {status} body", code_hint="provider_invalid_request",
        retry_summary=summary,
    )
    return KernelProviderModelExecutionFailed(fact, f"HTTP {status}: concrete body")


def quote(byte_count):
    return FrozenProviderWireInputQuote(
        wire_api="openai_chat_completions", estimator_fingerprint="sha256:test",
        effective_input_budget_tokens=1000, generic_wire_estimated_input_tokens=10,
        generic_wire_visual_image_tokens=0, replaced_generic_wire_estimated_tokens=0,
        replay_wire_estimated_tokens=0, raw_final_wire_estimated_input_tokens=10,
        final_wire_visual_image_tokens=0, final_wire_utf8_bytes=byte_count,
        budget_input_tokens=10,
    )


class Dispatch(PreparedProviderDispatch):
    """Control preparation resources; the production runner owns their order."""

    def __init__(self, fixture, label, byte_count):
        self.label = label
        self.fixture = fixture
        self.quote = quote(byte_count)
        self.finished = False
        self.installed_provider_open = None
        self.prepared_call = object.__new__(PreparedKernelModelCall)
        identity = SimpleNamespace(
            conversation_scope_kind=fixture.scope, session_id=fixture.session_id,
            turn_id=fixture.turn_id,
        )
        self.canonical_facts = SimpleNamespace(
            canonical_input=SimpleNamespace(identity=identity),
        )
        self.planning = SimpleNamespace(scope=fixture.permit.scope)
        self.memory_context = SimpleNamespace(memory_use_policy=MemoryUsePolicy.ALL_DISABLED_BY_USER)
        self.append_result = SimpleNamespace(compiled_input=SimpleNamespace(
            canonical_input_identity=identity,
        ))
        self.surface = SimpleNamespace(close=Mock(side_effect=self.close_surface))

    @property
    def owns_execution_authority(self):
        return not self.finished

    def finish_model_operation(self):
        assert not self.finished
        self.fixture.events.append((self.label, "handle_closed"))
        self.finished = True
        return self.surface

    def retire_hook_context_reservation(self):
        pass

    def close_surface(self):
        self.fixture.events.append((self.label, "surface_closed"))

    def close(self, **kwargs):
        if not self.finished:
            self.finish_model_operation().close()


class RunnerFixture:
    def __init__(self, responses, *, scope=ModelInputScopeKind.SUBAGENT, allowed=True):
        self.scope = scope
        self.session_id, self.turn_id = random_id("session"), random_id("turn")
        self.events, self.installed = [], []
        self.permit = SimpleNamespace(
            scope=SimpleNamespace(session_id=self.session_id, scope_kind=scope),
            epoch_nonce="epoch", epoch_revision=1,
        )
        self.intent = ActiveTurnCancellationIntent(self.turn_id, scope)
        self.responses = iter(responses)
        self.initial = Dispatch(self, "initial", 1000)
        self.successors = [Dispatch(self, "successor", 500)]
        self.normal_dispatches = iter([self.initial])
        self.finalize = lambda dispatch: dispatch
        self.physical_state = "PHYSICALLY_CLOSED"
        runner = self.runner = ConversationKernelRunner.__new__(ConversationKernelRunner)
        runner._root_session_id = None if scope is ModelInputScopeKind.ROOT else random_id("session")
        runner._session_scope_kind = scope
        runner._root_memory_use_policy = MemoryUsePolicy.ALL_DISABLED_BY_USER
        runner._writer_lease = SimpleNamespace(guard=SimpleNamespace(session_id=self.session_id))
        for name in (
            "_workspace_gate", "_root_control_preparation_barrier", "_before_provider_preparation",
            "_provider_input_installed_observer", "_provider_input_failure_observer", "_extensions",
            "_root_control_completion_fence", "_root_control_completion_settlement",
            "_hook_dispatcher", "_hook_context_owner", "_hook_scope", "_presentation_notice_sink",
        ):
            setattr(runner, name, None)
        runner._canonical_deadline = runner._planning_deadline = lambda: monotonic() + 30
        runner._repository = SimpleNamespace(require_provider_safe_turn=Mock())

        async def io(operation, *args, **kwargs):
            return operation(*args, **kwargs)

        runner._io = SimpleNamespace(run=io)
        runner._turn_admission = SimpleNamespace(interrupt_turn=AsyncMock())
        runner._session_start_boundary = SimpleNamespace(has_pending=False)
        runner._compact_session_start_port = Mock(return_value=None)
        runner._compact_session_start_boundary_port = Mock(return_value=None)
        runner._offer_compile_observation = Mock()
        runner._resolved_workspace_id = AsyncMock(return_value=random_id("workspace"))
        runner._canonical_blocks = AsyncMock(return_value=())
        runner._content = AsyncMock(return_value=object())
        runner._quote_post_response_resources = Mock(return_value=SimpleNamespace(image_call_allowances=()))
        runner._require_post_response_resources = Mock()
        runner._tools = SimpleNamespace(prepare_visualizations=AsyncMock(return_value=()))
        runner._assistant_settlements = SimpleNamespace(settle=AsyncMock(side_effect=self.settle))
        runner._live_bus = LiveAgentEventBus()
        runner._provider_dispatch = SimpleNamespace(
            wait_for_cold_epoch_readiness=AsyncMock(),
            prepare=AsyncMock(side_effect=lambda **kwargs: next(self.normal_dispatches)),
            finalize_compaction_input=AsyncMock(side_effect=self.finalize_input),
            bind_prepared_executable_wire_input=Mock(side_effect=lambda **kwargs: SimpleNamespace(
                quote=kwargs["owner_dispatch"].quote,
            )),
            install_provider_open=AsyncMock(side_effect=self.install),
        )
        admission = SimpleNamespace(
            prepared_target=object(), take_handle=Mock(return_value=object()),
            preflight=object(), close=Mock(),
        )
        runner.compaction = SimpleNamespace(
            prepare_precompile_admission=AsyncMock(return_value=admission),
            precompile_needed=Mock(return_value=False),
            context_allowance_change_pending=Mock(return_value=False),
            measure_dispatch_wire=AsyncMock(side_effect=lambda dispatch, **kwargs: self.decision(dispatch)),
            wire_decision_crosses_automatic_threshold=Mock(return_value=False),
            automatic_allowed=Mock(return_value=allowed),
            execute_active=AsyncMock(side_effect=self.compact),
        )
        runner._collect_model = AsyncMock(side_effect=self.collect)
        self.accepted = []

    def decision(self, dispatch):
        return SimpleNamespace(quote=dispatch.quote)

    async def finalize_input(self, dispatch, decision, **kwargs):
        dispatch = self.finalize(dispatch)
        return dispatch, self.decision(dispatch)

    async def install(self, **kwargs):
        dispatch = kwargs["dispatch"]
        self.installed.append(dispatch)
        return SimpleNamespace(
            request=SimpleNamespace(
                session_id=self.session_id, turn_id=self.turn_id,
                prepared_call=dispatch.prepared_call,
                compiled_input=dispatch.append_result.compiled_input,
                wire_input_plan=SimpleNamespace(quote=dispatch.quote),
                cut=SimpleNamespace(session_id=self.session_id),
            ),
            execution=SimpleNamespace(state=self.physical_state, scrub_provider_detail=lambda text: text),
            permit=self.permit,
        )

    async def collect(self, request, **kwargs):
        item = next(self.responses)
        self.events.append(("business", item if isinstance(item, str) else "failed"))
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            completed=SimpleNamespace(draft_identity=random_id("entry"), blocks=(), public_text=item),
            provider_replay=None,
        )

    async def settle(self, settlement):
        self.accepted.append(settlement)
        return SimpleNamespace(
            entry_id=settlement.entry_id, pending_steer_at_settlement=False,
            turn_completed=len(self.accepted) >= self.finish_after,
        )

    async def compact(self, **kwargs):
        self.events.append(("recovery", "started"))
        assert self.initial.finished
        assert self.initial.surface.close.call_count == 1
        assert not self.runner._turn_admission.interrupt_turn.called
        successor = self.successors.pop(0)
        return CompactionExecutionResult(
            CompactionOutcome(CompactionDisposition.COMPACTED, self.turn_id, None, None, "COMPACTED"),
            successor_dispatch=successor, successor_wire_decision=self.decision(successor),
        )

    async def run(self, *, first_history=None, finish_after=1):
        self.finish_after = finish_after
        started = None if first_history is None else SimpleNamespace(
            session_id=self.session_id, turn_id=self.turn_id,
            predecessor_through_sequence=first_history,
        )
        return await self.runner.run_accepted_turn(
            self.turn_id, cancellation_intent=self.intent, started_input_group=started,
        )


def test_http413_recovery_retires_original_resources_and_keeps_first_call_ceiling():
    fixture = RunnerFixture([failure(), "accepted"])
    result = asyncio.run(fixture.run(first_history=7))
    assert result.final_text == "accepted"
    assert fixture.events[:4] == [
        ("business", "failed"), ("initial", "handle_closed"),
        ("initial", "surface_closed"), ("recovery", "started"),
    ]
    call = fixture.runner.compaction.execute_active.call_args.kwargs
    assert call["trigger"] is CompactionTrigger.HTTP_413_RECOVERY
    assert call["first_call_history_through_sequence"] == 7
    assert call["failed_wire_quote"] is fixture.initial.quote
    assert len(fixture.accepted) == 1
    fixture.runner._turn_admission.interrupt_turn.assert_not_called()


@pytest.mark.parametrize("scope", [ModelInputScopeKind.ROOT, ModelInputScopeKind.SUBAGENT])
def test_retry_http413_does_not_reopen_recovery(scope):
    fixture = RunnerFixture([failure(), failure()], scope=scope)
    with pytest.raises(KernelProviderModelExecutionFailed, match="provider model execution failed") as caught:
        asyncio.run(fixture.run())
    assert "压缩后重试仍失败" in caught.value.public_detail
    assert fixture.runner.compaction.execute_active.await_count == 1
    assert len(fixture.installed) == 2 and not fixture.accepted
    fixture.runner._turn_admission.interrupt_turn.assert_awaited_once()


@pytest.mark.parametrize("error,state,allowed", [
    (failure(400), "PHYSICALLY_CLOSED", True),
    (failure(413, semantic=True), "PHYSICALLY_CLOSED", True),
    (failure(), "STREAMING", True),
    (failure(reason="physical_close_unconfirmed"), "PHYSICALLY_CLOSED", True),
    (failure(), "PHYSICALLY_CLOSED", False),
])
def test_unqualified_failure_never_starts_compaction(error, state, allowed):
    fixture = RunnerFixture([error], allowed=allowed)
    fixture.physical_state = state
    with pytest.raises(ProviderModelExecutionFailed):
        asyncio.run(fixture.run())
    fixture.runner.compaction.execute_active.assert_not_awaited()
    assert len(fixture.installed) == 1


@pytest.mark.parametrize("final_bytes", [1000, 1200])
def test_steer_enlargement_after_adoption_is_rejected_before_install(final_bytes):
    fixture = RunnerFixture([failure()])

    def larger(dispatch):
        dispatch.quote = replace(dispatch.quote, final_wire_utf8_bytes=final_bytes)
        return dispatch

    fixture.finalize = larger
    with pytest.raises(KernelProviderModelExecutionFailed) as caught:
        asyncio.run(fixture.run())
    assert "最终请求没有减少" in caught.value.public_detail
    assert len(fixture.installed) == 1
    assert fixture.runner.compaction.execute_active.await_count == 1


def test_retry_other_error_keeps_its_original_classification():
    other = failure(429)
    fixture = RunnerFixture([failure(), other])
    with pytest.raises(ProviderModelExecutionFailed) as caught:
        asyncio.run(fixture.run())
    assert caught.value is other
    assert caught.value.public_detail == "HTTP 429: concrete body"
    assert fixture.runner.compaction.execute_active.await_count == 1


def test_summary_http413_is_a_failed_recovery_without_another_summary():
    fixture = RunnerFixture([failure()])
    fixture.runner.compaction.execute_active.side_effect = failure()
    with pytest.raises(KernelProviderModelExecutionFailed) as caught:
        asyncio.run(fixture.run())
    assert "压缩恢复未完成" in caught.value.public_detail
    assert fixture.runner.compaction.execute_active.await_count == 1
    assert len(fixture.installed) == 1


def test_assistant_acceptance_allows_next_independent_business_recovery():
    fixture = RunnerFixture([failure(), "accepted one", failure(), "accepted two"])
    subsequent = Dispatch(fixture, "next business", 1000)
    fixture.normal_dispatches = iter([fixture.initial, subsequent])
    fixture.successors.append(Dispatch(fixture, "next successor", 400))
    result = asyncio.run(fixture.run(finish_after=2, first_history=9))
    assert result.final_text == "accepted two"
    calls = fixture.runner.compaction.execute_active.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["first_call_history_through_sequence"] == 9
    assert calls[1].kwargs["first_call_history_through_sequence"] is None
    assert calls[1].kwargs["failed_wire_quote"] is subsequent.quote


def test_stop_after_failed_dispatch_cleanup_does_not_start_summary():
    fixture = RunnerFixture([failure()])
    original = fixture.initial.close_surface

    def stop():
        original()
        fixture.intent.install_cause(ForegroundCancellationCause.USER_REQUEST)

    fixture.initial.surface.close.side_effect = stop
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fixture.run())
    fixture.runner.compaction.execute_active.assert_not_awaited()


def test_stop_after_adoption_retires_successor_without_business_retry():
    fixture = RunnerFixture([failure()])
    successor = fixture.successors[0]

    async def compact_and_stop(**kwargs):
        result = await fixture.compact(**kwargs)
        fixture.intent.install_cause(ForegroundCancellationCause.HOST_SESSION_CLOSE)
        return result

    fixture.runner.compaction.execute_active.side_effect = compact_and_stop
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fixture.run())
    assert successor.finished
    successor.surface.close.assert_called_once()
    assert len(fixture.installed) == 1


@pytest.mark.parametrize("borrow_fails", [False, True])
def test_http413_physical_state_includes_borrow_release(monkeypatch, borrow_fails):
    async def run():
        rejected = failure()
        transport = ControlledTransport()
        transport.items = iter([ProviderStreamTerminal(
            ProviderNormalizedTerminalKind.PROVIDER_ERROR,
            TransportUsageReport(usage_status="missing", usage=None), error=rejected.error,
        )])
        transport.release_close.set()
        runner, execution, permit, borrowed, usage = setup_execution(monkeypatch, transport)

        def close():
            assert execution.state != "PHYSICALLY_CLOSED"
            if borrow_fails:
                raise RuntimeError("borrow release failed")

        borrowed.close.side_effect = close
        with pytest.raises(ProviderModelExecutionFailed) as caught:
            await collect(runner, execution, permit)
        assert caught.value.error.retry_summary.final_status_code == 413
        assert (execution.state == "PHYSICALLY_CLOSED") is not borrow_fails
        borrowed.close.assert_called_once()
        if borrow_fails:
            assert "borrow release failed" in str(caught.value.__cause__)

    asyncio.run(run())


def test_cancellation_during_http413_cleanup_wins_over_recovery(monkeypatch):
    async def run():
        rejected = failure()
        transport = ControlledTransport()
        transport.items = iter([ProviderStreamTerminal(
            ProviderNormalizedTerminalKind.PROVIDER_ERROR,
            TransportUsageReport(usage_status="missing", usage=None), error=rejected.error,
        )])
        runner, execution, permit, borrowed, usage = setup_execution(monkeypatch, transport)
        task = asyncio.create_task(collect(runner, execution, permit))
        await asyncio.wait_for(transport.closing.wait(), 2)
        task.cancel()
        transport.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        borrowed.close.assert_called_once()
        assert runner._active_execution is None

    asyncio.run(run())


@pytest.mark.postgres
@pytest.mark.parametrize("api", ["openai_chat_completions", "openai_responses"])
def test_real_http413_large_steer_after_full_is_kept_but_never_dispatched(
    tmp_path, monkeypatch, isolated_migrated_postgres_database, api,
):
    """FULL remains adopted; the common safe point accepts the steer exactly once."""
    from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.conversation_kernel.prompt_storage import _read_body
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.llm.retry import LLMRetryConfig
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from psycopg.rows import dict_row
    from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
    from tests.support.model_config import test_model_binding, test_model_runtime
    from tests.test_http413_compaction_sdk import ByteGate, _CURRENT, _rows, byte_gated_server

    async def scenario():
        import pulsara_agent.conversation_kernel.host as kernel_host

        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
        monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        gate = ByteGate(evidence_label="large-steer-after-full")
        steer = "LARGE_STEER_AFTER_FULL: " + "preserve this accepted instruction " * 12_000
        observed = []
        with byte_gated_server(api, gate) as endpoint:
            runtime = test_model_runtime(
                base_url=endpoint, wire_api=api,
                postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
                retry=LLMRetryConfig(base_delay_seconds=0.001, max_delay_seconds=0.001, jitter_ratio=0),
            )
            core = KernelHostCore.production(model_runtime=runtime)
            root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
            try:
                await root.update_model_call_binding(test_model_binding(runtime))
                root._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=0.99)
                _seed_history(root, segments=8, repetitions=300)
                original = root._runner._provider_dispatch.finalize_compaction_input

                async def finalize_after_steer(base, decision, **kwargs):
                    assert not observed
                    assert len(_rows(root,
                        "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                        (root.session_id,))) == 1
                    receipt = await root.steer_active_turn(
                        command_id=random_id("command"), content=PromptContent.text(steer),
                        target_turn_id=base.cut.turn_id,
                    )
                    assert receipt.status == "PENDING"
                    prepared, actual = await original(base, decision, **kwargs)
                    observed.append((actual.quote, prepared.accepted_steers))
                    return prepared, actual

                monkeypatch.setattr(root._runner._provider_dispatch,
                    "finalize_compaction_input", finalize_after_steer)
                with pytest.raises(KernelProviderModelExecutionFailed) as caught:
                    await root.run_turn(PromptContent.text(_CURRENT), command_id=random_id("command"),
                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                assert "最终请求没有减少" in caught.value.public_detail
                assert len(observed) == 1
                assert observed[0][1] is not None
                failed = [request for request in gate.requests if request.status == 413]
                summaries = [request for request in gate.requests if request.summary]
                assert len(failed) == len(summaries) == 1
                assert len(gate.requests) == 2
                assert not any(request.condensed for request in gate.requests)
                assert observed[0][0].final_wire_utf8_bytes >= len(failed[0].raw)
                workspace_id = await root._runner._resolved_workspace_id()
                with root.repository.connection_provider.connection(
                    lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
                    deadline_monotonic=monotonic() + 30,
                ) as connection:
                    rows = connection.execute(
                        "SELECT * FROM pulsara_v3.transcript_entries "
                        "WHERE session_id=%s AND input_origin='HUMAN_STEER'",
                        (root.session_id,),
                    ).fetchall()
                    assert len(rows) == 1
                    body = _read_body(connection, row=rows[0], workspace_id=workspace_id,
                        expected_media_type=rows[0]["content_media_type"],
                        expected_codec=rows[0]["content_codec"])
                    assert steer.encode() in body
                assert len(_rows(root,
                    "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                    (root.session_id,))) == 1
                assert root._runner._active_execution is None
            finally:
                await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("api", ["openai_chat_completions", "openai_responses"])
def test_real_sdk_http413_client_close_failure_never_starts_recovery(
    tmp_path, monkeypatch, isolated_migrated_postgres_database, api,
):
    import openai
    from tests.test_http413_compaction_sdk import ByteGate, _run_host

    original = openai.AsyncOpenAI.close
    closed = []

    async def close(client):
        await original(client)
        closed.append(client)
        raise RuntimeError("controlled SDK close remains unconfirmed")

    monkeypatch.setattr(openai.AsyncOpenAI, "close", close)
    gate = ByteGate(evidence_label="sdk-close-unconfirmed")
    result, error, fresh, snapshots = asyncio.run(_run_host(
        tmp_path, monkeypatch, isolated_migrated_postgres_database.runtime_dsn, api, gate,
    ))
    assert result is None
    assert isinstance(error, KernelProviderModelExecutionFailed)
    assert error.error.retry_summary.final_status_code == 413
    assert error.error.retry_summary.final_reason == "physical_close_unconfirmed"
    assert closed
    assert len(gate.requests) == 1 and gate.requests[0].status == 413
    assert not snapshots
    assert not any(row["entry_kind"] == "ASSISTANT_MESSAGE" for row in fresh)


@pytest.mark.postgres
def test_first_call_above_auto_trigger_obeys_existing_failure_circuit(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
):
    """Entry model-switch inspection cannot reopen exhausted ordinary admission."""
    from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.workspace_identity import HostWorkspaceInput
    from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
    from tests.support.model_config import test_model_binding, test_model_runtime
    from tests.test_http413_compaction_sdk import ByteGate, _rows, byte_gated_server

    async def scenario():
        import pulsara_agent.conversation_kernel.host as kernel_host

        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
        monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        gate = ByteGate(byte_limit=2_000_000, evidence_label="ordinary-failure-circuit")
        with byte_gated_server("openai_chat_completions", gate) as endpoint:
            runtime = test_model_runtime(base_url=endpoint, wire_api="openai_chat_completions",
                postgres_dsn=isolated_migrated_postgres_database.runtime_dsn)
            core = KernelHostCore.production(model_runtime=runtime)
            root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
            try:
                await root.update_model_call_binding(test_model_binding(runtime))
                root._compaction.policy = ResolvedCompactionPolicy(auto_trigger_ratio=0.01)
                for _ in range(root._compaction.policy.maximum_consecutive_auto_failures):
                    root._compaction.record_automatic_failure(scope_kind=ModelInputScopeKind.ROOT)
                assert not root._compaction.automatic_allowed(scope_kind=ModelInputScopeKind.ROOT)
                _seed_history(root, segments=8, repetitions=300)
                installed = []
                monkeypatch.setattr(root._runner, "_provider_input_installed_observer", installed.append)
                result = await root.run_turn(PromptContent.text("Continue the historical investigation."),
                    command_id=random_id("command"))
                assert result.final_text == "HTTP413_FINISHED"
                assert len(installed) == len(gate.requests) == 1
                actual = installed[0].wire_input_plan.quote
                assert actual.budget_input_tokens >= int(
                    actual.effective_input_budget_tokens * root._compaction.policy.auto_trigger_ratio)
                assert not gate.requests[0].summary
                assert not _rows(root,
                    "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s", (root.session_id,))
                assert not root._compaction.automatic_allowed(scope_kind=ModelInputScopeKind.ROOT)
            finally:
                await core.shutdown()

    asyncio.run(scenario())
