"""Read-only canonical writer and PostgreSQL identity after the failed Quit."""
import json
from pathlib import Path
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

home = Path("/home/plumwsl/.pulsara")
result = {"observed_at": datetime.now(timezone.utc).isoformat()}
with psycopg.connect(f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin", row_factory=dict_row) as connection:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    result["identity"] = dict(connection.execute(
        "SELECT system_identifier::text, current_setting('data_directory') AS data,"
        "current_setting('unix_socket_directories') AS socket,pg_postmaster_start_time()::text AS postmaster_start "
        "FROM pg_control_system()"
    ).fetchone())
    assert result["identity"]["system_identifier"] == "7700523439330484737"
    result["sessions"] = list(map(dict, connection.execute(
        "SELECT id,writer_generation,writer_lease_owner_id,writer_lease_expires_at::text,"
        "writer_lease_expires_at > clock_timestamp() AS lease_unexpired,"
        "latest_entry_sequence,latest_event_sequence,updated_at::text FROM pulsara_v3.sessions ORDER BY id"
    ).fetchall()))
    result["connections"] = list(map(dict, connection.execute(
        "SELECT pid,datname,usename,application_name,backend_start::text,xact_start::text,state,"
        "wait_event_type,wait_event FROM pg_stat_activity WHERE backend_type='client backend' ORDER BY pid"
    ).fetchall()))
result["data_inode"] = (home / "desktop/postgres").stat().st_ino
result["postmaster_pid"] = int((home / "desktop/postgres/postmaster.pid").read_text().splitlines()[0])
target = Path(__file__).resolve().parent / "quit-failure-observation.json"
assert not target.exists()
target.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
