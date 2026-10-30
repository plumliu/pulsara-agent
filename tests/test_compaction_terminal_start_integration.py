"""Terminal's independent atomic start enters the same first-call compaction."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import shlex
import sys

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from tests.test_compaction_request_start_integration import (
    _HUMAN, _Model, _eventually, _render, _rows, _seed_history,
)
from tests.test_round2_terminal_host import _TerminalMonitorDogfoodModel
from tests.test_round4_plan_host import _open_test_session


pytestmark = pytest.mark.postgres


class _TerminalCompactionModel(_Model):
    def __init__(self, command):
        super().__init__()
        self.command = command
        self.autonomous_seen = asyncio.Event()

    async def _stream(self, request):
        self.root_requests.append(request)
        async for event in _TerminalMonitorDogfoodModel._stream(self, request):
            yield event


def test_terminal_observation_starts_before_required_first_summary_and_keeps_input(
    tmp_path, monkeypatch, stage2_migrated_postgres_database,
):
    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PULSARA_TERMINAL_SHELL_SNAPSHOT", "0")
        release_path = tmp_path / "release-process"
        source = (
            "import time; from pathlib import Path; "
            "print('R2_INITIAL_SENTINEL', flush=True); "
            f"release=Path({str(release_path)!r});\n"
            "while not release.exists(): time.sleep(0.01)\n"
            "print('R2_COMPLETION_SENTINEL', flush=True)"
        )
        model = _TerminalCompactionModel(f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}")
        core, root = await _open_test_session(tmp_path=tmp_path, model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn, monkeypatch=monkeypatch)
        try:
            first = await root.run_turn(PromptContent.text("Run the process and register its completion monitor."),
                command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            assert first.final_text == "MONITOR_REGISTERED_WAITING"
            assert len(model.root_requests) == 3
            _seed_history(root)
            usage = await root.read_context_usage()
            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
            root._compaction.policy = replace(root._compaction.policy,
                automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85, minimum_reclaim_tokens=1)
            release_path.touch()
            await asyncio.wait_for(model.summary.started.wait(), 20)
            active = root.active_root_turn_id()
            assert active is not None and active != first.turn_id
            turns = _rows(root, "SELECT id,status,initial_entry_id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'", (root.session_id,))
            assert len(turns) == 1 and turns[0]["id"] == active
            initial = _rows(root, "SELECT id,entry_kind FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s ORDER BY entry_sequence", (root.session_id, active))
            assert initial == [{"id": turns[0]["initial_entry_id"], "entry_kind": "TERMINAL_OBSERVATION"}]
            revisions = _rows(root, "SELECT revision_ordinal FROM pulsara_v3.turn_context_binding_revisions WHERE session_id=%s AND turn_id=%s", (root.session_id, active))
            assert [r["revision_ordinal"] for r in revisions] == [0]
            assert len(model.root_requests) == 3
            assert "R2_COMPLETION_SENTINEL" not in _render(model.summary.contexts[0].messages)
            accepted = await root.steer_active_turn(command_id=random_id("command"),
                target_turn_id=active, content=PromptContent.text(_HUMAN))
            assert accepted.status == "PENDING"
            model.summary.release.set()
            await asyncio.wait_for(model.autonomous_seen.wait(), 20)

            async def terminal():
                row = _rows(root, "SELECT status FROM pulsara_v3.turns WHERE session_id=%s AND id=%s", (root.session_id, active))[0]
                return row["status"] == "COMPLETED"
            await _eventually(terminal)
            rendered = _render(model.root_requests[3].compiled_input.messages)
            assert rendered.index("R2_COMPLETION_SENTINEL") < rendered.index(_HUMAN)
            assert len(model.summary.calls) == 1 and len(model.root_requests) == 4
        finally:
            release_path.touch()
            model.summary.release.set()
            await core.shutdown()
    asyncio.run(scenario())
