"""Compaction progress reuses live owners and never manufactures adoption history."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import CompactionScope, CompactionTrigger
from pulsara_agent.conversation_kernel.compaction.runtime import HostCompactionRuntimeOwner
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer, _Connection


@pytest.mark.parametrize('role', [wire.ATTACHMENT_ROLE_CONTROLLER, wire.ATTACHMENT_ROLE_OBSERVER])
@pytest.mark.parametrize('trigger', [CompactionTrigger.MANUAL, CompactionTrigger.AUTO_ACTIVE_CONTEXT, CompactionTrigger.MODEL_SWITCH_HANDOVER])
def test_progress_follows_root_fence_in_snapshot_and_observation(role: int, trigger: CompactionTrigger) -> None:
    async def run() -> None:
        owner = HostCompactionRuntimeOwner()
        host = SimpleNamespace(
            session_id='session:336wrfwkamrr3up6kjtsgvgcte', host_session_id='host:yinyxklzunar54s7objjdbphcy', _compaction=owner,
            live_bus=LiveAgentEventBus(), live_control=SessionLiveControlOwner(session_id='session:336wrfwkamrr3up6kjtsgvgcte'),
            current_todo_snapshots=lambda: (), active_root_turn_id=lambda: None,
            control_admission_deadline_ms=lambda: 10000, take_presentation_notices=lambda _: (),
            current_compaction_projection=owner.current_projection,
        )
        host.root_compaction_in_progress = lambda: KernelHostSession.root_compaction_in_progress(host)
        observer, epoch, revision = host.live_bus.subscribe()
        state = _Connection(
            attachment_id='attachment:hmfv6qyb4msxxwadjyltzzsdse', attachment_generation=1, host_session=host,
            granted_role=role, authenticated=True, live_observer_id=observer,
            live_epoch=epoch, live_revision=revision,
            protocol_reader=SimpleNamespace(observe_committed=lambda **_: SimpleNamespace(
                projections=(), through_event_sequence=0, gap_reason=None)),
        )
        server = TerminalKernelProtocolServer(socket_path=Path('/tmp/compaction-progress-test.sock'), session_provider=lambda _: host)
        root = CompactionScope('session:336wrfwkamrr3up6kjtsgvgcte', 'workspace:wdl6b3mtrkghwnvjo4an3ekbau', 'turn:klmmazz67vfgsfsvg6ceqgdsfu', ModelInputScopeKind.ROOT, None)
        child = CompactionScope('session:336wrfwkamrr3up6kjtsgvgcte', 'workspace:wdl6b3mtrkghwnvjo4an3ekbau', 'turn:h2g7tkxc3t3s7qaeuv3uyoxu7m', ModelInputScopeKind.SUBAGENT_TASK, 'task:kmz6pl55dsqspcz6dgmhr7sxpy')
        task = asyncio.current_task()
        assert task is not None

        def snapshot() -> bool:
            return server._live_control_snapshot(state, wire.LiveControlSnapshotRequest(request_id='snapshot')).live_control_snapshot.snapshot.root_compaction_in_progress

        async def observed() -> bool:
            frame = await server._observe(state, wire.ObserveRequest(
                request_id='observe', maximum_events=10, maximum_bytes=4096,
                live_owner_epoch=epoch, live_control_owner_epoch=state.live_control_epoch, wait_ms=0))
            assert not frame.observation.HasField('gap')
            assert not frame.observation.committed
            assert not frame.observation.live_control
            return frame.observation.root_compaction_in_progress

        assert not snapshot()
        owner.install_fence_under_host_lock(scope=child, trigger=trigger, attempt_id='child', owner_task=task)
        assert not snapshot()
        assert not await observed()
        # ROOT can start after the subagent, so the first global fence is insufficient.
        owner.install_fence_under_host_lock(scope=root, trigger=trigger, attempt_id='root', owner_task=task)
        assert snapshot()
        assert await observed()
        owner.remove_fence_under_host_lock(scope=root, attempt_id='root', owner_task=task)
        assert not await observed()
        assert not snapshot()
        owner.remove_fence_under_host_lock(scope=child, attempt_id='child', owner_task=task)
        host.live_bus.close()
        host.live_control.close()

    asyncio.run(run())
