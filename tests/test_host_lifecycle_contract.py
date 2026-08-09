"""Deterministic guards for the workspace terminal lifecycle contract.

These tests pin the §11.2 / §12 behaviors of
contracts/WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md: fail-closed identity,
rollback-safe open, exactly-once lease release with capacity recovery, a single
drainable execution handle, auditable host-teardown finalization, lifecycle
linearization, off-lock teardown, and shared-capacity diagnostics.
"""

from __future__ import annotations

from pulsara_agent.event_log.serialization import build_raw_stored_event_envelope

import asyncio
import json
import threading
import time
from typing import AsyncIterator

import pytest
from tests.support.runtime_session import in_memory_runtime_session
from tests.support.settings import compatibility_storage_config

from tests.support.raw_provider import (
    RawProviderTextBlockEnd,
    RawProviderTextBlockStart,
    RawProviderTextDelta,
    RawProviderToolCallDelta,
    RawProviderToolCallEnd,
    RawProviderToolCallStart,
)
from tests.support.host import component_manifest_store, component_test_host_core

from pulsara_agent.event import (
    AgentEvent,
    CapabilityExposureResolvedEvent,
    EventContext,
    ModelCallControlDispositionResolvedEvent,
    RunEndEvent,
    RunInteractionResumeBoundaryEvent,
    RunStartEvent,
)
from pulsara_agent.event_log import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.host import (
    DuplicateHostSessionError,
    HostCoreLifecycle,
    HostSession,
    HostSessionBusyError,
    HostSessionLifecycle,
    HostSessionRegistry,
    HostWorkspaceInput,
    WorkspaceClosingError,
)
from pulsara_agent.host.core import HostCore
from pulsara_agent.llm import LLMRuntime, ModelRole
from tests.support import test_llm_config
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.runtime.approval import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.plan import PendingPlanInteraction, PlanQuestionResolution
from pulsara_agent.runtime.recovery import AbortKind
from pulsara_agent.runtime.session import (
    EventBatchCommitOutcome,
    EventWriteCancelled,
)
from pulsara_agent.ports.event_write import (
    CommittedCheckpointHandoff,
    CommittedEventSettlementReceipt,
    CommittedPublicationSettlement,
    CommittedSemanticFoldSettlement,
    RuntimeThreadEventSettlementReceipt,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.ports.run_execution import (
    RunSuspendedOutcome,
    build_prepared_run_owner_reservation_key,
    build_run_owner_identity,
)
from pulsara_agent.ports.run_terminalization import (
    RunFinalOutputMaterializationFull,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.run_execution.final_output import (
    RunFinalOutputMaterializer,
)
from pulsara_agent.runtime.run_execution.owner import UnboundRunResources
from pulsara_agent.runtime.run_execution.recovery import (
    materialize_dormant_run_owner,
)
from pulsara_agent.ports.run_authority import InstalledRunAuthorityRevision
from pulsara_agent.runtime.state import LoopBudget
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.permission import EffectivePermissionPolicy, preset_to_policy
from pulsara_agent.runtime.terminal import (
    BorrowedWorkspaceTerminalRuntime,
    PendingTerminalCompletionError,
    TerminalOwnerContext,
    TerminalRequest,
    TerminalSessionManager,
    TerminalStatus,
)
from pulsara_agent.runtime.terminal.models import TerminalExecutionOwner
from pulsara_agent.settings import PulsaraSettings


class ScriptedTransport:
    api = "scripted"
    binding_id = "test.scripted"
    contract_version = "v1"

    def __init__(self, replies: list[dict], *, delay: float = 0) -> None:
        self.replies = replies
        self.delay = delay
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        call,
        context: LLMContext,
        event_context: EventContext,
    ) -> AsyncIterator[AgentEvent]:
        del call
        self.contexts.append(context)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0)
        if "text" in reply:
            yield RawProviderTextBlockStart(
                **event_context.event_fields(), block_id="text:1"
            )
            yield RawProviderTextDelta(
                **event_context.event_fields(), block_id="text:1", delta=reply["text"]
            )
            yield RawProviderTextBlockEnd(
                **event_context.event_fields(), block_id="text:1"
            )
        for call in reply.get("tool_calls", []):
            yield RawProviderToolCallStart(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield RawProviderToolCallDelta(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield RawProviderToolCallEnd(
                **event_context.event_fields(), tool_call_id=call["id"]
            )


def _settings() -> PulsaraSettings:
    return PulsaraSettings(
        llm=test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
        ),
        storage=compatibility_storage_config(),
    )


def _core(monkeypatch, transport: ScriptedTransport) -> HostCore:
    settings = _settings()
    registry = LLMTransportRegistry()
    registry.register(transport)
    core = component_test_host_core(settings)

    def _patched_runtime(_config):
        return LLMRuntime(config=settings.llm, registry=registry)

    import pulsara_agent.runtime.wiring as wiring

    monkeypatch.setattr(wiring, "build_llm_runtime", _patched_runtime)
    return core


def _trusted_terminal_policy() -> EffectivePermissionPolicy:
    return preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)


def _trusted_terminal_ask_policy() -> EffectivePermissionPolicy:
    return preset_to_policy(PermissionMode.ASK_PERMISSIONS)


async def _open(
    core, root, *, host_session_id="host:test", conversation_id=None, policy=None
):
    return await core.open_session(
        HostWorkspaceInput(
            workspace_kind="project", workspace_root=root, memory_domain_id="u_test"
        ),
        host_session_id=host_session_id,
        conversation_id=conversation_id or f"conversation:{host_session_id}",
        model_role=ModelRole.FLASH,
        memory_reflection=False,
        permission_policy=policy,
    )


# --- §12.1 identity + open transaction ---------------------------------------


def test_duplicate_host_session_id_fail_closed_and_does_not_disturb_live_owner(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:a",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "sleep 10", "yield_time_ms": 0}
                        ),
                    }
                ]
            },
            {"text": "a started"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open(
            core,
            tmp_path,
            host_session_id="host:dup",
            policy=_trusted_terminal_policy(),
        )
        await a.run_turn("start a process")
        manager = a.wiring.runtime_wiring.runtime_session.terminal_sessions
        a_proc = manager.list_owned("host:dup")[0].process_id
        with pytest.raises(DuplicateHostSessionError):
            await _open(
                core,
                tmp_path,
                host_session_id="host:dup",
                conversation_id="conversation:other",
            )
        same = await core.get_session("host:dup")
        proc_status = manager.poll_process(a_proc).status
        sessions = await core.list_sessions()
        await core.shutdown()
        return same is a, proc_status, len(sessions)

    is_same, proc_status, count = asyncio.run(run())
    assert is_same  # old session not overwritten
    assert proc_status is TerminalStatus.RUNNING  # old owner's process untouched
    assert count == 1  # the duplicate never registered


def test_duplicate_conversation_id_fail_closed(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "a"}])
    core = _core(monkeypatch, transport)

    async def run():
        a = await _open(
            core,
            tmp_path,
            host_session_id="host:a",
            conversation_id="conversation:shared",
        )
        with pytest.raises(DuplicateHostSessionError):
            await _open(
                core,
                tmp_path,
                host_session_id="host:b",
                conversation_id="conversation:shared",
            )
        resolved = await core.find_by_conversation("conversation:shared")
        sessions = await core.list_sessions()
        await core.shutdown()
        return resolved is a, len(sessions)

    resolves_to_a, count = asyncio.run(run())
    assert resolves_to_a  # conversation index not hijacked by the rejected open
    assert count == 1


def test_stale_reservation_cannot_release_or_publish_aba_successor() -> None:
    registry = HostSessionRegistry()

    async def run():
        stale = await registry.reserve("host:aba", "conversation:aba")
        await registry.release_reservation(stale)
        current = await registry.reserve("host:aba", "conversation:aba")
        assert current.token != stale.token

        # A delayed rollback from the first open transaction must not consume
        # the second transaction's reservation for the same identities.
        await registry.release_reservation(stale)
        with pytest.raises(DuplicateHostSessionError):
            await registry.reserve("host:aba", "conversation:other")
        with pytest.raises(RuntimeError, match="consumed or released"):
            await registry.publish(stale, None)  # type: ignore[arg-type]

        await registry.release_reservation(current)
        replacement = await registry.reserve("host:aba", "conversation:aba")
        return stale.token, current.token, replacement.token

    stale_token, current_token, replacement_token = asyncio.run(run())
    assert len({stale_token, current_token, replacement_token}) == 3


