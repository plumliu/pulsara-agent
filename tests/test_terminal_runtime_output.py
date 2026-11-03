"""Full JS values, one budgeted terminal tail, and exact follow-up actions."""

from __future__ import annotations

from dataclasses import replace
import json

from pulsara_agent.conversation_kernel.tool_runtime import (
    _terminal_execution_result,
    _terminal_process_payload,
)
from pulsara_agent.identity import derive_id
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolOutputSourceCoverage,
    ToolOutputSourceFormatHint,
)
from pulsara_agent.ports.tool_output import ToolOutputBodyCandidate
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    with_output_read,
)
from pulsara_agent.terminal_process.models import TerminalResult, TerminalStatus


PROCESS = derive_id("process", "terminal-runtime-output", "process")
RESULT_TARGET = {
    "kind": "SESSION_ENTRY",
    "session_id": derive_id("session", "terminal-runtime-output", "session"),
    "entry_id": derive_id("entry", "terminal-runtime-output", "result"),
}


def _result(text: str, maximum=512) -> TerminalResult:
    return TerminalResult(
        status=TerminalStatus.RUNNING,
        output=text[-maximum:],
        exit_code=-1,
        cwd="/workspace",
        process_id=PROCESS,
        output_cursor="through-cursor",
        retained_from_cursor="retained-cursor",
        output_body_candidate=ToolOutputBodyCandidate(
            text=text,
            source_format=ToolOutputSourceFormatHint.TEXT,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
        ),
        truncated_by_response_bound=len(text) > maximum,
    )


def test_small_body_honors_tail_request_and_js_keeps_complete_selected_text():
    text = "old\n" + "middle\n" * 700 + "end\n"
    call = ToolCall("call:terminal-view", "exec_process", {
        "command": "fixture", "max_output_chars": 512,
    }, page_budget=ToolResultPageBudget("call:terminal-view", RESULT_TARGET))
    result = _terminal_execution_result(call, _result(text))
    value, native = json.loads(result.output), json.loads(result.native_output)

    assert value["output"] == result.output_body_candidate.text == text
    assert value["truncated_by_response_bound"] is False
    assert native["output_preview"] == text[-512:]
    assert native["truncated_by_response_bound"] is True
    assert "output" not in native
    assert "read" not in native  # Added once after body storage establishes availability.
    assert value["observe"] == {
        "tool": "manage_process",
        "arguments": {
            "action": "wait", "target": {"kind": "PROCESS", "process_id": PROCESS},
            "since_cursor": "through-cursor", "timeout_seconds": 30,
            "max_output_chars": 512,
        },
    }
    assert result.required_facts["observe"] == value["observe"]


def test_tail_shrinks_against_exact_budget_including_saved_read():
    text = '中文🙂\\"\n' * 2_000
    budget = ToolResultPageBudget("call:terminal-budget", RESULT_TARGET)
    call = ToolCall("call:terminal-budget", "exec_process", {
        "command": "fixture", "max_output_chars": 512,
    }, page_budget=budget)
    template = json.loads(_terminal_execution_result(call, _result(text)).native_output)
    template["output_preview"] = ""
    baseline = with_output_read(json.dumps(template, ensure_ascii=False), budget.read)
    allowance = len(baseline.encode()) + 200
    constrained = ToolResultPageBudget(
        call.id, RESULT_TARGET,
        accepts_provider_body=lambda body, _ids: len(body.encode()) <= allowance,
    )

    result = _terminal_execution_result(replace(call, page_budget=constrained), _result(text))
    preview = json.loads(result.native_output)["output_preview"]
    assert 0 < len(preview) < 512
    assert text.endswith(preview)
    assert constrained.fits(result.native_output, include_read=True)
    assert json.loads(result.output)["output"] == text
    assert result.required_facts["output_cursor"] == "through-cursor"


def test_complete_observation_retains_output_without_preview_or_read():
    call = ToolCall("call:terminal-short", "exec_process", {"command": "fixture"})
    result = _terminal_execution_result(call, _result("ok\n"))
    assert result.native_output == result.output
    native = json.loads(result.native_output)
    assert native["output"] == "ok\n"
    assert "output_preview" not in native and "read" not in native


def test_control_result_has_no_log_fields_or_unobserved_cursor():
    call = ToolCall("call:terminal-control", "manage_process", {
        "action": "close_stdin", "target": {"kind": "PROCESS", "process_id": PROCESS},
    })
    result = _terminal_execution_result(call, _result("prior output\n"), action="close_stdin")
    value = json.loads(result.output)
    assert result.page_complete is True
    assert result.output_body_candidate is None
    assert result.native_output == result.output
    assert not ({"output", "output_preview", "output_cursor", "retained_from_cursor",
                 "truncated", "truncated_by_response_bound", "source_coverage"} & value.keys())
    assert "since_cursor" not in value["observe"]["arguments"]
    assert value["status"] == "running"
    assert value["target"] == {"kind": "PROCESS", "process_id": PROCESS}


def test_process_metadata_does_not_advance_a_log_reader():
    metadata = _terminal_process_payload({
        "process_id": PROCESS, "status": "running", "command": "fixture",
        "output_cursor": "through-cursor", "retained_from_cursor": "retained-cursor",
    })
    assert "output_cursor" not in metadata and "retained_from_cursor" not in metadata
    assert metadata["command"] == "fixture"
    assert metadata["target"] == {"kind": "PROCESS", "process_id": PROCESS}
