"""Terminal action budget admission and native result rendering."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Mapping
from pulsara_agent.message import ToolResultState
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    ToolResultPageBoundary,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolExecutionResult,
    ToolOutputSourceFormatHint,
)
from pulsara_agent.terminal_process import TerminalResult
from pulsara_agent.primitives.tool_observation import (
    TrustedToolObservationSupplement,
    normalize_observation_duration,
)


class TerminalEffectAdmissionRejected(ToolResultPageBoundary):
    """The owned process action was rejected before its physical effect."""


def _terminal_process_payload(native: Mapping[str, object]) -> dict[str, object]:
    payload = dict(native)
    payload["target"] = {"kind": "PROCESS", "process_id": payload.pop("process_id")}
    # Metadata was not an output observation and must not advance a log reader.
    payload.pop("output_cursor", None)
    payload.pop("retained_from_cursor", None)
    return payload


def _terminal_result_facts(
    call: ToolCall,
    result: TerminalResult,
    *,
    action: str = "start",
) -> dict[str, object]:
    control_only = action in {"close_stdin", "kill"}
    candidate = None if control_only else result.output_body_candidate
    payload = {
        "status": result.status.value,
        "terminal_process_action": action,
        "exit_code": result.exit_code,
        "cwd": result.cwd,
        "timed_out": result.timed_out,
        "error": result.error if action == "start" else None,
        "reason": None if result.reason is None else result.reason.value,
        "yielded_to_background": result.status.value == "running",
    }
    if result.process_id is not None:
        payload["target"] = {"kind": "PROCESS", "process_id": result.process_id}
    if not control_only:
        payload.update(
            output_disposition=result.output_disposition.value,
            output_cursor=result.output_cursor,
            retained_from_cursor=result.retained_from_cursor,
            gap_before_output=result.gap_before_output,
            source_coverage=(
                candidate.source_coverage.value if candidate is not None
                else result.source_coverage.value
            ),
        )
        if candidate is not None and candidate.source_coverage_reason is not None:
            payload["source_coverage_reason"] = candidate.source_coverage_reason.value
    if result.status.value == "running" and result.process_id is not None:
        observe_arguments = {
            "action": "wait",
            "target": payload["target"],
            "timeout_seconds": 30,
        }
        if not control_only and result.output_cursor is not None:
            observe_arguments["since_cursor"] = result.output_cursor
        if "max_output_chars" in call.arguments:
            observe_arguments["max_output_chars"] = call.arguments["max_output_chars"]
        payload["observe"] = {"tool": "manage_process", "arguments": observe_arguments}
    return payload


def _admit_terminal_effect(call: ToolCall, result: TerminalResult, *, action: str) -> None:
    """Quote known owned identities before stdin, signals, or process spawn."""
    from pulsara_agent.terminal_process.models import TerminalStatus
    from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverageReason

    budget = call.page_budget or ToolResultPageBudget(call.id)
    control_only = action in {"close_stdin", "kill"}
    # exit_code is sint32 in the existing terminal wire protocol. This is a
    # quote of its scalar width, never a synthesized process identity/outcome.
    # Cursors have no protocol upper bound: retain the actual owned positions.
    for status in (TerminalStatus.RUNNING, TerminalStatus.SUCCESS, TerminalStatus.ERROR,
                   TerminalStatus.TIMEOUT, TerminalStatus.KILLED):
        for timed_out in (False, True):
            facts = _terminal_result_facts(call, replace(
                result, status=status, exit_code=-(1 << 31), timed_out=timed_out,
                error=result.error or status.value,
            ), action=action)
            if not control_only:
                facts.update(
                    truncated=True, truncated_by_response_bound=True, output_preview="",
                    source_coverage="RETAINED_SNAPSHOT",
                    source_coverage_reason=max(
                        ToolOutputSourceCoverageReason, key=lambda reason: len(reason.value)
                    ).value,
                )
            body = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
            if not budget.fits(body, include_read=not control_only and budget.read is not None):
                raise TerminalEffectAdmissionRejected(
                    "Terminal minimum receipt cannot fit before the process action"
                )


def _terminal_execution_result(
    call: ToolCall,
    result: TerminalResult,
    *,
    action: str = "start",
) -> ToolExecutionResult:
    budget = call.page_budget or ToolResultPageBudget(call.id)
    control_only = action in {"close_stdin", "kill"}
    candidate = None if control_only else result.output_body_candidate
    payload = _terminal_result_facts(call, result, action=action)
    required_facts = dict(payload)

    def encoded(value: Mapping[str, object]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    if control_only:
        public_output = native_output = encoded(payload)
        if not budget.fits(native_output):
            if budget.accepts_provider_body is not None:
                # This exact action already returned. Preserve its real result;
                # final dispatch must admit/compact P, never rerun the action.
                return _terminal_execution_result(replace(call, page_budget=replace(
                    budget, accepts_provider_body=None
                )), result, action=action)
            raise ToolResultPageBoundary("Terminal control facts exceed the result budget")
    else:
        complete_text = candidate.text if candidate is not None else result.output
        payload.update(
            output=complete_text,
            truncated=result.gap_before_output,
            truncated_by_response_bound=False,
        )
        public_output = encoded(payload)
        if len(result.output) == len(complete_text) and budget.fits(public_output):
            native_output = public_output
        else:
            preview_facts = {
                **required_facts,
                "truncated": True,
                "truncated_by_response_bound": True,
            }
            maximum = len(result.output)
            low, high = 0, maximum
            winner = None
            while low <= high:
                take = (low + high) // 2
                preview = encoded({
                    **preview_facts,
                    "output_preview": result.output[-take:] if take else "",
                })
                if budget.fits(preview, include_read=budget.read is not None):
                    winner = preview
                    low = take + 1
                else:
                    high = take - 1
            if winner is None:
                if budget.accepts_provider_body is not None:
                    return _terminal_execution_result(replace(call, page_budget=replace(
                        budget, accepts_provider_body=None
                    )), result, action=action)
                raise ToolResultPageBoundary("Terminal observation facts exceed the result budget")
            native_output = winner
    state = (
        ToolResultState.SUCCESS
        if action != "start" or result.status.value in {"success", "running"}
        else ToolResultState.INTERRUPTED
        if result.status.value == "killed"
        else ToolResultState.ERROR
    )
    trusted_duration = (
        normalize_observation_duration(result.trusted_process_duration_microseconds)
        if action in {"start", "poll", "wait"}
        else None
    )
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=state,
        output=public_output,
        public_format=ToolOutputSourceFormatHint.JSON,
        output_body_candidate=candidate,
        native_output=native_output,
        page_complete=control_only,
        required_facts=required_facts,
        trusted_observation=(
            TrustedToolObservationSupplement(trusted_duration)
            if trusted_duration is not None
            else None
        ),
    )


def _success(
    call: ToolCall,
    payload: Mapping[str, object],
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultState.SUCCESS,
        output=json.dumps(dict(payload), ensure_ascii=False),
        public_format=ToolOutputSourceFormatHint.JSON,
    )