def test_registry_capacity_failure_leaks_no_supervisor_owner(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport([{"text": "a"}])
    core = _core(monkeypatch, transport)
    core.registry = HostSessionRegistry(max_sessions=1)

    async def run():
        await _open(core, tmp_path, host_session_id="host:a")
        with pytest.raises(RuntimeError, match="limit"):
            await _open(core, tmp_path, host_session_id="host:b")
        snapshots = await core.list_workspace_terminal_snapshots()
        await core.shutdown()
        return snapshots

    snapshots = asyncio.run(run())
    assert len(snapshots) == 1
    assert (
        snapshots[0].owner_session_count == 1
    )  # only host:a; the over-capacity open left no lease


def test_wiring_failure_rolls_back_lease_and_reservation(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "second open ok"}])
    core = _core(monkeypatch, transport)
    original = core._composition.build_agent_runtime_wiring
    calls = {"n": 0}

    def maybe_boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("wiring boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        core._composition,
        "build_agent_runtime_wiring",
        maybe_boom,
    )

    async def run():
        with pytest.raises(RuntimeError, match="wiring boom"):
            await _open(core, tmp_path, host_session_id="host:fail")
        # After rollback: no leaked supervisor lease, and the identity is free.
        leaked = await core.list_workspace_terminal_snapshots()
        reopened = await _open(core, tmp_path, host_session_id="host:fail")
        sessions = await core.list_sessions()
        await core.shutdown()
        return leaked, reopened.host_session_id, len(sessions)

    leaked, reopened_id, count = asyncio.run(run())
    assert leaked == []  # lease released on the failed open
    assert reopened_id == "host:fail"  # reservation released; id reusable
    assert count == 1


# --- §12.2 session close ------------------------------------------------------


