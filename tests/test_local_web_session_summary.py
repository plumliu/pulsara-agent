from __future__ import annotations

from datetime import datetime, timezone

from pulsara_agent.conversation_kernel.host import KernelSessionSummary
from pulsara_agent.web_app.session_controller import LocalSessionController


def test_session_summary_keeps_live_state_out_of_display_copy() -> None:
    summary = KernelSessionSummary(
        session_id="session:1234567890",
        workspace_id="workspace:1",
        workspace_kind="project",
        workspace_root="/tmp/project",
        workspace_label="project",
        memory_domain_id="memory-domain:1",
        lifecycle="OPEN",
        writer_generation=4,
        latest_entry_sequence=13,
        updated_at=datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc),
        subagent_task_total=7,
        subagent_task_active=2,
        subagent_task_waiting=1,
        subagent_task_attention=2,
    )

    resumable = LocalSessionController._summary_payload(summary, live=False)
    loaded = LocalSessionController._summary_payload(summary, live=True)

    assert resumable["subtitle"] == "13 条记录"
    assert loaded["subtitle"] == "13 条记录"
    assert resumable["live"] is False
    assert loaded["live"] is True
    assert resumable["status"] == "completed"
    assert loaded["status"] == "waiting"
    assert loaded["task_counts"] == {
        "total": 7,
        "active": 2,
        "waiting": 1,
        "attention": 2,
    }
