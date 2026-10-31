"""Clean-v0 must enforce identities even for direct database writers."""
from datetime import datetime, timezone

import psycopg
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
            ("""INSERT INTO pulsara_v3.tool_results
               (id,session_id,workspace_id,tool_call_id,result_record_kind,result_origin_kind,
                result_entry_id,result_state,observed_at,observation_origin_kind,
                output_artifact_disposition,output_artifact_id,output_artifact_blob_id)
               VALUES (%s,%s,%s,'external-provider-id','IMPORTED_HISTORY','PHYSICAL_ATTEMPT',
                       %s,'SUCCESS',%s,'BUILTIN','AVAILABLE','artifact:bad',%s)""",
             (random_id('result'), random_id('session'), random_id('workspace'), random_id('entry'),
              datetime.now(timezone.utc), random_id('blob')), 'tool_results_output_artifact_id_check'),
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
