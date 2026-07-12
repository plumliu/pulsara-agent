from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from pulsara_agent.capability.providers.mcp import (
    McpCapabilityProvider,
    build_mcp_installation,
    empty_mcp_installation,
)
from pulsara_agent.capability.types import (
    CapabilityExecutionSurfaceSnapshotContext,
    CapabilityProjectionResolveContext,
)
from pulsara_agent.primitives.capability import (
    build_capability_execution_surface_identity,
)
from pulsara_agent.primitives.mcp import (
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpServerLifecycleTimingFact,
    McpServerSnapshotFact,
)
from pulsara_agent.runtime.mcp.manager import MockMcpClientManager
from pulsara_agent.runtime.mcp.store import McpConfigStore
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpDrainError,
    McpInputRequestDTO,
    McpInputRequired,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpServerCandidate,
    McpServerConfig,
    McpServerRuntimeSpec,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpStreamableHttpConfig,
    McpToolAnnotations,
    McpDiscoveredTool,
    event_safe_mcp_config_fingerprint,
    mcp_config_set_fingerprint,
    new_mcp_slot,
    runtime_mcp_config_fingerprint,
    snapshot_semantic_fingerprint,
)
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool
from pulsara_agent.tools.base import ToolCall, ToolExecutionSuspended, ToolRuntimeContext
from pulsara_agent.event import EventContext


def _config(
    server_id: str = "docs",
    *,
    required: bool = False,
    enabled: bool = True,
) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        transport=McpStdioConfig(command="fake-mcp"),
        required=required,
        enabled=enabled,
        connect_timeout_ms=100,
        discovery_timeout_ms=100,
        startup_deadline_ms=250,
        refresh_ttl_ms=60_000,
        tool_timeout_ms=1_000,
    )


def _tool(name: str = "lookup") -> McpDiscoveredTool:
    return McpDiscoveredTool(
        server_id="docs",
        name=name,
        description="Lookup docs",
        input_schema={"type": "object", "properties": {}},
        annotations=McpToolAnnotations(read_only_hint=True),
    )


def _timing() -> McpServerLifecycleTimingFact:
    return McpServerLifecycleTimingFact(
        queued_at_utc="2026-01-01T00:00:00Z",
        connect_started_at_utc="2026-01-01T00:00:00Z",
        connect_ended_at_utc="2026-01-01T00:00:00.010000Z",
        discovery_started_at_utc="2026-01-01T00:00:00.010000Z",
        discovery_ended_at_utc="2026-01-01T00:00:00.020000Z",
        completed_at_utc="2026-01-01T00:00:00.020000Z",
        connect_duration_seconds=0.01,
        discovery_duration_seconds=0.01,
        total_duration_seconds=0.02,
    )


def _snapshot(
    config: McpServerConfig | None = None,
    *,
    status: McpServerStatus = McpServerStatus.READY,
    attempt_id: str = "mcp_attempt:test",
    generation: int = 1,
) -> McpServerSnapshot:
    config = config or _config()
    tools = (_tool(),) if status is McpServerStatus.READY else ()
    return McpServerSnapshot(
        snapshot_id=f"mcp_snapshot:{attempt_id}",
        server_id=config.server_id,
        config_epoch=1,
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
            server_id=config.server_id,
            status=status,
            tools=tools,
        ),
        reconcile_attempt_id=attempt_id,
        discovery_generation=generation,
        status=status,
        required=config.required,
        tools=tools,
        timing=_timing(),
    )


def _installed_surface(
    *,
    handler=lambda arguments: f"ok:{arguments.get('query', '')}",
):
    config = _config()
    snapshot = _snapshot(config)
    manager = MockMcpClientManager(
        _snapshots=(snapshot,),
        handlers={("docs", "lookup"): handler},
    )
    spec = McpServerRuntimeSpec(
        config=config,
        runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
    )
    slot = new_mcp_slot(spec=spec, snapshot=snapshot, manager=manager)
    candidate = McpServerCandidate(
        ticket_id="mcp_ticket:test",
        config_epoch=1,
        reconcile_attempt_id=snapshot.reconcile_attempt_id,
        reserved_discovery_generation=snapshot.discovery_generation,
        server_snapshot=snapshot,
        runtime_spec=spec,
        manager_slot=slot,
        trigger="initial",
    )
    supervisor = McpServerSupervisor()
    supervisor._desired_specs[config.server_id] = spec
    supervisor.commit_slot_transition(candidates=(candidate,), retiring_slot_ids=())
    installation = build_mcp_installation(
        supervisor=supervisor,
        config_epoch=1,
        event_safe_config_set_fingerprint=mcp_config_set_fingerprint((config,), event_safe=True),
        snapshots=(snapshot,),
        configs_by_server={"docs": config},
        slots_by_server={"docs": slot},
    )
    return supervisor, installation, manager, slot


def _runtime_context() -> ToolRuntimeContext:
    return ToolRuntimeContext(
        runtime_session_id="runtime:test",
        event_context=EventContext(
            run_id="run:test",
            turn_id="turn:test",
            reply_id="reply:test",
        ),
    )


def test_mcp_config_hard_cut_defaults_and_invariants() -> None:
    config = McpServerConfig(
        server_id="docs",
        transport=McpStdioConfig(command="docs"),
    )
    assert config.connect_timeout_ms == 10_000
    assert config.discovery_timeout_ms == 15_000
    assert config.startup_deadline_ms == 30_000
    assert config.refresh_ttl_ms == 300_000
    with pytest.raises(ValueError):
        replace(config, startup_deadline_ms=5_000)


def test_mcp_config_store_rejects_removed_startup_timeout(tmp_path: Path) -> None:
    path = tmp_path / "mcp.yaml"
    path.write_text(
        "servers:\n  docs:\n    command: docs\n    startup_timeout_ms: 1000\n"
    )
    with pytest.raises(ValueError, match="removed field"):
        McpConfigStore(path).load()


