"""Observable clean-schema contracts independent of repository implementations."""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb
import pytest

from pulsara_agent.identity import random_id
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)

pytestmark = pytest.mark.postgres
DIGEST = "sha256:" + "0" * 64
BINDING = Jsonb({"connection_id": "schema-test", "reasoning": None})


@pytest.fixture
def schema_database():
    database = create_migrated_postgres_test_database()
    try:
        yield database
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


@contextmanager
def _session(database):
    with psycopg.connect(database.runtime_dsn, autocommit=True) as connection:
        workspace, session = random_id("workspace"), random_id("session")
        connection.execute(
            "INSERT INTO pulsara_v3.workspaces VALUES (%s,%s,'project',%s,'Schema test')",
            ("schema-test", workspace, "/tmp/" + workspace),
        )
        connection.execute(
            "INSERT INTO pulsara_v3.sessions (id,workspace_id,memory_domain_id,lifecycle,writer_generation) "
            "VALUES (%s,%s,'schema-test','OPEN',1)",
            (session, workspace),
        )
        yield connection, session, workspace


def _queue(c, session, workspace, *, steer=False, segments=()):
    queue, command = random_id("queue"), random_id("command")
    with c.transaction():
        c.execute(
            "INSERT INTO pulsara_v3.session_commands "
            "(session_id,command_id,command_kind,semantic_digest,target_kind,target_queue_item_id) "
            "VALUES (%s,%s,%s,%s,'QUEUE_ITEM',%s)",
            (
                session,
                command,
                "STEER_QUEUED_PROMPT" if steer else "QUEUE_PROMPT",
                DIGEST,
                queue,
            ),
        )
        sequence = c.execute(
            "SELECT COALESCE(max(queue_sequence),0)+1 FROM pulsara_v3.prompt_queue_items WHERE session_id=%s",
            (session,),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO pulsara_v3.prompt_queue_items "
            "(id,session_id,workspace_id,queue_sequence,command_id,client_submission_id,delivery_mode,input_origin,"
            "model_call_binding,requested_permission_mode,status,target_turn_id,work_inputs) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)",
            (
                queue,
                session,
                workspace,
                sequence,
                command,
                command,
                "STEER_ACTIVE_TURN" if steer else "NEW_TURN",
                "HUMAN_STEER" if steer else "HUMAN_MESSAGE",
                None if steer else BINDING,
                None if steer else "read-only",
                _turn(c, session, workspace)[0] if steer else None,
                Jsonb(list(segments)),
            ),
        )
    return queue


def _turn(c, session, workspace):
    turn, entry = random_id("turn"), random_id("entry")
    sequence = c.execute(
        "SELECT COALESCE(max(entry_sequence),0)+1 FROM pulsara_v3.transcript_entries WHERE session_id=%s",
        (session,),
    ).fetchone()[0]
    with c.transaction():
        c.execute(
            "INSERT INTO pulsara_v3.turns "
            "(id,session_id,workspace_id,model_call_binding,status,initial_entry_id,permission_snapshot_id,"
            "requested_permission_mode,effective_permission_mode,permission_admission_source,permission_overlay,"
            "permission_plan_context_ordinal,permission_snapshot_fingerprint) "
            "VALUES (%s,%s,%s,%s,'RUNNING',%s,%s,'read-only','read-only','USER_SUBMISSION','NONE',0,%s)",
            (turn, session, workspace, BINDING, entry, random_id("permission"), DIGEST),
        )
        c.execute(
            "INSERT INTO pulsara_v3.transcript_entries "
            "(id,origin_entry_id,session_id,workspace_id,entry_owner_kind,turn_id,entry_sequence,entry_kind,input_origin) "
            "VALUES (%s,%s,%s,%s,'EXECUTED_TURN',%s,%s,'USER_MESSAGE','HUMAN_MESSAGE')",
            (entry, entry, session, workspace, turn, sequence),
        )
    return turn, entry


