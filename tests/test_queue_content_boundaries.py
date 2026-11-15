"""Queue material boundaries preserve scheduling and independent entries."""

import pytest

from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    pending_work_dependencies,
    validate_work_inputs,
)
from pulsara_agent.identity import random_id


def instruction():
    return dict(kind="instruction", ordinal=0, content_start=0, content_count=1)


def work(kind, *, ready, start=1, count=0, ordinal=0):
    return dict(
        kind=kind,
        ordinal=ordinal,
        source_work_id=random_id("queue"),
        ready=ready,
        content_start=start,
        content_count=count,
    )


def test_empty_ready_material_and_unsatisfied_dependency_are_distinct():
    ready = work("dependency", ready=True)
    pending = work("dependency", ready=False, ordinal=1)
    values = [instruction(), ready, pending]
    assert validate_work_inputs(values) == tuple(values)
    assert pending_work_dependencies(values) == (pending,)


def test_kind_order_and_source_order_do_not_follow_content_arrival():
    dependency = work("dependency", ready=True, start=3, count=1)
    material = work("material", ready=True, start=2, count=1)
    context = dict(kind="context", ordinal=0, content_start=1, content_count=1)
    assert validate_work_inputs([material, dependency, context, instruction()]) == (
        instruction(),
        context,
        dependency,
        material,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"body": "another durable copy"},
        {"ready": "false"},
        {"ready": False, "content_count": 1},
        {"content_start": -1},
    ],
)
def test_invalid_material_metadata_is_rejected(change):
    dependency = work("dependency", ready=True)
    with pytest.raises(ValueError):
        validate_work_inputs([instruction(), dict(dependency, **change)])


def test_content_cannot_be_assigned_to_two_materials():
    with pytest.raises(ValueError, match="overlapping"):
        validate_work_inputs(
            [instruction(), work("material", ready=True, start=0, count=1)]
        )


def test_cell_provider_projection_preserves_original_stored_code_units():
    import json
    from pulsara_agent.ports.terminal_observation import CellOutputContent

    value = CellOutputContent(
        random_id("cell"),
        random_id("turn"),
        random_id("attempt"),
        1,
        {"kind": "log", "text": "汉字\n\ud800"},
    )
    assert value.storage_text == "汉字\n\ud800"
    assert value.content.parts[0].text == "汉字\n\\ud800"
    assert json.loads(value.body) == {
        "parts": [{"type": "text", "text": "汉字\n\\ud800"}]
    }
    assert value.resource_quote.canonical_expanded_bytes == len(value.body)
