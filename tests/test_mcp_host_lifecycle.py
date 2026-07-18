from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from tests.test_agent_runtime_loop import _pending_mcp_installation_audit
from tests.test_host_lifecycle_contract import (
    ScriptedTransport,
    _core,
    _open,
    _trusted_terminal_ask_policy,
)

from pulsara_agent.host import (
    HostCore,
    HostSessionRegistry,
    ResumableSessionSummary,
)
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime import EventPublicationAfterCommitError
from pulsara_agent.runtime import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.event import (
    ContextWindowOpenedEvent,
    McpCapabilitySnapshotInstalledEvent,
    RolloutBudgetAccountOpenedEvent,
    RunStartEvent,
)
from pulsara_agent.primitives.mcp import McpServerLifecycleTimingFact
from pulsara_agent.runtime.mcp.manager import MockMcpClientManager
from pulsara_agent.runtime.plan import McpInputRequiredInteractionResolution
from pulsara_agent.runtime.mcp.types import (
    McpDiscoveredTool,
    McpInputRequestDTO,
    McpInputRequired,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpRequiredStartupError,
    McpRequiredStartupResult,
    McpServerCandidate,
    McpServerConfig,
    McpServerRuntimeSpec,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpToolAnnotations,
    event_safe_mcp_config_fingerprint,
    new_mcp_slot,
    runtime_mcp_config_fingerprint,
    snapshot_semantic_fingerprint,
)


def _config(*, required: bool) -> McpServerConfig:
    return McpServerConfig(
        server_id="slow-docs",
        transport=McpStdioConfig(command="fake-mcp"),
        required=required,
        connect_timeout_ms=100,
        discovery_timeout_ms=100,
        startup_deadline_ms=250,
    )


def _install_config(monkeypatch: pytest.MonkeyPatch, config: McpServerConfig) -> None:
    import pulsara_agent.host.core as host_core
    import pulsara_agent.host.session as host_session

    monkeypatch.setattr(
        host_core,
        "load_mcp_server_configs",
        lambda **_kwargs: (config,),
    )
    monkeypatch.setattr(
        host_session,
        "load_mcp_server_configs",
        lambda **_kwargs: (config,),
    )


def _enable_fake_durable_manifest(
    core: HostCore,
    monkeypatch: pytest.MonkeyPatch,
    store,
) -> None:
    import pulsara_agent.host.core as host_core

    original_build = host_core.build_agent_runtime_wiring

    def build_without_durable_storage(settings, workspace_root, **kwargs):
        kwargs["durable"] = False
        kwargs["retrieval_resources"] = None
        kwargs["governance_coordinator"] = None
        return original_build(settings, workspace_root, **kwargs)

    async def no_retrieval_resources(_self):
        return None

    core.durable = True
    monkeypatch.setattr(
        host_core,
        "build_agent_runtime_wiring",
        build_without_durable_storage,
    )
    monkeypatch.setattr(
        HostCore,
        "_get_retrieval_resources",
        no_retrieval_resources,
    )
    monkeypatch.setattr(HostCore, "_manifest_store", lambda _self: store)