def test_runtime_fingerprint_detects_secret_rotation_but_event_safe_does_not() -> None:
    left = McpServerConfig(
        server_id="docs",
        transport=McpStreamableHttpConfig(
            url="https://example.test/mcp?token=one",
            headers={"Authorization": "Bearer one"},
        ),
    )
    right = replace(
        left,
        transport=McpStreamableHttpConfig(
            url="https://example.test/mcp?token=two",
            headers={"Authorization": "Bearer two"},
        ),
    )
    assert runtime_mcp_config_fingerprint(left) != runtime_mcp_config_fingerprint(right)
    assert event_safe_mcp_config_fingerprint(left) == event_safe_mcp_config_fingerprint(right)


def test_runtime_fingerprint_detects_environment_secret_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = McpServerConfig(
        server_id="docs",
        transport=McpStreamableHttpConfig(
            url="https://example.test/mcp",
            bearer_token_env_var="PULSARA_TEST_MCP_TOKEN",
            env_headers={"X-Api-Key": "PULSARA_TEST_MCP_API_KEY"},
        ),
    )
    monkeypatch.setenv("PULSARA_TEST_MCP_TOKEN", "one")
    monkeypatch.setenv("PULSARA_TEST_MCP_API_KEY", "alpha")
    first_runtime = runtime_mcp_config_fingerprint(config)
    event_safe = event_safe_mcp_config_fingerprint(config)

    monkeypatch.setenv("PULSARA_TEST_MCP_TOKEN", "two")
    monkeypatch.setenv("PULSARA_TEST_MCP_API_KEY", "beta")
    assert runtime_mcp_config_fingerprint(config) != first_runtime
    assert event_safe_mcp_config_fingerprint(config) == event_safe


def test_snapshot_semantic_fingerprint_ignores_random_identity_and_timing() -> None:
    snapshot = _snapshot()
    changed = replace(
        snapshot,
        snapshot_id="mcp_snapshot:other",
        reconcile_attempt_id="mcp_attempt:other",
        timing=_timing().model_copy(update={"total_duration_seconds": 9.0}),
    )
    assert snapshot.snapshot_semantic_fingerprint == changed.snapshot_semantic_fingerprint


def test_snapshot_semantic_fingerprint_changes_with_catalog_semantics() -> None:
    original = _snapshot()
    changed_tool = replace(_tool(), description="A materially different capability")
    changed = snapshot_semantic_fingerprint(
        server_id=original.server_id,
        status=original.status,
        tools=(changed_tool,),
    )
    assert changed != original.snapshot_semantic_fingerprint


def test_mcp_snapshot_status_timing_invariants() -> None:
    with pytest.raises(ValueError, match="completed connect/discovery timing"):
        replace(
            _snapshot(),
            timing=McpServerLifecycleTimingFact(
                queued_at_utc="2026-01-01T00:00:00Z",
                completed_at_utc="2026-01-01T00:00:01Z",
                total_duration_seconds=1,
            ),
        )

    with pytest.raises(ValueError, match="completed timing"):
        McpServerSnapshotFact(
            snapshot_id="mcp_snapshot:failed",
            server_id="docs",
            config_epoch=1,
            event_safe_config_fingerprint="sha256:config",
            snapshot_semantic_fingerprint="sha256:snapshot",
            reconcile_attempt_id="mcp_attempt:failed",
            discovery_generation=1,
            status="failed",
            required=False,
            timing=McpServerLifecycleTimingFact(
                queued_at_utc="2026-01-01T00:00:00Z"
            ),
        )


def test_mcp_snapshot_fact_rejects_non_null_catalog_artifact_id_in_v1() -> None:
    attempt = McpReconcileAttemptSummaryFact(
        server_id="docs",
        reconcile_attempt_id="mcp_attempt:test",
        reconcile_trigger="initial",
        attempt_status="ready",
        request_count=1,
        page_count=1,
        cache_outcome="miss",
    )
    with pytest.raises(ValueError):
        McpInstalledServerSnapshotFact(
            server_id="docs",
            status="ready",
            required=False,
            changed_in_this_installation=True,
            attempt=attempt,
            snapshot_id="mcp_snapshot:test",
            discovery_generation=1,
            event_safe_config_fingerprint="sha256:config",
            snapshot_semantic_fingerprint="sha256:snapshot",
            lifecycle_timing=_timing(),
            catalog_artifact_id="artifact:catalog",  # type: ignore[arg-type]
        )


def test_mcp_nested_json_is_recursively_immutable_and_serializable() -> None:
    source_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    tool = McpDiscoveredTool(
        server_id="docs",
        name="lookup",
        description="lookup",
        input_schema=source_schema,
    )
    source_schema["properties"]["query"]["type"] = "integer"
    assert tool.input_schema["properties"]["query"]["type"] == "string"
    with pytest.raises(TypeError, match="immutable"):
        tool.input_schema["properties"]["query"]["type"] = "number"

    fact = McpServerSnapshotFact(
        snapshot_id="mcp_snapshot:test",
        server_id="docs",
        config_epoch=1,
        event_safe_config_fingerprint="sha256:config",
        snapshot_semantic_fingerprint="sha256:snapshot",
        reconcile_attempt_id="mcp_attempt:test",
        discovery_generation=1,
        status="ready",
        required=False,
        tools=(
            {
                "server_id": "docs",
                "name": "lookup",
                "description": "lookup",
                "input_schema": source_schema,
            },
        ),
        timing=_timing(),
    )
    with pytest.raises(TypeError, match="immutable"):
        fact.tools[0].input_schema["properties"] = {}
    assert fact.model_dump(mode="json")["tools"][0]["input_schema"]["type"] == "object"


def test_empty_installation_is_canonical() -> None:
    installation = empty_mcp_installation()
    assert installation.installation_id == "mcp_installation:empty"
    assert not installation.tools
    assert not installation.binding_identities


