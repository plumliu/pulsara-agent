"""Read only dogfood DBs whose canonical workspace belongs to this scratch."""

import json
from pathlib import Path
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

scratch = Path(sys.argv[1])
assert str(scratch).startswith("/home/plumwsl/.pvt-")
options = dict(
    host="/home/plumwsl/.pulsara/desktop/socket", port=5432, user="pulsara_admin"
)
with psycopg.connect(make_conninfo(dbname="pulsara", **options)) as connection:
    assert (
        connection.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
        == "7700523439330484737"
    )
    names = connection.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE 'pulsara_wsl_dogfood_%'"
    ).fetchall()
for (name,) in names:
    with psycopg.connect(make_conninfo(dbname=name, **options)) as connection:
        workspace = connection.execute(
            "SELECT workspace_root FROM pulsara_v3.workspaces WHERE workspace_root LIKE %s",
            (str(scratch) + "/%",),
        ).fetchall()
        if not workspace:
            continue
        counts = {}
        for table in ("subagent_tasks", "turns"):
            counts[table] = connection.execute(
                sql.SQL(
                    "SELECT status,count(*) FROM pulsara_v3.{} GROUP BY status"
                ).format(sql.Identifier(table))
            ).fetchall()
        from psycopg.rows import dict_row

        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT id,latest_entry_sequence,model_call_binding FROM pulsara_v3.sessions"
            )
            sessions = cursor.fetchall()
            cursor.execute(
                "SELECT id,status,conversation_scope_kind,scope_subagent_task_id FROM pulsara_v3.turns"
            )
            turns = cursor.fetchall()
            cursor.execute(
                "SELECT command_id,command_kind,target_turn_id FROM pulsara_v3.session_commands"
            )
            commands = cursor.fetchall()
        print(
            json.dumps(
                dict(
                    database=name,
                    workspaces=workspace,
                    counts=counts,
                    sessions=sessions,
                    turns=turns,
                    commands=commands,
                ),
                ensure_ascii=False,
                default=str,
            )
        )