def _part(c, session, workspace, *, queue=None, entry=None, ordinal=0, **kwargs):
    values = dict(
        id=random_id("block"),
        session_id=session,
        workspace_id=workspace,
        queue_item_id=queue,
        entry_id=entry,
        ordinal=ordinal,
        type="text",
        inline_value=b"first\nsecond\n",
    )
    values.update(kwargs)
    columns = ",".join(values)
    c.execute(
        f"INSERT INTO pulsara_v3.content_parts ({columns}) VALUES ({','.join(['%s'] * len(values))})",
        tuple(values.values()),
    )
    return values["id"]


def test_content_owner_transfer_preserves_identity_and_runtime_cannot_edit_payload(
    schema_database,
):
    with _session(schema_database) as (c, session, workspace):
        queue = _queue(c, session, workspace)
        part = _part(c, session, workspace, queue=queue)
        turn, entry = _turn(c, session, workspace)
        with c.transaction():
            c.execute(
                "UPDATE pulsara_v3.content_parts SET queue_item_id=NULL,entry_id=%s WHERE id=%s",
                (entry, part),
            )
            c.execute(
                "UPDATE pulsara_v3.prompt_queue_items SET status='CONSUMED',consumed_entry_id=%s,terminal_at=clock_timestamp() WHERE id=%s",
                (entry, queue),
            )
        assert c.execute(
            "SELECT id,ordinal,inline_value,entry_id,queue_item_id FROM pulsara_v3.content_parts WHERE id=%s",
            (part,),
        ).fetchone() == (part, 0, b"first\nsecond\n", entry, None)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "UPDATE pulsara_v3.content_parts SET inline_value=%s WHERE id=%s",
                (b"edited", part),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.content_parts SET entry_id=NULL,queue_item_id=%s WHERE id=%s",
                (queue, part),
            )


def test_invalid_owner_transfer_rolls_back_whole_transaction(schema_database):
    with _session(schema_database) as (c, session, workspace):
        queue = _queue(c, session, workspace)
        part = _part(c, session, workspace, queue=queue)
        _, entry = _turn(c, session, workspace)
        with pytest.raises(psycopg.errors.CheckViolation):
            with c.transaction():
                c.execute(
                    "UPDATE pulsara_v3.content_parts SET entry_id=%s,queue_item_id=NULL WHERE id=%s",
                    (entry, part),
                )
        assert c.execute(
            "SELECT queue_item_id,entry_id FROM pulsara_v3.content_parts WHERE id=%s",
            (part,),
        ).fetchone() == (queue, None)


def test_steer_redirect_reuses_parts_with_command_and_terminal_facts(schema_database):
    with _session(schema_database) as (c, session, workspace):
        source = _queue(c, session, workspace)
        target = _queue(c, session, workspace, steer=True)
        part = _part(c, session, workspace, queue=source)
        with c.transaction():
            c.execute(
                "UPDATE pulsara_v3.content_parts SET queue_item_id=%s WHERE id=%s",
                (target, part),
            )
            c.execute(
                "UPDATE pulsara_v3.prompt_queue_items SET status='CANCELLED',terminal_reason='USER_REDIRECTED_TO_STEER',terminal_at=clock_timestamp() WHERE id=%s",
                (source,),
            )
        assert c.execute(
            "SELECT id,queue_item_id FROM pulsara_v3.content_parts WHERE id=%s", (part,)
        ).fetchone() == (part, target)


