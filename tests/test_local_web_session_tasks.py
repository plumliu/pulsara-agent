from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.tool_permission import EffectivePermissionPolicy
from pulsara_agent.web_app.session_controller import (
    LocalSessionController,
    _create_quick_workspace_root,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


def _task_row(
    ordinal: int,
    *,
    status: str,
    total_count: int = 3,
) -> dict[str, object]:
    accepted_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc) + timedelta(
        seconds=ordinal
    )
    completed = status in {
        "COMPLETED",
        "CANCELLED",
        "FAILED",
        "INTERRUPTED",
        "BLOCKED_DEPENDENCY_FAILED",
    }
    return {
        "id": f"task-{ordinal}",
        "batch_id": "batch-a",
        "task_key": f"part-{ordinal}",
        "label": f"分工 {ordinal}",
        "profile_kind": "general_worker",
        "display_role": "协作者",
        "context_mode": "LAST_N" if ordinal == 2 else "NONE",
        "context_last_n_turns": 3 if ordinal == 2 else None,
        "parent_turn_id": "turn-root",
        "objective": f"完成第 {ordinal} 部分",
        "status": status,
        "pending_reason": "WAITING_DEPENDENCY" if status == "WAITING_DEPENDENCY" else None,
        "terminal_reason": "DONE" if completed else None,
        "accepted_at": accepted_at,
        "terminal_at": accepted_at + timedelta(seconds=1) if completed else None,
        "result_id": f"result-{ordinal}" if status == "COMPLETED" else None,
        "result_entry_id": f"entry-{ordinal}" if status == "COMPLETED" else None,
        "result_source": "EXPLICIT" if status == "COMPLETED" else None,
        "result_summary": f"第 {ordinal} 部分完成" if status == "COMPLETED" else None,
        "result_output_preview": "可见输出" if status == "COMPLETED" else None,
        "result_diagnostics": [{"message": "验证通过"}] if status == "COMPLETED" else None,
        "accepted_root_entry_id": None,
        "total_count": total_count,
    }


class _TaskCore:
    def __init__(self) -> None:
        self.rows = (
            _task_row(1, status="COMPLETED"),
            _task_row(2, status="WAITING_DEPENDENCY"),
            _task_row(3, status="BLOCKED_DEPENDENCY_FAILED"),
        )
        self.calls: list[tuple[datetime | None, str | None]] = []

    async def read_resumable_session(self, session_id: str, **_kwargs: object):
        return SimpleNamespace(session_id=session_id)

    async def read_subagent_task_page(
        self,
        *,
        session_id: str,
        maximum_items: int,
        after_accepted_at: datetime | None,
        after_task_id: str | None,
    ):
        assert session_id == "session-1"
        assert maximum_items == 2
        self.calls.append((after_accepted_at, after_task_id))
        start = 0
        if after_task_id is not None:
            start = next(
                index + 1
                for index, row in enumerate(self.rows)
                if row["id"] == after_task_id
            )
        page = self.rows[start : start + maximum_items + 1]
        dependencies = ()
        if start == 0:
            dependencies = (
                {
                    "task_id": "task-2",
                    "dependency_task_id": "task-1",
                    "task_key": "part-1",
                    "label": "分工 1",
                    "status": "COMPLETED",
                    "result_id": "result-1",
                    "result_source": "EXPLICIT",
                    "summary": "第 1 部分完成",
                },
            )
        return page, dependencies


def _controller(tmp_path: Path, core: _TaskCore) -> LocalSessionController:
    return LocalSessionController(
        core=cast(KernelHostCore, core),
        workspace_input=HostWorkspaceInput(
            workspace_kind="project",
            workspace_root=tmp_path,
            memory_domain_id="u_local",
        ),
        model_role=ModelRole.PRO,
        permission_policy=cast(EffectivePermissionPolicy, object()),
        active_skill_names=frozenset(),
    )


def test_quick_workspace_root_uses_readable_timestamp_and_short_random_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "workspaces"
    managed_root.mkdir()
    now = datetime(2026, 8, 31, 13, 42, 7, tzinfo=timezone.utc)
    tokens = iter(
        (
            "a1b2c3d4" + "0" * 24,
            "a1b2c3d4" + "1" * 24,
            "deadbeef" + "2" * 24,
        )
    )
    monkeypatch.setattr(
        "pulsara_agent.web_app.session_controller.uuid4",
        lambda: SimpleNamespace(hex=next(tokens)),
    )

    first = _create_quick_workspace_root(managed_root, now)
    second = _create_quick_workspace_root(managed_root, now)

    assert first.name == "quick-20260831-134207-a1b2c3d4"
    assert second.name == "quick-20260831-134207-deadbeef"
    assert first.is_dir() and second.is_dir()


def test_session_task_inventory_pages_every_durable_status_and_dependency(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        core = _TaskCore()
        controller = _controller(tmp_path, core)

        first = await controller.list_session_tasks(
            "session-1", maximum_items=2
        )
        assert first["total_count"] == 3
        assert first["remaining_count"] == 1
        assert first["next_cursor"]
        assert [task["status"] for task in first["tasks"]] == [
            "COMPLETED",
            "WAITING_DEPENDENCY",
        ]
        assert first["tasks"][1]["dependencies"] == [
            {
                "task_id": "task-1",
                "task_key": "part-1",
                "label": "分工 1",
                "status": "COMPLETED",
                "result_id": "result-1",
                "result_source": "EXPLICIT",
                "result_summary": "第 1 部分完成",
            }
        ]
        assert first["tasks"][0]["result"] == {
            "id": "result-1",
            "entry_id": "entry-1",
            "source": "EXPLICIT",
            "summary": "第 1 部分完成",
            "output_preview": "可见输出",
            "diagnostics": [{"message": "验证通过"}],
            "accepted": False,
        }

        second = await controller.list_session_tasks(
            "session-1",
            maximum_items=2,
            cursor=cast(str, first["next_cursor"]),
        )
        assert second["next_cursor"] is None
        assert second["remaining_count"] == 0
        assert [task["status"] for task in second["tasks"]] == [
            "BLOCKED_DEPENDENCY_FAILED"
        ]
        assert core.calls[1][1] == "task-2"

    asyncio.run(exercise())


def test_session_task_cursor_cannot_be_reused_for_another_session(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        controller = _controller(tmp_path, _TaskCore())
        first = await controller.list_session_tasks(
            "session-1", maximum_items=2
        )
        with pytest.raises(ValueError, match="cursor"):
            await controller.list_session_tasks(
                "session-2",
                maximum_items=2,
                cursor=cast(str, first["next_cursor"]),
            )

    asyncio.run(exercise())
