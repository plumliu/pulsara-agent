"""Read-only alpha9 observation against the actual, previously captured alpha7 baseline."""
from datetime import datetime, timezone
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

output = Path(__file__).resolve().parent
target = output / "alpha9-upgrade-after9.json"
assert not target.exists(), "preserve the original alpha9 observation"
baseline_path = output.parent / "alpha8-upgrade/alpha8-upgrade-before.json"
recovery_path = output.parent / "alpha8-upgrade/alpha7-retry-recovery-verified.json"
before = json.loads(baseline_path.read_text(encoding="utf-8"))
recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
assert before["package_version"] == recovery["package_version"] == "0.1.0a7"
assert all(recovery["preserved"].values()) and not recovery["new_postmaster_start"]
assert version("pulsara-agent") == "0.1.0a9"
prefix = "/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.9/"
assert sys.executable.startswith(prefix) and pulsara_agent.__file__.startswith(prefix)
home = require_pulsara_home()
assert home == Path("/home/plumwsl/.pulsara")
settings_path = home / "local-settings.yaml"
settings = LocalSettingsStore(settings_path).read()
connections = [
    {"connection_id": item.id.value, "model_id": item.target.model_id,
     "wire_api": item.target.wire_api.value, "reasoning_wire_profile": item.reasoning_wire_profile.value,
     "credential_present": not item.requires_api_key or settings.model_api_key(item.id) is not None}
    for item in settings.model_connections
]
metadata = settings_path.stat()
admin = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin"
runtime = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_runtime"
session_id = before["session"]["id"]
with psycopg.connect(admin, row_factory=dict_row) as connection:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    identity = dict(connection.execute(
        "SELECT system_identifier::text AS system_identifier,current_setting('data_directory') AS data,"
        "current_setting('unix_socket_directories') AS socket,current_setting('listen_addresses') AS listen_addresses,"
        "pg_postmaster_start_time()::text AS postmaster_start FROM pg_control_system()"
    ).fetchone())
    session = dict(connection.execute("SELECT id,title,workspace_id,model_call_binding,lifecycle FROM pulsara_v3.sessions WHERE id=%s", (session_id,)).fetchone())
    turns = list(map(dict, connection.execute("SELECT id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id", (session_id,)).fetchall()))
    counts = list(map(dict, connection.execute("SELECT entry_kind,count(*) AS total FROM pulsara_v3.transcript_entries WHERE session_id=%s GROUP BY entry_kind ORDER BY entry_kind", (session_id,)).fetchall()))
deep = PostgresRuntimeConnectionFactory(runtime).verify_deep(deadline_monotonic=monotonic() + 60)
result = {
    "stage": "after9", "observed_at": datetime.now(timezone.utc).isoformat(),
    "baseline_source": str(baseline_path), "recovery_source": str(recovery_path),
    "python": sys.executable, "package": pulsara_agent.__file__, "package_version": version("pulsara-agent"),
    "identity": identity, "data_inode": (home / "desktop/postgres").stat().st_ino,
    "settings_stat": {"inode": metadata.st_ino, "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
                      "uid": metadata.st_uid, "mode": stat.S_IMODE(metadata.st_mode)},
    "connections": connections, "session": session, "turns": turns,
    "transcript_entry_counts": counts, "deep_verification": deep.result.to_dict(),
}
result["preserved"] = {key: result[key] == before[key] for key in (
    "data_inode", "settings_stat", "connections", "session", "turns", "transcript_entry_counts")}
result["same_pg_identity"] = {key: identity[key] == before["identity"][key] for key in (
    "system_identifier", "data", "socket", "listen_addresses")}
result["new_postmaster_start"] = identity["postmaster_start"] != before["identity"]["postmaster_start"]
pg_pid = int((home / "desktop/postgres/postmaster.pid").read_text().splitlines()[0])
pg_process = Path("/proc") / str(pg_pid)
result["pg_process"] = {"pid": pg_pid, "uid": pg_process.stat().st_uid,
                        "start_ticks": pg_process.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]}
result["kernel_processes"] = []
wrapper = b"import signal,runpy; signal.signal(signal.SIGHUP,signal.SIG_IGN); runpy.run_module('pulsara_agent.desktop_runtime',run_name='__main__',alter_sys=True)"
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        command = entry.joinpath("cmdline").read_bytes().split(b"\0")
        if b"pulsara_agent.desktop_runtime" not in command and wrapper not in command:
            continue
        if b"--wait-owner" in command or not command[0].decode().startswith(prefix):
            continue
        if b"PULSARA_HOME=/home/plumwsl/.pulsara" not in entry.joinpath("environ").read_bytes().split(b"\0"):
            continue
        result["kernel_processes"].append({"pid": int(entry.name), "uid": entry.stat().st_uid,
            "python": command[0].decode(), "start_ticks": entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]})
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=True))
assert all(result["preserved"].values()) and all(result["same_pg_identity"].values())
assert result["new_postmaster_start"] and result["deep_verification"]["status"] == "verified"
assert len(result["kernel_processes"]) == 1 and result["pg_process"]["uid"] == 1000
