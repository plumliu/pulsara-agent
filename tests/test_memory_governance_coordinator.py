from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pulsara_agent.memory.governance.coordinator import MemoryGovernanceCoordinator


class _Pool:
    def __init__(self, session_id: str) -> None:
        self.pending = [SimpleNamespace(source_session_id=session_id)]

    def list_pending(self):
        return list(self.pending)


class _Engine:
    def __init__(self, session_id: str) -> None:
        self.executor = SimpleNamespace(
            runtime_session_id=session_id,
            candidate_pool=_Pool(session_id),
        )
        self.calls = 0

    async def run_pending(self, *, trigger_reason: str):
        assert trigger_reason == "turn_safe_point"
        self.calls += 1
        self.executor.candidate_pool.pending.clear()
        return SimpleNamespace(applied=[object()])


def test_governance_coordinator_debounces_safe_points_and_wakes_index_worker() -> None:
    async def scenario() -> None:
        wake_calls = 0

        def on_commit() -> None:
            nonlocal wake_calls
            wake_calls += 1

        engine = _Engine("runtime:coordinator")
        coordinator = MemoryGovernanceCoordinator(
            debounce_seconds=0.01,
            session_min_interval_seconds=0.01,
            on_commit=on_commit,
        )
        task = asyncio.create_task(coordinator.run())
        coordinator.notify(engine)  # type: ignore[arg-type]
        coordinator.notify(engine)  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        await coordinator.aclose()
        await task

        assert engine.calls == 1
        assert wake_calls == 1

    asyncio.run(scenario())


def test_governance_coordinator_ignores_sessions_without_pending_candidates() -> None:
    engine = _Engine("runtime:no-pending")
    engine.executor.candidate_pool.pending.clear()
    coordinator = MemoryGovernanceCoordinator()

    coordinator.notify(engine)  # type: ignore[arg-type]

    assert coordinator._pending == {}
