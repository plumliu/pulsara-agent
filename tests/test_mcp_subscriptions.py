from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import mcp.types as sdk_types

from pulsara_agent.runtime.mcp.sdk import SdkMcpClientManager
from pulsara_agent.runtime.mcp.subscriptions import (
    McpServerDirtySignal,
    McpSnapshotDirtyReason,
)
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor
from pulsara_agent.runtime.mcp.types import (
    McpServerRuntimeSpec,
    new_mcp_slot,
    runtime_mcp_config_fingerprint,
)
from tests.test_mcp_sdk_discovery import (
    _DiscoverySession,
    _connection,
    _discover,
)


def test_subscription_starts_only_after_explicit_slot_activation() -> None:
    async def run() -> None:
        session = _DiscoverySession()
        session.release.set()
        connection = _connection(session, tools=True)
        snapshot, _request_count, _page_count = await _discover(connection)
        raw = connection._connection
        raw.client.server_capabilities = sdk_types.ServerCapabilities(
            tools=sdk_types.ToolsCapability(list_changed=True)
        )
        entered = asyncio.Event()
        release_notification = asyncio.Event()
        signals: list[McpServerDirtySignal] = []

        @asynccontextmanager
        async def listen(**_kwargs):
            entered.set()

            async def events():
                await release_notification.wait()
                yield SimpleNamespace()

            yield events()

        raw.client.listen = listen
        manager = SdkMcpClientManager.from_connected_server(
            connection=connection,
            snapshot=snapshot,
            dirty_callback=signals.append,
        )
        assert raw.subscription_task is None
        assert not entered.is_set()

        manager.activate_subscription()
        await asyncio.wait_for(entered.wait(), timeout=1)
        release_notification.set()
        while not signals:
            await asyncio.sleep(0)

        signal = signals[0]
        assert signal.server_id == snapshot.server_id
        assert signal.snapshot_id == snapshot.snapshot_id
        assert signal.config_epoch == snapshot.config_epoch
        assert signal.discovery_generation == snapshot.discovery_generation
        assert signal.dirty_reasons == (McpSnapshotDirtyReason.LIST_CHANGED,)
        await manager.aclose()

    asyncio.run(run())


def test_supervisor_ignores_dirty_signal_from_retired_generation(monkeypatch) -> None:
    async def run() -> None:
        session = _DiscoverySession()
        session.release.set()
        connection = _connection(session, tools=True)
        snapshot, _request_count, _page_count = await _discover(connection)
        manager = SdkMcpClientManager.from_connected_server(
            connection=connection,
            snapshot=snapshot,
        )
        config = connection._connection.config
        spec = McpServerRuntimeSpec(
            config=config,
            runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
            event_safe_config_fingerprint=snapshot.event_safe_config_fingerprint,
        )
        slot = new_mcp_slot(spec=spec, snapshot=snapshot, manager=manager)
        slot.lifecycle = "installed"
        supervisor = McpServerSupervisor()
        supervisor._epoch = snapshot.config_epoch
        supervisor._desired_specs[config.server_id] = spec
        supervisor._slots[slot.slot_id] = slot
        supervisor._installed_slot_by_server[config.server_id] = slot.slot_id
        calls: list[tuple[tuple[object, ...], str]] = []

        def prepare(_self, configs, *, trigger):
            calls.append((tuple(configs), trigger))
            return None

        monkeypatch.setattr(McpServerSupervisor, "prepare", prepare)
        authority = snapshot.authority
        assert authority is not None
        exact = McpServerDirtySignal(
            server_id=snapshot.server_id,
            snapshot_id=snapshot.snapshot_id,
            config_epoch=snapshot.config_epoch,
            discovery_generation=snapshot.discovery_generation,
            transport_generation=(
                authority.discovery_attribution.transport_generation
            ),
            signal_generation=1,
            dirty_reasons=(McpSnapshotDirtyReason.LIST_CHANGED,),
            dirty_kinds=("tools",),
            observed_monotonic=1.0,
        )

        supervisor._on_subscription_dirty(
            replace(exact, snapshot_id="mcp_snapshot:retired")
        )
        supervisor._on_subscription_dirty(
            replace(exact, discovery_generation=exact.discovery_generation + 1)
        )
        assert calls == []

        supervisor._on_subscription_dirty(exact)
        assert calls == [((config,), "manual_refresh")]
        assert supervisor._subscription_retry_servers == {config.server_id}
        before = time.monotonic()
        supervisor._schedule_retry(config.server_id)
        delay = supervisor._next_retry_monotonic[config.server_id] - before
        assert 0 < delay <= 0.2
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())