async def _queue_ready_candidate(
    supervisor: McpServerSupervisor,
    runtime,
    *,
    handler=None,
) -> None:
    config = runtime.spec.config
    tool = McpDiscoveredTool(
        server_id=config.server_id,
        name="lookup",
        description="lookup",
        input_schema={"type": "object", "properties": {}},
        annotations=McpToolAnnotations(read_only_hint=True),
    )
    timing = McpServerLifecycleTimingFact(
        queued_at_utc=runtime.queued_at_utc,
        connect_started_at_utc="2026-01-01T00:00:00Z",
        connect_ended_at_utc="2026-01-01T00:00:00Z",
        discovery_started_at_utc="2026-01-01T00:00:00Z",
        discovery_ended_at_utc="2026-01-01T00:00:00.010000Z",
        completed_at_utc="2026-01-01T00:00:00.010000Z",
        connect_duration_seconds=0,
        discovery_duration_seconds=0.01,
        total_duration_seconds=0.01,
    )
    snapshot = McpServerSnapshot(
        snapshot_id=f"mcp_snapshot:{runtime.attempt.reconcile_attempt_id}",
        server_id=config.server_id,
        config_epoch=runtime.attempt.config_epoch,
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
            server_id=config.server_id,
            status=McpServerStatus.READY,
            tools=(tool,),
        ),
        reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
        discovery_generation=runtime.attempt.reserved_discovery_generation,
        status=McpServerStatus.READY,
        required=config.required,
        tools=(tool,),
        timing=timing,
    )
    manager = MockMcpClientManager(
        _snapshots=(snapshot,),
        handlers={(config.server_id, "lookup"): handler or (lambda arguments: "ok")},
    )
    spec = McpServerRuntimeSpec(
        config=config,
        runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
    )
    candidate = McpServerCandidate(
        ticket_id=runtime.ticket_id,
        config_epoch=runtime.attempt.config_epoch,
        reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
        reserved_discovery_generation=runtime.attempt.reserved_discovery_generation,
        server_snapshot=snapshot,
        runtime_spec=spec,
        manager_slot=new_mcp_slot(spec=spec, snapshot=snapshot, manager=manager),
        trigger=runtime.trigger,
        request_count=1,
        page_count=1,
    )
    with supervisor._state_lock:
        current = supervisor._current_attempts.get(config.server_id)
        if current is runtime:
            supervisor._candidates.append(candidate)


def test_optional_mcp_does_not_block_host_session_open(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_entered = asyncio.Event()
    worker_release = asyncio.Event()

    async def slow_worker(self, runtime):
        worker_entered.set()
        await worker_release.wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", slow_worker)
    core = _core(monkeypatch, ScriptedTransport([{"text": "done"}]))

    async def run() -> None:
        started = time.monotonic()
        session = await asyncio.wait_for(_open(core, tmp_path), timeout=0.2)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        await asyncio.wait_for(worker_entered.wait(), timeout=0.2)
        assert session.summary()["mcp"]["servers"][0]["status"] == "starting"
        worker_release.set()
        await core.shutdown()

    asyncio.run(run())


def test_required_mcp_failure_does_not_write_open_manifest(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=True))

    class ManifestStore:
        def __init__(self) -> None:
            self.upserts: list[str] = []

        def upsert_open_manifest(self, **kwargs) -> None:
            self.upserts.append(str(kwargs["runtime_session_id"]))

        def mark_closed(self, _runtime_session_id: str) -> None:
            raise AssertionError("required initialization failed before manifest write")

    store = ManifestStore()
    core = _core(monkeypatch, ScriptedTransport([]))
    _enable_fake_durable_manifest(core, monkeypatch, store)

    async def fail_required(self, ticket):
        raise McpRequiredStartupError(
            server_ids=ticket.required_server_ids,
            reason_code="mcp_required_generation_unavailable",
        )

    monkeypatch.setattr(McpServerSupervisor, "await_required", fail_required)

    async def run() -> None:
        with pytest.raises(McpRequiredStartupError):
            await _open(core, tmp_path, host_session_id="host:required-manifest")
        assert store.upserts == []
        assert await core.registry.list_manifest_close_tombstones() == ()
        await core.shutdown()

    asyncio.run(run())


