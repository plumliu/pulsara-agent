"""Copyable terminal identities at the existing model projection boundaries."""

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.lowering import (
    lower_canonical_item,
)
from pulsara_agent.ports.terminal import (
    parse_manage_process_input,
)
from pulsara_agent.ports.terminal_observation import (
    CellOutputContent,
    cell_output_provider_text,
)
from pulsara_agent.conversation_kernel.host import (
    KernelHostSession,
    _ProcessControlObservation,
)
from pulsara_agent.conversation_kernel.user_control import ProcessControlResult


def ident(kind):
    return derive_id(kind, "test", "terminal-target-projection")


@pytest.mark.parametrize("kind", ("log", "return", "diagnostic", "tool_result"))
@pytest.mark.parametrize("text", ("ok", "\ud800", "\udfff", "汉\ud800🙂"))
def test_cell_original_projection_preserves_exact_stored_text(kind, text):
    record = (
        {"kind": kind, "text": text}
        if kind in {"log", "return"}
        else {"kind": "error", "error": {"message": text}}
        if kind == "diagnostic"
        else {"kind": "tool_result", "result": {"message": text}}
    )
    fact = CellOutputContent(
        cell_id=ident("cell"),
        source_turn_id=ident("turn"),
        source_exec_attempt_id=ident("attempt"),
        record_sequence=1,
        record=record,
    )
    expected = cell_output_provider_text(fact.storage_text)
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        ident("entry"),
        1,
        ident("turn"),
        fact.content.parts,
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT,
    )
    normal = lower_canonical_item(item).fixed_message
    assert normal is not None
    assert normal.content == (LLMTextPart(expected),)
    assert fact.record == record
    assert json.loads(fact.body) == {"parts": [{"type": "text", "text": expected}]}
    assert normal.content[0].text.encode("utf-8").decode("utf-8") == expected


def test_human_stop_observation_projects_a_copyable_process_target():
    host = KernelHostSession.__new__(KernelHostSession)
    host._lock = asyncio.Lock()
    host._closing = host._closed = False
    host._pending_workspace_recreated = None
    process_id = ident("process")
    result = ProcessControlResult(
        "TERMINATION_COMPLETED", "killed", -15, "TERMINAL", False
    )
    host._pending_process_controls = {
        process_id: _ProcessControlObservation(
            process_id, "ROOT", datetime.now(timezone.utc), result, None
        )
    }
    host.workspace = SimpleNamespace(workspace_root="/tmp")
    body = asyncio.run(host.take_user_actions())
    assert body is not None
    public = json.loads(body)["actions"][0]
    assert (
        parse_manage_process_input(
            {"action": "poll", "target": public["target"]}
        ).target.process_id
        == process_id
    )
    assert public["kind"] == "process_control"
    assert public["origin"] == "ROOT"
    assert public["status"] == "killed"
    assert public["exit_code"] == -15
    assert public["physical_state"] == "TERMINAL"
    assert asyncio.run(host.take_user_actions()) is None