@pytest.mark.parametrize(
    "extra",
    [
        dict(entry_id="entry:aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        dict(queue_item_id=None),
        dict(inline_value=None),
        dict(blob_id="blob:aaaaaaaaaaaaaaaaaaaaaaaaaa"),
    ],
)
def test_content_rejects_invalid_owner_and_payload_combinations(schema_database, extra):
    with _session(schema_database) as (c, session, workspace):
        queue = _queue(c, session, workspace)
        with pytest.raises(
            (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)
        ):
            _part(c, session, workspace, queue=queue, **extra)


def test_tool_call_requires_assistant_request_and_unique_owner_ordinal(schema_database):
    with _session(schema_database) as (c, session, workspace):
        queue = _queue(c, session, workspace)
        _part(c, session, workspace, queue=queue)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _part(c, session, workspace, queue=queue)
        with pytest.raises(psycopg.errors.CheckViolation):
            _part(
                c,
                session,
                workspace,
                queue=queue,
                ordinal=1,
                type="tool_call",
                inline_value=None,
                tool_call_id="provider-call",
                tool_name="todo",
                tool_arguments=Jsonb({}),
            )
        _, entry = _turn(c, session, workspace)
        with pytest.raises(psycopg.errors.CheckViolation):
            _part(
                c,
                session,
                workspace,
                entry=entry,
                type="tool_call",
                inline_value=None,
                tool_call_id="provider-call",
                tool_name="todo",
                tool_arguments=Jsonb({}),
            )


def test_memory_title_kind_preference_and_microsecond_mutation_contract(
    schema_database,
):
    with _session(schema_database) as (c, session, workspace):
        fact = random_id("memory")
        pref = random_id("memory")
        for identity, kind, statement in [
            (fact, "FACT", "first body"),
            (pref, "RESPONSE_PREFERENCE", "discarded" * 9000),
        ]:
            c.execute(
                "INSERT INTO pulsara_v3.memory_facts (id,memory_domain_id,context_id,fact_kind,title,statement) VALUES (%s,%s,%s,%s,%s,%s)",
                (identity, "schema-test", "ctx:global", kind, "Same title", statement),
            )
        assert (
            c.execute(
                "SELECT statement FROM pulsara_v3.memory_facts WHERE id=%s", (pref,)
            ).fetchone()[0]
            == ""
        )
        original = c.execute(
            "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s", (pref,)
        ).fetchone()[0]
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET statement=%s WHERE id=%s",
            ("ignored again", pref),
        )
        assert (
            c.execute(
                "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s", (pref,)
            ).fetchone()[0]
            == original
        )
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET context_id=%s WHERE id=%s",
            (workspace, pref),
        )
        assert (
            c.execute(
                "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s", (pref,)
            ).fetchone()[0]
            > original
        )
        for field in ["title", "fact_kind"]:
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(
                    f"UPDATE pulsara_v3.memory_facts SET {field}=%s WHERE id=%s",
                    ("changed" if field == "title" else "FACT", pref),
                )
        before = c.execute(
            "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s", (fact,)
        ).fetchone()[0]
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET statement='next body' WHERE id=%s",
            (fact,),
        )
        after = c.execute(
            "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s", (fact,)
        ).fetchone()[0]
        assert (after - before).total_seconds() >= 0.000001
        assert (
            c.execute(
                "SELECT search_text FROM pulsara_v3.memory_facts WHERE id=%s", (fact,)
            ).fetchone()[0]
            == "title: Same title\nstatement: next body"
        )


def _memory(c, *, title, statement, kind="FACT"):
    identity = random_id("memory")
    c.execute(
        "INSERT INTO pulsara_v3.memory_facts "
        "(id,memory_domain_id,context_id,fact_kind,title,statement) "
        "VALUES (%s,'schema-test','ctx:global',%s,%s,%s)",
        (identity, kind, title, statement),
    )
    return identity


