"""Observe exact test DB/process absence, then remove only this test material."""
import json
import os
from pathlib import Path
import shutil
import psycopg

output = Path("/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/provider-acceptance")
preparation = json.loads((output / "installed-harness-preparation.json").read_text())
root = Path(preparation["directory"])
assert root.parent == Path("/home/plumwsl/workspaces") and root.name.startswith(".provider-accept-")
reports = [json.loads((output / name).read_text()) for name in (
    "terminal-real-provider.json", "terminal-short-home-real-provider.json", "terminal-input-closure-real-provider.json", "terminal-input-closure-clarified-real-provider.json", "terminal-toolchain-real-provider.json", "terminal-toolchain-explicit-node-real-provider.json")]
databases = [report["created_database"] for report in reports]
assert len(set(databases)) == len(databases) and all(name.startswith("pulsara_wsl_dogfood_") for name in databases)
assert all(report["undropped_databases"] == [] for report in reports)
homes = {str(Path(report["scratch"]) / "home") for report in reports}
homes.update(report["test_home"] for report in reports if "test_home" in report)
remaining_processes = []
inaccessible_same_uid = []
for process in Path("/proc").iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    try:
        if process.stat().st_uid != os.getuid():
            continue
        arguments = (process / "cmdline").read_bytes().split(b"\0")
        environment = (process / "environ").read_bytes().split(b"\0")
        if os.fsencode(str(root / "run_terminal_wsl.py")) in arguments or any(os.fsencode("PULSARA_HOME=" + home) in environment for home in homes):
            remaining_processes.append(int(process.name))
    except (FileNotFoundError, ProcessLookupError):
        pass
    except PermissionError:
        inaccessible_same_uid.append(int(process.name))
assert not remaining_processes
dsn = "host=/home/plumwsl/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin"
with psycopg.connect(dsn) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    system_id, data = connection.execute("SELECT system_identifier::text,current_setting('data_directory') FROM pg_control_system()").fetchone()
    assert system_id == "7700523439330484737" and data == "/home/plumwsl/.pulsara/desktop/postgres"
    remaining_databases = connection.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", (databases,)).fetchall()
assert not remaining_databases
removed = []
for report in reports:
    short_home = Path(report["test_home"]) if "test_home" in report else None
    if short_home is not None and short_home.exists():
        assert short_home.parent == Path("/home/plumwsl") and short_home.name.startswith(".pa-")
        assert short_home.resolve() == short_home and short_home.stat().st_uid == os.getuid()
        assert not list((short_home / "desktop").glob("i-*")), "a control scope directory still exists"
        shutil.rmtree(short_home)
        removed.append(str(short_home))
if root.exists():
    assert root.resolve() == root and root.stat().st_uid == os.getuid()
    assert not list(root.rglob("i-*/c")), "a retained control endpoint still exists"
    shutil.rmtree(root)
    removed.append(str(root))
facts = {"same_production_system_id": system_id, "test_databases_absent": databases,
         "remaining_exact_test_processes": remaining_processes, "inaccessible_same_uid_pids": inaccessible_same_uid,
         "removed": removed, "prepared_root_absent": not root.exists(),
         "production_pg_stopped_or_reset": False, "production_settings_written": False}
(output / "cleanup-result.json").write_text(json.dumps(facts, indent=2))
print(json.dumps(facts))