def test_installation_builds_descriptor_and_exact_binding() -> None:
    supervisor, installation, _manager, slot = _installed_surface()
    assert [descriptor.name for descriptor in installation.descriptors] == [
        "mcp__docs__lookup"
    ]
    tool = installation.tools[0]
    assert isinstance(tool, McpCapabilityTool)
    assert tool.binding_identity == slot.binding_identity
    assert not hasattr(tool, "installation_id")
    provider = McpCapabilityProvider(installation)
    descriptors = provider.snapshot_descriptors(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=Path.cwd(),
            workspace_kind="project",
            available_tool_names=frozenset({tool.name}),
            mcp_installation_id=installation.installation_id,
        )
    )
    projection = provider.resolve_projection(
        CapabilityProjectionResolveContext(
            workspace_root=Path.cwd(),
            workspace_kind="project",
            memory_domain=None,
            user_input="lookup",
        ),
        execution_surface=build_capability_execution_surface_identity(
            surface_contract_version="test:v1",
            entries=(),
            mcp_installation_id=installation.installation_id,
        ),
    )
    assert descriptors.descriptors == installation.descriptors
    assert projection.catalog_prompt is not None
    assert (
        "server=docs; status=ready; installed_tool_count=1"
        in projection.catalog_prompt
    )
    assert "actual tool schema remains the sole authority" in projection.catalog_prompt
    asyncio.run(supervisor.aclose(timeout_seconds=1))


def test_starting_mcp_prompt_strongly_freezes_current_run_availability() -> None:
    config = _config()
    starting = McpServerSnapshot(
        snapshot_id="mcp_snapshot:starting",
        server_id=config.server_id,
        config_epoch=1,
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
            server_id=config.server_id,
            status=McpServerStatus.STARTING,
        ),
        reconcile_attempt_id="mcp_attempt:starting",
        discovery_generation=1,
        status=McpServerStatus.STARTING,
        required=False,
        timing=McpServerLifecycleTimingFact(
            queued_at_utc="2026-01-01T00:00:00Z"
        ),
    )
    supervisor = McpServerSupervisor()
    installation = build_mcp_installation(
        supervisor=supervisor,
        config_epoch=1,
        event_safe_config_set_fingerprint="sha256:starting",
        snapshots=(starting,),
        configs_by_server={"docs": config},
        slots_by_server={},
        installation_id="mcp_installation:starting",
    )
    provider = McpCapabilityProvider(installation)
    descriptors = provider.snapshot_descriptors(
        CapabilityExecutionSurfaceSnapshotContext(
            workspace_root=Path.cwd(),
            workspace_kind="project",
            available_tool_names=frozenset(),
            mcp_installation_id=installation.installation_id,
        )
    )
    output = provider.resolve_projection(
        CapabilityProjectionResolveContext(
            workspace_root=Path.cwd(),
            workspace_kind="project",
            memory_domain=None,
            user_input="Can you see MCP?",
        ),
        execution_surface=build_capability_execution_surface_identity(
            surface_contract_version="test:v1",
            entries=(),
            mcp_installation_id=installation.installation_id,
        ),
    )

    assert descriptors.descriptors == ()
    prompt = output.catalog_prompt
    assert prompt is not None
    assert "server=docs; status=starting; installed_tool_count=0" in prompt
    assert "tools are NOT available in this run" in prompt
    assert "Do not infer current MCP availability from prior messages" in prompt
    assert "Do not describe status=starting as a configuration failure" in prompt
    assert "may become available in a later run after a HostSession safe point" in prompt
    assert "do not promise that the next run will succeed" in prompt
    asyncio.run(supervisor.aclose(timeout_seconds=1))


def test_unchanged_server_reuses_exact_descriptor_and_binding_objects() -> None:
    supervisor, installation, manager, slot = _installed_surface()
    config = _config()
    snapshot = installation.snapshots[0]
    rebuilt = build_mcp_installation(
        supervisor=supervisor,
        config_epoch=installation.config_epoch + 1,
        event_safe_config_set_fingerprint=installation.event_safe_config_set_fingerprint,
        snapshots=(snapshot,),
        configs_by_server={"docs": config},
        slots_by_server={"docs": slot},
        installation_id="mcp_installation:unrelated-change",
        previous_installation=installation,
    )
    assert rebuilt.tools[0] is installation.tools[0]
    assert rebuilt.descriptors[0] is installation.descriptors[0]

    replacement_manager = MockMcpClientManager(_snapshots=(snapshot,))
    spec = McpServerRuntimeSpec(
        config=config,
        runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
    )
    replacement_slot = new_mcp_slot(
        spec=spec,
        snapshot=snapshot,
        manager=replacement_manager,
    )
    changed = build_mcp_installation(
        supervisor=supervisor,
        config_epoch=installation.config_epoch + 1,
        event_safe_config_set_fingerprint=installation.event_safe_config_set_fingerprint,
        snapshots=(snapshot,),
        configs_by_server={"docs": config},
        slots_by_server={"docs": replacement_slot},
        installation_id="mcp_installation:slot-change",
        previous_installation=installation,
    )
    assert changed.tools[0] is not installation.tools[0]
    assert changed.tools[0].binding_identity == replacement_slot.binding_identity
    assert not hasattr(changed.tools[0], "installation_id")

    asyncio.run(supervisor.aclose(timeout_seconds=1))
    asyncio.run(replacement_manager.aclose(timeout_seconds=1))
    assert manager.close_count == 1


