"""Read-only evidence/DB audit, then exact private fixture cleanup."""
import json
import os
from pathlib import Path
import shutil
import psycopg
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

output = Path("/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/provider-acceptance/mcp-hook-execution")
scrub = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read().credential_scrub_set()
reports, audit = [], {}
for name in ("first.result.json", "bytes.result.json", "eof.result.json"):
    raw = (output / name).read_bytes()
    assert not scrub.contains(raw)
    report = json.loads(raw)
    reports.append(report)
    calls = report["provider_calls"]
    pairs = list(zip(calls, calls[1:]))
    tools = all(a["provider_input"]["tools"] == b["provider_input"]["tools"] for a, b in pairs)
    messages = all(b["provider_input"]["messages"][:len(a["provider_input"]["messages"])] == a["provider_input"]["messages"] for a, b in pairs)
    systems = [[m for m in c["provider_input"]["messages"] if m["role"] == "system"] for c in calls]
    system = all(s == systems[0] for s in systems)
    assert system and tools and messages
    assert report["undropped_databases"] == []
    audit[name] = {"status": report["status"], "provider_calls": len(calls), "system_equal": system,
                   "tools_equal": tools, "messages_append_only": messages, "configured_secret_found": False}
databases = [report["created_database"] for report in reports]
with psycopg.connect("host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin") as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    assert connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0] == "7700523439330484737"
    assert not connection.execute("SELECT datname FROM pg_database WHERE datname=ANY(%s)", (databases,)).fetchall()
root = Path("/home/plumwsl/workspaces/.provider-accept-80k6ptzi")
homes = {report["test_home"] for report in reports}
matches, inaccessible = [], []
for process in Path("/proc").iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    try:
        if process.stat().st_uid != os.getuid():
            continue
        args = (process / "cmdline").read_bytes().split(b"\0")
        env = (process / "environ").read_bytes().split(b"\0")
        if any(os.fsencode(str(root / filename)) in args for filename in ("mcp_hook_driver.py", "mcp_execution_fixture.py")) or any(os.fsencode("PULSARA_HOME=" + home) in env for home in homes):
            matches.append(int(process.name))
    except (FileNotFoundError, ProcessLookupError):
        pass
    except PermissionError:
        inaccessible.append(int(process.name))
assert not matches
removed = []
for value in homes:
    home = Path(value)
    if home.exists():
        assert home.parent == Path("/home/plumwsl") and home.name.startswith(".pa-")
        assert home.resolve() == home and home.stat().st_uid == os.getuid()
        assert not list((home / "desktop").glob("i-*"))
        shutil.rmtree(home)
        removed.append(str(home))
assert root.resolve() == root and root.stat().st_uid == os.getuid()
assert root.parent == Path("/home/plumwsl/workspaces")
shutil.rmtree(root)
removed.append(str(root))
facts = {"records": audit, "test_databases_absent": databases, "remaining_exact_fixture_processes": matches,
         "inaccessible_same_uid_pids": inaccessible, "removed": removed, "production_pg_stopped_or_reset": False}
(output / "review-cleanup.json").write_text(json.dumps(facts, indent=2))
print(json.dumps(facts))