def test_manifest_written_before_publish_failure_is_closed_or_tombstoned(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnceManifestStore:
        def __init__(self) -> None:
            self.upserts: list[str] = []
            self.close_calls = 0
            self.closed: list[str] = []

        def upsert_open_manifest(self, **kwargs) -> None:
            self.upserts.append(str(kwargs["runtime_session_id"]))

        def mark_closed(self, runtime_session_id: str) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("synthetic manifest close outage")
            self.closed.append(runtime_session_id)

        def list_resumable(self, **_kwargs):
            if not self.upserts:
                return []
            summaries = [
                ResumableSessionSummary(
                    runtime_session_id=self.upserts[0],
                    conversation_id="conversation:host:publish-manifest",
                    workspace_kind="project",
                    workspace_root=str(tmp_path),
                    display_label="failed open",
                    memory_domain_id="u_test",
                    model_role="flash",
                    permission_mode="bypass-permissions",
                    created_at=None,
                    last_active_at=None,
                    closed_at=None,
                    archived=False,
                    latest_run_status=None,
                    latest_run_id=None,
                ),
                ResumableSessionSummary(
                    runtime_session_id="runtime:healthy",
                    conversation_id="conversation:healthy",
                    workspace_kind="project",
                    workspace_root=str(tmp_path),
                    display_label="healthy",
                    memory_domain_id="u_test",
                    model_role="flash",
                    permission_mode="bypass-permissions",
                    created_at=None,
                    last_active_at=None,
                    closed_at=None,
                    archived=False,
                    latest_run_status="finished",
                    latest_run_id="run:healthy",
                ),
            ]
            return summaries[: int(_kwargs["limit"])]

    store = FailOnceManifestStore()
    core = _core(monkeypatch, ScriptedTransport([]))
    _enable_fake_durable_manifest(core, monkeypatch, store)

    async def fail_publish(self, reservation, session):
        del self, reservation, session
        raise RuntimeError("synthetic registry publish failure")

    monkeypatch.setattr(HostSessionRegistry, "publish", fail_publish)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="registry publish failure"):
            await _open(core, tmp_path, host_session_id="host:publish-manifest")
        assert len(store.upserts) == 1
        runtime_session_id = store.upserts[0]
        assert await core.registry.list_manifest_close_tombstones() == (
            ("host:publish-manifest", runtime_session_id),
        )
        resumable = await core.list_resumable_sessions(limit=1)
        assert [item.runtime_session_id for item in resumable] == ["runtime:healthy"]

        await core.close_session(
            "host:publish-manifest",
            close_conversation=True,
        )
        assert store.closed == [runtime_session_id]
        assert await core.registry.list_manifest_close_tombstones() == ()
        await core.shutdown()

    asyncio.run(run())