def test_session_close_is_idempotent_and_releases_owner_exactly_once(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:bg",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {
                                "command": "sleep 30",
                                "yield_time_ms": 0,
                                "terminal_session_id": "work",
                            }
                        ),
                    }
                ]
            },
            {"text": "started"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(
            core,
            tmp_path,
            host_session_id="host:cap",
            policy=_trusted_terminal_policy(),
        )
        await s.run_turn("start bg")
        manager = s.wiring.runtime_wiring.runtime_session.terminal_sessions
        proc = manager.list_owned("host:cap")[0].process_id
        sessions_before = manager.session_count()
        await core.close_session("host:cap")
        status_after = manager.poll_process(proc).status
        sessions_after = manager.session_count()
        await core.close_session("host:cap")  # idempotent, no-op, no error
        await core.shutdown()
        return sessions_before, sessions_after, status_after

    before, after, status = asyncio.run(run())
    assert before >= 1
    assert after == 0  # owner terminal sessions pruned: capacity restored (P0-7)
    assert status is TerminalStatus.KILLED


def test_begin_close_closes_mutation_gate_and_concurrent_close_waits(
    tmp_path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    original_close = HostSession.aclose

    async def delayed_close(self, *, reason=AbortKind.HOST_TEARDOWN):
        close_entered.set()
        await release_close.wait()
        return await original_close(self, reason=reason)

    monkeypatch.setattr(HostSession, "aclose", delayed_close)

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:closing-gate")
        first = asyncio.create_task(core.close_session(session.host_session_id))
        await close_entered.wait()
        assert session.lifecycle is HostSessionLifecycle.CLOSING
        with pytest.raises(RuntimeError, match="closed"):
            session.set_permission_mode("read-only")
        with pytest.raises(RuntimeError, match="closed"):
            await core.stop_current_turn(session.host_session_id)
        second = asyncio.create_task(core.close_session(session.host_session_id))
        await asyncio.sleep(0)
        assert not second.done()
        release_close.set()
        await asyncio.gather(first, second)
        return session.lifecycle, await core.list_sessions()

    lifecycle, sessions = asyncio.run(run())
    assert lifecycle is HostSessionLifecycle.CLOSED
    assert sessions == []


def test_session_cleanup_failure_does_not_wedge_registry_close(
    tmp_path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))

    async def release_boom(self, lease):
        raise RuntimeError("lease release boom")

    monkeypatch.setattr(HostCore, "_release_supervisor_lease", release_boom)

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:cleanup-error")
        with pytest.raises(RuntimeError, match="lease release boom"):
            await core.close_session(session.host_session_id)
        # The close transaction is finalized despite cleanup failure: the
        # identity is gone and a repeated close cannot wait forever.
        assert await core.list_sessions() == []
        await asyncio.wait_for(core.close_session(session.host_session_id), timeout=0.2)
        await core.shutdown()
        return session.lifecycle

    assert asyncio.run(run()) is HostSessionLifecycle.CLOSED


def test_session_drain_failure_preserves_lease_and_allows_close_retry(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_close = HostSession.aclose
    attempts = 0

    async def fail_once(
        self, *, reason=AbortKind.HOST_TEARDOWN, drain_timeout_seconds=5.0
    ):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("synthetic child drain timeout")
        return await original_close(
            self,
            reason=reason,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    monkeypatch.setattr(HostSession, "aclose", fail_once)

    async def run() -> None:
        session = await _open(core, tmp_path, host_session_id="host:drain-retry")
        lease = core._session_leases[session.host_session_id]  # noqa: SLF001

        with pytest.raises(TimeoutError, match="child drain timeout"):
            await core.close_session(session.host_session_id)

        assert await core.registry.get(session.host_session_id) is session
        assert core._session_leases[session.host_session_id] is lease  # noqa: SLF001
        assert session.lifecycle is HostSessionLifecycle.CLOSING
        assert lease.workspace_key in core._supervisors  # noqa: SLF001

        await core.close_session(session.host_session_id)
        assert await core.list_sessions() == []
        assert session.host_session_id not in core._session_leases  # noqa: SLF001
        await core.shutdown()

    asyncio.run(run())


def test_concurrent_session_close_waiters_share_failure_and_retry_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_close = HostSession.aclose
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    retry_entered = asyncio.Event()
    release_retry = asyncio.Event()
    attempts = 0

    async def controlled_close(
        self,
        *,
        reason=AbortKind.HOST_TEARDOWN,
        drain_timeout_seconds=5.0,
    ):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_entered.set()
            await release_first.wait()
            raise TimeoutError("shared close attempt failed")
        retry_entered.set()
        await release_retry.wait()
        return await original_close(
            self,
            reason=reason,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    monkeypatch.setattr(HostSession, "aclose", controlled_close)

    async def run() -> None:
        session = await _open(
            core, tmp_path, host_session_id="host:shared-close-result"
        )
        owner = asyncio.create_task(core.close_session(session.host_session_id))
        await first_entered.wait()
        waiter = asyncio.create_task(core.close_session(session.host_session_id))
        await asyncio.sleep(0)
        assert not waiter.done()

        release_first.set()
        outcomes = await asyncio.gather(owner, waiter, return_exceptions=True)
        assert all(isinstance(item, TimeoutError) for item in outcomes)
        assert all("shared close attempt failed" in str(item) for item in outcomes)

        retry_owner = asyncio.create_task(core.close_session(session.host_session_id))
        await retry_entered.wait()
        retry_waiter = asyncio.create_task(core.close_session(session.host_session_id))
        await asyncio.sleep(0)
        assert not retry_waiter.done()

        release_retry.set()
        await asyncio.gather(retry_owner, retry_waiter)
        assert await core.list_sessions() == []
        await core.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("explicit_owner", [False, True])
def test_close_attempt_monotonically_merges_detach_and_explicit_close_intent(
    tmp_path,
    monkeypatch,
    explicit_owner: bool,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_close = HostSession.aclose
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    marked_closed: list[str] = []

    def mark_closed(runtime_session_id: str) -> None:
        marked_closed.append(runtime_session_id)

    async def delayed_close(
        self,
        *,
        reason=AbortKind.HOST_TEARDOWN,
        drain_timeout_seconds=5.0,
    ):
        close_entered.set()
        await release_close.wait()
        return await original_close(
            self,
            reason=reason,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    monkeypatch.setattr(HostSession, "aclose", delayed_close)
    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> str:
        session = await _open(core, tmp_path, host_session_id="host:close-intent")
        owner = asyncio.create_task(
            core.close_session(
                session.host_session_id,
                close_conversation=explicit_owner,
            )
        )
        await close_entered.wait()
        waiter = asyncio.create_task(
            core.close_session(
                session.host_session_id,
                close_conversation=not explicit_owner,
            )
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        release_close.set()
        await asyncio.gather(owner, waiter)
        return session.runtime_session_id

    runtime_session_id = asyncio.run(run())
    assert marked_closed == [runtime_session_id]


def test_shutdown_close_attempt_merges_competing_explicit_close_intent(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_close = HostSession.aclose
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    marked_closed: list[str] = []

    def mark_closed(runtime_session_id: str) -> None:
        marked_closed.append(runtime_session_id)

    async def delayed_close(
        self,
        *,
        reason=AbortKind.HOST_TEARDOWN,
        drain_timeout_seconds=5.0,
    ):
        close_entered.set()
        await release_close.wait()
        return await original_close(
            self,
            reason=reason,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    monkeypatch.setattr(HostSession, "aclose", delayed_close)
    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> str:
        session = await _open(
            core, tmp_path, host_session_id="host:shutdown-close-intent"
        )
        shutdown = asyncio.create_task(core.shutdown())
        await close_entered.wait()
        explicit_close = asyncio.create_task(
            core.close_session(session.host_session_id, close_conversation=True)
        )
        await asyncio.sleep(0)
        assert not explicit_close.done()
        release_close.set()
        await asyncio.gather(shutdown, explicit_close)
        return session.runtime_session_id

    runtime_session_id = asyncio.run(run())
    assert marked_closed == [runtime_session_id]


def test_explicit_close_arriving_after_detach_intent_seal_closes_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_seal = HostSessionRegistry.seal_close_intent
    sealed = asyncio.Event()
    release_seal = asyncio.Event()
    marked_closed: list[str] = []

    def mark_closed(runtime_session_id: str) -> None:
        marked_closed.append(runtime_session_id)

    async def delayed_after_seal(self, attempt):
        merged = await original_seal(self, attempt)
        sealed.set()
        await release_seal.wait()
        return merged

    monkeypatch.setattr(HostSessionRegistry, "seal_close_intent", delayed_after_seal)
    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> str:
        session = await _open(
            core, tmp_path, host_session_id="host:late-explicit-intent"
        )
        detach = asyncio.create_task(core.detach_session(session.host_session_id))
        await sealed.wait()
        explicit_close = asyncio.create_task(
            core.close_session(session.host_session_id, close_conversation=True)
        )
        await asyncio.sleep(0)
        assert not explicit_close.done()
        release_seal.set()
        await asyncio.gather(detach, explicit_close)
        return session.runtime_session_id

    runtime_session_id = asyncio.run(run())
    assert marked_closed == [runtime_session_id]


def test_explicit_close_manifest_failure_keeps_tombstone_for_retry(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    calls = 0
    marked_closed: list[str] = []

    def mark_closed(runtime_session_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("manifest unavailable")
        marked_closed.append(runtime_session_id)

    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> str:
        session = await _open(core, tmp_path, host_session_id="host:manifest-retry")
        with pytest.raises(RuntimeError, match="manifest unavailable"):
            await core.close_session(
                session.host_session_id,
                close_conversation=True,
            )
        assert await core.list_sessions() == []
        assert await core.registry.list_manifest_close_tombstones() == (
            (session.host_session_id, session.runtime_session_id),
        )

        await core.close_session(
            session.host_session_id,
            close_conversation=True,
        )
        assert await core.registry.list_manifest_close_tombstones() == ()
        await core.shutdown()
        return session.runtime_session_id

    runtime_session_id = asyncio.run(run())
    assert calls == 2
    assert marked_closed == [runtime_session_id]


def test_late_explicit_manifest_failure_keeps_tombstone_for_retry(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_seal = HostSessionRegistry.seal_close_intent
    sealed = asyncio.Event()
    release_seal = asyncio.Event()
    calls = 0
    marked_closed: list[str] = []

    def mark_closed(runtime_session_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("late manifest unavailable")
        marked_closed.append(runtime_session_id)

    async def delayed_after_seal(self, attempt):
        merged = await original_seal(self, attempt)
        sealed.set()
        await release_seal.wait()
        return merged

    monkeypatch.setattr(HostSessionRegistry, "seal_close_intent", delayed_after_seal)
    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> str:
        session = await _open(
            core, tmp_path, host_session_id="host:late-manifest-retry"
        )
        detach = asyncio.create_task(core.detach_session(session.host_session_id))
        await sealed.wait()
        late_explicit = asyncio.create_task(
            core.close_session(session.host_session_id, close_conversation=True)
        )
        release_seal.set()
        await detach
        with pytest.raises(RuntimeError, match="late manifest unavailable"):
            await late_explicit
        assert await core.registry.list_manifest_close_tombstones() == (
            (session.host_session_id, session.runtime_session_id),
        )

        await core.close_session(session.host_session_id, close_conversation=True)
        assert await core.registry.list_manifest_close_tombstones() == ()
        await core.shutdown()
        return session.runtime_session_id

    runtime_session_id = asyncio.run(run())
    assert calls == 2
    assert marked_closed == [runtime_session_id]


def test_shutdown_retries_pending_manifest_close_before_shared_teardown(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    calls = 0

    def mark_closed(runtime_session_id: str) -> None:
        nonlocal calls
        del runtime_session_id
        calls += 1
        if calls == 1:
            raise RuntimeError("manifest unavailable before shutdown")

    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> HostCoreLifecycle:
        session = await _open(core, tmp_path, host_session_id="host:manifest-shutdown")
        with pytest.raises(RuntimeError, match="manifest unavailable before shutdown"):
            await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()
        assert await core.registry.list_manifest_close_tombstones() == ()
        return core.lifecycle

    assert asyncio.run(run()) is HostCoreLifecycle.CLOSED
    assert calls == 2


def test_cancelled_manifest_retry_owner_releases_retry_ownership(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    calls = 0
    retry_finish_entered = asyncio.Event()
    release_retry_finish = asyncio.Event()
    original_finish_retry = HostSessionRegistry.finish_manifest_close_retry

    def mark_closed(runtime_session_id: str) -> None:
        nonlocal calls
        del runtime_session_id
        calls += 1
        if calls == 1:
            raise RuntimeError("manifest unavailable")

    async def delayed_finish_retry(self, attempt, *, error=None):
        retry_finish_entered.set()
        await release_retry_finish.wait()
        return await original_finish_retry(self, attempt, error=error)

    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)
    monkeypatch.setattr(
        HostSessionRegistry,
        "finish_manifest_close_retry",
        delayed_finish_retry,
    )

    async def run() -> None:
        session = await _open(core, tmp_path, host_session_id="host:cancelled-retry")
        with pytest.raises(RuntimeError, match="manifest unavailable"):
            await core.close_session(session.host_session_id, close_conversation=True)

        retry = asyncio.create_task(
            core.close_session(session.host_session_id, close_conversation=True)
        )
        await retry_finish_entered.wait()
        tombstone = core.registry._manifest_close_tombstones[  # noqa: SLF001
            session.host_session_id
        ]
        owned_attempt = tombstone.retry_attempt
        assert owned_attempt is not None

        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry
        assert owned_attempt.completion.done()
        assert tombstone.retry_attempt is None

        release_retry_finish.set()
        await core.close_session(session.host_session_id, close_conversation=True)
        assert await core.registry.list_manifest_close_tombstones() == ()
        await core.shutdown()

    asyncio.run(run())
    assert calls == 3


def test_runtime_reservation_atomically_rejects_pending_manifest_tombstone(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    calls = 0

    def mark_closed(runtime_session_id: str) -> None:
        nonlocal calls
        del runtime_session_id
        calls += 1
        if calls == 1:
            raise RuntimeError("manifest unavailable")

    monkeypatch.setattr(component_manifest_store(core), "mark_closed", mark_closed)

    async def run() -> None:
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:old-runtime",
            conversation_id="conversation:same-runtime",
        )
        with pytest.raises(RuntimeError, match="manifest unavailable"):
            await core.close_session(session.host_session_id, close_conversation=True)

        with pytest.raises(
            DuplicateHostSessionError, match="conversation_id pending close"
        ):
            await core.registry.reserve(
                "host:new-conversation",
                "conversation:same-runtime",
            )
        with pytest.raises(
            DuplicateHostSessionError, match="runtime_session_id.*pending close"
        ):
            await core.registry.reserve(
                "host:new-runtime",
                "conversation:new-runtime",
                runtime_session_id=session.runtime_session_id,
            )

        await core.close_session(session.host_session_id, close_conversation=True)
        reservation = await core.registry.reserve(
            "host:new-runtime",
            "conversation:same-runtime",
            runtime_session_id=session.runtime_session_id,
        )
        await core.registry.release_reservation(reservation)
        await core.shutdown()

    asyncio.run(run())
    assert calls == 2


def test_tombstoned_resume_rejects_before_dangling_repair(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    repair_calls: list[str] = []
    fail_manifest_close = True
    manifest_store = component_manifest_store(core)
    original_mark_closed = manifest_store.mark_closed

    def mark_closed(runtime_session_id: str) -> None:
        if fail_manifest_close:
            raise RuntimeError("manifest unavailable")
        original_mark_closed(runtime_session_id)

    async def record_repair(
        *, connection_provider, runtime_session_id: str, workspace_root: str, **kwargs
    ):
        del connection_provider, workspace_root, kwargs
        repair_calls.append(runtime_session_id)
        raise AssertionError("repair must not run before runtime reservation")

    monkeypatch.setattr(manifest_store, "mark_closed", mark_closed)
    monkeypatch.setattr(
        "pulsara_agent.host.core.repair_dangling_runs_for_resume",
        record_repair,
    )

    async def run() -> None:
        nonlocal fail_manifest_close
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:tombstoned-resume",
            conversation_id="conversation:tombstoned-resume",
        )
        with pytest.raises(RuntimeError, match="manifest unavailable"):
            await core.close_session(session.host_session_id, close_conversation=True)

        with pytest.raises(
            DuplicateHostSessionError, match="runtime_session_id.*pending close"
        ):
            await core.resume_session(
                session.runtime_session_id,
                host_session_id="host:resume-new",
                conversation_id="conversation:resume-new",
            )
        assert repair_calls == []

        fail_manifest_close = False
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(run())


def test_host_shutdown_stops_before_shared_teardown_when_session_drain_fails(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    original_close = HostSession.aclose
    attempts = 0

    async def fail_once(
        self, *, reason=AbortKind.HOST_TEARDOWN, drain_timeout_seconds=5.0
    ):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("synthetic shutdown drain timeout")
        return await original_close(
            self,
            reason=reason,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    monkeypatch.setattr(HostSession, "aclose", fail_once)

    async def run() -> None:
        session = await _open(core, tmp_path, host_session_id="host:shutdown-retry")
        lease = core._session_leases[session.host_session_id]  # noqa: SLF001

        with pytest.raises(TimeoutError, match="shutdown drain timeout"):
            await core.shutdown()

        assert core.lifecycle is HostCoreLifecycle.OPEN
        assert await core.registry.get(session.host_session_id) is session
        assert core._session_leases[session.host_session_id] is lease  # noqa: SLF001
        assert lease.workspace_key in core._supervisors  # noqa: SLF001

        await core.shutdown()
        assert core.lifecycle is HostCoreLifecycle.CLOSED

    asyncio.run(run())


def test_host_close_pending_terminal_completion_preserves_retryable_session_and_lease(
    tmp_path,
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    available = False

    def recorder(event):
        if not available:
            raise RuntimeError("synthetic terminal event store outage")
        committed = event.model_copy(update={"sequence": 1})
        references = ((committed.id, 1, f"test:{committed.id}"),)
        payload = {
            "stored_batch_receipt_identity": f"test-batch:{committed.id}",
            "requested_event_references": references,
            "durability": "full",
            "semantic_fold": CommittedSemanticFoldSettlement.HEALTHY.value,
            "checkpoint_handoff": CommittedCheckpointHandoff.NOT_APPLICABLE.value,
            "publication": CommittedPublicationSettlement.COMPLETED.value,
        }
        return RuntimeThreadEventSettlementReceipt(
            committed_event=committed,
            settlement=CommittedEventSettlementReceipt(
                stored_batch_receipt_identity=f"test-batch:{committed.id}",
                requested_event_references=references,
                durability="full",
                semantic_fold=CommittedSemanticFoldSettlement.HEALTHY,
                checkpoint_handoff=CommittedCheckpointHandoff.NOT_APPLICABLE,
                publication=CommittedPublicationSettlement.COMPLETED,
                settlement_fingerprint=context_fingerprint(
                    "committed-event-settlement-receipt:v1", payload
                ),
            ),
        )

    async def run() -> None:
        nonlocal available
        session = await _open(core, tmp_path, host_session_id="host:terminal-pending")
        lease = core._session_leases[session.host_session_id]  # noqa: SLF001
        terminal = lease.manager.get_or_create(
            owner_host_session_id=session.host_session_id,
            owner_conversation_id=session.conversation_id,
        )
        started = terminal.execute(
            TerminalRequest(
                command="sleep 5",
                yield_time_ms=0,
            ),
            execution_owner=TerminalExecutionOwner(
                origin_event_context=EventContext(
                    run_id="run:terminal-pending",
                    turn_id="turn:terminal-pending",
                    reply_id="reply:terminal-pending",
                ),
                origin_tool_call_id="call:terminal-pending",
                record_event=recorder,
            ),
        )
        assert started.process_id is not None
        lease.manager.kill_process(
            started.process_id,
            owner_host_session_id=session.host_session_id,
        )

        with pytest.raises(PendingTerminalCompletionError):
            await core.close_session(session.host_session_id)

        assert await core.registry.get(session.host_session_id) is session
        assert core._session_leases[session.host_session_id] is lease  # noqa: SLF001
        assert lease.workspace_key in core._supervisors  # noqa: SLF001
        assert (
            lease.manager.pending_completion_count(
                owner_host_session_id=session.host_session_id
            )
            == 1
        )

        available = True
        await core.close_session(session.host_session_id)

        assert await core.registry.list_sessions() == []
        assert session.host_session_id not in core._session_leases  # noqa: SLF001
        await core.shutdown()

    asyncio.run(run())


def test_repeated_owner_release_does_not_exhaust_shared_session_capacity(
    tmp_path, monkeypatch
) -> None:
    # The §15.2 experiment: an anchor keeps the supervisor alive while ephemeral
    # owners reuse the default terminal and close. With kill_owned-only this hit
    # "terminal session limit reached: max 4" by the 4th ephemeral; release_owner
    # must prune stale session keys so capacity recovers.
    replies: list[dict] = []
    for i in range(7):
        replies.append(
            {
                "tool_calls": [
                    {
                        "id": f"call:{i}",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf ok"}),
                    }
                ]
            }
        )
        replies.append({"text": f"ok {i}"})
    core = _core(monkeypatch, ScriptedTransport(replies))

    async def run():
        anchor = await _open(
            core,
            tmp_path,
            host_session_id="host:anchor",
            policy=_trusted_terminal_policy(),
        )
        await anchor.run_turn("anchor uses terminal")
        manager = anchor.wiring.runtime_wiring.runtime_session.terminal_sessions
        for i in range(6):
            s = await _open(
                core,
                tmp_path,
                host_session_id=f"host:eph-{i}",
                policy=_trusted_terminal_policy(),
            )
            await s.run_turn(f"use terminal {i}")
            await core.close_session(f"host:eph-{i}")
        count = manager.session_count()
        await core.shutdown()
        return count

    count = asyncio.run(run())
    assert count == 1  # only the anchor's session remains


def test_borrowed_runtime_close_does_not_touch_shared_manager(tmp_path) -> None:
    manager = TerminalSessionManager(tmp_path)
    owner = TerminalOwnerContext(
        host_session_id="host:x", conversation_id="conversation:x"
    )
    rs = in_memory_runtime_session(
        tmp_path,
        terminal_binding=BorrowedWorkspaceTerminalRuntime(owner=owner, manager=manager),
    )
    manager.get_or_create("work", owner_host_session_id="host:x")
    assert manager.session_count() == 1
    assert rs.terminal_owner_host_session_id == "host:x"
    assert rs.terminal_owner_conversation_id == "conversation:x"
    rs.close()
    # Borrowed close releases nothing in the shared manager; lease release is the
    # supervisor/HostCore job (contract §5).
    assert manager.session_count() == 1


def test_owned_runtime_close_is_idempotent(tmp_path) -> None:
    rs = in_memory_runtime_session(tmp_path)
    assert rs._owns_terminal_manager is True
    rs.close()
    rs.close()  # second close is a no-op


def test_close_active_streaming_run_emits_auditable_host_teardown(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport([{"text": "streaming"}], delay=0.3)
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(core, tmp_path, host_session_id="host:stream")
        events: list[AgentEvent] = []

        async def consume():
            try:
                async for event in s.stream_turn("go"):
                    events.append(event)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        service = s.wiring.run_activation_service
        assert service is not None
        active_owner = service.active_host_run_view()
        boundary_task = s._current_boundary_task()
        owned = (
            active_owner is not None
            or boundary_task is not None
            and not boundary_task.done()
        )
        await core.close_session("host:stream")
        await consumer
        return owned, events, s.replay_events(), s.closed

    owned, _streamed_events, ledger_events, closed = asyncio.run(run())
    assert owned  # PREPARING and ACTIVE both have a drainable Host-owned handle.
    assert closed
    # Close first detaches the transport observer so a full queue cannot block
    # terminalization. The terminal fact remains durable even when the detached
    # stream does not observe it.
    run_ends = [e for e in ledger_events if isinstance(e, RunEndEvent)]
    assert run_ends and run_ends[-1].status == "aborted"
    assert run_ends[-1].abort_kind == "host_teardown"  # not masqueraded as user_stop


def test_close_suspended_run_emits_auditable_host_teardown(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:danger",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "rm -rf build"}),
                    }
                ]
            }
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(
            core,
            tmp_path,
            host_session_id="host:susp",
            policy=_trusted_terminal_ask_policy(),
        )
        first = await s.run_turn("danger")
        assert s.get_pending_approval() is not None
        await core.close_session("host:susp")
        events = s.replay_events()
        assert isinstance(first, RunSuspendedOutcome)
        return events, first.owner_identity.run_id, s.closed

    events, run_id, closed = asyncio.run(run())
    assert closed
    run_ends = [e for e in events if isinstance(e, RunEndEvent) and e.run_id == run_id]
    # The suspended run is not silently dropped; it gets a terminal RunEnd.
    assert any(
        e.status == "aborted" and e.abort_kind == "host_teardown" for e in run_ends
    )


def test_streaming_resume_is_owned_by_host_session(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:ask",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf ok"}),
                    }
                ]
            },
            {"text": "resumed"},
        ],
        delay=0.2,
    )
    core = _core(monkeypatch, transport)

    async def run():
        s = await _open(
            core,
            tmp_path,
            host_session_id="host:resume",
            policy=_trusted_terminal_ask_policy(),
        )
        await s.run_turn("do x")  # suspends on terminal-ask approval
        service = s.wiring.run_activation_service
        assert service is not None
        pending = s.get_pending_approval()
        suspended_boundary = s.summary()["boundary"]
        events: list[AgentEvent] = []

        async def consume():
            async for event in s.stream_approval_resolution(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(tool_call_id=c.id, confirmed=True)
                        for c in pending.tool_calls
                    ),
                )
            ):
                events.append(event)

        resume_task = asyncio.create_task(consume())
        for _ in range(100):
            if service.active_host_run_view() is not None:
                break
            await asyncio.sleep(0.005)
        owned = service.active_host_run_view() is not None
        active_boundary = s.summary()["boundary"]
        await resume_task
        await core.shutdown()
        return owned, events, suspended_boundary, active_boundary

    owned, events, suspended_boundary, active_boundary = asyncio.run(run())
    assert owned
    assert suspended_boundary["state"] == "committed"
    assert suspended_boundary["active_segment_id"] is None
    assert active_boundary["active_segment_generation"] == 2
    assert active_boundary["active_segment_owner_kind"] == "host_resume_boundary"
    continuation_exposures = [
        event
        for event in events
        if isinstance(event, CapabilityExposureResolvedEvent)
        and event.exposure_revision == 2
    ]
    resume_boundaries = [
        event
        for event in events
        if isinstance(event, RunInteractionResumeBoundaryEvent)
    ]
    assert len(continuation_exposures) == 1
    assert len(resume_boundaries) == 1
    assert (
        resume_boundaries[0].boundary.effective_exposure_id
        == continuation_exposures[0].exposure.exposure_id
    )


def test_one_committed_run_uses_distinct_generations_for_two_resumes(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:first",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf first"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:second",
                        "name": "terminal",
                        "arguments": json.dumps({"command": "printf second"}),
                    }
                ]
            },
            {"text": "both complete"},
        ]
    )
    core = _core(monkeypatch, transport)

    def approve(pending):
        return ApprovalResolution(
            approval_id=pending.approval_id,
            decisions=tuple(
                ToolApprovalDecision(tool_call_id=call.id, confirmed=True)
                for call in pending.tool_calls
            ),
        )

    async def run():
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:two-resumes",
            policy=_trusted_terminal_ask_policy(),
        )
        first = await session.run_turn("run both commands")
        first_pending = session.get_pending_approval()
        assert first_pending is not None

        second = await session.resolve_approval(approve(first_pending))
        second_pending = session.get_pending_approval()
        assert second_pending is not None
        assert isinstance(first, RunSuspendedOutcome)
        assert isinstance(second, RunSuspendedOutcome)
        assert second.owner_identity.run_id == first.owner_identity.run_id

        terminal = await session.resolve_approval(approve(second_pending))
        assert terminal.run_id == first.owner_identity.run_id
        assert session.get_pending_approval() is None
        events = session.replay_events()
        await core.shutdown()
        return first.owner_identity.run_id, session.runtime_session_id, events

    run_id, runtime_session_id, events = asyncio.run(run())
    starts = [
        event
        for event in events
        if isinstance(event, RunStartEvent) and event.run_id == run_id
    ]
    ends = [
        event
        for event in events
        if isinstance(event, RunEndEvent) and event.run_id == run_id
    ]
    control = [
        event
        for event in events
        if isinstance(event, ModelCallControlDispositionResolvedEvent)
        and event.run_id == run_id
    ]
    assert len(starts) == 1
    assert len(ends) == 1
    assert [event.run_execution_activation.segment_generation for event in control] == [
        1,
        2,
        3,
    ]
    assert [
        event.run_execution_activation.activation_owner_kind for event in control
    ] == ["host_run_boundary", "host_resume_boundary", "host_resume_boundary"]
    assert (
        len(
            [
                event
                for event in events
                if isinstance(event, RunInteractionResumeBoundaryEvent)
                and event.run_id == run_id
            ]
        )
        == 2
    )

    start = starts[0]
    end = ends[0]
    assert start.sequence is not None and end.sequence is not None
    pre_terminal_events = tuple(
        event
        for event in events
        if event.run_id == run_id
        and event.sequence is not None
        and event.sequence < end.sequence
    )

    async def rebuild_dormant_snapshot():
        dormant = materialize_dormant_run_owner(
            events=pre_terminal_events,
            run_start_envelope=build_raw_stored_event_envelope(
                event=start,
                runtime_session_id=runtime_session_id,
                schema_registry=DEFAULT_EVENT_SCHEMA_REGISTRY,
            ),
        )
        assert isinstance(dormant.resource_slot, UnboundRunResources)
        assert isinstance(dormant.authority_head, InstalledRunAuthorityRevision)
        snapshot = (
            dormant.lifecycle,
            dormant.resource_slot.reason,
            dormant.authority_head.revision.revision,
            dormant.latest_activation_owner_kind,
            dormant.active_segment,
        )
        dormant.run_completion.cancel()
        return snapshot

    assert asyncio.run(rebuild_dormant_snapshot()) == (
        "initializing",
        "reopen_continuation_rebind_pending",
        3,
        "host_resume_boundary",
        None,
    )


def test_final_output_rebuild_after_run_owner_retirement_is_byte_identical(
    tmp_path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([{"text": "stable final output"}]))

    async def run():
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:final-output-rebuild",
        )
        live = await session.run_turn("finish once")
        runtime_session = session.wiring.runtime_wiring.runtime_session
        events = tuple(runtime_session.event_log.iter(run_id=live.run_id))
        start = next(event for event in events if isinstance(event, RunStartEvent))
        end = next(event for event in events if isinstance(event, RunEndEvent))
        assert start.sequence is not None and end.sequence is not None
        owner_identity = build_run_owner_identity(
            reservation_key=build_prepared_run_owner_reservation_key(
                runtime_session_id=session.runtime_session_id,
                run_id=live.run_id,
                run_start_event_id=start.id,
            ),
            run_start_sequence=start.sequence,
        )
        end_reference = event_reference_from_stored(
            end,
            runtime_session_id=session.runtime_session_id,
        )
        loop_thread_id = threading.get_ident()
        raw_page_spans: list[int] = []
        event_log_type = type(runtime_session.event_log)
        original_read_range_snapshot = event_log_type.read_range_snapshot
        original_read_raw_events_by_types = event_log_type.read_raw_events_by_types

        def reject_unbounded_range(*_args, **_kwargs):
            raise AssertionError("final output must not scan a full run range")

        def read_bounded_lifecycle_page(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread_id
            start_sequence = kwargs.get("minimum_sequence")
            end_sequence = kwargs.get("through_sequence")
            if start_sequence is not None and end_sequence is not None:
                raw_page_spans.append(end_sequence - start_sequence + 1)
                assert raw_page_spans[-1] <= 512
            return original_read_raw_events_by_types(self, *args, **kwargs)

        def reject_reply_replay(*_args, **_kwargs):
            raise AssertionError(
                "final output must use canonical terminal projection authority"
            )

        monkeypatch.setattr(
            event_log_type, "read_range_snapshot", reject_unbounded_range
        )
        monkeypatch.setattr(
            event_log_type,
            "read_raw_events_by_types",
            read_bounded_lifecycle_page,
        )
        monkeypatch.setattr(
            event_log_type,
            "read_raw_reply_events",
            reject_reply_replay,
        )
        first = await RunFinalOutputMaterializer(
            event_log=runtime_session.event_log,
            runtime_session_id=session.runtime_session_id,
            io_service=runtime_session.context_input_io_service,
            transcript_projection=runtime_session.transcript_projection_state_store,
            archive=runtime_session.archive,
        ).materialize(
            owner_identity=owner_identity,
            run_end_event_reference=end_reference,
            deadline_monotonic=asyncio.get_running_loop().time() + 2.0,
        )
        recovered = await RunFinalOutputMaterializer(
            event_log=runtime_session.event_log,
            runtime_session_id=session.runtime_session_id,
            io_service=runtime_session.context_input_io_service,
            transcript_projection=runtime_session.transcript_projection_state_store,
            archive=runtime_session.archive,
        ).materialize(
            owner_identity=owner_identity,
            run_end_event_reference=end_reference,
            deadline_monotonic=asyncio.get_running_loop().time() + 2.0,
        )
        assert isinstance(first, RunFinalOutputMaterializationFull)
        assert isinstance(recovered, RunFinalOutputMaterializationFull)
        assert recovered.receipt == first.receipt
        assert recovered.receipt.output.final_text == live.final_text
        assert (
            recovered.receipt.output.usage.total_tokens == live.token_usage.total_tokens
        )
        assert raw_page_spans
        assert session.wiring.run_execution_registry.owner_count == 0
        monkeypatch.setattr(
            event_log_type,
            "read_range_snapshot",
            original_read_range_snapshot,
        )
        monkeypatch.setattr(
            event_log_type,
            "read_raw_events_by_types",
            original_read_raw_events_by_types,
        )
        await core.shutdown()

    asyncio.run(run())


def test_plan_interaction_uses_distinct_generations_for_two_resumes(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:plan-first",
                        "name": "ask_plan_question",
                        "arguments": json.dumps({"question": "First choice?"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call:plan-second",
                        "name": "ask_plan_question",
                        "arguments": json.dumps({"question": "Second choice?"}),
                    }
                ]
            },
            {"text": "plan complete"},
        ]
    )
    core = _core(monkeypatch, transport)

    async def run():
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:two-plan-resumes",
        )
        session.wiring.agent_runtime.budget = LoopBudget(
            max_plan_interactions_per_run=3
        )
        session.enter_plan(reason="two resume contract")
        first = await session.run_turn("ask both questions")
        first_pending = session.get_pending_interaction()
        assert isinstance(first_pending, PendingPlanInteraction)

        second = await session.resolve_plan_interaction(
            PlanQuestionResolution(
                interaction_id=first_pending.interaction_id,
                answer_text="first answer",
            )
        )
        second_pending = session.get_pending_interaction()
        assert isinstance(second_pending, PendingPlanInteraction)
        assert isinstance(first, RunSuspendedOutcome)
        assert isinstance(second, RunSuspendedOutcome)
        assert second.owner_identity.run_id == first.owner_identity.run_id

        terminal = await session.resolve_plan_interaction(
            PlanQuestionResolution(
                interaction_id=second_pending.interaction_id,
                answer_text="second answer",
            )
        )
        assert terminal.run_id == first.owner_identity.run_id
        events = session.replay_events()
        await core.shutdown()
        return first.owner_identity.run_id, events

    run_id, events = asyncio.run(run())
    control = [
        event
        for event in events
        if isinstance(event, ModelCallControlDispositionResolvedEvent)
        and event.run_id == run_id
    ]
    assert [event.run_execution_activation.segment_generation for event in control] == [
        1,
        2,
        3,
    ]
    assert [
        event.run_execution_activation.activation_owner_kind for event in control
    ] == ["host_run_boundary", "host_resume_boundary", "host_resume_boundary"]
    assert (
        len(
            [
                event
                for event in events
                if isinstance(event, RunInteractionResumeBoundaryEvent)
                and event.run_id == run_id
            ]
        )
        == 2
    )


