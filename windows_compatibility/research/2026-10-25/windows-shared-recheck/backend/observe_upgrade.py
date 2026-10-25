"""Read-only installed-package upgrade snapshot; never starts app or PostgreSQL."""
import argparse
import base64
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat
import sys
from time import monotonic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("before", "after"))
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    import psycopg
    from psycopg.rows import dict_row
    import pulsara_agent
    from pulsara_agent.capability.pulsara_home import require_pulsara_home
    from pulsara_agent.llm.model_connections import model_connection_to_dict
    from pulsara_agent.settings import LocalSettingsStore
    from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory

    output = Path(__file__).resolve().parent
    target = output / f"upgrade-{args.stage}.json"
    assert not target.exists(), "preserve original observation"
    home = require_pulsara_home()
    assert home == Path("/home/plumwsl/.pulsara")
    expected = Path("/home/plumwsl/.local/share/pulsara/runtime") / args.version
    assert Path(sys.executable).is_relative_to(expected) and Path(pulsara_agent.__file__).is_relative_to(expected)
    assert version("pulsara-agent") == args.version.replace("-alpha.", "a")
    settings_path = home / "local-settings.yaml"
    settings = LocalSettingsStore(settings_path).read()
    scrub = settings.credential_scrub_set()
    metadata = settings_path.stat()
    result = {"stage": args.stage, "observed_at": datetime.now(timezone.utc).isoformat(),
              "python": sys.executable, "product": pulsara_agent.__file__, "version": version("pulsara-agent"),
              "settings_exists": True,
              "settings_stat": {"inode": metadata.st_ino, "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
                                "uid": metadata.st_uid, "gid": metadata.st_gid, "mode": stat.S_IMODE(metadata.st_mode)},
              "model_connections": [dict(model_connection_to_dict(item), credential_present=not item.requires_api_key or settings.model_api_key(item.id) is not None)
                                    for item in settings.model_connections]}
    admin = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin"
    runtime = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_runtime"
    with psycopg.connect(admin, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        result["identity"] = dict(connection.execute(
            "SELECT system_identifier::text,current_setting('data_directory') AS data,current_setting('unix_socket_directories') AS socket,"
            "current_setting('listen_addresses') AS listen_addresses,pg_postmaster_start_time()::text AS postmaster_start FROM pg_control_system()"
        ).fetchone())
        assert result["identity"]["system_identifier"] == "7700523439330484737"
        assert result["identity"]["data"] == str(home / "desktop/postgres") and result["identity"]["listen_addresses"] == ""
        result["sessions"] = list(map(dict, connection.execute(
            "SELECT id,title,workspace_id,memory_domain_id,model_call_binding,lifecycle FROM pulsara_v3.sessions ORDER BY id").fetchall()))
        result["turns"] = list(map(dict, connection.execute(
            "SELECT id,session_id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,initial_entry_id,final_entry_id "
            "FROM pulsara_v3.turns ORDER BY session_id,id").fetchall()))
        result["entries"] = list(map(dict, connection.execute(
            "SELECT e.id,e.session_id,e.turn_id,e.entry_sequence,e.entry_kind,e.conversation_scope_kind,e.content_codec,"
            "convert_from(COALESCE(e.inline_content,b.body),'UTF8') AS content FROM pulsara_v3.transcript_entries e "
            "LEFT JOIN pulsara_v3.blobs b ON b.id=e.blob_id ORDER BY e.session_id,e.entry_sequence").fetchall()))
        result["blocks"] = list(map(dict, connection.execute(
            "SELECT a.id,a.session_id,a.assistant_entry_id,a.block_ordinal,a.block_kind,a.tool_name,a.tool_arguments,"
            "a.content_codec,convert_from(COALESCE(a.inline_content,b.body),'UTF8') AS content "
            "FROM pulsara_v3.assistant_message_blocks a LEFT JOIN pulsara_v3.blobs b ON b.id=a.blob_id "
            "ORDER BY a.session_id,a.assistant_entry_id,a.block_ordinal").fetchall()))
        result["workspaces"] = list(map(dict, connection.execute(
            "SELECT memory_domain_id,id,workspace_kind,workspace_root,workspace_label FROM pulsara_v3.workspaces "
            "WHERE (memory_domain_id,id) IN (SELECT memory_domain_id,workspace_id FROM pulsara_v3.sessions) ORDER BY memory_domain_id,id").fetchall()))
    result["data_inode"] = (home / "desktop/postgres").stat().st_ino
    result["workspace_files"] = []
    for workspace in result["workspaces"]:
        directory = Path(workspace["workspace_root"])
        assert directory.is_dir() and directory.resolve() == directory
        files = []
        for base, dirs, names in os.walk(directory, followlinks=False):
            for name in sorted(dirs + names):
                path = Path(base) / name
                info = path.lstat()
                item = {"relative": str(path.relative_to(directory)), "mode": stat.S_IMODE(info.st_mode)}
                if stat.S_ISLNK(info.st_mode):
                    item.update(kind="link", target=os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    item["kind"] = "directory"
                elif stat.S_ISREG(info.st_mode):
                    raw = path.read_bytes()
                    assert not scrub.contains(raw), "workspace file contains an actual credential; refuse to export"
                    item.update(kind="file", size=len(raw), content_base64=base64.b64encode(raw).decode("ascii"))
                else:
                    item["kind"] = "other"
                files.append(item)
        result["workspace_files"].append({"workspace_id": workspace["id"], "root": str(directory), "files": sorted(files, key=lambda item: item["relative"])})
    result["deep"] = PostgresRuntimeConnectionFactory(runtime).verify_deep(deadline_monotonic=monotonic() + 60).result.to_dict()
    if args.stage == "after":
        product = Path(pulsara_agent.__file__).parent
        repository = Path("/home/plumwsl/workspaces/pulsara_agent")
        result["installed_source_equal"] = {
            relative: (product / relative).read_bytes() == (repository / "src/pulsara_agent" / relative).read_bytes()
            for relative in ("conversation_kernel/provider_dispatch.py", "hooks/executor.py", "conversation_kernel/mcp/sdk_facade.py")
        }
        assert all(result["installed_source_equal"].values())
        before = json.loads((output / "upgrade-before.json").read_text(encoding="utf-8"))
        keys = ("settings_exists", "settings_stat", "model_connections", "sessions", "turns", "entries", "blocks", "workspaces", "workspace_files", "data_inode")
        result["preserved"] = {key: result[key] == before[key] for key in keys}
        result["same_pg"] = {key: result["identity"][key] == before["identity"][key] for key in ("system_identifier", "data", "socket", "listen_addresses")}
        result["new_pg_start"] = result["identity"]["postmaster_start"] != before["identity"]["postmaster_start"]
    public = scrub.scrub_json(result)
    payload = json.dumps(public, ensure_ascii=False, indent=2)
    assert not scrub.contains(payload)
    target.write_text(payload, encoding="utf-8")
    print(json.dumps({"stage": args.stage, "version": result["version"], "system_identifier": result["identity"]["system_identifier"],
                      "sessions": len(result["sessions"]), "turns": len(result["turns"]), "entries": len(result["entries"]),
                      "workspace_files": sum(len(item["files"]) for item in result["workspace_files"]), "deep": result["deep"]["status"],
                      "preserved": result.get("preserved"), "same_pg": result.get("same_pg"), "new_pg_start": result.get("new_pg_start")}))
    if args.stage == "after":
        assert all(result["preserved"].values()) and all(result["same_pg"].values()) and result["new_pg_start"]


if __name__ == "__main__":
    main()
