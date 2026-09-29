"""Worker history is public material at a frozen source cut, never executable state."""

from __future__ import annotations

from hashlib import sha256
import json

import pytest

from pulsara_agent.conversation_kernel.compaction.prompt import build_compaction_snapshot_carrier, freeze_compaction_summary_output
from pulsara_agent.conversation_kernel.compaction.contracts import CONTEXT_SNAPSHOT_CODEC, CONTEXT_SNAPSHOT_MEDIA_TYPE
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.conversation_kernel.subagents.history import read_terminal_worker_public_history
from pulsara_agent.model_input.contracts import CompactionContinuationMode

_SNAPSHOT = build_compaction_snapshot_carrier(
    summary=freeze_compaction_summary_output("COMPACTED_PUBLIC_SUMMARY", maximum_utf8_bytes=1024),
    recent_human_requests=(), continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
    active_request=None,
).body


def _content(body: bytes) -> dict[str, object]:
    return {
        "body": body,
        "content_size": len(body),
        "content_digest": "sha256:" + sha256(body).hexdigest(),
        "content_codec": "utf-8",
    }


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class _FrozenRows:
    def __init__(
        self, *, snapshot_body: bytes | None = _SNAPSHOT,
        status: str = "COMPLETED", has_image: bool = False,
    ) -> None:
        self.snapshot_body = snapshot_body
        self.status = status
        self.has_image = has_image
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, args: tuple[object, ...]):
        self.queries.append((query, args))
        if "FROM pulsara_v3.subagent_tasks AS t" in query:
            return _Result({
                "objective": "review the input", "parent_context_body": "ROOT_PUBLIC",
                "dependency_context_body": "DIRECT_DEPENDENCY",
                "terminal_material_body": "PRIOR_RESULT", "worker_history_body": '{"pulsara_worker_history":{"frames":[{"objective":"ANCESTOR_PUBLIC"}]}}', "status": self.status,
                "base_kind": "SNAPSHOT", "source_through_sequence": 8,
                "context_snapshot_id": "snapshot:source",
            })
        if "FROM pulsara_v3.context_snapshots AS s" in query:
            return _Result({**_content(self.snapshot_body or b""),
                "inline_content": self.snapshot_body, "blob_id": None,
                "content_codec": CONTEXT_SNAPSHOT_CODEC, "content_media_type": CONTEXT_SNAPSHOT_MEDIA_TYPE,
                "workspace_id": "workspace:one", "session_id": "session:one",
            })
        if "FROM pulsara_v3.canonical_image_refs AS r" in query:
            return _Result([])
        if "FROM pulsara_v3.canonical_image_refs AS image" in query:
            return _Result({"present": 1} if self.has_image else None)
        if "FROM pulsara_v3.transcript_entries AS e" in query:
            return _Result([
                {
                    "id": "entry:assistant", "entry_sequence": 9,
                    "entry_kind": "ASSISTANT_MESSAGE", "tool_call_id": None,
                    "result_state": None, **_content(b""),
                },
                {
                    "id": "entry:tool-result", "entry_sequence": 10,
                    "entry_kind": "TOOL_RESULT", "tool_call_id": "call:settled",
                    "result_state": "SUCCESS", **_content(b"public tool result"),
                },
            ])
        if "FROM pulsara_v3.assistant_message_blocks AS b" in query:
            return _Result([
                {
                    "assistant_entry_id": "entry:assistant", "block_kind": "TOOL_CALL",
                    "tool_call_id": "call:unsettled", "tool_name": "terminal",
                    "tool_arguments": {"command": "do not replay"},
                },
                {
                    "assistant_entry_id": "entry:assistant", "block_kind": "TOOL_CALL",
                    "tool_call_id": "call:settled", "tool_name": "terminal",
                    "tool_arguments": {"command": "already done"},
                },
            ])
        raise AssertionError(f"unexpected public-history query: {query}")


