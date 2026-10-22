"""Fixed-prefix construction, typed failures and shared variant work."""
from collections import Counter
from datetime import datetime, timezone
from time import monotonic

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel import history_projection
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.history_projection import (
    HistoryBuildFailure,
    HistoryBuildSuccess,
    build_history_document,
    build_history_group,
)
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_conversation_fork import identity, new_session, repo as repo, rows, turn
from tests.test_session_content_query import call_block, saved_calls

pytestmark = pytest.mark.postgres


def question(repo, guard, text="question 中文🌌"):
    owner, _, _ = turn(repo, guard, text, finish=False)
    q1 = rows(
        repo,
        "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s AND entry_kind='USER_MESSAGE'",
        (guard.session_id, owner),
    )[0]["id"]
    return owner, q1


def append(repo, guard, owner, text, *, complete=False):
    cut = repo.prepare_provider_input_cut(
        guard, turn_id=owner, deadline_monotonic=monotonic() + 30
    )
    entry_id = identity("entry")
    accepted = repo.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(
            AssistantTextBlock(identity("block"), InlineContent.from_bytes(text.encode())),
        ),
        complete_turn=complete,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry_id, accepted.entry_sequence


def group(repo, session_id, q1_id, cut):
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY,
        row_factory=dict_row,
        isolation_level=IsolationLevel.REPEATABLE_READ,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return build_history_group(
            connection, session_id, q1_id, covered_entry_sequence=cut
        )


def test_cut_freezes_Q1_A_and_first_Q2_without_using_projection(repo):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard)
    for cut in (0, 1):
        empty = group(repo, guard.session_id, q1, cut)
        assert isinstance(empty, HistoryBuildSuccess)
        assert empty.documents == {} and empty.expected_variants == frozenset()
        assert empty.memory_domain_id == "u_local"
    _, cut2 = append(repo, guard, owner, "oldprefixneedle 中文😀")
    old = group(repo, guard.session_id, q1, cut2)
    assert isinstance(old, HistoryBuildSuccess)
    assert old.expected_variants == frozenset({("QA", False), ("QA", True)})
    _, cut3 = append(repo, guard, owner, "newtailneedle", complete=True)
    next_owner, q2 = question(repo, guard, "successorword 原始第二问🪐")
    append(repo, guard, next_owner, "differentowneranswer", complete=True)
    assert group(repo, guard.session_id, q1, cut2) == old
    assert group(repo, guard.session_id, q1, cut3).expected_variants == old.expected_variants
    q2_sequence = rows(
        repo,
        "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s",
        (q2,),
    )[0]["entry_sequence"]
    current = group(repo, guard.session_id, q1, q2_sequence)
    assert current.expected_variants == frozenset(
        (variant, tools) for variant in ("QA", "QAQ") for tools in (False, True)
    )
    qa, qaq = current.documents[("QA", False)], current.documents[("QAQ", False)]
    assert qa["q2_entry_id"] == qaq["q2_entry_id"] == q2
    assert "successorword" not in qa["body"] and "successorword" in qaq["body"]
    assert "differentowneranswer" not in qaq["body"]
    assert "newtailneedle" not in old.documents[("QA", False)]["body"]
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY,
        row_factory=dict_row,
        isolation_level=IsolationLevel.REPEATABLE_READ,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        for (variant, tools), document in current.documents.items():
            assert build_history_document(
                connection, guard.session_id, q1,
                covered_entry_sequence=q2_sequence, variant=variant, include_tools=tools,
            ) == document
            body = document["body"].encode()
            for position in document["positions"]:
                message = document["messages"][position["message"]]
                field = next(
                    field for field in message["fields"]
                    if all(field[key] == position[key] for key in (
                        "block", "part", "field", "display_start"
                    ))
                )
                assert body[position["start"]:position["end"]] == field["text"].encode()


