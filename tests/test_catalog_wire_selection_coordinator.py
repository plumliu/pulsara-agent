"""The ordinary dispatch owner follows the final-wire selected candidate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.compaction.coordinator import (
    CompactionCoordinator,
)


@pytest.mark.parametrize("admitted", (True, False))
def test_measure_dispatch_adopts_selected_candidate_before_returning(admitted):
    original = object()
    selected = object()
    observation = object()
    decision = SimpleNamespace(
        candidate=selected, wire_input_plan=object() if admitted else None
    )
    selections = []
    dispatch = SimpleNamespace(select_wire_candidate=selections.append)

    def candidate_for_dispatch(value):
        assert value is dispatch
        return original

    async def measure(candidate, *, deadline, reusable_observation):
        assert candidate is original
        assert deadline == 123.0
        assert reusable_observation is observation
        assert not selections
        return decision

    coordinator = object.__new__(CompactionCoordinator)
    coordinator._provider_dispatch = SimpleNamespace(
        wire_candidate_for_dispatch=candidate_for_dispatch,
        measure_prepared_wire_candidate=measure,
    )

    result = asyncio.run(
        coordinator.measure_dispatch_wire(
            dispatch, deadline=123.0, reusable_observation=observation
        )
    )

    assert result is decision
    assert selections == [selected]


def test_failed_measurement_does_not_select_a_partial_candidate():
    selections = []
    dispatch = SimpleNamespace(select_wire_candidate=selections.append)

    async def measure(*_args, **_kwargs):
        raise TimeoutError("planning expired")

    coordinator = object.__new__(CompactionCoordinator)
    coordinator._provider_dispatch = SimpleNamespace(
        wire_candidate_for_dispatch=lambda _dispatch: object(),
        measure_prepared_wire_candidate=measure,
    )

    with pytest.raises(TimeoutError, match="planning expired"):
        asyncio.run(coordinator.measure_dispatch_wire(dispatch, deadline=123.0))

    assert not selections