def test_worker_history_uses_frozen_adopted_snapshot_and_marks_unsettled_tool() -> None:
    rows = _FrozenRows()
    body = read_terminal_worker_public_history(
        rows, session_id="session:one", source_task_id="task:old",
        through_sequence=10, binding_revision_id="revision:frozen",
    )
    package = json.loads(body)["pulsara_worker_history"]
    assert len(package["frames"]) == 1
    history = package["frames"][0]
    assert history["source_task_id"] == "task:old"
    assert history["initial_public_sources"] is None  # already covered by adopted compaction
    assert "handoff_instruction" not in body
    assert "AWAIT_NEXT_USER" not in body
    assert history["adopted_summary"] == "COMPACTED_PUBLIC_SUMMARY"
    assert history["adopted_summary_through_sequence"] == 8
    assert [entry["sequence"] for entry in history["committed_public_entries"]] == [9, 10]
    blocks = history["committed_public_entries"][0]["blocks"]
    assert blocks[0]["historical_result"] == "UNKNOWN_OR_UNFINISHED"
    assert "historical_result" not in blocks[1]
    assert package["content_semantics"] == "ADVISORY_HISTORY_NOT_EXECUTABLE"
    assert rows.queries[0][1] == ("revision:frozen", "session:one", "task:old")
    assert rows.queries[4][1][:4] == ("session:one", "task:old", 8, 10)


def test_worker_history_rejects_corrupted_adopted_summary() -> None:
    rows = _FrozenRows()
    rows.snapshot_body = b"corrupted"
    original_execute = rows.execute

    def execute(query: str, args: tuple[object, ...]):
        result = original_execute(query, args)
        if "FROM pulsara_v3.context_snapshots AS s" in query:
            result.value["content_digest"] = "sha256:" + "0" * 64
        return result

    rows.execute = execute  # type: ignore[method-assign]
    with pytest.raises(ConversationKernelConflict, match="corrupt"):
        read_terminal_worker_public_history(
            rows, session_id="session:one", source_task_id="task:old",
            through_sequence=10, binding_revision_id="revision:frozen",
        )


@pytest.mark.parametrize(
    ("rows", "message"),
    (
        (_FrozenRows(status="ACTIVE"), "not terminal"),
        (_FrozenRows(snapshot_body=None), "storage union"),
        (_FrozenRows(has_image=True), "unsupported multimodal"),
    ),
)
def test_worker_history_rejects_unavailable_public_source(
    rows: _FrozenRows, message: str,
) -> None:
    with pytest.raises(ConversationKernelConflict, match=message):
        read_terminal_worker_public_history(
            rows, session_id="session:one", source_task_id="task:old",
            through_sequence=10, binding_revision_id="revision:frozen",
        )


def test_uncompacted_worker_keeps_its_actual_imported_ancestor_history():
    rows = _FrozenRows()
    original = rows.execute
    def execute(query, args):
        result = original(query, args)
        if "FROM pulsara_v3.subagent_tasks AS t" in query:
            result.value.update(base_kind="EMPTY", source_through_sequence=0, context_snapshot_id=None)
        return result
    rows.execute = execute
    body = read_terminal_worker_public_history(rows, session_id="session:one", source_task_id="task:old", through_sequence=10, binding_revision_id="revision:frozen")
    assert json.loads(body)["pulsara_worker_history"]["frames"][0]["objective"] == "ANCESTOR_PUBLIC"


def test_worker_history_thousand_generations_stay_flat_and_ordered():
    rows = _FrozenRows()
    execute = rows.execute
    previous = None
    def read(query, args):
        result = execute(query, args)
        if "FROM pulsara_v3.subagent_tasks AS t" in query:
            result.value.update(base_kind="EMPTY", source_through_sequence=0,
                context_snapshot_id=None, worker_history_body=previous)
        return result
    rows.execute = read
    for generation in range(1000):
        previous = read_terminal_worker_public_history(rows, session_id="session:one",
            source_task_id=f"task:{generation}", through_sequence=10, binding_revision_id="revision:frozen")
        rows.queries.clear()
    package = json.loads(previous)["pulsara_worker_history"]
    assert [f["source_task_id"] for f in package["frames"]] == [f"task:{i}" for i in range(1000)]
    assert previous.count('"committed_public_entries"') == 1000