def test_tool_call_uses_and_releases_slot_lease() -> None:
    supervisor, installation, manager, slot = _installed_surface()
    tool = installation.tools[0]

    async def run() -> None:
        result = await tool.execute_async(
            ToolCall(id="call:1", name=tool.name, arguments={"query": "x"}),
            runtime_context=_runtime_context(),
        )
        assert result.output == "ok:x"
        assert slot.borrower_count == 0
        assert manager.calls == [("docs", "lookup", {"query": "x"})]
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_suspended_tool_promotes_lease_and_resume_borrows_same_slot() -> None:
    def handler(arguments):
        return McpInputRequired(
            interaction_id="mcp_input_required:1",
            server_id="docs",
            protocol_version="2026-07-28",
            request_state=None,
            input_requests=(
                McpInputRequestDTO(
                    key="token",
                    method="elicitation/create",
                    params={"message": "token"},
                ),
            ),
            original_request=McpOriginalRequest(
                source_method=McpRequestSourceMethod.TOOL_CALL,
                tool_name="lookup",
                arguments=arguments,
            ),
        )

    supervisor, installation, _manager, slot = _installed_surface(handler=handler)
    tool = installation.tools[0]

    async def run() -> None:
        suspended = await tool.execute_async(
            ToolCall(id="call:1", name=tool.name, arguments={"query": "x"}),
            runtime_context=_runtime_context(),
        )
        assert isinstance(suspended, ToolExecutionSuspended)
        reservation_id = suspended.payload["mcp_pending_lease_reservation_id"]
        supervisor.confirm_pending_lease("mcp_input_required:1", reservation_id)
        borrowed = supervisor.borrow_pending_lease(
            "mcp_input_required:1", tool.binding_identity
        )
        assert borrowed.slot_id == slot.slot_id
        supervisor.return_pending_borrow("mcp_input_required:1")
        supervisor.complete_pending_lease("mcp_input_required:1")
        assert slot.borrower_count == 0
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_pending_creation_failure_releases_newly_acquired_lease() -> None:
    def handler(arguments):
        return McpInputRequired(
            interaction_id="mcp_input_required:duplicate",
            server_id="docs",
            protocol_version="2026-07-28",
            request_state=None,
            input_requests=(
                McpInputRequestDTO(
                    key="token",
                    method="elicitation/create",
                    params={"message": "token"},
                ),
            ),
            original_request=McpOriginalRequest(
                source_method=McpRequestSourceMethod.TOOL_CALL,
                tool_name="lookup",
                arguments=arguments,
            ),
        )

    supervisor, installation, _manager, slot = _installed_surface(handler=handler)
    tool = installation.tools[0]

    async def run() -> None:
        first = await tool.execute_async(
            ToolCall(id="call:1", name=tool.name, arguments={}),
            runtime_context=_runtime_context(),
        )
        assert isinstance(first, ToolExecutionSuspended)
        assert slot.borrower_count == 1

        second = await tool.execute_async(
            ToolCall(id="call:2", name=tool.name, arguments={}),
            runtime_context=_runtime_context(),
        )
        assert not isinstance(second, ToolExecutionSuspended)
        assert second.status.value == "error"
        assert "pending input ownership failed" in second.output
        assert slot.borrower_count == 1

        reservation_id = first.payload["mcp_pending_lease_reservation_id"]
        supervisor.abort_pending_lease(
            "mcp_input_required:duplicate",
            reservation_id,
        )
        assert slot.borrower_count == 0
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_retiring_slot_rejects_new_acquisition_until_borrower_releases() -> None:
    supervisor, installation, _manager, slot = _installed_surface()
    identity = installation.tools[0].binding_identity
    lease = supervisor.acquire_binding_lease(identity)
    supervisor.commit_slot_transition(candidates=(), retiring_slot_ids=(slot.slot_id,))
    with pytest.raises(RuntimeError, match="generation_unavailable"):
        supervisor.acquire_binding_lease(identity)
    supervisor.release_lease(lease)
    asyncio.run(supervisor.close_retiring_slots(timeout_seconds=1))
    assert slot.lifecycle == "closed"
    asyncio.run(supervisor.aclose(timeout_seconds=1))


def test_retiring_slot_closes_when_last_async_borrower_releases() -> None:
    supervisor, installation, manager, slot = _installed_surface()
    identity = installation.tools[0].binding_identity

    async def run() -> None:
        lease = supervisor.acquire_binding_lease(identity)
        supervisor.commit_slot_transition(
            candidates=(),
            retiring_slot_ids=(slot.slot_id,),
        )
        supervisor.release_lease(lease)
        cleanup = tuple(supervisor._retiring_slot_cleanup_tasks)
        assert cleanup
        await asyncio.gather(*cleanup)
        assert slot.lifecycle == "closed"
        assert manager.close_count == 1
        assert supervisor.slots() == ()
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_pending_lease_prevents_close_until_completed() -> None:
    supervisor, installation, _manager, _slot = _installed_surface()
    identity = installation.tools[0].binding_identity
    lease = supervisor.acquire_binding_lease(identity)
    reservation = supervisor.promote_lease_to_pending(lease, "interaction:1")
    supervisor.confirm_pending_lease("interaction:1", reservation.reservation_id)

    async def run() -> None:
        with pytest.raises(Exception):
            await supervisor.aclose(timeout_seconds=0.01)
        supervisor.complete_pending_lease("interaction:1")
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_prepare_optional_returns_before_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(self, runtime):
        entered.set()
        await release.wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", fake_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        ticket = supervisor.prepare((_config(),), trigger="initial")
        assert ticket.optional_server_ids == ("docs",)
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert supervisor.current_starting_snapshots()[0].status is McpServerStatus.STARTING
        release.set()
        await supervisor.await_ticket_snapshots(ticket)
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_server_workers_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = {server_id: asyncio.Event() for server_id in ("one", "two")}
    release = asyncio.Event()

    async def controlled_run(self, runtime):
        entered[runtime.spec.config.server_id].set()
        await release.wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        supervisor.prepare((_config("one"), _config("two")), trigger="initial")
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())),
            timeout=0.2,
        )
        release.set()
        await asyncio.gather(*supervisor._workers.values())
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_worker_exception_returns_background_task_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_run(self, runtime):
        raise RuntimeError("synthetic worker architecture fault")

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", broken_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        supervisor.prepare((_config(),), trigger="initial")
        worker = supervisor._workers["docs"]
        with pytest.raises(RuntimeError, match="architecture fault"):
            await worker
        await asyncio.sleep(0)
        assert supervisor._owned_background_tasks == set()
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_session_close_cancels_and_drains_mcp_discovery_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def blocked_run(self, runtime):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", blocked_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        supervisor.prepare((_config(),), trigger="initial")
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        await supervisor.aclose(timeout_seconds=1)
        assert drained.is_set()
        assert supervisor.lifecycle == "closed"
        assert supervisor._owned_background_tasks == set()
        assert supervisor.slots() == ()

    asyncio.run(run())


