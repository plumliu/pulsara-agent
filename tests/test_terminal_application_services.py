from __future__ import annotations

from types import SimpleNamespace

import pytest

from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.runtime.terminal_application.control_projection import (
    TerminalControlProjectionStore,
    TerminalControlSourceCaptureOwner,
    build_terminal_control_capture_input,
)
from pulsara_agent.runtime.terminal_presentation.projection import _text_block


def test_control_baseline_accepts_first_transition_from_generation_zero() -> None:
    item = _active_item(ordinal=1)
    active_accumulator = context_fingerprint(
        "terminal-active-prompt-queue-items:v1", (item.view_fingerprint,)
    )
    checkpoint = SimpleNamespace(
        checkpoint_fingerprint="checkpoint:genesis",
        checkpoint_generation=0,
        through_sequence=0,
        transition_count=0,
        transition_accumulator="checkpoint-transition:genesis",
    )
    receipt = SimpleNamespace(
        checkpoint_fingerprint=checkpoint.checkpoint_fingerprint,
        checkpoint_generation=0,
        checkpoint_through_sequence=0,
        checkpoint_transition_count=0,
        checkpoint_transition_accumulator=checkpoint.transition_accumulator,
        resulting_account_revision=1,
        resulting_active_client_item_count=1,
        resulting_active_client_item_accumulator=active_accumulator,
        bounded_tail_count=1,
        bounded_tail_first_sequence=7,
        bounded_tail_last_sequence=7,
        bounded_tail_accumulator="queue-tail:one",
        resulting_queue_head_event_id="event:queue-accepted",
        resulting_queue_head_payload_fingerprint="queue-event:one",
        receipt_fingerprint="queue-head-receipt:one",
    )
    snapshot = _session_snapshot(items=(item,), account_revision=1)

    owner = TerminalControlSourceCaptureOwner(runtime_session_id="runtime:control")
    captured = owner.capture(
        lambda: build_terminal_control_capture_input(
            session_snapshot=snapshot,
            queue_checkpoint=checkpoint,
            queue_head_receipt=receipt,
            durable_active_item_count=1,
            durable_active_item_accumulator=active_accumulator,
        )
    )
    control = TerminalControlProjectionStore(
        runtime_session_id="runtime:control"
    ).install_captured(captured)

    assert control.view.prompt_queue.queue_head.head_kind == "committed"
    assert control.view.prompt_queue.queue_head.checkpoint_generation == 0
    assert control.view.prompt_queue.queue_head.head_event_id == (
        "event:queue-accepted"
    )
    assert control.view.prompt_queue.ordered_active_items == (item,)
    assert len(captured.ordered_fence_receipts) == 5
    assert tuple(item.section_kind for item in captured.ordered_fence_receipts) == (
        "session_lifecycle",
        "run_control",
        "pending_interaction",
        "prompt_queue",
        "notifications",
    )


