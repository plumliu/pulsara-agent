"""Capability notice consumption at the production dispatch installation boundary."""

import asyncio
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceRegistry, KernelContextSourceCollector, _collected,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.model_input.contracts import (
    ContextSourceKind, ContextRenderMode, ModelInputScopeKind, StructuredModelInputCompileError,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope, decode_runtime_observation
from pulsara_agent.llm.input import join_text_content
from tests.support.postgres_database import (
    admin_root_dsn, create_migrated_postgres_test_database, drop_postgres_test_database,
)
from tests.support.model_config import frozen_test_prompt, test_model_binding as model_binding, test_model_runtime as model_runtime, test_model_limits as model_limits
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import ScriptedKernelModel, StaticContextSourceCollector, StructuredToolPort
from tests.test_capability_change_observation import change
from tests.test_stage2_conversation_runner import _acquire_bound_host_writer, _name, _AssertingTool, _text_stream

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def notice_database():
    # Explicit integration fixture; production settings contain no database DSN.
    database = create_migrated_postgres_test_database()
    try:
        yield verified_postgres_provider(database.runtime_dsn)
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


class _NoticeCollector(StaticContextSourceCollector):
    def freeze_non_trigger_sources(self, **kwargs):
        frozen = super().freeze_non_trigger_sources(**kwargs)
        notice = kwargs.get("capability_change")
        if notice is None:
            return frozen
        collector = object.__new__(KernelContextSourceCollector)
        collector._registry = ContextSourceRegistry()
        candidate = collector._candidate(
            ContextSourceKind.CAPABILITY_CHANGE,
            (notice.render(), ""), domain_identity={"revision": notice.revision},
        )
        return replace(
            frozen, candidates=(*frozen.candidates, candidate), capability_change=notice,
            absent_facts=tuple(f for f in frozen.absent_facts if f.source_kind is not ContextSourceKind.CAPABILITY_CHANGE),
        )

    def complete_frozen_sources(self, frozen, **kwargs):
        base = super().complete_frozen_sources(frozen, **kwargs)
        return _collected(
            candidates=base.candidates, diagnostics=base.diagnostics,
            registry_fingerprint=base.registry_fingerprint,
            absent_facts=base.absent_facts, capability_change=frozen.capability_change,
        )


def _notices(view):
    notices = []
    for message in view.messages:
        if "pulsara_runtime_observation" in join_text_content(message.content):
            observation = decode_runtime_observation(message)
            if observation.source_kind is ContextSourceKind.CAPABILITY_CHANGE:
                notices.append(observation)
    return notices


@pytest.mark.parametrize("boundary", ["preflight_failure", "cancel_before_install", "network_after_install", "newer_after_freeze", "budget_fallback"])
def test_notice_pending_follows_actual_dispatch_install(notice_database, boundary):
    async def run():
        provider = notice_database
        repository = ConversationKernelRepository(provider)
        session_id, workspace_id = _name("session"), _name("workspace")
        lease = _acquire_bound_host_writer(
            repository, session_id=session_id, workspace_id=workspace_id,
            writer_owner_id=_name("host"), lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
        model = ScriptedKernelModel([_text_stream("baseline"), _text_stream("next"), _text_stream("followup")])
        # A physical per-request context boundary forces the optional source to
        # degrade; this fixture is not a product history/turn limit.
        runtime = model_runtime(model_id="test-pro", wire_api="openai_chat_completions", limits=model_limits(
            total_context_tokens=256_000, max_input_tokens=24_000,
            max_output_tokens=1_000, default_output_tokens=1_000, input_safety_margin_tokens=0,
        ))
        repository.update_session_model_call_binding(lease.guard, binding=model_binding(runtime), deadline_monotonic=monotonic() + 30)
        model._model_runtime = runtime
        model._preparer = DirectKernelModelPort(model_runtime=runtime)
        host = SimpleNamespace(
            _lock=asyncio.Lock(), _require_open=lambda: None,
            _pending_capability_change=None, _capability_refresh_requested_revision=0,
            _capability_refresh_applied_revision=0, _capability_refresh_attention=None,
            reload_capabilities=AsyncMock(return_value={"status": "RELOADED"}),
        )
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=runtime.freeze_resolution_snapshot,
            repository=repository, writer_lease=lease, model=model,
            tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
            live_bus=LiveAgentEventBus(), context_source_collector=_NoticeCollector(),
            workspace_id=workspace_id,
            capability_change_reader=lambda: KernelHostSession.freeze_capability_change(host),
            capability_change_installed=lambda frozen: KernelHostSession.consume_capability_change(host, frozen),
        )
        scope = ProviderInputContinuityScope(session_id, ModelInputScopeKind.ROOT, None)
        await runner.run_turn(frozen_test_prompt("baseline"))
        baseline = runner._continuity.current_view(scope)
        fact = change("large-" + "x" * 300_000) if boundary == "budget_fallback" else change()
        await KernelHostSession.request_capability_refresh(host, fact)
        await KernelHostSession._adopt_capabilities_if_requested(host)
        pending = host._pending_capability_change
        dispatch = runner._provider_dispatch
        original_install = dispatch.install_provider_open
        original_preflight = model.preflight_execution
        entered, release = asyncio.Event(), asyncio.Event()

        def preflight(*args, **kwargs):
            if boundary == "preflight_failure":
                raise ValueError("injected preflight failure")
            execution = original_preflight(*args, **kwargs)
            if boundary == "network_after_install":
                async def failed_open(permit):
                    execution._install_authority.consume(permit, candidate=execution._append_candidate, execution=execution)
                    raise ConnectionError("injected network failure after install")
                    yield  # async iterator protocol
                execution.open_once = failed_open
            return execution

        async def install(**kwargs):
            assert kwargs["dispatch"].sources.capability_change == pending
            if boundary == "cancel_before_install":
                entered.set()
                await release.wait()
            elif boundary == "newer_after_freeze":
                await KernelHostSession.request_capability_refresh(host, change("newer"))
            return await original_install(**kwargs)

        model.preflight_execution = preflight
        dispatch.install_provider_open = install
        try:
            if boundary == "cancel_before_install":
                task = asyncio.create_task(runner.run_turn(frozen_test_prompt("cancel this request")))
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif boundary == "preflight_failure":
                with pytest.raises(StructuredModelInputCompileError):
                    await runner.run_turn(frozen_test_prompt("fail preflight"))
            elif boundary == "network_after_install":
                with pytest.raises(ConnectionError, match="injected network"):
                    await runner.run_turn(frozen_test_prompt("fail after installation"))
            else:
                assert (await runner.run_turn(frozen_test_prompt("normal user request"))).final_text == "next"
        finally:
            model.preflight_execution = original_preflight
            dispatch.install_provider_open = original_install
        installed = runner._continuity.current_view(scope)
        assert installed.system_prompt == baseline.system_prompt
        assert installed.tools == baseline.tools
        assert installed.messages[:len(baseline.messages)] == baseline.messages
        if boundary in {"preflight_failure", "cancel_before_install"}:
            assert host._pending_capability_change == pending
            assert installed == baseline
            assert not _notices(installed)
            await runner.run_turn(frozen_test_prompt("retry naturally"))
            assert host._pending_capability_change is None
            assert len(_notices(runner._continuity.current_view(scope))) == 1
        elif boundary == "budget_fallback":
            assert host._pending_capability_change == pending
            decision = next(d for d in model.requests[-1].compiled_input.source_decisions if d.source_kind is ContextSourceKind.CAPABILITY_CHANGE)
            assert decision.selected_mode is ContextRenderMode.UNAVAILABLE_MINIMAL
            assert all(not n.body for n in _notices(installed))
            assert "large-" not in str(installed.messages)
        elif boundary == "newer_after_freeze":
            assert host._pending_capability_change.revision > pending.revision
            assert host._pending_capability_change.changes == 1
            assert "newer" in host._pending_capability_change.render()
            assert len(_notices(installed)) == 1
            await KernelHostSession._adopt_capabilities_if_requested(host)
            await runner.run_turn(frozen_test_prompt("observe the newer operation"))
            assert host._pending_capability_change is None
            assert len(_notices(runner._continuity.current_view(scope))) == 2
        else:
            assert host._pending_capability_change is None
            assert len(_notices(installed)) == 1
            await runner.run_turn(frozen_test_prompt("continue after transport failure"))
            assert len(_notices(runner._continuity.current_view(scope))) == 1

    asyncio.run(run())