def test_group_shares_source_decode_and_derives_QAQ_without_rescan(repo, monkeypatch):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard)
    a1, _ = append(repo, guard, owner, "one")
    a2, _ = append(repo, guard, owner, "two", complete=True)
    _, q2 = question(repo, guard, "raw successor")
    cut = rows(repo, "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s", (guard.session_id,))[0]["latest_entry_sequence"]
    restored = []
    original = history_projection.canonical_history_message

    def restore(connection, entry, *, include_tools=False, assistant_content=None):
        restored.append((entry["id"], include_tools))
        return original(
            connection, entry, include_tools=include_tools,
            assistant_content=assistant_content,
        )

    monkeypatch.setattr(history_projection, "canonical_history_message", restore)
    decoded = []
    decode = history_projection.decode_saved_text

    def observe_decode(row):
        if row["kind"] == "assistant":
            decoded.append(row["assistant_entry_id"])
        return decode(row)

    monkeypatch.setattr(history_projection, "decode_saved_text", observe_decode)
    queries = []

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, *args, **kwargs):
            queries.append(query)
            return self.connection.execute(query, *args, **kwargs)

    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row,
        isolation_level=IsolationLevel.REPEATABLE_READ,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        result = build_history_group(
            ObservedConnection(connection), guard.session_id, q1,
            covered_entry_sequence=cut,
        )
    assert isinstance(result, HistoryBuildSuccess) and len(result.documents) == 4
    assert Counter(restored) == Counter({
        (q1, False): 1, (q2, False): 1,
        (a1, False): 1, (a1, True): 1,
        (a2, False): 1, (a2, True): 1,
    })
    assert sum("COALESCE(turn_id,imported_history_group_id)" in q for q in queries) == 1
    assert sum("ORDER BY entry_sequence LIMIT 1" in q for q in queries) == 1
    # Each TEXT is verified/decoded once for all four rendered variants.
    assert Counter(decoded) == Counter({a1: 1, a2: 1})
    assert sum("FROM pulsara_v3.assistant_message_blocks a" in q for q in queries) == 1
    assert not any("history_search_documents" in q for q in queries)


@pytest.mark.parametrize("corrupt", ["Q1", "A", "Q2"])
def test_required_failure_is_typed_optional_Q2_preserves_QA(
    repo, stage2_migrated_postgres_database, corrupt
):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard)
    a, old_cut = append(repo, guard, owner, "completeprefix")
    old = group(repo, guard.session_id, q1, old_cut)
    later, cut = append(repo, guard, owner, "laterprefix", complete=True)
    _, q2 = question(repo, guard, "optional second question")
    head = rows(repo, "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id=%s", (guard.session_id,))[0]["latest_entry_sequence"]
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn, row_factory=dict_row) as connection:
        if corrupt == "A":
            connection.execute(
                "UPDATE pulsara_v3.assistant_message_blocks SET inline_content=%s || substring(inline_content FROM 2) WHERE assistant_entry_id=%s AND block_kind='TEXT'",
                (b"x", later),
            )
            assert build_history_group(connection, guard.session_id, q1, covered_entry_sequence=old_cut) == old
        else:
            connection.execute(
                "UPDATE pulsara_v3.transcript_entries SET inline_content=%s || substring(inline_content FROM 2) WHERE id=%s",
                (b"x", q1 if corrupt == "Q1" else q2),
            )
        result = build_history_group(connection, guard.session_id, q1, covered_entry_sequence=head)
        requested = build_history_document(
            connection, guard.session_id, q1, covered_entry_sequence=head, variant="QA"
        )
        if corrupt in {"Q1", "A"}:
            assert isinstance(result, HistoryBuildFailure)
            assert result.stage == corrupt and isinstance(result.error, RuntimeError)
            assert requested is None
        else:
            assert isinstance(result, HistoryBuildSuccess)
            assert result.expected_variants == frozenset({("QA", False), ("QA", True)})
            assert requested is not None and requested["q2_entry_id"] is None
            assert "completeprefix" in requested["body"] and "laterprefix" in requested["body"]
            assert build_history_document(
                connection, guard.session_id, q1, covered_entry_sequence=head, variant="QAQ"
            ) is None
        connection.rollback()
    # Fault injection is rolled back; canonical accepted rows remain intact.
    assert group(repo, guard.session_id, q1, cut).documents[("QA", False)]["body"]


def test_sparse_tools_only_and_wrong_session_cannot_supply_Q1(repo):
    lease = new_session(repo)
    request = saved_calls(repo, lease, (
        call_block("terminal", {"command": "toolonlyneedle 中文🛠️"}),
        call_block("search_session_content", {"query": "excludedquerytoolword"}),
    ))
    q1 = rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='USER_MESSAGE'", (lease.guard.session_id,))[0]["id"]
    cut = rows(repo, "SELECT entry_sequence FROM pulsara_v3.transcript_entries WHERE id=%s", (request,))[0]["entry_sequence"]
    result = group(repo, lease.guard.session_id, q1, cut)
    assert result.expected_variants == frozenset({("QA", True)})
    assert "toolonlyneedle" in result.documents[("QA", True)]["body"]
    assert "excludedquerytoolword" not in result.documents[("QA", True)]["body"]
    unrelated = new_session(repo).guard
    empty = group(repo, unrelated.session_id, q1, cut)
    assert isinstance(empty, HistoryBuildSuccess) and empty.documents == {}
    assert empty.memory_domain_id == "u_local"
    absent = group(repo, identity("session"), q1, cut)
    assert isinstance(absent, HistoryBuildSuccess)
    assert absent.memory_domain_id is None and absent.documents == {}


