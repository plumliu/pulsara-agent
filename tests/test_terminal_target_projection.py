"""Copyable terminal identities at the existing model projection boundaries."""

import json

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
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
    parse_manage_process_input,
)
from pulsara_agent.ports.terminal_observation import (
    CellOutputContent,
)
from pulsara_agent.ports.user_control_feedback import (
    ProcessControlFeedbackContent,
    UserControlProcessFact,
    project_user_control_feedback_for_provider,
)


def ident(kind):
    return derive_id(kind, "test", "terminal-target-projection")


@pytest.mark.parametrize("kind", ("log", "error", "tool_result"))
def test_cell_normal_and_retained_paths_share_exact_typed_source(kind):
    fact = CellOutputContent(
        cell_id=ident("cell"),
        source_turn_id=ident("turn"),
        source_exec_attempt_id=ident("attempt"),
        record_sequence=1,
        record={"kind": kind, "text": "ok"},
    )
    body = fact.canonical_bytes()
    parts = (LLMTextPart(body.decode()),)
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        ident("entry"), 1, ident("turn"), parts,
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT,
    )
    normal = lower_canonical_item(
        item, artifact_read_available=False, limits=StructuredModelInputLimits(),
    ).fixed_message
    retained = FrozenRetainedHistoricalRequest(
        item.item_kind, CanonicalInputOriginKind.CELL_OUTPUT, FrozenPromptContent(parts),
    )
    assert normal is not None
    assert normal.content == lower_retained_request_content(retained)
    projected = json.loads(normal.content[0].text)["pulsara_terminal_observation"]
    assert projected == fact.canonical_mapping()
    assert projected["cell_id"] == fact.cell_id
    assert projected["source_exec_attempt_id"] == fact.source_exec_attempt_id
    assert projected["record"] == {"kind": kind, "text": "ok"}
    assert fact.canonical_bytes() == body


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
        public_code="BACKGROUND_CONTROL_COMPLETED",
        public_detail="Stopped.",
    )
    body = fact.canonical_bytes()
    public = json.loads(project_user_control_feedback_for_provider(body))[
        "pulsara_user_control_feedback"
    ]
    assert (
        parse_manage_process_input(
            {"action": "poll", "target": public["target"]}
        ).target.process_id
        == fact.process_id
    )
    assert public["process"]["status"] == "killed"
    assert public["process"]["exit_code"] == -15
    assert public["process"]["physical_state"] == "TERMINAL"
    assert "process_id" not in public
    assert fact.canonical_bytes() == body
    assert "monitor" not in json.loads(body) and "monitor_result" not in public
