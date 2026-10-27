"""Copyable terminal identities at the existing model projection boundaries."""

from dataclasses import replace
import json

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    FrozenRetainedHistoricalRequest,
    StructuredModelInputLimits,
)
from pulsara_agent.model_input.lowering import (
    lower_canonical_item,
    lower_retained_request_content,
)
from pulsara_agent.ports.terminal import (
    parse_terminal_monitor_input,
    parse_terminal_process_input,
)
from pulsara_agent.ports.terminal_observation import (
    TerminalDeliveryCoverage,
    TerminalObservationContentV1,
    TerminalObservationKind,
)
from pulsara_agent.ports.user_control_feedback import (
    ProcessControlFeedbackContent,
    UserControlMonitorFact,
    UserControlProcessFact,
    project_user_control_feedback_for_provider,
)


def ident(kind):
    return derive_id(kind, "test", "terminal-target-projection")


@pytest.mark.parametrize(
    "kind,status,code",
    [
        (TerminalObservationKind.PROGRESS, "running", None),
        (TerminalObservationKind.COMPLETION, "success", 0),
        (TerminalObservationKind.EXPIRY, "running", None),
    ],
)
def test_notification_normal_and_retained_paths_share_copyable_targets(
    kind, status, code
):
    fact = TerminalObservationContentV1(
        observation_id=ident("observation"),
        monitor_id=ident("monitor"),
        process_id=ident("process"),
        observation_ordinal=1,
        observation_kind=kind,
        process_status=status,
        exit_code=code,
        output_disposition="EXACT_DELTA",
        gap_before_output=False,
        delivery_coverage=TerminalDeliveryCoverage.COMPLETE,
        available_source_utf8_bytes=2,
        included_source_utf8_bytes=2,
        omitted_by_delivery_bound_utf8_bytes=0,
        output="ok",
    )
    body = fact.canonical_bytes()
    parts = (LLMTextPart(body.decode()),)
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        ident("entry"),
        1,
        ident("turn"),
        parts,
    )
    normal = lower_canonical_item(
        item,
        artifact_read_available=False,
        limits=StructuredModelInputLimits(),
    ).fixed_message
    retained = FrozenRetainedHistoricalRequest(
        item.item_kind,
        None,
        FrozenPromptContent(parts),
    )
    assert normal is not None
    assert normal.content == lower_retained_request_content(retained)
    projected = json.loads(normal.content[0].text)["pulsara_terminal_observation"]
    process = parse_terminal_process_input(
        {"action": "poll", "target": projected["process_target"]}
    )
    monitor = parse_terminal_monitor_input(
        {"action": "cancel", "target": projected["target"]}
    )
    assert process.target.process_id == fact.process_id
    assert monitor.target.monitor_id == fact.monitor_id
    assert projected["status"] == status and projected["output"] == "ok"
    assert ("exit_code" in projected) == (code is not None)
    assert "process_id" not in projected and "monitor_id" not in projected
    assert fact.canonical_bytes() == body
    assert json.loads(body)["process_id"] == fact.process_id


def test_human_stop_feedback_projects_targets_without_changing_native_control():
    fact = ProcessControlFeedbackContent(
        command_id=ident("command"),
        session_id=ident("session"),
        host_session_id=ident("host"),
        process_id=ident("process"),
        command="sleep 10",
        cwd="/tmp",
        origin_turn_id=ident("turn"),
        origin_subagent_task_id=None,
        target_root_turn_id=ident("turn"),
        process=UserControlProcessFact(
            "TERMINATION_COMPLETED", "killed", -15, "TERMINAL", False
        ),
        monitor=UserControlMonitorFact(ident("monitor"), "CANCELLED"),
        public_code="BACKGROUND_CONTROL_COMPLETED",
        public_detail="Stopped.",
    )
    body = fact.canonical_bytes()
    public = json.loads(project_user_control_feedback_for_provider(body))[
        "pulsara_user_control_feedback"
    ]
    assert (
        parse_terminal_process_input(
            {"action": "poll", "target": public["target"]}
        ).target.process_id
        == fact.process_id
    )
    assert (
        parse_terminal_monitor_input(
            {"action": "cancel", "target": public["monitor_result"]["target"]}
        ).target.monitor_id
        == fact.monitor.monitor_id
    )
    assert public["monitor_result"]["outcome"] == "CANCELLED"
    assert "process_id" not in public and "monitor_id" not in public["monitor_result"]
    assert fact.canonical_bytes() == body
    assert json.loads(body)["monitor"]["monitor_id"] == fact.monitor.monitor_id
    without_monitor = replace(fact, monitor=None)
    assert (
        json.loads(
            project_user_control_feedback_for_provider(
                without_monitor.canonical_bytes()
            )
        )["pulsara_user_control_feedback"]["monitor_result"]
        is None
    )