def test_resume_audit_post_commit_publication_failure_acknowledges_pending(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _core(
        monkeypatch,
        ScriptedTransport(
            [
                {
                    "tool_calls": [
                        {
                            "id": "call:resume-audit",
                            "name": "terminal",
                            "arguments": json.dumps({"command": "printf ok"}),
                        }
                    ]
                },
                {"text": "unused after publication failure"},
            ]
        ),
    )

    class FailInstallationAudit:
        async def on_published_event(self, published: RuntimePublishedEvent) -> None:
            if isinstance(
                published.event,
                McpCapabilitySnapshotInstalledEvent,
            ):
                raise RuntimeError("synthetic resume audit observer failure")

    async def run() -> None:
        session = await _open(
            core,
            tmp_path,
            host_session_id="host:resume-audit",
            policy=_trusted_terminal_ask_policy(),
        )
        await session.run_turn("suspend before MCP audit")
        pending = session.get_pending_approval()
        assert pending is not None
        runtime_session = session.wiring.runtime_wiring.runtime_session
        runtime_session.set_mcp_installation_contract(
            installation_id="mcp_installation:atomic",
            pending_audit=_pending_mcp_installation_audit(),
        )
        failing = FailInstallationAudit()
        runtime_session.publisher.subscribe(failing)

        with pytest.raises(EventPublicationAfterCommitError):
            await session.resolve_approval(
                ApprovalResolution(
                    approval_id=pending.approval_id,
                    decisions=tuple(
                        ToolApprovalDecision(
                            tool_call_id=tool_call.id,
                            confirmed=True,
                        )
                        for tool_call in pending.tool_calls
                    ),
                )
            )

        assert runtime_session._pending_mcp_installation_audits == []
        runtime_session.publisher.unsubscribe(failing)
        assert (
            len(
                [
                    event
                    for event in runtime_session.event_log.iter()
                    if isinstance(event, McpCapabilitySnapshotInstalledEvent)
                ]
            )
            == 1
        )
        await core.shutdown()

    asyncio.run(run())


def test_background_ready_installs_only_at_next_safe_point_and_is_audited(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    core = _core(monkeypatch, ScriptedTransport([{"text": "done"}]))

    async def run() -> None:
        session = await _open(core, tmp_path)
        initial = session.wiring.runtime_wiring.mcp_installation
        assert initial.snapshots[0].status is McpServerStatus.STARTING
        assert not initial.tools

        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        assert (
            session.wiring.runtime_wiring.mcp_installation.installation_id
            == initial.installation_id
        )

        await session.run_turn("install ready MCP")
        installed = session.wiring.runtime_wiring.mcp_installation
        assert installed.installation_id != initial.installation_id
        assert installed.snapshots[0].status is McpServerStatus.READY
        assert [tool.name for tool in installed.tools] == ["mcp__slow-docs__lookup"]

        events = session.wiring.runtime_wiring.runtime_session.event_log.iter()
        run_start = next(event for event in events if isinstance(event, RunStartEvent))
        audit = next(
            event
            for event in events
            if isinstance(event, McpCapabilitySnapshotInstalledEvent)
            and event.installation_id == installed.installation_id
        )
        assert run_start.mcp_installation_id == audit.installation_id
        assert run_start.sequence is not None and audit.sequence is not None
        opening_batch = tuple(
            event
            for event in events
            if run_start.sequence <= (event.sequence or 0) <= audit.sequence
        )
        assert tuple(type(event) for event in opening_batch) == (
            RunStartEvent,
            ContextWindowOpenedEvent,
            RolloutBudgetAccountOpenedEvent,
            McpCapabilitySnapshotInstalledEvent,
        )
        assert tuple(event.sequence for event in opening_batch) == tuple(
            range(run_start.sequence, audit.sequence + 1)
        )
        await core.shutdown()

    asyncio.run(run())


def test_mcp_lifecycle_prompt_tracks_the_run_frozen_tool_schema(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    transport = ScriptedTransport(
        [
            {"text": "starting acknowledged"},
            {"text": "ready acknowledged"},
        ]
    )
    core = _core(monkeypatch, transport)

    def visible_text(index: int) -> str:
        context = transport.contexts[index]
        return "\n".join(
            [
                context.system_prompt or "",
                *(
                    text
                    for message in context.messages
                    for text in message.content
                    if isinstance(text, str)
                ),
            ]
        )

    async def run() -> None:
        session = await _open(core, tmp_path)

        await session.run_turn("Can you see MCP while it is starting?")
        first = transport.contexts[0]
        assert not any(tool.name.startswith("mcp__") for tool in first.tools)
        first_text = visible_text(0)
        assert "server=slow-docs; status=starting; installed_tool_count=0" in first_text
        assert "tools are NOT available in this run" in first_text
        assert "Do not infer current MCP availability from prior messages" in first_text

        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        await session.run_turn("Can you see MCP after the next safe point?")
        second = transport.contexts[1]
        assert [
            tool.name for tool in second.tools if tool.name.startswith("mcp__")
        ] == ["mcp__slow-docs__lookup"]
        second_text = visible_text(1)
        assert "server=slow-docs; status=ready; installed_tool_count=1" in second_text
        assert "actual tool schema remains the sole authority" in second_text
        await core.shutdown()

    asyncio.run(run())


def test_optional_reconfigure_revokes_old_surface_without_waiting_for_discovery(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = [_config(required=False)]
    import pulsara_agent.host.core as host_core
    import pulsara_agent.host.session as host_session

    monkeypatch.setattr(
        host_core,
        "load_mcp_server_configs",
        lambda **_kwargs: tuple(configs),
    )
    monkeypatch.setattr(
        host_session,
        "load_mcp_server_configs",
        lambda **_kwargs: tuple(configs),
    )
    first_release = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()

    async def staged_worker(self, runtime):
        if runtime.attempt.reserved_discovery_generation == 1:
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()
        await _queue_ready_candidate(self, runtime)

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", staged_worker)
    core = _core(
        monkeypatch,
        ScriptedTransport([{"text": "first"}, {"text": "second"}]),
    )

    async def run() -> None:
        session = await _open(core, tmp_path)
        first_release.set()
        await asyncio.wait_for(
            session.mcp_supervisor._workers["slow-docs"],
            timeout=0.2,
        )
        await session.run_turn("install first generation")
        old_slot = session.mcp_supervisor.slots()[0]
        assert session.wiring.runtime_wiring.mcp_installation.tools

        configs[0] = replace(configs[0], tool_timeout_ms=2_000)
        result = await session.run_turn("do not wait for optional replacement")
        assert result.final_text == "second"
        await asyncio.wait_for(second_entered.wait(), timeout=0.2)

        installation = session.wiring.runtime_wiring.mcp_installation
        assert installation.snapshots[0].status is McpServerStatus.STARTING
        assert not installation.tools
        assert old_slot.lifecycle == "closed"

        second_release.set()
        await core.shutdown()

    asyncio.run(run())


def test_required_mcp_blocks_session_open_until_required_wait_finishes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=True))
    required_entered = asyncio.Event()
    required_release = asyncio.Event()

    async def idle_worker(self, runtime):
        await required_release.wait()

    async def controlled_required(self, ticket):
        required_entered.set()
        await required_release.wait()
        return McpRequiredStartupResult(ready_server_ids=ticket.required_server_ids)

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", idle_worker)
    monkeypatch.setattr(McpServerSupervisor, "await_required", controlled_required)
    core = _core(monkeypatch, ScriptedTransport([{"text": "done"}]))

    async def run() -> None:
        opening = asyncio.create_task(_open(core, tmp_path))
        await asyncio.wait_for(required_entered.wait(), timeout=0.2)
        assert not opening.done()
        assert await core.list_sessions() == []
        required_release.set()
        await asyncio.wait_for(opening, timeout=0.2)
        await core.shutdown()

    asyncio.run(run())


def test_required_mcp_failure_rolls_back_host_open_and_background_worker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=True))
    worker_drained = asyncio.Event()

    async def idle_worker(self, runtime):
        try:
            await asyncio.Event().wait()
        finally:
            worker_drained.set()

    async def fail_required(self, ticket):
        await asyncio.sleep(0)
        raise McpRequiredStartupError(
            server_ids=ticket.required_server_ids,
            reason_code="mcp_required_generation_unavailable",
        )

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", idle_worker)
    monkeypatch.setattr(McpServerSupervisor, "await_required", fail_required)
    core = _core(monkeypatch, ScriptedTransport([]))

    async def run() -> None:
        with pytest.raises(McpRequiredStartupError):
            await _open(core, tmp_path)
        await asyncio.wait_for(worker_drained.wait(), timeout=0.5)
        assert await core.list_sessions() == []
        assert await core.list_workspace_terminal_snapshots() == []
        await core.shutdown()

    asyncio.run(run())


