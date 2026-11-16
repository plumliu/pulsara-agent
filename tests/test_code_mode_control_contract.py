"""Native control reads full source and observes existing Cell lifecycle facts."""

import asyncio
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock

from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.tools.builtins.filesystem import ReadFileTool
from pulsara_agent.identity import random_id


def port(owner, **extra):
    value = SimpleNamespace(_code_mode=owner, **extra)
    value._query_cells = MethodType(DirectKernelToolPort._query_cells, value)
    value._invoke_code_mode_control = MethodType(
        DirectKernelToolPort._invoke_code_mode_control, value
    )
    return value


def test_exec_cell_freezes_complete_path_source_before_accepting_cell(tmp_path):
    source = "/*" + "x" * (6 << 20) + "*/ return 1;"
    (tmp_path / "program.js").write_text(source)
    cell = random_id("cell")
    attempt = random_id("attempt")
    turn = random_id("turn")
    owner = SimpleNamespace(start=AsyncMock(return_value=cell))
    freeze = AsyncMock()
    snapshot = SimpleNamespace(
        decision_available=False, tool_directory=SimpleNamespace(tool_specs=())
    )
    surface = port(
        owner,
        _tools={"read_file": ReadFileTool(tmp_path)},
        _code_mode_invoke=AsyncMock(),
        _code_mode_freeze_source=freeze,
    )
    result = asyncio.run(
        surface._invoke_code_mode_control(
            "exec_cell",
            {"target": {"kind": "PATH", "path": "program.js"}},
            SimpleNamespace(
                code_mode_snapshot=snapshot,
                code_cell_id=None,
                attempt_id=attempt,
                turn_id=turn,
            ),
        )
    )
    assert result.public_value() == {
        "target": {"kind": "CELL", "cell_id": cell},
        "meta": {"status": "SUCCESS"},
        "content": [{"type": "text", "value": "已接纳脚本执行。"}],
    }
    assert freeze.await_args.kwargs == {"attempt_id": attempt, "source": source}
    assert owner.start.await_args.kwargs["source"] == source
    assert owner.start.await_args.kwargs["snapshot"] is snapshot
    (tmp_path / "program.js").write_text("return 2;")
    assert owner.start.await_args.kwargs["source"] == source


def test_query_cells_has_complete_scope_and_exact_frozen_source():
    targets = [{"kind": "CELL", "cell_id": random_id("cell")} for _ in range(71)]
    rows = [
        {"target": t, "state": "running", "pending_calls": i}
        for i, t in enumerate(targets)
    ]

    def listing(*, max_items):
        assert max_items is None
        return {"cells": rows}

    source = "x" * (6 << 20)
    capture = SimpleNamespace(
        state="failed",
        pending_calls=2,
        source_exec_attempt_id=random_id("attempt"),
        error={"message": "actual failure"},
    )
    surface = port(
        SimpleNamespace(list_cell_snapshots=listing),
        capture_code_cell=lambda _: capture,
        _code_mode_read_source=AsyncMock(return_value=source),
    )
    context = SimpleNamespace(session_id=random_id("session"))
    result = asyncio.run(
        surface._invoke_code_mode_control("query_cells", {}, context)
    ).public_value()
    assert [p["value"] for p in result["content"]] == rows
    result = asyncio.run(
        surface._invoke_code_mode_control(
            "query_cells", {"target": targets[0]}, context
        )
    ).public_value()
    assert result["meta"] == {
        "status": "SUCCESS",
        "cell_status": "failed",
        "pending_calls": 2,
    }
    assert result["content"] == [
        {"type": "text", "value": "执行诊断：actual failure\n\n执行源码：\n"},
        {"type": "text", "value": source},
    ]
    assert surface._code_mode_read_source.await_args.kwargs == {
        "session_id": context.session_id,
        "attempt_id": capture.source_exec_attempt_id,
    }


def test_manage_cell_reports_actual_terminal_state_and_unsettled_effect_count():
    target = {"kind": "CELL", "cell_id": random_id("cell")}
    capture = SimpleNamespace(error=None)
    owner = SimpleNamespace(
        capture_cell=lambda _: capture,
        stop=AsyncMock(return_value={"state": "terminated", "pending_calls": 2}),
        wait_state=AsyncMock(
            return_value={
                "state": "failed",
                "pending_calls": 1,
                "error": {"message": "actual failure"},
            }
        ),
    )
    surface = port(owner)
    result = asyncio.run(
        surface._invoke_code_mode_control(
            "manage_cell", {"target": target, "action": "stop"}, None
        )
    ).public_value()
    assert result["meta"] == {
        "status": "SUCCESS",
        "cell_status": "terminated",
        "pending_calls": 2,
    }
    assert result["content"] == [{"type": "text", "value": "已停止脚本执行。"}]
    result = asyncio.run(
        surface._invoke_code_mode_control(
            "manage_cell",
            {"target": target, "action": "wait", "yield_time_ms": 0},
            None,
        )
    ).public_value()
    assert result["meta"] == {
        "status": "SUCCESS",
        "cell_status": "failed",
        "pending_calls": 1,
    }
    assert result["content"] == [{"type": "text", "value": "actual failure"}]
    owner.wait_state.assert_awaited_once_with(target["cell_id"], 0)
    owner.stop.return_value = {"state": "completed", "pending_calls": 0}
    result = asyncio.run(
        surface._invoke_code_mode_control(
            "manage_cell", {"target": target, "action": "stop"}, None
        )
    ).public_value()
    assert result["content"] == [{"type": "text", "value": "脚本已结束。"}]