def test_control_transition_ring_proves_control_only_successor() -> None:
    item = _active_item(ordinal=1)
    active_accumulator = context_fingerprint(
        "terminal-active-prompt-queue-items:v1", (item.view_fingerprint,)
    )
    checkpoint = SimpleNamespace(
        checkpoint_fingerprint="checkpoint:genesis",
        checkpoint_generation=0,
        through_sequence=0,
        transition_count=0,
        transition_accumulator="checkpoint-transition:genesis",
    )
    receipt = SimpleNamespace(
        checkpoint_fingerprint=checkpoint.checkpoint_fingerprint,
        checkpoint_generation=0,
        checkpoint_through_sequence=0,
        checkpoint_transition_count=0,
        checkpoint_transition_accumulator=checkpoint.transition_accumulator,
        resulting_account_revision=1,
        resulting_active_client_item_count=1,
        resulting_active_client_item_accumulator=active_accumulator,
        bounded_tail_count=1,
        bounded_tail_first_sequence=7,
        bounded_tail_last_sequence=7,
        bounded_tail_accumulator="queue-tail:one",
        resulting_queue_head_event_id="event:queue-accepted",
        resulting_queue_head_payload_fingerprint="queue-event:one",
        receipt_fingerprint="queue-head-receipt:one",
    )
    store = TerminalControlProjectionStore(runtime_session_id="runtime:control")
    capture_owner = TerminalControlSourceCaptureOwner(
        runtime_session_id="runtime:control"
    )
    initial = _capture_control(
        store=store,
        capture_owner=capture_owner,
        snapshot=_session_snapshot(items=(item,), account_revision=1),
        checkpoint=checkpoint,
        receipt=receipt,
        active_item_count=1,
        active_item_accumulator=active_accumulator,
    )
    successor_source = _session_snapshot(items=(item,), account_revision=1)
    successor_source.active_run_id = "run:control-only"
    successor = _capture_control(
        store=store,
        capture_owner=capture_owner,
        snapshot=successor_source,
        checkpoint=checkpoint,
        receipt=receipt,
        active_item_count=1,
        active_item_accumulator=active_accumulator,
    )

    read = store.read_after(initial.cursor)

    assert read.status == "changed"
    assert read.changed_sections == ("run_control",)
    assert len(read.ordered_records) == 1
    assert read.latest_cursor == successor.cursor
    assert read.ordered_records[0].base_control_projection_fingerprint == (
        initial.view.control_view_fingerprint
    )
    assert read.ordered_records[0].resulting_control_projection_fingerprint == (
        successor.view.control_view_fingerprint
    )
    initial_versions = {
        item.section_kind: item.source_owner_revision
        for item in initial.view.section_versions
    }
    successor_versions = {
        item.section_kind: item.source_owner_revision
        for item in successor.view.section_versions
    }
    assert successor_versions["run_control"] == initial_versions["run_control"] + 1
    assert {
        key: value for key, value in successor_versions.items() if key != "run_control"
    } == {key: value for key, value in initial_versions.items() if key != "run_control"}


def test_control_baseline_rejects_more_than_64_active_queue_items() -> None:
    items = tuple(_active_item(ordinal=index) for index in range(1, 66))
    with pytest.raises(RuntimeError, match="exceeds 64"):
        TerminalControlSourceCaptureOwner(runtime_session_id="runtime:control").capture(
            lambda: build_terminal_control_capture_input(
                session_snapshot=_session_snapshot(items=items, account_revision=65),
                queue_checkpoint=SimpleNamespace(),
                queue_head_receipt=SimpleNamespace(),
                durable_active_item_count=65,
                durable_active_item_accumulator="not-reached",
            )
        )


def test_terminal_public_text_neutralizes_ansi_osc_and_c1_controls() -> None:
    block = _text_block(
        "safe\x1b]52;c;clipboard\x07\x9b31mred\r\nnext",
        role="primary",
    )

    assert "\x1b" not in block.text
    assert "\x07" not in block.text
    assert "\x9b" not in block.text
    assert "\r" not in block.text
    assert block.text == ("safe\\x1B]52;c;clipboard\\x07\\x9B31mred\\x0D\nnext")
    assert block.text_utf8_bytes == len(block.text.encode("utf-8"))


def _capture_control(
    *,
    store,
    capture_owner,
    snapshot,
    checkpoint,
    receipt,
    active_item_count,
    active_item_accumulator,
):
    captured = capture_owner.capture(
        lambda: build_terminal_control_capture_input(
            session_snapshot=snapshot,
            queue_checkpoint=checkpoint,
            queue_head_receipt=receipt,
            durable_active_item_count=active_item_count,
            durable_active_item_accumulator=active_item_accumulator,
        )
    )
    return store.install_captured(captured)


def _active_item(*, ordinal: int):
    return SimpleNamespace(
        queue_item_id=f"queue:{ordinal}",
        accepted_ordinal=ordinal,
        delivery_state="accepted_pending",
        content_retention_state="active",
        view_fingerprint=f"queue-view:{ordinal}",
    )


def _session_snapshot(*, items: tuple[object, ...], account_revision: int):
    return SimpleNamespace(
        runtime_session_id="runtime:control",
        lifecycle="open",
        active_run_id=None,
        suspended_run_id=None,
        stopping_run_id=None,
        pending_interaction=None,
        queue_items=items,
        queue_account_revision=account_revision,
        snapshot_fingerprint=f"snapshot:{account_revision}",
    )