def test_memory_runtime_writes_normalize_unicode_and_preserve_internal_layout(
    schema_database,
):
    from pulsara_agent.conversation_kernel.memory.contracts import normalize_memory_text

    # Include nonbreaking spaces, the Unicode separators, and Python strip's
    # C0 information separators; normalization must not depend on DB locale.
    whitespace = (
        "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
        "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
        "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
    )
    with _session(schema_database) as (c, _, _):
        title = whitespace + "Cafe\u0301 🙂" + whitespace
        body = whitespace + "e\u0301\r\n  indented\r\tend" + whitespace
        identity = _memory(c, title=title, statement=body)
        stored = c.execute(
            "SELECT title,statement,search_text FROM pulsara_v3.memory_facts WHERE id=%s",
            (identity,),
        ).fetchone()
        assert stored == (
            "Café 🙂",
            "é\n  indented\n\tend",
            "title: Café 🙂\nstatement: é\n  indented\n\tend",
        )
        assert stored[:2] == (normalize_memory_text(title), normalize_memory_text(body))
        for char in whitespace:
            for values in [
                dict(title=char, statement="body"),
                dict(title="title", statement=char),
            ]:
                with pytest.raises(psycopg.errors.CheckViolation):
                    _memory(c, **values)
        for separator in [
            "\n",
            "\r",
            "\r\n",
            "\v",
            "\f",
            "\x1c",
            "\x1d",
            "\x1e",
            "\x85",
            "\u2028",
            "\u2029",
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                _memory(c, title="first" + separator + "second", statement="body")
            # These boundaries remain valid inside a multiline statement.
            identity = _memory(
                c, title="valid\ttitle", statement="first" + separator + "  second"
            )
            assert c.execute(
                "SELECT title,statement FROM pulsara_v3.memory_facts WHERE id=%s",
                (identity,),
            ).fetchone() == (
                "valid\ttitle",
                normalize_memory_text("first" + separator + "  second"),
            )
        # Neither character is whitespace in the existing Python owner.
        identity = _memory(c, title="\u200btitle\ufeff", statement="\u200bbody\ufeff")
        assert c.execute(
            "SELECT title,statement FROM pulsara_v3.memory_facts WHERE id=%s",
            (identity,),
        ).fetchone() == ("\u200btitle\ufeff", "\u200bbody\ufeff")


def test_memory_limits_count_normalized_unicode_codepoints(schema_database):
    # The memory contract defines 128/8000 code points after NFC and trimming,
    # not UTF-8 bytes or UTF-16 units. NFD inputs exceed the raw code-point limits.
    with _session(schema_database) as (c, _, _):
        identity = _memory(
            c,
            title="\t" + "e\u0301" * 127 + "🙂\xa0",
            statement="\u3000" + "e\u0301" * 7999 + "🙂\r\n",
        )
        assert c.execute(
            "SELECT title,statement FROM pulsara_v3.memory_facts WHERE id=%s",
            (identity,),
        ).fetchone() == ("é" * 127 + "🙂", "é" * 7999 + "🙂")
        for values in [
            dict(title="é" * 128 + "🙂", statement="body"),
            dict(title="title", statement="é" * 8000 + "🙂"),
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                _memory(c, **values)
        preference = _memory(
            c,
            title="\tPrefer concise answers\xa0",
            statement="e\u0301\r\n" * 9000,
            kind="RESPONSE_PREFERENCE",
        )
        assert c.execute(
            "SELECT title,statement FROM pulsara_v3.memory_facts WHERE id=%s",
            (preference,),
        ).fetchone() == ("Prefer concise answers", "")


def test_memory_normalizes_before_identity_and_noop_timestamp_checks(schema_database):
    with _session(schema_database) as (c, _, workspace):
        identity = _memory(c, title="Café", statement="é\n  indented")
        original = c.execute(
            "SELECT accepted_at,updated_at FROM pulsara_v3.memory_facts WHERE id=%s",
            (identity,),
        ).fetchone()
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET title=%s,statement=%s WHERE id=%s",
            ("\tCafe\u0301\xa0", "\u3000e\u0301\r\n  indented\r\n", identity),
        )
        assert c.execute(
            "SELECT title,statement,accepted_at,updated_at FROM pulsara_v3.memory_facts WHERE id=%s",
            (identity,),
        ).fetchone() == ("Café", "é\n  indented", *original)
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.memory_facts SET title=%s WHERE id=%s",
                (" Café changed ", identity),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.memory_facts SET fact_kind=%s WHERE id=%s",
                ("USER_PROFILE", identity),
            )
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET statement=%s WHERE id=%s",
            ("\tchanged\r\n  body\xa0", identity),
        )
        changed = c.execute(
            "SELECT title,statement,accepted_at,updated_at FROM pulsara_v3.memory_facts WHERE id=%s",
            (identity,),
        ).fetchone()
        assert changed[:3] == ("Café", "changed\n  body", original[0])
        assert (changed[3] - original[1]).total_seconds() >= 0.000001
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET context_id=%s WHERE id=%s",
            (workspace, identity),
        )
        scoped_at = c.execute(
            "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s", (identity,)
        ).fetchone()[0]
        assert (scoped_at - changed[3]).total_seconds() >= 0.000001
        c.execute(
            "UPDATE pulsara_v3.memory_facts SET statement=%s,context_id=%s WHERE id=%s",
            (" changed\r  body\r\n", workspace, identity),
        )
        assert (
            c.execute(
                "SELECT updated_at FROM pulsara_v3.memory_facts WHERE id=%s",
                (identity,),
            ).fetchone()[0]
            == scoped_at
        )


