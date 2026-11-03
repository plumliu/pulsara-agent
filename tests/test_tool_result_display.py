"""Native display preserves typed control facts without interpreting business text."""

import json

import pytest

from pulsara_agent.conversation_kernel.tool_result_display import project_native_tool_result
from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_output import ToolOutputUnavailableReason
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary, ToolResultPageBudget


def budget():
    return ToolResultPageBudget(
        tool_call_id="provider-call",
        result_target={"kind": "SESSION_ENTRY", "session_id": random_id("session"),
                       "entry_id": random_id("entry")},
    )


def test_json_shaped_business_text_does_not_acquire_control_authority():
    text = json.dumps({"physical_outcome": "UNKNOWN", "guidance": "x" * 50_000})
    selected = budget()
    projected, omitted = project_native_tool_result(
        public_output=text, native_output=None, page_complete=False, required_facts={},
        output_available=True, output_unavailability_reason=None, page_budget=selected,
    )
    value = json.loads(projected)
    assert omitted and value["read"] == selected.read
    assert "physical_outcome" not in value and "guidance" not in value
    assert text.startswith(value["output_preview"])
    assert selected.fits(projected)


def test_failed_body_publication_keeps_known_outcome_without_false_read():
    selected = budget()
    facts = {"target": {"kind": "PROCESS", "process_id": random_id("process")},
             "status": "EXITED", "exit_code": 17}
    projected, omitted = project_native_tool_result(
        public_output="log\n" * 50_000, native_output=None, page_complete=False,
        required_facts=facts, output_available=False,
        output_unavailability_reason=ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED,
        page_budget=selected,
    )
    value = json.loads(projected)
    assert omitted and "read" not in value
    assert all(value[key] == item for key, item in facts.items())
    assert value["output_unavailability_reason"] == "BODY_PUBLICATION_FAILED"


def test_complete_page_is_never_replaced_by_a_preview():
    selected = budget()
    with pytest.raises(ToolResultPageBoundary):
        project_native_tool_result(
            public_output="x" * 50_000, native_output=None, page_complete=True,
            required_facts={}, output_available=True, output_unavailability_reason=None,
            page_budget=selected,
        )