def test_close_during_discovery_retries_connection_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.runtime.mcp.supervisor as supervisor_module

    discovery_entered = asyncio.Event()
    discovery_cancelled = asyncio.Event()

    class FailOnceConnection:
        def __init__(self):
            self.close_count = 0
            self.closed = False

        async def aclose(self, *, timeout_seconds=5.0):
            del timeout_seconds
            self.close_count += 1
            if self.close_count == 1:
                raise RuntimeError("synthetic connection close failure")
            self.closed = True

    connection = FailOnceConnection()

    async def fake_connect(cls, config, *, timeout_seconds):
        del cls, config, timeout_seconds
        return connection

    async def blocked_discovery(_connection, **_kwargs):
        discovery_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            discovery_cancelled.set()

    monkeypatch.setattr(
        supervisor_module.SdkMcpConnection,
        "connect",
        classmethod(fake_connect),
    )
    monkeypatch.setattr(
        supervisor_module,
        "discover_mcp_server",
        blocked_discovery,
    )

    async def run() -> None:
        supervisor = McpServerSupervisor()
        supervisor.prepare((_config(),), trigger="initial")
        await asyncio.wait_for(discovery_entered.wait(), timeout=0.2)
        await supervisor.aclose(timeout_seconds=1)
        assert discovery_cancelled.is_set()
        assert connection.closed
        assert connection.close_count == 2
        assert supervisor._orphan_connections == {}
        assert supervisor.lifecycle == "closed"

    asyncio.run(run())


def test_same_config_safe_point_does_not_restart_inflight_optional_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(self, runtime):
        entered.set()
        await release.wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", slow_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        first = supervisor.prepare((_config(),), trigger="initial")
        await asyncio.wait_for(entered.wait(), timeout=1)
        first_worker = supervisor._workers["docs"]
        second = supervisor.prepare((_config(),), trigger="config_change")
        assert second.config_epoch == first.config_epoch
        assert second.server_attempts == {}
        assert supervisor._workers["docs"] is first_worker
        assert not first_worker.cancelled()
        release.set()
        await first_worker
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_unchanged_installed_config_before_ttl_does_not_schedule_refresh() -> None:
    supervisor, _installation, _manager, _slot = _installed_surface()

    async def run() -> None:
        ticket = supervisor.prepare((_config(),), trigger="config_change")
        assert ticket.server_attempts == {}
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_reconfigure_does_not_retire_old_slot_before_install_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def slow_run(self, runtime):
        await release.wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", slow_run)
    supervisor, installation, _manager, old_slot = _installed_surface()
    old_identity = installation.tools[0].binding_identity

    async def run() -> None:
        changed = replace(_config(), tool_timeout_ms=2_000)
        ticket = supervisor.prepare((changed,), trigger="config_change")
        assert ticket.server_attempts
        assert old_slot.lifecycle == "installed"
        assert not supervisor.binding_matches_current_desired_runtime(old_identity)

        with pytest.raises(RuntimeError, match="generation_unavailable"):
            supervisor.acquire_binding_lease(old_identity)

        release.set()
        await supervisor.await_ticket_snapshots(ticket)
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_newer_same_epoch_attempt_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    releases: list[asyncio.Event] = []

    async def fake_run(self, runtime):
        release = asyncio.Event()
        releases.append(release)
        try:
            await release.wait()
        except asyncio.CancelledError:
            return

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", fake_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        first = supervisor.prepare((_config(),), trigger="initial")
        await asyncio.sleep(0)
        second = supervisor.prepare((_config(),), trigger="manual_refresh")
        assert first.config_epoch == second.config_epoch
        assert (
            first.server_attempts["docs"].reconcile_attempt_id
            != second.server_attempts["docs"].reconcile_attempt_id
        )
        releases[-1].set()
        await supervisor.await_ticket_snapshots(second)
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_newer_same_epoch_attempt_discards_already_queued_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_network(self, runtime):
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", no_network)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        config = _config()
        first = supervisor.prepare((config,), trigger="initial")
        await asyncio.sleep(0)
        first_attempt = first.server_attempts["docs"]
        snapshot = _snapshot(
            config,
            attempt_id=first_attempt.reconcile_attempt_id,
            generation=first_attempt.reserved_discovery_generation,
        )
        manager = MockMcpClientManager(_snapshots=(snapshot,))
        spec = McpServerRuntimeSpec(
            config=config,
            runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
            event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        )
        stale = McpServerCandidate(
            ticket_id=first.ticket_id,
            config_epoch=first.config_epoch,
            reconcile_attempt_id=first_attempt.reconcile_attempt_id,
            reserved_discovery_generation=first_attempt.reserved_discovery_generation,
            server_snapshot=snapshot,
            runtime_spec=spec,
            manager_slot=new_mcp_slot(spec=spec, snapshot=snapshot, manager=manager),
            trigger="initial",
        )
        supervisor._candidates.append(stale)

        second = supervisor.prepare((config,), trigger="manual_refresh")
        assert second.config_epoch == first.config_epoch
        await asyncio.sleep(0)
        assert manager.closed
        assert manager.close_count == 1
        assert supervisor.stale_discard_counts() == {"docs": 1}
        batch = supervisor.drain_installable_candidates(
            expected_epoch=second.config_epoch
        )
        assert stale not in batch.candidates
        supervisor.acknowledge_stale_discard_counts({"docs": 1})
        assert supervisor.stale_discard_counts() == {}
        await supervisor.aclose(timeout_seconds=1)
        assert manager.close_count == 1

    asyncio.run(run())