def test_driver_failure_propagates_and_cut_is_required(
    repo, stage2_migrated_postgres_database
):
    guard = new_session(repo).guard
    owner, q1 = question(repo, guard)
    append(repo, guard, owner, "body")
    with repo.connection_provider.connection(
        lane=PostgresConnectionLane.MEMORY_QUERY, row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        with pytest.raises(TypeError, match="covered_entry_sequence"):
            build_history_document(connection, guard.session_id, q1)
    # Exercise the driver's actual closed-connection rejection, not a fake None.
    connection = psycopg.connect(stage2_migrated_postgres_database.runtime_dsn)
    connection.close()
    with pytest.raises(psycopg.OperationalError, match="closed"):
        build_history_group(
            connection, guard.session_id, q1, covered_entry_sequence=2
        )


def test_bad_tool_result_fails_group_but_not_requested_no_tools(
    repo, stage2_migrated_postgres_database
):
    from pulsara_agent.conversation_kernel.repository import build_prepared_tool_result_acceptance
    from pulsara_agent.conversation_kernel.tool_artifacts import ToolOutputArtifactProcessor
    from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
    from tests.test_round1_tool_output_artifact import _install_tool_call

    workspace = identity("workspace")
    lease, owner, request, call, attempt = _install_tool_call(repo, workspace)
    entry_id = identity("entry")
    projection = ToolOutputArtifactProcessor(repo.connection_provider).prepare(
        session_id=lease.guard.session_id, workspace_id=workspace,
        result_entry_id=entry_id, public_output="tool result 中文🧰",
        candidate=None, artifact_inline_result=False,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = build_prepared_tool_result_acceptance(
        guard=lease.guard, workspace_id=workspace,
        result_id=identity("result"), result_entry_id=entry_id, turn_id=owner,
        assistant_entry_id=request, tool_call_id=call, attempt_id=attempt,
        result_state="SUCCESS", canonical_preview_content=projection.canonical_preview,
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
    repo.accept_tool_result(
        lease.guard, candidate=candidate, deadline_monotonic=monotonic() + 30
    )
    _, cut = append(repo, lease.guard, owner, "ordinary complete no-tools answer")
    q1 = rows(repo, "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND entry_kind='USER_MESSAGE'", (lease.guard.session_id,))[0]["id"]
    expected = group(repo, lease.guard.session_id, q1, cut).documents[("QA", False)]
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn, row_factory=dict_row) as connection:
        connection.execute(
            "UPDATE pulsara_v3.transcript_entries SET inline_content=%s || substring(inline_content FROM 2) WHERE id=%s",
            (b"x", entry_id),
        )
        result = build_history_group(
            connection, lease.guard.session_id, q1, covered_entry_sequence=cut
        )
        assert isinstance(result, HistoryBuildFailure) and result.entry_id == entry_id
        assert build_history_document(
            connection, lease.guard.session_id, q1, covered_entry_sequence=cut,
            variant="QA", include_tools=False,
        ) == expected
        assert build_history_document(
            connection, lease.guard.session_id, q1, covered_entry_sequence=cut,
            variant="QA", include_tools=True,
        ) is None
        connection.rollback()


@pytest.mark.parametrize("field", ["q1_entry_id", "q2_entry_id"])
def test_projection_composite_FK_rejects_other_session_question(
    repo, stage2_migrated_postgres_database, field
):
    from psycopg import sql
    from pulsara_agent.conversation_kernel.history_publication import HistoryProjectionRepository

    guard = new_session(repo).guard
    owner, q1 = question(repo, guard)
    append(repo, guard, owner, "first complete answer", complete=True)
    question(repo, guard, "raw next question")
    foreign_guard = new_session(repo).guard
    _, foreign_q1 = question(repo, foreign_guard, "foreign session question")
    projection = HistoryProjectionRepository(repo.connection_provider)
    build = projection.build_question(
        guard.session_id, q1, deadline_monotonic=monotonic() + 30
    )
    assert isinstance(build, HistoryBuildSuccess) and len(build.documents) == 4
    assert projection.publish(build, deadline_monotonic=monotonic() + 30) == "published"
    before = rows(repo, "SELECT * FROM pulsara_v3.history_search_documents WHERE session_id=%s ORDER BY id", (guard.session_id,))
    # Schema, independently of the publisher's typed source qualification, owns
    # same-session Q1/Q2 foreign keys. No cross-session id can silently bind.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(stage2_migrated_postgres_database.runtime_dsn) as connection:
            connection.execute(
                sql.SQL("UPDATE pulsara_v3.history_search_documents SET {}=%s WHERE session_id=%s").format(sql.Identifier(field)),
                (foreign_q1, guard.session_id),
            )
    assert rows(repo, "SELECT * FROM pulsara_v3.history_search_documents WHERE session_id=%s ORDER BY id", (guard.session_id,)) == before