def test_cancel_after_run_start_commit_terminalizes_stable_run(
    tmp_path, monkeypatch
) -> None:
    core = _core(monkeypatch, ScriptedTransport([{"text": "unused"}]))

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:cancel-start")
        runtime = session.wiring.runtime_wiring.runtime_session
        original = type(runtime).write_events
        injected = False

        async def commit_then_cancel(
            self,
            events,
            *,
            expected_last_sequence=None,
            state=None,
            transaction_companion=None,
        ):
            nonlocal injected
            should_cancel = not injected and any(
                isinstance(event, RunStartEvent) for event in events
            )
            if should_cancel:
                injected = True
                result = await self.write_events(
                    tuple(events),
                    expected_last_sequence=expected_last_sequence,
                )
                raise EventWriteCancelled(
                    EventBatchCommitOutcome(
                        status="full",
                        deadline_monotonic=time.monotonic(),
                        result=result,
                    )
                )
            return await original(
                self,
                events,
                expected_last_sequence=expected_last_sequence,
                transaction_companion=transaction_companion,
            )

        monkeypatch.setattr(type(runtime), "write_events", commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await session.run_turn("cancel after commit")
        events = session.replay_events()
        started = next(event for event in events if isinstance(event, RunStartEvent))
        ended = next(event for event in events if isinstance(event, RunEndEvent))
        await core.shutdown()
        return started, ended

    started, ended = asyncio.run(run())
    assert ended.id == started.terminal_run_end_event_id
    assert ended.status == "failed"


def test_cancel_after_resume_boundary_commit_terminalizes_original_run(
    tmp_path, monkeypatch
) -> None:
    core = _core(
        monkeypatch,
        ScriptedTransport(
            [
                {
                    "tool_calls": [
                        {
                            "id": "call:ask-cancel",
                            "name": "terminal",
                            "arguments": json.dumps({"command": "printf ok"}),
                        }
                    ]
                },
                {"text": "unused"},
            ]
        ),
    )

    async def run():
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:cancel-resume",
            policy=_trusted_terminal_ask_policy(),
        )
        await session.run_turn("suspend")
        pending = session.get_pending_approval()
        assert pending is not None
        runtime = session.wiring.runtime_wiring.runtime_session
        original = type(runtime).write_events
        injected = False

        async def commit_then_cancel(
            self,
            events,
            *,
            expected_last_sequence=None,
            state=None,
            transaction_companion=None,
        ):
            nonlocal injected
            should_cancel = not injected and any(
                isinstance(event, RunInteractionResumeBoundaryEvent) for event in events
            )
            if should_cancel:
                injected = True
                result = await self.write_events(
                    tuple(events),
                    expected_last_sequence=expected_last_sequence,
                )
                raise EventWriteCancelled(
                    EventBatchCommitOutcome(
                        status="full",
                        deadline_monotonic=time.monotonic(),
                        result=result,
                    )
                )
            return await original(
                self,
                events,
                expected_last_sequence=expected_last_sequence,
                transaction_companion=transaction_companion,
            )

        monkeypatch.setattr(type(runtime), "write_events", commit_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(tool_call_id=call.id, confirmed=True)
                        for call in pending.tool_calls
                    ),
                )
            )
        events = session.replay_events()
        started = next(event for event in events if isinstance(event, RunStartEvent))
        boundary = next(
            event
            for event in events
            if isinstance(event, RunInteractionResumeBoundaryEvent)
        )
        ended = next(event for event in events if isinstance(event, RunEndEvent))
        await core.shutdown()
        return started, boundary, ended

    started, boundary, ended = asyncio.run(run())
    assert boundary.boundary.original_run_start_event_id == started.id
    assert ended.id == started.terminal_run_end_event_id
    assert ended.status == "failed"


