"""The shared adapter against the current baseline and runtime grants."""

import psycopg
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.content_parts import (
    ResultPart,
    move_queue_content_parts,
    read_content_parts,
    write_content_parts,
)
from pulsara_agent.identity import random_id


pytestmark = pytest.mark.postgres


def test_complete_content_uses_real_blob_owner_and_sealed_runtime_rows(isolated_migrated_postgres_database):
    database = isolated_migrated_postgres_database
    session_id, workspace_id = random_id("session"), random_id("workspace")
    queue_id, replacement_id = random_id("queue"), random_id("queue")
    with psycopg.connect(database.admin_dsn) as admin:
        admin.execute("""INSERT INTO pulsara_v3.workspaces
                         (memory_domain_id,id,workspace_kind,workspace_root,workspace_label)
                         VALUES ('content-test',%s,'transient','/content-test','Content test')""", (workspace_id,))
        admin.execute("""INSERT INTO pulsara_v3.sessions
                         (id,workspace_id,memory_domain_id,lifecycle,writer_generation)
                         VALUES (%s,%s,'content-test','OPEN',1)""", (session_id, workspace_id))
        for sequence, queue_item_id in enumerate((queue_id, replacement_id), 1):
            admin.execute("""INSERT INTO pulsara_v3.prompt_queue_items
                             (id,session_id,workspace_id,queue_sequence,delivery_mode,input_origin,
                              source_session_id,model_call_binding,requested_permission_mode,status,
                              source_cell_id,source_record_sequence,cell_output_kind)
                             VALUES (%s,%s,%s,%s,'NEW_TURN','CELL_OUTPUT',%s,
                                     '{"connection_id":"test","reasoning":null}', 'read-only','PENDING',%s,1,'log')""",
                          (queue_item_id, session_id, workspace_id, sequence, session_id, random_id("cell")))
    large = "完整正文\n" + "x" * ((16 << 20) + 1)
    parts = [ResultPart("text", "first\n"), ResultPart("object", {"value": [None, 1, 1.25]}),
             ResultPart("target", {"kind": "PATH", "path": "/outside-workspace"}),
             ResultPart("text", large), ResultPart("text", "last")]
    with psycopg.connect(database.runtime_dsn, row_factory=dict_row) as runtime:
        accepted = write_content_parts(runtime, session_id=session_id, workspace_id=workspace_id,
                                       queue_item_id=queue_id, parts=parts)
        runtime.commit()
        restored = read_content_parts(runtime, queue_item_id=queue_id)
        assert restored == accepted
        assert [part.ordinal for part in restored] == list(range(5))
        payloads = runtime.execute("""SELECT p.type,p.inline_value,p.blob_id,b.logical_size,b.workspace_id
                                      FROM pulsara_v3.content_parts p
                                      LEFT JOIN pulsara_v3.blobs b ON b.id=p.blob_id
                                      WHERE p.queue_item_id=%s ORDER BY p.ordinal""", (queue_id,)).fetchall()
        assert payloads[0]["inline_value"] == b"first\n"
        assert payloads[3]["inline_value"] is None
        assert payloads[3]["logical_size"] == len(large.encode())
        assert payloads[3]["workspace_id"] == workspace_id
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.transaction():
                runtime.execute("UPDATE pulsara_v3.content_parts SET inline_value=%s WHERE id=%s",
                                (b"rewritten", accepted[0].id))
        # Having UPDATE on the owner columns does not permit arbitrary moves.
        with pytest.raises(psycopg.errors.CheckViolation, match="actual steer redirect"):
            with runtime.transaction():
                move_queue_content_parts(runtime, queue_item_id=queue_id,
                                         replacement_queue_item_id=replacement_id)
                runtime.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert read_content_parts(runtime, queue_item_id=queue_id) == accepted