def _plan_review(c, session, workspace):
    turn, initial = _turn(c, session, workspace)
    workflow, enter = random_id("plan"), random_id("command")
    binding, assistant = random_id("binding"), random_id("entry")
    call, interaction = random_id("call"), random_id("plan-interaction")
    with c.transaction():
        c.execute(
            "INSERT INTO pulsara_v3.session_commands (session_id,command_id,command_kind,semantic_digest,target_kind,target_plan_workflow_id) VALUES (%s,%s,'ENTER_PLAN',%s,'PLAN_WORKFLOW',%s)",
            (session, enter, DIGEST, workflow),
        )
        c.execute(
            "INSERT INTO pulsara_v3.plan_workflows (id,session_id,workspace_id,workflow_ordinal,status,entered_by,entry_command_id,resume_permission_mode,workflow_revision) VALUES (%s,%s,%s,1,'ACTIVE','USER',%s,'accept-edits',1)",
            (workflow, session, workspace, enter),
        )
        c.execute(
            "INSERT INTO pulsara_v3.turn_context_binding_revisions (id,session_id,turn_id,revision_ordinal,base_kind,source_through_sequence) VALUES (%s,%s,%s,0,'FULL_HISTORY',0)",
            (binding, session, turn),
        )
        c.execute(
            "INSERT INTO pulsara_v3.transcript_entries (id,origin_entry_id,session_id,workspace_id,entry_owner_kind,turn_id,entry_sequence,entry_kind,context_binding_revision_id,provider_input_through_sequence) VALUES (%s,%s,%s,%s,'EXECUTED_TURN',%s,2,'ASSISTANT_TOOL_REQUEST',%s,1)",
            (assistant, assistant, session, workspace, turn, binding),
        )
        _part(
            c,
            session,
            workspace,
            entry=assistant,
            type="tool_call",
            inline_value=None,
            tool_call_id="review-call",
            tool_name="exit_plan",
            tool_arguments=Jsonb({"plan": "# Complete original plan\n\nDo the work."}),
        )
        c.execute(
            "INSERT INTO pulsara_v3.tool_invocations (id,session_id,turn_id,source_kind,assistant_entry_id,tool_call_id) VALUES (%s,%s,%s,'MODEL_TOOL_CALL',%s,'review-call')",
            (call, session, turn, assistant),
        )
        c.execute(
            "INSERT INTO pulsara_v3.plan_interactions (id,session_id,workspace_id,plan_workflow_id,interaction_ordinal,kind,status,origin_turn_id,assistant_entry_id,tool_call_id,request_semantic_digest) VALUES (%s,%s,%s,%s,1,'DRAFT_REVIEW','OPEN',%s,%s,'review-call',%s)",
            (interaction, session, workspace, workflow, turn, assistant, DIGEST),
        )
    return turn, workflow, assistant, call, interaction