def test_required_wait_propagates_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def wait_forever(self, runtime):
        await release.wait()

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", wait_forever)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        ticket = supervisor.prepare((_config(required=True),), trigger="initial")
        waiter = asyncio.create_task(supervisor.await_required(ticket))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_mixed_server_ticket_waits_only_for_required_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    optional_entered = asyncio.Event()
    optional_drained = asyncio.Event()

    async def controlled_run(self, runtime):
        config = runtime.spec.config
        if not config.required:
            optional_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                optional_drained.set()
            return
        snapshot = McpServerSnapshot(
            snapshot_id=f"mcp_snapshot:{runtime.attempt.reconcile_attempt_id}",
            server_id=config.server_id,
            config_epoch=runtime.attempt.config_epoch,
            event_safe_config_fingerprint=runtime.spec.event_safe_config_fingerprint,
            snapshot_semantic_fingerprint=snapshot_semantic_fingerprint(
                server_id=config.server_id,
                status=McpServerStatus.READY,
            ),
            reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
            discovery_generation=runtime.attempt.reserved_discovery_generation,
            status=McpServerStatus.READY,
            required=True,
            timing=_timing(),
        )
        manager = MockMcpClientManager(_snapshots=(snapshot,))
        candidate = McpServerCandidate(
            ticket_id=runtime.ticket_id,
            config_epoch=runtime.attempt.config_epoch,
            reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
            reserved_discovery_generation=runtime.attempt.reserved_discovery_generation,
            server_snapshot=snapshot,
            runtime_spec=runtime.spec,
            manager_slot=new_mcp_slot(
                spec=runtime.spec,
                snapshot=snapshot,
                manager=manager,
            ),
            trigger=runtime.trigger,
        )
        with self._state_lock:
            self._candidates.append(candidate)

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        ticket = supervisor.prepare(
            (
                _config("required", required=True),
                _config("optional", required=False),
            ),
            trigger="initial",
        )
        await asyncio.wait_for(optional_entered.wait(), timeout=0.2)
        result = await asyncio.wait_for(
            supervisor.await_required(ticket),
            timeout=0.2,
        )
        assert result.ready_server_ids == ("required",)
        assert not supervisor._workers["optional"].done()
        await supervisor.aclose(timeout_seconds=1)
        assert optional_drained.is_set()

    asyncio.run(run())


def test_concurrent_close_waiters_share_failure_and_retry_same_supervisor() -> None:
    supervisor, installation, _manager, _slot = _installed_surface()
    identity = installation.tools[0].binding_identity
    lease = supervisor.acquire_binding_lease(identity)
    reservation = supervisor.promote_lease_to_pending(lease, "interaction:close")
    supervisor.confirm_pending_lease(
        "interaction:close",
        reservation.reservation_id,
    )

    async def run() -> None:
        outcomes = await asyncio.gather(
            *(supervisor.aclose(timeout_seconds=0.02) for _ in range(16)),
            return_exceptions=True,
        )
        assert all(isinstance(outcome, McpDrainError) for outcome in outcomes)
        assert {str(outcome) for outcome in outcomes} == {
            "timed out draining MCP leases"
        }
        assert supervisor.lifecycle == "open_with_close_pending"
        supervisor.complete_pending_lease("interaction:close")
        await supervisor.aclose(timeout_seconds=1)
        assert supervisor.lifecycle == "closed"

    asyncio.run(run())


def test_cancelled_close_waiter_does_not_cancel_shared_owner_attempt() -> None:
    supervisor, installation, _manager, _slot = _installed_surface()
    identity = installation.tools[0].binding_identity
    lease = supervisor.acquire_binding_lease(identity)
    reservation = supervisor.promote_lease_to_pending(lease, "interaction:cancel-waiter")
    supervisor.confirm_pending_lease(
        "interaction:cancel-waiter",
        reservation.reservation_id,
    )

    async def run() -> None:
        owner = asyncio.create_task(supervisor.aclose(timeout_seconds=1))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(supervisor.aclose(timeout_seconds=1))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        supervisor.complete_pending_lease("interaction:cancel-waiter")
        await owner
        assert supervisor.lifecycle == "closed"
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_blocked_stale_candidate_close_preserves_manager_for_retry() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingManager:
        def __init__(self, snapshot):
            self.snapshots = (snapshot,)
            self.close_count = 0
            self.closed = False

        async def aclose(self, *, timeout_seconds=5.0):
            del timeout_seconds
            self.close_count += 1
            entered.set()
            await release.wait()
            self.closed = True

    async def run() -> None:
        supervisor = McpServerSupervisor()
        config = _config()
        snapshot = _snapshot(config)
        spec = McpServerRuntimeSpec(
            config=config,
            runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
            event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        )
        manager = BlockingManager(snapshot)
        candidate = McpServerCandidate(
            ticket_id="mcp_ticket:stale",
            config_epoch=1,
            reconcile_attempt_id=snapshot.reconcile_attempt_id,
            reserved_discovery_generation=snapshot.discovery_generation,
            server_snapshot=snapshot,
            runtime_spec=spec,
            manager_slot=new_mcp_slot(
                spec=spec,
                snapshot=snapshot,
                manager=manager,
            ),
            trigger="initial",
        )
        supervisor._schedule_candidate_close(candidate)
        await asyncio.wait_for(entered.wait(), timeout=0.2)

        with pytest.raises(McpDrainError, match="background MCP manager cleanup"):
            await supervisor.aclose(timeout_seconds=0.01)
        assert supervisor.lifecycle == "open_with_close_pending"
        assert not manager.closed
        assert id(manager) in supervisor._candidate_cleanup_managers

        release.set()
        await asyncio.sleep(0)
        await supervisor.aclose(timeout_seconds=1)
        assert manager.closed
        assert manager.close_count == 1
        assert supervisor.lifecycle == "closed"

    asyncio.run(run())