def test_stream_observer_is_bounded_and_detach_does_not_cancel_run(
    tmp_path, monkeypatch
) -> None:
    from pulsara_agent.host.session import _STREAM_QUEUE_MAX_ITEMS

    total_deltas = _STREAM_QUEUE_MAX_ITEMS * 3

    class BurstTransport:
        api = "scripted"
        binding_id = "test.scripted"
        contract_version = "v1"

        def __init__(self) -> None:
            self.produced = 0

        async def stream(self, *, call, context, event_context):
            del call
            yield RawProviderTextBlockStart(
                **event_context.event_fields(), block_id="text:burst"
            )
            for _ in range(total_deltas):
                self.produced += 1
                yield RawProviderTextDelta(
                    **event_context.event_fields(),
                    block_id="text:burst",
                    delta="x",
                )
            yield RawProviderTextBlockEnd(
                **event_context.event_fields(), block_id="text:burst"
            )

    transport = BurstTransport()
    core = _core(monkeypatch, transport)  # type: ignore[arg-type]

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:bounded-stream")
        stream = session.stream_turn("burst")
        await anext(stream)
        await asyncio.sleep(0.05)
        produced_while_attached = transport.produced
        assert produced_while_attached < total_deltas
        assert produced_while_attached <= _STREAM_QUEUE_MAX_ITEMS + 4

        await stream.aclose()  # transport observer detaches; execution stays owned
        service = session.wiring.run_activation_service
        assert service is not None
        view = service.current_host_run_view()
        boundary_task = session._current_boundary_task()
        if view is not None and view.active_driver_running:
            await service.wait_active_driver(view.run_id, timeout_seconds=10.0)
        else:
            assert boundary_task is not None
            await asyncio.wait_for(asyncio.shield(boundary_task), timeout=10.0)
        assert service.active_host_run_view() is None
        await core.shutdown()
        return produced_while_attached, transport.produced

    produced_while_attached, produced_after_detach = asyncio.run(run())
    assert produced_while_attached < total_deltas
    assert produced_after_detach == total_deltas


