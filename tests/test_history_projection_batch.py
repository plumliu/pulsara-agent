"""Batch restoration preserves concrete canonical views and RR ownership.

Single-entry equality exercises the public batch/single contract, not a frozen
old implementation. Independent expected fields below pin display/UTF-8 meaning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from time import monotonic

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.history_projection import (
    HistoryBuildFailure,
    HistoryBuildSuccess,
    build_history_document,
    build_history_group,
    canonical_history_message,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.tool_artifacts import ToolOutputArtifactProcessor
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence,
    VisualizationOccurrenceState,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    HISTORICAL_IMAGE_MARKER,
    LLMTextPart,
    PromptAnnotationPart,
    PromptAnnotationSource,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputArtifactCandidate,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_conversation_fork import (
    final,
    fork,
    identity,
    new_session,
    repo as repo,
    rows,
)
from tests.test_history_projection_cut import append, question
from tests.test_kernel_image_input_k2_postgres import (
    _bound_session,
    _direct_intent,
    _image,
    _workspace,
)
from tests.test_round1_tool_output_artifact import _install_tool_call

pytestmark = pytest.mark.postgres


@contextmanager
def snapshot(repo):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY,
        row_factory=dict_row,
        isolation_level=IsolationLevel.REPEATABLE_READ,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        yield connection


def head(repo, session_id):
    return rows(repo, "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s", (session_id,))[0]["latest_entry_sequence"]


def _plain_message(entry, text, *, region, label, block=None):
    user = region in {"Q1", "Q2"}
    return {
        "entry_id": entry["id"], "origin_entry_id": entry["origin_entry_id"],
        "sequence": entry["entry_sequence"], "role": "user" if user else "assistant",
        "label": label, "at": entry["accepted_at"].isoformat(), "text": text,
        "annotated": False,
        "fields": [{"part": 0 if user else None, "block": block,
                    "field": "text", "text": text, "display_start": 0}],
        "body_ranges": [[0, len(text)]], "region": region,
    }


def test_concrete_utf8_documents_and_variant_mutation_are_independent(repo):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard, "问 Ω😀")
    progress, _ = append(repo, guard, owner, "进展 café")
    answer, _ = append(repo, guard, owner, "答 🪐", complete=True)
    _, q2 = question(repo, guard, "下一问 λ")
    cut = head(repo, guard.session_id)
    entries = {row["id"]: row for row in rows(repo, "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s", (guard.session_id,))}
    blocks = {row["assistant_entry_id"]: row["id"] for row in rows(repo, "SELECT id,assistant_entry_id FROM pulsara_v3.assistant_message_blocks WHERE session_id=%s", (guard.session_id,))}
    expected = [
        _plain_message(entries[q1], "问 Ω😀", region="Q1", label="用户输入"),
        _plain_message(entries[progress], "进展 café", region="A", label="助手进展", block=blocks[progress]),
        _plain_message(entries[answer], "答 🪐", region="A", label="助手回答", block=blocks[answer]),
        _plain_message(entries[q2], "下一问 λ", region="Q2", label="用户输入"),
    ]
    with snapshot(repo) as connection:
        built = build_history_group(connection, guard.session_id, q1, covered_entry_sequence=cut)
        repeated = build_history_group(connection, guard.session_id, q1, covered_entry_sequence=cut)
        assert isinstance(built, HistoryBuildSuccess) and built == repeated
        assert len(built.documents) == 4
        for (variant, tools), document in built.documents.items():
            messages = expected if variant == "QAQ" else expected[:3]
            assert set(document) == {"messages", "body", "positions", "identity", "q2_entry_id"}
            assert document["messages"] == messages
            assert document["body"] == ("问 Ω😀\n进展 café\n答 🪐\n下一问 λ" if variant == "QAQ" else "问 Ω😀\n进展 café\n答 🪐")
            # Coordinates are UTF-8 bytes while display offsets are characters.
            ranges = [(0, 10), (11, 23), (24, 32), (33, 45)]
            assert [(p["start"], p["end"]) for p in document["positions"]] == ranges[:len(messages)]
            assert [p["display_start"] for p in document["positions"]] == [0] * len(messages)
            assert [p["block"] for p in document["positions"]] == [None, blocks[progress], blocks[answer], None][:len(messages)]
            assert document["identity"] == [
                {"region": message["region"], "origin": message["origin_entry_id"],
                 "role": message["role"], "label": message["label"], "text": message["text"]}
                for message in messages
            ]
            assert document["q2_entry_id"] == q2
            assert build_history_document(connection, guard.session_id, q1,
                covered_entry_sequence=cut, variant=variant, include_tools=tools) == document
        untouched = deepcopy(built.documents[("QAQ", True)])
        built.documents[("QA", False)]["messages"][1]["fields"][0]["text"] = "local mutation"
        assert built.documents[("QAQ", True)] == untouched
        assert built.documents[("QA", True)] == repeated.documents[("QA", True)]
        assert build_history_group(connection, guard.session_id, q1, covered_entry_sequence=cut) == repeated


def test_annotation_image_visualization_and_final_labels_match_single_entry_reads(repo):
    lease, runtime, _ = _bound_session(repo)
    workspace = _workspace(repo, lease)
    source, _, _ = _direct_intent(repo, lease, runtime, FrozenPromptContent.text("quote source"))
    quote = "引用 Ω😀"
    source_answer = final(repo, lease.guard, source.turn_id, quote)
    image = _image(workspace_id=workspace)
    comment = "批注 café"
    prompt = FrozenPromptContent((
        LLMTextPart("前缀"),
        PromptAnnotationPart(quote=quote,
            source=PromptAnnotationSource(source_answer, 0, len(quote.encode("utf-16-le")) // 2), comment=comment),
        image,
    ))
    intent, _, _ = _direct_intent(repo, lease, runtime, prompt)
    progress, _ = append(repo, lease.guard, intent.turn_id, "进展 Ω")
    cut = repo.prepare_provider_input_cut(lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30)
    final_id = identity("entry")
    final_text = "完成 🛰"
    repo.commit_assistant_message(
        lease.guard, cut=cut, entry_id=final_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(AssistantTextBlock(identity("block"), InlineContent.from_bytes(final_text.encode())),),
        visualizations=(
            FrozenVisualizationOccurrence("plot.html", VisualizationOccurrenceState.READY, html=b"<h1>plot</h1>"),
            FrozenVisualizationOccurrence("image.png", VisualizationOccurrenceState.READY, image=image),
            FrozenVisualizationOccurrence("missing.png", VisualizationOccurrenceState.FAILED,
                failure_code="IMAGE_READ_FAILED", failure_detail="not available"),
        ),
        complete_turn=True, occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    q2, _, _ = _direct_intent(repo, lease, runtime, FrozenPromptContent.text("successor"))
    boundary = head(repo, lease.guard.session_id)
    with snapshot(repo) as connection:
        built = build_history_group(connection, lease.guard.session_id, intent.entry_id,
                                    covered_entry_sequence=boundary)
        assert isinstance(built, HistoryBuildSuccess) and len(built.documents) == 4
        for (variant, tools), document in built.documents.items():
            expected_ids = [intent.entry_id, progress, final_id] + ([q2.entry_id] if variant == "QAQ" else [])
            assert [message["entry_id"] for message in document["messages"]] == expected_ids
            for message in document["messages"]:
                entry = connection.execute("SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (message["entry_id"],)).fetchone()
                single = canonical_history_message(connection, entry, include_tools=tools)
                assert message == {**single, "region": message["region"]}
            q1_message, progress_message, answer_message = document["messages"][:3]
            prefix = "历史引用（被引用的原文）：\n" + quote + "\n历史用户批注：\n"
            assert q1_message["text"] == "前缀\n" + prefix + comment + "\n" + HISTORICAL_IMAGE_MARKER
            assert q1_message["annotated"] is True
            assert q1_message["fields"] == [
                {"part": 0, "block": None, "field": "text", "text": "前缀", "display_start": 0},
                {"part": 1, "block": None, "field": "comment", "text": comment, "display_start": 3 + len(prefix)},
            ]
            marker_start = len("前缀\n" + prefix + comment + "\n")
            assert q1_message["atomic_ranges"] == [[marker_start, marker_start + len(HISTORICAL_IMAGE_MARKER)]]
            assert progress_message["label"] == "助手进展"
            assert answer_message["label"] == "助手回答"
            visualization = "已保存展示项 2：" + HISTORICAL_IMAGE_MARKER
            assert answer_message["text"] == final_text + "\n" + visualization
            assert answer_message["body_ranges"] == [[0, len(final_text)]]
            assert answer_message["atomic_ranges"] == [[len(final_text) + 1, len(final_text) + 1 + len(visualization)]]
            assert quote not in document["body"] and HISTORICAL_IMAGE_MARKER not in document["body"]
            assert "已保存展示项" not in document["body"] and image.image_ref not in document["body"]


def test_imported_history_final_label_uses_its_canonical_group(repo):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard, "fork question")
    progress, _ = append(repo, guard, owner, "before final")
    answer, _ = append(repo, guard, owner, "final answer", complete=True)
    child = fork(repo, guard.session_id, answer)
    child_entries = rows(repo, "SELECT * FROM pulsara_v3.transcript_entries WHERE session_id=%s ORDER BY entry_sequence", (child.child_session_id,))
    copied = {entry["origin_entry_id"]: entry for entry in child_entries}
    child_q1 = copied[q1]["id"]
    assert copied[answer]["turn_id"] is None and copied[answer]["imported_history_group_id"] is not None
    with snapshot(repo) as connection:
        built = build_history_group(connection, child.child_session_id, child_q1,
                                    covered_entry_sequence=head(repo, child.child_session_id))
        assert isinstance(built, HistoryBuildSuccess)
        for document in built.documents.values():
            labels = {message["origin_entry_id"]: message["label"] for message in document["messages"]}
            assert labels[progress] == "助手进展" and labels[answer] == "助手回答"
            for message in document["messages"]:
                entry = copied[message["origin_entry_id"]]
                single = canonical_history_message(connection, entry)
                assert single["label"] == message["label"]


def _tool_fixture(repo):
    workspace = identity("workspace")
    lease, owner, request, call, attempt = _install_tool_call(repo, workspace)
    result_entry = identity("entry")
    artifact = "artifact-only Ω🛠"
    projection = ToolOutputArtifactProcessor(repo.connection_provider).prepare(
        session_id=lease.guard.session_id, workspace_id=workspace,
        result_entry_id=result_entry, public_output="preview café",
        candidate=ToolOutputArtifactCandidate(role="OUTPUT", text=artifact,
            source_coverage=ToolOutputSourceCoverage.RETAINED_SNAPSHOT,
            source_coverage_reason=ToolOutputSourceCoverageReason.TERMINAL_RETENTION_GAP),
        artifact_inline_result=False, deadline_monotonic=monotonic() + 30,
    )
    assert projection.artifact_blob is not None and projection.artifact_disposition == "INCOMPLETE"
    acceptance = build_prepared_tool_result_acceptance(
        guard=lease.guard, workspace_id=workspace, result_id=identity("result"),
        result_entry_id=result_entry, turn_id=owner, assistant_entry_id=request,
        tool_call_id=call, attempt_id=attempt, result_state="SUCCESS",
        canonical_preview_content=projection.canonical_preview,
        artifact_disposition=projection.artifact_disposition,
        artifact_id=projection.artifact_id, artifact_blob_descriptor=projection.artifact_blob,
        source_coverage=projection.source_coverage, display_kind=projection.display_kind,
        source_coverage_reason=projection.source_coverage_reason,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
        actor_id="tool:test", observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.TERMINAL_PROCESS,
        trusted_tool_reported_duration_microseconds=None,
    )
    repo.accept_tool_result(lease.guard, candidate=acceptance, deadline_monotonic=monotonic() + 30)
    answer, cut = append(repo, lease.guard, owner, "final TEXT 🪐", complete=True)
    q1 = rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='USER_MESSAGE'", (lease.guard.session_id,))[0]["id"]
    return lease, q1, result_entry, answer, cut, artifact


@pytest.mark.parametrize("corrupt", ["preview", "artifact", "TEXT"])
def test_tool_and_text_failures_do_not_cross_mode_qualification(
    repo, stage2_migrated_postgres_database, corrupt,
):
    lease, q1, result_entry, answer, cut, artifact = _tool_fixture(repo)
    with snapshot(repo) as connection:
        valid = build_history_group(connection, lease.guard.session_id, q1, covered_entry_sequence=cut)
    assert isinstance(valid, HistoryBuildSuccess)
    assert artifact in valid.documents[("QA", True)]["body"]
    assert artifact not in valid.documents[("QA", False)]["body"]
    assert "terminal\n" in valid.documents[("QA", True)]["body"]
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn, row_factory=dict_row) as connection:
        if corrupt == "preview":
            changed = connection.execute("UPDATE pulsara_v3.transcript_entries SET inline_content=%s || substring(inline_content FROM 2) WHERE id=%s", (b"x", result_entry))
        elif corrupt == "artifact":
            changed = connection.execute("UPDATE pulsara_v3.blobs SET body=%s || substring(body FROM 2) WHERE id=(SELECT output_artifact_blob_id FROM pulsara_v3.tool_results WHERE result_entry_id=%s)", (b"x", result_entry))
        else:
            changed = connection.execute("UPDATE pulsara_v3.assistant_message_blocks SET inline_content=%s || substring(inline_content FROM 2) WHERE assistant_entry_id=%s AND block_kind='TEXT'", (b"x", answer))
        assert changed.rowcount == 1
        failed = build_history_group(connection, lease.guard.session_id, q1, covered_entry_sequence=cut)
        assert isinstance(failed, HistoryBuildFailure)
        assert failed.stage == "A" and failed.entry_id == (answer if corrupt == "TEXT" else result_entry)
        assert build_history_document(connection, lease.guard.session_id, q1,
            covered_entry_sequence=cut, variant="QA", include_tools=True) is None
        without_tools = build_history_document(connection, lease.guard.session_id, q1,
            covered_entry_sequence=cut, variant="QA", include_tools=False)
        assert without_tools == (None if corrupt == "TEXT" else valid.documents[("QA", False)])
        connection.rollback()


def test_parse_cache_cannot_cross_two_independent_repeatable_read_snapshots(
    repo, stage2_migrated_postgres_database,
):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard)
    answer, cut = append(repo, guard, owner, "stable decoded answer", complete=True)
    original = rows(repo, "SELECT id,inline_content FROM pulsara_v3.assistant_message_blocks WHERE assistant_entry_id=%s AND block_kind='TEXT'", (answer,))[0]
    admin_dsn = stage2_migrated_postgres_database.admin_dsn
    try:
        with snapshot(repo) as first:
            before = build_history_group(first, guard.session_id, q1, covered_entry_sequence=cut)
            assert isinstance(before, HistoryBuildSuccess)
            with psycopg.connect(admin_dsn, autocommit=True) as writer:
                writer.execute("UPDATE pulsara_v3.assistant_message_blocks SET inline_content=%s || substring(inline_content FROM 2) WHERE id=%s", (b"x", original["id"]))
            with snapshot(repo) as second:
                assert second.info.backend_pid != first.info.backend_pid
                after = build_history_group(second, guard.session_id, q1, covered_entry_sequence=cut)
                assert isinstance(after, HistoryBuildFailure) and after.entry_id == answer
                for tools in (False, True):
                    assert build_history_document(second, guard.session_id, q1,
                        covered_entry_sequence=cut, variant="QA", include_tools=tools) is None
            # The first real RR still sees its original tuple version. Neither
            # another build nor the second reader's failure may poison it.
            assert build_history_group(first, guard.session_id, q1, covered_entry_sequence=cut) == before
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as writer:
            writer.execute("UPDATE pulsara_v3.assistant_message_blocks SET inline_content=%s WHERE id=%s", (original["inline_content"], original["id"]))
    with snapshot(repo) as third:
        assert build_history_group(third, guard.session_id, q1, covered_entry_sequence=cut) == before