def test_candidate_manager_close_failure_keeps_candidate_for_retry() -> None:
    class FailOnceManager:
        def __init__(self, snapshot):
            self.snapshots = (snapshot,)
            self.close_count = 0
            self.closed = False

        async def aclose(self, *, timeout_seconds=5.0):
            del timeout_seconds
            self.close_count += 1
            if self.close_count == 1:
                raise RuntimeError("synthetic candidate close failure")
            self.closed = True

    async def run() -> None:
        supervisor = McpServerSupervisor()
        config = _config()
        snapshot = _snapshot(config)
        spec = McpServerRuntimeSpec(
            config=config,
            runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
            event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        )
        manager = FailOnceManager(snapshot)
        candidate = McpServerCandidate(
            ticket_id="mcp_ticket:close-retry",
            config_epoch=1,
            reconcile_attempt_id=snapshot.reconcile_attempt_id,
            reserved_discovery_generation=snapshot.discovery_generation,
            server_snapshot=snapshot,
            runtime_spec=spec,
            manager_slot=new_mcp_slot(
                spec=spec,
                snapshot=snapshot,
                manager=manager,
            ),
            trigger="initial",
        )
        supervisor._candidates.append(candidate)

        with pytest.raises(RuntimeError, match="candidate close failure"):
            await supervisor.aclose(timeout_seconds=1)
        assert supervisor.lifecycle == "open_with_close_pending"
        assert supervisor._candidates == [candidate]

        await supervisor.aclose(timeout_seconds=1)
        assert manager.closed
        assert manager.close_count == 2
        assert supervisor._candidates == []
        assert supervisor.lifecycle == "closed"

    asyncio.run(run())