def _review_result(c, session, workspace, review, status):
    turn, workflow, assistant, call, interaction = review
    result, entry, command = (
        random_id("result"),
        random_id("entry"),
        random_id("command"),
    )
    with c.transaction():
        c.execute(
            "INSERT INTO pulsara_v3.transcript_entries (id,origin_entry_id,session_id,workspace_id,entry_owner_kind,turn_id,entry_sequence,entry_kind) VALUES (%s,%s,%s,%s,'EXECUTED_TURN',%s,3,'TOOL_RESULT')",
            (entry, entry, session, workspace, turn),
        )
        c.execute(
            "INSERT INTO pulsara_v3.session_commands (session_id,command_id,command_kind,semantic_digest,target_kind,target_plan_interaction_id) VALUES (%s,%s,'RESOLVE_PLAN_INTERACTION',%s,'PLAN_INTERACTION',%s)",
            (session, command, DIGEST, interaction),
        )
        c.execute(
            "INSERT INTO pulsara_v3.tool_results (id,session_id,workspace_id,invocation_id,tool_call_entry_id,tool_call_id,result_record_kind,result_origin_kind,control_plan_interaction_id,permission_snapshot_fingerprint,result_entry_id,result_state,observed_at,observation_origin_kind) VALUES (%s,%s,%s,%s,%s,'review-call','EXECUTED','PLAN_CONTROL',%s,%s,%s,%s,clock_timestamp(),'PLAN_CONTROL')",
            (
                result,
                session,
                workspace,
                call,
                assistant,
                interaction,
                DIGEST,
                entry,
                "CANCELLED" if status == "CANCELLED" else "SUCCESS",
            ),
        )
        c.execute(
            "UPDATE pulsara_v3.plan_interactions SET status=%s,control_tool_result_id=%s,resolution_command_id=%s,response_semantic_digest=%s,resolved_at=clock_timestamp() WHERE id=%s",
            (status, result, command, DIGEST, interaction),
        )
    return result


def test_open_plan_review_has_no_result_and_keeps_original_turn_running(
    schema_database,
):
    with _session(schema_database) as (c, session, workspace):
        turn, _, _, _, interaction = _plan_review(c, session, workspace)
        assert c.execute(
            "SELECT status,control_tool_result_id FROM pulsara_v3.plan_interactions WHERE id=%s",
            (interaction,),
        ).fetchone() == ("OPEN", None)
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.turns SET status='COMPLETED',terminal_at=clock_timestamp() WHERE id=%s",
                (turn,),
            )
        assert (
            c.execute(
                "SELECT status FROM pulsara_v3.turns WHERE id=%s", (turn,)
            ).fetchone()[0]
            == "RUNNING"
        )


@pytest.mark.parametrize("status", ["REVISION_REQUESTED", "CANCELLED"])
def test_plan_revision_and_cancel_settle_once_in_same_running_turn(
    schema_database, status
):
    with _session(schema_database) as (c, session, workspace):
        review = _plan_review(c, session, workspace)
        result = _review_result(c, session, workspace, review, status)
        turn, workflow, _, _, interaction = review
        assert c.execute(
            "SELECT status,control_tool_result_id,decision_continuation_entry_id FROM pulsara_v3.plan_interactions WHERE id=%s",
            (interaction,),
        ).fetchone() == (status, result, None)
        assert (
            c.execute(
                "SELECT status FROM pulsara_v3.turns WHERE id=%s", (turn,)
            ).fetchone()[0]
            == "RUNNING"
        )
        assert (
            c.execute(
                "SELECT status FROM pulsara_v3.plan_workflows WHERE id=%s", (workflow,)
            ).fetchone()[0]
            == "ACTIVE"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM pulsara_v3.tool_results WHERE control_plan_interaction_id=%s",
                (interaction,),
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.plan_interactions SET resolved_at=clock_timestamp() WHERE id=%s",
                (interaction,),
            )


