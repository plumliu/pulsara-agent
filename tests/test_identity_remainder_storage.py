"""Clean-v0 must enforce identities even for direct database writers."""

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.tool_invocations import ToolInvocationSource, ToolInvocationSourceKind


@pytest.mark.postgres
def test_clean_v0_identity_checks_reject_malformed_rows(isolated_migrated_postgres_database):
    db = isolated_migrated_postgres_database
    with psycopg.connect(db.admin_dsn) as conn:
        for statement, values, expected in (
            ("""INSERT INTO pulsara_v3.tool_invocations
               (id,session_id,turn_id,source_kind,assistant_entry_id,tool_call_id)
               VALUES (%s,%s,%s,'MODEL_TOOL_CALL',%s,'external-provider-id')""",
             ('call:bad', random_id('session'), random_id('turn'), random_id('entry')),
             'tool_invocations_id_check'),
            ("""INSERT INTO pulsara_v3.tool_execution_attempts
               (id,session_id,invocation_id,tool_call_id,authorization_kind,authorization_reference,
                permission_snapshot_fingerprint,actor_kind,actor_id,remote_identity,remote_identity_published_at)
               VALUES (%s,%s,%s,'external-provider-id','policy','trusted',%s,'runtime','test',%s,now())""",
             (random_id('attempt'), random_id('session'), random_id('call'), 'sha256:'+'a'*64,
              random_id('mcp-tool')), 'tool_execution_attempts_check2'),
        ):
            with pytest.raises(psycopg.errors.CheckViolation) as error:
                with conn.transaction():
                    conn.execute(statement, values)
            assert error.value.diag.constraint_name == expected


def test_invocation_validates_local_identities_without_constraining_provider_id():
    valid = dict(invocation_id=random_id('call'), kind=ToolInvocationSourceKind.MODEL_TOOL_CALL,
                 turn_id=random_id('turn'), assistant_entry_id=random_id('entry'),
                 tool_call_id='provider/opaque call id')
    assert ToolInvocationSource(**valid).tool_call_id == 'provider/opaque call id'
    for field, invalid in [('invocation_id', 'call:bad'), ('turn_id', random_id('entry')),
                           ('assistant_entry_id', random_id('turn'))]:
        with pytest.raises(ValueError):
            ToolInvocationSource(**(valid | {field: invalid}))


@pytest.mark.postgres
def test_result_body_source_and_identity_constraints_replace_removed_artifact_identity(
    isolated_migrated_postgres_database,
):
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
    from tests.support.postgres import verified_postgres_provider
    from tests.test_conversation_fork import new_session
    from tests.test_entry_projection_batch import _tool_outcome

    db = isolated_migrated_postgres_database
    repository = ConversationKernelRepository(verified_postgres_provider(db.runtime_dsn))
    lease = new_session(repository)
    entry_id = _tool_outcome(repository, lease)
    with psycopg.connect(db.admin_dsn) as connection:
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='pulsara_v3' AND table_name='tool_results'"
        )}
        assert {"output_body_inline_content", "output_body_blob_id", "output_body_digest",
            "output_body_size", "output_body_media_type", "output_body_codec",
            "output_source_coverage", "output_source_coverage_reason",
            "output_unavailability_reason", "output_omitted", "result_facts"} <= columns
        assert not any("artifact" in column for column in columns)
        assert not any("artifact" in row[0] for row in connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='pulsara_v3' AND tablename='tool_results'"
        ))
        definitions = dict(connection.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='pulsara_v3.tool_results'::regclass AND contype='c'"
        ))
        # Mutate an otherwise valid accepted result. Each failed update must be
        # the relevant local constraint, not an unrelated FK or owner failure.
        for changes, definition_fragment in (
            ({"id": "result:bad"}, "^result:"),
            ({"id": "artifact:" + "a" * 26}, "^result:"),
            ({"output_body_size": 8}, "octet_length(output_body_inline_content)"),
            ({"output_body_codec": "latin-1"}, "output_body_codec"),
            ({"output_body_digest": "sha256:bad"}, "output_body_digest"),
            ({"result_facts": Jsonb([])}, "jsonb_typeof(result_facts)"),
            ({"output_source_coverage": "RETAINED_SNAPSHOT", "output_source_coverage_reason": None},
             "output_source_coverage"),
            ({"output_source_coverage": "COMPLETE", "output_source_coverage_reason": "TERMINAL_RETENTION_GAP"},
             "output_source_coverage"),
            ({"output_unavailability_reason": "BODY_PUBLICATION_FAILED"}, "output_body_inline_content"),
        ):
            assignments = sql.SQL(", ").join(
                sql.SQL("{}=%s").format(sql.Identifier(field)) for field in changes
            )
            try:
                with connection.transaction():
                    connection.execute(sql.SQL(
                        "UPDATE pulsara_v3.tool_results SET {} WHERE session_id=%s AND result_entry_id=%s"
                    ).format(assignments), (*changes.values(), lease.guard.session_id, entry_id))
            except psycopg.errors.CheckViolation as failure:
                assert definition_fragment in definitions[failure.diag.constraint_name]
            else:
                pytest.fail(f"canonical result constraints accepted invalid fields: {changes!r}")