def test_detached_stream_remains_active_and_blocks_second_run(
    tmp_path, monkeypatch
) -> None:
    paused = asyncio.Event()
    release = asyncio.Event()

    class PausingTransport:
        api = "scripted"
        binding_id = "test.scripted"
        contract_version = "v1"

        async def stream(self, *, call, context, event_context):
            del call
            paused.set()
            await release.wait()
            if False:
                yield

    core = _core(monkeypatch, PausingTransport())  # type: ignore[arg-type]

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:detached-stream")
        stream = session.stream_turn("pause")
        await anext(stream)
        await paused.wait()
        service = session.wiring.run_activation_service
        assert service is not None
        view = service.active_host_run_view()
        await stream.aclose()
        assert view is not None and view.active_driver_running
        assert service.active_host_run_view() is not None
        with pytest.raises(HostSessionBusyError):
            await session.run_turn("must not overlap")
        release.set()
        await service.wait_active_driver(view.run_id, timeout_seconds=30.0)
        await core.shutdown()
        return session.active_run_id

    assert asyncio.run(run()) is None


def test_host_close_aborts_run_after_stream_observer_detaches(
    tmp_path, monkeypatch
) -> None:
    paused = asyncio.Event()

    class PausingTransport:
        api = "scripted"
        binding_id = "test.scripted"
        contract_version = "v1"

        async def stream(self, *, call, context, event_context):
            del call
            paused.set()
            await asyncio.Event().wait()
            if False:
                yield

    core = _core(monkeypatch, PausingTransport())  # type: ignore[arg-type]

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:detached-close")
        stream = session.stream_turn("pause")
        await anext(stream)
        await paused.wait()
        await stream.aclose()
        service = session.wiring.run_activation_service
        assert service is not None and service.active_host_run_view() is not None
        await core.close_session(session.host_session_id)
        return session.replay_events()

    events = asyncio.run(run())
    run_ends = [event for event in events if isinstance(event, RunEndEvent)]
    assert run_ends and run_ends[-1].abort_kind == AbortKind.HOST_TEARDOWN.value


