"""Read-only update and reinstall observations of the authorized alpha7 build."""
import argparse
from importlib.metadata import version
import json
from pathlib import Path
import stat
import sys
from time import monotonic

import psycopg
from psycopg.rows import dict_row
import pulsara_agent
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory

parser = argparse.ArgumentParser()
parser.add_argument("stage", choices=("before", "after", "reinstalled"))
args = parser.parse_args()
home = require_pulsara_home()
assert home == Path("/home/plumwsl/.pulsara")
expected_runtime = "0.1.0-alpha.6" if args.stage == "before" else "0.1.0-alpha.7"
prefix = "/home/plumwsl/.local/share/pulsara/runtime/" + expected_runtime + "/"
assert sys.executable.startswith(prefix) and pulsara_agent.__file__.startswith(prefix)
settings_path = home / "local-settings.yaml"
settings = LocalSettingsStore(settings_path).read()
connections = [{"connection_id": item.id.value, "model_id": item.target.model_id,
                "wire_api": item.target.wire_api.value, "reasoning_wire_profile": item.reasoning_wire_profile.value,
                "credential_present": not item.requires_api_key or settings.model_api_key(item.id) is not None}
               for item in settings.model_connections]
metadata = settings_path.stat()
admin = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin"
runtime = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_runtime"
session_id = "session:r3d6okwuq2trl3ribxzct6rbuy"
with psycopg.connect(admin, row_factory=dict_row) as connection:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    identity = dict(connection.execute(
        "SELECT system_identifier::text AS system_identifier,current_setting('data_directory') AS data,"
        "current_setting('unix_socket_directories') AS socket,current_setting('listen_addresses') AS listen_addresses,"
        "pg_postmaster_start_time()::text AS postmaster_start FROM pg_control_system()"
    ).fetchone())
    assert identity["system_identifier"] == "7700523439330484737"
    assert identity["data"] == str(home / "desktop/postgres") and identity["socket"] == str(home / "desktop/socket")
    assert identity["listen_addresses"] == ""
    session = connection.execute("SELECT id,title,workspace_id,model_call_binding,lifecycle FROM pulsara_v3.sessions WHERE id=%s", (session_id,)).fetchone()
    assert session is not None
    turns = connection.execute("SELECT id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id", (session_id,)).fetchall()
    assert len(turns) == 3 and all(row["status"] == "COMPLETED" for row in turns)
    entry_counts = connection.execute("SELECT entry_kind,count(*) AS total FROM pulsara_v3.transcript_entries WHERE session_id=%s GROUP BY entry_kind ORDER BY entry_kind", (session_id,)).fetchall()
data_inode = (home / "desktop/postgres").stat().st_ino
assert data_inode == 171484
deep = PostgresRuntimeConnectionFactory(runtime).verify_deep(deadline_monotonic=monotonic() + 60)
result = {"stage": args.stage, "python": sys.executable, "package": pulsara_agent.__file__,
          "package_version": version("pulsara-agent"), "identity": identity, "data_inode": data_inode,
          "settings_stat": {"inode": metadata.st_ino, "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
                            "uid": metadata.st_uid, "mode": stat.S_IMODE(metadata.st_mode)},
          "connections": connections, "session": dict(session), "turns": list(map(dict, turns)),
          "transcript_entry_counts": list(map(dict, entry_counts)), "deep_verification": deep.result.to_dict()}
output = Path("/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/alpha7-upgrade")
target = output / f"alpha7-upgrade-{args.stage}.json"
assert not target.exists(), "preserve the original upgrade observation"
if args.stage != "before":
    before = json.loads((output / "alpha7-upgrade-before.json").read_text())
    result["preserved"] = {key: result[key] == before[key] for key in (
        "data_inode", "settings_stat", "connections", "session", "turns", "transcript_entry_counts")}
    result["same_pg_identity"] = {key: identity[key] == before["identity"][key]
                                  for key in ("system_identifier", "data", "socket", "listen_addresses")}
    result["new_postmaster_start"] = identity["postmaster_start"] != before["identity"]["postmaster_start"]
    if args.stage == "reinstalled":
        after = json.loads((output / "alpha7-upgrade-after.json").read_text())
        result["same_package_as_after"] = result["package_version"] == after["package_version"]
        result["new_postmaster_after_reinstall"] = identity["postmaster_start"] != after["identity"]["postmaster_start"]
target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
if args.stage != "before":
    assert all(result["preserved"].values()) and all(result["same_pg_identity"].values()) and result["new_postmaster_start"]
    if args.stage == "reinstalled":
        assert result["same_package_as_after"] and result["new_postmaster_after_reinstall"]