def test_failed_open_retains_mcp_cleanup_owner_until_shutdown_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=True))
    worker_drained = asyncio.Event()

    async def idle_worker(self, runtime):
        try:
            await asyncio.Event().wait()
        finally:
            worker_drained.set()

    async def fail_required(self, ticket):
        await asyncio.sleep(0)
        raise McpRequiredStartupError(
            server_ids=ticket.required_server_ids,
            reason_code="mcp_required_generation_unavailable",
        )

    original_close = McpServerSupervisor.aclose
    close_calls = 0

    async def fail_close_once(self, *, timeout_seconds=5.0):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("synthetic failed-open MCP close")
        await original_close(self, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", idle_worker)
    monkeypatch.setattr(McpServerSupervisor, "await_required", fail_required)
    monkeypatch.setattr(McpServerSupervisor, "aclose", fail_close_once)
    core = _core(monkeypatch, ScriptedTransport([]))

    async def run() -> None:
        with pytest.raises(McpRequiredStartupError):
            await _open(core, tmp_path)
        assert len(core._failed_open_mcp_supervisors) == 1
        assert await core.list_sessions() == []

        await core.shutdown()
        await asyncio.wait_for(worker_drained.wait(), timeout=0.5)
        assert close_calls == 2
        assert core._failed_open_mcp_supervisors == {}

    asyncio.run(run())


def test_open_cancellation_drains_required_mcp_worker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=True))
    required_entered = asyncio.Event()
    worker_drained = asyncio.Event()

    async def idle_worker(self, runtime):
        try:
            await asyncio.Event().wait()
        finally:
            worker_drained.set()

    async def wait_required(self, ticket):
        required_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", idle_worker)
    monkeypatch.setattr(McpServerSupervisor, "await_required", wait_required)
    core = _core(monkeypatch, ScriptedTransport([]))

    async def run() -> None:
        opening = asyncio.create_task(_open(core, tmp_path))
        await asyncio.wait_for(required_entered.wait(), timeout=0.2)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.wait_for(worker_drained.wait(), timeout=0.5)
        assert await core.list_sessions() == []
        await core.shutdown()

    asyncio.run(run())