# --- §12.4 workspace close ----------------------------------------------------


def test_workspace_close_rejects_concurrent_open_in_same_workspace(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport([{"text": "a"}, {"text": "b"}])
    core = _core(monkeypatch, transport)
    reached = asyncio.Event()
    release = asyncio.Event()
    original_close = HostCore.close_session

    async def barrier_close(self, host_session_id):
        reached.set()
        await release.wait()
        return await original_close(self, host_session_id)

    monkeypatch.setattr(HostCore, "close_session", barrier_close)

    async def run():
        a = await _open(core, tmp_path, host_session_id="host:a")
        close_task = asyncio.create_task(
            core.close_workspace(a.workspace.workspace_key)
        )
        await reached.wait()  # supervisor is CLOSING, paused inside close_session
        with pytest.raises(WorkspaceClosingError):
            await _open(core, tmp_path, host_session_id="host:b")
        release.set()
        await close_task
        return await core.list_sessions()

    sessions = asyncio.run(run())
    assert sessions == []


def test_workspace_close_invalidates_lease_acquired_by_unpublished_open(
    tmp_path, monkeypatch
) -> None:
    """The open-before-close interleaving must lose at its publish boundary."""
    core = _core(monkeypatch, ScriptedTransport([]))
    attached = asyncio.Event()
    release = asyncio.Event()
    original_attach = HostCore._attach_supervisor

    async def barrier_after_attach(self, workspace, host_session_id, conversation_id):
        lease = await original_attach(self, workspace, host_session_id, conversation_id)
        attached.set()
        await release.wait()
        return lease

    monkeypatch.setattr(HostCore, "_attach_supervisor", barrier_after_attach)

    async def run():
        opening = asyncio.create_task(
            _open(core, tmp_path, host_session_id="host:mid-open")
        )
        await attached.wait()
        workspace_key, supervisor = next(iter(core._supervisors.items()))
        manager = supervisor.terminal_sessions
        await core.close_workspace(workspace_key)
        with pytest.raises(RuntimeError, match="closed"):
            manager.get_or_create(owner_host_session_id="host:mid-open")
        release.set()
        with pytest.raises(WorkspaceClosingError):
            await opening
        return (
            await core.list_sessions(),
            await core.list_workspace_terminal_snapshots(),
        )

    sessions, supervisors = asyncio.run(run())
    assert sessions == []
    assert supervisors == []


# --- §12.5 HostCore shutdown --------------------------------------------------


def test_shutdown_gate_rejects_start_and_continue_facades_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    transport = ScriptedTransport([{"text": "ok"}])
    core = _core(monkeypatch, transport)

    async def run():
        await _open(core, tmp_path, host_session_id="host:gate")
        await core.shutdown()
        assert core.lifecycle is HostCoreLifecycle.CLOSED
        with pytest.raises(RuntimeError, match="closing|closed"):
            await _open(core, tmp_path, host_session_id="host:after")
        with pytest.raises(RuntimeError, match="closing|closed"):
            await core.set_permission_mode("host:gate", "read-only")
        with pytest.raises(RuntimeError, match="closing|closed"):
            await core.enter_plan("host:gate")
        with pytest.raises(RuntimeError, match="closing|closed"):
            await core.resolve_approval(
                "host:gate",
                ApprovalResolution(approval_id="x", decisions=()),
            )
        await core.shutdown()  # idempotent
        return await core.list_sessions()

    sessions = asyncio.run(run())
    assert sessions == []


def test_concurrent_shutdown_waits_for_owner_even_when_cleanup_fails(
    monkeypatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))
    close_entered = asyncio.Event()
    release_close = asyncio.Event()

    class FailingResources:
        fail = True

        async def aclose(self):
            close_entered.set()
            await release_close.wait()
            if self.fail:
                raise RuntimeError("retrieval close boom")

    resources = FailingResources()

    async def run():
        lease = await core._get_process_resource_lease()
        lease.retrieval_resources = resources  # type: ignore[attr-defined]
        owner = asyncio.create_task(core.shutdown())
        await close_entered.wait()
        waiter = asyncio.create_task(core.shutdown())
        await asyncio.sleep(0)
        assert not waiter.done()
        release_close.set()
        with pytest.raises(RuntimeError, match="retrieval close boom"):
            await owner
        with pytest.raises(RuntimeError, match="retrieval close boom"):
            await asyncio.wait_for(waiter, timeout=0.2)
        assert core.lifecycle is HostCoreLifecycle.CLOSING
        resources.fail = False
        await core.shutdown()
        return core.lifecycle

    assert asyncio.run(run()) is HostCoreLifecycle.CLOSED


