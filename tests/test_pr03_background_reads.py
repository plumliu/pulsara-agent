from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.terminal_process.manager import ProcessRegistry
from pulsara_agent.terminal_protocol.v3_gateway import (
    _decode_background_cursor,
    _encode_background_cursor,
)
from pulsara_agent.web_app.session_controller import LocalSessionController


def _launch(
    registry: ProcessRegistry,
    tmp_path: Path,
    *,
    owner: str,
    program: str,
    yield_time_ms: int,
):
    return registry.exec_with_yield(
        terminal_session_id="default",
        command=program,
        cwd=tmp_path,
        yield_time_ms=yield_time_ms,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", program),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )


def test_pr03_background_inventory_uses_adoption_and_retained_log_cursor(
    tmp_path: Path,
) -> None:
    registry = ProcessRegistry(maximum_host_retained_bytes=64)
    owner = "host:background-read"
    registry.activate_owner(owner)
    foreground, foreground_yielded, _ = _launch(
        registry,
        tmp_path,
        owner=owner,
        program="raise SystemExit(0)",
        yield_time_ms=2_000,
    )
    background, background_yielded, _ = _launch(
        registry,
        tmp_path,
        owner=owner,
        program="import time; time.sleep(30)",
        yield_time_ms=0,
    )
    assert foreground_yielded is False
    assert background_yielded is True
    assert [
        item.process_id
        for item in registry.list_background_processes(
            owner_host_session_id=owner
        )
    ] == [background.process_id]
    assert foreground.process_id != background.process_id

    first = registry.log(
        background.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    )
    background.output.append_raw(("真实后台输出🙂\n" * 30).encode())
    incremental = registry.log(
        background.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
        since_cursor=first.output_cursor,
    )
    assert incremental.gap_before_output is True
    assert incremental.retained_from_cursor != first.retained_from_cursor
    assert "真实后台输出" in incremental.output
    assert background.output.inflight_read_count == 0
    registry.terminate_if_running(
        background.process_id,
        max_output_chars=512,
        owner_host_session_id=owner,
    )
    assert [
        item.process_id
        for item in registry.list_background_processes(
            owner_host_session_id=owner
        )
    ] == [background.process_id]
    registry.release_owner(owner, timeout_seconds=2)


def test_pr03_background_cursor_is_bound_to_exact_session_and_host() -> None:
    cursor = _encode_background_cursor(
        session_id="session:one",
        host_session_id="host:one",
        started_at=12.5,
        process_id="process:one",
    )
    assert _decode_background_cursor(
        cursor, session_id="session:one", host_session_id="host:one"
    ) == (12.5, "process:one")
    with pytest.raises(ValueError, match="background cursor"):
        _decode_background_cursor(
            cursor, session_id="session:one", host_session_id="host:two"
        )
    with pytest.raises(ValueError, match="background cursor"):
        _decode_background_cursor(
            cursor, session_id="session:two", host_session_id="host:one"
        )


class _ReadCore:
    def __init__(self) -> None:
        self.group_rows = [
            {
                "batch_id": "batch:a",
                "parent_turn_id": "turn:root",
                "first_accepted_at": datetime(2026, 9, 11, 1, tzinfo=timezone.utc),
                "task_count": 2,
                "pending_count": 0,
                "active_count": 1,
                "waiting_count": 1,
                "completed_count": 0,
                "cancelled_count": 0,
                "failed_count": 0,
                "interrupted_count": 0,
                "blocked_count": 0,
                "single_task_label": None,
                "total_count": 2,
            },
            {
                "batch_id": "batch:b",
                "parent_turn_id": "turn:root",
                "first_accepted_at": datetime(2026, 9, 11, 2, tzinfo=timezone.utc),
                "task_count": 1,
                "pending_count": 0,
                "active_count": 0,
                "waiting_count": 0,
                "completed_count": 1,
                "cancelled_count": 0,
                "failed_count": 0,
                "interrupted_count": 0,
                "blocked_count": 0,
                "single_task_label": "单节点",
                "total_count": 2,
            },
        ]

    async def read_resumable_session(self, session_id: str, **_kwargs):
        return SimpleNamespace(session_id=session_id)

    async def read_subagent_task_group_page(
        self, *, after_batch_id: str | None, **_kwargs
    ):
        if after_batch_id is None:
            return tuple(self.group_rows)
        return tuple(row for row in self.group_rows if row["batch_id"] > after_batch_id)

    async def read_subagent_task_activity_page(
        self, *, task_id: str, after_entry_sequence: int, **_kwargs
    ):
        if task_id != "task:a":
            raise KeyError(task_id)
        rows = [
            {
                "id": "entry:1",
                "turn_id": "turn:child",
                "entry_sequence": 10,
                "entry_kind": "ASSISTANT_TOOL_REQUEST",
                "accepted_at": datetime(2026, 9, 11, 1, 1, tzinfo=timezone.utc),
                "task_objective": "检查分页",
                "inline_content": b"first",
                "content_digest": "sha256:first",
                "content_size": 5,
                "content_media_type": "text/plain",
                "content_codec": "utf-8",
            },
            {
                "id": "entry:2",
                "turn_id": "turn:child",
                "entry_sequence": 11,
                "entry_kind": "TOOL_RESULT",
                "accepted_at": datetime(2026, 9, 11, 1, 2, tzinfo=timezone.utc),
                "task_objective": "检查分页",
                "inline_content": b"second",
                "content_digest": "sha256:second",
                "content_size": 6,
                "content_media_type": "text/plain",
                "content_codec": "utf-8",
            },
        ]
        selected = [row for row in rows if row["entry_sequence"] > after_entry_sequence]
        if after_entry_sequence == 0:
            return (
                tuple(selected[:1]),
                ({
                    "id": "block:1",
                    "assistant_entry_id": "entry:1",
                    "block_ordinal": 0,
                    "block_kind": "TOOL_CALL",
                    "tool_call_id": "call:1",
                    "tool_name": "read_file",
                },),
                (),
                True,
            )
        return tuple(selected), (), (), False


def _read_controller(core: _ReadCore) -> LocalSessionController:
    controller = object.__new__(LocalSessionController)
    controller.core = core
    controller.workspace_input = SimpleNamespace(memory_domain_id="memory:one")
    return controller


def test_pr03_task_group_and_activity_cursors_preserve_exact_read_identity() -> None:
    async def scenario() -> None:
        controller = _read_controller(_ReadCore())
        first = await controller.list_session_task_groups(
            "session:one", maximum_items=1
        )
        assert [value["group_id"] for value in first["groups"]] == ["batch:a"]
        assert first["remaining_count"] == 1
        assert isinstance(first["next_cursor"], str)
        second = await controller.list_session_task_groups(
            "session:one", maximum_items=1, cursor=first["next_cursor"]
        )
        assert [value["group_id"] for value in second["groups"]] == ["batch:b"]
        with pytest.raises(ValueError, match="task group cursor"):
            await controller.list_session_task_groups(
                "session:other", maximum_items=1, cursor=first["next_cursor"]
            )

        activity = await controller.list_session_task_activities(
            "session:one", "task:a", maximum_items=1
        )
        assert activity["activities"][0]["blocks"][0]["tool_name"] == "read_file"
        assert isinstance(activity["next_cursor"], str)
        continued = await controller.list_session_task_activities(
            "session:one",
            "task:a",
            maximum_items=1,
            cursor=activity["next_cursor"],
        )
        assert [value["entry_id"] for value in continued["activities"]] == [
            "entry:2"
        ]
        with pytest.raises(ValueError, match="task activity cursor"):
            await controller.list_session_task_activities(
                "session:one",
                "task:b",
                maximum_items=1,
                cursor=activity["next_cursor"],
            )

    asyncio.run(scenario())
