"""Clean-v0 must enforce identities even for direct database writers."""

import psycopg
from psycopg import sql
from psycopg.types.json import Json, Jsonb
import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.tool_invocations import (
    ToolInvocationSource,
    ToolInvocationSourceKind,
)


@pytest.mark.postgres
def test_clean_v0_identity_checks_reject_malformed_rows(
    isolated_migrated_postgres_database,
):
    db = isolated_migrated_postgres_database
    with psycopg.connect(db.admin_dsn) as conn:
        for statement, values, expected in (
            (
                """INSERT INTO pulsara_v3.tool_invocations
               (id,session_id,turn_id,source_kind,assistant_entry_id,tool_call_id)
               VALUES (%s,%s,%s,'MODEL_TOOL_CALL',%s,'external-provider-id')""",
                (
                    "call:bad",
                    random_id("session"),
                    random_id("turn"),
                    random_id("entry"),
                ),
                "tool_invocations_id_check",
            ),
            (
                """INSERT INTO pulsara_v3.tool_execution_attempts
               (id,session_id,invocation_id,tool_call_id,authorization_kind,authorization_reference,
                permission_snapshot_fingerprint,actor_kind,actor_id,remote_identity,remote_identity_published_at)
               VALUES (%s,%s,%s,'external-provider-id','policy','trusted',%s,'runtime','test',%s,now())""",
                (
                    random_id("attempt"),
                    random_id("session"),
                    random_id("call"),
                    "sha256:" + "a" * 64,
                    random_id("mcp-tool"),
                ),
                "tool_execution_attempts_check2",
            ),
        ):
            with pytest.raises(psycopg.errors.CheckViolation) as error:
                with conn.transaction():
                    conn.execute(statement, values)
            assert error.value.diag.constraint_name == expected


def test_invocation_validates_local_identities_without_constraining_provider_id():
    valid = dict(
        invocation_id=random_id("call"),
        kind=ToolInvocationSourceKind.MODEL_TOOL_CALL,
        turn_id=random_id("turn"),
        assistant_entry_id=random_id("entry"),
        tool_call_id="provider/opaque call id",
    )
    assert ToolInvocationSource(**valid).tool_call_id == "provider/opaque call id"
    for field, invalid in [
        ("invocation_id", "call:bad"),
        ("turn_id", random_id("entry")),
        ("assistant_entry_id", random_id("turn")),
    ]:
        with pytest.raises(ValueError):
            ToolInvocationSource(**(valid | {field: invalid}))


@pytest.mark.postgres
def test_result_identity_and_public_value_shapes_reject_invalid_direct_writes(
    isolated_migrated_postgres_database,
):
    from psycopg.rows import dict_row
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from tests.support.postgres import verified_postgres_provider
    from tests.test_conversation_fork import new_session
    from tests.test_entry_projection_batch import _tool_outcome

    db = isolated_migrated_postgres_database
    repository = ConversationKernelRepository(
        verified_postgres_provider(db.runtime_dsn)
    )
    lease = new_session(repository)
    entry_id = _tool_outcome(repository, lease)
    with psycopg.connect(db.admin_dsn, row_factory=dict_row) as connection:
        accepted = connection.execute(
            "SELECT * FROM pulsara_v3.tool_results WHERE session_id=%s AND result_entry_id=%s",
            (lease.guard.session_id, entry_id),
        ).fetchone()
        definitions = {
            row["conname"]: row["definition"]
            for row in connection.execute(
                "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid='pulsara_v3.tool_results'::regclass AND contype='c'"
            )
        }
        for changes, definition_fragment in (
            ({"id": "result:bad"}, "^result:"),
            ({"id": "artifact:" + "a" * 26}, "^result:"),
            ({"public_fields": []}, "json_typeof(public_fields)"),
            ({"result_facts": []}, "jsonb_typeof(result_facts)"),
        ):
            candidate = accepted | changes
            candidate["public_fields"] = Json(candidate["public_fields"])
            candidate["result_facts"] = Jsonb(candidate["result_facts"])
            candidate["native_projection"] = None
            columns = sql.SQL(", ").join(map(sql.Identifier, candidate))
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in candidate)
            with pytest.raises(psycopg.errors.CheckViolation) as failure:
                with connection.transaction():
                    connection.execute(
                        sql.SQL(
                            "INSERT INTO pulsara_v3.tool_results ({}) VALUES ({})"
                        ).format(columns, placeholders),
                        tuple(candidate.values()),
                    )
            assert (
                definition_fragment in definitions[failure.value.diag.constraint_name]
            )
