"""Current native contracts preserve complete values and real local effects."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.current_work_runtime import CurrentWorkStateOwner, build_root_activation
from pulsara_agent.conversation_kernel.plan_files import publish_plan_markdown
from pulsara_agent.conversation_kernel.presentation_targets import ResolvedPresentationObject
from pulsara_agent.conversation_kernel.tool_runtime_management import _ToolManagementOperations
from pulsara_agent.conversation_kernel.tool_outputs import assemble_tool_business_value
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import ToolCall, ToolBusinessValue
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.plan_workflow import (
    validate_plan_entry_arguments, parse_plan_question_arguments, parse_plan_exit_arguments,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.scheduling.contracts import (
    ScheduledTaskError, normalize_schedule_input, public_schedule_rule, next_occurrence,
)
from pulsara_agent.scheduling.requests import validate_query, validate_action
from pulsara_agent.tools.builtins.presentation import OpenInPulsaraTool
from pulsara_agent.tools.builtins.todo import parse_todo_replacement, TodoValidationError


def test_todo_ten_long_lines_keep_types_counts_and_commit_only_after_saved_result():
    async def run():
        session, turn = random_id("session"), random_id("turn")
        owner = CurrentWorkStateOwner(session_id=session, owner_epoch=random_id("host"))
        owner.activate_root_run(build_root_activation(session_id=session, admission_kind="DIRECT",
            command_id=random_id("command"), exact_turn_id=turn, exact_initial_entry_id=random_id("entry"),
            exact_context_binding_revision_id=random_id("binding")))
        port = SimpleNamespace(_current_work_owner=owner, _todo_settlements={})
        context = SimpleNamespace(result_entry_id=random_id("entry"), surface_borrow=SimpleNamespace(
            prepared=SimpleNamespace(access=SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT))))
        items = [{"text": str(index) + "长" * 5000, "status": "in_progress" if index == 0 else "pending"} for index in range(10)]
        result = await _ToolManagementOperations._invoke_todo_replacement(port, arguments={"items": items},
            turn_id=turn, attempt_id=random_id("attempt"), invocation_context=context,
            invocation_started=monotonic(), observation_origin=ToolObservationOrigin.BUILTIN)
        assert isinstance(result.content, ToolBusinessValue)
        public = assemble_tool_business_value(public_fields=result.content.public_fields,
            content_parts=result.content.content, result_state=result.state)
        assert public == {"meta": {"status": "SUCCESS", "counts": {"pending": 9, "in_progress": 1, "completed": 0, "total": 10}},
                          "content": [{"type": "text", "value": "已更新进度清单。"}]}
        assert owner.snapshot(scope_kind=ModelInputScopeKind.ROOT).ordered_items == ()
        installed = owner.commit(result.process_local_settlement.prepared).installed_snapshot
        assert [item.text for item in installed.ordered_items] == [item["text"] for item in items]
        with pytest.raises(TodoValidationError):
            parse_todo_replacement({"items": items + [{"text": "eleven", "status": "pending"}]})
    asyncio.run(run())


def test_open_partial_failure_keeps_actual_targets_private_and_processes_all_items(tmp_path):
    async def run():
        root, agent = random_id("session"), random_id("session")
        tool = OpenInPulsaraTool(tmp_path, root_session_id=root)
        async def resolve(kind, identity):
            return ResolvedPresentationObject(kind, identity, root)
        result = await tool.execute_async(ToolCall("external-call", "open_in_pulsara", {"targets": [
            {"kind": "PATH", "path": "missing.md"}, {"kind": "URL", "url": "https://name:actual-secret@example.test"},
            {"kind": "AGENT", "agent_id": agent}, {"kind": "PROCESS", "process_id": random_id("process")},
        ]}), resolve_object=resolve)
        assert result.status is ToolResultState.ERROR
        public = assemble_tool_business_value(public_fields=result.business_value.public_fields,
            content_parts=result.business_value.content, result_state="APPLICATION_ERROR")
        assert set(public) == {"meta", "content"} and public["meta"] == {"status": "ERROR"}
        text = public["content"][0]["value"]
        assert "targets[0]、targets[2]" in text and "targets[1]" in text and "targets[3]" in text
        assert "actual-secret" not in text and "name:" not in text
        actual = result.result_facts["presentation_targets"]
        assert actual[0]["target"] == {"kind": "PATH", "path": str(tmp_path / "missing.md")}
        assert actual[1] == {"target": {"kind": "AGENT", "agent_id": agent}, "root_session_id": root}
    asyncio.run(run())


def test_plan_inputs_have_one_current_shape_and_preserve_full_plan_text():
    validate_plan_entry_arguments(arguments=freeze_json({}))
    with pytest.raises(ValueError):
        validate_plan_entry_arguments(arguments=freeze_json({"reason": "removed"}))
    question, options = parse_plan_question_arguments(arguments=freeze_json({"question": "怎么做？"}))
    assert question == "怎么做？" and options == ()
    with pytest.raises(ValueError):
        parse_plan_question_arguments(arguments=freeze_json({"question": "问题", "allow_free_text": True}))
    text = "# 方案\r\n\n末尾\r"
    assert parse_plan_exit_arguments(arguments=freeze_json({"plan": text, "summary": "标题"})) == (text.encode(), "标题")
    assert parse_plan_exit_arguments(arguments=freeze_json({"plan": "  \n", "summary": "unused"}))[0] is None
    with pytest.raises(ValueError):
        parse_plan_exit_arguments(arguments=freeze_json({"plan": " " * ((1 << 20) + 1)}))


def test_markdown_fixed_directory_no_clobber_and_exact_bytes(tmp_path):
    body = "# 原文\r\n\n末尾\r".encode()
    first, error = publish_plan_markdown(tmp_path, plan=body, summary=None, deadline_monotonic=monotonic()+10)
    second, error2 = publish_plan_markdown(tmp_path, plan=body, summary="  ", deadline_monotonic=monotonic()+10)
    third, error3 = publish_plan_markdown(tmp_path, plan=body, summary="../标题/说明", deadline_monotonic=monotonic()+10)
    assert error is error2 is error3 is None
    assert first == ".pulsara/plans/方案.md" and second == ".pulsara/plans/方案 (2).md"
    assert Path(third).parent == Path(".pulsara/plans")
    assert (tmp_path / first).read_bytes() == (tmp_path / second).read_bytes() == (tmp_path / third).read_bytes() == body


def test_schedule_creation_defaults_once_and_calendar_edges_are_canonical():
    now = datetime(2026, 11, 16, 12, 34, 56, 999, tzinfo=timezone.utc)
    interval = normalize_schedule_input({"kind": "interval", "seconds": 900}, "Asia/Shanghai", now, creating=True)
    assert interval == {"kind": "interval", "seconds": 900, "anchor_at_utc": "2026-11-16T12:34:56+00:00"}
    assert next_occurrence(interval, "Asia/Shanghai", now) == datetime(2026, 11, 16, 12, 49, 56, tzinfo=timezone.utc)
    daily = normalize_schedule_input({"kind": "daily", "time": "09:00"}, "Asia/Shanghai", now, creating=True)
    assert daily["start_date"] == "2026-11-16"
    monthly = normalize_schedule_input({"kind": "monthly", "time": "09:00", "day": 31}, "Asia/Shanghai", now, creating=True)
    assert next_occurrence(monthly, "Asia/Shanghai", datetime(2027, 1, 31, 2, tzinfo=timezone.utc)) == datetime(2027, 3, 31, 1, tzinfo=timezone.utc)
    once = normalize_schedule_input({"kind": "once", "run_at": "2027-11-07T01:30:00"}, "America/New_York", now, creating=True)
    assert public_schedule_rule(once, "America/New_York") == {"kind": "once", "run_at": "2027-11-07T01:30:00"}
    assert normalize_schedule_input(public_schedule_rule(once, "America/New_York"), "America/New_York",
        datetime(2028, 1, 1, tzinfo=timezone.utc), creating=False, previous_rule=once,
        previous_timezone="America/New_York") == once
    with pytest.raises(ScheduledTaskError):
        normalize_schedule_input({"kind": "once", "run_at": "2027-03-14T02:30:00"}, "America/New_York", now, creating=True)
    for seconds in [True, 1, 899, 900.5]:
        with pytest.raises(ScheduledTaskError):
            normalize_schedule_input({"kind": "interval", "seconds": seconds}, "Asia/Shanghai", now, creating=True)
    with pytest.raises(ScheduledTaskError):
        normalize_schedule_input({"kind": "interval", "seconds": 1800}, "Asia/Shanghai", now, creating=False)


def test_schedule_native_scope_and_concurrency_fields_are_closed():
    assert validate_query({"status": "ALL"}) == {"status": "ALL"}
    assert validate_query({}) == {}
    for value in [{"session": {"kind": "SESSION", "session_id": random_id("session")}}, {"cursor": random_id("schedule")}, {"status": None}]:
        with pytest.raises(ScheduledTaskError):
            validate_query(value)
    for value in [{"action": "pause", "target": {"kind": "SCHEDULE", "task_id": random_id("schedule")}, "expected_revision": 1},
                  {"action": "create", "values": {"name": "n", "prompt": "p", "permission_mode": "read-only", "schedule": {"kind": "interval", "seconds": 900}}}]:
        with pytest.raises(ScheduledTaskError):
            validate_action(value)
