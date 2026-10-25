"""Read-only, secret-free observations of the existing GUI's OAuth owner.

This does not construct an OAuth manager or start another authorization flow.
In-process pending/listener settlement and connection-test counts belong to the
GUI's existing API, not to this independent settings/catalog observation.
"""

import argparse
import errno
from importlib.metadata import version
import json
from pathlib import Path
import socket
import sys
from time import time
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
import pulsara_agent
from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.mcp_config import _load_raw, default_user_mcp_config_path
from pulsara_agent.mcp_credentials import owner_to_dict
from pulsara_agent.settings import LocalSettingsStore


def origin(value):
    parsed = urlsplit(value)
    # Never export an authentication query, fragment, userinfo or callback URL.
    return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("authorized", "cleared"))
    args = parser.parse_args()
    output = Path(__file__).resolve().parent
    target = output / f"alpha7-notion-{args.stage}.json"
    assert not target.exists(), "preserve the original OAuth observation"
    home = require_pulsara_home()
    assert home == Path("/home/plumwsl/.pulsara")
    prefix = "/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/"
    assert sys.executable.startswith(prefix) and pulsara_agent.__file__.startswith(prefix)
    before = json.loads((output / "alpha7-upgrade-reinstalled.json").read_text(encoding="utf-8"))
    settings = LocalSettingsStore(home / "local-settings.yaml").read()
    server_id = "notion-oauth-alpha7-smoke"
    owner = LocalMcpTarget(server_id).owner
    record = settings.mcp_authorization(owner)
    config = _load_raw(default_user_mcp_config_path()).get(server_id)
    connections = [
        {"connection_id": item.id.value, "model_id": item.target.model_id,
         "wire_api": item.target.wire_api.value,
         "reasoning_wire_profile": item.reasoning_wire_profile.value,
         "credential_present": not item.requires_api_key or settings.model_api_key(item.id) is not None}
        for item in settings.model_connections
    ]
    session_id = before["session"]["id"]
    dsn = f"host={home}/desktop/socket port=5432 dbname=pulsara user=pulsara_admin"
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        identity = dict(connection.execute(
            "SELECT system_identifier::text AS system_identifier,"
            "current_setting('data_directory') AS data,"
            "current_setting('unix_socket_directories') AS socket FROM pg_control_system()"
        ).fetchone())
        session = dict(connection.execute(
            "SELECT id,title,workspace_id,model_call_binding,lifecycle FROM pulsara_v3.sessions WHERE id=%s",
            (session_id,),
        ).fetchone())
        turns = list(map(dict, connection.execute(
            "SELECT id,status,conversation_scope_kind,scope_subagent_task_id,model_call_binding,"
            "initial_entry_id,final_entry_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY id",
            (session_id,),
        ).fetchall()))
        counts = list(map(dict, connection.execute(
            "SELECT entry_kind,count(*) AS total FROM pulsara_v3.transcript_entries "
            "WHERE session_id=%s GROUP BY entry_kind ORDER BY entry_kind", (session_id,),
        ).fetchall()))
    preserved = {"connections": connections == before["connections"],
                 "session": session == before["session"],
                 "turns": turns == before["turns"],
                 "transcript_entry_counts": counts == before["transcript_entry_counts"],
                 "pg_identity": all(identity[key] == before["identity"][key] for key in identity)}
    result = {
        "stage": args.stage, "package_version": version("pulsara-agent"),
        "owner": owner_to_dict(owner), "config_exists": config is not None,
        "config_disabled": config.get("enabled") is False if config is not None else None,
        "endpoint_matches_notion": config.get("transport", {}).get("endpoint") == "https://mcp.notion.com/mcp" if config is not None else None,
        "grant_exists": record is not None,
        "grant_metadata": None if record is None else {
            "issuer_origin": origin(record.issuer), "resource_origin": origin(record.resource_url),
            "expiry_present": record.expires_at is not None,
            "expired": record.expires_at <= time() if record.expires_at is not None else None},
        "managed_credentials_for_owner": sum(item.binding.owner == owner for item in settings.mcp_credentials),
        "connections": connections, "preserved": preserved,
        "observation_limits": "No in-process OAuth pending/listener access; no token/client serialization exported. Model comparison uses the recorded non-secret connection fields plus canonical session bindings. Settings stat may legitimately change for OAuth.",
    }
    if args.stage == "cleared":
        with socket.socket() as probe:
            probe.settimeout(1)
            callback_result = probe.connect_ex(("127.0.0.1", 17839))
        result["linux_callback_port_connect_errno"] = callback_result
        result["linux_callback_port_refused"] = callback_result == errno.ECONNREFUSED
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    assert all(preserved.values())
    if args.stage == "authorized":
        assert record is not None and result["config_disabled"] and result["endpoint_matches_notion"]
    else:
        assert record is None and config is None and result["managed_credentials_for_owner"] == 0
        assert result["linux_callback_port_refused"]


if __name__ == "__main__":
    main()
