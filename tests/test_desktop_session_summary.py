from __future__ import annotations

from datetime import datetime, timezone

from pulsara_agent.conversation_kernel.host import KernelSessionSummary
from pulsara_agent.desktop_app.session_controller import LocalSessionController


def test_session_summary_keeps_live_state_out_of_display_copy() -> None:
    summary = KernelSessionSummary(
        session_id='session:ieymc5yb2mysjmkepfxgiqst3i',
        workspace_id='workspace:tx722zsrfsrhp7m363cs7ve6ii',
        workspace_kind="project",
        workspace_root="/tmp/project",
        workspace_label="project",
        memory_domain_id="memory-domain:1",
        lifecycle="OPEN",
        writer_generation=4,
        latest_entry_sequence=13,
        updated_at=datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc),
        subagent_count=7,
    )

    resumable = LocalSessionController._summary_payload(summary, live=False)
    loaded = LocalSessionController._summary_payload(summary, live=True)

    assert resumable["subtitle"] == "13 条记录"
    assert loaded["subtitle"] == "13 条记录"
    assert resumable["live"] is False
    assert loaded["live"] is True
    assert resumable["latest_root_turn"] is None
    assert loaded["latest_root_turn"] is None
    assert loaded["subagent_count"] == 7
