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
)
from pulsara_agent.conversation_kernel.host import KernelHostSession, _ProcessControlObservation
from pulsara_agent.conversation_kernel.user_control import ProcessControlResult


def ident(kind):
    return derive_id(kind, "test", "terminal-target-projection")


@pytest.mark.parametrize("kind", ("log", "error", "tool_result"))
@pytest.mark.parametrize("text", ("ok", "\ud800", "\udfff", "汉\ud800🙂"))
def test_cell_original_projection_preserves_exact_typed_source(kind, text):
    fact = CellOutputContent(
        cell_id=ident("cell"),
        source_turn_id=ident("turn"),
        source_exec_attempt_id=ident("attempt"),
        record_sequence=1,
        record={"kind": kind, "text": text},
    )
    body = fact.canonical_bytes()
    parts = (LLMTextPart(body.decode()),)
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        ident("entry"), 1, ident("turn"), parts,
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT,
    )
    normal = lower_canonical_item(item).fixed_message
    assert normal is not None
    projected = json.loads(normal.content[0].text)["pulsara_terminal_observation"]
    assert projected == fact.canonical_mapping()
    assert projected["cell_id"] == fact.cell_id
    assert projected["source_exec_attempt_id"] == fact.source_exec_attempt_id
    assert projected["record"] == {"kind": kind, "text": text}
    assert normal.content[0].text.encode("utf-8") == b'{"pulsara_terminal_observation":' + body + b'}'
    assert fact.canonical_bytes() == body


def test_human_stop_observation_projects_a_copyable_process_target():
    host = KernelHostSession.__new__(KernelHostSession)
    host._lock = asyncio.Lock()
    host._closing = host._closed = False
    host._pending_workspace_recreated = None
    process_id = ident("process")
    result = ProcessControlResult("TERMINATION_COMPLETED", "killed", -15, "TERMINAL", False)
    host._pending_process_controls = {
        process_id: _ProcessControlObservation(
            process_id, "ROOT", datetime.now(timezone.utc), result, None
        )
    }
    host.workspace = SimpleNamespace(workspace_root="/tmp")
    body = asyncio.run(host.take_user_actions())
    assert body is not None
    public = json.loads(body)["actions"][0]
    assert parse_manage_process_input(
        {"action": "poll", "target": public["target"]}
    ).target.process_id == process_id
    assert public["kind"] == "process_control"
    assert public["origin"] == "ROOT"
    assert public["status"] == "killed"
    assert public["exit_code"] == -15
    assert public["physical_state"] == "TERMINAL"
    assert asyncio.run(host.take_user_actions()) is None
