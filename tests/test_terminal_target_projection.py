"""Copyable terminal identities at the existing model projection boundaries."""

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.identity import derive_id
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
    from pulsara_agent.code_mode.output import project_output_record

    raw = (
        {"kind": kind, "values": [{"type": "text", "value": text}]}
        if kind in {"log", "return"}
        else (
            {"kind": "error", "error": {"message": text}}
            if kind == "diagnostic"
            else {
                "kind": "tool_result",
                "tool_name": "sample",
                "value": {"message": text},
            }
        )
    )
    output_kind, parts = project_output_record(raw)
    record = {"kind": output_kind, "content": list(parts)}
    fact = CellOutputContent(
        cell_id=ident("cell"),
        source_turn_id=ident("turn"),
        source_exec_attempt_id=ident("attempt"),
        record_sequence=1,
        record=record,
    )
    expected = "".join(part.text for part in fact.content.parts)
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        ident("entry"),
        1,
        ident("turn"),
        fact.content.parts,
        input_origin=CanonicalInputOriginKind.CELL_OUTPUT,
        cell_output_source=fact.source,
    )
    normal = lower_canonical_item(item).message
    assert normal is not None
    assert normal.content[1:] == fact.content.parts
    assert fact.cell_id in normal.content[0].text
    assert kind in normal.content[0].text
    from pulsara_agent.ports.tool_execution import thaw_tool_json_object

    assert thaw_tool_json_object(fact.record) == record
    assert json.loads(fact.body) == {"parts": [{"type": "text", "text": expected}]}
    assert (
        "".join(part.text for part in normal.content[1:])
        .encode("utf-8")
        .decode("utf-8")
        == expected
    )


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
    from pulsara_agent.primitives.context import thaw_json

    assert len(body) == 1
    public = thaw_json(body[0].target)
    assert (
        parse_manage_process_input(
            {"action": "wait", "target": public}
        ).target.process_id
        == process_id
    )
    assert body[0].text == "用户已停止后台进程。"
    assert asyncio.run(host.take_user_actions()) is None
