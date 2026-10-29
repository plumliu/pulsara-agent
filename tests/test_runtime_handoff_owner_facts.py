"""Handoff reads the owner facts without joining delivery or retention."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.code_mode import CodeModeError, CodeModeHost
from pulsara_agent.code_mode.contracts import CodeModeObservation
from pulsara_agent.code_mode.host import _Cell, _Output, _Pending
from pulsara_agent.code_mode.targets import cell_target
from pulsara_agent.identity import random_id


def test_cell_handoff_filters_complete_pending_without_consuming_or_retaining():
    async def scenario():
        host = CodeModeHost()

        def cell(state, *, delivered=False):
            value = _Cell(
                cell_id=random_id("cell"), turn_id="turn", source_exec_attempt_id="attempt",
                snapshot=object(), tool_catalog={}, invoke=None, prepare_batch=None,
                admitted=asyncio.get_running_loop().create_future(),
                state=state, terminal_delivered=delivered,
            )
            host._cells[value.cell_id] = value
            return value

        queued = cell("queued", delivered=True)
        running = cell("running", delivered=True)
        unacknowledged = cell("completed")
        unread = cell("failed", delivered=True)
        unread.output.append(_Output({"kind": "log", "text": "unread"}, charge=12))
        preparation = cell("terminated", delivered=True)
        preparation.pending["prepare"] = _Pending("prepare", None, None, 4)
        call = cell("terminated", delivered=True)
        call.pending["invoke"] = _Pending("invoke", 1, "read_file", 4)
        cleanup = cell("completed", delivered=True)
        cleanup.observation = CodeModeObservation(cleanup.cell_id, "completed", (), False, 0, None)
        cleanup.vm_released.set()
        vm_cleanup = cell("terminated", delivered=True)
        before = {
            value.cell_id: (value.terminal_delivered, tuple(value.output), dict(value.pending),
                            value.observation, dict(value.reservations), value.observed_count)
            for value in host._cells.values()
        }

        facts = host.freeze_compaction_handoff()
        expected = sorted((queued, running, unacknowledged, unread, preparation, call),
                          key=lambda value: value.cell_id)
        assert facts == tuple(
            {"target": cell_target(value.cell_id), "state": value.state,
             "has_unread_output": bool(value.output), "pending_calls": value.pending_calls}
            for value in expected
        )
        assert next(row for row in facts if row["target"] == cell_target(preparation.cell_id))["pending_calls"] == 0
        assert {
            value.cell_id: (value.terminal_delivered, tuple(value.output), dict(value.pending),
                            value.observation, dict(value.reservations), value.observed_count)
            for value in host._cells.values()
        } == before
        # User controls retain all current cells, including internal cleanup.
        assert {row["cell_id"] for row in host.snapshot_cell_index()} == set(host._cells)
        assert {row["target"]["cell_id"] for row in host.query_cells()["cells"]} == set(host._cells)
        assert cleanup.cell_id not in {row["target"]["cell_id"] for row in facts}
        assert vm_cleanup.cell_id not in {row["target"]["cell_id"] for row in facts}

        # A frozen fact is a value, not a Cell owner. The existing collector may
        # forget the object immediately after its real terminal delivery.
        forgotten = []

        async def send(message):
            forgotten.append(message)

        host._safe_send = send
        unacknowledged.terminal_delivered = True
        unacknowledged.vm_released.set()
        await host._collect(unacknowledged)
        assert unacknowledged.cell_id not in host._cells
        assert forgotten == [{"type": "forget", "cell_id": unacknowledged.cell_id}]
        assert any(row["target"] == cell_target(unacknowledged.cell_id) for row in facts)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["closed", "sidecar", "exited"])
def test_cell_handoff_preserves_typed_unavailable_instead_of_empty(failure):
    host = CodeModeHost()
    if failure == "closed":
        host._closed = True
    elif failure == "sidecar":
        host._sidecar_failure = "sidecar failed"
    else:
        host._process = SimpleNamespace(returncode=1)
    with pytest.raises(CodeModeError) as caught:
        host.freeze_compaction_handoff()
    assert caught.value.code == "unavailable"


def test_available_cell_owner_can_observe_an_empty_inventory():
    assert CodeModeHost().freeze_compaction_handoff() == ()