def test_faulted_mcp_installation_is_close_only_until_reopen(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _core(monkeypatch, ScriptedTransport([{"text": "must not run"}]))

    async def run() -> None:
        session = await _open(core, tmp_path)
        session._mcp_installation_faulted = True
        summary = session.summary()
        assert summary["mcp"]["faulted"] is True
        with pytest.raises(RuntimeError, match="only inspect/status/close"):
            await session.run_turn("blocked after architecture fault")
        assert not hasattr(session, "reconcile_mcp_installation")
        await core.close_session(session.host_session_id)
        assert await core.list_sessions() == []

        reopened = await _open(
            core,
            tmp_path,
            host_session_id="host:reopened",
        )
        assert reopened.summary()["mcp"]["faulted"] is False
        await core.shutdown()

    asyncio.run(run())


def test_post_linearization_installation_fault_latches_and_close_drains_slots(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    core = _core(monkeypatch, ScriptedTransport([{"text": "first"}]))

    async def run() -> None:
        session = await _open(core, tmp_path)
        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        await session.run_turn("install first generation")

        supervisor = session.mcp_supervisor
        old_slot = supervisor.slots()[0]
        current_attempt = supervisor._current_attempts["slow-docs"]
        await _queue_ready_candidate(supervisor, current_attempt)
        replacement_slot = supervisor._candidates[-1].manager_slot
        assert replacement_slot is not None

        runtime_session = session.wiring.runtime_wiring.runtime_session
        runtime_session_type = type(runtime_session)
        original_set_contract = runtime_session_type.set_mcp_installation_contract

        def fail_after_surface_swap(self, **kwargs):
            if self is runtime_session:
                raise RuntimeError("synthetic post-linearization fault")
            return original_set_contract(self, **kwargs)

        monkeypatch.setattr(
            runtime_session_type,
            "set_mcp_installation_contract",
            fail_after_surface_swap,
        )
        with pytest.raises(RuntimeError, match="post-linearization"):
            await session.run_turn("trigger fault")

        assert session.summary()["mcp"]["faulted"] is True
        assert old_slot.lifecycle == "retiring"
        assert replacement_slot.lifecycle == "installed"
        with pytest.raises(RuntimeError, match="only inspect/status/close"):
            await session.run_turn("must remain blocked")
        assert not hasattr(session, "reconcile_mcp_installation")

        await core.close_session(session.host_session_id)
        assert old_slot.manager.closed
        assert replacement_slot.manager.closed
        assert await core.list_sessions() == []

    asyncio.run(run())


def test_mcp_close_failure_preserves_host_session_and_retries_same_manager(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    core = _core(monkeypatch, ScriptedTransport([{"text": "done"}]))
    original_close = MockMcpClientManager.aclose
    close_calls = 0

    async def fail_once(self, *, timeout_seconds=5.0):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("synthetic SDK close failure")
        await original_close(self, timeout_seconds=timeout_seconds)

    async def run() -> None:
        session = await _open(core, tmp_path)
        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        await session.run_turn("install ready MCP")
        installed_manager = session.mcp_supervisor.slots()[0].manager
        monkeypatch.setattr(MockMcpClientManager, "aclose", fail_once)

        with pytest.raises(RuntimeError, match="synthetic SDK close failure"):
            await core.close_session(session.host_session_id)
        assert await core.get_session(session.host_session_id) is session
        assert core._session_leases[session.host_session_id] is session.terminal_lease
        assert session.mcp_supervisor.slots()[0].manager is installed_manager

        await core.close_session(session.host_session_id)
        assert close_calls == 2
        assert await core.list_sessions() == []

    asyncio.run(run())


def test_optional_installation_validation_failure_restores_old_slot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    core = _core(
        monkeypatch,
        ScriptedTransport([{"text": "first"}, {"text": "second"}]),
    )

    async def run() -> None:
        import pulsara_agent.host.session as host_session

        session = await _open(core, tmp_path)
        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        await session.run_turn("install first")
        supervisor = session.mcp_supervisor
        old_installation = session.wiring.runtime_wiring.mcp_installation
        old_slot = supervisor.slots()[0]
        current_attempt = supervisor._current_attempts["slow-docs"]
        await _queue_ready_candidate(supervisor, current_attempt)
        candidate_manager = supervisor._candidates[-1].manager_slot.manager

        def reject_installation(**_kwargs):
            raise ValueError("synthetic descriptor collision")

        monkeypatch.setattr(host_session, "build_mcp_installation", reject_installation)
        await session.run_turn("keep old optional installation")

        assert (
            session.wiring.runtime_wiring.mcp_installation.installation_id
            == old_installation.installation_id
        )
        assert old_slot.lifecycle == "installed"
        assert candidate_manager.closed
        assert session.summary()["mcp"]["diagnostics"][-1]["code"] == (
            "mcp_optional_installation_rejected"
        )
        await core.shutdown()

    asyncio.run(run())


def test_reconfigured_pending_binding_terminalizes_and_releases_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = [_config(required=False)]
    import pulsara_agent.host.core as host_core
    import pulsara_agent.host.session as host_session

    monkeypatch.setattr(
        host_core,
        "load_mcp_server_configs",
        lambda **_kwargs: tuple(configs),
    )
    monkeypatch.setattr(
        host_session,
        "load_mcp_server_configs",
        lambda **_kwargs: tuple(configs),
    )
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    def input_required(arguments):
        return McpInputRequired(
            interaction_id="mcp_input_required:reconfigure",
            server_id="slow-docs",
            protocol_version="2026-07-28",
            request_state="state:1",
            input_requests=(
                McpInputRequestDTO(
                    key="choice",
                    method="elicitation/create",
                    params={"message": "choose"},
                ),
            ),
            original_request=McpOriginalRequest(
                source_method=McpRequestSourceMethod.TOOL_CALL,
                tool_name="lookup",
                arguments=arguments,
            ),
        )

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime, handler=input_required)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    core = _core(
        monkeypatch,
        ScriptedTransport(
            [
                {
                    "tool_calls": [
                        {
                            "id": "call:mcp",
                            "name": "mcp__slow-docs__lookup",
                            "arguments": "{}",
                        }
                    ]
                },
                {"text": "binding was revoked"},
            ]
        ),
    )

    async def run() -> None:
        session = await _open(core, tmp_path)
        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        suspended = await session.run_turn("request MCP input")
        assert suspended.status.value == "waiting_user"
        pending = session.get_pending_interaction()
        assert pending is not None
        assert session.mcp_supervisor.pending_completion_count == 1
        old_slot = session.mcp_supervisor.slots()[0]

        configs[0] = replace(
            configs[0],
            required=True,
            tool_timeout_ms=2_000,
        )
        event_log = session.wiring.runtime_wiring.runtime_session.event_log
        event_log_type = type(event_log)
        original_extend = event_log_type.extend_with_materialization_state
        failed_once = False

        def fail_resume_audit_once(self, events, **kwargs):
            nonlocal failed_once
            event_batch = tuple(events)
            if (
                self is event_log
                and not failed_once
                and any(
                    isinstance(event, McpCapabilitySnapshotInstalledEvent)
                    for event in event_batch
                )
            ):
                failed_once = True
                raise RuntimeError("synthetic resume audit commit failure")
            return original_extend(self, event_batch, **kwargs)

        monkeypatch.setattr(
            event_log_type,
            "extend_with_materialization_state",
            fail_resume_audit_once,
        )
        with pytest.raises(Exception):
            await session.resolve_mcp_input_required(
                McpInputRequiredInteractionResolution(
                    interaction_id=pending.interaction_id,
                    responses={"choice": {"value": "yes"}},
                )
            )
        assert session.get_pending_interaction() is pending
        assert session.mcp_supervisor.pending_completion_count == 1
        assert old_slot.lifecycle == "retiring"

        monkeypatch.setattr(
            event_log_type,
            "extend_with_materialization_state",
            original_extend,
        )
        result = await session.resolve_mcp_input_required(
            McpInputRequiredInteractionResolution(
                interaction_id=pending.interaction_id,
                responses={"choice": {"value": "yes"}},
            )
        )

        assert result.status.value == "finished"
        assert session.get_pending_interaction() is None
        assert session.mcp_supervisor.pending_completion_count == 0
        assert old_slot.lifecycle == "closed"
        assert session.wiring.runtime_wiring.mcp_installation.tools
        audits = [
            event
            for event in event_log.iter()
            if isinstance(event, McpCapabilitySnapshotInstalledEvent)
        ]
        assert len(audits) == 2
        assert audits[-1].server_snapshots[0].status == "ready"
        binding_change_events = [
            event
            for event in event_log.iter()
            if getattr(event, "name", None) == "mcp_input_required_binding_changed"
        ]
        assert len(binding_change_events) == 1
        await core.shutdown()

    asyncio.run(run())


def test_host_close_terminalizes_pending_mcp_and_drains_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_config(monkeypatch, _config(required=False))
    worker_release = asyncio.Event()
    candidate_ready = asyncio.Event()

    def input_required(arguments):
        return McpInputRequired(
            interaction_id="mcp_input_required:close",
            server_id="slow-docs",
            protocol_version="2026-07-28",
            request_state="state:close",
            input_requests=(
                McpInputRequestDTO(
                    key="choice",
                    method="elicitation/create",
                    params={"message": "choose"},
                ),
            ),
            original_request=McpOriginalRequest(
                source_method=McpRequestSourceMethod.TOOL_CALL,
                tool_name="lookup",
                arguments=arguments,
            ),
        )

    async def controlled_worker(self, runtime):
        await worker_release.wait()
        await _queue_ready_candidate(self, runtime, handler=input_required)
        candidate_ready.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_worker)
    core = _core(
        monkeypatch,
        ScriptedTransport(
            [
                {
                    "tool_calls": [
                        {
                            "id": "call:mcp-close",
                            "name": "mcp__slow-docs__lookup",
                            "arguments": "{}",
                        }
                    ]
                }
            ]
        ),
    )

    async def run() -> None:
        session = await _open(core, tmp_path)
        worker_release.set()
        await asyncio.wait_for(candidate_ready.wait(), timeout=0.2)
        result = await session.run_turn("suspend on MCP input")
        assert result.status.value == "waiting_user"
        supervisor = session.mcp_supervisor
        slot = supervisor.slots()[0]
        assert supervisor.pending_completion_count == 1
        assert slot.borrower_count == 1

        await core.close_session(session.host_session_id)

        assert supervisor.pending_completion_count == 0
        assert slot.borrower_count == 0
        assert slot.manager.closed
        assert supervisor.lifecycle == "closed"
        assert await core.list_sessions() == []

    asyncio.run(run())
