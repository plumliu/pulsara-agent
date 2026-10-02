"""Worker history preserves fixed canonical references, not JSON history packages."""

from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.conversation_kernel.subagents.history import (
    validate_worker_history_source,
    worker_history_segments,
    worker_initial_material_items,
)
from pulsara_agent.model_input.contracts import PreparedProviderInputCut


class Result:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value


class Source:
    def __init__(self, status="COMPLETED", frontier=True, found=True):
        self.status, self.frontier, self.found = status, frontier, found

    def execute(self, query, args):
        if "task.status" in query:
            assert args == ("revision:A", "session:one", "task:A")
            return Result(
                dict(
                    status=self.status,
                    turn_id="turn:A",
                    entry_sequence=4,
                    source_through_sequence=6,
                )
                if self.found
                else None
            )
        assert args == ("session:one", "task:A", 8)
        return Result({"present": 1} if self.frontier else None)


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"])
def test_started_terminal_source_has_exact_cut_and_revision(status):
    assert (
        validate_worker_history_source(
            Source(status),
            session_id="session:one",
            source_task_id="task:A",
            through_sequence=8,
            binding_revision_id="revision:A",
        )
        == "turn:A"
    )


@pytest.mark.parametrize(
    "source,cut",
    [
        (Source(status="ACTIVE"), 8),
        (Source(found=False), 8),
        (Source(frontier=False), 8),
        (Source(), 5),
    ],
)
def test_invalid_source_never_becomes_summary_fallback(source, cut):
    with pytest.raises(ConversationKernelConflict):
        validate_worker_history_source(
            source,
            session_id="session:one",
            source_task_id="task:A",
            through_sequence=cut,
            binding_revision_id="revision:A",
        )


class Chain:
    def __init__(self, snapshot_at=None):
        self.snapshot_at = snapshot_at
        self.visited = []

    def execute(self, query, args):
        if "SELECT turn.scope_subagent_task_id" in query:
            revision, session, turn = args
            n = int(turn.split(":")[1])
            assert session == "session:one" and revision == f"revision:{n}"
            self.visited.append(n)
            return Result(
                dict(
                    scope_subagent_task_id=f"task:{n}",
                    base_kind="SNAPSHOT" if n == self.snapshot_at else "FULL_HISTORY",
                    source_through_sequence=n * 2,
                    history_source_task_id=f"task:{n - 1}" if n else None,
                    history_cut_sequence=n * 2,
                    history_context_binding_revision_id=f"revision:{n - 1}"
                    if n
                    else None,
                )
            )
        if "task.status" in query:
            n = int(args[2].split(":")[1])
            return Result(
                dict(
                    status="COMPLETED",
                    turn_id=f"turn:{n}",
                    entry_sequence=n * 2 + 1,
                    source_through_sequence=n * 2,
                )
            )
        return Result({"present": 1})


def test_thousand_generation_ancestry_is_iterative_and_ordered():
    connection = Chain()
    cut = PreparedProviderInputCut("session:one", "turn:999", "revision:999", 2000)
    segments = worker_history_segments(
        connection, cut, deadline_monotonic=monotonic() + 10, maximum_items=2000
    )
    assert [s.turn_id for s in segments.segments] == [f"turn:{n}" for n in range(1000)]
    assert connection.visited == list(reversed(range(1000)))
    assert segments.segments[0].provider_input_through_sequence == 2


def test_snapshot_stops_expanding_covered_ancestors():
    connection = Chain(snapshot_at=997)
    cut = PreparedProviderInputCut("session:one", "turn:999", "revision:999", 2000)
    segments = worker_history_segments(
        connection, cut, deadline_monotonic=monotonic() + 10, maximum_items=100
    )
    assert [s.turn_id for s in segments.segments] == ["turn:997", "turn:998", "turn:999"]
    assert connection.visited == [999, 998, 997]


def test_existing_physical_item_budget_limits_one_read_without_truncation():
    with pytest.raises(ConversationKernelConflict, match="item budget"):
        worker_history_segments(
            Chain(),
            PreparedProviderInputCut("session:one", "turn:999", "revision:999", 2000),
            deadline_monotonic=monotonic() + 10,
            maximum_items=100,
        )


def test_initial_materials_are_ordered_anchored_and_covered_once():
    class Materials:
        def execute(self, query, args):
            return Result(
                dict(
                    initial_entry_id="entry:initial",
                    entry_sequence=9,
                    parent_context_body="parent",
                    dependency_context_body="dependency",
                    terminal_material_body="terminal",
                )
            )

    cut = PreparedProviderInputCut("session:one", "turn:worker", "revision:worker", 10)
    items = worker_initial_material_items(Materials(), cut=cut, floor=0)
    assert len(items) == 3
    assert all(
        i.source_entry_id == "entry:initial" and i.source_entry_sequence == 9
        for i in items
    )
    texts = [i.content[0].text for i in items]
    assert (
        "PARENT_CONTEXT" in texts[0]
        and "DEPENDENCY_RESULTS" in texts[1]
        and "TERMINAL_MATERIAL" in texts[2]
    )
    assert all("UNTRUSTED_COLLABORATION_DATA" in t for t in texts)
    assert worker_initial_material_items(Materials(), cut=cut, floor=9) == ()
