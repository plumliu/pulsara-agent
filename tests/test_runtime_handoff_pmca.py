"""The handoff is a bounded PMCA observation with independent complete TODO."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    CompactionRuntimeHandoffBoundError,
    CompactionRuntimeHandoffBuilder,
    FrozenTerminalMonitorHandoffFact,
    FrozenTerminalProcessHandoffFact,
    HANDOFF_BUDGET_OMISSION,
    HANDOFF_CONTINUATION,
    MAXIMUM_RUNTIME_HANDOFF_PMCA_TOKENS,
    freeze_compaction_runtime_handoff,
)
from pulsara_agent.conversation_kernel.todo_runtime import (
    FrozenTodoSnapshot,
    TodoRunIdentity,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.llm.adapters.openai.chat_completions import chat_semantic_wire_group
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3
from pulsara_agent.model_input.compiler import estimate_runtime_handoff_pmca_tokens
from pulsara_agent.model_input.continuity import (
    SourceObservationLifecycle,
    SourceObservationPresence,
    encode_runtime_observation,
)
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ContextTrustClass,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.tools.builtins.todo import parse_todo_replacement


def process(index):
    return FrozenTerminalProcessHandoffFact(f"process:{index:026d}", "running")


def monitor(index):
    return FrozenTerminalMonitorHandoffFact(
        f"monitor:{index:026d}", f"process:{index:026d}", "active", bool(index % 2)
    )


def cell(index):
    return {
        "target": {"kind": "CELL", "cell_id": f"cell:{index:026d}"},
        "state": "completed",
        "has_unread_output": False,
        "pending_calls": 0,
    }


def agent(index):
    return {
        "agent": {"kind": "AGENT", "agent_id": f"session:{index:026d}"},
        "current_work": None,
        "queued_count": 1,
    }


def payload(handoff):
    return json.loads(handoff.full_text.split("\n", 1)[0])


def pmca_body(handoff):
    data = payload(handoff)
    del data["todos"]
    del data["todo_counts"]
    return (
        canonical_json_bytes(data).decode() + "\n" + handoff.full_text.split("\n", 1)[1]
    )


def todo_snapshot(items):
    parsed = parse_todo_replacement({"items": items})
    return FrozenTodoSnapshot(
        run_identity=TodoRunIdentity("root", ModelInputScopeKind.ROOT, "run", "owner"),
        revision=1,
        ordered_items=parsed.ordered_items,
        pending_count=parsed.pending_count,
        in_progress_count=parsed.in_progress_count,
        completed_count=parsed.completed_count,
    )


@pytest.mark.parametrize("todo", [None, todo_snapshot([])])
def test_no_pmca_and_no_todo_is_explicit_empty(todo):
    assert (
        freeze_compaction_runtime_handoff(
            terminal_processes=(), terminal_monitors=(), todo=todo
        )
        is None
    )


def test_todo_only_preserves_status_text_order_counts_and_continuation():
    items = [
        {"text": "start", "status": "completed"},
        {"text": "continue", "status": "in_progress"},
        {"text": "finish", "status": "pending"},
    ]
    handoff = freeze_compaction_runtime_handoff(
        terminal_processes=(), terminal_monitors=(), todo=todo_snapshot(items)
    )
    assert payload(handoff)["todos"] == items
    assert payload(handoff)["todo_counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 1,
        "total": 3,
    }
    assert handoff.full_text == handoff.compact_text
    assert handoff.full_text.endswith(HANDOFF_CONTINUATION)
    assert "omitted" not in payload(handoff)


@pytest.mark.parametrize(
    "serializer", [chat_semantic_wire_group, responses_semantic_wire_group]
)
def test_actual_source_encoder_and_both_serializers_stay_within_pmca_boundary(
    serializer,
):
    handoff = freeze_compaction_runtime_handoff(
        terminal_processes=tuple(process(i) for i in range(200, 0, -1)),
        terminal_monitors=tuple(monitor(i) for i in range(200, 0, -1)),
        code_cells=tuple(cell(i) for i in range(200, 0, -1)),
        agent_work_index=(agent(i) for i in range(200)),
        todo=None,
    )
    body = pmca_body(handoff)
    message = encode_runtime_observation(
        source_kind=ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
        trust_class=ContextTrustClass.UNTRUSTED_OBSERVATION,
        lifecycle=SourceObservationLifecycle.SNAPSHOT,
        presence=SourceObservationPresence.VALUE,
        contract_version="pulsara.compaction-runtime-handoff.v3-active-pmca",
        body=body,
    )
    primitive = PulsaraHeuristicTokenEstimatorV3()
    cost = sum(
        primitive.estimate_wire_json_component(item) for item in serializer(message)
    )
    assert cost <= MAXIMUM_RUNTIME_HANDOFF_PMCA_TOKENS
    assert estimate_runtime_handoff_pmca_tokens(body) >= cost
    assert handoff.full_text.endswith(HANDOFF_BUDGET_OMISSION)
    data = payload(handoff)
    counts = [
        len(data[key])
        for key in ("terminal_processes", "terminal_monitors", "code_cells", "agents")
    ]
    assert max(counts) - min(counts) <= 1
    assert data["terminal_processes"][0] == {
        "target": {"kind": "PROCESS", "process_id": f"process:{1:026d}"},
        "status": "running",
    }
    assert data["code_cells"][0] == cell(1)
    assert data["agents"][0] == agent(0)
    assert set(data) == {
        "terminal_processes",
        "terminal_monitors",
        "code_cells",
        "agents",
        "todos",
        "todo_counts",
    }


def test_legal_quoted_todo_can_exceed_local_budget_without_changing_pmca_selection():
    # This passes the real NFC/unique/192-byte/4-KiB parser. Source JSON nesting
    # amplifies escaping; the complete message is deliberately above 3,200.
    items = [{"text": str(i) + '"' * 107, "status": "pending"} for i in range(16)]
    todo = todo_snapshot(items)
    facts = dict(
        terminal_processes=tuple(process(i) for i in range(150)),
        terminal_monitors=tuple(monitor(i) for i in range(150)),
        code_cells=tuple(cell(i) for i in range(150)),
        agent_work_index=tuple(agent(i) for i in range(150)),
    )
    without = freeze_compaction_runtime_handoff(**facts, todo=None)
    with_todo = freeze_compaction_runtime_handoff(**facts, todo=todo)
    assert pmca_body(with_todo) == pmca_body(without)
    assert payload(with_todo)["todos"] == items
    assert payload(with_todo)["todo_counts"]["total"] == 16
    assert estimate_runtime_handoff_pmca_tokens(with_todo.full_text) > 3_200
    only_todo = freeze_compaction_runtime_handoff(
        terminal_processes=(), terminal_monitors=(), todo=todo
    )
    assert estimate_runtime_handoff_pmca_tokens(only_todo.full_text) > 3_200
    assert HANDOFF_BUDGET_OMISSION not in only_todo.full_text


def test_exact_boundary_and_omission_note_remove_multiple_whole_rows_in_reverse_order():
    seen = []

    def measure(body):
        data = json.loads(body.split("\n", 1)[0])
        count = sum(
            len(data[key])
            for key in (
                "terminal_processes",
                "terminal_monitors",
                "code_cells",
                "agents",
            )
        )
        assert "todos" not in data and "todo_counts" not in data
        seen.append((count, HANDOFF_BUDGET_OMISSION in body))
        if HANDOFF_BUDGET_OMISSION in body:
            return 3_200 if count <= 1 else 3_201
        return 3_200 if count <= 3 else 3_201

    handoff = freeze_compaction_runtime_handoff(
        terminal_processes=(process(1),),
        terminal_monitors=(monitor(1),),
        code_cells=(cell(1),),
        agent_work_index=(agent(1),),
        todo=None,
        measure_pmca=measure,
    )
    data = payload(handoff)
    assert len(data["terminal_processes"]) == 1
    assert data["terminal_monitors"] == data["code_cells"] == data["agents"] == []
    assert (3, False) in seen and (4, False) in seen
    assert seen[-3:] == [(3, True), (2, True), (1, True)]


def test_first_candidate_too_large_still_produces_handoff_and_stops_enumeration():
    def candidates():
        yield agent(1)
        raise AssertionError("must stop after confirmed overflow")

    def measure(body):
        return 3_201 if json.loads(body.split("\n", 1)[0])["agents"] else 1

    handoff = freeze_compaction_runtime_handoff(
        terminal_processes=(),
        terminal_monitors=(),
        agent_work_index=candidates(),
        todo=None,
        measure_pmca=measure,
    )
    assert handoff is not None
    assert payload(handoff)["agents"] == []
    assert handoff.full_text.endswith(HANDOFF_BUDGET_OMISSION)


def test_invalid_envelope_and_reader_errors_are_not_reported_as_empty_or_omission():
    with pytest.raises(CompactionRuntimeHandoffBoundError):
        CompactionRuntimeHandoffBuilder(todo=None, measure_pmca=lambda _: 3_201)

    def candidates():
        yield agent(1)
        raise RuntimeError("repository read failed")

    with pytest.raises(RuntimeError, match="repository read failed"):
        freeze_compaction_runtime_handoff(
            terminal_processes=(),
            terminal_monitors=(),
            agent_work_index=candidates(),
            todo=None,
        )


def port_fixture(*, subagent=None, code_mode=None, monitor_freezer=None, processes=()):
    port = DirectKernelToolPort.__new__(DirectKernelToolPort)
    port._session_id = "session:current"
    port._terminal_owner_id = "host:current"
    port._terminal = SimpleNamespace(freeze_compaction_handoff=lambda **_: processes)
    port._terminal_monitor = SimpleNamespace(
        freeze_compaction_handoff=monitor_freezer or (lambda **_: ())
    )
    port._todo_owner = SimpleNamespace(freeze_compaction_handoff=lambda **_: None)
    port._subagent = subagent
    port._code_mode = code_mode
    return port


def freeze(port, scope=ModelInputScopeKind.ROOT):
    return asyncio.run(
        port.freeze_compaction_runtime_handoff(
            conversation_scope_kind=scope,
            session_id="session:current",
            work_turn_id="turn:actual",
        )
    )


def test_subagent_passes_real_agent_and_work_scope_without_root_inventory():
    scopes = []

    def monitors(**scope):
        scopes.append(scope)
        return (
            {
                "monitor_id": "monitor:own",
                "process_id": "process:own",
                "state": "dormant",
                "pending_observation": True,
            },
        )

    class RootInventory:
        def freeze_compaction_handoff(self, **kwargs):
            raise AssertionError("Subagent must not read ROOT Agent/Cell inventory")

    own = SimpleNamespace(
        process_id="process:own", origin=SimpleNamespace(session_id="session:current")
    )
    foreign = SimpleNamespace(
        process_id="process:foreign", origin=SimpleNamespace(session_id="session:other")
    )
    port = port_fixture(
        monitor_freezer=monitors,
        subagent=RootInventory(),
        code_mode=RootInventory(),
        processes=(foreign, own),
    )
    data = payload(freeze(port, ModelInputScopeKind.SUBAGENT))
    assert scopes == [{"agent_id": "session:current", "work_turn_id": "turn:actual"}]
    assert data["terminal_processes"] == [
        {
            "target": {"kind": "PROCESS", "process_id": "process:own"},
            "status": "running",
        }
    ]
    assert data["agents"] == data["code_cells"] == []
    assert data["terminal_monitors"][0]["state"] == "dormant"


def test_lazy_agent_pages_read_after_50_and_stop_on_confirmed_budget_omission():
    reads = []

    class Inventory:
        async def freeze_compaction_handoff(self, *, cursor=None):
            reads.append(cursor)
            start = 0 if cursor is None else int(cursor.rsplit(":", 1)[1]) + 1
            assert start < 100, "do not scan remaining pages after overflow"
            return tuple(agent(i) for i in range(start, start + 50))

    handoff = freeze(port_fixture(subagent=Inventory()))
    assert reads == [None, agent(49)["agent"]["agent_id"]]
    assert 50 < len(payload(handoff)["agents"]) < 100
    assert handoff.full_text.endswith(HANDOFF_BUDGET_OMISSION)


def test_full_agent_page_requests_next_page_to_distinguish_exactly_complete_inventory():
    reads = []

    class Inventory:
        async def freeze_compaction_handoff(self, *, cursor=None):
            reads.append(cursor)
            return tuple(agent(i) for i in range(50)) if cursor is None else ()

    handoff = freeze(port_fixture(subagent=Inventory()))
    assert len(reads) == 2
    assert len(payload(handoff)["agents"]) == 50
    assert HANDOFF_BUDGET_OMISSION not in handoff.full_text


def test_async_reader_failure_on_needed_next_page_propagates():
    class Inventory:
        async def freeze_compaction_handoff(self, *, cursor=None):
            if cursor is not None:
                raise RuntimeError("active page read failed")
            return tuple(agent(i) for i in range(50))

    with pytest.raises(RuntimeError, match="active page read failed"):
        freeze(port_fixture(subagent=Inventory()))


def test_wrong_scope_identity_is_rejected_before_reading_owners():
    port = port_fixture()
    with pytest.raises(ValueError, match="scope differs"):
        asyncio.run(
            port.freeze_compaction_runtime_handoff(
                conversation_scope_kind=ModelInputScopeKind.SUBAGENT,
                session_id="session:foreign",
                work_turn_id="turn:actual",
            )
        )