def test_agent_material_metadata_preserves_sparse_dependency_positions(schema_database):
    with _session(schema_database) as (c, session, workspace):
        queue = random_id("queue")
        metadata = [
            {
                "kind": "instruction",
                "ordinal": 0,
                "content_start": 0,
                "content_count": 1,
            },
            {"kind": "context", "ordinal": 4, "content_start": 2, "content_count": 0},
            {
                "kind": "dependency",
                "ordinal": 9,
                "content_start": 3,
                "content_count": 0,
                "source_work_id": random_id("queue"),
                "ready": False,
            },
            {
                "kind": "material",
                "ordinal": 3,
                "content_start": 4,
                "content_count": 1,
                "source_work_id": random_id("queue"),
                "ready": True,
            },
        ]
        c.execute(
            "INSERT INTO pulsara_v3.prompt_queue_items (id,session_id,workspace_id,queue_sequence,delivery_mode,input_origin,source_session_id,work_inputs,model_call_binding,permission_snapshot_id,requested_permission_mode,effective_permission_mode,permission_admission_source,permission_overlay,permission_plan_context_ordinal,permission_snapshot_fingerprint,status) VALUES (%s,%s,%s,1,'NEW_TURN','AGENT_INSTRUCTION',%s,%s,%s,%s,'read-only','read-only','USER_SUBMISSION','NONE',0,%s,'PENDING')",
            (
                queue,
                session,
                workspace,
                session,
                Jsonb(metadata),
                BINDING,
                random_id("permission"),
                DIGEST,
            ),
        )
        _part(c, session, workspace, queue=queue)
        _part(c, session, workspace, queue=queue, ordinal=4)
        assert c.execute(
            "SELECT ordinal FROM pulsara_v3.content_parts WHERE queue_item_id=%s ORDER BY ordinal",
            (queue,),
        ).fetchall() == [(0,), (4,)]
        invalid = [dict(item) for item in metadata]
        invalid[-1]["body"] = "second body authority"
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.prompt_queue_items SET work_inputs=%s WHERE id=%s",
                (Jsonb(invalid), queue),
            )
        invalid = [dict(item) for item in metadata]
        invalid[-1]["content_start"] = 0
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE pulsara_v3.prompt_queue_items SET work_inputs=%s WHERE id=%s",
                (Jsonb(invalid), queue),
            )


def test_protocol_projects_shared_parts_open_review_and_final_result(schema_database):
    from psycopg.rows import dict_row
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader

    with _session(schema_database) as (c, session, workspace):
        review = _plan_review(c, session, workspace)
        queue = _queue(c, session, workspace)
        _part(c, session, workspace, queue=queue)
        reader = CanonicalProtocolReader(None)
        with psycopg.connect(
            schema_database.runtime_dsn, autocommit=True, row_factory=dict_row
        ) as projection:
            control = reader._control(
                projection,
                session_id=session,
                lifecycle="OPEN",
                maximum_items=64,
                entry_sequence_floor=1,
            )
            assert control.open_plan_interaction.interaction_id == review[-1]
            assert control.open_plan_interaction.status == "OPEN"
            assert control.prompt_queue[0].queue_item_id == queue
            assert (
                b"first\\nsecond\\n" in control.prompt_queue[0].content.inline_content
            )
            assistant_row = projection.execute(
                "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (review[2],)
            ).fetchone()
            parts = reader._assistant_parts_batch(projection, session, [review[2]])[
                review[2]
            ]
            entry = reader._entry(projection, assistant_row, assistant_parts=parts)
            assert entry.blocks[0].block_kind == "TOOL_CALL"
            assert entry.blocks[0].tool_name == "exit_plan"
        result = _review_result(c, session, workspace, review, "CANCELLED")
        with psycopg.connect(
            schema_database.runtime_dsn, autocommit=True, row_factory=dict_row
        ) as projection:
            result_row = projection.execute(
                "SELECT e.* FROM pulsara_v3.transcript_entries e JOIN pulsara_v3.tool_results r ON r.result_entry_id=e.id WHERE r.id=%s",
                (result,),
            ).fetchone()
            relations = reader._entry_relations(projection, [result_row])
            entry = reader._entry(projection, result_row, relations=relations)
            assert entry.tool_result.result_state == "CANCELLED"
            assert entry.tool_result.output_body_available
            assert b"CANCELLED" in entry.content.inline_content