def test_unrelated_required_failure_preserves_pending_binding_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_without_candidate(self, runtime):
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", fail_without_candidate)
    supervisor, installation, _manager, slot = _installed_surface()
    identity = installation.tools[0].binding_identity
    lease = supervisor.acquire_binding_lease(identity)
    reservation = supervisor.promote_lease_to_pending(lease, "interaction:unrelated")
    supervisor.confirm_pending_lease(
        "interaction:unrelated",
        reservation.reservation_id,
    )

    async def run() -> None:
        ticket = supervisor.prepare(
            (
                _config(),
                _config("required-other", required=True),
            ),
            trigger="config_change",
        )
        with pytest.raises(Exception, match="required MCP"):
            await supervisor.await_required(ticket)
        borrowed = supervisor.borrow_pending_lease(
            "interaction:unrelated",
            identity,
        )
        assert borrowed.slot_id == slot.slot_id
        supervisor.return_pending_borrow("interaction:unrelated")
        supervisor.complete_pending_lease("interaction:unrelated")
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_same_config_refresh_failure_preserves_valid_pending_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_without_candidate(self, runtime):
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", fail_without_candidate)
    supervisor, installation, _manager, slot = _installed_surface()
    identity = installation.tools[0].binding_identity
    lease = supervisor.acquire_binding_lease(identity)
    reservation = supervisor.promote_lease_to_pending(lease, "interaction:refresh")
    supervisor.confirm_pending_lease("interaction:refresh", reservation.reservation_id)

    async def run() -> None:
        ticket = supervisor.prepare((_config(),), trigger="ttl_refresh")
        await supervisor.await_ticket_snapshots(ticket)
        assert slot.lifecycle == "installed"
        borrowed = supervisor.borrow_pending_lease("interaction:refresh", identity)
        assert borrowed.slot_id == slot.slot_id
        supervisor.return_pending_borrow("interaction:refresh")
        supervisor.complete_pending_lease("interaction:refresh")
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_required_ticket_fails_closed_without_ready_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(self, runtime):
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", fake_run)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        ticket = supervisor.prepare((_config(required=True),), trigger="initial")
        with pytest.raises(Exception, match="required MCP"):
            await supervisor.await_required(ticket)
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_required_ready_candidate_from_prior_ticket_installs_on_next_safe_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_network(self, runtime):
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", no_network)

    async def run() -> None:
        supervisor = McpServerSupervisor()
        config = _config(required=True)
        first = supervisor.prepare((config,), trigger="initial")
        await asyncio.sleep(0)
        attempt = first.server_attempts["docs"]
        snapshot = _snapshot(
            config,
            attempt_id=attempt.reconcile_attempt_id,
            generation=attempt.reserved_discovery_generation,
        )
        spec = supervisor._desired_specs["docs"]
        manager = MockMcpClientManager(_snapshots=(snapshot,))
        candidate = McpServerCandidate(
            ticket_id=first.ticket_id,
            config_epoch=first.config_epoch,
            reconcile_attempt_id=attempt.reconcile_attempt_id,
            reserved_discovery_generation=attempt.reserved_discovery_generation,
            server_snapshot=snapshot,
            runtime_spec=spec,
            manager_slot=new_mcp_slot(
                spec=spec,
                snapshot=snapshot,
                manager=manager,
            ),
            trigger="initial",
        )
        supervisor._candidates.append(candidate)

        second = supervisor.prepare((config,), trigger="config_change")
        assert second.server_attempts == {}
        ready = await supervisor.await_required(second)
        assert ready.ready_server_ids == ("docs",)
        batch = supervisor.drain_installable_candidates(
            expected_epoch=second.config_epoch
        )
        assert batch.candidates == (candidate,)
        supervisor.reject_candidates(batch.candidates)
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_required_failed_candidate_retries_in_background_with_retry_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    triggers: list[str] = []

    async def no_network(self, runtime):
        triggers.append(runtime.trigger)
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", no_network)

    async def run() -> None:
        supervisor = McpServerSupervisor(
            retry_base_seconds=0.01,
            retry_max_seconds=0.01,
        )
        config = _config(required=True)
        first = supervisor.prepare((config,), trigger="initial")
        await asyncio.sleep(0)
        runtime = supervisor._current_attempts["docs"]
        failed = supervisor._failed_candidate(
            runtime,
            status=McpServerStatus.FAILED,
            exc=RuntimeError("synthetic startup failure"),
        )
        supervisor._candidates.append(failed)
        supervisor._schedule_retry("docs")

        before_due = supervisor.prepare((config,), trigger="config_change")
        assert before_due.server_attempts == {}

        for _ in range(20):
            if (
                supervisor._generation_by_server["docs"] == 2
                and triggers == ["initial", "retry"]
            ):
                break
            await asyncio.sleep(0.01)
        assert supervisor._generation_by_server["docs"] == 2
        assert triggers == ["initial", "retry"]
        retried = supervisor._current_attempts["docs"].attempt
        assert (
            retried.reconcile_attempt_id
            != first.server_attempts["docs"].reconcile_attempt_id
        )
        assert retried.reserved_discovery_generation == 2
        assert failed not in supervisor._candidates
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_safe_point_joins_running_required_background_retry_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_started = asyncio.Event()
    release_retry = asyncio.Event()

    async def controlled_run(self, runtime):
        if runtime.trigger != "retry":
            return
        retry_started.set()
        await release_retry.wait()
        config = runtime.spec.config
        snapshot = _snapshot(
            config,
            attempt_id=runtime.attempt.reconcile_attempt_id,
            generation=runtime.attempt.reserved_discovery_generation,
        )
        manager = MockMcpClientManager(_snapshots=(snapshot,))
        candidate = McpServerCandidate(
            ticket_id=runtime.ticket_id,
            config_epoch=runtime.attempt.config_epoch,
            reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,
            reserved_discovery_generation=(
                runtime.attempt.reserved_discovery_generation
            ),
            server_snapshot=snapshot,
            runtime_spec=runtime.spec,
            manager_slot=new_mcp_slot(
                spec=runtime.spec,
                snapshot=snapshot,
                manager=manager,
            ),
            trigger="retry",
            retry_attempt=1,
        )
        with self._state_lock:
            self._candidates.append(candidate)

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", controlled_run)

    async def run() -> None:
        supervisor = McpServerSupervisor(
            retry_base_seconds=0.01,
            retry_max_seconds=0.01,
        )
        config = _config(required=True)
        first = supervisor.prepare((config,), trigger="initial")
        await supervisor._workers["docs"]
        supervisor._schedule_retry("docs")
        await asyncio.wait_for(retry_started.wait(), timeout=0.2)
        retry_runtime = supervisor._current_attempts["docs"]
        assert retry_runtime.trigger == "retry"

        safe_point_ticket = supervisor.prepare(
            (config,),
            trigger="config_change",
        )
        assert (
            safe_point_ticket.server_attempts["docs"]
            == retry_runtime.attempt
        )
        assert (
            safe_point_ticket.server_attempts["docs"].reconcile_attempt_id
            != first.server_attempts["docs"].reconcile_attempt_id
        )

        required_wait = asyncio.create_task(
            supervisor.await_required(safe_point_ticket)
        )
        await asyncio.sleep(0)
        assert not required_wait.done()
        release_retry.set()
        result = await asyncio.wait_for(required_wait, timeout=0.2)
        assert result.ready_server_ids == ("docs",)

        batch = supervisor.drain_installable_candidates(
            expected_epoch=safe_point_ticket.config_epoch
        )
        supervisor.reject_candidates(batch.candidates)
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_real_attempt_failure_owns_background_retry_timer_until_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.runtime.mcp.supervisor as supervisor_module

    async def fail_connect(cls, config, *, timeout_seconds):
        del cls, config, timeout_seconds
        raise RuntimeError("synthetic connect failure")

    monkeypatch.setattr(
        supervisor_module.SdkMcpConnection,
        "connect",
        classmethod(fail_connect),
    )

    async def run() -> None:
        supervisor = McpServerSupervisor(
            retry_base_seconds=0.01,
            retry_max_seconds=0.01,
        )
        supervisor.prepare((_config(),), trigger="initial")
        for _ in range(30):
            if supervisor._generation_by_server.get("docs", 0) >= 2:
                break
            await asyncio.sleep(0.01)
        assert supervisor._generation_by_server["docs"] >= 2
        assert supervisor._current_attempts["docs"].trigger == "retry"
        assert supervisor._retry_attempts["docs"] >= 1

        await supervisor.aclose(timeout_seconds=1)
        await asyncio.sleep(0)
        assert supervisor._retry_tasks == {}
        assert supervisor._retry_timer_tasks == set()
        assert supervisor._owned_background_tasks == set()

    asyncio.run(run())


def test_runtime_config_change_and_removal_reset_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_network(self, runtime):
        return None

    monkeypatch.setattr(McpServerSupervisor, "_run_attempt", no_network)

    async def run() -> None:
        supervisor = McpServerSupervisor(retry_base_seconds=60)
        first = supervisor.prepare((_config(),), trigger="initial")
        await asyncio.sleep(0)
        supervisor._schedule_retry("docs")
        old_timer = supervisor._retry_tasks["docs"]
        assert supervisor._retry_attempts["docs"] == 1

        second = supervisor.prepare(
            (_config(), _config("other")),
            trigger="config_change",
        )
        assert second.config_epoch == first.config_epoch + 1
        assert "docs" not in supervisor._retry_attempts
        assert "docs" not in supervisor._next_retry_monotonic
        await asyncio.sleep(0)
        assert old_timer.done()

        supervisor._schedule_retry("docs")
        assert "docs" in supervisor._retry_attempts
        supervisor.prepare((), trigger="config_change")
        assert "docs" not in supervisor._retry_attempts
        assert "docs" not in supervisor._next_retry_monotonic
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_config_change_produces_new_epoch() -> None:
    async def run() -> None:
        supervisor = McpServerSupervisor()
        first = supervisor.prepare((_config(),), trigger="initial")
        second = supervisor.prepare(
            (replace(_config(), tool_timeout_ms=2_000),),
            trigger="config_change",
        )
        assert second.config_epoch == first.config_epoch + 1
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())