def test_projection_close_block_retains_retrieval_dependencies(monkeypatch) -> None:
    core = _core(monkeypatch, ScriptedTransport([]))

    class ProjectionService:
        blocked = True

        async def aclose(self, *, deadline_monotonic):
            del deadline_monotonic
            if self.blocked:
                raise RuntimeError("projection physical owner still active")

    class RetrievalResources:
        close_count = 0

        async def aclose(self):
            self.close_count += 1

    projection = ProjectionService()
    resources = RetrievalResources()

    async def run():
        lease = await core._get_process_resource_lease()
        lease.projection_service = projection  # type: ignore[attr-defined]
        lease.retrieval_resources = resources  # type: ignore[attr-defined]
        with pytest.raises(
            RuntimeError,
            match="projection physical owner still active",
        ):
            await core.shutdown()
        assert core.lifecycle is HostCoreLifecycle.CLOSING
        assert core._process_resource_lease is lease
        assert lease.projection_service is projection
        assert lease.retrieval_resources is resources
        assert resources.close_count == 0

        projection.blocked = False
        await core.shutdown()
        assert core.lifecycle is HostCoreLifecycle.CLOSED
        assert resources.close_count == 1
        assert core._process_resource_lease is None

    asyncio.run(run())


def test_open_parked_at_attach_then_shutdown_leaves_no_session(
    tmp_path, monkeypatch
) -> None:
    # Linearization (P0-2): an open that passed the entry check but is still mid
    # transaction when shutdown completes must roll back, never leak a session
    # into a closed HostCore.
    transport = ScriptedTransport([{"text": "late"}])
    core = _core(monkeypatch, transport)
    reached = asyncio.Event()
    release = asyncio.Event()
    original_attach = HostCore._attach_supervisor

    async def barrier_attach(self, workspace, host_session_id, conversation_id):
        if host_session_id == "host:late":
            reached.set()
            await release.wait()
        return await original_attach(self, workspace, host_session_id, conversation_id)

    monkeypatch.setattr(HostCore, "_attach_supervisor", barrier_attach)

    async def run():
        open_task = asyncio.create_task(
            _open(core, tmp_path, host_session_id="host:late")
        )
        await reached.wait()
        await core.shutdown()  # completes while the open is parked
        release.set()
        with pytest.raises(RuntimeError):
            await open_task  # the open aborts instead of leaking
        return await core.list_sessions()

    sessions = asyncio.run(run())
    assert sessions == []


def test_nonstream_close_waits_for_abort_finalization(tmp_path, monkeypatch) -> None:
    transport = ScriptedTransport([{"text": "late"}], delay=0.3)
    core = _core(monkeypatch, transport)
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()

    async def run():
        session = await _open(core, tmp_path, host_session_id="host:nonstream-close")
        runtime_type = type(session.wiring.agent_runtime)
        original_abort = runtime_type.abort_run

        async def delayed_abort(self, state, *, reason):
            abort_started.set()
            await release_abort.wait()
            return await original_abort(self, state, reason=reason)

        monkeypatch.setattr(runtime_type, "abort_run", delayed_abort)
        caller = asyncio.create_task(session.run_turn("hold"))
        service = session.wiring.run_activation_service
        assert service is not None
        for _ in range(200):
            if service.active_host_run_view() is not None:
                break
            await asyncio.sleep(0.005)
        assert service.active_host_run_view() is not None
        closing = asyncio.create_task(core.close_session(session.host_session_id))
        await abort_started.wait()
        assert not closing.done()
        assert not session.closed
        release_abort.set()
        await closing
        result = await caller
        return result, session.replay_events()

    result, events = asyncio.run(run())
    assert result.status.value == "aborted"
    run_ends = [event for event in events if isinstance(event, RunEndEvent)]
    assert run_ends and run_ends[-1].abort_kind == AbortKind.HOST_TEARDOWN.value


# --- §12.6 event-loop responsiveness -----------------------------------------


def test_slow_owner_release_runs_off_lock_and_does_not_block_other_workspace(
    tmp_path, monkeypatch
) -> None:
    ws_a = tmp_path / "A"
    ws_a.mkdir()
    ws_b = tmp_path / "B"
    ws_b.mkdir()
    transport = ScriptedTransport([{"text": "a"}, {"text": "b"}])
    core = _core(monkeypatch, transport)
    import pulsara_agent.runtime.terminal.manager as manager_mod

    original_release = manager_mod.TerminalSessionManager.release_owner

    def slow_release(self, owner_host_session_id):
        time.sleep(0.3)  # synchronous, would block the loop if run on it
        return original_release(self, owner_host_session_id)

    monkeypatch.setattr(
        manager_mod.TerminalSessionManager, "release_owner", slow_release
    )

    async def run():
        await _open(core, ws_a, host_session_id="host:a")
        close_task = asyncio.create_task(core.close_session("host:a"))
        await asyncio.sleep(0.02)  # let the close reach the off-lock kill
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await _open(core, ws_b, host_session_id="host:b")  # different workspace
        elapsed = loop.time() - t0
        await close_task
        await core.shutdown()
        return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 0.2  # not serialized behind the 0.3s synchronous teardown


def test_release_owner_is_thread_safe_with_concurrent_sibling_session_create(
    tmp_path,
) -> None:
    manager = TerminalSessionManager(tmp_path, max_sessions=8)
    released_session = manager.get_or_create("one", owner_host_session_id="host:a")
    manager.get_or_create("two", owner_host_session_id="host:a")
    iteration_started = threading.Event()

    class SlowIterDict(dict):
        def __iter__(self):
            iterator = super().__iter__()
            yield next(iterator)
            iteration_started.set()
            time.sleep(0.1)
            yield from iterator

    manager._sessions = SlowIterDict(manager._sessions)
    errors: list[BaseException] = []

    def release_owner():
        try:
            manager.release_owner("host:a")
        except BaseException as exc:
            errors.append(exc)

    def create_sibling_session():
        try:
            manager.get_or_create("work", owner_host_session_id="host:b")
        except BaseException as exc:
            errors.append(exc)

    release_thread = threading.Thread(target=release_owner)
    release_thread.start()
    assert iteration_started.wait(timeout=1)
    create_thread = threading.Thread(target=create_sibling_session)
    create_thread.start()
    release_thread.join(timeout=2)
    create_thread.join(timeout=2)

    assert not release_thread.is_alive()
    assert not create_thread.is_alive()
    assert errors == []
    assert manager.owner_session_counts() == {"host:b": 1}
    stale_result = released_session.execute(
        TerminalRequest(command="printf should-not-run")
    )
    assert stale_result.status is TerminalStatus.BLOCKED
    assert "released" in (stale_result.error or "")


# --- §12.7 shared capacity ----------------------------------------------------


def test_shared_capacity_limit_error_reports_owner_distribution(
    tmp_path, monkeypatch
) -> None:
    # Two host sessions sharing one workspace each create named terminal sessions
    # until the workspace-wide ceiling trips; the error must name the owners.
    replies: list[dict] = []
    for owner_idx, name in [
        ("a", "one"),
        ("a", "two"),
        ("b", "three"),
        ("b", "four"),
        ("b", "five"),
    ]:
        replies.append(
            {
                "tool_calls": [
                    {
                        "id": f"call:{name}",
                        "name": "terminal",
                        "arguments": json.dumps(
                            {"command": "printf ok", "terminal_session_id": name}
                        ),
                    }
                ]
            }
        )
        replies.append({"text": f"ok {name}"})
    core = _core(monkeypatch, ScriptedTransport(replies))

    async def run():
        a = await _open(
            core, tmp_path, host_session_id="host:a", policy=_trusted_terminal_policy()
        )
        b = await _open(
            core, tmp_path, host_session_id="host:b", policy=_trusted_terminal_policy()
        )
        await a.run_turn("named one")
        await a.run_turn("named two")
        await b.run_turn("named three")
        await b.run_turn("named four")
        await b.run_turn("named five")  # 5th named session over the default max of 4
        snapshot = (await core.list_workspace_terminal_snapshots())[0]
        await core.shutdown()
        return snapshot

    snapshot = asyncio.run(run())
    # Owner distribution is visible for diagnostics even at/under the ceiling.
    assert snapshot.terminal_session_count == 4
    assert snapshot.owner_session_distribution.get("host:a", 0) >= 1
    assert snapshot.owner_session_distribution.get("host:b", 0) >= 1
